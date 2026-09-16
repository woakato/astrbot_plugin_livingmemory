"""Tests for the companion bridge surface (fork of LivingMemory presenting an
MC-compatible face to astrbot_plugin_private_companion).

Regression coverage for the audit-confirmed bug classes:
- core-memory schema/doc_id and fail-closed scope residency
- composer hard budget accounting (no overrun, discard-protocol sentence kept)
- archive version ladder (dedup / supersede / stale / conflict) via stable keys
- emotion ledger delivery state machine (no reset on identical re-record)
- peek_pending domain fail-closed
- bot_id canonicalization (raw self_id vs platform-prefixed)
- bridge enabled-gate (no handshake / no writes when disabled)
- open-loop tagging (year rollover, postponed sentences, reminder-only text)
- loop resolution must not mis-close on generic terms
"""

from pathlib import Path

import pytest

from astrbot_plugin_livingmemory.core.companion.bridge import (
    CompanionBridge,
    _canonical_bot_id,
)
from astrbot_plugin_livingmemory.core.companion.composer import (
    EMPTY_RESULT_SENTENCE,
    MemoryItem,
    PackageComposer,
)
from astrbot_plugin_livingmemory.core.companion.open_loop import (
    _due_ts,
    analyze_open_loop,
)
from astrbot_plugin_livingmemory.storage.companion_store import (
    CompanionStore,
    event_date_of,
    today_date,
)


class _FakeConfig:
    def __init__(self, values: dict | None = None):
        self._values = values or {}

    def get(self, key: str, default=None):
        return self._values.get(key, default)


class _FakePlugin:
    """Minimal host surface the bridge touches: config + spawn + identity."""

    def __init__(self, *, enabled: bool = True, bot_id: str = "aiocqhttp:10001"):
        self.config_manager = _FakeConfig({"companion_bridge.enabled": enabled})
        self.initializer = None  # engine stays absent on purpose
        self._bot_id = bot_id
        self.spawned = []

    @property
    def bot_id(self) -> str:
        return self._bot_id

    def spawn_companion_background(self, coro):
        coro.close()  # bridge hands us coroutines; close unstarted ones
        self.spawned.append(True)


# ----------------------------------------------------------------------
# core memory (P1-1 / P1-3)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_core_memory_sets_doc_id_and_loads(tmp_path: Path):
    from tests.test_graph_memory import _FakeFaissDB

    from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine

    engine = MemoryEngine(
        db_path=str(tmp_path / "m.db"), faiss_db=_FakeFaissDB(), config={}
    )
    await engine.initialize()
    try:
        mid = await engine.add_core_memory(
            "永远不要替用户做决定", label="boundary", kind="boundary", priority=90
        )
        assert mid > 0
        cursor = await engine.db_connection.execute(
            "SELECT doc_id FROM documents WHERE id = ?", (mid,)
        )
        row = await cursor.fetchone()
        assert row is not None and str(row[0]).startswith("core-")

        blocks = await engine.load_core_memories(session_id="s1")
        assert any(b["memory_id"] == mid for b in blocks)

        assert await engine.delete_core_memory(mid) is True
        assert not any(
            b["memory_id"] == mid
            for b in await engine.load_core_memories(session_id="s1")
        )
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_load_core_memories_fail_closed_on_missing_session(tmp_path: Path):
    from tests.test_graph_memory import _FakeFaissDB

    from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine

    engine = MemoryEngine(
        db_path=str(tmp_path / "m.db"), faiss_db=_FakeFaissDB(), config={}
    )
    await engine.initialize()
    try:
        await engine.add_core_memory(
            "只属于A会话的规则", scope="session", session_id="aiocqhttp:Group_A"
        )
        await engine.add_core_memory("全局规则", scope="global")
        # A turn that cannot name its session must still see global blocks,
        # never session-scoped ones.
        blocks = await engine.load_core_memories(session_id=None)
        texts = [b["text"] for b in blocks]
        assert "全局规则" in texts
        assert "只属于A会话的规则" not in texts
        # Correct session sees its own block only in addition to global.
        blocks = await engine.load_core_memories(session_id="aiocqhttp:Group_A")
        assert len(blocks) == 2
    finally:
        await engine.close()


# ----------------------------------------------------------------------
# composer budget (P1-5)
# ----------------------------------------------------------------------


def _stress_kwargs():
    items = [
        MemoryItem(
            text="记" * 300,
            source="EPISODIC",
            time_label="2026-09-01",
            weight=1.0 - i * 0.01,
        )
        for i in range(10)
    ]
    return dict(
        core_blocks=[{"text": "核" * 200, "label": "r", "kind": "rule", "priority": 90}]
        * 4,
        one_line_slots=[
            ("emotional_hint", "余" * 40),
            ("relationship_hint", "关" * 40),
            ("self_timeline", "今" * 40),
        ],
        open_loops=[MemoryItem(text="闭" * 100) for _ in range(3)],
        retrieval_items=items,
        current_message="用" * 280,
        window_label="窗" * 120,
    )


