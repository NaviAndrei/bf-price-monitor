---
name: create-migration
description: Add a numbered, idempotent SQLite schema migration (the _MIGRATIONS tuple in bf_price_monitor/storage/sqlite.py, PRAGMA user_version) with real-validator checks, a grep for runtime readers, and a run-twice test. Use when the user asks to add a table, column or index to data/price_history.db, or to change a migration script.
disable-model-invocation: true
allowed-tools: Read, Grep, Glob, Edit, Write, Bash(python -m pytest *), Bash(python scripts/verify_migration.py *), Bash(ruff *), Bash(grep *)
---

# Create a migration

Argument: a short description of the schema change, `$ARGUMENTS`.

This repo has three kinds of "migration". Pick the right one first; they have
different validators:

| Kind | Where | Validator |
|---|---|---|
| Schema change to `data/price_history.db` | `_MIGRATIONS` in `bf_price_monitor/storage/sqlite.py` (versions 1-3 exist; numbered, applied via `PRAGMA user_version`) | `tests/unit/test_sqlite_storage.py`, `tests/unit/test_feedback_storage.py` |
| One-off JSON-to-SQLite history import | `scripts/migrate_history_to_sqlite.py` | `python scripts/verify_migration.py` (independent recount from the source JSON) |
| Watchlist shape | `scripts/migrate_watchlist_to_modern.py` | `validate_watchlist()` in `bf_price_monitor/config/validator.py` |

## The two traps this skill exists for

- **Schema-valid is not production-safe:** Validate migration output programmatically against the actual validator (e.g. `validate_watchlist()`) before approving promotion — never by eyeballing printed JSON.
- **Schema validation misses undeclared downstream dependencies:** A field a downstream consumer reads at runtime may never be declared in the schema. Grep actual runtime usage before claiming any migration is "1:1, no behavior loss."

## Procedure

1. Read `docs/DECISIONS.md` for entries about the tables involved before editing anything.
2. State which kind of migration this is (table above).
3. For a schema change, append one tuple `(N+1, (statements...))` to `_MIGRATIONS`. The module comment says it: "Append new versions; never edit an applied one." Each version runs in one `BEGIN IMMEDIATE` transaction together with its `user_version` bump, so keep every statement in the tuple. `SCHEMA_VERSION` is derived from the last entry; do not edit it.
4. Keep the change additive. Anything that drops, renames or retypes existing data needs the user's explicit approval first.
5. Work only on a temporary database (`tmp_path`). Never point a test or script at `data/price_history.db`.
6. Run the validator for the kind of migration, then grep for runtime readers of every touched name (`scripts/runtime_state.py` snapshots and restores the database, so always check it).
7. Add the run-twice test and a rollback test, following the patterns named in `CHECKLIST.md` row 8 and 9.
8. Work through every row of [CHECKLIST.md](CHECKLIST.md) and report each with its evidence. Finish with `ruff check .`, `ruff format --check .` and `python -m pytest -x --tb=short -q`, with exit codes.
9. For an independent second opinion, ask the `migration-reviewer` agent to review the diff. Read-only safety comes from that agent's tool list, not from `allowed-tools`.

## Notes

- `allowed-tools` here only pre-approves the listed tools to cut permission prompts. It does not restrict anything; the repo's `permissions.deny` rules and the branch/price-history hooks still apply.
- Do not edit `scripts/` application code as part of a schema migration unless the user asks. Report the readers that need updating instead.
- Do not add a `docs/progress.md` entry unless the user asks.
