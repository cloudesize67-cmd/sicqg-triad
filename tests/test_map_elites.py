"""Tests for CVT-MAP-Elites."""

import random

import pytest

from sicqg_triad.map_elites import CVTMapElites, Elite


def make_archive(n_niches=8, n_islands=2, dim=2):
    return CVTMapElites(n_niches=n_niches, n_islands=n_islands, descriptor_dim=dim)


def test_add_and_best():
    arch = make_archive()
    assert arch.best() is None
    e1 = Elite("a", 1.0, (0.1, 0.1), 0)
    e2 = Elite("b", 2.0, (0.9, 0.9), 0)
    assert arch.add(e1) and arch.add(e2)
    assert arch.best().variant_id == "b"


def test_niche_replacement_only_if_better():
    arch = make_archive(n_niches=1, n_islands=1)
    assert arch.add(Elite("weak", 1.0, (0.5, 0.5), 0))
    assert not arch.add(Elite("weaker", 0.5, (0.5, 0.5), 0))
    assert arch.add(Elite("strong", 3.0, (0.5, 0.5), 0))
    assert arch.best().variant_id == "strong"


def test_coverage_and_islands():
    arch = make_archive(n_niches=4, n_islands=2)
    arch.add(Elite("a", 1.0, (0.0, 0.0), 0))
    arch.add(Elite("b", 1.0, (1.0, 1.0), 1))
    assert 0.0 < arch.coverage() <= 1.0
    with pytest.raises(ValueError):
        arch.add(Elite("bad", 1.0, (0.0, 0.0), 5))
    with pytest.raises(ValueError):
        arch.add(Elite("bad2", 1.0, (0.0,), 0))


def test_sample_parents_deterministic():
    arch = make_archive()
    for i in range(5):
        arch.add(Elite(f"e{i}", float(i), (i / 10.0, i / 10.0), i % 2))
    parents = arch.sample_parents(3, random.Random(42))
    assert len(parents) == 3
    parents2 = arch.sample_parents(3, random.Random(42))
    assert [p.variant_id for p in parents] == [p.variant_id for p in parents2]
    assert make_archive().sample_parents(2, random.Random(0)) == []


def test_migrate_copies_to_neighbor_island():
    arch = make_archive(n_niches=4, n_islands=3)
    arch.add(Elite("solo", 5.0, (0.5, 0.5), 0))
    moved = arch.migrate(rate=1.0)
    assert moved >= 1
    # elite now present on island 1 as well
    hits = [e for cell in arch._archive for e in cell.values()
            if e.variant_id == "solo"]
    assert {e.island for e in hits} == {0, 1}


def test_custom_centroids():
    cents = [(0.0, 0.0), (1.0, 1.0)]
    arch = CVTMapElites(2, 1, 2, centroids=cents)
    assert arch._niche((0.1, 0.2)) == 0
    assert arch._niche((0.9, 0.8)) == 1


def test_numpy_centroids_if_available():
    np = pytest.importorskip("numpy")
    cents = np.array([[0.0, 0.0], [1.0, 1.0]])
    arch = CVTMapElites(2, 1, 2, centroids=cents)
    assert arch._niche((0.8, 0.9)) == 1
