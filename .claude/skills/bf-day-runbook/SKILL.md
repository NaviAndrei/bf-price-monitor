---
name: bf-day-runbook
description: Black Friday day-of operations status. Reads the runbooks, recent monitor.yml runs, the runner status, the alert outbox and scrape health, then returns a short status and a yes/no recommendation on falling back to scripts/run_emergency_local.ps1. Recommends only, never acts. Use during the BF window or whenever alerts go quiet.
disable-model-invocation: true
context: fork
agent: Explore
background: false
allowed-tools: Read, Grep, Glob, Bash(gh run list *), Bash(gh api repos/NaviAndrei/bf-price-monitor/actions/runners), Bash(gh run view *), Bash(python *)
---

# BF day runbook status

This skill reports and recommends. It never fixes, restarts, pushes, edits or
sends anything. It runs in a forked `Explore` subagent (`context: fork`,
`agent: Explore`), which has no Write or Edit tools; that, not `allowed-tools`,
is what keeps it read-only. Use Bash only for read-only commands (`gh run list`,
`gh run view`, `gh api` GET, and `python -c` that only reads files). If a step
would change anything, stop and say so.

`background: false` makes the forked run return its result in the same turn.
That field needs Claude Code 2.1.218 or newer; this repo's installed version
was 2.1.285 when the skill was written. On an older version, delete the line
and expect the result to arrive later as a notification.

## Sources (read these, do not rely on memory)

- `docs/runbooks/RUNNER_OUTAGE.md`, section 1 (trigger criteria) and section 2 (diagnostics)
- `docs/runbooks/BF_WEEK_CADENCE.md` (schedule; section 4 covers what to do if a retailer blocks us)
- `scripts/run_emergency_local.ps1` (the fallback; do not run it)

## Procedure

1. **Latest runs:** `gh run list --repo NaviAndrei/bf-price-monitor --workflow monitor.yml --limit 5 --json status,conclusion,createdAt,databaseId`.
   Report the newest run and whether there are 2 or more consecutive failed or
   cancelled runs (a trigger in RUNNER_OUTAGE.md section 1).
2. **Runner:** `gh api repos/NaviAndrei/bf-price-monitor/actions/runners`. Report
   `status` (`online`/`offline`) and `busy`. `offline` supports the "host
   unreachable" trigger.
3. **Dead-man silence trigger (over 180 minutes without a Healthchecks.io ping):**
   this skill cannot check it, because `HEALTHCHECK_URL` is a secret and must not be
   read. Report it as "not checked" and say what the user should look at.
4. **Alert outbox:** read `data/alert_outbox.jsonl` if it exists. It is append-only; the
   effective status of an event is the last record for its `event_id`
   (see `_write_outbox_record` in `scripts/notify.py`). Count events whose effective
   status is `PENDING`, and flag any older than 300 seconds (`REPLAY_GRACE_SECONDS`) by
   `created_at_utc`. Never print `alert_payload` or `send_payload`. If the file does
   not exist, say "no outbox file on this machine".
5. **Scrape health:** read the last record per `store` in `data/scrape_health.jsonl`
   (fields: `store`, `products_parsed`, `parse_failures`, `challenge_detected`,
   `challenge_wait_entered`, `fetch_attempts`, `rate_limit_hits`, `last_known_good_utc`).
   That file is local to whichever machine ran the scrape and is wiped by the runner's
   `git clean`, so if it is missing or old, use the durable record instead:
   `gh run view <id> --repo NaviAndrei/bf-price-monitor --log` and look for the
   `[scrape:health]` JSON line, which `scripts/scrape.py` prints every run.
6. **Decide the fallback recommendation:** yes if any trigger from RUNNER_OUTAGE.md
   section 1 is evidenced (2+ consecutive failed runs, runner offline, or silence
   reported by the user), otherwise no. If a retailer is blocking us but the runner
   is healthy, the answer is no and the pointer is section 4 of
   `BF_WEEK_CADENCE.md` ("Rollback plan: a site starts blocking"), not the emergency script.

## Output (short, 15 lines at most)

```
Latest run: <id> <status>/<conclusion> <time>   Consecutive failures: <n>
Runner: <online|offline>, busy=<bool>
Dead-man (Healthchecks): not checked by this skill
Outbox: <n> events PENDING (<m> older than 300s)   [or: no outbox file here]
Scrape health: emag <products_parsed>/<parse_failures>, pcgarage ..., flanco ...  (source: file | run log)
Fallback to run_emergency_local.ps1: YES|NO because <one sentence tied to a trigger>
Next step: <one line, pointing to the runbook section>
```

Never claim a check passed that you did not run. Print `not checked` or `not available`.
