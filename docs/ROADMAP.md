# Roadmap Snapshot (source of truth: GitHub milestones/issues, refresh weekly)

| Sprint | Status | Due | Notes |
|---|---|---|---|
| 0-2 | Done | 09-16 to 09-23 | Foundations, correctness, notification reliability |
| 3 | Done | 09-27 | All P0 security sub-issues closed |
| 4 | Done (0 open issues) | 09-29 | SQLite storage migration |
| 5 | Done (0 open issues) | 09-30 | Performance/AI guardrails |
| 6 | Done (0 open issues) | 10-01 | Features MVP |
| 7 | Done (0 open issues) | 10-29 | BF readiness dry run |
| Q4 backlog | In progress | 12-31 | 4 open: #47 post-BF retro, #40 / #37 / #36 feedback labels and anomaly pilot |

Outside a milestone: #75 local watchlist dashboard, Phase 1 (open).

Shipped since the last snapshot: #79 (quarantined stores are re-probed every
6 hours, so a quarantine can clear) and #80 (health-alert text identifies the
outage, so a repeat outage is not deduped). Neither has been observed on a
live repeat outage yet; see `docs/DECISIONS.md`.

Housekeeping: milestones for Sprints 0, 1, 4, 5, 6 and 7 are still marked
open on GitHub although they have no open issues; closing them is left to
the owner.

Last synced: 2026-10-19 from `gh issue list` and the milestones API. Re-sync
via GitHub milestones before each sprint kickoff.
