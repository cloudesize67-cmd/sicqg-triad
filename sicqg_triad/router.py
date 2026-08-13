"""Adaptive latency routing + LLM provider interface."""
from __future__ import annotations

import hashlib
import os
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
