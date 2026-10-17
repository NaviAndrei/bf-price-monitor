# scripts/persist_history.py
"""Commit the scrape job's price history onto the current master tip (#74).

monitor.yml's persist job runs this with the runner's bare system Python
(stdlib only). It replaces the old "commit the whole file, then pull and
replay it on top" step, which failed with a textual conflict whenever another
run had appended to the same JSON arrays first, losing that run's rows.

Each attempt starts from origin/master as it is *now*, merges the incoming
document into it by data (bf_price_monitor.history_merge, three-way against
the commit the scrape started from), and pushes a fast-forward. A rejected
push is retried from a fresh fetch, so the merge is always recomputed on the
newest tip. Nothing here moves a branch backwards or rewrites a commit: an
unpushed attempt is simply left behind on a detached HEAD.

Usage (CI always passes --require-base):
    python scripts/persist_history.py --incoming DIR --base SHA --require-base

Exit codes: 0 pushed or nothing to commit; 1 every push attempt was rejected;
2 bad input or a merge/git error (never retried).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bf_price_monitor.history_merge import (  # noqa: E402  (path insert above)
    HISTORY_SCHEMA_VERSION,
    Doc,
    HistoryMergeError,
    dump_history_doc,
    load_history_doc,
    merge_history_docs,
)

HISTORY_PATH = Path("data") / "price_history.json"
INCOMING_FILE_NAME = "price_history.json"
REMOTE = "origin"
BRANCH = "master"
REMOTE_REF = f"refs/remotes/{REMOTE}/{BRANCH}"
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "github-actions[bot]@users.noreply.github.com"
COMMIT_MESSAGE = "chore: update price history [skip ci]"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RETRY_DELAY_STEP_SECONDS = 5

EXIT_OK = 0
EXIT_PUSH_REJECTED = 1
EXIT_BAD_INPUT = 2


class PersistError(Exception):
    """A non-retryable failure: bad input or a git command that must succeed."""


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, encoding="utf-8"
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1:] or ["no error output"]
        raise PersistError(
            f"git {args[0]} failed (exit {result.returncode}): {detail[0]}"
        )
    return result


def _empty_doc() -> Doc:
    return {"schema_version": HISTORY_SCHEMA_VERSION, "products": {}}


def _validate_base(raw: str, require_base: bool) -> str | None:
    base = raw.strip()
    if not base:
        if require_base:
            raise PersistError(
                "--base is empty but --require-base is set; the scrape job's "
                "'Record base commit' step did not produce a commit SHA"
            )
        print(
            "WARNING [persist] no --base given: falling back to a two-way "
            "union, which cannot honour rows the scrape pruned",
            file=sys.stderr,
        )
        return None
    if not SHA_RE.fullmatch(base):
        raise PersistError(f"--base must be a 40-character hex SHA, got {base!r}")
    return base


def _load_incoming(incoming_dir: Path) -> Doc:
    path = incoming_dir / INCOMING_FILE_NAME
    if not path.is_file():
        raise PersistError(f"incoming history not found: {path}")
    return load_history_doc(path.read_text(encoding="utf-8"))


def _load_base(base: str) -> Doc:
    if _git("cat-file", "-e", f"{base}^{{commit}}", check=False).returncode != 0:
        raise PersistError(f"base commit {base} is not in this repository")
    spec = f"{base}:{HISTORY_PATH.as_posix()}"
    if _git("cat-file", "-e", spec, check=False).returncode != 0:
        return _empty_doc()
    return load_history_doc(_git("show", spec).stdout)


def _load_tip() -> Doc:
    if not HISTORY_PATH.is_file():
        return _empty_doc()
    return load_history_doc(HISTORY_PATH.read_text(encoding="utf-8"))


def _attempt(incoming: Doc, base: str | None, base_doc: Doc | None) -> bool | None:
    """One fetch-merge-commit pass. Returns None when there is nothing to
    commit, otherwise whether the caller should push."""
    _git("fetch", REMOTE, f"refs/heads/{BRANCH}:{REMOTE_REF}")
    tip = _git("rev-parse", REMOTE_REF).stdout.strip()
    print(f"[persist] base={base or 'none'} origin_tip={tip}")
    _git("switch", "--detach", REMOTE_REF)
    merged = merge_history_docs(incoming, _load_tip(), base_doc)
    dump_history_doc(merged, HISTORY_PATH)
    _git("add", "--", HISTORY_PATH.as_posix())
    if _git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return None
    _git(
        "-c",
        f"user.name={BOT_NAME}",
        "-c",
        f"user.email={BOT_EMAIL}",
        "commit",
        "-q",
        "-m",
        COMMIT_MESSAGE,
    )
    return True


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Merge scraped price history into origin/master by data and push."
    )
    parser.add_argument(
        "--incoming",
        type=Path,
        required=True,
        help="Directory holding the scrape job's price_history.json",
    )
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument(
        "--base",
        default="",
        help="Commit the scrape job checked out (40-character hex SHA)",
    )
    parser.add_argument(
        "--require-base",
        action="store_true",
        help="Fail instead of falling back to a two-way merge when --base is empty",
    )
    args = parser.parse_args(argv)
    if args.max_attempts < 1:
        parser.error("--max-attempts must be at least 1")
    return args


def main(
    argv: Sequence[str] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    before_push: Callable[[], None] = lambda: None,
) -> int:
    args = parse_args(argv)
    try:
        base = _validate_base(args.base, args.require_base)
        incoming = _load_incoming(args.incoming)
        base_doc = None if base is None else _load_base(base)
        for attempt in range(1, args.max_attempts + 1):
            print(f"[persist] attempt {attempt}/{args.max_attempts}")
            if _attempt(incoming, base, base_doc) is None:
                print(
                    "[persist] merged history equals origin/master; nothing to commit"
                )
                return EXIT_OK
            before_push()
            push = _git("push", REMOTE, f"HEAD:refs/heads/{BRANCH}", check=False)
            if push.returncode == 0:
                head = _git("rev-parse", "HEAD").stdout.strip()
                print(f"[persist] pushed {head} to {REMOTE}/{BRANCH}")
                return EXIT_OK
            reason = push.stderr.strip().splitlines()[-1:] or ["no error output"]
            print(f"WARNING [persist] push rejected: {reason[0]}", file=sys.stderr)
            if attempt < args.max_attempts:
                sleep_fn(RETRY_DELAY_STEP_SECONDS * attempt)
    except (PersistError, HistoryMergeError) as e:
        print(f"::error::persist_history: {e}", file=sys.stderr)
        return EXIT_BAD_INPUT
    print(
        f"::error::persist_history: push to {REMOTE}/{BRANCH} was rejected "
        f"{args.max_attempts} time(s). The scraped history is still in this "
        "run's price-data artifact (kept 1 day): re-run the persist job, or "
        "download the artifact and run scripts/persist_history.py --incoming "
        "<dir> --base <scrape commit> --require-base from a clean master checkout.",
        file=sys.stderr,
    )
    return EXIT_PUSH_REJECTED


if __name__ == "__main__":
    sys.exit(main())
