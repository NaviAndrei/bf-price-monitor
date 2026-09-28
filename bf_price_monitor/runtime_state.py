"""Cross-run runtime state for the self-hosted monitor runner (B2, #42).

`actions/checkout` wipes the job workspace on every run, including
`data/price_history.db`, `data/scrape_health.jsonl` and
`data/alert_outbox.jsonl` -- the three files the cooldown, dead-man and
scraper-quarantine safeguards depend on seeing from the *previous* run. This
module persists exactly those three files to a runner-local directory
outside the workspace (never as a GitHub artifact, which this public repo's
own read access would make effectively public -- see docs/DECISIONS.md).

Design, approved 2026-09-28 (#42 B2):
- `restore()` runs before scraping and copies the last known-good snapshot
  into the production paths the scripts already read.
- `save()` runs with `if: always()` after scrape/analyze/notify so a crashed
  run's PENDING/SENT outbox records are not lost, but only *promotes* a
  candidate to CURRENT after it fully validates (SQLite integrity_check,
  JSONL parses line-by-line, manifest hashes match).
- Both refuse to touch the state directory unless its ACL matches exactly
  SYSTEM + Administrators (Full Control) and the runner's own service SID
  (Modify) -- see `validate_state_root_acl`. A failed check disables runtime
  state for that run rather than silently treating an insecure directory as
  private.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bf_price_monitor.storage import sqlite as sqlite_storage

SCHEMA_VERSION = 1

STATE_FILENAMES: dict[str, str] = {
    "db": "price_history.db",
    "health": "scrape_health.jsonl",
    "outbox": "alert_outbox.jsonl",
}

# The only principals ever allowed on the state directory's ACL. Anything
# else -- NETWORK SERVICE, Users, Authenticated Users, Everyone, an
# inherited entry, or an unrecognized principal -- fails validation.
REQUIRED_FULL_CONTROL_PRINCIPALS: tuple[str, ...] = (
    "NT AUTHORITY\\SYSTEM",
    "BUILTIN\\Administrators",
)


class AclViolation(RuntimeError):
    """The state directory's ACL does not meet the required security bar."""


class RuntimeStateError(RuntimeError):
    """A candidate snapshot failed validation and must not be promoted."""


@dataclass(frozen=True)
class RuntimeStatePaths:
    """The production paths runtime state is restored into / saved from."""

    db: Path
    health: Path
    outbox: Path


@dataclass(frozen=True)
class AclEntry:
    principal: str
    rights: str
    inherited: bool


@dataclass(frozen=True)
class AclSnapshot:
    owner: str
    protected: bool
    entries: tuple[AclEntry, ...]


@dataclass(frozen=True)
class RestoreResult:
    status: str
    snapshot_id: str | None
    detail: str


@dataclass(frozen=True)
class SaveResult:
    status: str
    snapshot_id: str | None
    detail: str


# ---------------------------------------------------------------------------
# ACL validation
# ---------------------------------------------------------------------------


def validate_state_root_acl(
    acl: AclSnapshot,
    *,
    expected_owner: str,
    modify_principal: str,
) -> None:
    """Raises AclViolation unless the ACL grants exactly:
    SYSTEM (Full Control), Administrators (Full Control), and
    `modify_principal` (Modify, never Full Control) -- all explicit, no
    inherited entries, owned by `expected_owner`.
    """
    if not acl.protected:
        raise AclViolation(
            "state directory ACL is not protected (inheritance is not disabled)"
        )
    if acl.owner.lower() != expected_owner.lower():
        raise AclViolation(
            f"state directory owner is '{acl.owner}', expected '{expected_owner}'"
        )

    seen: dict[str, AclEntry] = {}
    for entry in acl.entries:
        if entry.inherited:
            raise AclViolation(
                f"inherited ACE present for principal '{entry.principal}'"
            )
        seen[entry.principal] = entry

    allowed = set(REQUIRED_FULL_CONTROL_PRINCIPALS) | {modify_principal}
    unexpected = set(seen) - allowed
    if unexpected:
        raise AclViolation(
            f"unexpected principal(s) on state directory ACL: {sorted(unexpected)}"
        )

    for principal in REQUIRED_FULL_CONTROL_PRINCIPALS:
        required_entry = seen.get(principal)
        if required_entry is None:
            raise AclViolation(
                f"required principal '{principal}' is missing Full Control"
            )
        if "FullControl" not in required_entry.rights:
            raise AclViolation(
                f"principal '{principal}' lacks Full Control "
                f"(has '{required_entry.rights}')"
            )

    modify_entry = seen.get(modify_principal)
    if modify_entry is None:
        raise AclViolation(
            f"required service principal '{modify_principal}' is missing from the ACL"
        )
    if "FullControl" in modify_entry.rights:
        raise AclViolation(
            f"service principal '{modify_principal}' has Full Control; "
            "Modify only is required"
        )
    if "Modify" not in modify_entry.rights:
        raise AclViolation(
            f"service principal '{modify_principal}' lacks Modify rights "
            f"(has '{modify_entry.rights}')"
        )


