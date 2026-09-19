# Progress Log

Handoff log for completed tasks. Each entry: issue closed, tests status, commit SHA.

## Sprint 0 (T-01..T-04): package, domain models, CI gate, watchlist validation
Tests: 52 passed, 100% coverage on bf_price_monitor/, ruff + mypy strict clean. Commits: 7496b68, f18eb4b, 9f1d44f, 70c69d1, d53452f.

## Sprint 0 reconciliation: merge and push to origin/master
Replaced `-e .` with `.` in requirements.txt for a non-editable install on the monitor.yml runner (a0f9246). Merged origin/master's automated price-history commits with no conflicts (cf2594a) and pushed. Post-merge: 52 tests passed, ruff clean, mypy --strict clean on bf_price_monitor/ (mypy --strict across the whole repo still reports 108 pre-existing errors in scripts/ and tests/, unrelated to Sprint 0 and out of scope). Verified bf_price_monitor/, pyproject.toml, uv.lock, and .github/workflows/quality.yml are present on origin/master. Pushed commit: cf2594a.

## T-09 (#15): per-store health metrics and zero-match regression alerting
Added scrape_health.jsonl per-store health records, Critical Selector Drift quarantine + Telegram alert (via new scrape_health_alerts.json handoff read by notify.py), and a 24h dead-man check.
Tests: 103 passed (96 existing unchanged + 7 new), ruff check and ruff format clean on changed files. Commit: 0c643d4.
