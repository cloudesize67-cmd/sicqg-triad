# Project Charter — sicqg-triad

*Framed with the problem-selection framework of Fischbach & Walsh (Cell, 2024):
problem choice >> execution quality.*

## The idea (kernel)

Autonomous self-improving code systems fail in three predictable ways: they waste
compute evaluating every candidate, they reward-hack their own scoring functions,
and they leak credentials at machine speed. This project builds a small, honest
reference architecture that addresses all three with **deterministic, verifiable
mechanisms** rather than bigger models:

1. **Lazy evaluation** — candidates live as un-evaluated, lineage-tracked
   "superposition" vectors until a demand forces evaluation.
2. **Verify-then-commit** — a Z3 SMT gate applies fatal penalties to logically
   unsound mutations before they can enter the evolutionary archive.
3. **Zero static secrets** — all tool access flows through an OAuth 2.1 + PKCE
   client that never persists tokens.

## Why it is a big deal if it works

Evolutionary code search (AlphaEvolve, FunSearch, CodeEvolve) is demonstrated but
opaque and expensive. A transparent, $0-runnable loop with a formal verification
gate produces something arguably more valuable than the artifacts it evolves:
**verifier-scored execution traces** — a clean dataset of (candidate, proof result,
held-out fitness) tuples suitable for future RLVR fine-tuning. The system is its
own data engine.

## Evaluation axes

| Axis | Assessment |
|---|---|
| Impact if successful | High: reusable gate + archive pattern for any evolutionary search; trace dataset |
| Likelihood of success | High for the loop itself (done, 48 tests green); medium for beating engineered baselines on real problems (Milestone A) |

## Risk matrix (top risks, befriended not avoided)

| Risk | Likelihood | Mitigation |
|---|---|---|
| Formal gate coverage gap (invariants only as strong as probe inputs) | Medium | Boundary-probe auto-generation + symbolic refutation for arithmetic candidates; residual risk documented |
| Local sandbox weaker than Firecracker/gVisor | Medium | bwrap/proot confinement ladder; E2B/Modal adapter stubs for when budget allows |
| Reward hacking via evaluator overfitting | Medium | Held-out seeds never enter prompts (spy-tested); deterministic evaluators only, never LLM-as-judge |
| Zero-budget constraint stalls LLM-guided mutation | Medium | Provider interface: free-tier Gemini now, paid models slot in without code changes |

## Optimization function (success metrics)

- **Primary:** held-out fitness of the committed elite vs. a fixed engineer
  baseline, on seeds never visible to the proposer.
- **Secondary:** archive coverage (diversity), fatal-penalty count (gate
  sensitivity), determinism (byte-identical reruns).

## Parameter strategy (fix ONE meaningful constraint)

Fixed: **the evaluator is always deterministic code; scoring is always on held-out
data.** Everything else — models, sandbox backend, archive geometry, mutation
operators — floats.

## Decision tree (next nodes)

1. Milestone A: point the loop at the torsion-filter problem; beat baseline 5.956
   on held-out seeds (candidate_a already at 17.92 in prior runs).
2. Milestone B: blind re-discovery POC on stripped manipulation-campaign labels,
   scored against public takedown ground truth.
3. NSF ACCESS Explore application with the above as preliminary results.
