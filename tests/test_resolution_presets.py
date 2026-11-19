"""Saved custom shortcuts integrate with presets and the existing hotkey path.

Windows display reads, display writes and hotkey registration are mocked;
config persistence uses the real atomic config writer in a temporary directory.
"""

from concurrent.futures import ThreadPoolExecutor

import pytest

from quickres import config, display, hotkey, i18n
from quickres.webview import bridge


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", str(tmp_path))
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(config, "PENDING_PATH", str(tmp_path / "pending_restore.json"))
    monkeypatch.setattr(config, "LOG_PATH", str(tmp_path / "quickres.log"))
    monkeypatch.setattr(display, "get_current_resolution", lambda: (1920, 1080))
    monkeypatch.setattr(display, "get_supported_resolutions",
                        lambda: {(1920, 1080), (1440, 1080), (1728, 1080)} | {(1700 + i, 1080) for i in range(20)})
    monkeypatch.setattr(display, "detect_gpu_vendors", lambda: {"nvidia"})
    monkeypatch.setattr(i18n, "_current_lang", "en")

    def no_display_write(*args):
        pytest.fail("Saving/removing a preset must never switch the display")

    monkeypatch.setattr(display, "set_resolution", no_display_write)
    return bridge.Api()


def customs(result):
    assert result["ok"] is True, result
    assert result["data"].get("ok", True) is True, result
    return [(p["width"], p["height"]) for p in result["data"]["presets"] if p["custom"]]


def test_save_reload_and_remove_preserve_other_settings(api):
    other = {"theme": "light", "hotkey": "F8", "stretched_res": "1728x1080",
             "ui_preferences": {"favorite_resolutions": ["1728x1080"]}}
    config.update_config(other)
    assert customs(api.add_resolution_preset("1728 x 1080")) == [(1728, 1080)]
    assert config.load_config() == {**other, "saved_resolution_presets": ["1728x1080"]}
    fresh = bridge.Api()
    assert customs(fresh.get_initial_state()) == [(1728, 1080)]
    assert customs(fresh.get_resolution_state()) == [(1728, 1080)]
    assert customs(fresh.remove_resolution_preset([1728, 1080])) == []
    assert config.load_config() == {**other, "saved_resolution_presets": []}
    assert customs(bridge.Api().get_initial_state()) == []


def test_duplicate_and_builtin_saves_do_not_duplicate_cards(api):
    for value in ("1728x1080", [1728, 1080], ["1728", "1080"]):
        assert customs(api.add_resolution_preset(value)) == [(1728, 1080)]
    result = api.add_resolution_preset("1920x1080")
    assert customs(result) == [(1728, 1080)]
    assert sum(p["width"] == 1920 and p["height"] == 1080 for p in result["data"]["presets"]) == 1
    assert config.load_config()["saved_resolution_presets"] == ["1728x1080"]
    assert customs(api.remove_resolution_preset("1920x1080")) == [(1728, 1080)]


@pytest.mark.parametrize("value", [None, {}, [], [True, 1080], [1728.5, 1080],
                                  [0, 1080], [-1, 1080], [10, 100000], "0x1080",
                                  "0000x1080", "<script>", "999999999x1080"])
@pytest.mark.parametrize("method", ["add_resolution_preset", "remove_resolution_preset"])
def test_invalid_input_never_writes_config(api, value, method, monkeypatch):
    monkeypatch.setattr(display, "get_supported_resolutions", lambda: pytest.fail("Invalid input must not reach driver lookup"))
    config.update_config({"theme": "light"})
    assert getattr(api, method)(value)["ok"] is False
    assert config.load_config() == {"theme": "light"}


@pytest.mark.parametrize("saved", [None, {}, "1728x1080", [None, {}, "0x1080", "oops"]])
def test_invalid_saved_data_does_not_break_boot(api, saved):
    config.update_config({"saved_resolution_presets": saved})
    assert customs(api.get_initial_state()) == []


def test_saved_data_is_sanitized_and_builtin_entries_are_not_removable(api):
    config.update_config({"saved_resolution_presets": ["1728x1080", "1728x1080", "1920x1080", None, "oops"]})
    result = api.get_resolution_state()
    assert customs(result) == [(1728, 1080)]
    native = next(p for p in result["data"]["presets"] if p["width"] == 1920 and p["height"] == 1080)
    assert native["custom"] is False


