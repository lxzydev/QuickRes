from types import SimpleNamespace

import pytest

from quickres.webview import app


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self

    def emit(self, *args):
        for fn in self.handlers:
            fn(*args)


class Timer:
    instances = []

    def __init__(self, delay, fn, args=()):
        self.fn = fn
        self.args = args
        self.cancelled = False
        self.instances.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.fn(*self.args)


@pytest.fixture
def shell(monkeypatch):
    cfg = {}
    writes = []
    requested = {}
    window = SimpleNamespace(
        events=SimpleNamespace(closing=Event(), resized=Event()),
        native=SimpleNamespace(WindowState="Normal", _scale=1.5,
                               Size=SimpleNamespace(Width=780, Height=1050)),
    )
    monkeypatch.setattr(app.config, "load_config", lambda: cfg)
    monkeypatch.setattr(app.config, "update_config", lambda update: (writes.append(update), cfg.update(update)))
    monkeypatch.setattr(app, "_work_area_size", lambda: None)
    monkeypatch.setattr(app.threading, "Timer", Timer)
    Timer.instances = []

    def run():
        def create(*args, **kwargs):
            requested.update(kwargs)
            return window
        api = app.run_app(create_window_fn=create, start_fn=lambda: None)
        monkeypatch.setattr(api, "_stop_hotkey_impl", lambda: None)
        return api

    return SimpleNamespace(cfg=cfg, writes=writes, requested=requested, window=window, run=run)


def test_default_and_saved_geometry(shell):
    shell.run()
    assert (shell.requested["width"], shell.requested["height"]) == (520, 700)
    assert shell.requested["resizable"] is True
    assert shell.requested["min_size"] == (420, 500)
    shell.cfg["window_size"] = {"width": 800, "height": 900}
    shell.run()
    assert (shell.requested["width"], shell.requested["height"]) == (800, 900)


@pytest.mark.parametrize("size", [None, [], "800x900", {}, {"width": True, "height": 700},
    {"width": "800", "height": 700}, {"width": float("nan"), "height": 700},
    {"width": 800, "height": float("inf")}, {"width": -1, "height": 700},
    {"width": 0, "height": 0}, {"width": 100000, "height": 700},
    {"width": 10 ** 400, "height": 700}])
def test_invalid_geometry_uses_default(shell, size):
    shell.cfg["window_size"] = size
    shell.run()
    assert (shell.requested["width"], shell.requested["height"]) == (520, 700)


def test_saved_size_clamped_to_smaller_workarea(shell, monkeypatch):
    shell.cfg["window_size"] = {"width": 1500, "height": 1200}
    monkeypatch.setattr(app, "_work_area_size", lambda: (800, 600))
    shell.run()
    assert (shell.requested["width"], shell.requested["height"]) == (800, 600)


def test_minimum_adapts_to_tiny_workarea(shell, monkeypatch):
    monkeypatch.setattr(app, "_work_area_size", lambda: (400, 450))
    shell.run()
    assert shell.requested["min_size"] == (400, 450)
    assert (shell.requested["width"], shell.requested["height"]) == (400, 450)


def test_resize_burst_debounced_and_preserves_other_preferences(shell):
    shell.cfg["theme"] = "dark"
    shell.run()
    shell.window.events.resized.emit(700, 800)
    first = Timer.instances[-1]
    shell.window.events.resized.emit(750, 850)
    assert first.cancelled
    first.fire()
    assert shell.writes == []
    Timer.instances[-1].fire()
    assert shell.writes == [{"window_size": {"width": 750, "height": 850}}]
    assert shell.cfg["theme"] == "dark"


def test_close_flushes_latest_native_size_even_before_resize_callback(shell):
    shell.run()
    shell.window.native.Size = SimpleNamespace(Width=1200, Height=1350)
    shell.window.events.closing.emit()
    assert shell.writes == [{"window_size": {"width": 800, "height": 900}}]
    shell.window.events.resized.emit(900, 1000)
    assert len(shell.writes) == 1


@pytest.mark.parametrize("state, dimensions", [("Minimized", (0, 0)), ("Maximized", (1920, 1080))])
def test_abnormal_states_keep_last_normal_size(shell, state, dimensions):
    shell.run()
    shell.window.events.resized.emit(700, 800)
    shell.window.native.WindowState = state
    shell.window.native.Size = SimpleNamespace(Width=dimensions[0], Height=dimensions[1])
    shell.window.events.resized.emit(*dimensions)
    shell.window.events.closing.emit()
    Timer.instances[-1].fire()
    assert shell.writes == [{"window_size": {"width": 700, "height": 800}}]


def test_write_failure_is_logged_and_does_not_prevent_close(shell, monkeypatch):
    logged = []
    monkeypatch.setattr(app.config, "log_msg", logged.append)
    def fail(update):
        raise OSError("disk full")
    monkeypatch.setattr(app.config, "update_config", fail)
    api = shell.run()
    stopped = []
    monkeypatch.setattr(api, "_stop_hotkey_impl", lambda: stopped.append(True))
    shell.window.native.Size = SimpleNamespace(Width=1200, Height=1350)
    shell.window.events.closing.emit()
    assert stopped == [True]
    assert any("disk full" in entry for entry in logged)


def test_workarea_native_failure_is_optional(monkeypatch):
    monkeypatch.setattr(app.ctypes.windll.user32, "SystemParametersInfoW", lambda *args: 0)
    assert app._work_area_size() is None


def test_workarea_converts_physical_dimensions_to_logical(monkeypatch):
    def workarea(action, parameter, pointer, flags):
        rect = pointer._obj
        rect.left, rect.top, rect.right, rect.bottom = 0, 0, 1920, 1200
        return 1
    monkeypatch.setattr(app.ctypes.windll.user32, "SystemParametersInfoW", workarea)
    monkeypatch.setattr(app.ctypes.windll.user32, "GetDpiForSystem", lambda: 144)
    assert app._work_area_size() == (1280, 800)


def test_undersized_saved_geometry_uses_minimum(shell):
    shell.cfg["window_size"] = {"width": 200, "height": 300}
    shell.run()
    assert (shell.requested["width"], shell.requested["height"]) == (420, 500)


def test_invalid_resize_events_do_not_write(shell):
    shell.run()
    for size in ((0, 0), (True, 700), (float("nan"), 700), (200, 300)):
        shell.window.events.resized.emit(*size)
    shell.window.events.closing.emit()
    assert shell.writes == []
