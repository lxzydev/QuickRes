import threading

import pytest

import quickres.hotkey as hotkey_mod
from quickres.hotkey import HotkeyToggle


def test_toggle_does_not_flip_is_stretched_when_set_resolution_fails(monkeypatch):
    monkeypatch.setattr(hotkey_mod, "set_resolution", lambda *a, **kw: (False, "failed to switch"))
    statuses = []
    toggle = HotkeyToggle(
        "F9",
        native_res=(1920, 1080),
        stretched_res=(1280, 1024),
        on_status=statuses.append,
    )

    toggle._toggle()

    assert toggle.is_stretched is False
    assert statuses[-1] == "failed to switch"


def test_toggle_flips_is_stretched_when_set_resolution_succeeds(monkeypatch):
    monkeypatch.setattr(hotkey_mod, "get_current_resolution", lambda: (1920, 1080))
    monkeypatch.setattr(hotkey_mod, "set_resolution", lambda *a, **kw: (True, "ok"))
    toggle = HotkeyToggle(
        "F9",
        native_res=(1920, 1080),
        stretched_res=(1280, 1024),
        on_status=lambda m: None,
    )

    toggle._toggle()

    assert toggle.is_stretched is True


def test_toggle_from_stretched_back_to_native_does_not_flip_on_failure(monkeypatch):
    monkeypatch.setattr(hotkey_mod, "set_resolution", lambda *a, **kw: (False, "failed to switch"))
    toggle = HotkeyToggle(
        "F9",
        native_res=(1920, 1080),
        stretched_res=(1280, 1024),
        on_status=lambda m: None,
    )
    toggle.is_stretched = True

    toggle._toggle()

    assert toggle.is_stretched is True


def test_start_raises_when_ready_wait_times_out(monkeypatch):
    """Round 3 (Stream E, finding 1): a worker thread that never reaches
    `_ready.set()` within the timeout must surface a failure from start(),
    not return as if it had succeeded.
    """
    monkeypatch.setattr(hotkey_mod, "_READY_TIMEOUT_S", 0.05)
    release = threading.Event()

    def slow_run(self):
        # Simulates a listener thread scheduled too late to register within
        # the window -- it never calls self._ready.set() until released.
        release.wait(timeout=2)

    monkeypatch.setattr(HotkeyToggle, "_run", slow_run)
    toggle = HotkeyToggle(
        "F9", native_res=(1920, 1080), stretched_res=(1280, 1024),
        on_status=lambda m: None,
    )

    try:
        with pytest.raises(TimeoutError):
            toggle.start()
    finally:
        release.set()
        if toggle._thread:
            toggle._thread.join(timeout=1)


def test_start_raises_when_register_hotkey_fails_even_though_ready_fires_promptly(monkeypatch):
    """Round 3 (Stream A, finding 2): RegisterHotKey can fail (e.g. another
    app already owns that key combo). The worker thread still calls
    `_ready.set()` right away on that failure branch, so `_ready.wait()`
    returns True well within the timeout -- Stream E's timeout fix (above)
    does not by itself catch this case. start() must detect the
    registration failure and raise, instead of returning as if the hotkey
    is now live.
    """
    monkeypatch.setattr(hotkey_mod.user32, "RegisterHotKey", lambda *a, **kw: 0)
    statuses = []
    toggle = HotkeyToggle(
        "F9", native_res=(1920, 1080), stretched_res=(1280, 1024),
        on_status=statuses.append,
    )

    with pytest.raises(RuntimeError):
        toggle.start()

    assert toggle._thread_id is None
    if toggle._thread:
        toggle._thread.join(timeout=1)


def test_start_succeeds_and_marks_registered_when_register_hotkey_succeeds(monkeypatch):
    monkeypatch.setattr(hotkey_mod.user32, "RegisterHotKey", lambda *a, **kw: 1)
    monkeypatch.setattr(hotkey_mod.user32, "GetMessageW", lambda *a, **kw: 0)  # exit loop immediately
    toggle = HotkeyToggle(
        "F9", native_res=(1920, 1080), stretched_res=(1280, 1024),
        on_status=lambda m: None,
    )

    toggle.start()  # must not raise

    assert toggle._registered is True
    toggle._thread.join(timeout=1)


