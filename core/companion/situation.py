"""Situation clues from the companion plugin: read, guard, apply — never store.

Spec §0 数据分诊 承诺：局面线索（``event.private_companion_context``）只用于
改写检索词，不留档。PC 在消息管道早期（``message_pipeline``）就把 payload 挂到
事件上，所以这一类线索只在**本轮**有效，任何一处都不落库。

两个消费面：

- 话题类（topic / entities / facts / keywords）→ 扩写本轮检索 query。
  落点在召回链（``memory_recall``），因为 PC 调 ``compose_context`` 时只传
  query/session_context/mood/energy，事件对象根本不在参数里，桥内拿不到。
- 心情 / 精力（mood_bias / energy）→ 生成注入包的氛围提示行。
  MC 在注入组装阶段消费它们（``injection._atmosphere_hint``），落点在桥内。

全部为纯正则 / 集合运算，零 LLM 调用，符合 compose_context 的预算纪律。
"""

from __future__ import annotations

import re
from typing import Any

#: PC 挂载线索的属性名（adapter 里 setattr 的名字，与 spec §0 一致）。
SITUATION_ATTR = "private_companion_context"

# MC context_orchestrator 的字段候选表，照抄以免两侧取值口径漂移。
_TOPIC_KEYS = ("topic", "title", "subject", "current_topic")
_ENTITY_KEYS = ("entities", "entity", "participants", "users")
_FACT_KEYS = ("facts", "key_facts", "recent_facts", "memory_facts")
_KEYWORD_KEYS = ("keywords", "keyword", "main_topics", "topics")
_NOTE_KEYS = (
    "motive",
    "scene",
    "topic_summary",
    "planned_proactive_motive",
    "planned_proactive_reason",
    "schedule",
)

_MOOD_KEYS = ("mood_bias", "mood", "bot_mood", "emotion", "bot_emotion", "emotional_state")
_ENERGY_KEYS = ("energy", "bot_energy", "psychological_energy")

#: MC 把扩写后的 query 截断到 1400 字符。
_QUERY_MAX_CHARS = 1400
_FIELD_MAX_CHARS = 120

_TERM_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]{2,}")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{4,}")


