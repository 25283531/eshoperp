"""素材与 AI 模块：Asset / AiTask / AiTaskResult（§4.4.2）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Integer, String, Text, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BaseMixin, JSONType, SoftDeleteMixin
from app.models.enums import AiClientName, AiTaskStatus, AiTaskType, AssetOrigin, AssetType, ReviewStatus


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
    # ★ 任务类型：`rework_items_json` 是"要做哪几项"的**子项列表**（AiReworkItem），
    #   它回答不了"这条 AI 任务到底要产出什么"，故单独设本列（AiTaskType）。
    task_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=AiTaskType.AI_REWORK.value,
        server_default=AiTaskType.AI_REWORK.value,
        comment="AI 任务类型（AiTaskType）：ai_rework/image_redraw/title_suggest/video_script",
    )
    # ★ 使用者输入的提示词（**输入侧**；产出侧回填的快照在 ai_task_result.prompt_snapshot）。
    #   结构见 `app/adapters/ai/base.py:AiInputPrompt`，键名**逐字照抄**它：
    #     {"global_prompt": str, "images": [{"index": 0, "prompt": str, "asset_id": int|null,
    #                                        "source_path": str, "tag": str}],
    #      "title_prompt": str, "video_script_prompt": str, "extra": {}}
    #   —— 用一个 JSON 列同时表达「全局提示词」与「逐图提示词」，不逐图开列。
    #   ★ 注释曾把全局提示词写成 "global"，而实现收发的是 "global_prompt"：
    #     照错的键写代码会取到 None，且**不报错**（只是提示词静默失效），
    #     排查成本极高 —— 键名一律以 AiInputPrompt.to_dict() 为准。
    input_prompt_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONType, nullable=True, default=dict, comment="使用者输入的提示词（全局 + 逐图），见 AiInputPrompt"
    )
    ai_client: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=AiClientName.FILE_BRIDGE.value,
        comment="创建时固化的 AI 客户端名（README 第九节第 17 条：切换配置后在途任务仍按原客户端跑完）",
    )
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
        Index("ix_ai_task_type", "task_type"),
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
