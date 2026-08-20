import itertools
import json
import os
import sys

import pytest

import main
from quickres import config


@pytest.fixture(autouse=True)
def _isolated_app_dir(tmp_path, monkeypatch):
    # 1d: _run_elevated_helper now validates args.result_file via
    # recovery.is_safe_result_path(path, config.APP_DIR) before writing --
    # every test's result_file must live under (a monkeypatched) APP_DIR and
    # match the monitor_op_result_<pid>_<ms>.json shape.
    monkeypatch.setattr(config, "APP_DIR", str(tmp_path))
    # config.LOG_PATH is computed once at import time from the real APP_DIR,
    # not re-derived from it -- patching APP_DIR alone leaves log_msg() (hit
    # by several tests below on the auto-revert path) writing into the
    # developer's actual %LOCALAPPDATA%\QuickRes\quickres.log.
    monkeypatch.setattr(config, "LOG_PATH", str(tmp_path / "quickres.log"))
    yield


def _fake_worker_op(results_by_id):
    def worker(op, instance_id):
        ok, message = results_by_id[instance_id]
        if isinstance(ok, Exception):
            raise ok
        return ok, message
    return worker


# _guarded_disable_argv's default and _force_deadline_after's mocked
# time.monotonic() ceiling must agree on the same guard-timeout value --
# main.py computes its real deadline from --guard-timeout-s, and a caller
# that changed one without the other would make the mocked clock never
# reach a timeout main.py is actually waiting for, hanging the guard loop
# forever instead of failing with a clear assertion. One shared constant
# instead of two independently-typed magic numbers closes that gap.
_DEFAULT_GUARD_TIMEOUT_S = "1"


def _guarded_disable_argv(tmp_path, suffix, *, timeout_s=_DEFAULT_GUARD_TIMEOUT_S):
    command_file = tmp_path / f"monitor_guard_command_111_{suffix}.json"
    completion_file = tmp_path / f"monitor_guard_result_111_{suffix}.json"
    argv = [
        "--monitor-op", "guarded-disable",
        "--instance-id", "A",
        "--result-file", str(tmp_path / f"monitor_op_result_111_{suffix}.json"),
        "--guard-command-file", str(command_file),
        "--guard-result-file", str(completion_file),
        "--guard-timeout-s", timeout_s,
    ]
    return command_file, completion_file, argv


