"""Acceptance for the stages a command could not previously grade.

THE PROBLEM THIS SOLVES

`WorkItem` requires acceptance_checks that are commands, and `needs_architect`
is True for any item without one. That rule is right for implementation work and
it is exactly why a research lifecycle cannot be driven by this harness: 문헌조사
and 아이디어 기획 have no command that says "this is done", so every such item
halts the loop at NEEDS_ARCHITECT and a person supplies the judgement each time.

The fix is not to relax the rule. It is to notice that the ungradeable thing was
never the STAGE — it was the stage's *outcome as prose*. A stage that must emit a
structured artifact is a stage whose artifact can be checked by a command, and
that is what this module provides: one validator per stage kind, each reachable
from the command line, each exiting non-zero when the artifact does not hold up.

WHY EVERY GATE GOES PAST EXISTENCE

`docs/FAILURE_CATALOG.md` names the trap and this repository repeats it:
existence is not a measurement. A validator that only checks "the file is there
and parses" grades the stage on whether somebody wrote a file, which every
failing stage also does. So each gate below asserts a property that a stage doing
the work badly would actually fail:

- a background row must resolve to a path that EXISTS on this disk;
- a literature artifact must contain a source retrieved by a refutation- or
  limitation-shaped query, because a search that only looked for confirmation is
  the failure `dobby/research.py` was written to prevent;
- an ideation artifact is handed to `swarm/grounding.py`'s existing gate, so an
  idea with no evidence anchor or no falsifiable test fails here for the same
  reason it fails there;
- an evaluation artifact must beat a trivial baseline and carry more than one run
  per arm, delegated to `dobby/mlops.py` rather than re-implemented;
- a defect row must carry a reproducible input to wrong-output pair, the standard
  `dobby/review.py` already applies to a finding.

NOTHING HERE IS A MODEL CALL

Every check is arithmetic, a filesystem probe, or a delegation to an existing
deterministic gate. A validator that asked a model "is this literature review
good enough?" would answer yes for the same reason an ungated panel returns
plausible nonsense, and it would answer differently on the second run, which
makes it useless as a definition of done.

WHAT A PASS DOES NOT MEAN

A pass means the artifact has the structure and the minima the stage declared. It
does not mean the sources are real — resolution against a retrieved corpus is
`research.verify_citations`, and with no corpus supplied this module reports NOT
CHECKED rather than clean, preserving the distinction that module already makes.
Read `unverified` in every verdict before treating a pass as a finding.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..mlops import compare_runs, trivial_baseline_check
from ..research import parse_reference, verify_citations
from ..swarm.grounding import Evidence, Idea, assess, explore_cycle, gate

# -- stage kinds -------------------------------------------------------------
BACKGROUND = "background"
LITERATURE = "literature"
DATASET = "dataset"
IDEATION = "ideation"
ELABORATION = "elaboration"
EVALUATION = "evaluation"
DEBUG = "debug"

#: Every kind this module can grade. `implementation` is deliberately absent: an
#: implementation stage is graded by the project's OWN smoke and test commands,
#: which already exist in the manifest, and inventing a second definition of done
#: for it is how two disagreeing measurements get created.
KINDS = (BACKGROUND, LITERATURE, DATASET, IDEATION, ELABORATION, EVALUATION,
         DEBUG)

#: Minimum rows per kind. Small on purpose — these are floors that catch an empty
#: or one-line artifact, not quality bars. A project that wants more passes
#: `--min`, and the emitted acceptance command records the number it used.
DEFAULT_MIN = {BACKGROUND: 3, LITERATURE: 5, DATASET: 1, IDEATION: 3,
               ELABORATION: 1, EVALUATION: 1, DEBUG: 1}

#: Query shapes whose presence proves the search looked for disconfirming
#: evidence. Mirrors `research.QUERY_SHAPES`; kept as its own tuple so that
#: adding a shape there cannot silently change what counts as a passing search.
DISCONFIRMING = ("refutation", "limitation")

#: Splits a dataset row may declare. A row with no split is a row whose leakage
#: story is unstated, and `dobby/mlops.py` exists because that is the error that
#: invalidates everything downstream of it.
SPLITS = ("train", "dev", "val", "validation", "test", "holdout", "full")


class ArtifactError(ValueError):
    """The artifact could not be read or is not the shape this kind requires."""


def _rows(doc: Any, key: str) -> list[dict]:
    """Pull the row list, refusing the shapes that silently grade as empty."""
    if not isinstance(doc, dict):
        raise ArtifactError(
            f"expected a JSON object with a {key!r} list, got "
            f"{type(doc).__name__}")
    rows = doc.get(key)
    if rows is None:
        raise ArtifactError(
            f"no {key!r} key: an artifact whose rows are missing is not an "
            f"artifact with zero rows, and grading it as empty would report a "
            f"malformed file as a stage that honestly found nothing")
    if not isinstance(rows, list):
        raise ArtifactError(f"{key!r} must be a list, got {type(rows).__name__}")
    return [r for r in rows if isinstance(r, dict)]


def _verdict(kind: str, ok: bool, *, measured: dict, failures: list[str],
             unverified: list[str]) -> dict:
    return {
        "kind": kind,
        "ok": bool(ok and not failures),
        "measured": measured,
        # Each failure names the row index and the field, because "3 rows failed"
        # without the three is the finding shape this repository rejects.
        "failures": failures,
        # What a pass still does not establish. Never empty for a kind that
        # depends on external truth.
        "unverified": unverified,
    }


# -- per-kind gates ----------------------------------------------------------

def check_background(doc: Any, *, min_rows: int, root: str = ".") -> dict:
    """Claims about the project, each anchored to a path that exists here.

    The anchor is resolved against the filesystem rather than trusted, which is
    invariant 3 applied to a research artifact: a cited path is a claim, and the
    tree is the fact.
    """
    rows = _rows(doc, "findings")
    failures: list[str] = []
    resolved = 0
    for i, row in enumerate(rows):
        claim = str(row.get("claim", "")).strip()
        path = str(row.get("path", "")).strip()
        if not claim:
            failures.append(f"findings[{i}].claim is empty")
        if not path:
            failures.append(
                f"findings[{i}].path is empty: an unanchored claim cannot be "
                f"checked by the next stage")
            continue
        if os.path.exists(os.path.join(root, path)) or os.path.exists(path):
            resolved += 1
        else:
            failures.append(
                f"findings[{i}].path {path!r} does not exist under {root!r}")
    if len(rows) < min_rows:
        failures.append(
            f"{len(rows)} finding(s), fewer than the {min_rows} this stage "
            f"declared")
    return _verdict(BACKGROUND, len(rows) >= min_rows,
                    measured={"rows": len(rows), "paths_resolved": resolved,
                              "min_rows": min_rows},
                    failures=failures,
                    unverified=["whether each claim is TRUE of the file it "
                                "names — this gate resolves the path only"])


def check_literature(doc: Any, *, min_rows: int,
                     corpus: list[dict] | None = None) -> dict:
    """Sources with locators, and proof the search looked for disconfirmation."""
    rows = _rows(doc, "sources")
    failures: list[str] = []
    located = 0
    shapes: set[str] = set()
    for i, row in enumerate(rows):
        raw = " ".join(str(row.get(k, "")) for k in
                       ("title", "publisher", "url", "identifier", "raw")).strip()
        if not raw:
            failures.append(f"sources[{i}] has no title/publisher/url at all")
            continue
        ref = parse_reference(raw)
        if ref.identifier:
            located += 1
        else:
            failures.append(
                f"sources[{i}] {ref.title[:48]!r} has no resolvable locator "
                f"(DOI, arXiv id, or URL): nobody else can reach it, so the "
                f"claim resting on it is not checkable")
        shape = str(row.get("shape", "")).strip().lower()
        if shape:
            shapes.add(shape)
    disconfirming = sorted(shapes & set(DISCONFIRMING))
    if not disconfirming:
        failures.append(
            "no source carries shape 'refutation' or 'limitation': a search that "
            "only ran confirming queries returns enough to stop early, which is "
            "the failure dobby/research.py plans against. Record the "
            "disconfirming query even when it returned nothing — "
            "searched-and-empty is a result, not-searched is not")
    if len(rows) < min_rows:
        failures.append(
            f"{len(rows)} source(s), fewer than the {min_rows} this stage "
            f"declared")
    citations = verify_citations([str(r.get("title", "")) for r in rows],
                                 corpus or [])
    return _verdict(LITERATURE, len(rows) >= min_rows,
                    measured={"rows": len(rows), "with_locator": located,
                              "min_rows": min_rows,
                              "disconfirming_shapes": disconfirming,
                              "citation_check": citations.get(
                                  "verdict", citations.get("note", ""))},
                    failures=failures,
                    unverified=([] if corpus else
                                ["every source is a CLAIM of a source: no "
                                 "corpus was supplied, so verify_citations "
                                 "reports NOT CHECKED rather than clean"]))


def check_dataset(doc: Any, *, min_rows: int, root: str = ".") -> dict:
    """A data manifest whose leakage story is stated before anything is trained."""
    rows = _rows(doc, "datasets")
    failures: list[str] = []
    for i, row in enumerate(rows):
        if not str(row.get("name", "")).strip():
            failures.append(f"datasets[{i}].name is empty")
        if not str(row.get("source", "")).strip():
            failures.append(
                f"datasets[{i}].source is empty: a dataset with no stated "
                f"origin cannot be checked for re-hosted public test data")
        if not str(row.get("license", "")).strip():
            failures.append(
                f"datasets[{i}].license is empty: an unlicensed dataset is an "
                f"escalation, not a default")
        n = row.get("n_rows")
        if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
            failures.append(
                f"datasets[{i}].n_rows is {n!r}, not a positive integer "
                f"measured from the data")
        split = str(row.get("split", "")).strip().lower()
        if split not in SPLITS:
            failures.append(
                f"datasets[{i}].split is {(split or '(missing)')!r}; expected "
                f"one of {SPLITS}")
        path = str(row.get("path", "")).strip()
        if path and not (os.path.exists(os.path.join(root, path))
                         or os.path.exists(path)):
            failures.append(
                f"datasets[{i}].path {path!r} does not exist under {root!r}")
    if len(rows) < min_rows:
        failures.append(
            f"{len(rows)} dataset(s), fewer than the {min_rows} this stage "
            f"declared")
    return _verdict(DATASET, len(rows) >= min_rows,
                    measured={"rows": len(rows), "min_rows": min_rows},
                    failures=failures,
                    unverified=["whether the split is actually disjoint — run "
                                "`dobby ml --file <experiment.json>` on the "
                                "experiment that consumes this"])


def ideas_and_corpus(doc: Any) -> tuple[list[Idea], list[Evidence]]:
    rows = _rows(doc, "ideas")
    ideas = [Idea(title=str(r.get("title", "")),
                  body=str(r.get("body", "")),
                  evidence_ids=tuple(str(e) for e in (r.get("evidence_ids")
                                                      or [])),
                  falsifiable_test=str(r.get("falsifiable_test", "")),
                  lens=str(r.get("lens", "")),
                  author=str(r.get("author", ""))) for r in rows]
    corpus = [Evidence(id=str(c.get("id", "")),
                       summary=str(c.get("summary", "")),
                       path=(str(c["path"]) if c.get("path") else None),
                       verified=bool(c.get("verified", False)))
              for c in (doc.get("corpus") or []) if isinstance(c, dict)]
    return ideas, corpus


def check_ideation(doc: Any, *, min_rows: int) -> dict:
    """Delegated wholesale to the grounding gate, which already decides this.

    Re-implementing the accept rule here would create a second definition of a
    grounded idea and the two would drift. What this adds is the ACCEPTED floor:
    the gate reports a histogram, and a stage that produced twenty ideas of which
    none survived has not completed.
    """
    ideas, corpus = ideas_and_corpus(doc)
    result = gate(ideas, corpus)
    failures: list[str] = []
    if result["accepted"] < min_rows:
        failures.append(
            f"{result['accepted']} idea(s) survived the grounding gate, fewer "
            f"than the {min_rows} this stage declared; rejection histogram "
            f"{result['rejection_histogram']}")
    if not result["prior_art_available"]:
        failures.append(f"prior art: {result['prior_art_note']}")
    return _verdict(IDEATION, result["accepted"] >= min_rows,
                    measured={"total": result["total"],
                              "accepted": result["accepted"],
                              "min_accepted": min_rows,
                              "rejection_histogram":
                                  result["rejection_histogram"],
                              "accepted_titles": result["accepted_titles"]},
                    failures=failures,
                    unverified=["the gate is structural: it does not judge "
                                "whether an accepted idea is a GOOD one"])


def check_elaboration(doc: Any, *, min_rows: int) -> dict:
    """A grounded idea that also names what would be built, and how it is graded.

    Elaboration is where an idea stops being a direction. The extra fields are
    the two an implementation worker cannot proceed without, so their absence is
    caught here rather than three stages later as an ungradeable implementation
    item — which is the halt this whole module exists to remove.
    """
    ideas, corpus = ideas_and_corpus(doc)
    result = gate(ideas, corpus)
    rows = _rows(doc, "ideas")
    failures: list[str] = []
    concrete = 0
    for i, row in enumerate(rows):
        targets = row.get("targets") or []
        checks = row.get("acceptance_checks") or []
        if not (isinstance(targets, list) and targets):
            failures.append(
                f"ideas[{i}].targets is empty: name the file(s) or module(s) "
                f"this would change")
        if not (isinstance(checks, list) and checks):
            failures.append(
                f"ideas[{i}].acceptance_checks is empty: an elaboration that "
                f"does not say how it would be graded hands the next stage the "
                f"same problem")
        if targets and checks:
            concrete += 1
    if result["accepted"] < min_rows:
        failures.append(
            f"{result['accepted']} elaborated idea(s) passed the grounding "
            f"gate, fewer than {min_rows}; histogram "
            f"{result['rejection_histogram']}")
    return _verdict(ELABORATION,
                    result["accepted"] >= min_rows and concrete >= min_rows,
                    measured={"total": result["total"],
                              "grounded": result["accepted"],
                              "with_target_and_check": concrete,
                              "min_rows": min_rows},
                    failures=failures,
                    unverified=["whether the named acceptance checks actually "
                                "run — that is the implementation stage's gate"])


def check_evaluation(doc: Any, *, min_rows: int = 1) -> dict:
    """A score is not a result. Delegated to the rigor gates that already exist."""
    if not isinstance(doc, dict):
        raise ArtifactError("expected a JSON object")
    metric = str(doc.get("metric", "")).strip()
    failures: list[str] = []
    if not metric:
        failures.append("metric is empty: an unnamed number is not "
                        "interpretable")
    baseline = [float(x) for x in (doc.get("baseline_runs") or [])
                if isinstance(x, (int, float)) and not isinstance(x, bool)]
    candidate = [float(x) for x in (doc.get("candidate_runs") or [])
                 if isinstance(x, (int, float)) and not isinstance(x, bool)]
    comparison = compare_runs(baseline, candidate,
                              min_runs=int(doc.get("min_runs", 3)))
    if not comparison.get("comparable"):
        failures.append(f"run comparison: {comparison.get('verdict')}")
    trivial = trivial_baseline_check(
        metric or "unnamed",
        float(doc.get("headline_score", 0.0)),
        majority_class_rate=doc.get("majority_class_rate"),
        n_classes=doc.get("n_classes"))
    if trivial.get("checked") and not trivial.get("beats_trivial"):
        failures.append(f"trivial baseline: {trivial['verdict']}")
    if not trivial.get("checked"):
        failures.append(f"trivial baseline: {trivial['note']}")
    return _verdict(EVALUATION, not failures,
                    measured={"metric": metric,
                              "baseline_runs": len(baseline),
                              "candidate_runs": len(candidate),
                              "comparison": comparison.get("verdict"),
                              "trivial": trivial.get("verdict",
                                                     trivial.get("note"))},
                    failures=failures,
                    unverified=["leakage: run `dobby ml --file "
                                "<experiment.json>`; a leaked score makes every "
                                "number beside it meaningless"])


def check_debug(doc: Any, *, min_rows: int = 1) -> dict:
    """Defects with a reproduction, which is the only kind that is actionable."""
    rows = _rows(doc, "defects")
    failures: list[str] = []
    actionable = 0
    for i, row in enumerate(rows):
        fields = (("repro", str(row.get("repro", "")).strip()),
                  ("observed", str(row.get("observed", "")).strip()),
                  ("expected", str(row.get("expected", "")).strip()),
                  ("path", str(row.get("path", "")).strip()))
        missing = [k for k, v in fields if not v]
        if missing:
            failures.append(
                f"defects[{i}] missing {missing}: a finding without an input to "
                f"wrong-output scenario is not actionable and does not count "
                f"toward a decision")
        else:
            actionable += 1
    if actionable < min_rows:
        failures.append(
            f"{actionable} actionable defect(s), fewer than the {min_rows} this "
            f"stage declared")
    return _verdict(DEBUG, actionable >= min_rows,
                    measured={"rows": len(rows), "actionable": actionable,
                              "min_rows": min_rows},
                    failures=failures,
                    unverified=["whether each repro actually reproduces — "
                                "run it"])


_GATES = {
    BACKGROUND: check_background,
    LITERATURE: check_literature,
    DATASET: check_dataset,
    IDEATION: check_ideation,
    ELABORATION: check_elaboration,
    EVALUATION: check_evaluation,
    DEBUG: check_debug,
}


def check_file(kind: str, path: str, *, min_rows: int | None = None,
               root: str = ".", corpus_path: str | None = None) -> dict:
    """Read an artifact and grade it. The entry point the CLI and the checks use.

    A missing or unparseable file is a FAIL and not an exception, because this is
    reached as an acceptance check whose caller needs a verdict rather than a
    traceback — and "the stage produced nothing" is the most common way a stage
    fails.
    """
    if kind not in _GATES:
        raise ArtifactError(f"unknown kind {kind!r}; expected one of {KINDS}")
    floor = DEFAULT_MIN[kind] if min_rows is None else int(min_rows)
    # A relative artifact path is relative to the PROJECT, not to whoever
    # happened to invoke this. `reattempt.derive_repair` runs from the caller's
    # cwd while grading a project elsewhere, and resolving against cwd there
    # would report "the stage produced no artifact" about a file that exists.
    target = path if os.path.isabs(path) else os.path.join(root, path)
    if not os.path.exists(target):
        return _verdict(kind, False,
                        measured={"path": path, "resolved": target,
                                  "exists": False},
                        failures=[f"{path} does not exist: the stage produced "
                                  f"no artifact"],
                        unverified=[])
    try:
        with open(target, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return _verdict(kind, False,
                        measured={"path": path, "resolved": target,
                                  "exists": True},
                        failures=[f"{path} is not readable JSON: {exc}"],
                        unverified=[])

    corpus = None
    corpus_target = (corpus_path if not corpus_path or os.path.isabs(corpus_path)
                     else os.path.join(root, corpus_path))
    if corpus_target and os.path.exists(corpus_target):
        try:
            with open(corpus_target, encoding="utf-8") as fh:
                loaded = json.load(fh)
            corpus = loaded if isinstance(loaded, list) else loaded.get("corpus")
        except (OSError, json.JSONDecodeError):
            corpus = None

    fn = _GATES[kind]
    try:
        if kind in (BACKGROUND, DATASET):
            verdict = fn(doc, min_rows=floor, root=root)
        elif kind == LITERATURE:
            verdict = fn(doc, min_rows=floor, corpus=corpus)
        else:
            verdict = fn(doc, min_rows=floor)
    except ArtifactError as exc:
        return _verdict(kind, False, measured={"path": path},
                        failures=[str(exc)], unverified=[])

    verdict["path"] = path
    # An ideation-family failure carries repair instructions, so the next attempt
    # has something to CHANGE. That is what `project/reattempt.py` consumes, and
    # it is why explore_cycle stopped being advice a human had to relay by hand.
    if kind in (IDEATION, ELABORATION) and not verdict["ok"]:
        ideas, corpus_objs = ideas_and_corpus(doc)
        verdict["repairs"] = explore_cycle([assess(i, corpus_objs)
                                            for i in ideas])
    return verdict


def acceptance_command(kind: str, artifact: str, *, min_rows: int | None = None,
                       python: str = "python") -> str:
    """The command a work item carries as its definition of done.

    Emitted here rather than assembled by each caller so that the check an item
    is graded by and the check this module runs cannot drift apart.
    """
    floor = DEFAULT_MIN[kind] if min_rows is None else int(min_rows)
    return (f"{python} -m dobby.cli project check --kind {kind} "
            f"--file {artifact} --min {floor}")
