"""Tests for the REQ-036 user-portrait pipeline (Phase 5, spec §13).

Covers the load-bearing invariants of the ported MC pipeline:
- capture is fail-closed without a valid companion DTO / grant;
- raw message text never lands in any portrait table (hash-only evidence);
- evidence dedupe + distinct-statement counting gate the nightly
  "intelligent" promotion (the batch is LLM-free by construction);
- single-value dimensions supersede older active claims;
- reads enforce capability, confidence floor, freshness, suppression and
  low-sensitivity; bridge surface mirrors MC's degrade codes;
- contract fingerprint matches the companion plugin's copy.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from astrbot_plugin_livingmemory.core.companion.contracts import (
    unified_profile_contract as contract,
)
from astrbot_plugin_livingmemory.core.companion.portrait_rules import (
    extract_explicit_candidates,
    portrait_access_decision,
)
from astrbot_plugin_livingmemory.core.companion.portrait_service import PortraitService
from astrbot_plugin_livingmemory.storage.portrait_store import PortraitStore

PERSON_ID = "person_" + hashlib.sha1(b"test-person").hexdigest()[:24]
IDENTITY_KEY = "chat-origin-v1:" + hashlib.sha256(b"idk").hexdigest()
_HASH64_RE = re.compile(r"[0-9a-f]{64}")


class _Event:
    """Duck-typed event carrying PC's bare-setattr DTO."""

    def __init__(self, dto: dict | None):
        if dto is not None:
            self.private_companion_unified_profile_context = dto


def _dto(*, mode: str = "learn_and_use") -> dict:
    person_ref = {
        "person_id": PERSON_ID,
        "resolved_identity_key": IDENTITY_KEY,
        "projection_revision": 1,
        "identity_assurance": "verified",
        "profile_status": "active",
    }
    return contract.build_profile_dto(
        person_ref=person_ref,
        identity_summary={"display_name": "小测"},
        expression_summary={"relationship_score": 10, "relationship_role": "friend"},
        capability_summary={
            "private_companion_enabled": True,
            "proactive_private_enabled": True,
            "portrait_mode": mode,
            "grant_source": "user_consent",
        },
    )


class _Cfg:
    def __init__(self, **over):
        self.values = {
            "usage_min_confidence": 0.75,
            "inferred_freshness_days": 90,
            "min_independent_evidence": 3,
            "daily_success_limit_per_person": 1,
            "daily_attempt_limit_per_person": 2,
        }
        self.values.update(over)

    def get(self, key: str, default=None):
        return self.values.get(str(key).removeprefix("portrait."), default)


async def _service(tmp_path: Path, **cfg) -> tuple[PortraitService, PortraitStore]:
    store = PortraitStore(str(tmp_path / "portrait.db"))
    await store.initialize()
    return PortraitService(store, _Cfg(**cfg).get), store


# ----------------------------------------------------------------------
# extractor sanity (ported rule-v2)
# ----------------------------------------------------------------------


def test_extractor_catches_first_person_claims_only():
    hits = extract_explicit_candidates("我喜欢抹茶拿铁")
    assert hits and hits[0]["profile_dimension"] == "preference"
    # Third-person / interrogative forms never become candidates.
    assert not extract_explicit_candidates("他喜欢抹茶拿铁")
    assert not extract_explicit_candidates("我喜欢什么？")
    assert not extract_explicit_candidates("今天天气好")


# ----------------------------------------------------------------------
# capture boundary
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_requires_dto_and_grant(tmp_path: Path):
    service, store = await _service(tmp_path)
    # No DTO at all -> contract mismatch, nothing stored.
    result = await service.capture_user_message(
        text="我喜欢抹茶拿铁", session_id="s1", scope="private", event=_Event(None)
    )
    assert result["ok"] is False
    assert result["code"] == "bridge_contract_mismatch"
    assert await store.list_people() == []
    # DTO present but portrait disabled -> learning refused, person synced.
    result = await service.capture_user_message(
        text="我喜欢抹茶拿铁",
        session_id="s1",
        scope="private",
        event=_Event(_dto(mode="disabled")),
    )
    assert result["code"] == "portrait_learning_disabled"
    people = await store.list_people()
    assert [p["person_id"] for p in people] == [PERSON_ID]


