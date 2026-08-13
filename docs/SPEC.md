# SPEC — sicqg_triad

Python 3.10+ package implementing the triadic self-improving architecture from the
SICQG blueprint. Termux-compatible. Only deps: `z3-solver`, `requests`, `pytest`.
No paid cloud services — sandbox/auth/LLM layers are adapter interfaces with local
backends. NO secrets, API keys, or tokens in code or persisted state. Deterministic
scoring only — never LLM-as-judge where a programmatic metric exists.

## Package layout (repo root: /mnt/agents/output/project)
```
sicqg_triad/
  __init__.py
  superposition.py   # Level 0 registry
  map_elites.py      # CVT-MAP-Elites archive, multi-island
  z3_gate.py         # formal verification gate
  router.py          # adaptive latency routing + LLM provider interface
  sandbox.py         # executor interface + local subprocess backend
  mcp_auth.py        # OAuth 2.1 PKCE client (RFC 8414/9728/7591/7636/8707)
  orchestrator.py    # 5-stage workflow
  cli.py             # demo entry point
tests/
  test_superposition.py test_map_elites.py test_z3_gate.py
  test_router.py test_sandbox.py test_mcp_auth.py test_e2e.py
README.md
pyproject.toml
```

## Interface contracts (sacred)

### superposition.py
```python
@dataclass
class Variant:
    id: str                 # uuid4 hex
    code: str               # candidate source (python function body or full def)
    invariants: list[str]   # Z3-checkable claims, e.g. "result >= 0"
    parent_ids: list[str]
    generation: int
    mutation_op: str        # "seed" | "point" | "crossover" | ...
    status: str             # "superposed"|"dispatched"|"verified"|"fatal"|"committed"|"pruned"
    metadata: dict
class SuperpositionRegistry:
    def __init__(self, path: str, capacity: int = 10000): ...
    def add(self, v: Variant) -> str: ...
    def get(self, vid: str) -> Variant: ...
    def update_status(self, vid: str, status: str) -> None: ...
    def query(self, status: str | None = None, generation: int | None = None) -> list[Variant]: ...
    def lineage(self, vid: str) -> list[Variant]: ...   # ancestors, root-first
    def prune(self, keep_statuses: set[str]) -> int: ... # capacity bounding; returns count removed
    # persistence: append-only JSONL at `path`; load on init
```

### map_elites.py
```python
@dataclass
class Elite:
    variant_id: str
    fitness: float
    descriptors: tuple[float, ...]   # behavior descriptors
    island: int
class CVTMapElites:
    def __init__(self, n_niches: int, n_islands: int, descriptor_dim: int,
                 centroids: "np.ndarray | None" = None, seed: int = 0): ...
    def add(self, elite: Elite) -> bool: ...   # True if inserted/replaced
    def sample_parents(self, k: int, rng) -> list[Elite]: ...
    def migrate(self, rate: float = 0.1) -> int: ...   # island exchange
    def best(self) -> Elite | None: ...
    def coverage(self) -> float: ...   # fraction of occupied niches
```
Pure-python fallback if numpy missing (implement small kmeans for CVT centroids).

### z3_gate.py
```python
@dataclass
class GateResult:
    passed: bool
    fatal: bool
    counterexample: str | None     # human-readable model when UNSAT/violation
    proof_log: list[str]
class Z3Gate:
    def verify_invariants(self, variant_code: str, invariants: list[str],
                          test_inputs: list[dict]) -> GateResult: ...
    # Strategy: exec candidate in a RESTRICTED namespace (no builtins beyond safe set),
    # run on test_inputs, then check each invariant by translating simple arithmetic
    # claims on inputs/result into z3 assertions (support: comparisons, arithmetic,
    # "forall x in domain: P(x)" via bounded enumeration). Failure => fatal=True +
    # counterexample. Never trust code that errors => fatal.
```

### router.py
```python
class LLMProvider(Protocol):
    def complete(self, prompt: str, thinking_budget: int) -> str: ...
class StubProvider:      # deterministic offline provider for tests/demos
    def complete(self, prompt, thinking_budget): ...
class GeminiFreeProvider:  # reads key ONLY from env GEMINI_API_KEY at call time; never stores
    def __init__(self): ...
def estimate_complexity(task: str) -> int: ...   # 0-100 heuristic
def route(task: str) -> dict:  # {"thinking_budget": int, "mode": "fast"|"deep"}
```

### sandbox.py
```python
@dataclass
class ExecResult:
    ok: bool; stdout: str; stderr: str; wall_ms: int; exit_code: int
class Executor(Protocol):
    def run(self, code: str, timeout_s: int = 10) -> ExecResult: ...
class LocalSubprocessExecutor:
    def __init__(self, mem_mb: int = 256, cpu_s: int = 5): ...
    # runs `python -I -c code` in a fresh temp dir, resource.setrlimit
    # (AS, CPU, NOFILE, FSIZE), no network env passed, killed on timeout, dir deleted
class E2BExecutor:  # stub raising NotImplementedError with setup docs
class ModalExecutor:  # same
```

### mcp_auth.py
```python
@dataclass
class TokenSet:  # lives in memory only; __repr__ redacts values
    access_token: str; refresh_token: str | None; expires_at: float; scope: str
class MCPOAuthClient:
    def __init__(self, resource_url: str, redirect_port: int = 0): ...
    def discover(self) -> dict: ...          # RFC 9728 + RFC 8414
    def register(self, metadata: dict) -> dict: ...  # RFC 7591 -> client_id
    def authorize(self, scope: str, resource: str) -> TokenSet: ...
    # PKCE S256: verifier 128 chars, challenge=B64URL(SHA256(verifier))
    # local loopback listener for callback; state = 32-byte nonce, single-use
    def refresh(self, tokens: TokenSet) -> TokenSet: ...
# TokenSet must NEVER be written to disk or logged. module has no file I/O.
```

### orchestrator.py
```python
class Orchestrator:
    def __init__(self, registry, archive, gate, executor, provider,
                 evaluator: Callable[[str, list[int]], float]): ...
    def demand(self, task: str, n_variants: int = 4, generations: int = 3) -> dict:
        # stage1: provider proposes variants -> registry.add (status superposed)
        # stage2: dispatch top candidates through executor (lazy eval)
        # stage3: z3 gate; fatal penalty prunes from archive eligibility
        # stage4: score survivors with deterministic evaluator on HELD-OUT seeds
        #         only -> insert into MAP-Elites; crossover best -> next generation
        # stage5: commit best (status committed), prune obsolete branches
        # returns {"best_id", "fitness_heldout", "archive_coverage", "log": [...]}
```

## Deterministic-evaluator law
The demo evaluator must score on held-out seeds never shown in prompts; scoring
function is pure code. Tests must show a reward-hacking mutant (e.g. one violating
its invariant) receiving fatal penalty and never entering the archive.

## cli.py
`python -m sicqg_triad.cli --task demo` runs the full loop on the toy problem:
evolve `f(x)` to maximize sum over held-out seeds with invariant "result >= 0",
baseline provided; prints held-out fitness vs baseline and archive coverage.
