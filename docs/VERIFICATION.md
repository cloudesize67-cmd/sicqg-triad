# Verification Evidence

All claims below were reproduced by an independent adversarial verification pass
(2026-08-14), not just by the build agents.

## Test suite

```
python -m pytest tests -q
48 passed in ~6s
```

Coverage: registry persistence/lineage/pruning, MAP-Elites insertion/migration/
coverage, Z3 gate proof/refutation/fatal paths, router determinism, sandbox
limits/kill/env-scrub/confinement-warning, full mocked OAuth 2.1 PKCE flow,
and 4 end-to-end orchestrator tests.

## Adversarial falsification results

| Attack | Result |
|---|---|
| Hardcoded high score violating invariant (`return 10**9 if x != 4 else -777`) | fatal, never archived |
| Error on some inputs (`return 10**9 // (x - 5)`) | fatal, never archived |
| Held-out-only violator (`return x if x < 50 else -1`, tests x∈[1..10]) | caught by auto boundary probes (x=-10 probe) |
| Sandbox escapes: `().__class__`, `__builtins__`, `__import__`, `import os`, `f.__globals__` | rejected at load; `eval`/`open`/`getattr` absent from namespace |
| Infinite loop in sandbox | killed at timeout, process group reaped, temp dir removed |
| 300MB allocation under 64MB rlimit | fails cleanly |
| Parent env secret (`MY_PARENT_SECRET`) | invisible to child (scrubbed env) |
| Held-out seeds [101,103,107,109,113] in proposer/LLM inputs | zero occurrences (spy-instrumented, both paths) |
| `TokenSet` repr / logging | redacted; module has zero file I/O (source-scanned) |
| Determinism | two full runs byte-identical (modulo uuid prefixes) |

## Demo (reproducible)

```
python -m sicqg_triad.cli --task demo
baseline fitness (held-out, f(x)=x): 533.0
best held-out fitness:               56949.0   (def f(x): return x * x + 8)
archive coverage:                    12.50%
fatally-penalized variants:          8
```

## Known limitations (tracked honestly)

1. Sandbox filesystem confinement requires `bwrap` or `proot`; without them the
   executor emits an explicit warning. Production answer: E2B/Modal adapters (stubs included).
2. Symbolic z3 proofs cover single-expression arithmetic candidates; richer code
   gets concrete checking over test + probe inputs only.
3. `proot` confinement is weak by design (documented in README).
4. On Android/bionic (Termux) the sandbox memory rlimit (RLIMIT_AS) is disabled:
   setting it crashes the child in the bionic linker (CFI shadow check). CPU
   timeout and file limits are still enforced; full memory isolation requires
   the E2B/Modal adapters.

## Milestone A — torsion-balance e2e (2025 run)

```
python -m pytest tests -q
54 passed, 1 skipped in 19.58s
```

Evaluator: examples/torsion/evaluator.py is now the VERBATIM upstream file
(cloudesize67-cmd/OpenAlpha_Evolve, examples/torsion_filter/
evaluator_termux.py) below its provenance header; numpy 2.2.5 provides
np.trapezoid, so no compat shim was needed.

Real measured ladder (`python examples/torsion/evaluator.py --selftest`):
- naive_moving_average train 3.847 / held-out 3.900
- engineer_baseline  train 5.956 / held-out 6.201  (original README: 6.12)
- bandpass(2,8) seed train 10.123 / held-out 9.940
- candidate_a (bp 3.5-6.5) train 18.086 / held-out 17.917 (records: 17.92)
- np.zeros mutant: -1185.6 (distortion penalty); contract violators -> None
  -> orchestrator fitness -1e6

tests/test_torsion_e2e.py coverage:
- evaluator ladder holds on TRAIN seeds (naive < baseline < bandpass(2,8)).
- full loop: committed variant held-out 24.535 dB > baseline held-out
  6.201 dB (Milestone A claim; selection saw TRAIN seeds only).
- reference proximity: candidate_a held-out 17.917 dB; the loop EXCEEDS it
  by +6.6 dB with an evolved bandpass(3.8-5.74 Hz, 1201-tap Hann) — the
  upstream metric legitimately rewards a tighter passband around 5 Hz.
- leakage spy: zero occurrences of held-out seeds {101,203,307,409,503}
  (word-boundary regex) in any proposer input across all generations.
- reward hacking: np.zeros mutant and erroring mutant both gate-fatal;
  a proposer offering only mutants commits nothing (fatal_count == 2,
  best_id is None).
- determinism: two full runs commit identical scores and code
  (fitness_heldout byte-identical: 17.392383721243284 both runs;
  uuid-prefixed variant ids and archive coverage may differ).

### `python -m examples.torsion.run` scoreboard (identical across two runs)

```
committed TRAIN score:            23.348
engineer baseline TRAIN score:     5.956
committed HELD-OUT score:         24.535
engineer baseline HELD-OUT:        6.201
candidate_a HELD-OUT (ref):       17.917
fatally-penalized variants:        0
archive coverage:                 31-44% (uuid-dependent niche hashing)
committed: bandpass fc_low=3.8 fc_high=5.74 taps=1201 window=hann notch=0
```

### Gate extensions (minimal, safe)

- `z3` import is optional: when absent the gate runs concrete-only and says
  so in `proof_log` (symbolic paths raise ValueError -> noted fallback).
- Invariant evaluator allows `len()` (concrete only; symbolic translator
  rejects it -> concrete fallback), enabling `len(result) == len(x)` on
  numpy arrays.
- `Z3Gate(allowed_modules=("numpy",))` relaxes the candidate namespace with
  a guarded `__import__` that only resolves whitelisted modules. For the
  torsion task, isolation comes from the sandbox executor stage; the gate
  namespace relaxation is documented in `examples/torsion/run.py`.

## Governance layer (appended)

With the governance upgrades (HITL circuit breaker, telemetry, budget
caps, failover docs, bootstrap prompt v2) the suite is now:

```
python -m pytest tests -q
63 passed, 1 skipped in ~24s
```

`tests/test_governance.py` (9 tests): commit_policy=False blocks the
commit and sets `commit_blocked` (best stays `verified`); default policy
auto-approves; telemetry logs one event per generation with correct
summary math and JSONL roundtrip; `BudgetedProvider` raises
`BudgetExhausted` past call/cost caps, never raises on the free-tier
zero-cost path within `max_calls`, and reports `spent()` correctly; e2e
demo run with telemetry + blocking policy completes the search honestly.
Both demos produce unchanged scores (533.0/56949.0 and 6.201/24.535)
with the telemetry summary now printed.
