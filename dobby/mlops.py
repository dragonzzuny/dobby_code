"""ML and data-analysis gates: the checks a general coding agent does not make.

Why ML work needs its own gates
-------------------------------
A coding agent judges its work by "does it run and do the tests pass". Both can
be true while an ML result is worthless, and the failure is silent — a leaked
target produces a 0.99 AUC that looks like success and reports as success. The
capabilities that separate ML agents from coding agents are consistently the
same ones: experiment tracking and reproducibility, data validation and leakage
detection, statistical rigor, and benchmarking against a real baseline.

The gates here are deliberately structural and deterministic. A model asked "did
I leak the target?" will usually say no; a check that looks at whether the split
happened before the scaler was fit does not have an opinion.

Four gates, in the order failures actually occur
-----------------------------------------------
1. **Leakage** — the most damaging and the least visible. Fit-before-split,
   target-derived features, group spillover, temporal shuffling.
2. **Reproducibility** — an unrepeatable number is not a result. Seeds, data
   versions, environment, and the exact command.
3. **Statistical rigor** — a single run's delta is not an improvement. Variance,
   comparison against a trivial baseline, multiple-comparison awareness.
4. **Interpretation** — the step where a correct number becomes a wrong claim.
   Wired to `research.classify_strength` so an ML conclusion is held to the same
   evidence bar as a paper's.

Nothing here trains, fits, or imports a modelling library. The gates read the
described SETUP and the reported NUMBERS, so they run in the same stdlib-only
environment as the rest of the kit and can be applied to a notebook, a script, or
a paragraph of prose describing an experiment.
"""

from __future__ import annotations

import dataclasses
import math
import re
from collections.abc import Sequence

# --------------------------------------------------------------------------
# Leakage
# --------------------------------------------------------------------------

#: Transformations that must be FIT on training data only. Fitting them on the
#: full dataset before splitting leaks test-set statistics into training, which
#: inflates every subsequent score.
_FIT_BEFORE_SPLIT_OPS = (
    "standardscaler", "minmaxscaler", "robustscaler", "normalizer",
    "quantiletransformer", "powertransformer", "pca", "truncatedsvd",
    "selectkbest", "rfe", "targetencoder", "labelencoder", "imputer",
    "simpleimputer", "knnimputer", "tfidfvectorizer", "countvectorizer",
    "smote", "oversampl", "undersampl", "resample",
)

#: Naming patterns that suggest a feature was derived from the target. These are
#: HINTS, not proof — a column named `target_region` is legitimate — so they are
#: reported as suspicions with the reason, never as confirmed leakage.
_TARGET_DERIVED_HINTS = (
    "target", "label", "outcome", "_y", "ground_truth", "gt_",
    "actual", "future_", "next_", "post_", "_after", "settled",
    "final_", "resolved", "churned", "converted", "died", "survived",
)

#: Splitters that ignore group structure. When rows share an entity (patient,
#: user, device, session), a random split puts the same entity on both sides and
#: the model memorizes the entity rather than the pattern.
_NON_GROUP_SPLITTERS = ("train_test_split", "kfold", "stratifiedkfold",
                        "shufflesplit", "repeatedkfold")

_GROUP_AWARE = ("groupkfold", "groupshufflesplit", "stratifiedgroupkfold",
                "leaveonegroupout", "timeseriessplit")

LEAKAGE_FIT_BEFORE_SPLIT = "fit_before_split"
LEAKAGE_TARGET_DERIVED = "target_derived_feature"
LEAKAGE_GROUP_SPILLOVER = "group_spillover"
LEAKAGE_TEMPORAL = "temporal_shuffle"
LEAKAGE_DUPLICATE_ROWS = "duplicate_rows_across_split"
LEAKAGE_TEST_REUSE = "test_set_reused_for_selection"


