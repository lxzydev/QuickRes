import pytest

from quickres import config
from quickres.webview.bridge import Api


@pytest.fixture(autouse=True)
def _isolated_app_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", str(tmp_path))
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(config, "PENDING_PATH", str(tmp_path / "pending_restore.json"))


def test_start_update_reuses_the_active_background_job(monkeypatch):
    instances = []

    class FakeJob:
        def __init__(self, url, version_info=None):
            self.url = url
            self.version_info = version_info
            self.state = {
                "stage": "downloading", "downloaded_bytes": 0,
                "total_bytes": None, "error": None,
            }
            instances.append(self)

        def start(self):
            return True

        def snapshot(self):
            return dict(self.state)

    monkeypatch.setattr("quickres.webview.bridge.updater.UpdateJob", FakeJob)
    api = Api()

    first = api.start_update("https://lxzy.my/QuickRes_new.exe", {"version": "2.0"})
    second = api.start_update("https://lxzy.my/QuickRes_new.exe", {"version": "2.0"})

    assert first["ok"] is True
    assert second["ok"] is True
    assert len(instances) == 1
    assert api.get_update_status()["data"]["stage"] == "downloading"


def test_install_verified_update_uses_safety_handoff_and_existing_rollback_path(monkeypatch):
    class ReadyJob:
        version_info = {"version": "2.0"}

        def snapshot(self):
            return {
                "stage": "ready", "downloaded_bytes": 20,
                "total_bytes": 20, "error": None,
            }

    api = Api()
    api._update_job = ReadyJob()
    events = []
    monkeypatch.setattr(api, "_prepare_update_handoff_locked", lambda: events.append("handoff"))

    def fake_apply_update(url, version_info=None, **kwargs):
        events.append("apply")
        assert url is None
        assert version_info == {"version": "2.0"}
        assert kwargs == {"reuse_download": True}
        return {"started": True}

    monkeypatch.setattr("quickres.webview.bridge.updater.apply_update", fake_apply_update)

    result = api.install_downloaded_update()

    assert result["ok"] is True
    assert result["data"] == {"started": True}
    assert events == ["handoff", "apply"]


@pytest.mark.parametrize("failure", [None, OSError("could not stage update")])
def test_verified_install_exits_the_process_only_after_success(monkeypatch, failure):
    from types import SimpleNamespace
    from quickres import updater

    api = Api()
    api._update_job = SimpleNamespace(snapshot=lambda: {"stage": "ready"}, version_info={})
    events = []
    monkeypatch.setattr(api, "_prepare_update_handoff_locked", lambda: events.append("handoff"))

    def apply(*args, **kwargs):
        events.append("stage")
        if failure:
            raise failure
        raise SystemExit(0)

    monkeypatch.setattr(updater, "apply_update", apply)
    monkeypatch.setattr(updater.os, "_exit", lambda code: events.append(("exit", code)))

    result = api.install_downloaded_update()

    assert events == (["handoff", "stage"] if failure else ["handoff", "stage", ("exit", 0)])
    assert result["ok"] is (failure is None)
    assert not api._op_lock.locked()
