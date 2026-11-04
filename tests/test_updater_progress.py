from quickres import updater


def test_update_job_downloads_and_verifies_off_the_calling_thread(monkeypatch):
    calls = []

    def fake_apply_update(url, version_info=None, **kwargs):
        calls.append((url, version_info, kwargs, updater.threading.current_thread()))
        kwargs["progress_callback"]({
            "stage": "downloading", "downloaded_bytes": 10,
            "total_bytes": 20, "error": None,
        })
        kwargs["progress_callback"]({
            "stage": "verifying", "downloaded_bytes": 20,
            "total_bytes": 20, "error": None,
        })
        return {"staged_path": "QuickRes_new.exe"}

    monkeypatch.setattr(updater, "apply_update", fake_apply_update)
    job = updater.UpdateJob("https://lxzy.my/QuickRes_new.exe", {"version": "2.0"})

    assert job.start() is True
    job._thread.join(timeout=1)

    assert not job._thread.is_alive()
    assert calls[0][0:2] == ("https://lxzy.my/QuickRes_new.exe", {"version": "2.0"})
    assert calls[0][2]["download_only"] is True
    assert calls[0][3] is not updater.threading.main_thread()
    assert job.snapshot() == {
        "stage": "ready", "downloaded_bytes": 20,
        "total_bytes": 20, "error": None,
    }


def test_update_job_surfaces_download_failure_without_raising_to_ui(monkeypatch):
    monkeypatch.setattr(
        updater, "apply_update", lambda *a, **kw: (_ for _ in ()).throw(OSError("offline"))
    )
    job = updater.UpdateJob("https://lxzy.my/QuickRes_new.exe")

    assert job.start() is True
    job._thread.join(timeout=1)

    assert job.snapshot()["stage"] == "failed"
    assert job.snapshot()["error"] == "offline"
