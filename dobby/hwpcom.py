"""Edit a legacy `.hwp` in place by driving 한글 itself over COM.

WHY THIS EXISTS

`hwp5.py` reads HWP 5.0 and says, in its own docstring, that it will not write:
the body is compressed records inside a compound file whose sector allocation
would have to be rebuilt, and a half-correct writer corrupts documents in ways
that only surface when 한글 opens them. That refusal still stands.

This module takes the other route. It does not parse the format at all. It asks
the application that owns the format to do the edit, then reads the result back
out of the saved file with `hwp5.py` to check that the edit is really there.
The writer and the verifier are therefore different implementations, which is
the only reason to trust the answer.

WHAT IT COSTS

Windows, an installed 한글, and a registered security module (below). None of
that is portable, and none of it belongs in a test suite. Everything here
degrades to a named refusal when the environment is missing — `available()`
reports what is absent, and every entry point raises `HwpComError` with the
same detail rather than failing somewhere deeper.

THE SECURITY MODULE

한글 refuses file access from automation unless a check module is registered
under `HKCU\\Software\\HNC\\HwpAutomation\\Modules`. Without it `Open()` either
prompts or fails, so unattended editing is impossible. The DLL ships with
`pyhwpx` (PyPI); registering it is a one-line registry write that this module
detects but never performs — installing a DLL into a user's registry is not
something a document utility should do behind their back. `available()` tells
you it is missing and what to set.

WHAT WAS MEASURED, AND ON WHAT

Every behaviour below was established against 한글 2018 (COM `Version` reported
`10, 0, 0, 14454`) while editing one real 15-page manuscript: 40-odd substring
replacements across body text, a title table, and a page header, each verified
by reopening the saved file and re-reading it. The failure modes in the next
section are not hypotheses; each one cost a debugging cycle.

FIVE THINGS THAT DO NOT WORK, AND WHAT IS DONE INSTEAD

1. `AllReplace` returns False and changes nothing, on a build where `RepeatFind`
   finds the same string. So the replacement here is manual: select the span,
   read it back, delete, insert.

2. Retyping a whole paragraph flattens its inline character runs. A paragraph
   that begins with a bold lead-in loses the bold. So edits are substring-precise
   and never touch a character outside the match.

3. 한글's internal character offset can run AHEAD of the offset you computed on
   the paragraph's extracted text, by an amount that grows with how many inline
   runs the paragraph carries: 4 in one measured paragraph, more than 8 in
   another. So the selection is probed across a window and accepted only if what
   comes back is byte-equal to what was asked for. A mismatch is reported, never
   guessed past. The window defaults to 40 because a 28-occurrence replace lost
   2 of them at 8 and none at 40; the probe costs nothing when the first offset
   is already right, which is the common case.

4. Some characters cannot be found through COM text at all. An en dash (U+2013)
   inside `z = 3.49-4.35` silently failed to match twice in a row, in a string
   `hwp5.py` had no trouble finding. `split_at_unmatchable()` exists to break a
   pattern around such characters; `replace()` reports a miss rather than
   pretending the string was absent.

5. A replacement whose NEW text contains its OLD text — any append — makes a
   naive find-from-start loop rediscover its own output forever. The scan here
   advances a per-paragraph cursor past what it just wrote.

And one that is not 한글's fault: Windows PowerShell 5.1 reads a `.ps1` as ANSI,
so a script file carrying Korean literals is mojibake before it runs. The script
below is ASCII-only and every payload reaches it through a UTF-8 file.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile

__all__ = [
    "HwpComError", "available", "page_count", "export",
    "paragraph_shapes", "replace", "split_at_unmatchable",
    "UNMATCHABLE",
]


class HwpComError(RuntimeError):
    """한글 is not drivable here, or it refused an operation."""


# Characters observed to be unfindable through COM text search even though the
# document contains them. Extend only with something that was actually measured.
UNMATCHABLE = "–—−"          # en dash, em dash, minus sign

_REG_PATH = r"HKCU:\Software\HNC\HwpAutomation\Modules"
_TIMEOUT = 900


# --------------------------------------------------------------------------
# The PowerShell side. ASCII ONLY -- see the module docstring. Korean and any
# other non-ASCII payload arrives through -Data (UTF-8) and leaves through
# -Out (UTF-8). Nothing non-ASCII may appear in this string.
# --------------------------------------------------------------------------
_PS = r'''
param(
  [Parameter(Mandatory=$true)][string]$Mode,
  [Parameter(Mandatory=$true)][string]$Path,
  [string]$Data = "",
  [string]$Out  = "",
  [string]$Dest = "",
  [int]$ListId  = 0,
  [int]$Probe   = 8,
  [int]$MaxPara = 2000,
  [switch]$Apply
)
$ErrorActionPreference = "Stop"
$result = @{ ok = $false; mode = $Mode }

function Write-Result($obj) {
  $json = $obj | ConvertTo-Json -Depth 6 -Compress
  [System.IO.File]::WriteAllText($Out, $json, (New-Object System.Text.UTF8Encoding($false)))
}

# SetPos CLAMPS past the last paragraph instead of failing, so a scan bounded by
# "SetPos returned false" never terminates on its own and its tail is the final
# paragraph repeated. MoveListEnd + GetPosBySet gives the real index.
function Get-LastPara($hwp, $list) {
  if (-not $hwp.SetPos($list, 0, 0)) { return -1 }
  $hwp.Run("MoveListEnd") | Out-Null
  try { return [int]$hwp.GetPosBySet().Item("Para") } catch { return -1 }
}

try {
  $h = New-Object -ComObject HWPFrame.HwpObject
  $h.RegisterModule("FilePathCheckDLL","FilePathCheckerModule") | Out-Null
  if (-not $h.Open($Path, "HWP", "forceopen:true")) { throw "Open refused: $Path" }
  $result.pages_before = $h.PageCount

  if ($Mode -eq "pagecount") {
    $result.pages = $h.PageCount
    $result.ok = $true
  }
  elseif ($Mode -eq "export") {
    $fmt = if ($Dest -match '\.pdf$') { "PDF" } elseif ($Dest -match '\.hwpx$') { "HWPX" } else { "HWP" }
    $h.SaveAs($Dest, $fmt, "") | Out-Null
    $result.written = $Dest; $result.format = $fmt; $result.ok = $true
  }
  elseif ($Mode -eq "shapes") {
    $rows = New-Object System.Collections.ArrayList
    $last = Get-LastPara $h $ListId
    if ($last -lt 0) { throw "list $ListId has no paragraphs" }
    if ($last -ge $MaxPara) { $last = $MaxPara - 1 }
    $result.last_paragraph = $last
    $p = 0
    while ($p -le $last) {
      if (-not $h.SetPos($ListId, $p, 0)) { break }
      $h.Run("MoveSelParaEnd") | Out-Null
      $t = ""
      try { $t = $h.GetTextFile("TEXT","saveblock") } catch { $t = "" }
      if ($null -eq $t) { $t = "" }
      $t = (($t -replace "`r","") -replace "`n","")
      $ca = $h.CreateAction("CharShape"); $cs = $ca.CreateSet(); $ca.GetDefault($cs) | Out-Null
      $pa = $h.CreateAction("ParagraphShape"); $ps = $pa.CreateSet(); $pa.GetDefault($ps) | Out-Null
      [void]$rows.Add(@{
        index = $p; text = $t
        face  = [string]$cs.Item("FaceNameHangul")
        height= [string]$cs.Item("Height")
        bold  = [string]$cs.Item("Bold")
        ratio = [string]$cs.Item("RatioHangul")
        align = [string]$ps.Item("AlignType")
        line_spacing = [string]$ps.Item("LineSpacing")
      })
      $h.Run("Cancel") | Out-Null
      $p++
    }
    $result.paragraphs = $rows
    $result.ok = $true
  }
  elseif ($Mode -eq "replace") {
    # Payload: UTF-8, one "old<TAB>new" per line.
    $rows = @()
    foreach ($line in [System.IO.File]::ReadAllLines($Data, [System.Text.Encoding]::UTF8)) {
      if ([string]::IsNullOrWhiteSpace($line)) { continue }
      $f = $line.Split("`t")
      if ($f.Count -ge 2) { $rows += ,@($f[0], $f[1]) }
    }

    # Cache paragraph text once; refresh only what we edit.
    $texts = @{}
    $last = Get-LastPara $h $ListId
    if ($last -lt 0) { throw "list $ListId has no paragraphs" }
    if ($last -ge $MaxPara) { $last = $MaxPara - 1 }
    $result.last_paragraph = $last
    $p = 0
    while ($p -le $last) {
      if (-not $h.SetPos($ListId, $p, 0)) { break }
      $h.Run("MoveSelParaEnd") | Out-Null
      $t = ""
      try { $t = $h.GetTextFile("TEXT","saveblock") } catch { $t = "" }
      if ($null -eq $t) { $t = "" }
      $texts[$p] = (($t -replace "`r","") -replace "`n","")
      $h.Run("Cancel") | Out-Null
      $p++
    }

    $items = New-Object System.Collections.ArrayList
    $applied = 0; $failed = 0
    foreach ($r in $rows) {
      $old = $r[0]; $new = $r[1]; $hits = 0
      foreach ($k in ($texts.Keys | Sort-Object)) {
        $cursor = 0
        while ($true) {
          if ($cursor -ge $texts[$k].Length) { break }
          $netIdx = $texts[$k].IndexOf($old, $cursor)
          if ($netIdx -lt 0) { break }

          $found = $false; $useOff = -1
          $offsets = @(0)
          for ($d = 1; $d -le $Probe; $d++) { $offsets += $d; $offsets += (0 - $d) }
          foreach ($d in $offsets) {
            $try = $netIdx + $d
            if ($try -lt 0) { continue }
            $h.SetPos($ListId, $k, 0) | Out-Null
            $h.SelectText($k, $try, $k, $try + $old.Length) | Out-Null
            $got = ""
            try { $got = $h.GetTextFile("TEXT","saveblock") } catch { $got = "" }
            if ($null -eq $got) { $got = "" }
            $got = (($got -replace "`r","") -replace "`n","")
            if ($got -eq $old) { $found = $true; $useOff = $try; break }
            $h.Run("Cancel") | Out-Null
          }

          if (-not $found) {
            [void]$items.Add(@{ old = $old; paragraph = $k; status = "mismatch" })
            $failed++
            $cursor = $netIdx + $old.Length
            continue
          }

          if ($Apply) {
            $h.Run("Delete") | Out-Null
            $act = $h.CreateAction("InsertText"); $st = $act.CreateSet(); $act.GetDefault($st)
            $st.SetItem("Text", $new)
            $act.Execute($st) | Out-Null
            $h.SetPos($ListId, $k, 0) | Out-Null
            $h.Run("MoveSelParaEnd") | Out-Null
            $nt = ""
            try { $nt = $h.GetTextFile("TEXT","saveblock") } catch { $nt = "" }
            if ($null -eq $nt) { $nt = "" }
            $texts[$k] = (($nt -replace "`r","") -replace "`n","")
            $h.Run("Cancel") | Out-Null
            [void]$items.Add(@{ old = $old; paragraph = $k; offset = $useOff; status = "replaced" })
          } else {
            $h.Run("Cancel") | Out-Null
            [void]$items.Add(@{ old = $old; paragraph = $k; offset = $useOff; status = "verified" })
            # Mask the hit so a dry run cannot rediscover it.
            $mask = ([string][char]1) * $old.Length
            $texts[$k] = $texts[$k].Substring(0, $netIdx) + $mask + $texts[$k].Substring($netIdx + $old.Length)
          }
          $applied++; $hits++
          # Advance past what was written: an append contains its own needle.
          $cursor = $netIdx + $new.Length
        }
      }
      if ($hits -eq 0 -and $failed -eq 0) {
        [void]$items.Add(@{ old = $old; status = "absent" })
      }
    }

    $result.items = $items
    $result.applied = $applied
    $result.failed = $failed
    $result.pages_after = $h.PageCount
    # A partial edit is worse than none, and a save that changed nothing is a
    # lie told by a timestamp. Write only when every item landed and at least
    # one did.
    $result.written = $null
    if ($Apply -and $Dest -ne "") {
      if ($failed -gt 0) {
        $result.why = "nothing was written; $failed occurrence(s) did not read back as asked"
      } elseif ($applied -eq 0) {
        $result.why = "nothing was written because nothing was replaced"
      } else {
        $h.SaveAs($Dest, "HWP", "") | Out-Null
        $result.written = $Dest
      }
    }
    $result.ok = $true
  }
  else { throw "unknown mode $Mode" }

  $h.XHwpDocuments.Item(0).Close($false) | Out-Null
  $h.Quit()
  Write-Result $result
}
catch {
  $result.ok = $false
  $result.error = $_.Exception.Message
  try { $h.Quit() } catch {}
  Write-Result $result
}
'''


# --------------------------------------------------------------------------
# environment
# --------------------------------------------------------------------------
def _powershell() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


def available() -> dict:
    """What is present, what is missing, and what to do about it.

    Returns a dict rather than a bool because "not available" has four causes
    that need four different answers, and collapsing them to False sends the
    caller looking in the wrong place.
    """
    info = {
        "ok": False,
        "platform": platform.system(),
        "powershell": _powershell(),
        "security_module": None,
        "missing": [],
    }
    if info["platform"] != "Windows":
        info["missing"].append("한글 automation is Windows-only (COM)")
        return info
    if not info["powershell"]:
        info["missing"].append("neither powershell nor pwsh is on PATH")
        return info

    probe = (
        "$ErrorActionPreference='SilentlyContinue';"
        f"(Get-ItemProperty -Path '{_REG_PATH}').FilePathCheckerModule"
    )
    try:
        out = subprocess.run(
            [info["powershell"], "-NoProfile", "-NonInteractive", "-Command", probe],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip()
    except Exception:                                    # noqa: BLE001
        out = ""
    info["security_module"] = out or None
    if not out:
        info["missing"].append(
            "FilePathCheckerModule is not registered. 한글 will refuse file "
            f"access from automation. Set the value under {_REG_PATH} to the "
            "path of FilePathCheckerModule.dll (it ships inside the pyhwpx "
            "wheel on PyPI). This module will not write your registry for you."
        )
        return info
    if not os.path.exists(out):
        info["missing"].append(f"registered security module is missing on disk: {out}")
        return info

    info["ok"] = True
    return info


def _require() -> str:
    info = available()
    if not info["ok"]:
        raise HwpComError("; ".join(info["missing"]) or "한글 automation unavailable")
    return info["powershell"]


def _run(mode: str, path: str, **kw) -> dict:
    ps = _require()
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise HwpComError(f"no such file: {path}")

    tmp = tempfile.mkdtemp(prefix="dobby-hwpcom-")
    try:
        script = os.path.join(tmp, "drive.ps1")
        # ASCII by construction; assert it so a future edit cannot smuggle in a
        # Korean literal and produce mojibake under PowerShell 5.1.
        _PS.encode("ascii")
        with open(script, "w", encoding="ascii", newline="\r\n") as fh:
            fh.write(_PS)
        out = os.path.join(tmp, "out.json")

        argv = [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", script, "-Mode", mode, "-Path", path, "-Out", out]
        for flag, key in (("-Data", "data"), ("-Dest", "dest")):
            if kw.get(key):
                argv += [flag, str(kw[key])]
        for flag, key in (("-ListId", "list_id"), ("-Probe", "probe"),
                          ("-MaxPara", "max_paragraphs")):
            if kw.get(key) is not None:
                argv += [flag, str(kw[key])]
        if kw.get("apply"):
            argv.append("-Apply")

        proc = subprocess.run(argv, capture_output=True, text=True, timeout=_TIMEOUT)
        if not os.path.exists(out):
            raise HwpComError(
                f"한글 driver produced no result (exit {proc.returncode}): "
                f"{(proc.stderr or proc.stdout or '').strip()[:400]}")
        with open(out, encoding="utf-8") as fh:
            data = json.load(fh)
        if not data.get("ok"):
            raise HwpComError(data.get("error") or "한글 refused the operation")
        return data
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# operations
# --------------------------------------------------------------------------
def page_count(path: str) -> int:
    """Pages as 한글 lays them out -- the only number a page limit can mean."""
    return int(_run("pagecount", path)["pages"])


def export(path: str, dest: str) -> dict:
    """Save a copy as PDF / HWPX / HWP. Format is taken from `dest`'s suffix."""
    return _run("export", path, dest=os.path.abspath(dest))


