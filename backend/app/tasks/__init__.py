"""异步任务框架（ADR-3：APScheduler + ThreadPoolExecutor + TaskRecord 持久化）。

    runner.py     ★ TaskRunner 抽象接口 + LocalTaskRunner 实现（未来可换 Celery 不改业务代码）
    registry.py   任务处理器注册表 @task_handler("publish")
    scheduler.py  APScheduler 装配与定时任务注册
    recovery.py   启动时扫描未完成任务并恢复（PRD 8.4）
"""

from app.tasks.registry import (
    TASK_HANDLER_REGISTRY,
    autoload_handlers,
    get_handler,
    list_handlers,
    task_handler,
    unregister_handler,
)
from app.tasks.recovery import RecoveryStats, recover_pending_tasks, scan_pending_tasks
from app.tasks.runner import (
    LocalTaskRunner,
    TaskContext,
    TaskRunner,
    get_task_runner,
    set_task_runner,
    shutdown_task_runner,
)
from app.tasks.scheduler import (
    SCHEDULED_JOBS,
    build_scheduler,
    get_scheduler,
    is_running,
    list_jobs,
    reschedule,
    start_scheduler,
    stop_scheduler,
)

__all__ = [
    "LocalTaskRunner",
    "RecoveryStats",
    "SCHEDULED_JOBS",
    "TASK_HANDLER_REGISTRY",
    "TaskContext",
    "TaskRunner",
    "autoload_handlers",
    "build_scheduler",
    "get_handler",
    "get_scheduler",
    "get_task_runner",
    "is_running",
    "list_handlers",
    "list_jobs",
    "recover_pending_tasks",
    "reschedule",
    "scan_pending_tasks",
    "set_task_runner",
    "shutdown_task_runner",
    "start_scheduler",
    "stop_scheduler",
    "task_handler",
    "unregister_handler",
]
