"""Model-agnostic agent harness engine.

Engine code is repo-agnostic; all repository-specific knowledge lives under
the data directory (default: .dobby/ at the repo root). See
docs/HARNESS_V2_ARCHITECTURE.md.
"""

__version__ = "2.0.0"

import os


def data_dir(repo_root: str) -> str:
    return os.path.join(repo_root, ".dobby")
