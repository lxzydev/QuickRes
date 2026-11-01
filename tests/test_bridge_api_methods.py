import pytest

from quickres.webview import bridge as bridge_mod
from quickres.webview.bridge import Api
from quickres import config
from quickres import recovery


@pytest.fixture(autouse=True)
def _isolated_app_dir(tmp_path, monkeypatch):
    """Every test gets its own APP_DIR so config.json/pending_restore.json
    never touch the real user profile."""
    monkeypatch.setattr(config, "APP_DIR", str(tmp_path))
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(config, "PENDING_PATH", str(tmp_path / "pending_restore.json"))
    yield


# ---------------------------------------------------------------------------
# T3.2 -- read-only / simple methods
# ---------------------------------------------------------------------------

class TestOpenExternal:
    def test_rejects_non_https_scheme(self, monkeypatch):
        opened = []
        monkeypatch.setattr("quickres.webview.bridge.webbrowser.open", lambda u: opened.append(u))
        api = Api()

        result = api.open_external("http://quickres.online/")

        assert result["ok"] is False
        assert opened == []

    def test_rejects_non_allowlisted_host(self, monkeypatch):
        opened = []
        monkeypatch.setattr("quickres.webview.bridge.webbrowser.open", lambda u: opened.append(u))
        api = Api()

        result = api.open_external("https://evil.example.com/")

        assert result["ok"] is False
        assert opened == []

    def test_accepts_allowlisted_https_host(self, monkeypatch):
        opened = []
        monkeypatch.setattr("quickres.webview.bridge.webbrowser.open", lambda u: opened.append(u))
        api = Api()

        result = api.open_external("https://quickres.online/")

        assert result["ok"] is True
        assert opened == ["https://quickres.online/"]


class TestCustomResolutionRetired:
    """RES-2 / D6: the 6-slot custom-resolution list is retired entirely --
    no `add_custom`/`remove_custom` bridge methods, no `customs` key."""

    def test_add_custom_removed_from_api(self):
        assert hasattr(Api, "add_custom") is False

    def test_remove_custom_removed_from_api(self):
        assert hasattr(Api, "remove_custom") is False


class TestGetInitialState:
    def test_returns_all_expected_top_level_keys(self, monkeypatch):
        monkeypatch.setattr(
            "quickres.webview.bridge.display.get_current_resolution", lambda: (1920, 1080)
        )
        monkeypatch.setattr(
            "quickres.webview.bridge.monitors.enumerate_monitors", lambda: []
        )
        api = Api()

        result = api.get_initial_state()

        assert result["ok"] is True
        data = result["data"]
        for key in ("theme", "version", "language", "strings", "current_resolution",
                    "presets", "hotkey", "monitors", "pending", "faq"):
            assert key in data, f"missing key: {key}"
        assert len(data["faq"]) == 4


class TestGetResolutionState:
    def test_reads_the_os_mode_and_reclassifies_presets(self, monkeypatch):
        monkeypatch.setattr(
            "quickres.webview.bridge.display.get_current_resolution", lambda: (1280, 960)
        )
        api = Api()

        result = api.get_resolution_state()

        assert result["ok"] is True
        data = result["data"]
        assert data["current_resolution"] == {"width": 1280, "height": 960}
        assert [p for p in data["presets"] if p["kind"] == "native"] == [
            p for p in data["presets"] if (p["width"], p["height"]) == (1280, 960)
        ]

    def test_has_no_customs_key(self, monkeypatch):
        # RES-2/D6: the 6-slot custom-resolution list is retired -- nothing
        # reads `custom_resolutions` from config anymore, so the response
        # must not carry a `customs` key at all.
        monkeypatch.setattr(
            "quickres.webview.bridge.display.get_current_resolution", lambda: (1920, 1080)
        )
        monkeypatch.setattr(
            "quickres.webview.bridge.monitors.enumerate_monitors", lambda: []
        )
        api = Api()

        result = api.get_initial_state()

        assert "customs" not in result["data"]

    def test_theme_falls_back_to_os_detection_when_unset(self, monkeypatch):
        monkeypatch.setattr("quickres.webview.bridge.display.get_current_resolution", lambda: (1920, 1080))
        monkeypatch.setattr("quickres.webview.bridge.monitors.enumerate_monitors", lambda: [])
        monkeypatch.setattr("quickres.webview.bridge.detect_system_theme", lambda: "light")
        api = Api()

        result = api.get_initial_state()

        assert result["data"]["theme"] == "light"

    def test_saved_theme_wins_over_os_detection(self, monkeypatch):
        monkeypatch.setattr("quickres.webview.bridge.display.get_current_resolution", lambda: (1920, 1080))
        monkeypatch.setattr("quickres.webview.bridge.monitors.enumerate_monitors", lambda: [])
        monkeypatch.setattr("quickres.webview.bridge.detect_system_theme", lambda: "light")
        config.update_config({"theme": "dark"})
        api = Api()

        result = api.get_initial_state()

        assert result["data"]["theme"] == "dark"

    def test_language_defaults_to_auto_setting_with_resolved_value(self, monkeypatch):
        monkeypatch.setattr("quickres.webview.bridge.display.get_current_resolution", lambda: (1920, 1080))
        monkeypatch.setattr("quickres.webview.bridge.monitors.enumerate_monitors", lambda: [])
        api = Api()

        result = api.get_initial_state()

        language = result["data"]["language"]
        assert language["setting"] == "auto"
        assert language["resolved"] in ("en", "ru")
        assert language["options"] == {"auto": "Auto", "en": "English", "ru": "Русский"}


