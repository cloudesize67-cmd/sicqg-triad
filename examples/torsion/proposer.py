"""Deterministic parametric proposer for the torsion-balance denoise task.

No LLM, $0 per run. Generates ``apply_filter(x, fs)`` sources from a
parameterized family: zero-phase FIR filters built from windowed-sinc
kernels — lowpass(fc) or bandpass(fc_low, fc_high) as the difference of two
sinc lowpass kernels — with an optional 60 Hz notch (a narrow 58-62 Hz
bandpass subtracted from the kernel). Window in {hamming, hann, blackman},
numtaps in {201, 401, 801, 1201}.

Generation 0 is a fixed seed pool: moving-average, lowpass(12),
lowpass(25), bandpass(2,8). Later generations mutate (perturb cutoffs by
+/-0.5-2 Hz, switch window, change taps, toggle notch) or, when the
orchestrator passes a crossover task containing two parent sources,
recombine the parents' band edges / window / taps.

LEAKAGE LAW (enforced by construction): this module never imports the
evaluator and its ``proposer`` entry point receives only
(task, generation, feedback). It has no access to any seed values.
All randomness comes from ``random.Random(SEED + ...)`` so runs are
fully deterministic.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, replace

SEED = 20240517  # module-level determinism constant

INVARIANTS = ["len(result) == len(x)", "max(result) > min(result)"]

WINDOWS = {"hamming": "np.hamming", "hann": "np.hanning",
           "blackman": "np.blackman"}
TAPS = (201, 401, 801, 1201)

# Where the physics says the answer lives (5 Hz target): the mutation base
# pool for generations > 0 sweeps bandpass edges around 3-7 Hz with
# selective (801-tap) kernels — wide-transition low-taps filters are
# heavily penalized by the evaluator's distortion term, so the pool stays
# at 801+ taps. Exactly 5 entries: the orchestrator takes [:n_variants].
_MUTATION_BASES: list[dict] = [
    dict(kind="bandpass", fc_low=3.0, fc_high=7.0, taps=801,
         window="hamming", notch=0),
    dict(kind="bandpass", fc_low=4.0, fc_high=6.0, taps=801,
         window="hamming", notch=0),
    dict(kind="bandpass", fc_low=3.5, fc_high=6.5, taps=1201,
         window="hann", notch=0),
    dict(kind="bandpass", fc_low=2.5, fc_high=7.5, taps=801,
         window="blackman", notch=1),
    dict(kind="lowpass", fc_low=0.0, fc_high=12.0, taps=801,
         window="hamming", notch=0),
]

_SEED_POOL: list[dict] = [
    dict(kind="moving_average", fc_low=0.0, fc_high=0.0, taps=25,
         window="none", notch=0),
    dict(kind="lowpass", fc_low=0.0, fc_high=12.0, taps=401,
         window="hamming", notch=0),
    dict(kind="lowpass", fc_low=0.0, fc_high=25.0, taps=401,
         window="hamming", notch=0),
    dict(kind="bandpass", fc_low=2.0, fc_high=8.0, taps=801,
         window="hamming", notch=0),
]

_PARAM_RE = re.compile(
    r"#\s*torsion-filter\s+kind=(\S+)\s+fc_low=([\d.]+)\s+fc_high=([\d.]+)"
    r"\s+taps=(\d+)\s+window=(\S+)\s+notch=([01])")


@dataclass(frozen=True)
class Params:
    kind: str        # "moving_average" | "lowpass" | "bandpass"
    fc_low: float
    fc_high: float
    taps: int
    window: str
    notch: int       # 0/1: subtract a narrow 58-62 Hz bandpass


def parse_params(code: str) -> Params | None:
    m = _PARAM_RE.search(code)
    if not m:
        return None
    return Params(kind=m.group(1), fc_low=float(m.group(2)),
                  fc_high=float(m.group(3)), taps=int(m.group(4)),
                  window=m.group(5), notch=int(m.group(6)))


def _emit(p: Params) -> str:
    hdr = (f"# torsion-filter kind={p.kind} fc_low={p.fc_low} "
           f"fc_high={p.fc_high} taps={p.taps} window={p.window} "
           f"notch={p.notch}")
    if p.kind == "moving_average":
        body = f'''k = np.ones({p.taps}) / float({p.taps})
    return np.convolve(x, k, mode="same")'''
        return f"import numpy as np\n\n{hdr}\ndef apply_filter(x, fs):\n    {body}\n"
    win = WINDOWS[p.window]
    lines = [
        f"n = {p.taps}",
        "m = np.arange(n) - (n - 1) / 2.0",
        f"w = {win}(n)",
        f"k = np.sinc(2 * {p.fc_high} / fs * m) * w",
        "k /= k.sum()",
    ]
    if p.kind == "bandpass":
        lines += [
            f"h_lo = np.sinc(2 * {p.fc_low} / fs * m) * w",
            "h_lo /= h_lo.sum()",
            "k = k - h_lo",
        ]
    if p.notch:
        lines += [
            "n58 = np.sinc(2 * 58.0 / fs * m) * w; n58 /= n58.sum()",
            "n62 = np.sinc(2 * 62.0 / fs * m) * w; n62 /= n62.sum()",
            "k = k - (n62 - n58)",
        ]
    lines += [
        'y = np.convolve(x, k, mode="same")',
        'return np.convolve(y[::-1], k, mode="same")[::-1]',
    ]
    body = "\n    ".join(lines)
    return f"import numpy as np\n\n{hdr}\ndef apply_filter(x, fs):\n    {body}\n"


def _clamp(p: Params) -> Params:
    if p.kind == "moving_average":
        return p
    fc_low = min(max(p.fc_low, 0.5), 55.0)
    fc_high = min(max(p.fc_high, fc_low + 0.5), 60.0)
    if p.kind == "lowpass":
        fc_low = 0.0
    taps = p.taps if p.taps in TAPS else min(TAPS, key=lambda t: abs(t - p.taps))
    window = p.window if p.window in WINDOWS else "hamming"
    return replace(p, fc_low=round(fc_low, 2), fc_high=round(fc_high, 2),
                   taps=taps, window=window)


def _mutate(base: Params, rng: random.Random) -> Params:
    p = base
    if p.kind != "moving_average":
        # perturb cutoffs by +/- 0.5..2 Hz (sign randomized)
        dl = rng.uniform(0.5, 2.0) * rng.choice([-1.0, 1.0])
        dh = rng.uniform(0.5, 2.0) * rng.choice([-1.0, 1.0])
        p = replace(p, fc_low=p.fc_low + dl, fc_high=p.fc_high + dh)
    if rng.random() < 0.5:
        p = replace(p, window=rng.choice(sorted(WINDOWS)))
    if rng.random() < 0.5:
        p = replace(p, taps=rng.choice(TAPS))
    if rng.random() < 0.3:
        p = replace(p, notch=1 - p.notch)
    return _clamp(p)


def _crossover(pa: Params, pb: Params, rng: random.Random) -> list[Params]:
    """fc_low from one parent's band edges, fc_high from the other's;
    second child swaps window/taps instead."""
    out = []
    if pa.kind != "moving_average" and pb.kind != "moving_average":
        c1 = replace(pa, fc_low=min(pa.fc_low, pb.fc_low),
                     fc_high=max(pa.fc_high, pb.fc_high))
        lo, hi = sorted([pa.fc_high, pb.fc_low])
        c2 = replace(pb, fc_low=lo, fc_high=hi)
        out += [_clamp(c1), _clamp(c2)]
    c3 = replace(pa, window=pb.window, taps=pb.taps, notch=pb.notch)
    out.append(_clamp(c3))
    return out


def _parents_from_task(task: str) -> list[Params]:
    """Parse parent param headers out of an orchestrator crossover task."""
    found = [parse_params(m) for m in
             re.findall(r"(import numpy.*?)(?=\nParent [AB]:|\Z)",
                        task, re.DOTALL)]
    return [p for p in found if p is not None]


def proposer(task: str, generation: int,
             feedback: list[str]) -> list[tuple[str, list[str], str]]:
    """Orchestrator proposer protocol: (task, generation, feedback) ->
    [(code, invariants, mutation_op)]. Fully deterministic via SEED."""
    rng = random.Random(SEED + 7919 * generation + 31 * len(task))
    if generation == 0:
        return [(_emit(Params(**p)), list(INVARIANTS), "seed")
                for p in _SEED_POOL]
    parents = _parents_from_task(task)
    if len(parents) >= 2:
        return [(_emit(p), list(INVARIANTS), "crossover")
                for p in _crossover(parents[0], parents[1], rng)]
    bases = [Params(**b) for b in _MUTATION_BASES]
    rng.shuffle(bases)
    return [(_emit(_mutate(b, rng)), list(INVARIANTS), "point")
            for b in bases]
