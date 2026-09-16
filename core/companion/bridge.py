"""Companion bridge: presents this plugin as a memory_companion look-alike.

The private_companion plugin discovers memory plugins by duck-typing: it
scans registered stars, matches names against an alias table (our plugin's
display_name hits it), then probes ``get_active_bridge()`` / attributes and
calls a fixed set of methods. This module implements that method surface
against the LivingMemory engine:

- handshake mirrors MC 1.10.5's probe response byte-for-byte in the fields
  the companion compares (fingerprint/revision/windows/memory_types order);
- reads (compose_context) reuse the shared gate + search + PackageComposer
  pipeline, wrapped in MC's ``<MemoryCompanion-Context>`` markers;
- writes (record_*) dedupe on a stable key and triage into messages /
  companion_events / emotion_ledger, all off the request path;
- emotion delivery is a real state machine (pending/delivered/acked);
- unimplemented optional surfaces (REQ-041 scoped, projections, profiles)
  are simply absent — the companion degrades per-feature on missing attrs.

Spec source: docs/companion-fork-spec.md section 2.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

from .composer import MemoryItem, PackageComposer
from .contracts import bot_personal_contract, emotion_event_contract

PC_PLUGIN_IDENTITY = "astrbot_plugin_private_companion"
PC_NAME_ALIASES = frozenset({"PrivateCompanion", "private_companion"})
_EMOTION_CONSUMER_ID = "private_companion.daily_state"

# record_* defaults extracted from MC bridge (memory_type, visibility,
# reality_level, sayability, tags, importance) for each thin wrapper.
_RECORD_DEFAULTS: dict[str, dict[str, Any]] = {
    "bot_action": {
        "memory_type": "self_action",
        "visibility": "bot_self",
        "reality_level": "bot_action",
        "sayability": "direct",
    },
    "persona_life": {
        "memory_type": "persona_life",
        "visibility": "bot_self",
        "reality_level": "persona_life",
        "sayability": "indirect",
    },
    "proactive_message": {
        "memory_type": "proactive_message",
        "visibility": "bot_self",
        "reality_level": "bot_action",
        "sayability": "direct",
        "tags": ["proactive", "bot_action"],
        "importance": 0.55,
    },
    "creative_work": {
        "memory_type": "creative_work",
        "visibility": "bot_self",
        "reality_level": "fictional_content",
        "sayability": "direct",
        "tags": ["creative_work"],
        "importance": 0.72,
    },
    "qzone_action": {
        "memory_type": "qzone_action",
        "visibility": "bot_self",
        "reality_level": "bot_action",
        "sayability": "direct",
        "tags": ["qzone", "bot_action"],
        "importance": 0.58,
    },
    "shared_experience": {
        "memory_type": "shared_experience",
        "visibility": "shareable",
        "reality_level": "bot_action",
        "sayability": "direct",
    },
    "search_action": {
        "memory_type": "search_action",
        "visibility": "bot_self",
        "reality_level": "bot_action",
        "sayability": "indirect",
    },
    "image_action": {
        "memory_type": "image_action",
        "visibility": "bot_self",
        "reality_level": "bot_action",
        "sayability": "indirect",
    },
    "reading": {
        "memory_type": "reading",
        "visibility": "bot_self",
        "reality_level": "bot_action",
        "sayability": "indirect",
    },
    "schedule_fragment": {
        "memory_type": "schedule_fragment",
        "visibility": "bot_self",
        "reality_level": "bot_action",
        "sayability": "indirect",
    },
}

# companion_events kinds for time-window storage (bot_self mirror stream).
_EVENT_KIND_TYPES = frozenset(
    {
        "persona_life",
        "proactive_message",
        "creative_work",
        "qzone_action",
        "self_action",
        "schedule_fragment",
        "search_action",
        "image_action",
        "reading",
    }
)
# memory_types accepted by the archive envelope (contract superset check).
_ARCHIVE_TYPES = frozenset(bot_personal_contract.BOT_PERSONAL_MEMORY_TYPES)


def _canonical_bot_id(value: Any) -> str:
    """Normalize every bot-id surface to one stored form (spec §2.3).

    The companion hands us a raw self_id (e.g. "10001") in producer hooks
    and session_context, while our own plugin identity getter carries a
    platform prefix ("aiocqhttp:10001"). Storing both spellings splits the
    day-window queries by half. Raw form wins; single-platform deployments
    make the strip lossless.
    """
    text = str(value or "").strip()
    if ":" in text:
        head, tail = text.split(":", 1)
        if head and tail:
            return tail
    return text


@dataclass(frozen=True)
class _ProducerCapability:
    """Opaque token proving a producer plugin was registered."""

    token: str
    bot_id: str
    persona_id: str


@dataclass(frozen=True)
class _ProducerContext:
    """Attested domain tuple for emotion writes."""

    token: str
    bot_id: str
    scope: str
    platform: str
    user_id: str
    session_id: str


@dataclass(frozen=True)
class _DeliveryContext(_ProducerContext):
    consumer_id: str
    allow_cross_window: bool


class CompanionBridge:
    """Method surface the private_companion adapter probes. See spec §2."""

    def __init__(self, plugin: Any):
        self._plugin = plugin
        self._active = True
        self._producers: dict[str, _ProducerCapability] = {}
        # token -> capability, rotates on deactivate to invalidate old tokens
        self._token_epoch = uuid.uuid4().hex[:8]

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def deactivate(self) -> None:
        self._active = False
        self._token_epoch = uuid.uuid4().hex[:8]
        self._producers.clear()

    def _disabled(self) -> bool:
        """Single gate: lifecycle token plus the enabled config switch."""
        if not self._active:
            return True
        return not bool(self._cfg("enabled", True))

    def bridge_lifecycle_status(self) -> dict[str, Any]:
        return {"active": bool(self._active and self._cfg("enabled", True))}

    # ------------------------------------------------------------------
    # handshake
    # ------------------------------------------------------------------

    def probe_bot_personal_memory_capabilities(self) -> dict[str, Any]:
        """Mirror MC's full probe payload; companion compares ordered lists."""
        if self._disabled():
            return {
                "available": False,
                "state": "negative",
                "capability_state": "negative",
                "degraded": True,
                "pending": False,
                "read_only": False,
                "error_code": "bridge_inactive",
                "p5": {"state": "degraded", "error_code": "bridge_inactive"},
            }
        probe = dict(bot_personal_contract.capability_descriptor(available=True))
        probe.update(
            {
                "state": "ready",
                "degraded": False,
                "pending": False,
                "error_code": "",
                "profiles": (
                    "bot_schedule_current",
                    "bot_schedule_history",
                    "bot_creative",
                    "bot_subjective",
                    "locked_frame_personal",
                ),
                "legacy_profiles": ["bot_personal_archive"],
                "domain": bot_personal_contract.BOT_PERSONAL_MEMORY_DOMAIN,
                "domains": [bot_personal_contract.BOT_PERSONAL_MEMORY_DOMAIN],
                "methods": sorted(self._implemented_method_names()),
                "contract_version": str(bot_personal_contract.CONTRACT_REVISION),
                "schema_version": bot_personal_contract.BOT_PERSONAL_CAPABILITY_SCHEMA_VERSION,
                "capability_state": "available",
                "p5": {"state": "unprobed", "error_code": "p5_status_not_probed"},
                "legacy_state": "ready",
            }
        )
        return probe

    def _implemented_method_names(self) -> list[str]:
        names = []
        for attr in dir(CompanionBridge):
            if attr.startswith("_") or attr in ("deactivate",):
                continue
            if callable(getattr(CompanionBridge, attr, None)):
                names.append(attr)
        return names

    # ------------------------------------------------------------------
    # config helpers
    # ------------------------------------------------------------------

    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            return self._plugin.config_manager.get(f"companion_bridge.{key}", default)
        except Exception:
            return default

    def _engine(self) -> Any:
        return getattr(self._plugin.initializer, "memory_engine", None)

    def _companion_store(self) -> Any:
        engine = self._engine()
        return getattr(engine, "companion_store", None) if engine else None

    def _deferred_sections(self) -> set[str]:
        # Mirrors MC's defer protocol: with dedupe + prefer flags on, the
        # companion defers these prompt sections to the memory side.
        if not self._cfg("dedupe_prompt_context", True):
            return set()
        if self._cfg("prefer_memory_companion_memory", True):
            return {
                "self_timeline",
                "private_context",
                "livingmemory_guidance",
                "companion_memory",
                "dialogue_history",
            }
        return set()

    def coordination_status(self) -> dict[str, Any]:
        if self._disabled():
            return {
                "available": False,
                "state": "degraded",
                "degraded": True,
                "reason": "bridge_inactive",
            }
        return {
            "available": True,
            "state": "ready",
            "degraded": False,
            "schedule_fast_context": bool(
                self._cfg("schedule_fast_context_enabled", True)
            ),
            "outfit_fast_context": bool(self._cfg("outfit_fast_context_enabled", True)),
            "bridge_enabled": bool(self._cfg("enabled", True)),
            "memory_injection_enabled": True,
            "dedupe_prompt_context": bool(self._cfg("dedupe_prompt_context", True)),
            "prefer_memory_companion_memory": bool(
                self._cfg("prefer_memory_companion_memory", True)
            ),
            "clean_proactive_history": bool(self._cfg("clean_proactive_history", True)),
            "suppress_self_timeline_when_companion_seen": bool(
                self._cfg("suppress_self_timeline_when_companion_seen", True)
            ),
            "suppress_user_context_when_companion_seen": bool(
                self._cfg("suppress_user_context_when_companion_seen", True)
            ),
        }

    def should_defer_private_companion_section(self, section: str) -> bool:
        return not self._disabled() and str(section) in self._deferred_sections()

    def get_token_usage_summary(self) -> dict[str, Any]:
        # v1: minimal honest shape; companion only reads available flags.
        try:
            usage = self._plugin.get_companion_token_usage()
        except Exception:
            usage = {}
        return {
            "token_usage_schema_version": 2,
            "available": True,
            "display_name": "我会牢牢记住你",
            "plugin_name": "astrbot_plugin_livingmemory",
            "counted_in_private_companion_budget": False,
            "note": "LivingMemory kernel: companion bridge usage",
            "usage": usage or {},
        }

    # ------------------------------------------------------------------
    # read path: compose_context
    # ------------------------------------------------------------------

    async def compose_context(
        self,
        *,
        query: str = "",
        session_context: Any = None,
        top_k: int | None = None,
        max_chars: int | None = None,
        companion_bot_mood: str = "",
        companion_bot_energy: float = 0.0,
        retrieval_profile: str = "",
        p5_attestation: Any = None,
        p5_attestation_consumer: Any = None,
    ) -> str:
        """Return an MC-shaped memory package text for the companion plugin.

        ``retrieval_profile`` fast lanes (schedule/outfit) query the
        time-window index directly; the default lane reuses the shared
        gate + search + composer pipeline (identical quality to main-chain
        injection). Internal budget 1.0s < companion's 1.2s timeout.
        """
        if self._disabled():
            return ""
        try:
            return await asyncio.wait_for(
                self._compose_context_inner(
                    query=query,
                    session_context=session_context,
                    top_k=top_k,
                    max_chars=max_chars,
                    companion_bot_mood=companion_bot_mood,
                    companion_bot_energy=companion_bot_energy,
                    retrieval_profile=str(retrieval_profile or "").strip().lower()[:40],
                ),
                timeout=1.0,
            )
        except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
            logger.debug(f"[companion_bridge] compose_context degraded: {exc}")
            return ""

    async def _compose_context_inner(self, **kwargs: Any) -> str:
        ctx = kwargs.get("session_context") or {}
        if not isinstance(ctx, dict):
            ctx = getattr(ctx, "__dict__", {}) or {}
        session_id = str(ctx.get("session_id") or "")
        scope = str(ctx.get("scope") or "")
        bot_id = _canonical_bot_id(ctx.get("bot_id") or "")
        composer = PackageComposer(budget_chars=int(kwargs.get("max_chars") or 1000))

        profile = kwargs.get("retrieval_profile") or ""
        if profile in {"schedule_fast", "outfit_fast"}:
            allowed = (
                self._cfg("schedule_fast_context_enabled", True)
                if profile == "schedule_fast"
                else self._cfg("outfit_fast_context_enabled", True)
            )
            if not allowed:
                return ""
            pkg = await self._compose_fast_context(
                composer,
                bot_id=bot_id,
                scope=scope,
                session_id=session_id,
                query=kwargs.get("query") or "",
                current_message=str(ctx.get("message_text") or ""),
                window_label=self._window_label(scope, ctx),
            )
            return composer.wrap_for_bridge(pkg)

        pkg = await self._compose_default_context(
            composer,
            session_id=session_id,
            query=kwargs.get("query") or "",
            top_k=int(kwargs.get("top_k") or 6),
            current_message=str(ctx.get("message_text") or ""),
            window_label=self._window_label(scope, ctx),
            mood=str(kwargs.get("companion_bot_mood") or ""),
        )
        return composer.wrap_for_bridge(pkg)

    @staticmethod
    def _window_label(scope: str, ctx: dict) -> str:
        kind = {"group": "群聊", "private": "私聊"}.get(scope, scope or "会话")
        label = ctx.get("user_name") or ctx.get("group_name") or ""
        return f"会话类型：{kind}\n当前对象：{label}" if label else f"会话类型：{kind}"

    async def _compose_fast_context(
        self,
        composer: PackageComposer,
        *,
        bot_id: str,
        scope: str,
        session_id: str,
        query: str,
        current_message: str,
        window_label: str,
    ):
        store = self._companion_store()
        items: list[MemoryItem] = []
        if store is not None:
            from ...storage.companion_store import today_date

            today = today_date()
            rows = await store.list_events_between(
                bot_id=bot_id, since_date=today, until_date=today, limit=8
            )
            for row in rows:
                items.append(
                    MemoryItem(
                        text=row["content"],
                        source="companion",
                        time_label=row.get("window_slug", ""),
                        weight=float(row.get("importance") or 0.5),
                    )
                )
        one_lines = await self._afterglow_line(bot_id=bot_id, session_id=session_id)
        return composer.compose(
            one_line_slots=one_lines,
            retrieval_items=items,
            current_message=current_message,
            window_label=window_label,
        )

    async def _compose_default_context(
        self,
        composer: PackageComposer,
        *,
        session_id: str,
        query: str,
        top_k: int,
        current_message: str,
        window_label: str,
        mood: str,
    ):
        engine = self._engine()
        items: list[MemoryItem] = []
        core_blocks: list[dict[str, Any]] = []
        loops: list[MemoryItem] = []
        if engine is not None and query.strip():
            try:
                results = await engine.search_memories(
                    query=query, k=top_k, session_id=session_id or None
                )
                for mem in results:
                    meta = getattr(mem, "metadata", None) or {}
                    items.append(
                        MemoryItem(
                            text=str(getattr(mem, "content", "") or ""),
                            source=str(meta.get("memory_type") or ""),
                            time_label="",
                            weight=float(getattr(mem, "final_score", 0.0) or 0.0),
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[companion_bridge] search degraded: {exc}")
            core_blocks = await self._load_core_blocks(engine, session_id)
            loops = await self._load_open_loops(session_id)
        one_lines = await self._afterglow_line(session_id=session_id)
        return composer.compose(
            core_blocks=core_blocks,
            one_line_slots=one_lines,
            open_loops=loops,
            retrieval_items=items,
            current_message=current_message,
            window_label=window_label,
        )

    async def _load_core_blocks(
        self, engine: Any, session_id: str
    ) -> list[dict[str, Any]]:
        """Always-resident core memories for this session domain (spec §5.3)."""
        loader = getattr(engine, "load_core_memories", None)
        if loader is None:
            return []
        try:
            return await loader(session_id=session_id)
        except Exception:  # noqa: BLE001
            return []

    async def _load_open_loops(self, session_id: str) -> list[MemoryItem]:
        loader = getattr(self._engine(), "load_open_loop_memories", None)
        if loader is None:
            return []
        try:
            rows = await loader(session_id=session_id or None, limit=3)
        except Exception:  # noqa: BLE001
            return []
        from .slots import _loop_text

        return [
            MemoryItem(
                text=_loop_text(row),
                source="open_loop",
                weight=0.5 if row.get("promise") else 0.0,
            )
            for row in rows
        ]

    async def _afterglow_line(
        self, *, bot_id: str = "", session_id: str = ""
    ) -> list[tuple[str, str]]:
        """Mood afterglow one-liner derived from un-acked ledger events.

        Read-only (peek): delivery/ack consumption belongs to the companion
        plugin's list_emotion_events, never to our own injection path.
        """
        store = self._companion_store()
        if store is None:
            return []
        try:
            rows = await store.peek_pending(
                scope="private", platform="", user_id="", session_id=session_id, limit=3
            )
        except Exception:  # noqa: BLE001
            return []
        out: list[tuple[str, str]] = []
        mood_map = {
            "scar_touched": "心里还有点发酸",
            "warm_memory": "带着暖意",
            "vulnerable_resonance": "变得柔软",
        }
        for row in rows:
            phrase = mood_map.get(row.get("event_type", ""))
            if phrase:
                out.append(("emotional_hint", f"Bot当前情绪余波：{phrase}"))
                break
        return out

    # ------------------------------------------------------------------
    # write path: record_*
    # ------------------------------------------------------------------

    async def record_event(self, **kwargs: Any) -> str:
        return await self._record(**kwargs)

    async def record_bot_action(self, *, content: str, **kwargs: Any) -> str:
        return await self._record(
            content=content, **{**_RECORD_DEFAULTS["bot_action"], **kwargs}
        )

    async def record_persona_life(self, *, content: str, **kwargs: Any) -> str:
        return await self._record(
            content=content, **{**_RECORD_DEFAULTS["persona_life"], **kwargs}
        )

    async def record_proactive_message(self, *, content: str, **kwargs: Any) -> str:
        return await self._record(
            content=content, **{**_RECORD_DEFAULTS["proactive_message"], **kwargs}
        )

    async def record_creative_work(self, *, content: str, **kwargs: Any) -> str:
        return await self._record(
            content=content, **{**_RECORD_DEFAULTS["creative_work"], **kwargs}
        )

    async def record_qzone_action(self, *, content: str, **kwargs: Any) -> str:
        return await self._record(
            content=content, **{**_RECORD_DEFAULTS["qzone_action"], **kwargs}
        )

    async def record_shared_experience(self, *, content: str, **kwargs: Any) -> str:
        return await self._record(
            content=content, **{**_RECORD_DEFAULTS["shared_experience"], **kwargs}
        )

    async def record_search_action(self, *, content: str, **kwargs: Any) -> str:
        return await self._record(
            content=content, **{**_RECORD_DEFAULTS["search_action"], **kwargs}
        )

    async def record_image_action(self, *, content: str, **kwargs: Any) -> str:
        return await self._record(
            content=content, **{**_RECORD_DEFAULTS["image_action"], **kwargs}
        )

    async def record_reading(self, *, content: str, **kwargs: Any) -> str:
        return await self._record(
            content=content, **{**_RECORD_DEFAULTS["reading"], **kwargs}
        )

    async def record_schedule_fragment(self, *, content: str, **kwargs: Any) -> str:
        return await self._record(
            content=content, **{**_RECORD_DEFAULTS["schedule_fragment"], **kwargs}
        )

    async def record_visible_turn(
        self,
        *,
        role: str = "",
        content: str = "",
        scope: str = "unknown",
        session_id: str = "",
        platform: str = "",
        user_id: str = "",
        user_name: str = "",
        group_id: str = "",
        message_id: str = "",
        source: str = "external",
        metadata: dict | None = None,
        occurred_at: Any = "",
    ) -> str:
        """Mirror one visible dialogue turn into the conversation timeline.

        The main-chain hooks already capture the same traffic, so this is a
        safety mirror: a recent duplicate (same role+content within the
        dedupe window) is dropped instead of double-writing into summaries.
        """
        if self._disabled() or not content.strip():
            return ""
        store = self._companion_store()
        if store is None:
            return ""
        key = store.make_event_key(
            f"turn:{role}",
            self._bot_id_of(),
            scope,
            session_id,
            user_id or group_id,
            content,
            _to_ts(occurred_at),
        )
        bot_role = role in {"assistant", "bot"}

        async def _write() -> None:
            manager = getattr(self._plugin.initializer, "conversation_manager", None)
            if manager is None:
                return
            try:
                # Pre-check recent same-role identical content (retry or
                # main-chain mirror dedupe).
                sid = (
                    session_id
                    or f"{platform}:External:{user_id or group_id or 'unknown'}"
                )
                wanted = content.strip()
                try:
                    recent = await manager.get_messages(sid, limit=20)
                    for msg in reversed(recent):
                        if (
                            getattr(msg, "role", "")
                            == ("assistant" if bot_role else "user")
                            and str(getattr(msg, "content", "")).strip() == wanted
                        ):
                            logger.debug(
                                f"[companion_bridge] visible_turn duplicate dropped: {key[:16]}"
                            )
                            return
                except Exception:  # noqa: BLE001
                    pass
                await manager.add_message(
                    sid,
                    role="assistant" if bot_role else "user",
                    content=content[:4000],
                    sender_id=user_id or bot_self_id(self._bot_id_of()),
                    sender_name=user_name or "Bot",
                    group_id=group_id or None,
                    platform=platform or "unknown",
                    is_bot_message=bot_role,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[companion_bridge] visible_turn dropped: {exc}")

        self._plugin.spawn_companion_background(_write())
        return key

    def _bot_id_of(self) -> str:
        try:
            return _canonical_bot_id(str(self._plugin.bot_id))
        except Exception:
            return ""

    async def _record(
        self,
        *,
        content: str,
        memory_type: str = "external_event",
        scope: str = "unknown",
        session_id: str = "",
        platform: str = "",
        message_id: str = "",
        group_id: str = "",
        subject: dict | None = None,
        object: dict | None = None,
        visibility: str = "bot_self",
        sayability: str = "direct",
        reality_level: str = "bot_action",
        lifecycle: str = "stable_memory",
        confidence: float = 0.85,
        importance: float = 0.5,
        review_status: str = "auto",
        tags: list | None = None,
        metadata: dict | None = None,
        source_plugin: str = "external",
        memory_id: str = "",
        occurred_at: Any = "",
    ) -> str:
        """Shared record entry: dedupe, triage, persist off-path (spec §2.3)."""
        if self._disabled() or not str(content).strip():
            return memory_id or ""
        store = self._companion_store()
        if store is None:
            return memory_id or ""
        subject_id = ""
        if isinstance(subject, dict):
            subject_id = str(subject.get("id") or "")
        user_id = subject_id or str((metadata or {}).get("user_id") or "")
        event_key = store.make_event_key(
            str(memory_type),
            self._bot_id_of(),
            str(scope),
            str(session_id),
            user_id or str(group_id),
            str(content),
            _to_ts(occurred_at),
        )
        meta = dict(metadata or {})
        meta.update(
            {
                "source_plugin": str(source_plugin or "external"),
                "origin_plugin": str(source_plugin or "external"),
                "mc_bridge": "livingmemory3",
                "memory_type": str(memory_type),
                "visibility": str(visibility),
                "reality_level": str(reality_level),
                "tags": list(tags or []),
            }
        )

        async def _persist() -> None:
            try:
                _key, deduped = await store.upsert_event(
                    kind=str(memory_type),
                    content=str(content)[:4000],
                    bot_id=self._bot_id_of(),
                    persona_id=str(meta.get("persona_id") or ""),
                    scope=str(scope),
                    session_id=str(session_id),
                    user_id=user_id,
                    group_id=str(group_id),
                    window_slug=str(meta.get("source_window") or ""),
                    occurred_at=_to_ts(occurred_at),
                    importance=float(importance),
                    metadata=meta,
                    event_key=event_key,
                )
                if deduped:
                    logger.debug(f"[companion_bridge] dedupe hit: {event_key[:16]}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[companion_bridge] record dropped: {exc}")

        self._plugin.spawn_companion_background(_persist())
        return event_key

    async def record_bot_personal_archive(
        self,
        envelope: Any,
        *,
        producer_capability: Any = None,
        producer_context: Any = None,
    ) -> dict[str, Any]:
        """Store one schedule/diary archive envelope (spec §2.3 fixed 6 keys)."""
        if self._disabled():
            return {
                "ok": False,
                "record_id": "",
                "deduplicated": False,
                "version": 0,
                "error_code": "bridge_inactive",
                "state": "degraded",
            }
        if not isinstance(producer_capability, _ProducerCapability):
            return {
                "ok": False,
                "record_id": "",
                "deduplicated": False,
                "version": 0,
                "error_code": "producer_capability_required",
                "state": "forbidden",
            }
        if not isinstance(envelope, dict):
            envelope = getattr(envelope, "__dict__", None)
            if not isinstance(envelope, dict):
                return {
                    "ok": False,
                    "record_id": "",
                    "deduplicated": False,
                    "version": 0,
                    "error_code": "envelope_invalid",
                    "state": "invalid",
                }

        def _s(key: str, limit: int = 200) -> str:
            return str(envelope.get(key) or "")[:limit]

        record_id = _s("record_id", 96) or f"lmce_{uuid.uuid4().hex[:16]}"
        memory_type = _s("memory_type", 48)
        if memory_type not in _ARCHIVE_TYPES:
            return {
                "ok": False,
                "record_id": record_id,
                "deduplicated": False,
                "version": 0,
                "error_code": "memory_type_rejected",
                "state": "invalid",
            }
        owner_bot_id = _s("owner_bot_id", 160)
        persona_id = _s("persona_id", 160)
        # Namespace cross-check mirroring MC: producer hooks must match DTO.
        bot_hook = getattr(producer_capability, "bot_id", "")
        persona_hook = getattr(producer_capability, "persona_id", "")
        if bot_hook and owner_bot_id and bot_hook != owner_bot_id:
            return {
                "ok": False,
                "record_id": record_id,
                "deduplicated": False,
                "version": 0,
                "error_code": "producer_namespace_mismatch",
                "state": "forbidden",
            }
        if persona_hook and persona_id and persona_hook != persona_id:
            return {
                "ok": False,
                "record_id": record_id,
                "deduplicated": False,
                "version": 0,
                "error_code": "producer_namespace_mismatch",
                "state": "forbidden",
            }
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        try:
            payload_text = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError):
            payload_text = "{}"
        if (
            len(payload_text.encode("utf-8"))
            > bot_personal_contract.BOT_PERSONAL_MAX_PAYLOAD_BYTES
        ):
            return {
                "ok": False,
                "record_id": record_id,
                "deduplicated": False,
                "version": 0,
                "error_code": "payload_too_large",
                "state": "invalid",
            }
        version = int(envelope.get("version") or 1)
        idem = _s("idempotency_key", 160)
        if not idem:
            return {
                "ok": False,
                "record_id": record_id,
                "deduplicated": False,
                "version": 0,
                "error_code": "idempotency_key_required",
                "state": "invalid",
            }
        store = self._companion_store()
        if store is None:
            return {
                "ok": False,
                "record_id": record_id,
                "deduplicated": False,
                "version": 0,
                "error_code": "store_unavailable",
                "state": "degraded",
            }
        summary = _s("summary", 400) or _extract_payload_summary(payload)
        window = bot_personal_contract.normalize_window(envelope.get("window"))
        occurred = _to_ts(envelope.get("occurred_at") or envelope.get("created_at"))
        # Stable, cross-process idempotency key: Python's hash() is salted per
        # run, so abs(hash(idem)) would let every restart re-insert a "new"
        # row and swallow MC's version ladder.
        event_key = store.stable_key("lmce_arch_", idem)
        fingerprint = store.archive_payload_fingerprint(payload_text)

        try:
            outcome = await store.upsert_archive_event(
                event_key=event_key,
                version=version,
                fingerprint=fingerprint,
                kind="archive",
                content=summary,
                bot_id=_canonical_bot_id(owner_bot_id) or self._bot_id_of(),
                persona_id=persona_id,
                scope="private",
                session_id="bot_personal",
                user_id="",
                group_id="",
                window_slug=window,
                occurred_at=occurred,
                importance=0.55,
                metadata={
                    "archive_version": version,
                    "payload_fingerprint": fingerprint,
                    "idempotency_key": idem,
                    "archive_memory_type": memory_type,
                    "subject": _s("subject", 64),
                    "date": _s("date", 16),
                    "evidence_level": _s("evidence_level", 16),
                    "status": _s("status", 32),
                    "source_kind": _s("source_kind", 48),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[companion_bridge] archive failed: {exc}")
            return {
                "ok": False,
                "record_id": record_id,
                "deduplicated": False,
                "version": 0,
                "error_code": "store_error",
                "state": "degraded",
            }
        state = str(outcome.get("state") or "sent")
        # PC's outbox normalizes invalid/version_conflict/stale_version as
        # dead-letter and retries non-ok; ok stays True only for sent/deduped.
        ok = state in {"sent", "deduplicated"}
        error_code = (
            None
            if ok
            else (
                "stale_version"
                if state == "stale_version"
                else "version_conflict"
                if state == "version_conflict"
                else state
            )
        )
        return {
            "ok": ok,
            "record_id": record_id,
            "deduplicated": bool(outcome.get("deduplicated")),
            "version": int(outcome.get("version") or version),
            "error_code": error_code,
            "state": state,
        }

    # ------------------------------------------------------------------
    # emotion domain (spec §2.4)
    # ------------------------------------------------------------------

    def register_emotion_producer(self, producer: Any) -> Any | None:
        if self._disabled() or producer is None:
            return None
        meta = self._match_pc_star(producer)
        if meta is None:
            logger.warning(
                "[companion_bridge] emotion producer registration refused: "
                "producer is not a live private_companion star"
            )
            return None
        bot_id, persona_id = self._producer_namespace(producer)
        capability = _ProducerCapability(
            token=f"{self._token_epoch}:{uuid.uuid4().hex[:16]}",
            bot_id=bot_id,
            persona_id=persona_id,
        )
        self._producers[capability.token] = capability
        return capability

    def register_private_companion(self, producer: Any) -> Any | None:
        return self.register_emotion_producer(producer)

    def register_bot_personal_producer(self, producer: Any) -> Any | None:
        return self.register_emotion_producer(producer)

    @staticmethod
    def _producer_namespace(producer: Any) -> tuple[str, str]:
        bot_hook = getattr(producer, "_memory_companion_bridge_bot_id", None)
        persona_hook = getattr(producer, "_memory_companion_archive_persona_id", None)
        bot_id = _canonical_bot_id(bot_hook()) if callable(bot_hook) else ""
        persona_id = str(persona_hook()) if callable(persona_hook) else ""
        return bot_id, persona_id

    def _match_pc_star(self, producer: Any) -> Any | None:
        """Verify producer is a live private_companion star (no name fallback).

        ``context.get_all_stars()`` returns StarMetadata objects whose
        ``star_cls`` is the plugin instance and ``star_cls_type`` its class.
        """
        try:
            stars = self._plugin.context.get_all_stars()
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(stars, (list, tuple)):
            return None
        for star in stars:
            root_dir = str(getattr(star, "root_dir_name", "") or "")
            name = str(getattr(star, "name", "") or "")
            if root_dir != PC_PLUGIN_IDENTITY and name not in PC_NAME_ALIASES:
                continue
            if getattr(star, "activated", False) is not True:
                continue
            instance = getattr(star, "star_cls", None)
            star_type = getattr(star, "star_cls_type", None)
            if instance is not None and instance is producer:
                return star
            if star_type is not None and type(producer) is star_type:
                return star
        return None

    def create_emotion_producer_context(
        self,
        capability: Any,
        *,
        bot_id: str,
        scope: str,
        platform: str,
        user_id: str,
        session_id: str,
    ) -> Any | None:
        return self._make_context(
            capability,
            bot_id=bot_id,
            scope=scope,
            platform=platform,
            user_id=user_id,
            session_id=session_id,
        )

    def create_user_memory_context(self, capability: Any, **kwargs: Any) -> Any | None:
        return self._make_context(capability, **kwargs)

    def create_emotion_delivery_context(
        self,
        capability: Any,
        *,
        bot_id: str,
        scope: str,
        platform: str,
        user_id: str,
        session_id: str,
        consumer_id: str = _EMOTION_CONSUMER_ID,
        allow_cross_window: bool = False,
    ) -> Any | None:
        if type(allow_cross_window) is not bool:
            return None
        if consumer_id != _EMOTION_CONSUMER_ID:
            return None
        if allow_cross_window and not self._cfg(
            "cross_window_emotional_continuity_enabled", False
        ):
            return None
        base = self._make_context(
            capability,
            bot_id=bot_id,
            scope=scope,
            platform=platform,
            user_id=user_id,
            session_id=session_id,
        )
        if base is None:
            return None
        return _DeliveryContext(
            token=base.token,
            bot_id=base.bot_id,
            scope=base.scope,
            platform=base.platform,
            user_id=base.user_id,
            session_id=base.session_id,
            consumer_id=consumer_id,
            allow_cross_window=allow_cross_window,
        )

    def _make_context(
        self,
        capability: Any,
        *,
        bot_id: str = "",
        scope: str = "",
        platform: str = "",
        user_id: str = "",
        session_id: str = "",
        **_ignored: Any,
    ) -> _ProducerContext | None:
        if not isinstance(capability, _ProducerCapability):
            return None
        if self._producers.get(capability.token) is not capability:
            return None
        # Fail-closed domain validation mirroring MC's normalize gate.
        five = (bot_id, scope, platform, user_id, session_id)
        if not all(str(x).strip() for x in five):
            return None
        if scope != "private":
            return None
        if not str(session_id).startswith(f"{platform}:"):
            return None
        return _ProducerContext(
            token=capability.token,
            bot_id=str(bot_id),
            scope="private",
            platform=str(platform),
            user_id=str(user_id),
            session_id=str(session_id),
        )

    async def record_emotion_event(
        self, event: Any, *, producer_context: Any = None
    ) -> dict[str, Any]:
        forbidden = {
            "ok": False,
            "state": "forbidden",
            "read_only": False,
            "event_id": "",
            "error_code": "producer_context_required",
        }
        if self._disabled():
            return {
                "ok": False,
                "state": "degraded",
                "read_only": False,
                "event_id": "",
                "error_code": "bridge_inactive",
            }
        if not isinstance(producer_context, _ProducerContext):
            return forbidden
        store = self._companion_store()
        if store is None:
            return {
                "ok": False,
                "state": "degraded",
                "read_only": False,
                "event_id": "",
                "error_code": "store_unavailable",
            }
        attested = dict(event) if isinstance(event, dict) else {}
        attested.update(
            {
                "producer_plugin": "private_companion",
                "bot_id": producer_context.bot_id,
                "scope": "private",
                "platform": producer_context.platform,
                "session_id": producer_context.session_id,
                "target_ref": attested.get("target_ref")
                or {"id": producer_context.user_id},
            }
        )
        origin = str(attested.get("origin_kind") or "")
        if origin not in emotion_event_contract.EMOTION_EVENT_ORIGINS:
            attested["origin_kind"] = "interaction"
        normalized = emotion_event_contract.normalize_emotion_event(attested)
        context_fields = {
            "bot_id": producer_context.bot_id,
            "scope": "private",
            "platform": producer_context.platform,
            "user_id": producer_context.user_id,
            "session_id": producer_context.session_id,
        }
        try:
            stored = await store.record_emotion(normalized, context=context_fields)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[companion_bridge] emotion record failed: {exc}")
            return {
                "ok": False,
                "state": "degraded",
                "read_only": False,
                "event_id": normalized.get("event_id", ""),
                "error_code": "store_error",
            }
        stored["ok"] = True
        return stored

    async def list_emotion_events(
        self,
        *,
        delivery_context: Any = None,
        cursor: str = "",
        limit: int = 10,
        **_legacy: Any,
    ) -> dict[str, Any]:
        forbidden = {
            "schema_version": "emotion_afterglow_delivery.v1",
            "state": "forbidden",
            "read_only": True,
            "events": [],
            "next_cursor": "",
            "has_more": False,
            "error_code": "delivery_context_required",
        }
        if not isinstance(delivery_context, _DeliveryContext):
            return forbidden
        store = self._companion_store()
        if store is None:
            return {
                "schema_version": "emotion_afterglow_delivery.v1",
                "state": "degraded",
                "read_only": True,
                "events": [],
                "next_cursor": "",
                "has_more": False,
                "error_code": "store_unavailable",
            }
        try:
            events = await store.list_deliverable(
                scope=delivery_context.scope,
                platform=delivery_context.platform,
                user_id=delivery_context.user_id,
                session_id=delivery_context.session_id,
                allow_cross_window=delivery_context.allow_cross_window,
                limit=int(limit),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[companion_bridge] emotion list failed: {exc}")
            return {
                "schema_version": "emotion_afterglow_delivery.v1",
                "state": "degraded",
                "read_only": True,
                "events": [],
                "next_cursor": "",
                "has_more": False,
                "error_code": "store_error",
            }
        return {
            "schema_version": "emotion_afterglow_delivery.v1",
            "state": "ready",
            "read_only": True,
            "events": events,
            "next_cursor": "",
            "has_more": False,
        }

    async def ack_emotion_events(
        self, event_refs: Any, *, delivery_context: Any = None, **_legacy: Any
    ) -> dict[str, Any]:
        forbidden = {
            "state": "forbidden",
            "acked": 0,
            "error_code": "delivery_context_required",
        }
        if not isinstance(delivery_context, _DeliveryContext):
            return forbidden
        if not isinstance(event_refs, list):
            return forbidden
        store = self._companion_store()
        if store is None:
            return forbidden
        try:
            acked = await store.ack(
                [r for r in event_refs[:64] if isinstance(r, dict)],
                consumer_id=delivery_context.consumer_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[companion_bridge] ack failed: {exc}")
            return forbidden
        return {
            "acked": int(acked),
            "consumer_id": delivery_context.consumer_id,
            "acked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    # ------------------------------------------------------------------
    # optional surfaces (spec §2.5)
    # ------------------------------------------------------------------

    async def search_open_loops(
        self, *, session_id: str = "", limit: int = 3
    ) -> list[dict[str, Any]]:
        loader = getattr(self._engine(), "load_open_loop_memories", None)
        if loader is None:
            return []
        try:
            rows = await loader(session_id=session_id or None, limit=int(limit))
        except Exception:  # noqa: BLE001
            return []
        return [
            {
                "memory_id": row.get("memory_id"),
                "content": str(row.get("content") or "")[:300],
                "session_id": str(row.get("session_id") or ""),
                "occurred_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(row.get("create_time") or 0)
                ),
                "age_days": float(row.get("age_days") or 0.0),
                "open_loop_weight": 0.7 if row.get("due_ts") else 0.5,
                "promise_weight": 0.8 if row.get("promise") else 0.2,
                "memory_reason": str(row.get("reason") or "")[:200],
            }
            for row in rows
        ]

    def get_relationship_phase(
        self,
        *,
        session_id: str = "",
        scope: str = "private",
        platform: str = "",
        user_id: str = "",
        group_id: str = "",
        bot_id: str = "",
    ) -> dict[str, Any]:
        # v1: honest unknown shape; real estimation arrives with the portrait.
        return {"phase": "unknown", "momentum": 0.0}

    def peek_relationship_phase(
        self,
        *,
        session_id: str = "",
        scope: str = "private",
        platform: str = "",
        user_id: str = "",
        group_id: str = "",
        bot_id: str = "",
    ) -> dict[str, Any]:
        return {"observed": False, "phase": "unknown", "momentum_band": "unknown"}

    # ------------------------------------------------------------------
    # portrait surface (REQ-036; spec §13). Shapes mirror MC's bridge
    # read_unified_profile_portrait / unified_profile_portrait_status /
    # run_unified_profile_portrait_batch: when the service is absent
    # (feature switch off or bridge disabled) they degrade to honest
    # bridge_unavailable codes, which the companion already tolerates.
    # ------------------------------------------------------------------

    def _portrait_service(self) -> Any:
        if self._disabled():
            return None
        engine = self._engine()
        return getattr(engine, "portrait_service", None) if engine else None

    async def read_unified_profile_portrait(
        self, request: Any = None, *, limit: int = 8
    ) -> dict[str, Any]:
        base = {
            "ok": False,
            "read_only": True,
            "code": "bridge_unavailable",
            "items": [],
        }
        service = self._portrait_service()
        if service is None:
            return base
        try:
            result = await service.read_summary(
                request if isinstance(request, dict) else {},
                limit=max(1, min(16, int(limit))),
            )
        except Exception:  # noqa: BLE001
            return {**base, "code": "bridge_degraded"}
        if not isinstance(result, dict):
            return {**base, "code": "bridge_degraded"}
        # Second-layer low-sensitivity filter mirroring MC: nothing leaves
        # the bridge surface unless explicitly labelled low.
        raw_items = result.get("items")
        items: list[dict[str, Any]] = []
        for item in raw_items if isinstance(raw_items, list) else []:
            if not isinstance(item, dict):
                continue
            if str(item.get("sensitivity") or "")[:24] != "low":
                continue
            items.append(
                {
                    "dimension": str(item.get("dimension") or "")[:80],
                    "summary": str(item.get("summary") or "")[:180],
                    "portrait_tier": str(item.get("portrait_tier") or "")[:24],
                    "epistemic_status": str(item.get("epistemic_status") or "")[:40],
                    "confidence": float(item.get("confidence") or 0),
                    "updated_at": str(item.get("updated_at") or "")[:80],
                }
            )
        return {
            "ok": bool(result.get("ok")),
            "read_only": True,
            "code": str(result.get("code") or "bridge_degraded")[:80],
            "items": items,
            "portrait_revision": int(result.get("portrait_revision") or 0),
        }

    async def unified_profile_portrait_status(
        self, person_id: Any = ""
    ) -> dict[str, Any]:
        fallback = {
            "ok": False,
            "read_only": True,
            "code": "bridge_unavailable",
            "last_synced_at": "",
            "portrait_revision": 0,
        }
        service = self._portrait_service()
        if service is None:
            return fallback
        try:
            result = await service.status(str(person_id or ""))
        except Exception:  # noqa: BLE001
            return {**fallback, "code": "bridge_degraded"}
        if not isinstance(result, dict):
            return {**fallback, "code": "bridge_degraded"}
        return {
            "ok": bool(result.get("ok")),
            "read_only": True,
            "code": str(result.get("code") or "bridge_degraded")[:80],
            "last_synced_at": str(result.get("last_synced_at") or "")[:80],
            "portrait_revision": int(result.get("portrait_revision") or 0),
        }

    async def run_unified_profile_portrait_batch(
        self, person_id: Any = "", *, run_day: str = ""
    ) -> dict[str, Any]:
        service = self._portrait_service()
        if service is None:
            return {"ok": False, "code": "bridge_unavailable"}
        try:
            result = await service.run_daily_batch(
                str(person_id or ""), run_day=str(run_day or "")
            )
        except Exception:  # noqa: BLE001
            return {"ok": False, "code": "bridge_degraded"}
        return (
            dict(result)
            if isinstance(result, dict)
            else {"ok": False, "code": "bridge_degraded"}
        )


def bot_self_id(bot_id: str) -> str:
    """Fallback sender id for bot-authored mirrored turns."""
    return f"bot_self:{bot_id}" if bot_id else "bot_self"


def _extract_payload_summary(payload: dict) -> str:
    """Best-effort one-line summary from an archive payload dict."""
    for key in (
        "summary",
        "text",
        "content",
        "title",
        "description",
        "plan",
        "activity",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:400]
        if isinstance(value, list):
            joined = "；".join(str(x).strip() for x in value[:4] if x)
            if joined:
                return joined[:400]
    return "（空归档）"


def _to_ts(value: Any) -> float:
    """Normalize ISO text / numeric seconds to float epoch seconds (MC feeds ISO)."""
    from ...storage.companion_store import to_epoch_seconds

    return to_epoch_seconds(value)
