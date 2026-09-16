# Companion Fork Spec — LivingMemory 3.0（LM 内核 · MC 桥兼容）

> 本文档是实施规格的唯一事实源。任何新会话/压缩后接力，读本文档即可无损继续。
> 调研来源：MC v1.10.5 源码逐行核对 + PC memory_companion_adapter.py 消费方式核对 + LM 2.7.0-beta.1 扩展点核对。
> 备份：原始 LM 在 `data/plugins_backup_livingmemory_v2.7.0/`。

## 0. 已定决策

| 决策点 | 结论 |
|---|---|
| 身份 | 目录/内部名保持 `astrbot_plugin_livingmemory`，version→3.0.0。仅 metadata `display_name` 改为 `我会牢牢记住你`（命中 PC 别名表，见 §3）。AstrBot 视其为 LM 版本升级。 |
| 原 MC | 禁用 + 目录改 `astrbot_plugin_memory_companion.disabled`。PC 官方冲突表把"双记忆插件同开"标为 high，必须唯一注入方。 |
| 画像 | 默认关（`portrait.enabled=false`）。桥+七件套验收后才开。先"仅直接证据、禁性格推断"模式。 |
| 数据搬家 | MC 库实测 44 表全 0 行（plugin_data 2.1MB 全是 WAL+schema）。迁移器出范围，入 backlog。 |
| 数据分诊 | 互动流水→conversations.db messages；日程/日记/归档→新表 companion_events；情绪→新表 emotion_ledger；局面线索（event.private_companion_context）→只用于改写检索词，不留档。 |
| 核心记忆落点 | documents 层（metadata.memory_type=CORE_MEMORY），不进 atom（躲 `_prepare_atom_for_insert` 的 TTL 覆写，atom_store.py:160-170 无条件用 now 重算 created_at/expires_at）。 |
| 情绪不并入原子 | 投递是状态机（pending→delivered→acked），原子模型无签收语义；超时丢数据不可接受。 |
| 桥的纪律 | 写全异步（离开请求路径）；compose_context 内部零 LLM 调用、预算 1.0s（PC 侧超时 1.2s）；备份默认开。 |

## 1. 总体架构

```
PC（不改）──鸭子类型找插件──▶ 本 fork（显示名命中别名表）
   │ probe_bot_personal_memory_capabilities() 握手（契约指纹比对）
   │ compose_context(query,...) ──▶ 闸门+search_memories+装箱 生成器（与主链注入同一代码）
   │ record_*(...) ──▶ 幂等键去重 ──▶ 分诊：messages / companion_events / emotion_ledger
   │ 情绪六方法 ──▶ emotion_ledger 签收账本
   ▼
LM 引擎（FAISS+FTS+图谱+总结+衰减，全复用）
新增：core/companion/（bridge, contracts/, gate, composer, portrait）+ storage/companion_store.py
```

## 2. 桥接口规格（按 MC v1.10.5 钉死）

入口发现：PC 优先 `context.get_all_stars()` 找 star → 实例上探测（顺序）：类/实例方法 `get_active_bridge` / `get_memory_companion_bridge`，属性 `memory_companion` / `memory_companion_bridge` / `bridge` / `_ACTIVE_BRIDGE`。命中后每次取缓存都会调 `bridge_lifecycle_status()`。缓存：命中 30s / 缺失 2s。

### 2.1 握手（必须，不匹配则 PC 置 bridge=None）

`probe_bot_personal_memory_capabilities()` -> dict（同步）。PC 比对项（`_memory_companion_probe_capabilities`，adapter L932-1034），逐项**全等**（list 顺序敏感）：

- contract_fingerprint == "ecf1d69406a8445d"；contract_revision == 3；capability_schema_version == "1.3"；payload_schema_version == "1.0"；canonical_schema_version == 3
- memory_domain == "bot_self_schedule"（回退键 domain）
- windows == ["late_night","morning","noon","afternoon","evening"]
- memory_types == 12 项按序：bot_schedule_plan, bot_observed_activity, bot_schedule_reconciliation, bot_window_snapshot, bot_daily_diary, bot_creative_work, bot_media_memory, bot_subjective_memory, bot_shared_activity, bot_detail_fragment, bot_calendar_event, bot_proactive_message
- available is True
- 降级兼容路：legacy v2（指纹 0ffe3a1ab69b659c/rev2/1.2/canon2）或 超集（mismatch ⊆ {fingerprint,windows,memory_types} 且 PC 期望集 ⊆ 观测集且观测严格更大）。**v1 走全等路径即可，不申报超集。**

返回值须含的键（PC 与 outbox 消费）：available/state("ready")/degraded/pending/read_only/contract_name("bot_personal_archive")/contract_revision/contract_fingerprint/capability_schema_version/canonical_schema_version/payload_schema_version/canonical_fields/canonical_source_kinds/canonical_statuses/canonical_evidence_kinds/canonical_fact_eligibilities/memory_domain/max_payload_bytes(16384)/windows/memory_types/profiles/legacy_profiles/methods/warnings/error_code/legacy_state/capability_state/p5({"state":"unprobed","error_code":"p5_status_not_probed"})/negotiated 相关由 PC 回填。