class TestConfigWriteFailurePropagation:
    # Round-3 finding 5: set_theme/set_language/start_hotkey all call
    # config.update_config() and used to unconditionally report success even
    # if the underlying write_json_atomic silently failed. config.py is
    # owned by a different stream this round (update_config's
    # read-modify-write locking) -- update_config() now raises directly on
    # a failed write, an additive change that doesn't touch the locking
    # structure, so these three call sites need no change: bridge_op's
    # existing exception-to-envelope machinery already surfaces it.
    def test_set_theme_reports_error_when_write_fails(self, monkeypatch):
        monkeypatch.setattr(
            "quickres.webview.bridge.config.save_config", lambda cfg: False
        )
        api = Api()

        result = api.set_theme("dark")

        assert result["ok"] is False
        assert result["kind"] == "error"

    def test_set_language_reports_error_when_write_fails(self, monkeypatch):
        monkeypatch.setattr(
            "quickres.webview.bridge.config.save_config", lambda cfg: False
        )
        api = Api()

        result = api.set_language("en")

        assert result["ok"] is False
        assert result["kind"] == "error"

    def test_start_hotkey_reports_error_when_write_fails(self, monkeypatch):
        monkeypatch.setattr(
            "quickres.webview.bridge.config.save_config", lambda cfg: False
        )
        start_calls = []

        class FakeToggle:
            def __init__(self, key_name, native_res, stretched_res, on_status):
                pass

            def start(self):
                start_calls.append(1)

        monkeypatch.setattr("quickres.webview.bridge.HotkeyToggle", FakeToggle)
        api = Api()

        result = api.start_hotkey("F6", [1920, 1080], [1440, 1080])

        assert result["ok"] is False
        assert result["kind"] == "error"
        # Must fail BEFORE ever starting the hotkey listener -- a config
        # write failure must not leave a live hotkey registered with
        # nothing persisted to restore it on next launch.
        assert start_calls == []
        assert api._hotkey_running is False


class TestSetLanguage:
    def test_persists_and_resolves_russian(self, monkeypatch):
        monkeypatch.setattr("quickres.webview.bridge.display.get_current_resolution", lambda: (1920, 1080))
        monkeypatch.setattr("quickres.webview.bridge.monitors.enumerate_monitors", lambda: [])
        api = Api()

        result = api.set_language("ru")

        assert result["ok"] is True
        assert result["data"]["language"]["resolved"] == "ru"
        assert config.load_config()["language"] == "ru"
        assert result["data"]["faq"][0]["q"] == "Почему появляется сообщение «Разрешение не найдено»?"

    def test_rejects_unknown_language(self):
        api = Api()

        result = api.set_language("fr")

        assert result["ok"] is False


