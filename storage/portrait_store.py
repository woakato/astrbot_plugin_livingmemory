"""SQLite-backed store for the REQ-036 user-portrait pipeline (Phase 5).

Faithful port of memory_companion's portrait table set and the subset of
``core/store.py`` portrait methods this fork needs: people projections,
hash-only evidence, governed facts, learning queue, nightly batch promotion,
summary reads, suppressions, status. Portraits are deliberately LLM-free
(regex rule-v2 extraction upstream; the nightly "intelligent" tier is pure
distinct-evidence counting with a 可能 prefix), so nothing here depends on
model capability.

Safety properties preserved from MC (each one is load-bearing for the
privacy contract with the companion plugin):

- raw message text NEVER lands in these tables: evidence rows keep only
  sha256 hashes / statement fingerprints;
- per-person ``portrait_revision`` bumps on every fact/suppression write as
  the reader's cache fence;
- single-value dimensions supersede all other active facts of the same
  person+dimension+scope (a new "叫我X" retires the old one);
- every credential-shaped string is scrubbed before any write (the same
  deterministic redactor MC ships, byte-identical copy under contracts/);
- all reads are fail-closed: missing person, stale projection revision,
  disabled capability, suppression, confidence floor, 90-day inferred
  freshness, and scope allowlist each independently reject a row.

Suppression rows are enforced by every write/read path, but note the
end-user denial flow (MC's governance API) is not wired in this fork v1:
in practice suppressions stay empty unless written internally (spec §13.4).
MC-only administration tables (portrait_operations, profile_repair_operations)
are intentionally absent with their unported surfaces.

Follows CompanionStore's short-connection pattern (WAL + busy_timeout).
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from ..core.companion.contracts import sensitive_data
from ..core.companion.portrait_rules import cross_scene_whitelisted_fact

PORTRAIT_SINGLE_VALUE_DIMENSIONS = frozenset(
    {
        "preferred_address",
        "name",
        "birthday",
        "birth_date",
        "occupation",
        "profession",
        "education",
        "major",
        "zodiac",
        "zodiac_or_blood_type",
        "blood_type",
    }
)
_HASH64 = re.compile(r"[0-9a-f]{64}")
_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _clean_text(value: Any, limit: int = 2000) -> str:
    """Same cleaner MC uses (collapse whitespace, ellipsis-truncate)."""
    import re as _re

    text = "" if value is None else str(value)
    text = _re.sub(r"\s+", " ", text.replace("\u3000", " ")).strip()
    if len(text) > limit:
        return text[: max(0, limit - 1)].rstrip() + "…"
    return text


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stable_fingerprint(*parts: Any) -> str:
    import hashlib

    raw = "|".join(_clean_text(p, 1000).lower() for p in parts if p is not None)
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()


def _json_dumps(value: Any) -> str:
    return json.dumps(
        value if value is not None else {}, ensure_ascii=False, separators=(",", ":")
    )


def _json_loads(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


class PortraitStore:
    """Persistence for the deterministic portrait pipeline (8 MC tables)."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._write_lock = asyncio.Lock()

    @asynccontextmanager
    async def _connect(self):
        db = await aiosqlite.connect(self.db_path)
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute("PRAGMA busy_timeout = 10000")
            yield db
        finally:
            await db.close()

    async def initialize(self) -> None:
        """Create portrait tables (idempotent; mirrors MC's DDL)."""
        async with self._connect() as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS portrait_people (
                    person_id TEXT PRIMARY KEY,
                    resolved_identity_key TEXT NOT NULL DEFAULT '',
                    projection_revision INTEGER NOT NULL DEFAULT 0,
                    identity_assurance TEXT NOT NULL DEFAULT '',
                    profile_status TEXT NOT NULL DEFAULT '',
                    capability_summary TEXT NOT NULL DEFAULT '{}',
                    portrait_revision INTEGER NOT NULL DEFAULT 0,
                    last_synced_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS portrait_scope_capabilities (
                    person_id TEXT NOT NULL DEFAULT '',
                    source_scope TEXT NOT NULL DEFAULT '',
                    capability_summary TEXT NOT NULL DEFAULT '{}',
                    projection_revision INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(person_id, source_scope)
                );
                CREATE TABLE IF NOT EXISTS portrait_evidence (
                    evidence_hash TEXT PRIMARY KEY,
                    person_id TEXT NOT NULL DEFAULT '',
                    origin_identity_key TEXT NOT NULL DEFAULT '',
                    scope TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    message_id TEXT NOT NULL DEFAULT '',
                    statement_fingerprint TEXT NOT NULL DEFAULT '',
                    context_refs TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS portrait_facts (
                    id TEXT PRIMARY KEY,
                    person_id TEXT NOT NULL DEFAULT '',
                    dimension TEXT NOT NULL DEFAULT '',
                    normalized_claim_hash TEXT NOT NULL DEFAULT '',
                    claim_summary TEXT NOT NULL DEFAULT '',
                    portrait_tier TEXT NOT NULL DEFAULT '',
                    producer_kind TEXT NOT NULL DEFAULT '',
                    producer_version TEXT NOT NULL DEFAULT '',
                    derivation_kind TEXT NOT NULL DEFAULT '',
                    epistemic_status TEXT NOT NULL DEFAULT '',
                    source_scope TEXT NOT NULL DEFAULT '',
                    usable_scope TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0,
                    sensitivity TEXT NOT NULL DEFAULT 'high',
                    status TEXT NOT NULL DEFAULT 'active',
                    evidence_hashes TEXT NOT NULL DEFAULT '[]',
                    context_refs TEXT NOT NULL DEFAULT '[]',
                    first_evidence_at TEXT NOT NULL DEFAULT '',
                    last_evidence_at TEXT NOT NULL DEFAULT '',
                    expires_at TEXT NOT NULL DEFAULT '',
                    supersedes_id TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 1,
                    operation_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(person_id, dimension, normalized_claim_hash, portrait_tier, source_scope)
                );
                CREATE TABLE IF NOT EXISTS portrait_suppressions (
                    suppression_key TEXT PRIMARY KEY,
                    person_id TEXT NOT NULL DEFAULT '',
                    dimension TEXT NOT NULL DEFAULT '',
                    normalized_claim_hash TEXT NOT NULL DEFAULT '',
                    scope TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    origin_identity_key TEXT NOT NULL DEFAULT '',
                    operation_id TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '',
                    expires_at TEXT NOT NULL DEFAULT '',
                    revoked_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS portrait_learning_queue (
                    queue_id TEXT PRIMARY KEY,
                    person_id TEXT NOT NULL DEFAULT '',
                    fact_id TEXT NOT NULL DEFAULT '',
                    evidence_hash TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(person_id, fact_id, evidence_hash)
                );
                CREATE TABLE IF NOT EXISTS portrait_daily_runs (
                    person_id TEXT NOT NULL DEFAULT '',
                    run_day TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    successes INTEGER NOT NULL DEFAULT 0,
                    last_code TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(person_id, run_day)
                );
                CREATE INDEX IF NOT EXISTS idx_portrait_evidence_person ON portrait_evidence(person_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_portrait_facts_person ON portrait_facts(person_id, status, sensitivity, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_portrait_scope_capabilities_person ON portrait_scope_capabilities(person_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_portrait_suppressions_person ON portrait_suppressions(person_id, status);
                CREATE INDEX IF NOT EXISTS idx_portrait_learning_queue_person ON portrait_learning_queue(person_id, state, updated_at);
                CREATE INDEX IF NOT EXISTS idx_portrait_learning_queue_fact ON portrait_learning_queue(fact_id, state);
                """
            )
            await db.commit()

    # ------------------------------------------------------------------
    # people projection (PC attaches the REQ-036 DTO on every message)
    # ------------------------------------------------------------------

    async def upsert_person_projection(
        self,
        person_ref: dict[str, Any],
        capability_summary: dict[str, Any],
        *,
        source_scope: str = "",
    ) -> dict[str, Any]:
        person_id = _clean_text(person_ref.get("person_id"), 80)
        identity_key = _clean_text(person_ref.get("resolved_identity_key"), 96)
        revision = int(person_ref.get("projection_revision") or 0)
        assurance = _clean_text(person_ref.get("identity_assurance"), 40)
        status = _clean_text(person_ref.get("profile_status"), 40)
        source_scope = _clean_text(source_scope, 80)
        if (
            not person_id
            or not identity_key
            or revision < 1
            or assurance
            not in {"unverified", "observed", "verified", "explicit_linked"}
            or status not in {"active", "suspended", "quarantined", "deleted"}
        ):
            return {"ok": False, "code": "bridge_person_mismatch", "state": "invalid"}
        now = _utc_now()
        async with self._write_lock:
            async with self._connect() as db:
                async with db.execute(
                    "SELECT * FROM portrait_people WHERE person_id=?", (person_id,)
                ) as cur:
                    previous = await cur.fetchone()
                if previous is not None:
                    old_revision = int(previous["projection_revision"] or 0)
                    if old_revision > revision:
                        return {
                            "ok": False,
                            "code": "bridge_stale_revision",
                            "state": "stale",
                            "projection_revision": old_revision,
                        }
                    if (
                        old_revision == revision
                        and previous["resolved_identity_key"] != identity_key
                    ):
                        return {
                            "ok": False,
                            "code": "bridge_person_mismatch",
                            "state": "invalid",
                        }
                portrait_revision = (
                    int(previous["portrait_revision"] or 0)
                    if previous is not None
                    else 0
                )
                # Durable rule (MC store.py:1932-1936): a person row may only
                # be overwritten by a private-scope projection; group-scope
                # syncs live in portrait_scope_capabilities alone.
                durable_capability_summary = capability_summary
                if (
                    previous is not None
                    and source_scope
                    and not (
                        source_scope == "private" or source_scope.startswith("private@")
                    )
                ):
                    durable_capability_summary = _json_loads(
                        previous["capability_summary"], {}
                    )
                await db.execute(
                    """
                    INSERT INTO portrait_people(
                        person_id, resolved_identity_key, projection_revision,
                        identity_assurance, profile_status, capability_summary,
                        portrait_revision, last_synced_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(person_id) DO UPDATE SET
                        resolved_identity_key=excluded.resolved_identity_key,
                        projection_revision=excluded.projection_revision,
                        identity_assurance=excluded.identity_assurance,
                        profile_status=excluded.profile_status,
                        capability_summary=excluded.capability_summary,
                        last_synced_at=excluded.last_synced_at,
                        updated_at=excluded.updated_at
                    """,
                    (
                        person_id,
                        identity_key,
                        revision,
                        assurance,
                        status,
                        _json_dumps(durable_capability_summary),
                        portrait_revision,
                        now,
                        now,
                    ),
                )
                if source_scope:
                    await db.execute(
                        """
                        INSERT INTO portrait_scope_capabilities(
                            person_id, source_scope, capability_summary,
                            projection_revision, updated_at
                        ) VALUES(?,?,?,?,?)
                        ON CONFLICT(person_id, source_scope) DO UPDATE SET
                            capability_summary=excluded.capability_summary,
                            projection_revision=excluded.projection_revision,
                            updated_at=excluded.updated_at
                        """,
                        (
                            person_id,
                            source_scope,
                            _json_dumps(capability_summary),
                            revision,
                            now,
                        ),
                    )
                await db.commit()
        return {
            "ok": True,
            "code": "profile_exact",
            "state": "ready",
            "portrait_revision": portrait_revision,
        }

    async def projection_decision(self, person_ref: dict[str, Any]) -> dict[str, Any]:
        person_id = _clean_text(person_ref.get("person_id"), 80)
        identity_key = _clean_text(person_ref.get("resolved_identity_key"), 96)
        revision = int(person_ref.get("projection_revision") or 0)
        assurance = _clean_text(person_ref.get("identity_assurance"), 40)
        if (
            not person_id
            or not identity_key
            or revision < 1
            or assurance not in {"observed", "verified", "explicit_linked"}
        ):
            return {"ok": False, "code": "bridge_person_mismatch"}
        async with self._connect() as db:
            async with db.execute(
                "SELECT resolved_identity_key, projection_revision, "
                "identity_assurance, profile_status FROM portrait_people "
                "WHERE person_id=?",
                (person_id,),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return {"ok": False, "code": "bridge_unavailable"}
        if _clean_text(row["resolved_identity_key"], 96) != identity_key:
            return {"ok": False, "code": "bridge_person_mismatch"}
        if int(row["projection_revision"] or 0) != revision:
            return {"ok": False, "code": "bridge_stale_revision"}
        if _clean_text(row["identity_assurance"], 40) not in {
            "observed",
            "verified",
            "explicit_linked",
        }:
            return {"ok": False, "code": "bridge_person_mismatch"}
        if _clean_text(row["profile_status"], 40) != "active":
            return {"ok": False, "code": "bridge_person_mismatch"}
        return {"ok": True, "code": "profile_exact"}

    async def status(self, person_id: str) -> dict[str, Any]:
        person_id = _clean_text(person_id, 80)
        async with self._connect() as db:
            async with db.execute(
                "SELECT portrait_revision, last_synced_at, profile_status "
                "FROM portrait_people WHERE person_id=?",
                (person_id,),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return {
                "ok": False,
                "code": "bridge_unavailable",
                "person_id": person_id,
                "last_synced_at": "",
                "portrait_revision": 0,
            }
        active = _clean_text(row["profile_status"], 40) == "active"
        return {
            "ok": active,
            "code": "profile_exact" if active else "bridge_person_mismatch",
            "person_id": person_id,
            "portrait_revision": int(row["portrait_revision"] or 0),
            "last_synced_at": _clean_text(row["last_synced_at"], 80),
        }

    async def list_people(self, *, limit: int = 100) -> list[dict[str, Any]]:
        async with self._connect() as db:
            async with db.execute(
                """
                SELECT p.*, COUNT(DISTINCT f.id) AS fact_count,
                       COUNT(DISTINCT e.evidence_hash) AS evidence_count
                FROM portrait_people p
                LEFT JOIN portrait_facts f ON f.person_id=p.person_id
                LEFT JOIN portrait_evidence e ON e.person_id=p.person_id
                GROUP BY p.person_id
                ORDER BY p.updated_at DESC
                LIMIT ?
                """,
                (max(1, min(500, int(limit))),),
            ) as cur:
                rows = await cur.fetchall()
        return [
            {
                "person_id": _clean_text(row["person_id"], 80),
                "identity_assurance": _clean_text(row["identity_assurance"], 40),
                "profile_status": _clean_text(row["profile_status"], 40),
                "projection_revision": int(row["projection_revision"] or 0),
                "portrait_revision": int(row["portrait_revision"] or 0),
                "fact_count": int(row["fact_count"] or 0),
                "evidence_count": int(row["evidence_count"] or 0),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # evidence (hash-only; raw text never stored)
    # ------------------------------------------------------------------

    async def add_evidence(self, evidence: dict[str, Any]) -> dict[str, Any]:
        person_id = _clean_text(evidence.get("person_id"), 80)
        evidence_key = _clean_text(evidence.get("evidence_hash"), 80)
        if not person_id or not _HASH64.fullmatch(evidence_key):
            return {"ok": False, "code": "portrait_evidence_invalid", "created": False}
        statement_key = _clean_text(evidence.get("statement_fingerprint"), 80)
        if not _HASH64.fullmatch(statement_key):
            statement_key = evidence_key
        context_refs = (
            evidence.get("context_refs")
            if isinstance(evidence.get("context_refs"), list)
            else []
        )
        now = _utc_now()
        async with self._write_lock:
            async with self._connect() as db:
                cur = await db.execute(
                    """
                    INSERT OR IGNORE INTO portrait_evidence(
                        evidence_hash, person_id, origin_identity_key, scope,
                        session_id, message_id, statement_fingerprint,
                        context_refs, created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        evidence_key,
                        person_id,
                        _clean_text(evidence.get("origin_identity_key"), 96),
                        _clean_text(evidence.get("scope"), 80),
                        _clean_text(evidence.get("session_id"), 200),
                        _clean_text(evidence.get("message_id"), 120),
                        statement_key,
                        _json_dumps(
                            [
                                _clean_text(i, 160)
                                for i in context_refs
                                if _clean_text(i, 160)
                            ][:8]
                        ),
                        now,
                    ),
                )
                await db.commit()
                created = bool(cur.rowcount)
        return {"ok": True, "code": "portrait_evidence_recorded", "created": created}

    # ------------------------------------------------------------------
    # facts (governed upsert with revision fence + single-value supersede)
    # ------------------------------------------------------------------

    async def upsert_fact(self, fact: dict[str, Any]) -> dict[str, Any]:
        async with self._write_lock:
            return await self._upsert_fact_locked(fact)

    async def _upsert_fact_locked(self, fact: dict[str, Any]) -> dict[str, Any]:
        fact = sensitive_data.redact_sensitive_value(fact)
        person_id = _clean_text(fact.get("person_id"), 80)
        dimension = _clean_text(fact.get("dimension"), 80)
        claim_hash = _clean_text(fact.get("normalized_claim_hash"), 80)
        tier = _clean_text(fact.get("portrait_tier"), 24)
        source_scope = _clean_text(fact.get("source_scope"), 80)
        if (
            not person_id
            or not dimension
            or not _HASH64.fullmatch(claim_hash)
            or tier not in {"base", "intelligent"}
        ):
            return {"ok": False, "code": "portrait_fact_invalid", "created": False}
        if not source_scope:
            source_scope = "private"
        now = _utc_now()
        evidence_hashes = [
            _clean_text(item, 80)
            for item in (fact.get("evidence_hashes") or [])
            if _HASH64.fullmatch(_clean_text(item, 80))
        ][:16]
        status = _clean_text(fact.get("status"), 40) or "active"
        cardinality = _clean_text(fact.get("profile_cardinality"), 20).lower()
        single_value = (
            cardinality == "single" or dimension in PORTRAIT_SINGLE_VALUE_DIMENSIONS
        )
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                if await self._suppressed(
                    db, person_id, dimension, claim_hash, source_scope
                ):
                    await db.rollback()
                    return {
                        "ok": False,
                        "code": "portrait_suppressed",
                        "created": False,
                    }
                async with db.execute(
                    """
                    SELECT * FROM portrait_facts WHERE person_id=? AND dimension=?
                      AND normalized_claim_hash=? AND portrait_tier=? AND source_scope=?
                    """,
                    (person_id, dimension, claim_hash, tier, source_scope),
                ) as cur:
                    previous = await cur.fetchone()
                previous_hashes = (
                    _json_loads(previous["evidence_hashes"], [])
                    if previous is not None
                    else []
                )
                merged_hashes = list(
                    dict.fromkeys(
                        [
                            _clean_text(item, 80)
                            for item in previous_hashes + evidence_hashes
                            if _clean_text(item, 80)
                        ]
                    )
                )[:16]
                fact_id = (
                    _clean_text(previous["id"], 120)
                    if previous is not None
                    else (
                        f"portrait_{_stable_fingerprint(person_id, dimension, claim_hash, tier, source_scope)[:24]}"
                    )
                )
                revision = (
                    int(previous["revision"] or 0) + 1 if previous is not None else 1
                )
                first_at = (
                    _clean_text(previous["first_evidence_at"], 80)
                    if previous is not None
                    else now
                )
                sensitivity = _clean_text(fact.get("sensitivity"), 24)
                if sensitivity not in {"low", "sensitive", "high"}:
                    sensitivity = "high"
                usable_scope = _clean_text(fact.get("usable_scope"), 80)
                if usable_scope == "self_low_global" and cross_scene_whitelisted_fact(
                    dimension=dimension,
                    claim_summary=fact.get("claim_summary"),
                    sensitivity=sensitivity,
                    source_scope=source_scope,
                ):
                    usable_scope = "self_low_global"
                else:
                    usable_scope = "source_only"
                await db.execute(
                    """
                    INSERT INTO portrait_facts(
                        id, person_id, dimension, normalized_claim_hash, claim_summary,
                        portrait_tier, producer_kind, producer_version, derivation_kind,
                        epistemic_status, source_scope, usable_scope, confidence,
                        sensitivity, status, evidence_hashes, context_refs,
                        first_evidence_at, last_evidence_at, expires_at, supersedes_id,
                        revision, operation_id, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(person_id, dimension, normalized_claim_hash,
                                portrait_tier, source_scope) DO UPDATE SET
                        claim_summary=excluded.claim_summary,
                        producer_kind=excluded.producer_kind,
                        producer_version=excluded.producer_version,
                        derivation_kind=excluded.derivation_kind,
                        epistemic_status=excluded.epistemic_status,
                        usable_scope=excluded.usable_scope,
                        confidence=max(portrait_facts.confidence, excluded.confidence),
                        sensitivity=excluded.sensitivity,
                        status=excluded.status,
                        evidence_hashes=excluded.evidence_hashes,
                        context_refs=excluded.context_refs,
                        last_evidence_at=excluded.last_evidence_at,
                        expires_at=excluded.expires_at,
                        supersedes_id=excluded.supersedes_id,
                        revision=excluded.revision,
                        operation_id=excluded.operation_id,
                        updated_at=excluded.updated_at
                    """,
                    (
                        fact_id,
                        person_id,
                        dimension,
                        claim_hash,
                        _clean_text(fact.get("claim_summary"), 180),
                        tier,
                        _clean_text(fact.get("producer_kind"), 80),
                        _clean_text(fact.get("producer_version"), 80),
                        _clean_text(fact.get("derivation_kind"), 80),
                        _clean_text(fact.get("epistemic_status"), 80),
                        source_scope,
                        usable_scope,
                        max(0.0, min(1.0, float(fact.get("confidence") or 0.0))),
                        sensitivity,
                        status,
                        _json_dumps(merged_hashes),
                        _json_dumps(
                            [
                                _clean_text(i, 160)
                                for i in (fact.get("context_refs") or [])
                                if _clean_text(i, 160)
                            ][:8]
                        ),
                        first_at,
                        now,
                        _clean_text(fact.get("expires_at"), 80),
                        _clean_text(fact.get("supersedes_id"), 120),
                        revision,
                        _clean_text(fact.get("operation_id"), 120),
                        _clean_text(previous["created_at"], 80)
                        if previous is not None
                        else now,
                        now,
                    ),
                )
                superseded_count = 0
                if status == "active" and single_value:
                    async with db.execute(
                        """
                        SELECT id FROM portrait_facts
                        WHERE person_id=? AND dimension=?
                          AND source_scope=? AND status='active' AND id!=?
                        """,
                        (person_id, dimension, source_scope, fact_id),
                    ) as cur:
                        srows = await cur.fetchall()
                    superseded_ids = [_clean_text(r["id"], 120) for r in srows]
                    superseded_count = len(superseded_ids)
                    if superseded_ids:
                        marks = ",".join("?" for _ in superseded_ids)
                        await db.execute(
                            f"""
                            UPDATE portrait_facts
                            SET status='superseded', supersedes_id=?,
                                revision=revision+1, operation_id=?, updated_at=?
                            WHERE id IN ({marks})
                            """,
                            [
                                fact_id,
                                _clean_text(fact.get("operation_id"), 120),
                                now,
                                *superseded_ids,
                            ],
                        )
                        await db.execute(
                            f"""
                            UPDATE portrait_learning_queue
                            SET state='superseded', updated_at=?
                            WHERE fact_id IN ({marks}) AND state='pending'
                            """,
                            [now, *superseded_ids],
                        )
                portrait_revision = await self._bump_revision(db, person_id)
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return {
            "ok": True,
            "code": "portrait_fact_upserted",
            "created": previous is None,
            "fact_id": fact_id,
            "portrait_revision": portrait_revision,
            "superseded": superseded_count,
        }

    async def _suppressed(
        self, db, person_id: str, dimension: str, claim_hash: str, scope: str
    ) -> bool:
        async with db.execute(
            """
            SELECT 1 FROM portrait_suppressions
            WHERE person_id=? AND dimension=? AND normalized_claim_hash=?
              AND status IN ('active', 'reconfirmation_pending')
              AND (scope='' OR scope=?)
              AND (expires_at='' OR expires_at>?)
            LIMIT 1
            """,
            (person_id, dimension, claim_hash, scope, _utc_now()),
        ) as cur:
            return await cur.fetchone() is not None

    async def _bump_revision(self, db, person_id: str) -> int:
        await db.execute(
            "UPDATE portrait_people SET portrait_revision=portrait_revision+1, "
            "updated_at=? WHERE person_id=?",
            (_utc_now(), person_id),
        )
        async with db.execute(
            "SELECT portrait_revision FROM portrait_people WHERE person_id=?",
            (person_id,),
        ) as cur:
            row = await cur.fetchone()
        return int(row["portrait_revision"] or 0) if row is not None else 0

    # ------------------------------------------------------------------
    # learning queue + nightly batch (pure counting; "可能" prefix)
    # ------------------------------------------------------------------

    async def enqueue_learning(
        self, *, person_id: str, fact_id: str, evidence_hash: str
    ) -> dict[str, Any]:
        person_id = _clean_text(person_id, 80)
        fact_id = _clean_text(fact_id, 120)
        evidence_hash = _clean_text(evidence_hash, 80)
        if not person_id or not fact_id or not _HASH64.fullmatch(evidence_hash):
            return {"ok": False, "code": "portrait_queue_invalid"}
        queue_id = f"portrait_queue_{_stable_fingerprint(person_id, fact_id, evidence_hash)[:24]}"
        now = _utc_now()
        async with self._write_lock:
            async with self._connect() as db:
                cur = await db.execute(
                    """
                    INSERT OR IGNORE INTO portrait_learning_queue(
                        queue_id, person_id, fact_id, evidence_hash, state,
                        created_at, updated_at)
                    VALUES(?,?,?,?, 'pending', ?, ?)
                    """,
                    (queue_id, person_id, fact_id, evidence_hash, now, now),
                )
                await db.commit()
                created = bool(cur.rowcount)
        return {
            "ok": True,
            "code": "portrait_queued",
            "created": created,
            "queue_id": queue_id,
        }

    async def list_pending_people(self, *, limit: int = 500) -> list[str]:
        async with self._connect() as db:
            async with db.execute(
                """
                SELECT DISTINCT q.person_id
                FROM portrait_learning_queue q
                JOIN portrait_people p ON p.person_id=q.person_id
                WHERE q.state='pending' AND p.profile_status='active'
                ORDER BY q.updated_at ASC
                LIMIT ?
                """,
                (max(1, min(2000, int(limit))),),
            ) as cur:
                rows = await cur.fetchall()
        return [
            _clean_text(r["person_id"], 80)
            for r in rows
            if _clean_text(r["person_id"], 80)
        ]

    async def _distinct_statement_count(self, db, evidence_hashes: list[Any]) -> int:
        hashes = list(
            dict.fromkeys(
                _clean_text(item, 80)
                for item in evidence_hashes
                if _HASH64.fullmatch(_clean_text(item, 80))
            )
        )[:16]
        if not hashes:
            return 0
        marks = ",".join("?" for _ in hashes)
        async with db.execute(
            f"SELECT evidence_hash, statement_fingerprint FROM portrait_evidence "
            f"WHERE evidence_hash IN ({marks})",
            hashes,
        ) as cur:
            rows = await cur.fetchall()
        fingerprints = {
            _clean_text(r["statement_fingerprint"], 80)
            or _clean_text(r["evidence_hash"], 80)
            for r in rows
            if _clean_text(r["statement_fingerprint"], 80)
            or _clean_text(r["evidence_hash"], 80)
        }
        return len(fingerprints)

    async def _scope_capability(
        self, db, person_id: str, source_scope: str, *, legacy_scope: str = ""
    ) -> dict[str, Any]:
        source_scope = _clean_text(source_scope, 80)
        async with db.execute(
            "SELECT capability_summary FROM portrait_scope_capabilities "
            "WHERE person_id=? AND source_scope=?",
            (person_id, source_scope),
        ) as cur:
            row = await cur.fetchone()
        if row is not None:
            value = _json_loads(row["capability_summary"], {})
            return value if isinstance(value, dict) else {}
        if source_scope == "private" or source_scope.startswith("group:"):
            async with db.execute(
                "SELECT capability_summary FROM portrait_people WHERE person_id=?",
                (person_id,),
            ) as cur:
                person = await cur.fetchone()
            value = (
                _json_loads(person["capability_summary"], {})
                if person is not None
                else {}
            )
            return value if isinstance(value, dict) else {}
        return {}

    async def run_daily_batch(
        self,
        *,
        person_id: str,
        run_day: str,
        min_independent_evidence: int = 3,
        success_limit: int = 1,
        attempt_limit: int = 2,
    ) -> dict[str, Any]:
        """Nightly promotion for one person (MC _run_portrait_daily_batch_sync).

        Cap check, attempt record and promotion form one critical section:
        asyncio.Lock is not reentrant, so the body calls the *_locked
        helpers directly. A second concurrent batch must never pass the
        per-day caps on pre-promotion counters (MC holds its lock across
        the same span).
        """
        async with self._write_lock:
            return await self._run_daily_batch_locked(
                person_id=person_id,
                run_day=run_day,
                min_independent_evidence=min_independent_evidence,
                success_limit=success_limit,
                attempt_limit=attempt_limit,
            )

    async def _run_daily_batch_locked(
        self,
        *,
        person_id: str,
        run_day: str,
        min_independent_evidence: int = 3,
        success_limit: int = 1,
        attempt_limit: int = 2,
    ) -> dict[str, Any]:
        """Batch body; the caller must hold _write_lock."""
        person_id = _clean_text(person_id, 80)
        run_day = _clean_text(run_day, 16)
        if not person_id or not _DAY_RE.fullmatch(run_day):
            return {"ok": False, "code": "invalid_request"}
        min_independent_evidence = max(1, min(16, int(min_independent_evidence)))
        success_limit = max(1, min(4, int(success_limit)))
        attempt_limit = max(success_limit, min(8, int(attempt_limit)))
        now = _utc_now()
        async with self._connect() as db:
            async with db.execute(
                "SELECT * FROM portrait_people WHERE person_id=?", (person_id,)
            ) as cur:
                person = await cur.fetchone()
            if person is None:
                return {"ok": False, "code": "bridge_unavailable"}
            async with db.execute(
                "SELECT * FROM portrait_daily_runs WHERE person_id=? AND run_day=?",
                (person_id, run_day),
            ) as cur:
                run = await cur.fetchone()
            attempts = int(run["attempts"] or 0) if run is not None else 0
            successes = int(run["successes"] or 0) if run is not None else 0
            if successes >= success_limit or attempts >= attempt_limit:
                return {
                    "ok": False,
                    "code": "portrait_daily_limit",
                    "attempts": attempts,
                    "successes": successes,
                }
            async with db.execute(
                """
                SELECT DISTINCT f.source_scope
                FROM portrait_learning_queue q
                JOIN portrait_facts f ON f.id=q.fact_id
                WHERE q.person_id=? AND q.state='pending' AND f.status='active'
                """,
                (person_id,),
            ) as cur:
                pending_scopes = await cur.fetchall()
            learning_allowed = False
            for prow in pending_scopes:
                scope_text = _clean_text(prow["source_scope"], 80)
                caps = await self._scope_capability(
                    db,
                    person_id,
                    scope_text,
                    legacy_scope="private" if scope_text == "private" else "",
                )
                if bool(caps.get("portrait_learning_enabled")):
                    learning_allowed = True
                    break
            if not learning_allowed:
                return {"ok": False, "code": "portrait_learning_disabled"}
            await db.execute(
                """
                INSERT INTO portrait_daily_runs(person_id, run_day, attempts,
                    successes, last_code, updated_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(person_id, run_day) DO UPDATE SET
                    attempts=excluded.attempts, updated_at=excluded.updated_at
                """,
                (
                    person_id,
                    run_day,
                    attempts + 1,
                    successes,
                    "portrait_insufficient_evidence",
                    now,
                ),
            )
            await db.commit()
            async with db.execute(
                """
                SELECT q.queue_id, q.fact_id, f.* FROM portrait_learning_queue q
                JOIN portrait_facts f ON f.id=q.fact_id
                WHERE q.person_id=? AND q.state='pending' AND f.status='active'
                  AND f.portrait_tier='base'
                ORDER BY q.created_at ASC
                """,
                (person_id,),
            ) as cur:
                rows = await cur.fetchall()
            created = 0
            for row in rows:
                row_scope = _clean_text(row["source_scope"], 80)
                row_caps = await self._scope_capability(
                    db,
                    person_id,
                    row_scope,
                    legacy_scope="private" if row_scope == "private" else "",
                )
                if not bool(row_caps.get("portrait_learning_enabled")):
                    continue
                evidence_hashes = _json_loads(row["evidence_hashes"], [])
                if (
                    await self._distinct_statement_count(db, evidence_hashes)
                    < min_independent_evidence
                ):
                    continue
                if await self._suppressed(
                    db,
                    person_id,
                    row["dimension"],
                    row["normalized_claim_hash"],
                    row["source_scope"],
                ):
                    await db.execute(
                        "UPDATE portrait_learning_queue SET state='suppressed', "
                        "updated_at=? WHERE person_id=? AND fact_id=? AND state='pending'",
                        (now, person_id, row["fact_id"]),
                    )
                    await db.commit()
                    continue
                inferred = {
                    "person_id": person_id,
                    "dimension": row["dimension"],
                    "normalized_claim_hash": row["normalized_claim_hash"],
                    "claim_summary": _clean_text(f"可能{row['claim_summary']}", 180),
                    "portrait_tier": "intelligent",
                    "producer_kind": "daily_evidence_batch",
                    "producer_version": "req036.batch.v1",
                    "derivation_kind": "independent_evidence_aggregate",
                    "epistemic_status": "inferred",
                    "source_scope": row["source_scope"],
                    "usable_scope": "self_low_global"
                    if row_scope == "private" or row_scope.startswith("private@")
                    else "source_only",
                    "confidence": min(0.95, max(0.75, float(row["confidence"] or 0.0))),
                    "sensitivity": row["sensitivity"],
                    "evidence_hashes": evidence_hashes,
                    "context_refs": _json_loads(row["context_refs"], []),
                    "operation_id": f"portrait.daily:{person_id[-12:]}:{run_day}",
                }
                # _write_lock is already held by run_daily_batch (not
                # reentrant), so the locked variant is called directly.
                result = await self._upsert_fact_locked(inferred)
                if result.get("ok"):
                    async with self._connect() as db2:
                        await db2.execute(
                            "UPDATE portrait_learning_queue SET state='processed', "
                            "updated_at=? WHERE person_id=? AND fact_id=? "
                            "AND state='pending'",
                            (now, person_id, row["fact_id"]),
                        )
                        await db2.commit()
                    created += 1
                    break
            successes += 1 if created else 0
            code = (
                "portrait_fact_upserted"
                if created
                else "portrait_insufficient_evidence"
            )
            await db.execute(
                "UPDATE portrait_daily_runs SET successes=?, last_code=?, "
                "updated_at=? WHERE person_id=? AND run_day=?",
                (successes, code, now, person_id, run_day),
            )
            await db.commit()
        return {
            "ok": bool(created),
            "code": code,
            "attempts": attempts + 1,
            "successes": successes,
            "created": created,
        }

    # ------------------------------------------------------------------
    # summary reads (fail-closed multi-gate)
    # ------------------------------------------------------------------

    @staticmethod
    def _timestamp_is_fresh(value: Any, freshness_days: int) -> bool:
        text = _clean_text(value, 80)
        if not text:
            return False
        try:
            observed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            return False
        age = datetime.now(timezone.utc) - observed.astimezone(timezone.utc)
        return age.total_seconds() <= max(1, freshness_days) * 86400

    @staticmethod
    def _scope_persona(value: Any) -> str:
        parts = str(value or "").split("@")
        if len(parts) != 3 or len(parts[1]) != 16:
            return ""
        return parts[1]

    def _scope_allows_row(
        self, row: aiosqlite.Row, requested_scope: str, legacy_scope: str = ""
    ) -> bool:
        usable_scope = _clean_text(row["usable_scope"], 80)
        source_scope = _clean_text(row["source_scope"], 80)
        if usable_scope == "self_low_global":
            source_persona = self._scope_persona(source_scope)
            requested_persona = self._scope_persona(requested_scope)
            return not source_persona or (
                bool(requested_persona) and source_persona == requested_persona
            )
        return (
            usable_scope == "source_only"
            and bool(requested_scope)
            and source_scope in {requested_scope, legacy_scope}
        )

    async def summary(
        self,
        person_id: str,
        *,
        scope: str = "",
        legacy_scope: str = "",
        limit: int = 8,
        low_only: bool = True,
        usage_min_confidence: float = 0.75,
        inferred_freshness_days: int = 90,
    ) -> dict[str, Any]:
        person_id = _clean_text(person_id, 80)
        scope = _clean_text(scope, 80)
        legacy_scope = _clean_text(legacy_scope, 80)
        confidence_floor = max(0.0, min(1.0, float(usage_min_confidence or 0.75)))
        freshness_days = max(1, min(3650, int(inferred_freshness_days or 90)))
        async with self._connect() as db:
            async with db.execute(
                "SELECT * FROM portrait_people WHERE person_id=?", (person_id,)
            ) as cur:
                person = await cur.fetchone()
            if person is None:
                return {
                    "ok": False,
                    "code": "bridge_unavailable",
                    "items": [],
                    "portrait_revision": 0,
                }
            if _clean_text(person["profile_status"], 40) != "active" or _clean_text(
                person["identity_assurance"], 40
            ) not in {"observed", "verified", "explicit_linked"}:
                return {
                    "ok": False,
                    "code": "bridge_person_mismatch",
                    "items": [],
                    "portrait_revision": int(person["portrait_revision"] or 0),
                }
            capabilities = await self._scope_capability(
                db, person_id, scope, legacy_scope=legacy_scope
            )
            if not bool(capabilities.get("portrait_usage_enabled")):
                return {
                    "ok": False,
                    "code": "portrait_usage_disabled",
                    "items": [],
                    "portrait_revision": int(person["portrait_revision"] or 0),
                }
            query = (
                "SELECT * FROM portrait_facts "
                "WHERE person_id=? AND status='active' "
                "AND producer_version!='req036.rule.v1'"
            )
            params: list[Any] = [person_id]
            if low_only:
                query += " AND sensitivity='low'"
            query += " ORDER BY confidence DESC, updated_at DESC LIMIT ?"
            params.append(max(16, min(128, int(limit) * 8)))
            async with db.execute(query, params) as cur:
                rows = await cur.fetchall()
            items: list[dict[str, Any]] = []
            for row in rows:
                if not self._scope_allows_row(row, scope, legacy_scope):
                    continue
                if await self._suppressed(
                    db,
                    person_id,
                    row["dimension"],
                    row["normalized_claim_hash"],
                    row["source_scope"],
                ):
                    continue
                confidence = float(row["confidence"] or 0)
                if confidence < confidence_floor:
                    continue
                if row[
                    "portrait_tier"
                ] == "intelligent" and not self._timestamp_is_fresh(
                    row["updated_at"], freshness_days
                ):
                    continue
                items.append(
                    {
                        "dimension": row["dimension"],
                        "summary": row["claim_summary"],
                        "portrait_tier": row["portrait_tier"],
                        "epistemic_status": row["epistemic_status"],
                        "confidence": confidence,
                        "sensitivity": row["sensitivity"],
                        "usable_scope": row["usable_scope"],
                        "updated_at": row["updated_at"],
                    }
                )
                if len(items) >= max(1, min(32, int(limit))):
                    break
        return {
            "ok": True,
            "code": "profile_exact",
            "items": items,
            "portrait_revision": int(person["portrait_revision"] or 0),
            "last_synced_at": _clean_text(person["last_synced_at"], 80),
        }

    # ------------------------------------------------------------------
    # suppressions (user denial — never silently reopened)
    # ------------------------------------------------------------------

    async def upsert_suppression(self, marker: dict[str, Any]) -> dict[str, Any]:
        key = _clean_text(marker.get("suppression_key"), 80)
        person_id = _clean_text(marker.get("person_id"), 80)
        status = _clean_text(marker.get("status"), 40) or "active"
        operation_id = _clean_text(marker.get("operation_id"), 120)
        if (
            not key
            or not person_id
            or not operation_id
            or status
            not in {
                "active",
                "reconfirmation_pending",
                "revoked",
                "superseded",
                "expired",
            }
        ):
            return {"ok": False, "code": "suppression_invalid"}
        now = _utc_now()
        async with self._write_lock:
            async with self._connect() as db:
                async with db.execute(
                    "SELECT * FROM portrait_suppressions WHERE suppression_key=?",
                    (key,),
                ) as cur:
                    previous = await cur.fetchone()
                if (
                    previous is not None
                    and _clean_text(previous["operation_id"], 120) == operation_id
                ):
                    return {
                        "ok": True,
                        "code": "suppression_idempotent_replay",
                        "revision": int(previous["revision"] or 1),
                    }
                revision = (
                    int(previous["revision"] or 0) + 1 if previous is not None else 1
                )
                await db.execute(
                    """
                    INSERT INTO portrait_suppressions(
                        suppression_key, person_id, dimension, normalized_claim_hash,
                        scope, reason, actor, status, origin_identity_key,
                        operation_id, revision, created_at, updated_at, expires_at,
                        revoked_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(suppression_key) DO UPDATE SET
                        reason=excluded.reason, actor=excluded.actor,
                        status=excluded.status, operation_id=excluded.operation_id,
                        revision=excluded.revision, updated_at=excluded.updated_at,
                        expires_at=excluded.expires_at, revoked_at=excluded.revoked_at
                    """,
                    (
                        key,
                        person_id,
                        _clean_text(marker.get("dimension"), 80),
                        _clean_text(marker.get("normalized_claim_hash"), 80),
                        _clean_text(marker.get("scope"), 80),
                        _clean_text(marker.get("reason"), 80),
                        _clean_text(marker.get("actor"), 80),
                        status,
                        _clean_text(marker.get("origin_identity_key"), 96),
                        operation_id,
                        revision,
                        now
                        if previous is None
                        else _clean_text(marker.get("created_at"), 80) or now,
                        now,
                        _clean_text(marker.get("expires_at"), 80),
                        _clean_text(marker.get("revoked_at"), 80),
                    ),
                )
                await self._bump_revision(db, person_id)
                await db.commit()
        return {"ok": True, "code": "suppression_upserted", "revision": revision}

    # ------------------------------------------------------------------
    # maintenance
    # ------------------------------------------------------------------

    @staticmethod
    def new_operation_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:16]}"
