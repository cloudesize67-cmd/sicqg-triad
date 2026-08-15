"""Adaptive latency routing + LLM provider interface."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Protocol

import requests


class LLMProvider(Protocol):
    def complete(self, prompt: str, thinking_budget: int) -> str: ...


class StubProvider:
    """Deterministic offline provider for tests/demos.

    Never touches the network; output is a pure function of
    (prompt, thinking_budget).
    """

    def complete(self, prompt: str, thinking_budget: int) -> str:
        digest = hashlib.sha256(f"{thinking_budget}:{prompt}".encode()).hexdigest()
        return f"[stub budget={thinking_budget}] {digest[:16]}: response to {prompt!r}"


class GeminiFreeProvider:
    """Gemini free-tier provider.

    Reads the API key ONLY from env ``GEMINI_API_KEY`` at call time; the key
    is never stored on the instance, logged, or persisted.
    """

    API_URL = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.0-flash:generateContent"
    )

    def __init__(self) -> None:
        # Deliberately stateless; key read at call time only.
        pass

    def complete(self, prompt: str, thinking_budget: int) -> str:
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError(
                "GEMINI_API_KEY not set; export it in the environment to use "
                "GeminiFreeProvider (free tier key from https://aistudio.google.com)."
            )
        resp = requests.post(
            self.API_URL,
            params={"key": key},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        return "".join(
            part.get("text", "")
            for cand in data.get("candidates", [])
            for part in cand.get("content", {}).get("parts", [])
        )


@dataclass
class Budget:
    """Economic governance cap for an LLM provider."""
    max_calls: int
    max_est_cost_usd: float


class BudgetExhausted(Exception):
    """Raised by BudgetedProvider when a budget cap would be exceeded.

    Propagates out of Orchestrator.demand() and aborts the run honestly —
    no silent fallbacks, no partial results presented as complete.
    """


class BudgetedProvider:
    """Wraps any LLMProvider with call-count and cost caps.

    ``cost_per_call_usd`` defaults to 0.0 (free tier), in which case only
    the call cap applies. The budget is checked BEFORE each call: the call
    that would exceed the cap raises ``BudgetExhausted`` and is never
    executed.
    """

    def __init__(self, provider, budget: Budget,
                 cost_per_call_usd: float = 0.0) -> None:
        self.provider = provider
        self.budget = budget
        self.cost_per_call_usd = float(cost_per_call_usd)
        self.calls = 0

    def complete(self, prompt: str, thinking_budget: int) -> str:
        if self.calls + 1 > self.budget.max_calls:
            raise BudgetExhausted(
                f"call cap reached: {self.calls}/{self.budget.max_calls}")
        if ((self.calls + 1) * self.cost_per_call_usd
                > self.budget.max_est_cost_usd):
            raise BudgetExhausted(
                f"cost cap reached: est ${(self.calls + 1) * self.cost_per_call_usd:.6f}"
                f" > ${self.budget.max_est_cost_usd:.6f}")
        self.calls += 1
        return self.provider.complete(prompt, thinking_budget)

    def spent(self) -> dict:
        """Current spend: {calls, max_calls, est_cost_usd, max_est_cost_usd}."""
        return {
            "calls": self.calls,
            "max_calls": self.budget.max_calls,
            "est_cost_usd": self.calls * self.cost_per_call_usd,
            "max_est_cost_usd": self.budget.max_est_cost_usd,
        }


_COMPLEX_WORDS = (
    "prove", "optimize", "design", "architecture", "debug", "refactor",
    "analyze", "formal", "verify", "concurrent", "distributed", "recursive",
    "theorem", "invariant", "schedule", "plan",
)


def estimate_complexity(task: str) -> int:
    """0-100 deterministic heuristic complexity estimate for a task string."""
    if not task:
        return 0
    lower = task.lower()
    words = lower.split()
    score = min(30, len(words))  # up to 30 from length
    hits = sum(1 for w in _COMPLEX_WORDS if w in lower)
    score += min(40, hits * 8)
    if "?" in task:
        score += 5
    if any(c in task for c in "{}[]()=;"):
        score += 10
    if task.count("\n") > 2:
        score += 10
    if any(w in lower for w in ("step", "first", "then", "finally")):
        score += 5
    return max(0, min(100, score))


def route(task: str) -> dict:
    """Route a task to a thinking budget and mode."""
    c = estimate_complexity(task)
    if c < 40:
        return {"thinking_budget": 512, "mode": "fast"}
    if c < 70:
        return {"thinking_budget": 4096, "mode": "fast"}
    return {"thinking_budget": 16384, "mode": "deep"}
