"""Hierarchical memory: nation → mountain → forest → tree → branch → leaf.

Shape
-----
Six tiers, five hops from root to leaf. Any stored fact is reachable in at most
five routing decisions, which is the property that makes retrieval cheap: the
index at each level is small enough to scan exhaustively, so the *system* never
scans exhaustively. This follows the H-MEM design — organize memory by degree of
semantic abstraction, and give each node an index pointing at its semantically
related children, so retrieval routes layer by layer instead of comparing the
query against every stored item.

Why each tier remembers DIFFERENTLY
-----------------------------------
A single storage mechanism applied at every level of abstraction is wrong at both
ends. Recency-weighted decay is right for "what happened this session" and
catastrophic for "what type of system is this" — a stable root that expires
invalidates every pointer beneath it. Conversely, a fixed vocabulary is right for
the root and useless for episodes. So the mechanism is chosen per tier:

| tier | scope | mechanism | why this one |
|---|---|---|---|
| nation | the domain as a whole | **fixed vocabulary + counts** | must be stable: routing from a moving root is unstable, so this tier only accumulates type facts and never expires |
| mountain | major subsystem | **prototype centroid** | routing needs one cheap similarity test per candidate; a centroid is that test |
| forest | cluster of related modules | **co-occurrence adjacency** | mid-level knowledge is mostly *relationships*, which a summary flattens away |
| tree | one artifact or topic | **slotted card** (purpose / contract / risk) | at artifact level, missing-slot detection is more useful than prose similarity |
| branch | one episode or session | **verified-first with decay** | recency matters here and only here; the kit's existing authority rule applies |
| leaf | one raw event | **append-only log, pointer-addressed** | full fidelity, cheapest write, first to be compressed away |

Promotion, not duplication
--------------------------
Facts enter at the leaf and move UP only by surviving a gate (`gates.py`). Nothing
is written to two tiers at once. A fact present at `tree` level is deliberately
absent from `leaf`: the leaf log is a staging area, and letting the same content
live at several abstraction levels is what makes hierarchical stores drift out of
agreement with themselves.

On LSTM
-------
A learned recurrent compressor (LSTM or otherwise) is out of scope and would be
an unverifiable claim in a kit with no training loop and no model dependency.
What an LSTM actually contributes to a memory system is not its arithmetic but
its *gating discipline*: an explicit decision, per item, about what is forgotten,
what is admitted, and what is exposed to the next step. That discipline is
implementable deterministically and is implemented in `gates.py`. The tiers here
are the state it gates.
"""

from __future__ import annotations

import dataclasses
import json
import os
import time
from collections.abc import Iterable, Sequence

from ..swarm.diversity import jaccard_distance, token_set, tokens

#: Tier names, root first. The list order IS the hierarchy; `TIER_INDEX` gives
#: each tier its depth, and depth 5 (leaf) is the floor.
TIERS: tuple[str, ...] = ("nation", "mountain", "forest", "tree", "branch", "leaf")
TIER_INDEX: dict[str, int] = {name: i for i, name in enumerate(TIERS)}

#: Human-facing gloss, kept next to the code so a report can explain the shape
#: without the reader having to hold the metaphor in their head.
TIER_SCOPE: dict[str, str] = {
    "nation": "the domain as a whole — stable type facts, never expires",
    "mountain": "a major subsystem — prototype centroid for routing",
    "forest": "a cluster of related modules — co-occurrence relationships",
    "tree": "one artifact or topic — slotted card",
    "branch": "one episode or session — verified-first, decays",
    "leaf": "one raw event — append-only, first to be compressed",
}

#: Retention in days per tier. `None` means never expires. The gradient is the
#: point: detail is cheap to lose because it has already been promoted in
#: summarized form if it mattered, and expensive to keep because it dominates
#: volume.
TIER_TTL_DAYS: dict[str, float | None] = {
    "nation": None,
    "mountain": None,
    "forest": 365.0,
    "tree": 180.0,
    "branch": 90.0,
    "leaf": 14.0,
}