`bridge_lifecycle_status()` -> {"active": bool}（仅此一键；terminate 后 active=False + token 轮换使已发 capability 失效）。

### 2.2 compose_context（读路径，async，返回 str）

```python
async def compose_context(*, query="", session_context=None, top_k=None, max_chars=None,
    companion_bot_mood="", companion_bot_energy=0.0, retrieval_profile="",
    p5_attestation=None, p5_attestation_consumer=None) -> str
```

- session_context 接受 dict，键：session_id/scope/platform/user_id/user_name/preferred_address/preferred_address_locked/bot_id/message_id/group_id/group_name/persona_id/strict_session_only/topic_fit_policy。
- retrieval_profile ∈ {"schedule_fast","outfit_fast"}（PC 仅在 coordination_status 对应布尔为 true 时才传；受 companion_bridge 开关控制）。命中走时间窗口快查（companion_events + 近期 messages），否则走统一生成器。
- 返回文本外层用 `<MemoryCompanion-Context>` 包裹（沿用 MC 头尾，PC 判废/过滤规则按它写）。
- **空结果判废协议**：返回含原句 `没有检索到足够相关的长期记忆；只依据当前用户消息回复。` 且全串 `\n- ` 计数 ≤1 时 PC 丢弃。正常结果则避免该原句与裸 "- " 行结构。
- PC 侧处理：str().strip()、过滤 provider 错误行、截断 text[:max(300, max_chars)]（feature 版 [:max(240, min(1800, max_chars))]）。

### 2.3 record_*（写路径，全 async 返回 str memory_id；PC 忽略返回值只 catch 异常）

`record_event` 完整签名（L881）：content, memory_type="external_event", scope="unknown", session_id, platform, message_id, group_id, subject:dict{kind,id,name,role}, object, visibility="bot_self", sayability="direct", reality_level="bot_action", lifecycle="stable_memory", confidence=0.85, importance=0.5, review_status="auto", tags:list, metadata:dict, source_plugin="external", memory_id="", occurred_at=""。

包装默认值（照抄）：record_bot_action(self_action/bot_self/bot_action)；record_persona_life(persona_life/bot_self/persona_life/sayability=indirect)；record_proactive_message(proactive_message/bot_self/bot_action/tags=[proactive,bot_action]/imp=0.55)；record_visible_turn(role,content,...)→ 透传语义（PC 传 role="assistant"、source、metadata、occurred_at）；record_creative_work(creative_work/bot_self/fictional_content/tags=[creative_work]/imp=0.72)；record_qzone_action(qzone_action/bot_self/bot_action/tags=[qzone,bot_action]/imp=0.58；**PC 缺此方法时降级调 record_persona_life 并显式传 memory_type=qzone_action**——两个都要有)。

`record_bot_personal_archive(envelope, *, producer_capability=None, producer_context=None) -> dict`，固定六键：`{"ok","record_id","deduplicated","version","error_code","state"}`，state ∈ sent/deduplicated/forbidden/invalid/degraded。校验：capability 无效→forbidden+error_code=producer_capability_required；DTO 构建失败→invalid；producer 对象须提供 `_memory_companion_bridge_bot_id()`/`_memory_companion_archive_persona_id()` 且与 DTO owner_bot_id/persona_id 一致否则 forbidden/producer_namespace_mismatch。PC outbox 归一：state ∈ {invalid,version_conflict,stale_version}→dead-letter，非 ok→retry；**非 dict 返回会被视为 retry**。

**幂等**：全部 record_* 按域分离哈希键（移植 MC `memory_atom.scoped_canonical_key`：sha1(owner+platform+scope+subject+object+memory_type) 的 `mav2:<domain24>:<semantic40>` 形态）+ (content_fingerprint, occurred_at) 去重，命中则返回既有 id 且记 deduplicated=true。PC 投喂失败会重试，无去重必双写。

### 2.4 情绪六方法

