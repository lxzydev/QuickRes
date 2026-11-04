import time

import quickres.webview.app as app
from quickres.monitors import PendingDisableGuard


class _FakeClosingSlot:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self


class _FakeEvents:
    def __init__(self):
        self.closing = _FakeClosingSlot()
        self.resized = _FakeClosingSlot()


class _FakeWindow:
    def __init__(self):
        self.events = _FakeEvents()
        self.native = None


def test_slow_guard_resolution_does_not_hang_close_handler(monkeypatch):
    # A slow/hanging guard resolution (e.g. blocked behind a live UAC
    # prompt) must not stall process exit indefinitely -- the close handler
    # bounds how long it waits and moves on, leaving the on-disk pending
    # record + next-launch recover_on_boot as the real safety net.
    monkeypatch.setattr(app, "_GUARD_RESOLVE_CLOSE_TIMEOUT_S", 0.05)

    fake_window = _FakeWindow()
    api = app.run_app(
        create_window_fn=lambda *a, **k: fake_window,
        start_fn=lambda: None,
    )
    monkeypatch.setattr(api, "_stop_hotkey_impl", lambda: None)

    def _slow_resolve(guard, now=None):
        time.sleep(0.5)

    monkeypatch.setattr(api, "_resolve_guard_unbounded_under_lock", _slow_resolve)

    guard = PendingDisableGuard(
        armed_at=0.0, target_ids=["DISPLAY\\A\\1"],
        revert_fn=lambda ids: [(iid, True, "ok") for iid in ids], timeout_s=10.0,
    )
    api._pending_guard = guard

    started = time.monotonic()
    fake_window.events.closing.handlers[0]()  # must not raise, must not hang
    elapsed = time.monotonic() - started

    assert elapsed < 0.3  # bounded well below the 0.5s the slow call takes


def test_guard_resolution_failure_is_logged_via_call_logged(monkeypatch):
    fake_window = _FakeWindow()
    api = app.run_app(
        create_window_fn=lambda *a, **k: fake_window,
        start_fn=lambda: None,
    )
    monkeypatch.setattr(api, "_stop_hotkey_impl", lambda: None)

    logged = []
    monkeypatch.setattr("quickres.config.log_msg", lambda msg: logged.append(msg))

    def _boom(guard, now=None):
        raise RuntimeError("guard resolution failure")

    monkeypatch.setattr(api, "_resolve_guard_unbounded_under_lock", _boom)

    guard = PendingDisableGuard(
        armed_at=0.0, target_ids=["DISPLAY\\A\\1"],
        revert_fn=lambda ids: [(iid, True, "ok") for iid in ids], timeout_s=10.0,
    )
    api._pending_guard = guard

    fake_window.events.closing.handlers[0]()  # must not raise

    assert len(logged) == 1
    assert "guard resolution failure" in logged[0]
    assert "_on_closing: resolve_guard" in logged[0]
