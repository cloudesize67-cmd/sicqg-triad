"""Tests for the Z3 verification gate."""

from sicqg_triad.z3_gate import Z3Gate

GATE = Z3Gate()


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
