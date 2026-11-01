"""`HotkeyToggle` used to decide its toggle direction from `_is_stretched`, an
in-memory bool seeded to False. That seed asserts "the session begins on
`native_res`", which is simply not true when the user arms the hotkey while
already sitting on `stretched_res`: the first press then re-applied the
resolution that was already active (a press that visibly did nothing) and left
the flag inverted relative to reality for every press after it.

`_toggle` now reads the current mode instead, and `start()` records the
resolution observed at arm time as `entry_res` so `webview/bridge.py`'s
`_stop_hotkey_impl` can put the display back where the user left it rather than
inferring a target from the flag.
"""
import pytest

import quickres.hotkey as hotkey_mod
from quickres.hotkey import HotkeyToggle


def _toggle_for(monkeypatch, *, current, applied=None, ok=True):
    """A toggle whose `set_resolution`/`get_current_resolution` are stubbed.

    `current` is the resolution the display reports; `applied` collects every
    resolution the toggle asks for.
    """
    monkeypatch.setattr(hotkey_mod, "get_current_resolution", lambda: current)

    def fake_set_resolution(width, height):
        if applied is not None:
            applied.append((width, height))
        return ok, f"Now running {width} x {height}"

    monkeypatch.setattr(hotkey_mod, "set_resolution", fake_set_resolution)
    return HotkeyToggle(
        "F6",
        native_res=(1920, 1440),
        stretched_res=(2560, 1440),
        on_status=lambda message: None,
    )


class TestEntryResolutionCapture:
    def test_start_records_the_resolution_observed_at_arm_time(self, monkeypatch):
        monkeypatch.setattr(hotkey_mod, "get_current_resolution", lambda: (2560, 1440))
        monkeypatch.setattr(
            HotkeyToggle, "_run", lambda self: self._ready.set() or setattr(self, "_registered", True)
        )
        toggle = HotkeyToggle(
            "F6",
            native_res=(1920, 1440),
            stretched_res=(2560, 1440),
            on_status=lambda message: None,
        )
        assert toggle.entry_res is None, "nothing observed before start()"

        toggle.start()

        assert toggle.entry_res == (2560, 1440)

    def test_entry_res_is_none_when_the_display_cannot_be_read(self, monkeypatch):
        monkeypatch.setattr(hotkey_mod, "get_current_resolution", lambda: None)
        monkeypatch.setattr(
            HotkeyToggle, "_run", lambda self: self._ready.set() or setattr(self, "_registered", True)
        )
        toggle = HotkeyToggle(
            "F6",
            native_res=(1920, 1440),
            stretched_res=(2560, 1440),
            on_status=lambda message: None,
        )

        toggle.start()

        assert toggle.entry_res is None


class TestToggleDirectionFollowsTheDisplayNotTheFlag:
    def test_first_press_while_already_stretched_is_not_a_no_op(self, monkeypatch):
        """The regression this fixes: armed while the display is already on
        `stretched_res`, the first press must move to `native_res` rather than
        re-apply the resolution that is already active.
        """
        applied = []
        toggle = _toggle_for(monkeypatch, current=(2560, 1440), applied=applied)

        toggle._toggle()

        assert applied == [(1920, 1440)]
        assert toggle.is_stretched is False

    def test_first_press_while_on_native_goes_to_stretched(self, monkeypatch):
        applied = []
        toggle = _toggle_for(monkeypatch, current=(1920, 1440), applied=applied)

        toggle._toggle()

        assert applied == [(2560, 1440)]
        assert toggle.is_stretched is True

    def test_direction_follows_an_externally_changed_resolution(self, monkeypatch):
        """Something else (a game, another tool, Windows) changed the mode to
        `stretched_res` behind the toggle's back. The next press must go to
        `native_res` even though the flag still says "not stretched".
        """
        applied = []
        toggle = _toggle_for(monkeypatch, current=(2560, 1440), applied=applied)
        assert toggle.is_stretched is False

        toggle._toggle()

        assert applied == [(1920, 1440)]

    def test_flag_does_not_flip_when_the_resolution_change_fails(self, monkeypatch):
        toggle = _toggle_for(monkeypatch, current=(1920, 1440), ok=False)

        toggle._toggle()

        assert toggle.is_stretched is False


class TestUnreadableDisplayFallsBackToTheFlag:
    def test_falls_back_to_is_stretched_when_current_resolution_is_unknown(
        self, monkeypatch
    ):
        """A failed `EnumDisplaySettingsW` must degrade to the previous
        flag-driven behaviour rather than break the toggle outright.
        """
        applied = []
        toggle = _toggle_for(monkeypatch, current=None, applied=applied)
        toggle.is_stretched = True

        toggle._toggle()

        assert applied == [(1920, 1440)]
        assert toggle.is_stretched is False

    def test_fallback_from_not_stretched_targets_stretched(self, monkeypatch):
        applied = []
        toggle = _toggle_for(monkeypatch, current=None, applied=applied)

        toggle._toggle()

        assert applied == [(2560, 1440)]
        assert toggle.is_stretched is True