- `register_emotion_producer(producer) -> capability|None`（别名 register_private_companion/register_bot_personal_producer）。校验（L601-646）：bridge active；producer 命中某 StarMetadata 的 star_cls 实例（`type(instance) is cls`）；activated is True；root_dir_name == "astrbot_plugin_private_companion" 或 name ∈ {"PrivateCompanion","private_companion"}。**不许名字兜底**。返回 frozen 不透明 capability（带 token，terminate 轮换失效）。
- `create_emotion_producer_context(capability, *, bot_id, scope, platform, user_id, session_id) -> ctx|None`；`create_emotion_delivery_context(..., consumer_id="private_companion.daily_state", allow_cross_window=False)`。域校验（L683-703）：五项非空、scope=="private"、session_id 以 f"{platform}:" 开头；delivery 要求 consumer_id 恰为 "private_companion.daily_state"、allow_cross_window 严格 bool（True 需配置 cross_window_emotional_continuity_enabled，默认 false）。
- `record_emotion_event(event, *, producer_context=None) -> dict`：无 context → {"ok":False,"state":"forbidden","read_only":False,"event_id":"","error_code":"producer_context_required"}。有效则 attested 覆盖 producer_plugin="private_companion"、origin_kind∈{interaction,system_condition}（否则置 interaction）、bot_id/scope/platform/session_id/actor_ref/target_ref/quoted_target_ref 以 context 为准；upsert 返回 normalize 后完整 event dict。
- `list_emotion_events(*, delivery_context=None, cursor="", limit=10, **kw) -> dict`：形状（schema "emotion_afterglow_delivery.v1"）：`{"events":[{event_id,revision,trace_id,event_type,intensity,confidence,energy_delta,valence,arousal,vulnerability,occurred_at,expires_at,affect_modulation:{"schema_version":"affect_modulation.v1",valence,arousal,vulnerability,confidence,source_event_ids,computed_at}}],"next_cursor","has_more","state","read_only"}`；forbidden 形状见规格调研（read_only=True, events=[]）。
- `ack_emotion_events(event_refs, *, delivery_context=None) -> {"acked","consumer_id","acked_at"}`；refs=[{"event_id","revision"}]。PC 只在应用成功后调。
- PC 消费：afterglow 映射 event_type→mood（scar_touched→低落/warm_memory→微暖/vulnerable_resonance→柔软/其它→平稳），半衰 1800s，energy_delta clamp -8..5，intensity 0..100。
- 账本落库即 emotion_ledger；list=同私聊域+pending/delivered+未过期；ack 推进 delivered→acked 并记 acked_at/consumer_id；acked 且过 TTL(7 天) 由周任务清除。

### 2.5 可选面（实现即得功能，缺失 PC 自动降级）

- `search_open_loops(*, session_id="", limit=3) -> list[dict]`：每项 {memory_id,content≤300,session_id,occurred_at(ISO str),age_days(float|None),open_loop_weight,promise_weight,memory_reason≤200}。PC 读 content/age_days（fallback "约N天前"）。数据源：未闭环标记（§5.4）。
- `get_relationship_phase(...) -> {"phase","momentum",...}`；`peek_relationship_phase(...) -> {"observed","phase","momentum_band","touch_count"?}`。phase 白名单 {acquaintance,familiar,close,intimate,deeply_bonded}；不合格→{"observed":False,"phase":"unknown","momentum_band":"unknown"}。**v1 实现 peek/get 返回 observed:False 固定形状**；真估算入画像阶段。
- `should_defer_private_companion_section(section) -> bool`：主链注入开启且 dedupe_prompt_context 且 prefer_memory_companion_memory 且 section ∈ {self_timeline,private_context,livingmemory_guidance,companion_memory,dialogue_history} 时 True。**让位协议核心，v1 必做**（否则 PC 垫日程 + 我们注入包再垫一遍=双份）。PC 据此在 event 上写 `memory_companion_companion_deferred_sections: set[str]`。
- `coordination_status() -> dict`（11+键，形状照 MC setdefault available/state/degraded，含 schedule_fast_context/outfit_fast_context/bridge_enabled/memory_injection_enabled/dedupe_prompt_context/prefer_memory_companion_memory/clean_proactive_history/suppress_self_timeline_when_companion_seen/suppress_user_context_when_companion_seen）。PC 用它决定是否传 fast 档与回填 negotiated 信息。
- `get_token_usage_summary() -> dict`：非 dict 也可（PC 只 setdefault 三键）。返回 LM 侧分用途统计 + plugin_name/display_name 自报。
- **不实现**（PC 全部 getattr 缺失→降级）：REQ-041 scoped 八方法族 + probe/bind；consume_person/context/relationship_projection；read_bot_profile；read_user_memory_summary；create_user_memory_context。注意：consume_relationship_projection 真 MC 下 PC 以无 capability 调用恒 invalid，我们缺失同样落 invalid 语义，行为一致，无风险。

## 3. PC 别名表（adapter L80-90）与命中方式

`_MEMORY_COMPANION_PLUGIN_ALIASES` = {astrbot_plugin_memory_companion, astrbot_plugin_remember_you, memorycompanion, memory_companion, rememberyou, remember_you, 我会牢牢记住你}。
匹配（L518-533）：metadata 的 name/display_name/root_dir_name/module_path 任一命中，或模块 `__file__` 路径段命中。
→ 本 fork 只需 display_name=`我会牢牢记住你` 即命中。sys.modules 回退路只查 `...astrbot_plugin_memory_companion(.main)` 与 remember_you 模块名——不走回退（注册表正常时优先），无需担心。

## 4. 契约文件复制清单（字节级一致，CRLF 归一后）

| 文件 | 来源（MC） | 关键值 |
|---|---|---|
| bot_personal_contract.py | core/bot_personal_contract.py | REVISION=3, FINGERPRINT=ecf1d69406a8445d, CAP_SCHEMA="1.3", PAYLOAD="1.0", CANONICAL=3, LEGACY=(1,2), MAX_PAYLOAD_BYTES=16384, windows 边界 21:00/06:00/11:00/14:30/18:00, memory_types 12 项（§2.1 顺序） |
| emotion_event_contract.py | core/emotion_event_contract.py | 14 事件类型白名单、EMOTION_EVENT_CONTRACT_FINGERPRINT、字段集 |
| affect_modulation.py | core/affect_modulation.py | "affect_modulation.v1", fingerprint=sha256(fields)[:20] |

