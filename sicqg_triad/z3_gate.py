"""Formal verification gate.

Executes candidate code in a restricted namespace, runs it on test inputs
plus auto-generated boundary probes, then checks each invariant:
  1. concretely, on every (inputs, result) pair, including probes;
  2. with z3: input-only claims are refuted over the bounded domain spanned
     by the test inputs; result-claims are proven symbolically against the
     candidate's own return expression when it is a single pure arithmetic
     expression (concrete-only fallback otherwise, noted in proof_log).

Any failure or exception is fatal and carries a counterexample.
"""

from __future__ import annotations

import ast
import itertools
from dataclasses import dataclass, field

# Max number of auto-generated boundary probe combinations.
_MAX_PROBES = 64
# Input bound used for symbolic result-claim proofs (much wider than the
# domain spanned by the test inputs, so off-domain exploits are caught).
_SYMBOLIC_BOUND = 10**6

import z3

# Builtins allowed inside candidate code. Nothing that touches I/O, imports,
# or the interpreter itself.
_SAFE_BUILTINS = {
    name: getattr(__builtins__, name) if not isinstance(__builtins__, dict)
    else __builtins__[name]
    for name in (
        "abs min max sum len range enumerate zip map filter sorted reversed "
        "int float bool str round pow divmod all any list tuple dict set "
        "isinstance ord chr".split()
    )
}

# AST nodes allowed inside invariant expressions (pure arithmetic logic).
_ALLOWED_EXPR_NODES = (
    ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare,
    ast.Name, ast.Load, ast.Constant, ast.IfExp,
    ast.And, ast.Or, ast.Not, ast.Add, ast.Sub, ast.Mult, ast.Div,
    ast.FloorDiv, ast.Mod, ast.Pow, ast.USub, ast.UAdd,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.Call,  # only abs/min/max, filtered separately
)
_ALLOWED_CALLS = {"abs", "min", max.__name__}


@dataclass
class GateResult:
    """Outcome of the verification gate."""

    passed: bool
    fatal: bool
    counterexample: str | None  # human-readable model when UNSAT/violation
    proof_log: list[str] = field(default_factory=list)