def _clean(value: Any, limit: int = _FIELD_MAX_CHARS) -> str:
    """Trim one scalar to a single line, MC ``clean_text`` 的等价物。"""
    text = str(value if value is not None else "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit]


def read_situation(event: Any) -> dict[str, Any]:
    """Read the companion situation payload off an event; ``{}`` when absent.

    Only the ``private_companion_context`` attribute is used (that is what PC
    writes). ``event.get_extra`` is tried as a secondary channel because the
    fork already routes cross-plugin data through extras elsewhere.
    """
    if event is None:
        return {}
    payload = getattr(event, SITUATION_ATTR, None)
    if not isinstance(payload, dict):
        getter = getattr(event, "get_extra", None)
        if callable(getter):
            try:
                payload = getter(SITUATION_ATTR)
            except Exception:  # noqa: BLE001
                payload = None
    if isinstance(payload, dict):
        return payload
    # Some producers hand over a plain object; fall back to its attributes.
    fields = getattr(payload, "__dict__", None)
    return dict(fields) if isinstance(fields, dict) else {}


def _coerce_list(value: Any) -> list[str]:
    """Normalise a payload field that may be a scalar, list or mapping.

    ``_memory_companion_attach_context`` merges ``entities``/``facts``/
    ``keywords`` as lists but overwrites every other key with a scalar, so a
    reader must accept both shapes.
    """
    if value in (None, "", [], {}):
        return []
    if isinstance(value, list):
        return [_clean(item) for item in value if _clean(item)]
    if isinstance(value, dict):
        return [_clean(item) for item in value.values() if _clean(item)]
    text = _clean(value)
    return [text] if text else []


def _first_text(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            value = next((item for item in value if _clean(item)), "")
        text = _clean(value)
        if text:
            return text
    return ""


def _list_field(payload: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    collected: list[str] = []
    for key in keys:
        collected.extend(_coerce_list(payload.get(key)))
    return list(dict.fromkeys(collected))[:12]


def topic_terms(payload: dict[str, Any]) -> list[str]:
    """Ordered companion terms used to widen the retrieval query (MC order)."""
    topic = _first_text(payload, _TOPIC_KEYS)
    entities = _list_field(payload, _ENTITY_KEYS)
    facts = _list_field(payload, _FACT_KEYS)
    keywords = _list_field(payload, _KEYWORD_KEYS)
    return [term for term in [topic, *entities, *facts[:4], *keywords[:8]] if term]


def note_terms(payload: dict[str, Any]) -> list[str]:
    """Companion notes (motive/scene/schedule), for prompt hints only."""
    return _list_field(payload, _NOTE_KEYS)


def mood_and_energy(payload: dict[str, Any]) -> tuple[str, float]:
    """Read ``mood_bias`` / ``energy`` from a situation payload."""
    mood = _first_text(payload, _MOOD_KEYS)
    raw = _first_text(payload, _ENERGY_KEYS)
    try:
        energy = float(raw) if raw else 0.0
    except (TypeError, ValueError):
        energy = 0.0
    return mood, energy


def _overlap_terms(values: list[str]) -> set[str]:
    """2-gram / word set for the overlap guard, MC ``_overlap_terms`` 等价。"""
    text = " ".join(_clean(value, 160) for value in values if _clean(value, 160)).lower()
    terms: set[str] = set()
    for word in _TERM_RE.findall(text):
        if _CJK_RUN_RE.fullmatch(word):
            terms.update(word[i : i + 2] for i in range(0, len(word) - 1))
        if len(word) >= 2:
            terms.add(word)
    return terms


def overlaps_message(message_query: str, companion_terms: list[str]) -> bool:
    """True when any companion term shares a 2-gram/word with the message.

    This is the guard that stops an unrelated old thread from hijacking the
    current turn's retrieval (MC: ``_companion_overlaps_message``).
    """
    message_terms = _overlap_terms([message_query])
    hint_terms = _overlap_terms(companion_terms)
    if not message_terms or not hint_terms:
        return False
    return bool(message_terms & hint_terms)


def expand_query(
    query: str, payload: dict[str, Any], *, message_query: str = ""
) -> tuple[str, str]:
    """Widen ``query`` with the companion's topic terms.

    The terms are only used when they overlap the current message; otherwise
    they are dropped, so a stale topic cannot pull retrieval off course.

    Args:
        query: the query already built by the recall chain.
        payload: situation payload from :func:`read_situation`.
        message_query: the current user message used for the overlap guard.

    Returns:
        ``(query, status)`` where status is one of ``no_clues`` /
        ``guarded_no_overlap`` / ``guarded_overlap``.
    """
    terms = topic_terms(payload) if payload else []
    if not terms:
        return query, "no_clues"
    if not overlaps_message(message_query or query, terms):
        return query, "guarded_no_overlap"
    parts = [*terms, query]
    expanded = " ".join(part for part in parts if _clean(part, 160))
    expanded = _clean(expanded, _QUERY_MAX_CHARS)
    return (expanded or query), "guarded_overlap"


def atmosphere_lines(mood: str, energy: float) -> list[str]:
    """Atmosphere hint lines for the injection package (MC semantics).

    MC renders these during injection assembly so the model knows the bot's own
    mood/energy when deciding *how* to bring memories up. Buckets and wording
    follow ``injection._atmosphere_hint``.
    """
    hints: list[str] = []
    mood_lower = _clean(mood, 80).lower()
    if mood_lower:
        if any(kw in mood_lower for kw in ("累", "疲惫", "低落", "疲", "倦")):
            hints.append("你此刻心理状态偏疲态；记忆注入以轻量、温暖为主，避免大量翻旧账加重负担。")
        elif any(kw in mood_lower for kw in ("开心", "愉快", "兴奋", "高涨", "好心情")):
            hints.append("你此刻心情不错；记忆可以更活泼地融入，用轻松的方式提起共同经历。")
        elif any(kw in mood_lower for kw in ("难过", "伤心", "低气压", "emo", "郁")):
            hints.append("你此刻情绪偏低；如果记忆中有温暖、被关心的经历，可以自然用它来安抚自己，但不要强行翻沉重旧事。")
        elif any(kw in mood_lower for kw in ("生气", "愤怒", "不爽", "烦")):
            hints.append("你此刻情绪不太稳定；记忆只用来理解关系脉络，不要在情绪上头时翻敏感旧事。")
    try:
        value = float(energy)
    except (TypeError, ValueError):
        value = 0.0
    if 0 < value < 30:
        hints.append("你此刻心理能量很低；记忆注入以最少必要为主，优先用语气底色而非明说来减轻认知负担。")
    elif 0 < value < 50:
        hints.append("你此刻心理能量偏低；记忆可以参与但以轻量提及为主，避免一次引入太多线索。")
    return hints


__all__ = [
    "SITUATION_ATTR",
    "atmosphere_lines",
    "expand_query",
    "mood_and_energy",
    "note_terms",
    "overlaps_message",
    "read_situation",
    "topic_terms",
]