不复制：person_context_contract / scoped_domain_contract / namespace / namespace_capability / p6_four_package_manifest（绑定未实现的投影/REQ-041 面）。v1 不申报 scoped capability → PC negotiate 失败自动 local，无需文件。
PC 测试基线：test_c1_contract.py 字节比对 + 冻结断言——我们不动 PC，只是自证复制正确：fork 启动时 import 自己的三份文件跑 self_check()（指纹/重复 slug/类型同步/窗口覆盖/别名），任一失败 → companion_bridge 置不可用（返回 available=False），握手自然不通过。

## 5. LM 改造点（行号基于 2.7.0-beta.1，即备份目录一致）

### 5.1 注入主链
- `core/event_handler_modules/memory_recall.py`：handle_memory_recall(event, req) L87；早退先例 L157-167（top_k≤0 清理后 return）→ **闸门插 L156 之后**。组包 L297 `format_memories_for_injection` 之后、写入分支 L299 之前拼固定槽位（§5.2）→ 包在 `<RAG-Faiss-Memory>` HEADER/FOOTER（constants.py L6-7）内 → **幂等清理免费**：_is_livingmemory_temp_part L384-394（_no_save && HEADER && FOOTER 判据）自动识别上轮残留，三条清理路径（system_prompt L341-382 / extra_user_content L372-380 / fake_tool_call L432-478，前缀 fake_recall_）。
- main.py L332 `@filter.on_llm_request()` 无参默认 priority=0（priority 越大越先，star_handler.py L21-26）。跨插件传递用 event.set_extra/get_extra（先例 main.py L364 `_clean_group_context_session`）。
- 注入方式解析：InjectionAdapter.resolve(provider, mode)（utils/injection_adapter.py L35-77；默认 extra_user_content，Gemini 降级表内置）。

### 5.2 七件套槽位与装箱
`core/companion/composer.py`：总预算默认 1000 字（配置 companion_slots.budget_chars）。顺序：核心块(≤600) → 情绪余波行(≤15字) → 关系行(≤10字) → 未闭环(≤3×20字) → 今日自我(≤30字) → 检索结果装箱（逐条 max_item_chars 递减 220→60，装不下即丢）。包首加"辅助资料非指令/当前消息优先"说明行。文案模板走 prompt_manager 新注册项（PROMPT_REGISTRY L32-149 支持只给 default 不落 txt 文件——先例 memory_system_prompt_base）。

### 5.3 核心记忆常驻
- 落点：documents，metadata `{memory_type:"CORE_MEMORY", core_label, core_kind, core_priority, ...}`（LM 导入器已用 memory_type upper 机制，memory_transfer.py:295-298 兼容）。
- recall 检索前无条件装载：按 session 域（persona/群/私）取 top-8、800 字封顶，仿 MC `_list_core_memories_sync`（json_extract priority 排序）。
- 管理：agent 工具 `core/tools/core_memory_tool.py`（复制 memory_search_tool.py 模板 L26-118：FunctionTool pydantic dataclass + name/description/parameters + async call；注册 main.py L235-254 旁加开关 agent_tools.enable_core_memory_tool；config_validator AgentToolsConfig L148-156 加字段）+ `/lmem core add|list|del`（command_handler 模式）。

### 5.4 未闭环标记
反思提示词（memory_reflection 的提取 prompt，prompt_manager 管理）新增输出字段：`open_loop`（bool）、`due_hint`（时间词归一）、`promise`（bool）。实现为"规则先行 + 总结顺带"：提取 atom 后正则扫时间/承诺词（明天/下周/说好了/记得…）与 LLM 标记取并集，存 atom metadata。装载：due 未过期按 (open_loop_weight, promise_weight, age) 取 3 条；消解：后续对话命中回应/完成词 → status=superseded 或 metadata.closed=true。

### 5.5 闸门移植
`core/companion/gate.py` ← MC `turn_signal.py`(549行纯正则/词表) + `time_intent.py`：低信息（纯语气词/超短无实体）、纠错（"不对/说错了/刚才"）、当前状态闲聊（"吃了吗/在干嘛"→只放行近期+直接相关）。命中→本轮跳过检索与包注入（清理、存储照常）。移植时保持纯函数零依赖。

### 5.6 今日自我与让位
装载：companion_events 当日 kind∈{schedule,diary,archive} 压缩成一行（≤30字）。让位：`event.get_extra("memory_companion_companion_deferred_sections")` 含 self_timeline → 跳过该槽（对应 §2.5 defer 方法）。

### 5.7 重要原文
命中的 atom/doc 带 memory_sources 证据（memory_source.serialize_source_messages 机制）且属约定/时间敏感类（metadata 标记）→ 允许截断 60 字原话替转述进包。总结提示词硬规矩：时间/数字/专名不得意译。

## 6. 新存储：storage/companion_store.py

仿 atom_store.py（短连接 `_connect()` L31 模式、IF NOT EXISTS DDL、独立 CREATE INDEX）。持有：`memory_engine.initialize()` L211-212 atom_store 旁 `self.companion_store = CompanionStore(self.db_path); await initialize()`（同库 livingmemory.db，可 JOIN documents）。close 无需额外（仿 atom_store）。

