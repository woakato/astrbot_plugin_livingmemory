# -*- coding: utf-8 -*-
"""跨插件共享合同：Bot 个人归档 + 日程分级（陪伴分支）

⚠️  这个文件在两个插件里**各有一份，必须逐字相同**：
        陪伴插件：astrbot_plugin_private_companion/bot_personal_contract.py
        记忆插件：astrbot_plugin_memory_companion/core/bot_personal_contract.py
    权威副本在 peiban/doc/shared/bot_personal_contract.py。

    两个插件是独立的 AstrBot 插件包，互相不能 import，所以「共享常量」只能靠
    「同一份文件复制两份 + 启动时指纹互校」来保证。任何改动必须同时满足三条：

        ① 两侧同一次提交（不允许一侧先合）
        ② 升 CONTRACT_REVISION
        ③ 重算并更新 CONTRACT_FINGERPRINT（python bot_personal_contract.py 会打印）

    只依赖标准库。不要在这里 import 任何插件内部模块，否则复制到对侧会炸。

修改规则与背景见：peiban/doc/跨插件共享合同.md
"""
from __future__ import annotations

import hashlib
import json

CONTRACT_NAME = "bot_personal_archive"
CONTRACT_REVISION = 3

# ---------------------------------------------------------------------------
# 一、日程分级（原「四段」→ 陪伴分支五段）
#
# 这是**唯一定义源**。两侧任何地方需要窗口划分，都必须从这张表派生，不允许再写
# 第二份硬编码——OPS 分支就是因为存在四份互不派生的副本，改一处改不动全部。
#
# 边界与原版已持久化的主动配额桶（user["proactive_daypart_counts"]，
# constants.py:489）完全对齐，所以「日程窗口」与「主动配额桶」共享切点，
# 但**仍是两套独立存储**：配额桶是计数器，窗口是快照/归档的维度，不要合并。
#
# 字段：(slug, 中文名, 起始分钟, 结束分钟)。结束分钟 <= 起始分钟表示跨午夜。
# ---------------------------------------------------------------------------
SCHEDULE_WINDOWS: tuple[tuple[str, str, int, int], ...] = (
    ("late_night", "深夜", 21 * 60, 6 * 60),        # 21:00 - 次日 06:00
    ("morning",    "早晨",  6 * 60, 11 * 60),       # 06:00 - 11:00
    ("noon",       "中午", 11 * 60, 14 * 60 + 30),  # 11:00 - 14:30
    ("afternoon",  "下午", 14 * 60 + 30, 18 * 60),  # 14:30 - 18:00
    ("evening",    "晚上", 18 * 60, 21 * 60),       # 18:00 - 21:00
)

WINDOW_SLUGS: tuple[str, ...] = tuple(item[0] for item in SCHEDULE_WINDOWS)
WINDOW_NAMES: tuple[str, ...] = tuple(item[1] for item in SCHEDULE_WINDOWS)
WINDOW_SLUG_BY_NAME: dict[str, str] = {item[1]: item[0] for item in SCHEDULE_WINDOWS}
WINDOW_NAME_BY_SLUG: dict[str, str] = {item[0]: item[1] for item in SCHEDULE_WINDOWS}

# 记忆侧 validate_bot_personal_envelope 用它做**硬校验**：不在集合里 → invalid_window，
# 归档静默失败。所以它必须由上表派生，不能手写。
BOT_PERSONAL_WINDOWS = frozenset(WINDOW_SLUGS)

# LLM 与用户口语里的窗口别名 → slug。只做输入归一，不参与判定。
WINDOW_ALIASES: dict[str, str] = {
    "深夜": "late_night", "凌晨": "late_night", "半夜": "late_night", "夜里": "late_night",
    "早晨": "morning", "早上": "morning", "清晨": "morning", "上午": "morning",
    "中午": "noon", "正午": "noon", "午间": "noon", "晌午": "noon",
    "下午": "afternoon",
    "晚上": "evening", "傍晚": "evening", "晚间": "evening",
}

# 旧四段（OPS 22/06/12/18）→ 新五段的迁移映射。
# 值为 None 表示「靠窗口名判不出来，必须看时间戳」，见 migrate_legacy_window()。
LEGACY_WINDOW_MIGRATION: dict[str, str | None] = {
    "late_night": "late_night",  # 旧 22:00-06:00 ⊂ 新 21:00-06:00，可直接沿用
    "morning": None,             # 旧 06:00-12:00 → 新 morning(06-11) + noon(11-12)
    "afternoon": None,           # 旧 12:00-18:00 → 新 noon(12-14:30) + afternoon(14:30-18)
    "evening": None,             # 旧 18:00-22:00 → 新 evening(18-21) + late_night(21-22)
}


