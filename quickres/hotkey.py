import ctypes
from ctypes import wintypes
import threading
import time

from quickres.config import call_logged, log_msg
from quickres.display import get_current_resolution, set_resolution

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_NOREPEAT = 0x4000
HOTKEY_ID = 1

HOTKEY_OPTIONS = {
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73,
    "F5": 0x74, "F6": 0x75, "F7": 0x76, "F8": 0x77,
    "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
    "Insert": 0x2D, "Delete": 0x2E, "Home": 0x24, "End": 0x23,
    "PageUp": 0x21, "PageDown": 0x22, "`": 0xC0,
}

# How long start() waits for the worker thread to reach
# _ready.set() (either after a successful RegisterHotKey or a failed one --
# either way the thread has decided) before treating the start as failed,
# and how long stop() waits for the worker thread to actually exit after
# posting WM_QUIT. Module-level (matching bridge.py's
# _HOTKEY_STOP_LOCK_TIMEOUT_S precedent) so tests can monkeypatch them down
# for speed instead of eating the real timeout.
_READY_TIMEOUT_S = 2.0
_STOP_JOIN_TIMEOUT_S = 2.0

# After start() gives up on `_ready.wait()`
# and is about to raise TimeoutError, it signals `self._cancel` so a
# merely-late-scheduled worker thread self-aborts in `_run` before it ever
# calls RegisterHotKey (see `_run`). As a backstop for the narrow race
# where the thread had already slipped past that check right at the
# timeout boundary, start() polls -- for this bounded window, at this
# interval -- for `self._thread_id` to show up so it can still reclaim the
# thread via the same signal/join sequence `stop()` uses, before control
# returns to the caller with the exception. Kept short (unlike
# `_STOP_JOIN_TIMEOUT_S`) since the common case is the cancel flag being
# honored immediately with nothing to poll for.
_CANCEL_RECLAIM_POLL_TIMEOUT_S = 0.2
_CANCEL_RECLAIM_POLL_INTERVAL_S = 0.02