def test_limit_is_enforced_but_duplicates_and_removal_still_work(api, monkeypatch):
    monkeypatch.setattr(bridge, "_MAX_SAVED_RESOLUTION_PRESETS", 2)
    assert api.add_resolution_preset("1728x1080")["ok"]
    assert api.add_resolution_preset("1700x1080")["ok"]
    assert api.add_resolution_preset("1710x1080")["ok"] is False
    assert api.add_resolution_preset("1728x1080")["ok"]
    assert api.add_resolution_preset("1920x1080")["ok"]
    assert api.remove_resolution_preset("1728x1080")["ok"]
    assert customs(api.add_resolution_preset("1710x1080")) == [(1700, 1080), (1710, 1080)]


@pytest.mark.parametrize("method,value", [("add_resolution_preset", "1700x1080"),
                                         ("remove_resolution_preset", "1728x1080")])
def test_failed_write_is_not_reported_as_success(api, monkeypatch, method, value):
    api.add_resolution_preset("1728x1080")
    monkeypatch.setattr(config, "save_config", lambda cfg: False)
    result = getattr(api, method)(value)
    assert result["ok"] is False
    assert "Failed to write" in result["message"]
    assert customs(api.get_resolution_state()) == [(1728, 1080)]
    assert not bridge._resolution_presets_lock.locked()


def test_parallel_additions_across_api_instances_do_not_lose_updates(api):
    config.update_config({"theme": "light"})
    values = [f"{1700 + i}x1080" for i in range(20)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda value: bridge.Api().add_resolution_preset(value), values))
    assert all(result["ok"] for result in results)
    assert set(config.load_config()["saved_resolution_presets"]) == set(values)
    assert config.load_config()["theme"] == "light"


def test_custom_profile_reaches_real_toggle_logic_and_survives_removal(api, monkeypatch):
    # Replace only Windows I/O, not HotkeyToggle._toggle or bridge profile parsing.
    current = [(1920, 1080)]
    applied = []

    def set_resolution(width, height):
        current[0] = (width, height)
        applied.append(current[0])
        return True, "ok"

    monkeypatch.setattr(hotkey.HotkeyToggle, "start", lambda self: None)
    monkeypatch.setattr(hotkey, "get_current_resolution", lambda: current[0])
    monkeypatch.setattr(hotkey, "set_resolution", set_resolution)
    assert api.add_resolution_preset("1728x1080")["ok"]
    assert api.start_hotkey("F8", "1920x1080", "1728x1080")["ok"]
    toggle = api._hotkey_toggle
    toggle._toggle()
    rejected = api.remove_resolution_preset("1728x1080")
    assert rejected["ok"] is False
    assert rejected["message"] == i18n.t("preset_in_use")
    assert customs(api.get_resolution_state()) == [(1728, 1080)]
    assert api._hotkey_running is True
    assert api._hotkey_toggle is toggle
    toggle._toggle()
    assert applied == [(1728, 1080), (1920, 1080)]
    assert api.stop_hotkey()["ok"]
    assert customs(api.remove_resolution_preset("1728x1080")) == []
    assert config.load_config()["stretched_res"] == "1728x1080"


def test_unsupported_save_is_rejected_but_driver_registration_can_be_retried(api, monkeypatch):
    supported = {(1920, 1080)}
    monkeypatch.setattr(display, "get_supported_resolutions", lambda: supported)
    rejected = api.add_resolution_preset("1728x1080")
    assert rejected["data"] == {"ok": False, "reason": "unsupported", "vendors": ["nvidia"],
                                "width": 1728, "height": 1080}
    assert config.load_config() == {}
    assert customs(api.get_resolution_state()) == []
    supported.add((1728, 1080))
    assert customs(api.add_resolution_preset("1728x1080")) == [(1728, 1080)]


