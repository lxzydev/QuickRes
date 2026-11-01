"""Stopping the hotkey used to change the display to `native_res` whenever
`toggle.is_stretched` was True -- a decision made purely from an in-memory
bool, never checked against the display.

That was wrong in a way real users hit. `native_res`/`stretched_res` come from
user-editable panel fields that `panel.html`'s `applyHotkeyDefaults` pre-fills
from whatever resolution happens to be active on first launch, so someone who
first opened QuickRes while already stretched ends up with the two values the
wrong way round in `config.json`. Combined with `is_stretched` being seeded to
False (so arming the hotkey while already on `stretched_res` made the first
press a visible no-op that inverted the flag), closing QuickRes threw the
display TO the stretched resolution the user had just toggled away from.

Reported repro, reconstructed here from a real `quickres.log`: true native
2560x1440, stretch 1920x1440, but `config.json` holding
`native_res: 1920x1440` / `stretched_res: 2560x1440`.

    21:57:02  Now running 2560 x 1440   press 1 -> stretched_res (no-op)
    21:57:05  Now running 1920 x 1440   press 2 -> native_res
    21:57:09  Now running 2560 x 1440   press 3 -> stretched_res
    close -> is_stretched True -> applied native_res == 1920x1440

Fix: `_stop_hotkey_impl` restores `HotkeyToggle.entry_res` -- the resolution
observed when the hotkey was armed -- which is correct whichever way round the
two configured fields are labelled.
"""
import pytest

from quickres import config
from quickres.webview.bridge import Api


@pytest.fixture(autouse=True)
def _isolated_app_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", str(tmp_path))
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(config, "PENDING_PATH", str(tmp_path / "pending_restore.json"))
    yield


class _FakeToggle:
    """Minimal stand-in for `HotkeyToggle` as `_stop_hotkey_impl` uses it:
    a `stop()` that reports clean teardown, the two configured resolutions,
    and the observed `entry_res` the restore decision is made from.
    """

    def __init__(self, *, entry_res, native_res=(1920, 1440), stretched_res=(2560, 1440)):
        self.entry_res = entry_res
        self.last_applied_res = (1920, 1440)
        self.native_res = native_res
        self.stretched_res = stretched_res
        self.is_running = False
        self.stop_called = False

    def stop(self):
        self.stop_called = True
        return True


def _api_with(toggle):
    api = Api()
    api._hotkey_toggle = toggle
    api._hotkey_running = True
    return api


@pytest.fixture()
def applied(monkeypatch):
    """Record every `set_resolution` the stop path performs."""
    calls = []

    def fake_set_resolution(width, height):
        calls.append((width, height))
        return True, f"Now running {width} x {height}"

    monkeypatch.setattr(
        "quickres.webview.bridge.display.set_resolution", fake_set_resolution
    )
    return calls


def _observe(monkeypatch, resolution):
    monkeypatch.setattr(
        "quickres.webview.bridge.display.get_current_resolution",
        lambda: resolution,
    )


class TestStopRestoresTheEntryResolution:
    def test_reported_repro_leaves_the_users_native_resolution_alone(
        self, monkeypatch, applied
    ):
        """The exact reported bug: backwards config, and the display is left
        on the resolution the user armed the hotkey from. Stopping must not
        touch the resolution at all -- the old code applied `native_res`
        (1920x1440) here and threw the user to the stretched resolution.
        """
        toggle = _FakeToggle(
            entry_res=(2560, 1440),
            native_res=(1920, 1440),      # backwards, as in the real config.json
            stretched_res=(2560, 1440),
        )
        _observe(monkeypatch, (2560, 1440))

        _api_with(toggle)._stop_hotkey_impl()

        assert toggle.stop_called
        assert applied == [], (
            "stop must not change the resolution when the display is already "
            "on the resolution the hotkey was armed from"
        )

    def test_restores_entry_resolution_when_left_on_the_other_target(
        self, monkeypatch, applied
    ):
        """Closing while the display is on a resolution the toggle applied,
        and which is NOT where the user started, must put the entry
        resolution back -- the genuinely useful half of the old behaviour,
        preserved here and now correct even with the fields reversed.
        """
        toggle = _FakeToggle(
            entry_res=(2560, 1440),
            native_res=(1920, 1440),
            stretched_res=(2560, 1440),
        )
        _observe(monkeypatch, (1920, 1440))

        _api_with(toggle)._stop_hotkey_impl()

        assert applied == [(2560, 1440)]

    def test_correctly_configured_stretched_session_still_reverts(
        self, monkeypatch, applied
    ):
        """The intended flow with correctly-labelled fields: armed from
        native, closed while stretched -> restore native. Guards against the
        fix regressing the behaviour the revert existed for.
        """
        toggle = _FakeToggle(
            entry_res=(2560, 1440),
            native_res=(2560, 1440),
            stretched_res=(1920, 1440),
        )
        _observe(monkeypatch, (1920, 1440))

        _api_with(toggle)._stop_hotkey_impl()

        assert applied == [(2560, 1440)]


class TestStopLeavesForeignResolutionsAlone:
    def test_resolution_this_toggle_never_applied_is_not_undone(
        self, monkeypatch, applied
    ):
        """If the current mode is neither configured resolution, something
        else set it (a game, another tool, the panel's own Apply button).
        Shutdown must not stomp on it.
        """
        toggle = _FakeToggle(
            entry_res=(2560, 1440),
            native_res=(1920, 1440),
            stretched_res=(2560, 1440),
        )
        _observe(monkeypatch, (1280, 1024))

        _api_with(toggle)._stop_hotkey_impl()

        assert applied == []

    def test_unknown_entry_resolution_restores_nothing(self, monkeypatch, applied):
        """`entry_res` is None when `get_current_resolution()` failed at
        `start()`. There is nothing known to restore, so guess nothing.
        """
        toggle = _FakeToggle(entry_res=None)
        _observe(monkeypatch, (1920, 1440))

        _api_with(toggle)._stop_hotkey_impl()

        assert applied == []

    def test_unreadable_current_resolution_reports_retryable_failure(
        self, monkeypatch, applied
    ):
        toggle = _FakeToggle(entry_res=(2560, 1440))
        _observe(monkeypatch, None)

        api = _api_with(toggle)
        with pytest.raises(RuntimeError, match="Could not read the current resolution"):
            api._stop_hotkey_impl()

        assert applied == []
        assert api._hotkey_toggle is toggle


class TestFailedRestoreIsNotReportedAsACleanStop:
    def test_failed_restore_raises(self, monkeypatch):
        monkeypatch.setattr(
            "quickres.webview.bridge.display.set_resolution",
            lambda w, h: (False, "driver refused resolution"),
        )
        toggle = _FakeToggle(
            entry_res=(2560, 1440),
            native_res=(1920, 1440),
            stretched_res=(2560, 1440),
        )
        _observe(monkeypatch, (1920, 1440))

        with pytest.raises(RuntimeError, match="driver refused resolution"):
            _api_with(toggle)._stop_hotkey_impl()


class TestRestoreHelperRequiresTheHotkeyLock:
    def test_calling_without_the_lock_held_trips_the_assertion(self, monkeypatch):
        """`_restore_entry_resolution` carries no `_under_lock` suffix, so by
        this class's documented naming convention the caller must already
        hold the lock. Enforce that with the same assertion style the
        `self._op_lock` family uses.
        """
        toggle = _FakeToggle(entry_res=(2560, 1440))
        _observe(monkeypatch, (1920, 1440))
        api = _api_with(toggle)

        with pytest.raises(AssertionError):
            api._restore_entry_resolution(toggle)
