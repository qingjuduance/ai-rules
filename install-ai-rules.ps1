[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "Medium")]
param(
    [Parameter(Mandatory = $true)]
    [string]$TargetProjectPath,
    [string]$RulesRepoPath = $PSScriptRoot,
    [string]$EmbedPath = ".codex\ai-rules",
    [string]$RemoteUrl,
    [ValidateSet("clone", "submodule")]
    [string]$Mode = "submodule",
    [string]$Branch,
    [switch]$PlanOnly,
    [switch]$AdoptExistingGitRepo,
    [switch]$NoBackup,
    [switch]$SkipRootEntry,
    [switch]$ForceRootEntry,
    [switch]$SkipAgentAdapters,
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
        if ($OnlyIfMissing) {
            Write-Host "Keeping existing $RelativePath; generated file was skipped."
            return
        }
        $existing = Get-Content -LiteralPath $target -Raw -Encoding UTF8
        if ($existing.TrimEnd() -eq $Content.TrimEnd()) {
            Write-Host "$RelativePath is already up to date."
            return
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
        '# AI Rules Entry Adapter',
        '',
        '## Read Order',
        '',
        'This file is a thin adapter for AI tools that understand `AGENTS.md`.',
        'Before working in this project, read:',
        '',
        '1. `.codex/ai-rules/AGENTS.md`',
        '2. `.codex/project/rules/project/AGENTS.md`',
        '',
        'If `.codex/ai-rules/` is missing, read `.codex/ai-rules-config.json`,',
        'locate the configured ai-rules repository, embed it at `.codex/ai-rules/`,',
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
        '- `.codex/ai-rules/` is the embedded common rules Git repository.',
        '- Git projects should register it as a submodule so the parent project',
        '  records the exact ai-rules commit.',
        '- Priority order: native project assets > `.codex/project/` specializations > `.codex/ai-rules/` common rules.',
        '- `.codex/project/rules/project/` belongs to this project only.',
        '- Existing project-owned rule adapters, native project skills, and original local rules stay authoritative;',
        '  ai-rules may add missing integration notes but must not silently overwrite them.',
        '- Do not write project-specific rules back to the common ai-rules repo.',
        '- Before each new session, run `.codex/ai-rules/check-ai-rules-sync.ps1`',
        '  or an equivalent wrapper and warn until the embedded repo is synchronized.'
    )
    return ($lines -join [Environment]::NewLine)
}

function Get-AdapterBaseLines {
    param([string]$ToolName)

    return @(
        "# $ToolName AI Rules Adapter",
        '',
        'This file is a thin adapter for the current AI tool. It should not duplicate',
        'the full common rules. Before working in this project, read:',
        '',
        '1. `AGENTS.md` if it exists in the project root.',
        '2. `.codex/ai-rules/AGENTS.md`.',
        '3. `.codex/project/rules/project/AGENTS.md`.',
        '',
        'If `.codex/ai-rules/` is missing, read `.codex/ai-rules-config.json`,',
        'locate the configured ai-rules repository, embed it at `.codex/ai-rules/`,',
        'then restart the read order.',
        '',
        'Run `.codex/ai-rules/check-ai-rules-sync.ps1` or an equivalent wrapper at',
        'the start of a new session. Warn until the embedded ai-rules repository is',
        'synchronized. Do not pull or push automatically.',
        '',
        'Existing project-owned native instructions stay authoritative. Do not write',
        'project-specific rules back to the common ai-rules repository.'
    )
}

function Get-GenericAdapterContent {
    param([string]$ToolName)
    return ((Get-AdapterBaseLines -ToolName $ToolName) -join [Environment]::NewLine)
}

function Get-ClaudeAdapterContent {
    $lines = @(
        '# Claude Code AI Rules Adapter',
        '',
        'This file is a thin adapter for Claude Code. The `@` imports keep Claude',
        'pointed at the shared facts instead of copying long rules here.',
        '',
        '@AGENTS.md',
        '@.codex/ai-rules/AGENTS.md',
        '@.codex/project/rules/project/AGENTS.md',
        '',
        'If an import is missing, read `.codex/ai-rules-config.json`, embed the',
        '`ai-rules` repository at `.codex/ai-rules/`, then restart the read order.',
        'Run `.codex/ai-rules/check-ai-rules-sync.ps1` at the start of a new session.'
    )
    return ($lines -join [Environment]::NewLine)
}

