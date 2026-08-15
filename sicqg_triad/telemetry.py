"""Per-generation telemetry for the orchestration loop.

``TelemetryEvent`` captures one generation of the demand() loop:
how many variants were proposed, how many went fatal, archive coverage,
best fitness so far, and descriptor drift (mean pairwise Euclidean
distance between the elite descriptors inserted this generation and the
previous one; 0.0 for generation 0 or when either generation inserted
nothing).

``TelemetryLogger`` optionally appends each event as one JSON line to a
JSONL file and always keeps events in memory for ``summary()``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass


@dataclass
class TelemetryEvent:
    generation: int
    n_proposed: int
    n_fatal: int
    archive_coverage: float
    best_fitness: float
    descriptor_drift: float


class TelemetryLogger:
    """Records TelemetryEvents; JSONL sink optional.

    Parameters
    ----------
    path : str | None
        When set, every ``log()`` appends one JSON line to this file.
        When None, events are kept in memory only.
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = path
        self.events: list[TelemetryEvent] = []

    def log(self, event: TelemetryEvent) -> None:
        self.events.append(event)
        if self.path is not None:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(event)) + "\n")

    def summary(self) -> dict:
        """Aggregate stats over all recorded events.

        Returns {fatal_rate, coverage_trend, max_drift, events} where
        fatal_rate = total fatal / total proposed (0.0 if no proposals),
        coverage_trend = (first coverage, last coverage), max_drift =
        largest descriptor drift observed, events = count.
        """
        if not self.events:
            return {"fatal_rate": 0.0, "coverage_trend": (0.0, 0.0),
                    "max_drift": 0.0, "events": 0}
        n_prop = sum(e.n_proposed for e in self.events)
        n_fatal = sum(e.n_fatal for e in self.events)
        return {
            "fatal_rate": (n_fatal / n_prop) if n_prop else 0.0,
            "coverage_trend": (self.events[0].archive_coverage,
                               self.events[-1].archive_coverage),
            "max_drift": max(e.descriptor_drift for e in self.events),
            "events": len(self.events),
        }