def _default_acl_reader(path: Path) -> str:
    """Shells out to PowerShell's Get-Acl and returns raw JSON text.

    Kept separate from the parser so tests inject a canned string instead of
    touching the real filesystem ACL.
    """
    escaped = str(path).replace("'", "''")
    script = (
        f"$acl = Get-Acl -LiteralPath '{escaped}'; "
        "$obj = [pscustomobject]@{ "
        "owner = $acl.Owner; "
        "protected = $acl.AreAccessRulesProtected; "
        "entries = @($acl.Access | ForEach-Object { [pscustomobject]@{ "
        "principal = $_.IdentityReference.Value; "
        "rights = $_.FileSystemRights.ToString(); "
        "inherited = $_.IsInherited } }) "
        "}; $obj | ConvertTo-Json -Depth 4 -Compress"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return result.stdout


def read_acl_snapshot(
    path: Path, *, reader: Callable[[Path], str] = _default_acl_reader
) -> AclSnapshot:
    raw = reader(path)
    data = json.loads(raw)
    entries_raw = data.get("entries") or []
    if isinstance(entries_raw, dict):
        entries_raw = [entries_raw]
    entries = tuple(
        AclEntry(
            principal=str(e["principal"]),
            rights=str(e["rights"]),
            inherited=bool(e["inherited"]),
        )
        for e in entries_raw
    )
    return AclSnapshot(
        owner=str(data["owner"]), protected=bool(data["protected"]), entries=entries
    )


# ---------------------------------------------------------------------------
# Snapshot filesystem layout helpers
# ---------------------------------------------------------------------------


def _snapshots_root(state_root: Path) -> Path:
    return state_root / "snapshots"


def _snapshot_dir(state_root: Path, snapshot_id: str) -> Path:
    return _snapshots_root(state_root) / snapshot_id


def _current_pointer(state_root: Path) -> Path:
    return state_root / "CURRENT"


def _status_file(state_root: Path) -> Path:
    return state_root / "status.json"


def _list_snapshot_ids(state_root: Path) -> list[str]:
    """Newest first; snapshot ids are UTC-timestamp-prefixed so they sort
    lexically in chronological order."""
    snap_root = _snapshots_root(state_root)
    if not snap_root.is_dir():
        return []
    return sorted((p.name for p in snap_root.iterdir() if p.is_dir()), reverse=True)


def _sanitize_run_id(run_id: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)
    return sanitized or "unknown"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _emit_error(message: str) -> None:
    print(f"::error::{message}")


def _write_status(
    state_root: Path, status: str, *, snapshot_id: str | None = None
) -> None:
    state_root.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": status,
        "snapshot_id": snapshot_id,
        "written_at_utc": datetime.now(UTC).isoformat(),
    }
    tmp = state_root / "status.json.tmp"
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, _status_file(state_root))


