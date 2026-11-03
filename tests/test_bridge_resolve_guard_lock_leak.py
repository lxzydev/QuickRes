"""Round 9 finding (HIGH): `_resolve_guard_unbounded_under_lock` acquires
`self._op_lock` by hand (it sits outside `bridge_op`'s own try/finally
machinery -- it is not itself `bridge_op`-wrapped) and used to release it
with a bare statement after the guard-check/trim body, not a try/finally.
Any exception raised by `_clear_or_trim_pending_record` after a successful
`guard.check()` permanently leaked the lock -- every `lock=True` bridge_op
method (`set_monitors_enabled`, `keep_disabled`, `revert_now`,
`force_unlock_pending`, `recheck_pending`) would report "busy" forever,
until the app restarted.
"""
import threading
import time
from unittest.mock import Mock

import pytest

from quickres.webview.bridge import Api
from quickres import config, monitors


@pytest.fixture(autouse=True)
def _isolated_app_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", str(tmp_path))
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(config, "PENDING_PATH", str(tmp_path / "pending_restore.json"))
    yield


def _api_with_confirmed_disable(monkeypatch):
    monkeypatch.setattr(
        "quickres.webview.bridge.monitors.enumerate_monitors",
        lambda: [{"instance_id": "DISPLAY\\A\\1", "friendly_name": "A", "enabled": True}],
    )
    monkeypatch.setattr(
        "quickres.webview.bridge.monitors.set_monitors_enabled",
        lambda ids, enabled, **kwargs: [(iid, True, "ok", monitors.OUTCOME_CONFIRMED) for iid in ids],
    )
    api = Api()
    api.set_monitors_enabled(["DISPLAY\\A\\1"], False)
    return api


class TestLockReleasedEvenWhenTrimRaises:
    def test_lock_is_released_after_clear_or_trim_pending_record_raises(self, monkeypatch):
        api = _api_with_confirmed_disable(monkeypatch)
        guard = api._pending_guard
        monkeypatch.setattr(
            api, "_clear_or_trim_pending_record",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("trim boom")),
        )

        future = time.time() + 3600
        with pytest.raises(RuntimeError, match="trim boom"):
            api._resolve_guard_unbounded_under_lock(guard, now=future)

        # The bug: the old code released the lock with a bare statement
        # AFTER the trim call, never reached once the trim raised -- the
        # lock stayed acquired forever. Proven here two ways: the raw Lock
        # object reports itself unlocked, and a fresh non-blocking acquire
        # (exactly what bridge_op(lock=True) does for every real monitor
        # operation) succeeds immediately instead of reporting busy.
        assert api._op_lock.locked() is False
        reacquired = api._op_lock.acquire(blocking=False)
        assert reacquired is True, (
            "lock leaked: a subsequent monitor operation would report "
            "busy forever after this exception"
        )
        api._op_lock.release()

    def test_a_real_monitor_operation_is_not_locked_out_after_the_leak_scenario(
        self, monkeypatch
    ):
        # End-to-end proof at the bridge_op boundary: keep_disabled (a real
        # lock=True method) must succeed normally after a prior
        # _resolve_guard_unbounded_under_lock call raised past its trim step, not
        # report kind="busy" forever.
        api = _api_with_confirmed_disable(monkeypatch)
        guard = api._pending_guard
        real_trim = api._clear_or_trim_pending_record
        monkeypatch.setattr(
            api, "_clear_or_trim_pending_record",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("trim boom")),
        )
        future = time.time() + 3600
        with pytest.raises(RuntimeError):
            api._resolve_guard_unbounded_under_lock(guard, now=future)

        # Restore the real trim implementation -- the fault under test is
        # the lock leak, not this second, unrelated raise.
        monkeypatch.setattr(api, "_clear_or_trim_pending_record", real_trim)
        result = api.keep_disabled()

        assert result["ok"] is True
        assert result["kind"] != "busy"

    def test_lock_still_released_on_the_success_path(self, monkeypatch):
        # Regression guard: the fix must not accidentally leave the lock
        # held on the ordinary, non-raising path either.
        api = _api_with_confirmed_disable(monkeypatch)
        guard = api._pending_guard

        future = time.time() + 3600
        api._resolve_guard_unbounded_under_lock(guard, now=future)

        assert api._op_lock.locked() is False


