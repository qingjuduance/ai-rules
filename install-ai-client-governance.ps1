[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "Medium")]
param(
    [Parameter(Mandatory = $true)]
    [string]$TargetProjectPath,
    [string]$RulesRepoPath = $PSScriptRoot,
    [string]$EmbedPath = ".ai-client\ai-client-governance",
    [string]$RemoteUrl,
    [ValidateSet("clone", "submodule", "existing")]
    [string]$Mode = "submodule",
    [string]$Branch,
    [switch]$PlanOnly,
    [switch]$AdoptExistingGitRepo,
    [switch]$NoBackup,
    [switch]$SkipRootEntry,
    [switch]$ForceRootEntry,
    [switch]$InstallAgentAdapters,
    [switch]$ForceAgentAdapters,
    [switch]$SkipProjectPlaceholder,
    [switch]$SkipSyncCheck
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($RulesRepoPath)) {
    $RulesRepoPath = Split-Path -Parent $MyInvocation.MyCommand.Path
}

function Resolve-RequiredPath {
    param([string]$Path)
    return (Resolve-Path -LiteralPath $Path).Path
}

function Join-ProjectPath {
    param([string]$RelativePath)
    return Join-Path $TargetProjectPath $RelativePath
}

function Write-Utf8NoBomFile {
    param([string]$Path, [string]$Content)

    $encoding = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Normalize-RelativePath {
    param([string]$RelativePath)
    return $RelativePath.Replace("\", "/")
}

function Test-ContainsCanonicalAiClientFacts {
    param([string]$Content)
    return (
        $Content.Contains(".ai-client/ai-client-governance/AGENTS.md") -and
        $Content.Contains(".ai-client/project/rules/project/AGENTS.md") -and
        $Content.Contains("client_type") -and
        $Content.Contains("model_id")
    )
}

function Test-LegacyAiClientAdapterContent {
    param([string]$Content)
    $lower = $Content.ToLowerInvariant()
    $mentionsOldLayout = (
        $lower.Contains(".codex/rules/common") -or
        $lower.Contains(".codex/ai-client-governance") -or
        $lower.Contains(".codex/project") -or
        $lower.Contains(".codex/skills")
    )
    $mentionsGovernance = (
        $lower.Contains("ai client governance") -or
        $lower.Contains("ai-client-governance") -or
        $lower.Contains("ai-client governance")
    )
    return ($mentionsOldLayout -and $mentionsGovernance)
}

function Test-GeneratedAiClientAdapterContent {
    param([string]$Content)
    $signals = @(
        "AI Client Governance Entry Adapter",
        "AI Client Governance Adapter",
        "AI Client Governance 入口",
        "This file is a thin adapter",
        "This project uses `.ai-client/ai-client-governance/` as the shared AI execution framework.",
        "ai-client-governance may add missing integration notes"
    )
    foreach ($signal in $signals) {
        if ($Content.Contains($signal)) {
            return $true
        }
    }
    return $false
}

function Test-DedicatedAiClientAdapterPath {
    param([string]$RelativePath)
    $normalized = Normalize-RelativePath -RelativePath $RelativePath
    return (
        $normalized -eq ".github/copilot-instructions.md" -or
        $normalized -eq ".github/instructions/ai-client-governance.instructions.md" -or
        $normalized -eq ".cursor/rules/ai-client-governance.mdc" -or
        $normalized -eq ".clinerules/ai-client-governance.md" -or
        $normalized -eq ".windsurf/rules/ai-client-governance.md" -or
        $normalized -eq ".continue/rules/ai-client-governance.md" -or
        $normalized -eq ".roo/rules/ai-client-governance.md" -or
        $normalized -eq ".trae/rules/ai-client-governance.md" -or
        $normalized -eq ".codebuddy/rules/ai-client-governance/RULE.mdc"
    )
}

function Test-ShouldUpgradeExistingAiClientAdapter {
    param(
        [string]$RelativePath,
        [string]$ExistingContent
    )
    $hasCanonicalFacts = Test-ContainsCanonicalAiClientFacts -Content $ExistingContent
    $isLegacy = Test-LegacyAiClientAdapterContent -Content $ExistingContent
    $isGenerated = Test-GeneratedAiClientAdapterContent -Content $ExistingContent
    $isDedicatedAdapter = Test-DedicatedAiClientAdapterPath -RelativePath $RelativePath

    if ($isLegacy -and ($isDedicatedAdapter -or $isGenerated) -and (-not $hasCanonicalFacts)) {
        return $true
    }
    if ($isDedicatedAdapter -and $isGenerated -and (-not $hasCanonicalFacts)) {
        return $true
    }
    if ($isGenerated -and (-not $hasCanonicalFacts)) {
        return $true
    }
    return $false
}

function Invoke-InstallAction {
    param(
        [string]$Description,
        [scriptblock]$Action
    )

    if ($PlanOnly) {
        Write-Host "PLAN: $Description"
        return $true
    }
    if ($PSCmdlet.ShouldProcess($TargetProjectPath, $Description)) {
        & $Action
        return $true
    }
    Write-Host "Skipped: $Description"
    return $false
}

function Assert-UnderTargetProject {
    param([string]$Path)

    $full = [System.IO.Path]::GetFullPath($Path).TrimEnd("\", "/")
    $root = [System.IO.Path]::GetFullPath($TargetProjectPath).TrimEnd("\", "/")
    $rootWithSeparator = $root + [System.IO.Path]::DirectorySeparatorChar
    if ($full -ne $root -and -not $full.StartsWith($rootWithSeparator, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify a path outside the target project: $Path"
    }
}

function Backup-ExistingFile {
    param([string]$TargetPath, [string]$RelativeTarget, [string]$BackupRoot)

    if (-not (Test-Path -LiteralPath $TargetPath)) {
        return $true
    }
    Assert-UnderTargetProject -Path $TargetPath
    if ($NoBackup) {
        return Invoke-InstallAction -Description "Remove existing $RelativeTarget without backup" -Action {
            Remove-Item -LiteralPath $TargetPath -Force
        }
    }
    $backup = Join-Path $BackupRoot $RelativeTarget
    Assert-UnderTargetProject -Path $backup
    return Invoke-InstallAction -Description "Back up existing $RelativeTarget to $backup" -Action {
        New-Item -ItemType Directory -Path (Split-Path -Parent $backup) -Force | Out-Null
        Move-Item -LiteralPath $TargetPath -Destination $backup -Force
    }
}

function Write-GeneratedFile {
    param(
        [string]$RelativePath,
        [string]$Content,
        [string]$BackupRoot,
        [switch]$OnlyIfMissing
    )

    $target = Join-ProjectPath -RelativePath $RelativePath
    if (Test-Path -LiteralPath $target) {
        $existing = Get-Content -LiteralPath $target -Raw -Encoding UTF8
        if ($existing.TrimEnd() -eq $Content.TrimEnd()) {
            Write-Host "$RelativePath is already up to date."
            return
        }
        if ($OnlyIfMissing) {
            if (-not (Test-ShouldUpgradeExistingAiClientAdapter -RelativePath $RelativePath -ExistingContent $existing)) {
                Write-Host "Keeping existing $RelativePath; generated file was skipped."
                return
            }
            Write-Host "Updating existing $RelativePath because it looks like a legacy or stale ai-client-governance adapter."
        }
        $backupSucceeded = Backup-ExistingFile -TargetPath $target -RelativeTarget $RelativePath -BackupRoot $BackupRoot
        if (-not $backupSucceeded) {
            Write-Warning "Did not update $RelativePath because the existing file was not backed up or removed."
            return
        }
    }

    Assert-UnderTargetProject -Path $target
    Invoke-InstallAction -Description "Write generated $RelativePath" -Action {
        New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
        Write-Utf8NoBomFile -Path $target -Content $Content
    } | Out-Null
}

function Invoke-Git {
    param(
        [string]$WorkingDirectory,
        [string[]]$GitArgs,
        [string[]]$GitOptions = @()
    )

    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $output = & git @GitOptions -C $WorkingDirectory @GitArgs 2>&1
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $oldPreference
    if ($exitCode -ne 0) {
        $displayArgs = @($GitOptions + @("-C", $WorkingDirectory) + $GitArgs)
        throw "git $($displayArgs -join ' ') failed with exit code $exitCode`n$($output -join "`n")"
    }
    if ($output) {
        $output | Write-Host
    }
}

function Invoke-GitText {
    param(
        [string]$WorkingDirectory,
        [string[]]$GitArgs
    )

    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $output = & git -C $WorkingDirectory @GitArgs 2>&1
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $oldPreference
    return [pscustomobject]@{
        ExitCode = $exitCode
        Text = (($output -join "`n").Trim())
    }
}

function Test-GitSubmoduleRegistered {
    param([string]$WorkingDirectory, [string]$RelativePath)

    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $output = & git -C $WorkingDirectory ls-files --stage -- $RelativePath 2>$null
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $oldPreference
    if ($exitCode -ne 0 -or -not $output) {
        return $false
    }
    return (($output -join "`n") -match "^160000\s")
}

function Test-GitWorkTree {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return $false
    }
    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $output = & git -C $Path rev-parse --is-inside-work-tree 2>$null
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $oldPreference
    return $exitCode -eq 0 -and (($output -join "").Trim() -eq "true")
}

function Test-GitRepoRoot {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return $false
    }
    $topLevel = Invoke-GitText -WorkingDirectory $Path -GitArgs @("rev-parse", "--show-toplevel")
    if ($topLevel.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($topLevel.Text)) {
        return $false
    }
    $expected = [System.IO.Path]::GetFullPath($Path).TrimEnd("\", "/")
    $actual = [System.IO.Path]::GetFullPath($topLevel.Text).TrimEnd("\", "/")
    return $expected -eq $actual
}

function Resolve-LocalSourcePath {
    param([string]$Source)

    if ([string]::IsNullOrWhiteSpace($Source)) {
        return $null
    }
    if ($Source -match "^[A-Za-z][A-Za-z0-9+.-]*://" -or
        $Source -match "^[^/\\@]+@[^/\\:]+:.+") {
        return $null
    }
    $candidate = if ([System.IO.Path]::IsPathRooted($Source)) {
        $Source
    }
    else {
        Join-Path $TargetProjectPath $Source
    }
    if (Test-Path -LiteralPath $candidate) {
        return (Resolve-Path -LiteralPath $candidate).Path
    }
    return $null
}

function Warn-If-LocalSourceDirty {
    param([string]$Source)

    $sourcePath = Resolve-LocalSourcePath -Source $Source
    if (-not $sourcePath -or -not (Test-GitWorkTree -Path $sourcePath)) {
        return
    }
    $status = Invoke-GitText -WorkingDirectory $sourcePath -GitArgs @("-c", "core.quotepath=false", "status", "--porcelain")
    if ($status.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($status.Text)) {
        Write-Warning "Rules source has uncommitted changes. Git clone/submodule installs only committed objects; commit the source first, or put the prepared repo at the embed path and use -Mode existing."
    }
}

function Get-DefaultRulesSource {
    if (-not [string]::IsNullOrWhiteSpace($RemoteUrl)) {
        return $RemoteUrl
    }

    $remote = Invoke-GitText -WorkingDirectory $RulesRepoPath -GitArgs @("remote", "get-url", "origin")
    if ($remote.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($remote.Text)) {
        return $remote.Text
    }

    return $RulesRepoPath
}

function Get-DefaultBranch {
    if (-not [string]::IsNullOrWhiteSpace($Branch)) {
        return $Branch
    }

    $currentBranch = Invoke-GitText -WorkingDirectory $RulesRepoPath -GitArgs @("branch", "--show-current")
    if ($currentBranch.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($currentBranch.Text)) {
        return $currentBranch.Text
    }

    return $null
}

function Get-SubmoduleGitOptions {
    param([string]$Source)

    if ([string]::IsNullOrWhiteSpace($Source)) {
        return @()
    }
    if ($Source.StartsWith("file:", [System.StringComparison]::OrdinalIgnoreCase)) {
        return @("-c", "protocol.file.allow=always")
    }
    if ([System.IO.Path]::IsPathRooted($Source)) {
        return @("-c", "protocol.file.allow=always")
    }
    if ($Source.StartsWith(".", [System.StringComparison]::OrdinalIgnoreCase) -or
        $Source.StartsWith("~", [System.StringComparison]::OrdinalIgnoreCase) -or
        $Source.StartsWith("/", [System.StringComparison]::OrdinalIgnoreCase) -or
        $Source.StartsWith("\", [System.StringComparison]::OrdinalIgnoreCase) -or
        $Source.Contains("\")) {
        return @("-c", "protocol.file.allow=always")
    }
    if (-not ($Source -match "^[A-Za-z][A-Za-z0-9+.-]*://") -and
        -not ($Source -match "^[^/\\@]+@[^/\\:]+:.+")) {
        $targetRelative = Join-Path $TargetProjectPath $Source
        if (Test-Path -LiteralPath $targetRelative) {
            return @("-c", "protocol.file.allow=always")
        }
    }
    return @()
}

function Get-RootAgentsContent {
    $lines = @(
        '# AI Client Governance Entry Adapter',
        '',
        '## Read Order',
        '',
        'This file is a thin adapter for AI tools that understand `AGENTS.md`.',
        'Before working in this project, read:',
        '',
        '1. `.ai-client/ai-client-governance/AGENTS.md`',
        '2. `.ai-client/project/rules/project/AGENTS.md`',
        '',
        'If `.ai-client/ai-client-governance/` is missing, read `.ai-client/ai-client-governance-config.json`,',
        'locate the configured ai-client-governance repository, embed it at `.ai-client/ai-client-governance/`,',
        'then restart the read order.',
        '',
        '## Encoding',
        '',
        'On Windows/PowerShell, read rule files with explicit UTF-8. Set',
        '`$OutputEncoding = [System.Text.UTF8Encoding]::new()` and',
        '`[Console]::InputEncoding/OutputEncoding` to UTF-8 in the command scope,',
        'then use `Get-Content -Encoding UTF8` or',
        '`Get-Content -Raw -Encoding UTF8`.',
        '',
        '## Boundaries',
        '',
        '- `.ai-client/ai-client-governance/` is the embedded common rules Git repository.',
        '- Git projects should register it as a submodule so the parent project',
        '  records the exact ai-client-governance commit.',
        '- Priority order: native project assets > `.ai-client/project/` specializations > `.ai-client/ai-client-governance/` common rules.',
        '- `.ai-client/project/rules/project/` belongs to this project only.',
        '- Existing project-owned rule adapters, native project skills, and original local rules stay authoritative;',
        '  ai-client-governance may add missing integration notes but must not silently overwrite them.',
        '- Lifecycle and telemetry records should include `client_type` and `model_id`; use explicit `unknown` when unavailable.',
        '- Do not write project-specific rules back to the common ai-client-governance repo.',
        '- Before each new session, run `.ai-client/ai-client-governance/check-ai-client-governance-sync.ps1`',
        '  or an equivalent wrapper and warn until the embedded repo is synchronized.'
    )
    return ($lines -join [Environment]::NewLine)
}

function Get-AdapterBaseLines {
    param([string]$ToolName)

    return @(
        "# $ToolName AI Client Governance Adapter",
        '',
        'This file is a thin adapter for the current AI tool. It should not duplicate',
        'the full common rules. Before working in this project, read:',
        '',
        '1. `AGENTS.md` if it exists in the project root.',
        '2. `.ai-client/ai-client-governance/AGENTS.md`.',
        '3. `.ai-client/project/rules/project/AGENTS.md`.',
        '',
        'If `.ai-client/ai-client-governance/` is missing, read `.ai-client/ai-client-governance-config.json`,',
        'locate the configured ai-client-governance repository, embed it at `.ai-client/ai-client-governance/`,',
        'then restart the read order.',
        '',
        'Run `.ai-client/ai-client-governance/check-ai-client-governance-sync.ps1` or an equivalent wrapper at',
        'the start of a new session. Warn until the embedded ai-client-governance repository is',
        'synchronized. Do not pull or push automatically.',
        '',
        'When running lifecycle or telemetry commands, record the current client/model identity:',
        "`client_type=$($ToolName.ToLowerInvariant().Replace(' ', '-'))` and `model_id=<current model>`;",
        'if unavailable, use explicit `unknown` values instead of omitting them.',
        '',
        'Existing project-owned native instructions stay authoritative. Do not write',
        'project-specific rules back to the common ai-client-governance repository.'
    )
}

function Get-GenericAdapterContent {
    param([string]$ToolName)
    return ((Get-AdapterBaseLines -ToolName $ToolName) -join [Environment]::NewLine)
}

function Get-ClaudeAdapterContent {
    $lines = @(
        '# Claude Code AI Client Governance Adapter',
        '',
        'This file is a thin adapter for Claude Code. The `@` imports keep Claude',
        'pointed at the shared facts instead of copying long rules here.',
        '',
        '@AGENTS.md',
        '@.ai-client/ai-client-governance/AGENTS.md',
        '@.ai-client/project/rules/project/AGENTS.md',
        '',
        'If an import is missing, read `.ai-client/ai-client-governance-config.json`, embed the',
        '`ai-client-governance` repository at `.ai-client/ai-client-governance/`, then restart the read order.',
        'When running lifecycle or telemetry commands, record `client_type=claude-code` and `model_id=<current model>`; use `unknown` if unavailable.',
        'Run `.ai-client/ai-client-governance/check-ai-client-governance-sync.ps1` at the start of a new session.'
    )
    return ($lines -join [Environment]::NewLine)
}

function Get-GeminiAdapterContent {
    $lines = @(
        '# Gemini CLI AI Client Governance Adapter',
        '',
        'This file is a thin adapter for Gemini CLI. The `@` imports keep Gemini',
        'pointed at the shared facts instead of copying long rules here.',
        '',
        '@AGENTS.md',
        '@.ai-client/ai-client-governance/AGENTS.md',
        '@.ai-client/project/rules/project/AGENTS.md',
        '',
        'If an import is missing, read `.ai-client/ai-client-governance-config.json`, embed the',
        '`ai-client-governance` repository at `.ai-client/ai-client-governance/`, then restart the read order.',
        'When running lifecycle or telemetry commands, record `client_type=gemini-cli` and `model_id=<current model>`; use `unknown` if unavailable.',
        'Run `.ai-client/ai-client-governance/check-ai-client-governance-sync.ps1` at the start of a new session.'
    )
    return ($lines -join [Environment]::NewLine)
}

function Get-CursorAdapterContent {
    $lines = @(
        '---',
        'alwaysApply: true',
        '---',
        '',
        '# Cursor AI Client Governance Adapter',
        '',
        'This project uses `.ai-client/ai-client-governance/` as the shared AI execution framework.',
        'Before changing files, read `AGENTS.md`, `.ai-client/ai-client-governance/AGENTS.md`, and',
        '`.ai-client/project/rules/project/AGENTS.md`. Keep this Cursor rule as a thin',
        'adapter and do not copy long common rules here. Record `client_type=cursor` and',
        '`model_id=<current model>` in lifecycle or telemetry commands; use `unknown` if unavailable.'
    )
    return ($lines -join [Environment]::NewLine)
}

function Get-CodeBuddyAdapterContent {
    $lines = @(
        '---',
        'description: AI Client Governance thin adapter for CodeBuddy',
        'alwaysApply: true',
        'enabled: true',
        '---',
        '',
        '# CodeBuddy AI Client Governance Adapter',
        '',
        'This file is a thin adapter for CodeBuddy. It should not duplicate',
        'the full common rules. Before working in this project, read:',
        '',
        '1. `AGENTS.md` if it exists in the project root (CodeBuddy auto-loads it when `CODEBUDDY.md` is absent).',
        '2. `.ai-client/ai-client-governance/AGENTS.md`.',
        '3. `.ai-client/project/rules/project/AGENTS.md`.',
        '',
        'If `.ai-client/ai-client-governance/` is missing, read `.ai-client/ai-client-governance-config.json`,',
        'locate the configured ai-client-governance repository, embed it at `.ai-client/ai-client-governance/`,',
        'then restart the read order.',
        '',
        'Run `.ai-client/ai-client-governance/check-ai-client-governance-sync.ps1` or an equivalent wrapper at',
        'the start of a new session. Warn until the embedded ai-client-governance repository is',
        'synchronized. Do not pull or push automatically.',
        '',
        'When running lifecycle or telemetry commands, record the current client/model identity:',
        '`client_type=codebuddy` and `model_id=<current model>`;',
        'if unavailable, use explicit `unknown` values instead of omitting them.',
        '',
        'Existing project-owned native instructions stay authoritative. Do not write',
        'project-specific rules back to the common ai-client-governance repository.'
    )
    return ($lines -join [Environment]::NewLine)
}

function Get-GitHubInstructionsContent {
    $lines = @(
        '---',
        'applyTo: "**"',
        '---',
        '',
        '# AI Client Governance Instructions',
        '',
        'This repository uses `.ai-client/ai-client-governance/` as the shared AI execution framework.',
        'Before working, read `AGENTS.md`, `.ai-client/ai-client-governance/AGENTS.md`, and',
        '`.ai-client/project/rules/project/AGENTS.md`. Keep this file as a thin adapter',
        'for GitHub Copilot instructions. Record `client_type=github-copilot` and',
        '`model_id=<current model>` in lifecycle or telemetry commands; use `unknown` if unavailable.'
    )
    return ($lines -join [Environment]::NewLine)
}

function Get-AgentAdapterFiles {
    return @(
        [pscustomobject]@{ RelativePath = "CLAUDE.md"; Content = (Get-ClaudeAdapterContent) },
        [pscustomobject]@{ RelativePath = "GEMINI.md"; Content = (Get-GeminiAdapterContent) },
        [pscustomobject]@{ RelativePath = ".github\copilot-instructions.md"; Content = (Get-GenericAdapterContent -ToolName "GitHub Copilot") },
        [pscustomobject]@{ RelativePath = ".github\instructions\ai-client-governance.instructions.md"; Content = (Get-GitHubInstructionsContent) },
        [pscustomobject]@{ RelativePath = ".cursor\rules\ai-client-governance.mdc"; Content = (Get-CursorAdapterContent) },
        [pscustomobject]@{ RelativePath = ".clinerules\ai-client-governance.md"; Content = (Get-GenericAdapterContent -ToolName "Cline") },
        [pscustomobject]@{ RelativePath = ".windsurf\rules\ai-client-governance.md"; Content = (Get-GenericAdapterContent -ToolName "Windsurf") },
        [pscustomobject]@{ RelativePath = ".continue\rules\ai-client-governance.md"; Content = (Get-GenericAdapterContent -ToolName "Continue") },
        [pscustomobject]@{ RelativePath = ".roo\rules\ai-client-governance.md"; Content = (Get-GenericAdapterContent -ToolName "Roo Code") },
        [pscustomobject]@{ RelativePath = ".trae\rules\ai-client-governance.md"; Content = (Get-GenericAdapterContent -ToolName "Trae") },
        [pscustomobject]@{ RelativePath = ".codebuddy\rules\ai-client-governance\RULE.mdc"; Content = (Get-CodeBuddyAdapterContent) },
        [pscustomobject]@{ RelativePath = "CONVENTIONS.md"; Content = (Get-GenericAdapterContent -ToolName "Aider") }
    )
}

function Get-ProjectRulesPlaceholder {
    $lines = @(
        '# Project-Specific Rules',
        '',
        '## Scope',
        '',
        '- This file records only this project''s specific rules.',
        '- Common AI execution workflow rules live in `.ai-client/ai-client-governance/AGENTS.md`.',
        '- Do not write project-specific rules back to the common `ai-client-governance` repo.',
        '- Add local directory, business, documentation, source snapshot, runtime,',
        '  deliverable, and maintenance requirements here or in nearby Markdown files.'
    )
    return ($lines -join [Environment]::NewLine)
}

function Get-AiClientGitignoreBlock {
    $lines = @(
        '# BEGIN AI Client Governance generated runtime',
        '# Local runtime state generated by ai-client-governance.',
        '# The host repository tracks stable adapters, project rules/tools/records,',
        '# and the embedded common repository gitlink, not live DBs or task worktrees.',
        '.ai-client/project/cache/',
        '.ai-client/project/tmp/',
        '.ai-client/project/logs/',
        '.ai-client/project/state/',
        '.ai-client/project/.worktree/',
        '.ai-client/project/doc-index/',
        '.ai-client/project/lifecycle/',
        '.ai-client/project/agents/comm/groups/',
        '.ai-client/project/agents/comm/locks.json',
        '.ai-client/project/agents/groups/',
        '# END AI Client Governance generated runtime'
    )
    return ($lines -join [Environment]::NewLine)
}

function Ensure-AiClientGitignore {
    $relativePath = ".gitignore"
    $target = Join-ProjectPath -RelativePath $relativePath
    Assert-UnderTargetProject -Path $target
    $block = Get-AiClientGitignoreBlock
    $existing = ""
    if (Test-Path -LiteralPath $target) {
        $existing = Get-Content -LiteralPath $target -Raw -Encoding UTF8
    }
    $begin = '# BEGIN AI Client Governance generated runtime'
    $end = '# END AI Client Governance generated runtime'
    $pattern = [regex]::Escape($begin) + '[\s\S]*?' + [regex]::Escape($end)
    if ([regex]::IsMatch($existing, $pattern)) {
        $updated = [regex]::Replace($existing, $pattern, { param($m) $block }, 1)
    }
    elseif ([string]::IsNullOrWhiteSpace($existing)) {
        $updated = $block + [Environment]::NewLine
    }
    else {
        $updated = $existing.TrimEnd() + [Environment]::NewLine + [Environment]::NewLine + $block + [Environment]::NewLine
    }
    if ($updated -eq $existing) {
        Write-Host ".gitignore AI Client Governance runtime block is already up to date."
        return
    }
    Invoke-InstallAction -Description "Ensure .gitignore contains AI Client Governance runtime block" -Action {
        New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
        Write-Utf8NoBomFile -Path $target -Content $updated
    } | Out-Null
}

$TargetProjectPath = Resolve-RequiredPath -Path $TargetProjectPath
$RulesRepoPath = Resolve-RequiredPath -Path $RulesRepoPath

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git is not available in PATH"
}

$embedTarget = if ([System.IO.Path]::IsPathRooted($EmbedPath)) {
    $EmbedPath
}
else {
    Join-ProjectPath -RelativePath $EmbedPath
}
$embedTarget = [System.IO.Path]::GetFullPath($embedTarget)
Assert-UnderTargetProject -Path $embedTarget
$embedParent = Split-Path -Parent $embedTarget

$source = Get-DefaultRulesSource
$effectiveBranch = Get-DefaultBranch
$submoduleGitOptions = Get-SubmoduleGitOptions -Source $source
$relativeEmbed = $EmbedPath.Replace("\", "/")

if ($Mode -ne "existing") {
    Warn-If-LocalSourceDirty -Source $source
}

Write-Host "AI Client Governance install preflight"
Write-Host "- Target project: $TargetProjectPath"
Write-Host "- Rules repo path: $RulesRepoPath"
Write-Host "- Source: $source"
Write-Host "- Mode: $Mode"
Write-Host "- Embed path: $relativeEmbed"
if (-not [string]::IsNullOrWhiteSpace($effectiveBranch)) {
    Write-Host "- Branch: $effectiveBranch"
}
if ($PlanOnly) {
    Write-Host "- Plan only: no files or Git state will be changed."
}

if (Test-Path -LiteralPath $embedTarget) {
    if (-not (Test-GitRepoRoot -Path $embedTarget)) {
        throw "Embed path exists but is not an ai-client-governance Git repository root: $embedTarget. Move it, remove it manually, or choose another -EmbedPath."
    }
    if ($Mode -eq "submodule") {
        if (-not (Test-GitRepoRoot -Path $TargetProjectPath)) {
            throw "Submodule mode requires target project path to be a Git repository root."
        }
        if (-not (Test-GitSubmoduleRegistered -WorkingDirectory $TargetProjectPath -RelativePath $relativeEmbed)) {
            if (-not $AdoptExistingGitRepo) {
                throw "Embed path exists as a Git work tree but is not registered as a submodule: $relativeEmbed. Review it first, then rerun with -AdoptExistingGitRepo to register it."
            }
            $gitArgs = @("submodule", "add", "--force")
            if (-not [string]::IsNullOrWhiteSpace($effectiveBranch)) {
                $gitArgs += @("-b", $effectiveBranch)
            }
            $gitArgs += @($source, $relativeEmbed)
            Invoke-InstallAction -Description "Register existing Git work tree as submodule at $relativeEmbed" -Action {
                Invoke-Git -WorkingDirectory $TargetProjectPath -GitArgs $gitArgs -GitOptions $submoduleGitOptions
            } | Out-Null
        }
        else {
            Write-Host "Submodule already registered: $relativeEmbed"
        }
    }
    Write-Host "Embedded ai-client-governance repo already exists: $embedTarget"
}
elseif ($Mode -eq "submodule") {
    if (-not (Test-GitRepoRoot -Path $TargetProjectPath)) {
        throw "Submodule mode requires target project path to be a Git repository root."
    }
    $targetStatus = Invoke-GitText -WorkingDirectory $TargetProjectPath -GitArgs @("-c", "core.quotepath=false", "status", "--porcelain")
    if ($targetStatus.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($targetStatus.Text)) {
        Write-Warning "Target project already has uncommitted changes. This script will not commit or push; review repository status after install."
    }
    Invoke-InstallAction -Description "Create embed parent directory $embedParent" -Action {
        New-Item -ItemType Directory -Path $embedParent -Force | Out-Null
    } | Out-Null
    $gitArgs = @("submodule", "add")
    if (-not [string]::IsNullOrWhiteSpace($effectiveBranch)) {
        $gitArgs += @("-b", $effectiveBranch)
    }
    $gitArgs += @($source, $relativeEmbed)
    Invoke-InstallAction -Description "Add ai-client-governance as Git submodule at $relativeEmbed" -Action {
        Invoke-Git -WorkingDirectory $TargetProjectPath -GitArgs $gitArgs -GitOptions $submoduleGitOptions
    } | Out-Null
}
elseif ($Mode -eq "existing") {
    throw "Mode existing requires an existing Git repository at $relativeEmbed. Put ai-client-governance there first, or use -Mode clone/-Mode submodule."
}
else {
    Invoke-InstallAction -Description "Create embed parent directory $embedParent" -Action {
        New-Item -ItemType Directory -Path $embedParent -Force | Out-Null
    } | Out-Null
    $gitArgs = @("clone")
    if (-not [string]::IsNullOrWhiteSpace($effectiveBranch)) {
        $gitArgs += @("-b", $effectiveBranch)
    }
    $gitArgs += @($source, $embedTarget)
    Invoke-InstallAction -Description "Clone ai-client-governance into $relativeEmbed" -Action {
        $oldPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $output = & git @gitArgs 2>&1
        $exitCode = $LASTEXITCODE
        $ErrorActionPreference = $oldPreference
        if ($exitCode -ne 0) {
            throw "git $($gitArgs -join ' ') failed with exit code $exitCode`n$($output -join "`n")"
        }
        if ($output) {
            $output | Write-Host
        }
    } | Out-Null
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupRoot = Join-Path $TargetProjectPath ".ai-client\ai-client-governance-backups\$timestamp"
$generateAgentAdapters = [bool]($InstallAgentAdapters -or $ForceAgentAdapters)

Ensure-AiClientGitignore

if (-not $SkipRootEntry) {
    if ($ForceRootEntry) {
        Write-GeneratedFile -RelativePath "AGENTS.md" -Content (Get-RootAgentsContent) -BackupRoot $backupRoot
    }
    else {
        Write-GeneratedFile -RelativePath "AGENTS.md" -Content (Get-RootAgentsContent) -BackupRoot $backupRoot -OnlyIfMissing
    }
}
if ($generateAgentAdapters) {
    foreach ($adapter in Get-AgentAdapterFiles) {
        if ($ForceAgentAdapters) {
            Write-GeneratedFile -RelativePath $adapter.RelativePath -Content $adapter.Content -BackupRoot $backupRoot
        }
        else {
            Write-GeneratedFile -RelativePath $adapter.RelativePath -Content $adapter.Content -BackupRoot $backupRoot -OnlyIfMissing
        }
    }
}
if (-not $SkipProjectPlaceholder) {
    Write-GeneratedFile -RelativePath ".ai-client\project\rules\project\AGENTS.md" -Content (Get-ProjectRulesPlaceholder) -BackupRoot $backupRoot -OnlyIfMissing
}

$configSourceRepoUrl = $source
$availableAgentAdapters = @((Get-AgentAdapterFiles) | ForEach-Object { $_.RelativePath.Replace("\", "/") })
$agentAdapters = @("AGENTS.md")
if ($generateAgentAdapters) {
    $agentAdapters += $availableAgentAdapters
}
$config = [ordered]@{
    schema_version = 4
    mode = if ($Mode -eq "submodule") { "git-submodule" } elseif ($Mode -eq "existing") { "existing-embedded-git-repository" } else { "nested-git-clone" }
    distributionMode = "embedded-git-repository"
    installedAt = (Get-Date).ToUniversalTime().ToString("o")
    sourceRepoUrl = $configSourceRepoUrl
    sourceRepoPath = $RulesRepoPath
    embeddedRepoPath = $EmbedPath.Replace("\", "/")
    commonEntry = ".ai-client/ai-client-governance/AGENTS.md"
    commonEntrySemantics = "agent-neutral workflow source exposed through an adapter filename"
    projectEntry = ".ai-client/project/rules/project/AGENTS.md"
    layout = [ordered]@{
        aiClientRoot = ".ai-client"
        commonRepoName = "ai-client-governance"
        commonRepoPath = ".ai-client/ai-client-governance"
        projectPath = ".ai-client/project"
        configPath = ".ai-client/ai-client-governance-config.json"
    }
    structuredRecords = [ordered]@{
        store = ".ai-client/project/state/aicg.db"
        engine = "sqlite"
        schemaVersion = 1
        markdownPolicy = "Markdown task tracking is a human-readable export or historical audit artifact, not the primary gate input for new tasks."
    }
    fileOwnership = [ordered]@{
        hostTracks = @(
            ".ai-client/ai-client-governance gitlink only",
            ".ai-client/ai-client-governance-config.json",
            ".ai-client/project/rules/",
            ".ai-client/project/skills/",
            ".ai-client/project/tools/",
            ".ai-client/project/records/",
            ".ai-client/project/agents/briefs/"
        )
        hostIgnores = @(
            ".ai-client/project/cache/",
            ".ai-client/project/tmp/",
            ".ai-client/project/logs/",
            ".ai-client/project/state/",
            ".ai-client/project/.worktree/",
            ".ai-client/project/doc-index/",
            ".ai-client/project/lifecycle/",
            ".ai-client/project/agents/comm/groups/",
            ".ai-client/project/agents/comm/locks.json",
            ".ai-client/project/agents/groups/"
        )
        gitignoreManagedBlock = $true
        auditCommand = "python .ai-client/ai-client-governance/scripts/ai_client_governance.py file-ownership audit --root . --strict --record-state"
    }
    oldLayoutPolicy = "The previous .codex governance layout is not supported. Do not write adapters, state, records, tools, skills, or fallbacks there."
    agentEntryAdapters = $agentAdapters
    availableAgentEntryAdapters = $availableAgentAdapters
    adapterPolicy = [ordered]@{
        generateMissingAdapters = $generateAgentAdapters
        installParameter = "InstallAgentAdapters"
        forceAgentAdapters = [bool]$ForceAgentAdapters
        existingAdapterPolicy = if ($ForceAgentAdapters) { "backup-then-rewrite" } elseif ($generateAgentAdapters) { "preserve-native; backup-and-upgrade-legacy-ai-client-generated" } else { "not-generated-by-default" }
        adapterContentPolicy = "thin-read-order-sync-client-model-boundary-only"
        canonicalFacts = @(".ai-client/ai-client-governance/AGENTS.md", ".ai-client/project/rules/project/AGENTS.md")
    }
    syncPolicy = [ordered]@{
        checkEverySession = $true
        fetchIntervalHours = 24
        warnUntilSynced = $true
        autoFetch = $true
        autoPull = $false
        autoPush = $false
        remote = "origin"
    }
    submodule = if ($Mode -eq "submodule") {
        [ordered]@{
            path = $EmbedPath.Replace("\", "/")
            url = $source
            branch = $effectiveBranch
            parentTracksEmbeddedCommit = $true
        }
    } else {
        $null
    }
    boundaries = [ordered]@{
        commonRulesSource = ".ai-client/ai-client-governance/"
        priorityOrder = @("native-project-assets", "project-specialization", "ai-client-governance-common")
        nativeProjectAssets = @("AGENTS.md", "CLAUDE.md", "GEMINI.md", "CONVENTIONS.md", ".github/", ".cursor/", ".clinerules/", ".windsurf/", ".continue/", ".roo/", "skills/")
        nativeProjectWritePolicy = "read-index-report-only unless the user explicitly approves native asset edits"
        projectRulesSource = ".ai-client/project/rules/project/"
        commonSkillsSource = ".ai-client/ai-client-governance/skills/"
        projectSkillsSource = ".ai-client/project/skills/"
        rootAgentsPolicy = "project-owned; create-if-missing; rewrite only with -ForceRootEntry"
        agentAdapterPolicy = "project-owned; only AGENTS.md is generated by default; create missing tool adapters with -InstallAgentAdapters; backup-and-upgrade legacy ai-client generated adapters; rewrite all with -ForceAgentAdapters"
        skillConflictPolicy = "native project skill wins, then project specialization, then ai-client-governance common; duplicate names require review"
        parentTracksEmbeddedCommit = ($Mode -eq "submodule")
    }
    installer = [ordered]@{
        script = "install-ai-client-governance.ps1"
        planOnly = [bool]$PlanOnly
        rootEntry = if ($SkipRootEntry) { "skipped" } elseif ($ForceRootEntry) { "force-generated-with-backup" } else { "create-if-missing-preserve-existing" }
        agentAdapters = if ($ForceAgentAdapters) { "force-generated-with-backup" } elseif ($InstallAgentAdapters) { "create-if-missing-preserve-existing" } else { "skipped-by-default" }
        projectPlaceholder = if ($SkipProjectPlaceholder) { "skipped" } else { "create-if-missing" }
        existingGitRepoPolicy = if ($Mode -eq "existing") { "use-existing-embedded-repo" } elseif ($AdoptExistingGitRepo) { "adopt-when-requested" } else { "stop-unless-registered-submodule" }
        postInstallSyncCheck = (-not $SkipSyncCheck)
    }
}
Write-GeneratedFile -RelativePath ".ai-client\ai-client-governance-config.json" -Content ($config | ConvertTo-Json -Depth 8) -BackupRoot $backupRoot

if (-not $SkipSyncCheck) {
    $syncScript = Join-Path $embedTarget "check-ai-client-governance-sync.ps1"
    if ($PlanOnly -or $WhatIfPreference) {
        Write-Host "PLAN: Run post-install sync check with -NoFetch."
    }
    elseif (Test-Path -LiteralPath $syncScript) {
        Invoke-InstallAction -Description "Run post-install ai-client-governance sync check" -Action {
            & $syncScript -TargetProjectPath $TargetProjectPath -NoFetch
            if (-not $?) {
                throw "Post-install sync check failed."
            }
        } | Out-Null
    }
    else {
        Write-Warning "Post-install sync check skipped because $syncScript was not found."
    }
}

if ($PlanOnly -or $WhatIfPreference) {
    Write-Host "AI Client Governance install plan completed for $TargetProjectPath"
    Write-Host "No files or Git state were changed."
}
else {
    Write-Host "AI Client Governance embedded into $TargetProjectPath"
    Write-Host "Embedded repo: $EmbedPath"
    Write-Host "Common entry: .ai-client/ai-client-governance/AGENTS.md"
    Write-Host "Project entry: .ai-client/project/rules/project/AGENTS.md"
    Write-Host "Gitignore: AI Client Governance runtime block ensured"
    if ($generateAgentAdapters) {
        Write-Host "Agent adapters: $($agentAdapters -join ', ')"
    }
    else {
        Write-Host "Agent adapters: skipped by default. Rerun with -InstallAgentAdapters to create Claude/Gemini/Copilot/Cursor/Trae/CodeBuddy/etc. thin adapters."
    }
    if (-not $NoBackup) {
        Write-Host "Changed generated files, if any, were backed up under $backupRoot"
    }
}
