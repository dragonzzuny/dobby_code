"""Knowledge graph with ontology validation, provenance, and hybrid retrieval.

Storage: one JSON file (human-diffable, no external deps). Retrieval combines
lexical overlap, 1-hop graph expansion, authority, recency, and a provenance
penalty for unverified assertions. The scoring weights are external config —
they are the search space of dobby/core/optimizer.py.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass, field, asdict

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_가-힣]+")


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


DEFAULT_WEIGHTS = {
    "lexical": 1.0,       # token-overlap score
    "graph": 0.5,         # 1-hop neighbor propagation factor
    "authority": 0.3,     # node authority boost
    "recency": 0.1,       # newer nodes boost (mild)
    "unverified_penalty": 0.5,  # multiplier subtracted for non-verified provenance
    "keyword_bonus": 1.0,  # exact keyword-field hits
}


class OntologyError(ValueError):
    pass


class Ontology:
    def __init__(self, spec: dict):
        self.spec = spec
        self.node_types = set(spec["node_types"])
        self.edge_types = set(spec["edge_types"])
        self.confidence_levels = set(spec["confidence_levels"])
        self.prov_fields = list(spec["provenance_required_fields"])
        self.prov_methods = set(spec["provenance_methods"])

    @classmethod
    def load(cls, path: str) -> "Ontology":
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f))

    def validate_provenance(self, prov: dict, where: str) -> None:
        if not isinstance(prov, dict):
            raise OntologyError(f"{where}: provenance missing")
        for k in self.prov_fields:
            if k not in prov:
                raise OntologyError(f"{where}: provenance lacks required field '{k}'")
        if prov["confidence"] not in self.confidence_levels:
            raise OntologyError(f"{where}: unknown confidence '{prov['confidence']}'")
        if prov["method"] not in self.prov_methods:
            raise OntologyError(f"{where}: unknown provenance method '{prov['method']}'")
        if prov["method"] == "model_assertion" and prov["confidence"] == "verified":
            raise OntologyError(f"{where}: model_assertion can never be 'verified'")

    def validate_node(self, node: dict) -> None:
        nid = node.get("id", "<no id>")
        for k in ("id", "type", "name"):
            if not node.get(k):
                raise OntologyError(f"node {nid}: missing '{k}'")
        if node["type"] not in self.node_types:
            raise OntologyError(f"node {nid}: unknown type '{node['type']}'")
        self.validate_provenance(node.get("provenance"), f"node {nid}")

    def validate_edge(self, edge: dict, node_ids: set) -> None:
        eid = f"{edge.get('src')}-{edge.get('rel')}->{edge.get('dst')}"
        if edge.get("rel") not in self.edge_types:
            raise OntologyError(f"edge {eid}: unknown relation")
        for end in ("src", "dst"):
            if edge.get(end) not in node_ids:
                raise OntologyError(f"edge {eid}: {end} not a known node")
        self.validate_provenance(edge.get("provenance"), f"edge {eid}")


@dataclass
class Hit:
    node: dict
    score: float
    why: list[str] = field(default_factory=list)


class KnowledgeGraph:
    def __init__(self, ontology: Ontology, nodes: list[dict] | None = None,
                 edges: list[dict] | None = None):
        self.ontology = ontology
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self._adj: dict[str, list[tuple[str, str]]] = {}
        for n in nodes or []:
            self.add_node(n)
        for e in edges or []:
            self.add_edge(e)

    # -- mutation ----------------------------------------------------------
    def add_node(self, node: dict) -> None:
        self.ontology.validate_node(node)
        node.setdefault("keywords", [])
        node.setdefault("authority", 0.5)
        node.setdefault("created", time.strftime("%Y-%m-%d"))
        self.nodes[node["id"]] = node

    def add_edge(self, edge: dict) -> None:
        self.ontology.validate_edge(edge, set(self.nodes))
        self.edges.append(edge)
        self._adj.setdefault(edge["src"], []).append((edge["rel"], edge["dst"]))
        self._adj.setdefault(edge["dst"], []).append((edge["rel"] + "_of", edge["src"]))

    # -- persistence -------------------------------------------------------
    @classmethod
    def load(cls, ontology: Ontology, path: str) -> "KnowledgeGraph":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return cls(ontology, data.get("nodes", []), data.get("edges", []))

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"nodes": list(self.nodes.values()), "edges": self.edges},
                      f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    # -- queries -----------------------------------------------------------
    def neighbors(self, node_id: str) -> list[tuple[str, str]]:
        return self._adj.get(node_id, [])

    def contradictions(self) -> list[dict]:
        return [e for e in self.edges if e["rel"] == "contradicts"]

    def _lexical_score(self, qtokens: set, node: dict) -> tuple[float, float]:
        text_tokens = set(tokenize(node["name"])) | set(tokenize(node.get("summary", "")))
        kw_tokens = set()
        for kw in node.get("keywords", []):
            kw_tokens |= set(tokenize(kw))
        overlap = len(qtokens & text_tokens)
        kw_overlap = len(qtokens & kw_tokens)
        denom = math.sqrt(len(text_tokens) + 1)
        return overlap / denom, float(kw_overlap)

    def retrieve(self, query: str, weights: dict | None = None, k: int = 10,
                 type_filter: set | None = None) -> list[Hit]:
        """Hybrid retrieval. Deterministic given (query, weights, graph)."""
        w = dict(DEFAULT_WEIGHTS)
        w.update(weights or {})
        qtokens = set(tokenize(query))
        base: dict[str, Hit] = {}
        for nid, node in self.nodes.items():
            lex, kw = self._lexical_score(qtokens, node)
            score = w["lexical"] * lex + w["keyword_bonus"] * kw
            why = []
            if lex:
                why.append(f"lexical={lex:.2f}")
            if kw:
                why.append(f"keywords={kw:.0f}")
            if score > 0:
                base[nid] = Hit(node, score, why)
        # 1-hop graph expansion from the strongest seeds
        seeds = sorted(base.values(), key=lambda h: -h.score)[:k]
        for hit in seeds:
            for rel, nb in self.neighbors(hit.node["id"]):
                nbn = self.nodes.get(nb)
                if nbn is None:
                    continue
                bonus = w["graph"] * hit.score * 0.5
                if nb in base:
                    base[nb].score += bonus
                    base[nb].why.append(f"graph:{rel}<-{hit.node['id']}")
                else:
                    base[nb] = Hit(nbn, bonus, [f"graph:{rel}<-{hit.node['id']}"])
        # authority / recency / provenance adjustments
        for hit in base.values():
            node = hit.node
            hit.score += w["authority"] * float(node.get("authority", 0.5))
            year = str(node.get("created", ""))[:4]
            if year.isdigit():
                hit.score += w["recency"] * max(0.0, (int(year) - 2020) / 10.0)
            conf = node["provenance"]["confidence"]
            if conf not in ("verified", "strongly_supported"):
                hit.score *= max(0.0, 1.0 - w["unverified_penalty"])
                hit.why.append(f"penalty:{conf}")
        hits = [h for h in base.values()
                if not type_filter or h.node["type"] in type_filter]
        hits.sort(key=lambda h: (-h.score, h.node["id"]))
        return hits[:k]

    def context_pack(self, query: str, weights: dict | None = None, k: int = 8,
                     token_budget: int = 2000) -> dict:
        """Progressive-disclosure bundle: names+summaries first, bodies on demand.

        Returns a dict a caller can render into a prompt. Stays under
        token_budget (approximated as chars/4)."""
        hits = self.retrieve(query, weights, k=k * 2)
        items, spent = [], 0
        for h in hits:
            entry = {
                "id": h.node["id"], "type": h.node["type"], "name": h.node["name"],
                "summary": h.node.get("summary", ""),
                "path": h.node.get("path"),
                "confidence": h.node["provenance"]["confidence"],
                "score": round(h.score, 3),
            }
            cost = len(json.dumps(entry, ensure_ascii=False)) // 4
            if spent + cost > token_budget or len(items) >= k:
                break
            items.append(entry)
            spent += cost
        return {"query": query, "items": items, "approx_tokens": spent,
                "note": "summaries only; fetch full body via path / get_capability"}
