# Concurrency stress

Three defects were found here that no single pass of the test suite could find,
because each existed only at a particular interleaving:

| defect | how it showed |
|---|---|
| a stale worker wrote READY over a live lease, and both ran the node | 4 of 6 runs under load, 0 of 6 alone |
| every survivor of a killed worker closed the same interrupted attempt | 5 of 8 kill rounds |
| a worker attaching to a run another had just finished | 1 in 48 rounds |

`tests/test_stress_smoke.py` runs one round of each shape inside the ordinary
suite so this harness cannot rot. **A green suite is not evidence of a
concurrency-clean build**; the evidence is a run of this.

## Running it

```
python evals/stress/concurrency.py --rounds 14 --workers 5 --load 6 --scenario all
```

Exit code 1 when any invariant broke, with the failing rounds named.

| flag | what it changes |
|---|---|
| `--rounds` | rounds per scenario |
| `--workers` | OS processes racing one run |
| `--load` | busy processes alongside — the defects above needed this |
| `--parallel` | `max_parallel` inside each worker |
| `--hold` | seconds each node holds, to widen the overlap |
| `--scenario` | `contend`, `diamond`, `kill`, `effect`, `all` |

## Before merging anything that touches the runtime

The three files where a race can hide, and what to run when one changes:

| changed | run |
|---|---|
| `runtime/store.py` | `--scenario all --rounds 12 --workers 5 --load 6` |
| `runtime/runner.py` | the same |
| `runtime/graph.py` | the same |
| `providers/run.py` | `tests/test_timeout_kills_the_tree.py` as well |

A short run is not a substitute. Every one of the three defects needed both
several rounds and competing load; the 1-in-48 needed 48.

## What this is not

A reproducer. Reverting ONE fix on its own — the compare-and-set on the READY
promotion — and running twenty rounds at six workers produced zero violations,
because the other two fixes had already closed the crash cascade that widened
the window. So a clean run against a deliberately broken build is not evidence
the build is sound, and a green result here is evidence about *this* build under
*this* load and nothing more.

The mechanism each fix relies on is proved deterministically elsewhere:
`tests/test_multiprocess_run.py::CompareAndSet` constructs the exact
interleaving and fails when the store loses its compare-and-set. This harness is
the reason anybody knew to write it.

## Standing result

2026-08-30, this machine: 162 rounds with no violation across
contend / diamond / kill / effect, at up to six worker processes, eight busy
cores, and three-way in-process parallelism.
