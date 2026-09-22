# Runner outage contingency runbook

Covers what to do when the self-hosted Windows runner (`PC-A1208`) that
`.github/workflows/monitor.yml` depends on becomes unreachable or degraded,
so the scrape → analyze → notify cycle can keep running from another
machine instead of going silent. Written after the 2026-09-22 incident,
where the pipeline was silent for ~38 hours from a `setup-uv` crash
(fixed by PR #53) compounded by a transient DNS failure on `PC-A1208`
(see `docs/progress.md`'s outage entry for the full timeline).

This is the manual fallback. `monitor.yml` itself is unmodified by this
document — the goal here is a human-operated escape hatch, not a second
automated pipeline.

## 1. Trigger criteria

Invoke this runbook if **any one** of these is true:

- **Dead-man silence:** the Healthchecks.io check configured in
  `monitor.yml`'s `persist` job (see `docs/security/secret-rotation-runbook.md`
  for how `HEALTHCHECK_URL` is issued) has not received a ping in over
  **180 minutes**. At the `0 */2 * * *` cron cadence, a healthy runner pings
  roughly every 2 hours — 180 minutes of silence means at least one full
  cycle was missed.
- **Repeated CI failures:** `monitor.yml` has **2 or more consecutive**
  failed or cancelled runs (check with the command in section 2).
- **Host unreachable:** `PC-A1208` doesn't respond on the network at all —
  VPN adapter down, power outage, or the machine is otherwise offline.

A single failed run that a rerun immediately fixes is not a trigger for
this runbook — that's normal transient flakiness (see the 2026-09-22
outage's own retry/backoff mitigations already in `monitor.yml`). This
runbook is for sustained silence or repeated failure, not one bad run.

## 2. Diagnostic checklist

Run these, in order, before deciding between local fallback (section 3)
and cloud failover (section 4):

**a. Is the runner registered and idle/offline in GitHub's eyes?**
```bash
gh api repos/NaviAndrei/bf-price-monitor/actions/runners
```
Look at the returned runner's `status` field (`online` / `offline`) and
`busy` flag. `offline` means GitHub itself has lost the listener
heartbeat — go straight to step (b) on the host if you can reach it, or
assume the host is down if you can't.

**b. Is the Windows runner service actually running on the host?**
(Run this on `PC-A1208` itself, e.g. via RDP if the network path is up but
something else is wrong.)
```powershell
Get-Service actions.runner.*
```
Expect a single service in the `Running` state. `Stopped` means the
listener process died without the host itself being down — restart it
with `Start-Service` and re-check `gh api .../actions/runners` before
assuming a full outage.

**c. What does the listener's own log say?**
```powershell
Get-Content "C:\actions-runner\_diag\Runner_*.log" -Tail 100
```
(Adjust the path if the runner was installed elsewhere.) Look for the
most recent entries — a clean shutdown, an unhandled exception, or a
listener that simply stopped writing (consistent with the DNS-hang half
of the 2026-09-22 incident) all point to different next actions.

**d. Are the last runs' failure reasons already known?**
```bash
gh run list --workflow=monitor.yml --limit 5 --repo NaviAndrei/bf-price-monitor
gh run view <run-id> --repo NaviAndrei/bf-price-monitor --log-failed
```
If the failure is a code/dependency problem shared by every runner (e.g.
another `setup-uv` regression), fixing that in the workflow is faster than
either fallback path below — a fallback run on a different machine would
hit the exact same bug.

## 3. Immediate fallback execution (local machine)

Use this when the host is down/unreachable but you have another machine
(personal laptop, spare desktop) available right now.

**a. Clone the repository and verify the lockfile.**
```powershell
git clone https://github.com/NaviAndrei/bf-price-monitor.git
cd bf-price-monitor
git log -1 --format="%H %s" -- uv.lock
```
The clone already contains the current `data/watchlist.json` and
`data/price_history.json` — both are committed to `origin/master` by
`monitor.yml`'s `persist` job on every run with data changes, so there is
no separate data-transfer step.

**b. Export required secrets into the local shell environment.**
Never paste these into a file that could be committed. Set them for the
current PowerShell session only:
```powershell
$env:HF_TOKEN = "<value from your own password manager / HF account>"
$env:TELEGRAM_BOT_TOKEN = "<value>"
$env:TELEGRAM_CHAT_ID = "<value>"
$env:HEALTHCHECK_URL = "<value>"   # optional — see step (e)
```
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are hard requirements —
`scripts/notify.py` reads them with `os.environ[...]` and crashes
immediately if either is unset. `HF_TOKEN` is soft: if it's missing or the
Hugging Face call fails, `scripts/analyze.py` falls back to a local Ollama
call, and if that's unavailable too (likely, on a machine with no Ollama
install), falls back again to a deterministic rule-only summary — the run
still completes and alerts still go out, just without an LLM-written
summary sentence.

**c. Run the emergency execution script.**
```powershell
.\scripts\run_emergency_local.ps1
```
This runs preflight checks, then `uv sync`, then Playwright's Chromium
install if missing, then the three pipeline stages (`scrape.py` →
`analyze.py` → `notify.py`) in order, stopping at the first failure. See
the script's own header comment for its exit codes.

