# sicqg_triad

A Termux-compatible Python 3.10+ implementation of the triadic
self-improving architecture from the SICQG blueprint, mapped to boring,
verifiable engineering:

1. **Superposition registry** (`superposition.py`) — every candidate program
   variant lives in an append-only JSONL registry with lineage, lifecycle
   status (`superposed → dispatched → verified / fatal → committed /
   pruned`), and capacity bounding.
2. **Formal gate** (`z3_gate.py`) — candidate code is executed in a
   restricted namespace, then each claimed invariant (e.g. `result >= 0`)
   is checked concretely on test inputs and, where translatable, refuted or
   proven with z3 over a bounded domain. Violations are **fatal**: the
   variant is excluded from the archive and its counterexample is fed back
   into the next generation (Reflexion pattern).
3. **Quality-diversity archive** (`map_elites.py`) — verified survivors are
   scored by a deterministic evaluator and inserted into a CVT-MAP-Elites
   archive with multi-island populations and migration. Crossover of
   archived parents seeds the next generation.

`orchestrator.py` runs the 5-stage `demand()` loop (propose → dispatch →
gate → score/archive → commit/prune). `router.py` provides adaptive
latency routing plus the `LLMProvider` interface (`StubProvider` for
offline use, optional `GeminiFreeProvider` that reads `GEMINI_API_KEY` only
at call time). `sandbox.py` provides a local subprocess executor with
rlimits and a scrubbed environment, plus `E2BExecutor`/`ModalExecutor`
adapter stubs. Filesystem confinement uses the best backend on PATH:
`bwrap` (temp dir as the only writable mount, tmpfs on /tmp, read-only
binds of /usr /lib /bin etc.), else `proot` (`-b <tempdir>:/workspace -w
/workspace`, /tmp shadowed by a scratch dir), else no confinement — in
which case every `ExecResult.stderr` carries an explicit
"WARNING: no filesystem confinement available (install bwrap/proot)"
line. **For production, the cloud adapters (E2B/Modal) are the answer:**
a local subprocess, even with bwrap/proot, is not a hard security
boundary. `mcp_auth.py` is an OAuth 2.1 PKCE client (RFC
8414/9728/7591/7636/8707) with in-memory-only tokens.

## SICQG metaphor → engineering mapping

| SICQG metaphor | Engineering realization |
|---|---|
| Quantum superposition of hypotheses | `SuperpositionRegistry`: all candidate variants registered with status `superposed` until collapsed by verification |
| Wavefunction collapse / measurement | `Z3Gate` + sandbox dispatch: execution and invariant checking collapse a variant to `verified` or `fatal` |
| Decoherence / error correction | Fatal penalty: invariant violators are excluded from the archive; counterexamples become feedback for the next generation |
| Triadic self-improvement loop | `Orchestrator.demand`: propose → dispatch → gate → score/archive → commit/prune |
| Genetic/memetic evolution | CVT-MAP-Elites quality-diversity archive, multi-island migration, crossover of parents via the proposer |
| Adaptive computation depth | `router.route`: deterministic complexity heuristic → thinking budget / mode |
| Secure enclave execution | `sandbox.py` executor interface; local subprocess backend with rlimits and scrubbed env |
| Attested identity | `mcp_auth.py` OAuth 2.1 PKCE client, tokens in memory only |

## What this is NOT

- **No real Firecracker / AWS Nitro Enclaves.** The sandbox layer is an
  adapter interface; the only working backend is a local subprocess with
  `setrlimit`. `E2BExecutor` and `ModalExecutor` are documented stubs that
  raise `NotImplementedError`.
- **No paid API required.** All tests and the demo run fully offline with
  `StubProvider` and the deterministic built-in proposer.
  `GeminiFreeProvider` (free tier) is optional and reads its key only from
  the `GEMINI_API_KEY` environment variable at call time — no secrets are
  ever stored, logged, or persisted.
- **No LLM-as-judge.** Standing law: wherever a programmatic metric
  exists, scoring is a deterministic pure-code evaluator, and demo numbers
  are computed on **held-out seeds only** — seeds that never appear in any
  prompt or proposer input. The e2e test asserts this explicitly.
- **Not a claim of quantum anything.** The SICQG vocabulary is a design
  metaphor; every mechanism here is classical and testable.

## Demo

```
python -m sicqg_triad.cli --task demo
```

Evolves `f(x)` to maximize the sum of `f(x)` over held-out seeds with the
invariant `result >= 0`. Prints baseline fitness, best held-out fitness,
archive coverage, and the number of fatally-penalized variants.

## Install

### Regular Python

```
pip install -e .[dev]
python -m pytest tests -q
```

### Termux

```
pkg install python
pip install -e .          # z3-solver and requests install from wheels/sdist
pip install pytest        # dev extra
python -m pytest tests -q
python -m sicqg_triad.cli --task demo
```

The MAP-Elites archive uses numpy when available and falls back to a small
pure-python k-means otherwise, so it works on Termux without extra native
packages.

## Tests

```
python -m pytest tests -q
```

Includes an end-to-end test proving that a reward-hacking mutant
(returns `-1` while claiming `result >= 0`) is marked `fatal`, never
enters the archive, and that held-out seeds never leak into proposer
inputs.
