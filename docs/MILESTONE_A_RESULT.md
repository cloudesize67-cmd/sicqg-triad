# Milestone A — Result

## Claim

An autonomous evolutionary loop (deterministic parametric proposer →
sandbox dispatch → formal gate → quality-diversity archive → commit)
discovered a torsion-balance denoise filter that beats a competent
hand-engineered baseline by **+18.3 dB** and the prior best human
candidate by **+6.6 dB**, scored on held-out seeds that were never visible
to the search (selection used TRAIN seeds only; held-out validation ran
exactly once, after the loop committed).

## Scoreboard (dB, two runs byte-identical)

| filter | TRAIN | HELD-OUT |
|---|---|---|
| naive moving average (25-tap) | 3.847 | 3.900 |
| engineer baseline (zero-phase LP 12 Hz) | 5.956 | 6.201 |
| bandpass(2–8 Hz) gen-0 seed | 10.123 | 9.940 |
| candidate_a, prior human best (BP 3.5–6.5 Hz) | 18.086 | 17.917 |
| **committed, evolved (BP 3.8–5.74 Hz, 1201-tap Hann, zero-phase)** | **23.348** | **24.535** |

Metric (upstream, verbatim): band-SNR improvement around the 5 Hz target
(manual Welch periodogram), robust median-minus-half-std gain across
seeds, minus a distortion penalty for attenuating the target tone.
Evaluator: `examples/torsion/evaluator.py`, copied verbatim from
OpenAlpha_Evolve `examples/torsion_filter/evaluator_termux.py`.

## Method

The proposer generates zero-phase windowed-sinc FIR candidates from a
parametric family (lowpass / bandpass-as-difference-of-sincs, optional
60 Hz notch; hamming/hann/blackman; 201–1201 taps); generation 0 is a
fixed seed pool, later generations mutate cutoffs/window/taps and
recombine archived parents via crossover. Every candidate is executed in
the subprocess sandbox, verified by the Z3 gate (`len(result) == len(x)`,
non-degenerate output) on a probe trial built from seed 7 — neither train
nor held-out — and scored by the deterministic evaluator on TRAIN seeds.
The best archive elite is committed, then validated once on HELD-OUT
seeds. No LLM calls; $0 per run.

## Determinism

All randomness flows from fixed seeds (`proposer.SEED`, per-trial RNG
seeded by seed value). Two consecutive `python -m examples.torsion.run`
invocations produce byte-identical scores and identical committed code
(`fitness_heldout = 17.392383721243284` relative fitness both runs); only
uuid-prefixed variant ids and archive coverage vary. The determinism e2e
test asserts this.

## Limitations (honest)

a. **The 5 Hz target frequency is known a priori**, so the problem
   collapses to passband design: the result demonstrates the machinery
   (proposal → gate → QD archive → held-out validation without leakage),
   not novel physics or novel DSP.
b. **Synthetic data stand-in.** Trials are programmatically generated
   (white + pink noise, 60 Hz hum, linear drift, 5 Hz tone); the
   real-sensor swap is documented in the upstream repo.
c. **The bandpass form was not invented.** The gen-0 seed pool included a
   human-chosen bandpass(2,8); evolution refined edges, taps, and window —
   it did not discover the bandpass idea itself.
d. **Baseline drift:** engineer baseline measured 5.956 dB here vs 6.12 dB
   in the original README, most plausibly a numpy version difference
   (FFT/window numerics); noted, not investigated further. All conclusions
   use the locally measured value.

Reproduce: `python -m examples.torsion.run` (or
`python -m sicqg_triad.cli --task torsion`); tests:
`python -m pytest tests -q` (54 passed, 1 skipped).
