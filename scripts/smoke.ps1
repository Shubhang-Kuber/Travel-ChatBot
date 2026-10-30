<#
.SYNOPSIS
  WanderChat smoke test for a fresh clone (Windows PowerShell 5.1 compatible).

.DESCRIPTION
  1. checks .env exists (copy .env.example and set JWT_SECRET first; see backend/README.md)
  2. docker compose up --build -d --wait
  3. GET /health -> signup (random email) -> login -> POST /chat -> POST /chat/stream (done event)
  Prints PASS/FAIL per step; exits 1 on the first failure.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\smoke.ps1
  powershell -ExecutionPolicy Bypass -File scripts\smoke.ps1 -SkipUp -BaseUrl http://localhost:8000
#>
param(
    [string]$BaseUrl = "http://localhost:8000",
    [switch]$SkipUp
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$failed = $false

function Pass([string]$step, [string]$info) { Write-Host ("PASS  {0,-28} {1}" -f $step, $info) }

function Fail([string]$step, [string]$info) {
    Write-Host ("FAIL  {0,-28} {1}" -f $step, $info)
    exit 1
}

function Step([string]$name, [scriptblock]$body) {
    try {
        $info = & $body
        Pass $name ([string]$info)
    }
    catch {
        Fail $name $_.Exception.Message
    }
}

# 1. .env
Step ".env present" {
    if (-not (Test-Path (Join-Path $root ".env"))) {
        throw "missing .env: run 'Copy-Item .env.example .env' and set JWT_SECRET (see backend/README.md)"
    }
    ".env found"
}

# 2. stack up (docker writes progress to stderr: run it with Continue, judge by exit code)
if (-not $SkipUp) {
    Step "docker compose up --wait" {
        $prev = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $out = & docker compose up --build -d --wait 2>&1 | ForEach-Object { "$_" }
        $code = $LASTEXITCODE
        $ErrorActionPreference = $prev
        if ($code -ne 0) { throw ("exit $code`n" + (($out | Select-Object -Last 15) -join "`n")) }
        "healthy after {0:N1}s" -f $sw.Elapsed.TotalSeconds
    }
}

$email = "smoke-{0}@example.com" -f ([guid]::NewGuid().ToString("N").Substring(0, 12))
$password = "smoke test password 123"
$creds = @{ email = $email; password = $password } | ConvertTo-Json -Compress
$script:access = $null
$script:threadId = "smoke-" + ([guid]::NewGuid().ToString("N").Substring(0, 8))

Step "GET /health" {
    $h = Invoke-RestMethod -Method Get "$BaseUrl/health" -TimeoutSec 10
    if ($h.status -ne "ok" -or $h.mongo -ne "up") { throw "unexpected body: $($h | ConvertTo-Json -Compress)" }
    "status=ok mongo=up"
}

Step "POST /auth/signup" {
    $u = Invoke-RestMethod -Method Post "$BaseUrl/auth/signup" -ContentType "application/json" -Body $creds -TimeoutSec 15
    if ($u.email -ne $email -or -not $u.id) { throw "unexpected body: $($u | ConvertTo-Json -Compress)" }
    "id=$($u.id)"
}

Step "POST /auth/login" {
    $t = Invoke-RestMethod -Method Post "$BaseUrl/auth/login" -ContentType "application/json" -Body $creds -TimeoutSec 15
    if (-not $t.access_token -or $t.token_type -ne "bearer") { throw "no access token" }
    $script:access = $t.access_token
    "expires_in=$($t.expires_in)"
}

Step "POST /chat" {
    $body = @{ message = "Plan 3 days in Lisbon"; thread_id = $script:threadId } | ConvertTo-Json -Compress
    $r = Invoke-RestMethod -Method Post "$BaseUrl/chat" -ContentType "application/json" -Body $body `
        -Headers @{ Authorization = "Bearer $script:access" } -TimeoutSec 60
    if ($r.thread_id -ne $script:threadId -or -not $r.response) { throw "unexpected body: $($r | ConvertTo-Json -Compress)" }
    $preview = $r.response
    if ($preview.Length -gt 50) { $preview = $preview.Substring(0, 50) + "..." }
    "thread_id=$($r.thread_id) response='$preview'"
}

Step "POST /chat/stream" {
    # Body via a file: PS 5.1 strips inner double quotes from JSON passed to native programs.
    $bodyFile = Join-Path $env:TEMP ("wanderchat-smoke-{0}.json" -f $PID)
    $json = @{ message = "And day 4?"; thread_id = $script:threadId } | ConvertTo-Json -Compress
    Set-Content -Path $bodyFile -Value $json -Encoding ascii
    try {
        $prev = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $raw = & curl.exe -s -N --max-time 60 -X POST "$BaseUrl/chat/stream" `
            -H "Authorization: Bearer $script:access" -H "Content-Type: application/json" --data-binary "@$bodyFile"
        $code = $LASTEXITCODE
        $ErrorActionPreference = $prev
    }
    finally {
        Remove-Item $bodyFile -ErrorAction SilentlyContinue
    }
    if ($code -ne 0) { throw "curl.exe exit $code" }
    $lines = @($raw)
    $tokens = @($lines | Where-Object { $_ -eq "event: token" }).Count
    if (-not ($lines -contains "event: done")) { throw "no 'done' event; last lines: $(($lines | Select-Object -Last 4) -join ' | ')" }
    if ($lines -contains "event: error") { throw "stream ended with an error event" }
    "$tokens token events, then done"
}

Write-Host "SMOKE: ALL PASS"
exit 0
