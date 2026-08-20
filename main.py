import ctypes
import json
import sys
import time
import traceback

from quickres.monitors import run_elevated_worker_op

GUARD_REASON_NO_COMMAND = "no_command"
GUARD_REASON_UNREADABLE_COMMAND = "unreadable_command"
GUARD_REASON_INVALID_ACTION = "invalid_action"


def _run_elevated_helper(argv) -> int:
    """Elevated-helper CLI branch: loops every `--instance-id`
    occurrence (N=1 is just the one-element case of the same uniform path;
    there is no separate flag or code path for multi-target operations),
    always writes the unified `{"results": [...]}` shape via the shared
    atomic writer.
    """
    import argparse
    from quickres import recovery
    from quickres.config import APP_DIR, log_msg, write_json_atomic

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--monitor-op", required=True, choices=["enable", "disable", "guarded-disable"]
    )
    parser.add_argument("--instance-id", required=True, action="append")
    parser.add_argument("--result-file", required=True)
    parser.add_argument("--guard-command-file")
    parser.add_argument("--guard-result-file")
    parser.add_argument("--guard-timeout-s", type=float)
    args = parser.parse_args(argv)

    # This elevated (admin-privileged) process must validate its own
    # --result-file argv value before writing to it, mirroring the check
    # already done on the read side (monitors.read_op_result / bridge.py) --
    # an unvalidated write target here would let a corrupted/malicious argv
    # make an admin process write anywhere on disk.
    if not recovery.is_safe_result_path(args.result_file, APP_DIR):
        message = f"Refusing to write to unsafe result file path: {args.result_file}"
        # This is the sole observability path explaining why the elevated
        # helper refused to proceed, so it goes to quickres.log via
        # log_msg the same way every other diagnostic in this codebase
        # does under QuickRes.spec's console=False build -- sys.stdout and
        # sys.stderr are both None in that build (no console attached),
        # and a plain print(..., file=sys.stderr) there is not a reliable
        # way to surface anything to the user or developer. The print is
        # kept only as a best-effort extra for a console-attached run
        # (e.g. running this script directly during development).
        log_msg(message)
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        return 1

    guarded = args.monitor_op == "guarded-disable"
    if guarded:
        if not (
            args.guard_command_file
            and args.guard_result_file
            and args.guard_timeout_s is not None
            and 1.0 <= args.guard_timeout_s <= 60.0
            and recovery.is_safe_guard_command_path(args.guard_command_file, APP_DIR)
            and recovery.is_safe_guard_result_path(args.guard_result_file, APP_DIR)
        ):
            message = "Refusing invalid guarded monitor-helper arguments"
            log_msg(message)
            if sys.stderr is not None:
                print(message, file=sys.stderr)
            return 1

    results = []
    for instance_id in args.instance_id:
        try:
            ok, message = run_elevated_worker_op(
                "disable" if guarded else args.monitor_op, instance_id
            )
        except Exception as e:
            ok, message = False, f"Elevated helper crashed: {e}"
        results.append({"instance_id": instance_id, "ok": bool(ok), "message": message})

    if not write_json_atomic(args.result_file, {"results": results}):
        return 1
    if not guarded:
        return 0 if all(r["ok"] for r in results) else 1

    # The helper remains elevated only for this bounded confirmation window.
    # Its unprivileged command file cannot introduce a new operation: it can
    # only keep the already-applied disable or re-enable these exact ids.
    deadline = time.monotonic() + args.guard_timeout_s
    action = None
    last_reason = GUARD_REASON_NO_COMMAND
    while time.monotonic() < deadline:
        try:
            with open(args.guard_command_file, "r", encoding="utf-8") as f:
                command = json.load(f)
        except FileNotFoundError:
            last_reason = GUARD_REASON_NO_COMMAND
        except Exception:
            # Deliberately catches every exception, not just the
            # expected (OSError, ValueError, json.JSONDecodeError):
            # this is a fail-safe boundary that must never let an
            # unusual command file (a permission/sharing-violation
            # read racing the atomic replace in config.write_json_atomic,
            # or pathological JSON like deep nesting raising
            # RecursionError) crash this still-elevated helper before
            # it can auto-revert. Any such read is simply ignored and
            # the timeout remains the fail-safe.
            last_reason = GUARD_REASON_UNREADABLE_COMMAND
        else:
            # A tuple, not a set: an "action" that is a list/dict (valid
            # JSON, just not a string) then compares by equality instead
            # of needing `requested` to be hashable, so it cannot crash
            # with an uncaught TypeError.
            requested = command.get("action") if isinstance(command, dict) else None
            if requested in ("keep", "revert"):
                action = requested
                break
            last_reason = GUARD_REASON_INVALID_ACTION
        time.sleep(0.05)

    if action is None:
        action = "auto_revert"
        # auto_revert fires for three different causes that otherwise look
        # identical from the result file alone: no command file present on
        # the most recent poll, an unreadable/corrupt read on that poll,
        # or a well-formed read that was not a recognized keep/revert
        # action (including a missing or wrong-typed "action" field).
        # last_reason reflects only the most recent poll outcome, not
        # the first failure ever seen, so a transient early glitch cannot
        # mislabel a window that later settled into a different state.
        log_msg(f"Guard window expired without a valid keep/revert command (reason={last_reason}); auto-reverting")

    # "reason" is always present (None when it does not apply), matching
    # the convention this codebase already uses for always-present
    # classification fields (e.g. monitors.py OUTCOME_* as a required
    # tuple element) rather than a key a caller could forget to guard
    # with action == "auto_revert" before reading.
    completion = {
        "action": action,
        "reason": last_reason if action == "auto_revert" else None,
        "results": [],
    }
    if action != "keep":
        for instance_id in args.instance_id:
            try:
                ok, message = run_elevated_worker_op("enable", instance_id)
            except Exception as e:
                ok, message = False, f"Elevated helper crashed: {e}"
            completion["results"].append(
                {"instance_id": instance_id, "ok": bool(ok), "message": message}
            )

    if not write_json_atomic(args.guard_result_file, completion):
        return 1
    if action == "keep":
        return 0
    return 0 if all(r["ok"] for r in completion["results"]) else 1


