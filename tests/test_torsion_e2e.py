"""End-to-end tests for Milestone A: torsion-balance denoise task.

Covers:
  a. evaluator integrity ladder (naive < engineer baseline < bandpass(2,8));
  b. the full loop commits a variant whose HELD-OUT score beats the
     engineer baseline's HELD-OUT score (the Milestone A claim);
  c. proximity to the hand-built reference candidate_a (logged; committed
     held-out must clear baseline + 1.0 dB);
  d. held-out seeds never reach the proposer (spy on all proposer inputs);
  e. reward-hacking mutants (zero-output / erroring) are fatal and never
     committed.
"""

from __future__ import annotations

import re

import numpy as np
import pytest

from examples.torsion import evaluator as ev
from examples.torsion.baselines import CANDIDATE_A_CODE
from examples.torsion.proposer import proposer
from examples.torsion.run import (GATE_INPUTS, TASK, run_torsion,
                                  torsion_evaluator, _write_tmp)
from sicqg_triad.map_elites import CVTMapElites
from sicqg_triad.orchestrator import Orchestrator
from sicqg_triad.router import StubProvider
from sicqg_triad.sandbox import LocalSubprocessExecutor
from sicqg_triad.superposition import SuperpositionRegistry
from sicqg_triad.z3_gate import Z3Gate

import tempfile


def _load(path_or_code):
    import os
    if os.path.exists(path_or_code):
        return ev.load_candidate(path_or_code)
    fd, p = tempfile.mkstemp(suffix=".py")
    with os.fdopen(fd, "w") as fh:
        fh.write(path_or_code)
    fn = ev.load_candidate(p)
    os.unlink(p)
    return fn


# ------------------------------------------------------------ a. selftest

def test_evaluator_ladder_on_train_seeds():
    naive = ev.evaluate_with_seeds(ev.naive_moving_average, ev.TRAIN_SEEDS)
    baseline = ev.evaluate_with_seeds(ev.engineer_baseline, ev.TRAIN_SEEDS)
    bp28 = _load(proposer(TASK, 0, [])[3][0])  # bandpass(2,8) seed
    bp28_train = ev.evaluate_with_seeds(bp28, ev.TRAIN_SEEDS)
    print(f"\n  ladder (train): naive={naive:.3f} baseline={baseline:.3f}"
          f" bandpass(2,8)={bp28_train:.3f}")
    assert naive < baseline
    assert bp28_train > baseline
    # reward math sanity: zeroing the signal is heavily penalized
    zeros = ev.evaluate_with_seeds(lambda x, fs: np.zeros_like(x),
                                   ev.TRAIN_SEEDS)
    assert zeros < 0.0


# --------------------------------------- b/c/d. full loop (shared fixture)

class _ProposerSpy:
    def __init__(self):
        self.calls: list[tuple[str, int, list[str]]] = []

    def __call__(self, task, generation, feedback):
        self.calls.append((task, generation, list(feedback)))
        return proposer(task, generation, feedback)


@pytest.fixture(scope="module")
def loop_result():
    spy = _ProposerSpy()
    result = run_torsion(verbose=False, proposer_fn=spy)
    return result, spy


def test_full_loop_beats_baseline_heldout(loop_result):
    """Milestone A claim: committed variant beats engineer baseline on
    HELD-OUT seeds it was never selected against."""
    result, _ = loop_result
    assert result["best_id"] is not None
    assert result["committed_heldout"] > result["baseline_heldout"]


def test_reference_proximity_to_candidate_a(loop_result):
    result, _ = loop_result
    committed = result["committed_heldout"]
    ref = result["candidate_a_heldout"]
    base = result["baseline_heldout"]
    print(f"\n  loop held-out:      {committed:.3f} dB"
          f"\n  candidate_a ref:    {ref:.3f} dB"
          f"\n  engineer baseline:  {base:.3f} dB"
          f"\n  gap to reference:   {ref - committed:.3f} dB")
    assert committed > base + 1.0  # minimum margin, not the full claim


def test_heldout_seeds_never_reach_proposer(loop_result):
    _, spy = loop_result
    assert spy.calls, "proposer was never invoked"
    pat = re.compile(r"\b(101|203|307|409|503)\b")
    for task, generation, feedback in spy.calls:
        assert not pat.search(task)
        for f in feedback:
            assert not pat.search(f)
        assert isinstance(generation, int)


# ------------------------------------------------- e. reward-hacking

ZERO_MUTANT = '''\
import numpy as np

# torsion-filter kind=moving_average fc_low=0.0 fc_high=0.0 taps=25 window=none notch=0
def apply_filter(x, fs):
    return np.zeros_like(x)
'''

ERROR_MUTANT = '''\
import numpy as np

# torsion-filter kind=moving_average fc_low=0.0 fc_high=0.0 taps=25 window=none notch=0
def apply_filter(x, fs):
    raise RuntimeError("boom")
'''


def test_reward_hacking_mutants_fatal_and_never_committed():
    gate = Z3Gate(allowed_modules=("numpy",))
    inv = ["len(result) == len(x)", "max(result) > min(result)"]

    # zero-output: kills signal AND noise -> degenerate output invariant
    r_zero = gate.verify_invariants(ZERO_MUTANT, inv, GATE_INPUTS)
    assert not r_zero.passed and r_zero.fatal
    # erroring candidate: never trusted
    r_err = gate.verify_invariants(ERROR_MUTANT, inv, GATE_INPUTS)
    assert not r_err.passed and r_err.fatal
    # even if it slipped past the gate, the distortion math sinks it
    assert torsion_evaluator(ZERO_MUTANT, None) < -20.0

    # through the full loop: a proposer offering ONLY mutants commits nothing
    registry = SuperpositionRegistry(
        tempfile.mktemp(prefix="sicqg_torsion_mut_", suffix=".jsonl"))
    archive = CVTMapElites(n_niches=8, n_islands=2, descriptor_dim=2, seed=0)
    orch = Orchestrator(
        registry=registry, archive=archive, gate=gate,
        executor=LocalSubprocessExecutor(), provider=StubProvider(),
        evaluator=torsion_evaluator,
        proposer=lambda t, g, f: [(ZERO_MUTANT, inv, "seed"),
                                  (ERROR_MUTANT, inv, "seed")])
    result = orch.demand(TASK, n_variants=2, generations=1,
                         train_seeds=ev.TRAIN_SEEDS,
                         heldout_seeds=ev.TRAIN_SEEDS,
                         gate_inputs=GATE_INPUTS)
    assert result["fatal_count"] == 2
    assert result["best_id"] is None  # nothing ever committed


# ------------------------------------------------- determinism

def test_two_runs_commit_identical_scores():
    r1 = run_torsion(verbose=False)
    r2 = run_torsion(verbose=False)
    assert r1["fitness_heldout"] == r2["fitness_heldout"]
    assert r1["committed_heldout"] == r2["committed_heldout"]
    assert r1["committed_code"] == r2["committed_code"]
