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

## Milestone A — torsion balance

The same orchestrator loop, wired to a real DSP task: denoise synthetic
torsion-balance readouts (20 s trials at fs = 1000 Hz; 5 Hz target tone
buried in white + pink noise, 60 Hz mains hum, and linear drift). The
evaluator (`examples/torsion/evaluator.py`) is copied verbatim from the
user's OpenAlpha_Evolve repo
(`examples/torsion_filter/evaluator_termux.py`, the pure-numpy Termux
twin): band-SNR improvement around 5 Hz (manual Welch periodogram), a
robust median-minus-half-std gain across seeds, and a distortion penalty
for attenuating the target tone. Measured ladder (train): naive moving
average 3.85 dB < engineer baseline 5.96 dB < good bandpass (10+ dB).

```
python -m examples.torsion.run        # or: python -m sicqg_triad.cli --task torsion
```

A deterministic parametric proposer (`examples/torsion/proposer.py` — no
LLM, $0 per run) generates zero-phase windowed-sinc FIR candidates
(lowpass / bandpass-as-difference-of-sincs, optional 60 Hz notch; hamming /
hann / blackman; 201-1201 taps). The gate verifies
`len(result) == len(x)` and a non-degenerate-output invariant on a probe
trial built with seed 7 (neither train nor held-out); selection scores on
TRAIN seeds only; the committed variant is validated ONCE against the
held-out seeds.

Expected output (fully deterministic, modulo uuid prefixes):

```
committed TRAIN score:            23.348
engineer baseline TRAIN score:     5.956
committed HELD-OUT score:         24.535
engineer baseline HELD-OUT:        6.201
candidate_a HELD-OUT (ref):       17.917
```

i.e. the loop's committed filter — an evolved bandpass(3.8-5.74 Hz,
1201-tap Hann, zero-phase) — beats the hand-engineered baseline by
~18.3 dB on held-out data it was never selected against, and actually
**exceeds the hand-built reference** candidate_a (bandpass 3.5-6.5 Hz,
17.92 held-out per the original records; 17.917 measured here) by
~6.6 dB: the evaluator honestly rewards the tighter passband around the
5 Hz target, which rejects more out-of-band noise without clipping the
tone. The metric is the user's own; no reward hacking involved (the
zero-output and erroring mutants are gate-fatal / deeply negative — see
tests).

Honest framing: this is a **synthetic stand-in** for real torsion-balance
sensor data; the original repo documents the real-sensor swap. The
evaluator file is byte-identical to upstream below its provenance header
(requires numpy >= 2.0 for `np.trapezoid`; on Termux use
`pkg install python-numpy`, which ships numpy 2.x).

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
packages. Note: the sandbox memory rlimit is disabled on Android due to a
bionic linker conflict (CPU timeout + file limits still enforced); full
memory isolation requires the E2B/Modal adapters.

## Tests

```
python -m pytest tests -q
```

Includes an end-to-end test proving that a reward-hacking mutant
(returns `-1` while claiming `result >= 0`) is marked `fatal`, never
enters the archive, and that held-out seeds never leak into proposer
inputs.