class TestFaqBundleDedup:
    # Round-2 dedup: the FAQ list-comprehension building [{"q":..., "a":...}]
    # from _FAQ_KEYS was copy-pasted verbatim in both get_initial_state and
    # set_language -- both must now route through one shared helper.
    def test_get_initial_state_and_set_language_share_one_faq_helper(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            bridge_mod, "_faq_bundle", lambda lang=None: calls.append(1) or [{"q": "Q", "a": "A"}],
        )
        monkeypatch.setattr(bridge_mod.display, "get_current_resolution", lambda: (1920, 1080))
        monkeypatch.setattr(bridge_mod.monitors, "enumerate_monitors", lambda: [])
        api = Api()

        state = api.get_initial_state()
        lang_result = api.set_language("en")

        assert state["data"]["faq"] == [{"q": "Q", "a": "A"}]
        assert lang_result["data"]["faq"] == [{"q": "Q", "a": "A"}]
        assert calls == [1, 1]


class TestPickResolution:
    def test_rejects_non_numeric(self, monkeypatch):
        lookup_calls = []
        monkeypatch.setattr(
            "quickres.webview.bridge.display.get_supported_resolutions",
            lambda: lookup_calls.append(1) or {(1920, 1080)},
        )
        api = Api()

        result = api.pick_resolution(None, "abc")

        assert result["ok"] is False
        assert result["kind"] == "error"
        assert lookup_calls == []

    def test_unsupported_resolution_never_calls_set_resolution(self, monkeypatch):
        monkeypatch.setattr(
            "quickres.webview.bridge.display.get_supported_resolutions", lambda: {(1920, 1080)}
        )
        set_calls = []
        monkeypatch.setattr(
            "quickres.webview.bridge.display.set_resolution",
            lambda w, h: set_calls.append((w, h)),
        )
        monkeypatch.setattr(
            "quickres.webview.bridge.display.detect_gpu_vendors", lambda: {"nvidia"}
        )
        api = Api()

        result = api.pick_resolution(1280, 720)

        assert result["data"]["ok"] is False
        assert result["data"]["reason"] == "unsupported"
        assert result["data"]["vendors"] == ["nvidia"]
        assert set_calls == []

    def test_detection_failure_reports_all_three_vendors(self, monkeypatch):
        monkeypatch.setattr(
            "quickres.webview.bridge.display.get_supported_resolutions", lambda: {(1920, 1080)}
        )
        monkeypatch.setattr(
            "quickres.webview.bridge.display.detect_gpu_vendors", lambda: set()
        )
        api = Api()

        result = api.pick_resolution(1280, 720)

        assert sorted(result["data"]["vendors"]) == ["amd", "intel", "nvidia"]


# ---------------------------------------------------------------------------
# T3.3 -- monitor + recovery methods (locked)
# ---------------------------------------------------------------------------

class TestSetMonitorsEnabledLocked:
    def test_save_pending_false_aborts_before_elevation(self, monkeypatch):
        monkeypatch.setattr(
            "quickres.webview.bridge.config.save_pending", lambda record: False
        )
        elevate_calls = []
        monkeypatch.setattr(
            "quickres.webview.bridge.monitors.set_monitors_enabled",
            lambda *a, **k: elevate_calls.append((a, k)),
        )
        monkeypatch.setattr(
            "quickres.webview.bridge.monitors.enumerate_monitors", lambda: []
        )
        api = Api()

        result = api.set_monitors_enabled(["DISPLAY\\A\\1"], False)

        assert result["ok"] is False
        assert elevate_calls == []


class TestHotkeyConcurrency:
    def test_rapid_double_start_only_registers_once(self, monkeypatch):
        # HK-3: concurrent start does not double-register or leak a thread.
        started = []

        class FakeToggle:
            def __init__(self, key_name, native_res, stretched_res, on_status):
                self.key_name = key_name
                self.native_res = native_res
                self.stretched_res = stretched_res
                self.is_stretched = False

            def start(self):
                started.append(1)

            def stop(self):
                pass

        monkeypatch.setattr("quickres.webview.bridge.HotkeyToggle", FakeToggle)
        api = Api()
        # Simulate "start already in progress" by holding the hotkey lock.
        api._hotkey_lock.acquire()

        result = api.start_hotkey("F6", [1920, 1080], [1440, 1080])

        assert result["ok"] is False
        assert started == []

    def test_start_then_stop_reverts_when_stretched(self, monkeypatch):
        calls = []

        class FakeToggle:
            def __init__(self, key_name, native_res, stretched_res, on_status):
                self.native_res = native_res
                self.stretched_res = stretched_res
                # Armed from the native resolution -- what _stop_hotkey_impl
                # now restores, instead of inferring a target from a flag.
                self.entry_res = native_res
                self.last_applied_res = stretched_res

            def start(self):
                calls.append("start")

            def stop(self):
                calls.append("stop")

        monkeypatch.setattr("quickres.webview.bridge.HotkeyToggle", FakeToggle)
        # Simulate the display having been left on the stretched resolution.
        monkeypatch.setattr(
            "quickres.webview.bridge.display.get_current_resolution",
            lambda: (1440, 1080),
        )
        monkeypatch.setattr(
            "quickres.webview.bridge.display.set_resolution",
            lambda w, h: (calls.append(("revert", w, h)), (True, "reverted"))[1],
        )
        api = Api()
        api.start_hotkey("F6", [1920, 1080], [1440, 1080])

        result = api.stop_hotkey()

        assert result["ok"] is True
        # Round 21 finding 4: toggle.stop() now runs BEFORE the revert call,
        # not after -- see _stop_hotkey_impl's docstring for why (closing
        # the race where the hotkey stayed registered for the whole
        # duration of the unlocked revert call).
        assert calls == ["start", "stop", ("revert", 1920, 1080)]
        assert api._hotkey_running is False


