"""Running a provider in a role that must not write, and finding out if it did.

WHY A ROUTING RULE IS NOT ENOUGH

`catalog.READ_ONLY_ROLES` removes the provider this repository has MEASURED
writing under the default argv. That is the right first move and it is still only
a list, which makes it a claim of exactly the kind invariant 3 is about: the
config says this provider does not write, and the tree is what actually knows.
Every provider on that list other than the text-only ones is `RO_CLAIMED` — the
vendor documents a read-only mode and nobody here has tried to break it. `agy`
was documented that way too, right up until somebody ran the probe.

So this fingerprints the working tree either side of the call. If the tree moved,
the run is a failure regardless of how good the returned plan looks, because a
plan produced by a process that also edited the repository is not a plan — it is
an edit with a document attached, and the whole architecture boundary
(`project/architecture.py`) rests on those being different things.

WHY A MUTATION IS A HARD STOP AND NOT A WARNING

A warning here would be read once and then never again. More to the point, the
caller cannot recover: the tree it was about to plan against is not the tree it
measured its baseline on, so PK-4 would refuse the next session anyway — later,
and attributed to the wrong change. Failing at the call site attributes it to the
provider that did it, while the operator still knows what ran.

WHAT IT DOES NOT DO

It does not PREVENT the write. Nothing in this process can: the provider is a
separate program with the user's own filesystem permissions, and `cwd` plus the
prompt are the only levers this harness has. Detection is the honest ceiling, and
saying so is better than a `contain()` function that would imply otherwise. Real
prevention needs an OS-level sandbox or a throwaway clone, and neither exists
here yet.

A false positive is possible: anything else editing the tree during the call —
another agent, an editor autosave, a build — moves the fingerprint too. That
direction is deliberate. The failure mode of a missed mutation is a corrupted
baseline nobody notices; the failure mode of a false alarm is one halted call.
"""

from __future__ import annotations

from .init import repo_digest
from .models import ProjectError


class ReadOnlyViolation(ProjectError):
    """A provider invoked in a read-only role changed the working tree."""


def fingerprint(root: str) -> str:
    """What the tree looks like right now.

    `repo_digest` already answers this and already handles both cases: inside git
    it is HEAD plus the porcelain status, so an uncommitted edit moves it, and
    `.dobby/state/` is gitignored so the harness's own request record does not.
    Outside git it is a bounded walk of paths and sizes. Reused rather than
    reimplemented, because a second definition of "the tree changed" would
    eventually disagree with the one `session.py` enforces PK-4 with.
    """
    return repo_digest(root)


def run_read_only(spec, prompt: str, *, root: str, timeout_s: int | None = None,
                  runner=None, role: str = "read-only"):
    """Invoke `spec` and refuse the result if the tree moved underneath it.

    `runner` is the seam, defaulting to `providers.run.run_provider`. Injecting
    it is how the violation path is tested without a provider installed — the
    same reason `architecture.request_architecture` takes `propose`.
    """
    if runner is None:
        from ..providers.run import run_provider as runner

    before = fingerprint(root)
    result = runner(spec, prompt, cwd=root, timeout_s=timeout_s)
    after = fingerprint(root)

    if before != after:
        raise ReadOnlyViolation(
            f"{getattr(spec, 'id', spec)!r} was invoked in the {role!r} role, "
            f"which may not write, and {root} changed while it ran "
            f"({before[:12]} -> {after[:12]}). The result is discarded: a "
            f"document produced by a process that also edited the repository is "
            f"not a plan. Inspect the tree (`git status`) before running "
            f"anything else, and if the provider did this, record it against "
            f"`read_only_default` in providers/catalog.py — that field exists "
            f"because one provider already did")
    return result
