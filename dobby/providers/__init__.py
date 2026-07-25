"""Multi-provider fleet: detect, invoke, and fan out across agent runners."""

from .base import (CAPABILITIES, COST_TIERS, ProviderError, ProviderRegistry,
                   ProviderResult, ProviderSpec)
from .catalog import CATALOG, ROLE_ROUTING, registry, role_preference
from .detect import ABSENT, AVAILABLE, BLOCKED, report, resolve_panel, resolve_role, survey
from .fanout import AgentTask, FanoutRound, broadcast, run_round
from .run import probe, run_by_id, run_provider

__all__ = [
    "CAPABILITIES", "COST_TIERS", "ProviderError", "ProviderRegistry",
    "ProviderResult", "ProviderSpec", "CATALOG", "ROLE_ROUTING", "registry",
    "role_preference", "AVAILABLE", "ABSENT", "BLOCKED", "survey", "report",
    "resolve_role", "resolve_panel", "AgentTask", "FanoutRound", "run_round",
    "broadcast", "run_provider", "run_by_id", "probe",
]
