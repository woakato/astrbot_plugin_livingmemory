"""数据分诊（spec §0）回归测试：说说/创作进总结链、日记每周蒸馏、日程永不进总结。

分诊表：
    互动流水（聊天、主动消息、说说、创作）→ 时间线→总结→原子
    日程                                → 单独索引，永远不进总结
    日记                                → 单存原始条目，每周蒸馏"有分量"的进长期库
    情绪                                → 签收账本（另有 test_companion_bridge 覆盖）

注意：日程与日记在库里共用 ``kind='archive'``，只有
``metadata.archive_memory_type`` 能区分——本文件锁住这一点。
"""

import types
from pathlib import Path

import pytest

from astrbot_plugin_livingmemory.core.companion.bridge import (
    CompanionBridge,
    _SUMMARY_BOUND_TYPES,
)
from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine
from astrbot_plugin_livingmemory.core.schedulers.companion_maintenance_scheduler import (
    MIN_DIARY_DISTILL_CHARS,
    CompanionMaintenanceScheduler,
)
from astrbot_plugin_livingmemory.core.retrieval.hybrid_retriever import HybridResult
from astrbot_plugin_livingmemory.storage.companion_store import (
    DIARY_ARCHIVE_MEMORY_TYPES,
)
from tests.test_companion_bridge import _FakeConfig
from tests.test_graph_memory import _FakeFaissDB


class _FakeConversationManager:
    """Minimal timeline double: the bridge reads rows by attribute."""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def get_messages(self, session_id: str, limit: int = 20):
        rows = [m for m in self.messages if m["session_id"] == session_id]
        return [
            types.SimpleNamespace(role=m["role"], content=m["content"])
            for m in rows[-limit:]
        ]

    async def add_message(self, session_id: str, role: str, content: str, **kwargs):
        self.messages.append(
            {"session_id": session_id, "role": role, "content": content, **kwargs}
        )
        return len(self.messages)


class _TriagePlugin:
    """Host double that runs the bridge's fire-and-forget coroutines inline."""

    def __init__(self, *, engine, manager, enabled: bool = True) -> None:
        self.config_manager = _FakeConfig({"companion_bridge.enabled": enabled})
        self.initializer = types.SimpleNamespace(
            memory_engine=engine, conversation_manager=manager
        )
        self._bot_id = "aiocqhttp:10001"
        self.pending: list = []

    @property
    def bot_id(self) -> str:
        return self._bot_id

    def spawn_companion_background(self, coro):
        # 收集而不关闭：测试需要真正执行写入。
        self.pending.append(coro)

    async def drain(self) -> None:
        while self.pending:
            await self.pending.pop(0)


async def _make_env(tmp_path):
    engine = MemoryEngine(
        db_path=str(tmp_path / "m.db"), faiss_db=_FakeFaissDB(), config={}
    )
    await engine.initialize()
    manager = _FakeConversationManager()
    plugin = _TriagePlugin(engine=engine, manager=manager)
    return engine, manager, plugin, CompanionBridge(plugin)


# ----------------------------------------------------------------------
# 说说 / 创作 → 时间线（→总结→原子）
# ----------------------------------------------------------------------


def test_summary_bound_types_are_exactly_qzone_and_creative():
    assert _SUMMARY_BOUND_TYPES == frozenset({"qzone_action", "creative_work"})


