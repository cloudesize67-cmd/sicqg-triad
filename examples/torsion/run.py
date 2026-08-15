"""Milestone A runner: torsion-balance denoise on the sicqg-triad loop.

    python -m examples.torsion.run
    python -m sicqg_triad.cli --task torsion

Evaluator wiring: candidate source is written to a temp file and exec'd to
obtain ``apply_filter`` with numpy available. NOTE: the gate-namespace
import restriction is deliberately relaxed for this DSP task
(``Z3Gate(allowed_modules=("numpy",))`` and unrestricted exec in the
scorer) — isolation comes from the sandbox executor stage, not the gate
namespace. The proposer itself is pure deterministic code and never sees
the evaluator or any seed value.

Scoring: fitness = evaluate_with_seeds(fn, TRAIN_SEEDS) minus the cached
engineer_baseline TRAIN score (relative dB improvement). Selection inside
the loop uses TRAIN seeds only; the true HELD-OUT seeds are touched once,
after ``demand()`` returns, to score the committed variant (Milestone A's
claim) and the baselines.
"""

from __future__ import annotations

import os
import tempfile

import numpy as np

from sicqg_triad.map_elites import CVTMapElites
from sicqg_triad.orchestrator import Orchestrator, hitl_prompt_policy
from sicqg_triad.router import StubProvider
from sicqg_triad.sandbox import LocalSubprocessExecutor
from sicqg_triad.superposition import SuperpositionRegistry, Variant
from sicqg_triad.telemetry import TelemetryLogger
from sicqg_triad.z3_gate import Z3Gate

from . import evaluator as ev
from .baselines import BASELINE_CODE, CANDIDATE_A_CODE
from .proposer import parse_params, proposer

TASK = (
    "Torsion-balance denoise. Evolve apply_filter(x: np.ndarray, fs: float)"
    " -> np.ndarray (same length, finite) that recovers a 5 Hz target tone"
    " from 20 s of noisy sampled data at fs=1000 Hz: white noise, 60 Hz"
    " mains hum, slow drift, and an in-band interferer. Zero-phase FIR"
    " filters from windowed-sinc kernels are the parametric family."
)

# Gate probe: 4000 samples of a trial generated with seed 7 — deliberately
# neither a TRAIN nor a HELD-OUT seed. make_trial returns (time, clean, noisy).
GATE_PROBE = ev.make_trial(7)[2][:4000]
GATE_INPUTS = [{"x": GATE_PROBE, "fs": ev.FS}]

_baseline_cache: dict[str, float] = {}


def _load_fn(code: str):
    """Write candidate source to a temp file and exec it (numpy allowed)."""
    fd, path = tempfile.mkstemp(prefix="torsion_candidate_", suffix=".py")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(code)
        return ev.load_candidate(path)
    finally:
        os.unlink(path)


def baseline_train() -> float:
    if "train" not in _baseline_cache:
        _baseline_cache["train"] = ev.evaluate_with_seeds(
            ev.engineer_baseline, ev.TRAIN_SEEDS)
    return _baseline_cache["train"]


def torsion_evaluator(code: str, seeds) -> float:
    """Orchestrator evaluator: robust TRAIN gain minus baseline (dB).

    The ``seeds`` argument from the orchestrator is ignored on purpose:
    scoring is pinned to TRAIN_SEEDS so selection can never see held-out
    data. Contract-violating or unloadable candidates score -1e6.
    """
    try:
        fn = _load_fn(code)
    except Exception:
        return -1e6
    score = ev.evaluate_with_seeds(fn, ev.TRAIN_SEEDS)
    if score is None:  # contract violation (wrong length / non-finite)
        return -1e6
    return score - baseline_train()


def descriptors(v: Variant, train_fit: float) -> tuple[float, float]:
    """2-D MAP-Elites descriptor: (squashed train fitness, normalized
    parametric distance from the seed — cutoff sum/100 + taps/2000)."""
    p = parse_params(v.code)
    if p is None:
        dist = 0.0
    else:
        dist = (p.fc_low + p.fc_high) / 100.0 + p.taps / 2000.0
    return (train_fit / (1.0 + abs(train_fit)), min(dist, 1.0))


