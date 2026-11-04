import pytest

from quickres import config, monitors
from quickres.webview.bridge import Api


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", str(tmp_path))
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(config, "PENDING_PATH", str(tmp_path / "pending_restore.json"))
    return Api()


def test_preferences_survive_reload_and_preserve_other_settings(api):
    config.update_config({"theme": "light", "hotkey": "F7"})
    expected = {"collapsed_sections": ["custom"], "favorite_resolutions": ["1440x1080", "1920x1080"]}
    assert api.set_ui_preferences(expected)["data"] == expected
    assert api.get_initial_state()["data"]["ui_preferences"] == expected
    assert config.load_config()["hotkey"] == "F7"
    assert config.load_config()["theme"] == "light"


@pytest.mark.parametrize("value", [[], {"collapsed_sections": ["invalid"]}, {"favorite_resolutions": ["<script>"]}, {"theme": "dark"}])
def test_invalid_preferences_do_not_replace_saved_settings(api, value):
    config.update_config({"theme": "light"})
    assert api.set_ui_preferences(value)["ok"] is False
    assert config.load_config() == {"theme": "light"}


def test_initial_state_does_not_enumerate_monitors_before_the_modal_is_opened(api, monkeypatch):
    calls = []
    monkeypatch.setattr(monitors, "enumerate_monitors", lambda: calls.append(1) or [])
    initial = api.get_initial_state()
    assert initial["ok"] is True
    assert calls == []
    assert api.list_monitors()["ok"] is True
    assert calls == [1]
