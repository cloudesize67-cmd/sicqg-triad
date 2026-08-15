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
from .telemetry import TelemetryEvent

# Matches ```python ... ``` or plain ``` ... ``` fenced blocks.
_FENCE_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)

_DEFAULT_GATE_INPUTS = [{"x": i} for i in range(3)]


def _mean_pairwise_dist(a: list[tuple[float, ...]],
                        b: list[tuple[float, ...]]) -> float:
    """Mean Euclidean distance over all (x, y) pairs in a x b."""
    if not a or not b:
        return 0.0
    total = 0.0
    for x in a:
        for y in b:
            total += sum((p - q) ** 2 for p, q in zip(x, y)) ** 0.5
    return total / (len(a) * len(b))


def _parse_code(text: str) -> str:
    """Extract code from markdown fences if present, else return as-is."""
    blocks = _FENCE_RE.findall(text)
    if blocks:
        return "\n\n".join(b.strip() for b in blocks if b.strip())
    return text.strip()


def hitl_prompt_policy(payload: dict) -> bool:
    """Human-in-the-loop commit policy: print payload, prompt on stdin.

    Approves only on an explicit "y"/"yes"; anything else (including EOF
    or empty input) blocks the commit.
    """
    print("HITL commit gate — payload:")
    for k, v in payload.items():
        print(f"  {k}: {v}")
    try:
        answer = input("Approve commit? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    return answer in ("y", "yes")


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
    descriptor_fn : Callable[[Variant, float], tuple[float, ...]] | None
        Optional MAP-Elites descriptor override: (variant, train_fitness)
        -> behavior descriptor tuple. Defaults to squashed train fitness +
        normalized code length.
    commit_policy : Callable[[dict], bool] | None
        Optional human-in-the-loop circuit breaker. Called before the
        stage-5 commit with payload {"best_id", "fitness_train",
        "fatal_count", "generations"}; returning False blocks the commit
        (result["commit_blocked"] is set True and the best variant stays
        "verified" instead of "committed"). None (default) = auto-approve,
        the historical behavior.
    telemetry : TelemetryLogger | None
        Optional telemetry sink; one TelemetryEvent is logged per
        generation and demand() includes "telemetry_summary" in its
        result when a logger is present.
    """

    def __init__(self, registry, archive, gate, executor, provider,
                 evaluator: Callable[[str, list[int]], float],
                 proposer: Callable[[str, int, list[str]],
                                    list[tuple[str, list[str], str]]] | None = None,
                 descriptor_fn: Callable[[Variant, float],
                                         tuple[float, ...]] | None = None,
                 commit_policy: Callable[[dict], bool] | None = None,
                 telemetry=None):
        self.registry = registry
        self.archive = archive
        self.gate = gate
        self.executor = executor
        self.provider = provider
        self.evaluator = evaluator
        self.proposer = proposer
        self.descriptor_fn = descriptor_fn
        self.commit_policy = commit_policy
        self.telemetry = telemetry

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
        prev_gen_descriptors: list[tuple[float, ...]] = []

        for gen in range(generations):
            fatal_before = fatal_count
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
            gen_descriptors: list[tuple[float, ...]] = []
            for v in survivors:
                fitness = float(self.evaluator(v.code, heldout))
                v.metadata["fitness_heldout"] = fitness
                train_fit = float(self.evaluator(v.code, train))
                v.metadata["fitness_train"] = train_fit
                if self.descriptor_fn is not None:
                    descriptors = tuple(self.descriptor_fn(v, train_fit))
                else:
                    descriptors = (
                        train_fit / (1.0 + abs(train_fit)),  # squashed train fit
                        min(len(v.code) / 500.0, 1.0),       # norm. code length
                    )
                island = int(v.id[:8], 16) % self.archive.n_islands
                self.archive.add(Elite(variant_id=v.id, fitness=fitness,
                                       descriptors=descriptors, island=island))
                gen_descriptors.append(descriptors)

            if gen < generations - 1:
                xover_parents = self.archive.sample_parents(
                    2, random.Random(gen))
                if len(xover_parents) == 2:
                    log.append(f"gen {gen}: crossover parents selected for "
                               f"next generation")

            # ---- telemetry (one event per generation) ------------------
            if self.telemetry is not None:
                drift = (_mean_pairwise_dist(gen_descriptors,
                                             prev_gen_descriptors)
                         if gen > 0 else 0.0)
                best_now = self.archive.best()
                self.telemetry.log(TelemetryEvent(
                    generation=gen,
                    n_proposed=len(variants),
                    n_fatal=fatal_count - fatal_before,
                    archive_coverage=self.archive.coverage(),
                    best_fitness=(best_now.fitness if best_now is not None
                                  else float("-inf")),
                    descriptor_drift=drift))
            prev_gen_descriptors = gen_descriptors

        # ---- stage 5: commit best, prune obsolete -----------------------
        best = self.archive.best()
        best_id = None
        fitness_heldout = float("-inf")
        commit_blocked = False
        if best is not None:
            best_id = best.variant_id
            fitness_heldout = best.fitness
            if self.commit_policy is not None:
                best_variant = self.registry.get(best_id)
                train_fit = float(best_variant.metadata.get(
                    "fitness_train", float("nan")))
                approved = bool(self.commit_policy({
                    "best_id": best_id,
                    "fitness_train": train_fit,
                    "fatal_count": fatal_count,
                    "generations": generations,
                }))
            else:
                approved = True
            if approved:
                self.registry.update_status(best_id, "committed")
                log.append(f"committed {best_id[:8]} "
                           f"fitness_heldout={fitness_heldout}")
            else:
                # circuit breaker tripped: best stays verified, no commit
                commit_blocked = True
                self.registry.update_status(best_id, "verified")
                log.append(f"commit BLOCKED by commit_policy for "
                           f"{best_id[:8]}; best stays verified")
        removed = self.registry.prune({"committed", "superposed", "verified"})
        log.append(f"pruned {removed} obsolete variants")

        result = {
            "best_id": best_id,
            "fitness_heldout": fitness_heldout,
            "archive_coverage": self.archive.coverage(),
            "fatal_count": fatal_count,
            "log": log,
        }
        if commit_blocked:
            result["commit_blocked"] = True
        if self.telemetry is not None:
            result["telemetry_summary"] = self.telemetry.summary()
        return result