def run_torsion(verbose: bool = True, proposer_fn=proposer,
                hitl: bool = False) -> dict:
    registry = SuperpositionRegistry(
        tempfile.mktemp(prefix="sicqg_torsion_registry_", suffix=".jsonl"))
    archive = CVTMapElites(n_niches=8, n_islands=2, descriptor_dim=2, seed=0)
    gate = Z3Gate(allowed_modules=("numpy",))  # relaxed: isolation=sandbox
    executor = LocalSubprocessExecutor()
    telemetry = TelemetryLogger(
        tempfile.mktemp(prefix="sicqg_torsion_telemetry_", suffix=".jsonl"))
    orch = Orchestrator(
        registry=registry, archive=archive, gate=gate, executor=executor,
        provider=StubProvider(), evaluator=torsion_evaluator,
        proposer=proposer_fn, descriptor_fn=descriptors,
        commit_policy=hitl_prompt_policy if hitl else None,
        telemetry=telemetry)

    result = orch.demand(TASK, n_variants=5, generations=3,
                         train_seeds=ev.TRAIN_SEEDS,
                         heldout_seeds=ev.TRAIN_SEEDS,  # selection on train
                         gate_inputs=GATE_INPUTS)

    # ---- post-loop: held-out validation of the committed variant --------
    out = dict(result)
    if result["best_id"]:
        best = registry.get(result["best_id"])
        fd, path = tempfile.mkstemp(prefix="torsion_committed_", suffix=".py")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(best.code)
        held = ev.validate_heldout(path)
        os.unlink(path)
        train = ev.evaluate_with_seeds(ev.load_candidate(
            _write_tmp(best.code)), ev.TRAIN_SEEDS)
        base_held = ev.validate_heldout(_write_tmp(BASELINE_CODE_ENG))
        cand_a_held = ev.validate_heldout(_write_tmp(CANDIDATE_A_CODE))
        out.update(committed_train=train, committed_heldout=held,
                   baseline_heldout=base_held, candidate_a_heldout=cand_a_held,
                   committed_code=best.code)
        if verbose:
            for line in result["log"]:
                print(f"  {line}")
            print("--- Milestone A scoreboard (dB) ---")
            print(f"committed TRAIN score:          {train:8.3f}")
            print(f"engineer baseline TRAIN score:  {baseline_train():8.3f}")
            print(f"committed HELD-OUT score:       {held:8.3f}")
            print(f"engineer baseline HELD-OUT:     {base_held:8.3f}")
            print(f"candidate_a HELD-OUT (ref):     {cand_a_held:8.3f}")
            print(f"fatally-penalized variants:     {result['fatal_count']}")
            print(f"archive coverage:               "
                  f"{result['archive_coverage']:.2%}")
            if result.get("commit_blocked"):
                print("commit BLOCKED by HITL policy; best stays verified")
            print(f"telemetry summary: {result['telemetry_summary']}")
            print(f"committed variant ({best.mutation_op}):\n{best.code}")
    return out


def _write_tmp(code: str) -> str:
    fd, path = tempfile.mkstemp(prefix="torsion_ref_", suffix=".py")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(code)
    return path


# engineer_baseline (zero-phase 12 Hz lowpass) as a standalone source string
# so it can go through validate_heldout like any candidate file.
BASELINE_CODE_ENG = '''\
import numpy as np

# torsion-filter kind=lowpass fc_low=0.0 fc_high=12.0 taps=801 window=hamming notch=0
def apply_filter(x, fs):
    m = np.arange(801) - 400.0
    h = np.sinc(2 * 12.0 / fs * m) * np.hamming(801)
    h /= h.sum()
    y = np.convolve(x, h, mode="same")
    return np.convolve(y[::-1], h, mode="same")[::-1]
'''


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(prog="examples.torsion.run")
    parser.add_argument("--hitl", action="store_true",
                        help="require human approval before the stage-5 commit")
    args = parser.parse_args()
    run_torsion(hitl=args.hitl)


if __name__ == "__main__":
    main()
