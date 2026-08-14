# Verification Evidence

All claims below were reproduced by an independent adversarial verification pass
(2026-08-14), not just by the build agents.

## Test suite

```
python -m pytest tests -q
48 passed in ~6s
```

Coverage: registry persistence/lineage/pruning, MAP-Elites insertion/migration/
coverage, Z3 gate proof/refutation/fatal paths, router determinism, sandbox
limits/kill/env-scrub/confinement-warning, full mocked OAuth 2.1 PKCE flow,
and 4 end-to-end orchestrator tests.

## Adversarial falsification results

| Attack | Result |
|---|---|
| Hardcoded high score violating invariant (`return 10**9 if x != 4 else -777`) | fatal, never archived |
| Error on some inputs (`return 10**9 // (x - 5)`) | fatal, never archived |
| Held-out-only violator (`return x if x < 50 else -1`, tests x∈[1..10]) | caught by auto boundary probes (x=-10 probe) |
| Sandbox escapes: `().__class__`, `__builtins__`, `__import__`, `import os`, `f.__globals__` | rejected at load; `eval`/`open`/`getattr` absent from namespace |
| Infinite loop in sandbox | killed at timeout, process group reaped, temp dir removed |
| 300MB allocation under 64MB rlimit | fails cleanly |
| Parent env secret (`MY_PARENT_SECRET`) | invisible to child (scrubbed env) |
| Held-out seeds [101,103,107,109,113] in proposer/LLM inputs | zero occurrences (spy-instrumented, both paths) |
| `TokenSet` repr / logging | redacted; module has zero file I/O (source-scanned) |
| Determinism | two full runs byte-identical (modulo uuid prefixes) |

## Demo (reproducible)

```
python -m sicqg_triad.cli --task demo
baseline fitness (held-out, f(x)=x): 533.0
best held-out fitness:               56949.0   (def f(x): return x * x + 8)
archive coverage:                    12.50%
fatally-penalized variants:          8
```

## Known limitations (tracked honestly)

1. Sandbox filesystem confinement requires `bwrap` or `proot`; without them the
   executor emits an explicit warning. Production answer: E2B/Modal adapters (stubs included).
2. Symbolic z3 proofs cover single-expression arithmetic candidates; richer code
   gets concrete checking over test + probe inputs only.
3. `proot` confinement is weak by design (documented in README).
4. On Android/bionic (Termux) the sandbox memory rlimit (RLIMIT_AS) is disabled:
   setting it crashes the child in the bionic linker (CFI shadow check). CPU
   timeout and file limits are still enforced; full memory isolation requires
   the E2B/Modal adapters.
