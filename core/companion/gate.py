"""Silence gate: decide whether this turn deserves long-term memory at all.

Ported from the memory_companion plugin's turn_signal.py + time_intent.py
(pure-function classifiers, no store dependency). Author-persona-specific
terms from the original word lists were removed; generic Chinese chitchat
markers stay. The gate answers three questions per turn:

- low_information: reaction-only / affection-only / correction-only / empty
  messages do not deserve long-term memory retrieval at all (saves the whole
  package's tokens and prevents "did you eat?" recalling your childhood);
- current_state_chat: 在干嘛/吃了吗 type questions only allow recent,
  directly relevant state memories (retrieval is constrained, not skipped);
- time_intent: explicit day/week windows for 最近X天 style summaries.

See docs/companion-fork-spec.md section 5.5.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# --- classifier vocabularies (generic CJK; no persona names) ---

REACTION_TOKENS = frozenset({
    "嗯", "嗯嗯", "啊", "哦", "噢", "唔", "哇", "好", "好的", "行", "好嘞", "好滴",
    "嗯呢", "嗯哼", "草", "乐", "哈", "哈哈", "哈哈哈", "？", "?", "什么", "啥",
    "对", "是", "收到", "了解", "明白", "知道啦", "嘿嘿", "嘻嘻", "x", "xx",
    "qwq", "awa", "6", "66", "666", "1", "233", "2333", "h", "hh", "hhh",
})
AFFECTION_CHARS = "摸贴抱蹭亲揉拍戳"
AFFECTION_UNITS = ("摸摸", "贴贴", "抱抱", "蹭蹭", "亲亲", "揉揉", "拍拍", "戳戳", "rua")
AFFECTION_TARGETS = frozenset({
    "你", "你呀", "你哦", "宝宝", "宝贝", "老婆", "老公", "姐姐", "妹妹",
    "头", "脑袋", "小可爱", "小宝",
})
AFFECTION_TARGET_SUFFIXES = ("酱", "宝", "宝宝", "宝贝", "老婆", "亲", "达令", "亲爱的")
CORRECTION_TOKENS = frozenset({
    "不对", "不是", "错了", "不对吧", "不是吧", "不对啊", "不是啊", "不对呀",
    "不是呀", "并不是", "没错",
})
CORRECTION_MARKERS = (
    "你说错", "说错了", "答错了", "不是这个", "不是这样", "不对劲", "理解错",
    "搞错了", "记错了", "别乱说", "又忘了", "胡说", "瞎说",
)
# "吃了吗 / 在干嘛": current-state small talk — only fresh state memories fit.
CURRENT_STATE_MARKERS = (
    "吃了吗", "吃了没", "吃啥", "吃饭了吗", "吃了么", "在干嘛", "在干什么", "在忙吗",
    "在忙什么", "干嘛呢", "忙吗", "睡了吗", "睡了没", "起床了吗", "起床没", "起床了",
    "醒了吗", "下班了吗", "到家了吗", "今天怎么样", "现在在做什么",
)
CONTEXT_DEPENDENT_MARKERS = (
    "刚才", "上面", "前面", "上一", "继续", "接着", "再来", "再发", "再画", "也来",
    "同样", "换个", "为什么", "咋回事", "怎么回事", "啥意思",
)

_TERM_STOPWORDS = frozenset({
    "给我", "你的", "一张", "一下", "这个", "那个", "什么", "怎么", "为什么",
    "可以", "是不是", "有没有", "知道", "当前", "用户",
})


def _clean(text: object, limit: int = 1200) -> str:
    """Trim non-str values and collapse whitespace (replaces MC clean_text)."""
    value = text if isinstance(text, str) else str(text or "")
    return " ".join(value.split())[:limit]


def _compact(text: object) -> str:
    """Lowercased marker-stripped form used for token membership tests."""
    value = _clean(text, 1200)
    value = re.sub(r"\[At:\d+\]", "", value, flags=re.IGNORECASE)
    value = re.sub(r"@[\w一-鿿]+", "", value)
    value = re.sub(r"[\s,，。.!！~～…、:：;；\"'“”‘’()（）\[\]【】<>《》]+", "", value)
    return value.lower()


def message_terms(text: object, *, limit: int = 40) -> list[str]:
    """Extract 2-4 gram Chinese blocks + ascii tokens for query expansion."""
    compact = _compact(text)
    if not compact:
        return []
    terms: list[str] = list(re.findall(r"[a-z0-9_]{2,}", compact))
    for block in re.findall(r"[\u4e00-\u9fff]+", compact):
        if len(block) <= 1:
            continue
        if len(block) <= 4:
            terms.append(block)
        for size in (2, 3, 4):
            if len(block) >= size:
                terms.extend(block[i:i + size] for i in range(len(block) - size + 1))
    filtered = [t for t in terms if len(t) >= 2 and t not in _TERM_STOPWORDS]
    return list(dict.fromkeys(filtered))[:limit]


@dataclass(slots=True)
class GateDecision:
    """Per-turn gate outcome consumed by the recall handler."""

    retrieve: bool = True          # whether long-term search runs at all
    state_only: bool = False       # current-state chat: recent+relevant only
    reason: str = ""               # machine-readable code (for /lmem explain)
    signal_kind: str = "normal"    # reaction/affection/correction/state/empty/normal
    terms: list[str] = field(default_factory=list)

    @classmethod
    def pass_through(cls) -> GateDecision:
        return cls(terms=message_terms(""))


def decide_gate(text: object) -> GateDecision:
    """Should this message get long-term memory? (pure, no I/O)

    Args:
        text: the raw current user message.

    Returns:
        GateDecision with retrieve/state_only/reason filled.
    """
    compact = _compact(text)
    terms = message_terms(text)
    if not compact:
        return GateDecision(retrieve=False, reason="empty_message",
                            signal_kind="empty", terms=terms)
    if compact in REACTION_TOKENS or (
        len(compact) <= 12 and re.fullmatch(r"(哈|呵|嘿|嘻|嗯|啊|哦|噢|唔|哇|草|乐|x)+", compact)
    ):
        return GateDecision(retrieve=False, reason="reaction_only",
                            signal_kind="reaction", terms=terms)
    if _is_affection_only(compact):
        return GateDecision(retrieve=False, reason="affection_only",
                            signal_kind="affection", terms=terms)
    if _is_correction(compact):
        return GateDecision(retrieve=False, reason="correction_only",
                            signal_kind="correction", terms=terms)
    if len(compact) <= 16 and any(m in compact for m in CURRENT_STATE_MARKERS):
        return GateDecision(retrieve=True, state_only=True, reason="current_state_chat",
                            signal_kind="state", terms=terms)
    return GateDecision(terms=terms)


def _is_affection_only(compact: str) -> bool:
    if not compact:
        return False
    rest = compact
    for unit in AFFECTION_UNITS:
        rest = rest.replace(unit, "")
    if not rest or rest in AFFECTION_TARGETS:
        return True
    if len(rest) <= 4 and re.fullmatch(r"[\u4e00-\u9fff]+", rest):
        if len(rest) == 2 and rest[0] == rest[1]:
            return True
        if any(rest.endswith(s) for s in AFFECTION_TARGET_SUFFIXES):
            return True
    return len(compact) >= 2 and all(ch in AFFECTION_CHARS for ch in compact)


def _is_correction(compact: str) -> bool:
    if compact in CORRECTION_TOKENS:
        return True
    return len(compact) <= 12 and any(m in compact for m in CORRECTION_MARKERS)


# ----------------------------------------------------------------------
# time window intent (ported from time_intent.py, LOCAL_TZ kept Asia/Shanghai)
# ----------------------------------------------------------------------

LOCAL_TZ_NAME = "Asia/Shanghai"


@dataclass(slots=True)
class TimeWindow:
    """Parsed calendar-window intent for 最近X天/本周/昨天 style questions."""

    active: bool = False
    label: str = ""
    start_ts: float = 0.0
    end_ts: float = 0.0
    summary_like: bool = False

    @property
    def display_range(self) -> str:
        if not self.active:
            return ""
        return self.label


_SUMMARY_MARKERS = (
    "怎么样", "如何", "总结", "概括", "回顾", "发生了什么", "聊了什么", "说了什么",
    "讲了什么", "问了什么", "做了什么", "有什么", "有哪些", "过得", "状态", "近况",
)
_LABELS = {
    "today": "今天", "yesterday": "昨天", "day_before_yesterday": "前天",
    "recent_week": "最近一周", "current_week": "本周", "previous_week": "上周",
    "recent_month": "最近一个月", "current_month": "本月", "previous_month": "上个月",
    "recent_few_days": "最近几天", "recent_default_summary": "最近",
}


def _local_now(now: datetime | None) -> datetime:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(LOCAL_TZ_NAME)
    except Exception:  # zoneinfo unavailable on stripped runtimes
        tz = timezone(timedelta(hours=8))
    current = now or datetime.now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    return current.astimezone(tz)


def parse_time_window(text: object, *, now: datetime | None = None) -> TimeWindow:
    """Parse explicit time windows. Returns inactive window when nothing matches."""
    compact = re.sub(r"\s+", "", _clean(text, 1000)).lower()
    if not compact:
        return TimeWindow()
    current = _local_now(now)
    today = current.replace(hour=0, minute=0, second=0, microsecond=0)
    summary_like = any(m in compact for m in _SUMMARY_MARKERS)

    start: datetime | None = None
    end: datetime | None = None
    source = ""

    if "前天" in compact:
        start, end, source = today - timedelta(days=2), today - timedelta(days=1), "day_before_yesterday"
    elif "昨天" in compact or "昨日" in compact:
        start, end, source = today - timedelta(days=1), today, "yesterday"
    elif "今天" in compact or "今日" in compact:
        start, end, source = today, today + timedelta(days=1), "today"
    elif "上个月" in compact or "上月" in compact:
        first = today.replace(day=1)
        prev = first - timedelta(days=1)
        start, end, source = prev.replace(day=1), first, "previous_month"
    elif "这个月" in compact or "本月" in compact or "这月" in compact:
        start, end, source = today.replace(day=1), today + timedelta(days=1), "current_month"
    elif "最近一个月" in compact or "近一个月" in compact or "过去一个月" in compact:
        start, end, source = today - timedelta(days=30), today + timedelta(days=1), "recent_month"
    elif "上周" in compact or "上一周" in compact:
        monday = today - timedelta(days=today.weekday())
        start, end, source = monday - timedelta(days=7), monday, "previous_week"
    elif "本周" in compact or "这周" in compact or "这一周" in compact:
        start, end, source = today - timedelta(days=today.weekday()), today + timedelta(days=1), "current_week"
    elif any(m in compact for m in ("最近一周", "近一周", "过去一周", "最近7天", "最近七天", "过去7天", "过去七天")):
        start, end, source = today - timedelta(days=7), today + timedelta(days=1), "recent_week"
    else:
        days = _relative_days(compact)
        if days:
            start, end, source = today - timedelta(days=days), today + timedelta(days=1), f"recent_{days}_days"
        elif "这几天" in compact or "最近几天" in compact or "近几天" in compact:
            start, end, source = today - timedelta(days=3), today + timedelta(days=1), "recent_few_days"
        elif "最近" in compact and summary_like:
            start, end, source = today - timedelta(days=7), today + timedelta(days=1), "recent_default_summary"

    if start is None or end is None:
        return TimeWindow(summary_like=summary_like)
    label = _LABELS.get(source, "")
    if not label and source.startswith("recent_") and source.endswith("_days"):
        label = f"最近 {source.removeprefix('recent_').removesuffix('_days')} 天"
    return TimeWindow(
        active=True, label=label or source,
        start_ts=start.timestamp(), end_ts=end.timestamp(),
        summary_like=summary_like,
    )


def _relative_days(compact: str) -> int:
    match = re.search(r"(最近|过去|近)(\d{1,2})天", compact)
    if match:
        return max(1, min(60, int(match.group(2))))
    chinese = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
               "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    match = re.search(r"(最近|过去|近)([一二两三四五六七八九十])天", compact)
    return chinese.get(match.group(2), 0) if match else 0