def _show_startup_failure(message: str):
    """Injectable seam around `ctypes.windll.user32.MessageBoxW` (same
    module-level-function seam shape as `config._create_or_open_mutex` /
    `config._foreground_existing_window`), so tests can substitute a fake
    and assert on the message without popping a real dialog.

    Used only when the pywebview window itself failed to come up -- the
    webview can't render anything to tell the user what went wrong, and
    QuickRes.spec builds with `console=False`, so a native message box is
    the only surface left to report the failure on.
    """
    MB_ICONERROR = 0x10
    ctypes.windll.user32.MessageBoxW(None, message, "QuickRes", MB_ICONERROR)


def _launch_webview(run_app_fn=None) -> None:
    """Create the pywebview window and run its event loop, with the whole
    call guarded by a catch-all: window creation/the event-loop start is
    the single most likely first-launch failure point for any pywebview
    app -- most commonly the Microsoft Edge WebView2 Runtime not being
    installed (pywebview's EdgeChromium backend requires it, and Windows
    does not guarantee it is present). Any exception here is logged to
    quickres.log via `config.log_msg` (the only trace available under
    QuickRes.spec's `console=False` build) and reported to the user via
    `_show_startup_failure`.

    `run_app_fn` is an injectable seam (defaults to the real
    `quickres.webview.app.run_app`) so tests can exercise the failure path
    without a real GUI toolkit event loop.
    """
    try:
        if run_app_fn is None:
            from quickres.webview.app import run_app as run_app_fn

        run_app_fn()
    except Exception:
        from quickres.config import log_msg

        log_msg(f"run_app failed to start:\n{traceback.format_exc()}")
        _show_startup_failure(
            "QuickRes couldn't start. Make sure the Microsoft Edge WebView2 "
            "Runtime is installed, then try again."
        )


def main() -> int:
    if sys.platform != "win32":
        # This tool has no Tkinter (and no messagebox) to fall back on -- it is a
        # Windows-only tool (ctypes.windll doesn't even exist on other
        # platforms), so there is nothing left to pop a native dialog
        # against here. Report to stderr instead.
        print("QuickRes only works on Windows.", file=sys.stderr)
        return 1

    if "--monitor-op" in sys.argv:
        return _run_elevated_helper(sys.argv[1:])

    from quickres.config import enforce_single_instance

    if not enforce_single_instance():
        # Another instance is already running -- it has already been
        # foregrounded by enforce_single_instance(). This process exits
        # without ever creating a window.
        return 0

    _launch_webview()
    return 0


if __name__ == "__main__":
    sys.exit(main())
