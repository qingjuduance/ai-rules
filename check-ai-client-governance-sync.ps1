[CmdletBinding()]
param(
    [string]$TargetProjectPath = (Get-Location).Path,
    [string]$EmbeddedRepoPath,
    [string]$ConfigPath,
    [int]$FetchIntervalHours = 24,
    [string]$RemoteName = "origin",
    [switch]$ForceFetch,
    [switch]$NoFetch,
    [switch]$FailOnWarning,
    [ValidateSet("text", "json")]
    [string]$OutputFormat = "text"
)

$ErrorActionPreference = "Stop"

$ScriptDir = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($ScriptDir)) {
    $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}

$pythonEntry = Join-Path $ScriptDir "scripts\ai_client_governance.py"
if (-not (Test-Path -LiteralPath $pythonEntry)) {
    Write-Warning "Missing ai-client-governance CLI entry at scripts/ai_client_governance.py"
    if ($FailOnWarning) {
        exit 1
    }
    exit 0
}

$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCommand) {
    $pythonCommand = Get-Command py -ErrorAction SilentlyContinue
}
if (-not $pythonCommand) {
    Write-Warning "Python is not available; cannot run scripts/ai_client_governance.py sync-check"
    if ($FailOnWarning) {
        exit 1
    }
    exit 0
}

$projectPath = (Resolve-Path -LiteralPath $TargetProjectPath).Path
$argsList = @(
    $pythonEntry,
    "sync-check",
    "--target-project-path", $projectPath,
    "--fetch-interval-hours", "$FetchIntervalHours",
    "--remote-name", $RemoteName,
    "--format", $OutputFormat
)

if (-not [string]::IsNullOrWhiteSpace($EmbeddedRepoPath)) {
    $argsList += @("--embedded-repo-path", $EmbeddedRepoPath)
}
if (-not [string]::IsNullOrWhiteSpace($ConfigPath)) {
    $argsList += @("--config-path", $ConfigPath)
}
if ($ForceFetch) {
    $argsList += "--force-fetch"
}
if ($NoFetch) {
    $argsList += "--no-fetch"
}
if ($FailOnWarning) {
    $argsList += "--fail-on-warning"
}

& $pythonCommand.Source @argsList
exit $LASTEXITCODE
