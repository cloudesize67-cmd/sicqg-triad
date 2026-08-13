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
