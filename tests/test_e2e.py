"""End-to-end test: Orchestrator.demand with the deterministic demo proposer.

Verifies:
  a) best held-out fitness improves over the baseline;
  b) a deliberately injected reward-hacking mutant (returns -1 while
     claiming result >= 0) ends "fatal" and never enters the archive;
  c) registry lineage of the committed variant traces back to a seed;
  d) held-out seeds never appear in any string passed to the proposer.
"""

from __future__ import annotations

import random

import pytest

from sicqg_triad.cli import (BASELINE_CODE, HELDOUT_SEEDS, TASK,
                             TRAIN_SEEDS, demo_evaluator, demo_proposer)
from sicqg_triad.map_elites import CVTMapElites
from sicqg_triad.orchestrator import Orchestrator
from sicqg_triad.router import StubProvider
from sicqg_triad.sandbox import LocalSubprocessExecutor
from sicqg_triad.superposition import SuperpositionRegistry
from sicqg_triad.z3_gate import Z3Gate

HACK_CODE = "def f(x):\n    return -1"  # claims result >= 0


@pytest.fixture()
def run(tmp_path):
    """Run the full loop with an injected reward-hacking mutant.

    Returns (result, registry, archive, proposer_calls) where
    proposer_calls records every (task, generation, feedback) tuple the
    proposer ever saw.
    """
    proposer_calls: list[tuple[str, int, list[str]]] = []

    def recording_proposer(task, generation, feedback):
        proposer_calls.append((task, generation, list(feedback)))
        out = demo_proposer(task, generation, feedback)
        if generation == 0:
            out = out + [(HACK_CODE, ["result >= 0"], "seed")]
        return out

    registry = SuperpositionRegistry(str(tmp_path / "registry.jsonl"))
    archive = CVTMapElites(n_niches=8, n_islands=2, descriptor_dim=2, seed=0)
    orch = Orchestrator(
        registry=registry, archive=archive, gate=Z3Gate(),
        executor=LocalSubprocessExecutor(), provider=StubProvider(),
        evaluator=demo_evaluator, proposer=recording_proposer)
    result = orch.demand(TASK, n_variants=5, generations=3,
                         train_seeds=TRAIN_SEEDS, heldout_seeds=HELDOUT_SEEDS)
    return result, registry, archive, proposer_calls


def _all_elites(archive) -> list:
    return [e for cell in archive._archive for e in cell.values()]


def test_improves_over_baseline(run):
    result, *_ = run
    baseline = demo_evaluator(BASELINE_CODE, HELDOUT_SEEDS)
    assert result["fitness_heldout"] > baseline


def test_reward_hacking_mutant_is_fatal_and_not_archived(run):
    result, registry, archive, _ = run
    hackers = [v for v in registry._variants.values()
               if HACK_CODE in v.code]
    # may have been pruned from the registry; check the archive either way
    for v in hackers:
        assert v.status == "fatal"
    assert all(HACK_CODE not in registry.get(e.variant_id).code
               for e in _all_elites(archive))
    assert result["fatal_count"] >= 1


def test_committed_lineage_traces_to_seed(run):
    result, registry, *_ = run
    assert result["best_id"] is not None
    lineage = registry.lineage(result["best_id"])
    assert lineage, "committed variant must have a lineage"
    assert lineage[0].mutation_op == "seed"
    assert lineage[-1].id == result["best_id"]


def test_heldout_seeds_never_reach_proposer(run):
    _, _, _, proposer_calls = run
    assert proposer_calls, "proposer must have been called"
    leaked = []
    for task, generation, feedback in proposer_calls:
        haystack = task + "\n" + "\n".join(feedback)
        for seed in HELDOUT_SEEDS:
            # word-boundary match so substrings of other numbers don't count
            for token in haystack.replace("\n", " ").split():
                if token.strip(".,;:()[]{}") == str(seed):
                    leaked.append((seed, generation))
    assert not leaked, f"held-out seeds leaked to proposer: {leaked}"
