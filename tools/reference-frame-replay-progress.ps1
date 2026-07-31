[CmdletBinding()]
param(
    [int]$ChannelId = 1,
    [string]$ReferenceTime = '2026-07-20 12:34:18',
    [switch]$CompareNeighbors,
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$serverUri = "http://127.0.0.1:$Port"
$scratch = Join-Path ([System.IO.Path]::GetTempPath()) ("vigi-replay-progress-" + [guid]::NewGuid().ToString('N'))
$stdoutLog = Join-Path $scratch 'server-out.log'
$stderrLog = Join-Path $scratch 'server-error.log'
$server = $null
$previousProgressSetting = $env:VIGI_REPLAY_PROGRESS_DIAGNOSTICS
$previousPartialSetting = $env:VIGI_REPLAY_TIMEOUT_DIAGNOSTIC_DIRECTORY

function Write-SafeLine([string]$Message) {
    Write-Output $Message
}

function Stop-LoopbackServer {
    $listener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($null -ne $listener) {
        Stop-Process -Id $listener.OwningProcess -Force
    }
}

function Wait-ForServer {
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            $response = Invoke-WebRequest -Uri "$serverUri/openapi.json" -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) { return }
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    throw 'The loopback server did not become ready.'
}

function Write-SafeProgressSummary {
    Get-Content -LiteralPath $stderrLog -ErrorAction SilentlyContinue |
        Where-Object { $_ -match '^replay\.progress_timeout ' } |
        ForEach-Object {
            if ($_ -match '^(replay\.progress_timeout channel_id=\d+ duration_seconds=\d+ elapsed_ms=\d+ frame=(?:\d+|none) out_time_us=(?:\d+|none) total_size=(?:\d+|none) last_progress_age_ms=(?:\d+|none) media_time_stalled_ms=(?:\d+|none) size_stalled_ms=(?:\d+|none) reached_requested_duration=(?:True|False) progress_end_seen=(?:True|False))$') {
                Write-SafeLine $_
            }
        }
}

try {
    Set-Location $projectRoot
    New-Item -ItemType Directory -Path $scratch | Out-Null
    Stop-LoopbackServer
    $env:VIGI_REPLAY_PROGRESS_DIAGNOSTICS = 'true'
    Remove-Item Env:VIGI_REPLAY_TIMEOUT_DIAGNOSTIC_DIRECTORY -ErrorAction SilentlyContinue
    $server = Start-Process -FilePath 'uv' -WorkingDirectory $projectRoot -PassThru -NoNewWindow `
        -ArgumentList @('run', 'uvicorn', 'vigi_vision.reference_frame_api:create_reference_frame_app_from_environment', '--factory', '--host', '127.0.0.1', '--port', $Port) `
        -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
    Wait-ForServer
    $offsets = if ($CompareNeighbors) { @(-10, 0, 10) } else { @(0) }
    $body = @{ channel_id = $ChannelId; reference_time = $ReferenceTime; source_timezone = 'Asia/Seoul'; offsets_seconds = $offsets } | ConvertTo-Json -Compress
    $response = Invoke-RestMethod -Method Post -Uri "$serverUri/api/v1/reference-frame-candidate-sets" -ContentType 'application/json' -Body $body
    Write-SafeLine "request_status=completed"
    foreach ($candidate in $response.candidates) {
        $safeCode = if ($null -eq $candidate.failure) { 'none' } else { $candidate.failure.code }
        Write-SafeLine "candidate offset_seconds=$($candidate.offset_seconds) status=$($candidate.status) failure_code=$safeCode"
    }
    Write-SafeProgressSummary
    Get-Process -Name ffmpeg,ffprobe -ErrorAction SilentlyContinue | Select-Object Id, ProcessName, StartTime
} catch {
    Write-SafeLine 'diagnostic_status=failed'
    exit 1
} finally {
    if ($null -ne $server -and -not $server.HasExited) { Stop-Process -Id $server.Id -Force }
    if ($null -eq $previousProgressSetting) { Remove-Item Env:VIGI_REPLAY_PROGRESS_DIAGNOSTICS -ErrorAction SilentlyContinue } else { $env:VIGI_REPLAY_PROGRESS_DIAGNOSTICS = $previousProgressSetting }
    if ($null -eq $previousPartialSetting) { Remove-Item Env:VIGI_REPLAY_TIMEOUT_DIAGNOSTIC_DIRECTORY -ErrorAction SilentlyContinue } else { $env:VIGI_REPLAY_TIMEOUT_DIAGNOSTIC_DIRECTORY = $previousPartialSetting }
    Remove-Item -LiteralPath $scratch -Recurse -Force -ErrorAction SilentlyContinue
}
