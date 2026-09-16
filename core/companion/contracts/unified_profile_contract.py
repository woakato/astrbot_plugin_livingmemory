"""REQ-036 minimal cross-plugin unified-profile contract.

The contract deliberately contains references and policy state only.  Raw chat
text, evidence bodies, and authority-owned portrait records never cross this
boundary.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any


CONTRACT_NAME = "chat.unified_profile.v1"
CONTRACT_VERSION = "1.0"
SCHEMA_VERSION = "1.0"
MAX_DTO_BYTES = 32768

PORTRAIT_MODES = frozenset({"disabled", "use_existing", "learn_and_use"})
PORTRAIT_PURPOSES = frozenset({"adapt_for_subject", "summarize_to_subject", "disclose_to_third_party"})
IDENTITY_ASSURANCE = frozenset({"unverified", "observed", "verified", "explicit_linked"})
PROFILE_STATUS = frozenset({"active", "suspended", "quarantined", "deleted"})
LOW_SENSITIVITY = "low"

PERSON_REF_FIELDS = (
    "person_id",
    "resolved_identity_key",
    "projection_revision",
    "identity_assurance",
    "profile_status",
)
CAPABILITY_FIELDS = (
    "private_companion_enabled",
    "proactive_private_enabled",
    "effective_proactive_private_enabled",
    "portrait_mode",
    "portrait_learning_enabled",
    "portrait_usage_enabled",
    "grant_source",
    "blocked_reasons",
)
DTO_FIELDS = (
    "contract_name",
    "contract_version",
    "contract_fingerprint",
    "schema_version",
    "person_ref",
    "identity_summary",
    "expression_summary",
    "capability_summary",
    "portrait_summary",
    "context_overlays",
    "bridge_status",
)
FORBIDDEN_KEYS = frozenset(
    {
        "content",
        "chat_text",
        "evidence",
        "messages",
        "transcript",
        "prompt",
        "raw_prompt",
        "raw_text",
        "private_object",
        "database",
    }
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _shape() -> dict[str, Any]:
    return {
        "name": CONTRACT_NAME,
        "version": CONTRACT_VERSION,
        "schema": SCHEMA_VERSION,
        "person_ref": list(PERSON_REF_FIELDS),
        "capability_summary": list(CAPABILITY_FIELDS),
        "dto": list(DTO_FIELDS),
        "portrait_modes": sorted(PORTRAIT_MODES),
        "portrait_purposes": sorted(PORTRAIT_PURPOSES),
        "max_bytes": MAX_DTO_BYTES,
    }


CONTRACT_FINGERPRINT = hashlib.sha256(_canonical(_shape()).encode("utf-8")).hexdigest()[:16]


def _text(value: Any, limit: int = 240) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    result = str(value).strip()
    if not result or "\x00" in result:
        return ""
    return result[:limit]


def _safe(value: Any, depth: int = 0) -> Any:
    if depth > 2 or value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return _text(value, 240)
    if isinstance(value, (list, tuple)):
        return [_safe(item, depth + 1) for item in list(value)[:16]]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:24]:
            name = _text(key, 80).lower()
            if not name or name in FORBIDDEN_KEYS:
                continue
            safe = _safe(item, depth + 1)
            if safe not in (None, "", [], {}):
                result[name] = safe
        return result
    return None


def _contains_forbidden(value: Any, depth: int = 0) -> bool:
    """Reject caller-supplied DTOs that smuggle conversation material in.

    ``build_profile_dto`` already drops these fields, but validation is also a
    trust boundary because the Memory bridge receives an event-carried DTO.
    """
    if depth > 4:
        return True
    if isinstance(value, dict):
        for key, item in value.items():
            name = _text(key, 80).lower()
            if not name or name in FORBIDDEN_KEYS:
                return True
            if _contains_forbidden(item, depth + 1):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_forbidden(item, depth + 1) for item in value)
    return False


def _positive_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def normalize_portrait_mode(value: Any) -> str:
    mode = _text(value, 40).lower()
    aliases = {
        "off": "disabled",
        "disabled": "disabled",
        "use": "use_existing",
        "use_existing": "use_existing",
        "learn": "learn_and_use",
        "learn_and_use": "learn_and_use",
    }
    return aliases.get(mode, "disabled")


def portrait_mode_flags(mode: Any) -> tuple[bool, bool]:
    normalized = normalize_portrait_mode(mode)
    return normalized == "learn_and_use", normalized in {"use_existing", "learn_and_use"}


def build_person_ref(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    result = {
        "person_id": _text(source.get("person_id"), 80),
        "resolved_identity_key": _text(source.get("resolved_identity_key"), 96),
        "projection_revision": _positive_int(source.get("projection_revision")),
        "identity_assurance": _text(source.get("identity_assurance"), 40),
        "profile_status": _text(source.get("profile_status"), 40),
    }
    return result


def validate_person_ref(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["person_ref_invalid"]
    errors = [f"missing_{field}" for field in PERSON_REF_FIELDS if field not in value]
    person_id = _text(value.get("person_id"), 80)
    identity_key = _text(value.get("resolved_identity_key"), 96)
    if re.fullmatch(r"person_[0-9a-f]{24}", person_id) is None:
        errors.append("person_id_invalid")
    if re.fullmatch(r"chat-origin-v1:[0-9a-f]{64}", identity_key) is None:
        errors.append("identity_key_invalid")
    if not isinstance(value.get("projection_revision"), int) or value["projection_revision"] < 1:
        errors.append("projection_revision_invalid")
    if value.get("identity_assurance") not in IDENTITY_ASSURANCE:
        errors.append("identity_assurance_invalid")
    if value.get("profile_status") not in PROFILE_STATUS:
        errors.append("profile_status_invalid")
    return errors


def build_capability_summary(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    mode = normalize_portrait_mode(source.get("portrait_mode"))
    learning, usage = portrait_mode_flags(mode)
    private_enabled = source.get("private_companion_enabled") is True
    proactive_enabled = source.get("proactive_private_enabled") is True
    reasons = [_text(item, 80) for item in source.get("blocked_reasons", []) if _text(item, 80)] if isinstance(source.get("blocked_reasons"), list) else []
    if proactive_enabled and not private_enabled and "proactive_requires_private_companion" not in reasons:
        reasons.append("proactive_requires_private_companion")
    if not private_enabled and "private_companion_disabled" not in reasons:
        reasons.append("private_companion_disabled")
    return {
        "private_companion_enabled": private_enabled,
        "proactive_private_enabled": proactive_enabled,
        "effective_proactive_private_enabled": proactive_enabled and private_enabled,
        "portrait_mode": mode,
        "portrait_learning_enabled": learning,
        "portrait_usage_enabled": usage,
        "grant_source": _text(source.get("grant_source"), 80) or "default_closed",
        "blocked_reasons": reasons[:8],
    }


def build_profile_dto(
    *,
    person_ref: Any,
    identity_summary: Any = None,
    expression_summary: Any = None,
    capability_summary: Any = None,
    portrait_summary: Any = None,
    context_overlays: Any = None,
    bridge_status: Any = None,
) -> dict[str, Any]:
    return {
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "contract_fingerprint": CONTRACT_FINGERPRINT,
        "schema_version": SCHEMA_VERSION,
        "person_ref": build_person_ref(person_ref),
        "identity_summary": _safe(identity_summary or {}) or {},
        "expression_summary": _safe(expression_summary or {}) or {},
        "capability_summary": build_capability_summary(capability_summary),
        "portrait_summary": _safe(portrait_summary or {}) or {},
        "context_overlays": _safe(context_overlays or {}) or {},
        "bridge_status": _safe(bridge_status or {}) or {},
    }


def dto_size_bytes(value: Any) -> int:
    try:
        return len(_canonical(value).encode("utf-8"))
    except (TypeError, ValueError):
        return MAX_DTO_BYTES + 1


def validate_profile_dto(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["dto_invalid"]
    errors = [f"missing_{field}" for field in DTO_FIELDS if field not in value]
    if value.get("contract_name") != CONTRACT_NAME:
        errors.append("contract_name_mismatch")
    if value.get("contract_version") != CONTRACT_VERSION:
        errors.append("contract_version_mismatch")
    if value.get("contract_fingerprint") != CONTRACT_FINGERPRINT:
        errors.append("contract_fingerprint_mismatch")
    if value.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version_mismatch")
    for field in ("identity_summary", "expression_summary", "portrait_summary", "context_overlays", "bridge_status"):
        if not isinstance(value.get(field), dict):
            errors.append(f"{field}_invalid")
        elif _contains_forbidden(value.get(field)):
            errors.append(f"{field}_contains_forbidden_data")
    errors.extend(validate_person_ref(value.get("person_ref")))
    capabilities = value.get("capability_summary")
    if not isinstance(capabilities, dict):
        errors.append("capability_summary_invalid")
    elif capabilities != build_capability_summary(capabilities):
        errors.append("capability_summary_invalid")
    if dto_size_bytes(value) > MAX_DTO_BYTES:
        errors.append("dto_too_large")
    return errors


def build_portrait_request(
    *,
    person_ref: Any,
    requester_person_id: Any,
    target_person_id: Any,
    scope: Any,
    purpose: Any,
) -> dict[str, Any]:
    return {
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "contract_fingerprint": CONTRACT_FINGERPRINT,
        "person_ref": build_person_ref(person_ref),
        "requester_person_id": _text(requester_person_id, 80),
        "target_person_id": _text(target_person_id, 80),
        "scope": _text(scope, 80),
        "purpose": _text(purpose, 80),
        "max_sensitivity": LOW_SENSITIVITY,
    }


def validate_portrait_request(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["portrait_request_invalid"]
    errors: list[str] = []
    if value.get("contract_name") != CONTRACT_NAME or value.get("contract_version") != CONTRACT_VERSION:
        errors.append("contract_mismatch")
    if value.get("contract_fingerprint") != CONTRACT_FINGERPRINT:
        errors.append("contract_fingerprint_mismatch")
    errors.extend(validate_person_ref(value.get("person_ref")))
    if _text(value.get("target_person_id"), 80) != _text((value.get("person_ref") or {}).get("person_id"), 80):
        errors.append("target_person_mismatch")
    if value.get("purpose") not in PORTRAIT_PURPOSES:
        errors.append("portrait_purpose_invalid")
    if not _text(value.get("scope"), 80):
        errors.append("scope_invalid")
    if value.get("max_sensitivity") != LOW_SENSITIVITY:
        errors.append("max_sensitivity_invalid")
    if _contains_forbidden(value):
        errors.append("portrait_request_contains_forbidden_data")
    return errors


def contract_self_check() -> list[str]:
    expected = hashlib.sha256(_canonical(_shape()).encode("utf-8")).hexdigest()[:16]
    return [] if expected == CONTRACT_FINGERPRINT else ["contract_fingerprint_stale"]


__all__ = [name for name in globals() if name.isupper() or name in {
    "build_capability_summary", "build_person_ref", "build_profile_dto", "build_portrait_request",
    "contract_self_check", "dto_size_bytes", "normalize_portrait_mode", "portrait_mode_flags",
    "validate_person_ref", "validate_profile_dto", "validate_portrait_request",
}]