**d. Commit and push the updated price history back to `origin/master`.**
```powershell
git add data/price_history.json data/watchlist.json
git commit -m "chore: emergency fallback run [skip ci]" --author="NaviAndrei <andrei.ivan1208@gmail.com>"
git pull --rebase origin master
git push origin master
```
Use `git pull --rebase` before pushing, the same pattern `monitor.yml`'s
own `persist` job uses (`.github/workflows/monitor.yml`), since the
primary runner may come back online and push its own commit while this
fallback run is in progress.

**e. Clear the dead-man alert.**
If you set `HEALTHCHECK_URL` in step (b), the "Ping healthcheck" logic
isn't run by any of the three pipeline scripts — it's specific to
`monitor.yml`'s `persist` job. Ping it manually once the run above
succeeds:
```powershell
Invoke-WebRequest -Uri $env:HEALTHCHECK_URL -TimeoutSec 15
```
If you don't have the URL handy, log into Healthchecks.io directly and
click **"I'm alive"** on the relevant check instead.

## 4. Temporary cloud runner failover

Use this when no local machine is available (traveling, no spare
hardware) — a GitHub-hosted runner or short-lived VPS runs the same
script instead.

**a. Fastest option — a one-off `workflow_dispatch` on `ubuntu-latest`.**
Rather than standing up new workflow YAML during an active incident,
clone the repo on a GitHub Codespace or any cloud VM, export the same four
secrets as section 3(b) (pull them from your own secret manager, never
from this repo's GitHub Secrets store directly, which requires repo
write access this runbook doesn't assume you have from an arbitrary
machine), and run `scripts/run_emergency_local.ps1` (or a `pwsh`
equivalent — PowerShell 7+ runs on Linux) exactly as in section 3.
- `pcgarage` and `flanco` scraping goes through headless Chromium via
  Playwright (`scrape.py:453-509`) — `playwright install chromium` must
  succeed on whatever OS the cloud host runs; the script handles this the
  same way regardless of platform.
- `emag` scraping uses plain `requests` (`scrape.py:512-543`) with no
  browser dependency.

**b. If a cloud IP gets blocked where the self-hosted runner wasn't.**
The self-hosted runner's residential/ISP IP may pass checks that a cloud
provider's IP range doesn't (this is a known, not-yet-solved gap — see
`scrape.py:782-798`'s note on `altex.ro`'s Akamai block already being
out of scope even from the primary runner). If a store starts returning
403s or Cloudflare-challenge pages that never clear even after
`fetch_with_browser`'s retry loop, treat that store's data as
temporarily unavailable for this run rather than escalating to proxy
infrastructure — that is explicitly out of scope for this runbook (see
the note at the top of this document).

**c. Standing up a temporary GitHub-hosted runner in `monitor.yml` itself.**
Only do this if the outage is expected to last beyond a single manual
run (e.g. multi-day travel). Temporarily point `runs-on:` at
`ubuntu-latest` for the `scrape-analyze-notify` job — **not** `persist`,
which pushes to `master` and should stay on infrastructure you control
directly — run it via `workflow_dispatch`, then revert the `runs-on:`
change once `PC-A1208` is confirmed healthy again (section 5). Do not
leave this change in place on `master` longer than the outage itself.

## 5. Recovery and rollback

**a. Bring `PC-A1208` back online cleanly.**
- If the fix was a code change (e.g. another `setup-uv` regression),
  merge the fix to `master` first, then restart the runner service:
  ```powershell
  Restart-Service actions.runner.*
  ```
- If the fix was infrastructure (DNS, VPN, power), restart the service
  the same way after confirming basic connectivity
  (`Test-Connection files.pythonhosted.org` or similar) rather than
  assuming network recovery alone fixed the listener.

**b. Verify the primary runner is actually taking jobs again.**
```bash
gh api repos/NaviAndrei/bf-price-monitor/actions/runners
gh workflow run monitor.yml --repo NaviAndrei/bf-price-monitor
gh run watch <run-id> --repo NaviAndrei/bf-price-monitor --exit-status
```
Confirm the run lands on the self-hosted runner (not a leftover
`ubuntu-latest` override from section 4c) and completes successfully.

**c. Clear the dead-man alert if it's still showing down.**
A successful `persist` job run pings Healthchecks.io on its own
(`monitor.yml:204-223`) — no manual ping should be needed once step (b)
completes, but confirm the check's dashboard shows "Up" before considering
the incident closed.

**d. Reconcile any fallback commits.**
If section 3(d) or 4 pushed one or more `chore: emergency fallback run`
commits while the primary runner was down, no special reconciliation is
needed — `git pull --rebase` in that step already handled ordering against
whatever else landed on `master`. Just confirm `git log --oneline -10` on
`master` shows a clean, non-conflicted history before considering the
runner fully recovered.

**e. Log the incident.**
Append a short entry to `docs/progress.md` (task completed, root cause,
commit SHAs) once things are stable, matching the existing 2026-09-22
outage entry's format — this runbook doesn't replace that handoff log.
