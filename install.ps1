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

# Preconditions are MEASURED, not assumed - and existence is not a measurement.
#
# `python3` on Windows usually resolves to the Microsoft Store App Installer
# redirector: a stub that prints the word "Python", executes nothing, and is
# found by Get-Command exactly like a real binary. Selecting it makes the
# installer refuse to run on a machine with a working Python 3.11.
#
# So each candidate is asked to COMPUTE something. A stub can echo its own name;
# it cannot return 311.
$py = $null
foreach ($cand in @('python', 'py', 'python3', 'python3.13', 'python3.12',
                    'python3.11', 'python3.10')) {
    $found = Get-Command $cand -ErrorAction SilentlyContinue
    if ($null -eq $found) { continue }
    $probe = & $found.Source -c "import sys;print(sys.version_info[0]*100+sys.version_info[1])" 2>$null
    if ($LASTEXITCODE -ne 0) { continue }
    $n = 0
    if (-not [int]::TryParse(($probe | Out-String).Trim(), [ref]$n)) { continue }
    if ($n -lt 310) { continue }
    $py = $found.Source
    break
}
if ($null -eq $py) {
    Say "no working Python 3.10+ found. Candidates tried: python, py, python3"
    Say "Names that resolve but do not execute (the Windows Store stub at"
    Say "  ~\AppData\Local\Microsoft\WindowsApps\python3.exe) are skipped."
    Die "install a real Python 3.10+ and re-run"
}

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

# Runtime state that must NEVER travel to a host project. Mirrors .gitignore;
# tests/test_install.py asserts the two lists agree, because they drift silently.
#
# Installing from a working tree copies whatever the source repo accumulated.
# Before this exclusion, a real install carried the source machine's audit log,
# session trajectories, and state\sandbox\* - captured stdout of arbitrary
# commands, which is exactly the content that must not move between machines.
$RuntimeState = @('state', 'knowledge/kg.bootstrap.json', 'inventory.json',
                  'memory', 'compression_guideline.json', 'specialization.json')

function CopyDataExcludingState($from, $to) {
    if ($DryRun) { Say "  would copy $from -> $to (runtime state excluded)"; return }
    New-Item -ItemType Directory -Path $to -Force | Out-Null
    $fromFull = (Resolve-Path -LiteralPath $from).Path
    foreach ($file in Get-ChildItem -LiteralPath $from -Recurse -File) {
        $rel = $file.FullName.Substring($fromFull.Length).TrimStart('\', '/')
        $relPosix = $rel -replace '\\', '/'
        $skip = $false
        foreach ($pat in $RuntimeState) {
            if ($relPosix -eq $pat -or $relPosix.StartsWith("$pat/")) {
                $skip = $true; break
            }
        }
        if ($skip) { continue }
        $destFile = Join-Path $to $rel
        New-Item -ItemType Directory -Path (Split-Path $destFile) -Force | Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $destFile -Force
    }
}

Say ""
Say "project data (preserved if it already exists; runtime state never copied):"
foreach ($dir in @('.dobby', 'evals')) {
    $dest = Join-Path $Target $dir
    if (Test-Path -LiteralPath $dest) {
        Say "  $dir\ EXISTS - left untouched (your curated knowledge)"
    } elseif ($dir -eq '.dobby') {
        Say "  $dir\ created from the distribution defaults (state\ excluded)"
        CopyDataExcludingState (Join-Path $src $dir) $dest
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
    # Engine health, not the whole suite - see install.sh for the reasoning.
    $engineTests = @('tests.test_kg', 'tests.test_router', 'tests.test_skills',
                     'tests.test_evaluator', 'tests.test_security',
                     'tests.test_memory', 'tests.test_trajectory')
    & $py -m unittest @engineTests -q 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Say "  PASS engine health (core modules)"
        Say "       full suite: cd $Target; $py -m unittest discover -s tests"
    } else { Say "  FAIL engine health - run: $py -m unittest @engineTests" }

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