@dataclasses.dataclass
class ExperimentSetup:
    """How an experiment was actually run. Everything the gates need.

    Fields are optional because a real setup is often partly undocumented — and
    an UNKNOWN field is itself a finding. `None` means "not stated", which the
    gates report as a gap rather than assuming the safe value.
    """

    #: Ordered names of pipeline steps as they were executed. Order is what makes
    #: fit-before-split detectable at all.
    steps: tuple[str, ...] = ()
    split_method: str | None = None
    feature_names: tuple[str, ...] = ()
    target_name: str | None = None
    #: Column identifying the entity a row belongs to, if rows are grouped.
    group_column: str | None = None
    #: True when observations are ordered in time (any forecasting setup).
    temporal: bool = False
    seeds: tuple[int, ...] = ()
    data_version: str | None = None
    environment_pinned: bool = False
    command: str | None = None
    #: Number of times the test/holdout set was consulted while choosing a model.
    test_set_evaluations: int = 1
    duplicate_rows: int | None = None
    n_train: int | None = None
    n_test: int | None = None

    def step_index(self, needle: str) -> int | None:
        for i, step in enumerate(self.steps):
            if needle in step.lower().replace("_", ""):
                return i
        return None

    def split_index(self) -> int | None:
        for i, step in enumerate(self.steps):
            low = step.lower().replace("_", "")
            if "split" in low or "fold" in low:
                return i
        return None


@dataclasses.dataclass
class Leak:
    """One suspected leakage path."""

    kind: str
    severity: str          # "confirmed" | "suspected"
    detail: str
    fix: str

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def detect_leakage(setup: ExperimentSetup) -> dict:
    """Find leakage paths in a described setup.

    Severity is `confirmed` only when the ORDER of operations proves it — a
    scaler fitted at step 1 and a split at step 3 is not a matter of opinion.
    Everything driven by naming or missing information is `suspected`, because a
    false accusation of leakage costs a real investigation.
    """
    leaks: list[Leak] = []
    split_at = setup.split_index()

    # 1. fit-before-split — provable from ordering.
    for op in _FIT_BEFORE_SPLIT_OPS:
        idx = setup.step_index(op)
        if idx is None:
            continue
        if split_at is None:
            leaks.append(Leak(
                LEAKAGE_FIT_BEFORE_SPLIT, "suspected",
                f"'{op}' appears in the pipeline but no split step was recorded, "
                "so it cannot be shown to have been fitted on training data only",
                "record the split step explicitly, and fit every transformer "
                "inside a pipeline that is fitted after the split"))
        elif idx < split_at:
            leaks.append(Leak(
                LEAKAGE_FIT_BEFORE_SPLIT, "confirmed",
                f"'{setup.steps[idx]}' (step {idx}) is fitted BEFORE the split "
                f"(step {split_at}): test-set statistics enter training and "
                "inflate every score computed afterwards",
                "move it into a Pipeline fitted after the split, or fit on train "
                "and only transform test"))

    # 2. target-derived features — naming heuristic, hence suspected.
    target = (setup.target_name or "").lower()
    for feature in setup.feature_names:
        low = feature.lower()
        if target and (low == target):
            leaks.append(Leak(
                LEAKAGE_TARGET_DERIVED, "confirmed",
                f"feature '{feature}' IS the target column",
                "drop it from the feature matrix"))
            continue
        if target and target in low and low != target:
            leaks.append(Leak(
                LEAKAGE_TARGET_DERIVED, "suspected",
                f"feature '{feature}' contains the target name '{target}': it may "
                "be a transformation of the label",
                f"confirm '{feature}' is knowable at prediction time; if it is "
                "computed from the outcome, drop it"))
            continue
        for hint in _TARGET_DERIVED_HINTS:
            if hint in low:
                leaks.append(Leak(
                    LEAKAGE_TARGET_DERIVED, "suspected",
                    f"feature '{feature}' matches the post-outcome pattern "
                    f"'{hint}': such columns are often only knowable AFTER the "
                    "event being predicted",
                    f"check whether '{feature}' is available at prediction time "
                    "for a real unseen case"))
                break

    # 3. group spillover.
    method = (setup.split_method or "").lower().replace("_", "")
    # Group-aware splitters are checked FIRST and short-circuit. Order matters
    # because the names nest: "groupkfold" CONTAINS "kfold", so testing the
    # non-group list first would flag GroupKFold — the correct choice — as the
    # problem. (Same substring hazard as `"proves" in "improves"`.)
    group_aware = any(g in method for g in _GROUP_AWARE)
    if setup.group_column:
        if not group_aware:
            leaks.append(Leak(
                LEAKAGE_GROUP_SPILLOVER, "confirmed",
                f"rows are grouped by '{setup.group_column}' but the split is "
                f"'{setup.split_method}', which ignores groups: the same entity "
                "appears in train and test, so the model can memorize the entity "
                "instead of the pattern",
                "use GroupKFold / GroupShuffleSplit / StratifiedGroupKFold on "
                f"'{setup.group_column}'"))
    elif not group_aware and any(s in method for s in _NON_GROUP_SPLITTERS):
        leaks.append(Leak(
            LEAKAGE_GROUP_SPILLOVER, "suspected",
            f"'{setup.split_method}' assumes rows are independent, and no group "
            "column was declared",
            "state explicitly that rows are independent, or name the entity "
            "column and use a group-aware splitter"))

    # 4. temporal shuffling.
    if setup.temporal and "timeseries" not in method:
        leaks.append(Leak(
            LEAKAGE_TEMPORAL, "confirmed",
            f"observations are temporal but the split is '{setup.split_method}': "
            "training on future rows to predict past ones measures nothing that "
            "will hold in deployment",
            "use TimeSeriesSplit or a fixed cut-off date, and never shuffle"))

    # 5. duplicates across the split.
    if setup.duplicate_rows:
        leaks.append(Leak(
            LEAKAGE_DUPLICATE_ROWS, "suspected",
            f"{setup.duplicate_rows} duplicate row(s) exist: identical rows on "
            "both sides of the split are memorization, scored as generalization",
            "deduplicate before splitting, or split on a content hash"))

    # 6. test-set reuse for selection.
    if setup.test_set_evaluations > 1:
        leaks.append(Leak(
            LEAKAGE_TEST_REUSE, "confirmed",
            f"the test set was evaluated {setup.test_set_evaluations} times while "
            "selecting a model: it has become a validation set, and the reported "
            "score is optimistically biased",
            "select on a validation split and touch the holdout exactly once"))

    confirmed = [l for l in leaks if l.severity == "confirmed"]
    return {
        "leaks": [l.to_dict() for l in leaks],
        "confirmed": len(confirmed),
        "suspected": len(leaks) - len(confirmed),
        "blocks_result": bool(confirmed),
        "verdict": (f"{len(confirmed)} CONFIRMED leakage path(s): the reported "
                    "scores do not estimate generalization and must not be "
                    "reported as performance"
                    if confirmed else
                    f"no confirmed leakage; {len(leaks)} suspicion(s) to check"
                    if leaks else
                    "no leakage detected in the described setup — note this "
                    "checks the DESCRIPTION, not the data"),
    }