```sql
CREATE TABLE IF NOT EXISTS companion_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,   -- schedule|diary|archive|creative|qzone|proactive
  content TEXT NOT NULL, bot_id TEXT DEFAULT '', persona_id TEXT DEFAULT '',
  scope TEXT DEFAULT '', session_id TEXT DEFAULT '',
  event_date TEXT NOT NULL,                                    -- YYYY-MM-DD（UTC 本地化日）
  window_slug TEXT DEFAULT '', occurred_at REAL NOT NULL,      -- epoch 秒（LM 全域秒制）
  dedupe_key TEXT NOT NULL UNIQUE, metadata TEXT DEFAULT '{}');
-- idx: (event_date), (bot_id, kind)
CREATE TABLE IF NOT EXISTS emotion_ledger (
  event_id TEXT PRIMARY KEY, revision INTEGER DEFAULT 1, trace_id TEXT DEFAULT '',
  event_type TEXT NOT NULL, intensity REAL, confidence REAL, energy_delta REAL,
  valence REAL, arousal REAL, vulnerability REAL,
  bot_id TEXT, scope TEXT, platform TEXT, user_id TEXT, session_id TEXT,
  quoted_target_ref TEXT DEFAULT '', origin_kind TEXT DEFAULT 'interaction',
  producer_plugin TEXT DEFAULT 'private_companion',
  modulation TEXT DEFAULT '{}',                                -- affect_modulation.v1 JSON
  occurred_at REAL, expires_at REAL,
  delivery_state TEXT DEFAULT 'pending',                        -- pending|delivered|acked
  consumer_id TEXT DEFAULT '', acked_at REAL,
  distilled INTEGER DEFAULT 0,                                  -- 周任务蒸馏标记
  metadata TEXT DEFAULT '{}');
-- idx: (delivery_state, expires_at), (scope, user_id, session_id)
```
日程按日快查直接 SELECT WHERE event_date BETWEEN（LM 全库无现成 BETWEEN 方法，write_ops `_get_recent_memory_results` L228-275 是扩参模板；messages 表索引 idx_msg_timestamp/idx_msg_session 已建好，未来补上界即可复用）。

## 7. 配置（_conf_schema.json 新段 + config_validator.py BaseModel 节；extra:allow 兜底、点号 get）

- `companion_bridge`: enabled(true), schedule_fast_context_enabled(true), outfit_fast_context_enabled(true), dedupe_prompt_context(true), prefer_memory_companion_memory(true), clean_proactive_history(true), suppress_self_timeline_when_companion_seen(true), suppress_user_context_when_companion_seen(true), cross_window_emotional_continuity_enabled(false), legacy_emotion_compatibility_enabled(true)
- `core_memory`: enabled(true), llm_management_enabled(true), max_blocks(8), max_chars(800)
- `companion_slots`: budget_chars(1000), gate_enabled(true), enable_core_block(true), enable_mood_line(true), enable_relationship_line(true), enable_open_loops(true), enable_self_line(true), enable_raw_quote(true)
- `portrait`: **enabled(false)**, direct_evidence_only(true), window_start(3), window_end(5), min_independent_evidence(2), daily_limit_per_person(1), max_inject(3)
- 每节加 `config_version` 字段（LM 无配置迁移机制，自管）。
- backup_settings 默认保持开（MC 教训）。

## 8. 时间/ID/编码约定

- 时间：LM 全域 REAL epoch **秒**；MC 是 ISO8601 UTC 秒文本。桥收到的 occurred_at（ISO 文本或秒数串）一律 `fromisoformat().timestamp()` 归一为秒；勿乘 1000。compose_context 返回内日期文本用本地日。
- ID：LM 全 INTEGER AUTOINCREMENT + documents 另有 TEXT doc_id(UUID)。companion 事件对外返回自造文本 id（`lmce_<uuid16>` / `lmee_<uuid16>`），映射表内列存，MC 的 source id 放 metadata.imported_from_id 模式（对称 MC 迁移器手法）。
- 中文路径 FAISS：plugin_initializer_faiss.py L102-145 已全局 monkey-patch（Windows 非 ASCII 经 Temp 桥接）；批量写走 `FaissVecDB.insert_batch`/`add_memory_entries_batch`（graph_vector_retriever.py:92），导入后对两个向量库 `get_async_persister(...).flush_now()`（3s 防抖+整文件重写，大批期间检索排队——预期内）。
- embedding 维度绑定当前 provider（initializer_faiss:159 自动重建），MC 向量永远不搬。

## 9. 验收清单

1. 握手：重启后 PC 联动状态页 available+state=ready、contract 匹配（PC 侧 integration_status 页可见）。
2. 读：PC 日程生成拉快上下文非空且 <1.2s；空结果被 PC 判废丢弃。
3. 写：PC 主动消息/说说/创作进 messages/companion_events；重复投喂命中 dedupe_key 不双写。
4. 情绪：私聊戳旧伤→30 分钟内 PC daily_state 出现 memory_afterglow 条件→ack 后不再重复投递。
5. 注入包：调试日志抽样——七槽齐全、≤预算、无跨轮残留（temp part 判据）、PC defer 时自我槽位让位。
6. 回归：/lmem status/search/summarize、Pages 面板、agent 召回工具全部正常；"吃了吗"被闸门抑制。
7. 画像（开启后一周）：≥1 条 active PORTRAIT_FACT、无敏感未审字面。

