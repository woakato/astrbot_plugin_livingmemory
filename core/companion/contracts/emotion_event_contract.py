"""Versioned, redacted emotion-event contract shared across companion plugins."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import re
from typing import Any, Mapping


EMOTION_EVENT_SCHEMA_VERSION = "companion_emotion_event.v1"
EMOTION_EVENT_TYPES = frozenset({
    "neutral", "hurt", "apology", "comfort", "praise", "comfort_need",
    "external_negative", "scar_touched", "warm_memory", "vulnerable_resonance",
    "play", "intimacy", "boundary",
})
EMOTION_EVENT_ORIGINS = frozenset({"interaction", "memory_recall", "system_condition"})
EMOTION_EVENT_STATUSES = frozenset({"observed", "revised", "applied", "ignored", "expired"})
EMOTION_EVENT_CONTRACT_FIELDS = (
    "schema_version", "event_id", "trace_id", "revision", "producer_plugin",
    "origin_kind", "platform", "bot_id", "scope", "session_id", "actor_ref",
    "target_ref", "quoted_target_ref", "event_type", "intensity", "confidence",
    "valence_hint", "arousal_hint", "vulnerability_hint", "source_rule",
    "occurred_at", "expires_at", "dedupe_key", "payload_hash", "privacy_level",
    "applied_interaction", "applied_energy_delta", "correction_of", "status",
    "reason_codes",
)
EMOTION_EVENT_CONTRACT_FINGERPRINT = hashlib.sha256(
    "|".join(EMOTION_EVENT_CONTRACT_FIELDS).encode("ascii")
).hexdigest()[:20]


def _text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "").replace("\u3000", " ")).strip()
    return text[:limit]


def _number(value: Any, default: float, low: float, high: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return round(max(low, min(high, number)), 4)


def _integer(value: Any, default: int = 1) -> int:
    if isinstance(value, bool):
        return default
    try:
        return max(1, min(1_000_000, int(value)))
    except (TypeError, ValueError):
        return default


def _entity(value: Any) -> dict[str, str]:
    source = value if isinstance(value, Mapping) else {}
    return {
        "kind": _text(source.get("kind"), 24) or "unknown",
        "id": _text(source.get("id"), 160),
        "role": _text(source.get("role"), 40),
    }


def _timestamp(value: Any) -> str:
    text = _text(value, 48)
    if text:
        return text
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stable_hash(*parts: Any) -> str:
    raw = "|".join(_text(part, 500) for part in parts)
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()


def normalize_emotion_event(value: Any, *, producer_plugin: str = "") -> dict[str, Any]:
    """Return a bounded JSON-safe event without retaining raw message content."""
    source = dict(value) if isinstance(value, Mapping) else {}
    producer = _text(source.get("producer_plugin") or producer_plugin, 80) or "unknown"
    origin = _text(source.get("origin_kind"), 40)
    if origin not in EMOTION_EVENT_ORIGINS:
        origin = "interaction"
    event_type = _text(source.get("event_type"), 48).lower()
    if event_type not in EMOTION_EVENT_TYPES:
        event_type = "neutral"
    status = _text(source.get("status"), 24).lower()
    if status not in EMOTION_EVENT_STATUSES:
        status = "observed"
    session_id = _text(source.get("session_id"), 220)
    dedupe_key = _text(source.get("dedupe_key"), 160)
    payload_hash = _text(source.get("payload_hash"), 80)
    if not payload_hash:
        payload_hash = _stable_hash(
            producer, origin, session_id, event_type, dedupe_key,
            source.get("message_fingerprint"), source.get("source_rule"),
        )
    event_id = _text(source.get("event_id"), 96)
    if not event_id:
        event_id = "emo_" + _stable_hash(producer, origin, session_id, event_type, dedupe_key, payload_hash)[:24]
    trace_id = _text(source.get("trace_id"), 96)
    if not trace_id:
        trace_id = "etr_" + _stable_hash(session_id, dedupe_key or event_id)[:24]
    reason_codes = source.get("reason_codes")
    if not isinstance(reason_codes, (list, tuple)):
        reason_codes = []
    clean_reasons: list[str] = []
    for item in reason_codes[:12]:
        reason = _text(item, 64)
        if reason and reason not in clean_reasons:
            clean_reasons.append(reason)
    return {
        "schema_version": EMOTION_EVENT_SCHEMA_VERSION,
        "event_id": event_id,
        "trace_id": trace_id,
        "revision": _integer(source.get("revision"), 1),
        "producer_plugin": producer,
        "origin_kind": origin,
        "platform": _text(source.get("platform"), 80),
        "bot_id": _text(source.get("bot_id"), 160),
        "scope": _text(source.get("scope"), 24) or "private",
        "session_id": session_id,
        "actor_ref": _entity(source.get("actor_ref")),
        "target_ref": _entity(source.get("target_ref")),
        "quoted_target_ref": _entity(source.get("quoted_target_ref")),
        "event_type": event_type,
        "intensity": _number(source.get("intensity"), 0.0, 0.0, 100.0),
        "confidence": _number(source.get("confidence"), 0.0, 0.0, 1.0),
        "valence_hint": _number(source.get("valence_hint"), 0.0, -1.0, 1.0),
        "arousal_hint": _number(source.get("arousal_hint"), 0.0, 0.0, 1.0),
        "vulnerability_hint": _number(source.get("vulnerability_hint"), 0.0, 0.0, 1.0),
        "source_rule": _text(source.get("source_rule"), 80),
        "occurred_at": _timestamp(source.get("occurred_at")),
        "expires_at": _text(source.get("expires_at"), 48),
        "dedupe_key": dedupe_key or payload_hash[:32],
        "payload_hash": payload_hash,
        "privacy_level": _text(source.get("privacy_level"), 24) or "redacted",
        "applied_interaction": _text(source.get("applied_interaction"), 32),
        "applied_energy_delta": _number(source.get("applied_energy_delta"), 0.0, -100.0, 100.0),
        "correction_of": _text(source.get("correction_of"), 96),
        "status": status,
        "reason_codes": clean_reasons,
    }


def emotion_event_json(value: Any, *, producer_plugin: str = "") -> str:
    return json.dumps(normalize_emotion_event(value, producer_plugin=producer_plugin), ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "EMOTION_EVENT_CONTRACT_FIELDS",
    "EMOTION_EVENT_CONTRACT_FINGERPRINT",
    "EMOTION_EVENT_SCHEMA_VERSION",
    "EMOTION_EVENT_STATUSES",
    "EMOTION_EVENT_TYPES",
    "emotion_event_json",
    "normalize_emotion_event",
]