def _remove_sqlite_sidecars(db_path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.exists():
            sidecar.unlink()


def _quarantine_snapshot(state_root: Path, snapshot_id: str) -> None:
    src = _snapshot_dir(state_root, snapshot_id)
    if not src.is_dir():
        return
    dest_root = state_root / "quarantine"
    dest_root.mkdir(parents=True, exist_ok=True)
    dest = dest_root / snapshot_id
    if dest.exists():
        shutil.rmtree(dest)
    os.replace(src, dest)


def _quarantine_tmp(state_root: Path, tmp_dir: Path, snapshot_id: str) -> None:
    dest_root = state_root / "quarantine"
    dest_root.mkdir(parents=True, exist_ok=True)
    dest = dest_root / f"invalid-{snapshot_id}"
    if dest.exists():
        shutil.rmtree(dest)
    os.replace(tmp_dir, dest)


# ---------------------------------------------------------------------------
# SQLite / JSONL validation
# ---------------------------------------------------------------------------


def _snapshot_sqlite(source_db: Path, dest_db: Path) -> None:
    """Copies source_db into dest_db via the SQLite backup API -- never a raw
    file copy of a possibly WAL-mode-open database. If source_db doesn't
    exist yet (nothing scraped this run), writes a fresh, schema-initialized,
    empty database instead of letting sqlite3.connect silently create the
    real path as a side effect."""
    dest_db.parent.mkdir(parents=True, exist_ok=True)
    if dest_db.exists():
        dest_db.unlink()
    if not source_db.exists():
        conn = sqlite_storage.init_db(dest_db)
        conn.close()
        return
    src_conn = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    try:
        dest_conn = sqlite3.connect(str(dest_db))
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        src_conn.close()


def _sqlite_integrity_ok(db_path: Path) -> bool:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute("PRAGMA integrity_check;").fetchone()
        return row is not None and row[0] == "ok"
    finally:
        conn.close()


def _jsonl_valid(path: Path) -> bool:
    if not path.is_file():
        return True
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                json.loads(stripped)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return True


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def _write_manifest(
    snapshot_dir: Path, *, snapshot_id: str, run_id: str, files: dict[str, str]
) -> None:
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "files": files,
    }
    (snapshot_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


def _read_manifest(snapshot_dir: Path) -> dict[str, Any] | None:
    manifest_path = snapshot_dir / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        data: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
        return data
    except (json.JSONDecodeError, OSError):
        return None


def _validate_snapshot(snapshot_dir: Path) -> bool:
    manifest = _read_manifest(snapshot_dir)
    if manifest is None:
        return False
    if manifest.get("schema_version") != SCHEMA_VERSION:
        return False
    files = manifest.get("files")
    if not isinstance(files, dict):
        return False
    for filename, expected_hash in files.items():
        file_path = snapshot_dir / filename
        if not file_path.is_file():
            return False
        if _sha256(file_path) != expected_hash:
            return False
    db_path = snapshot_dir / STATE_FILENAMES["db"]
    if db_path.is_file() and not _sqlite_integrity_ok(db_path):
        return False
    for key in ("health", "outbox"):
        jsonl_path = snapshot_dir / STATE_FILENAMES[key]
        if jsonl_path.is_file() and not _jsonl_valid(jsonl_path):
            return False
    return True


def _copy_snapshot_to_targets(snap_dir: Path, targets: RuntimeStatePaths) -> None:
    _remove_sqlite_sidecars(targets.db)
    targets.db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(snap_dir / STATE_FILENAMES["db"], targets.db)
    for key, target_path in (("health", targets.health), ("outbox", targets.outbox)):
        src = snap_dir / STATE_FILENAMES[key]
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if src.is_file():
            shutil.copy2(src, target_path)
        elif target_path.exists():
            target_path.unlink()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def restore(
    state_root: Path,
    targets: RuntimeStatePaths,
    *,
    run_id: str,
    acl_snapshot: AclSnapshot,
    expected_owner: str,
    modify_principal: str,
) -> RestoreResult:
    """Restores the newest valid snapshot into `targets`. Never restores
    anything if the ACL check fails -- the caller (the CLI) must then fail
    the workflow's final state gate rather than pretend nothing happened."""
    try:
        validate_state_root_acl(
            acl_snapshot,
            expected_owner=expected_owner,
            modify_principal=modify_principal,
        )
    except AclViolation as exc:
        _emit_error(f"runtime state disabled: insecure state directory ({exc})")
        _write_status(state_root, "disabled_insecure_state_dir")
        return RestoreResult("disabled_insecure_state_dir", None, str(exc))

    current_path = _current_pointer(state_root)
    if not current_path.is_file():
        print(
            f"[runtime-state] no prior state at {state_root}; "
            "starting fresh (first run or state reset)"
        )
        _write_status(state_root, "fresh_first_run")
        return RestoreResult("fresh_first_run", None, "no CURRENT pointer")

    pointed_id = current_path.read_text(encoding="utf-8").strip()
    candidate_ids = [pointed_id] + [
        sid for sid in _list_snapshot_ids(state_root) if sid != pointed_id
    ]

    for sid in candidate_ids:
        snap_dir = _snapshot_dir(state_root, sid)
        if not snap_dir.is_dir():
            continue
        if _validate_snapshot(snap_dir):
            _copy_snapshot_to_targets(snap_dir, targets)
            status = "restored" if sid == pointed_id else "restored_fallback"
            if status == "restored_fallback":
                print(
                    f"[runtime-state] CURRENT snapshot '{pointed_id}' is invalid; "
                    f"restored older valid snapshot '{sid}' instead"
                )
                _quarantine_snapshot(state_root, pointed_id)
            _write_status(state_root, status, snapshot_id=sid)
            return RestoreResult(status, sid, "restored from snapshot")
        _quarantine_snapshot(state_root, sid)

    _emit_error(
        f"runtime state corrupted: no valid snapshot found under {state_root}; "
        "starting fresh (cross-run safeguards reset to first-run behavior)"
    )
    _write_status(state_root, "fresh_after_corruption")
    return RestoreResult("fresh_after_corruption", None, "no valid snapshot")


def save(
    state_root: Path,
    sources: RuntimeStatePaths,
    *,
    run_id: str,
    acl_snapshot: AclSnapshot,
    expected_owner: str,
    modify_principal: str,
    keep: int = 3,
) -> SaveResult:
    """Builds a candidate snapshot and promotes it to CURRENT only if it
    fully validates. On any failure, the prior CURRENT is left untouched and
    the invalid candidate is quarantined."""
    try:
        validate_state_root_acl(
            acl_snapshot,
            expected_owner=expected_owner,
            modify_principal=modify_principal,
        )
    except AclViolation as exc:
        _emit_error(f"runtime state disabled: insecure state directory ({exc})")
        _write_status(state_root, "disabled_insecure_state_dir")
        return SaveResult("disabled_insecure_state_dir", None, str(exc))

    snapshot_id = (
        f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}Z_{_sanitize_run_id(run_id)}"
    )
    snap_dir = _snapshot_dir(state_root, snapshot_id)
    tmp_dir = _snapshots_root(state_root) / f".tmp-{snapshot_id}"

    try:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True)

        db_dest = tmp_dir / STATE_FILENAMES["db"]
        _snapshot_sqlite(sources.db, db_dest)
        if not _sqlite_integrity_ok(db_dest):
            raise RuntimeStateError("candidate SQLite snapshot failed integrity_check")

        files: dict[str, str] = {STATE_FILENAMES["db"]: _sha256(db_dest)}
        for key, src_path in (("health", sources.health), ("outbox", sources.outbox)):
            dest_path = tmp_dir / STATE_FILENAMES[key]
            if src_path.is_file():
                if not _jsonl_valid(src_path):
                    raise RuntimeStateError(
                        f"{src_path.name} contains malformed JSON lines"
                    )
                shutil.copy2(src_path, dest_path)
            else:
                dest_path.write_text("", encoding="utf-8")
            files[STATE_FILENAMES[key]] = _sha256(dest_path)

        _write_manifest(tmp_dir, snapshot_id=snapshot_id, run_id=run_id, files=files)
        if not _validate_snapshot(tmp_dir):
            raise RuntimeStateError("candidate snapshot failed post-write validation")

        os.replace(tmp_dir, snap_dir)
    except Exception as exc:  # noqa: BLE001 - any failure here must not promote a bad candidate
        if tmp_dir.exists():
            _quarantine_tmp(state_root, tmp_dir, snapshot_id)
        _emit_error(f"runtime state save failed, keeping previous CURRENT: {exc}")
        _write_status(state_root, "save_failed")
        return SaveResult("save_failed", None, str(exc))

    tmp_pointer = state_root / "CURRENT.tmp"
    tmp_pointer.write_text(snapshot_id, encoding="utf-8")
    os.replace(tmp_pointer, _current_pointer(state_root))

    _prune_old_snapshots(state_root, keep=keep)
    _write_status(state_root, "saved", snapshot_id=snapshot_id)
    return SaveResult("saved", snapshot_id, "candidate promoted to CURRENT")


def _prune_old_snapshots(state_root: Path, *, keep: int) -> None:
    ids = _list_snapshot_ids(state_root)
    for old_id in ids[keep:]:
        shutil.rmtree(_snapshot_dir(state_root, old_id), ignore_errors=True)
