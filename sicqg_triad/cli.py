"""Demo CLI: evolve f(x) under the triadic loop.

    python -m sicqg_triad.cli --task demo

Toy problem: evolve ``f(x)`` (single int arg) to maximize the sum of
``f(x)`` over HELD-OUT seeds, subject to the invariant ``result >= 0``.

Standing law: the evaluator is deterministic pure code; the held-out seeds
below are NEVER included in any prompt or proposer input — the proposer
only sees the task text, the generation number, and gate feedback derived
from train seeds.
"""

from __future__ import annotations

import argparse
import random
import tempfile

from .map_elites import CVTMapElites
from .orchestrator import Orchestrator
from .router import StubProvider
from .sandbox import LocalSubprocessExecutor
from .superposition import SuperpositionRegistry
from .z3_gate import Z3Gate, _SAFE_BUILTINS

TASK = ("evolve f(x) to maximize the sum of f(x) over unseen inputs "
        "with invariant result >= 0")

# Train seeds: shown to the proposer (via the task/gate) and used by the
# gate for verification.
TRAIN_SEEDS = [1, 2, 3, 4, 5, 6]

# Held-out seeds: used ONLY by the deterministic evaluator. Distinctive
# values so tests can assert they never leak into proposer inputs.
HELDOUT_SEEDS = [101, 103, 107, 109, 113]

BASELINE_CODE = "def f(x):\n    return x"

INVARIANT = "result >= 0"


def demo_evaluator(code: str, seeds: list[int]) -> float:
    """Deterministic fitness: sum of f(x) over the given seeds.

    Penalizes runtime errors with a large negative score. Pure code — no
    LLM-as-judge.
    """
    ns: dict = {"__builtins__": dict(_SAFE_BUILTINS)}
    try:
        exec(compile(code, "<eval>", "exec"), ns)
        f = ns.get("f")
        if not callable(f):
            return -1e9
        return float(sum(f(x) for x in seeds))
    except Exception:
        return -1e9


def demo_proposer(task: str, generation: int,
                  feedback: list[str]) -> list[tuple[str, list[str], str]]:
    """Deterministic built-in proposer.

    Seed pool at generation 0; afterwards arithmetic mutations (changing
    constants/operators). Never receives or derives held-out seeds. The
    generation-1 pool deliberately includes a reward-hacking mutant
    (``return -x`` claiming ``result >= 0``) so the gate's fatal penalty
    is exercised.
    """
    rng = random.Random(1234 + generation)
    inv = [INVARIANT]
    if generation == 0:
        pool = [
            "def f(x):\n    return x",
            "def f(x):\n    return x * x",
            "def f(x):\n    return 2 * x",
            "def f(x):\n    return abs(x)",
        ]
        return [(c, inv, "seed") for c in pool]
    # mutations: vary constants/operators deterministically
    a = rng.randint(1, 5)
    b = rng.randint(0, 9)
    op = rng.choice(["+", "*"])
    pool = [
        f"def f(x):\n    return x * x + {b}",
        f"def f(x):\n    return {a} * x {op} {b}",
        "def f(x):\n    return -x",  # reward-hacking: claims result >= 0
    ]
    return [(c, inv, "point") for c in pool]


def run_demo() -> dict:
    """Run the full 5-stage loop on the toy problem; print a summary."""
    registry = SuperpositionRegistry(
        tempfile.mktemp(prefix="sicqg_registry_", suffix=".jsonl"))
    archive = CVTMapElites(n_niches=8, n_islands=2, descriptor_dim=2, seed=0)
    gate = Z3Gate()
    executor = LocalSubprocessExecutor()
    orch = Orchestrator(
        registry=registry, archive=archive, gate=gate, executor=executor,
        provider=StubProvider(), evaluator=demo_evaluator,
        proposer=demo_proposer)

    baseline = demo_evaluator(BASELINE_CODE, HELDOUT_SEEDS)
    result = orch.demand(TASK, n_variants=4, generations=3,
                         train_seeds=TRAIN_SEEDS,
                         heldout_seeds=HELDOUT_SEEDS)

    for line in result["log"]:
        print(f"  {line}")
    print(f"baseline fitness (held-out, f(x)=x): {baseline}")
    print(f"best held-out fitness:               {result['fitness_heldout']}")
    print(f"archive coverage:                    {result['archive_coverage']:.2%}")
    print(f"fatally-penalized variants:          {result['fatal_count']}")
    if result["best_id"]:
        best = registry.get(result["best_id"])
        print(f"committed variant ({best.mutation_op}):\n{best.code}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(prog="sicqg_triad.cli")
    parser.add_argument("--task", default="demo", choices=["demo"])
    args = parser.parse_args()
    if args.task == "demo":
        run_demo()


if __name__ == "__main__":
    main()
