"""Import edges from Python source, so `blast_radius` works without being fed.

`tokens.blast_radius` takes plain `(src, dst)` tuples and asks "who depends on
what I changed". That design is right — it works over the knowledge graph or a
hand-written list — but it left every caller to produce the edges, and the
recorded gap said as much: nothing in the kit built them.

This builds them for Python with the standard library's own parser. Tree-sitter is
not needed for Python and adding a dependency to reach parity with `ast` would be
a cost with no return.

WHAT AN IMPORT GRAPH IS NOT

It is not a call graph, and the difference cuts both ways. Saying so is the point,
because a radius that is quietly wrong in one direction is worse than one that is
declared approximate.

  * **Over-approximates.** `b` importing `a` does not mean `b` touches the symbol
    that changed in `a`. A review scoped by this will look at modules that turn
    out to be unaffected.
  * **Under-approximates.** A dependency created by `importlib.import_module`, a
    plugin registry, a string in a config, an entry point, or a template is
    invisible to a parser. So is anything reached through a dynamic attribute
    lookup. A radius from this graph is a floor, not a ceiling.

A function-level call graph is deliberately NOT attempted. Resolving `x.run()` to
a definition needs type information that Python does not carry, and the honest
options are a graph that is right or a graph that looks right. `report["note"]`
carries this to every caller rather than leaving it in a docstring nobody opens.

UNPARSEABLE FILES ARE REPORTED, NEVER SKIPPED QUIETLY

A file that fails to parse contributes no edges, which makes the radius look
smaller. That is the failure mode this module has to avoid, so every skipped file
appears in `report["unreadable"]` with its reason, and `report["coverage"]` states
what fraction of discovered files were actually parsed.
"""

from __future__ import annotations

import ast
import os
from typing import Iterable, Sequence

#: Directories never walked. Build outputs and vendored trees produce edges that
#: describe someone else's code, and `.dobby/state` holds captured output.
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".venv", "venv", "env", "node_modules", "build",
    "dist", "site-packages", ".eggs", ".idea", ".vscode",
})


def module_name(path: str, root: str) -> str:
    """Dotted module name for `path` relative to `root`.

    `pkg/__init__.py` becomes `pkg`, not `pkg.__init__`, because that is the name
    every importer writes.

    A RELATIVE path is resolved against `root`, not against the process's cwd.
    `os.path.abspath` uses the cwd, so `module_name("pkg/mod.py", other_root)`
    produced names like `............Downloads.project.pkg.mod` — and a name that
    matches no module yields an EMPTY radius, which reads as "nothing depends on
    this change". Wrong in the one direction a review must not be misled in. It
    only worked at all because the cwd happened to equal the root.
    """
    root_abs = os.path.abspath(root)
    path_abs = (path if os.path.isabs(path)
                else os.path.join(root_abs, path))
    try:
        rel = os.path.relpath(os.path.abspath(path_abs), root_abs)
    except ValueError:
        # Different Windows volumes. That is the plainest possible case of
        # "outside the root", and the branch below already knows what to do
        # with one -- it just never got there, because `relpath` raised first.
        # Returns what that branch returns, so a caller sees one answer for one
        # condition.
        return ""
    rel = rel.replace(os.sep, "/")
    if rel.startswith("../"):
        # Outside the root: there is no module name for it, and inventing one
        # would put a name in the graph that no importer can ever reference.
        return ""
    if rel.endswith(".py"):
        rel = rel[:-3]
    if rel.endswith("/__init__"):
        rel = rel[: -len("/__init__")]
    return rel.replace("/", ".")


def discover(root: str) -> list[str]:
    """Every .py file under `root`, skipping build output and vendored tooling.

    In a HOST the installed engine is not the project. Graphing it reported 94
    modules and 190 edges of dobby's own code for a project whose entire content
    was one JPEG — technically true, since those files are in the tree, and useless
    as an answer to "who depends on what I changed". In the KIT they are the
    product and are included. `core.scan_exclusions` is the single predicate.
    """
    from .core import scan_exclusions

    skips = set(SKIP_DIRS) | set(scan_exclusions(root))
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skips]
        for name in filenames:
            if name.endswith(".py"):
                found.append(os.path.join(dirpath, name))
    return sorted(found)


