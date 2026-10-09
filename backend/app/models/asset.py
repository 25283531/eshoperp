"""素材与 AI 模块：Asset / AiTask / AiTaskResult（§4.4.2）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Integer, String, Text, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BaseMixin, JSONType, SoftDeleteMixin
from app.models.enums import AiTaskStatus, AssetOrigin, AssetType, ReviewStatus


class Asset(BaseMixin, SoftDeleteMixin, Base):
    """素材：1688 原始 / AI 重构，支持版本管理与回滚。"""

    __tablename__ = "asset"

    source_product_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="FK → source_product.id")
    source_sku_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="FK → source_sku.id")
    asset_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AssetType.MAIN_IMAGE.value, comment="main_image/detail_image/video"
    )
    origin: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AssetOrigin.RAW.value, comment="raw 原始 / ai_rework AI 重构"
    )
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False, comment="本地存储相对路径 data/assets/...")
    origin_url: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="原始 URL（可追溯）")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="内容哈希（去重依据）")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="版本号（同一 lineage 内递增）")
    lineage_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="版本族 ID：同族可回滚切换")
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="当前生效版本")
    width: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="宽度")
    height: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="高度")
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="文件大小")
    tags_json: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True, comment="标签（P1）")
    ai_task_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="产出该素材的 AI 任务")

    __table_args__ = (
        Index("uq_asset_content_hash", "content_hash", unique=True),
        Index("ix_asset_lineage", "lineage_id", "version"),
        Index("ix_asset_product_origin", "source_product_id", "origin", "is_deleted"),
        Index("ix_asset_current", "is_current", "is_deleted"),
    )

    def __repr__(self) -> str:  # noqa: D105
        return f"<Asset {self.id} {self.asset_type}/{self.origin}>"


class AiTask(BaseMixin, Base):
    """AI 重构任务。"""

    __tablename__ = "ai_task"

    source_product_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="FK → source_product.id")
    target_platform: Mapped[str] = mapped_column(String(32), nullable=False, comment="目标平台")
    rework_items_json: Mapped[list[Any]] = mapped_column(
        JSONType, nullable=False, default=list, comment='重构项 ["main_image","detail_image","title","attribute"]'
    )
    template_version: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="Prompt 模板版本")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AiTaskStatus.QUEUED.value, comment="见 §7.3 状态机"
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5, comment="调度优先级")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="已重试次数")
    max_retry: Mapped[int] = mapped_column(Integer, nullable=False, default=3, comment="最大重试次数")
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="耗时")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="错误码")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True, comment="错误信息")
    task_record_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="FK → task_record.id")
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="创建人")

    __table_args__ = (
        Index("ix_ai_task_status", "status", "priority"),
        Index("ix_ai_task_product", "source_product_id"),
    )

    def __repr__(self) -> str:  # noqa: D105
        return f"<AiTask {self.id} {self.status}>"


class AiTaskResult(BaseMixin, Base):
    """AI 重构产出与审核（未 approved 禁止被上架引用）。"""

    __tablename__ = "ai_task_result"

    ai_task_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="FK → ai_task.id")
    output_asset_ids_json: Mapped[list[Any] | None] = mapped_column(JSONType, nullable=True, comment="产出素材 ID 列表")
    output_title: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="产出标题")
    output_selling_points: Mapped[str | None] = mapped_column(Text, nullable=True, comment="产出卖点")
    output_attributes_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONType, nullable=True, comment="产出属性（平台类目模板填充）"
    )
    banned_words_json: Mapped[list[Any] | None] = mapped_column(
        JSONType, nullable=True, comment="违禁词 / 极限词命中（AIR-P0-04）"
    )
    review_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ReviewStatus.PENDING.value, comment="pending/approved/rejected"
    )
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True, comment="审核意见")
    reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="审核人")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="审核时间")
    model_name: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="模型名（成本归因）")
    prompt_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True, comment="Prompt 快照（复现用）")

    __table_args__ = (
        Index("ix_ai_task_result_task", "ai_task_id"),
        Index("ix_ai_task_result_review", "review_status"),
    )

    @property
    def approved(self) -> bool:
        """是否已审核通过（上架前置条件）。"""
        return self.review_status == ReviewStatus.APPROVED.value

    def __repr__(self) -> str:  # noqa: D105
        return f"<AiTaskResult {self.id} {self.review_status}>"


__all__ = ["Asset", "AiTask", "AiTaskResult"]
