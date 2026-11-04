import os
import re
import sys
import threading
import uuid

import velopack
from packaging.version import InvalidVersion, Version

from quickres import __version__
from quickres.config import log_msg

REPOSITORY_URL = "https://github.com/lxzydev/QuickRes"
INSTALLER_URL = REPOSITORY_URL + "/releases/latest"
_sessions = {}
_sessions_lock = threading.Lock()


def _parse_version_tuple(value):
    if not isinstance(value, str):
        return None
    match = re.match(r"^\s*v?(\d+(?:\.\d+)*)", value)
    return tuple(map(int, match.group(1).split("."))) if match else None


def is_newer_version(current, remote):
    if not isinstance(current, str) or not isinstance(remote, str):
        return False
    try:
        return Version(remote.strip().removeprefix("v")) > Version(current.strip().removeprefix("v"))
    except InvalidVersion:
        return False


def update_available(current_version, version_info):
    return isinstance(version_info, dict) and is_newer_version(current_version, version_info.get("version"))


def _create_manager():
    prerelease = Version(__version__).is_prerelease
    source = velopack.GithubSource(REPOSITORY_URL, prerelease=prerelease)
    options = velopack.UpdateOptions(False, 10, "win-beta" if prerelease else "win")
    return velopack.UpdateManager(source, options)


def fetch_version_info():
    try:
        manager = _create_manager()
    except Exception as exc:
        log_msg(f"Velopack installation unavailable: {exc}")
        return {
            "version": "", "migration_required": True,
            "installer_url": INSTALLER_URL,
            "message": "Install QuickRes with the new installer, or extract the complete portable ZIP, to enable updates.",
        }
    info = manager.check_for_updates()
    if info is None:
        return {"version": __version__, "download_url": INSTALLER_URL}
    asset = info.TargetFullRelease
    identity = (asset.Version, asset.FileName, asset.SHA256, asset.Size)
    with _sessions_lock:
        token = next((key for key, session in _sessions.items() if session["identity"] == identity), None)
        if token is None:
            if len(_sessions) >= 32:
                raise RuntimeError("Too many available update versions; restart QuickRes before checking again")
            token = uuid.uuid4().hex
            _sessions[token] = {"manager": manager, "info": info, "identity": identity, "ready": False, "lock": threading.Lock()}
    return {
        "version": info.TargetFullRelease.Version,
        "download_url": INSTALLER_URL, "update_token": token,
        "total_bytes": info.TargetFullRelease.Size,
    }


def _get_session(version_info):
    token = version_info.get("update_token") if isinstance(version_info, dict) else None
    with _sessions_lock:
        session = _sessions.get(token) if isinstance(token, str) else None
    if session is None:
        raise RuntimeError("Check for updates again before downloading or installing")
    return session


def apply_update(download_url, version_info=None, *, progress_callback=None, download_only=False, reuse_download=False):
    session = _get_session(version_info)
    with session["lock"]:
        manager, info = session["manager"], session["info"]
        total = info.TargetFullRelease.Size

        def report(stage, percent=0):
            if progress_callback is not None:
                progress_callback({
                    "stage": stage, "downloaded_bytes": total * max(0, min(100, int(percent))) // 100,
                    "total_bytes": total, "error": None,
                })

        if not reuse_download:
            session["ready"] = False
            report("downloading")
            manager.download_updates(info, lambda percent: report("downloading", percent))
            report("verifying", 100)
            session["ready"] = True
        if not session["ready"]:
            raise RuntimeError("No verified update is ready to install")
        if download_only:
            return {"ready": True}
        report("installing", 100)
        manager.wait_exit_then_apply_updates(info, silent=True, restart=True)
        session["ready"] = False
    sys.exit(0)


class UpdateJob:
    def __init__(self, download_url, version_info=None):
        self._download_url = download_url
        self._version_info = version_info
        self._lock = threading.Lock()
        self._thread = None
        self._state = {"stage": "idle", "downloaded_bytes": 0, "total_bytes": None, "error": None}

    @property
    def version_info(self):
        return self._version_info

    def start(self):
        with self._lock:
            if self._thread is not None:
                return False
            self._state["stage"] = "downloading"
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return True

    def snapshot(self):
        with self._lock:
            return dict(self._state)

    def _set_progress(self, progress):
        with self._lock:
            self._state.update(progress)

    def _run(self):
        try:
            apply_update(self._download_url, version_info=self._version_info,
                         progress_callback=self._set_progress, download_only=True)
        except Exception as exc:
            log_msg(f"Update download failed: {exc}")
            self._set_progress({"stage": "failed", "error": str(exc)})
            return
        self._set_progress({"stage": "ready", "error": None})


def confirm_update(download_url, version_info=None, *, reuse_download=False):
    try:
        if reuse_download:
            return apply_update(download_url, version_info=version_info, reuse_download=True)
        return apply_update(download_url, version_info=version_info)
    except SystemExit as exc:
        if exc.code not in (None, 0):
            raise
        os._exit(0)
