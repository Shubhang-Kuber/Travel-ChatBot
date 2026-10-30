<#
.SYNOPSIS
  Operational checks against the running compose stack (Windows PowerShell 5.1 compatible).

.DESCRIPTION
  -Check health   "Healthcheck truth": stop mongo -> backend /health must go 503 and the
                  container health must become 'unhealthy'; start mongo -> both recover.
  -Check startup  "Startup ordering": backend started while mongo is down must not wedge:
                  it retries, exits, the restart policy restarts it, and it becomes
                  healthy once mongo is started.

  Prints a timestamped timeline (seconds since the check began) and exits non-zero if a
  phase does not reach its expected state within the time limit.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\ops-checks.ps1 -Check health
#>
param(
    [Parameter(Mandatory = $true)][ValidateSet("health", "startup")][string]$Check,
    [int]$TimeoutSeconds = 180,
    [string]$BaseUrl = "http://localhost:8000"
)

$ErrorActionPreference = "Stop"

# Native tools write progress to stderr; in PS 5.1 that becomes an ErrorRecord, which is
# fatal under "Stop". Run docker with "Continue" and judge success by the exit code only.
function Invoke-Docker {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & docker @args 2>&1 | Out-Null
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($code -ne 0) { throw "docker $args failed (exit $code)" }
}
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$backend = "wanderchat-backend-1"
$clock = [Diagnostics.Stopwatch]::StartNew()

function Now { "{0,6:N1}s" -f $clock.Elapsed.TotalSeconds }

# Write-Host, not Write-Output: functions below return booleans through the pipeline.
function Log([string]$msg) { Write-Host ("[{0}] {1}" -f (Now), $msg) }

function HttpStatus {
    $code = & curl.exe -s -o NUL -w "%{http_code}" --max-time 5 "$BaseUrl/health" 2>$null
    if (-not $code) { return "000" }
    return $code
}

function State {
    $fmt = '{{.State.Status}}|{{.RestartCount}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.State.ExitCode}}'
    $s = & docker inspect -f $fmt $backend 2>$null
    if (-not $s) { return "missing|?|?|?" }
    return $s
}

function Snapshot {
    $parts = (State).Split("|")
    return [pscustomobject]@{ Http = (HttpStatus); Status = $parts[0]; Restarts = $parts[1]; Health = $parts[2]; Exit = $parts[3] }
}

# Poll until $Predicate is true for a snapshot. Logs every change of state.
function WaitFor([string]$label, [scriptblock]$Predicate) {
    $deadline = $clock.Elapsed.TotalSeconds + $TimeoutSeconds
    $last = ""
    while ($clock.Elapsed.TotalSeconds -lt $deadline) {
        $s = Snapshot
        $line = "http=$($s.Http) status=$($s.Status) health=$($s.Health) restarts=$($s.Restarts) exit=$($s.Exit)"
        if ($line -ne $last) { Log $line; $last = $line }
        if (& $Predicate $s) { Log "REACHED: $label"; return $true }
        Start-Sleep -Milliseconds 1000
    }
    Log "TIMEOUT waiting for: $label"
    return $false
}

$ok = $true
if ($Check -eq "health") {
    Log "precondition: stack healthy"
    $ok = $ok -and (WaitFor "backend healthy, /health 200" { param($s) $s.Http -eq "200" -and $s.Health -eq "healthy" })
    Log "ACTION: docker compose stop mongo"
    Invoke-Docker compose stop mongo
    $ok = $ok -and (WaitFor "/health 503" { param($s) $s.Http -eq "503" })
    $ok = $ok -and (WaitFor "container health 'unhealthy'" { param($s) $s.Health -eq "unhealthy" })
    Log "ACTION: docker compose start mongo"
    Invoke-Docker compose start mongo
    $ok = $ok -and (WaitFor "/health 200 again" { param($s) $s.Http -eq "200" })
    $ok = $ok -and (WaitFor "container health 'healthy' again" { param($s) $s.Health -eq "healthy" })
}
else {
    Log "ACTION: docker compose stop backend mongo"
    Invoke-Docker compose stop backend mongo
    $startRestarts = [int]((State).Split("|")[1])
    Log "ACTION: docker start $backend (bypasses compose depends_on: mongo is DOWN)"
    Invoke-Docker start $backend
    $ok = $ok -and (WaitFor "backend exited and was restarted by the restart policy" {
            param($s) [int]$s.Restarts -gt $startRestarts })
    Log "ACTION: docker compose start mongo"
    Invoke-Docker compose start mongo
    $ok = $ok -and (WaitFor "backend healthy, /health 200" { param($s) $s.Http -eq "200" -and $s.Health -eq "healthy" })
}

if ($ok) { Log "RESULT: PASS ($Check)"; exit 0 }
Log "RESULT: FAIL ($Check)"
exit 1
