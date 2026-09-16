"""SQLite-backed storage for companion bridge data.

Two tables serve the private_companion compatibility bridge:

- ``companion_events``: time-window indexed schedule / diary / archive
  entries mirrored from the companion plugin. Queried by day (fast-context)
  instead of semantic search.
- ``emotion_ledger``: emotion afterglow events with a delivery state machine
  (pending -> delivered -> acked). Kept separate from memory atoms because
  delivery acknowledgement semantics do not fit the atom lifecycle.

Follows the short-connection pattern of ``atom_store.py`` (each operation
opens its own aiosqlite connection with WAL + busy_timeout).
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import aiosqlite

# Acked afterglow rows older than this are cleared by the weekly task.
_EMOTION_RETENTION_SECONDS = 7 * 24 * 3600

# Mirror MC's afterglow half-life default for events without explicit expiry.
_DEFAULT_AFTERGLOW_TTL_SECONDS = 24 * 3600


def to_epoch_seconds(value: Any) -> float:
    """Normalize an MC-style timestamp (ISO text or epoch number) to float seconds.

    Args:
        value: ISO8601 text, numeric seconds (int/float/str), or None.

    Returns:
        Epoch seconds; current time when value is missing or unparsable.
    """
    if value is None or value == "":
        return time.time()
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return time.time()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return time.time()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def event_date_of(occurred_at: float) -> str:
    """Return the YYYY-MM-DD date string for an epoch-seconds timestamp."""
    return datetime.fromtimestamp(occurred_at, tz=timezone.utc).strftime("%Y-%m-%d")


class CompanionStore:
    """Persist companion mirrored events and the emotion delivery ledger."""

    def __init__(self, db_path: str):
        self.db_path = db_path

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
        """Create companion tables (idempotent, no migration version bump)."""
        async with self._connect() as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS companion_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    bot_id TEXT NOT NULL DEFAULT '',
                    persona_id TEXT NOT NULL DEFAULT '',
                    scope TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL DEFAULT '',
                    group_id TEXT NOT NULL DEFAULT '',
                    event_date TEXT NOT NULL,
                    window_slug TEXT NOT NULL DEFAULT '',
                    occurred_at REAL NOT NULL,
                    importance REAL NOT NULL DEFAULT 0.5,
                    archived INTEGER NOT NULL DEFAULT 0,
                    metadata TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_ce_date ON companion_events(event_date)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_ce_bot_kind "
                "ON companion_events(bot_id, kind, event_date)"
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS emotion_ledger (
                    event_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 1,
                    event_type TEXT NOT NULL,
                    intensity REAL NOT NULL DEFAULT 0.0,
                    confidence REAL NOT NULL DEFAULT 0.0,
                    energy_delta REAL NOT NULL DEFAULT 0.0,
                    valence REAL NOT NULL DEFAULT 0.0,
                    arousal REAL NOT NULL DEFAULT 0.0,
                    vulnerability REAL NOT NULL DEFAULT 0.0,
                    producer_plugin TEXT NOT NULL DEFAULT '',
                    origin_kind TEXT NOT NULL DEFAULT 'interaction',
                    bot_id TEXT NOT NULL DEFAULT '',
                    scope TEXT NOT NULL DEFAULT 'private',
                    platform TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    quoted_target_ref TEXT NOT NULL DEFAULT '',
                    dedupe_key TEXT NOT NULL DEFAULT '',
                    payload_hash TEXT NOT NULL DEFAULT '',
                    occurred_at REAL NOT NULL,
                    expires_at REAL NOT NULL DEFAULT 0.0,
                    delivery_state TEXT NOT NULL DEFAULT 'pending',
                    consumer_id TEXT NOT NULL DEFAULT '',
                    acked_at REAL NOT NULL DEFAULT 0.0,
                    distilled INTEGER NOT NULL DEFAULT 0,
                    modulation TEXT NOT NULL DEFAULT '{}',
                    raw_event TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_el_delivery "
                "ON emotion_ledger(delivery_state, expires_at)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_el_domain "
                "ON emotion_ledger(scope, user_id, session_id)"
            )
            await db.commit()

    # ------------------------------------------------------------------
    # companion_events
    # ------------------------------------------------------------------

    @staticmethod
    def make_event_key(kind: str, bot_id: str, scope: str, session_id: str,
                       subject_id: str, content: str, occurred_at: float) -> str:
        """Domain-separated dedupe key so companion retries never double-write."""
        digest = uuid.uuid5(
            uuid.NAMESPACE_URL,
            "lmce:v1:"
            + ":".join(
                (kind, bot_id, scope, session_id, subject_id,
                 content[:200], f"{to_epoch_seconds(occurred_at):.0f}")
            ),
        ).hex
        return f"lmce_{digest[:24]}"

    async def upsert_event(self, *, kind: str, content: str, bot_id: str = "",
                           persona_id: str = "", scope: str = "", session_id: str = "",
                           user_id: str = "", group_id: str = "",
                           window_slug: str = "", occurred_at: Any = None,
                           importance: float = 0.5, metadata: dict | None = None,
                           event_key: str = "") -> tuple[str, bool]:
        """Insert one mirrored companion event with dedupe protection.

        Args:
            event_key: optional caller-provided stable key (companion archive
                retries carry their own idempotency key); generated when empty.

        Returns:
            (event_key, deduplicated) — deduplicated=True means the event was
            already stored and nothing changed.
        """
        ts = to_epoch_seconds(occurred_at)
        key = event_key or self.make_event_key(
            kind, bot_id, scope, session_id, user_id or group_id, content, ts
        )
        async with self._connect() as db:
            cursor = await db.execute(
                """
                INSERT INTO companion_events
                    (event_key, kind, content, bot_id, persona_id, scope, session_id,
                     user_id, group_id, event_date, window_slug, occurred_at,
                     importance, metadata)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(event_key) DO NOTHING
                """,
                (
                    key, kind, content[:4000], bot_id, persona_id, scope, session_id,
                    user_id, group_id, event_date_of(ts), window_slug, ts, importance,
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
            await db.commit()
            return key, cursor.rowcount == 0

    async def list_events_between(self, *, bot_id: str = "", kind: str = "",
                                  kinds: tuple[str, ...] | None = None,
                                  session_id: str = "",
                                  since_date: str = "", until_date: str = "",
                                  limit: int = 20) -> list[dict[str, Any]]:
        """Day-window query for fast contexts and the self-line slot."""
        sql = "SELECT * FROM companion_events WHERE archived = 0"
        params: list[Any] = []
        if bot_id:
            sql += " AND bot_id = ?"
            params.append(bot_id)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        if since_date:
            sql += " AND event_date >= ?"
            params.append(since_date)
        if until_date:
            sql += " AND event_date <= ?"
            params.append(until_date)
        sql += " ORDER BY occurred_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 200)))
        async with self._connect() as db:
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.get("metadata") or "{}")
            except (json.JSONDecodeError, TypeError):
                item["metadata"] = {}
            out.append(item)
        return out

    async def purge_proactive_between(self, *, bot_id: str, since_date: str,
                                      before_occurred_at: float) -> int:
        """Soft-remove proactive-message noise older than a boundary (clean_proactive_history)."""
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE companion_events SET archived = 1 "
                "WHERE kind = 'proactive' AND bot_id = ? AND event_date >= ? "
                "AND occurred_at < ? AND archived = 0",
                (bot_id, since_date, before_occurred_at),
            )
            await db.commit()
            return cursor.rowcount

    # ------------------------------------------------------------------
    # emotion_ledger
    # ------------------------------------------------------------------

    async def record_emotion(self, event: dict[str, Any], *, context: dict[str, str]) -> dict[str, Any]:
        """Upsert one normalized emotion event, then return the ledger view.

        Args:
            event: normalized emotion event dict (contract output).
            context: attested domain fields (bot_id/scope/platform/user_id/session_id).

        Returns:
            The stored event dict including delivery_state.
        """
        event_id = str(event.get("event_id") or "")
        occurred = to_epoch_seconds(event.get("occurred_at"))
        expires = to_epoch_seconds(event.get("expires_at")) if event.get("expires_at") else (
            occurred + _DEFAULT_AFTERGLOW_TTL_SECONDS
        )
        modulation = {
            "schema_version": "affect_modulation.v1",
            "valence": float(event.get("valence_hint") or 0.0),
            "arousal": float(event.get("arousal_hint") or 0.0),
            "vulnerability": float(event.get("vulnerability_hint") or 0.0),
            "confidence": float(event.get("confidence") or 0.0),
            "source_event_ids": [event_id] if event_id else [],
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }
        async with self._connect() as db:
            async with db.execute(
                "SELECT revision, delivery_state FROM emotion_ledger WHERE event_id = ?",
                (event_id,),
            ) as cursor:
                old = await cursor.fetchone()
            revision = int((old["revision"] if old else 0) or 0) + 1 if old else int(event.get("revision") or 1)
            # A correction (revision bump) restarts delivery.
            state = "pending"
            await db.execute(
                """
                INSERT INTO emotion_ledger
                    (event_id, trace_id, revision, event_type, intensity, confidence,
                     energy_delta, valence, arousal, vulnerability, producer_plugin,
                     origin_kind, bot_id, scope, platform, user_id, session_id,
                     quoted_target_ref, dedupe_key, payload_hash, occurred_at, expires_at,
                     delivery_state, consumer_id, acked_at, distilled, modulation, raw_event)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(event_id) DO UPDATE SET
                    revision=excluded.revision, event_type=excluded.event_type,
                    intensity=excluded.intensity, confidence=excluded.confidence,
                    energy_delta=excluded.energy_delta, valence=excluded.valence,
                    arousal=excluded.arousal, vulnerability=excluded.vulnerability,
                    expires_at=excluded.expires_at, occurred_at=excluded.occurred_at,
                    delivery_state='pending', consumer_id='', acked_at=0.0,
                    modulation=excluded.modulation, raw_event=excluded.raw_event
                """,
                (
                    event_id, event.get("trace_id") or "", revision,
                    event.get("event_type") or "neutral",
                    float(event.get("intensity") or 0.0), float(event.get("confidence") or 0.0),
                    float(event.get("applied_energy_delta") or 0.0),
                    float(event.get("valence_hint") or 0.0), float(event.get("arousal_hint") or 0.0),
                    float(event.get("vulnerability_hint") or 0.0),
                    event.get("producer_plugin") or "unknown", event.get("origin_kind") or "interaction",
                    context.get("bot_id", ""), context.get("scope", "private"),
                    context.get("platform", ""), context.get("user_id", ""),
                    context.get("session_id", ""),
                    (event.get("quoted_target_ref") or {}).get("id", "") if isinstance(event.get("quoted_target_ref"), dict) else "",
                    event.get("dedupe_key") or "", event.get("payload_hash") or "",
                    occurred, expires, state, "", 0.0, 0,
                    json.dumps(modulation, ensure_ascii=False),
                    json.dumps(event, ensure_ascii=False, default=str),
                ),
            )
            await db.commit()
        stored = dict(event)
        stored["revision"] = revision
        stored["delivery_state"] = state
        return stored

    async def list_deliverable(self, *, scope: str, platform: str, user_id: str,
                               session_id: str, allow_cross_window: bool = False,
                               limit: int = 10) -> list[dict[str, Any]]:
        """Return afterglow events pending delivery for a private session domain.

        Args:
            scope/platform/user_id/session_id: attested delivery context.
            allow_cross_window: when False, only exact session matches deliver.
            limit: max events per page.

        Returns:
            Delivery-shaped event dicts.
        """
        now = time.time()
        sql = (
            "SELECT * FROM emotion_ledger WHERE delivery_state != 'acked' "
            "AND scope = ? AND (expires_at <= 0 OR expires_at > ?)"
        )
        params: list[Any] = [scope, now]
        if allow_cross_window:
            sql += " AND platform = ? AND user_id = ?"
            params.extend([platform, user_id])
        else:
            sql += " AND session_id = ?"
            params.append(session_id)
        sql += " ORDER BY occurred_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 50)))
        async with self._connect() as db:
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
        out = []
        for row in rows:
            data = dict(row)
            try:
                modulation = json.loads(data.get("modulation") or "{}")
            except (json.JSONDecodeError, TypeError):
                modulation = {}
            out.append({
                "event_id": data["event_id"],
                "revision": data["revision"],
                "trace_id": data["trace_id"],
                "event_type": data["event_type"],
                "intensity": data["intensity"],
                "confidence": data["confidence"],
                "energy_delta": data["energy_delta"],
                "valence": data["valence"],
                "arousal": data["arousal"],
                "vulnerability": data["vulnerability"],
                "occurred_at": datetime.fromtimestamp(data["occurred_at"], tz=timezone.utc).isoformat(),
                "expires_at": (
                    datetime.fromtimestamp(data["expires_at"], tz=timezone.utc).isoformat()
                    if data["expires_at"] > 0 else ""
                ),
                "affect_modulation": modulation,
            })
            # Mark delivered so repeated polling does not redeliver forever.
        if out:
            async with self._connect() as db:
                ids = [item["event_id"] for item in out]
                marks = ",".join("?" * len(ids))
                await db.execute(
                    f"UPDATE emotion_ledger SET delivery_state = 'delivered' "
                    f"WHERE event_id IN ({marks})",
                    ids,
                )
                await db.commit()
        return out

    async def ack(self, refs: list[dict[str, Any]], *, consumer_id: str) -> int:
        """Acknowledge delivered events; returns acked count."""
        acked = 0
        now = time.time()
        async with self._connect() as db:
            for ref in refs[:64]:
                event_id = str(ref.get("event_id") or "")
                if not event_id:
                    continue
                revision = ref.get("revision")
                if revision is None:
                    cursor = await db.execute(
                        "UPDATE emotion_ledger SET delivery_state='acked', acked_at=?, "
                        "consumer_id=? WHERE event_id=? AND delivery_state='delivered'",
                        (now, consumer_id, event_id),
                    )
                else:
                    cursor = await db.execute(
                        "UPDATE emotion_ledger SET delivery_state='acked', acked_at=?, "
                        "consumer_id=? WHERE event_id=? AND revision=? "
                        "AND delivery_state='delivered'",
                        (now, consumer_id, event_id, int(revision)),
                    )
                acked += cursor.rowcount
            await db.commit()
        return acked

    async def peek_pending(self, *, bot_id: str = "", session_id: str = "",
                           user_id: str = "", scope: str = "", platform: str = "",
                           allow_cross_window: bool = False,
                           limit: int = 3) -> list[dict[str, Any]]:
        """Read undelivered afterglow WITHOUT consuming it (main-chain slot).

        The delivery marking belongs to the bridge's list_emotion_events
        (companion plugin); the injection self-line must stay read-only.
        """
        now = time.time()
        sql = ("SELECT event_type, occurred_at FROM emotion_ledger "
               "WHERE delivery_state='pending' AND (expires_at <= 0 OR expires_at > ?)")
        params: list[Any] = [now]
        if bot_id:
            sql += " AND bot_id = ?"
            params.append(bot_id)
        if scope:
            sql += " AND scope = ?"
            params.append(scope)
        if allow_cross_window and user_id:
            sql += " AND platform = ? AND user_id = ?"
            params.extend([platform, user_id])
        elif session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        sql += " ORDER BY occurred_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 10)))
        async with self._connect() as db:
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def interaction_stats(self, *, bot_id: str = "", scope: str = "",
                                platform: str = "", user_id: str = "",
                                since_ts: float) -> dict[str, int]:
        """Warm/scar event counts since a timestamp, for the relationship line."""
        sql = ("SELECT event_type, COUNT(*) AS n FROM emotion_ledger "
               "WHERE occurred_at >= ?")
        params: list[Any] = [since_ts]
        if bot_id:
            sql += " AND bot_id = ?"
            params.append(bot_id)
        if scope:
            sql += " AND scope = ?"
            params.append(scope)
        if platform:
            sql += " AND platform = ?"
            params.append(platform)
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " GROUP BY event_type"
        async with self._connect() as db:
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
        counts = {str(r["event_type"]): int(r["n"]) for r in rows}
        return {
            "warm": counts.get("warm_memory", 0) + counts.get("praise", 0),
            "intimate": counts.get("intimacy", 0),
            "scar": counts.get("scar_touched", 0) + counts.get("hurt", 0),
            "total": sum(counts.values()),
        }

    async def list_undistilled_acked(self, limit: int = 50) -> list[dict[str, Any]]:
        """Acked events awaiting the weekly distillation task."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT * FROM emotion_ledger WHERE delivery_state='acked' "
                "AND distilled=0 AND expires_at > 0 AND expires_at < ? "
                "ORDER BY acked_at ASC LIMIT ?",
                (time.time(), max(1, min(int(limit), 200))),
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def mark_distilled(self, event_ids: list[str]) -> None:
        if not event_ids:
            return
        async with self._connect() as db:
            marks = ",".join("?" * len(event_ids))
            await db.execute(
                f"UPDATE emotion_ledger SET distilled=1 WHERE event_id IN ({marks})",
                event_ids,
            )
            await db.commit()

    async def purge_expired(self) -> int:
        """Drop acked events past retention, and expired undelivered leftovers."""
        now = time.time()
        async with self._connect() as db:
            cursor = await db.execute(
                "DELETE FROM emotion_ledger WHERE "
                "(delivery_state='acked' AND acked_at < ?) "
                "OR (delivery_state != 'acked' AND expires_at > 0 AND expires_at < ?)",
                (now - _EMOTION_RETENTION_SECONDS, now - _EMOTION_RETENTION_SECONDS),
            )
            await db.commit()
            return cursor.rowcount
