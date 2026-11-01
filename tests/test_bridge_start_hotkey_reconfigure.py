"""Round 12 (Reliability finding): start_hotkey used to silently no-op
(return {"running": True} without applying the new key/native_res/
stretched_res at all) whenever a hotkey was already running -- a
success-looking envelope that quietly contradicted a caller's request for a
DIFFERENT configuration, leaving the old HotkeyToggle instance, config, and
registered key all unchanged.

panel.html's own hotkey UI never triggers this: its Start button is
`hidden` for the entire time state.hotkey.running is true (renderHotkeySection
sets `el('qr-hotkey-start').hidden = state.hotkey.running`), so the normal
click flow always calls stop_hotkey before a fresh start_hotkey. The fix
matches that expectation: an identical repeat call while running stays a
harmless idempotent no-op, but a call requesting a genuinely different
configuration while running is now rejected outright (caller must call
stop_hotkey first) instead of silently keeping the stale toggle alive.
"""
import pytest

from quickres.webview.bridge import Api
from quickres import config


@pytest.fixture(autouse=True)
def _isolated_app_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", str(tmp_path))
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(config, "PENDING_PATH", str(tmp_path / "pending_restore.json"))
    yield


class _FakeToggle:
    instances = []
    entry_res = None

    def __init__(self, key_name, native_res, stretched_res, on_status):
        self.key_name = key_name
        self.native_res = native_res
        self.stretched_res = stretched_res
        self.on_status = on_status
        self.started = False
        self.stopped = False
        _FakeToggle.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    @property
    def is_stretched(self):
        return False


@pytest.fixture(autouse=True)
def _fake_toggle(monkeypatch):
    _FakeToggle.instances = []
    monkeypatch.setattr("quickres.webview.bridge.HotkeyToggle", _FakeToggle)
    yield _FakeToggle
    _FakeToggle.instances = []


class TestStartHotkeyWhileRunningWithDifferentConfigIsRejected:
    def test_second_start_with_different_key_is_rejected_not_silently_ignored(self):
        api = Api()
        first = api.start_hotkey("F6", [1920, 1080], [1440, 1080])
        assert first["ok"] is True

        second = api.start_hotkey("F7", [1920, 1080], [1440, 1080])

        assert second["ok"] is False
        assert second["kind"] == "error"
        # The original toggle/config must remain completely untouched --
        # never silently swapped, never silently left in a half-updated
        # state.
        assert len(_FakeToggle.instances) == 1
        assert api._hotkey_toggle.key_name == "F6"
        assert config.load_config().get("hotkey") == "F6"

    def test_second_start_with_different_native_res_is_rejected(self):
        api = Api()
        api.start_hotkey("F6", [1920, 1080], [1440, 1080])

        second = api.start_hotkey("F6", [2560, 1440], [1440, 1080])

        assert second["ok"] is False
        assert api._hotkey_toggle.native_res == (1920, 1080)

    def test_second_start_with_different_stretched_res_is_rejected(self):
        api = Api()
        api.start_hotkey("F6", [1920, 1080], [1440, 1080])

        second = api.start_hotkey("F6", [1920, 1080], [1280, 1024])

        assert second["ok"] is False
        assert api._hotkey_toggle.stretched_res == (1440, 1080)

    def test_identical_repeat_call_while_running_is_a_harmless_idempotent_noop(self):
        api = Api()
        first = api.start_hotkey("F6", [1920, 1080], [1440, 1080])
        assert first["ok"] is True

        second = api.start_hotkey("F6", [1920, 1080], [1440, 1080])

        assert second["ok"] is True
        assert second["data"]["running"] is True
        # Never a second HotkeyToggle instance -- the identical call is a
        # true no-op, not a reconfigure-in-place.
        assert len(_FakeToggle.instances) == 1

    def test_stop_then_start_with_new_config_succeeds(self):
        api = Api()
        api.start_hotkey("F6", [1920, 1080], [1440, 1080])
        api.stop_hotkey()

        result = api.start_hotkey("F7", [2560, 1440], [1440, 1080])

        assert result["ok"] is True
        assert api._hotkey_toggle.key_name == "F7"
        assert len(_FakeToggle.instances) == 2
