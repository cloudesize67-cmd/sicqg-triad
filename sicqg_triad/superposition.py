"""Level 0 superposition registry.

Variants exist in a "superposed" state until dispatched/verified/committed.
Persistence is an append-only JSONL file; state is rebuilt on init.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field


@dataclass
class Variant:
    """A candidate program in the superposition registry."""

    id: str  # uuid4 hex
    code: str  # candidate source (python function body or full def)
    invariants: list[str]  # Z3-checkable claims, e.g. "result >= 0"
    parent_ids: list[str]
    generation: int
    mutation_op: str  # "seed" | "point" | "crossover" | ...
    status: str  # "superposed"|"dispatched"|"verified"|"fatal"|"committed"|"pruned"
    metadata: dict = field(default_factory=dict)


class SuperpositionRegistry:
    """Append-only JSONL-backed registry of variants."""

    def __init__(self, path: str, capacity: int = 10000):
        self.path = path
        self.capacity = capacity
        self._variants: dict[str, Variant] = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    op = rec.get("op")
                    if op == "add":
                        v = Variant(**rec["variant"])
                        self._variants[v.id] = v
                    elif op == "status":
                        vid = rec["id"]
                        if vid in self._variants:
                            self._variants[vid].status = rec["status"]
                    elif op == "prune":
                        for vid in rec["ids"]:
                            self._variants.pop(vid, None)

    def _append(self, rec: dict) -> None:
        d = os.path.dirname(os.path.abspath(self.path))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def add(self, v: Variant) -> str:
        """Register a variant; returns its id."""
        self._variants[v.id] = v
        self._append({"op": "add", "variant": asdict(v)})
        self._enforce_capacity()
        return v.id

    def get(self, vid: str) -> Variant:
        """Return the variant with the given id (KeyError if absent)."""
        return self._variants[vid]

    def update_status(self, vid: str, status: str) -> None:
        """Update a variant's lifecycle status."""
        self._variants[vid].status = status
        self._append({"op": "status", "id": vid, "status": status})

    def query(self, status: str | None = None,
              generation: int | None = None) -> list[Variant]:
        """Filter variants by status and/or generation."""
        out = []
        for v in self._variants.values():
            if status is not None and v.status != status:
                continue
            if generation is not None and v.generation != generation:
                continue
            out.append(v)
        return out

    def lineage(self, vid: str) -> list[Variant]:
        """Ancestors of vid, root-first (includes vid itself last)."""
        chain: list[Variant] = []
        seen: set[str] = set()
        cur = vid
        while cur and cur in self._variants and cur not in seen:
            seen.add(cur)
            v = self._variants[cur]
            chain.append(v)
            cur = v.parent_ids[0] if v.parent_ids else None
        chain.reverse()
        return chain

    def prune(self, keep_statuses: set[str]) -> int:
        """Remove variants whose status is not in keep_statuses.

        Also enforces capacity: if still over capacity, removes oldest
        (lowest generation first) variants. Returns count removed.
        """
        doomed = [vid for vid, v in self._variants.items()
                  if v.status not in keep_statuses]
        remaining = len(self._variants) - len(doomed)
        if remaining > self.capacity:
            surplus = remaining - self.capacity
            rest = sorted(
                ((vid, v) for vid, v in self._variants.items()
                 if v.status in keep_statuses),
                key=lambda kv: kv[1].generation,
            )
            doomed.extend(vid for vid, _ in rest[:surplus])
        for vid in doomed:
            self._variants.pop(vid, None)
        if doomed:
            self._append({"op": "prune", "ids": doomed})
        return len(doomed)

    def _enforce_capacity(self) -> None:
        if len(self._variants) > self.capacity:
            self.prune({"superposed", "dispatched", "verified", "committed"})
