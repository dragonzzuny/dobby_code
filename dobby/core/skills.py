"""Skill registry + lifecycle with gate-before-persist promotion.

Lifecycle: candidate -> sandboxed -> evaluated -> approved -> active
           -> monitored -> deprecated | revised
Promotion gates (Voyager-style verification before a skill enters the
library; anti Skill-Pollution): a skill may NOT be promoted on the
proposing agent's own judgment — it needs distinct eval evidence and,
for approved/active, a non-self approver.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time

STATES = ("candidate", "sandboxed", "evaluated", "approved", "active",
          "monitored", "deprecated", "revised")
TRANSITIONS = {
    "candidate": {"sandboxed", "deprecated"},
    "sandboxed": {"evaluated", "deprecated"},
    "evaluated": {"approved", "candidate", "deprecated"},
    "approved": {"active", "deprecated"},
    "active": {"monitored", "revised", "deprecated"},
    "monitored": {"active", "revised", "deprecated"},
    "revised": {"candidate"},
}
# minimum distinct passing eval scenarios required to ENTER a state
EVIDENCE_FLOOR = {"evaluated": 1, "approved": 2, "active": 2}

REQUIRED_METADATA = ("name", "description", "applicable_when", "not_applicable_when",
                     "inputs", "outputs", "validation_commands", "version", "provenance")


class SkillError(ValueError):
    pass


def check_requires(req: dict) -> tuple[bool, str]:
    """Load-time environment gating (adopted from OpenClaw's
    metadata.requires): a skill whose runtime requirements are not met never
    reaches the router, instead of failing mid-procedure."""
    for b in req.get("bins", []):
        if not shutil.which(b):
            return False, f"missing binary: {b}"
    any_bins = req.get("any_bins", [])
    if any_bins and not any(shutil.which(b) for b in any_bins):
        return False, f"none of the alternative binaries present: {any_bins}"
    for e in req.get("env", []):
        if e not in os.environ:
            return False, f"missing env var: {e}"
    oss = req.get("os", [])
    if oss and sys.platform not in oss:
        return False, f"os '{sys.platform}' not in {oss}"
    return True, "ok"


def _content_digest(path: str) -> str:
    """sha256 of a skill body with line endings NORMALIZED.

    Hashing raw bytes made the tamper check fail on every clean checkout. git
    rewrites line endings on the way out - core.autocrlf=true is the Windows
    default - so a body pinned at 3325 bytes of LF arrives as 3381 bytes of CRLF
    and the digest differs. Measured on an untouched file: pin 13336a69, fresh
    clone 1beccd11. Every CI run of this repository was red for this reason.

    A control that reports tampering after a normal `git clone` is worse than no
    control: it trains everyone to ignore it, and the one real tamper then looks
    like the usual noise.

    Normalizing CRLF and lone CR to LF hashes the CONTENT, which is what the pin
    is for. An injected step, a rewritten command, a changed path all change
    content. A change that is only line endings is precisely the change nobody
    needs to be warned about.
    """
    with open(path, "rb") as handle:
        raw = handle.read()
    normalized = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(normalized).hexdigest()


class SkillRegistry:
    def __init__(self, path: str):
        self.path = path
        self.skills: dict[str, dict] = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.skills = {s["name"]: s for s in json.load(f)["skills"]}

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"skills": list(self.skills.values())}, f,
                      ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # -- lifecycle -----------------------------------------------------------
    def register_candidate(self, meta: dict, proposed_by: str) -> dict:
        for k in REQUIRED_METADATA:
            if k not in meta:
                raise SkillError(f"skill metadata missing '{k}'")
        if meta["name"] in self.skills:
            raise SkillError(f"skill '{meta['name']}' exists — use revise()")
        skill = dict(meta)
        skill.update({
            "state": "candidate", "proposed_by": proposed_by,
            "eval_passes": [], "approved_by": None,
            "created": time.strftime("%Y-%m-%d"),
            "history": [{"to": "candidate", "by": proposed_by,
                         "date": time.strftime("%Y-%m-%d")}],
        })
        self.skills[meta["name"]] = skill
        return skill

    def record_eval_pass(self, name: str, scenario_id: str, evidence_path: str) -> None:
        s = self._get(name)
        s["eval_passes"].append({"scenario": scenario_id, "evidence": evidence_path,
                                 "date": time.strftime("%Y-%m-%d")})

    def transition(self, name: str, to_state: str, by: str) -> dict:
        s = self._get(name)
        cur = s["state"]
        if to_state not in TRANSITIONS.get(cur, set()):
            raise SkillError(f"illegal transition {cur} -> {to_state}")
        floor = EVIDENCE_FLOOR.get(to_state, 0)
        distinct = {p["scenario"] for p in s["eval_passes"]}
        if len(distinct) < floor:
            raise SkillError(
                f"gate: '{to_state}' needs >= {floor} distinct passing scenarios, "
                f"have {len(distinct)} — anti single-example promotion")
        if to_state in ("approved", "active") and by == s["proposed_by"]:
            raise SkillError(
                "gate: proposer cannot approve their own skill (anti self-judgment)")
        if to_state == "approved":
            s["approved_by"] = by
        s["state"] = to_state
        s["history"].append({"to": to_state, "by": by,
                             "date": time.strftime("%Y-%m-%d")})
        return s

    def revise(self, name: str, new_meta: dict, by: str) -> dict:
        s = self._get(name)
        if s["state"] not in ("active", "monitored"):
            raise SkillError("only active/monitored skills can be revised")
        self.transition(name, "revised", by)
        old_version = s["version"]
        s.update(new_meta)
        s["version"] = new_meta.get("version", old_version)
        s["state"] = "candidate"
        s["eval_passes"] = []      # revision resets evidence
        s["history"].append({"to": "candidate", "by": by, "note": "post-revision",
                             "date": time.strftime("%Y-%m-%d")})
        return s

    # -- discovery (progressive disclosure) ------------------------------------
    def eligible(self, name: str) -> tuple[bool, str]:
        return check_requires(self._get(name).get("requires", {}))

    def index(self, states: tuple = ("active", "monitored"),
              runtime_gate: bool = True) -> list[dict]:
        """Level 1: names + one-line descriptions only. Skills whose runtime
        requirements are unmet are filtered out (runtime_gate=False to see
        them anyway, e.g. for diagnostics)."""
        out = []
        for s in self.skills.values():
            if s["state"] not in states:
                continue
            if runtime_gate and not check_requires(s.get("requires", {}))[0]:
                continue
            out.append({"name": s["name"], "description": s["description"]})
        return out

    def snapshot(self) -> dict:
        """Session-start skill snapshot (OpenClaw semantics): freeze the
        eligible-skill set once per session so mid-session registry edits
        cannot change behavior silently. Callers store and reuse this."""
        return {"taken": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "skills": self.index()}

    # -- provenance pinning (ClawHub origin.json analogue) ---------------------
    def pin_origin(self, name: str, repo_root: str = ".") -> dict:
        """Record a content hash of the skill's body file so later tampering
        (or an unreviewed self-improvement edit) is detectable."""
        s = self._get(name)
        body = os.path.join(repo_root, s.get("path") or "")
        if not os.path.exists(body):
            raise SkillError(f"cannot pin '{name}': body not found at {body}")
        h = _content_digest(body)
        s["origin"] = {"pinned_sha256": h, "path": s["path"],
                       "date": time.strftime("%Y-%m-%d")}
        return s["origin"]

    def verify_origin(self, name: str, repo_root: str = ".") -> tuple[bool, str]:
        s = self._get(name)
        origin = s.get("origin")
        if not origin:
            return False, "no pinned origin (pin_origin first)"
        body = os.path.join(repo_root, s.get("path") or "")
        if not os.path.exists(body):
            return False, f"body missing: {body}"
        h = _content_digest(body)
        if h != origin["pinned_sha256"]:
            return False, ("body hash differs from pinned origin — review the "
                           "change, then re-pin via the lifecycle (revise)")
        return True, "ok"

    def signature(self, name: str) -> dict:
        """Level 2: applicability + IO contract, still no full body."""
        s = self._get(name)
        return {k: s[k] for k in ("name", "description", "applicable_when",
                                  "not_applicable_when", "inputs", "outputs",
                                  "validation_commands", "version", "state")}

    def body_path(self, name: str) -> str | None:
        """Level 3: path to full SKILL.md — caller reads it only when executing."""
        return self._get(name).get("path")

    def _get(self, name: str) -> dict:
        if name not in self.skills:
            raise SkillError(f"unknown skill '{name}'")
        return self.skills[name]