function Get-GeminiAdapterContent {
    $lines = @(
        '# Gemini CLI AI Rules Adapter',
        '',
        'This file is a thin adapter for Gemini CLI. The `@` imports keep Gemini',
        'pointed at the shared facts instead of copying long rules here.',
        '',
        '@AGENTS.md',
        '@.codex/ai-rules/AGENTS.md',
        '@.codex/project/rules/project/AGENTS.md',
        '',
        'If an import is missing, read `.codex/ai-rules-config.json`, embed the',
        '`ai-rules` repository at `.codex/ai-rules/`, then restart the read order.',
        'Run `.codex/ai-rules/check-ai-rules-sync.ps1` at the start of a new session.'
    )
    return ($lines -join [Environment]::NewLine)
}

function Get-CursorAdapterContent {
    $lines = @(
        '---',
        'alwaysApply: true',
        '---',
        '',
        '# Cursor AI Rules Adapter',
        '',
        'This project uses `.codex/ai-rules/` as the shared AI execution framework.',
        'Before changing files, read `AGENTS.md`, `.codex/ai-rules/AGENTS.md`, and',
        '`.codex/project/rules/project/AGENTS.md`. Keep this Cursor rule as a thin',
        'adapter and do not copy long common rules here.'
    )
    return ($lines -join [Environment]::NewLine)
}

function Get-GitHubInstructionsContent {
    $lines = @(
        '---',
        'applyTo: "**"',
        '---',
        '',
        '# AI Rules Instructions',
        '',
        'This repository uses `.codex/ai-rules/` as the shared AI execution framework.',
        'Before working, read `AGENTS.md`, `.codex/ai-rules/AGENTS.md`, and',
        '`.codex/project/rules/project/AGENTS.md`. Keep this file as a thin adapter',
        'for GitHub Copilot instructions.'
    )
    return ($lines -join [Environment]::NewLine)
}

