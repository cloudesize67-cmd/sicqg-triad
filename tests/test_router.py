import os
import pytest

from sicqg_triad.router import (
    GeminiFreeProvider,
    StubProvider,
    estimate_complexity,
    route,
)


def test_stub_provider_deterministic():
    p = StubProvider()
    a = p.complete("hello", 512)
    b = p.complete("hello", 512)
    assert a == b
    assert p.complete("hello", 1024) != a


def test_stub_provider_offline_and_str():
    out = StubProvider().complete("task", 1)
    assert isinstance(out, str) and "task" in out


def test_estimate_complexity_bounds_and_monotonic():
    assert 0 <= estimate_complexity("") <= 100
    easy = estimate_complexity("add two numbers")
    hard = estimate_complexity(
        "design and formally verify a distributed concurrent scheduler; "
        "prove the invariant holds for all recursive plans step by step "
        "def f(x): return {x: x}"
    )
    assert 0 <= easy < hard <= 100


def test_route_modes():
    r = route("add two numbers")
    assert r["mode"] == "fast" and isinstance(r["thinking_budget"], int)
    r2 = route(
        "prove, optimize, design, analyze, verify a formal distributed "
        "concurrent recursive theorem invariant architecture plan "
        "def f(x): return {x: x} " * 5
    )
    assert r2 == {"thinking_budget": 16384, "mode": "deep"}


def test_gemini_provider_requires_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        GeminiFreeProvider().complete("hi", 1)


def test_gemini_provider_reads_key_at_call_time(monkeypatch):
    gp = GeminiFreeProvider()
    assert not any("GEMINI" in k for k in vars(gp))  # nothing persisted
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    called = {}

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}

    def fake_post(url, params=None, json=None, timeout=None):
        called["params"] = params
        return FakeResp()

    monkeypatch.setattr("sicqg_triad.router.requests.post", fake_post)
    assert gp.complete("hi", 1) == "ok"
    assert called["params"]["key"] == "fake-key"
    assert not hasattr(gp, "_key") and "fake-key" not in repr(gp)