def window_for_minutes(minutes: int) -> str:
    """把「当日分钟数」归入窗口 slug。跨午夜的窗口拆成两截判定。

    这是**唯一判定实现**。带日期的判定（跨午夜时 window_date 归属哪一天）留在
    陪伴侧 agenda_contracts.window_for_datetime，但它必须调用本函数取窗口名，
    不允许自己再判一次阈值。
    """
    try:
        value = int(minutes) % (24 * 60)
    except (TypeError, ValueError):
        return ""
    for slug, _name, start, end in SCHEDULE_WINDOWS:
        if start < end:
            if start <= value < end:
                return slug
        elif value >= start or value < end:
            return slug
    return ""


def normalize_window(value: object) -> str:
    """把窗口名/别名/slug 归一成 slug；无法识别返回空串（**不要猜**）。"""
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered in BOT_PERSONAL_WINDOWS:
        return lowered
    return WINDOW_ALIASES.get(text, "")


def migrate_legacy_window(old_window: object, minutes: object = None) -> str:
    """旧四段窗口值 → 新五段 slug。

    拿得到当日分钟数就按时间重算；拿不到且旧值本身不唯一对应新值时返回空串，
    调用方应当**按旧名保留、只读不重算**，不要猜——猜错会让历史快照的窗口归属
    和它内部的时间戳自相矛盾。
    """
    old = normalize_window(old_window) or str(old_window or "").strip().lower()
    if minutes is not None:
        recomputed = window_for_minutes(minutes)
        if recomputed:
            return recomputed
    return LEGACY_WINDOW_MIGRATION.get(old) or ""


# ---------------------------------------------------------------------------
# 二、Bot 个人归档域
#
# memory_domain 保持 OPS 时期的取值 "bot_self_schedule"：它描述的是「Bot 自有
# 日程域」，不含运维语义，改名要同步两侧 7 处 + 存量数据迁移，收益为零。
# ---------------------------------------------------------------------------
BOT_PERSONAL_MEMORY_DOMAIN = "bot_self_schedule"
BOT_PERSONAL_SUBJECT = "bot_self"
BOT_PERSONAL_SESSION_ID = "bot_personal"
BOT_PERSONAL_SCOPE = "private"
BOT_PERSONAL_VISIBILITY = "bot_self"
BOT_PERSONAL_MAX_PAYLOAD_BYTES = 16 * 1024

# payload 结构本身未变 → 保持 1.0。
BOT_PERSONAL_PAYLOAD_SCHEMA_VERSION = "1.0"
# 信封字段 window 的**值域**变了（四段→五段）→ 能力版本必升，且必须在能力探测里
# 回传 windows，让调用方启动时就能发现两侧值域不一致，而不是等到归档 invalid_window。
BOT_PERSONAL_CAPABILITY_SCHEMA_VERSION = "1.3"

# Canonical C3 agenda capability surface.  These values are deliberately
# duplicated in the memory-side contract so startup negotiation can reject a
# stale reader before it promotes archived payloads to facts.
BOT_PERSONAL_CANONICAL_SCHEMA_VERSION = 3
BOT_PERSONAL_LEGACY_CANONICAL_SCHEMA_VERSIONS: tuple[int, ...] = (1, 2)
BOT_PERSONAL_CANONICAL_FIELDS: tuple[str, ...] = (
    "owner_bot_id",
    "persona_id",
    "source_kind",
    "status",
    "temporal_phase",
    "evidence_kind",
    "evidence_level",
    "canonical_evidence_level",
    "archive_evidence_level",
    "evidence_level_mapping",
    "authority_kind",
    "commitment_level",
    "epistemic_status",
    "content_granularity",
    "materialization_state",
    "fact_eligibility",
    "source_refs",
    "runtime_origin_refs",
    "expires_at",
    "actor_type",
    "subject_actor_id",
    "object_actor_id",
    "source_actor_id",
    "target_user_id",
    "participant_roles",
    "decision_trace",
)
BOT_PERSONAL_CANONICAL_SOURCE_KINDS: tuple[str, ...] = ("planned", "observed", "projection", "reconciled")
BOT_PERSONAL_CANONICAL_STATUSES: tuple[str, ...] = (
    "planned", "active", "completed", "partially_completed", "overridden",
    "reconciled", "deferred", "cancelled", "unknown",
)
BOT_PERSONAL_CANONICAL_EVIDENCE_KINDS: tuple[str, ...] = (
    "none", "interaction", "self_state_commit", "tool_action",
    "external_record", "external_commitment",
)
BOT_PERSONAL_CANONICAL_FACT_ELIGIBILITIES: tuple[str, ...] = (
    "none", "schedule_commitment", "current_internal", "current_observed", "history_observed",
)