def test_stop_reports_status_when_post_thread_message_fails(monkeypatch):
    """Round 3 (Stream E, finding 2): a failed PostThreadMessageW (e.g. the
    target thread already exited or the id is stale) must be surfaced via
    on_status/log_msg, not silently treated as a clean stop.
    """
    monkeypatch.setattr(hotkey_mod.user32, "PostThreadMessageW", lambda *a, **kw: 0)
    statuses = []
    toggle = HotkeyToggle(
        "F9", native_res=(1920, 1080), stretched_res=(1280, 1024),
        on_status=statuses.append,
    )
    toggle._thread_id = 999999
    toggle._thread = None

    assert toggle.stop() is True

    assert statuses, "expected a status message reporting the failed signal"
    assert "stop" in statuses[-1].lower() or "signal" in statuses[-1].lower()
    assert toggle._thread_id is None


def test_stop_reports_status_when_thread_does_not_exit_within_join_timeout(monkeypatch):
    """Round 3 (Stream E, finding 2): if the listener thread is still alive
    after the join timeout (WM_QUIT lost/ignored), stop() must surface that
    rather than silently reporting success regardless.
    """
    monkeypatch.setattr(hotkey_mod, "_STOP_JOIN_TIMEOUT_S", 0.05)
    monkeypatch.setattr(hotkey_mod.user32, "PostThreadMessageW", lambda *a, **kw: 1)
    statuses = []
    toggle = HotkeyToggle(
        "F9", native_res=(1920, 1080), stretched_res=(1280, 1024),
        on_status=statuses.append,
    )
    toggle._thread_id = 999999
    block = threading.Event()
    toggle._thread = threading.Thread(target=lambda: block.wait(timeout=2), daemon=True)
    toggle._thread.start()

    try:
        assert toggle.stop() is False
        assert statuses, "expected a status message reporting the orphaned thread"
        assert "stop" in statuses[-1].lower() or "alive" in statuses[-1].lower()
        assert toggle._thread_id == 999999, "a live listener must remain addressable for retry"
    finally:
        block.set()
        toggle._thread.join(timeout=1)


def test_stop_keeps_thread_id_when_post_fails_but_listener_is_still_alive(monkeypatch):
    """A failed WM_QUIT post is not proof that a live listener disappeared.

    Keep its id so a later Stop can retry rather than orphaning the thread
    and letting bridge.py start a competing listener.
    """
    monkeypatch.setattr(hotkey_mod, "_STOP_JOIN_TIMEOUT_S", 0.05)
    monkeypatch.setattr(hotkey_mod.user32, "PostThreadMessageW", lambda *a, **kw: 0)
    toggle = HotkeyToggle(
        "F9", native_res=(1920, 1080), stretched_res=(1280, 1024),
        on_status=lambda m: None,
    )
    toggle._thread_id = 4242
    toggle._registered = True
    release = threading.Event()
    toggle._thread = threading.Thread(target=lambda: release.wait(timeout=2), daemon=True)
    toggle._thread.start()

    try:
        assert toggle.stop() is False
        assert toggle._thread_id == 4242
        assert toggle._registered is True
        assert toggle.is_running is True
    finally:
        release.set()
        toggle._thread.join(timeout=1)


