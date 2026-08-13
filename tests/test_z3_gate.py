"""Tests for the Z3 verification gate."""

import pytest

from sicqg_triad import z3_gate
from sicqg_triad.z3_gate import Z3Gate, Z3_AVAILABLE

GATE = Z3Gate()

requires_z3 = pytest.mark.skipif(not Z3_AVAILABLE, reason="z3 not installed")


def test_honest_candidate_passes():
    code = "def f(x):\n    return abs(x)"
    res = GATE.verify_invariants(code, ["result >= 0"],
                                 [{"x": -5}, {"x": 0}, {"x": 7}])
    assert res.passed
    assert not res.fatal
    assert res.counterexample is None
    assert res.proof_log  # proof trail recorded


def test_reward_hacking_mutant_is_fatal_with_counterexample():
    # mutant claims result >= 0 but returns negative for x < 0 to "win"
    # a higher fitness elsewhere (reward hacking)
    code = "def f(x):\n    return -x if x < 0 else x * -10"
    res = GATE.verify_invariants(code, ["result >= 0"],
                                 [{"x": 1}, {"x": 2}, {"x": 5}])
    assert not res.passed
    assert res.fatal
    assert res.counterexample is not None
    assert "result >= 0" in res.counterexample


@requires_z3
def test_input_only_invariant_proven_by_z3():
    code = "def f(x):\n    return x"
    res = GATE.verify_invariants(code, ["x * x >= 0"],
                                 [{"x": -3}, {"x": 4}])
    assert res.passed
    assert any("proven" in line for line in res.proof_log)


def test_input_only_invariant_refuted_by_z3():
    code = "def f(x):\n    return x"
    res = GATE.verify_invariants(code, ["x >= 0"],
                                 [{"x": 1}, {"x": 4}])
    assert not res.passed
    assert res.fatal
    # caught either by boundary probes (concrete) or z3 refutation
    assert "x >= 0" in res.counterexample


def test_runtime_error_is_fatal():
    code = "def f(x):\n    return 1 / x"
    res = GATE.verify_invariants(code, ["result >= 0"], [{"x": 0}])
    assert not res.passed and res.fatal
    assert "runtime error" in res.counterexample


def test_malicious_code_is_fatal():
    res = GATE.verify_invariants("import os\nos.system('echo pwned')",
                                 ["result >= 0"], [{"x": 1}])
    assert res.fatal
    res2 = GATE.verify_invariants(
        "def f(x):\n    return __import__('os')", ["result >= 0"], [{"x": 1}])
    assert res2.fatal


def test_body_only_code_is_wrapped():
    res = GATE.verify_invariants("return x + 1", ["result >= x"],
                                 [{"x": 0}, {"x": 4}])
    assert res.passed


def test_off_domain_exploit_caught_by_boundary_probes():
    # exploit: honors "result >= 0" on x in [1..10] but violates it elsewhere
    code = "def f(x):\n    return x if x < 50 else -1"
    res = GATE.verify_invariants(code, ["result >= 0"],
                                 [{"x": i} for i in range(1, 11)])
    assert not res.passed and res.fatal
    assert res.counterexample is not None
    assert "result >= 0" in res.counterexample
    assert any("probe" in line for line in res.proof_log)


@requires_z3
def test_symbolic_proof_of_result_claim_via_z3():
    code = "def f(x):\n    return x * x"
    res = GATE.verify_invariants(code, ["result >= 0"],
                                 [{"x": -3}, {"x": 4}])
    assert res.passed
    assert any("proven symbolically" in line for line in res.proof_log)


def test_symbolic_result_claim_refuted():
    code = "def f(x):\n    return x - 3"
    res = GATE.verify_invariants(code, ["result >= 0"], [{"x": 5}])
    assert not res.passed and res.fatal
    assert "result >= 0" in res.counterexample


@requires_z3
def test_non_arith_candidate_falls_back_to_concrete_with_note():
    code = "def f(xs):\n    return sorted(xs)[0]"
    res = GATE.verify_invariants(code, ["result <= max(xs)"],
                                 [{"xs": [3, 1, 2]}])
    assert res.passed
    assert any("concrete check only" in line for line in res.proof_log)


def test_caller_supplied_probe_inputs():
    code = "def f(x):\n    return x"
    res = GATE.verify_invariants(code, ["result >= 0"], [{"x": 1}],
                                 probe_inputs=[{"x": -7}])
    assert not res.passed and res.fatal


def test_gate_works_without_z3(monkeypatch):
    """Module imports and concrete checks still work when z3 is absent."""
    monkeypatch.setattr(z3_gate, "z3", None)
    gate = Z3Gate()
    # concrete checks pass for an honest candidate
    res = gate.verify_invariants("def f(x):\n    return abs(x)",
                                 ["result >= 0"],
                                 [{"x": -5}, {"x": 0}, {"x": 7}])
    assert res.passed and not res.fatal
    assert any("z3 unavailable: symbolic proof skipped; concrete checks only"
               in line for line in res.proof_log)
    # fatal penalties still work: invariant violators are caught concretely
    bad = gate.verify_invariants("def f(x):\n    return -1",
                                 ["result >= 0"], [{"x": 1}])
    assert not bad.passed and bad.fatal
    assert bad.counterexample is not None
    # boundary probes still catch off-domain exploits without z3
    exploit = gate.verify_invariants(
        "def f(x):\n    return x if x < 50 else -1", ["result >= 0"],
        [{"x": i} for i in range(1, 11)])
    assert not exploit.passed and exploit.fatal
