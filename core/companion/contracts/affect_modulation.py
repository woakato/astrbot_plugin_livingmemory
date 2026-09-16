"""Bounded affect modulation DTO shared across companion plugins."""

from __future__ import annotations

import hashlib
import math
from typing import Any, Mapping

AFFECT_MODULATION_VERSION = "affect_modulation.v1"
AFFECT_MODULATION_FIELDS = ("schema_version", "valence", "arousal", "vulnerability", "confidence", "source_event_ids", "computed_at")
AFFECT_MODULATION_FINGERPRINT = hashlib.sha256("|".join(AFFECT_MODULATION_FIELDS).encode("ascii")).hexdigest()[:20]


def _number(value: Any, default: float, low: float, high: float) -> float:
    if type(value) not in {int, float}:
        return default
    numeric = float(value)
    if not math.isfinite(numeric):
        return default
    return round(max(low, min(high, numeric)), 4)


def normalize_affect_modulation(value: Any) -> dict[str, Any]:
    source = dict(value) if isinstance(value, Mapping) else {}
    raw_ids = source.get("source_event_ids")
    ids: list[str] = []
    if isinstance(raw_ids, (list, tuple)):
        for item in raw_ids[:12]:
            if type(item) is not str:
                continue
            cleaned = " ".join(item.split())[:96]
            if cleaned and cleaned not in ids:
                ids.append(cleaned)
    computed_at = source.get("computed_at")
    return {
        "schema_version": AFFECT_MODULATION_VERSION,
        "valence": _number(source.get("valence"), 0.0, -1.0, 1.0),
        "arousal": _number(source.get("arousal"), 0.0, 0.0, 1.0),
        "vulnerability": _number(source.get("vulnerability"), 0.0, 0.0, 1.0),
        "confidence": _number(source.get("confidence"), 0.0, 0.0, 1.0),
        "source_event_ids": ids,
        "computed_at": float(computed_at) if type(computed_at) in {int, float} and math.isfinite(float(computed_at)) else 0.0,
    }


__all__ = ["AFFECT_MODULATION_FIELDS", "AFFECT_MODULATION_FINGERPRINT", "AFFECT_MODULATION_VERSION", "normalize_affect_modulation"]
