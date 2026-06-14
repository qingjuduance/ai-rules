[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$TargetProjectPath,
    [switch]$SkipSync,
    [switch]$NoBackup,
    [bool]$AutoRefresh = $true
)

$ErrorActionPreference = "Stop"
$RulesRepoPath = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($RulesRepoPath)) {
    $RulesRepoPath = Split-Path -Parent $MyInvocation.MyCommand.Path
}
$TargetProjectPath = (Resolve-Path -LiteralPath $TargetProjectPath).Path

function Join-ProjectPath {
    param([string]$RelativePath)
    return Join-Path $TargetProjectPath $RelativePath
}

function Join-RulesPath {
    param([string]$RelativePath)
    return Join-Path $RulesRepoPath $RelativePath
}

function Get-RelativeHashLines {
    param([string]$RootPath)

    $root = (Resolve-Path -LiteralPath $RootPath).Path.TrimEnd("\", "/")
    $lines = @()
    Get-ChildItem -LiteralPath $root -Recurse -File -Force |
        Sort-Object FullName |
        ForEach-Object {
            $relative = $_.FullName.Substring($root.Length).TrimStart("\", "/")
            $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
            $lines += "$relative|$hash"
        }
    return $lines
}

function Test-SameContent {
    param([string]$SourcePath, [string]$TargetPath)

    if (-not (Test-Path -LiteralPath $TargetPath)) {
        return $false
    }

    $sourceItem = Get-Item -LiteralPath $SourcePath
    $targetItem = Get-Item -LiteralPath $TargetPath
    if ($sourceItem.PSIsContainer -ne $targetItem.PSIsContainer) {
        return $false
    }

    if (-not $sourceItem.PSIsContainer) {
        $sourceHash = (Get-FileHash -LiteralPath $SourcePath -Algorithm SHA256).Hash
        $targetHash = (Get-FileHash -LiteralPath $TargetPath -Algorithm SHA256).Hash
        return $sourceHash -eq $targetHash
    }

    $sourceLines = @(Get-RelativeHashLines -RootPath $SourcePath)
    $targetLines = @(Get-RelativeHashLines -RootPath $TargetPath)
    if ($sourceLines.Count -ne $targetLines.Count) {
        return $false
    }
    for ($i = 0; $i -lt $sourceLines.Count; $i++) {
        if ($sourceLines[$i] -ne $targetLines[$i]) {
            return $false
        }
    }
    return $true
}

function Backup-Or-RemoveTarget {
    param([string]$TargetPath, [string]$RelativeTarget, [string]$BackupRoot)

    if (-not (Test-Path -LiteralPath $TargetPath)) {
        return
    }

    if (-not $NoBackup) {
        $backup = Join-Path $BackupRoot $RelativeTarget
        New-Item -ItemType Directory -Path (Split-Path -Parent $backup) -Force | Out-Null
        Move-Item -LiteralPath $TargetPath -Destination $backup -Force
    }
    else {
        Remove-Item -LiteralPath $TargetPath -Recurse -Force
    }
}

function Copy-ManagedPath {
    param(
        [string]$SourceRelativePath,
        [string]$TargetRelativePath,
        [string]$BackupRoot
    )

    $source = Join-RulesPath -RelativePath $SourceRelativePath
    $target = Join-ProjectPath -RelativePath $TargetRelativePath
    if (-not (Test-Path -LiteralPath $source)) {
        throw "Missing managed path in rules repo: $SourceRelativePath"
    }

    if (Test-SameContent -SourcePath $source -TargetPath $target) {
        return
    }

    Backup-Or-RemoveTarget -TargetPath $target -RelativeTarget $TargetRelativePath -BackupRoot $BackupRoot
    New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination $target -Recurse -Force
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
            return
        }
        $existing = Get-Content -LiteralPath $target -Raw -Encoding UTF8
        if ($existing.TrimEnd() -eq $Content.TrimEnd()) {
            return
        }
        Backup-Or-RemoveTarget -TargetPath $target -RelativeTarget $RelativePath -BackupRoot $BackupRoot
    }

    New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
    $Content | Set-Content -LiteralPath $target -Encoding UTF8
}

