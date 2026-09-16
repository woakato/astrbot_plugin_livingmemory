"""Fixed injection slots (spec §5.2): core block, mood afterglow line,
relationship line, open loops, bot-self-today line — collected once and
shared by the main-chain injection and the companion bridge's compose_context.

Every collector is budget-cheap: small metadata queries only, no LLM calls,
no vector search. Individual failures degrade to an empty slot (the package
just loses one line), never to an exception on the request path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

from .composer import MemoryItem

# Relationship line: five coarse bands from 30-day interaction warmth.
_RELATIONSHIP_BANDS = (
    (0, "初识", "保持礼貌距离"),
    (8, "熟络", "可以开轻松玩笑"),
    (25, "亲近", "熟悉彼此习惯"),
    (60, "亲密", "自然亲昵，注意分寸"),
    (120, "深厚", "像老朋友"),
)
_SELF_KINDS = ("schedule_fragment", "archive", "persona_life", "creative_work")


@dataclass
class SlotBundle:
    """Pre-assembled fixed slots for one injection round."""

    core_blocks: list[dict[str, Any]] = field(default_factory=list)
    one_line_slots: list[tuple[str, str]] = field(default_factory=list)
    open_loops: list[MemoryItem] = field(default_factory=list)
    deferred_sections: set[str] = field(default_factory=set)


async def collect_slots(
    *,
    config_manager: Any,
    memory_engine: Any,
    companion_store: Any,
    session_id: str,
    persona_id: str | None,
    user_id: str = "",
    platform: str = "",
    event_extras: set[str] | None = None,
    session_message_count: int = 0,
) -> SlotBundle:
    """Collect all enabled fixed slots for this turn.

    Args:
        user_id/platform: current speaker identity (private-scope queries).
        event_extras: sections the companion plugin deferred this round
            (PC's defer protocol payload), e.g. {"self_timeline"}.
        session_message_count: conversation volume feeding the relationship
            estimate (sessions table message_count).

    Returns:
        A SlotBundle; each disabled/failed collector leaves its part empty.

    Note:
        companion_events are matched by kind+date only, not bot_id: the
        recall side cannot reliably read the bot id the bridge stamped at
        write time, and this deployment is single-bot by design (spec §11).
    """
    bundle = SlotBundle(deferred_sections=set(event_extras or ()))
    get = config_manager.get

    # ---- core block (always resident) ----
    if get("companion_slots.enable_core_block", True) and get("core_memory.enabled", True):
        try:
            bundle.core_blocks = await memory_engine.load_core_memories(
                session_id=session_id,
                persona_id=persona_id,
                user_id=user_id,
                max_blocks=int(get("core_memory.max_blocks", 8)),
                max_chars=int(get("core_memory.max_chars", 800)),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[slots] core block load failed: {exc}")

    # ---- mood afterglow line ----
    if companion_store is not None and get("companion_slots.enable_mood_line", True):
        try:
            rows = await companion_store.peek_pending(
                scope="private", platform=platform,
                user_id=user_id, session_id=session_id,
                allow_cross_window=bool(
                    get("companion_bridge.cross_window_emotional_continuity_enabled", False)),
                limit=3)
            mood_map = {"scar_touched": "心里还有点发酸", "warm_memory": "带着暖意",
                        "vulnerable_resonance": "变得柔软"}
            for row in rows:
                phrase = mood_map.get(str(row.get("event_type") or ""))
                if phrase:
                    bundle.one_line_slots.append(("emotional_hint", f"情绪余波：{phrase}"))
                    break
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[slots] mood line failed: {exc}")

    # ---- relationship one-liner (v1 rough estimate) ----
    if get("companion_slots.enable_relationship_line", True):
        try:
            bundle.one_line_slots.extend(
                await _relationship_slots(
                    companion_store=companion_store, user_id=user_id,
                    platform=platform, session_message_count=session_message_count))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[slots] relationship line failed: {exc}")

    # ---- open loops ----
    if get("companion_slots.enable_open_loops", True):
        try:
            loops = await memory_engine.load_open_loop_memories(
                session_id=session_id, limit=6)
            bundle.open_loops = [
                MemoryItem(text=_loop_text(item), source="open_loop",
                           time_label=f"约{item['age_days']}天前" if item.get("age_days") else "",
                           weight=(2.0 if item.get("due_ts") and item["due_ts"] < time.time() else 0.0)
                           + (0.5 if item.get("promise") else 0.0))
                for item in loops[:3]
            ]
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[slots] open loops failed: {exc}")

    # ---- bot-self today line, with defer-based yielding ----
    suppress_self = bool(get("companion_bridge.suppress_self_timeline_when_companion_seen", True))
    if (companion_store is not None and get("companion_slots.enable_self_line", True)
            and not (suppress_self and "self_timeline" in bundle.deferred_sections)):
        try:
            today = time.strftime("%Y-%m-%d", time.gmtime())
            rows = await companion_store.list_events_between(
                kinds=_SELF_KINDS, since_date=today, until_date=today,
                limit=6)
            texts = [str(r.get("content") or "").strip() for r in rows if r.get("content")]
            if texts:
                bundle.one_line_slots.append(("self_timeline", "今天：" + "；".join(texts[:2])[:36]))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[slots] self line failed: {exc}")

    return bundle


def _loop_text(item: dict[str, Any]) -> str:
    content = str(item.get("content") or "")[:60]
    due = item.get("due_ts") or 0.0
    if due and due < time.time():
        return f"{content}（已到期，记得问结果）"
    if item.get("age_days", 0) >= 5:
        return f"{content}（约{item['age_days']:.0f}天前，可关心进展）"
    return content


async def _relationship_slots(*, companion_store: Any, user_id: str,
                              platform: str,
                              session_message_count: int = 0) -> list[tuple[str, str]]:
    """Warmth-graded one-liner from 30-day emotion events + session volume."""
    stats = {"total": 0}
    if companion_store is not None and user_id:
        stats = await companion_store.interaction_stats(
            scope="private", platform=platform,
            user_id=user_id, since_ts=time.time() - 30 * 86400)
    score = int(stats.get("total") or 0) + int(session_message_count or 0) // 10
    band_name, band_hint = _RELATIONSHIP_BANDS[0][1], _RELATIONSHIP_BANDS[0][2]
    for threshold, name, hint in _RELATIONSHIP_BANDS:
        if score >= threshold:
            band_name, band_hint = name, hint
    return [("relationship_hint", f"关系阶段：{band_name}（{band_hint}）")]
