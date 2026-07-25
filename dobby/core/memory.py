"""Function-separated agent memory (file-backed, stdlib only).

Kinds: working / episodic / semantic / procedural / decision / negative.
Design sources (docs/RESEARCH_EVIDENCE_MATRIX.md): Reflexion (bounded episodic
lessons, cap on surfaced reflections), Generative Agents (recency+importance+
relevance scoring, lexical relevance substituted for embeddings), MemGPT
(explicit paging: small surfaced core, large on-disk archive), self-evolving-
agent surveys (pollution risk -> gate-before-persist, authority over recency).

Authority rule: a newer unverified item NEVER overrides an older verified one.
Supersession must be explicit (supersedes=<id>) and is only allowed when the
new item's verification level >= the old one's.
"""

from __future__ import annotations

import json
import os
import time
import uuid

from .kg import tokenize

KINDS = ("working", "episodic", "semantic", "procedural", "decision", "negative")
VERIFICATION_RANK = {"unverified": 0, "model_asserted": 0,
                     "strongly_supported": 1, "verified": 2}
SURFACE_CAP = {"episodic": 3, "negative": 3}   # Reflexion-style bound
DEFAULT_EXPIRY_DAYS = {"working": 2, "episodic": 90}  # others: no expiry


class MemoryError_(ValueError):
    pass


class MemoryStore:
    """One JSONL file per kind under <data_dir>/memory/."""

    def __init__(self, data_dir: str):
        self.root = os.path.join(data_dir, "memory")
        os.makedirs(self.root, exist_ok=True)

    def _path(self, kind: str) -> str:
        return os.path.join(self.root, f"{kind}.jsonl")

    def _load(self, kind: str) -> list[dict]:
        path = self._path(kind)
        items = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        items.append(json.loads(line))
        return items

    def _write_all(self, kind: str, items: list[dict]) -> None:
        tmp = self._path(kind) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        os.replace(tmp, self._path(kind))

    # -- write ---------------------------------------------------------------
    def add(self, kind: str, content: str, *, verification: str = "unverified",
            source: str = "model", authority: float = 0.5,
            supersedes: str | None = None, tags: list[str] | None = None,
            expires_days: int | None = None) -> dict:
        if kind not in KINDS:
            raise MemoryError_(f"unknown memory kind '{kind}'")
        if verification not in VERIFICATION_RANK:
            raise MemoryError_(f"unknown verification '{verification}'")
        items = self._load(kind)
        if supersedes:
            old = next((i for i in items if i["id"] == supersedes), None)
            if old is None:
                raise MemoryError_(f"supersedes target {supersedes} not found")
            if VERIFICATION_RANK[verification] < VERIFICATION_RANK[old["verification"]]:
                raise MemoryError_(
                    "authority rule: a lower-verification item cannot supersede "
                    f"a {old['verification']} item — verify first")
            old["superseded_by_pending"] = True
        exp = expires_days if expires_days is not None else DEFAULT_EXPIRY_DAYS.get(kind)
        item = {
            "id": uuid.uuid4().hex[:12], "kind": kind, "content": content,
            "verification": verification, "source": source,
            "authority": authority, "tags": tags or [],
            "created": time.time(), "created_date": time.strftime("%Y-%m-%d"),
            "supersedes": supersedes,
            "expires": (time.time() + exp * 86400) if exp else None,
            "superseded_by": None,
        }
        if supersedes:
            for it in items:
                if it["id"] == supersedes:
                    it["superseded_by"] = item["id"]
                    it.pop("superseded_by_pending", None)
        items.append(item)
        self._write_all(kind, items)
        return item

    # -- read ------------------------------------------------------------------
    def _alive(self, it: dict) -> bool:
        if it.get("superseded_by"):
            return False
        exp = it.get("expires")
        return not (exp and time.time() > exp)

    def recall(self, kind: str, query: str = "", k: int | None = None) -> list[dict]:
        """Score = relevance (lexical) + recency decay + authority; verified-first.

        Returns at most SURFACE_CAP[kind] (or k) items — the surfaced core;
        the archive stays on disk (MemGPT-style paging)."""
        items = [i for i in self._load(kind) if self._alive(i)]
        q = set(tokenize(query))
        now = time.time()

        def score(it):
            rel = len(q & set(tokenize(it["content"] + " " + " ".join(it["tags"])))) if q else 0
            rec = 0.995 ** ((now - it["created"]) / 3600.0)  # hourly decay
            return (VERIFICATION_RANK[it["verification"]], rel + rec + it["authority"])

        items.sort(key=score, reverse=True)
        cap = k if k is not None else SURFACE_CAP.get(kind, 5)
        return items[:cap]

    def get(self, kind: str, item_id: str) -> dict | None:
        return next((i for i in self._load(kind) if i["id"] == item_id), None)

    # -- maintenance -------------------------------------------------------------
    def compact(self, kind: str) -> dict:
        """Drop expired items; keep superseded ones (audit trail) but count them."""
        items = self._load(kind)
        kept = [i for i in items if not (i.get("expires") and time.time() > i["expires"])]
        self._write_all(kind, kept)
        return {"before": len(items), "after": len(kept)}

    def stats(self) -> dict:
        return {k: len([i for i in self._load(k) if self._alive(i)]) for k in KINDS}