class TestStartHotkeySurfacesRegistrationFailure:
    # Round-3 finding 2: RegisterHotKey can fail (e.g. another app already
    # owns that key combo), but start_hotkey used to unconditionally return
    # {"running": True} regardless of whether HotkeyToggle.start() actually
    # registered anything. HotkeyToggle.start() now raises on a failed
    # registration (quickres/hotkey.py), and bridge_op's existing
    # exception-to-envelope machinery is all that's needed to surface it --
    # no try/except added here (bridge.py's D2 grep-gate invariant).
    def test_reports_error_not_a_false_running_true(self, monkeypatch):
        class FailingToggle:
            def __init__(self, key_name, native_res, stretched_res, on_status):
                pass

            def start(self):
                raise RuntimeError("Failed to register hotkey F6 -- it may already be in use")

        monkeypatch.setattr("quickres.webview.bridge.HotkeyToggle", FailingToggle)
        api = Api()

        result = api.start_hotkey("F6", [1920, 1080], [1440, 1080])

        assert result["ok"] is False
        assert result["kind"] == "error"
        assert "register" in result["message"].lower()
        assert api._hotkey_running is False
        assert api._hotkey_toggle is None


class TestForceUnlockPending:
    def test_refused_while_in_flight(self, monkeypatch):
        in_flight_outcome = recovery.PendingOutcome(
            resolution=recovery.Resolution.IN_FLIGHT,
            instance_id="DISPLAY\\A\\1", friendly_name="A",
            message="Still in progress", elapsed_s=1.0, can_force_unlock=False,
        )
        monkeypatch.setattr(
            "quickres.webview.bridge.Api._resolve_pending_now",
            lambda self: [in_flight_outcome],
        )
        api = Api()

        result = api.force_unlock_pending()

        assert result["ok"] is False

    def test_success_only_writes_unlocked_at(self, monkeypatch):
        unconfirmable_outcome = recovery.PendingOutcome(
            resolution=recovery.Resolution.UNCONFIRMABLE,
            instance_id="DISPLAY\\A\\1", friendly_name="A",
            message="Could not confirm", elapsed_s=200.0, can_force_unlock=True,
        )
        monkeypatch.setattr(
            "quickres.webview.bridge.Api._resolve_pending_now",
            lambda self: [unconfirmable_outcome],
        )
        saved = []
        monkeypatch.setattr(
            "quickres.webview.bridge.config.save_pending",
            lambda record: saved.append(record) or True,
        )
        monkeypatch.setattr(
            "quickres.webview.bridge.config.load_pending",
            lambda: {"action": "disable", "targets": [{"instance_id": "DISPLAY\\A\\1"}]},
        )
        device_mutated = []
        monkeypatch.setattr(
            "quickres.webview.bridge.monitors.set_monitors_enabled",
            lambda *a, **k: device_mutated.append((a, k)),
        )
        api = Api()

        result = api.force_unlock_pending()

        assert result["ok"] is True
        assert len(saved) == 1
        # Round 21 finding 3: unlocked_at is now stamped per-target, not
        # record-wide -- see tests/test_bridge_force_unlock_pending_per_target_scoping.py.
        assert saved[0]["targets"][0]["unlocked_at"] is not None
        assert device_mutated == []