def test_start_timeout_cancels_late_thread_before_it_registers(monkeypatch):
    """Round 4 (Stream C, finding 1): a worker thread that is merely
    scheduled late by the OS (hasn't even reached the RegisterHotKey call
    yet) must never go on to register orphaned from the HotkeyToggle
    instance that already gave up and raised TimeoutError. The
    cancellation check `_run` performs right before RegisterHotKey must
    see start()'s signal and self-abort instead of proceeding.
    """
    monkeypatch.setattr(hotkey_mod, "_READY_TIMEOUT_S", 0.05)
    release = threading.Event()
    register_calls = []

    def slow_get_thread_id():
        # Simulates the OS not scheduling this thread until well after
        # start() has already given up and raised.
        release.wait(timeout=2)
        return 4242

    def spy_register(*a, **kw):
        register_calls.append((a, kw))
        return 1

    monkeypatch.setattr(hotkey_mod.kernel32, "GetCurrentThreadId", slow_get_thread_id)
    monkeypatch.setattr(hotkey_mod.user32, "RegisterHotKey", spy_register)
    monkeypatch.setattr(hotkey_mod.user32, "GetMessageW", lambda *a, **kw: 0)
    toggle = HotkeyToggle(
        "F9", native_res=(1920, 1080), stretched_res=(1280, 1024),
        on_status=lambda m: None,
    )

    with pytest.raises(TimeoutError):
        toggle.start()

    # start() has already given up and raised -- release the worker thread
    # now, simulating it finally getting CPU time after the deadline.
    release.set()
    toggle._thread.join(timeout=2)

    assert not toggle._thread.is_alive(), "late thread must not run on forever"
    assert register_calls == [], "late thread must self-abort instead of registering"
    assert toggle._registered is False
    assert toggle._thread_id is None


def test_start_timeout_reclaims_thread_that_registers_right_at_the_boundary(monkeypatch):
    """Round 4 (Stream C, finding 1) backstop: if the worker thread had
    already slipped past the cancellation check and registered right as
    start()'s wait timed out, start() must still notice `_thread_id`
    appearing shortly after and reclaim the thread (WM_QUIT + join)
    before returning control to the caller with the exception -- not
    leave it running, orphaned, forever.
    """
    monkeypatch.setattr(hotkey_mod, "_READY_TIMEOUT_S", 0.05)
    quit_posted = threading.Event()

    def fake_run(self):
        # Simulates the worker having already slipped past the
        # cancellation check and registered right as start() gives up:
        # _ready never fires within the timeout, but _thread_id becomes
        # visible shortly after.
        release_delay.wait(timeout=2)
        self._registered = True
        self._thread_id = 4242
        quit_posted.wait(timeout=2)  # "alive" until reclaimed via WM_QUIT

    release_delay = threading.Event()

    def spy_post(thread_id, *a, **kw):
        quit_posted.set()
        return 1

    monkeypatch.setattr(HotkeyToggle, "_run", fake_run)
    monkeypatch.setattr(hotkey_mod.user32, "PostThreadMessageW", spy_post)
    toggle = HotkeyToggle(
        "F9", native_res=(1920, 1080), stretched_res=(1280, 1024),
        on_status=lambda m: None,
    )

    monkeypatch.setattr(hotkey_mod, "_CANCEL_RECLAIM_POLL_TIMEOUT_S", 0.3)
    # Release the simulated late registration only after start()'s
    # _ready.wait() has already timed out, so it lands inside start()'s
    # reclaim poll window rather than being caught by the initial wait.
    def timed_release():
        import time as _time
        _time.sleep(0.1)
        release_delay.set()

    threading.Thread(target=timed_release, daemon=True).start()

    with pytest.raises(TimeoutError):
        toggle.start()

    assert quit_posted.is_set(), "expected the late-registering thread to be sent WM_QUIT"
    toggle._thread.join(timeout=2)
    assert not toggle._thread.is_alive()


def test_run_message_loop_exits_and_logs_on_get_message_error(monkeypatch):
    """Round 5 (Stream C, finding 1): GetMessageW returns -1 on error (e.g.
    an invalid window handle), not 0 (WM_QUIT) or a positive value (a normal
    message). The old `while user32.GetMessageW(...) != 0:` treated -1
    identically to a normal message (`-1 != 0` is True) and looped forever,
    calling GetMessageW again with no trace of the failure. This asserts the
    loop notices -1, exits instead of spinning, and leaves a trace via both
    on_status and log_msg (matching this file's existing failure-reporting
    convention, e.g. `stop()`'s PostThreadMessageW failure handling).
    """
    call_count = {"n": 0}

    def fake_get_message(*a, **kw):
        call_count["n"] += 1
        if call_count["n"] > 1:
            pytest.fail(
                "GetMessageW called again after a -1 error return -- the "
                "loop spun instead of exiting"
            )
        return -1

    monkeypatch.setattr(hotkey_mod.user32, "RegisterHotKey", lambda *a, **kw: 1)
    monkeypatch.setattr(hotkey_mod.user32, "GetMessageW", fake_get_message)
    logged = []
    monkeypatch.setattr(hotkey_mod, "log_msg", logged.append)
    statuses = []
    toggle = HotkeyToggle(
        "F9", native_res=(1920, 1080), stretched_res=(1280, 1024),
        on_status=statuses.append,
    )

    toggle.start()  # must not raise -- RegisterHotKey succeeded
    toggle._thread.join(timeout=1)

    assert not toggle._thread.is_alive(), "worker thread must exit, not spin forever"
    assert call_count["n"] == 1
    assert any("getmessagew" in m.lower() for m in logged), (
        "expected a log_msg trace of the GetMessageW failure"
    )
    assert statuses, "expected an on_status report of the listener stopping"


