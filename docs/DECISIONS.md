# Decisions Log

## 2026-09-22 — Issue #5 stray comment
Comment on issue #5 reads "Check and update the names of my table from a AI Tuhoty" —
unrelated/garbled text, does not match issue content. Treated as noise, not acted on.
Revisit only if it recurs or a related issue references it.

## 2026-09-22 — Quality Gate does not enforce ruff format --check (issue #54)
Discovered while pushing T-17 (#25): `scripts/analyze.py` fails `ruff format --check`
but has passed CI through several merges, including four same-day Dependabot PRs
(#50-#53). Confirms the Quality Gate workflow only runs `ruff check` (lint), not
`ruff format --check` (formatting) — drift can accumulate silently between the two.
Filed as its own issue rather than folded into #25, since it's unrelated pre-existing
tech debt, not a regression from T-17. See #54 for the two remediation options
(add a format-check CI step + one repo-wide format pass, vs. explicitly documenting
formatting as unenforced).

## 2026-09-22 — analyze.py formatting drift deliberately deferred
While closing #25, only the one `ruff format --check`-flagged line in `analyze.py`
(the long `print(...)` in `main()`) was hand-fixed, committed alone as 8a1b5fd. Three
other pre-existing formatting issues in the same file — a missing blank line after
the `RawAlertCandidate` class, quote-style on the Romanian JSON-prompt string in
`build_omnibus_prompt`, and the `get_analysis(...)` signature wrapping — were left
untouched on purpose, because a full `ruff format` pass would have rewritten all four
spots in one commit, mixing unrelated cosmetic changes into a security-task commit.
This is why `ruff format --check scripts/analyze.py` still fails after 8a1b5fd — that
failure is expected and tracked by #54, not a sign the fix was incomplete.

## 2026-09-22 — T-18 re-prioritized ahead of T-16
T-18 re-prioritized ahead of T-16 — today's outage was a PyPI-fetch failure,
not a runner-hardening gap; a cached/immutable environment (T-18) would have
prevented it outright by removing the PyPI dependency from routine scheduled
runs. T-16 remains necessary but doesn't address this specific failure mode.

Unresolved watch item: PC-A1208 has a VPN adapter (PIA) with route metric
32000 vs LAN's 25 — unusually high, possibly indicating past tunnel
instability. Not fixed, not proven causal to the DNS failures. Revisit if
DNS errors recur.