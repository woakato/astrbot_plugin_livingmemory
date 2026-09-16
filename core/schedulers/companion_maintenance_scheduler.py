"""Companion maintenance scheduler: weekly emotion distillation + purges.

Runs daily at a fixed hour (mirrors DecayScheduler's loop style):

- purge expired/acked emotion-ledger rows;
- distill "weighty" acked emotion events into durable long-term memories
  (rule-built sentence from event fields — deliberately no LLM call), via
  add_memory so BM25/FAISS/graph all index them properly;
- age out stale open loops that never received a resolution (older than
  OPEN_LOOP_MAX_AGE_DAYS without a future due_ts are no longer worth
  asking about).

Spec: docs/companion-fork-spec.md section 10 (Phase 4).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from astrbot.api import logger

if TYPE_CHECKING:
    from ..managers.memory_engine import MemoryEngine

OPEN_LOOP_MAX_AGE_DAYS = 45
_MIN_DISTILL_INTENSITY = 40.0

_EVENT_PHRASES = {
    "scar_touched": "被提起旧伤，留下了痕迹",
    "warm_memory": "一段温暖的共同记忆",
    "vulnerable_resonance": "彼此袒露过柔软",
    "hurt": "有过一次不愉快",
    "boundary_violation": "越界行为被认真记下，边界还在",
    "comfort": "被安慰过",
    "praise": "被认真夸奖过",
    "intimacy": "关系更近了一步",
}


class CompanionMaintenanceScheduler:
    """Periodic companion-data housekeeping (distill/purge/age-out)."""

    def __init__(
        self,
        *,
        memory_engine: MemoryEngine,
        check_hour: int = 4,
        check_minute: int = 10,
    ):
        """
        Args:
            memory_engine: engine exposing companion_store + add_memory.
            check_hour/check_minute: daily run time.
        """
        self.memory_engine = memory_engine
        self.check_hour = int(check_hour)
        self.check_minute = int(check_minute)
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_run_date: str = ""

    async def start(self) -> None:
        if self._running:
            logger.warning("[陪伴维护] 调度器已在运行")
            return
        self._running = True
        await self.run_once("startup")
        self._task = asyncio.create_task(self._scheduler_loop())
        logger.info(
            f"[陪伴维护] 调度器已启动 (执行时间: {self.check_hour:02d}:{self.check_minute:02d})"
        )

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("[陪伴维护] 调度器已停止")

    async def _scheduler_loop(self) -> None:
        while self._running:
            try:
                now = datetime.now()
                target = now.replace(
                    hour=self.check_hour,
                    minute=self.check_minute,
                    second=0,
                    microsecond=0,
                )
                if target <= now:
                    target += timedelta(days=1)
                await asyncio.sleep((target - now).total_seconds())
                if self._running:
                    await self.run_once("scheduled")
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.error(f"[陪伴维护] 循环异常: {e}", exc_info=True)
                await asyncio.sleep(3600)

    async def run_once(self, reason: str = "manual") -> dict[str, int]:
        """Execute one maintenance pass. Returns per-step counters."""
        today = datetime.now().strftime("%Y-%m-%d")
        if self._last_run_date == today and reason == "scheduled":
            return {"skipped": 1}
        store = getattr(self.memory_engine, "companion_store", None)
        if store is None:
            return {"skipped": 1}

        result = {"purged": 0, "distilled": 0, "aged_out": 0}
        # Distill before purge: purge_expired removes acked rows past the
        # retention window, which are exactly what the distiller reads.
        try:
            result["distilled"] = await self._distill_emotions(store)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[陪伴维护] 情绪蒸馏失败: {exc}")

        try:
            result["purged"] = await store.purge_expired()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[陪伴维护] 账本清理失败: {exc}")

        try:
            result["aged_out"] = await self._age_out_open_loops()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[陪伴维护] 未闭环老化失败: {exc}")

        if any(result.values()):
            logger.info(f"[陪伴维护] {reason}: {result}")
        self._last_run_date = today
        return result

    async def _distill_emotions(self, store) -> int:
        """Turn weighty acked afterglows into durable memories.

        The distilled line names the event type and the person involved so
        the long-term layer remembers "there was something" without storing
        the raw exchange again (privacy_level=redacted is preserved).
        """
        rows = await store.list_undistilled_acked(limit=50)
        if not rows:
            return 0
        distilled = 0
        processed_ids: list[str] = []
        for row in rows:
            event_id = str(row.get("event_id") or "")
            try:
                intensity = float(row.get("intensity") or 0)
                event_type = str(row.get("event_type") or "")
                if intensity < _MIN_DISTILL_INTENSITY:
                    processed_ids.append(event_id)
                    continue
                phrase = _EVENT_PHRASES.get(event_type)
                if not phrase:
                    processed_ids.append(event_id)
                    continue
                session_id = str(row.get("session_id") or "")
                content = f"（情绪记忆）{phrase}"
                await self.memory_engine.add_memory(
                    content=content,
                    session_id=session_id or None,
                    importance=0.62,
                    metadata={
                        "memory_type": "EPISODIC",
                        "emotion_distilled": event_type,
                        "source_emotion_ids": [event_id],
                        "bot_self": str(row.get("scope") or "") == "private",
                        "create_time": float(row.get("occurred_at") or time.time()),
                    },
                    preserve_create_time=True,
                )
                distilled += 1
                processed_ids.append(event_id)
            except Exception as exc:  # noqa: BLE001
                # 写入失败不标记 distilled：留给下次重试（账本 7 天保留期内）
                logger.warning(f"[陪伴维护] 单条蒸馏失败({event_id}): {exc}")
        if processed_ids:
            await store.mark_distilled(processed_ids)
        return distilled

    async def _age_out_open_loops(self) -> int:
        """Close loops too old to still matter (no future due date pending)."""
        cutoff = time.time() - OPEN_LOOP_MAX_AGE_DAYS * 86400
        resolver = getattr(self.memory_engine, "close_stale_open_loops", None)
        if resolver is None:
            return 0
        return await resolver(before_create_time=cutoff)