# --------------------------------------------------------------------------
# Reproducibility
# --------------------------------------------------------------------------

def check_reproducibility(setup: ExperimentSetup) -> dict:
    """Whether the experiment could be re-run to the same number.

    Every missing element is reported with what it makes unreproducible, because
    "not reproducible" alone does not tell anyone what to record next time.
    """
    gaps = []

    def gap(what: str, consequence: str, fix: str) -> None:
        gaps.append({"missing": what, "consequence": consequence, "fix": fix})

    if not setup.seeds:
        gap("random seed",
            "the same code produces a different number on every run, so any "
            "reported delta may be run-to-run noise",
            "set and record seeds for the splitter, the model, and the "
            "framework's global RNG")
    if not setup.data_version:
        gap("data version",
            "a future run may use different rows, making the number "
            "unreproducible even with a fixed seed",
            "record a content hash, row count, and date range of the input")
    if not setup.environment_pinned:
        gap("pinned environment",
            "library version changes silently alter defaults (solver, "
            "regularization, tokenizer), moving results without any code change",
            "pin versions in a lock file and record it with the result")
    if not setup.command:
        gap("exact command",
            "the result cannot be re-invoked, so re-running becomes guesswork",
            "record the full command line including every flag")
    if setup.n_train is None or setup.n_test is None:
        gap("split sizes",
            "without n_train/n_test a reader cannot judge whether the score's "
            "confidence interval is meaningful",
            "record both counts")

    return {
        "reproducible": not gaps,
        "gap_count": len(gaps),
        "gaps": gaps,
        "verdict": ("reproducible: seed, data version, environment, and command "
                    "are all recorded"
                    if not gaps else
                    f"{len(gaps)} reproducibility gap(s): the number cannot be "
                    "re-derived, so it is an observation and not yet a result"),
    }


# --------------------------------------------------------------------------
# Statistical rigor
# --------------------------------------------------------------------------