@pytest.mark.parametrize("budget", [300, 400, 600, 800, 1000, 1500])
def test_composer_never_overruns_budget(budget: int):
    pkg = PackageComposer(budget_chars=budget).compose(**_stress_kwargs())
    assert pkg.used_chars <= budget, f"budget {budget} overrun: {pkg.used_chars}"


def test_composer_empty_package_keeps_discard_protocol():
    """An empty search result must ship exactly the sentence PC drops on."""
    composer = PackageComposer(budget_chars=1000)
    pkg = composer.compose(current_message="你好", window_label="会话类型：私聊")
    wrapped = composer.wrap_for_bridge(pkg)
    bullets = [line for line in pkg.body.splitlines() if line.startswith("- ")]
    assert len(bullets) == 1 and EMPTY_RESULT_SENTENCE in bullets[0]
    # PC rule: sentence present and at most one "\n- " occurrence => discard.
    assert wrapped.count("\n- ") <= 1


def test_composer_suppress_empty_hint():
    composer = PackageComposer(budget_chars=1000)
    pkg = composer.compose(retrieval_items=[], suppress_empty_hint=True)
    assert EMPTY_RESULT_SENTENCE not in pkg.body


def test_composer_budget_pressure_drops_low_priority():
    """Under a tiny budget the retrieval section reports drops, not overflow."""
    items = [MemoryItem(text="长" * 250, weight=1.0 - i * 0.01) for i in range(8)]
    pkg = PackageComposer(budget_chars=600).compose(retrieval_items=items)
    assert pkg.used_chars <= 600
    assert pkg.dropped_count >= 1


# ----------------------------------------------------------------------
# archive version ladder (BUG#1)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stable_key_is_process_independent():
    k1 = CompanionStore.stable_key("lmce_arch_", "idem-abc")
    assert k1.startswith("lmce_arch_")
    assert len(k1) == len("lmce_arch_") + 24
    # sha256 hex: deterministic across runs (unlike built-in hash()).
    assert k1 == CompanionStore.stable_key("lmce_arch_", "idem-abc")
    assert k1 != CompanionStore.stable_key("lmce_arch_", "idem-abd")


@pytest.mark.asyncio
async def test_archive_version_ladder(tmp_path: Path):
    store = CompanionStore(str(tmp_path / "c.db"))
    await store.initialize()
    fields = dict(
        kind="archive",
        content="今天的日程",
        bot_id="10001",
        persona_id="p",
        scope="private",
        session_id="bot_personal",
        window_slug="morning",
        occurred_at=1_750_000_000.0,
        importance=0.55,
    )
    key = store.stable_key("lmce_arch_", "idem-1")
    fp_v1 = store.archive_payload_fingerprint('{"a":1}')
    fp_v2 = store.archive_payload_fingerprint('{"a":2}')

    first = await store.upsert_archive_event(
        event_key=key,
        version=1,
        fingerprint=fp_v1,
        metadata={"archive_version": 1, "payload_fingerprint": fp_v1},
        **fields,
    )
    assert first["state"] == "sent"

    same = await store.upsert_archive_event(
        event_key=key,
        version=1,
        fingerprint=fp_v1,
        metadata={"archive_version": 1, "payload_fingerprint": fp_v1},
        **fields,
    )
    assert same["state"] == "deduplicated"

    conflict = await store.upsert_archive_event(
        event_key=key,
        version=1,
        fingerprint=fp_v2,
        metadata={"archive_version": 1, "payload_fingerprint": fp_v2},
        **fields,
    )
    assert conflict["state"] == "version_conflict"

    stale = await store.upsert_archive_event(
        event_key=key,
        version=1,
        fingerprint=fp_v1,
        metadata={"archive_version": 1, "payload_fingerprint": fp_v1},
        **fields,
    )
    # Row was superseded below; before that, old==1, new==1 dedup. Now push v2.
    assert stale["state"] in {"deduplicated", "stale_version"}

    newer = await store.upsert_archive_event(
        event_key=key,
        version=2,
        fingerprint=fp_v2,
        metadata={"archive_version": 2, "payload_fingerprint": fp_v2},
        **fields,
    )
    assert newer["state"] == "sent"

    stale_after = await store.upsert_archive_event(
        event_key=key,
        version=1,
        fingerprint=fp_v1,
        metadata={"archive_version": 1, "payload_fingerprint": fp_v1},
        **fields,
    )
    assert stale_after["state"] == "stale_version"
    assert stale_after["version"] == 2


