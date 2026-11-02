# BF-week scrape cadence & rate-limit safety runbook

Covers how `.github/workflows/monitor.yml` tightens its scrape cadence for
Black Friday week, the request-safety limits that apply on every run, and
what to do if eMAG, PC Garage, or Flanco start blocking us. Written for
T-32 (#43).

## 1. Schedule decision

| Period | Cron | What actually scrapes |
|---|---|---|
| Normal (all year) | `0 */2 * * *` | Every run, all watches |
| BF peak window | `0 1-23/2 * * *` (odd UTC hours, plus the 2-hour cron on even hours) | Every hour, all watches |

- The odd-hour cron is **always registered** in `monitor.yml`, but a
  lightweight `gate` job (`scripts/cadence_gate.py`) lets it through only
  when the current UTC time falls inside the window defined by two
  repository **variables** (Settings → Secrets and variables → Actions →
  Variables):
  - `BF_PEAK_START_UTC` — ISO 8601 with offset. Set on 2026-10-28 to `2026-11-05T18:00:00+00:00`
  - `BF_PEAK_END_UTC` — ISO 8601 with offset. Set on 2026-10-28 to `2026-11-30T21:59:00+00:00`
- Both variables are **unset by default**. With either one unset, unparsable,
  or missing its UTC offset, the gate **fails closed**: the odd-hour cron is
  skipped. The 2-hour cron and `workflow_dispatch` always pass the gate.
- **The BF window is not hardcoded.** eMAG, PC Garage, and Flanco announce
  their Black Friday campaign dates themselves, and not far enough ahead to
  bake into code. The variables hold the window the owner configured on
  2026-10-28: 5 Nov 20:00 to 30 Nov 23:59 Romania local time (UTC+2 in late
  November). That is wider than the original blueprint target of "Thursday
  20:00 to Friday 23:59", which is kept for reference only and is no longer
  the configured window.
- **Changing the window:** edit the two variables, with
  `gh variable set BF_PEAK_START_UTC --body "<ISO 8601>" --repo NaviAndrei/bf-price-monitor`
  (same for `BF_PEAK_END_UTC`), or use Settings → Secrets and variables →
  Actions → Variables. The gate reads one start and end pair, so a second
  window needs a code change.
- **Cadence decision (owner, 2026-10-30, final):** the peak cadence is
  **1 hour**, not the 30 minutes in the original blueprint. The string in
  `monitor.yml` must equal `TIGHT_CADENCE_CRON` in `scripts/cadence_gate.py`;
  `tests/test_monitor_workflow.py` fails if they differ. If only the
  workflow string changed, the gate would treat the new cron as ungated and
  it would scrape all year.
- **Scope note:** `data/watchlist.json` has no priority field today, so
  during the window **all** watches run hourly. With the current watchlist
  size (4 watches over 3 stores), that is 4 page loads per hour. The two live trial runs in section 3 were at a much
  tighter spacing and showed no blocking. Per-item priority would be a
  separate change.
- **No overlap:** the 2-hour cron fires on even UTC hours and the window cron
  on odd UTC hours, so the two never fire in the same minute and nothing
  queues behind the `monitor` concurrency group. GitHub may start a
  scheduled run late, so a delayed run can still wait for the previous one;
  `cancel-in-progress` stays false.
- Outside the window, the odd-hour gate check costs one checkout plus one
  Python call on the self-hosted runner (seconds). It never installs
  Playwright or touches a retailer.

### Configured window and campaign calendar

Coverage at the hourly cadence, with the window starting 5 Nov 20:00 local:

- eMAG 6 Nov campaign: inside the window.
- evoMAG main stage 6-8 Nov: inside the window.
- Altex stage 2: inside the window only if its dates fall between 5 and 30
  Nov. Its dates were not supplied.
- Flanco (22 Oct to 30 Nov): 5 to 30 Nov runs hourly. 22 Oct to 4 Nov
  runs at the 2-hour cadence.
- Altex/Media Galaxy (29 Oct to 18 Nov) and evoMAG (29 Oct to 15 Nov): 29 Oct
  to 4 Nov runs at the 2-hour cadence.
- Last weekend: the window already runs to 30 Nov 23:59 local, so Flanco's
  last day is covered. No second window is configured.

Campaign dates, **as of 2026-10-28, from press reports, not store pages**.
They were supplied by the owner and have not been checked by the assistant
against store pages. Dates can change.

| Store | Campaign | Dates |
|---|---|---|
| eMAG | Campaign day | 6 Nov (one day) |
| evoMAG | Full campaign; main stage | 29 Oct to 15 Nov; main stage 6 to 8 Nov |
| Flanco | Campaign | 22 Oct to 30 Nov |
| Altex / Media Galaxy | Campaign; stage 2 dates not supplied | 29 Oct to 18 Nov |
| PC Garage | Not officially announced | none |

## 2. Request-safety limits (always on, every run)

All in `scripts/scrape.py`:

- **Jitter between watches:** `random.uniform(4, 9)` seconds (was 3–7).
- **Per-domain floor:** `_pace_domain()` guarantees at least
  `DOMAIN_PACING_FLOOR_SECONDS = 5.0` between two requests to the same store
  within one run. This includes retries and the browser path.
- **User-agent rotation:** each browser context and each plain HTTP attempt
  picks a UA at random from `CHROME_USER_AGENTS`: Chrome 131 and Chrome 130
  on Windows 10 x64.
- **403/429 tracking:** every fetch attempt increments
  `fetch_attempts`, and every 403/429 increments `rate_limit_hits`. Both are
  written per store into `data/scrape_health.jsonl`. The runtime-state step
  restores and saves this file between runs (B2, #42).
- **Automatic cooldown:** if a store's most recent health record shows
  `rate_limit_hits / fetch_attempts > 5%`, that store is skipped for any run
  starting within **30 minutes** of that record's `run_started_utc`. The
  console shows
  `[<store>] skipped: cooling down after a >5% 403/429 rate ...`, and the
  skipped run writes no new health record. The cooldown is derived from
  history on every run rather than stored as a flag, the same pattern as the
  T-09 Critical Selector Drift quarantine. It clears on its own.
  (A quarantined store is likewise re-probed every 6 hours and released by
  its first non-zero result, #79; see README "Per-store scraper health".)
  - **Limitation:** the 30 minutes are counted from the flagged run's
    *start*. At the hourly cadence the next scheduled run starts about an
    hour later, so the cooldown has already expired. In practice, it only
    blocks a manual dispatch or a delayed run that starts right behind the
    flagged one. It does **not** skip a scheduled cycle. If a store keeps
    tripping it, use the rollback steps in section 4 rather than relying on
    the cooldown.
- **Pre-existing, unchanged:** 403/429 retry backoff of 5s / 10s / 20s
  (`RETRY_BACKOFFS`), Cloudflare challenge detection, the Critical Selector
  Drift quarantine, and the dead-man check.

## 3. Trial evidence

Two real, back-to-back runs of `uv run scripts/scrape.py` against all three
live retailers, from the owner's dev machine on 2026-09-28. The runs were
about 105 seconds apart, which is much tighter than the hourly peak
cadence. Source: the local `data/scrape_health.jsonl` rows they wrote.

| Run start (UTC) | Store | fetch_attempts | rate_limit_hits | challenge_detected | products_parsed |
|---|---|---|---|---|---|
| 2026-09-28T17:59:44 | emag | 2 | 0 | false | 57 |
| 2026-09-28T17:59:44 | pcgarage | 1 | 0 | false | 20 |
| 2026-09-28T17:59:44 | flanco | 1 | 0 | false | 10 |
| 2026-09-28T18:01:29 | emag | 2 | 0 | false | 56 |
| 2026-09-28T18:01:29 | pcgarage | 1 | 0 | false | 20 |
| 2026-09-28T18:01:29 | flanco | 1 | 0 | false | 10 |

Run IDs: `d1447e2f-7ec2-454c-ba14-86b70b6c8b90`,
`afa78f2a-dcee-408f-a038-6a6c5569ab80`.

**What this does and does not show:**

- It shows that two consecutive full runs close together got zero 403/429
  responses and zero challenge pages on any of the three stores, with the
  new UA rotation and pacing active.
- **NOT YET MEASURED:** a sustained multi-hour hourly cadence, behaviour
  from the production runner's IP (`PC-A1208`), and behaviour under BF-week
  anti-bot tightening on the retailers' side. Retailers may harden
  protections during BF itself, so check the section 5 dashboards on the
  first peak day.
- The pre-existing `parse_failures` counts (eMAG 1, Flanco 2) are extraction
  misses, not blocks. They are unrelated to cadence.

## 4. Rollback plan: a site starts blocking

**Signals** (any one of these):

- Console lines `skipped: cooling down after a >5% 403/429 rate` for the
  same store on more than one run.
- `rate_limit_hits > 0` or `challenge_detected: true` in
  `scrape_health.jsonl` for a store across consecutive runs.
- A Critical Selector Drift alert for a store that was healthy the day before.
  Some block pages return 200 with no product markup.

**Steps, in order of escalation:**

1. **Drop back to the 2-hour cadence (instant, no deploy).** Delete, or
   clear, the `BF_PEAK_START_UTC` repository variable. The next odd-hour
   trigger's gate fails closed and skips. No code change, no commit, no
   runner restart needed. This is the primary lever.
   ```bash
   gh variable delete BF_PEAK_START_UTC --repo NaviAndrei/bf-price-monitor
   ```
2. **Shrink the window instead of removing it.** If blocking only happens
   at particular times, edit `BF_PEAK_START_UTC`/`BF_PEAK_END_UTC` to a
   narrower range.
   ```bash
   gh variable set BF_PEAK_END_UTC --body "2026-11-27T12:00:00+00:00" --repo NaviAndrei/bf-price-monitor
   ```
3. **Pause the one blocked store.** Temporarily remove that store's watches
   from `data/watchlist.json` in a normal commit. The other stores keep
   running at the current cadence.
4. **Stop all scheduled scraping.** Disable the workflow and run manually
   with `workflow_dispatch` when needed.
   ```bash
   gh workflow disable monitor.yml --repo NaviAndrei/bf-price-monitor
   ```
   Re-enable with `gh workflow enable monitor.yml --repo NaviAndrei/bf-price-monitor`.
5. **Slow down every run (code change).** Raise
   `DOMAIN_PACING_FLOOR_SECONDS`, the `random.uniform(4, 9)` jitter, or
   `RATE_LIMIT_COOLDOWN_MINUTES` in `scripts/scrape.py`, then commit to
   `master`.

**Recovery:** once a blocked store shows two consecutive clean runs on the
2-hour cadence (`rate_limit_hits: 0`, `challenge_detected: false`), restore
the window variables to re-enable the hourly cadence.

## 4a. Reliable trigger (catch-up dispatcher)

**Why.** GitHub drops and delays scheduled runs. Measured from `gh` output
on 100 schedule runs, 2026-10-21 05:05 to 2026-10-30 20:56 UTC: 41% of the
2-hour runs and 11.5% of the old `*/30` runs were delivered; scraping
averaged 4.9 runs per day against 12 expected; the gap between scraping runs
had a median of 5.1 h and a maximum of 7.4 h. The runner picked up jobs in a
median of 2 s, so the loss is on GitHub's side. The 1-hour cadence stays as
decided; this section only makes it arrive.

**How it works.** `scripts/peak_dispatch.py` runs every 10 minutes from
Windows Task Scheduler on the runner PC and does one thing: dispatch
`monitor.yml` (`gh workflow run monitor.yml --ref master`) when a scrape is
due. It changes nothing in the workflow; the crons and the gate stay.

- Window: `BF_PEAK_START_UTC` / `BF_PEAK_END_UTC`, checked with the same
  `is_in_peak_window` as the gate (both ends inclusive). Missing
  variables mean "no peak window": nothing is dispatched unless
  `--baseline-interval` is on, in which case that cadence applies. Invalid
  values: no dispatch, exit 1. Every `gh` call carries
  `--repo NaviAndrei/bf-price-monitor`, so it never depends on the folder it
  runs from.
- Last scrape: the newest `monitor.yml` run of any event whose
  `scrape-analyze-notify` job was not skipped. Runs the gate skipped do not
  count.
- A run that is queued or in progress (and younger than 2 h) means a scrape
  is under way: no dispatch.
- Due: 55 minutes after the last scrape (60 minus 5 minutes slack), or when
  there is no scrape in the last 24 hours.
- `--baseline-interval MINUTES` (default 0, off) applies the same rule
  outside the window.
- `--dry-run` prints the decision and dispatches nothing.
- Each tick appends one line (UTC time, decision, reason) to
  `%LOCALAPPDATA%\bf-price-monitor\peak_dispatch.log`, trimmed to its last 500
  lines when it passes 1 MB. Tokens are never written.
- Exit codes: 0 dispatched or nothing to do, 1 any `gh` or parse error
  (nothing dispatched), 2 bad usage.

**Owner setup (not done by the assistant).** The token is never typed on a
command line, pasted into chat or stored in the repo. It lives in a file
encrypted with Windows DPAPI (readable only by your Windows user on this PC),
next to the log, outside the repo.

1. Create a fine-grained personal access token for this repository only,
   with `Actions: Read and write` (list runs and dispatch), `Variables: Read`
   (the two window variables) and the default `Metadata: Read`. Give it an
   expiry you will remember.
2. Store it encrypted. On the GitHub page, copy the token (the copy button;
   do not paste it anywhere), then run this. It waits for you, reads the
   clipboard, and saves only a value that starts with `github_pat_` or `ghp_`
   and is at least 40 characters. It prints the length, never the token, and
   clears the clipboard either way:
   ```powershell
   & {
     $dir = "$env:LOCALAPPDATA\bf-price-monitor"
     New-Item -ItemType Directory -Force $dir | Out-Null
     Read-Host "Copy the token now, then press Enter" | Out-Null
     $t = "$(Get-Clipboard -Raw)".Trim()
     $n = $t.Length
     if ($t -match '^(github_pat_|ghp_)[A-Za-z0-9_]+$' -and $n -ge 40) {
       ConvertTo-SecureString $t -AsPlainText -Force | ConvertFrom-SecureString |
         Set-Content -NoNewline "$dir\gh_token.dpapi"
       "saved, length $n"
     } else {
       "rejected, length $n"
     }
     Set-Clipboard -Value " "
   }
   ```
   `rejected` means nothing was written (an older file stays as it was): copy
   the token again and rerun.
3. Create the wrapper `%LOCALAPPDATA%\bf-price-monitor\peak_dispatch.ps1`.
   It decrypts the token into `GH_TOKEN` for this one process, trims it (a
   stray space or newline makes GitHub answer 401), runs the script with
   absolute paths, and appends an `error` line to the log if the wrapper
   itself throws:
   ```powershell
   $ErrorActionPreference = "Stop"
   $dir = Join-Path $env:LOCALAPPDATA "bf-price-monitor"
   $file = Join-Path $dir "gh_token.dpapi"
   try {
     # Without a token gh silently uses your main keyring login, which is broader
     # than this token, so a missing or empty file must stop the run here.
     $text = if (Test-Path $file) { "$(Get-Content $file -Raw)".Trim() }
     if (-not $text) { throw "gh_token.dpapi is missing or empty: not falling back to the keyring login" }
     $ss = ConvertTo-SecureString $text
     $token = [System.Net.NetworkCredential]::new("", $ss).Password.Trim()
     $env:GH_TOKEN = $token
     # A non-zero python exit does not throw; the script logs its own errors.
     & "D:\Projects\bf-price-monitor\.venv\Scripts\python.exe" "D:\Projects\bf-price-monitor\scripts\peak_dispatch.py"
     exit $LASTEXITCODE
   } catch {
     # Only exceptions are logged here (same line format as peak_dispatch.py).
     try {
       $msg = "wrapper: $($_.Exception.GetType().Name): $($_.Exception.Message)"
       $msg = ($msg -replace '(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]+', '[redacted]') -replace '\s+', ' '
       if ($msg.Length -gt 200) { $msg = $msg.Substring(0, 200) }
       $now = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss'Z'")
       [System.IO.File]::AppendAllText((Join-Path $dir "peak_dispatch.log"), "$now`terror`t$msg`r`n", [System.Text.UTF8Encoding]::new($false))
     } catch { }
     exit 1
   } finally {
     Remove-Item Env:GH_TOKEN -ErrorAction SilentlyContinue
   }
   ```
   A missing or empty token file ends the wrapper with exit code 1 and that
   message, and nothing is dispatched. The wrapper also appends it to the
   log as an `error` line starting `wrapper:` (see Failure modes).
   Add `--baseline-interval 120` to the script line only after one
   supervised day (see Rollback).
4. Check whether the task exists, then register it on a fresh install only.
   Inspecting changes nothing and is always safe:
   ```powershell
   Export-ScheduledTask -TaskName "bf-peak-dispatch"
   Get-ScheduledTaskInfo -TaskName "bf-peak-dispatch"
   ```
   If the task exists, compare it with "Task configuration" below and stop:
   do not register over it or rebuild it from defaults. Fresh install, no
   task of that name yet (the script stops if one exists, and builds the
   wrapper path from `LOCALAPPDATA`; the backtick-quoted `"$wrapper"` keeps a
   path with spaces as one argument):
   ```powershell
   & {
     $ErrorActionPreference = "Stop"
     $name = "bf-peak-dispatch"
     $wrapper = Join-Path $env:LOCALAPPDATA "bf-price-monitor\peak_dispatch.ps1"
     if (-not (Test-Path -LiteralPath $wrapper)) { throw "wrapper not found: $wrapper" }
     if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
       throw "task $name already exists: not changing it"
     }
     $arguments = "--headless powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$wrapper`""
     $action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\conhost.exe" -Argument $arguments
     $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 10)
     $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
     $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 8)
     Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
   }
   ```
   Without `-Force`, `Register-ScheduledTask` also refuses an existing name.
   Afterwards run `Export-ScheduledTask` and compare it with the table below.
5. Verify before 5 Nov 18:00 UTC. Run the wrapper by hand, then look at the
   log after the task has run a few times:
   ```powershell
   powershell -NoProfile -File "$env:LOCALAPPDATA\bf-price-monitor\peak_dispatch.ps1"
   Get-Content "$env:LOCALAPPDATA\bf-price-monitor\peak_dispatch.log" -Tail 5
   ```
   Outside the window with no baseline it logs `skip: outside peak window,
   baseline off`. Keep the PC awake from 5 to 30 Nov (no sleep on AC power).

**Task configuration.** Read back from the live task on 2026-11-02 with
`Export-ScheduledTask` and `Get-ScheduledTask`. Settings the export omits are
at their defaults, so it does not print them. The setup script above builds
the same values; the script itself has not been run (syntax and argument
checks only).

| Item | Value | Status |
|---|---|---|
| Principal | Current user, `InteractiveToken` (runs only while that user is signed in), run level Limited | Supported setting |
| Trigger | Time trigger, start 2026-11-01 16:11, repeat every `PT10M`, no end | Supported setting |
| Action | `C:\Windows\System32\conhost.exe` with `--headless powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "<LOCALAPPDATA>\bf-price-monitor\peak_dispatch.ps1"` | `conhost --headless` is not a documented Task Scheduler feature; it works here (last result 0, log lines written) but may change with Windows |
| `ExecutionTimeLimit` | `PT8M` (Task Scheduler default is `PT72H`) | Supported setting. Forced stop at 8 minutes and child-process cleanup are untested |
| Batteries | `DisallowStartIfOnBatteries` false, `StopIfGoingOnBatteries` false | Supported setting |
| `MultipleInstancesPolicy` | `IgnoreNew` | Supported setting; a tick that starts while the last one still runs is dropped without a log line |
| `WakeToRun`, `StartWhenAvailable` | Both false (defaults, so absent from the export) | Supported setting; a sleeping PC is not woken and missed ticks are not replayed |
| `UseUnifiedSchedulingEngine` | `true` | A documented XML element, but nothing here shows why it is true: on this machine saving the task set it, and restoring `PT72H` did not clear it. PT8M is not shown to need it |

**Troubleshooting.** The log line carries the `gh` exit code and the first
200 characters of its error, with token-shaped text removed.

| Log text | Meaning | Fix |
|---|---|---|
| `HTTP 401` / `Bad credentials` | GitHub does not accept the token: wrong, expired, revoked, truncated, or it carries whitespace | Recreate the file in step 2 (the wrapper trims, but a half-pasted token cannot be repaired) |
| `HTTP 403` | Token is valid but lacks a permission | Add `Actions: Read and write` or `Variables: Read` |
| `HTTP 404` / `Not Found` | The token cannot see this repository (fine-grained tokens return 404 for hidden resources) | Grant the token this repository |
| `variable ... was not found` | The window variable is not set; this is not an error | Set it, or leave it if no window is wanted |

**Rollback.** `schtasks /Change /TN "bf-peak-dispatch" /DISABLE` stops it
completely. Deleting `BF_PEAK_START_UTC` only ends the peak window: with
`--baseline-interval` on, the dispatcher keeps its baseline cadence, so
remove that flag too (or disable the task) for a full stop.

**Failure modes.**

- PC off or asleep: nothing is dispatched, and the runner is offline too.
- Token that GitHub rejects (see Troubleshooting): every tick exits 1 and logs `error`;
  the scheduled crons still run at their delivered rate.
- Failures before the script runs: a missing or empty `gh_token.dpapi`, a token
  file that cannot be decrypted, or a wrong Python path make the wrapper's
  `catch` append `error  wrapper: <exception type>: <message>` (first 200
  characters, token-shaped text removed) and exit 1. A non-zero exit from
  Python is not logged by the wrapper; the script logs its own errors. Tested
  2026-11-02 with the token file renamed: the line was written. No line is
  written when the wrapper file is missing or has a syntax error, when
  `powershell.exe` cannot start, or when the task is killed at 8 minutes.
- Do not rely on the task result: with the `conhost --headless` action, Task
  Scheduler showed `LastTaskResult` 0 for that run although the wrapper exited
  1 (cause not investigated). Use the log.
- A tick longer than 8 minutes is stopped by the execution limit. The script
  allows far longer (each `gh` call may take 60 s and a tick can make dozens),
  so a hung tick is cut off and writes no log line (inferred, untested).
- A delayed cron run can still arrive after a dispatch and queue behind it
  in the `monitor` concurrency group, giving two scrapes close together.
  The dispatcher only counts runs it can see, so it cannot prevent that.
- A scrape that fails after the scrape job started still counts as a scrape
  (the job was not skipped), so a persistent failure is not retried hourly.

## 5. Where to look

- Per-store health: `data/scrape_health.jsonl` on the runner, restored from
  `C:\bf-monitor-runtime-state` at the start of each run.
- Whether the gate let a trigger through: the `gate` job's
  `[cadence_gate] ... -> should_run=<True|False>` log line. When the gate
  says no, `scrape-analyze-notify` and `persist` show as *skipped* rather
  than failed.
- Recent runs:
  ```bash
  gh run list --workflow monitor.yml --repo NaviAndrei/bf-price-monitor --limit 20
  ```
