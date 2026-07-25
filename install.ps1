<#
.SYNOPSIS
  Install dobby into a host project on Windows. Idempotent; re-running is the
  supported upgrade path.

.DESCRIPTION
  Mirrors install.sh exactly, including the one rule that matters most:

    ENGINE  (dobby\, mcp\, tests\)  is overwritten on upgrade — it is
                                    repo-agnostic code with no project knowledge.
    DATA    (.dobby\, evals\)       is copied ONLY IF ABSENT — it is the
                                    project's curated knowledge graph, protected
                                    paths, policies, and gold labels. Overwriting
                                    it would silently destroy every session of
                                    curation.
    ENTRY   (AGENTS.md, CLAUDE.md)  is never overwritten; a pointer is appended.

.PARAMETER Target
  Host project directory.

.PARAMETER DryRun
  Show what would change without writing anything.

.EXAMPLE
  .\install.ps1 -Target C:\src\my-project
  .\install.ps1 -Target C:\src\my-project -DryRun
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Target,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$src = $PSScriptRoot

function Say([string]$m) { Write-Host $m }
function Die([string]$m) { Write-Host "error: $m"; exit 1 }

if (-not (Test-Path -LiteralPath $Target -PathType Container)) {
    Die "target is not a directory: $Target"
}
$Target = (Resolve-Path -LiteralPath $Target).Path
if ($Target -eq $src) { Die "target is the dobby repo itself; pick a host project" }

# Preconditions are MEASURED, not assumed. `python3` does not exist on a default
# Windows install, so every candidate name is probed.
$py = $null
foreach ($cand in @('python', 'py', 'python3')) {
    $found = Get-Command $cand -ErrorAction SilentlyContinue
    if ($null -ne $found) { $py = $found.Source; break }
}
if ($null -eq $py) { Die "no python interpreter on PATH (need 3.10+)" }

$verOk = & $py -c "import sys; print(1 if sys.version_info >= (3,10) else 0)"
if ($verOk.Trim() -ne '1') { Die "python 3.10+ required (found: $(& $py --version))" }

& $py -c "import yaml" 2>$null
if ($LASTEXITCODE -ne 0) {
    Say "warning: PyYAML is not importable with $py."
    Say "         dobby optimize / improve-auto need it. Install with:"
    Say "           $py -m pip install PyYAML"
}

Say "dobby -> $Target"
Say ""

function CopyTree($from, $to) {
    if ($DryRun) { Say "  would copy: $from -> $to"; return }
    if (Test-Path -LiteralPath $to) { Remove-Item -LiteralPath $to -Recurse -Force }
    Copy-Item -LiteralPath $from -Destination $to -Recurse -Force
}

Say "engine (overwritten on upgrade):"
foreach ($dir in @('dobby', 'mcp', 'tests')) {
    Say "  $dir\"
    CopyTree (Join-Path $src $dir) (Join-Path $Target $dir)
}

Say ""
Say "project data (preserved if it already exists):"
foreach ($dir in @('.dobby', 'evals')) {
    $dest = Join-Path $Target $dir
    if (Test-Path -LiteralPath $dest) {
        Say "  $dir\ EXISTS - left untouched (your curated knowledge)"
    } else {
        Say "  $dir\ created from the distribution defaults"
        CopyTree (Join-Path $src $dir) $dest
    }
}

Say ""
Say "rules and skills (per-file, existing files kept):"
foreach ($sub in @('.claude\rules', '.claude\skills', 'reports', 'docs')) {
    $p = Join-Path $Target $sub
    if (-not (Test-Path -LiteralPath $p)) {
        if ($DryRun) { Say "  would create: $sub" }
        else { New-Item -ItemType Directory -Path $p -Force | Out-Null }
    }
}
foreach ($f in Get-ChildItem (Join-Path $src '.claude\rules') -Filter *.md) {
    $dest = Join-Path $Target (Join-Path '.claude\rules' $f.Name)
    if (Test-Path -LiteralPath $dest) { Say "  rules\$($f.Name) exists - kept" }
    elseif ($DryRun) { Say "  would copy rules\$($f.Name)" }
    else { Copy-Item -LiteralPath $f.FullName -Destination $dest -Force }
}
foreach ($d in Get-ChildItem (Join-Path $src '.claude\skills') -Directory) {
    $dest = Join-Path $Target (Join-Path '.claude\skills' $d.Name)
    if (Test-Path -LiteralPath $dest) { Say "  skills\$($d.Name) exists - kept" }
    else { CopyTree $d.FullName $dest }
}
foreach ($f in Get-ChildItem (Join-Path $src 'docs') -Filter *.md) {
    $dest = Join-Path $Target (Join-Path 'docs' $f.Name)
    if (Test-Path -LiteralPath $dest) { Say "  docs\$($f.Name) exists - kept" }
    elseif ($DryRun) { Say "  would copy docs\$($f.Name)" }
    else { Copy-Item -LiteralPath $f.FullName -Destination $dest -Force }
}

Say ""
Say "entry points:"
$pointer = "Agent harness: read AGENTS.md in this repository before any task (dobby)."
foreach ($f in @('AGENTS.md', 'CLAUDE.md')) {
    $dest = Join-Path $Target $f
    if (-not (Test-Path -LiteralPath $dest)) {
        Say "  $f created"
        if (-not $DryRun) {
            Copy-Item -LiteralPath (Join-Path $src $f) -Destination $dest -Force
        }
    } elseif (Select-String -LiteralPath $dest -Pattern 'dobby' -Quiet) {
        Say "  $f already references dobby - unchanged"
    } else {
        Say "  $f exists - appending a one-line pointer (your contract wins)"
        if (-not $DryRun) {
            Add-Content -LiteralPath $dest -Value "`n$pointer" -Encoding utf8
        }
    }
}
$designDest = Join-Path $Target 'DESIGN.md'
if (Test-Path -LiteralPath $designDest) { Say "  DESIGN.md exists - kept" }
else {
    Say "  DESIGN.md created (edit the tokens for your product)"
    if (-not $DryRun) {
        Copy-Item -LiteralPath (Join-Path $src 'DESIGN.md') -Destination $designDest -Force
    }
}

Say ""
if ($DryRun) { Say "dry run complete; nothing was written."; exit 0 }

# Verify the install rather than assuming it succeeded.
Say "verifying:"
Push-Location $Target
try {
    & $py -m unittest discover -s tests -q 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Say "  PASS engine tests" }
    else { Say "  FAIL engine tests - run: $py -m unittest discover -s tests" }

    & $py -m dobby.cli init --scan . 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Say "  PASS bootstrap scan" }
    else { Say "  FAIL bootstrap scan - run: $py -m dobby.cli init --scan ." }
} finally {
    Pop-Location
}

Say ""
Say "next:"
Say "  cd $Target"
Say "  $py -m dobby.cli doctor      # what works here, and what does not"
Say "  $py -m dobby.cli context ""your first task"""
Say ""
Say "then curate .dobby\knowledge\kg.json and .dobby\config.json protected_paths"
Say "(see .claude\skills\bootstrap-project\SKILL.md)."
