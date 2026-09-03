"""Model-agnostic agent harness engine.

Engine code is repo-agnostic; all repository-specific knowledge lives under
the data directory (default: .dobby/ at the repo root). See
docs/HARNESS_V2_ARCHITECTURE.md.
"""

#: Re-exported, not declared. This said "2.0.0" while `dobby/__init__.py` said
#: "0.1.0" and `dobby doctor` reported the second -- two numbers for one engine,
#: and nothing imported this one. A version nobody reads is free to drift, and
#: the first thing anybody asks about a bug report is which version produced it.
#:
#: `tests/test_version.py` holds them to being the same object.
from .. import __version__  # noqa: F401

import os

#: Directories the installer COPIES into a host. In a host these are the tool,
#: not the work, and every scanner has to know the difference.
#:
#: Found by installing into two real projects. `dobby init --scan .` runs after the
#: engine lands, so it inventoried the harness: the knowledge graph came back with
#: `area:dobby`, `area:mcp`, `area:tests`, `doc:AGENTS.md` and "114 files scanned,
#: languages: python, markdown" — in a project whose entire content was one JPEG.
#: `dobby graph` reported 94 modules and 190 edges, all of them dobby's own.
#:
#: The visible symptom was worse than the cause: `dobby context "<task>"` returned
#: `"items": []`. That is step 1 of the README's five-minute walkthrough, and on a
#: fresh install it answered nothing, because everything it could retrieve was
#: about the harness and the task was about the project.
HARNESS_DIRS = frozenset({
    "dobby", "mcp", "tests", "evals", "docs", "reports", ".claude", ".dobby",
})

#: Files that exist only in the distribution, never in a host — the installer does
#: not copy them. Their presence is what distinguishes the kit from a project.
_KIT_MARKERS = ("install.sh", ".gitignore")


def data_dir(repo_root: str) -> str:
    return os.path.join(repo_root, ".dobby")


def is_kit(repo_root: str) -> bool:
    """True when `repo_root` is the dobby distribution rather than a host.

    In the kit, `dobby/` and `tests/` ARE the product and scanning them is
    correct. In a host they are vendored tooling and scanning them buries the
    project. One predicate, so a scanner cannot get this right in one place and
    wrong in another.
    """
    return all(os.path.exists(os.path.join(repo_root, marker))
               for marker in _KIT_MARKERS)


def scan_exclusions(repo_root: str) -> frozenset:
    """Directory names a scanner should skip for this root.

    Empty in the kit; the harness footprint in a host.
    """
    return frozenset() if is_kit(repo_root) else HARNESS_DIRS