def compare_runs(baseline: Sequence[float], candidate: Sequence[float], *,
                 min_runs: int = 3) -> dict:
    """Is the candidate actually better, or is this one lucky run?

    Uses the mean difference against the pooled spread rather than a named
    hypothesis test. The reason is honesty about sample size: with the 3–5 runs an
    ML experiment typically has, a t-test's p-value carries a precision the data
    does not support. What IS defensible at n=3 is "the gap is larger than the
    noise" versus "it is not", and that is what this reports.

    A single run per arm cannot separate signal from variance at all, and that is
    stated rather than scored.
    """
    b, c = list(baseline), list(candidate)
    if not b or not c:
        return {"comparable": False,
                "verdict": "no runs supplied for one or both arms"}
    if len(b) < min_runs or len(c) < min_runs:
        return {
            "comparable": False,
            "baseline_runs": len(b), "candidate_runs": len(c),
            "baseline_mean": round(sum(b) / len(b), 6),
            "candidate_mean": round(sum(c) / len(c), 6),
            "verdict": (f"only {len(b)}/{len(c)} run(s): fewer than {min_runs} "
                        "per arm cannot separate an improvement from run-to-run "
                        "variance. Report the delta as UNVERIFIED and re-run"),
        }

    def mean(xs):
        return sum(xs) / len(xs)

    def stdev(xs):
        m = mean(xs)
        return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))

    mb, mc = mean(b), mean(c)
    sb, sc = stdev(b), stdev(c)
    pooled = math.sqrt((sb ** 2 + sc ** 2) / 2)
    delta = mc - mb

    # "Zero variance" must be a TOLERANCE, not an equality test. Identical
    # floats do not give an exactly-zero stdev: mean([0.7,0.7,0.7]) is
    # 0.7000000000000001, so the residuals are ~1e-17 and `pooled == 0` is
    # False. Dividing by that produces an effect size of ~1e15, which reads as
    # overwhelming evidence when the truth is that there is no variance to
    # measure against. The tolerance is scaled to the data's magnitude so it
    # works for metrics in [0,1] and for losses in the thousands alike.
    scale = max(abs(mb), abs(mc), 1e-12)
    degenerate = pooled <= scale * 1e-12

    # Effect size in units of the noise. |d| < 1 means the arms' run-to-run
    # spread is as large as the gap between them.
    effect = 0.0 if degenerate else (delta / pooled)
    overlapping = (min(b) <= max(c)) and (min(c) <= max(b))

    if degenerate and delta != 0:
        verdict = ("zero variance in both arms with a nonzero gap: either the "
                   "runs are not independent (same seed) or the metric is "
                   "deterministic — check before claiming an improvement")
    elif abs(effect) < 1.0:
        verdict = (f"delta {delta:+.4f} is smaller than the pooled run-to-run "
                   f"spread ({pooled:.4f}): NOT distinguishable from noise")
    elif overlapping:
        verdict = (f"delta {delta:+.4f} exceeds the spread (effect "
                   f"{effect:+.2f}) but the run ranges OVERLAP: report as "
                   "suggestive, not established")
    else:
        verdict = (f"delta {delta:+.4f} with effect {effect:+.2f} and "
                   "non-overlapping ranges: a real difference on this metric")

    return {
        "comparable": True,
        "baseline_runs": len(b), "candidate_runs": len(c),
        "baseline_mean": round(mb, 6), "candidate_mean": round(mc, 6),
        "baseline_stdev": round(sb, 6), "candidate_stdev": round(sc, 6),
        "delta": round(delta, 6),
        "pooled_stdev": round(pooled, 6),
        "effect_size": round(effect, 4),
        "degenerate_variance": degenerate,
        "ranges_overlap": overlapping,
        "better": delta > 0 and abs(effect) >= 1.0 and not overlapping,
        "verdict": verdict,
    }


