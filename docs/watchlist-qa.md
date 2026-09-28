# Watchlist QA Report (T-34, #45)

Generated: 2026-09-28T14:48:33.849674+00:00 UTC (2026-09-28 Europe/Bucharest)

Black Friday 2026 date: **2026-11-27** (Computed: day after the fourth Thursday of November 2026 (fixed US-Thanksgiving-anchored Black Friday definition); no project-specific override found in docs/DECISIONS.md.)
Seven-day verification window starts 2026-11-20; this audit falls inside it: **False**.

Active watches checked: **4**.
Retailer checks performed: **4**.
PASS: **4**. Not verified/failed: **0**.

Overall acceptance status: **BLOCKED**

## Per-watch evidence

| watch_id | configured_site | retailer_checked | query | status | matches | reason |
|---|---|---|---|---|---|---|
| babebbc9-2a56-5d0c-b57c-9ca8ad44d7b4 | emag | emag | laptop lenovo v15 | PASS | 55 |  |
| 4d24a18d-89d8-59f0-8798-68babdceb715 | pcgarage | pcgarage | laptop asus vivobook | PASS | 20 |  |
| ba36f3c8-f2ac-5cd9-9bed-a5828741ff66 | flanco | flanco | laptop asus vivobook | PASS | 11 |  |
| 2db088f5-a629-5b44-b01b-b75e3ee88e42 | emag | emag | iphone 15 | PASS | 3 |  |

## Corrections / removals

None applied this run. All 4 active watches passed on their first live check
with specific, correctly-scoped queries; no evidence in this session
supported a correction or removal. Target-price feasibility: the one watch
with `target_price` set (`babebbc9…`, 2500.0 RON) is within the observed
historical price range (`observed_low` 1933.99 RON) -- no warning. The other
three watches have no `target_price` set.

## Sample evidence

- `babebbc9…` (emag, "laptop lenovo v15"): 55 matching titles, all
  `in_stock`, structurally valid `emag.ro` URLs, e.g. "Laptop Lenovo V15 G4
  AMN cu procesor AMD Ryzen 5 7520U...".
- `4d24a18d…` (pcgarage, "laptop asus vivobook"): 20 matching titles,
  structurally valid `pcgarage.ro` URLs.
- `ba36f3c8…` (flanco, "laptop asus vivobook"): 11 matching titles,
  structurally valid `flanco.ro` URLs.
- `2db088f5…` (emag, "iphone 15"): 3 matching titles (eMAG's first search
  page returns few token-exact "iphone 15" matches -- other iPhone models
  and accessories are correctly excluded by the existing token-match
  contract).

## Unresolved operational failures

None. No watch returned UNVERIFIED, MISMATCH, NO_RESULTS, UNSUPPORTED, or
MANUAL_REVIEW in the final run used for this report. Two individual listing
cards (one on eMAG, one on Flanco) failed per-card price extraction during
the live runs and were logged by the reused scraper code to
`data/extraction_failures.jsonl` (existing production diagnostic log, not
part of this task's deliverables and not committed) -- this did not affect
any watch's overall status, since each watch still had many other matching
cards.

## Verification

```bash
python -m pytest tests/test_qa_watchlist.py -v   # 22 passed
ruff check scripts/qa_watchlist.py tests/test_qa_watchlist.py   # all checks passed
ruff format --check scripts/qa_watchlist.py tests/test_qa_watchlist.py   # 2 files already formatted
ruff check .            # all checks passed
ruff format --check .   # 37 files already formatted
pytest -v               # 436 passed
python -m mypy --strict bf_price_monitor/   # no issues (scripts/ is not the strict-mypy target, per pyproject.toml/CLAUDE.md)
uv run python scripts/qa_watchlist.py       # live run against the real data/watchlist.json and data/price_history.json
```

## Acceptance matrix

| # | Criterion | Implementation | Automated test | Live/manual evidence | Status |
|---|---|---|---|---|---|
| 1 | Every active watchlist entry verified against a live search result within 7 days of Black Friday | `scripts/qa_watchlist.py` calls each watch's real retailer scraper live | `tests/test_qa_watchlist.py` (22 tests, offline/mocked) | Live run 2026-09-28: 4/4 active watches PASS, but Black Friday 2026 is 2026-11-27 (computed: day after the fourth Thursday of November) and today (2026-09-28 UTC / Europe/Bucharest) is ~60 days before the 7-day pre-BF window (2026-11-20 to 2026-11-27) | **BLOCKED** (timing) |
| 2 | Stale or mismatched entries removed or corrected | Auditor classifies MISMATCH/NO_RESULTS/UNVERIFIED and would report any found | 22 unit tests cover MISMATCH, NO_RESULTS, UNVERIFIED, duplicate, and URL-validity paths | Live run found zero stale or mismatched entries; nothing required correction or removal | PASS (nothing to fix) |
| 3 | Count of entries checked documented | `active_watch_count` / `retailer_check_count` fields in the JSON report and this document | Covered by `test_report_contains_timestamps_counts_evidence_and_summary` and others | This report: 4 active watches, 4 retailer checks | PASS |

## Close/no-close decision

**Issue #45 is NOT closed.** Criterion 1 cannot be satisfied today regardless
of audit quality: the acceptance criteria require verification within seven
days of Black Friday 2026 (2026-11-20 through 2026-11-27), and this session
runs on 2026-09-28. The auditor itself is fully implemented, tested, and has
already been proven against the live, real watchlist with a clean 4/4 PASS
result -- the remaining step is purely a matter of re-running
`uv run python scripts/qa_watchlist.py` once inside that window and
confirming the result still holds, then closing the issue with that
evidence.
