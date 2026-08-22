# Measured before the benchmark could run

Three measurements taken this session, each with the command that produced it.
None is an estimate.

## 1. Token and cost reporting EXISTS for claude

```
claude -p "Reply with exactly: OK" --output-format json --permission-mode plan
```

returned:

| field | value |
|---|---|
| `total_cost_usd` | 0.3069575 |
| `usage.input_tokens` | 2 |
| `usage.output_tokens` | 4 |
| `usage.output_tokens_details.thinking_tokens` | 0 |
| `usage.cache_creation_input_tokens` | 29,613 |
| `usage.cache_read_input_tokens` | 21,435 |
| `duration_api_ms` | 1,763 |

Second call, through the instrumented `run_provider(collect_usage=True)`:

| field | value |
|---|---|
| `total_cost_usd` | 0.2925925 |
| `output_tokens` | 5 |
| `cache_creation_input_tokens` | 28,174 |
| wall `duration_s` | 24.55 |

So `runtime/metrics.py`'s standing claim that "this engine cannot see money" was
true when written and is now false for one provider. Corrected in place.

**The floor matters for any benchmark budget.** A call that answers `OK` costs
~$0.29–0.31 and ~28–30k cache-creation tokens, because the CLI's own system
prompt and tool definitions are cached per call. Task content is on top of that.

## 2. The dobby execute node CANNOT write files

Measured, not inferred:

```python
node = TaskNode(node_id="execute", kind="execute", worker="provider",
                instruction="Create a file named PROOF.txt ... containing exactly: DOBBY_CAN_WRITE",
                contract=ArtifactContract(side_effect_class=LOCAL_WRITE),
                config={"provider": "claude", "timeout_s": 240})
ProviderWorker().run(node, {"repo": d, "cwd": d})
```

| | |
|---|---|
| worker result | `ok: True` |
| failure | `None` |
| `PROOF.txt` exists | **False** |
| directory after | `['.omc']` |

Cause, read in two places and consistent with the result:
`ProviderWorker.run` calls `run_provider(spec, prompt, model=..., cwd=...,
timeout_s=...)` with **no `extra`**, so `write_extra` is never passed; and the
catalog's `_claude` argv ends in `--permission-mode plan`, which is read-only.
The node's `side_effect_class=LOCAL_WRITE` declares the intent and grants
nothing.

Two consequences, and the second is worse than the first:

1. **A code-editing A/B is not runnable as built.** Arm B would fail every task
   for a reason that has nothing to do with gates, state or classification.
2. **The worker reported success while changing nothing.** The acceptance gate
   would still fail the item, so PK-2 holds and nothing gets promoted — but the
   worker's own verdict was `ok`, which is the layer that should have said "I was
   not allowed to do that".

## 3. What this means for the benchmark

The corpus cannot be code-editing tasks until (2) is fixed. Two honest routes,
and the choice is the operator's because they cost different things:

- **Fix the write grant first**, then benchmark on code tasks. This is the
  comparison that matches "완성된 프로젝트" and it is the stronger claim. It needs
  a real change to `ProviderWorker` (grant `write_extra` when the node's contract
  declares LOCAL_WRITE, and refuse to report `ok` when a declared write produced
  no change), plus its own tests.
- **Benchmark artifact-producing tasks now.** dobby's evidence gates
  (`project/evidence.py`) were built for exactly these, arm A is a single prompt
  producing the artifact, arm B is the gated loop. Runnable today, and the claim
  narrows to "on artifact-producing tasks" rather than "on coding tasks".

Both are real benchmarks. Neither is the other.