## 10. Backlog（明确不做）

MC→LM 数据迁移器（库空；映射表已存档：memories→documents via add_memory(preserve_create_time)、timeline→messages、relationship_edges→关系语句重抽、core_memory→CORE_MEMORY docs；跳过 FTS/embedding/injection_logs/batches/acl/emotion 短时效）；REQ-041 scoped 八法；consume_*_projection 三面；read_bot_profile/read_user_memory_summary；身份核验协议；记忆重建；跨窗口情绪连续性。

## 11. 风险登记

- display_name 改动使 AstrBot 列表显示"我会牢牢记住你"——用户知情（决策 §0）。
- LM 上游若发新版，fork 需手工 rebase 差异（备份目录保留对照；fork-dev 分支已挂 origin，上游 master 可 fetch 对比）。
- PC 升级若改契约（REVISION→4），握手 fail-closed 桥断开——升级 PC 前先看两侧契约版本。
- compose_context 1.0s 预算内含 FAISS 查询+装箱：embedding provider 慢时快档不受影响（时间索引纯 SQL），默认档检索超时→search_route 降级空结果→判废原句，符合协议。
- 上游 2.7.0-beta.1 存在若干文件级 ruff I001（imports 未排序）存量，本 fork 不顺手重排（控制 diff），仅保证自己触碰的文件干净。

## 12. 实施状态（fork-dev 分支，2026-09-16）

已完成并验证（本地测试 816/816 全绿（774 原有 + 30 companion 桥接套件 + 12 画像套件）；提交见 git log）：
- [x] Phase 0 身份：metadata version=3.0.0、display_name=我会牢牢记住你；main.py 模块级 get_active_bridge/get_memory_companion_bridge + 类上 staticmethod 钩子 + terminate 收口。
- [x] Phase 1a 契约：三份逐字复制（gitattributes 锁 LF；git blob==MC 原件 sha256 三方一致，验证过防 ruff 误改）。
- [x] Phase 1b 桥：core/companion/bridge.py——握手（模拟 PC 比较逻辑零 mismatch）、compose_context（判废句形状正确）、record_*（幂等键+异步落库）、情绪六法（fail-closed 域校验、peek 与 deliver 分离）、defer/coordination/open_loops/relationship 形状、terminate 令牌轮换。
- [x] Phase 2 存储：storage/companion_store.py（companion_events + emotion_ledger + peek_pending + interaction_stats），冒烟测试过状态机（投递/签收/修订重投/过期拒签）。
- [x] Phase 3a 闸门：core/companion/gate.py（纯规则移植，去作者私有词表；吃了吗→state_only、嗯嗯/贴贴/你说错了→抑制，测试过）。
- [x] Phase 3b 七件套主链：core/companion/composer.py（预算装箱+两出口包装，包在 <RAG-Faiss-Memory> 内免费幂等清理）+ core/companion/slots.py（核心/余波/关系/未闭环/今日自我+让位）+ memory_recall 接入（state_only 近期守卫、时间窗口提示行、重要原文 60 字、fake_tool_call 双通道兼容、legacy 回退开关 companion_slots.enable_package）。端到端模拟 8/8 通过。
- [x] Phase 3c 管理面：/lmem core list|add|del（原始整行自行解析避免逐词截断）+ manage_core_memory agent 工具（默认开）+ 三语后端 i18n + schema/validator 开关注册。
- [x] Phase 3d/4：core/companion/open_loop.py（纯规则承诺/待办/时间推断，含到期排序）；反思链打标；resolve_open_loops_by_text 关键词重叠消解（挂召回链）；close_stale_open_loops 45 天老化；CompanionMaintenanceScheduler（日频：purge_expired + 情绪蒸馏规则式零 LLM + 老化，finalize 接线 + stop/teardown 对称）。蒸馏/幂等/跳过行为验证过。
- [x] Phase 6：_conf_schema.json 新段（companion_bridge/core_memory/companion_slots/agent_tools 开关）+ en/ru 前端覆盖 + zh/en/ru 后端 core 命令文案 + 版本常量 3.0.0 三处同步（backup_manager/package.json/lock，测试强制）。