@pytest.mark.asyncio
async def test_capture_stores_no_raw_text(tmp_path: Path):
    service, store = await _service(tmp_path)
    secret = "我喜欢的暗号是紫罗兰42号"
    result = await service.capture_user_message(
        text=secret,
        session_id="s1",
        message_id="m1",
        scope="private",
        event=_Event(_dto()),
    )
    assert result["ok"] is True
    # MC invariant: the raw MESSAGE never lands in portrait tables. The
    # extracted claim_summary IS the portrait conclusion and legitimately
    # carries the value (same as MC); evidence rows stay hash-only.
    async with store._connect() as db:
        async with db.execute("SELECT * FROM portrait_evidence") as cur:
            evidence_rows = await cur.fetchall()
        joined_evidence = " | ".join(str(dict(r)) for r in evidence_rows)
    assert secret not in joined_evidence
    assert "紫罗兰42号" not in joined_evidence
    for r in evidence_rows:
        row = dict(r)
        assert _HASH64_RE.fullmatch(str(row["evidence_hash"]))
        assert _HASH64_RE.fullmatch(str(row["statement_fingerprint"]))
        assert str(row.get("context_refs")) in ("[]", "{}", "")


@pytest.mark.asyncio
async def test_duplicate_message_is_one_evidence(tmp_path: Path):
    service, store = await _service(tmp_path)
    event = _Event(_dto())
    first = await service.capture_user_message(
        text="我喜欢抹茶拿铁",
        session_id="s1",
        message_id="m1",
        scope="private",
        event=event,
    )
    assert first["facts"] == 1
    retry = await service.capture_user_message(
        text="我喜欢抹茶拿铁",
        session_id="s1",
        message_id="m1",
        scope="private",
        event=event,
    )
    # Same evidence hash -> no new evidence, no double queue entries.
    assert retry["code"] == "portrait_evidence_recorded"
    assert retry["facts"] == 0


# ----------------------------------------------------------------------
# nightly batch promotion
# ----------------------------------------------------------------------


def _stmt_hash(text: str) -> str:
    return hashlib.sha256("".join(text.split()).lower().encode()).hexdigest()


async def _feed(service, store, statements: list[str]) -> None:
    event = _Event(_dto())
    for i, stmt in enumerate(statements):
        await service.capture_user_message(
            text=stmt, session_id="s1", message_id=f"m{i}", scope="private", event=event
        )
    # Distinct messages must share ONE claim so counting converges.


@pytest.mark.asyncio
async def test_batch_requires_distinct_statements(tmp_path: Path):
    service, store = await _service(tmp_path)
    # Three differently-phrased statements of the SAME preference claim.
    await _feed(
        service,
        store,
        [
            "我喜欢抹茶拿铁",
            "我超喜欢抹茶拿铁",
            "我最爱抹茶拿铁",
        ],
    )
    async with store._connect() as db:
        async with db.execute(
            "SELECT evidence_hashes FROM portrait_facts WHERE portrait_tier='base'"
        ) as cur:
            row = await cur.fetchone()
    hashes = json.loads(row[0])
    # Distinct message ids -> distinct evidence, merged into one base claim.
    assert len(hashes) >= 3

    # Run nightly promotion twice: first succeeds, second is daily-limited.
    out1 = await service.run_daily_batch(PERSON_ID, run_day="2026-09-16")
    out2 = await service.run_daily_batch(PERSON_ID, run_day="2026-09-16")
    assert out1["ok"] is True and out1["created"] == 1
    assert out2["code"] == "portrait_daily_limit"
    # The intelligent tier is the 可能-prefixed aggregate claim.
    read = await service.read_summary(_portrait_request(), limit=8)
    summaries = [i["summary"] for i in read["items"]]
    assert any(s.startswith("可能") for s in summaries)
    assert any(i["epistemic_status"] == "inferred" for i in read["items"])


@pytest.mark.asyncio
async def test_batch_attempt_cap_survives_failures(tmp_path: Path):
    """Failed (insufficient-evidence) runs still consume the attempt budget."""
    service, _ = await _service(tmp_path, min_independent_evidence=9)
    await _feed(
        service,
        service.store,
        [
            "我喜欢抹茶拿铁",
            "我超喜欢抹茶拿铁",
            "我最爱抹茶拿铁",
        ],
    )
    day = "2026-09-16"
    out1 = await service.run_daily_batch(PERSON_ID, run_day=day)
    assert out1["ok"] is False and out1["code"] == "portrait_insufficient_evidence"
    out2 = await service.run_daily_batch(PERSON_ID, run_day=day)
    assert out2["ok"] is False
    # attempts reached 2 (default attempt_limit): third pass is capped out.
    out3 = await service.run_daily_batch(PERSON_ID, run_day=day)
    assert out3["code"] == "portrait_daily_limit"


