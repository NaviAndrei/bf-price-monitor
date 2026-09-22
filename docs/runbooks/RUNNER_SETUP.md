# Runner hardening & rebuild runbook

Covers hardening the self-hosted Windows runner (`PC-A1208`) that
`.github/workflows/monitor.yml` depends on, and rebuilding it from scratch
if the host is lost or replaced. Written for T-16 (#26): the runner
executes Playwright headless Chromium against external retailer pages it
does not control, so a compromised or malicious page is the realistic
threat model this hardening reduces the blast radius of — not a hostile
GitHub Actions job (that risk is already addressed by never registering a
`pull_request`/`pull_request_target` trigger on this public repo, per the
warning at the top of `monitor.yml`).

This is a setup/hardening reference, not an incident runbook. For "the
runner is down, keep the pipeline running from elsewhere," see
`docs/runbooks/RUNNER_OUTAGE.md` instead.

## 1. Service account configuration

As of the 2026-09-23 inspection, the runner service
(`actions.runner.NaviAndrei-bf-price-monitor.PC-A1208`) runs as
`NT AUTHORITY\NETWORK SERVICE`:

```powershell
Get-WmiObject Win32_Service | Where-Object { $_.Name -like "*runner*" } |
    Select-Object Name, StartName, State, PathName
```

`NETWORK SERVICE` is a built-in low-privilege account — it is not a local
administrator and cannot install software or modify most system state.
That satisfies "non-administrative account." It is **not** a dedicated,
isolated account, though: it's shared machine-wide by any other Windows
service that happens to run under the same identity, so a compromise via
one `NETWORK SERVICE` process is not contained to just the runner.

If stronger isolation is needed later, create a dedicated local service
account and reassign the service to it:

```powershell
# Create a dedicated, non-administrative local account for the runner
New-LocalUser -Name "svc-gh-runner" -NoPassword -AccountNeverExpires `
    -Description "Dedicated service account for the bf-price-monitor GitHub Actions runner"
Set-LocalUser -Name "svc-gh-runner" -PasswordNeverExpires $true

# Reassign the service (requires the runner service to be stopped/restarted --
# do NOT run this against the live listener without prior approval, see
# the constraints in the T-16 task and Section 5 below for the safe sequence)
sc.exe config "actions.runner.NaviAndrei-bf-price-monitor.PC-A1208" obj= ".\svc-gh-runner" password= ""
```

Reassigning the live service account is an online-service change, not a
read-only or purely additive one — treat it as requiring explicit sign-off
before running, separately from the DACL and cleanup changes below, which
don't touch the running service at all.

## 2. Directory access control lists (DACLs)

Inspection on 2026-09-23 found both `C:\actions-runner` and
`C:\actions-runner\cache\bf-monitor` granting `NT AUTHORITY\Authenticated
Users` the **Modify** right, in addition to the runner's own dedicated
group (`PC-A1208\GITHUB_ActionsRunner_G17fda`, Full Control) and
`SYSTEM`/`Administrators` (Full Control):

```powershell
icacls "C:\actions-runner"
icacls "C:\actions-runner\cache\bf-monitor"
```

`Authenticated Users` means every logged-on account on the host, not just
the runner service or administrators — broader write access than the
runner needs. Tighten both paths to just the runner's own group, `SYSTEM`,
and `Administrators`:

```powershell
# Remove the overly broad grant
icacls "C:\actions-runner" /remove:g "NT AUTHORITY\Authenticated Users"
icacls "C:\actions-runner\cache\bf-monitor" /remove:g "NT AUTHORITY\Authenticated Users"

# Re-assert the intended baseline explicitly (safe to re-run; /grant is additive)
icacls "C:\actions-runner" /grant "PC-A1208\GITHUB_ActionsRunner_G17fda:(OI)(CI)F"
icacls "C:\actions-runner" /grant "SYSTEM:(OI)(CI)F"
icacls "C:\actions-runner" /grant "Administrators:(OI)(CI)F"
icacls "C:\actions-runner\cache\bf-monitor" /grant "PC-A1208\GITHUB_ActionsRunner_G17fda:(OI)(CI)F"
icacls "C:\actions-runner\cache\bf-monitor" /grant "SYSTEM:(OI)(CI)F"
icacls "C:\actions-runner\cache\bf-monitor" /grant "Administrators:(OI)(CI)F"

# Verify
icacls "C:\actions-runner"
icacls "C:\actions-runner\cache\bf-monitor"
```

Re-run `icacls` (no flags) after any Windows Update or runner reinstall —
installers sometimes re-widen ACLs to `Authenticated Users` or `Everyone`
as a default, silently undoing this hardening.

## 3. Browser cache pinning

`monitor.yml` pins `PLAYWRIGHT_BROWSERS_PATH` to
`C:\actions-runner\ms-playwright-cache`, separate from the interactive
user's default `%LOCALAPPDATA%\ms-playwright` cache — this matters because
the runner service account (`NETWORK SERVICE`, see Section 1) has its own
profile, distinct from whatever account is logged in interactively, so the
install step and the scrape step must agree on one fixed path rather than
relying on a per-profile default that could differ between the two.

T-18 (#24) additionally pins `UV_PROJECT_ENVIRONMENT` to
`C:\actions-runner\cache\bf-monitor\venv`, fingerprinted against `uv.lock`
via a sibling `env.marker` file — this is the directory Section 2's DACL
tightening and `scripts/runner_cleanup.ps1`'s integrity check both apply
to. Neither the cleanup script nor this runbook deletes from that cache;
only `monitor.yml`'s own fingerprint mismatch logic rebuilds it.

## 4. Attack surface reduction

Chromium runs headless against arbitrary external pages, so limiting what
a compromised renderer/child process could do next reduces the impact of a
future browser exploit reaching this host. Recommended, in order of impact
vs. setup cost:

1. **Enable Windows Defender Attack Surface Reduction (ASR) rules** that
   block Office/script-driven child process creation and credential theft
   from LSASS — even though Chromium isn't Office, the "block process
   creations from PSExec and WMI" and "block credential stealing from the
   Windows local security authority subsystem" rules narrow what a
   post-exploitation payload could do on this host:
   ```powershell
   Set-MpPreference -AttackSurfaceReductionRules_Ids 9e6c4e1f-7d60-472f-ba1a-a39ef669e4b2 -AttackSurfaceReductionRules_Actions Enabled
   Set-MpPreference -AttackSurfaceReductionRules_Ids 9e6c4e1f-7d60-472f-ba1a-a39ef669e4b2 -AttackSurfaceReductionRules_Actions AuditMode  # test first
   ```
   Test in `AuditMode` first and check `Get-MpPreference` /
   Event Viewer (`Microsoft-Windows-Windows Defender/Operational`) for
   false positives against `scrape.py`'s own subprocess usage before
   switching to `Enabled` — this repo's CLAUDE.md forbids touching
   firewall/DNS/network routes, but ASR rules are host-level malware
   mitigation, not network routing, so they're in scope here.
2. **Windows Defender Application Control / Exploit Protection** on
   `chrome.exe`/`node.exe` — enable DEP, ASLR, and CFG (most are on by
   default on modern Windows; verify with
   `Get-ProcessMitigation -Name chrome`) rather than authoring a custom
   policy from scratch.
3. **Do not run the runner service interactively logged-in as an admin
   session** — Section 1's `NETWORK SERVICE` choice already avoids this;
   don't regress it by ever starting the runner manually from an elevated
   interactive shell "just to test something."

Full EDR/AV product rollout is out of scope for this runbook — Defender's
built-in ASR and Exploit Protection cover the realistic threat (a
compromised headless browser process) without adding a new dependency.

## 5. Process & temp cleanup

`scripts/runner_cleanup.ps1` (T-16, #26) terminates orphaned
`chrome`/`chromium` processes older than 30 minutes and purges `%TEMP%`
items older than 24 hours, including Playwright's `scoped_dir*` profile
directories. `monitor.yml`'s `scrape-analyze-notify` job runs it as the
last step with `if: always()`, so it runs whether or not scrape/analyze/
notify succeeded.

Run it manually any time, e.g. after a manual `workflow_dispatch` or when
investigating disk usage:

```powershell
.\scripts\runner_cleanup.ps1 -DryRun   # report only, changes nothing
.\scripts\runner_cleanup.ps1           # actually terminate/delete
```

It never deletes from `C:\actions-runner\cache\bf-monitor` (the T-18
venv/browser cache) — it only reports whether `env.marker` and the `venv`
directory are both present or both absent, and warns (without deleting
anything) if only one is present, since that's the inconsistent state that
would otherwise make `monitor.yml`'s fingerprint check trust a
partially-built environment.

## 6. Full reinstall / disaster recovery

If `PC-A1208` is lost, replaced, or needs a clean reinstall:

1. **Provision the host.** Windows 11 Pro (or Server equivalent), Python
   3.11+, `uv`, PowerShell 7+. Join it to whatever network path the
   scrapers need — no VPN/firewall/DNS changes are this runbook's concern
   (see `docs/DECISIONS.md`'s PC-A1208 VPN watch item for the one known,
   unresolved routing quirk on the current host).
2. **Register the runner.** Follow GitHub's own runner registration flow
   from the repo's Settings → Actions → Runners → "New self-hosted
   runner" page (`gh` doesn't script initial registration — a token
   from that page is required). Install as a Windows service so it
   survives reboots and interactive logoff.
3. **Set the service account.** Immediately after registration, apply
   Section 1 — don't leave the service on whatever default account the
   installer chose without reviewing it.
4. **Apply the DACLs.** Run Section 2's `icacls` commands against the
   fresh `C:\actions-runner` install before the service processes its
   first real job.
5. **Recreate the cache directories.** `C:\actions-runner\cache\bf-monitor`
   and `C:\actions-runner\ms-playwright-cache` don't need to be
   pre-populated — `monitor.yml`'s cache-miss path (T-18, #24) builds
   both automatically on the first run. Just confirm the DACLs from
   Section 2 are in place on the parent `cache` directory before that
   first run, so the freshly created subdirectories inherit them.
6. **Apply Section 4's ASR/Exploit Protection settings.**
7. **Confirm secrets are configured** at the repo level (Settings →
   Secrets and variables → Actions): `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_CHAT_ID`, `HF_TOKEN` (optional), `HEALTHCHECK_URL`
   (optional). See `docs/security/secret-rotation-runbook.md` for how
   each one is issued/rotated — this runbook doesn't duplicate that.
8. **Run one manual `workflow_dispatch`** and confirm it lands on the new
   runner and completes successfully before re-enabling the `0 */2 * * *`
   schedule's normal cadence (the schedule trigger itself isn't
   host-specific, so nothing needs to change there — this step is purely
   a smoke test of the new host).
9. **Verify cleanup runs.** Check the workflow run's "Runner cleanup"
   step log for a normal `[runner_cleanup] Cleanup complete.` line, even
   on a run with nothing to clean.