def paragraph_shapes(path: str, list_id: int = 0, max_paragraphs: int = 2000) -> list[dict]:
    """Per-paragraph font, size, weight, ratio, alignment and line spacing.

    `list_id` selects the text list: 0 is the body. Title blocks, page headers
    and every table cell live in their own lists, so a document's front matter
    is invisible from list 0 and has to be asked for by number.

    The scan is bounded by the list's real last paragraph, not by walking until
    `SetPos` fails -- it never fails, it clamps, which both pads the result with
    a repeated tail and made an early version of this scan 11x slower than it
    needed to be.
    """
    return _run("shapes", path, list_id=list_id, max_paragraphs=max_paragraphs)["paragraphs"]


def split_at_unmatchable(text: str) -> list[str]:
    """Break a pattern around characters COM text search cannot find.

    Returns the runs between unmatchable characters. A caller that wants to
    edit across one of them replaces each run separately and leaves the
    character itself alone.
    """
    out, cur = [], []
    for ch in text:
        if ch in UNMATCHABLE:
            if cur:
                out.append("".join(cur)); cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def replace(path: str, pairs, out: str | None = None, *, overwrite: bool = False,
            list_id: int = 0, probe: int = 40, apply: bool = True,
            max_paragraphs: int = 2000) -> dict:
    """Replace substrings inside a `.hwp`, verifying each one before writing.

    `pairs` is an iterable of `(old, new)`. Each occurrence is located, selected,
    READ BACK, and only replaced if what came back equals `old` exactly. If any
    occurrence fails that check the document is not saved at all -- a partially
    applied edit is harder to notice than one that refused.

    Pass `apply=False` for a dry run: every occurrence is still located and
    verified, nothing is written.

    Like the HWPX writer in `hwpx.py`, this never writes over the source unless
    told to: give `out`, or pass `overwrite=True`.
    """
    pairs = [(str(a), str(b)) for a, b in pairs]
    if not pairs:
        raise HwpComError("nothing to replace")
    for old, _ in pairs:
        bad = sorted({c for c in old if c in UNMATCHABLE})
        if bad:
            raise HwpComError(
                f"pattern contains {', '.join('U+%04X' % ord(c) for c in bad)}, "
                f"which COM text search does not match. Split the pattern around "
                f"it (see split_at_unmatchable) and replace the runs separately.")

    dest = None
    if apply:
        if out:
            dest = os.path.abspath(out)
        elif overwrite:
            dest = os.path.abspath(path)
        else:
            raise HwpComError("replace needs out=..., or overwrite=True; "
                              "this never writes over the source by default")

    tmp = tempfile.mkdtemp(prefix="dobby-hwpcom-")
    try:
        data = os.path.join(tmp, "pairs.tsv")
        with open(data, "w", encoding="utf-8", newline="\n") as fh:
            for old, new in pairs:
                if "\t" in old or "\t" in new:
                    raise HwpComError("a tab in a pattern would break the payload format")
                fh.write(f"{old}\t{new}\n")
        return _run("replace", path, data=data, dest=dest, apply=apply,
                    list_id=list_id, probe=probe, max_paragraphs=max_paragraphs)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
