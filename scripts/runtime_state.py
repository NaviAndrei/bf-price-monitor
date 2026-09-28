"""Thin CLI for bf_price_monitor.runtime_state (B2, #42 No-Go blocker).

Called from monitor.yml's scrape-analyze-notify job as two separate steps:

    scripts/runtime_state.py restore --state-root <dir> --run-id <id> \\
        --modify-principal <principal>
    scripts/runtime_state.py save    --state-root <dir> --run-id <id> \\
        --modify-principal <principal>

Both always write a `state_status=<value>` line to $GITHUB_OUTPUT (or stdout
when run locally) so the persist job's final "Verify runtime state gate"
step can see the result even though this step itself runs with
continue-on-error. Exit code reflects success/failure for the step's own
visibility in the Actions UI, but never blocks the job -- the gate step is
what turns the workflow run red.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import notify
import scrape

from bf_price_monitor import runtime_state as rs

_OK_RESTORE_EXIT_STATUSES = {
    "fresh_first_run",
    "restored",
    "restored_fallback",
    "fresh_after_corruption",
}
_OK_SAVE_EXIT_STATUSES = {"saved"}


def _target_paths() -> rs.RuntimeStatePaths:
    return rs.RuntimeStatePaths(
        db=scrape.DB_FILE,
        health=scrape.SCRAPE_HEALTH_FILE,
        outbox=notify.OUTBOX_FILE,
    )


def _write_github_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    else:
        print(f"{name}={value}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["restore", "save", "status"])
    parser.add_argument(
        "--state-root",
        required=True,
        help="Runner-local, ACL-protected directory outside the job workspace.",
    )
    parser.add_argument(
        "--run-id",
        default=os.environ.get("GITHUB_RUN_ID", "local"),
        help="Defaults to $GITHUB_RUN_ID, else 'local'.",
    )
    parser.add_argument(
        "--modify-principal",
        required=True,
        help="The one principal (the runner's service SID) allowed Modify on the ACL.",
    )
    parser.add_argument(
        "--expected-owner",
        default="BUILTIN\\Administrators",
        help="Required owner of the state directory.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    state_root = Path(args.state_root)

    if args.action == "status":
        status_file = state_root / "status.json"
        if status_file.is_file():
            print(status_file.read_text(encoding="utf-8"))
            return 0
        print("no status recorded yet")
        return 0

    try:
        acl_snapshot = rs.read_acl_snapshot(state_root)
    except Exception as exc:  # noqa: BLE001 - any read failure disables state, never crashes
        print(f"::error::could not read state directory ACL for '{state_root}': {exc}")
        rs._write_status(state_root, "disabled_insecure_state_dir")
        _write_github_output("state_status", "disabled_insecure_state_dir")
        return 1

    targets = _target_paths()
    if args.action == "restore":
        restore_result = rs.restore(
            state_root,
            targets,
            run_id=args.run_id,
            acl_snapshot=acl_snapshot,
            expected_owner=args.expected_owner,
            modify_principal=args.modify_principal,
        )
        _write_github_output("state_status", restore_result.status)
        return 0 if restore_result.status in _OK_RESTORE_EXIT_STATUSES else 1

    save_result = rs.save(
        state_root,
        targets,
        run_id=args.run_id,
        acl_snapshot=acl_snapshot,
        expected_owner=args.expected_owner,
        modify_principal=args.modify_principal,
    )
    _write_github_output("state_status", save_result.status)
    return 0 if save_result.status in _OK_SAVE_EXIT_STATUSES else 1


if __name__ == "__main__":
    raise SystemExit(main())
