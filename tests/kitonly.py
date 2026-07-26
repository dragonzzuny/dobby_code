"""Is this the dobby DISTRIBUTION, or a project dobby was installed into?

The installer copies `tests/` into every host and tells the user, in its own final
message, to run `python -m unittest discover -s tests`. So every test that asserts
something about the distribution runs inside somebody's project, where it is false.

Measured in a freshly installed host before this existed: **3 failures and 3
errors** from six tests that describe the kit rather than the engine —

    test_ci_tools                 ImportError: no tools/ in a host
    test_origin_pin (x2)          no .gitattributes in a host
    test_search_driver (x1)       no .gitignore in a host
    test_self_kg (x2)             the kit's own knowledge graph and refresh script

None of that is a defect in the installed harness. It is the kit's own self-checks
firing in the wrong place, and a user's first act after installing is to see six
red lines. `test_install.py` had this guard from the start and it was never applied
anywhere else; putting the predicate in one importable place is what stops the
next test module repeating it.

WHAT MAKES A FILE KIT-ONLY

`install.sh` and `.gitignore` sit next to `tests/` in the distribution and are not
copied into a host — the installer excludes them. Their joint presence is
therefore a reliable marker, and it is the same one `test_install.py` has used
since it was written.
"""

from __future__ import annotations

import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The two files that exist only in the distribution.
_MARKERS = ("install.sh", ".gitignore")

IS_THE_KIT = all(os.path.exists(os.path.join(REPO, marker))
                 for marker in _MARKERS)

SKIP_REASON = (
    "not the dobby kit (no install.sh / .gitignore beside tests/) — this test "
    "describes the DISTRIBUTION, not an installed project, and the installer "
    "copies tests/ into every host")


def kit_only(target):
    """Decorator: skip this test or class outside the distribution.

    Use it on anything that reads `install.sh`, `.gitattributes`, `.gitignore`,
    `tools/`, `.github/`, or the kit's own knowledge graph.
    """
    return unittest.skipUnless(IS_THE_KIT, SKIP_REASON)(target)
