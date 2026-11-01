"""JS bridge -- pywebview `js_api` object.

Every public `Api` method is decorated with `bridge_op`, which converts
a plain return value or a raised exception into the uniform
`{ok, kind, data, message}` envelope. Method bodies MUST NOT contain their
own `try/except` -- pywebview's own call machinery already turns a raised
exception into a JS-catchable rejected promise, so app-level lock/flag
cleanup is the only thing `bridge_op` needs to own, in one `try/finally`.

Invariant, enforced by convention (not by tooling): `rg '^\\s*try\\s*:' quickres/webview/bridge.py`
must return exactly 1 match -- the one inside `bridge_op` below.
"""

import functools
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser

from quickres import __version__
from quickres import display
from quickres import monitors
from quickres import recovery
from quickres import updater
from quickres.config import detect_system_theme, log_msg
from quickres import config
from quickres import i18n
from quickres.hotkey import HOTKEY_OPTIONS, HotkeyToggle
from quickres.monitors import PendingDisableGuard

# Real domains this app actually opens via open_external: panel.html's own
# `data-external` attributes are the source of truth for both -- the panel's
# logo/title links to https://quickres.online/, and its GitHub button links
# to https://github.com/lxzydev/QuickRes. The update-check host (lxzy.my,
# quickres/__init__.py UPDATE_URL) is fetched directly via
# updater.fetch_version_info(), never routed through open_external, so it is
# deliberately NOT in this allowlist.
_EXTERNAL_URL_ALLOWED_HOSTS = {"quickres.online", "github.com"}

# Must be kept char-for-char in sync with panel.html's CUSTOM_RE (client-side
# "WxH" input parsing, ~qr-custom-apply handler) -- this is the server-side
# source of truth _coerce_wh ultimately validates against, so a drift here
# would silently accept/reject text the UI disagrees with.
_RES_TEXT_RE = re.compile(r"^(\d{2,5})\s*[x, ]\s*(\d{2,5})$")

_FAQ_KEYS = [
    ("faq_q1", "faq_a1"),
    ("faq_q2", "faq_a2"),
    ("faq_q3", "faq_a3"),
    ("faq_q4", "faq_a4"),
]

_DRIVER_PANEL_OPENERS = {
    "nvidia": lambda: display.open_nvidia_control_panel(),
    "amd": lambda: display.open_amd_software(),
    "intel": lambda: display.open_intel_graphics_software(),
}


def _ui_strings(lang: str | None = None) -> dict:
    """Bundle of translated static UI-chrome strings for the resolved
    `i18n` language. JS renders these into the static markup at boot
    instead of the hardcoded English text.

    `lang` should always be passed by callers here -- a pinned snapshot of
    the resolved language, taken once before the bundle is built -- rather
    than left to default to `i18n.t()`'s own global re-read. Without a
    pinned snapshot, a second `set_language()` call landing on another
    bridge-dispatch thread partway through this dict comprehension could
    mutate the shared `i18n` global between two of these per-key lookups,
    producing a response whose `language.resolved` disagrees with some of
    its own `strings` values.
    """
    keys = [
        "quick_resolutions_label", "custom_resolution_label", "custom_res_placeholder",
        "btn_apply", "hotkey_toggle_label", "native_label", "stretched_label",
        "btn_start_hotkey", "btn_stop_hotkey", "hotkey_state_stopped", "hotkey_state_running",
        "notice_title", "btn_faq", "btn_monitors", "btn_updates",
        "faq_window_title", "monitors_window_title", "btn_disable", "btn_enable",
        "btn_keep_disabled", "btn_revert_now", "btn_force_unlock", "revert_note",
        "dialog_res_not_found_title", "btn_nvidia_panel", "btn_amd_software",
        "btn_intel_graphics", "btn_cancel", "theme_light", "theme_dark",
        "monitor_status_enabled", "monitor_status_disabled",
        "revert_dialog_title", "btn_disable_all", "monitors_detected_count",
        "preset_kind_native", "preset_kind_stretched", "preset_kind_low",
        "boot_error_title", "boot_error_body", "btn_retry",
        "update_available_title", "update_available_body", "btn_update_now", "btn_later",
        "btn_retry_download", "update_downloading", "update_downloading_unknown",
        "update_verifying", "update_ready", "update_installing", "update_failed",
    ]
    return {key: i18n.t(key, lang=lang) for key in keys}


def _faq_bundle(lang: str | None = None) -> list:
    """Translated FAQ entries for the resolved `i18n` language -- shared by
    get_initial_state (boot) and set_language (language switch) so the
    `{"q": ..., "a": ...}` translation is built in exactly one place.

    `lang` carries the same pinned-snapshot requirement as `_ui_strings`
    above -- see its docstring.
    """
    return [{"q": i18n.t(q_key, lang=lang), "a": i18n.t(a_key, lang=lang)} for q_key, a_key in _FAQ_KEYS]


