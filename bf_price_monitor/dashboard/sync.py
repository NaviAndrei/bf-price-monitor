"""Turn operations into one commit each and push them, without losing any.

Each attempt mirrors scripts/persist_history.py: fetch, detach onto the
fresh origin tip, make the edits, commit, push. Because the clone is
detached before every attempt, an unpushed local commit is simply left
behind (rewriting nothing); the journal re-derives it from its operation.

    journal append -> fetch -> detach at origin/master -> replay every
    pending op (+ the new one) -> one commit each -> push -> clear journal
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from bf_price_monitor.dashboard import operations, store
from bf_price_monitor.dashboard.config import DashboardConfig
from bf_price_monitor.dashboard.gitcmd import (
    NETWORK_TIMEOUT,
    Git,
    GitError,
    GitTimeout,
    Runner,
    classify_failure,
    default_runner,
)
from bf_price_monitor.dashboard.journal import Entry, Journal
from bf_price_monitor.dashboard.locking import CloneLock
from bf_price_monitor.dashboard.operations import Conflict, InvalidOperation
from bf_price_monitor.dashboard.store import StoreError

State = Literal["pushed", "not_pushed", "rejected", "conflict", "invalid"]

WATCHLIST_REL = "data/watchlist.json"
MAX_ATTEMPTS = 3
RETRY_DELAY_STEP_SECONDS = 1
CASCADE_REASON = "an earlier operation on this watch conflicted"


@dataclass(frozen=True)
class SyncStatus:
    pending_count: int
    conflicts: list[Entry] = field(default_factory=list)


@dataclass(frozen=True)
class SyncResult:
    state: State
    pending_count: int
    conflicts: list[Entry]
    reason: str | None = None
    detail: str = ""
    head: str | None = None


class _Stop(Exception):
    """Ends a sync early with a final state (carries no data loss: the
    journal still holds every un-pushed operation)."""

    def __init__(self, state: State, reason: str | None, detail: str) -> None:
        super().__init__(detail)
        self.state: State = state
        self.reason = reason
        self.detail = detail


def _last_line(text: str) -> str:
    return (text.strip().splitlines() or ["no error output"])[-1]


class GitSync:
    def __init__(
        self,
        cfg: DashboardConfig,
        *,
        runner: Runner = default_runner,
        sleep_fn: Callable[[float], None] = time.sleep,
        before_push: Callable[[], None] = lambda: None,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self._cfg = cfg
        self._git = Git(cfg.clone, runner)
        self._sleep = sleep_fn
        self._before_push = before_push
        self._max_attempts = max_attempts
        state_dir = cfg.clone / ".git" / "bf-dashboard"
        self.journal = Journal(state_dir)
        self.lock_path = state_dir / "lock"
        self._ref = f"refs/remotes/{cfg.remote}/{cfg.branch}"

    # --- public API --------------------------------------------------------

    def status(self) -> SyncStatus:
        return SyncStatus(len(self.journal.pending()), self.journal.conflicts())

    def dismiss_conflict(self, op_id: str) -> bool:
        return self.journal.dismiss_conflict(op_id)

    def submit(self, op: operations.Op) -> SyncResult:
        """Queue one operation and try to push it. Raises CloneBusy when
        another process holds the clone (nothing is journaled then)."""
        try:
            operations.check(op)
        except InvalidOperation as exc:
            return self._result("invalid", detail=str(exc))
        with CloneLock(self.lock_path):
            entry = self.journal.append(op)
            return self._sync(focus=entry["op_id"])

    def retry(self) -> SyncResult:
        with CloneLock(self.lock_path):
            if not self.journal.pending():
                return self._result("pushed")
            return self._sync(focus=None)

    # --- the loop ----------------------------------------------------------

    def _sync(self, focus: str | None) -> SyncResult:
        fates: dict[str, tuple[State, str]] = {}
        blocked: set[str] = set()
        try:
            for attempt in range(1, self._max_attempts + 1):
                applied = self._replay(fates, blocked)
                if not applied:
                    return self._finish("pushed", focus, fates)
                self._before_push()
                push = self._run_network("push", self._cfg.remote, self._push_spec())
                if push.returncode == 0:
                    try:
                        head: str | None = self._git.run(
                            "rev-parse", "HEAD"
                        ).stdout.strip()
                    except (GitError, GitTimeout):
                        head = None  # the push landed; only the label is unknown
                    self.journal.remove(applied)
                    return self._finish("pushed", focus, fates, head=head)
                reason = classify_failure(push.stderr)
                detail = _last_line(push.stderr)
                if reason != "rejected":
                    raise _Stop("not_pushed", reason, detail)
                if attempt == self._max_attempts:
                    raise _Stop("rejected", "rejected", detail)
                self._sleep(RETRY_DELAY_STEP_SECONDS * attempt)
        except _Stop as stop:
            return self._finish(
                stop.state, focus, fates, reason=stop.reason, detail=stop.detail
            )
        raise AssertionError("unreachable")  # pragma: no cover

    def _push_spec(self) -> str:
        return f"HEAD:refs/heads/{self._cfg.branch}"

    def _run_network(self, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            return self._git.run(*args, timeout=NETWORK_TIMEOUT, check=False)
        except GitTimeout as exc:
            raise _Stop("not_pushed", "timeout", str(exc)) from exc

    def _replay(
        self, fates: dict[str, tuple[State, str]], blocked: set[str]
    ) -> list[str]:
        """Fetch, detach at the fresh tip and re-apply every pending op.

        Returns the op_ids that produced a commit (they stay journaled until
        the push succeeds). Ops that are already on origin are dropped; ops
        that conflict move to conflicts.jsonl, and so do later ops on the
        same watch, in order."""
        fetch = self._run_network(
            "fetch", self._cfg.remote, f"refs/heads/{self._cfg.branch}:{self._ref}"
        )
        if fetch.returncode != 0:
            raise _Stop(
                "not_pushed", classify_failure(fetch.stderr), _last_line(fetch.stderr)
            )
        path = self._cfg.clone / WATCHLIST_REL
        try:
            self._git.run("switch", "--detach", self._ref)
            doc = store.load(path)
        except StoreError as exc:
            raise _Stop("invalid", None, str(exc)) from exc
        except (GitError, GitTimeout) as exc:
            raise _Stop("not_pushed", "other", str(exc)) from exc

        applied: list[str] = []
        try:
            for entry in self.journal.pending():
                doc = self._replay_one(entry, doc, path, applied, fates, blocked)
        except (GitError, GitTimeout, OSError) as exc:
            self._discard_edits()
            raise _Stop("not_pushed", "other", str(exc)) from exc
        return applied

    def _replay_one(
        self,
        entry: Entry,
        doc: store.Doc,
        path: Path,
        applied: list[str],
        fates: dict[str, tuple[State, str]],
        blocked: set[str],
    ) -> store.Doc:
        op_id, op = entry["op_id"], entry["op"]
        try:
            wid = operations.op_watch_id(op)
        except InvalidOperation as exc:
            self._park(entry, "invalid", str(exc), None, fates, blocked)
            return doc
        if wid in blocked:
            self._park(entry, "conflict", CASCADE_REASON, wid, fates, blocked)
            return doc
        try:
            status, new_doc = operations.apply(doc, op)
            if status == operations.ALREADY_APPLIED:
                self.journal.remove([op_id])
                return doc
            store.validate(new_doc)
        except Conflict as exc:
            self._park(entry, "conflict", str(exc), wid, fates, blocked)
            return doc
        except (InvalidOperation, StoreError) as exc:
            self._park(entry, "invalid", str(exc), wid, fates, blocked)
            return doc
        store.save(path, new_doc)
        self._git.run("add", "--", WATCHLIST_REL)
        if self._git.run("diff", "--cached", "--quiet", check=False).returncode == 0:
            self.journal.remove([op_id])  # nothing changed on disk: no commit
            return new_doc
        self._git.run(
            "-c",
            f"user.name={self._cfg.author_name}",
            "-c",
            f"user.email={self._cfg.author_email}",
            "commit",
            "-q",
            "-m",
            operations.describe(op),
        )
        applied.append(op_id)
        return new_doc

    def _park(
        self,
        entry: Entry,
        state: State,
        reason: str,
        watch: str | None,
        fates: dict[str, tuple[State, str]],
        blocked: set[str],
    ) -> None:
        self.journal.move_to_conflicts(entry, reason=reason, kind=state)
        fates[entry["op_id"]] = (state, reason)
        if watch is not None:
            blocked.add(watch)

    def _discard_edits(self) -> None:
        """Best effort: undo a half-made edit so the work tree stays clean.
        The journal still has the operation, so nothing is lost."""
        try:
            self._git.run(
                "restore",
                "--source=HEAD",
                "--staged",
                "--worktree",
                "--",
                WATCHLIST_REL,
                check=False,
            )
        except (GitTimeout, OSError):
            pass

    # --- results -----------------------------------------------------------

    def _finish(
        self,
        pushed_state: State,
        focus: str | None,
        fates: dict[str, tuple[State, str]],
        *,
        reason: str | None = None,
        detail: str = "",
        head: str | None = None,
    ) -> SyncResult:
        state, text = pushed_state, detail
        if focus is not None and focus in fates:
            state, text = fates[focus]
            reason = None
        elif focus is None and fates and pushed_state == "pushed":
            kinds = {kind for kind, _ in fates.values()}
            state = "conflict" if "conflict" in kinds else "invalid"
            text = f"{len(fates)} pending operation(s) could not be applied"
            reason = None
        return self._result(state, reason=reason, detail=text, head=head)

    def _result(
        self,
        state: State,
        *,
        reason: str | None = None,
        detail: str = "",
        head: str | None = None,
    ) -> SyncResult:
        return SyncResult(
            state=state,
            pending_count=len(self.journal.pending()),
            conflicts=self.journal.conflicts(),
            reason=reason,
            detail=detail,
            head=head,
        )
