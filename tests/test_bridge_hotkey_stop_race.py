"""1e/1f: stop_hotkey/_stop_hotkey_impl must be mutually exclusive with
start_hotkey (both guarded by self._hotkey_lock), and must surface a failed
revert-to-native instead of silently discarding it.
"""
import threading
import time

import pytest

from quickres.webview.bridge import Api
from quickres import config


@pytest.fixture(autouse=True)
def _isolated_app_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", str(tmp_path))
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(config, "PENDING_PATH", str(tmp_path / "pending_restore.json"))
    yield


class TestStopHotkeyMutualExclusionWithStart:
    def test_stop_does_not_silently_no_op_while_start_holds_the_lock(self, monkeypatch):
        # Keep the wait short so this test stays fast -- production keeps
        # the real ~3s margin (HotkeyToggle.start()'s up-to-2s wait + slack).
        monkeypatch.setattr("quickres.webview.bridge._HOTKEY_STOP_LOCK_TIMEOUT_S", 0.05)
        api = Api()
        api._hotkey_toggle = object()  # pretend a hotkey is "running"
        api._hotkey_running = True
        api._hotkey_lock.acquire()  # simulate start_hotkey still in progress

        result = api.stop_hotkey()

        assert result["ok"] is False
        # Must NOT have silently reported success while the toggle is still
        # actually live -- state must be left untouched, not force-cleared.
        assert api._hotkey_running is True
        assert api._hotkey_toggle is not None

    def test_stop_waits_for_start_to_release_then_succeeds(self, monkeypatch):
        class FakeToggle:
            # No observed entry resolution -> nothing for the stop path to
            # restore, keeping this test focused on the lock handoff.
            entry_res = None

            def stop(self):
                pass

        api = Api()
        api._hotkey_toggle = FakeToggle()
        api._hotkey_running = True
        api._hotkey_lock.acquire()

        def _release_soon():
            time.sleep(0.05)
            api._hotkey_lock.release()

        threading.Thread(target=_release_soon, daemon=True).start()

        result = api.stop_hotkey()

        assert result["ok"] is True
        assert api._hotkey_running is False


class TestStopHotkeySurfacesRevertFailure:
    def test_failed_native_revert_is_reported_not_swallowed(self, monkeypatch):
        class FakeToggle:
            native_res = (1920, 1080)
            stretched_res = (1440, 1080)
            entry_res = (1920, 1080)

            def __init__(self):
                self.stopped = False

            def stop(self):
                self.stopped = True

        # Left on the stretched resolution, so the stop path attempts the
        # restore back to entry_res -- and that attempt is what fails here.
        monkeypatch.setattr(
            "quickres.webview.bridge.display.get_current_resolution",
            lambda: (1440, 1080),
        )
        monkeypatch.setattr(
            "quickres.webview.bridge.display.set_resolution",
            lambda w, h: (False, "driver refused resolution"),
        )
        api = Api()
        toggle = FakeToggle()
        api._hotkey_toggle = toggle
        api._hotkey_running = True

        result = api.stop_hotkey()

        assert result["ok"] is False
        assert "driver refused resolution" in result["message"]


def test_stop_keeps_live_toggle_when_listener_did_not_exit():
    class FakeToggle:
        is_stretched = False
        is_running = True

        def stop(self):
            return False

    api = Api()
    toggle = FakeToggle()
    api._hotkey_toggle = toggle
    api._hotkey_running = True

    result = api.stop_hotkey()

    assert result["ok"] is False
    assert "still stopping" in result["message"].lower()
    assert api._hotkey_toggle is toggle
    assert api._hotkey_running is True