class TestLockAcquireGuardUnboundedTimeoutNone:
    """Unit coverage for the new `timeout=None` mode added to
    `_LockAcquireGuard` (round 9) -- an unbounded blocking acquire whose
    `__exit__` always releases, distinct from the pre-existing `timeout=0`
    (non-blocking) and `timeout>0` (bounded blocking) modes.
    """

    def test_timeout_none_blocks_until_available_then_releases_on_raise(self):
        from quickres.webview.bridge import _LockAcquireGuard

        lock = threading.Lock()
        lock.acquire()
        released_by_other = threading.Event()

        def _release_soon():
            time.sleep(0.05)
            lock.release()
            released_by_other.set()

        threading.Thread(target=_release_soon, daemon=True).start()

        with pytest.raises(RuntimeError):
            with _LockAcquireGuard(lock, timeout=None) as guard:
                assert released_by_other.is_set(), (
                    "timeout=None must actually block until the lock frees"
                )
                assert guard.acquired is True
                raise RuntimeError("boom")

        assert lock.locked() is False


class TestLockAcquireGuardNamedConstructors:
    """Round 14 readability finding: a raw `timeout=` kwarg gives no local
    indication that 0/float/None select three qualitatively different kinds
    of wait. These named constructors must produce exactly the same acquire
    behavior as the equivalent raw `timeout=` value -- proven here for each
    of the three modes.
    """

    def test_non_blocking_does_not_wait_or_release_a_held_lock(self):
        from quickres.webview.bridge import _LockAcquireGuard

        lock = threading.Lock()
        lock.acquire()
        observed_lock = Mock(wraps=lock)

        with _LockAcquireGuard.non_blocking(observed_lock) as guard:
            assert guard.acquired is False

        observed_lock.acquire.assert_called_once_with(blocking=False)
        assert lock.locked() is True
        lock.release()

    def test_non_blocking_acquires_when_lock_is_free(self):
        from quickres.webview.bridge import _LockAcquireGuard

        lock = threading.Lock()

        with _LockAcquireGuard.non_blocking(lock) as guard:
            assert guard.acquired is True

        assert lock.locked() is False

    def test_bounded_uses_timeout_and_does_not_release_a_held_lock(self):
        from quickres.webview.bridge import _LockAcquireGuard

        lock = threading.Lock()
        lock.acquire()
        observed_lock = Mock(wraps=lock)

        with _LockAcquireGuard.bounded(observed_lock, 0.1) as guard:
            assert guard.acquired is False

        observed_lock.acquire.assert_called_once_with(blocking=True, timeout=0.1)
        assert lock.locked() is True
        lock.release()

    def test_bounded_acquires_if_released_within_the_timeout(self):
        from quickres.webview.bridge import _LockAcquireGuard

        lock = threading.Lock()
        lock.acquire()

        def _release_soon():
            time.sleep(0.05)
            lock.release()

        threading.Thread(target=_release_soon, daemon=True).start()

        with _LockAcquireGuard.bounded(lock, 5.0) as guard:
            assert guard.acquired is True

        assert lock.locked() is False

    def test_unbounded_blocks_until_available_then_releases_on_raise(self):
        from quickres.webview.bridge import _LockAcquireGuard

        lock = threading.Lock()
        lock.acquire()
        released_by_other = threading.Event()

        def _release_soon():
            time.sleep(0.05)
            lock.release()
            released_by_other.set()

        threading.Thread(target=_release_soon, daemon=True).start()

        with pytest.raises(RuntimeError):
            with _LockAcquireGuard.unbounded(lock) as guard:
                assert released_by_other.is_set(), (
                    "unbounded must actually block until the lock frees"
                )
                assert guard.acquired is True
                raise RuntimeError("boom")

        assert lock.locked() is False
