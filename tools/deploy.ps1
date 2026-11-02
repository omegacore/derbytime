<#
.SYNOPSIS
  Merge a finished worktree branch into main, run the tests, and restart the
  race server between heats.

.DESCRIPTION
  The merge is staged with --no-commit so a failing test run can be undone with
  `git merge --abort`; main only gets a commit once the suite is green. The
  restart is refused while any heat is staged, armed or pending, because a
  restart drops the USB/serial timer connection. Heat state itself lives in
  SQLite and phones queue lane times, so a restart between heats loses nothing.

.EXAMPLE
  tools/deploy.ps1 -Branch shared-heat-queue
  tools/deploy.ps1 -Branch shared-heat-queue -LoadTest -Port 8000
  tools/deploy.ps1 -RestartOnly
  tools/deploy.ps1 -Branch x -DryRun
#>
param(
    [string]$Branch,
    [int]$Port = 8000,
    [switch]$LoadTest,      # also run tools/loadtest.py on a copy of the DB
    [switch]$NoRestart,     # merge + test only
    [switch]$RestartOnly,   # skip merge/test, just restart between heats
    [switch]$Force,         # restart even if heats are on track (not on race day!)
    [switch]$DryRun         # show what would happen; change nothing
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root '.venv/Scripts/python.exe'
$exe = Join-Path $root '.venv/Scripts/derbytime.exe'

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "deploy: $msg" -ForegroundColor Red; exit 1 }
function Invoke-Git { & git @args; if ($LASTEXITCODE) { Fail "git $($args -join ' ') failed" } }

function Get-ActiveHeats {
    # Read-only open, so this is safe while the server has the DB.
    $code = @'
import sqlite3, sys
from derbytime.config import load_settings
db = load_settings().db_path
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
rows = con.execute("select status, count(*) from heat where status in ('staged','armed','pending') group by status").fetchall()
print(", ".join(f"{n} {s}" for s, n in rows))
'@
    $out = $code | & $py -
    if ($LASTEXITCODE) { Fail 'could not read heat state from the database' }
    return "$out".Trim()
}

function Get-ServerRoot([int]$port) {
    # derbytime.exe is a launcher with python children; kill from the top so
    # nothing is left holding the port. 'racemgr' is the server's pre-rename name.
    $conn = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $conn) { return $null }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($conn.OwningProcess)"
    while ($proc) {
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($proc.ParentProcessId)"
        if (-not $parent -or $parent.CommandLine -notmatch 'derbytime|racemgr') { break }
        $proc = $parent
    }
    return $proc
}

function Wait-Server([int]$port) {
    foreach ($i in 1..30) {
        try { Invoke-WebRequest "http://127.0.0.1:$port/api/time" -UseBasicParsing -TimeoutSec 2 | Out-Null; return $true }
        catch { Start-Sleep -Milliseconds 500 }
    }
    return $false
}

# --- merge + test -----------------------------------------------------------
if (-not $RestartOnly) {
    if (-not $Branch) { Fail 'pass -Branch <name> (or -RestartOnly)' }
    if ((git branch --show-current) -ne 'main') { Fail 'run from the main checkout on branch main' }
    if (git status --porcelain --untracked-files=no) { Fail 'main has uncommitted changes; commit or stash them first' }
    git rev-parse --verify --quiet "refs/heads/$Branch" | Out-Null
    if ($LASTEXITCODE) { Fail "no local branch '$Branch'" }

    $wt = (git worktree list --porcelain) -join "`n" -split "`n`n" | Where-Object { $_ -match "(?m)^branch refs/heads/$([regex]::Escape($Branch))$" }
    if ($wt -match 'worktree (.+)') {
        $wtPath = $Matches[1].Trim()
        if (git -C $wtPath status --porcelain --untracked-files=no) { Fail "worktree $wtPath has uncommitted changes; the session isn't finished" }
    }

    Step "commits to merge from $Branch"
    git log --oneline "main..refs/heads/$Branch"
    if ($DryRun) { Step 'dry run: stopping before merge'; }
    else {
        Step "merging $Branch (staged, not committed)"
        & git merge --no-ff --no-commit "refs/heads/$Branch"
        if ($LASTEXITCODE) { & git merge --abort; Fail "merge of $Branch conflicts; rebase it on main in its worktree first" }

        if (-not (Test-Path $exe)) {
            # First deploy after a package rename: the editable install still
            # points at the old module, so tests and the restart need a reinstall.
            Step 'reinstalling the package (editable)'
            & $py -m pip install -q --no-deps -e .
            if ($LASTEXITCODE) { & git merge --abort; Fail 'pip install -e . failed; merge aborted' }
        }

        Step 'running test suite'
        & $py -m pytest -q
        if ($LASTEXITCODE) { & git merge --abort; Fail 'tests failed; merge aborted, main unchanged' }

        if ($LoadTest) {
            Step 'running load test on a copy of the DB'
            & $py tools/loadtest.py local --db racemgr.db
            if ($LASTEXITCODE) { & git merge --abort; Fail 'load test failed; merge aborted, main unchanged' }
        }

        Invoke-Git commit --no-edit
        Step "merged $Branch into main: $(git log -1 --oneline)"
    }
}

# --- restart between heats --------------------------------------------------
if ($NoRestart) { exit 0 }

$active = Get-ActiveHeats
if ($active) {
    if (-not $Force) { Fail "not restarting: heats on track ($active). Re-run with -RestartOnly after the heat." }
    Write-Host "warning: restarting with heats on track ($active)" -ForegroundColor Yellow
}

$server = Get-ServerRoot $Port
if ($DryRun) {
    Step ("dry run: would restart " + $(if ($server) { "PID $($server.ProcessId): $($server.CommandLine)" } else { "(nothing listening on $Port; would start one)" }))
    exit 0
}
if ($server) {
    Step "stopping server PID $($server.ProcessId) on port $Port"
    & taskkill /T /F /PID $server.ProcessId | Out-Null
    foreach ($i in 1..20) { if (-not (Get-ServerRoot $Port)) { break }; Start-Sleep -Milliseconds 250 }
}

New-Item -ItemType Directory -Force (Join-Path $root 'logs') | Out-Null
$log = Join-Path $root "logs/server-$Port"
Step "starting server on port $Port (logs/server-$Port.out.log)"
Start-Process -FilePath $exe -ArgumentList 'serve', '--host', '0.0.0.0', '--port', $Port `
    -WorkingDirectory $root -WindowStyle Hidden `
    -RedirectStandardOutput "$log.out.log" -RedirectStandardError "$log.err.log" | Out-Null
if (-not (Wait-Server $Port)) { Fail "server did not come back on port $Port; see logs/server-$Port.err.log" }
Step "server is up on port $Port"
