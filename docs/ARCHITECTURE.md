# Architecture

## The triad

```
Demand ──► Level 0: SuperpositionRegistry     (memory; un-evaluated variants + lineage)
              │ stage 1: generate & defer
              ▼
           Level 1: Router + Executor         (adaptive compute; sandboxed dispatch)
              │ stage 2: lazy evaluation
              ▼
           Z3Gate                             (formal verification; fatal penalty)
              │ stage 3: verify
              ▼
           CVTMapElites                       (quality-diversity archive, multi-island)
              │ stage 4: mutate & search
              ▼
           State commit + memory pruning      (stage 5: only verified elites commit)
```

Cross-cutting: `mcp_auth.py` — every external tool call goes through OAuth 2.1
discovery (RFC 9728/8414), dynamic client registration (RFC 7591), PKCE S256
(RFC 7636), and audience-bound resource indicators (RFC 8707). Tokens exist only
in process memory; `TokenSet.__repr__` redacts; the module performs zero file I/O.

## Module map

| Module | Role | Key guarantees |
|---|---|---|
| `superposition.py` | Variant registry | Append-only JSONL lineage; capacity-bounded pruning; status machine: superposed → dispatched → (fatal \| committed \| pruned) |
| `router.py` | Adaptive compute routing | `estimate_complexity` → thinking budget; provider protocol with deterministic stub + optional free-tier Gemini (key read from env at call time, never stored) |
| `sandbox.py` | Execution isolation | rlimits (AS/CPU/NOFILE/FSIZE), scrubbed env, process-group kill on timeout, bwrap → proot → honest-warning confinement ladder; E2B/Modal stubs |
| `z3_gate.py` | Formal verification gate | Restricted exec namespace (no imports/dunders); concrete invariant checks on test + auto-generated boundary probe inputs; symbolic z3 proof/refutation for single-expression arithmetic candidates; any error = fatal + counterexample |
| `map_elites.py` | QD archive | CVT-MAP-Elites, per-island archives, ring migration; numpy optional (pure-python kmeans fallback) |
| `orchestrator.py` | 5-stage loop | Wires registry → executor → gate → archive → commit; Reflexion feedback: gate counterexamples feed the next generation's proposer |
| `mcp_auth.py` | Zero-trust tool auth | Full PKCE flow against any compliant AS; no secrets on disk, ever |
| `cli.py` | Demo | `python -m sicqg_triad.cli --task demo` |

## Governance layer

- **HITL circuit breaker** — `Orchestrator(commit_policy=...)` gates the
  stage-5 commit on a human/policy decision (payload: best_id,
  fitness_train, fatal_count, generations). A False verdict sets
  `commit_blocked` in the result and the best variant stays `verified`;
  `None` preserves the historical auto-approve behavior. The demos expose
  it via `--hitl`.
- **Telemetry** — `telemetry.py` logs one `TelemetryEvent` per generation
  (n_proposed, n_fatal, archive_coverage, best_fitness, descriptor_drift =
  mean pairwise distance of elite descriptors vs the previous generation,
  0 for gen 0) to an optional JSONL sink; `TelemetryLogger.summary()`
  aggregates fatal_rate / coverage_trend / max_drift and rides along in
  `demand()`'s result as `telemetry_summary`.
- **Economic governance** — `router.BudgetedProvider` wraps any
  `LLMProvider` with a `Budget(max_calls, max_est_cost_usd)`; the call
  that would exceed a cap raises `BudgetExhausted`, which propagates out
  of `demand()` and aborts the run honestly (no silent fallback).
  `cost_per_call_usd=0.0` models the free tier (call cap only).

## Infrastructure Failover

Execution isolation ascends an adapter ladder with a single
vendor-neutral interface (`Executor.run(code, timeout_s) -> ExecResult`):

```
LocalSubprocessExecutor  (rlimits, scrubbed env)
  -> bwrap / proot confinement       (best local filesystem isolation)
    -> E2BExecutor                   (Firecracker microVMs; stub)
      -> ModalExecutor               (gVisor containers; stub)
```

There is no vendor-specific code in the executor interface or the
orchestrator: switching providers is constructing a different adapter
class and injecting it. Failover when a provider is unavailable or over
quota = move down the ladder and state which guarantees were lost.
Hardware attestation (future AWS Nitro / KMS work) belongs in a NEW
adapter implementing the same interface — never in core orchestration
code.

## The standing engineering law

Credibility = demonstrated prediction against independent ground truth.

- Deterministic evaluators only — never LLM-as-judge where a programmatic metric exists.
- Held-out numbers only — seeds/tests never leak into prompts (spy-tested in CI).
- Any auto-metric must be validated against ground truth before it is trusted.

## What this is NOT (honest boundaries)

- No real Firecracker/gVisor/Nitro Enclave execution — those are paid cloud
  services. The sandbox is an adapter interface; local backends run today,
  cloud backends slot in when budget allows.
- The Z3 gate's guarantee is exactly as strong as its input coverage for
  non-arithmetic candidates; symbolic proof applies to single-expression
  arithmetic candidates. This is documented, not hidden.
- The SICQG "quantum gravity" framing is a design metaphor. The mechanisms
  (lazy eval, verify-then-commit, QD search, zero-trust auth) are the substance.
