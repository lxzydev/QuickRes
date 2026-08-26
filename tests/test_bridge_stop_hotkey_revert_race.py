"""Round 21 finding 4 (HIGH): `_stop_hotkey_impl` used to read
`toggle.is_stretched` and call `display.set_resolution` for the
native-resolution revert BEFORE calling `toggle.stop()` -- so the hotkey
stayed registered for the whole duration of that revert call. A physical
hotkey press landing in that window dispatches WM_HOTKEY on the listener
thread, which runs `HotkeyToggle._toggle()` (its own `set_resolution` call)
CONCURRENTLY with `_stop_hotkey_impl`'s unlocked revert call -- two
unsynchronized `ChangeDisplaySettingsW` calls racing on two different
threads, with no ordering guarantee over which one wins.

Fix: `_stop_hotkey_impl` now calls `toggle.stop()` (which unregisters the
hotkey and joins the listener thread -- so it does not return until any
message the worker thread was already processing, including an in-flight
`_toggle()` call, has fully finished) BEFORE making the restore decision or
calling the revert's `display.set_resolution`. This closes the race: by
the time the revert call runs, no hotkey press can still be in flight and
no new one can be dispatched (the hotkey is unregistered).
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


class _JoinAwareFakeToggle:
    """Models just enough of the real `HotkeyToggle.stop()`/`_toggle()`
    interplay to prove the ordering fix: like the real `stop()`'s
    `self._thread.join()`, this `stop()` does not return until a
    concurrently in-flight "hotkey press" (simulated by the test via
    `press_in_progress`) has finished -- because the real Win32 message
    loop only exits (letting `join()` return) after it has fully finished
    processing whatever message it was already handling, even one queued
    just before WM_QUIT takes effect.
    """

    def __init__(self, press_in_progress: threading.Event):
        self.native_res = (1920, 1080)
        self.stretched_res = (1440, 1080)
        # Armed from the native resolution. Paired with the
        # get_current_resolution stub below (reporting the stretched
        # resolution), this makes _stop_hotkey_impl actually perform its
        # restore -- without which this test would pass vacuously, never
        # reaching the set_resolution call whose overlap it exists to detect.
        self.entry_res = (1920, 1080)
        self._press_in_progress = press_in_progress
        self.stop_called = threading.Event()

    def stop(self):
        self.stop_called.set()
        deadline = time.monotonic() + 2.0
        while self._press_in_progress.is_set() and time.monotonic() < deadline:
            time.sleep(0.005)


class TestStopHotkeyImplDoesNotRaceAConcurrentHotkeyPress:
    def test_revert_never_overlaps_an_in_flight_hotkey_press(self, monkeypatch):
        exclusive = threading.Lock()
        overlap_detected = threading.Event()
        press_entered = threading.Event()
        press_release = threading.Event()
        press_in_progress = threading.Event()

        def fake_press_set_resolution(w, h):
            acquired = exclusive.acquire(blocking=False)
            if not acquired:
                overlap_detected.set()
            press_entered.set()
            press_release.wait(timeout=2)
            if acquired:
                exclusive.release()
            return True, "ok"

        def fake_revert_set_resolution(w, h):
            acquired = exclusive.acquire(blocking=False)
            if not acquired:
                overlap_detected.set()
            else:
                exclusive.release()
            return True, "ok"

        monkeypatch.setattr(
            "quickres.webview.bridge.display.set_resolution", fake_revert_set_resolution
        )
        monkeypatch.setattr(
            "quickres.webview.bridge.display.get_current_resolution",
            lambda: (1440, 1080),
        )

        toggle = _JoinAwareFakeToggle(press_in_progress)
        api = Api()
        api._hotkey_toggle = toggle
        api._hotkey_running = True

        def press():
            press_in_progress.set()
            fake_press_set_resolution(*toggle.native_res)
            press_in_progress.clear()

        press_thread = threading.Thread(target=press, daemon=True)
        press_thread.start()
        assert press_entered.wait(timeout=2), "press never entered set_resolution"

        stop_thread = threading.Thread(target=api._stop_hotkey_impl, daemon=True)
        stop_thread.start()

        # Give _stop_hotkey_impl a brief window: with the fix, it must be
        # blocked inside toggle.stop() waiting out the in-flight press --
        # not already past it and into the revert call.
        time.sleep(0.15)
        assert toggle.stop_called.is_set(), "toggle.stop() must be called"
        assert stop_thread.is_alive(), (
            "_stop_hotkey_impl must block on toggle.stop() until the "
            "in-flight hotkey press finishes, not race ahead into the revert"
        )

        press_release.set()
        press_thread.join(timeout=2)
        stop_thread.join(timeout=2)

        assert not stop_thread.is_alive()
        assert not overlap_detected.is_set(), (
            "the revert's set_resolution and the hotkey press's set_resolution "
            "ran concurrently -- both threads issued ChangeDisplaySettingsW "
            "unsynchronized"
        )
