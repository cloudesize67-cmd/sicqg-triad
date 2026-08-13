"""CVT-MAP-Elites archive with multi-island populations and migration.

Uses numpy when available; otherwise a pure-python fallback (small built-in
kmeans on random sample points) so the module works on Termux without numpy.
Deterministic: all randomness flows through `seed` or a caller-provided rng.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

try:
    import numpy as np
except ImportError:  # pragma: no cover - exercised on Termux
    np = None


@dataclass
class Elite:
    """An archived solution occupying a behavioral niche."""

    variant_id: str
    fitness: float
    descriptors: tuple[float, ...]  # behavior descriptors
    island: int


def _kmeans(points: list[tuple[float, ...]], k: int, dim: int,
            seed: int, iters: int = 20) -> list[tuple[float, ...]]:
    """Small deterministic k-means (pure python)."""
    rng = random.Random(seed)
    centroids = [list(p) for p in rng.sample(points, min(k, len(points)))]
    while len(centroids) < k:  # not enough points; pad with copies
        centroids.append(list(rng.choice(points)))
    for _ in range(iters):
        buckets: list[list[list[float]]] = [[] for _ in range(k)]
        for p in points:
            i = min(range(k),
                    key=lambda j: sum((a - b) ** 2 for a, b in zip(p, centroids[j])))
            buckets[i].append(list(p))
        for j, bucket in enumerate(buckets):
            if bucket:
                centroids[j] = [sum(col) / len(bucket) for col in zip(*bucket)]
    return [tuple(c) for c in centroids]


def _dist2(a, b) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b))


class CVTMapElites:
    """CVT-MAP-Elites archive partitioned into `n_islands` islands."""

    def __init__(self, n_niches: int, n_islands: int, descriptor_dim: int,
                 centroids: "np.ndarray | None" = None, seed: int = 0):
        if n_niches < 1 or n_islands < 1 or descriptor_dim < 1:
            raise ValueError("n_niches, n_islands, descriptor_dim must be >= 1")
        self.n_niches = n_niches
        self.n_islands = n_islands
        self.descriptor_dim = descriptor_dim
        self.seed = seed
        if centroids is not None:
            if np is not None and isinstance(centroids, np.ndarray):
                cents = [tuple(float(x) for x in row) for row in centroids.tolist()]
            else:
                cents = [tuple(float(x) for x in row) for row in centroids]
            if len(cents) != n_niches or any(len(c) != descriptor_dim for c in cents):
                raise ValueError("centroids shape mismatch")
            self.centroids = cents
        else:
            rng = random.Random(seed)
            samples = [tuple(rng.random() for _ in range(descriptor_dim))
                       for _ in range(max(n_niches * 10, 100))]
            self.centroids = _kmeans(samples, n_niches, descriptor_dim, seed)
        # archive[island][niche] -> Elite
        self._archive: list[dict[int, Elite]] = [
            {} for _ in range(n_islands)]

    def _niche(self, descriptors: tuple[float, ...]) -> int:
        """Index of nearest centroid (Euclidean, deterministic tie-break)."""
        return min(range(self.n_niches),
                   key=lambda j: (_dist2(descriptors, self.centroids[j]), j))

    def add(self, elite: Elite) -> bool:
        """Insert elite; True if inserted or replaced a weaker occupant."""
        if not (0 <= elite.island < self.n_islands):
            raise ValueError(f"island must be in [0, {self.n_islands})")
        if len(elite.descriptors) != self.descriptor_dim:
            raise ValueError("descriptor dimension mismatch")
        niche = self._niche(elite.descriptors)
        cell = self._archive[elite.island]
        cur = cell.get(niche)
        if cur is None or elite.fitness > cur.fitness:
            cell[niche] = elite
            return True
        return False

    def sample_parents(self, k: int, rng) -> list[Elite]:
        """Sample k elites uniformly at random across all islands."""
        pool = [e for cell in self._archive for e in cell.values()]
        if not pool:
            return []
        return [rng.choice(pool) for _ in range(k)]

    def migrate(self, rate: float = 0.1) -> int:
        """Exchange elites between islands (ring topology).

        Each island sends copies of up to ceil(rate * size) of its best
        elites to the next island. Returns number of successful insertions.
        """
        rng = random.Random(self.seed)
        moved = 0
        # snapshot to avoid double-hopping within one migration round
        snapshot = [dict(cell) for cell in self._archive]
        for i in range(self.n_islands):
            elites = sorted(snapshot[i].values(),
                            key=lambda e: -e.fitness)
            n_send = max(1, math.ceil(rate * len(elites))) if elites else 0
            for e in elites[:n_send]:
                dest = (i + 1) % self.n_islands
                copy = Elite(variant_id=e.variant_id, fitness=e.fitness,
                             descriptors=e.descriptors, island=dest)
                if self.add(copy):
                    moved += 1
        return moved

    def best(self) -> Elite | None:
        """Highest-fitness elite across all islands, or None if empty."""
        pool = [e for cell in self._archive for e in cell.values()]
        if not pool:
            return None
        return max(pool, key=lambda e: e.fitness)

    def coverage(self) -> float:
        """Fraction of occupied niches (union across islands / n_niches)."""
        if self.n_niches == 0:
            return 0.0
        total = sum(len(cell) for cell in self._archive)
        return total / (self.n_niches * self.n_islands)
