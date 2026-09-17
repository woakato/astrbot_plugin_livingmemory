"""局面线索（spec §0 数据分诊）：只用于改写本轮检索词 / 生成氛围行，不留档。

设计目标（spec §0 分诊表）：
    局面线索（话题/心情/精力）→ 不留档，用完即弃，只拿来改写本轮检索词。

两路落点不同，原因见 situation 模块 docstring：
- 话题类落召回链（PC 调 compose_context 时不传事件，桥取不到）；
- 心情/精力落桥内（MC 在注入组装阶段消费它们决定"怎么提起"记忆）。
"""

from pathlib import Path
import types

import pytest

from astrbot_plugin_livingmemory.core.companion.bridge import CompanionBridge
from astrbot_plugin_livingmemory.core.companion.situation import (
    SITUATION_ATTR,
    atmosphere_lines,
    expand_query,
    mood_and_energy,
    read_situation,
    topic_terms,
)
from tests.test_companion_bridge import _FakePlugin


# ----------------------------------------------------------------------
# 读取：属性优先，get_extra 兜底
# ----------------------------------------------------------------------


class _EventWithAttr:
    def __init__(self, payload):
        setattr(self, SITUATION_ATTR, payload)


class _EventWithExtra:
    def __init__(self, payload):
        self._extra = payload

    def get_extra(self, key):
        return self._extra if key == SITUATION_ATTR else None


def test_read_situation_prefers_direct_attribute():
    payload = {"topic": "面试", "keywords": ["求职"]}
    assert read_situation(_EventWithAttr(payload)) == payload


def test_read_situation_falls_back_to_extra_channel():
    payload = {"topic": "搬家"}
    assert read_situation(_EventWithExtra(payload)) == payload


def test_read_situation_absent_returns_empty():
    class _Bare:
        pass

    assert read_situation(_Bare()) == {}
    assert read_situation(None) == {}


# ----------------------------------------------------------------------
# 字段容错：PC 对 entities/facts/keywords 走 list 合并，其余键是标量覆盖
# ----------------------------------------------------------------------


def test_topic_terms_accepts_list_scalar_and_dict_shapes():
    payload = {
        "topic": "面试",
        "entities": ["我", "面试官"],          # list（PC 合并语义）
        "facts": "明天上午十点",               # 标量（PC 单值覆盖语义）
        "keywords": {"a": "求职", "b": "简历"},  # dict（防御性）
    }
    terms = topic_terms(payload)
    assert terms[0] == "面试"
    assert "我" in terms and "面试官" in terms
    assert "明天上午十点" in terms
    assert "求职" in terms and "简历" in terms


def test_expand_query_handles_scalar_entities_without_crashing():
    payload = {"topic": "面试", "entities": "面试官"}
    query, status = expand_query("面试怎么样了", payload, message_query="面试怎么样了")
    assert status == "guarded_overlap"
    assert "面试官" in query


# ----------------------------------------------------------------------
# overlap 守卫：无重叠必须丢弃，防旧话头把本轮检索带偏
# ----------------------------------------------------------------------


def test_expand_query_uses_terms_when_they_overlap_the_message():
    payload = {"topic": "面试", "keywords": ["求职"]}
    query, status = expand_query("结果如何", payload, message_query="我面试完了")
    assert status == "guarded_overlap"
    assert "面试" in query and "结果如何" in query


def test_expand_query_drops_terms_when_no_overlap():
    payload = {"topic": "面试", "keywords": ["求职"]}
    query, status = expand_query("今天天气", payload, message_query="今天天气")
    assert status == "guarded_no_overlap"
    assert query == "今天天气"
    assert "面试" not in query


def test_expand_query_without_clues_returns_query_untouched():
    query, status = expand_query("随便聊聊", {}, message_query="随便聊聊")
    assert status == "no_clues"
    assert query == "随便聊聊"


def test_expand_query_preserves_existing_context_expansion():
    # 召回链可能已把最近两轮上下文拼进来了，扩写必须保留它。
    payload = {"topic": "面试"}
    query, status = expand_query("历史上下文 我面试完了", payload, message_query="我面试完了")
    assert status == "guarded_overlap"
    assert "历史上下文" in query