def test_is_running_true_once_registered_false_before_start_and_after_exit(monkeypatch):
    """Round 24 finding (R4 Resilience, HIGH): nothing exposed whether the
    listener thread was actually still alive/registered -- bridge.py's
    `self._hotkey_running` flag was only ever set/cleared by
    start_hotkey/stop_hotkey themselves, so it went stale the moment the
    listener thread died on its own (e.g. a GetMessageW failure). This
    proves `is_running` tracks the real state: False before start(), True
    once RegisterHotKey succeeds and the message loop is running, and False
    again once the message loop exits for ANY reason -- including a
    GetMessageW failure with no explicit stop() call.
    """
    monkeypatch.setattr(hotkey_mod.user32, "RegisterHotKey", lambda *a, **kw: 1)
    monkeypatch.setattr(hotkey_mod.user32, "GetMessageW", lambda *a, **kw: -1)
    toggle = HotkeyToggle(
        "F9", native_res=(1920, 1080), stretched_res=(1280, 1024),
        on_status=lambda m: None,
    )

    assert toggle.is_running is False, "must be False before start() is ever called"

    toggle.start()  # must not raise -- RegisterHotKey succeeded
    toggle._thread.join(timeout=1)

    assert not toggle._thread.is_alive()
    assert toggle.is_running is False, (
        "must flip back to False once the message loop exits on its own "
        "(GetMessageW error), with no stop() ever called"
    )


def test_is_running_true_while_the_message_loop_is_still_running(monkeypatch):
    monkeypatch.setattr(hotkey_mod.user32, "RegisterHotKey", lambda *a, **kw: 1)
    block = threading.Event()

    def blocking_get_message(*a, **kw):
        block.wait(timeout=2)
        return 0  # WM_QUIT once released

    monkeypatch.setattr(hotkey_mod.user32, "GetMessageW", blocking_get_message)
    toggle = HotkeyToggle(
        "F9", native_res=(1920, 1080), stretched_res=(1280, 1024),
        on_status=lambda m: None,
    )

    try:
        toggle.start()
        assert toggle.is_running is True, (
            "must report True while the message loop is genuinely still "
            "running, not only immediately after start()"
        )
    finally:
        block.set()
        toggle._thread.join(timeout=2)

    assert toggle.is_running is False


