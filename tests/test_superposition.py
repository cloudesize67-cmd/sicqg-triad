"""Tests for the superposition registry."""

import uuid

import pytest

from sicqg_triad.superposition import SuperpositionRegistry, Variant


def make_variant(status="superposed", generation=0, parents=None, op="seed"):
    return Variant(
        id=uuid.uuid4().hex,
        code="return x",
        invariants=["result >= 0"],
        parent_ids=parents or [],
        generation=generation,
        mutation_op=op,
        status=status,
        metadata={},
    )


def test_add_get_roundtrip(tmp_path):
    reg = SuperpositionRegistry(str(tmp_path / "reg.jsonl"))
    v = make_variant()
    assert reg.add(v) == v.id
    got = reg.get(v.id)
    assert got.id == v.id and got.status == "superposed"
    with pytest.raises(KeyError):
        reg.get("missing")


def test_persistence_across_instances(tmp_path):
    path = str(tmp_path / "reg.jsonl")
    reg = SuperpositionRegistry(path)
    v = make_variant()
    reg.add(v)
    reg.update_status(v.id, "verified")
    reg2 = SuperpositionRegistry(path)  # reload from JSONL
    assert reg2.get(v.id).status == "verified"
    assert reg2.get(v.id).code == v.code


def test_query_filters(tmp_path):
    reg = SuperpositionRegistry(str(tmp_path / "reg.jsonl"))
    a = make_variant(status="superposed", generation=0)
    b = make_variant(status="verified", generation=1)
    c = make_variant(status="superposed", generation=1)
    for v in (a, b, c):
        reg.add(v)
    assert {v.id for v in reg.query(status="superposed")} == {a.id, c.id}
    assert {v.id for v in reg.query(generation=1)} == {b.id, c.id}
    assert reg.query(status="superposed", generation=1)[0].id == c.id
    assert len(reg.query()) == 3


def test_lineage_root_first(tmp_path):
    reg = SuperpositionRegistry(str(tmp_path / "reg.jsonl"))
    root = make_variant()
    child = make_variant(parents=[root.id], generation=1, op="point")
    grand = make_variant(parents=[child.id], generation=2, op="point")
    for v in (root, child, grand):
        reg.add(v)
    lin = reg.lineage(grand.id)
    assert [v.id for v in lin] == [root.id, child.id, grand.id]


def test_prune_by_status(tmp_path):
    path = str(tmp_path / "reg.jsonl")
    reg = SuperpositionRegistry(path)
    keep = [make_variant(status="verified", generation=i) for i in range(3)]
    dead = [make_variant(status="fatal") for _ in range(2)]
    for v in keep + dead:
        reg.add(v)
    removed = reg.prune({"verified"})
    assert removed == 2
    assert {v.id for v in reg.query()} == {v.id for v in keep}
    # prune survives reload (append-only log replays the removal)
    reg2 = SuperpositionRegistry(path)
    assert len(reg2.query()) == 3


def test_capacity_bounding():
    import tempfile, os
    with tempfile.TemporaryDirectory() as d:
        reg = SuperpositionRegistry(os.path.join(d, "reg.jsonl"), capacity=3)
        for i in range(6):
            reg.add(make_variant(status="verified", generation=i))
        assert len(reg.query()) <= 3
