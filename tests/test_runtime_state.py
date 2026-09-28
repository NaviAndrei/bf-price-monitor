"""B2 (#42 No-Go blocker): cross-run runtime state survives actions/checkout
cleaning the self-hosted runner workspace, via a runner-local, ACL-protected
state directory outside the Git workspace (see docs/DECISIONS.md).

Everything here uses tmp_path. Nothing touches data/, the real state
directory, the network, or a live workflow.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import notify
import pytest
import scrape

from bf_price_monitor import runtime_state as rs
from bf_price_monitor.storage import sqlite as sqlite_storage

WORKFLOW_PATH = (
    Path(__file__).resolve().parent.parent / ".github" / "workflows" / "monitor.yml"
)

SECURE_OWNER = "BUILTIN\\Administrators"
SERVICE_PRINCIPAL = "NT SERVICE\\actions.runner.NaviAndrei-bf-price-monitor.PC-A1208"


def _secure_acl(**overrides: object) -> rs.AclSnapshot:
    entries = overrides.pop(
        "entries",
        (
            rs.AclEntry("NT AUTHORITY\\SYSTEM", "FullControl", False),
            rs.AclEntry("BUILTIN\\Administrators", "FullControl", False),
            rs.AclEntry(SERVICE_PRINCIPAL, "Modify, Synchronize", False),
        ),
    )
    return rs.AclSnapshot(
        owner=overrides.pop("owner", SECURE_OWNER),
        protected=overrides.pop("protected", True),
        entries=entries,  # type: ignore[arg-type]
    )


_INSERT_PRODUCT_SQL = (
    "INSERT INTO canonical_products (id, title, category) VALUES (?, ?, ?)"
)
_INSERT_OFFER_SQL = (
    "INSERT INTO offers (id, product_id, retailer, sku, url) VALUES (?, ?, ?, ?, ?)"
)
_INSERT_OBSERVATION_SQL = (
    "INSERT INTO price_observations "
    "(offer_id, price, original_price, in_stock, scraped_at) "
    "VALUES (?, ?, ?, ?, ?)"
)


def _paths(tmp_path: Path) -> rs.RuntimeStatePaths:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return rs.RuntimeStatePaths(
        db=data / "price_history.db",
        health=data / "scrape_health.jsonl",
        outbox=data / "alert_outbox.jsonl",
    )


# ---------------------------------------------------------------------------
# ACL policy (pure, no subprocess)
# ---------------------------------------------------------------------------


def test_acl_accepts_the_verified_secure_state():
    rs.validate_state_root_acl(
        _secure_acl(), expected_owner=SECURE_OWNER, modify_principal=SERVICE_PRINCIPAL
    )


@pytest.mark.parametrize(
    "bad_principal",
    [
        "NT AUTHORITY\\NETWORK SERVICE",
        "BUILTIN\\Users",
        "NT AUTHORITY\\Authenticated Users",
        "Everyone",
    ],
)
def test_acl_rejects_broad_or_unexpected_principals(bad_principal):
    acl = _secure_acl(
        entries=(
            rs.AclEntry("NT AUTHORITY\\SYSTEM", "FullControl", False),
            rs.AclEntry("BUILTIN\\Administrators", "FullControl", False),
            rs.AclEntry(SERVICE_PRINCIPAL, "Modify, Synchronize", False),
            rs.AclEntry(bad_principal, "Modify", False),
        )
    )
    with pytest.raises(rs.AclViolation):
        rs.validate_state_root_acl(
            acl, expected_owner=SECURE_OWNER, modify_principal=SERVICE_PRINCIPAL
        )


def test_acl_rejects_inherited_write_access():
    acl = _secure_acl(
        entries=(
            rs.AclEntry("NT AUTHORITY\\SYSTEM", "FullControl", False),
            rs.AclEntry("BUILTIN\\Administrators", "FullControl", False),
            rs.AclEntry(SERVICE_PRINCIPAL, "Modify, Synchronize", True),
        )
    )
    with pytest.raises(rs.AclViolation):
        rs.validate_state_root_acl(
            acl, expected_owner=SECURE_OWNER, modify_principal=SERVICE_PRINCIPAL
        )


def test_acl_rejects_unexpected_owner():
    acl = _secure_acl(owner="PC-A1208\\IvanA")
    with pytest.raises(rs.AclViolation):
        rs.validate_state_root_acl(
            acl, expected_owner=SECURE_OWNER, modify_principal=SERVICE_PRINCIPAL
        )


def test_acl_rejects_service_principal_with_full_control_instead_of_modify():
    acl = _secure_acl(
        entries=(
            rs.AclEntry("NT AUTHORITY\\SYSTEM", "FullControl", False),
            rs.AclEntry("BUILTIN\\Administrators", "FullControl", False),
            rs.AclEntry(SERVICE_PRINCIPAL, "FullControl", False),
        )
    )
    with pytest.raises(rs.AclViolation):
        rs.validate_state_root_acl(
            acl, expected_owner=SECURE_OWNER, modify_principal=SERVICE_PRINCIPAL
        )


def test_acl_rejects_unprotected_acl():
    acl = _secure_acl(protected=False)
    with pytest.raises(rs.AclViolation):
        rs.validate_state_root_acl(
            acl, expected_owner=SECURE_OWNER, modify_principal=SERVICE_PRINCIPAL
        )


def test_read_acl_snapshot_parses_injected_powershell_output(monkeypatch, tmp_path):
    raw = json.dumps(
        {
            "owner": SECURE_OWNER,
            "protected": True,
            "entries": [
                {
                    "principal": "NT AUTHORITY\\SYSTEM",
                    "rights": "FullControl",
                    "inherited": False,
                },
                {
                    "principal": "BUILTIN\\Administrators",
                    "rights": "FullControl",
                    "inherited": False,
                },
                {
                    "principal": SERVICE_PRINCIPAL,
                    "rights": "Modify, Synchronize",
                    "inherited": False,
                },
            ],
        }
    )
    acl = rs.read_acl_snapshot(tmp_path, reader=lambda _path: raw)
    assert acl.owner == SECURE_OWNER
    assert acl.protected is True
    assert len(acl.entries) == 3


# ---------------------------------------------------------------------------
# First run
# ---------------------------------------------------------------------------


def test_first_run_has_no_prior_state_and_does_not_fail(tmp_path, capsys):
    state_root = tmp_path / "state"
    targets = _paths(tmp_path)
    result = rs.restore(
        state_root,
        targets,
        run_id="1",
        acl_snapshot=_secure_acl(),
        expected_owner=SECURE_OWNER,
        modify_principal=SERVICE_PRINCIPAL,
    )
    assert result.status == "fresh_first_run"
    assert not targets.db.exists()
    status = json.loads((state_root / "status.json").read_text())
    assert status["status"] == "fresh_first_run"
    assert "no prior state" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Restore into production-consumed paths
# ---------------------------------------------------------------------------


def _seed_db(db_path: Path) -> None:
    conn = sqlite_storage.init_db(db_path)
    conn.execute(_INSERT_PRODUCT_SQL, ("p1", "Prod", None))
    conn.execute(
        _INSERT_OFFER_SQL, ("o1", "p1", "emag", "sku1", "https://example/emag")
    )
    conn.execute(
        _INSERT_OBSERVATION_SQL, ("o1", 100.0, None, 1, "2026-09-26T09:00:00+00:00")
    )
    conn.commit()
    conn.close()


def _make_snapshot(
    state_root: Path,
    snapshot_id: str,
    *,
    db_seed=_seed_db,
    health_lines=None,
    outbox_lines=None,
    run_id="1",
) -> Path:
    snap_dir = state_root / "snapshots" / snapshot_id
    snap_dir.mkdir(parents=True)
    db_path = snap_dir / "price_history.db"
    db_seed(db_path)
    health_path = snap_dir / "scrape_health.jsonl"
    health_path.write_text(
        "\n".join(json.dumps(r) for r in (health_lines or []))
        + ("\n" if health_lines else ""),
        encoding="utf-8",
    )
    outbox_path = snap_dir / "alert_outbox.jsonl"
    outbox_path.write_text(
        "\n".join(json.dumps(r) for r in (outbox_lines or []))
        + ("\n" if outbox_lines else ""),
        encoding="utf-8",
    )
    files = {
        "price_history.db": rs._sha256(db_path),
        "scrape_health.jsonl": rs._sha256(health_path),
        "alert_outbox.jsonl": rs._sha256(outbox_path),
    }
    rs._write_manifest(snap_dir, snapshot_id=snapshot_id, run_id=run_id, files=files)
    (state_root / "CURRENT").write_text(snapshot_id, encoding="utf-8")
    return snap_dir


def test_restore_places_synthetic_state_into_production_paths(tmp_path):
    state_root = tmp_path / "state"
    prior_health = [
        {
            "run_id": "prior",
            "run_started_utc": "2026-09-26T09:00:00+00:00",
            "site": "emag",
            "products_parsed": 3,
            "last_known_good_utc": "2026-09-26T09:00:00+00:00",
        }
    ]
    prior_outbox = [
        {
            "event_id": "evt-1",
            "channel": "telegram",
            "status": "SENT",
            "created_at_utc": "2026-09-26T09:00:00+00:00",
            "dedup_key": "dk-1",
        }
    ]
    _make_snapshot(
        state_root,
        "20260926T090000000000Z_prior",
        health_lines=prior_health,
        outbox_lines=prior_outbox,
    )

    targets = _paths(tmp_path)
    result = rs.restore(
        state_root,
        targets,
        run_id="2",
        acl_snapshot=_secure_acl(),
        expected_owner=SECURE_OWNER,
        modify_principal=SERVICE_PRINCIPAL,
    )

    assert result.status == "restored"
    assert targets.db.is_file()
    assert json.loads(targets.health.read_text().splitlines()[0])["site"] == "emag"
    assert json.loads(targets.outbox.read_text().splitlines()[0])["event_id"] == "evt-1"

    conn = sqlite3.connect(str(targets.db))
    row = conn.execute("SELECT price FROM price_observations").fetchone()
    conn.close()
    assert row == (100.0,)


def test_restore_removes_stale_wal_and_shm_sidecars(tmp_path):
    state_root = tmp_path / "state"
    _make_snapshot(state_root, "20260926T090000000000Z_prior")
    targets = _paths(tmp_path)

    stale_wal = targets.db.with_name(targets.db.name + "-wal")
    stale_shm = targets.db.with_name(targets.db.name + "-shm")
    targets.db.parent.mkdir(parents=True, exist_ok=True)
    stale_wal.write_bytes(b"stale-wal-from-a-different-database")
    stale_shm.write_bytes(b"stale-shm")

    rs.restore(
        state_root,
        targets,
        run_id="2",
        acl_snapshot=_secure_acl(),
        expected_owner=SECURE_OWNER,
        modify_principal=SERVICE_PRINCIPAL,
    )

    assert not stale_wal.exists()
    assert not stale_shm.exists()
    # the restored DB must still open and validate cleanly with no stale WAL
    conn = sqlite3.connect(str(targets.db))
    assert conn.execute("PRAGMA integrity_check;").fetchone()[0] == "ok"
    conn.close()


# ---------------------------------------------------------------------------
# Save: SQLite backup API, integrity check, manifest, promotion
# ---------------------------------------------------------------------------


def test_save_snapshots_committed_wal_state_via_backup_api_and_passes_integrity_check(
    tmp_path,
):
    state_root = tmp_path / "state"
    sources = _paths(tmp_path)
    conn = sqlite_storage.init_db(sources.db)
    conn.execute(_INSERT_PRODUCT_SQL, ("p1", "Prod", None))
    conn.execute(
        _INSERT_OFFER_SQL, ("o1", "p1", "emag", "sku1", "https://example/emag")
    )
    conn.execute(
        _INSERT_OBSERVATION_SQL, ("o1", 55.5, None, 1, "2026-09-28T09:00:00+00:00")
    )
    conn.commit()  # committed to WAL, not necessarily checkpointed into the main file
    sources.health.write_text(json.dumps({"site": "emag"}) + "\n", encoding="utf-8")
    sources.outbox.write_text(
        json.dumps({"event_id": "evt-1"}) + "\n", encoding="utf-8"
    )

    result = rs.save(
        state_root,
        sources,
        run_id="2",
        acl_snapshot=_secure_acl(),
        expected_owner=SECURE_OWNER,
        modify_principal=SERVICE_PRINCIPAL,
    )
    conn.close()

    assert result.status == "saved"
    assert result.snapshot_id is not None
    snap_dir = state_root / "snapshots" / result.snapshot_id
    snap_conn = sqlite3.connect(str(snap_dir / "price_history.db"))
    assert snap_conn.execute("PRAGMA integrity_check;").fetchone()[0] == "ok"
    row = snap_conn.execute("SELECT price FROM price_observations").fetchone()
    snap_conn.close()
    assert row == (55.5,)


def test_save_writes_manifest_with_required_fields_and_matching_hashes(tmp_path):
    state_root = tmp_path / "state"
    sources = _paths(tmp_path)
    sqlite_storage.init_db(sources.db).close()
    sources.health.write_text(json.dumps({"site": "emag"}) + "\n", encoding="utf-8")
    sources.outbox.write_text("", encoding="utf-8")

    result = rs.save(
        state_root,
        sources,
        run_id="42",
        acl_snapshot=_secure_acl(),
        expected_owner=SECURE_OWNER,
        modify_principal=SERVICE_PRINCIPAL,
    )
    manifest = json.loads(
        (state_root / "snapshots" / result.snapshot_id / "manifest.json").read_text()
    )
    assert manifest["schema_version"] == rs.SCHEMA_VERSION
    assert manifest["run_id"] == "42"
    assert manifest["snapshot_id"] == result.snapshot_id
    assert "created_at_utc" in manifest
    for filename, expected_hash in manifest["files"].items():
        actual = rs._sha256(state_root / "snapshots" / result.snapshot_id / filename)
        assert actual == expected_hash


def test_malformed_jsonl_blocks_promotion_and_keeps_prior_current(tmp_path):
    state_root = tmp_path / "state"
    _make_snapshot(state_root, "20260926T090000000000Z_good")
    prior_current = (state_root / "CURRENT").read_text()

    sources = _paths(tmp_path)
    sqlite_storage.init_db(sources.db).close()
    sources.health.write_text("{not valid json\n", encoding="utf-8")
    sources.outbox.write_text("", encoding="utf-8")

    result = rs.save(
        state_root,
        sources,
        run_id="3",
        acl_snapshot=_secure_acl(),
        expected_owner=SECURE_OWNER,
        modify_principal=SERVICE_PRINCIPAL,
    )

    assert result.status == "save_failed"
    assert (state_root / "CURRENT").read_text() == prior_current


def test_save_failure_preserves_prior_current_unchanged(tmp_path):
    state_root = tmp_path / "state"
    _make_snapshot(state_root, "20260926T090000000000Z_good")
    prior_current = (state_root / "CURRENT").read_text()
    prior_snapshot_dir = state_root / "snapshots" / prior_current

    sources = _paths(tmp_path)
    # sources.db does not exist and health is malformed -> save must fail
    sources.health.write_text("not json at all {{{\n", encoding="utf-8")
    sources.outbox.write_text("", encoding="utf-8")

    result = rs.save(
        state_root,
        sources,
        run_id="3",
        acl_snapshot=_secure_acl(),
        expected_owner=SECURE_OWNER,
        modify_principal=SERVICE_PRINCIPAL,
    )

    assert result.status == "save_failed"
    assert prior_snapshot_dir.is_dir()
    assert (state_root / "CURRENT").read_text() == prior_current


def test_save_retains_only_newest_three_snapshots(tmp_path):
    state_root = tmp_path / "state"
    sources = _paths(tmp_path)
    sqlite_storage.init_db(sources.db).close()
    sources.health.write_text("", encoding="utf-8")
    sources.outbox.write_text("", encoding="utf-8")

    ids = []
    for i in range(5):
        result = rs.save(
            state_root,
            sources,
            run_id=str(i),
            acl_snapshot=_secure_acl(),
            expected_owner=SECURE_OWNER,
            modify_principal=SERVICE_PRINCIPAL,
        )
        assert result.status == "saved"
        ids.append(result.snapshot_id)

    remaining = {p.name for p in (state_root / "snapshots").iterdir() if p.is_dir()}
    assert remaining == set(ids[-3:])


# ---------------------------------------------------------------------------
# Corruption fallback
# ---------------------------------------------------------------------------


def test_damaged_current_falls_back_to_older_valid_snapshot(tmp_path):
    state_root = tmp_path / "state"
    _make_snapshot(state_root, "20260926T080000000000Z_older")
    _make_snapshot(state_root, "20260926T090000000000Z_newer")
    # Corrupt the CURRENT (newer) snapshot's manifest hash.
    manifest_path = (
        state_root / "snapshots" / "20260926T090000000000Z_newer" / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["price_history.db"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    targets = _paths(tmp_path)
    result = rs.restore(
        state_root,
        targets,
        run_id="3",
        acl_snapshot=_secure_acl(),
        expected_owner=SECURE_OWNER,
        modify_principal=SERVICE_PRINCIPAL,
    )

    assert result.status == "restored_fallback"
    assert result.snapshot_id == "20260926T080000000000Z_older"
    quarantined = state_root / "quarantine" / "20260926T090000000000Z_newer"
    assert quarantined.is_dir()


def test_no_valid_snapshot_quarantines_and_starts_fresh(tmp_path, capsys):
    state_root = tmp_path / "state"
    _make_snapshot(state_root, "20260926T090000000000Z_bad")
    manifest_path = (
        state_root / "snapshots" / "20260926T090000000000Z_bad" / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["price_history.db"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    targets = _paths(tmp_path)
    result = rs.restore(
        state_root,
        targets,
        run_id="3",
        acl_snapshot=_secure_acl(),
        expected_owner=SECURE_OWNER,
        modify_principal=SERVICE_PRINCIPAL,
    )

    assert result.status == "fresh_after_corruption"
    assert not targets.db.exists()
    assert (state_root / "quarantine" / "20260926T090000000000Z_bad").is_dir()
    status = json.loads((state_root / "status.json").read_text())
    assert status["status"] == "fresh_after_corruption"
    assert "::error::" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# ACL failure disables restore/save entirely
# ---------------------------------------------------------------------------


def test_insecure_acl_disables_restore_without_falsely_claiming_success(
    tmp_path, capsys
):
    state_root = tmp_path / "state"
    _make_snapshot(state_root, "20260926T090000000000Z_prior")
    targets = _paths(tmp_path)
    insecure = _secure_acl(
        entries=(
            rs.AclEntry("NT AUTHORITY\\SYSTEM", "FullControl", False),
            rs.AclEntry("BUILTIN\\Administrators", "FullControl", False),
            rs.AclEntry("NT AUTHORITY\\NETWORK SERVICE", "FullControl", False),
        )
    )
    result = rs.restore(
        state_root,
        targets,
        run_id="2",
        acl_snapshot=insecure,
        expected_owner=SECURE_OWNER,
        modify_principal=SERVICE_PRINCIPAL,
    )
    assert result.status == "disabled_insecure_state_dir"
    assert not targets.db.exists()
    assert "::error::" in capsys.readouterr().out


def test_insecure_acl_disables_save(tmp_path):
    state_root = tmp_path / "state"
    sources = _paths(tmp_path)
    sqlite_storage.init_db(sources.db).close()
    sources.health.write_text("", encoding="utf-8")
    sources.outbox.write_text("", encoding="utf-8")
    insecure = _secure_acl(protected=False)
    result = rs.save(
        state_root,
        sources,
        run_id="1",
        acl_snapshot=insecure,
        expected_owner=SECURE_OWNER,
        modify_principal=SERVICE_PRINCIPAL,
    )
    assert result.status == "disabled_insecure_state_dir"
    assert not (state_root / "CURRENT").exists()


# ---------------------------------------------------------------------------
# Two-run cross-run correctness, workspace wiped between runs
# ---------------------------------------------------------------------------


def test_two_runs_with_workspace_wipe_preserve_cross_run_safeguards(
    tmp_path, monkeypatch
):
    """Simulates: run 1 -> save -> workspace wiped (like actions/checkout
    clean) -> run 2 restores -> cooldown, dead-man, quarantine, and SQLite
    history all see run 1's state.
    """
    state_root = tmp_path / "state"
    acl_kwargs = dict(
        acl_snapshot=_secure_acl(),
        expected_owner=SECURE_OWNER,
        modify_principal=SERVICE_PRINCIPAL,
    )

    def fresh_workspace() -> Path:
        ws = tmp_path / "ws"
        if ws.exists():
            import shutil

            shutil.rmtree(ws)
        ws.mkdir()
        return ws

    # --- Run 1: first run, no prior state ---
    ws1 = fresh_workspace()
    targets1 = rs.RuntimeStatePaths(
        db=ws1 / "price_history.db",
        health=ws1 / "scrape_health.jsonl",
        outbox=ws1 / "alert_outbox.jsonl",
    )
    restore1 = rs.restore(state_root, targets1, run_id="1", **acl_kwargs)
    assert restore1.status == "fresh_first_run"

    monkeypatch.setattr(scrape, "DB_FILE", targets1.db)
    monkeypatch.setattr(scrape, "SCRAPE_HEALTH_FILE", targets1.health)
    monkeypatch.setattr(notify, "DB_FILE", targets1.db)
    monkeypatch.setattr(notify, "OUTBOX_FILE", targets1.outbox)

    db1 = sqlite_storage.init_db(targets1.db)
    db1.execute(_INSERT_PRODUCT_SQL, ("p1", "Prod", None))
    db1.execute(
        _INSERT_OFFER_SQL, ("o1", "p1", "brandx", "sku1", "https://example/brandx")
    )
    db1.execute(
        _INSERT_OBSERVATION_SQL, ("o1", 199.0, None, 1, "2026-09-26T09:00:00+00:00")
    )
    db1.commit()
    db1.close()

    run1_started = "2026-09-26T09:00:00+00:00"
    health_records_1 = [
        {
            "run_id": "1",
            "run_started_utc": run1_started,
            "store": "brandx",
            "products_parsed": 0,
            "last_known_good_utc": run1_started,
        },
        {
            "run_id": "1",
            "run_started_utc": run1_started,
            "store": "emag",
            "products_parsed": 5,
            "last_known_good_utc": run1_started,
        },
    ]
    targets1.health.write_text(
        "\n".join(json.dumps(r) for r in health_records_1) + "\n", encoding="utf-8"
    )
    outbox_record_1 = {
        "event_id": "evt-1",
        "channel": "telegram",
        "status": "SENT",
        "created_at_utc": run1_started,
        "last_attempt_at_utc": run1_started,
        "dedup_key": "brandx:sku1:discount",
    }
    targets1.outbox.write_text(json.dumps(outbox_record_1) + "\n", encoding="utf-8")

    save1 = rs.save(state_root, targets1, run_id="1", **acl_kwargs)
    assert save1.status == "saved"

    # --- Workspace wiped, exactly like actions/checkout with clean: true ---
    ws2 = fresh_workspace()
    targets2 = rs.RuntimeStatePaths(
        db=ws2 / "price_history.db",
        health=ws2 / "scrape_health.jsonl",
        outbox=ws2 / "alert_outbox.jsonl",
    )
    restore2 = rs.restore(state_root, targets2, run_id="2", **acl_kwargs)
    assert restore2.status == "restored"

    # Dead-man / quarantine: run 2 sees run 1's health records.
    restored_health = [
        json.loads(line) for line in targets2.health.read_text().splitlines() if line
    ]
    assert any(
        r["store"] == "brandx" and r["products_parsed"] == 0 for r in restored_health
    )

    # Two-run quarantine condition: brandx failed in run 1; if it fails again
    # in run 2, scrape.py's _quarantined_stores must be able to see both.
    run2_started = "2026-09-26T11:00:00+00:00"
    health_records_2 = restored_health + [
        {
            "run_id": "2",
            "run_started_utc": run2_started,
            "store": "brandx",
            "products_parsed": 0,
            "last_known_good_utc": run1_started,
        }
    ]
    quarantined = scrape._quarantined_stores(health_records_2)
    assert "brandx" in quarantined

    # Cooldown: run 2's outbox replay/cooldown logic must see run 1's SENT
    # record for the same dedup key.
    restored_outbox = [
        json.loads(line) for line in targets2.outbox.read_text().splitlines() if line
    ]
    assert restored_outbox[0]["dedup_key"] == "brandx:sku1:discount"
    monkeypatch.setattr(notify, "OUTBOX_FILE", targets2.outbox)
    effective = notify._load_outbox_effective()
    block = notify._find_cooldown_block(
        "brandx:sku1:discount",
        24,
        effective,
        datetime.fromisoformat(run2_started),
        channel="telegram",
    )
    assert block is not None
    assert block["dedup_key"] == "brandx:sku1:discount"

    # SQLite holds observations from both runs.
    db2 = sqlite_storage.init_db(targets2.db)
    db2.execute(_INSERT_OBSERVATION_SQL, ("o1", 189.0, None, 1, run2_started))
    db2.commit()
    prices = [
        r[0]
        for r in db2.execute(
            "SELECT price FROM price_observations ORDER BY scraped_at"
        ).fetchall()
    ]
    db2.close()
    assert prices == [199.0, 189.0]


# ---------------------------------------------------------------------------
# Workflow-text invariants
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def workflow_text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def test_workflow_restore_precedes_scrape_prices(workflow_text):
    restore_idx = workflow_text.index("Restore runtime state")
    scrape_idx = workflow_text.index("name: Scrape prices")
    assert restore_idx < scrape_idx


def test_workflow_save_step_uses_if_always(workflow_text):
    save_idx = workflow_text.index("Save runtime state")
    preceding = workflow_text[:save_idx]
    following = workflow_text[save_idx:]
    next_if = following.index("if:")
    assert "always()" in following[next_if : next_if + 20]
    assert preceding  # sanity: step exists after some prior content


def test_workflow_has_visible_final_state_gate_after_healthcheck(workflow_text):
    gate_idx = workflow_text.index("name: Verify runtime state gate")
    healthcheck_idx = workflow_text.index("name: Ping healthcheck")
    assert gate_idx > healthcheck_idx
    gate_section = workflow_text[gate_idx:]
    assert "::error::" in gate_section
    assert "exit 1" in gate_section
    # must not be hidden behind continue-on-error / warning-only
    gate_step_text = gate_section[: gate_section.index("\n\n")]
    assert "continue-on-error" not in gate_step_text


def test_workflow_checkout_cleaning_remains_enabled(workflow_text):
    assert "clean: false" not in workflow_text


def test_workflow_scrape_job_has_no_contents_write(workflow_text):
    scrape_job_start = workflow_text.index("scrape-analyze-notify:")
    persist_job_start = workflow_text.index("\n  persist:")
    scrape_job_text = workflow_text[scrape_job_start:persist_job_start]
    assert "contents: write" not in scrape_job_text
    assert "contents: read" in scrape_job_text


def test_workflow_concurrency_unchanged(workflow_text):
    assert "group: monitor" in workflow_text
    assert "cancel-in-progress: false" in workflow_text
    assert "cancel-in-progress: true" not in workflow_text


def test_workflow_all_actions_are_sha_pinned(workflow_text):
    import re

    uses_lines = re.findall(r"^\s*uses:\s*(\S+)", workflow_text, re.MULTILINE)
    assert uses_lines, "expected at least one 'uses:' line in the workflow"
    for ref in uses_lines:
        assert "@" in ref, f"action ref not pinned at all: {ref}"
        sha = ref.split("@", 1)[1]
        assert re.fullmatch(r"[0-9a-f]{40}", sha), f"action ref not SHA-pinned: {ref}"
