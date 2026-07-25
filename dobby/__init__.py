"""dobby — a portable agent harness.

Layout mirrors the split the architecture depends on:

- `dobby.core`      the proven repo-agnostic engine (knowledge graph, router,
                    policies, skills, evaluator, trajectory, optimizer,
                    improvement loop, cross-project evolution)
- `dobby.providers` multi-provider subprocess fleet + parallel fan-out
- `dobby.memory`    six-tier hierarchical memory and its gates
- `dobby.swarm`     decorrelated ideation: protocols, diversity, grounding
- `dobby.specialize` generalist → domain-expert gradient with a dual gate
- `dobby.research`  search planning and claim/citation verification
- `dobby.design`    DESIGN.md tokens and validation

Project DATA lives outside the package in `.dobby/`, so upgrading the engine
never rewrites a host project's knowledge.
"""

__version__ = "0.1.0"

DATA_DIRNAME = ".dobby"


def data_dir(repo_root: str = ".") -> str:
    import os
    return os.path.join(repo_root, DATA_DIRNAME)
