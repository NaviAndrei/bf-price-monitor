<#
.SYNOPSIS
    Emergency fallback execution of the scrape -> analyze -> notify pipeline
    from any machine, for use when the self-hosted runner (PC-A1208) is
    unreachable. See docs/runbooks/RUNNER_OUTAGE.md for the full procedure
    this script is one step of.

.DESCRIPTION
    Preflight-checks Python and uv, verifies the required Telegram secrets
    are set (without ever echoing their values), runs `uv sync --frozen`,
    installs the Playwright Chromium browser if missing, then runs
    scripts/scrape.py, scripts/analyze.py, and scripts/notify.py in that
    order -- the same sequence and dependency chain as
    .github/workflows/monitor.yml's scrape-analyze-notify job. Stops at the
    first failing stage rather than continuing on stale/missing input.

    Does not commit or push data/price_history.json -- that step is manual
    (see the runbook), since it needs your own git identity.

.PARAMETER Help
    Print this usage text and exit 0 without doing anything else.

.PARAMETER DryRun
    Run only the preflight checks (Python version, uv presence, required
    env vars) and report the result. Does not run uv sync, install
    Chromium, or touch the pipeline/data files.

.EXAMPLE
    .\scripts\run_emergency_local.ps1 -DryRun

.EXAMPLE
    .\scripts\run_emergency_local.ps1
#>

param(
    [switch]$Help,
    [switch]$DryRun
)

# Exit codes -- distinct per failure point so a caller (human or a wrapping
# script) can tell which stage failed without parsing output text.
$EXIT_OK = 0
$EXIT_PREFLIGHT_FAILED = 1
$EXIT_MISSING_ENV_VAR = 2
$EXIT_UV_SYNC_FAILED = 3
$EXIT_PLAYWRIGHT_INSTALL_FAILED = 4
$EXIT_SCRAPE_FAILED = 5
$EXIT_ANALYZE_FAILED = 6
$EXIT_NOTIFY_FAILED = 7

if ($Help) {
    Get-Help $PSCommandPath -Full
    exit $EXIT_OK
}

# pyproject.toml's own requires-python floor, not an arbitrarily higher
# number -- this fallback machine only needs to satisfy what the project
# itself declares as its minimum.
$MIN_PYTHON_MAJOR = 3
$MIN_PYTHON_MINOR = 11

# Env vars notify.py reads with os.environ[...] and crashes on immediately
# if unset (scripts/notify.py:501-502) -- hard requirements for this script.
$REQUIRED_ENV_VARS = @("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")

# HF_TOKEN is read via os.environ.get(..., "") in analyze.py and degrades
# to an Ollama call, then to a deterministic rule-only template, if missing
# or if the call fails -- soft requirement, warn only.
$OPTIONAL_ENV_VARS = @("HF_TOKEN")

function Write-Step {
    param([string]$Message)
    Write-Output "[run_emergency_local] $Message"
}

function Test-PythonVersion {
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCmd) {
        Write-Error "[run_emergency_local] python not found on PATH."
        return $false
    }
    $versionOutput = (& python --version) 2>&1
    if ($versionOutput -match "Python (\d+)\.(\d+)") {
        $major = [int]$Matches[1]
        $minor = [int]$Matches[2]
        if ($major -gt $MIN_PYTHON_MAJOR -or ($major -eq $MIN_PYTHON_MAJOR -and $minor -ge $MIN_PYTHON_MINOR)) {
            Write-Step "Python $major.$minor found (>= $MIN_PYTHON_MAJOR.$MIN_PYTHON_MINOR required)."
            return $true
        }
        Write-Error "[run_emergency_local] Python $major.$minor found, but $MIN_PYTHON_MAJOR.$MIN_PYTHON_MINOR+ is required."
        return $false
    }
    Write-Error "[run_emergency_local] Could not parse Python version from: $versionOutput"
    return $false
}

function Test-UvPresence {
    $uvCmd = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uvCmd) {
        Write-Error "[run_emergency_local] uv not found on PATH. Install from https://docs.astral.sh/uv/"
        return $false
    }
    Write-Step "uv found at $($uvCmd.Source)."
    return $true
}

function Test-RequiredEnvVars {
    $allPresent = $true
    foreach ($varName in $REQUIRED_ENV_VARS) {
        $value = [System.Environment]::GetEnvironmentVariable($varName)
        if ([string]::IsNullOrWhiteSpace($value)) {
            Write-Error "[run_emergency_local] Required environment variable not set: $varName"
            $allPresent = $false
        } else {
            Write-Step "$varName is set."
        }
    }
    foreach ($varName in $OPTIONAL_ENV_VARS) {
        $value = [System.Environment]::GetEnvironmentVariable($varName)
        if ([string]::IsNullOrWhiteSpace($value)) {
            Write-Warning "[run_emergency_local] Optional environment variable not set: $varName (analyze.py will fall back to Ollama, then to a rule-only template)"
        } else {
            Write-Step "$varName is set."
        }
    }
    return $allPresent
}

Write-Step "Running preflight checks..."
$preflightOk = (Test-PythonVersion) -and (Test-UvPresence)
if (-not $preflightOk) {
    exit $EXIT_PREFLIGHT_FAILED
}

if (-not (Test-RequiredEnvVars)) {
    exit $EXIT_MISSING_ENV_VAR
}

if ($DryRun) {
    Write-Step "Dry run complete -- preflight checks passed, no pipeline stages were run."
    exit $EXIT_OK
}

Write-Step "Syncing dependencies (uv sync --frozen)..."
uv sync --frozen
if ($LASTEXITCODE -ne 0) {
    Write-Error "[run_emergency_local] uv sync failed (exit $LASTEXITCODE)."
    exit $EXIT_UV_SYNC_FAILED
}

# Only pcgarage/flanco scraping needs a browser (scrape.py's
# fetch_with_browser); a machine that already has the Chromium revision
# uv.lock pins skips the download instead of re-fetching it every run.
Write-Step "Checking Playwright Chromium install..."
uv run playwright install chromium
if ($LASTEXITCODE -ne 0) {
    Write-Error "[run_emergency_local] Playwright Chromium install failed (exit $LASTEXITCODE)."
    exit $EXIT_PLAYWRIGHT_INSTALL_FAILED
}

Write-Step "Running scrape.py..."
uv run python scripts/scrape.py
if ($LASTEXITCODE -ne 0) {
    Write-Error "[run_emergency_local] scrape.py failed (exit $LASTEXITCODE)."
    exit $EXIT_SCRAPE_FAILED
}

Write-Step "Running analyze.py..."
uv run python scripts/analyze.py
if ($LASTEXITCODE -ne 0) {
    Write-Error "[run_emergency_local] analyze.py failed (exit $LASTEXITCODE)."
    exit $EXIT_ANALYZE_FAILED
}

Write-Step "Running notify.py..."
uv run python scripts/notify.py
if ($LASTEXITCODE -ne 0) {
    Write-Error "[run_emergency_local] notify.py failed (exit $LASTEXITCODE)."
    exit $EXIT_NOTIFY_FAILED
}

Write-Step "Emergency pipeline run complete. Commit and push data/price_history.json and data/watchlist.json per the runbook, then ping HEALTHCHECK_URL manually."
exit $EXIT_OK
