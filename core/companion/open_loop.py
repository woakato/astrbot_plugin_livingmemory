"""Open-loop detector: pure-rule tagging for unfinished commitments.

Deliberately model-free: reflection runs on whatever LLM the user configured,
and commitment detection must not depend on that model's instruction
following. Cheap Chinese cue words cover the cases that matter for
companionship (promises, appointments, pending events worth following up).
Output metadata feeds engine.load_open_loop_memories / bridge.search_open_loops.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

# Explicit commitment language. Bare reminders ("记得吃饭") are deliberately
# NOT strong promises: they only tag when a time/event/pending cue co-occurs.
PROMISE_MARKS = ("说好", "答应", "约定", "承诺", "帮我留意", "我会", "我要去", "我得")
# Weak reminder cues: counted only together with an event/pending/due signal.
WEAK_PROMISE_MARKS = ("记得", "别忘了")
# Postponement phrasing that signals "no real appointment" ("明天再说吧").
SOFTEN_EXCLUDES = ("再说", "以后再说", "下次再说", "再说吧")
# Pending/unfinished cue: something has not happened yet.
PENDING_MARKS = (
    "还没",
    "尚未",
    "未回",
    "没回",
    "没去",
    "没问",
    "没说",
    "等结果",
    "等回复",
    "到时候",
)
# Event words that usually deserve a follow-up question.
EVENT_MARKS = (
    "面试",
    "考试",
    "汇报",
    "开会",
    "会议",
    "演出",
    "比赛",
    "手术",
    "复查",
    "体检",
    "高铁",
    "火车",
    "航班",
    "飞机",
    "出发",
    "到家",
    "来找我",
    "搬家",
    "入职",
    "答辩",
    "放榜",
    "开奖",
    "结果",
)
# Near-future markers -> days offset for due_ts (coarse on purpose).
_NEAR_DAYS: tuple[tuple[str, int], ...] = (
    ("明天", 1),
    ("明日", 1),
    ("后天", 2),
    ("大后天", 3),
    ("下周", 7),
    ("下星期", 7),
    ("下礼拜", 7),
    ("周末", 5),
    ("这周末", 5),
)
_NEAR_DAYS_KEYS = tuple(mark for mark, _ in _NEAR_DAYS)
_WEEKDAY_INDEX = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
}
_DATE_RE = re.compile(r"(\d{1,2})\s*[月](\d{1,2})\s*[日号]")
_DAY_RE = re.compile(r"[月]?\s*(\d{1,2})\s*[日号]")
_WEEKDAY_RE = re.compile(r"(?:星期|周|礼拜)([一二三四五六日天])")


def _due_ts(text: str, now: float) -> float:
    """Estimate a due timestamp (epoch seconds) from the text; 0 when unclear."""
    current = datetime.fromtimestamp(now)
    base = current.replace(hour=20, minute=0, second=0, microsecond=0)

    for mark, days in _NEAR_DAYS:
        if mark in text:
            target = base + timedelta(days=days)
            if mark in ("周末", "这周末"):
                # snap forward to the coming Saturday
                target = base + timedelta(days=(5 - base.weekday()) % 7 or 7)
            return target.timestamp()

    weekday_match = _WEEKDAY_RE.search(text)
    if weekday_match:
        wanted = _WEEKDAY_INDEX.get(weekday_match.group(1))
        if wanted is not None:
            delta = (wanted - current.weekday()) % 7 or 7
            return (base + timedelta(days=delta)).timestamp()

    date_match = _DATE_RE.search(text)
    if date_match:
        try:
            month, day = int(date_match.group(1)), int(date_match.group(2))
            target = current.replace(month=month, day=day, hour=20)
            if target < current:
                # Explicit month/day already passed this year: it means next
                # year's same date (e.g. "3月5日" seen in September), not a
                # shifted month of the current year.
                target = target.replace(year=current.year + 1)
            return target.timestamp()
        except ValueError:
            return 0.0

    day_match = _DAY_RE.search(text)
    if day_match:
        try:
            day = int(day_match.group(1))
            month = current.month
            for _ in range(2):
                year = current.year + (1 if month > 12 else 0)
                target = current.replace(
                    year=year, month=(month - 1) % 12 + 1, day=day, hour=20
                )
                if target >= current:
                    return target.timestamp()
                month += 1
        except ValueError:
            return 0.0
    return 0.0


def analyze_open_loop(text: str, *, now: float | None = None) -> dict[str, Any]:
    """Tag a summarized memory with open-loop metadata (empty dict if none).

    Args:
        text: the memory/summary content to inspect.
        now: override for "now" (epoch seconds), for tests.

    Returns:
        Metadata keys to merge into the document metadata: open_loop,
        promise, due_ts, open_loop_reason — or {} when nothing matched.
    """
    if not text:
        return {}
    now_value = now if now is not None else __import__("time").time()
    # Postponement phrasing ("明天再说吧") explicitly closes the topic; tag it
    # only when the sentence carries a stronger cue elsewhere.
    postponed = any(mark in text for mark in SOFTEN_EXCLUDES)
    has_promise = any(mark in text for mark in PROMISE_MARKS) or any(
        mark in text for mark in WEAK_PROMISE_MARKS
    )
    has_hard_promise = any(mark in text for mark in PROMISE_MARKS)
    has_pending = any(mark in text for mark in PENDING_MARKS)
    has_event = any(mark in text for mark in EVENT_MARKS)
    due = _due_ts(text, now_value)

    if postponed:
        # A future time mark alone is not a commitment once the sentence
        # defers ("明天再说吧"); only explicit promises / pending cues count.
        open_loop = has_hard_promise or (has_pending and (has_promise or has_event))
    else:
        open_loop = (
            bool(due)
            or (has_pending and (has_promise or has_event))
            or has_hard_promise
            or (has_promise and (has_event or any(m in text for m in _NEAR_DAYS_KEYS)))
        )
    if not open_loop:
        return {}
    if has_promise:
        reason = "用户的约定或承诺，适合适时跟进"
    elif has_pending:
        reason = "提及了尚未完成的事，可关心进展"
    else:
        reason = "含近期时间点，到期后值得问结果"
    return {
        "open_loop": 1,
        "promise": 1 if has_promise else 0,
        "due_ts": due,
        "open_loop_reason": reason,
    }
