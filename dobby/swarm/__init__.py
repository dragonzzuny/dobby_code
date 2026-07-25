"""Decorrelated multi-agent ideation: protocols, diversity metrics, grounding gate."""

from .diversity import (COLLAPSE_MPD, SCATTER_MPD, DiversityReport, analyze,
                        coupling_ratio, effective_n, entropy_of_votes,
                        mean_pairwise_distance)
from .grounding import Evidence, Idea, IdeaAssessment, assess, explore_cycle, gate, has_prior_art
from .protocols import PROTOCOLS, Protocol, build_prompts, get, recommend
from .topologies import (FAN_OUT_IN, HIERARCHICAL, INDEPENDENT, MESH,
                         PIPELINE, SUPERVISOR, TeamPlan)
from .topologies import build as build_team
from .topologies import recommend as recommend_topology

__all__ = [
    "analyze", "DiversityReport", "effective_n", "mean_pairwise_distance",
    "coupling_ratio", "entropy_of_votes", "COLLAPSE_MPD", "SCATTER_MPD",
    "PROTOCOLS", "Protocol", "get", "recommend", "build_prompts",
    "Evidence", "Idea", "IdeaAssessment", "assess", "gate", "explore_cycle",
    "has_prior_art",
    "INDEPENDENT", "PIPELINE", "FAN_OUT_IN", "SUPERVISOR",
    "HIERARCHICAL", "MESH", "TeamPlan", "build_team",
    "recommend_topology",
]