def _portrait_request() -> dict:
    person_ref = {
        "person_id": PERSON_ID,
        "resolved_identity_key": IDENTITY_KEY,
        "projection_revision": 1,
        "identity_assurance": "verified",
        "profile_status": "active",
    }
    return contract.build_portrait_request(
        person_ref=person_ref,
        requester_person_id=PERSON_ID,
        target_person_id=PERSON_ID,
        scope="private",
        purpose="summarize_to_subject",
    )


# ----------------------------------------------------------------------
# reads: fail-closed gates
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_denies_third_party_and_bad_contract(tmp_path: Path):
    service, _ = await _service(tmp_path)
    bad = {"contract_name": "evil"}
    result = await service.read_summary(bad, limit=8)
    assert result["ok"] is False
    assert result["code"] == "bridge_contract_mismatch"
    req = _portrait_request()
    req["purpose"] = "disclose_to_third_party"
    result = await service.read_summary(req, limit=8)
    assert result["ok"] is False
    assert result["code"] == "portrait_third_party_forbidden"


@pytest.mark.asyncio
async def test_suppression_blocks_write_and_read(tmp_path: Path):
    service, store = await _service(tmp_path)
    event = _Event(_dto())
    await service.capture_user_message(
        text="我喜欢抹茶拿铁",
        session_id="s1",
        message_id="m9",
        scope="private",
        event=event,
    )
    from astrbot_plugin_livingmemory.core.companion.portrait_rules import (
        suppression_key,
    )

    async with store._connect() as db:
        async with db.execute(
            "SELECT dimension, normalized_claim_hash, source_scope "
            "FROM portrait_facts LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    await store.upsert_suppression(
        {
            "suppression_key": suppression_key(
                PERSON_ID,
                row["dimension"],
                row["normalized_claim_hash"],
                row["source_scope"],
            ),
            "person_id": PERSON_ID,
            "dimension": row["dimension"],
            "normalized_claim_hash": row["normalized_claim_hash"],
            "scope": row["source_scope"],
            "reason": "user_denied",
            "actor": "test",
            "status": "active",
            "operation_id": "op-1",
        }
    )
    # Re-capturing the same statement must not resurrect the fact read.
    result = await service.read_summary(_portrait_request(), limit=8)
    assert result["ok"] is True
    assert all("抹茶拿铁" not in i["summary"] for i in result["items"])
    # ...and the write path must refuse re-upserting it (MC gate parity).
    from astrbot_plugin_livingmemory.core.companion.portrait_rules import (
        normalized_claim_hash,
    )

    write = await store.upsert_fact(
        {
            "person_id": PERSON_ID,
            "dimension": row["dimension"],
            "normalized_claim_hash": row["normalized_claim_hash"],
            "claim_summary": "喜欢 抹茶拿铁",
            "portrait_tier": "base",
            "source_scope": row["source_scope"],
            "confidence": 1.0,
            "status": "active",
            "sensitivity": "low",
            "evidence_hashes": [_stmt_hash("我喜欢抹茶拿铁")],
        }
    )
    assert write["ok"] is False and write["code"] == "portrait_suppressed"
    assert (
        normalized_claim_hash("preference", "like:抹茶拿铁")
        == row["normalized_claim_hash"]
    )