class HotkeyToggle:
    def __init__(self, key_name, native_res, stretched_res, on_status):
        self.key_name = key_name
        self.vk_code = HOTKEY_OPTIONS.get(key_name, 0x75)
        self.native_res = native_res
        self.stretched_res = stretched_res
        self.on_status = on_status
        self._thread = None
        self._thread_id = None
        self._ready = threading.Event()
        # RegisterHotKey can fail (e.g.
        # another app already owns the key combo). The worker thread still
        # calls `_ready.set()` right away on that failure branch (see
        # `_run` below), so `_ready.wait()` alone can't distinguish "ready
        # because registration succeeded" from "ready because it gave up" --
        # this flag makes that distinction explicit for `start()` to check.
        self._registered = False
        # Signals a worker thread that
        # start() has already given up on (timed out) to self-abort in
        # `_run` before it registers, instead of going on to register and
        # run orphaned, disconnected from this HotkeyToggle instance. See
        # `start()`'s timeout branch and `_run`'s check before
        # RegisterHotKey.
        self._cancel = threading.Event()
        # The Win32 message-loop worker
        # thread (_run/_toggle) and the bridge dispatcher thread (bridge.py's
        # _stop_hotkey_impl, which reads `is_stretched` to decide whether to
        # revert to native before stopping) touch this flag from two
        # different threads with no synchronization -- a narrow TOCTOU
        # window between reading `is_stretched` to decide the revert-or-not
        # branch and posting WM_QUIT. `is_stretched` is exposed below as a
        # property backed by `_is_stretched` and guarded by
        # `_is_stretched_lock`, so EVERY external read (bridge.py's
        # `toggle.is_stretched`, unchanged -- no code there needs to change
        # for the guard to apply, since attribute access already routes
        # through this property regardless of caller) and every internal
        # read/write go through the same lock. `_toggle()` below holds the
        # lock for its ENTIRE body (not just the final assignment), so a
        # concurrent reader blocks until the whole
        # read-decide-set_resolution-write sequence finishes instead of
        # observing a stale value mid-flight through the slow
        # set_resolution call -- closing the race for the slow part of the
        # window. What remains is the handful of bytecodes between
        # bridge.py's now-current read and its following `toggle.stop()`
        # call, which would need another physical hotkey press to land in;
        # judged inconsequential for a single-user desktop utility.
        self._is_stretched_lock = threading.Lock()
        self._is_stretched = False
        # The resolution actually active when this listener went live, so
        # stopping can put the display back where the user left it instead of
        # inferring a target from `_is_stretched` (see `entry_res` below).
        self._entry_res = None
        self._last_applied_res = None

    @property
    def is_running(self) -> bool:
        """True while the listener thread is actually registered and
        running its Win32 message loop.

        Backed by `self._thread_id`, which `_run` only ever sets once
        `RegisterHotKey` has genuinely succeeded, and clears again the
        moment the message loop exits -- for ANY reason: a normal WM_QUIT
        from `stop()`, or a `GetMessageW` failure that kills the thread on
        its own with no `stop()` call at all. A caller with no other way to
        detect that second case (e.g. bridge.py's `self._hotkey_running`
        flag, which only start()/stop() themselves ever touch) can poll
        this property instead of trusting a flag that would otherwise stay
        stale forever.
        """
        return self._thread_id is not None

    @property
    def is_stretched(self):
        with self._is_stretched_lock:
            return self._is_stretched

    @is_stretched.setter
    def is_stretched(self, value):
        with self._is_stretched_lock:
            self._is_stretched = value

    @property
    def last_applied_res(self):
        with self._is_stretched_lock:
            return self._last_applied_res

    @property
    def entry_res(self):
        """The `(width, height)` the display was actually running when
        `start()` armed this listener, or `None` if it could not be read.

        This exists because `is_stretched` is not a safe basis for deciding
        what to restore on stop. It is an in-memory guess seeded to `False`
        and flipped once per successful toggle, so it only describes reality
        if `native_res`/`stretched_res` are labelled correctly AND the
        display was genuinely on `native_res` when the listener started.
        Neither is guaranteed: the two values come from user-editable fields
        that `webview/panel.html`'s `applyHotkeyDefaults` pre-fills from
        whatever resolution happened to be active on first launch, so a user
        who first opened QuickRes while already stretched has them the wrong
        way round in `config.json` -- and stopping then applied
        `native_res`, throwing the display to the stretched resolution the
        user had just toggled away from.

        Recording the observed entry resolution sidesteps those labels
        entirely: `webview/bridge.py`'s `_stop_hotkey_impl` restores this
        exact value, which is correct whichever way round the fields are.
        """
        return self._entry_res

    def start(self):
        """Start the listener thread and block until it has decided
        (registered the hotkey or failed to). The return value of
        `_ready.wait()` must be checked: a worker thread scheduled late
        enough to miss the timeout would otherwise let `start()` return as
        if it had succeeded -- `self._thread_id` stays `None`, so a
        `stop()` called moments later can't send WM_QUIT, leaving a live
        orphaned listener thread. Raising here (rather than silently
        returning) is the primitive bridge.py's `start_hotkey` builds on to
        detect and surface a failed start instead of reporting
        `{"running": True}` regardless.
        """
        self._ready.clear()
        self._registered = False
        self._cancel.clear()
        # Read BEFORE the listener thread can register and process a press,
        # so this is the resolution the user was on when they armed the
        # hotkey and never one this toggle applied itself. A failed read
        # leaves it None, which `_stop_hotkey_impl` treats as "nothing known
        # to restore" rather than guessing.
        self._entry_res = get_current_resolution()
        with self._is_stretched_lock:
            self._last_applied_res = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=_READY_TIMEOUT_S):
            log_msg(
                f"Hotkey {self.key_name} listener thread did not become "
                f"ready within {_READY_TIMEOUT_S}s"
            )
            # Tell a merely-late-scheduled
            # worker thread to abort before it registers (see `_run`), then
            # poll briefly in case it had already slipped past that check
            # right at the boundary and is about to register anyway -- if
            # `_thread_id` shows up, reclaim the thread the same way
            # `stop()` does before this method hands the exception back to
            # the caller, so nothing is left running orphaned.
            self._cancel.set()
            deadline = time.monotonic() + _CANCEL_RECLAIM_POLL_TIMEOUT_S
            while self._thread_id is None and time.monotonic() < deadline:
                time.sleep(_CANCEL_RECLAIM_POLL_INTERVAL_S)
            if self._thread_id is not None:
                self.stop()
            raise TimeoutError(
                f"Timed out waiting for the hotkey listener thread to start ({self.key_name})"
            )
        # `_ready` fires on BOTH the success
        # and the RegisterHotKey-failure branch of `_run` -- reaching this
        # point within the timeout only proves the thread decided, not that
        # it decided successfully. Without this check, a failed
        # RegisterHotKey (another app already owns the key combo) was never
        # surfaced: bridge.py's start_hotkey would unconditionally report
        # `{"running": True}` even though no hotkey is actually listening.
        if not self._registered:
            raise RuntimeError(
                f"Failed to register hotkey {self.key_name} -- it may already "
                "be in use by another application"
            )

    def stop(self) -> bool:
        """Signal the listener thread to quit and wait for it to actually
        exit. `PostThreadMessageW`'s return value must be checked (it can
        fail, e.g. if the target thread already exited or the thread id is
        stale), and the thread's actual exit must be verified after the
        join timeout -- otherwise a failed/lost WM_QUIT could leave an
        orphaned hotkey thread alive while the caller was told stop
        succeeded. Both failure modes are surfaced via the same
        `on_status`/`log_msg` pattern `_run` uses for its own status
        reporting, rather than being silently reported as a clean stop.
        """
        thread = self._thread
        if self._thread_id:
            if not user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0):
                msg = f"Could not signal hotkey listener thread to stop ({self.key_name})"
                self.on_status(msg)
                log_msg(
                    f"PostThreadMessageW failed for hotkey {self.key_name} "
                    f"listener thread {self._thread_id}"
                )
        if thread:
            thread.join(timeout=_STOP_JOIN_TIMEOUT_S)
            if thread.is_alive():
                msg = f"Hotkey listener thread did not stop cleanly ({self.key_name})"
                self.on_status(msg)
                log_msg(
                    f"Hotkey {self.key_name} listener thread still alive after "
                    f"{_STOP_JOIN_TIMEOUT_S}s join timeout"
                )
                # Keep the live listener's id.  Clearing it here would make
                # `is_running` lie and would prevent a later Stop attempt
                # from posting WM_QUIT to the still-running thread.
                return False

        # A failed PostThreadMessageW can mean either a stale id or a race
        # with a listener that already exited.  Once the thread is known to
        # be gone (or there was never a thread object), it is safe to clear
        # the stale state; until then, preserve it for a retry.
        self._thread_id = None
        self._registered = False
        return True

    def _toggle(self):
        # Held for the whole read + slow
        # set_resolution call + write, not just the final assignment -- see
        # the comment on `_is_stretched_lock` in __init__ for why.
        with self._is_stretched_lock:
            # Direction comes from the resolution the display is ACTUALLY on,
            # not from `_is_stretched`. Seeded to False, that flag assumes the
            # session begins on `native_res` -- so starting the hotkey while
            # already on `stretched_res` made the first press re-apply the
            # resolution that was already active: a press that visibly did
            # nothing, and left the flag inverted relative to reality for
            # every press after it. Observing the current mode instead also
            # keeps the toggle correct when something else changed the
            # resolution mid-session (a game, another tool, Windows itself).
            #
            # `_is_stretched` is still maintained below -- it continues to
            # back the `is_stretched` property other modules read -- but it is
            # now derived from the observation rather than trusted as input.
            current = get_current_resolution()
            if current is not None:
                currently_stretched = tuple(current) == tuple(self.stretched_res)
            else:
                # No reading available (EnumDisplaySettingsW failed): fall
                # back to the flag so a failed query degrades to the previous
                # behaviour instead of breaking the toggle outright.
                currently_stretched = self._is_stretched
            target = self.native_res if currently_stretched else self.stretched_res
            ok, result_msg = set_resolution(*target)
            if ok:
                self._is_stretched = not currently_stretched
                self._last_applied_res = tuple(target)
        self.on_status(result_msg)
        log_msg(f"Hotkey toggle -> {target[0]}x{target[1]} ({'ok' if ok else 'fail'})")

    def _handle_hotkey_message(self):
        """Entry point `_run`'s Win32 message loop calls on WM_HOTKEY.

        Routes `_toggle()` through `config.call_logged`, matching every
        other background/teardown callback in this codebase (the
        auto-revert timer, the window-close handler's guard resolution and
        hotkey-stop call in `webview/app.py`) -- QuickRes.spec builds with
        `console=False`, so an unguarded exception here (e.g.
        `display.set_resolution` raising unexpectedly instead of returning
        its usual `(ok, message)` pair) would otherwise propagate up
        through this message loop, kill the listener thread, and vanish
        with zero trace in quickres.log.

        Residual limitation: catching and logging the exception here stops
        the silent-crash/no-trace half of the problem, but does not by
        itself correct `webview/bridge.py`'s `self._hotkey_running` flag --
        that flag lives in a different module with no callback/state-
        sharing mechanism back from this class today, so after a caught
        exception the UI can still keep reporting the hotkey as active
        while this listener thread has in fact exited. Fixing that fully
        would mean giving `HotkeyToggle` a way to report thread death back
        to its owner (e.g. an `on_status`-style callback fired from here,
        or bridge.py polling `self._thread.is_alive()`), which reaches
        beyond this file's scope for this fix.
        """
        call_logged(self._toggle, on_error=f"Hotkey {self.key_name} toggle")

    def _run(self):
        thread_id = kernel32.GetCurrentThreadId()
        # start() sets `_cancel` right
        # before raising TimeoutError when this thread missed
        # `_READY_TIMEOUT_S`. Checking it here, right before the only
        # point of no return (RegisterHotKey), lets a thread that was
        # simply scheduled late by the OS self-abort instead of going on
        # to register and run forever, orphaned from the HotkeyToggle
        # instance that already gave up on it.
        if self._cancel.is_set():
            log_msg(
                f"Hotkey {self.key_name} listener thread aborting before "
                "registering -- start() already timed out and gave up on it"
            )
            return
        if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_NOREPEAT, self.vk_code):
            self.on_status(f"Could not register {self.key_name}, it may be in use")
            self._ready.set()
            return

        self._registered = True
        self._thread_id = thread_id
        self._ready.set()

        self.on_status(f"Hotkey active: {self.key_name} toggles resolution")
        log_msg(f"Hotkey {self.key_name} registered")

        msg = wintypes.MSG()
        while True:
            result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result == 0:
                # WM_QUIT -- normal exit, requested via stop()'s
                # PostThreadMessageW.
                break
            if result == -1:
                # GetMessageW returns -1 on
                # error (e.g. an invalid window handle), not 0 (WM_QUIT) or
                # a positive value (a normal message). A loop written as
                # `while user32.GetMessageW(...) != 0:` would treat -1
                # identically to a normal message (`-1 != 0` is True) and
                # keep looping, calling GetMessageW again forever with no
                # trace of the failure. Exit here instead, surfacing it the
                # same way `stop()` surfaces its own failure modes.
                msg_text = (
                    f"Hotkey {self.key_name} listener stopped unexpectedly "
                    "(GetMessageW error)"
                )
                self.on_status(msg_text)
                log_msg(
                    f"GetMessageW failed for hotkey {self.key_name} listener "
                    f"thread {thread_id} -- exiting message loop"
                )
                break
            if msg.message == WM_HOTKEY:
                self._handle_hotkey_message()

        user32.UnregisterHotKey(None, HOTKEY_ID)
        self._registered = False
        self._thread_id = None