@pytest.mark.asyncio
async def test_qzone_action_is_mirrored_onto_timeline(tmp_path: Path):
    engine, manager, plugin, bridge = await _make_env(tmp_path)
    try:
        await bridge.record_qzone_action(
            content="今天发了条说说：展览很好看",
            session_id="private_companion:qzone",
            platform="aiocqhttp",
        )
        await plugin.drain()
        roles = [(m["role"], m["content"]) for m in manager.messages]
        assert ("assistant", "今天发了条说说：展览很好看") in roles
        assert all(m["session_id"] == "private_companion:qzone" for m in manager.messages)
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_creative_work_is_mirrored_onto_timeline(tmp_path: Path):
    engine, manager, plugin, bridge = await _make_env(tmp_path)
    try:
        await bridge.record_creative_work(
            content="Bot 私下创作项目《雨》有新进展。", session_id="private_companion:creative"
        )
        await plugin.drain()
        assert any(m["role"] == "assistant" for m in manager.messages)
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_timeline_mirror_is_idempotent(tmp_path: Path):
    engine, manager, plugin, bridge = await _make_env(tmp_path)
    try:
        for _ in range(2):
            await bridge.record_qzone_action(
                content="重复投递的说说", session_id="private_companion:qzone"
            )
            await plugin.drain()
        assert len([m for m in manager.messages if m["role"] == "assistant"]) == 1
    finally:
        await engine.close()


# ----------------------------------------------------------------------
# 日程 / 日记：绝不进时间线
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_fragment_never_reaches_timeline(tmp_path: Path):
    engine, manager, plugin, bridge = await _make_env(tmp_path)
    try:
        await bridge.record_schedule_fragment(
            content="上午十点开会", session_id="bot_personal"
        )
        await plugin.drain()
        assert manager.messages == [], "日程不进总结链（spec §0）"
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_diary_archive_path_never_invokes_timeline_mirror(tmp_path: Path):
    """归档路径（日记/日程）完全不经 _record，因此永不触发时间线镜像。"""
    engine, manager, _plugin, bridge = await _make_env(tmp_path)
    mirror_calls: list[dict] = []
    original = CompanionBridge._mirror_summary_bound

    async def spy(self, **kwargs):
        mirror_calls.append(kwargs)
        return await original(self, **kwargs)

    CompanionBridge._mirror_summary_bound = spy
    try:
        await bridge.record_bot_personal_archive(
            {
                "record_id": "r-diary",
                "memory_type": "bot_daily_diary",
                "summary": "今天把阳台的花都换了盆，折腾了一下午。",
                "payload": {"date": "2026-09-17"},
                "idempotency_key": "diary:2026-09-17",
                "version": 1,
                "occurred_at": "2026-09-17T20:00:00+08:00",
            },
            producer_capability=object(),
        )
        await _plugin.drain()
        assert mirror_calls == [], "归档一律走窗口索引，不经时间线（spec §0）"
        assert manager.messages == []
    finally:
        CompanionBridge._mirror_summary_bound = original
        await engine.close()


# ----------------------------------------------------------------------
# 日记蒸馏：只认 bot_daily_diary、只升"有分量"的、日程绝不碰
# ----------------------------------------------------------------------


async def _seed_archive(store, *, key: str, memory_type: str, content: str, day: str):
    return await store.upsert_archive_event(
        event_key=key,
        version=1,
        fingerprint="fp-" + key,
        kind="archive",
        content=content,
        bot_id="10001",
        scope="private",
        session_id="bot_personal",
        occurred_at=f"{day}T20:00:00+08:00",
        importance=0.55,
        metadata={
            "archive_version": 1,
            "payload_fingerprint": "fp-" + key,
            "idempotency_key": key,
            "archive_memory_type": memory_type,
            "date": day,
        },
    )


def _spy_add_memory(engine):
    created: list[dict] = []
    original = engine.add_memory

    async def spy(**kwargs):
        created.append(kwargs)
        return await original(**kwargs)

    engine.add_memory = spy
    return created


