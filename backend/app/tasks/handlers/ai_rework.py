"""AI 重构任务处理器（`ai_rework`）。

★ 用户决策 ②：AI 默认走 `file_bridge`（与 WorkBuddy 协作的文件桥）。
   等待产出超时会抛 `AiTimeoutError` —— **本处理器不吞掉它**，
   由 `TaskRunner` 捕获后按 `max_retry` 重试（"可重试"语义的正确处理方式）。

★ ★ ★ 事务纪律 A（本项目硬规则）★ ★ ★
本处理器**自己不开数据库会话**，只是把 `ai_task_id` 转交给
`AiTaskService.run_task()` —— 后者按「① 短事务读参数 → ② 无事务等外部产出 →
③ 短事务回写」的三段式执行。

史实教训：早期版本在这里 `async with factory() as session:` 包住整个执行过程，
而 ② 阶段等待 WorkBuddy 产出最长可达 `ai.poll_timeout_sec`（默认 1800s）。
SQLite 是**单写者**，于是两个卡住的 ai_rework 任务就把全系统的写能力锁死了：
所有写接口集体 500，只读接口因命中缓存仍返回 200 —— 表象把排查方向带偏了两轮。
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.models.enums import TaskType
from app.services.ai_task_service import AiTaskService
from app.tasks.registry import task_handler

logger = get_logger(__name__)

__all__ = ["ai_rework_handler"]


@task_handler(TaskType.AI_REWORK.value)
async def ai_rework_handler(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """执行 AI 重构任务（三段式，等待期间不持有任何数据库事务）。

    payload:
        ai_task_id: int      AI 任务 ID
        operator: str?       操作人

    Raises:
        AiTimeoutError: 等待 WorkBuddy 产出超时（可重试，交给 TaskRunner 重试）。
                        提示信息指向 `data/ai_queue/<task_id>/prompt.md`。
    """
    ai_task_id = int(payload.get("ai_task_id") or 0)
    operator = str(payload.get("operator") or "system")
    if ai_task_id <= 0:
        return {"ok": False, "reason": "ai_task_id 缺失"}

    task = await AiTaskService.run_task(ai_task_id, operator=operator)

    return {
        "ok": True,
        "ai_task_id": ai_task_id,
        "status": task.status,
        "source_product_id": int(task.source_product_id),
    }
