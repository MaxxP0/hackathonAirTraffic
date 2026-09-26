"""Supplemental arrival service-time diagnostics from an existing observation.

The clock starts when an arrival enters the simulated sector and stops at
touchdown, crash, or sector exit. It includes normal approach flight, holding
and go-arounds: this is not a measurement of excess delay or queue time alone.
Pending arrivals retain their observed wait at the episode horizon. Diversions
are classified as failed as soon as commanded, while their clock continues
until sector exit. Read the outcome counts alongside the wait and score: an
early crash or diversion must not be interpreted as efficient landing service.

This module does not change simulator state, model input, scalar score or rank.
"""
from __future__ import annotations

import math


_OUTCOME = {
    "landing_roll": "landed", "taxi_in": "landed", "landed": "landed",
    "inbound": "pending", "holding": "pending", "approach": "pending",
    "diverting": "failed", "diverted": "failed", "crashed": "failed",
}


def _summary(values: list[float]) -> dict:
    return {"count": len(values), "seconds": round(sum(values), 3),
            "mean_seconds": round(sum(values) / len(values), 3) if values else None,
            "max_seconds": round(max(values), 3) if values else None}


def landing_metrics(observation: dict) -> dict:
    """Summarize all spawned arrivals, including pending and failed arrivals.

    ``airborne_time_s`` is the simulator's existing accumulated clock; elapsed
    episode time is not used, so runway roll and taxi do not extend landing wait.
    Empty populations return null mean, maximum and diagnostic score.
    """
    groups: dict[str, list[float]] = {key: [] for key in ("landed", "pending", "failed")}
    for aircraft in observation.get("aircraft", []):
        if aircraft.get("kind") != "arrival":
            continue
        status = aircraft.get("status")
        if status not in _OUTCOME:
            raise ValueError(f"Unknown arrival status for landing metrics: {status!r}")
        value = aircraft.get("airborne_time_s")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("Arrival airborne_time_s must be a finite nonnegative number")
        groups[_OUTCOME[status]].append(value)
    values = [value for group in groups.values() for value in group]
    overall = _summary(values)
    mean = sum(values) / len(values) if values else None
    return {"landing_wait_count": overall["count"], "landing_wait_seconds": overall["seconds"],
            "landing_wait_mean_seconds": overall["mean_seconds"], "landing_wait_max_seconds": overall["max_seconds"],
            "landing_wait_score": round(100 / (1 + mean / 600), 3) if mean is not None else None,
            "landing_wait_by_outcome": {key: _summary(group) for key, group in groups.items()}}