BOT_PERSONAL_MEMORY_TYPES: tuple[str, ...] = (
    "bot_schedule_plan",
    "bot_observed_activity",
    "bot_schedule_reconciliation",
    "bot_window_snapshot",
    "bot_daily_diary",
    "bot_creative_work",
    "bot_media_memory",
    "bot_subjective_memory",
    "bot_shared_activity",
    "bot_detail_fragment",
    "bot_calendar_event",
    "bot_proactive_message",
)

# memory_type → (source_kind, 默认 evidence_level, 默认 status)。
# 记忆侧一律用合同 source_kind 覆盖调用方上报值，所以两侧必须同表，
# 否则创作/梦境/日历会被标成「真实观察」。
TYPE_CONTRACTS: dict[str, tuple[str, str, str]] = {
    "bot_schedule_plan": ("planned", "L0", "planned"),
    "bot_observed_activity": ("observed", "L2", "active"),
    "bot_schedule_reconciliation": ("reconciled", "L3", "completed"),
    "bot_window_snapshot": ("projection", "L2", "completed"),
    "bot_daily_diary": ("subjective", "L1", "completed"),
    "bot_creative_work": ("creative", "L1", "active"),
    "bot_media_memory": ("media", "L1", "active"),
    "bot_subjective_memory": ("subjective", "L1", "active"),
    "bot_shared_activity": ("shared", "L1", "active"),
    "bot_detail_fragment": ("detail", "L1", "active"),
    "bot_calendar_event": ("calendar", "L1", "active"),
    "bot_proactive_message": ("proactive", "L1", "active"),
}

EVIDENCE_LEVELS: tuple[str, ...] = ("L0", "L1", "L2", "L3")

# 幂等键前缀表。归档链路每条写入都要落在这些前缀之一下，方便对账与去重排查。
# ⚠️ agenda_snapshot / reconciliation 两类的 id 内部含窗口 slug，改分级后新旧 key
#    不同源：**旧 key 不重写，新记录用新 slug**（见迁移策略）。
IDEMPOTENCY_KEY_PREFIXES: tuple[str, ...] = (
    "agenda_snapshot",   # agenda_snapshot:{snapshot_id}        含窗口 slug
    "reconciliation",    # reconciliation:{reconciliation_id}   含窗口 slug
    "observed",          # observed:{activity_id}
    "daily_plan",        # daily_plan:{date}
    "detail",            # detail:{date}:{start}:{end}
    "diary",             # diary:{date}
    "calendar",          # calendar:{event_id}:{action}
    "proactive",         # proactive:{user_id}:{action}:{content[:80]}
    "creative",          # creative:{project_id|title}:{chunks}
    "photo",             # photo:{media_id}
    "daily_outfit",      # daily_outfit:{date}
    "dream",             # dream:{text[:120]}:{mood}
    "qzone",             # qzone:{tid|content[:100]}            ← 娱乐能力，陪伴分支保留
)


def capability_descriptor(*, available: bool = True, read_only: bool = False) -> dict[str, object]:
    """记忆侧 probe_bot_personal_memory_capabilities 的标准返回骨架。

    windows / memory_types / contract_fingerprint 三个字段是给陪伴侧做启动自检用的：
    对不上就说明两侧合同文件漂移了，应当直接告警并停用归档，而不是带着不一致跑。
    """
    return {
        "available": bool(available),
        "read_only": bool(read_only),
        "memory_domain": BOT_PERSONAL_MEMORY_DOMAIN,
        "contract_name": CONTRACT_NAME,
        "contract_revision": CONTRACT_REVISION,
        "contract_fingerprint": CONTRACT_FINGERPRINT,
        "capability_schema_version": BOT_PERSONAL_CAPABILITY_SCHEMA_VERSION,
        "canonical_schema_version": BOT_PERSONAL_CANONICAL_SCHEMA_VERSION,
        "legacy_canonical_schema_versions": list(BOT_PERSONAL_LEGACY_CANONICAL_SCHEMA_VERSIONS),
        "canonical_fields": list(BOT_PERSONAL_CANONICAL_FIELDS),
        "canonical_source_kinds": list(BOT_PERSONAL_CANONICAL_SOURCE_KINDS),
        "canonical_statuses": list(BOT_PERSONAL_CANONICAL_STATUSES),
        "canonical_evidence_kinds": list(BOT_PERSONAL_CANONICAL_EVIDENCE_KINDS),
        "canonical_fact_eligibilities": list(BOT_PERSONAL_CANONICAL_FACT_ELIGIBILITIES),
        "payload_schema_version": BOT_PERSONAL_PAYLOAD_SCHEMA_VERSION,
        "windows": list(WINDOW_SLUGS),
        "memory_types": list(BOT_PERSONAL_MEMORY_TYPES),
        "max_payload_bytes": BOT_PERSONAL_MAX_PAYLOAD_BYTES,
        "warnings": [],
    }