def _force_deadline_after(monkeypatch, *poll_values, guard_timeout_s=float(_DEFAULT_GUARD_TIMEOUT_S)):
    # Forces exactly len(poll_values) guard-loop iterations: the first
    # monotonic() call computes the deadline (0.0 + guard_timeout_s), each
    # of poll_values lets one while-condition check pass and its loop body
    # run, and monotonic() then keeps returning the timeout value forever
    # -- so a future extra monotonic() call per iteration fails the
    # deadline check (and the test's own assertions) instead of raising an
    # opaque StopIteration. guard_timeout_s must match whatever timeout_s
    # was passed to _guarded_disable_argv for the same test, or the real
    # while loop in main.py never sees its mocked clock cross the actual
    # deadline and spins forever -- pass it explicitly if that test uses a
    # non-default timeout_s.
    monotonic_values = itertools.chain(
        [0.0], poll_values, itertools.repeat(guard_timeout_s)
    )
    monkeypatch.setattr(main.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(main.time, "sleep", lambda _seconds: None)


def _force_single_poll_timeout(monkeypatch):
    _force_deadline_after(monkeypatch, 0.0)


def test_single_instance_id_produces_one_result_entry(monkeypatch, tmp_path):
    monkeypatch.setattr(
        main, "run_elevated_worker_op", _fake_worker_op({"A": (True, "Disabled")})
    )
    result_file = str(tmp_path / "monitor_op_result_111_222.json")

    exit_code = main._run_elevated_helper(
        ["--monitor-op", "disable", "--instance-id", "A", "--result-file", result_file]
    )

    assert exit_code == 0
    with open(result_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data == {"results": [{"instance_id": "A", "ok": True, "message": "Disabled"}]}


def test_three_instance_id_occurrences_produce_three_result_entries(monkeypatch, tmp_path):
    monkeypatch.setattr(
        main,
        "run_elevated_worker_op",
        _fake_worker_op(
            {
                "A": (True, "Disabled"),
                "B": (True, "Disabled"),
                "C": (True, "Disabled"),
            }
        ),
    )
    result_file = str(tmp_path / "monitor_op_result_111_223.json")

    exit_code = main._run_elevated_helper(
        [
            "--monitor-op", "disable",
            "--instance-id", "A",
            "--instance-id", "B",
            "--instance-id", "C",
            "--result-file", result_file,
        ]
    )

    assert exit_code == 0
    with open(result_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert [r["instance_id"] for r in data["results"]] == ["A", "B", "C"]
    assert all(r["ok"] is True for r in data["results"])


def test_one_device_raises_internally_others_still_processed(monkeypatch, tmp_path):
    monkeypatch.setattr(
        main,
        "run_elevated_worker_op",
        _fake_worker_op(
            {
                "A": (True, "Disabled"),
                "B": (RuntimeError("boom"), None),
                "C": (True, "Disabled"),
            }
        ),
    )
    result_file = str(tmp_path / "monitor_op_result_111_224.json")

    exit_code = main._run_elevated_helper(
        [
            "--monitor-op", "disable",
            "--instance-id", "A",
            "--instance-id", "B",
            "--instance-id", "C",
            "--result-file", result_file,
        ]
    )

    assert exit_code == 1  # not all ok
    with open(result_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    results = {r["instance_id"]: r for r in data["results"]}
    assert results["A"]["ok"] is True
    assert results["C"]["ok"] is True
    assert results["B"]["ok"] is False
    assert "boom" in results["B"]["message"]


def test_unsafe_result_file_path_is_refused_before_writing(monkeypatch, tmp_path):
    # 1d: main.py trusts args.result_file from its own argv unconditionally
    # today -- mirror monitors.read_op_result's read-side check with a
    # write-side recovery.is_safe_result_path validation. A path outside
    # APP_DIR (or with the wrong basename shape) must never be written to.
    monkeypatch.setattr(
        main, "run_elevated_worker_op", _fake_worker_op({"A": (True, "Disabled")})
    )
    outside_dir = tmp_path.parent / "outside_main_helper"
    outside_dir.mkdir(exist_ok=True)
    unsafe_result_file = str(outside_dir / "monitor_op_result_111_225.json")

    exit_code = main._run_elevated_helper(
        ["--monitor-op", "disable", "--instance-id", "A", "--result-file", unsafe_result_file]
    )

    assert exit_code == 1
    assert not os.path.exists(unsafe_result_file)


def test_unsafe_result_file_path_diagnostic_survives_console_false_build(monkeypatch, tmp_path):
    # QuickRes.spec builds with console=False, which leaves both sys.stdout
    # and sys.stderr as None (no console attached, same as pythonw.exe) --
    # print(..., file=sys.stderr) with sys.stderr=None falls back to
    # sys.stdout (CPython's print() treats an explicit file=None the same
    # as an omitted one), so both must be None to reproduce the real
    # frozen-build crash: print() then tries None.write() and raises
    # AttributeError instead of the intended clean return 1. The diagnostic
    # must still reach an observable place (quickres.log via
    # config.log_msg) and the function must still return exit code 1.
    monkeypatch.setattr(
        main, "run_elevated_worker_op", _fake_worker_op({"A": (True, "Disabled")})
    )
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    logged = []
    monkeypatch.setattr(config, "log_msg", lambda msg: logged.append(msg))

    outside_dir = tmp_path.parent / "outside_main_helper_console_false"
    outside_dir.mkdir(exist_ok=True)
    unsafe_result_file = str(outside_dir / "monitor_op_result_111_226.json")

    exit_code = main._run_elevated_helper(
        ["--monitor-op", "disable", "--instance-id", "A", "--result-file", unsafe_result_file]
    )

    assert exit_code == 1
    assert not os.path.exists(unsafe_result_file)
    assert len(logged) == 1
    assert unsafe_result_file in logged[0]


def test_no_batch_flag_code_path_exists():
    with open("main.py", "r", encoding="utf-8") as f:
        content = f.read()
    assert "--batch" not in content


def test_guarded_disable_reuses_the_same_elevated_helper_for_revert(monkeypatch, tmp_path):
    calls = []

    def worker(op, instance_id):
        calls.append((op, instance_id))
        return True, f"{op} {instance_id}"

    monkeypatch.setattr(main, "run_elevated_worker_op", worker)
    command_file, completion_file, argv = _guarded_disable_argv(tmp_path, "227")
    command_file.write_text(json.dumps({"action": "revert"}), encoding="utf-8")

    exit_code = main._run_elevated_helper(argv)

    assert exit_code == 0
    assert calls == [("disable", "A"), ("enable", "A")]
    assert json.loads(completion_file.read_text(encoding="utf-8")) == {
        "action": "revert",
        "reason": None,
        "results": [{"instance_id": "A", "ok": True, "message": "enable A"}],
    }


def test_guarded_disable_refuses_an_unsafe_command_path_before_any_operation(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(main, "run_elevated_worker_op", lambda *args: calls.append(args))
    result_file = str(tmp_path / "monitor_op_result_111_228.json")
    completion_file = tmp_path / "monitor_guard_result_111_228.json"

    exit_code = main._run_elevated_helper(
        [
            "--monitor-op", "guarded-disable",
            "--instance-id", "A",
            "--result-file", result_file,
            "--guard-command-file", str(tmp_path.parent / "monitor_guard_command_111_228.json"),
            "--guard-result-file", str(completion_file),
            "--guard-timeout-s", "1",
        ]
    )

    assert exit_code == 1
    assert calls == []


def test_guarded_disable_auto_reverts_when_no_command_arrives(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        main,
        "run_elevated_worker_op",
        lambda op, instance_id: calls.append((op, instance_id)) or (True, op),
    )
    _force_single_poll_timeout(monkeypatch)
    _command_file, completion_file, argv = _guarded_disable_argv(tmp_path, "229")

    exit_code = main._run_elevated_helper(argv)

    assert exit_code == 0
    assert calls == [("disable", "A"), ("enable", "A")]
    completion = json.loads(completion_file.read_text(encoding="utf-8"))
    assert completion["action"] == "auto_revert"
    assert completion["reason"] == main.GUARD_REASON_NO_COMMAND


def test_guarded_disable_ignores_an_invalid_action_and_still_auto_reverts(monkeypatch, tmp_path):
    # main.py's guard loop only treats "keep"/"revert" as actionable; any
    # other value must be ignored (not mistaken for keep or revert) and the
    # loop must keep polling until the deadline, landing on auto_revert --
    # same timeout-forcing technique as
    # test_guarded_disable_auto_reverts_when_no_command_arrives.
    calls = []
    monkeypatch.setattr(
        main,
        "run_elevated_worker_op",
        lambda op, instance_id: calls.append((op, instance_id)) or (True, op),
    )
    _force_single_poll_timeout(monkeypatch)
    command_file, completion_file, argv = _guarded_disable_argv(tmp_path, "230")
    command_file.write_text(json.dumps({"action": "nuke"}), encoding="utf-8")

    exit_code = main._run_elevated_helper(argv)

    assert exit_code == 0
    assert calls == [("disable", "A"), ("enable", "A")]
    completion = json.loads(completion_file.read_text(encoding="utf-8"))
    assert completion["action"] == "auto_revert"
    assert completion["reason"] == main.GUARD_REASON_INVALID_ACTION


def test_guarded_disable_unhashable_action_value_does_not_crash(monkeypatch, tmp_path):
    # A well-formed JSON command whose "action" is a list/dict (not a
    # string) must not crash `requested in {"keep", "revert"}` with an
    # unhashable-type TypeError -- that would kill this still-elevated
    # helper before it ever writes a result or re-enables the monitor,
    # forcing bridge.py's separate guard to re-elevate with a second UAC
    # prompt instead of the fail-safe auto-reverting cleanly.
    calls = []
    monkeypatch.setattr(
        main,
        "run_elevated_worker_op",
        lambda op, instance_id: calls.append((op, instance_id)) or (True, op),
    )
    _force_single_poll_timeout(monkeypatch)
    command_file, completion_file, argv = _guarded_disable_argv(tmp_path, "234")
    command_file.write_text(json.dumps({"action": ["revert"]}), encoding="utf-8")

    exit_code = main._run_elevated_helper(argv)

    assert exit_code == 0
    assert calls == [("disable", "A"), ("enable", "A")]
    completion = json.loads(completion_file.read_text(encoding="utf-8"))
    assert completion["action"] == "auto_revert"
    assert completion["reason"] == main.GUARD_REASON_INVALID_ACTION


def test_guarded_disable_pathological_json_does_not_crash(monkeypatch, tmp_path):
    # json.load() raises RecursionError on deeply-nested JSON -- a
    # RuntimeError subclass, not OSError/ValueError/JSONDecodeError. The
    # read/parse except clause must catch this too (it deliberately
    # catches Exception broadly), or an adversarial/corrupt command file
    # crashes this still-elevated helper the same way an unhashable
    # "action" value did before that was fixed.
    calls = []
    monkeypatch.setattr(
        main,
        "run_elevated_worker_op",
        lambda op, instance_id: calls.append((op, instance_id)) or (True, op),
    )
    _force_single_poll_timeout(monkeypatch)
    command_file, completion_file, argv = _guarded_disable_argv(tmp_path, "236")
    command_file.write_text("[" * 200_000, encoding="utf-8")

    exit_code = main._run_elevated_helper(argv)

    assert exit_code == 0
    assert calls == [("disable", "A"), ("enable", "A")]
    completion = json.loads(completion_file.read_text(encoding="utf-8"))
    assert completion["action"] == "auto_revert"
    assert completion["reason"] == main.GUARD_REASON_UNREADABLE_COMMAND


def test_guarded_disable_reason_reflects_the_most_recent_poll(monkeypatch, tmp_path):
    # last_reason is overwritten every iteration, not OR'd together across
    # the whole window -- a transient bad read on an early poll must not
    # permanently outrank whatever the loop actually observes on a later
    # poll, right up until the deadline. The command file's content can't
    # change mid-test here, so json.load itself is faked to be flaky: the
    # first poll raises (malformed), the second poll succeeds and reads a
    # well-formed-but-invalid action.
    calls = []
    monkeypatch.setattr(
        main,
        "run_elevated_worker_op",
        lambda op, instance_id: calls.append((op, instance_id)) or (True, op),
    )
    _force_deadline_after(monkeypatch, 0.0, 0.5)
    command_file, completion_file, argv = _guarded_disable_argv(tmp_path, "235")
    command_file.write_text(json.dumps({"action": "nuke"}), encoding="utf-8")

    real_json_load = json.load
    poll_count = {"n": 0}

    def flaky_load(f):
        poll_count["n"] += 1
        if poll_count["n"] == 1:
            raise json.JSONDecodeError("boom", "doc", 0)
        return real_json_load(f)

    monkeypatch.setattr(main.json, "load", flaky_load)

    exit_code = main._run_elevated_helper(argv)

    assert exit_code == 0
    assert poll_count["n"] == 2
    assert calls == [("disable", "A"), ("enable", "A")]
    completion = json.loads(completion_file.read_text(encoding="utf-8"))
    assert completion["action"] == "auto_revert"
    assert completion["reason"] == main.GUARD_REASON_INVALID_ACTION


def test_guarded_disable_command_file_only_acts_on_the_launched_instance_id(monkeypatch, tmp_path):
    # The guard loop reads only the "action" field from the command file --
    # it has no code path that reads command["instance_id"] at all, so an
    # extraneous "instance_id" naming a monitor the helper was never
    # launched for is just inert dead data, not something being actively
    # rejected. This locks in that (currently structural, not enforced)
    # guarantee: the helper always applies the action to the fixed
    # args.instance_id list it was launched with ("A"), never to "B".
    calls = []

    def worker(op, instance_id):
        calls.append((op, instance_id))
        return True, f"{op} {instance_id}"

    monkeypatch.setattr(main, "run_elevated_worker_op", worker)
    command_file, completion_file, argv = _guarded_disable_argv(tmp_path, "231")
    command_file.write_text(
        json.dumps({"action": "revert", "instance_id": "B"}), encoding="utf-8"
    )

    exit_code = main._run_elevated_helper(argv)

    assert exit_code == 0
    # calls == [("disable", "A"), ("enable", "A")] below already pins every
    # element exactly, which is itself the proof that "B" was never acted
    # on -- no separate `"B" not in calls` assertion needed on top of it.
    assert calls == [("disable", "A"), ("enable", "A")]
    assert json.loads(completion_file.read_text(encoding="utf-8")) == {
        "action": "revert",
        "reason": None,
        "results": [{"instance_id": "A", "ok": True, "message": "enable A"}],
    }


def test_guarded_disable_malformed_command_file_does_not_crash_and_auto_reverts(
    monkeypatch, tmp_path
):
    # An unparseable command file (truncated/garbage JSON) must be swallowed
    # by the loop's deliberately-broad `except Exception` rather than
    # propagating -- the loop keeps polling and falls through to the
    # deadline's auto_revert fail-safe, same timeout-forcing technique as
    # test_guarded_disable_auto_reverts_when_no_command_arrives.
    calls = []
    monkeypatch.setattr(
        main,
        "run_elevated_worker_op",
        lambda op, instance_id: calls.append((op, instance_id)) or (True, op),
    )
    _force_single_poll_timeout(monkeypatch)
    command_file, completion_file, argv = _guarded_disable_argv(tmp_path, "232")
    command_file.write_text("{not valid json", encoding="utf-8")

    exit_code = main._run_elevated_helper(argv)

    assert exit_code == 0
    assert calls == [("disable", "A"), ("enable", "A")]
    completion = json.loads(completion_file.read_text(encoding="utf-8"))
    assert completion["action"] == "auto_revert"
    assert completion["reason"] == main.GUARD_REASON_UNREADABLE_COMMAND
