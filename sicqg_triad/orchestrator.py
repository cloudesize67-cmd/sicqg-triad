"""5-stage orchestration workflow.

Stages of ``Orchestrator.demand``:
  1. propose variants (via injected proposer, else the LLM provider) and
     register them in the superposition registry (status "superposed");
  2. dispatch candidates through the sandbox executor (lazy eval);
  3. z3 formal-verification gate — fatal variants get status "fatal", are
     excluded from archive eligibility, and their counterexamples are
     appended to the feedback for the next generation (Reflexion pattern);
  4. survivors are scored by the deterministic evaluator on held-out seeds
     and inserted into the MAP-Elites archive with 2-D descriptors;
     crossover of archived parents (via the proposer) seeds the next
     generation;
  5. the best variant is committed and obsolete branches pruned.

Standing law: scoring is deterministic code only; held-out seeds are never
included in any prompt or proposer input — the proposer only ever receives
(task, generation, feedback).
"""

from __future__ import annotations

import random
import re
import uuid
from typing import Callable

from .map_elites import Elite
from .router import route
from .superposition import Variant

# Matches ```python ... ``` or plain ``` ... ``` fenced blocks.
_FENCE_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)

_DEFAULT_GATE_INPUTS = [{"x": i} for i in range(3)]


def _parse_code(text: str) -> str:
    """Extract code from markdown fences if present, else return as-is."""
    blocks = _FENCE_RE.findall(text)
    if blocks:
        return "\n\n".join(b.strip() for b in blocks if b.strip())
    return text.strip()


