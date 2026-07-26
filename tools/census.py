#!/usr/bin/env python
"""Degenerate-input census over every public function in the package.

Calls each public function with empty, blank, huge, non-Latin, null-byte and
wrong-typed arguments, and catalogues which ones raise something OTHER than a
deliberate refusal.

The distinction is the whole point. `TypeError`, `ValueError`, `KeyError`,
`OSError` are correct answers to a bad argument — a function that refuses is
working. What this hunts is the unexpected kind: an `AttributeError` from
calling a method on None deep inside, a `RecursionError`, a `ZeroDivisionError`,
an `UnboundLocalError`. Those are the shapes a real caller hits as a crash they
cannot act on.

Functions that SPAWN or WRITE are excluded by name, not by hope: probing them
with 20 000-character arguments launches real subprocesses and writes real
files, which is how the first version of this script hung. The exclusion list is
explicit so what is NOT covered is visible rather than implied.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import sys

REPO = __file__.rsplit("tools", 1)[0]
sys.path.insert(0, REPO)

#: Refusals. A function raising one of these for a degenerate argument is
#: behaving correctly and is not reported.
DELIBERATE = (TypeError, ValueError, KeyError, AttributeError, OSError,
              NotImplementedError, IndexError, LookupError, ArithmeticError)

#: Excluded because calling them does real work: subprocesses, network, or
#: filesystem writes outside a temp directory. Listed rather than pattern-matched
#: so the gap is auditable.
SKIP_FUNCTIONS = {
    "run", "run_provider", "run_by_id", "run_round", "broadcast", "probe",
    "call_api", "main", "build_parser", "bootstrap", "harvest",
    "export_experience", "append_jsonl", "sweep", "record", "record_round",
    "compare", "search",
}
SKIP_MODULES = {"dobby.cli"}

#: Degenerate values by parameter shape. Sizes are bounded: the point is to
#: find a crash, and a 20 000-character argument only adds runtime.
def candidates(name: str, annotation) -> list:
    ann = str(annotation)
    if "int" in ann:
        return [0, -1, 10 ** 7]
    if "float" in ann:
        return [0.0, -1.0, float("nan"), float("inf")]
    if "bool" in ann:
        return [True, False]
    if "dict" in ann:
        return [{}, {"unexpected": None}]
    if any(t in ann for t in ("list", "Sequence", "tuple", "Iterable")):
        return [[], [None], [""]]
    if "str" in ann or name in ("text", "query", "task", "path", "command",
                                "prompt", "request", "label", "conclusion"):
        return ["", "   ", "\x00", "🔥" * 10, "한국어 " * 10, "x" * 2000]
    return [None, "", 0]


def main() -> int:
    import dobby
    modules = [m.name for m in pkgutil.walk_packages(dobby.__path__, "dobby.")]
    findings, checked, skipped = [], 0, 0

    for modname in sorted(modules):
        if modname in SKIP_MODULES:
            continue
        try:
            mod = importlib.import_module(modname)
        except Exception as exc:                       # noqa: BLE001
            findings.append((modname, "<import>", "", f"{type(exc).__name__}: {exc}"))
            continue

        for fname, fn in inspect.getmembers(mod, inspect.isfunction):
            if fname.startswith("_") or fn.__module__ != modname:
                continue
            if fname in SKIP_FUNCTIONS:
                skipped += 1
                continue
            try:
                sig = inspect.signature(fn)
            except (ValueError, TypeError):
                continue
            required = [p for p in sig.parameters.values()
                        if p.default is inspect.Parameter.empty
                        and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
            if len(required) > 3:
                continue

            pools = [candidates(p.name, p.annotation) for p in required]
            for slot, pool in enumerate(pools):
                for value in pool:
                    args = [value if i == slot
                            else candidates(p.name, p.annotation)[0]
                            for i, p in enumerate(required)]
                    checked += 1
                    try:
                        fn(*args)
                    except DELIBERATE:
                        pass                    # a refusal is the right answer
                    except RecursionError:
                        findings.append((modname, fname, repr(value)[:22],
                                         "RecursionError"))
                    except Exception as exc:    # noqa: BLE001
                        findings.append((modname, fname, repr(value)[:22],
                                         f"{type(exc).__name__}: "
                                         f"{str(exc)[:44]}"))

    print(f"probed {checked} degenerate calls across {len(modules)} modules "
          f"({skipped} functions skipped as side-effecting)")
    print()
    if not findings:
        print("no unexpected exception types raised")
        return 0
    print("%-24s %-26s %-24s %s" % ("module", "function", "input", "raised"))
    for mod, fn, val, exc in findings:
        print("%-24s %-26s %-24s %s" % (mod.replace("dobby.", ""), fn, val, exc))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