def _parse_res_text(text: str):
    match = _RES_TEXT_RE.match((text or "").strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _coerce_wh(value):
    """Accept either a [width, height] pair (ints or numeric strings) or a
    free-text "WxH" string. No try/except (bridge.py's grep gate allows
    exactly one `try:`, inside `bridge_op`) -- validated by type/shape
    checks instead of catching a conversion failure.
    """
    if isinstance(value, (list, tuple)) and len(value) == 2:
        w, h = value
        w_ok = isinstance(w, int) and not isinstance(w, bool)
        h_ok = isinstance(h, int) and not isinstance(h, bool)
        if w_ok and h_ok:
            return w, h
        if isinstance(w, str) and isinstance(h, str) and w.isdigit() and h.isdigit():
            return int(w), int(h)
        raise ValueError(f"Invalid resolution: {value!r}")
    if isinstance(value, str):
        parsed = _parse_res_text(value)
        if parsed:
            return parsed
    raise ValueError(f"Invalid resolution: {value!r}")


def _outcome_to_dict(outcome) -> dict:
    return {
        "resolution": outcome.resolution.value,
        "instance_id": outcome.instance_id,
        "friendly_name": outcome.friendly_name,
        "message": outcome.message,
        "elapsed_s": outcome.elapsed_s,
        "can_force_unlock": outcome.can_force_unlock,
    }


# How long _stop_hotkey_impl waits to acquire self._hotkey_lock when a
# start_hotkey is still in progress -- must comfortably cover
# HotkeyToggle.start()'s own up-to-2s _ready.wait(timeout=2), plus slack.
# Module-level so tests can monkeypatch it down for speed.
_HOTKEY_STOP_LOCK_TIMEOUT_S = 3.0

# How long _resolve_pending_now_bounded_under_lock waits to acquire
# self._op_lock before giving up and reporting no newly-resolved outcomes
# for that call. Short and bounded rather than unbounded (unlike
# _resolve_guard_unbounded_under_lock's timeout=None) -- this method's caller
# (recover_on_boot, reached from the unlocked get_initial_state) is
# directly re-invocable by the user (the boot-error Retry button), so an
# unbounded wait here risks freezing the UI on a lock that is only ever
# released by the user acting on a recovery dialog THIS very call is
# responsible for rendering. Module-level so tests can monkeypatch it down
# for speed.
_RESOLVE_PENDING_LOCK_TIMEOUT_S = 3.0

# How long confirm_update() waits, on a background thread, for a currently
# armed auto-revert guard (self._pending_guard) to resolve. Used twice in
# confirm_update, back to back: first to bound how long it waits on the
# background resolver thread itself (a `Thread.join`, which does not stop
# that thread if it is still legitimately running), then again to bound how
# long it waits to reacquire self._op_lock afterward (the resolver thread
# may still be holding it) -- so the total extra wait confirm_update can
# impose is capped at roughly twice this value, not an unbounded wait on
# top of an already-bounded one. Mirrors webview/app.py's own
# _GUARD_RESOLVE_CLOSE_TIMEOUT_S (same value and same best-effort intent --
# see confirm_update's docstring); it is not imported directly from app.py
# because app.py imports Api from this module, and importing app.py back
# here would create a circular import. Module-level so tests can
# monkeypatch it down for speed.
_GUARD_RESOLVE_UPDATE_TIMEOUT_S = 5.0

# How long confirm_update() waits, on a background thread, for an active
# hotkey toggle's revert-to-native attempt (Api._stop_hotkey_impl) to
# finish. app.py's own _on_closing calls _stop_hotkey_impl directly with no
# outer bound of its own, relying on that method's internal
# _HOTKEY_STOP_LOCK_TIMEOUT_S to cap the lock wait -- but the actual work
# once the lock is held (joining the listener thread, then a display-mode
# change) is not itself time-bounded there. confirm_update cannot accept
# that same open-ended risk on the update path, so this bounds the whole
# attempt the same way _GUARD_RESOLVE_UPDATE_TIMEOUT_S bounds the
# pending-guard resolve attempt just above. Module-level so tests can
# monkeypatch it down for speed.
_HOTKEY_REVERT_UPDATE_TIMEOUT_S = 5.0

# The auto-revert safety net's own revert attempt runs through
# monitors.set_monitors_enabled, which ALWAYS launches
# a fresh interactive UAC ("runas") prompt via ShellExecuteExW -- this
# codebase has no cached/reused elevation token, so every attempt needs a
# human to see and answer that secure-desktop prompt. That is exactly the
# black-screen scenario this feature exists for (see i18n.py's revert_note:
# "Use this if your screen went black") -- if the user can't see the
# prompt, it goes unanswered, _wait_for_helper times out, and a purely
# one-shot timer would never try again, leaving the monitor disabled with
# no further automatic recourse. Bounding automatic retries at
# _AUTO_REVERT_MAX_ATTEMPTS gives that prompt a few more chances to be
# noticed/answered. This does NOT solve the underlying limitation --
# elevation still requires an interactive prompt every single time, and a
# user who genuinely cannot see or reach any prompt still ends up with a
# monitor stuck disabled after the budget is exhausted, recoverable only
# through the existing manual force-unlock path. Module-level so tests can
# monkeypatch both down for speed.
_AUTO_REVERT_MAX_ATTEMPTS = 3
_AUTO_REVERT_RETRY_DELAY_S = 5.0


class _LockAcquireGuard:
    """Context manager wrapping a `Lock.acquire()` without a `try:`
    statement (bridge.py's grep gate allows exactly one `try:`, inside
    `bridge_op`). `__exit__` always releases if this guard actually
    acquired the lock, regardless of whether the `with` body raised.

    This class used to be named `_NonBlockingGuard`, but that only
    describes one of its two modes --
    its own docstring below has always admitted `timeout>0` is a real
    BLOCKING acquire, so the old name was actively misleading at every
    `timeout>0` call site (`_stop_hotkey_impl`). Renamed to describe what
    it actually does in both modes: acquire a lock, non-blocking or
    blocking depending on `timeout`.

    `timeout=0` (default) is a non-blocking acquire -- a second concurrent
    caller fails immediately (used by `start_hotkey`, where a second click
    should report busy right away). `timeout>0` is a short blocking acquire
    (used by `_stop_hotkey_impl`, so a stop racing a still-starting hotkey
    waits for start to finish instead of silently no-op'ing against a
    `_hotkey_toggle` that hasn't been set yet). `timeout=None` is an
    UNBOUNDED blocking acquire (used by `_resolve_guard_unbounded_under_lock`, whose
    two callers -- the auto-revert timer and app.py's window-close handler
    -- have no interactive caller to report "busy" to and must simply wait
    out any in-progress operation, however long it takes).

    Readability note: passing a raw `timeout=` value at the call site does
    not by itself signal which of these three qualitatively different waits
    is intended -- that distinction previously lived only in this docstring,
    not in the call-site shape. Prefer the named constructors below
    (`non_blocking`, `bounded`, `unbounded`) at every call site instead of
    constructing directly with `timeout=`; the raw parameter remains for
    internal use by those constructors and for backward compatibility.
    """

    def __init__(self, lock: threading.Lock, *, timeout: float | None = 0):
        self._lock = lock
        self._timeout = timeout
        self.acquired = False

    @classmethod
    def non_blocking(cls, lock: threading.Lock) -> "_LockAcquireGuard":
        """A second concurrent caller fails immediately instead of waiting."""
        return cls(lock, timeout=0)

    @classmethod
    def bounded(cls, lock: threading.Lock, timeout_s: float) -> "_LockAcquireGuard":
        """A short blocking acquire that gives up after `timeout_s`."""
        return cls(lock, timeout=timeout_s)

    @classmethod
    def unbounded(cls, lock: threading.Lock) -> "_LockAcquireGuard":
        """Blocks indefinitely until the lock frees, however long that takes."""
        return cls(lock, timeout=None)

    def __enter__(self):
        if self._timeout is None:
            self.acquired = self._lock.acquire(blocking=True)
        elif self._timeout:
            self.acquired = self._lock.acquire(blocking=True, timeout=self._timeout)
        else:
            self.acquired = self._lock.acquire(blocking=False)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.acquired:
            self._lock.release()
        return False


class _LockReacquireFailed(Exception):
    """Raised by a `bridge_op(lock=True)` method body that released
    `self._op_lock` partway through its own work (to let some other bounded
    operation run without holding this lock) and could not reacquire it
    again within its own bound before returning.

    This thread does not hold `self._op_lock` by the time `bridge_op`'s
    `finally` block runs, so `bridge_op` special-cases this exception: it
    skips its normal `release()` (there is nothing this thread holds to
    release -- the lock may by then be free, or legitimately held by
    whatever other operation this thread was waiting on) and reports the
    same kind="busy" envelope every other lock=True method already uses for
    "could not get the lock right now", rather than either hanging longer
    or letting the generic exception handler try to release a lock this
    thread doesn't own.
    """


class _InFlightStillPending(Exception):
    """Raised by a `bridge_op(lock=True)` method body's own "still in
    flight" precondition check (`_check_no_in_flight_pending`, and
    `force_unlock_pending`'s equivalent inline check) to report that the
    on-disk crash-recovery record still resolves to `Resolution.IN_FLIGHT`
    -- genuinely not safe to act on yet, not a bug in the method body. Every
    raise site for this exception is a pure read-then-raise with no state
    mutation beforehand, so catching it changes nothing about
    `self._op_lock`/`self._boot_armed`: they are left exactly as they were
    the moment this call started.

    `bridge_op` reports this as `kind="busy"` (the same transient-outcome
    kind every other "can't proceed right now" case already uses) rather
    than `kind="error"`, and -- for a `boot_armed_bypass` call in
    particular, where `needs_lock` is False for the whole call and nothing
    was freshly acquired -- performs no release, since there is nothing new
    to release. A later call, once the underlying operation genuinely
    resolves, reaches this same method again with `self._op_lock`/
    `self._boot_armed` untouched by the earlier rejection, so it still
    succeeds and releases normally.
    """


def bridge_op(*, lock: bool = False, boot_armed_bypass: bool = False, releases_boot_arm: bool = False):
    """Wrap a public Api method into the uniform {ok, kind, message, data} envelope.

    Methods return plain data or raise. They MUST NOT contain try/except.
    lock=True guards state-mutating monitor operations with a non-blocking
    acquire, so a second click returns kind="busy" instead of queueing.

    `self._boot_armed` (set by `recover_on_boot` alongside its direct
    `self._op_lock.acquire()` on an IN_FLIGHT outcome) has no code path that
    ever releases that lock on its own -- without an escape hatch, every
    `lock=True` method, including the intended `force_unlock_pending`
    escape hatch itself, would fail the busy-check forever. `boot_armed_bypass`
    lets a specific method skip the normal busy-check while `self._boot_armed`
    is set:
      - `releases_boot_arm=False` (recheck_pending): runs its read-only body
        WITHOUT ever releasing the lock or clearing the flag.
      - `releases_boot_arm=True` (keep_disabled/revert_now/
        force_unlock_pending -- the operations that actually resolve the
        boot-armed recovery state): runs the body, and only on successful
        completion releases the lock and clears the flag.
    `set_monitors_enabled` deliberately does NOT set `boot_armed_bypass` --
    a genuinely new operation must still see kind="busy" while a boot-armed
    recovery is unresolved.

    `boot_bypass` above is computed as `lock and boot_armed_bypass and ...`
    -- the leading `lock and`
    short-circuits the whole expression to `False` whenever `lock` is not
    also `True`, so passing `boot_armed_bypass=True` or
    `releases_boot_arm=True` without `lock=True` silently does nothing at
    runtime (no error, no bypass, just a decorator kwarg that quietly never
    took effect). Fail loudly at decoration time instead -- when
    `bridge_op(...)` itself is called to build the decorator, once per
    method definition, not per call -- so this mistake is caught at import
    time rather than discovered later as a confusing runtime no-op.
    """
    if (boot_armed_bypass or releases_boot_arm) and not lock:
        raise AssertionError(
            "bridge_op: boot_armed_bypass/releases_boot_arm require "
            "lock=True -- without it, 'lock and boot_armed_bypass and ...' "
            "silently short-circuits to a no-op"
        )

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            boot_bypass = lock and boot_armed_bypass and getattr(self, "_boot_armed", False)
            needs_lock = lock and not boot_bypass
            # The acquire() call used to live as a side effect inside this
            # `if`'s own boolean condition (`... and not
            # self._op_lock.acquire(...)`), the exact hazard this function's
            # own `boot_bypass` fix above already calls out and avoids.
            # Pulled into its own statement, only reached when a lock is
            # actually needed, so the side effect is never hidden inside a
            # condition.
            lock_acquired = True
            if needs_lock:
                lock_acquired = self._op_lock.acquire(blocking=False)
            if needs_lock and not lock_acquired:
                return {"ok": False, "kind": "busy", "data": None,
                        "message": self._lock_reason or "Another monitor operation is in progress"}
            # Tracks whether THIS thread actually holds self._op_lock at
            # any given point in this call -- `finally` below reads it to
            # decide whether its release() is safe. Starts equal to
            # `needs_lock` (true exactly when the acquire() above actually
            # succeeded and handed this thread the lock) and is only ever
            # cleared, never set back to True, by the _LockReacquireFailed
            # branch below.
            lock_owned = needs_lock
            try:
                data = fn(self, *args, **kwargs)
                if boot_bypass and releases_boot_arm:
                    self._op_lock.release()
                    self._boot_armed = False
                return {"ok": True, "kind": "ok", "data": data, "message": ""}
            except _LockReacquireFailed as exc:
                lock_owned = False
                return {"ok": False, "kind": "busy", "data": None, "message": str(exc)}
            except _InFlightStillPending as exc:
                # No state was mutated before this raise (see the exception's
                # own docstring) -- `lock_owned` is deliberately left as-is
                # so the `finally` block below still runs its normal release
                # for a plain lock=True call that freshly acquired the lock,
                # and still no-ops for a boot_armed_bypass call that never
                # acquired one to begin with.
                return {"ok": False, "kind": "busy", "data": None, "message": str(exc)}
            except Exception as exc:
                log_msg(f"{fn.__name__} failed: {exc!r}\n{traceback.format_exc()}")
                return {"ok": False, "kind": "error", "data": None,
                        "message": str(exc) or exc.__class__.__name__}
            finally:
                if needs_lock and lock_owned:
                    self._op_lock.release()
        return wrapper
    return deco


class Api:
    """pywebview `js_api` object. Public methods are `@bridge_op`-wrapped;
    method BODIES contain zero try/except -- the decorator owns all
    exception-to-envelope conversion and lock cleanup. `self._op_lock` is a
    plain `threading.Lock` (not RLock -- reentrancy would silently allow
    nested monitor ops).

    Most public methods are thin delegations into display.py/hotkey.py/
    updater.py/config.py/monitors.py/recovery.py, but this class is not
    purely a delegation surface: it also owns the crash-recovery/
    auto-revert state machine for monitor-disable operations directly, in
    a set of private helper methods (`_resolve_guard_for_enabled_ids`,
    `_build_and_save_pending_record`, `_arm_auto_revert_guard`,
    `_arm_guard_timer`, `_maybe_retry_auto_revert`, `_resolve_pending_now`,
    `_resolve_pending_now_bounded_under_lock`, `recover_on_boot`, and others below).
    That logic lives here rather than in monitors.py/recovery.py because it
    is inherently tied to this instance's own lock/timer/guard state
    (`self._op_lock`, `self._pending_guard`, `self._pending_guard_timer`),
    not because it is a thin pass-through.

    Concurrency model
    ------------------
    Five primitives coordinate concurrent access to this instance's state.
    Read this section once before touching any lock-related code here --
    the individual docstrings below on `bridge_op` and on the
    `_resolve_pending_now`/`_resolve_pending_now_bounded_under_lock`/
    `_resolve_guard_unbounded_under_lock` trio go into per-case detail, but
    this is the one place the whole picture is assembled:

    - `self._op_lock`: the main lock. `bridge_op(lock=True)` acquires it
      non-blocking around a monitor-mutating method's body, so a second
      concurrent call reports kind="busy" instead of queueing or racing.
    - `self._hotkey_lock`: guards hotkey start/stop against a second
      concurrent toggle. Independent of `self._op_lock`.
    - `self._boot_recovery_lock`: serializes concurrent `recover_on_boot`
      calls so only one thread runs the crash-recovery resolution ladder
      at a time.
    - `self._pending_guard` / `self._pending_guard_timer`: the single
      global auto-revert guard slot and its timer, tracking at most one
      in-flight pending-disable record.
    - `self._boot_armed`, together with `bridge_op`'s
      `boot_armed_bypass=`/`releases_boot_arm=` kwargs: lets one specific
      method skip the normal busy-check and later release `self._op_lock`
      on behalf of an unresolved boot-time recovery session.

    Naming convention -- read this before adding any new
    `self._op_lock`-touching helper: a method whose name ends in
    `_under_lock` ACQUIRES `self._op_lock` itself; call it from a context
    that does NOT already hold the lock. A method with no such suffix
    (e.g. `_resolve_pending_now`) instead requires the CALLER to already
    hold `self._op_lock` -- calling it without the lock held trips an
    assertion, and wrapping a call to an `_under_lock`-suffixed method in
    your own extra `with self._op_lock:` will self-deadlock, since
    `self._op_lock` is a plain, non-reentrant `threading.Lock` (see above).

    The two families this file has: for pending-state resolution, the
    caller-must-already-hold-the-lock method is `_resolve_pending_now`,
    and its acquires-the-lock-itself counterpart is
    `_resolve_pending_now_bounded_under_lock` (bounded acquire, since it is
    reached synchronously from a user-re-invocable JS call). For
    guard-state resolution the acquires-the-lock-itself method is
    `_resolve_guard_unbounded_under_lock` (unbounded acquire, since its
    callers -- a background timer and the window-close handler -- have no
    interactive caller waiting on them); its own caller-must-already-hold-
    the-lock helpers are `_arm_guard_timer` and `_maybe_retry_auto_revert`,
    reused from both `_resolve_guard_unbounded_under_lock`'s own acquire
    and the `bridge_op(lock=True)` callers that arm/retry a guard directly
    (`set_monitors_enabled`, `revert_now`).

    Every caller-must-already-hold-the-lock method named above
    (`_resolve_pending_now`, `_arm_guard_timer`, `_maybe_retry_auto_revert`)
    asserts `self._op_lock.locked()` at its own top, turning a future call
    from an unlocked context into a loud `AssertionError` at the exact call
    site instead of a silent race over the state it touches.
    """

    def __init__(self):
        self._op_lock = threading.Lock()
        self._lock_reason = None
        self._boot_armed = False
        # A dedicated lock guarding `recover_on_boot`'s
        # own idempotency cache (`self._boot_recovery_result` below), separate
        # from `self._op_lock`. It is intentionally NOT the same lock: the
        # IN_FLIGHT branch of `recover_on_boot` needs `self._op_lock` to
        # remain HELD after that method returns (so bridge_op's busy-check
        # keeps blocking other operations until the user resolves the
        # recovery UI), which rules out wrapping the whole method body in a
        # single `with self._op_lock:` block. This lock is only ever held for
        # the bounded duration of one `recover_on_boot` call (never carried
        # across a return), so a plain blocking acquire here cannot freeze
        # the app the way an unbounded `self._op_lock` wait could.
        self._boot_recovery_lock = threading.Lock()
        # Caches the FIRST completed `recover_on_boot()` call's
        # return value for the lifetime of this process. `monitors.read_op_result`
        # destructively deletes the helper result file on its first read, so
        # a second call that re-ran the whole resolution ladder after the
        # first one already consumed that file (e.g. a double-clicked/
        # retried boot-error Retry button) could land on a different --
        # sometimes worse -- outcome for the exact same still-pending target.
        # `None` means "no completed resolution cached yet"; a bounded-lock
        # timeout inside `_resolve_pending_now_bounded_under_lock` is NOT a
        # completed resolution and must never populate this cache.
        self._boot_recovery_result = None
        self._hotkey_lock = threading.Lock()
        self._hotkey_toggle = None
        self._hotkey_running = False
        self._hotkey_restore_pending = False
        # The download/verification phase of an update runs independently
        # from the UI thread.  Replacement still goes through this Api so
        # the normal monitor and hotkey safety hand-off is preserved.
        self._update_job = None
        self._pending_guard = None
        self._pending_guard_timer = None
        # Present only during the same short confirmation window as
        # _pending_guard.  It is a one-use channel to the already-elevated
        # helper, never persisted or reused after the guard settles.
        self._guarded_disable_session = None
        # How many auto-revert attempts (initial + retries) have
        # been armed for the CURRENT self._pending_guard -- see
        # _AUTO_REVERT_MAX_ATTEMPTS above and _maybe_retry_auto_revert
        # below. Reset to 1 every time a genuinely new guard is armed.
        self._pending_guard_attempt = 0
        self._gpu_vendors = None

    # -- Read-only / simple methods ------------------------------------------

    def _resolution_state(self):
        """Read the actual desktop mode and classify the quick presets.

        Kept in one helper because the panel needs the exact same snapshot at
        boot and during its lightweight external-display-change refresh.
        """
        current = display.get_current_resolution()
        current_wh = current or (display.QUICK_LIST[0][1], display.QUICK_LIST[0][2])
        presets = [
            {
                "label": label, "width": width, "height": height,
                "kind": display.classify_resolution(width, height, *current_wh),
                "aspect_ratio": display.aspect_ratio_label(width, height),
            }
            for label, width, height in display.QUICK_LIST
        ]
        return {
            "current_resolution": {"width": current[0], "height": current[1]} if current else None,
            "presets": presets,
        }

    @bridge_op()
    def get_initial_state(self):
        cfg = config.load_config()
        theme = cfg.get("theme") if cfg.get("theme") in ("dark", "light") else detect_system_theme()
        lang_setting = cfg.get("language", "auto")
        resolved_lang = i18n.resolve_language(lang_setting)
        i18n.set_language(resolved_lang)
        resolution_state = self._resolution_state()
        return {
            "theme": theme,
            "version": __version__,
            "language": {
                "setting": lang_setting,
                "resolved": resolved_lang,
                "options": i18n.LANGUAGE_NAMES,
            },
            "strings": _ui_strings(resolved_lang),
            **resolution_state,
            "hotkey": {
                "key": cfg.get("hotkey", "F6"),
                "native_res": cfg.get("native_res", ""),
                "stretched_res": cfg.get("stretched_res", ""),
                "running": self._hotkey_running,
                "restore_pending": self._hotkey_restore_pending,
            },
            "monitors": monitors.enumerate_monitors(),
            "pending": self.recover_on_boot(),
            "faq": _faq_bundle(resolved_lang),
        }

    @bridge_op()
    def get_resolution_state(self):
        """Current OS resolution for the panel's low-cost refresh loop."""
        return self._resolution_state()

    @bridge_op()
    def set_theme(self, theme):
        if theme not in ("dark", "light"):
            raise ValueError(f"Unknown theme: {theme!r}")
        config.update_config({"theme": theme})
        return {"theme": theme}

    @bridge_op()
    def set_language(self, lang):
        # `resolved` is snapshotted into a
        # local once here and threaded through _ui_strings/_faq_bundle
        # explicitly, rather than those two helpers re-reading i18n's
        # shared `_current_lang` global key-by-key. bridge_op does not
        # serialize this method against other bridge_ops (no lock=True),
        # so two rapid set_language calls on different pywebview
        # dispatch threads can interleave; without a pinned snapshot, a
        # concurrent call's `i18n.set_language(...)` write could land
        # between two of THIS call's per-key lookups and mix strings from
        # both languages into one response whose own `language.resolved`
        # then disagrees with part of its own payload. `i18n.set_language`
        # is still called so the shared global stays in sync for any other
        # code that intentionally wants "whatever language is active now"
        # (e.g. a plain `i18n.t()` call with no `lang=` pin) -- only the
        # bundle built and returned BY THIS response is pinned.
        if lang not in i18n.LANGUAGE_NAMES:
            raise ValueError(f"Unknown language: {lang!r}")
        config.update_config({"language": lang})
        resolved = i18n.resolve_language(lang)
        i18n.set_language(resolved)
        return {
            "language": {"setting": lang, "resolved": resolved, "options": i18n.LANGUAGE_NAMES},
            "strings": _ui_strings(resolved),
            "faq": _faq_bundle(resolved),
        }

    @bridge_op()
    def open_external(self, url):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in _EXTERNAL_URL_ALLOWED_HOSTS:
            raise ValueError(f"Refusing to open non-allowlisted URL: {url!r}")
        webbrowser.open(url)
        return {"opened": url}

    @bridge_op()
    def pick_resolution(self, width, height):
        # Reused verbatim by the panel's custom-resolution Apply
        # button (parses "WxH" client-side, then calls this identically to a
        # preset click) -- so a malformed pair reaching here from free text
        # must be rejected before any Win32 call, not fall through to an
        # "unsupported" vendor-dialog response.
        width, height = _coerce_wh([width, height])
        supported = display.get_supported_resolutions()
        if (width, height) not in supported:
            # display.detect_gpu_vendors() returns an EMPTY set (falsy, but
            # not None) on any failure -- a subprocess timeout, or
            # PowerShell/WMI not yet warmed up right after boot -- not only
            # when there is genuinely nothing to report. Caching that empty
            # result the same way a genuine "no vendors detected" answer
            # would be cached permanently degrades this dialog for the rest
            # of the session, even though a later attempt (this dialog is
            # shown infrequently enough that a repeated bounded subprocess
            # call is not a real cost concern) would likely succeed once
            # whatever caused the transient failure has passed. Only a
            # NON-EMPTY result is trusted and cached; `None` or empty both
            # mean "no confirmed answer yet" and retry.
            if not self._gpu_vendors:
                self._gpu_vendors = display.detect_gpu_vendors()
            vendors = sorted(self._gpu_vendors) if self._gpu_vendors else ["amd", "intel", "nvidia"]
            return {"ok": False, "reason": "unsupported", "vendors": vendors}
        ok, message = display.set_resolution(width, height)
        if not ok:
            # This is the identical underlying Win32
            # call hotkey.py's _toggle reaches (which already logs its own
            # failure) -- match that logging pattern here so the most
            # common user-facing failure mode (clicking a resolution preset
            # and it failing) leaves a trace in quickres.log instead of
            # vanishing silently.
            log_msg(f"pick_resolution -> {width}x{height} (fail): {message}")
        return {"ok": ok, "reason": None, "message": message}

    @bridge_op()
    def open_driver_panel(self, vendor):
        opener = _DRIVER_PANEL_OPENERS.get(vendor)
        if opener is None:
            raise ValueError(f"Unknown GPU vendor: {vendor!r}")
        opener()
        return {"opened": vendor}

    @bridge_op()
    def start_hotkey(self, key, native, stretched):
        # This used to silently no-op
        # (return {"running": True} without touching config/toggle/registered
        # key at all) whenever a hotkey was already running, regardless of
        # whether the caller's key/native/stretched args matched what's
        # actually live -- a success-looking envelope that quietly
        # contradicted what was just requested. panel.html's own UI never
        # triggers this: its Start button is `hidden` for the whole time
        # `state.hotkey.running` is true (only Stop is shown), so the normal
        # click flow always calls stop_hotkey before a fresh start_hotkey.
        # An identical repeat call (same key/native/stretched as what's
        # already running) stays a harmless idempotent no-op; a call that
        # asks for a genuinely different configuration while running is now
        # rejected outright instead of silently keeping the old toggle
        # alive -- the caller must call stop_hotkey first, matching the
        # sequencing the UI already relies on.
        with _LockAcquireGuard.non_blocking(self._hotkey_lock) as guard:
            if not guard.acquired:
                raise RuntimeError("A hotkey start/stop is already in progress")
            if key not in HOTKEY_OPTIONS:
                raise ValueError(f"Unknown hotkey: {key!r}")
            native_wh = _coerce_wh(native)
            stretched_wh = _coerce_wh(stretched)
            if self._hotkey_restore_pending:
                raise RuntimeError("The previous resolution still needs restoring -- call stop_hotkey first")
            if self._hotkey_running:
                toggle = self._hotkey_toggle
                same_config = (
                    toggle is not None
                    and toggle.key_name == key
                    and toggle.native_res == native_wh
                    and toggle.stretched_res == stretched_wh
                )
                if same_config:
                    return {"running": True}
                raise RuntimeError(
                    "A hotkey is already running with a different "
                    "configuration -- call stop_hotkey first"
                )
            config.update_config({
                "hotkey": key,
                "native_res": f"{native_wh[0]}x{native_wh[1]}",
                "stretched_res": f"{stretched_wh[0]}x{stretched_wh[1]}",
            })
            toggle = HotkeyToggle(
                key_name=key, native_res=native_wh, stretched_res=stretched_wh,
                on_status=log_msg,
            )
            toggle.start()
            self._hotkey_toggle = toggle
            self._hotkey_running = True
            return {"running": True}

    def _stop_hotkey_impl(self):
        """Shared revert-on-stop implementation. Called both by
        the bridge_op-wrapped `stop_hotkey` below AND directly by
        `webview/app.py`'s window-close handler, so there is exactly one
        place that reverts to native on stop -- never duplicated logic.

        Guarded by the same `self._hotkey_lock` `start_hotkey` uses, so a
        stop racing a still-starting hotkey can no longer silently no-op
        (reading `self._hotkey_toggle` as still None while start finishes
        moments later and leaves the hotkey live). Unlike `start_hotkey`'s
        non-blocking guard, this is a short blocking acquire
        (`_HOTKEY_STOP_LOCK_TIMEOUT_S`) so an ordinary stop simply waits out
        an in-progress start instead of failing outright -- this also
        preserves correctness for `webview/app.py`'s synchronous
        window-close handler, which calls this directly (not through
        `bridge_op`) and needs the revert to actually happen, not to bail
        immediately.

        A failed native-resolution revert is now raised (unlike the
        discarded result this used to be) so it isn't silently reported as
        a successful stop -- the bridge_op-wrapped `stop_hotkey` call site
        lets the decorator's existing catch-and-envelope machinery surface
        it; `_on_closing` propagates it same as any other exception during
        window teardown.

        `toggle.stop()` now runs BEFORE the restore decision and the revert
        call, not after. With the old read-then-revert-then-stop
        order, the hotkey stayed registered for the entire duration of the
        revert call: a physical press landing in that window dispatched
        WM_HOTKEY on the still-live listener thread, which ran
        `HotkeyToggle._toggle()`'s own `set_resolution` call concurrently
        with this method's unlocked revert call -- two unsynchronized
        `ChangeDisplaySettingsW` calls on two different threads, with no
        ordering guarantee over which one won. `toggle.stop()` unregisters
        the hotkey and joins the listener thread, which only exits once it
        has fully finished whatever message it was already processing
        (including an in-flight `_toggle()` call queued right before
        WM_QUIT takes effect) -- so by the time `stop()` returns, no press
        can still be in flight and no new one can be dispatched, and the
        current-resolution read that follows is both stable and safe to act
        on without racing a concurrent toggle.

        What gets restored is `HotkeyToggle.entry_res` (the resolution the
        user was on when the hotkey was armed), not `native_res` -- see
        `_restore_entry_resolution` for why the previous `is_stretched`/
        `native_res` pairing changed the resolution to the wrong one.
        """
        with _LockAcquireGuard.bounded(self._hotkey_lock, _HOTKEY_STOP_LOCK_TIMEOUT_S) as guard:
            if not guard.acquired:
                raise RuntimeError("A hotkey start/stop is already in progress")
            toggle = self._hotkey_toggle
            if toggle is not None:
                # A listener that did not acknowledge WM_QUIT is still live.
                # Keep its object and running state so the user can retry
                # Stop; discarding it would orphan the thread and permit a
                # competing Start attempt.
                if toggle.stop() is False:
                    self._hotkey_running = toggle.is_running
                    raise RuntimeError(
                        "The hotkey listener is still stopping. Try again in a moment."
                    )
                self._hotkey_running = False
                self._hotkey_restore_pending = True
                self._restore_entry_resolution(toggle)
                self._hotkey_restore_pending = False
                self._hotkey_toggle = None
            else:
                self._hotkey_running = False

    def _restore_entry_resolution(self, toggle):
        """Put the display back to the resolution the user was on when the
        hotkey was armed (`HotkeyToggle.entry_res`).

        This used to be `if toggle.is_stretched: set_resolution(*native_res)`,
        which changed the resolution on the strength of an in-memory flag
        seeded to `False` and never checked against the display. Two ways that
        went wrong, and they compounded:

        * Arming the hotkey while already on `stretched_res` made the first
          press a visible no-op and inverted the flag relative to reality (now
          fixed in `HotkeyToggle._toggle`, which reads the current mode).
        * `native_res`/`stretched_res` are user-editable fields that
          `panel.html`'s `applyHotkeyDefaults` pre-fills from whatever
          resolution is active on first launch, so a user who first opened
          QuickRes while stretched has them the wrong way round in
          `config.json`. Applying `native_res` then threw the display TO the
          stretched resolution on close -- the exact opposite of the intent.

        Restoring the observed entry resolution is correct regardless of how
        those two fields are labelled, which is why the decision is made here
        rather than from `is_stretched`.

        Per this class's naming convention (see the Concurrency model section
        of the class docstring), the absence of an `_under_lock` suffix means
        the CALLER must already hold the relevant lock -- here
        `self._hotkey_lock`, asserted below. It must also be called only
        AFTER `toggle.stop()` has returned, so no hotkey press can still be
        in flight issuing its own concurrent `ChangeDisplaySettingsW` -- see
        `_stop_hotkey_impl`'s docstring for why that ordering matters. A
        failed restore raises, so it is never reported as a clean stop.
        """
        assert self._hotkey_lock.locked()
        entry_res = toggle.entry_res
        if entry_res is None:
            return
        last_applied_res = toggle.last_applied_res
        if last_applied_res is None:
            return
        current_res = display.get_current_resolution()
        if current_res is None:
            raise RuntimeError("Could not read the current resolution -- try stopping the hotkey again")
        if tuple(current_res) == tuple(entry_res):
            return
        if tuple(current_res) != tuple(last_applied_res):
            return
        ok, message = display.set_resolution(*entry_res)
        if not ok:
            raise RuntimeError(message)

    @bridge_op()
    def stop_hotkey(self):
        self._stop_hotkey_impl()
        return {"running": False}

    @bridge_op()
    def get_hotkey_status(self):
        """Cheap read-only poll of the hotkey listener's ACTUAL liveness,
        mirroring `recheck_pending`'s role as a lightweight polling
        endpoint. `self._hotkey_running` is only ever flipped by
        `start_hotkey`/`stop_hotkey` themselves -- if the listener thread
        dies on its own (a `GetMessageW` failure inside `HotkeyToggle._run`,
        which has no automatic restart), that flag goes stale and keeps
        reporting True with nothing to correct it. panel.html polls this
        method periodically while it believes a hotkey is running, so that
        staleness gets noticed and corrected within roughly one poll
        interval instead of never.

        A non-blocking attempt at `self._hotkey_lock` avoids racing an
        in-progress start_hotkey/stop_hotkey call; when the lock is busy,
        this simply reports the current (possibly momentarily stale) flag
        without correcting it -- the next poll a few seconds later will
        pick up the settled state.
        """
        with _LockAcquireGuard.non_blocking(self._hotkey_lock) as guard:
            if not guard.acquired:
                return {"running": self._hotkey_running, "restore_pending": self._hotkey_restore_pending}
            toggle = self._hotkey_toggle
            actually_running = toggle is not None and toggle.is_running
            if self._hotkey_running and not actually_running:
                self._hotkey_running = False
                if not self._hotkey_restore_pending:
                    self._hotkey_toggle = None
            return {"running": self._hotkey_running, "restore_pending": self._hotkey_restore_pending}

    @bridge_op()
    def check_updates(self):
        if not getattr(sys, "frozen", False):
            return None
        # Real update-available detection.
        # updater.update_available() owns the version-comparison logic (and
        # the server response's field-name convention) entirely -- this just
        # bundles its verdict alongside the raw response so a separate UI
        # stream (panel.html) can render a real dialog only when warranted,
        # instead of on every successful fetch regardless of version.
        info = updater.fetch_version_info()
        return {**info, "update_available": updater.update_available(__version__, info)}

    def _prepare_update_handoff_locked(self):
        """Best-effort safety hand-off before an update exits this process.

        The caller must hold ``self._op_lock``.  This is shared by the
        legacy one-shot updater and the new download-then-install flow, so
        both paths resolve a pending monitor guard and stop an active
        stretched hotkey before the updater launches its replacement batch.
        """
        # Forwarded through so updater.apply_update's optional sha256
        # hash-verification gate can actually engage once the
        # server-side version.json response starts supplying that field --
        # the caller is expected to pass back the same dict check_updates()
        # returned. version_info=None keeps this backward-compatible with
        # any caller that only ever passes the URL.
        #
        # lock=True (like set_monitors_enabled/keep_disabled/revert_now/
        # force_unlock_pending) serializes this against self._op_lock: two
        # concurrent calls (e.g. a double-clicked "Update Now" button while
        # a slow download is in flight) both used to be able to enter
        # updater.confirm_update -> apply_update at once, racing over the
        # same fixed on-disk paths (QuickRes_new.exe, update.bat) that
        # apply_update writes/reads with no locking of its own. A second
        # concurrent call now gets kind="busy" instead -- panel.html's
        # shared call() helper already renders that as a toast (the same
        # handling every other lock=True method gets), so no UI change is
        # needed.
        #
        # updater.confirm_update can force-kill this process with
        # os._exit(0) (inside apply_update, once the download is verified
        # and staged) -- os._exit skips normal interpreter shutdown, so
        # webview/app.py's window "closing" event, and therefore its
        # _on_closing handler, never runs. _on_closing is the only other
        # place that gives a still-armed 10s auto-revert guard
        # (self._pending_guard) a bounded, best-effort chance to resolve
        # before the process disappears -- the guard's own real
        # threading.Timer is a daemon thread and dies with the process
        # without firing. A monitor disabled just before an update-triggered
        # exit would otherwise get no in-process safety net at all until the
        # relaunched app's recover_on_boot() eventually notices it.
        #
        # Give the guard that same chance here, mirroring app.py's
        # _on_closing pattern exactly: self._op_lock is released first --
        # this method already holds it via bridge_op(lock=True), and
        # _resolve_guard_unbounded_under_lock needs to acquire that same
        # lock itself (it is written for callers that do NOT already hold
        # it, like _on_closing and the real auto-revert timer); without
        # releasing first, its internal acquire could never succeed while
        # this call is still holding the lock it wants, making the resolve
        # attempt a guaranteed no-op. The resolve runs on a background
        # thread joined with a bound (_GUARD_RESOLVE_UPDATE_TIMEOUT_S, same
        # value as app.py's own bound) so a slow/UAC-blocked revert cannot
        # hang the update flow indefinitely.
        #
        # `resolver.join(...)` only bounds how long THIS thread waits for
        # the background resolver -- it does not stop that thread, which
        # can legitimately keep running (and keep holding self._op_lock)
        # well past the join bound, e.g. while waiting on an unanswered UAC
        # prompt. The reacquire right after the join is therefore bounded
        # too, by the same constant, so the total extra wait this method can
        # impose is capped at roughly twice _GUARD_RESOLVE_UPDATE_TIMEOUT_S
        # instead of however long the resolver's own revert attempt takes.
        # If that bounded reacquire also fails, this thread never got the
        # lock back, so it must not proceed into updater.confirm_update (no
        # lock protects that call in that case) and must not let bridge_op
        # try to release a lock it doesn't hold -- raising
        # _LockReacquireFailed tells bridge_op's wrapper exactly that; it
        # skips its own release() and reports kind="busy" to the caller
        # instead, matching every other lock=True method's existing
        # "couldn't get the lock" response.
        guard = self._pending_guard
        if guard is not None:
            self._op_lock.release()
            resolver = threading.Thread(
                target=lambda: config.call_logged(
                    self._resolve_guard_unbounded_under_lock, guard,
                    on_error="confirm_update: resolve_guard",
                ),
                daemon=True,
            )
            resolver.start()
            resolver.join(_GUARD_RESOLVE_UPDATE_TIMEOUT_S)
            reacquired = self._op_lock.acquire(blocking=True, timeout=_GUARD_RESOLVE_UPDATE_TIMEOUT_S)
            if not reacquired:
                raise _LockReacquireFailed(
                    "A pending auto-revert guard is still resolving -- try again shortly"
                )

        # An active hotkey toggle gets the same best-effort chance to revert
        # to native that app.py's _on_closing already gives it on ordinary
        # window-close shutdown: os._exit(0) inside updater.confirm_update
        # skips window-closing entirely, so _on_closing's own call to
        # Api._stop_hotkey_impl -- the only other place that reverts a
        # stretched display back to native -- never runs on this path. A
        # toggle still stretched at that moment has no persisted memory of
        # which resolution was active, so the physical display would stay
        # stretched after the relaunch with no automatic recovery.
        # _stop_hotkey_impl already no-ops safely once there is no toggle to
        # act on (and does nothing to the display when the toggle isn't
        # currently stretched), so this only needs to skip the thread/join
        # overhead when there is plainly nothing to revert. Unlike the
        # guard-resolve attempt above, this does not need to release/
        # reacquire self._op_lock first: _stop_hotkey_impl only ever touches
        # self._hotkey_lock, never self._op_lock, so holding self._op_lock
        # here cannot block it. It still runs on a background thread joined
        # with a bound (_HOTKEY_REVERT_UPDATE_TIMEOUT_S), mirroring the
        # guard case, so a slow/stuck revert cannot hang the update flow
        # indefinitely -- if it can't finish in time, the update proceeds
        # anyway, the same "best effort, not guaranteed" trade-off the
        # guard-resolve attempt above already accepts.
        if self._hotkey_toggle is not None:
            hotkey_reverter = threading.Thread(
                target=lambda: config.call_logged(
                    self._stop_hotkey_impl,
                    on_error="confirm_update: stop_hotkey",
                ),
                daemon=True,
            )
            hotkey_reverter.start()
            hotkey_reverter.join(_HOTKEY_REVERT_UPDATE_TIMEOUT_S)

    @bridge_op(lock=True)
    def confirm_update(self, download_url, version_info=None):
        """Backward-compatible one-shot update entry point."""
        self._prepare_update_handoff_locked()
        return updater.confirm_update(download_url, version_info=version_info)

    @bridge_op(lock=True)
    def start_update(self, download_url, version_info=None):
        """Start a background download and return its current progress."""
        job = self._update_job
        if job is not None:
            state = job.snapshot()
            if state.get("stage") in {"downloading", "verifying", "ready", "installing"}:
                return state
        job = updater.UpdateJob(download_url, version_info=version_info)
        self._update_job = job
        job.start()
        return job.snapshot()

    @bridge_op()
    def get_update_status(self):
        """Return the latest non-blocking update progress snapshot."""
        job = self._update_job
        if job is None:
            return {
                "stage": "idle",
                "downloaded_bytes": 0,
                "total_bytes": None,
                "error": None,
            }
        return job.snapshot()

    @bridge_op(lock=True)
    def install_downloaded_update(self):
        """Run the rollback-capable replacement after verification finished."""
        job = self._update_job
        if job is None or job.snapshot().get("stage") != "ready":
            raise RuntimeError("No verified update is ready to install")
        self._prepare_update_handoff_locked()
        return updater.confirm_update(
            None,
            version_info=job.version_info,
            reuse_download=True,
        )

    @bridge_op()
    def list_monitors(self):
        return monitors.enumerate_monitors()

    # -- Monitor + recovery methods (locked) ---------------------------------

    @bridge_op(lock=True)
    def set_monitors_enabled(self, instance_ids, enabled):
        # An empty instance_ids list is a legitimate
        # client input (e.g. the "Disable all" button when nothing is
        # currently enabled) -- short-circuit to a no-op success before any
        # elevation attempt (crash-recovery record write, elevated helper
        # launch) instead of prompting UAC for zero targets.
        if not instance_ids:
            return {"results": []}

        if enabled:
            # The elevated re-enable call runs FIRST, and the guard/on-disk
            # crash-recovery record are only cleared afterward, scoped to
            # whichever targets actually confirmed `ok=True`. Re-enabling
            # can genuinely fail (a declined/cancelled UAC prompt reports
            # every target as OUTCOME_GENUINE_FAILURE, and the device stays
            # in its prior disabled state) -- a target that fails this way
            # keeps its existing auto-revert guard and on-disk entry exactly
            # as if this enable attempt had never happened, so a later
            # auto-revert or crash-recovery cycle can still catch it.
            results = monitors.set_monitors_enabled(list(instance_ids), True)
            self._log_genuine_monitor_failures("enable", results)
            succeeded_ids = [iid for iid, ok, *_ in results if ok]
            if succeeded_ids:
                # A confirmed re-enable must still resolve any OTHER disable
                # operation's active auto-revert guard that covers one of
                # these ids -- see _resolve_guard_for_enabled_ids.
                self._resolve_guard_for_enabled_ids(succeeded_ids)
                self._clear_force_unlocked_targets_from_pending(succeeded_ids)
                # Neither call above touches a target whose earlier disable
                # outcome was ambiguous (a timed-out helper, or a helper
                # result that never got written -- see
                # _finalize_disable_outcome's own comment on why such a
                # target's entry is deliberately left in place): it never
                # got a live guard, and it was never force-unlocked, so both
                # of the calls above treat it as a no-op and its entry would
                # otherwise sit in the on-disk record forever, permanently
                # blocking force_unlockable and leaving a stale
                # still-disabled notice on a monitor the user already
                # re-enabled. The device's own observed state confirming
                # `ok=True` here is sufficient on its own to drop any
                # remaining record of it, independent of how its earlier
                # disable outcome resolved -- this trim is unconditional and
                # a no-op for a target already cleared by either call above.
                self._remove_targets_from_pending(succeeded_ids)
            return {"results": results}

        self._check_no_live_guard_conflict()
        self._check_no_stale_record_conflict(instance_ids)

        result_path = os.path.join(config.APP_DIR, monitors.make_result_filename())
        record = self._build_and_save_pending_record(instance_ids, result_path)
        self._guarded_disable_session = None
        results = self._delegate_disable_to_helper(instance_ids, result_path, record)
        self._finalize_disable_outcome(results)

        return {"results": results}

    def _check_no_live_guard_conflict(self):
        """self._pending_guard is a single global slot. Without this check,
        disabling monitor A arms a guard, and before it resolves a SEPARATE
        disable call for monitor B (which CAN proceed once A's own
        lock=True acquire/release cycle has completed -- the lock is not
        held across the whole 10s grace period) would silently overwrite
        self._pending_guard, destroying A's auto-revert protection with no
        trace. Refuse the new disable outright instead until the prior
        guard is confirmed (keep_disabled/revert_now) or has auto-reverted.

        Deliberately scoped to the live `self._pending_guard` only, not the
        on-disk record: a guard only ever exists for a short, deterministic
        window (the 10s auto-revert grace period after a CONFIRMED disable),
        so refusing a second disable during that window is a minor,
        bounded inconvenience. A target that instead TIMED OUT gets no
        guard at all (see `_finalize_disable_outcome`) and can stay
        unresolved indefinitely -- refusing every future disable until such
        a target resolves would let one flaky elevation prompt permanently
        block the user from disabling any other monitor. Its on-disk entry
        is protected from data loss by `_build_and_save_pending_record`'s
        merge instead, and surfaced passively via `recover_on_boot`.
        """
        if self._pending_guard is not None and not self._pending_guard.resolved:
            raise RuntimeError(
                "A previous monitor disable is still waiting for auto-revert "
                "confirmation -- keep or revert it before disabling another monitor"
            )

    @staticmethod
    def _target_helper_identity(record, target):
        """The helper identity (helper_pid, owner_pid, helper_pid_start_time)
        that applies to this ONE target dict, not the whole record.

        A record's `targets` list can hold multiple independently-originated
        disable batches merged together (`_build_and_save_pending_record`'s
        union) -- each target now carries its OWN helper identity, reflecting
        whichever batch most recently launched a helper for that specific
        target, rather than one record-wide triple that every later batch's
        write would otherwise overwrite for every target in the record.

        Falls back to the record-level fields of the same names only when
        the target itself carries no per-target `helper_pid` key at all --
        an on-disk record written before this per-target scoping existed. A
        target written under the current schema always has that key
        (possibly `None`, before its own helper has launched), so the key's
        mere presence reliably tells the two schemas apart.
        """
        if "helper_pid" in target:
            return (
                target.get("helper_pid"),
                target.get("owner_pid"),
                target.get("helper_pid_start_time"),
            )
        return (
            record.get("helper_pid"),
            record.get("owner_pid"),
            record.get("helper_pid_start_time"),
        )

    def _check_no_stale_record_conflict(self, instance_ids):
        """Refuse a new disable of a target that already has an unresolved
        entry in the on-disk crash-recovery record, unless THAT TARGET'S OWN
        helper is confirmed dead.

        `_build_and_save_pending_record` unions a new batch's targets into
        the existing on-disk record, and each target now carries its own
        helper identity (see `_target_helper_identity`) -- so without this
        check, retrying a disable for a target that's already pending (e.g.
        after a timeout) would launch a SECOND concurrent elevated helper
        racing CM_Disable_DevNode/CM_Enable_DevNode against the same device
        instance. `monitors.process_liveness` on that specific target's own
        helper identity decides: ALIVE or UNKNOWN means the original helper
        cannot be ruled out as still running, so the retry is refused; only
        a confirmed DEAD liveness makes a fresh retry against that target
        safe to proceed. Each overlapping target is checked independently,
        against its own identity -- a sibling target's helper (dead or
        alive) never decides another target's outcome.

        Scoped to targets that actually overlap `instance_ids` -- an
        unrelated pending target elsewhere in the same record is untouched
        by this check (see `TestGuardConflictCheckRemainsScopedToTheLiveGuard`
        for why an unrelated stale entry must not block other operations).
        """
        record = config.load_pending()
        if not isinstance(record, dict):
            return
        existing_targets = record.get("targets")
        if not isinstance(existing_targets, list):
            return
        ids = set(instance_ids)
        overlapping = [
            t for t in existing_targets
            if isinstance(t, dict) and t.get("instance_id") in ids
        ]
        if not overlapping:
            return
        # A target already stamped `unlocked_at` (per-target, by
        # force_unlock_pending; or, for a record written before that
        # per-target stamping existed, the legacy record-wide field) was
        # already resolved by the user's own force-unlock action. Its
        # on-disk `owner_pid` belongs to whatever process wrote it, which
        # for a genuinely stale post-crash record is never this one, so
        # `monitors.process_liveness` can only ever answer ALIVE/UNKNOWN for
        # it -- DEAD is reachable only when `owner_pid` matches the current
        # process. Blocking on that liveness would refuse the retry forever
        # with no way out; skip the liveness check for it instead and let
        # the fresh disable proceed. `_build_and_save_pending_record`
        # supersedes this target's on-disk entry with a brand new one
        # (dropping the stale `unlocked_at`) once the batch actually
        # proceeds, the same way it already unions/overwrites other
        # stale-but-legitimate record state.
        record_unlocked = bool(record.get("unlocked_at"))
        for target in overlapping:
            if record_unlocked or target.get("unlocked_at"):
                continue
            helper_pid, owner_pid, start_time = self._target_helper_identity(record, target)
            liveness = monitors.process_liveness(
                helper_pid, owner_pid, helper_pid_start_time=start_time,
            )
            if liveness != recovery.Liveness.DEAD:
                raise RuntimeError(
                    "A previous disable for this monitor may still be running "
                    f"(helper liveness: {liveness.value}) -- wait for it to "
                    "resolve before retrying"
                )

    def _resolve_guard_for_enabled_ids(self, instance_ids):
        """Manually enabling a monitor that's still
        covered by another disable operation's active 10s auto-revert guard
        used to leave that guard/timer armed against now-stale state -- it
        could later fire a redundant (at best) or conflicting (at worst)
        revert against a monitor the user already re-enabled themselves.

        This method originally cancelled the WHOLE
        guard the moment ANY of its targets overlapped `instance_ids` --
        a deliberate tradeoff at the time
        ("PendingDisableGuard only supports a single-shot resolve"). The
        side effect: enabling monitor A out of a 3-monitor batch disable
        (A, B, C) silently destroyed auto-revert protection AND the on-disk
        crash-recovery record for B and C too, even though they remained
        physically disabled. `PendingDisableGuard.remove_targets` now gives
        real partial-target resolution, so that tradeoff is no longer
        necessary: when `instance_ids` covers only a STRICT SUBSET of the
        guard's current targets, only those targets are dropped from the
        guard (and trimmed from the on-disk record) -- the guard/timer stay
        armed, still protecting whatever targets remain. Full cancellation
        (guard confirmed, timer cancelled, `self._pending_guard` cleared)
        only happens when `instance_ids` covers every remaining target.
        A no-op when there is no active guard, or the guard doesn't cover
        any of `instance_ids`.
        """
        guard = self._pending_guard
        if guard is None or guard.resolved:
            return
        guard_ids = set(guard.target_ids)
        overlap = set(instance_ids) & guard_ids
        if not overlap:
            return
        # The elevated helper was launched for the original whole batch. If
        # the user manually re-enabled even one member, let that helper exit
        # instead of allowing its fixed target list to re-enable it again at
        # timeout. Any remaining guard targets still retain the ordinary
        # retry/UAC fallback below.
        if self._guard_session_covers(guard):
            if not monitors.keep_guarded_disable(self._guarded_disable_session):
                log_msg("guarded monitor helper did not acknowledge manual-enable handoff")
            self._guarded_disable_session = None
        if guard_ids <= set(instance_ids):
            guard.confirm()
            self._pending_guard = None
            if self._pending_guard_timer is not None:
                self._pending_guard_timer.cancel()
                self._pending_guard_timer = None
            self._clear_or_trim_pending_record(guard)
        else:
            guard.remove_targets(overlap)
            self._remove_targets_from_pending(overlap)

    def _clear_force_unlocked_targets_from_pending(self, instance_ids):
        """`force_unlock_pending()` stamps the
        on-disk record's `unlocked_at` but never removes the target's own
        entry -- a force-unlocked target is, by construction, one whose
        disable never armed a live guard at all (see
        `_finalize_disable_outcome`), so `_resolve_guard_for_enabled_ids`
        above has nothing to resolve for it. Manually enabling that monitor
        afterward left its stale "unlocked, unconfirmed" entry on disk for
        the rest of the session, with `recover_on_boot`/`recheck_pending`
        continuing to surface it as still pending. When a target has been
        force-unlocked, trim its entry here too if it overlaps
        `instance_ids` -- independent of whether a guard exists.

        `unlocked_at` is stamped per-target (see
        `force_unlock_pending`), not record-wide -- a target only counts as
        force-unlocked here when ITS OWN `unlocked_at` is set, or (for
        backward compatibility with an already-on-disk record written
        before this fix) when the legacy record-level field is set, which
        still applies to every target in the record.
        """
        record = config.load_pending()
        if not isinstance(record, dict):
            return
        existing_targets = record.get("targets")
        if not isinstance(existing_targets, list):
            return
        record_unlocked = bool(record.get("unlocked_at"))
        unlocked_ids = {
            t.get("instance_id") for t in existing_targets
            if isinstance(t, dict) and t.get("instance_id")
            and (record_unlocked or t.get("unlocked_at"))
        }
        overlap = unlocked_ids & set(instance_ids)
        if overlap:
            self._remove_targets_from_pending(overlap)

    def _clear_or_trim_pending_record(self, guard, succeeded_ids=None):
        """Every call site that resolves a guard used
        to call `config.clear_pending()` unconditionally, destroying the
        ENTIRE on-disk pending_restore.json record even when the guard's own
        `target_ids` is a strict subset of the record's full multi-monitor
        'targets' list (e.g. a 3-monitor batch disable where 2 confirmed and
        got a guard while 1 timed out and is still
        legitimately pending crash-recovery -- all three recorded in the
        SAME record). Only the guard's own targets are removed from the
        on-disk record here; if other targets remain, the trimmed record is
        re-saved instead of deleting the file outright. Only deletes the
        file when nothing remains, matching the pre-existing single-target
        behavior. Shared by every call site that resolves a guard
        (`keep_disabled`/`revert_now`/`_resolve_guard_unbounded_under_lock`/
        `_resolve_guard_for_enabled_ids` above) so this logic lives in
        exactly one place instead of being duplicated four times.

        `succeeded_ids`: when a caller actually
        attempted a per-target revert and knows the real per-id outcome
        (`_resolve_guard_unbounded_under_lock` reading `guard.last_results`,
        `revert_now` reading its own `set_monitors_enabled` results), pass
        the ids that genuinely succeeded (`ok=True`) here -- a target whose
        revert genuinely failed keeps its crash-recovery entry instead of
        being trimmed on the strength of the OTHER targets' success.
        Defaults to every one of `guard.target_ids` when omitted, which is
        correct for callers that resolve the guard WITHOUT any actual
        per-id revert outcome to check at all (`keep_disabled` keeping the
        disable as-is, `_resolve_guard_for_enabled_ids` cancelling because
        the user manually enabled a covered monitor).
        """
        self._remove_targets_from_pending(
            guard.target_ids if succeeded_ids is None else succeeded_ids
        )

    def _remove_targets_from_pending(self, target_ids):
        """Trim `target_ids` out of the on-disk pending_restore.json
        record's `targets` list, re-saving the trimmed record if any
        targets remain, deleting the file outright once none do. No-op if
        there is no on-disk record at all.

        The single shared implementation behind `_clear_or_trim_pending_record`
        (guard resolution) and `_finalize_disable_outcome`'s own trim of a
        genuinely-failed target: a target that outright fails -- not times
        out -- while mixed into the same batch as a confirmed success used
        to never get trimmed at all, since it never becomes part of any
        guard's `target_ids` and so no guard resolution would ever remove
        it either.
        """
        record = config.load_pending()
        if record is None:
            return
        ids = set(target_ids)
        # isinstance guard (matching `_check_no_stale_record_conflict`,
        # `_clear_force_unlocked_targets_from_pending`, and
        # `_pending_target_ids_from_disk`'s identical checks above): a
        # non-dict entry -- a partially-written record from a crash
        # mid-write, an older schema, external tampering/AV interference --
        # carries no instance_id to key on, so it is dropped along with
        # whatever else got trimmed rather than crashing this method with
        # an AttributeError on `.get`.
        remaining_targets = [
            t for t in record.get("targets", [])
            if isinstance(t, dict) and t.get("instance_id") not in ids
        ]
        if remaining_targets:
            config.save_pending({**record, "targets": remaining_targets})
        else:
            config.clear_pending()

    def _build_and_save_pending_record(self, instance_ids, result_path):
        """Persist the crash-recovery pending record BEFORE elevation starts
        (crash-before-launch protection) -- raises if the write itself
        fails, refusing to disable rather than proceeding with no recovery
        record on disk.

        `targets` is a UNION with whatever record is already on disk, keyed
        by instance_id, rather than an outright overwrite: an earlier
        disable batch can still have a genuinely unresolved target on disk
        (a timeout never arms `self._pending_guard` -- see
        `_check_no_live_guard_conflict` -- so that check alone can't
        prevent a second, unrelated disable from reaching this method), and
        that entry must survive this new batch's write instead of being
        silently destroyed -- including its own helper identity, untouched
        by this batch (see `_target_helper_identity`). Only the target(s) in
        `instance_ids` (this batch's own) get a fresh per-target helper
        identity here (`helper_pid`/`helper_pid_start_time` `None` until
        `_save_helper_pid` fills them in once the helper actually launches).
        The record-level fields of the same names (`result_file`,
        `helper_pid`, `owner_pid`, `started_at`) describe the batch actually
        launching now and intentionally replace whatever the prior record
        held for them, same as before -- they remain in place for whatever
        else already reads them, but are no longer the source of truth for a
        given target's own liveness (see `_check_no_stale_record_conflict`).
        """
        known = {m["instance_id"]: m["friendly_name"] for m in monitors.enumerate_monitors()}
        targets_by_id = {}
        existing = config.load_pending()
        existing_targets = existing.get("targets") if isinstance(existing, dict) else None
        if isinstance(existing_targets, list):
            for target in existing_targets:
                if isinstance(target, dict):
                    existing_id = target.get("instance_id")
                    if existing_id:
                        targets_by_id[existing_id] = target
        for iid in instance_ids:
            targets_by_id[iid] = {
                "instance_id": iid,
                "friendly_name": known.get(iid, ""),
                "helper_pid": None,
                "helper_pid_start_time": None,
                "owner_pid": os.getpid(),
            }
        record = {
            "action": "disable",
            "targets": list(targets_by_id.values()),
            "result_file": result_path,
            "helper_pid": None,
            "helper_pid_start_time": None,
            "owner_pid": os.getpid(),
            "started_at": time.time(),
            "unlocked_at": None,
        }
        if not config.save_pending(record):
            raise RuntimeError(
                "Could not write the crash-recovery record -- refusing to disable"
            )
        return record

    def _save_helper_pid(self, record, pid, instance_ids):
        """Capture the helper's real PID as early as possible, closing the
        crash-recovery blind window as tightly as practical -- `record`
        already exists from `_build_and_save_pending_record`'s pre-launch
        save, so this is a follow-up save updating it in place.

        Also captures the helper process's start time so a later liveness
        check can detect Windows reusing this PID for an unrelated process
        instead of trusting a bare "the PID number exists" probe.

        Updates this helper identity in TWO places: the record-level
        fields (kept for whatever else already reads them, e.g.
        `_resolve_pending_now`), and the per-target entry of every target in
        `instance_ids` -- this batch's own targets -- so a target from an
        earlier, still-merged batch keeps ITS OWN helper identity untouched
        (see `_target_helper_identity`) instead of being silently
        reattributed to this helper.
        """
        start_time = monitors.get_process_start_time(pid)
        owner_pid = os.getpid()
        ids = set(instance_ids)
        updated_targets = [
            {**target, "helper_pid": pid, "helper_pid_start_time": start_time, "owner_pid": owner_pid}
            if isinstance(target, dict) and target.get("instance_id") in ids
            else target
            for target in record.get("targets", [])
        ]
        config.save_pending({
            **record,
            "targets": updated_targets,
            "helper_pid": pid,
            "helper_pid_start_time": start_time,
        })

    def _delegate_disable_to_helper(self, instance_ids, result_path, record):
        """Disable via one bounded elevated helper, then retain its session."""
        with monitors.guarded_disable_session(timeout_s=10.0) as guard_context:
            results = monitors.set_monitors_enabled(
                list(instance_ids), False, result_path=result_path,
                on_helper_launched=lambda pid: self._save_helper_pid(record, pid, instance_ids),
            )
        self._guarded_disable_session = guard_context.session
        return results

    def _finalize_disable_outcome(self, results):
        """Arm the auto-revert guard+timer for a confirmed disable, trim any
        genuinely-failed target out of the on-disk crash-recovery record, or
        clear it entirely when nothing in the batch is left pending.

        Each `results` entry is `(instance_id, ok, message, kind)` --
        `monitors.set_monitors_enabled`'s own return shape. `kind` is one of
        `monitors.OUTCOME_CONFIRMED` / `OUTCOME_GENUINE_FAILURE` /
        `OUTCOME_AMBIGUOUS`, decided by monitors.py right where each per-target
        outcome is produced, and is read directly here -- `message` stays
        display/logging text only and is never used to infer control flow.

        A `kind` of `OUTCOME_AMBIGUOUS` (a timeout, a helper result that
        never got persisted, or a helper/observed-state mismatch) means the
        elevated helper may still complete the disable moments later in the
        background, or the true device state simply couldn't be confirmed
        either way -- that target's record entry is left in place so a
        future `recover_on_boot` can still surface the unresolved state.
        Only `OUTCOME_GENUINE_FAILURE` is trimmed here, including when mixed
        into the same batch as a confirmed success.
        """
        confirmed_ids = [iid for iid, ok, _, _ in results if ok]
        failed_ids = [
            iid for iid, ok, _, kind in results
            if not ok and kind == monitors.OUTCOME_GENUINE_FAILURE
        ]
        if failed_ids:
            failed_set = set(failed_ids)
            self._log_genuine_monitor_failures(
                "disable", [r for r in results if r[0] in failed_set]
            )
            self._remove_targets_from_pending(failed_ids)
        if confirmed_ids:
            self._arm_auto_revert_guard(confirmed_ids)
        # else: any remaining ambiguous target(s) are deliberately left
        # untouched in the on-disk record -- recover_on_boot must still be
        # able to surface them on next launch.

    def _log_genuine_monitor_failures(self, action, results):
        """Write a quickres.log trace for genuine per-target enable/disable
        failures -- a cancelled/declined UAC prompt and an elevated-helper
        crash both surface here as an ordinary `ok=False` entry with a
        descriptive message. Mirrors `pick_resolution`'s existing log_msg
        precedent (the same underlying observability gap: in a
        console=False packaged build, quickres.log is the only channel a
        user/maintainer investigating a "monitor won't disable/enable"
        report has). A no-op when `results` has no failing entries, so a
        fully successful call leaves no trace.

        Accepts either the 3-element `(instance_id, ok, message)` or the
        4-element `(instance_id, ok, message, kind)` shape -- the trailing
        `*_` discards whatever comes after `message` -- so both call sites
        (the raw results straight from `monitors.set_monitors_enabled`, and
        `_finalize_disable_outcome`'s own filtered subset of them) work
        unchanged.
        """
        failures = [(iid, msg) for iid, ok, msg, *_ in results if not ok]
        if not failures:
            return
        detail = "; ".join(f"{iid}: {msg}" for iid, msg in failures)
        log_msg(f"set_monitors_enabled({action}): {len(failures)} target(s) failed: {detail}")

    def _guard_session_covers(self, guard, session=None):
        session = self._guarded_disable_session if session is None else session
        return bool(
            session is not None
            and set(guard.target_ids).issubset(session.instance_ids)
        )

    def _revert_guarded_session_or_elevate(self, guard, instance_ids):
        """Use the one active helper first; fall back safely if it vanished."""
        session = self._guarded_disable_session
        if self._guard_session_covers(guard, session):
            results = monitors.revert_guarded_disable(session)
            # Each helper accepts exactly one terminal command.  A missing
            # completion is treated as unavailable rather than guessed;
            # the normal elevation path remains the safety fallback.
            self._guarded_disable_session = None
            if results is not None:
                return results
            log_msg("guarded monitor helper did not return a revert result; falling back to elevation")
        return monitors.set_monitors_enabled(instance_ids, True)

    def _arm_auto_revert_guard(self, confirmed_ids):
        guard = PendingDisableGuard(
            armed_at=time.time(), target_ids=confirmed_ids,
            revert_fn=lambda ids: self._revert_guarded_session_or_elevate(guard, ids),
        )
        self._pending_guard = guard
        self._pending_guard_attempt = 1
        self._arm_guard_timer(guard, guard.timeout_s)

    def _arm_guard_timer(self, guard, delay_s):
        """Shared timer-arming primitive behind both the initial 10s grace
        period (`_arm_auto_revert_guard`) and each bounded automatic retry
        (`_maybe_retry_auto_revert`) -- one place that starts the real
        `threading.Timer`, so both call sites stay identical in how they
        cancel a stale timer first and mark the new one daemon.

        A Python-side timer (not client-side JS) is required since it must
        fire even if the webview window isn't focused/rendering -- exactly
        the black-screen scenario this exists for. `_resolve_guard_unbounded_under_lock`
        is a safe no-op if `keep_disabled`/`revert_now` already confirmed
        the guard, and any previous timer is cancelled so it never
        leaks/fires a redundant revert across multiple disable operations.
        It runs through the same method webview/app.py's window-close
        handler calls directly, so a disable that's still in its grace
        period when the user closes the window gets resolved synchronously
        instead of dying with this daemon thread.

        Reused from call sites that hold `self._op_lock` through different
        mechanisms -- `_arm_auto_revert_guard` (called from
        `set_monitors_enabled`'s `bridge_op(lock=True)` acquire) and
        `_maybe_retry_auto_revert` (called both from `revert_now`'s own
        `bridge_op(lock=True)` acquire and from
        `_resolve_guard_unbounded_under_lock`'s own `with` block) -- exactly
        like `_resolve_pending_now`'s reuse across `recheck_pending`/
        `force_unlock_pending` and `_resolve_pending_now_bounded_under_lock`.
        This method itself never acquires `self._op_lock`; it assumes the
        caller already holds it, the same "caller-must-already-hold-the-
        lock" precondition `_resolve_pending_now` documents and asserts.
        """
        assert self._op_lock.locked(), (
            "_arm_guard_timer: self._op_lock must already be held by the "
            "caller -- this method does not acquire it itself"
        )
        if self._pending_guard_timer is not None:
            self._pending_guard_timer.cancel()
        # Keep the guard's own is_expired()/check() schedule in lockstep
        # with the real timer being (re-)armed right below -- without this,
        # a guard whose FIRST deadline already passed once (the standard
        # 10s grace period, or an earlier bounded retry) would report
        # is_expired() as permanently True from then on, since it only knew
        # its ORIGINAL armed_at/timeout_s from construction. Any caller
        # that resolves the guard between two scheduled attempts with no
        # `source_timer` of its own to check against -- webview/app.py's
        # window-close handler and confirm_update's background resolver
        # both call `_resolve_guard_unbounded_under_lock` this way -- would
        # then fire an out-of-schedule live elevation attempt well ahead of
        # this exact timer's real `delay_s`. Calling `rearm` here, exactly
        # once per real timer (re-)arm, makes "expired" mean "the next
        # legitimate attempt -- the one this timer represents -- is
        # actually due" instead of "has ever been due once".
        guard.rearm(now=time.time(), delay_s=delay_s)
        # Matches webview/app.py's window-close
        # handler, which already wraps its own
        # `api._resolve_guard_unbounded_under_lock(guard)` call in
        # `config.call_logged` for the exact same reason -- this daemon
        # thread has no caller above it to see a raised exception, and
        # `_resolve_guard_unbounded_under_lock` only shields `guard.check()`
        # itself internally, leaving whatever runs after it (e.g.
        # `_clear_or_trim_pending_record`) unguarded. Without this, an
        # exception there would vanish into Python's default
        # `threading.excepthook`, unreachable in the console=False packaged
        # build, leaving the guard permanently unresolved with no trace.
        # The callback closes over `timer`, a name this same function
        # assigns just below -- Python resolves closures at call time, not
        # definition time, so by the time this fires (after `delay_s` has
        # elapsed) `timer` already refers to the real threading.Timer
        # object created here. Threading it through as `source_timer` lets
        # _resolve_guard_unbounded_under_lock detect, once it finally
        # acquires self._op_lock, whether THIS exact timer is still the one
        # tracked as self._pending_guard_timer -- see that method's
        # docstring for the stale-timer race this closes.
        timer = threading.Timer(
            delay_s,
            lambda: config.call_logged(
                self._resolve_guard_unbounded_under_lock, guard,
                on_error="auto-revert timer: resolve_guard", source_timer=timer,
            ),
        )
        timer.daemon = True
        timer.start()
        self._pending_guard_timer = timer

    def _maybe_retry_auto_revert(self, guard):
        """Called after a `guard.check()`
        call actually attempted a revert (`triggered`) but did not fully
        resolve the guard -- i.e. the elevated UAC prompt this attempt
        depended on was never answered (missed/ignored on a black screen)
        or the revert genuinely failed. See the residual-limitation comment
        above `_AUTO_REVERT_MAX_ATTEMPTS` for why this cannot fully solve
        the underlying "elevation needs an interactive prompt" problem --
        this only bounds how many extra chances that prompt gets.

        Also called directly by `revert_now` for the same reason after its
        own manual, immediate re-enable attempt reports at least one
        target that did not genuinely succeed -- a user-triggered revert
        attempt gets the exact same bounded retry budget as a timer-
        triggered one instead of losing the guard's protection outright.

        No-op (leaves the guard exactly as-is, pending/unresolved) when:
        - the guard already fully resolved (nothing left to retry);
        - `self._pending_guard` no longer points at this exact guard object
          (superseded by a newer disable batch, or already torn down) --
          never resurrects a stale/replaced guard's timer;
        - the attempt budget (`_AUTO_REVERT_MAX_ATTEMPTS`) is exhausted.

        In every no-op case above, the guard is left pending/unresolved and
        the on-disk crash-recovery record survives untouched -- the existing
        manual force-unlock path (`force_unlock_pending`) remains the way
        out, same as before this retry behavior existed.

        Like `_arm_guard_timer` (which this method calls into on the retry
        path), this requires `self._op_lock` already held by the caller --
        it never acquires it itself.
        """
        assert self._op_lock.locked(), (
            "_maybe_retry_auto_revert: self._op_lock must already be held "
            "by the caller -- this method does not acquire it itself"
        )
        if guard.resolved:
            return
        if self._pending_guard is not guard:
            return
        if self._pending_guard_attempt >= _AUTO_REVERT_MAX_ATTEMPTS:
            # The bounded retry budget is exhausted and the guard is still
            # unresolved -- the manual force-unlock path is the only way
            # out from here (see this method's docstring above). Log it so
            # this outcome leaves a trace instead of the guard simply going
            # quiet in a console=False packaged build.
            log_msg(
                "auto-revert retry budget exhausted "
                f"({_AUTO_REVERT_MAX_ATTEMPTS} attempts) for targets "
                f"{', '.join(guard.target_ids)} -- still disabled, manual "
                "force-unlock required"
            )
            return
        self._pending_guard_attempt += 1
        self._arm_guard_timer(guard, _AUTO_REVERT_RETRY_DELAY_S)

    def _check_no_in_flight_pending(self):
        """Refuse to act on the on-disk crash-recovery record while it still
        resolves to `Resolution.IN_FLIGHT` -- the same hazard
        `force_unlock_pending` already refuses via an identical
        `_resolve_pending_now()` + `any(... == Resolution.IN_FLIGHT ...)`
        check. IN_FLIGHT means the record's owner_pid no longer matches this
        process (a crash/relaunch) while its helper's liveness cannot be
        confirmed dead within the expiry window -- the independent elevated
        helper process may still genuinely be running its own device-node
        call. Acting on the record now (a fresh re-enable in `revert_now`,
        or simply discarding it in `keep_disabled`) risks a lost-update race
        against that still-running helper.

        Only called when `self._pending_guard is None` (the boot/crash-
        recovered path -- see `keep_disabled`/`revert_now` below): a live
        in-process guard's own record always has `owner_pid == os.getpid()`
        for THIS process, so `monitors.process_liveness` can only report
        DEAD or ALIVE for it, never the UNKNOWN that IN_FLIGHT depends on --
        this check would never fire for that path anyway, but scoping it to
        `guard is None` keeps the intent explicit and matches exactly the
        boot-armed hazard the finding describes.

        Returns the resolved `outcomes` list so `keep_disabled`'s
        guard-is-None branch can scope its own on-disk trimming to the
        specific targets it actually resolved, instead of re-deriving them
        with a second `_resolve_pending_now()` call.
        """
        outcomes = self._resolve_pending_now()
        if any(o.resolution == recovery.Resolution.IN_FLIGHT for o in outcomes):
            raise _InFlightStillPending(
                "Cannot revert/keep-disabled while a monitor operation is "
                "still in flight"
            )
        return outcomes

    @bridge_op(lock=True, boot_armed_bypass=True, releases_boot_arm=True)
    def keep_disabled(self):
        guard = self._pending_guard
        outcomes = None
        if guard is None:
            outcomes = self._check_no_in_flight_pending()
        if guard is not None:
            if self._guard_session_covers(guard):
                if not monitors.keep_guarded_disable(self._guarded_disable_session):
                    raise RuntimeError(
                        "The temporary elevated helper did not confirm the keep action"
                    )
                self._guarded_disable_session = None
            guard.confirm()
            self._pending_guard = None
        if self._pending_guard_timer is not None:
            self._pending_guard_timer.cancel()
            self._pending_guard_timer = None
        # Trim only this guard's own targets from the
        # on-disk record rather than destroying it whole -- see
        # _clear_or_trim_pending_record. `guard` is None for the boot-armed
        # crash-recovery path (no live guard object exists yet in this
        # process). This used to blanket-clear the whole on-disk record for
        # that path on the assumption that everything in it was covered by
        # THIS click -- but the record is a union across possibly multiple
        # disable batches (see _build_and_save_pending_record), so an
        # UNCONFIRMABLE sibling target with no live guard of its own (its
        # on-disk entry is its ONLY safety net) could still be sitting in
        # the very same record. `_trim_settled_targets_from_pending` below
        # mirrors revert_now's per-target-scoped trimming instead: only the
        # targets this resolution actually settled are removed.
        if guard is not None:
            self._clear_or_trim_pending_record(guard)
        else:
            self._trim_settled_targets_from_pending(outcomes)
        return {"kept": True}

    def _trim_settled_targets_from_pending(self, outcomes):
        """Boot/crash-recovered `keep_disabled`'s per-target-scoped trim
        (mirrors `revert_now`'s equivalent guard-is-None handling): a target
        whose resolution is `UNCONFIRMABLE` still needs the on-disk record
        as its only safety net (it can only be released later via
        `force_unlock_pending`), so it is left untouched here. Every other
        resolved target (confirmed disabled, cleared, failed, or already
        force-unlocked) has nothing further to track and is trimmed.
        """
        settled_ids = [
            o.instance_id for o in (outcomes or [])
            if o.resolution != recovery.Resolution.UNCONFIRMABLE
        ]
        if settled_ids:
            self._remove_targets_from_pending(settled_ids)

    def _pending_target_ids_from_disk(self):
        """Read the crash-recovery record's own target instance ids straight
        off disk -- the same `record["targets"]` source `_resolve_pending_now`
        and `_check_no_stale_record_conflict` already read.

        Used by `revert_now` whenever `self._pending_guard` is `None`: a
        live guard object only ever exists in THIS process, for the short
        window between a disable this same process just confirmed and that
        disable's own 10s grace period resolving. Every boot/crash-recovered
        pending state (surfaced via `recover_on_boot`/`recheck_pending`) has
        no such guard, so target ids must come from the on-disk record
        instead of being treated as empty.
        """
        record = config.load_pending()
        if not isinstance(record, dict):
            return []
        targets = record.get("targets")
        if not isinstance(targets, list):
            return []
        return [
            t.get("instance_id") for t in targets
            if isinstance(t, dict) and t.get("instance_id")
        ]

    @bridge_op(lock=True, boot_armed_bypass=True, releases_boot_arm=True)
    def revert_now(self):
        guard = self._pending_guard
        if guard is None:
            self._check_no_in_flight_pending()
        # No live guard object exists for the boot/crash-recovered case
        # (recover_on_boot/recheck_pending never construct one) -- read the
        # real target ids from the on-disk record instead of silently
        # treating "no live guard" as "nothing to revert". Without this, the
        # elevated re-enable call below never runs for that path at all,
        # while the record itself still gets destroyed a few lines down.
        target_ids = guard.target_ids if guard is not None else self._pending_target_ids_from_disk()
        # The existing timer is cancelled up front regardless of outcome --
        # nothing should fire a redundant revert while this manual attempt
        # is in progress. The guard itself, however, is deliberately left
        # alone here: it is only confirmed/torn down further down, once the
        # actual re-enable outcome below is known. A guard destroyed before
        # that outcome is known would give a missed/declined UAC prompt on
        # this manual click zero further chances, unlike the exact same
        # failure on the automatic timer path, which already gets a bounded
        # number of extra chances via `_maybe_retry_auto_revert`.
        if self._pending_guard_timer is not None:
            self._pending_guard_timer.cancel()
            self._pending_guard_timer = None
        results = (
            self._revert_guarded_session_or_elevate(guard, target_ids)
            if guard is not None and target_ids
            else (monitors.set_monitors_enabled(target_ids, True) if target_ids else [])
        )
        # This used to trim/clear the record unconditionally on the
        # strength of "set_monitors_enabled didn't raise" -- but its return
        # value can (and does, for expected failure modes) report ok=False
        # per target. Only a target whose revert genuinely succeeded gets
        # trimmed; a target that reports ok=False here (still disabled)
        # keeps its crash-recovery entry, live guard or not.
        succeeded_ids = [iid for iid, ok, _, _ in results if ok]
        if guard is not None:
            if set(guard.target_ids) <= set(succeeded_ids):
                guard.confirm()
                self._pending_guard = None
                self._guarded_disable_session = None
                self._clear_or_trim_pending_record(guard, succeeded_ids=succeeded_ids)
            else:
                # At least one of this guard's targets did not genuinely
                # re-enable. Trim only the targets that did, and hand the
                # rest to the same bounded auto-retry budget the automatic
                # path already uses -- a manual click's own failure is no
                # different from the timer's own failure at this point.
                guard.remove_targets(succeeded_ids)
                self._remove_targets_from_pending(succeeded_ids)
                self._maybe_retry_auto_revert(guard)
        elif target_ids:
            self._remove_targets_from_pending(succeeded_ids)
        else:
            # Genuinely nothing on disk to act on (no guard, no record) --
            # a defensive clear in case a malformed/empty record is present.
            config.clear_pending()
        return {"results": results}

    @bridge_op(lock=True, boot_armed_bypass=True, releases_boot_arm=False)
    def recheck_pending(self):
        outcomes = self._resolve_pending_now()
        return {
            "outcomes": [_outcome_to_dict(o) for o in outcomes],
            "force_unlockable": recovery.force_unlockable(outcomes),
            "guard_remaining_s": (
                self._pending_guard.remaining_s(time.time()) if self._pending_guard else None
            ),
        }

    @bridge_op(lock=True, boot_armed_bypass=True, releases_boot_arm=True)
    def force_unlock_pending(self):
        outcomes = self._resolve_pending_now()
        if any(o.resolution == recovery.Resolution.IN_FLIGHT for o in outcomes):
            raise _InFlightStillPending("Cannot force-unlock while a monitor operation is still in flight")
        if not recovery.force_unlockable(outcomes):
            raise RuntimeError("Nothing to force-unlock")

        # `unlocked_at` used to be stamped on the
        # WHOLE on-disk record, applying to every target in it -- not only
        # the ones that are actually UNCONFIRMABLE. In a mixed-batch record
        # (one target already resolved, another still UNCONFIRMABLE), that
        # mislabeled the resolved sibling as "Unlocked, outcome unconfirmed"
        # forever, since recovery.resolve_pending()'s unlocked_at check
        # applied record-wide. Stamped per-target now (only on the target(s)
        # `outcome.can_force_unlock` actually flags), mirroring the same
        # per-target scoping already applied elsewhere in this file
        # (_clear_or_trim_pending_record / _remove_targets_from_pending).
        unlockable_ids = {o.instance_id for o in outcomes if o.can_force_unlock}
        record = config.load_pending()
        if record is not None and unlockable_ids:
            now = time.time()
            targets = record.get("targets")
            if isinstance(targets, list):
                for target in targets:
                    if isinstance(target, dict) and target.get("instance_id") in unlockable_ids:
                        target["unlocked_at"] = now
            config.save_pending(record)
            # The `outcomes` computed above are the PRE-stamp resolution --
            # returning them as-is would hand the caller a stale snapshot
            # (e.g. still "unconfirmable") even though the on-disk record was
            # just updated to reflect the unlock. panel.html renders its
            # notice banner directly from this method's return value, so
            # recompute against the just-written record before returning.
            outcomes = self._resolve_pending_now()

        return {"outcomes": [_outcome_to_dict(o) for o in outcomes]}

    # -- Boot-time / shared crash-recovery composition -----------------------

    def _resolve_pending_now(self):
        """Shared crash-recovery ladder composition, reused by
        `recover_on_boot` (via `_resolve_pending_now_bounded_under_lock`),
        `recheck_pending`, and `force_unlock_pending` -- one code path,
        always the same load-bearing order: liveness is sampled
        strictly BEFORE the result file is read, so a helper that dies and
        writes its result between the two probes is never scored
        DEAD-with-no-result and pushed prematurely to UNCONFIRMABLE.

        This is the raw, lock-free implementation. `recheck_pending` and
        `force_unlock_pending` call it directly and rely on their own
        `bridge_op(lock=True)` wrapping (or the carried-over boot-armed
        lock hold when `boot_armed_bypass` skips a fresh acquire) already
        holding `self._op_lock` by the time their body runs -- `self._op_lock`
        is a plain, non-reentrant `Lock` (see the class docstring), so this
        method must NOT itself try to acquire it, or those two callers would
        self-deadlock. `recover_on_boot` is the one caller that does NOT
        already hold the lock (it runs from the unlocked `get_initial_state`),
        so it goes through `_resolve_pending_now_bounded_under_lock` instead of
        calling this directly.

        The "must already hold the lock"
        precondition above used to live only in this prose, with nothing in
        the code enforcing it -- a future caller reaching this plainer-named
        method directly from a context that does not already hold
        `self._op_lock` would silently reintroduce the exact
        concurrent-result-file-read race this method's own load-bearing
        ordering exists to avoid. This assertion turns that misuse into a
        loud failure at the exact call site instead of a silent race.
        """
        assert self._op_lock.locked(), (
            "_resolve_pending_now: self._op_lock must already be held by "
            "the caller -- this method is not itself lock-safe (see "
            "docstring); use _resolve_pending_now_bounded_under_lock from "
            "an unlocked context instead"
        )
        raw = config.load_pending()
        mtime = config.pending_mtime()
        record = recovery.normalize_pending(raw, mtime=mtime if mtime is not None else time.time())
        if record is None:
            if raw is not None:
                config.clear_pending()
            return []

        owner_pid = record.get("owner_pid")
        helper_pid = record.get("helper_pid")
        liveness = monitors.process_liveness(
            helper_pid, owner_pid,
            helper_pid_start_time=record.get("helper_pid_start_time"),
        )

        result_file = record.get("result_file")
        helper_data = None
        if result_file and recovery.is_safe_result_path(result_file, config.APP_DIR):
            helper_data = monitors.read_op_result(result_file, config.APP_DIR)
        helper_results = {}
        if helper_data:
            for entry in helper_data.get("results", []):
                entry_id = entry.get("instance_id")
                if entry_id:
                    helper_results[entry_id] = (bool(entry.get("ok", False)), entry.get("message", ""))

        target_ids = [t["instance_id"] for t in record["targets"]]
        device_states = monitors.sample_device_states(target_ids)

        return recovery.resolve_pending(
            record, now=time.time(), liveness=liveness,
            helper_results=helper_results, device_states=device_states,
        )

    def _resolve_pending_now_bounded_under_lock(self):
        """Bounded-blocking wrapper around `_resolve_pending_now` for the
        one caller (`recover_on_boot`, reached from the unlocked
        `get_initial_state`) that does not already hold `self._op_lock`.

        Named with a `_bounded_` marker (as opposed to
        `_resolve_guard_unbounded_under_lock`'s `_unbounded_` marker) so the
        acquire semantics are visible from the method name itself, not only
        from its docstring -- a future `self._op_lock`-touching helper
        following this "_X_under_lock" convention should carry the same
        explicit marker.

        `get_initial_state` is directly
        re-callable by the user (the boot-error Retry button
        re-invokes `boot()` -> `get_initial_state()`), so two concurrent
        calls -- a double-click, or a Retry racing an in-flight automatic
        boot call -- can both reach `_resolve_pending_now` at once. Both
        read the same on-disk `pending_restore.json` record and the same
        helper result file, and `monitors.read_op_result` DELETES that file
        on read -- a second concurrent reader arriving right after the
        first one's delete would silently see nothing, corrupting the
        crash-recovery resolution for that boot/check cycle.

        Uses a SHORT bounded blocking acquire (`_RESOLVE_PENDING_LOCK_TIMEOUT_S`),
        not the unbounded `timeout=None` pattern `_resolve_guard_unbounded_under_lock`
        uses -- that method's callers (a background timer, a window-close
        handler) have no interactive caller waiting on it, but this one is
        reached synchronously from a user-re-invocable JS call. If the lock
        is instead held long-term by an outstanding boot-armed recovery
        session (only released once the user acts on the recovery UI that
        THIS very call is responsible for rendering), waiting unboundedly
        here would freeze the app. On timeout, this returns `None` rather
        than reading/mutating anything without the lock -- a safe no-op,
        not a corrupted read; the next boot/retry picks up the real state
        once the lock frees.

        `None` (timed out, nothing was read) is
        a DIFFERENT return value from `[]` (the lock was acquired and the
        resolution genuinely produced no outcomes, e.g. no pending record on
        disk at all) -- `recover_on_boot` needs to tell these two apart so it
        never caches a timeout as if it were a completed resolution.
        """
        with _LockAcquireGuard.bounded(self._op_lock, _RESOLVE_PENDING_LOCK_TIMEOUT_S) as guard:
            if not guard.acquired:
                return None
            return self._resolve_pending_now()

    def recover_on_boot(self):
        """Boot-time crash-recovery composition. Called from
        `get_initial_state()`, since that IS the app's single startup hook
        the JS side already calls before any render
        (`boot() = S = await call('get_initial_state')`), rather than a
        separate webview/app.py startup function.

        Only an IN_FLIGHT outcome re-arms `self._op_lock`; every
        other outcome is notice-only. A Python `Lock` may be released by a
        thread other than the one that acquired it, so re-arming here is a
        direct `acquire()`, not going through `bridge_op(lock=True)`.

        Reads pending state through `_resolve_pending_now_bounded_under_lock`
        (not the raw `_resolve_pending_now`) so two concurrent calls to this
        method -- reached from the unlocked `get_initial_state`, directly
        re-invocable via the boot-error Retry button -- are serialized
        instead of racing each other over the same on-disk record and
        result file.

        That serialization only protected ACCESS to
        the on-disk record/result file -- it did not make a second call
        (running strictly AFTER a first, already-completed call) compute the
        SAME resolution. `monitors.read_op_result` destructively deletes the
        helper result file on its first read, so a second call that re-ran
        this whole ladder against a record the first call intentionally left
        on disk (a mixed outcome set that still needs manual force-unlock)
        would read an empty result set and could land on a worse/different
        outcome for a target the first call already resolved confidently --
        the boot-error Retry button has no debounce, so a double-click is
        enough to trigger this. Fixed by making this method idempotent
        within the process lifetime: the first completed resolution is
        cached on `self._boot_recovery_result` and every later call simply
        returns that cache instead of re-deriving one from by-then-consumed
        signals. `self._boot_recovery_lock` (see `__init__`) serializes the
        cache check, the resolution, and the eventual `config.clear_pending()`
        call against each other, so no call can read/set the cache or clear
        the on-disk record while another call is still mid-resolution. A
        bounded-lock timeout (`_resolve_pending_now_bounded_under_lock`
        returning `None`) is deliberately never cached -- it is not a
        completed resolution, so a later call must still retry once
        `self._op_lock` frees.
        """
        with self._boot_recovery_lock:
            if self._boot_recovery_result is not None:
                return dict(self._boot_recovery_result)

            outcomes = self._resolve_pending_now_bounded_under_lock()
            if outcomes is None:
                return {"outcomes": [], "force_unlockable": False}
            if not outcomes:
                result = {"outcomes": [], "force_unlockable": False}
                self._boot_recovery_result = result
                return dict(result)

            if any(o.resolution == recovery.Resolution.IN_FLIGHT for o in outcomes):
                # Every OTHER lock-acquisition site in
                # this file checks acquire()'s return value -- this was the
                # one exception. Setting self._boot_armed = True
                # unconditionally used to assume this acquire always
                # succeeds; if the lock was (unexpectedly) already held by
                # something else at this exact moment, the flag would still
                # be set as if this path genuinely held the lock,
                # undermining bridge_op's boot_armed_bypass invariant (which
                # assumes _boot_armed implies a lock this path actually
                # holds). Only set the flag on a genuine acquire; log the
                # anomaly otherwise so it leaves a trace instead of silently
                # corrupting the busy-check bypass logic.
                lock_acquired = self._op_lock.acquire(blocking=False)
                if lock_acquired:
                    self._lock_reason = "A monitor operation from a previous session is still resolving"
                    # Flag the re-armed lock as boot-armed so bridge_op's
                    # boot_armed_bypass escape hatches (recheck_pending/keep_disabled/
                    # revert_now/force_unlock_pending) know to bypass the busy-check
                    # instead of failing forever -- nothing else ever releases this
                    # lock otherwise.
                    self._boot_armed = True
                else:
                    log_msg(
                        "recover_on_boot: could not acquire self._op_lock for an "
                        "IN_FLIGHT outcome -- lock unexpectedly already held; "
                        "leaving _boot_armed=False"
                    )
                result = {"outcomes": [_outcome_to_dict(o) for o in outcomes], "force_unlockable": False}
                self._boot_recovery_result = result
                return dict(result)

            force_unlockable = recovery.force_unlockable(outcomes)
            if not force_unlockable:
                config.clear_pending()
            result = {
                "outcomes": [_outcome_to_dict(o) for o in outcomes],
                "force_unlockable": force_unlockable,
            }
            self._boot_recovery_result = result
            return dict(result)

    def _resolve_guard_unbounded_under_lock(self, guard, now: float | None = None, source_timer=None):
        """Resolve a pending-disable guard's auto-revert check while
        ACQUIRING `self._op_lock` itself -- a real BLOCKING acquire, unlike
        bridge_op's normal non-blocking pattern, since neither caller of
        this method has a user waiting on immediate feedback; both simply
        need to wait out any in-progress operation first, then proceed.

        Named with an `_unbounded_` marker (as opposed to
        `_resolve_pending_now_bounded_under_lock`'s `_bounded_` marker) so
        the acquire semantics are visible from the method name itself, not
        only from its docstring.

        `source_timer`, when given, is the exact `threading.Timer` instance
        whose expiry called into this method (`_arm_guard_timer` passes
        itself this way). It exists to close a real race: `Timer.cancel()`
        is a no-op once the real timer thread has already passed
        `Timer.run()`'s own one-time "am I cancelled" check and started
        calling its function -- including while that call is merely
        blocked here waiting for `self._op_lock`. If some other caller
        (typically `revert_now`'s own partial-failure retry branch) wins
        the lock first and supersedes this timer -- arming a fresh one via
        `_arm_guard_timer`, or tearing it down entirely once the guard
        fully resolves -- `self._pending_guard_timer` no longer `is` this
        `source_timer` by the time this call finally gets the lock. Once
        that happens, re-running `guard.check()` here would fire a second,
        entirely unrequested real revert attempt (a second UAC prompt) and
        could arm a redundant third timer via `_maybe_retry_auto_revert` for
        what is really still the same logical retry cycle. `source_timer`
        left as `None` (the default, used by `webview/app.py`'s
        window-close handler and `confirm_update`'s background resolver --
        neither is tied to a specific timer instance) skips this check
        entirely, matching this method's behavior before `source_timer`
        existed.

        Shared by two callers that must never run this concurrently with a
        `bridge_op(lock=True)`-guarded method's body (keep_disabled/
        revert_now/force_unlock_pending, which hold this same lock):
        - the real `threading.Timer` armed in `set_monitors_enabled` on a
          confirmed disable, so the 10s auto-revert can never race a
          concurrent keep_disabled/revert_now right at the boundary and
          leave the crash-recovery record and the actual device state
          disagreeing;
        - webview/app.py's window-close handler, so a disable still in its
          grace period gets resolved synchronously before the process
          exits, instead of dying with this method's daemon-thread caller.

        Routed through `config.call_logged` (not a local try/except --
        bridge.py's own invariant of exactly one `try:` statement, the one
        inside `bridge_op`, must stay intact) so a failure in this
        background/teardown path still leaves a trace in quickres.log
        instead of vanishing silently under QuickRes.spec's console=False
        build.

        `call_logged` returns `None` instead of
        re-raising if `revert_fn` raised, so `triggered` below is falsy
        whenever the revert attempt itself raised -- the pending record
        deliberately survives in that case so a future `recover_on_boot`
        can still surface the unresolved state instead of it being silently
        lost.

        A raised exception must be eligible
        for the same bounded retry budget (`_maybe_retry_auto_revert`) as a
        normal `ok=False` failure -- `triggered` being falsy here is
        ambiguous between "not expired / already resolved yet, nothing to
        retry" and "expired, attempted, and the attempt raised". Recomputing
        `guard.is_expired(now)` disambiguates: it is only True once `check()`
        has actually passed its own expiry gate, whether the attempt that
        followed raised, returned a partial failure, or fully resolved the
        guard. Retrying is therefore driven by "expired and still
        unresolved" rather than solely by `triggered`, so an exception on
        the very first attempt no longer silently defeats attempts 2 and 3.

        `triggered` (guard.check()'s return value)
        only means "a revert attempt was made this call" -- NOT "it
        succeeded" (see monitors.PendingDisableGuard.check's own docstring;
        the real `revert_fn` deliberately never raises for an expected
        per-target failure, it reports `ok=False` results instead). So a
        genuine per-target outcome must be read from `guard.last_results`
        and only the ids that actually succeeded (`ok=True`) get trimmed --
        a target that failed keeps its crash-recovery entry rather than
        being trimmed just because SOME target in the same guard succeeded
        or because `revert_fn` happened not to raise.

        The lock acquire/release used to be two
        bare statements around this body with no `try/finally` -- this
        method sits OUTSIDE `bridge_op`'s own try/finally machinery (it is
        not itself `bridge_op`-wrapped), so any exception raised by
        `_clear_or_trim_pending_record` after a successful `guard.check()`
        permanently leaked `self._op_lock`, locking every monitor operation
        out until the app restarted. `_LockAcquireGuard` (already used
        elsewhere in this file for `self._hotkey_lock`) is reused here via
        its `unbounded()` constructor for the same unbounded-blocking
        semantics the old code had, but its `__exit__` unconditionally
        releases regardless of
        whether the `with` body raised -- without needing a second literal
        `try:` statement, which would break this file's own single-`try:`
        grep gate.
        """
        now = time.time() if now is None else now
        with _LockAcquireGuard.unbounded(self._op_lock):
            # Stale-timer guard -- see this method's `source_timer`
            # docstring section above. Only a timer-sourced call is subject
            # to this check; skip it entirely when source_timer is None.
            if source_timer is not None and self._pending_guard_timer is not source_timer:
                return
            triggered = config.call_logged(
                guard.check, now, on_error="pending-guard auto-revert check"
            )
            if triggered:
                succeeded_ids = [iid for iid, ok, _, _ in (guard.last_results or []) if ok]
                self._clear_or_trim_pending_record(guard, succeeded_ids=succeeded_ids)
            # Retry eligibility is driven by "expired and still
            # unresolved", not solely by `triggered` -- see this method's
            # docstring above. This also covers the case `triggered` doesn't:
            # `guard.check` raised and `call_logged` swallowed it into a
            # falsy `None`, which is otherwise indistinguishable here from
            # "not expired yet, nothing to do".
            if not guard.resolved and guard.is_expired(now):
                # An expired check that didn't fully resolve the
                # guard means this attempt's revert was incomplete (missed
                # UAC prompt, partial/total failure, or a raised exception)
                # -- see _maybe_retry_auto_revert's own docstring for the
                # bounded retry policy and its residual limitation.
                self._maybe_retry_auto_revert(guard)