@pytest.mark.asyncio
async def test_single_value_dimension_supersedes_previous(tmp_path: Path):
    service, store = await _service(tmp_path)
    event = _Event(_dto())
    await service.capture_user_message(
        text="叫我老王", session_id="s1", message_id="m1", scope="private", event=event
    )
    await service.capture_user_message(
        text="叫我小李", session_id="s1", message_id="m2", scope="private", event=event
    )
    # Row-state truth, not just read-membership: old claim superseded, new
    # active, with the back-pointer wired.
    async with store._connect() as db:
        async with db.execute(
            "SELECT id, claim_summary, status, supersedes_id FROM portrait_facts "
            "WHERE dimension='preferred_address'"
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    old = next(r for r in rows if "王" in r["claim_summary"])
    new = next(r for r in rows if "李" in r["claim_summary"])
    assert old["status"] == "superseded"
    assert new["status"] == "active"
    # MC write semantics: the retired row's supersedes_id points at the
    # replacement fact.
    assert old["supersedes_id"] == new["id"]
    assert new["supersedes_id"] == ""
    result = await service.read_summary(_portrait_request(), limit=8)
    addr = [i for i in result["items"] if i["dimension"] == "preferred_address"]
    assert len(addr) == 1 and "李" in addr[0]["summary"]


@pytest.mark.asyncio
async def test_group_scope_capture_and_read(tmp_path: Path):
    service, _ = await _service(tmp_path)
    event = _Event(_dto())
    group_scope = "group:aiocqhttp:98765"
    result = await service.capture_user_message(
        text="我喜欢看老电影",
        session_id="g1",
        message_id="gm1",
        scope=group_scope,
        event=event,
    )
    assert result["ok"] is True and result["facts"] == 1
    # Reads from the same group scope see it...
    req = _portrait_request()
    req["scope"] = group_scope
    read = await service.read_summary(req, limit=8)
    assert any("老电影" in i["summary"] for i in read["items"])
    # ...while a private-scope read must NOT (source_only facts stay put:
    # movie preference is deny-free but communication/venue markers aside,
    # the fact's usable_scope governs).
    private_read = await service.read_summary(_portrait_request(), limit=8)
    assert all("老电影" not in i["summary"] for i in private_read["items"])


@pytest.mark.asyncio
async def test_usage_disabled_returns_honest_code(tmp_path: Path):
    service, store = await _service(tmp_path)
    # Person projected with usage-only mode, then capture... read gate:
    event = _Event(_dto(mode="learn_and_use"))
    await service.capture_user_message(
        text="我喜欢抹茶拿铁",
        session_id="s1",
        message_id="m1",
        scope="private",
        event=event,
    )
    # Re-sync person under disabled mode (higher revision wins).
    dto_off = _dto(mode="disabled")
    dto_off["person_ref"]["projection_revision"] = 2
    ev_off = _Event(dto_off)
    req = _portrait_request()
    req["person_ref"]["projection_revision"] = 2
    req["requester_person_id"] = PERSON_ID
    req["target_person_id"] = PERSON_ID
    await service.sync_profile_context(event=ev_off, legacy_scope="private")
    result = await service.read_summary(req, limit=8)
    assert result["ok"] is False
    # With the matching (revision-2) person_ref the fence passes, so the
    # only honest outcome is usage disabled; bridge_stale_revision here
    # would mean the fence or the capability projection broke.
    assert result["code"] == "portrait_usage_disabled"
    # And the stale fence itself: the same read with revision 1 must be
    # refused as stale, proving the fence is real, not absent.
    stale = _portrait_request()
    stale_result = await service.read_summary(stale, limit=8)
    assert stale_result["ok"] is False
    assert stale_result["code"] == "bridge_stale_revision"


# ----------------------------------------------------------------------
# bridge surface (spec §13 exposure mirrors)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_portrait_degrades_without_service():

    from astrbot_plugin_livingmemory.core.companion.bridge import CompanionBridge

    class _Host:
        enabled = True

        class _CM:
            @staticmethod
            def get(key, default=None):
                # only the bridge master switch reads with default True
                return default

        config_manager = _CM()
        initializer = None
        bot_id = "aiocqhttp:10001"

        def spawn_companion_background(self, coro):
            coro.close()

    bridge = CompanionBridge(_Host())
    result = await bridge.read_unified_profile_portrait({}, limit=5)
    assert result == {
        "ok": False,
        "read_only": True,
        "code": "bridge_unavailable",
        "items": [],
    }
    status = await bridge.unified_profile_portrait_status("person_x")
    assert status["code"] == "bridge_unavailable" and status["ok"] is False
    batch = await bridge.run_unified_profile_portrait_batch("person_x")
    assert batch["code"] == "bridge_unavailable"
    # A deactivated bridge must answer the same honest shapes.
    bridge.deactivate()
    result = await bridge.read_unified_profile_portrait({}, limit=5)
    assert result["code"] == "bridge_unavailable"

    # ...and with the config switch off (enabled default false on this host).
    class _DisabledHost(_Host):
        class _CM:
            @staticmethod
            def get(key, default=None):
                return False if key == "companion_bridge.enabled" else default

        config_manager = _CM()

    disabled = CompanionBridge(_DisabledHost())
    result = await disabled.read_unified_profile_portrait({}, limit=5)
    assert result == {
        "ok": False,
        "read_only": True,
        "code": "bridge_unavailable",
        "items": [],
    }
    assert (await disabled.run_unified_profile_portrait_batch("person_x"))[
        "code"
    ] == "bridge_unavailable"


def _load_contract_module(path: Path, name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_contract_fingerprint_matches_companion_copy():
    # PC ships its own contract copy; both sides compare the COMPUTED
    # fingerprint values (PC rejects requests whose contract_fingerprint
    # differs), so load PC's module and recompute independently.
    pc_path = (
        Path(__file__).resolve().parents[2]
        / "astrbot_plugin_private_companion"
        / "unified_profile_contract.py"
    )
    if not pc_path.exists():
        pytest.skip("companion plugin not installed in this environment")
    pc_contract = _load_contract_module(pc_path, "pc_unified_profile_contract")
    assert pc_contract.CONTRACT_FINGERPRINT == contract.CONTRACT_FINGERPRINT, (
        "portrait contract fingerprint drifted from the companion plugin's copy"
    )
    # self_check recomputes from the shipped constants: catches any local edit.
    assert pc_contract.contract_self_check() == []
    assert contract.contract_self_check() == []
    # PC's strict validator must reject its own namespace_context injection...
    req = contract.build_portrait_request(
        person_ref={
            "person_id": PERSON_ID,
            "resolved_identity_key": IDENTITY_KEY,
            "projection_revision": 1,
            "identity_assurance": "verified",
            "profile_status": "active",
        },
        requester_person_id=PERSON_ID,
        target_person_id=PERSON_ID,
        scope="private",
        purpose="summarize_to_subject",
    )
    req["namespace_context"] = {"persona_id": "p"}
    assert "portrait_request_fields_invalid" in pc_contract.validate_portrait_request(
        dict(req)
    ), "PC validator unexpectedly permissive"
    # ...while the fork's (MC-copy) validator accepts the PC-produced shape.
    assert contract.validate_portrait_request(dict(req)) == []


def test_access_decision_matrix():
    ok = portrait_access_decision(_portrait_request())
    assert ok["allowed"] is True and ok["candidates_allowed"] is True
    # Unverified identity is refused even with an otherwise valid request.
    req = _portrait_request()
    req["person_ref"]["identity_assurance"] = "unverified"
    decision = portrait_access_decision(req)
    assert decision["candidates_allowed"] is False
    assert decision["code"] == "bridge_person_mismatch"


# ----------------------------------------------------------------------
# maintenance-scheduler robustness (audit A3)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_isolates_per_person_batch_failures(tmp_path: Path):
    """One raising person must not abort the pass for the rest (FIFO head
    starvation guard): later people still get their batch attempt."""
    from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine
    from astrbot_plugin_livingmemory.core.schedulers.companion_maintenance_scheduler import (  # noqa: E501
        CompanionMaintenanceScheduler,
    )

    from tests.test_graph_memory import _FakeFaissDB

    engine = MemoryEngine(
        db_path=str(tmp_path / "m.db"), faiss_db=_FakeFaissDB(), config={}
    )
    await engine.initialize()
    try:
        service, store = await _service(tmp_path)
        engine.portrait_store = store
        engine.portrait_service = service
        # Two pending people...
        await _feed(service, store, ["我喜欢抹茶拿铁"])
        other = "person_" + hashlib.sha1(b"other").hexdigest()[:24]
        await store.upsert_person_projection(
            {
                "person_id": other,
                "resolved_identity_key": "chat-origin-v1:"
                + hashlib.sha256(b"o").hexdigest(),
                "projection_revision": 1,
                "identity_assurance": "verified",
                "profile_status": "active",
            },
            {
                "private_companion_enabled": True,
                "proactive_private_enabled": True,
                "portrait_mode": "learn_and_use",
                "grant_source": "user_consent",
            },
        )
        await store.enqueue_learning(
            person_id=other, fact_id="missing-fact", evidence_hash=_stmt_hash("x")
        )
        calls: list[str] = []

        class _FlakyService:
            def __init__(self, inner):
                self._inner = inner

            async def run_daily_batch(self, person_id, *, run_day=""):
                calls.append(person_id)
                if person_id == calls[0]:
                    raise RuntimeError("simulated db failure")
                return await self._inner.run_daily_batch(person_id, run_day=run_day)

            async def status(self, person_id):
                return await self._inner.status(person_id)

        engine.portrait_service = _FlakyService(service)
        scheduler = CompanionMaintenanceScheduler(memory_engine=engine)
        result = await scheduler._run_portrait_batches()
        assert len(calls) == 2, f"both pending people must be attempted: {calls}"
        assert isinstance(result, int)
    finally:
        await engine.close()