class Z3Gate:
    """Verifies candidate code against Z3-checkable invariants."""

    # ------------------------------------------------------------------ exec
    @staticmethod
    def _load_callable(code: str, arg_names: list[str], log: list[str]):
        """Exec candidate code in a restricted namespace; return its function."""
        ns: dict = {"__builtins__": dict(_SAFE_BUILTINS)}
        if "def " not in code:
            args = ", ".join(arg_names) if arg_names else ""
            body = "\n".join("    " + ln for ln in code.splitlines())
            code = f"def _candidate({args}):\n{body}"
        tree = ast.parse(code, mode="exec")
        # forbid dunder attribute access / imports entirely
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                raise ValueError("imports are not allowed in candidate code")
            if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
                raise ValueError("dunder attribute access is not allowed")
            if isinstance(node, ast.Name) and node.id.startswith("__"):
                raise ValueError("dunder name access is not allowed")
        exec(compile(tree, "<candidate>", "exec"), ns)
        funcs = [v for k, v in ns.items()
                 if callable(v) and not k.startswith("__")
                 and k not in _SAFE_BUILTINS]
        if not funcs:
            raise ValueError("candidate code defines no callable")
        log.append(f"loaded candidate callable: {funcs[0].__name__}")
        return funcs[0]

    # ------------------------------------------------------- z3 translation
    @staticmethod
    def _to_z3(node: ast.AST, env: dict[str, z3.ArithRef]):
        """Translate a simple arithmetic AST into a z3 expression."""
        if isinstance(node, ast.Expression):
            return Z3Gate._to_z3(node.body, env)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                return z3.BoolVal(node.value)
            if isinstance(node.value, int):
                return z3.IntVal(node.value)
            if isinstance(node.value, float):
                return z3.RealVal(node.value)
            raise ValueError(f"unsupported constant {node.value!r}")
        if isinstance(node, ast.Name):
            if node.id in env:
                return env[node.id]
            raise ValueError(f"unknown name {node.id!r} in invariant")
        if isinstance(node, ast.BoolOp):
            vals = [Z3Gate._to_z3(v, env) for v in node.values]
            if isinstance(node.op, ast.And):
                return z3.And(*vals)
            if isinstance(node.op, ast.Or):
                return z3.Or(*vals)
        if isinstance(node, ast.UnaryOp):
            operand = Z3Gate._to_z3(node.operand, env)
            if isinstance(node.op, ast.Not):
                return z3.Not(operand)
            if isinstance(node.op, ast.USub):
                return -operand
            if isinstance(node.op, ast.UAdd):
                return operand
        if isinstance(node, ast.BinOp):
            left = Z3Gate._to_z3(node.left, env)
            right = Z3Gate._to_z3(node.right, env)
            op = node.op
            if isinstance(op, ast.Add):
                return left + right
            if isinstance(op, ast.Sub):
                return left - right
            if isinstance(op, ast.Mult):
                return left * right
            if isinstance(op, (ast.Div, ast.FloorDiv)):
                return left / right
            if isinstance(op, ast.Mod):
                return left % right
            if isinstance(op, ast.Pow) and isinstance(node.right, ast.Constant) \
                    and isinstance(node.right.value, int) and 0 <= node.right.value <= 4:
                r = z3.IntVal(1)
                for _ in range(node.right.value):
                    r = r * left
                return r
        if isinstance(node, ast.Compare):
            left = Z3Gate._to_z3(node.left, env)
            if len(node.ops) != 1:
                raise ValueError("chained comparisons not supported")
            right = Z3Gate._to_z3(node.comparators[0], env)
            op = node.ops[0]
            if isinstance(op, ast.Eq):
                return left == right
            if isinstance(op, ast.NotEq):
                return left != right
            if isinstance(op, ast.Lt):
                return left < right
            if isinstance(op, ast.LtE):
                return left <= right
            if isinstance(op, ast.Gt):
                return left > right
            if isinstance(op, ast.GtE):
                return left >= right
        if isinstance(node, ast.IfExp):
            return z3.If(Z3Gate._to_z3(node.test, env),
                         Z3Gate._to_z3(node.body, env),
                         Z3Gate._to_z3(node.orelse, env))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in _ALLOWED_CALLS and not node.keywords:
            args = [Z3Gate._to_z3(a, env) for a in node.args]
            if node.func.id == "abs" and len(args) == 1:
                return z3.If(args[0] >= 0, args[0], -args[0])
            if node.func.id == "min":
                out = args[0]
                for a in args[1:]:
                    out = z3.If(out <= a, out, a)
                return out
            if node.func.id == "max":
                out = args[0]
                for a in args[1:]:
                    out = z3.If(out >= a, out, a)
                return out
        raise ValueError(f"unsupported invariant syntax: {ast.dump(node)[:80]}")

    # ------------------------------------------------------------ probing
    @staticmethod
    def _gen_probe_inputs(test_inputs: list[dict],
                          arg_names: list[str]) -> list[dict]:
        """Auto-generate boundary probe inputs from test-input domains.

        For each int-valued argument: min-1, min, 0, 1, max, max+1 and the
        negatives (-1, -min, -max). Non-int arguments reuse observed values.
        Combinations are the cartesian product, capped at ``_MAX_PROBES``.
        """
        per_arg: list[list] = []
        for name in arg_names:
            vals = [ti[name] for ti in test_inputs if name in ti]
            ints = [v for v in vals
                    if isinstance(v, int) and not isinstance(v, bool)]
            if ints:
                lo, hi = min(ints), max(ints)
                probes = sorted({lo - 1, lo, 0, 1, hi, hi + 1,
                                 -1, -lo, -hi})
            else:
                probes = sorted({repr(v): v for v in vals}.values(),
                                key=repr) or [None]
            per_arg.append(probes)
        combos = []
        for prod in itertools.islice(
                itertools.product(*per_arg), _MAX_PROBES):
            combo = dict(zip(arg_names, prod))
            if combo not in test_inputs and combo not in combos:
                combos.append(combo)
        return combos

    # ------------------------------------------------- symbolic result path
    @staticmethod
    def _single_return_expr(code: str, arg_names: list[str]) -> ast.AST:
        """Extract ``<expr>`` from a candidate that is a single ``return``.

        Raises ValueError unless the candidate is exactly one function whose
        body is exactly one ``return <expr>`` statement.
        """
        if "def " not in code:
            args = ", ".join(arg_names) if arg_names else ""
            body = "\n".join("    " + ln for ln in code.splitlines())
            code = f"def _candidate({args}):\n{body}"
        tree = ast.parse(code, mode="exec")
        fdefs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
        if len(fdefs) != 1 or len(tree.body) != 1:
            raise ValueError("candidate is not a single function definition")
        fn = fdefs[0]
        if len(fn.body) != 1 or not isinstance(fn.body[0], ast.Return) \
                or fn.body[0].value is None:
            raise ValueError("function body is not a single return statement")
        return fn.body[0].value

    def _check_result_invariant_z3(self, variant_code: str, inv: str,
                                   arg_names: list[str],
                                   log: list[str]) -> str | None:
        """Symbolically refute a result-claim against the candidate itself.

        Only possible when the candidate is a single ``return <arith expr>``
        over its inputs (ops +,-,*,//,%,**, abs/min/max, comparisons). The
        expression and the invariant are compiled into one z3 formula and the
        negation is checked over inputs bounded to +/- _SYMBOLIC_BOUND.

        Returns a counterexample string if a violating model exists, None if
        proven. Raises ValueError when the candidate/invariant is not
        translatable (caller then falls back to concrete-only checking).
        """
        ret = self._single_return_expr(variant_code, arg_names)
        tree = ast.parse(inv, mode="eval")
        for node in ast.walk(tree):
            if not isinstance(node, _ALLOWED_EXPR_NODES):
                raise ValueError(f"disallowed node {type(node).__name__}")
        solver = z3.Solver()
        env: dict[str, z3.ArithRef] = {}
        for name in arg_names:
            v = z3.Int(name)
            solver.add(v >= -_SYMBOLIC_BOUND, v <= _SYMBOLIC_BOUND)
            env[name] = v
        result_expr = self._to_z3(ret, env)
        env["result"] = result_expr
        claim = self._to_z3(tree, env)
        solver.add(z3.Not(claim))
        if solver.check() == z3.sat:
            model = solver.model()
            cex = ", ".join(f"{d}={model[d]}" for d in model.decls())
            log.append(f"VIOLATED (z3 symbolic): {inv} counterexample: {cex}")
            return f"invariant '{inv}' violated; counterexample: {cex}"
        log.append(
            f"proven symbolically (result = {ast.unparse(ret)}) over "
            f"|inputs| <= {_SYMBOLIC_BOUND}: {inv}")
        return None

    # -------------------------------------------------------------- checking
    def _check_invariant_z3(self, inv: str, var_names: list[str],
                            domain: dict[str, tuple[int, int]],
                            log: list[str]) -> str | None:
        """Try to refute `inv` over the bounded input domain with z3.

        Returns a counterexample string if a violating model exists, else
        None. Raises ValueError if the invariant is not translatable.
        """
        tree = ast.parse(inv, mode="eval")
        for node in ast.walk(tree):
            if not isinstance(node, _ALLOWED_EXPR_NODES):
                raise ValueError(f"disallowed node {type(node).__name__}")
        env: dict[str, z3.ArithRef] = {}
        solver = z3.Solver()
        for name in var_names:
            lo, hi = domain.get(name, (-1000, 1000))
            v = z3.Int(name)
            solver.add(v >= lo, v <= hi)
            env[name] = v
        claim = self._to_z3(tree, env)
        if "result" in inv:
            # result range derived from concrete runs; bounded too
            lo, hi = domain.get("result", (-10**6, 10**6))
            r = z3.Int("result")
            solver.add(r >= lo, r <= hi)
            env["result"] = r
            claim = self._to_z3(tree, env)
        solver.add(z3.Not(claim))
        if solver.check() == z3.sat:
            model = solver.model()
            cex = ", ".join(f"{d}={model[d]}" for d in model.decls())
            log.append(f"VIOLATED (z3): {inv} counterexample: {cex}")
            return f"invariant '{inv}' violated; counterexample: {cex}"
        log.append(f"proven over bounded domain {domain}: {inv}")
        return None

    @staticmethod
    def _eval_concrete(inv: str, env: dict) -> bool:
        """Evaluate an invariant concretely in a builtins-free namespace."""
        tree = ast.parse(inv, mode="eval")
        for node in ast.walk(tree):
            if not isinstance(node, _ALLOWED_EXPR_NODES):
                raise ValueError(f"disallowed node {type(node).__name__}")
            if isinstance(node, ast.Call) and not (
                    isinstance(node.func, ast.Name)
                    and node.func.id in _ALLOWED_CALLS):
                raise ValueError("only abs/min/max calls allowed in invariants")
        safe = {k: _SAFE_BUILTINS[k] for k in _ALLOWED_CALLS}
        return bool(eval(compile(tree, "<invariant>", "eval"),
                         {"__builtins__": safe}, dict(env)))

    # ----------------------------------------------------------------- main
    def verify_invariants(self, variant_code: str, invariants: list[str],
                          test_inputs: list[dict],
                          probe_inputs: list[dict] | None = None) -> GateResult:
        """Verify candidate code against invariants.

        Guarantee: every invariant is checked concretely on ALL test inputs
        AND on boundary probe inputs. ``probe_inputs=None`` auto-generates
        boundary probes from the test-input domains (for int inputs: min-1,
        min, 0, 1, max, max+1 and negatives, capped at 64 combinations).
        Probe inputs that raise runtime errors are skipped (they are
        speculative, not caller-provided).

        Additionally, invariants mentioning ``result`` are *symbolically*
        checked with z3 against the candidate itself when the candidate is a
        single ``return <arith expr>`` over its inputs (ops +,-,*,//,%,**,
        abs/min/max, comparisons): the expression and the invariant are
        compiled into one formula and refuted over inputs bounded to
        +/-1e6. When the candidate is not parseable this way, the gate
        falls back to concrete-only checking and says so in proof_log.
        Invariants not mentioning ``result`` are refuted with z3 over the
        bounded domain spanned by the test inputs.

        Fatal on: code that fails to load, runtime errors on test inputs, or
        any invariant violation (concrete or z3-refuted). Counterexamples
        included.
        """
        log: list[str] = []
        arg_names = sorted({k for ti in test_inputs for k in ti})
        try:
            fn = self._load_callable(variant_code, arg_names, log)
        except Exception as exc:  # never trust code that errors
            log.append(f"FATAL: candidate failed to load: {exc}")
            return GateResult(False, True, f"load error: {exc}", log)

        results: list[tuple[dict, object]] = []
        for ti in test_inputs:
            try:
                res = fn(**ti)
            except Exception as exc:
                log.append(f"FATAL: runtime error on {ti}: {exc}")
                return GateResult(False, True,
                                  f"runtime error on input {ti}: {exc}", log)
            results.append((ti, res))
            log.append(f"ran {ti} -> {res}")

        # boundary probes (auto-generated unless caller supplied them)
        if probe_inputs is None:
            probe_inputs = self._gen_probe_inputs(test_inputs, arg_names)
        probe_results: list[tuple[dict, object]] = []
        for pi in probe_inputs:
            try:
                res = fn(**pi)
            except Exception as exc:
                log.append(f"probe {pi} raised {exc}; skipped")
                continue
            probe_results.append((pi, res))
            log.append(f"probed {pi} -> {res}")

        # bounded domains from observed inputs/results
        domain: dict[str, tuple[int, int]] = {}
        for name in arg_names:
            vals = [ti[name] for ti in test_inputs
                    if isinstance(ti.get(name), (int, float))
                    and not isinstance(ti.get(name), bool)]
            if vals:
                lo, hi = min(vals), max(vals)
                pad = max(1, int(hi - lo))
                domain[name] = (int(lo) - pad, int(hi) + pad)
        int_results = [r for _, r in results
                       if isinstance(r, (int, float)) and not isinstance(r, bool)]
        if int_results:
            lo, hi = min(int_results), max(int_results)
            pad = max(1, int(abs(hi - lo)))
            domain["result"] = (int(lo) - pad, int(hi) + pad)

        for inv in invariants:
            # 1) concrete check on every run (test inputs AND probes)
            for ti, res in itertools.chain(results, probe_results):
                env = dict(ti)
                env["result"] = res
                try:
                    ok = self._eval_concrete(inv, env)
                except Exception as exc:
                    log.append(f"FATAL: invariant '{inv}' not evaluable: {exc}")
                    return GateResult(
                        False, True,
                        f"invariant '{inv}' raised {exc}; not verifiable", log)
                if not ok:
                    kind = "probe" if (ti, res) not in results else "input"
                    cex = (f"invariant '{inv}' violated on {kind} {ti} "
                           f"with result={res}")
                    log.append(f"VIOLATED (concrete): {cex}")
                    return GateResult(False, True, cex, log)
            # 2) z3 refutation
            if "result" in inv:
                # symbolic check against the candidate's own return expr
                try:
                    cex = self._check_result_invariant_z3(
                        variant_code, inv, arg_names, log)
                except (ValueError, z3.Z3Exception) as exc:
                    log.append(f"note: '{inv}' not symbolically checkable "
                               f"({exc}); concrete check only")
                else:
                    if cex is not None:
                        return GateResult(False, True, cex, log)
            else:
                # bounded-domain refutation over the input vars
                try:
                    cex = self._check_invariant_z3(inv, arg_names, domain, log)
                except ValueError as exc:
                    log.append(f"note: '{inv}' not z3-translatable "
                               f"({exc}); concrete check only")
                else:
                    if cex is not None:
                        return GateResult(False, True, cex, log)
        log.append("all invariants verified")
        return GateResult(True, False, None, log)