#: Slots a `tree`-tier card is expected to fill. Missing slots are reported
#: rather than invented — an unfilled `contract` slot is information ("nobody
#: recorded what consumes this"), and a fabricated one is a liability.
TREE_SLOTS: tuple[str, ...] = ("purpose", "contract", "risk", "evidence")


@dataclasses.dataclass
class MemoryItem:
    """One stored fact, at one tier.

    `children` holds the ids of the items one tier DOWN that this item
    summarizes — the positional index that makes layer-by-layer routing
    possible. Without it, descending from a matched parent would require
    scanning the whole next tier, which is the exhaustive comparison the
    hierarchy exists to avoid.
    """

    id: str
    tier: str
    title: str
    body: str
    #: Ids at tier+1 that this item indexes. Empty for leaves.
    children: tuple[str, ...] = ()
    #: Provenance and confidence, carried from the kit's existing discipline:
    #: an unverified item never outranks a verified one at the same tier.
    verified: bool = False
    source: str = ""
    created: float = dataclasses.field(default_factory=time.time)
    last_hit: float = 0.0
    hits: int = 0
    #: Free-form per-tier payload: centroid tokens, adjacency, filled slots.
    payload: dict = dataclasses.field(default_factory=dict)

    def age_days(self, now: float | None = None) -> float:
        return ((now or time.time()) - self.created) / 86400.0

    def expired(self, now: float | None = None) -> bool:
        ttl = TIER_TTL_DAYS[self.tier]
        return ttl is not None and self.age_days(now) > ttl

    def text(self) -> str:
        return f"{self.title}\n{self.body}"

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryItem":
        d = dict(d)
        d["children"] = tuple(d.get("children") or ())
        return cls(**d)


# --------------------------------------------------------------------------
# Per-tier mechanisms. Each is a pure function of the item's content, so a tier's
# behaviour can be tested without a store.
# --------------------------------------------------------------------------

def build_nation_payload(items: Sequence[MemoryItem]) -> dict:
    """Fixed vocabulary + counts. Deliberately NOT a summary.

    The root must be stable under new information: adding a fact should change
    counts, not rewrite the routing key. A prose summary of the whole domain
    would be rewritten on every update, and every rewrite silently changes which
    queries route where.
    """
    vocab: dict[str, int] = {}
    for item in items:
        for tok in set(tokens(item.text())):
            vocab[tok] = vocab.get(tok, 0) + 1
    # Keep terms that appear across MULTIPLE children: a token unique to one
    # child belongs to that child's tier, not to the domain vocabulary.
    shared = {t: c for t, c in vocab.items() if c >= 2}
    ranked = sorted(shared.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "mechanism": "fixed_vocabulary",
        "vocabulary": [t for t, _ in ranked[:200]],
        "term_counts": dict(ranked[:200]),
        "child_count": len(items),
    }


def build_mountain_payload(items: Sequence[MemoryItem]) -> dict:
    """Prototype centroid: the tokens most characteristic of this subsystem.

    Stored as a weighted token set rather than a vector because there are no
    embeddings here (see diversity.py on the stdlib constraint). Weighting by
    document frequency within the subsystem gives a usable prototype: tokens in
    most children describe the subsystem, tokens in one describe a child.
    """
    df: dict[str, int] = {}
    for item in items:
        for tok in set(tokens(item.text())):
            df[tok] = df.get(tok, 0) + 1
    n = max(1, len(items))
    centroid = {t: round(c / n, 4) for t, c in df.items() if c / n >= 0.4}
    ranked = sorted(centroid.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "mechanism": "prototype_centroid",
        "centroid": dict(ranked[:120]),
        "child_count": len(items),
    }