class Orchestrator:
    """Triadic self-improvement workflow driver.

    Parameters
    ----------
    registry : SuperpositionRegistry
    archive : CVTMapElites
    gate : Z3Gate
    executor : Executor (local subprocess sandbox or compatible)
    provider : LLMProvider (StubProvider-compatible); used only when no
        ``proposer`` is given.
    evaluator : Callable[[str, list[int]], float]
        Deterministic scoring function: (code, seeds) -> fitness.
    proposer : Callable[[str, int, list[str]], list[tuple[str, list[str], str]]]
        Optional deterministic proposal function. Receives
        (task, generation, feedback) and returns
        (code, invariants, mutation_op) tuples. When provided, the LLM
        provider is never consulted.
    """

    def __init__(self, registry, archive, gate, executor, provider,
                 evaluator: Callable[[str, list[int]], float],
                 proposer: Callable[[str, int, list[str]],
                                    list[tuple[str, list[str], str]]] | None = None):
        self.registry = registry
        self.archive = archive
        self.gate = gate
        self.executor = executor
        self.provider = provider
        self.evaluator = evaluator
        self.proposer = proposer

    # ------------------------------------------------------------- stage 1
    def _propose(self, task: str, generation: int, n: int,
                 feedback: list[str],
                 parent_ids: list[str] | None = None,
                 mutation_op: str | None = None
                 ) -> list[Variant]:
        """Create and register n variants for this generation."""
        out: list[Variant] = []
        if self.proposer is not None:
            proposals = self.proposer(task, generation, list(feedback))[:n]
            for code, invariants, op in proposals:
                out.append(self._register(
                    code, invariants, generation,
                    mutation_op or op, parent_ids or []))
        else:
            budget = route(task)["thinking_budget"]
            for _ in range(n):
                prompt = (
                    "Write a single Python function solving this task.\n"
                    f"Task: {task}\n"
                    "Return ONLY the code, optionally in a markdown fence.\n"
                    "Claimed invariant: result >= 0\n"
                )
                if feedback:
                    prompt += ("Previous attempts failed verification:\n"
                               + "\n".join(f"- {f}" for f in feedback) + "\n")
                text = self.provider.complete(prompt, budget)
                out.append(self._register(
                    _parse_code(text), ["result >= 0"], generation,
                    mutation_op or ("seed" if generation == 0 else "point"),
                    parent_ids or []))
        return out

    def _register(self, code: str, invariants: list[str], generation: int,
                  mutation_op: str, parent_ids: list[str]) -> Variant:
        v = Variant(
            id=uuid.uuid4().hex,
            code=code,
            invariants=list(invariants),
            parent_ids=list(parent_ids),
            generation=generation,
            mutation_op=mutation_op,
            status="superposed",
            metadata={},
        )
        self.registry.add(v)
        return v

    # ---------------------------------------------------------------- main
    def demand(self, task: str, n_variants: int = 4, generations: int = 3,
               train_seeds: list[int] | None = None,
               heldout_seeds: list[int] | None = None,
               gate_inputs: list[dict] | None = None) -> dict:
        """Run the 5-stage loop. Returns a summary dict.

        ``heldout_seeds`` are used ONLY for scoring via the injected
        deterministic evaluator; they never reach the proposer or provider.
        """
        log: list[str] = []
        feedback: list[str] = []
        train = list(train_seeds) if train_seeds is not None else [0, 1, 2]
        heldout = list(heldout_seeds) if heldout_seeds is not None else train
        inputs = gate_inputs if gate_inputs is not None else (
            [{"x": s} for s in train] or list(_DEFAULT_GATE_INPUTS))
        fatal_count = 0
        xover_parents: list = []  # elites chosen for crossover next gen

        for gen in range(generations):
            # ---- stage 1: propose -------------------------------------
            variants = self._propose(task, gen, n_variants, feedback)
            # mutations of the current best elite descend from it
            if gen > 0:
                best_so_far = self.archive.best()
                if best_so_far is not None:
                    for v in variants:
                        if not v.parent_ids:
                            v.parent_ids = [best_so_far.variant_id]
            # crossover children of last generation's archived parents
            if len(xover_parents) == 2:
                p1 = self.registry.get(xover_parents[0].variant_id)
                p2 = self.registry.get(xover_parents[1].variant_id)
                xover_task = (
                    f"{task}\nCrossover of two parent solutions; combine "
                    f"their code fragments.\nParent A:\n{p1.code}\n"
                    f"Parent B:\n{p2.code}")
                variants += self._propose(
                    xover_task, gen, max(1, n_variants // 2), feedback,
                    parent_ids=[p1.id, p2.id], mutation_op="crossover")
            log.append(f"gen {gen}: proposed {len(variants)} variants")

            # ---- stage 2: dispatch (lazy eval via sandbox) ------------
            dispatched = []
            for v in variants:
                res = self.executor.run(v.code, timeout_s=5)
                if res.ok:
                    self.registry.update_status(v.id, "dispatched")
                    dispatched.append(v)
                else:
                    self.registry.update_status(v.id, "fatal")
                    fatal_count += 1
                    feedback.append(
                        f"variant {v.id[:8]} failed to execute: "
                        f"{res.stderr.strip()[:120]}")
            log.append(f"gen {gen}: {len(dispatched)}/{len(variants)} "
                       f"dispatched cleanly")

            # ---- stage 3: z3 gate -------------------------------------
            survivors = []
            for v in dispatched:
                gr = self.gate.verify_invariants(v.code, v.invariants, inputs)
                v.metadata["proof_log"] = gr.proof_log
                if gr.passed:
                    # remains in superposition (eligible for archive)
                    self.registry.update_status(v.id, "superposed")
                    survivors.append(v)
                elif gr.fatal:
                    self.registry.update_status(v.id, "fatal")
                    fatal_count += 1
                    if gr.counterexample:
                        feedback.append(gr.counterexample)  # Reflexion
            log.append(f"gen {gen}: gate passed {len(survivors)}, "
                       f"fatal {fatal_count} cumulative")

            # ---- stage 4: score + archive + crossover -----------------
            for v in survivors:
                fitness = float(self.evaluator(v.code, heldout))
                v.metadata["fitness_heldout"] = fitness
                train_fit = float(self.evaluator(v.code, train))
                descriptors = (
                    train_fit / (1.0 + abs(train_fit)),  # squashed train fitness
                    min(len(v.code) / 500.0, 1.0),       # normalized code length
                )
                island = int(v.id[:8], 16) % self.archive.n_islands
                self.archive.add(Elite(variant_id=v.id, fitness=fitness,
                                       descriptors=descriptors, island=island))

            if gen < generations - 1:
                xover_parents = self.archive.sample_parents(
                    2, random.Random(gen))
                if len(xover_parents) == 2:
                    log.append(f"gen {gen}: crossover parents selected for "
                               f"next generation")

        # ---- stage 5: commit best, prune obsolete -----------------------
        best = self.archive.best()
        best_id = None
        fitness_heldout = float("-inf")
        if best is not None:
            best_id = best.variant_id
            fitness_heldout = best.fitness
            self.registry.update_status(best_id, "committed")
            log.append(f"committed {best_id[:8]} "
                       f"fitness_heldout={fitness_heldout}")
        removed = self.registry.prune({"committed", "superposed"})
        log.append(f"pruned {removed} obsolete variants")

        return {
            "best_id": best_id,
            "fitness_heldout": fitness_heldout,
            "archive_coverage": self.archive.coverage(),
            "fatal_count": fatal_count,
            "log": log,
        }