def trivial_baseline_check(metric: str, score: float, *,
                           majority_class_rate: float | None = None,
                           n_classes: int | None = None) -> dict:
    """Is the score better than predicting the majority class or guessing?

    The most common way an ML result misleads is by omitting this comparison: a
    0.95 accuracy on a 95%-negative dataset is what a constant predictor
    achieves. Reported as a hard gate because a model that does not beat the
    trivial baseline has demonstrated nothing regardless of its architecture.
    """
    low = metric.lower()
    floor = None
    basis = ""
    if majority_class_rate is not None and ("acc" in low or "f1" in low):
        floor, basis = majority_class_rate, "always predict the majority class"
    elif n_classes and "acc" in low:
        floor, basis = 1.0 / n_classes, f"uniform guessing over {n_classes} classes"
    elif "auc" in low or "roc" in low:
        floor, basis = 0.5, "random ranking"
    elif "r2" in low:
        floor, basis = 0.0, "always predict the training mean"

    if floor is None:
        return {"checked": False, "metric": metric, "score": score,
                "note": (f"no trivial baseline is known for metric '{metric}'; "
                         "supply majority_class_rate or n_classes, or state the "
                         "baseline explicitly — an unanchored score is not "
                         "interpretable")}
    beats = score > floor
    return {
        "checked": True, "metric": metric, "score": score,
        "trivial_floor": round(floor, 6), "basis": basis,
        "beats_trivial": beats,
        "margin": round(score - floor, 6),
        "verdict": (f"{score} beats the trivial floor {round(floor, 4)} "
                    f"({basis}) by {round(score - floor, 4)}"
                    if beats else
                    f"{score} does NOT beat {round(floor, 4)} achievable by "
                    f"'{basis}': the model has demonstrated nothing on this "
                    "metric"),
    }


def multiple_comparison_note(configs_tried: int, *,
                             alpha: float = 0.05) -> dict:
    """The cost of picking the best of N configurations on the same data.

    Reported because hyperparameter search is a multiple-comparison procedure and
    is almost never treated as one: with enough configurations, the best score is
    partly a measure of how many were tried.
    """
    if configs_tried <= 1:
        return {"configs_tried": configs_tried,
                "note": "single configuration: no selection bias from search"}
    # Probability that at least one of N independent nulls clears alpha.
    family_wise = 1.0 - (1.0 - alpha) ** configs_tried
    return {
        "configs_tried": configs_tried,
        "alpha": alpha,
        "family_wise_error": round(family_wise, 4),
        "bonferroni_alpha": round(alpha / configs_tried, 6),
        "note": (f"{configs_tried} configurations were compared on the same data: "
                 f"the chance that at least one looks good by luck alone is "
                 f"~{family_wise * 100:.0f}%. The winner's score is optimistically "
                 "biased and must be re-measured on untouched data before being "
                 "reported"),
    }


# --------------------------------------------------------------------------
# Interpretation
# --------------------------------------------------------------------------

#: Causal language that a predictive model cannot support. Correlational findings
#: stated causally is the single most common misinterpretation of an ML result.
_CAUSAL_MARKERS = ("causes", "caused by", "leads to", "drives", "because of",
                   "results in", "due to", "the reason", "explains why",
                   "increases the", "reduces the", "impact of", "effect of")

_GENERALIZATION_MARKERS = ("in general", "always", "for any", "universally",
                           "in production", "in the real world", "all users",
                           "any dataset")


def check_interpretation(conclusion: str, *, is_causal_design: bool = False,
                         evaluated_on_holdout: bool = False,
                         population_described: bool = False) -> dict:
    """Whether a stated conclusion is supported by the design that produced it.

    Reuses the claim-strength classifier from `research.py` so an ML conclusion
    faces the same evidence bar as a published claim — the two are the same kind
    of assertion and splitting the standard would let the weaker one through.
    """
    from .research import classify_strength

    problems = []
    low = conclusion.lower()

    causal_used = [m for m in _CAUSAL_MARKERS if m in low]
    if causal_used and not is_causal_design:
        problems.append({
            "kind": "causal_from_predictive",
            "detail": f"causal language {causal_used[:3]} from a predictive "
                      "design: feature importance and correlation cannot "
                      "establish that changing the input changes the outcome",
            "fix": "restate as association ('is associated with', 'predicts'), "
                   "or run an intervention/quasi-experimental design"})

    general_used = [m for m in _GENERALIZATION_MARKERS if m in low]
    if general_used and not population_described:
        problems.append({
            "kind": "unbounded_generalization",
            "detail": f"generalizing language {general_used[:3]} without naming "
                      "the population the data came from",
            "fix": "state the population, time range, and collection method the "
                   "result holds for"})

    strength = classify_strength(conclusion)
    if strength in ("quantified", "absolute") and not evaluated_on_holdout:
        problems.append({
            "kind": "number_without_holdout",
            "detail": f"a {strength} claim whose number did not come from an "
                      "untouched holdout set is optimistically biased by however "
                      "much selection happened",
            "fix": "re-measure on data untouched during model or "
                   "hyperparameter selection"})

    return {
        "conclusion": conclusion[:300],
        "claim_strength": strength,
        "problems": problems,
        "supported": not problems,
        "verdict": ("the conclusion matches what the design can support"
                    if not problems else
                    f"{len(problems)} interpretation problem(s): the number may "
                    "be right while the sentence is wrong"),
    }