def _resolve_relative(importer: str, is_package: bool, level: int,
                      module: str | None) -> str:
    """Absolute module name for a relative import.

    `level` is the count of leading dots, and the base it counts from depends on
    whether the importing file is a package's `__init__.py`:

        in pkg/sub/mod.py   `from . import x`  ->  pkg.sub.x
        in pkg/sub/__init__.py  `from . import x`  ->  pkg.sub.x

    Both land in the same place, but from different starting names — `pkg.sub.mod`
    versus `pkg.sub` — so one dot strips a component in the first case and none in
    the second. `module_name` collapses `__init__`, which destroys exactly the
    distinction this needs, hence the explicit flag.
    """
    parts = importer.split(".") if importer else []
    package = parts if is_package else parts[:-1]
    # Each dot beyond the first walks one level further up.
    keep = len(package) - (level - 1)
    base = package[:keep] if keep > 0 else []
    tail = [p for p in (module or "").split(".") if p]
    return ".".join([*base, *tail])


def _imported_names(tree: ast.AST, importer: str, is_package: bool) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                target = _resolve_relative(importer, is_package, node.level,
                                           node.module)
            else:
                target = node.module or ""
            if not target:
                continue
            out.add(target)
            # `from pkg import mod` names a module, not an attribute, when
            # `pkg/mod.py` exists. Both spellings are recorded and the caller's
            # internal-module filter decides which survive.
            for alias in node.names:
                out.add(f"{target}.{alias.name}")
    return {name for name in out if name}


def import_edges(root: str, *, internal_only: bool = True
                 ) -> tuple[list[tuple[str, str]], dict]:
    """`(edges, report)` where each edge is `(importer, imported)`.

    Direction matches `blast_radius`, which walks edges BACKWARD from what
    changed to find its dependents.

    `internal_only=True` keeps only edges whose target is a module that exists
    under `root`. An edge to `json` or `numpy` is true and useless here: nothing
    in the repository changes them, so they can never be the origin of a blast
    radius, and including them would inflate the graph without adding an answer.
    """
    files = discover(root)
    names = {module_name(path, root): path for path in files}
    edges: set[tuple[str, str]] = set()
    unreadable: list[dict] = []
    parsed = 0

    for path in files:
        importer = module_name(path, root)
        try:
            with open(path, "rb") as handle:
                source = handle.read()
            tree = ast.parse(source, filename=path)
        except (SyntaxError, ValueError) as exc:
            # Reported, not skipped: a file that contributes no edges makes the
            # radius look smaller than it is.
            unreadable.append({"path": path.replace(os.sep, "/"),
                               "reason": f"{type(exc).__name__}: {exc}"})
            continue
        except OSError as exc:
            unreadable.append({"path": path.replace(os.sep, "/"),
                               "reason": f"unreadable: {exc}"})
            continue
        parsed += 1
        is_package = os.path.basename(path) == "__init__.py"

        for target in _imported_names(tree, importer, is_package):
            if internal_only:
                # Longest matching internal prefix: `from dobby.core.kg import X`
                # is an edge to `dobby.core.kg`, not to `dobby`.
                candidate = target
                while candidate and candidate not in names:
                    candidate, _, _ = candidate.rpartition(".")
                if not candidate or candidate == importer:
                    continue
                edges.add((importer, candidate))
            elif target != importer:
                edges.add((importer, target))

    report = {
        "root": os.path.abspath(root).replace(os.sep, "/"),
        "files_found": len(files),
        "files_parsed": parsed,
        "coverage": round(parsed / len(files), 4) if files else 0.0,
        "modules": len(names),
        "edges": len(edges),
        "unreadable": unreadable,
        "internal_only": internal_only,
        "note": ("import edges, not call edges. Over-approximates: importing a "
                 "module does not mean touching the symbol that changed. "
                 "Under-approximates: importlib, plugin registries, entry points "
                 "and config strings are invisible to a parser. A radius from "
                 "this graph is a floor, not a ceiling."),
    }
    return sorted(edges), report


def changed_modules(paths: Iterable[str], root: str) -> list[str]:
    """Map changed FILE paths to module names, dropping non-Python files."""
    out = []
    for path in paths:
        if not path.endswith(".py"):
            continue
        name = module_name(path, root)
        if name and name not in out:
            out.append(name)
    return out


def radius_for(root: str, changed_paths: Sequence[str], *, max_hops: int = 2,
               max_nodes: int = 40) -> dict:
    """Build the graph and answer "who depends on these files" in one call.

    Returns the blast radius with the graph's own report attached, so the caller
    sees the coverage and the approximation caveat next to the answer rather than
    having to know to ask.
    """
    from .tokens import blast_radius

    edges, report = import_edges(root)
    changed = changed_modules(changed_paths, root)
    if not changed:
        return {"changed": [], "graph": report,
                "note": ("no Python files among the changed paths, so an import "
                         "graph cannot say anything about this change")}
    result = blast_radius(edges, changed, max_hops=max_hops,
                          max_nodes=max_nodes)
    result["changed"] = changed
    result["graph"] = report
    return result