# ----------------------------------------------------------------------
# 心情 / 精力：氛围行（MC injection._atmosphere_hint 的桶）
# ----------------------------------------------------------------------


def test_atmosphere_lines_mood_buckets():
    assert atmosphere_lines("有点累", 0.0)
    assert any("疲态" in line for line in atmosphere_lines("疲惫", 0.0))
    assert any("心情不错" in line for line in atmosphere_lines("开心", 0.0))
    assert any("情绪偏低" in line for line in atmosphere_lines("难过", 0.0))
    assert any("不太稳定" in line for line in atmosphere_lines("很烦", 0.0))


def test_atmosphere_lines_neutral_mood_needs_no_hint():
    assert atmosphere_lines("平静", 0.0) == []
    assert atmosphere_lines("", 0.0) == []


def test_atmosphere_lines_energy_thresholds():
    assert any("很低" in line for line in atmosphere_lines("", 10.0))
    assert any("偏低" in line for line in atmosphere_lines("", 40.0))
    assert atmosphere_lines("", 80.0) == []


def test_mood_and_energy_reads_payload_keys():
    assert mood_and_energy({"mood_bias": "平静", "energy": 42}) == ("平静", 42.0)
    assert mood_and_energy({}) == ("", 0.0)
    assert mood_and_energy({"energy": "not-a-number"}) == ("", 0.0)


# ----------------------------------------------------------------------
# 端到端：桥内氛围行确实进了注入包（修复前 mood 是死参数、energy 零引用）
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compose_context_injects_atmosphere_line():
    plugin = _FakePlugin(enabled=True)
    bridge = CompanionBridge(plugin)
    text = await bridge.compose_context(
        query="今天怎么样",
        session_context={"bot_id": "1", "session_id": "aiocqhttp:FriendUin:u1"},
        companion_bot_mood="有点累",
        companion_bot_energy=20.0,
    )
    assert "疲态" in text
    assert "心理能量很低" in text


@pytest.mark.asyncio
async def test_compose_context_without_clues_has_no_atmosphere_line():
    plugin = _FakePlugin(enabled=True)
    bridge = CompanionBridge(plugin)
    text = await bridge.compose_context(
        query="今天怎么样",
        session_context={"bot_id": "1", "session_id": "aiocqhttp:FriendUin:u1"},
    )
    assert "疲态" not in text
    assert "心理能量" not in text


# ----------------------------------------------------------------------
# 不留档：线索用完后任何存储里都不该出现它们（端到端，真引擎 + 真 store）
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_situation_clues_are_not_persisted(tmp_path: Path):
    """线索用完即弃：消费过线索后，两张陪伴表都不该多出行。"""
    import aiosqlite

    from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine
    from tests.test_graph_memory import _FakeFaissDB

    engine = MemoryEngine(
        db_path=str(tmp_path / "m.db"), faiss_db=_FakeFaissDB(), config={}
    )
    await engine.initialize()
    try:
        store = getattr(engine, "companion_store", None)
        assert store is not None, "接线缺失：MemoryEngine 必须挂着 companion_store"
        store_path = store.db_path

        async with aiosqlite.connect(store_path) as db:
            for table in ("companion_events", "emotion_ledger"):
                await db.execute(f"DELETE FROM {table}")
            await db.commit()

        plugin = _FakePlugin(enabled=True)
        plugin.initializer = types.SimpleNamespace(memory_engine=engine)
        bridge = CompanionBridge(plugin)

        event = _EventWithAttr(
            {"topic": "面试", "keywords": ["求职"], "mood_bias": "有点累", "energy": 15}
        )
        payload = read_situation(event)
        query, status = expand_query("面试结果", payload, message_query="面试结果")
        assert status == "guarded_overlap"
        await bridge.compose_context(
            query=query,
            session_context={"bot_id": "1", "session_id": "aiocqsub:u1"},
            companion_bot_mood="有点累",
            companion_bot_energy=15.0,
        )

        async with aiosqlite.connect(store_path) as db:
            async with db.execute("SELECT COUNT(*) FROM companion_events") as cur:
                events = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM emotion_ledger") as cur:
                ledger = (await cur.fetchone())[0]
        assert events == 0, "局面线索不得落进 companion_events"
        assert ledger == 0, "局面线索不得落进 emotion_ledger"
    finally:
        await engine.close()