# --------------------------------------------------------------------------
# Combined gate
# --------------------------------------------------------------------------

def ml_gate(setup: ExperimentSetup, *,
            baseline_runs: Sequence[float] = (),
            candidate_runs: Sequence[float] = (),
            metric: str = "", score: float | None = None,
            majority_class_rate: float | None = None,
            n_classes: int | None = None,
            configs_tried: int = 1,
            conclusion: str = "",
            is_causal_design: bool = False,
            evaluated_on_holdout: bool = False,
            population_described: bool = False) -> dict:
    """Run every gate and produce one decision.

    Leakage is checked FIRST and blocks everything downstream: comparing two arms
    or interpreting a conclusion is meaningless when the scores do not estimate
    generalization. Reporting a statistical comparison alongside confirmed
    leakage would give a leaked result the appearance of rigor.
    """
    leakage = detect_leakage(setup)
    repro = check_reproducibility(setup)

    if leakage["blocks_result"]:
        return {
            "decision": "INVALID",
            "leakage": leakage,
            "reproducibility": repro,
            "comparison": {"skipped": True,
                           "why": "confirmed leakage makes the scores "
                                  "uninterpretable; fix the pipeline and "
                                  "re-measure before comparing anything"},
            "trivial_baseline": {"skipped": True, "why": "same"},
            "interpretation": {"skipped": True, "why": "same"},
            "verdict": leakage["verdict"],
        }

    comparison = (compare_runs(baseline_runs, candidate_runs)
                  if baseline_runs or candidate_runs
                  else {"skipped": True,
                        "why": "no runs supplied; a single number cannot be "
                               "compared to anything"})
    trivial = (trivial_baseline_check(metric, score,
                                      majority_class_rate=majority_class_rate,
                                      n_classes=n_classes)
               if metric and score is not None
               else {"skipped": True,
                     "why": "no metric/score supplied to anchor against a "
                            "trivial baseline"})
    multiplicity = multiple_comparison_note(configs_tried)
    interpretation = (check_interpretation(
        conclusion, is_causal_design=is_causal_design,
        evaluated_on_holdout=evaluated_on_holdout,
        population_described=population_described)
        if conclusion else {"skipped": True, "why": "no conclusion supplied"})

    blocking = []
    if trivial.get("checked") and not trivial.get("beats_trivial"):
        blocking.append("does not beat the trivial baseline")
    if comparison.get("comparable") is False and (baseline_runs or candidate_runs):
        blocking.append("too few runs to distinguish improvement from noise")
    if interpretation.get("problems"):
        blocking.append(f"{len(interpretation['problems'])} interpretation "
                        "problem(s)")

    if blocking:
        decision = "NOT_ESTABLISHED"
    elif repro["gap_count"] or leakage["suspected"]:
        decision = "PROVISIONAL"
    else:
        decision = "ESTABLISHED"

    return {
        "decision": decision,
        "leakage": leakage,
        "reproducibility": repro,
        "comparison": comparison,
        "trivial_baseline": trivial,
        "multiplicity": multiplicity,
        "interpretation": interpretation,
        "blocking": blocking,
        "verdict": (
            "result is established: no leakage, beats the trivial baseline, "
            "the gap exceeds run-to-run noise, and the conclusion matches the "
            "design" if decision == "ESTABLISHED" else
            f"result is PROVISIONAL: {repro['gap_count']} reproducibility gap(s) "
            f"and {leakage['suspected']} leakage suspicion(s) to close"
            if decision == "PROVISIONAL" else
            "result is NOT established: " + "; ".join(blocking)),
    }
