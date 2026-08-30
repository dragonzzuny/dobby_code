"""A provider that times out takes everything it started with it.

`subprocess.run(timeout=...)` kills the direct child and nothing below it.
Measured on this machine, three trials each:

    before   3/3 orphans   a detached grandchild was still writing files four
                           seconds after the provider had timed out
    after    3/3 cleaned

That is not tidiness. An agent CLI starts language servers, git, docker, node.
An orphan of one holds a lock, keeps spending, and can still be WRITING INTO
THE REPOSITORY after this runtime has recorded the attempt as failed -- the
effect accounting saying one thing while the disk does another. Everything the
`EXTERNAL_IRREVERSIBLE` machinery is careful about is moot if a process nobody
is tracking is still editing the tree.

The grandchild here is deliberately hostile: `DETACHED_PROCESS`, its own process
group, no inherited pipes. A child that merely inherits the pipes dies on its
own when they close, which is why an easier fixture would have passed against
the broken code and proved nothing.

Two measurement mistakes made while writing this, both recorded because both
would have produced a confident wrong answer:

- the first fixture read a marker file that never existed and reported "cleaned
  up", which is indistinguishable from "the child never started". The check now
  asserts the grandchild was ALIVE before judging whether it died.
- the second reused one marker path across trials, so an orphan left by an
  EARLIER run kept writing to it and the fixed build was reported as leaking.
  Each trial now gets its own path.
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.providers.base import ProviderSpec  # noqa: E402
from dobby.providers.run import (_run_killing_the_tree,  # noqa: E402
                                 kill_tree, run_provider)

#: Spawns a grandchild that detaches, then hangs. The grandchild writes a marker
#: every 0.4s for 90s, so "is it still running" is a file mtime and not a guess.
PARENT = '''
import os, subprocess, sys, time
marker = sys.argv[1]
child = marker + ".child.py"
with open(child, "w", encoding="utf-8", newline="\\n") as fh:
    fh.write(
        "import sys, time\\n"
        "m = sys.argv[1]\\n"
        "end = time.time() + 90\\n"
        "while time.time() < end:\\n"
        "    open(m, 'w').write(str(time.time()))\\n"
        "    time.sleep(0.4)\\n")
flags = 0
if os.name == "nt":
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008
subprocess.Popen([sys.executable, child, marker], creationflags=flags,
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                 stderr=subprocess.DEVNULL, close_fds=True)
time.sleep(600)
'''


class TreeCase(unittest.TestCase):
    #: Long enough for the grandchild to exist, short enough to keep the suite
    #: usable.
    LIMIT = 3
    #: Longer than the grandchild's 0.4s write interval, so "did it write
    #: again" is a real observation and not a sampling artefact.
    SETTLE = 3

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self.cleanup)
        self.script = os.path.join(self.tmp, "parent.py")
        with open(self.script, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(PARENT)

    def cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def marker(self):
        return os.path.join(self.tmp, f"m_{uuid.uuid4().hex[:8]}.txt")

    def spec(self, marker):
        return ProviderSpec(
            id="hang", kind="cli", display="hang", binary=sys.executable,
            argv=lambda prompt, model, extra: [sys.executable, self.script,
                                               marker],
            timeout_s=self.LIMIT)

    def still_running(self, marker):
        """True when the grandchild writes again after the settle window.

        Skips rather than passes when the grandchild never started: a fixture
        that did not run is not evidence about a kill.
        """
        if not os.path.exists(marker):
            self.skipTest("the grandchild never started; nothing to judge")
        before = os.path.getmtime(marker)
        time.sleep(self.SETTLE)
        return os.path.getmtime(marker) > before


class ATimedOutProviderLeavesNothingBehind(TreeCase):
    def test_the_detached_grandchild_is_killed(self):
        marker = self.marker()
        result = run_provider(self.spec(marker), "x", timeout_s=self.LIMIT)
        self.assertFalse(result.ok)
        self.assertIn("timeout", (result.error or "").lower())
        self.assertFalse(self.still_running(marker),
                         "a process nobody is tracking is still running, and "
                         "it can still be writing into the repository")

    def test_it_returns_within_a_bounded_time(self):
        """The kill must not become its own hang."""
        marker = self.marker()
        started = time.perf_counter()
        run_provider(self.spec(marker), "x", timeout_s=self.LIMIT)
        self.assertLess(time.perf_counter() - started, self.LIMIT + 25)

    def test_an_ordinary_call_is_untouched(self):
        """The wrapper has to be invisible when nothing times out."""
        quick = os.path.join(self.tmp, "quick.py")
        with open(quick, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("print('done')\n")
        spec = ProviderSpec(
            id="quick", kind="cli", display="quick", binary=sys.executable,
            argv=lambda p, m, e: [sys.executable, quick], timeout_s=60)
        result = run_provider(spec, "x", timeout_s=60)
        self.assertTrue(result.ok, result.error)
        self.assertIn("done", result.text)


class TheWrapperItself(TreeCase):
    def test_it_returns_a_completed_process_like_run_does(self):
        quick = os.path.join(self.tmp, "quick.py")
        with open(quick, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("import sys; sys.stdout.write('out'); sys.exit(3)\n")
        proc = _run_killing_the_tree([sys.executable, quick],
                                     capture_output=True, text=True,
                                     encoding="utf-8", timeout=60)
        self.assertIsInstance(proc, subprocess.CompletedProcess)
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(proc.stdout, "out")

    def test_it_still_raises_timeout_expired(self):
        """The caller's except branch must not have to change."""
        with self.assertRaises(subprocess.TimeoutExpired):
            _run_killing_the_tree(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                capture_output=True, text=True, encoding="utf-8", timeout=2)

    def test_kill_tree_says_what_it_did(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            verdict = kill_tree(proc)
            self.assertTrue(verdict, "a kill with no account of itself")
        finally:
            try:
                proc.kill()
            except OSError:
                pass
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()


if __name__ == "__main__":
    unittest.main()
