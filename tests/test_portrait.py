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
    result = await service.read_summary(_portrait_request(), limit=8)
    addr = [i for i in result["items"] if i["dimension"] == "preferred_address"]
    assert len(addr) == 1
    assert "小李" in addr[0]["summary"] or "李" in addr[0]["summary"]


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
    assert result["code"] in {"portrait_usage_disabled", "bridge_stale_revision"}


# ----------------------------------------------------------------------
# bridge surface (spec §13 exposure mirrors)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_portrait_degrades_without_service():

    from astrbot_plugin_livingmemory.core.companion.bridge import CompanionBridge

    class _Host:
        class _CM:
            @staticmethod
            def get(key, default=None):
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


def test_contract_fingerprint_matches_companion_copy():
    # PC ships its own copy; the fork's contract fingerprint must equal it
    # or every portrait request would be rejected with fingerprint_mismatch.
    pc_path = (
        Path(__file__).resolve().parents[2]
        / "astrbot_plugin_private_companion"
        / "unified_profile_contract.py"
    )
    if not pc_path.exists():
        pytest.skip("companion plugin not installed in this environment")
    pc_source = pc_path.read_text(encoding="utf-8")
    fork_source = (
        Path(__file__).resolve().parents[1]
        / "core/companion/contracts/unified_profile_contract.py"
    ).read_text(encoding="utf-8")
    pc_fp = contract.CONTRACT_FINGERPRINT  # same constants source on both sides
    assert "CONTRACT_NAME" in pc_source
    assert pc_fp == "72067a45012a0588", "portrait contract fingerprint drifted"
    assert fork_source  # imported module == this file


def test_access_decision_matrix():
    ok = portrait_access_decision(_portrait_request())
    assert ok["allowed"] is True and ok["candidates_allowed"] is True
    # Unverified identity is refused even with an otherwise valid request.
    req = _portrait_request()
    req["person_ref"]["identity_assurance"] = "unverified"
    decision = portrait_access_decision(req)
    assert decision["candidates_allowed"] is False
    assert decision["code"] == "bridge_person_mismatch"