@pytest.mark.asyncio
async def test_diary_distillation_promotes_only_substantial_entries(tmp_path: Path):
    engine, _manager, _plugin, _bridge = await _make_env(tmp_path)
    try:
        store = engine.companion_store
        long_diary = (
            "今天和很久没联系的朋友通了电话，聊到当年一起骑车去海边的那次，说着说着就笑了。" * 3
        )
        short_diary = "今天还行。"
        # 边界显式化：门槛就是 MIN_DIARY_DISTILL_CHARS，测试跟着它走。
        assert len(long_diary) >= MIN_DIARY_DISTILL_CHARS
        assert len(short_diary) < MIN_DIARY_DISTILL_CHARS
        await _seed_archive(
            store, key="k-long", memory_type="bot_daily_diary", content=long_diary, day="2026-09-10"
        )
        await _seed_archive(
            store, key="k-short", memory_type="bot_daily_diary", content=short_diary, day="2026-09-11"
        )
        created = _spy_add_memory(engine)

        sched = CompanionMaintenanceScheduler(memory_engine=engine)
        distilled = await sched._distill_diaries(store)

        assert distilled == 1, "只有内容够长的日记才晋升"
        assert len(created) == 1
        meta = created[0]["metadata"]
        assert meta["visibility"] == "bot_self", "晋升条目必须带 bot_self 标记"
        assert meta["source_event_key"] == "k-long"
        assert long_diary[:20] in created[0]["content"]

        # 短条目被标记跳过，不会每周反复扫；长的已标记，不重复蒸馏。
        again = await sched._distill_diaries(store)
        assert again == 0

        rows = await store.list_undistilled_diaries(
            memory_types=tuple(sorted(DIARY_ARCHIVE_MEMORY_TYPES))
        )
        assert rows == []
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_schedule_archives_are_never_distilled(tmp_path: Path):
    engine, _manager, _plugin, _bridge = await _make_env(tmp_path)
    try:
        store = engine.companion_store
        plan = "上午十点开项目会；下午三点和设计对稿；晚上八点陪家人吃饭。" * 2
        await _seed_archive(
            store, key="k-plan", memory_type="bot_schedule_plan", content=plan, day="2026-09-12"
        )
        await _seed_archive(
            store, key="k-snap", memory_type="bot_window_snapshot", content=plan, day="2026-09-12"
        )
        created = _spy_add_memory(engine)

        sched = CompanionMaintenanceScheduler(memory_engine=engine)
        distilled = await sched._distill_diaries(store)

        assert distilled == 0, "日程永远不进总结（spec §0）"
        assert created == []
        # 日程行也不该被标记，保持原样。
        rows = await store.list_undistilled_diaries(memory_types=("bot_schedule_plan",))
        assert len(rows) == 1, "日程不属于日记蒸馏范围，不应被标记处理"
    finally:
        await engine.close()


# ----------------------------------------------------------------------
# 读侧隔离：visibility=bot_self 不参与常规语义召回
# ----------------------------------------------------------------------


def _result(doc_id: int, *, visibility: str = "", importance: float = 0.8) -> HybridResult:
    return HybridResult(
        doc_id=doc_id,
        final_score=0.9,
        rrf_score=0.9,
        bm25_score=None,
        vector_score=0.9,
        content=f"memory-{doc_id}",
        metadata={
            "status": "active",
            "importance": importance,
            "memory_type": "EPISODIC",
            **({"visibility": visibility} if visibility else {}),
        },
        score_breakdown={},
    )


@pytest.mark.asyncio
async def test_bot_self_memories_are_excluded_from_recall(tmp_path: Path):
    engine, _manager, _plugin, _bridge = await _make_env(tmp_path)
    try:
        results = [_result(1), _result(2, visibility="bot_self"), _result(3)]
        kept = engine._filter_by_retrieval_policy(results)
        ids = [r.doc_id for r in kept]
        assert 2 not in ids, "bot 自述内容不得进入常规召回"
        assert ids == [1, 3]
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_visibility_matching_is_case_insensitive(tmp_path: Path):
    engine, _manager, _plugin, _bridge = await _make_env(tmp_path)
    try:
        kept = engine._filter_by_retrieval_policy([_result(7, visibility="BOT_SELF")])
        assert kept == []
    finally:
        await engine.close()