审计修复轮（两路子代理验收，逐条对源码复核后落地；本地测试 804/804 全绿）：
- [x] P1-1 核心记忆 INSERT 缺 doc_id（真 FAISS 表 NOT NULL+UNIQUE，探针复现 IntegrityError）→ 补 `core-{uuid4}`。
- [x] P1-2 defer 读取通道错位：PC 裸 setattr 在 event/req 上，原读 get_extra 永远落空 → getattr(event)→getattr(req)→get_extra 三级回退。
- [x] P1-3 load_core_memories fail-open：scope=session/persona/user 且当轮拿不到对应 id 时原会放行全部 → 改 fail-closed（缺域键即拒），global 不受影响。
- [x] P1-4 _TIME_MARK_RE 过宽（`[周天日]末?` 命中"今天天气"）→ 收窄为完整时间词；原文替代加 has_source 守卫，无源记忆不再空查 source 表。
- [x] P1-5 composer 预算虚高（只计正文，标签/换行/提示包裹漏算，实测 1000→1334）→ 全面改为按渲染段计费 + wrapper 预留 + 行级门禁 + 尾档 40；空结果判废句作为协议例外仍必发（保 PC `count("\n- ")<=1` 丢弃规则）。
- [x] P1-6 未闭环：显式"X月Y日"过期错判次月 → 跨年正确；"明天再说吧"误标 → 推迟语只认硬承诺；消解 overlap 泛词（明天/记得/结果…及其 n-gram 派生词）黑名单 + 至少 1 个话题实义词双条件。
- [x] BUG#1 archive 键用 salted `hash()`（跨进程不稳定→重启重复入库吞版本）→ sha256 稳定键 + companion_store.upsert_archive_event 版本阶梯（sent/deduplicated/version_conflict/stale_version，对齐 MC service.py:2070-2079；PC outbox 对 conflict/stale 走 dead-letter 语义保持）。
- [x] BUG#2 bot_id 格式分裂：桥写 `aiocqhttp:10001`（main.bot_id 带平台前缀），PC 查询/生产钩给裸 `10001` → `_canonical_bot_id` 统一裸 self_id（写入/查询/producer namespace 三处）。
- [x] BUG#3 情绪契约缺 `boundary_violation`（PC 白名单已含，属 MC1.10.5 后新增）→ contracts/emotion_event_contract.py 同步 PC 版（逐字节 diff 仅第 14 行，现与 PC 全等）；蒸馏短语表补"越界"条。指纹常量为 PC 模块内自算自比，跨插件不同步比对，握手无影响（bot_personal 指纹 ecf1d69406a8445d 三方一致不变）。
- [x] BUG#4 companion_bridge.enabled=false 不生效 → `_disabled()` 统一门（lifecycle+config）接入 probe/coordination/defer/compose/record/archive/emotion 全部公共面；main.__init__ 关闭时不发布 `_ACTIVE_BRIDGE`/`memory_companion_bridge`（PC 属性探测链 memory_companion→memory_companion_bridge→bridge→_ACTIVE_BRIDGE 全空即降级）。
- [x] P1-7 record_emotion 同 payload 重投重置 pending 导致反复余波 → payload_hash 相同且 revision 不升时保留现状态；revision 提升仍重启投递（有测试）。
- [x] P1-8 peek_pending session_id 空串时不加域过滤（跨会话泄漏余波）→ fail-closed 返回 []。
- [x] 杂项：companion_events 日期桶+两处"今天"查询统一 Asia/Shanghai（0-8 点错日）；维护调度改蒸馏先于清理（防 7 天保留边界丢蒸馏）；purge_proactive_between kind 修正为 proactive_message；schema 补 enable_package/state_guard_hours + en/ru 覆盖；except 日志全部带异常值。
- [x] 测试：tests/test_companion_bridge.py 30 例（doc_id/域隔离/预算硬顶 6 档/判废协议/归档版本阶梯/情绪状态机/peek 域/bot_id 归一/北京日桶/门控负形状/打标矩阵含跨年/消解泛词守卫）。

设计要点备忘：
- 核心记忆=documents status='core'：BM25/FAISS/validator 只认 'active'，天然隔离、零 embedding、永不重复命中。删除走专用 delete_core_memory。
- 蒸馏写入失败不标记 distilled（账本 7 天保留期内下次重试）；低强度/无映射直接标记跳过防重复扫。
- bridge 的 _record/_afterglow_line 全部只读或异步，关键路径零阻塞。

待办：
- [x] Phase 5 画像（见 §13，默认关；schema portrait 段已落地）。
- [ ] 联机验收：禁用 MC → 启用 LM 3.0 → 按 §9 清单逐项过 + §13 画像联机步骤。
- [ ] 上游更新时 rebase：git fetch origin && git log origin/master..fork-dev 对照。

## 13. Phase 5 用户画像（REQ-036，默认关）

### 13.1 研究结论（子代理全量走查，2026-09-17）

