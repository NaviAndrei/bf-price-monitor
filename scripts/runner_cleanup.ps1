<#
.SYNOPSIS
    Idempotent post-run maintenance for the self-hosted runner (PC-A1208):
    kills orphaned Playwright/Chromium processes and purges stale temp
    profile debris left behind by headless browser runs.

.DESCRIPTION
    Part of T-16 (#26), runner hardening. Playwright launches Chromium in
    headless mode against untrusted external retailer pages; a run that
    crashes or times out can leave the browser process and its scoped temp
    profile directory behind. This script is meant to run after every
    scrape-analyze-notify job (via `if: always()` in monitor.yml) so that
    debris never survives to the next scheduled run, whether or not the
    job itself succeeded.

    Does not touch C:\actions-runner\cache\bf-monitor (the T-18 venv/browser
    cache) beyond reporting whether its marker and venv directory are both
    present -- that cache is deliberately long-lived across runs, so this
    script only verifies it, it never deletes from it.

.PARAMETER DryRun
    Report what would be terminated or deleted without actually
    terminating any process or deleting any file.

.PARAMETER MaxProcessAgeMinutes
    Chromium/chrome processes older than this are considered orphaned and
    are terminated. Default 30 -- longer than any single scrape run takes.

.PARAMETER MaxTempAgeHours
    Temp files/directories older than this are purged. Default 24 -- one
    scheduled run happens every 2 hours, so anything older than a day was
    never cleaned by a prior run.

.EXAMPLE
    .\scripts\runner_cleanup.ps1 -DryRun

.EXAMPLE
    .\scripts\runner_cleanup.ps1
#>

param(
    [switch]$DryRun,
    [int]$MaxProcessAgeMinutes = 30,
    [int]$MaxTempAgeHours = 24
)

$EXIT_OK = 0

function Write-Step {
    param([string]$Message)
    Write-Output "[runner_cleanup] $Message"
}

function Stop-OrphanedBrowserProcesses {
    $cutoff = (Get-Date).AddMinutes(-$MaxProcessAgeMinutes)
    $procs = Get-Process -Name "chrome", "chromium" -ErrorAction SilentlyContinue
    if (-not $procs) {
        Write-Step "No chrome/chromium processes found."
        return
    }
    foreach ($proc in $procs) {
        $age = if ($proc.StartTime) { (Get-Date) - $proc.StartTime } else { $null }
        $isOrphaned = (-not $proc.StartTime) -or ($proc.StartTime -lt $cutoff)
        if ($isOrphaned) {
            $ageDesc = if ($age) { "{0:N0} min old" -f $age.TotalMinutes } else { "age unknown" }
            if ($DryRun) {
                Write-Step "[DryRun] Would terminate PID $($proc.Id) ($($proc.ProcessName), $ageDesc)."
            } else {
                Write-Step "Terminating PID $($proc.Id) ($($proc.ProcessName), $ageDesc)."
                Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

function Clear-StaleTempFiles {
    $tempPath = $env:TEMP
    if ([string]::IsNullOrWhiteSpace($tempPath) -or -not (Test-Path $tempPath)) {
        Write-Step "TEMP path not found, skipping temp cleanup."
        return
    }
    $cutoff = (Get-Date).AddHours(-$MaxTempAgeHours)
    $staleItems = Get-ChildItem -Path $tempPath -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $cutoff }
    if (-not $staleItems) {
        Write-Step "No stale temp items older than $MaxTempAgeHours hours."
        return
    }
    $totalSize = ($staleItems | Measure-Object -Property Length -Sum -ErrorAction SilentlyContinue).Sum
    if ($DryRun) {
        Write-Step "[DryRun] Would delete $($staleItems.Count) stale temp item(s) (~$([math]::Round(($totalSize / 1MB), 1)) MB)."
    } else {
        foreach ($item in $staleItems) {
            Remove-Item -Path $item.FullName -Recurse -Force -ErrorAction SilentlyContinue
        }
        Write-Step "Deleted $($staleItems.Count) stale temp item(s) (~$([math]::Round(($totalSize / 1MB), 1)) MB)."
    }
}

function Test-RunnerCacheIntegrity {
    $cacheDir = "C:\actions-runner\cache\bf-monitor"
    $markerPath = Join-Path $cacheDir "env.marker"
    $venvPath = Join-Path $cacheDir "venv"
    $markerExists = Test-Path $markerPath
    $venvExists = Test-Path $venvPath
    if ($markerExists -and $venvExists) {
        Write-Step "Runner cache OK: env.marker and venv both present at $cacheDir (not touched)."
    } elseif (-not $markerExists -and -not $venvExists) {
        Write-Step "Runner cache empty at $cacheDir (expected on a fresh runner; next run will rebuild it)."
    } else {
        Write-Warning "[runner_cleanup] Runner cache at $cacheDir is inconsistent: env.marker present=$markerExists, venv present=$venvExists. Not touching it -- the next monitor.yml run's fingerprint check will fall back to a full rebuild."
    }
}

Write-Step "Starting cleanup (DryRun=$($DryRun.IsPresent))..."
Stop-OrphanedBrowserProcesses
Clear-StaleTempFiles
Test-RunnerCacheIntegrity
Write-Step "Cleanup complete."
exit $EXIT_OK