function Get-RootAgentsContent {
    $lines = @(
        '# AI Rules Entry',
        '',
        '## Read Order',
        '',
        'This file is a thin entrypoint. Before working in this project, read:',
        '',
        '1. `.codex/rules/common/AGENTS.md`',
        '2. `.codex/rules/project/AGENTS.md`',
        '',
        'Common rules come from the `ai-rules` repository and cover collaboration,',
        'approval, task sizing, Git boundaries, recovery, sub-agent coordination,',
        'corrections, scripts, and rule sync.',
        '',
        'Project rules belong to this project and cover local directories, business',
        'context, documentation, source snapshots, deliverables, runtime behavior,',
        'and maintenance requirements.',
        '',
        '## Boundaries',
        '',
        '- Do not write project-specific rules back to the common `ai-rules` repo.',
        '- Update common rules only through `.codex/rules/common/`.',
        '- Update project rules only through `.codex/rules/project/`, following',
        '  approval, task tracking, validation, and Git boundaries.',
        '- If common and project rules conflict, follow system/developer rules first,',
        '  then the more specific project rule, without weakening common safety limits.'
    )
    return ($lines -join [Environment]::NewLine)
}

function Get-ProjectRulesPlaceholder {
    $lines = @(
        '# Project-Specific Rules',
        '',
        '## Scope',
        '',
        '- This file records only this project''s specific rules.',
        '- Common AI collaboration rules live in `.codex/rules/common/AGENTS.md`.',
        '- Do not write project-specific rules back to the common `ai-rules` repo.',
        '- Add local directory, business, documentation, source snapshot, runtime,',
        '  deliverable, and maintenance requirements here or in nearby Markdown files.'
    )
    return ($lines -join [Environment]::NewLine)
}

if (-not $SkipSync) {
    $checkScript = Join-Path $RulesRepoPath "check-ai-rules-sync.ps1"
    & powershell -ExecutionPolicy Bypass -File $checkScript -RulesRepoPath $RulesRepoPath -TargetProjectPath $TargetProjectPath -NoInstallRefresh
    if ($LASTEXITCODE -ne 0) {
        throw "AI rules pre-install sync failed with exit code $LASTEXITCODE"
    }
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupRoot = Join-Path $TargetProjectPath ".codex\ai-rules-backups\$timestamp"

$managedPaths = @(
    [ordered]@{ source = "AGENTS.md"; target = ".codex\rules\common\AGENTS.md"; type = "file" },
    [ordered]@{ source = ".codex\skills\agents-rule-maintainer"; target = ".codex\skills\agents-rule-maintainer"; type = "directory" },
    [ordered]@{ source = ".codex\skills\self-correction-planner"; target = ".codex\skills\self-correction-planner"; type = "directory" },
    [ordered]@{ source = "scripts\agent_comm.py"; target = "scripts\agent_comm.py"; type = "file" },
    [ordered]@{ source = "scripts\agent_group_status.py"; target = "scripts\agent_group_status.py"; type = "file" },
    [ordered]@{ source = "scripts\scan_corrections.py"; target = "scripts\scan_corrections.py"; type = "file" },
    [ordered]@{ source = "check-ai-rules-sync.ps1"; target = "check-ai-rules-sync.ps1"; type = "file" }
)

foreach ($path in $managedPaths) {
    Copy-ManagedPath -SourceRelativePath $path["source"] -TargetRelativePath $path["target"] -BackupRoot $backupRoot
}

Write-GeneratedFile -RelativePath "AGENTS.md" -Content (Get-RootAgentsContent) -BackupRoot $backupRoot
Write-GeneratedFile -RelativePath ".codex\rules\project\AGENTS.md" -Content (Get-ProjectRulesPlaceholder) -BackupRoot $backupRoot -OnlyIfMissing

$configDir = Join-Path $TargetProjectPath ".codex"
New-Item -ItemType Directory -Path $configDir -Force | Out-Null
$config = [ordered]@{
    rulesRepoPath = $RulesRepoPath
    installedAt = (Get-Date).ToUniversalTime().ToString("o")
    autoRefresh = $AutoRefresh
    rootEntryPath = "AGENTS.md"
    commonRulesPath = ".codex/rules/common/AGENTS.md"
    projectRulesPath = ".codex/rules/project/AGENTS.md"
    managedPaths = $managedPaths
    preservedPaths = @(
        ".codex/rules/project/",
        ".codex/task-tracking/",
        ".codex/pending-tasks/",
        ".codex/agent-comm/",
        ".codex/agent-groups/"
    )
}
$config | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $configDir "ai-rules-config.json") -Encoding UTF8

Write-Host "AI common rules installed into $TargetProjectPath"
Write-Host "Common rules: .codex/rules/common/AGENTS.md"
Write-Host "Project rules: .codex/rules/project/AGENTS.md"
if (-not $NoBackup) {
    Write-Host "Changed managed files, if any, were backed up under $backupRoot"
}