def _fingerprint_source() -> str:
    """参与指纹的只有**跨插件必须一致**的值。注释、别名表、辅助函数不参与——
    别名只影响本地输入归一，两侧不一致不会造成数据不一致。"""
    canonical = {
        "contract_name": CONTRACT_NAME,
        "contract_revision": CONTRACT_REVISION,
        "windows": [[slug, name, start, end] for slug, name, start, end in SCHEDULE_WINDOWS],
        "memory_domain": BOT_PERSONAL_MEMORY_DOMAIN,
        "subject": BOT_PERSONAL_SUBJECT,
        "session_id": BOT_PERSONAL_SESSION_ID,
        "scope": BOT_PERSONAL_SCOPE,
        "visibility": BOT_PERSONAL_VISIBILITY,
        "max_payload_bytes": BOT_PERSONAL_MAX_PAYLOAD_BYTES,
        "payload_schema_version": BOT_PERSONAL_PAYLOAD_SCHEMA_VERSION,
        "capability_schema_version": BOT_PERSONAL_CAPABILITY_SCHEMA_VERSION,
        "canonical_schema_version": BOT_PERSONAL_CANONICAL_SCHEMA_VERSION,
        "legacy_canonical_schema_versions": list(BOT_PERSONAL_LEGACY_CANONICAL_SCHEMA_VERSIONS),
        "canonical_fields": list(BOT_PERSONAL_CANONICAL_FIELDS),
        "canonical_source_kinds": list(BOT_PERSONAL_CANONICAL_SOURCE_KINDS),
        "canonical_statuses": list(BOT_PERSONAL_CANONICAL_STATUSES),
        "canonical_evidence_kinds": list(BOT_PERSONAL_CANONICAL_EVIDENCE_KINDS),
        "canonical_fact_eligibilities": list(BOT_PERSONAL_CANONICAL_FACT_ELIGIBILITIES),
        "memory_types": list(BOT_PERSONAL_MEMORY_TYPES),
        "type_contracts": {k: list(v) for k, v in TYPE_CONTRACTS.items()},
        "evidence_levels": list(EVIDENCE_LEVELS),
        "idempotency_key_prefixes": list(IDEMPOTENCY_KEY_PREFIXES),
    }
    return json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_contract_fingerprint() -> str:
    return hashlib.sha256(_fingerprint_source().encode("utf-8")).hexdigest()[:16]


# 由 compute_contract_fingerprint() 生成。改了上面任何常量都要重跑本文件更新它。
CONTRACT_FINGERPRINT = "ecf1d69406a8445d"


def contract_self_check() -> list[str]:
    """两侧启动时各自跑一遍，再互相比对 CONTRACT_FINGERPRINT。"""
    problems: list[str] = []
    if CONTRACT_FINGERPRINT != compute_contract_fingerprint():
        problems.append("contract_fingerprint_stale: 常量改过但没重算指纹")
    if len(set(WINDOW_SLUGS)) != len(WINDOW_SLUGS):
        problems.append("duplicate_window_slug")
    if set(TYPE_CONTRACTS) != set(BOT_PERSONAL_MEMORY_TYPES):
        problems.append("type_contracts_out_of_sync")
    covered = {window_for_minutes(m) for m in range(0, 24 * 60)}
    if covered != set(WINDOW_SLUGS):
        problems.append(f"window_coverage_gap: 未覆盖或多余 {covered ^ set(WINDOW_SLUGS)}")
    for alias, slug in WINDOW_ALIASES.items():
        if slug not in BOT_PERSONAL_WINDOWS:
            problems.append(f"alias_points_to_unknown_window: {alias}->{slug}")
    return problems


if __name__ == "__main__":  # pragma: no cover
    print("CONTRACT_FINGERPRINT =", compute_contract_fingerprint())
    issues = contract_self_check()
    print("self_check:", "OK" if not issues else issues)
