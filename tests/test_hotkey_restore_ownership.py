import pytest

from quickres import config, display, hotkey
from quickres.webview.bridge import Api


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(config, "APP_DIR", str(tmp_path))
    monkeypatch.setattr(config, "PENDING_PATH", str(tmp_path / "pending_restore.json"))
    monkeypatch.setattr(config, "LOG_PATH", str(tmp_path / "quickres.log"))
    mode = [(1920, 1080)]
    writes = []

    def apply(width, height):
        mode[0] = (width, height)
        writes.append(mode[0])
        return True, "ok"

    monkeypatch.setattr(hotkey, "get_current_resolution", lambda: mode[0])
    monkeypatch.setattr(display, "get_current_resolution", lambda: mode[0])
    monkeypatch.setattr(hotkey, "set_resolution", apply)
    monkeypatch.setattr(display, "set_resolution", apply)
    toggle = hotkey.HotkeyToggle("F6", (1920, 1080), (1440, 1080), lambda _: None)
    toggle._entry_res = mode[0]
    monkeypatch.setattr(toggle, "stop", lambda: True)
    api = Api()
    api._hotkey_toggle = toggle
    api._hotkey_running = True
    return api, toggle, mode, writes


def test_stop_does_not_undo_a_manual_change_without_a_hotkey_press(session):
    api, toggle, mode, writes = session
    mode[0] = (1440, 1080)

    assert api.stop_hotkey()["ok"] is True
    assert mode[0] == (1440, 1080)
    assert writes == []


def test_failed_toggle_does_not_claim_a_later_manual_change(session, monkeypatch):
    api, toggle, mode, writes = session
    monkeypatch.setattr(hotkey, "set_resolution", lambda *args: (False, "refused"))
    toggle._toggle()
    mode[0] = (1440, 1080)

    assert api.stop_hotkey()["ok"] is True
    assert writes == []


def test_stop_restores_a_successful_hotkey_change(session):
    api, toggle, mode, writes = session
    toggle._toggle()

    assert api.stop_hotkey()["ok"] is True
    assert mode[0] == (1920, 1080)
    assert writes == [(1440, 1080), (1920, 1080)]


def test_stop_preserves_a_mode_different_from_the_last_hotkey_change(session):
    api, toggle, mode, writes = session
    toggle._entry_res = (2560, 1440)
    toggle._toggle()
    mode[0] = (1920, 1080)

    assert api.stop_hotkey()["ok"] is True
    assert mode[0] == (1920, 1080)
    assert writes == [(1440, 1080)]


def test_failed_restore_survives_polling_and_rejects_start_until_retry(session, monkeypatch):
    api, toggle, mode, writes = session
    toggle._toggle()
    restore = display.set_resolution
    monkeypatch.setattr(display, "set_resolution", lambda *args: (False, "driver refused"))

    assert api.stop_hotkey()["ok"] is False
    status = api.get_hotkey_status()["data"]
    assert status == {"running": False, "restore_pending": True}
    assert api._hotkey_toggle is toggle
    assert api.start_hotkey("F7", "1920x1080", "1440x1080")["ok"] is False
    assert api._hotkey_toggle is toggle

    monkeypatch.setattr(display, "set_resolution", restore)
    assert api.stop_hotkey()["ok"] is True
    assert mode[0] == (1920, 1080)
    assert api.get_hotkey_status()["data"].get("restore_pending", False) is False
    assert api._hotkey_toggle is None


def test_unreadable_mode_keeps_restore_available_for_retry(session):
    api, toggle, mode, writes = session
    toggle._toggle()
    mode[0] = None

    assert api.stop_hotkey()["ok"] is False
    mode[0] = (1440, 1080)
    assert api.stop_hotkey()["ok"] is True
    assert writes == [(1440, 1080), (1920, 1080)]


def test_initial_state_keeps_a_failed_restore_visible(session, monkeypatch):
    from quickres.webview import bridge

    api, toggle, mode, writes = session
    toggle._toggle()
    monkeypatch.setattr(display, "set_resolution", lambda *args: (False, "refused"))
    monkeypatch.setattr(bridge.monitors, "enumerate_monitors", lambda: [])
    assert api.stop_hotkey()["ok"] is False

    state = api.get_initial_state()

    assert state["ok"] is True
    assert state["data"]["hotkey"]["restore_pending"] is True
