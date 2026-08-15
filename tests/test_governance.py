"""Governance upgrades: HITL circuit breaker, telemetry, budget caps."""

from __future__ import annotations

import json

import pytest

from sicqg_triad.cli import (HELDOUT_SEEDS, TASK, TRAIN_SEEDS,
                             demo_evaluator, demo_proposer)
from sicqg_triad.map_elites import CVTMapElites
from sicqg_triad.orchestrator import Orchestrator
from sicqg_triad.router import (Budget, BudgetedProvider, BudgetExhausted,
                                StubProvider)
from sicqg_triad.sandbox import LocalSubprocessExecutor
from sicqg_triad.superposition import SuperpositionRegistry
from sicqg_triad.telemetry import TelemetryEvent, TelemetryLogger
from sicqg_triad.z3_gate import Z3Gate


def _make_orch(tmp_path, commit_policy=None, telemetry=None):
    registry = SuperpositionRegistry(str(tmp_path / "registry.jsonl"))
    archive = CVTMapElites(n_niches=8, n_islands=2, descriptor_dim=2, seed=0)
    orch = Orchestrator(
        registry=registry, archive=archive, gate=Z3Gate(),
        executor=LocalSubprocessExecutor(), provider=StubProvider(),
        evaluator=demo_evaluator, proposer=demo_proposer,
        commit_policy=commit_policy, telemetry=telemetry)
    return orch, registry, archive


def _run(orch, n=4, gens=3):
    return orch.demand(TASK, n_variants=n, generations=gens,
                       train_seeds=TRAIN_SEEDS, heldout_seeds=HELDOUT_SEEDS)


# ----------------------------------------------------------- 1. HITL gate
def test_commit_policy_false_blocks_commit(tmp_path):
    seen: list[dict] = []
    orch, registry, _ = _make_orch(
        tmp_path, commit_policy=lambda p: seen.append(p) or False)
    result = _run(orch)
    assert result["commit_blocked"] is True
    assert result["best_id"] is not None
    assert registry.get(result["best_id"]).status == "verified"
    assert seen and set(seen[0]) == {"best_id", "fitness_train",
                                     "fatal_count", "generations"}
    assert seen[0]["generations"] == 3
    assert seen[0]["best_id"] == result["best_id"]


def test_commit_policy_default_approves(tmp_path):
    orch, registry, _ = _make_orch(tmp_path)
    result = _run(orch)
    assert "commit_blocked" not in result
    assert registry.get(result["best_id"]).status == "committed"


# --------------------------------------------------------- 2. telemetry
def test_telemetry_events_and_summary(tmp_path):
    logger = TelemetryLogger(str(tmp_path / "telemetry.jsonl"))
    orch, _, _ = _make_orch(tmp_path, telemetry=logger)
    result = _run(orch, gens=3)
    assert len(logger.events) == 3  # one event per generation
    assert [e.generation for e in logger.events] == [0, 1, 2]
    assert logger.events[0].descriptor_drift == 0.0
    n_prop = sum(e.n_proposed for e in logger.events)
    n_fatal = sum(e.n_fatal for e in logger.events)
    summary = result["telemetry_summary"]
    assert summary == logger.summary()
    assert summary["events"] == 3
    assert summary["fatal_rate"] == pytest.approx(n_fatal / n_prop)
    assert summary["coverage_trend"][0] == logger.events[0].archive_coverage
    assert summary["coverage_trend"][1] == logger.events[-1].archive_coverage
    assert summary["max_drift"] == max(e.descriptor_drift
                                       for e in logger.events)


def test_telemetry_jsonl_roundtrip(tmp_path):
    path = tmp_path / "t.jsonl"
    logger = TelemetryLogger(str(path))
    logger.log(TelemetryEvent(generation=0, n_proposed=4, n_fatal=1,
                              archive_coverage=0.125, best_fitness=533.0,
                              descriptor_drift=0.0))
    logger.log(TelemetryEvent(generation=1, n_proposed=5, n_fatal=2,
                              archive_coverage=0.25, best_fitness=999.0,
                              descriptor_drift=0.5))
    rows = [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines()]
    assert rows == [
        {"generation": 0, "n_proposed": 4, "n_fatal": 1,
         "archive_coverage": 0.125, "best_fitness": 533.0,
         "descriptor_drift": 0.0},
        {"generation": 1, "n_proposed": 5, "n_fatal": 2,
         "archive_coverage": 0.25, "best_fitness": 999.0,
         "descriptor_drift": 0.5},
    ]
    summary = logger.summary()
    assert summary["fatal_rate"] == pytest.approx(3 / 9)
    assert summary["coverage_trend"] == (0.125, 0.25)
    assert summary["max_drift"] == 0.5
    assert summary["events"] == 2


# ------------------------------------------------------ 3. budget caps
def test_budgeted_provider_raises_after_call_cap():
    bp = BudgetedProvider(StubProvider(), Budget(max_calls=2,
                                                 max_est_cost_usd=1.0))
    bp.complete("a", 1)
    bp.complete("b", 1)
    with pytest.raises(BudgetExhausted):
        bp.complete("c", 1)
    assert bp.calls == 2


def test_budgeted_provider_cost_cap():
    bp = BudgetedProvider(StubProvider(), Budget(max_calls=100,
                                                 max_est_cost_usd=0.02),
                          cost_per_call_usd=0.01)
    bp.complete("a", 1)
    bp.complete("b", 1)
    with pytest.raises(BudgetExhausted):
        bp.complete("c", 1)


def test_free_tier_zero_cost_never_raises_within_max_calls():
    bp = BudgetedProvider(StubProvider(), Budget(max_calls=5,
                                                 max_est_cost_usd=0.0),
                          cost_per_call_usd=0.0)
    for i in range(5):
        bp.complete(f"p{i}", 1)
    assert bp.calls == 5


def test_spent_reporting():
    bp = BudgetedProvider(StubProvider(), Budget(max_calls=10,
                                                 max_est_cost_usd=0.50),
                          cost_per_call_usd=0.05)
    bp.complete("x", 1)
    bp.complete("y", 1)
    assert bp.spent() == {"calls": 2, "max_calls": 10,
                          "est_cost_usd": pytest.approx(0.10),
                          "max_est_cost_usd": 0.50}


# ------------------------------------------- 4. e2e: telemetry + HITL
def test_e2e_demo_with_telemetry_and_blocking_policy(tmp_path):
    logger = TelemetryLogger(str(tmp_path / "telemetry.jsonl"))
    orch, registry, _ = _make_orch(
        tmp_path, commit_policy=lambda payload: False, telemetry=logger)
    result = _run(orch)
    assert result["commit_blocked"] is True
    assert result["fitness_heldout"] > 0  # search itself succeeded
    assert len(logger.events) == 3
    assert result["telemetry_summary"]["events"] == 3
    assert registry.get(result["best_id"]).status == "verified"