def test_is_stretched_toggle_is_atomic_across_the_whole_toggle_operation(monkeypatch):
    """Round 5 (Stream C, finding 2): the Win32 message-loop worker thread
    (_run/_toggle) and the bridge dispatcher thread (bridge.py's
    _stop_hotkey_impl, which reads `is_stretched` to decide whether to
    revert to native before stopping) touch `is_stretched` from two
    different threads with no synchronization -- a narrow TOCTOU window
    between reading `is_stretched` to decide the revert-or-not branch and
    posting WM_QUIT.

    `is_stretched` is now a property backed by `_is_stretched_lock`, and
    `_toggle()` holds that same lock for its ENTIRE body (read, the
    set_resolution call, and the write), not just the final assignment. So
    any external reader (bridge.py's `toggle.is_stretched`, unchanged, no
    code there needs to change) blocks until a whole toggle operation
    finishes instead of observing a stale value mid-flight through the slow
    set_resolution call.

    This test proves that: while `_toggle()` is mid-flight inside a slow
    (mocked) `set_resolution`, a concurrent reader of `is_stretched` blocks
    until `_toggle()` releases the lock, then observes the POST-toggle
    value -- never the stale pre-toggle value.
    """
    monkeypatch.setattr(hotkey_mod, "get_current_resolution", lambda: (1920, 1080))
    entered_set_resolution = threading.Event()
    release_set_resolution = threading.Event()

    def slow_set_resolution(*a, **kw):
        entered_set_resolution.set()
        release_set_resolution.wait(timeout=2)
        return True, "ok"

    monkeypatch.setattr(hotkey_mod, "set_resolution", slow_set_resolution)
    toggle = HotkeyToggle(
        "F9", native_res=(1920, 1080), stretched_res=(1280, 1024),
        on_status=lambda m: None,
    )
    assert toggle.is_stretched is False

    worker = threading.Thread(target=toggle._toggle, daemon=True)
    worker.start()
    assert entered_set_resolution.wait(timeout=2), "set_resolution was never entered"

    observed = {}

    def reader():
        # Should block on the lock until _toggle() finishes and releases it.
        observed["value"] = toggle.is_stretched
        observed["done"] = True

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()
    reader_thread.join(timeout=0.2)
    assert "done" not in observed, (
        "reader observed is_stretched while _toggle() was still mid-flight "
        "-- the lock did not serialize the read against the in-progress toggle"
    )

    release_set_resolution.set()
    worker.join(timeout=2)
    reader_thread.join(timeout=2)

    assert observed.get("value") is True, (
        "reader must observe the POST-toggle value, never the stale "
        "pre-toggle value"
    )
    assert toggle.is_stretched is True


def test_hotkey_message_dispatch_catches_and_logs_toggle_exception(monkeypatch):
    """Round 11 (Stream D): `_toggle()` (invoked from `_run`'s Win32
    message-loop worker thread on WM_HOTKEY) previously had no exception
    guard, unlike every other background callback in this codebase --
    QuickRes.spec builds with `console=False`, so a raise inside it (e.g.
    `display.set_resolution` failing unexpectedly) would propagate up
    through the message loop, kill the listener thread, and leave zero
    trace in quickres.log.

    This proves the dispatch entry point the message loop calls on
    WM_HOTKEY (`_handle_hotkey_message`) now routes `_toggle()` through
    `config.call_logged`: a raising `set_resolution` must be caught and
    logged via `log_msg`, not propagate out of `_handle_hotkey_message`.
    """

    def raising_set_resolution(*a, **kw):
        raise RuntimeError("boom: driver rejected the mode change")

    monkeypatch.setattr(hotkey_mod, "set_resolution", raising_set_resolution)
    logged = []
    # `call_logged` (imported from quickres.config into hotkey.py) logs via
    # its OWN module's `log_msg` reference, not hotkey.py's -- patch
    # `config.log_msg` (not `hotkey_mod.log_msg`) to observe it.
    import quickres.config as config_mod
    monkeypatch.setattr(config_mod, "log_msg", logged.append)
    toggle = HotkeyToggle(
        "F9", native_res=(1920, 1080), stretched_res=(1280, 1024),
        on_status=lambda m: None,
    )

    toggle._handle_hotkey_message()  # must not raise

    assert any("boom" in m for m in logged), (
        "expected the RuntimeError from set_resolution to be logged via "
        "log_msg (through call_logged), not silently swallowed or "
        "propagated"
    )


def test_stop_is_quiet_on_clean_success(monkeypatch):
    """Sanity check: the ordinary success path (message posted, thread
    exits before the join timeout) still reports nothing extra.
    """
    monkeypatch.setattr(hotkey_mod.user32, "PostThreadMessageW", lambda *a, **kw: 1)
    statuses = []
    toggle = HotkeyToggle(
        "F9", native_res=(1920, 1080), stretched_res=(1280, 1024),
        on_status=statuses.append,
    )
    toggle._thread_id = 999999
    toggle._thread = threading.Thread(target=lambda: None, daemon=True)
    toggle._thread.start()
    toggle._thread.join(timeout=1)  # ensure it has already exited before stop()

    toggle.stop()

    assert statuses == []
    assert toggle._thread_id is None