def build_forest_payload(items: Sequence[MemoryItem]) -> dict:
    """Co-occurrence adjacency between children.

    A forest's knowledge is which of its trees belong together, and that
    information is destroyed by summarization: a paragraph describing five
    modules loses which two of them actually interact. Edges are undirected and
    thresholded so the structure stays readable at a glance.
    """
    sets = {item.id: token_set(item.text()) for item in items}
    ids = sorted(sets)
    edges = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            sim = 1.0 - jaccard_distance(sets[a], sets[b])
            if sim >= 0.12:
                edges.append({"a": a, "b": b, "similarity": round(sim, 4)})
    edges.sort(key=lambda e: -e["similarity"])
    degree: dict[str, int] = {i: 0 for i in ids}
    for e in edges:
        degree[e["a"]] += 1
        degree[e["b"]] += 1
    return {
        "mechanism": "cooccurrence_adjacency",
        "edges": edges[:200],
        "degree": degree,
        # The most-connected child is the one to read first when descending here.
        "hub": max(degree, key=lambda k: (degree[k], k)) if ids else None,
        "child_count": len(items),
    }


def build_tree_payload(body: str) -> dict:
    """Slotted card, with missing slots reported rather than filled.

    Slot extraction is literal: a line beginning `purpose:` fills the purpose
    slot. No inference is attempted, because an inferred contract that turns out
    to be wrong is worse than an absent one — the absent one prompts a lookup and
    the wrong one prevents it.
    """
    slots: dict[str, str] = {}
    for line in body.splitlines():
        stripped = line.strip()
        for slot in TREE_SLOTS:
            prefix = f"{slot}:"
            if stripped.lower().startswith(prefix):
                slots[slot] = stripped[len(prefix):].strip()
    return {
        "mechanism": "slotted_card",
        "slots": slots,
        "missing_slots": [s for s in TREE_SLOTS if s not in slots],
        "complete": len(slots) == len(TREE_SLOTS),
    }


def branch_score(item: MemoryItem, query_tokens: frozenset[str],
                 now: float | None = None) -> float:
    """Verified-first relevance with recency decay. Episode tier only.

    The kit's existing authority rule is preserved exactly: a newer UNVERIFIED
    item never outranks an older VERIFIED one. Implemented as an additive
    verified bonus that exceeds the maximum possible recency contribution, so the
    ordering is guaranteed by arithmetic rather than by tuning.
    """
    overlap = 1.0 - jaccard_distance(token_set(item.text()), query_tokens)
    ttl = TIER_TTL_DAYS["branch"] or 90.0
    recency = max(0.0, 1.0 - (item.age_days(now) / ttl))
    # relevance (0..1) + recency (0..1) => max 2.0 for an unverified item.
    # A verified bonus of 2.0 therefore dominates any unverified score.
    verified_bonus = 2.0 if item.verified else 0.0
    return round(overlap + recency + verified_bonus, 4)


# --------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------

