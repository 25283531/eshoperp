"""异步任务持久化：TaskRecord（PRD 8.4 重启恢复依据）。

★ 幂等键 `task_key` 使用部分唯一索引：同一键同时只能有一个 pending/running 任务。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BaseMixin, JSONType
from app.models.enums import ACTIVE_TASK_STATUSES, TaskStatus
from app.utils.kit import utc_now


class TaskRecord(BaseMixin, Base):
    """异步任务记录（APScheduler 方案必须自建，用于重启恢复与幂等）。"""

    __tablename__ = "task_record"

    task_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="source_collect/ai_rework/publish/order_sync/inventory_sync/mapping_check",
    )
    task_key: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="幂等键，如 publish:{id}")
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict, comment="任务参数")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TaskStatus.PENDING.value, comment="pending/running/success/failed/cancelled"
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5, comment="优先级")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="已重试次数")
    max_retry: Mapped[int] = mapped_column(Integer, nullable=False, default=3, comment="最大重试次数")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="计划执行时间")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="开始时间")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="结束时间")
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="耗时")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="错误码")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True, comment="错误信息")
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="执行线程标识")
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="链路 ID")
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True, comment="任务产出摘要")

    __table_args__ = (
        # ★ 幂等：同一键同时只能有一个活跃（pending/running）任务
        Index(
            "uq_task_record_active_key",
            "task_key",
            unique=True,
            sqlite_where=text("status IN ('pending','running')"),
            postgresql_where=text("status IN ('pending','running')"),
        ),
        Index("ix_task_record_status", "status", "priority", "created_at"),
        Index("ix_task_record_type", "task_type", "status"),
        Index("ix_task_record_scheduled", "scheduled_at"),
    )

    @property
    def is_active(self) -> bool:
        """是否为活跃任务（pending / running）。"""
        return self.status in ACTIVE_TASK_STATUSES

    def mark_running(self, worker_id: str = "") -> None:
        """标记为执行中（不提交事务）。"""
        self.status = TaskStatus.RUNNING.value
        self.started_at = utc_now()
        self.worker_id = worker_id or self.worker_id

    def mark_finished(self, result: Any = None) -> None:
        """标记为成功（不提交事务）。"""
        self.status = TaskStatus.SUCCESS.value
        self.finished_at = utc_now()
        self._calc_duration()
        if result is not None:
            self.result_json = result if isinstance(result, dict) else {"value": result}

    def mark_failed(self, error_code: str = "", error_message: str = "") -> None:
        """标记为失败（不提交事务）。"""
        self.status = TaskStatus.FAILED.value
        self.finished_at = utc_now()
        self.error_code = error_code
        self.error_message = error_message
        self._calc_duration()

    def mark_cancelled(self) -> None:
        """标记为已取消（不提交事务）。"""
        self.status = TaskStatus.CANCELLED.value
        self.finished_at = utc_now()
        self._calc_duration()

    def reset_for_retry(self) -> None:
        """重置为待执行以便重试 / 重启恢复（不提交事务）。"""
        self.status = TaskStatus.PENDING.value
        self.started_at = None
        self.finished_at = None
        self.duration_ms = None
        self.worker_id = None
        self.retry_count = int(self.retry_count or 0) + 1

    @property
    def can_retry(self) -> bool:
        """是否还能重试。"""
        return int(self.retry_count or 0) < int(self.max_retry or 0)

    def _calc_duration(self) -> None:
        """计算耗时毫秒。"""
        if self.started_at and self.finished_at:
            self.duration_ms = int((self.finished_at - self.started_at).total_seconds() * 1000)

    def __repr__(self) -> str:  # noqa: D105
        return f"<TaskRecord {self.task_type}#{self.id} {self.status}>"


__all__ = ["TaskRecord"]
