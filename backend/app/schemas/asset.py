"""素材库 Schema（§5.5.4 / §5.5.5）。"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.schemas.common import BaseSchema, iso_or_none

__all__ = [
    "AiConcurrencyUpdate",
    "AiTaskCreate",
    "AiTaskDetailVo",
    "AiTaskEdited",
    "AiTaskResultVo",
    "AiTaskReviewRequest",
    "AiTaskVo",
    "AssetRollbackRequest",
    "AssetTagUpdate",
    "AssetVo",
    "BatchDownloadRequest",
    "BatchDownloadVo",
]


class AssetVo(BaseSchema):
    """素材响应体（§5.5.4）。"""

    id: int = 0
    source_product_id: int | None = None
    source_sku_id: int | None = None
    asset_type: str = "main_image"
    origin: str = "raw"
    storage_path: str = ""
    origin_url: str | None = None
    content_hash: str = ""
    version: int = 1
    lineage_id: str | None = None
    is_current: bool = True
    width: int | None = None
    height: int | None = None
    size_bytes: int | None = None
    tags: list[str] = Field(default_factory=list)
    preview_url: str = ""
    ai_task_id: int | None = None
    created_at: str | None = None

    @classmethod
    def from_model(cls, model: Any, *, preview_url: str = "") -> "AssetVo":
        """从 ORM 对象构造。`tags_json` 兼容 list 与 dict 两种落库形态。"""
        raw_tags = model.tags_json
        if isinstance(raw_tags, list):
            tags = [str(t) for t in raw_tags]
        elif isinstance(raw_tags, dict):
            tags = [str(t) for t in raw_tags.get("tags", [])]
        else:
            tags = []
        return cls(
            id=int(model.id or 0),
            source_product_id=model.source_product_id,
            source_sku_id=model.source_sku_id,
            asset_type=model.asset_type or "main_image",
            origin=model.origin or "raw",
            storage_path=model.storage_path or "",
            origin_url=model.origin_url,
            content_hash=model.content_hash or "",
            version=int(model.version or 1),
            lineage_id=model.lineage_id,
            is_current=bool(model.is_current),
            width=model.width,
            height=model.height,
            size_bytes=model.size_bytes,
            tags=tags,
            preview_url=preview_url or f"/api/v1/assets/{model.id}/download",
            ai_task_id=model.ai_task_id,
            created_at=iso_or_none(model.created_at),
        )


class AssetTagUpdate(BaseSchema):
    """更新素材标签。"""

    tags: list[str] = Field(default_factory=list, description="标签列表，全量覆盖")


class AssetRollbackRequest(BaseSchema):
    """素材版本回滚。"""

    reason: str = Field(default="", max_length=255, description="回滚原因（写审计）")


class BatchDownloadRequest(BaseSchema):
    """批量下载素材。"""

    ids: list[int] = Field(default_factory=list, description="素材 ID 列表")


class BatchDownloadVo(BaseSchema):
    """批量下载结果。"""

    download_url: str = ""
    file_count: int = 0
    missing_ids: list[int] = Field(default_factory=list)


# ======================================================================
#  AI 重构任务 / 产出（§5.5.5）
# ======================================================================


class AiTaskVo(BaseSchema):
    """AI 重构任务响应体（`GET /ai-tasks`）。"""

    id: int = 0
    source_product_id: int = 0
    source_product_title: str | None = None
    target_platform: str = ""
    rework_items: list[str] = Field(default_factory=list)
    template_version: str | None = None
    status: str = "queued"
    priority: int = 5
    retry_count: int = 0
    duration_ms: int | None = None
    error_message: str | None = None
    created_by: str | None = None
    created_at: str | None = None

    @classmethod
    def from_model(
        cls,
        model: Any,
        *,
        source_product_title: str | None = None,
    ) -> "AiTaskVo":
        """从 ORM 对象构造。"""
        raw_items = model.rework_items_json
        items = [str(i) for i in raw_items] if isinstance(raw_items, list) else []
        return cls(
            id=int(model.id or 0),
            source_product_id=int(model.source_product_id or 0),
            source_product_title=source_product_title,
            target_platform=model.target_platform or "",
            rework_items=items,
            template_version=model.template_version,
            status=model.status or "queued",
            priority=int(model.priority or 5),
            retry_count=int(model.retry_count or 0),
            duration_ms=model.duration_ms,
            error_message=model.error_message,
            created_by=model.created_by,
            created_at=iso_or_none(model.created_at),
        )


class AiTaskResultVo(BaseSchema):
    """AI 重构产出与审核结果（§5.5.5）。"""

    id: int = 0
    ai_task_id: int = 0
    output_title: str | None = None
    output_selling_points: str | None = None
    output_attributes_json: dict[str, Any] = Field(default_factory=dict)
    banned_words: list[dict[str, Any]] = Field(default_factory=list)
    output_assets: list[AssetVo] = Field(default_factory=list)
    review_status: str = "pending"
    review_note: str | None = None
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    model_name: str | None = None
    created_at: str | None = None

    @classmethod
    def from_model(
        cls,
        model: Any,
        *,
        assets: list[Any] | None = None,
    ) -> "AiTaskResultVo":
        """从 ORM 对象构造。`assets` 为已加载的 `Asset` 列表。"""
        raw_words = model.banned_words_json
        words = [dict(w) for w in raw_words if isinstance(w, dict)] if isinstance(raw_words, list) else []
        return cls(
            id=int(model.id or 0),
            ai_task_id=int(model.ai_task_id or 0),
            output_title=model.output_title,
            output_selling_points=model.output_selling_points,
            output_attributes_json=dict(model.output_attributes_json or {}),
            banned_words=words,
            output_assets=[AssetVo.from_model(a) for a in (assets or [])],
            review_status=model.review_status or "pending",
            review_note=model.review_note,
            reviewed_by=model.reviewed_by,
            reviewed_at=iso_or_none(model.reviewed_at),
            model_name=model.model_name,
            created_at=iso_or_none(model.created_at),
        )


class AiTaskDetailVo(AiTaskVo):
    """AI 任务详情（`GET /ai-tasks/{id}`，含 `result` 与 `assets[]`）。"""

    result: AiTaskResultVo | None = None
    assets: list[AssetVo] = Field(default_factory=list)


class AiTaskCreate(BaseSchema):
    """创建 AI 重构任务（`POST /ai-tasks`）。"""

    source_product_ids: list[int] = Field(default_factory=list, description="货源商品 ID 列表")
    target_platform: str = Field(..., description="目标平台 taobao / douyin / pdd")
    rework_items: list[str] = Field(
        default_factory=list, description="重构项；为空则取默认四项"
    )
    template_version: str | None = None


class AiTaskEdited(BaseSchema):
    """审核时的人工编辑内容（`action=edit`）。"""

    title: str | None = None
    selling_points: str | None = None
    attributes_json: dict[str, Any] | None = None


class AiTaskReviewRequest(BaseSchema):
    """AI 产出审核（`POST /ai-tasks/{id}/review`）。"""

    action: str = Field(..., description="approve / reject / edit")
    note: str | None = None
    edited: AiTaskEdited | None = None


class AiConcurrencyUpdate(BaseSchema):
    """AI 并发与重试配置（`PUT /ai-tasks/concurrency-config`）。"""

    max_concurrency: int | None = Field(default=None, ge=1, le=16)
    max_retry: int | None = Field(default=None, ge=0, le=10)
