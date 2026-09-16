"""REQ-036 portrait service: capture / governance / read boundary (Phase 5).

Ported from memory_companion's core/portrait_service.py with the fork's
context extraction (the companion attaches its unified-profile DTO onto the
event object by bare setattr; get_extra is only a legacy fallback here, same
channel lesson as the defer set). The pipeline is rule-based end to end —
regex candidate extraction, hash-only evidence, distinct-statement counting
in the nightly batch — so portrait quality does not depend on the configured
LLM at all. Raw message text is used transiently and never stored.

Spec: docs/companion-fork-spec.md section 13.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .contracts import unified_profile_contract as contract
from .portrait_rules import (
    build_evidence,
    cross_scene_whitelisted_fact,
    extract_explicit_candidates,
    portrait_access_decision,
)

DTO_ATTR = "private_companion_unified_profile_context"
NAMESPACE_ATTR = "private_companion_namespace_context"


class PortraitService:
    """Owns portrait capture/read; all methods degrade to honest no-ops."""

    def __init__(self, store: Any, config_get: Any):
        """
        Args:
            store: a PortraitStore.
            config_get: callable(key, default) into the plugin config manager
                (keys under ``portrait.*``).
        """
        self.store = store
        self._get = config_get

    # ------------------------------------------------------------------
    # context extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _profile_context(*sources: Any) -> dict[str, Any] | None:
        for source in sources:
            if source is None:
                continue
            value = getattr(source, DTO_ATTR, None)
            if callable(value):
                try:
                    value = value()
                except Exception:  # noqa: BLE001
                    value = None
            if isinstance(value, dict):
                return value
            get_extra = getattr(source, "get_extra", None)
            if callable(get_extra):
                try:
                    extra = get_extra(DTO_ATTR)
                except Exception:  # noqa: BLE001
                    extra = None
                if isinstance(extra, dict):
                    return extra
        return None

    @staticmethod
    def _namespace_raw(*sources: Any) -> tuple[bool, Any]:
        """Presence-flagged namespace lookup (absent => legacy scope)."""
        for source in sources:
            if source is None:
                continue
            if isinstance(source, dict) and "namespace_context" in source:
                return True, source.get("namespace_context")
            value = getattr(source, NAMESPACE_ATTR, None)
            if value is not None:
                return True, value
        return False, None

    async def sync_profile_context(
        self,
        *,
        event: Any = None,
        req: Any = None,
        legacy_scope: str = "private",
    ) -> dict[str, Any]:
        dto = self._profile_context(event, req)
        errors = contract.validate_profile_dto(dto)
        if errors:
            return {
                "ok": False,
                "code": "bridge_contract_mismatch",
                "errors": errors,
                "dto": None,
            }
        person_ref = contract.build_person_ref(dto["person_ref"])
        present, namespace_value = self._namespace_raw(event, req)
        namespace = self._namespace_decision(
            namespace_value,
            person_id=person_ref.get("person_id"),
            legacy_scope=legacy_scope,
            purpose="profile_write",
            namespace_present=present,
        )
        if not namespace.get("ok"):
            return {
                "ok": False,
                "code": namespace.get("code"),
                "errors": [],
                "dto": dto,
            }
        result = await self.store.upsert_person_projection(
            person_ref,
            dto.get("capability_summary", {}),
            source_scope=str(namespace.get("source_scope") or ""),
        )
        if not result.get("ok"):
            return {
                "ok": False,
                "code": result.get("code", "bridge_degraded"),
                "errors": [],
                "dto": dto,
            }
        if person_ref["profile_status"] != "active" or person_ref[
            "identity_assurance"
        ] not in {"observed", "verified", "explicit_linked"}:
            return {
                "ok": False,
                "code": "bridge_person_mismatch",
                "errors": [],
                "dto": dto,
            }
        return {
            "ok": True,
            "code": "profile_exact",
            "errors": [],
            "dto": dto,
            "person_ref": person_ref,
            "source_scope": str(namespace.get("source_scope") or ""),
            "legacy_scope": str(namespace.get("legacy_scope") or ""),
        }

    # ------------------------------------------------------------------
    # capture (main-chain hook, private + group user messages)
    # ------------------------------------------------------------------

    async def capture_user_message(
        self,
        *,
        text: str,
        session_id: str,
        message_id: str = "",
        scope: str = "",
        event: Any = None,
        req: Any = None,
    ) -> dict[str, Any]:
        """Capture one sender-authored message as portrait evidence.

        Args:
            text: the raw user message; used transiently for hashing and
                rule extraction, never stored.
            session_id: conversation session key for the evidence row.
            message_id: platform message id when available.
            scope: "private" or "group:<platform>:<gid>"; empty refuses.
            event/req: carriers of the companion's DTO/namespace attributes.

        Returns:
            {ok, code, facts} — codes mirror MC's PortraitService.
        """
        if not scope:
            return {"ok": False, "code": "bridge_person_mismatch", "facts": 0}
        synced = await self.sync_profile_context(
            event=event, req=req, legacy_scope=scope
        )
        if not synced.get("ok"):
            return {
                "ok": False,
                "code": synced.get("code", "bridge_degraded"),
                "facts": 0,
            }
        person_ref = synced["person_ref"]
        capabilities = synced["dto"].get("capability_summary", {})
        if not bool(capabilities.get("portrait_learning_enabled")):
            return {
                "ok": False,
                "code": "portrait_learning_disabled",
                "facts": 0,
                "person_id": person_ref["person_id"],
            }
        candidates = extract_explicit_candidates(text)
        if not candidates:
            return {
                "ok": True,
                "code": "portrait_no_candidate",
                "facts": 0,
                "person_id": person_ref["person_id"],
            }
        source_scope = str(synced.get("source_scope") or "")
        evidence = build_evidence(
            person_ref=person_ref,
            scope=source_scope,
            session_id=session_id,
            message_id=message_id,
            source_identity_key=person_ref["resolved_identity_key"],
            text=text,
        )
        evidence_result = await self.store.add_evidence(evidence)
        if not evidence_result.get("ok") or not evidence_result.get("created"):
            return {
                "ok": bool(evidence_result.get("ok")),
                "code": evidence_result.get("code", "portrait_evidence_recorded"),
                "facts": 0,
            }
        created = 0
        for candidate in candidates:
            fact = {
                **candidate,
                "person_id": person_ref["person_id"],
                "portrait_tier": "base",
                "source_scope": source_scope,
                "usable_scope": "self_low_global"
                if cross_scene_whitelisted_fact(
                    dimension=candidate["dimension"],
                    claim_summary=candidate["claim_summary"],
                    sensitivity=candidate["sensitivity"],
                    source_scope=source_scope,
                )
                else "source_only",
                "confidence": float(candidate.get("extraction_quality_score") or 0.0),
                "status": str(candidate.get("profile_state") or "candidate")[:40]
                or "candidate",
                "evidence_hashes": [evidence["evidence_hash"]],
                "context_refs": evidence["context_refs"],
                "operation_id": f"portrait.explicit:{evidence['evidence_hash'][:24]}",
            }
            result = await self.store.upsert_fact(fact)
            if result.get("ok"):
                created += 1
                await self.store.enqueue_learning(
                    person_id=person_ref["person_id"],
                    fact_id=result["fact_id"],
                    evidence_hash=evidence["evidence_hash"],
                )
        return {
            "ok": True,
            "code": "portrait_evidence_recorded",
            "facts": created,
            "person_id": person_ref["person_id"],
        }

    # ------------------------------------------------------------------
    # read boundary (the bridge's portrait surface)
    # ------------------------------------------------------------------

    async def read_summary(
        self, request: dict[str, Any], *, limit: int = 8
    ) -> dict[str, Any]:
        request = request if isinstance(request, dict) else {}
        decision = portrait_access_decision(request)
        if not decision["candidates_allowed"]:
            return {
                "ok": False,
                "code": decision["code"],
                "items": [],
                "decision": decision,
            }
        person_ref = (
            request.get("person_ref")
            if isinstance(request.get("person_ref"), dict)
            else {}
        )
        namespace = self._namespace_decision(
            request.get("namespace_context"),
            person_id=request.get("target_person_id"),
            legacy_scope=request.get("scope"),
            purpose="profile_read",
            namespace_present="namespace_context" in request,
        )
        if not namespace.get("ok"):
            return {
                "ok": False,
                "code": namespace.get("code", "portrait_namespace_invalid"),
                "items": [],
                "decision": decision,
            }
        projection = await self.store.projection_decision(person_ref)
        if not projection.get("ok"):
            return {
                "ok": False,
                "code": str(projection.get("code") or "bridge_degraded"),
                "items": [],
                "decision": decision,
            }
        result = await self.store.summary(
            str(request.get("target_person_id") or "")[:80],
            scope=str(namespace.get("source_scope") or ""),
            legacy_scope=str(namespace.get("legacy_scope") or ""),
            limit=max(1, min(16, int(limit))),
            low_only=True,
            usage_min_confidence=float(
                self._get("portrait.usage_min_confidence", 0.75)
            ),
            inferred_freshness_days=int(
                self._get("portrait.inferred_freshness_days", 90)
            ),
        )
        return {**result, "decision": decision}

    async def status(self, person_id: str) -> dict[str, Any]:
        return await self.store.status(str(person_id or "")[:80])

    async def run_daily_batch(
        self, person_id: str, *, run_day: str = ""
    ) -> dict[str, Any]:
        day = str(run_day or "")[:16]
        if not day:
            day = datetime.now(timezone.utc).astimezone().date().isoformat()
        return await self.store.run_daily_batch(
            person_id=str(person_id or "")[:80],
            run_day=day,
            min_independent_evidence=int(
                self._get("portrait.min_independent_evidence", 3)
            ),
            success_limit=int(self._get("portrait.daily_success_limit_per_person", 1)),
            attempt_limit=int(self._get("portrait.daily_attempt_limit_per_person", 2)),
        )

    # ------------------------------------------------------------------
    # namespace decision — MC-equivalent for the legacy path.
    #
    # The REQ-041 exact-match branch (digest scopes) is intentionally not
    # ported: this fork does not implement the namespace producer surface,
    # so an attested namespace must degrade to the legacy scope bucket
    # rather than reject every capture (MC itself supports namespace-less
    # operation via "portrait_namespace_legacy"). When REQ-041 lands here
    # with the scoped-erase surface, revisit this gate wholesale.
    # ------------------------------------------------------------------

    @staticmethod
    def _namespace_decision(
        value: Any,
        *,
        person_id: Any,
        legacy_scope: Any,
        purpose: str,
        namespace_present: bool | None = None,
    ) -> dict[str, Any]:
        clean_person = str(person_id or "").strip()[:240]
        scope = str(legacy_scope or "").strip()
        clean_legacy = (
            scope
            if scope == "private" or (scope.startswith("group:") and len(scope) <= 240)
            else ""
        )
        if not clean_person or not clean_legacy:
            return {
                "ok": False,
                "code": "portrait_namespace_invalid",
                "state": "invalid",
            }
        return {
            "ok": True,
            "code": "portrait_namespace_legacy",
            "state": "legacy",
            "source_scope": clean_legacy,
            "legacy_scope": clean_legacy,
            "context": None,
        }

    @staticmethod
    def scope_for_context(*, scope: str, platform: str = "", group_id: str = "") -> str:
        """Resolve the portrait scope for one turn (MC's rule)."""
        if scope == "private":
            return "private"
        if scope != "group":
            return ""
        platform = str(platform or "").strip().lower()[:40]
        group_id = str(group_id or "").strip()[:120]
        if not platform or not group_id:
            return ""
        return f"group:{platform}:{group_id}"


def resolve_turn_scope(event: Any, *, is_group: bool) -> str:
    """Best-effort portrait scope from a live message event."""
    scope = "group" if is_group else "private"
    try:
        platform = str(event.get_platform_name() or "")
    except Exception:  # noqa: BLE001
        platform = ""
    group_id = ""
    if is_group:
        try:
            group_id = str(event.get_group_id() or "")
        except Exception:  # noqa: BLE001
            group_id = ""
    return PortraitService.scope_for_context(
        scope=scope, platform=platform, group_id=group_id
    )


def spawn_portrait_capture(
    plugin: Any, service: Any, *, text: str, session_id: str, scope: str, event: Any
) -> None:
    """Queue one portrait capture off the request path (fire and forget).

    Args:
        plugin: active LivingMemory plugin (provides the tracked-task spawner);
            ``None`` silently drops the capture.
        service: the engine's PortraitService; ``None`` silently drops.
        text: the sender's raw message text (never stored, only hashed and
            rule-extracted).
        session_id: conversation session key.
        scope: portrait scope (``private`` / ``group:<platform>:<gid>``).
        event: live message event carrying the companion's DTO attributes.
    """
    if plugin is None or service is None:
        return
    if not text or text.startswith("/") or len(text) < 4 or not scope:
        return
    message_id = ""
    try:
        message_id = str(
            getattr(getattr(event, "message_obj", None), "message_id", "") or ""
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        plugin.spawn_companion_background(
            service.capture_user_message(
                text=text,
                session_id=session_id,
                message_id=message_id,
                scope=scope,
                event=event,
            )
        )
    except Exception:  # noqa: BLE001
        pass
