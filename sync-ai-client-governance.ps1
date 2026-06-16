[CmdletBinding()]
param(
    [string]$RulesRepoPath = $PSScriptRoot,
    [string]$RemoteName = "origin",
    [string]$CommitMessage,
    [switch]$NoPush
)

$ErrorActionPreference = "Stop"

$ScriptDir = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($ScriptDir)) {
    $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}
if ([string]::IsNullOrWhiteSpace($RulesRepoPath)) {
    $RulesRepoPath = $ScriptDir
}

function Write-State {
    param(
        [string]$Status,
        [string]$Message,
        [string]$LastCommit,
        [bool]$RemotePresent,
        [bool]$PushSucceeded
    )

    $stateDir = Join-Path $RulesRepoPath ".ai-client-governance-sync"
    $statePath = Join-Path $stateDir "state.json"
    New-Item -ItemType Directory -Path $stateDir -Force | Out-Null

    $now = (Get-Date).ToUniversalTime().ToString("o")
    $previous = [pscustomobject]@{}
    if (Test-Path -LiteralPath $statePath) {
        $previous = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
    }

    $data = [ordered]@{
        last_attempt_at = $now
        last_sync_at = if ($Status -eq "success") { $now } else { $previous.last_sync_at }
        last_push_at = if ($PushSucceeded) { $now } else { $previous.last_push_at }
        last_status = $Status
        last_message = $Message
        last_commit = $LastCommit
        remote = $RemoteName
        remote_present = $RemotePresent
    }

    $data | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statePath -Encoding UTF8
}

function Invoke-Git {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $output = & git @Args 2>&1
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $oldPreference
    if ($exitCode -ne 0) {
        throw "git $($Args -join ' ') failed with exit code $exitCode`n$($output -join "`n")"
    }
    if ($output) {
        $output | Write-Host
    }
}

function Get-GitText {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $output = & git @Args 2>$null
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $oldPreference
    if ($exitCode -ne 0) {
        return $null
    }
    return ($output -join "`n").Trim()
}

$RulesRepoPath = (Resolve-Path -LiteralPath $RulesRepoPath).Path
Push-Location $RulesRepoPath
try {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw "git is not available in PATH"
    }

    if (-not (Test-Path -LiteralPath (Join-Path $RulesRepoPath ".git"))) {
        Invoke-Git -Args @("init")
    }

    $branch = Get-GitText -Args @("branch", "--show-current")
    if ([string]::IsNullOrWhiteSpace($branch)) {
        Invoke-Git -Args @("checkout", "-B", "main")
        $branch = "main"
    }

    $status = Get-GitText -Args @("status", "--porcelain")
    if (-not [string]::IsNullOrWhiteSpace($status)) {
        Invoke-Git -Args @("add", "-A")
        if ([string]::IsNullOrWhiteSpace($CommitMessage)) {
            $CommitMessage = "chore: sync AI Client Governance $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
        }
        Invoke-Git -Args @("commit", "-m", $CommitMessage)
    }

    $remoteUrl = Get-GitText -Args @("remote", "get-url", $RemoteName)
    $remotePresent = -not [string]::IsNullOrWhiteSpace($remoteUrl)
    $pushSucceeded = $false

    if ($remotePresent) {
        Invoke-Git -Args @("fetch", $RemoteName)
        $remoteHead = Get-GitText -Args @("ls-remote", "--heads", $RemoteName, $branch)
        if (-not [string]::IsNullOrWhiteSpace($remoteHead)) {
            Invoke-Git -Args @("pull", "--no-rebase", "--no-edit", $RemoteName, $branch)
            $unmerged = Get-GitText -Args @("diff", "--name-only", "--diff-filter=U")
            if (-not [string]::IsNullOrWhiteSpace($unmerged)) {
                throw "merge conflict detected:`n$unmerged"
            }
        }

        if (-not $NoPush) {
            Invoke-Git -Args @("push", "-u", $RemoteName, $branch)
            $pushSucceeded = $true
        }
    }

    $lastCommit = Get-GitText -Args @("rev-parse", "--short", "HEAD")
    $message = if ($remotePresent) { "sync completed with remote $RemoteName" } else { "sync completed locally; no remote configured" }
    Write-State -Status "success" -Message $message -LastCommit $lastCommit -RemotePresent $remotePresent -PushSucceeded $pushSucceeded
    Write-Host $message
}
catch {
    Write-State -Status "failed" -Message $_.Exception.Message -LastCommit (Get-GitText -Args @("rev-parse", "--short", "HEAD")) -RemotePresent $false -PushSucceeded $false
    throw
}
finally {
    Pop-Location
}