# ----------------------------------------------------------------------
# emotion delivery state machine (P1-7)
# ----------------------------------------------------------------------


def _emotion_event(**over):
    import time as _time

    base = {
        "event_id": "emo_test_1",
        "revision": 1,
        "event_type": "warm_memory",
        "intensity": 60.0,
        "confidence": 0.8,
        "occurred_at": _time.time(),
        "payload_hash": "ph-abc",
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_record_emotion_identical_replay_keeps_state(tmp_path: Path):
    store = CompanionStore(str(tmp_path / "c.db"))
    await store.initialize()
    ctx = {
        "bot_id": "10001",
        "scope": "private",
        "platform": "aiocqhttp",
        "user_id": "u1",
        "session_id": "aiocqhttp:FriendUin:u1",
    }
    stored = await store.record_emotion(_emotion_event(), context=ctx)
    assert stored["delivery_state"] == "pending"

    # Delivery consumes it once...
    out = await store.list_deliverable(
        scope="private",
        platform="aiocqhttp",
        user_id="u1",
        session_id="aiocqhttp:FriendUin:u1",
    )
    assert len(out) == 1
    # ...and an identical retry must NOT restart delivery.
    again = await store.record_emotion(_emotion_event(), context=ctx)
    assert again["delivery_state"] == "delivered"

    rest = await store.list_deliverable(
        scope="private",
        platform="aiocqhttp",
        user_id="u1",
        session_id="aiocqhttp:FriendUin:u1",
    )
    # delivered rows remain deliverable until acked (re-delivery on poll),
    # but revision must not have been bumped by the replay.
    assert all(e["revision"] == 1 for e in rest)

    acked = await store.ack(
        [{"event_id": "emo_test_1", "revision": 1}],
        consumer_id="private_companion.daily_state",
    )
    assert acked == 1
    assert (
        await store.list_deliverable(
            scope="private",
            platform="aiocqhttp",
            user_id="u1",
            session_id="aiocqhttp:FriendUin:u1",
        )
        == []
    )


@pytest.mark.asyncio
async def test_record_emotion_revision_bump_restarts_delivery(tmp_path: Path):
    store = CompanionStore(str(tmp_path / "c.db"))
    await store.initialize()
    ctx = {
        "bot_id": "10001",
        "scope": "private",
        "platform": "aiocqhttp",
        "user_id": "u1",
        "session_id": "aiocqhttp:FriendUin:u1",
    }
    await store.record_emotion(_emotion_event(), context=ctx)
    await store.list_deliverable(
        scope="private",
        platform="aiocqhttp",
        user_id="u1",
        session_id="aiocqhttp:FriendUin:u1",
    )
    bumped = await store.record_emotion(
        _emotion_event(payload_hash="ph-xyz", revision=2), context=ctx
    )
    assert bumped["delivery_state"] == "pending"
    assert bumped["revision"] == 2


# ----------------------------------------------------------------------
# peek_pending fail-closed (P1-8)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_peek_pending_requires_domain(tmp_path: Path):
    store = CompanionStore(str(tmp_path / "c.db"))
    await store.initialize()
    ctx = {
        "bot_id": "10001",
        "scope": "private",
        "platform": "aiocqhttp",
        "user_id": "u1",
        "session_id": "aiocqhttp:FriendUin:u1",
    }
    await store.record_emotion(_emotion_event(), context=ctx)
    # No session, no cross-window identity => refuse (other sessions leak).
    assert await store.peek_pending(scope="private", session_id="") == []
    # Correct session sees its own row.
    rows = await store.peek_pending(
        scope="private", session_id="aiocqhttp:FriendUin:u1"
    )
    assert len(rows) == 1


# ----------------------------------------------------------------------
# bot_id canonicalization (BUG#2) + Beijing date bucketing
# ----------------------------------------------------------------------


def test_canonical_bot_id_strips_platform_prefix():
    assert _canonical_bot_id("aiocqhttp:10001") == "10001"
    assert _canonical_bot_id("10001") == "10001"
    assert _canonical_bot_id("") == ""
    assert _canonical_bot_id(None) == ""


def test_event_date_uses_beijing_calendar():
    # 2026-09-16 23:30 Beijing == 15:30 UTC same day; storage day must match
    # the Beijing calendar used by today_date().
    ts = 1_757_985_000.0  # 2025-09-15 23:30 UTC == 2025-09-16 07:30 Beijing
    assert event_date_of(ts) == "2025-09-16"
    assert today_date() == event_date_of(__import__("time").time())


# ----------------------------------------------------------------------
# bridge enabled gate (BUG#4)
# ----------------------------------------------------------------------


def test_bridge_disabled_publishes_negative_probe():
    plugin = _FakePlugin(enabled=False)
    bridge = CompanionBridge(plugin)
    probe = bridge.probe_bot_personal_memory_capabilities()
    assert probe.get("available") is False
    assert bridge.bridge_lifecycle_status() == {"active": False}
    assert bridge.coordination_status().get("available") is False
    assert bridge.should_defer_private_companion_section("self_timeline") is False


def test_bridge_active_publishes_handshake_ready():
    plugin = _FakePlugin(enabled=True)
    bridge = CompanionBridge(plugin)
    probe = bridge.probe_bot_personal_memory_capabilities()
    assert probe["state"] == "ready" and probe["available"] is True
    assert probe["contract_fingerprint"] == "ecf1d69406a8445d"
    assert probe["contract_revision"] == 3
    assert isinstance(probe["windows"], list) and len(probe["windows"]) == 5
    assert len(probe["memory_types"]) == 12
    assert bridge.bridge_lifecycle_status() == {"active": True}


@pytest.mark.asyncio
async def test_disabled_bridge_refuses_writes():
    plugin = _FakePlugin(enabled=False)
    bridge = CompanionBridge(plugin)
    assert await bridge.record_proactive_message(content="早上好") == ""
    result = await bridge.record_bot_personal_archive(
        {
            "record_id": "r1",
            "memory_type": "bot_daily_diary",
            "payload": {},
            "idempotency_key": "k1",
            "version": 1,
        },
        producer_capability=object(),
    )
    assert result["state"] == "degraded"
    assert (
        await bridge.compose_context(query="日程", session_context={"bot_id": "1"})
        == ""
    )


# ----------------------------------------------------------------------
# open loops (P1-6)
# ----------------------------------------------------------------------


def test_due_ts_explicit_month_rolls_to_next_year():
    # Sep 2025: "3月5日" already passed -> must mean next year, not shift month.
    now = Path(__file__).resolve() and 1_757_900_000.0  # ~2025-09-14 Beijing
    ts = _due_ts("我们明年3月5日再聚", now)
    assert ts > now
    import datetime as _dt

    got = _dt.datetime.fromtimestamp(ts)
    assert (got.month, got.day) == (3, 5)
    assert got.year == _dt.datetime.fromtimestamp(now).year + 1


def test_due_ts_day_only_stays_this_year():
    now = 1_757_900_000.0  # mid-Sep
    ts = _due_ts("18号出结果", now)
    import datetime as _dt

    got = _dt.datetime.fromtimestamp(ts)
    assert got.day == 18 and got.month == _dt.datetime.fromtimestamp(now).month


@pytest.mark.parametrize(
    ("text", "expect_open"),
    [
        ("答应帮他查资料，周三给结果", True),  # promise + event -> tag
        ("明天面试，记得加油", True),  # near-day + event -> tag
        ("记得吃饭", False),  # reminder-only chatter
        ("明天再说吧", False),  # postponed
        ("今天天气好", False),  # no cue
        ("周末一起去看演唱会", True),  # near-day promise-free but scheduled
    ],
)
def test_open_loop_tagging_matrix(text: str, expect_open: bool):
    result = analyze_open_loop(text, now=1_757_900_000.0)
    assert bool(result) == expect_open, f"{text!r}: {result}"


# ----------------------------------------------------------------------
# loop resolution guard (P1-6 blacklist)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_open_loops_ignores_generic_overlap(tmp_path: Path):
    import json as _json
    import time as _time

    from tests.test_graph_memory import _FakeFaissDB

    from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine

    engine = MemoryEngine(
        db_path=str(tmp_path / "m.db"), faiss_db=_FakeFaissDB(), config={}
    )
    await engine.initialize()
    try:
        meta = {
            "open_loop": 1,
            "session_id": "s1",
            "status": "active",
            "create_time": _time.time(),
        }
        mid = await engine.add_memory(
            content="用户明天要面试，说好等结果", session_id="s1", metadata=dict(meta)
        )
        # The fake faiss_db keeps documents in memory only; mirror the row
        # into sqlite like the other engine tests do.
        await engine.db_connection.execute(
            "INSERT INTO documents (id, doc_id, text, metadata, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))",
            (mid, f"uuid-{mid}", "用户明天要面试，说好等结果", _json.dumps(meta)),
        )
        await engine.db_connection.commit()
        # Unrelated message sharing only generic time words must not close it.
        closed = await engine.resolve_open_loops_by_text(
            session_id="s1", user_text="明天记得喝水哦"
        )
        assert closed == []
        loops = await engine.load_open_loop_memories(session_id="s1")
        assert len(loops) == 1
        # The actual result mention closes it.
        closed = await engine.resolve_open_loops_by_text(
            session_id="s1", user_text="面试结果出了，我过了！"
        )
        assert len(closed) == 1
        assert await engine.load_open_loop_memories(session_id="s1") == []
    finally:
        await engine.close()