1. **画像管道零 LLM**：MC 的画像（portrait.py / profile_quality.py / store 画像区）完全是正则第一人称陈述提取 + SHA256 证据哈希 + 夜间批"独立表述计数"（≥min_independent_evidence 条去重后的不同表述才升级为 `可能×××` 推断层）。`portrait.token_budget_*` 配置是预留死键，无任何代码读取。"受制于模型能力"的原假设不成立；真实特性是召回保守（提不到就空，不会出坏数据）。
2. **PC 消费面承重方法只有一个**：`read_unified_profile_portrait(request, limit)`——缺失它 PC 会强制 `portrait_mode=disabled`（unified_profile_service.py:296-301）并失去称呼偏好覆盖、群自述回复、管理页预览。其余画像相关探针（`unified_profile_portrait_status`、`read_user_memory_summary`、`peek_relationship_phase`）均为管理页展示，可诚实降级。
3. **契约必须用 MC 版**（`unified_profile_contract.py`）：PC 构建画像读请求时会注入额外键 `namespace_context`，PC 版自带的 `validate_portrait_request` 是"恰好9键"严格校验会直接拒死，MC 版是宽容子集校验，与 PC 生产端兼容。两版指纹同为 `72067a45012a0588`（PC 侧比对的是常量值不是文件字节），握手不受影响。
4. **DTO 挂载时机**：PC 在消息事件阶段（message_pipeline.py:139 私聊 / main.py:21392 群聊唤醒轮）裸 `setattr(event, "private_companion_unified_profile_context", dto)`，先于 LM 的 on_llm_request——fork 用 getattr 优先+get_extra 兜底读取（与 defer 通道同一教训）。群聊**未唤醒**消息走 LM 被动捕获通道，运行于 custom_filter 阶段早于 PC 的组处理器，DTO 必不在——故画像采集统一放在召回处理器内（覆盖私聊+唤醒群聊轮），MC 自己同样只在请求路径采集（service.py:1166），语义对齐。
5. **REQ-041 命名空间不实现**：PC 只有 REQ-041 握手成功（`namespace_negotiated`）才会把 `namespace_context` 塞进画像请求；fork 不宣告该面，读请求就不会带命名空间，走官方 legacy 回退（MC 自己的 `portrait_namespace_legacy` 语义）。capture 侧同理：PC 挂了 DTO 但不带 namespace 上下文时 fork 按 `private` / `group:<platform>:<gid>` 原样落桶。

### 13.2 落地组成

- `core/companion/contracts/unified_profile_contract.py`、`contracts/sensitive_data.py`：逐字节拷贝（sha256 已验证一致；`.gitattributes` eol=lf 覆盖 contracts/*.py）。ruff 永不碰 contracts 目录。
- `core/companion/profile_quality.py`：MC 同名文件逐字节拷贝（纯 stdlib 规则提取器 rule_v2），但**位于 contracts 之外**——它是可改的移植件，不是握手契约。
- `core/companion/portrait_rules.py`：MC core/portrait.py 的规则移植（import 接线改为 fork 内路径 + 内联 5 行 clean_text；规则逻辑逐行等同）。
- `core/companion/portrait_service.py`：MC PortraitService 移植 + fork 的 getattr 优先 DTO 读取 + legacy-only 命名空间决策（注释说明 REQ-041 落地的重审点）。`resolve_turn_scope`/`spawn_portrait_capture` 为共享入口函数。
- `storage/portrait_store.py`：PortraitStore（7 张 MC 画像表，短连接 WAL 模式，asyncio.Lock 串行写）；夜间批在独立连接上跑 + busy_timeout 互不阻塞。
- 引擎接线：`memory_engine.portrait_store/portrait_service`，config 键 `portrait_enabled` + 扁平 `portrait` 段（finalize 映射，默认关）。
- 捕获钩子：`memory_recall._capture_portrait_turn`（消息入库后、闸门判断前；短文本/斜杠命令跳过，后台 spawn 不占请求路径）。
- 夜间批：`CompanionMaintenanceScheduler.run_once` 新增 `_run_portrait_batches`（pending 人群逐人 batch，日限自限流）。
- 桥面：`read_unified_profile_portrait` / `unified_profile_portrait_status` / `run_unified_profile_portrait_batch`（形状镜像 MC bridge.py:1387-1455，含 low 二层过滤；`_portrait_service()` 走统一 `_disabled()` 门）。

### 13.3 安全不变式（有测试锁定）

- 原文不落画像表：evidence 只存哈希（test_capture_stores_no_raw_text）；claim_summary 作为"结论"允许携带提取值（与 MC 一致）。
- 读路径全 fail-closed：无 DTO/无授权/非 active 身份/stale 修订/第三方可见目的/低敏之外/置信度低于 0.75/推断超 90 天/被压制——每一条独立否决（test_capture_requires_dto_and_grant、test_read_denies_*、test_suppression_blocks_write_and_read、test_usage_disabled_*）。
- 单值维度（叫我X/生日/职业…）新声明自动 supersede 旧值（test_single_value_dimension_supersedes_previous）。
- 任何凭据形状的串在写库前被 redact_sensitive_value 洗掉（拷贝件行为）。
- 每日批自限：每人的 attempt/success 上限落 portrait_daily_runs，重复调用只回 portrait_daily_limit（test_batch_requires_distinct_statements）。

### 13.4 已知降级面（v1 刻意不做）

- 群聊未唤醒消息不做画像采集（原因见 13.1.4；需要画像的群场景建议用唤醒轮即可）。
- 不做画像管理页 / 治理面（suppress/revoke 仅经内部接口，PC 管理页的画像面板走 read 方法不受影响）。
- `get_relationship_phase` 维持 unknown 诚实形状；`consume_relationship_projection` 不实现——PC 侧用 `callable()` 探针守护（memory_companion_adapter.py:2497），缺方法即静默跳过，已核实无 TypeError 路径。
- REQ-041 命名空间精确隔离（digest 分桶）未移植；将来实现 scoped-erase 面时一并重审 `_namespace_decision`。