function Get-AgentAdapterFiles {
    return @(
        [pscustomobject]@{ RelativePath = "CLAUDE.md"; Content = (Get-ClaudeAdapterContent) },
        [pscustomobject]@{ RelativePath = "GEMINI.md"; Content = (Get-GeminiAdapterContent) },
        [pscustomobject]@{ RelativePath = ".github\copilot-instructions.md"; Content = (Get-GenericAdapterContent -ToolName "GitHub Copilot") },
        [pscustomobject]@{ RelativePath = ".github\instructions\ai-rules.instructions.md"; Content = (Get-GitHubInstructionsContent) },
        [pscustomobject]@{ RelativePath = ".cursor\rules\ai-rules.mdc"; Content = (Get-CursorAdapterContent) },
        [pscustomobject]@{ RelativePath = ".clinerules\ai-rules.md"; Content = (Get-GenericAdapterContent -ToolName "Cline") },
        [pscustomobject]@{ RelativePath = ".windsurf\rules\ai-rules.md"; Content = (Get-GenericAdapterContent -ToolName "Windsurf") },
        [pscustomobject]@{ RelativePath = ".continue\rules\ai-rules.md"; Content = (Get-GenericAdapterContent -ToolName "Continue") },
        [pscustomobject]@{ RelativePath = ".roo\rules\ai-rules.md"; Content = (Get-GenericAdapterContent -ToolName "Roo Code") },
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
        '- Common AI execution workflow rules live in `.codex/ai-rules/AGENTS.md`.',
        '- Do not write project-specific rules back to the common `ai-rules` repo.',
        '- Add local directory, business, documentation, source snapshot, runtime,',
        '  deliverable, and maintenance requirements here or in nearby Markdown files.'
    )
    return ($lines -join [Environment]::NewLine)
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

Write-Host "AI rules install preflight"
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
        throw "Embed path exists but is not an ai-rules Git repository root: $embedTarget. Move it, remove it manually, or choose another -EmbedPath."
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
    Write-Host "Embedded ai-rules repo already exists: $embedTarget"
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
    Invoke-InstallAction -Description "Add ai-rules as Git submodule at $relativeEmbed" -Action {
        Invoke-Git -WorkingDirectory $TargetProjectPath -GitArgs $gitArgs -GitOptions $submoduleGitOptions
    } | Out-Null
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
    Invoke-InstallAction -Description "Clone ai-rules into $relativeEmbed" -Action {
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
$backupRoot = Join-Path $TargetProjectPath ".codex\ai-rules-backups\$timestamp"

if (-not $SkipRootEntry) {
    if ($ForceRootEntry) {
        Write-GeneratedFile -RelativePath "AGENTS.md" -Content (Get-RootAgentsContent) -BackupRoot $backupRoot
    }
    else {
        Write-GeneratedFile -RelativePath "AGENTS.md" -Content (Get-RootAgentsContent) -BackupRoot $backupRoot -OnlyIfMissing
    }
}
if (-not $SkipAgentAdapters) {
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
    Write-GeneratedFile -RelativePath ".codex\project\rules\project\AGENTS.md" -Content (Get-ProjectRulesPlaceholder) -BackupRoot $backupRoot -OnlyIfMissing
}

$configDir = Join-Path $TargetProjectPath ".codex"
$configSourceRepoUrl = $source
$agentAdapters = @("AGENTS.md") + ((Get-AgentAdapterFiles) | ForEach-Object { $_.RelativePath.Replace("\", "/") })
$config = [ordered]@{
    schema_version = 3
    mode = if ($Mode -eq "submodule") { "git-submodule" } else { "nested-git-clone" }
    distributionMode = "embedded-git-repository"
    installedAt = (Get-Date).ToUniversalTime().ToString("o")
    sourceRepoUrl = $configSourceRepoUrl
    sourceRepoPath = $RulesRepoPath
    embeddedRepoPath = $EmbedPath.Replace("\", "/")
    commonEntry = ".codex/ai-rules/AGENTS.md"
    commonEntrySemantics = "agent-neutral workflow source; AGENTS.md is a compatibility filename"
    projectEntry = ".codex/project/rules/project/AGENTS.md"
    agentEntryAdapters = $agentAdapters
    adapterPolicy = [ordered]@{
        generateMissingAdapters = (-not $SkipAgentAdapters)
        forceAgentAdapters = [bool]$ForceAgentAdapters
        existingAdapterPolicy = if ($ForceAgentAdapters) { "backup-then-rewrite" } else { "preserve-existing" }
        adapterContentPolicy = "thin-read-order-sync-boundary-only"
        canonicalFacts = @(".codex/ai-rules/AGENTS.md", ".codex/project/rules/project/AGENTS.md")
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
        commonRulesSource = ".codex/ai-rules/"
        priorityOrder = @("native-project-assets", "project-specialization", "ai-rules-common")
        nativeProjectAssets = @("AGENTS.md", "CLAUDE.md", "GEMINI.md", "CONVENTIONS.md", ".github/", ".cursor/", ".clinerules/", ".windsurf/", ".continue/", ".roo/", ".codex/skills/")
        nativeProjectWritePolicy = "read-index-report-only unless the user explicitly approves native asset edits"
        projectRulesSource = ".codex/project/rules/project/"
        commonSkillsSource = ".codex/ai-rules/.codex/skills/"
        projectSkillsSource = ".codex/project/skills/"
        rootAgentsPolicy = "project-owned; create-if-missing; rewrite only with -ForceRootEntry"
        agentAdapterPolicy = "project-owned; create missing adapters unless -SkipAgentAdapters; rewrite only with -ForceAgentAdapters"
        skillConflictPolicy = "native project skill wins, then project specialization, then ai-rules common; duplicate names require review"
        parentTracksEmbeddedCommit = ($Mode -eq "submodule")
    }
    installer = [ordered]@{
        script = "install-ai-rules.ps1"
        planOnly = [bool]$PlanOnly
        rootEntry = if ($SkipRootEntry) { "skipped" } elseif ($ForceRootEntry) { "force-generated-with-backup" } else { "create-if-missing-preserve-existing" }
        agentAdapters = if ($SkipAgentAdapters) { "skipped" } elseif ($ForceAgentAdapters) { "force-generated-with-backup" } else { "create-if-missing-preserve-existing" }
        projectPlaceholder = if ($SkipProjectPlaceholder) { "skipped" } else { "create-if-missing" }
        existingGitRepoPolicy = if ($AdoptExistingGitRepo) { "adopt-when-requested" } else { "stop-unless-registered-submodule" }
        postInstallSyncCheck = (-not $SkipSyncCheck)
    }
}
Write-GeneratedFile -RelativePath ".codex\ai-rules-config.json" -Content ($config | ConvertTo-Json -Depth 8) -BackupRoot $backupRoot

if (-not $SkipSyncCheck) {
    $syncScript = Join-Path $embedTarget "check-ai-rules-sync.ps1"
    if ($PlanOnly -or $WhatIfPreference) {
        Write-Host "PLAN: Run post-install sync check with -NoFetch."
    }
    elseif (Test-Path -LiteralPath $syncScript) {
        Invoke-InstallAction -Description "Run post-install ai-rules sync check" -Action {
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
    Write-Host "AI rules install plan completed for $TargetProjectPath"
    Write-Host "No files or Git state were changed."
}
else {
    Write-Host "AI rules embedded into $TargetProjectPath"
    Write-Host "Embedded repo: $EmbedPath"
    Write-Host "Common entry: .codex/ai-rules/AGENTS.md"
    Write-Host "Project entry: .codex/project/rules/project/AGENTS.md"
    if (-not $SkipAgentAdapters) {
        Write-Host "Agent adapters: $($agentAdapters -join ', ')"
    }
    if (-not $NoBackup) {
        Write-Host "Changed generated files, if any, were backed up under $backupRoot"
    }
}
