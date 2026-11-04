import quickres.webview.app as app
from quickres import monitors
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


def test_module_level_window_holder_is_set_not_an_api_attribute(monkeypatch):
    fake_window = _FakeWindow()
    monkeypatch.setattr(app, "_WINDOW", None)

    api = app.run_app(
        create_window_fn=lambda *a, **k: fake_window,
        start_fn=lambda: None,
    )

    # APP-5: the Window object lives at module level...
    assert app.get_window() is fake_window
    # ...and is NEVER reachable as an attribute of the Api instance (would
    # trigger a pythonnet marshalling recursion on the EdgeChromium backend).
    for value in vars(api).values():
        assert not isinstance(value, _FakeWindow)


def test_close_handler_delegates_to_stop_hotkey_impl_no_duplicated_logic(monkeypatch):
    fake_window = _FakeWindow()

    api = app.run_app(
        create_window_fn=lambda *a, **k: fake_window,
        start_fn=lambda: None,
    )

    calls = []
    monkeypatch.setattr(api, "_stop_hotkey_impl", lambda: calls.append(1))

    assert len(fake_window.events.closing.handlers) == 1
    fake_window.events.closing.handlers[0]()

    assert calls == [1]


def test_run_app_calls_start(monkeypatch):
    fake_window = _FakeWindow()
    started = []

    app.run_app(
        create_window_fn=lambda *a, **k: fake_window,
        start_fn=lambda: started.append(1),
    )

    assert started == [1]


def test_run_app_requests_a_readable_window_size(monkeypatch):
    monkeypatch.setattr(app.config, "load_config", lambda: {})
    monkeypatch.setattr(app, "_work_area_size", lambda: None)
    fake_window = _FakeWindow()
    requested = {}

    app.run_app(
        create_window_fn=lambda *a, **kwargs: (requested.update(kwargs) or fake_window),
        start_fn=lambda: None,
    )

    assert requested["width"] == 520
    assert requested["height"] == 700


def test_closing_resolves_an_armed_pending_guard_before_process_exit(monkeypatch):
    # Round-2 regression fix: the auto-revert threading.Timer is a daemon
    # thread, so closing the window while a disable is still in its grace
    # period used to kill the timer before it ever fired -- the monitor
    # stayed disabled forever. Window-close must synchronously resolve any
    # armed guard itself, through the same path the timer would use.
    fake_window = _FakeWindow()
    api = app.run_app(
        create_window_fn=lambda *a, **k: fake_window,
        start_fn=lambda: None,
    )
    monkeypatch.setattr(api, "_stop_hotkey_impl", lambda: None)

    revert_calls = []

    def _revert(ids):
        revert_calls.append(ids)
        return [(iid, True, "ok", monitors.OUTCOME_CONFIRMED) for iid in ids]

    guard = PendingDisableGuard(
        armed_at=0.0, target_ids=["DISPLAY\\A\\1"],
        revert_fn=_revert, timeout_s=10.0,
    )
    api._pending_guard = guard

    fake_window.events.closing.handlers[0]()

    assert revert_calls == [["DISPLAY\\A\\1"]]
    assert guard.resolved is True


def test_closing_with_no_armed_guard_does_not_touch_pending_state(monkeypatch):
    fake_window = _FakeWindow()
    api = app.run_app(
        create_window_fn=lambda *a, **k: fake_window,
        start_fn=lambda: None,
    )
    monkeypatch.setattr(api, "_stop_hotkey_impl", lambda: None)
    assert api._pending_guard is None

    fake_window.events.closing.handlers[0]()  # must not raise

    assert api._pending_guard is None


def test_closing_logs_but_does_not_raise_when_stop_hotkey_impl_fails(monkeypatch):
    fake_window = _FakeWindow()
    api = app.run_app(
        create_window_fn=lambda *a, **k: fake_window,
        start_fn=lambda: None,
    )
    logged = []
    monkeypatch.setattr("quickres.config.log_msg", lambda msg: logged.append(msg))

    def _boom():
        raise RuntimeError("teardown failure")

    monkeypatch.setattr(api, "_stop_hotkey_impl", _boom)

    fake_window.events.closing.handlers[0]()  # must not raise

    assert len(logged) == 1
    assert "teardown failure" in logged[0]


def test_closing_still_stops_hotkey_when_guard_resolution_raises(monkeypatch):
    fake_window = _FakeWindow()
    api = app.run_app(
        create_window_fn=lambda *a, **k: fake_window,
        start_fn=lambda: None,
    )
    logged = []
    monkeypatch.setattr("quickres.config.log_msg", lambda msg: logged.append(msg))
    stop_calls = []
    monkeypatch.setattr(api, "_stop_hotkey_impl", lambda: stop_calls.append(1))

    def _boom(ids):
        raise RuntimeError("revert failure")

    guard = PendingDisableGuard(
        armed_at=0.0, target_ids=["DISPLAY\\A\\1"], revert_fn=_boom, timeout_s=10.0,
    )
    api._pending_guard = guard

    fake_window.events.closing.handlers[0]()  # must not raise

    assert stop_calls == [1]
    assert len(logged) == 1