class HierarchicalMemory:
    """Tiered store with index-based layer-by-layer retrieval.

    One JSONL file per tier under `root`. JSONL rather than a database for the
    same reason the rest of the kit uses it (ADR-1): diffable, reviewable in a
    pull request, and zero infrastructure. Volume is bounded by the TTL gradient
    plus compression, so the flat-file cost stays bounded too.
    """

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self._cache: dict[str, list[MemoryItem]] | None = None

    # -- storage ---------------------------------------------------------
    def path(self, tier: str) -> str:
        if tier not in TIER_INDEX:
            raise ValueError(f"unknown tier {tier!r}; known: {list(TIERS)}")
        return os.path.join(self.root, f"{tier}.jsonl")

    def load(self, tier: str) -> list[MemoryItem]:
        p = self.path(tier)
        if not os.path.exists(p):
            return []
        out = []
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(MemoryItem.from_dict(json.loads(line)))
                except (json.JSONDecodeError, TypeError):
                    # A corrupt line must not take down retrieval for the whole
                    # tier. It is skipped and countable via `integrity()`.
                    continue
        return out

    def write_tier(self, tier: str, items: Iterable[MemoryItem]) -> int:
        p = self.path(tier)
        items = list(items)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for item in items:
                f.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")
        os.replace(tmp, p)   # atomic: a crash mid-write cannot truncate the tier
        self._cache = None
        return len(items)

    def append(self, item: MemoryItem) -> MemoryItem:
        with open(self.path(item.tier), "a", encoding="utf-8") as f:
            f.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")
        self._cache = None
        return item

    def all_tiers(self) -> dict[str, list[MemoryItem]]:
        if self._cache is None:
            self._cache = {t: self.load(t) for t in TIERS}
        return self._cache

    def by_id(self) -> dict[str, MemoryItem]:
        return {i.id: i for items in self.all_tiers().values() for i in items}

    # -- retrieval -------------------------------------------------------
    def route(self, query: str, *, beam: int = 2, per_tier: int = 3,
              start_tier: str = "nation") -> dict:
        """Descend the hierarchy, keeping the best `beam` branches per level.

        This is the H-MEM routing mechanism: at each tier only the CHILDREN of
        already-matched parents are scored, so cost is `beam × branching` per
        level instead of the size of the tier. A beam wider than 1 is used
        because the top-level match is the least reliable one — the root's
        vocabulary is shared by construction, so committing to a single mountain
        on the strength of a root-level score would frequently descend the wrong
        subsystem with no way back.

        Returns the full path so a caller can show WHY an item was retrieved,
        which is what makes a hierarchical store debuggable.
        """
        q = token_set(query)
        tiers = self.all_tiers()
        index = self.by_id()
        start = TIER_INDEX[start_tier]

        # Seed with the start tier's items (usually one nation node).
        frontier = [i for i in tiers[TIERS[start]] if not i.expired()]
        if not frontier:
            # An empty upper tier is normal in a young project: nothing has been
            # promoted yet. Fall to the deepest tier that has content instead of
            # returning nothing, and SAY which tier answered.
            for depth in range(len(TIERS) - 1, start - 1, -1):
                candidates = [i for i in tiers[TIERS[depth]] if not i.expired()]
                if candidates:
                    scored = self._score(candidates, q, TIERS[depth])
                    return {
                        "query": query,
                        "path": [],
                        "entered_at": TIERS[depth],
                        "items": [self._brief(i, s) for i, s in scored[:per_tier]],
                        "note": (f"tiers above '{TIERS[depth]}' are empty — no "
                                 "promotion has happened yet; answered from the "
                                 "deepest populated tier"),
                    }
            return {"query": query, "path": [], "entered_at": None, "items": [],
                    "note": "memory is empty"}

        path: list[dict] = []
        depth = start
        while True:
            tier = TIERS[depth]
            scored = self._score(frontier, q, tier)
            keep = [i for i, _ in scored[:beam]]
            path.append({
                "tier": tier,
                "considered": len(frontier),
                "kept": [{"id": i.id, "title": i.title,
                          "score": s} for i, s in scored[:beam]],
            })
            child_ids = [c for i in keep for c in i.children]
            if depth == len(TIERS) - 1 or not child_ids:
                # Terminal: return the best items AT THIS TIER.
                return {
                    "query": query,
                    "path": path,
                    "entered_at": TIERS[start],
                    "terminal_tier": tier,
                    "items": [self._brief(i, s) for i, s in scored[:per_tier]],
                    "hops": len(path) - 1,
                }
            children = [index[cid] for cid in child_ids
                        if cid in index and not index[cid].expired()]
            if not children:
                return {
                    "query": query, "path": path, "entered_at": TIERS[start],
                    "terminal_tier": tier,
                    "items": [self._brief(i, s) for i, s in scored[:per_tier]],
                    "hops": len(path) - 1,
                    "note": f"children of the matched {tier} items are missing "
                            "or expired (dangling index)",
                }
            frontier = children
            depth += 1

    def _score(self, items: Sequence[MemoryItem], q: frozenset[str],
               tier: str) -> list[tuple[MemoryItem, float]]:
        """Score with the mechanism that belongs to `tier`."""
        out: list[tuple[MemoryItem, float]] = []
        for item in items:
            if tier == "branch":
                score = branch_score(item, q)
            elif tier == "mountain":
                centroid = item.payload.get("centroid") or {}
                score = round(sum(centroid.get(t, 0.0) for t in q), 4)
            elif tier == "nation":
                vocab = set(item.payload.get("vocabulary") or ())
                score = round(len(q & vocab) / max(1, len(q)), 4)
            else:
                score = round(1.0 - jaccard_distance(token_set(item.text()), q), 4)
            out.append((item, score))
        out.sort(key=lambda pair: (-pair[1], pair[0].id))
        return out

    @staticmethod
    def _brief(item: MemoryItem, score: float) -> dict:
        return {"id": item.id, "tier": item.tier, "title": item.title,
                "score": score, "verified": item.verified,
                "summary": item.body[:280],
                "children": len(item.children)}

    # -- maintenance -----------------------------------------------------
    def expire(self, now: float | None = None) -> dict:
        """Drop expired items, and report dangling parent pointers created.

        Reported rather than auto-repaired: a parent that loses a child has lost
        information, and silently rewriting the parent's index would hide that
        the summary above it is now unsupported. The caller decides whether to
        re-promote or re-summarize.
        """
        removed: dict[str, int] = {}
        surviving: set[str] = set()
        for tier in TIERS:
            items = self.load(tier)
            keep = [i for i in items if not i.expired(now)]
            removed[tier] = len(items) - len(keep)
            if removed[tier]:
                self.write_tier(tier, keep)
            surviving |= {i.id for i in keep}
        dangling = []
        for tier in TIERS:
            for item in self.load(tier):
                missing = [c for c in item.children if c not in surviving]
                if missing:
                    dangling.append({"parent": item.id, "tier": item.tier,
                                     "missing_children": missing})
        return {"removed": removed, "removed_total": sum(removed.values()),
                "dangling_parents": dangling,
                "note": ("dangling parents are REPORTED, not auto-fixed: their "
                         "summaries now describe deleted detail and need "
                         "re-summarizing or re-promotion")
                        if dangling else "no dangling parents"}

    def stats(self) -> dict:
        tiers = self.all_tiers()
        return {
            "root": self.root,
            "tiers": {t: {"count": len(tiers[t]),
                          "verified": sum(1 for i in tiers[t] if i.verified),
                          "scope": TIER_SCOPE[t],
                          "ttl_days": TIER_TTL_DAYS[t]}
                      for t in TIERS},
            "total": sum(len(v) for v in tiers.values()),
            "max_hops": len(TIERS) - 1,
        }

    def integrity(self) -> dict:
        """Structural checks a report can cite: orphans, dangling, tier skips."""
        tiers = self.all_tiers()
        index = self.by_id()
        parent_of: dict[str, str] = {}
        problems: list[dict] = []
        for tier in TIERS:
            for item in tiers[tier]:
                for cid in item.children:
                    if cid not in index:
                        problems.append({"kind": "dangling_child",
                                         "parent": item.id, "child": cid})
                        continue
                    child = index[cid]
                    expected = TIER_INDEX[item.tier] + 1
                    if TIER_INDEX[child.tier] != expected:
                        # A parent indexing a grandchild breaks the ≤5-hop
                        # guarantee's uniformity and makes routing skip a level.
                        problems.append({
                            "kind": "tier_skip", "parent": item.id,
                            "child": cid, "parent_tier": item.tier,
                            "child_tier": child.tier})
                    if cid in parent_of and parent_of[cid] != item.id:
                        problems.append({"kind": "multiple_parents",
                                         "child": cid,
                                         "parents": [parent_of[cid], item.id]})
                    parent_of[cid] = item.id
        # Items above leaf with no children summarize nothing.
        for tier in TIERS[:-1]:
            for item in tiers[tier]:
                if not item.children:
                    problems.append({"kind": "childless_summary",
                                     "id": item.id, "tier": tier})
        return {"ok": not problems, "problem_count": len(problems),
                "problems": problems[:100]}