@pytest.mark.parametrize("missing", [(1920, 1080), (1728, 1080)])
def test_start_rechecks_both_modes_before_saving_or_registering(api, monkeypatch, missing):
    api.add_resolution_preset("1728x1080")
    before = config.load_config()
    supported = {(1920, 1080), (1728, 1080)} - {missing}
    monkeypatch.setattr(display, "get_supported_resolutions", lambda: supported)
    monkeypatch.setattr(bridge, "HotkeyToggle", lambda **kwargs: pytest.fail("Unsupported profile must not register"))
    result = api.start_hotkey("F8", "1920x1080", "1728x1080")
    assert result["data"]["reason"] == "unsupported"
    assert (result["data"]["width"], result["data"]["height"]) == missing
    assert config.load_config() == before
    assert api._hotkey_running is False
    assert not api._hotkey_lock.locked()
    # A temporarily unsupported saved preset stays visible; never silently delete it.
    assert customs(api.get_resolution_state()) == [(1728, 1080)]


def test_apply_rechecks_a_saved_preset_after_driver_mode_disappears(api, monkeypatch):
    api.add_resolution_preset("1728x1080")
    monkeypatch.setattr(display, "get_supported_resolutions", lambda: {(1920, 1080)})
    assert api.pick_resolution(1728, 1080)["data"]["reason"] == "unsupported"
    assert config.load_config()["saved_resolution_presets"] == ["1728x1080"]


@pytest.mark.parametrize("method,args", [
    ("add_resolution_preset", ("1728x1080",)),
    ("pick_resolution", (1728, 1080)),
    ("start_hotkey", ("F8", "1920x1080", "1728x1080")),
])
@pytest.mark.parametrize("raises", [False, True])
def test_failed_driver_query_does_not_save_switch_or_start(api, monkeypatch, method, args, raises):
    def lookup():
        if raises:
            raise OSError("display query failed")
        return set()

    monkeypatch.setattr(display, "get_supported_resolutions", lookup)
    monkeypatch.setattr(display, "detect_gpu_vendors", lambda: pytest.fail("No modes is not an unsupported-mode diagnosis"))
    monkeypatch.setattr(bridge, "HotkeyToggle", lambda **kwargs: pytest.fail("Failed query must not register"))
    result = getattr(api, method)(*args)
    assert result["ok"] is False
    assert result["message"] == ("display query failed" if raises else i18n.t("resolution_query_failed"))
    assert config.load_config() == {}
    assert not api._hotkey_running
    assert not api._hotkey_lock.locked()


def test_removal_is_blocked_during_start_stop_and_pending_restore(api, monkeypatch):
    api.add_resolution_preset("1728x1080")
    monkeypatch.setattr(hotkey.HotkeyToggle, "start", lambda self: None)
    assert api.start_hotkey("F8", "1920x1080", "1728x1080")["data"]["running"]
    with api._hotkey_lock:
        assert api.remove_resolution_preset("1728x1080")["message"] == i18n.t("preset_busy")
    api._hotkey_running = False
    api._hotkey_restore_pending = True
    assert api.remove_resolution_preset("1728x1080")["message"] == i18n.t("preset_in_use")
    assert customs(api.get_resolution_state()) == [(1728, 1080)]
    assert not api._hotkey_lock.locked()


def test_unrelated_preset_can_be_removed_while_hotkey_is_running(api, monkeypatch):
    for value in ("1728x1080", "1700x1080"):
        api.add_resolution_preset(value)
    monkeypatch.setattr(hotkey.HotkeyToggle, "start", lambda self: None)
    api.start_hotkey("F8", "1920x1080", "1728x1080")
    assert customs(api.remove_resolution_preset("1700x1080")) == [(1728, 1080)]
    assert api._hotkey_running


def test_builtin_and_duplicate_results_are_explicit(api):
    assert api.add_resolution_preset("1728x1080")["data"]["added"] is True
    assert api.add_resolution_preset("1728x1080")["data"]["added"] is False
    assert api.add_resolution_preset("1920x1080")["data"]["added"] is False


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_preset_chrome_is_translated_in_the_bridge_bundle(lang):
    keys = ["btn_save_preset", "preset_save_note", "preset_saved", "preset_already_exists",
            "preset_removed", "preset_remove_label", "preset_busy"]
    bundle = bridge._ui_strings(lang)
    for key in keys:
        assert bundle[key] == i18n.STRINGS[lang][key]
        assert bundle[key] != key
    for key in ["preset_in_use", "preset_invalid_dimensions", "preset_limit_reached", "resolution_query_failed"]:
        assert key in i18n.STRINGS[lang]
