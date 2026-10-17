"""Pending-operations journal (JSON lines) kept under ``<clone>/.git/``.

Every edit is written here (and fsynced) before any git work starts, so a
failed push, a crash or a restart never loses it. Rewrites go through a temp
file and ``os.replace``. A corrupt line raises instead of being skipped:
silently dropping someone's edit is the one thing this file must not do.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

Entry = dict[str, Any]

PENDING_NAME = "pending.jsonl"
CONFLICTS_NAME = "conflicts.jsonl"


class JournalError(Exception):
    pass


class Journal:
    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._pending = directory / PENDING_NAME
        self._conflicts = directory / CONFLICTS_NAME
        self._lock = threading.RLock()

    def append(self, op: dict[str, Any]) -> Entry:
        entry: Entry = {
            "op_id": uuid.uuid4().hex,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "op": op,
        }
        with self._lock:
            self._append_line(self._pending, entry)
        return entry

    def pending(self) -> list[Entry]:
        with self._lock:
            return self._read(self._pending)

    def conflicts(self) -> list[Entry]:
        with self._lock:
            return self._read(self._conflicts)

    def remove(self, op_ids: Iterable[str]) -> None:
        drop = set(op_ids)
        with self._lock:
            kept = [e for e in self._read(self._pending) if e["op_id"] not in drop]
            self._rewrite(self._pending, kept)

    def move_to_conflicts(self, entry: Entry, *, reason: str, kind: str) -> None:
        """Record first, then un-queue: a crash in between duplicates nothing
        because the conflict record is skipped when its op_id is already there."""
        with self._lock:
            if all(c["op_id"] != entry["op_id"] for c in self._read(self._conflicts)):
                self._append_line(
                    self._conflicts,
                    {
                        **entry,
                        "reason": reason,
                        "kind": kind,
                        "moved_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    },
                )
            self.remove([entry["op_id"]])

    def dismiss_conflict(self, op_id: str) -> bool:
        with self._lock:
            entries = self._read(self._conflicts)
            kept = [e for e in entries if e["op_id"] != op_id]
            if len(kept) == len(entries):
                return False
            self._rewrite(self._conflicts, kept)
            return True

    # --- file helpers ------------------------------------------------------

    def _append_line(self, path: Path, entry: Entry) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _rewrite(self, path: Path, entries: list[Entry]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            for entry in entries:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def _read(self, path: Path) -> list[Entry]:
        if not path.exists():
            return []
        entries: list[Entry] = []
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError as exc:
                raise JournalError(f"{path.name} line {number} is corrupt") from exc
            if not isinstance(entry, dict) or "op_id" not in entry:
                raise JournalError(f"{path.name} line {number} is not an entry")
            entries.append(entry)
        return entries
