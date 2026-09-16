"""供 Agent 主动管理的常驻核心记忆工具。"""

import json
from dataclasses import field
from typing import Any

from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from ..base.config_manager import ConfigManager


def _json_result(data: dict[str, Any]) -> str:
    """将工具结果稳定序列化为 JSON 文本。"""
    return json.dumps(data, ensure_ascii=False, default=str)


@dataclass
class CoreMemoryTool(FunctionTool[AstrAgentContext]):
    """常驻核心记忆管理工具（记/查/删硬边界与偏好）。"""

    __pydantic_config__ = {"arbitrary_types_allowed": True}

    config_manager: ConfigManager | None = None
    memory_engine: Any = None

    name: str = "manage_core_memory"
    description: str = (
        "Manage core memories: a small set of rules, boundaries, preferences "
        "and stable facts that are injected EVERY turn without retrieval. "
        "Use action=add when the user states a hard preference or boundary "
        "worth never forgetting ('call me X', 'never discuss Y'). "
        "Use action=list to review current core memories. "
        "Use action=delete with the memory_id shown in list to remove one. "
        "Keep the total small (they occupy fixed injection budget); "
        "for ordinary facts use memorize/recall tools instead."
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "delete"],
                    "description": "add a core memory, list all, or delete one by memory_id.",
                },
                "content": {
                    "type": "string",
                    "description": "For add: the rule/fact text, short and imperative, e.g. '称呼用户为老板'.",
                },
                "label": {
                    "type": "string",
                    "description": "For add: short display name, e.g. '称呼规则'.",
                },
                "kind": {
                    "type": "string",
                    "enum": ["rule", "boundary", "preference", "stable_fact"],
                    "description": "For add: rule=行为规则, boundary=禁忌红线, preference=偏好, stable_fact=稳定事实.",
                },
                "priority": {
                    "type": "integer",
                    "description": "For add: 0-100, higher loads first when the core budget is tight. Default 50.",
                },
                "memory_id": {
                    "type": "integer",
                    "description": "For delete: id from action=list result.",
                },
            },
            "required": ["action"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        action: str,
        content: str = "",
        label: str = "",
        kind: str = "rule",
        priority: int = 50,
        memory_id: int = 0,
    ) -> ToolExecResult:
        """执行核心记忆管理操作。"""
        action = (action or "").strip().lower()
        if self.config_manager is None or self.memory_engine is None:
            return _json_result({"ok": False, "error": "core memory tool is not initialized"})
        if not self.config_manager.get("core_memory.enabled", True):
            return _json_result({"ok": False, "error": "core memory is disabled in config"})

        try:
            event = getattr(getattr(context, "context", None), "event", None)
            session_id = str(getattr(event, "unified_msg_origin", "") or "")

            if action == "add":
                if not content.strip():
                    return _json_result({"ok": False, "error": "content is required for add"})
                # 会话内规则默认落 session 域，其余全局
                scope = "session" if any(
                    mark in content for mark in ("这个群", "本群", "此群")
                ) else "global"
                mid = await self.memory_engine.add_core_memory(
                    content,
                    label=label or (content.strip()[:12]),
                    kind=kind if kind in ("rule", "boundary", "preference", "stable_fact") else "rule",
                    priority=priority,
                    scope=scope,
                    session_id=session_id if scope == "session" else None,
                )
                return _json_result({"ok": True, "action": "add", "memory_id": mid})

            if action == "list":
                items = await self.memory_engine.list_core_memories()
                return _json_result({
                    "ok": True, "action": "list", "count": len(items),
                    "items": [
                        {
                            "memory_id": item["memory_id"],
                            "label": item["label"],
                            "kind": item["kind"],
                            "priority": int(item["priority"]),
                            "content": item["text"],
                        }
                        for item in items[:20]
                    ],
                })

            if action == "delete":
                if not memory_id:
                    return _json_result({"ok": False, "error": "memory_id is required for delete"})
                ok = await self.memory_engine.delete_core_memory(int(memory_id))
                return _json_result({"ok": bool(ok), "action": "delete", "memory_id": memory_id})

            return _json_result({"ok": False, "error": f"unknown action: {action}"})
        except Exception as e:
            logger.error(f"核心记忆工具失败: {e}", exc_info=True)
            return _json_result({"ok": False, "error": "internal_error"})
