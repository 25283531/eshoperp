"""素材库 Schema（§5.5.4 / §5.5.5）。"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.schemas.common import BaseSchema, iso_or_none

__all__ = [
    "AiConcurrencyUpdate",
    "AiImagePromptIn",
    "AiTaskCreate",
    "AiTaskDetailVo",
    "AiTaskEdited",
    "AiTaskResultVo",
    "AiTaskReviewRequest",
    "AiTaskVo",
    "AiTitleCandidateVo",
    "AiTitleSelectRequest",
    "AiVideoScriptSceneVo",
    "AiVideoScriptVo",
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
    # ★ 任务类型（AiTaskType）：回答"这条任务要产出什么"，与异步队列类型 `TaskType` 是两个维度。
    task_type: str = "ai_rework"
    ai_client: str = ""
    input_prompt: dict[str, Any] | None = None
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
            task_type=str(model.task_type or "ai_rework"),
            ai_client=str(model.ai_client or ""),
            input_prompt=dict(model.input_prompt_json) if isinstance(model.input_prompt_json, dict) else None,
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


class AiTitleCandidateVo(BaseSchema):
    """**单条**标题候选（`AiTaskResultVo.output_title_candidates` 的元素）。

    ★ 字段与 `AiTitleCandidate.to_dict()` **逐字对齐**（前端直接按这份结构渲染）。
      `char_count` 也带上：平台对标题字数有硬上限，前端要能在选中前就提示超长。
    """

    title: str = ""
    selling_points: list[str] = Field(default_factory=list)
    style: str = ""
    reason: str = ""
    platform_fit: str = ""
    score: int = 0
    banned_words: list[str] = Field(default_factory=list)
    char_count: int = 0

    @classmethod
    def from_dict(cls, raw: Any) -> "AiTitleCandidateVo":
        """从落库的候选 dict 构造（脏数据退化为空候选，不抛异常）。"""
        payload = raw if isinstance(raw, dict) else {}
        title = str(payload.get("title", "") or "")
        return cls(
            title=title,
            selling_points=[str(s) for s in (payload.get("selling_points") or [])],
            style=str(payload.get("style", "") or ""),
            reason=str(payload.get("reason", "") or ""),
            platform_fit=str(payload.get("platform_fit", "") or ""),
            score=int(payload.get("score", 0) or 0),
            banned_words=[str(w) for w in (payload.get("banned_words") or [])],
            char_count=int(payload.get("char_count", len(title)) or len(title)),
        )


class AiVideoScriptSceneVo(BaseSchema):
    """短视频脚本的**一个分镜**（`AiVideoScriptVo.scenes` 的元素）。"""

    index: int = 0
    duration_sec: float = 0.0
    shot: str = ""
    camera: str = ""
    shooting_tips: str = ""
    narration: str = ""

    @classmethod
    def from_dict(cls, raw: Any) -> "AiVideoScriptSceneVo":
        """从落库的分镜 dict 构造（脏数据退化为空分镜）。"""
        payload = raw if isinstance(raw, dict) else {}
        try:
            duration = float(payload.get("duration_sec", 0) or 0)
        except (TypeError, ValueError):
            duration = 0.0
        return cls(
            index=int(payload.get("index", 0) or 0),
            duration_sec=duration,
            shot=str(payload.get("shot", "") or ""),
            camera=str(payload.get("camera", "") or ""),
            shooting_tips=str(payload.get("shooting_tips", "") or ""),
            narration=str(payload.get("narration", "") or ""),
        )


class AiVideoScriptVo(BaseSchema):
    """短视频拍摄脚本（`GET /ai-tasks/{id}` 返回，前端据此展示/复制文案）。

    ★ 只含文案，不含任何视频文件路径 —— 本能力只出脚本，视频由使用者自己拍。
    """

    title: str = ""
    style: str = ""
    scenes: list[AiVideoScriptSceneVo] = Field(default_factory=list)
    total_duration_sec: float = 0.0
    scene_count: int = 0
    text: str = ""  # ★ 渲染好的纯文本（`AiVideoScriptResult.to_text()`），可直接照着拍

    @classmethod
    def from_dict(cls, raw: Any) -> "AiVideoScriptVo | None":
        """从落库的脚本 dict 构造；**没有脚本时返回 None**（不返回空壳，避免前端误判"有产出"）。"""
        if not isinstance(raw, dict) or not raw:
            return None
        scenes = [AiVideoScriptSceneVo.from_dict(s) for s in (raw.get("scenes") or []) if isinstance(s, dict)]
        return cls(
            title=str(raw.get("title", "") or ""),
            style=str(raw.get("style", "") or ""),
            scenes=scenes,
            total_duration_sec=float(raw.get("total_duration_sec", 0) or 0),
            scene_count=int(raw.get("scene_count", len(scenes)) or len(scenes)),
            text=str(raw.get("text", "") or ""),
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
    # ★ 标题候选数组（`title_suggest` 产出；其余能力为空列表）
    output_title_candidates: list[AiTitleCandidateVo] = Field(default_factory=list)
    # ★ 短视频脚本（`video_script` 产出；其余能力为 None）
    output_video_script: AiVideoScriptVo | None = None
    # ★ 选中留痕：选了第几条 / 谁 / 什么时候（未发生过选择则全为 None）
    selected_title_index: int | None = None
    selected_title_at: str | None = None
    selected_title_by: str | None = None
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
        raw_candidates = getattr(model, "output_title_candidates_json", None)
        candidates = (
            [AiTitleCandidateVo.from_dict(c) for c in raw_candidates if isinstance(c, dict)]
            if isinstance(raw_candidates, list)
            else []
        )
        return cls(
            id=int(model.id or 0),
            ai_task_id=int(model.ai_task_id or 0),
            output_title=model.output_title,
            output_selling_points=model.output_selling_points,
            output_attributes_json=dict(model.output_attributes_json or {}),
            banned_words=words,
            output_assets=[AssetVo.from_model(a) for a in (assets or [])],
            output_title_candidates=candidates,
            output_video_script=AiVideoScriptVo.from_dict(getattr(model, "output_video_script_json", None)),
            selected_title_index=getattr(model, "selected_title_index", None),
            selected_title_at=iso_or_none(getattr(model, "selected_title_at", None)),
            selected_title_by=getattr(model, "selected_title_by", None),
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


class AiImagePromptIn(BaseSchema):
    """**逐图提示词**入参（图片重绘：每张图可单独给一句）。

    ★ 键名与 `AiInputPrompt` / `AiImagePrompt` **逐字一致**，
      避免"入参叫一套、落库叫另一套"（那会让提示词静默失效，且**不报错**）。
    """

    index: int = Field(0, ge=0, description="图片下标，与货源原图顺序一致（0 起）")
    prompt: str = Field("", description="这张图单独用的提示词；留空则回落到全局提示词")
    asset_id: int | None = Field(None, description="该图的素材 ID（可空，便于 UI 回显是哪张）")
    source_path: str = Field("", description="原图路径（可空）")
    tag: str = Field("", description="main_image / detail_image（可空）")


class AiTaskCreate(BaseSchema):
    """创建 AI 任务（`POST /ai-tasks`）。

    ★ 全部新增字段都**可空**：老调用方不带这些参数时，行为与改动前完全一致
      （`task_type` 默认 `ai_rework`，提示词为空）。
    """

    source_product_ids: list[int] = Field(default_factory=list, description="货源商品 ID 列表")
    target_platform: str = Field(..., description="目标平台 taobao / douyin / pdd")
    rework_items: list[str] = Field(
        default_factory=list, description="重构项；为空则取默认四项"
    )
    template_version: str | None = None
    # ★ 任务类型（AiTaskType）：ai_rework（默认，存量口径）/ image_redraw / title_suggest / video_script
    task_type: str | None = Field(
        None, description="AI 任务类型；为空则取 ai_rework（老调用方行为不变）"
    )
    # ★ 提示词入参：一个全局 + 三类分能力 + 逐图（全部可空）
    global_prompt: str | None = Field(None, description="全局提示词（逐图未单独指定时回落到此）")
    image_prompts: list[AiImagePromptIn] = Field(
        default_factory=list, description="逐图提示词（图片重绘：每张图一句）"
    )
    title_prompt: str | None = Field(None, description="标题建议的补充要求（风格 / 热词倾向）")
    video_script_prompt: str | None = Field(
        None, description="短视频脚本的拍摄风格 / 内容倾向（只出文案，不生成视频）"
    )


class AiTitleSelectRequest(BaseSchema):
    """选定某条标题候选（`POST /ai-tasks/{id}/select-title`）。

    ★ 定位方式二选一：
        `index`  按候选下标选（推荐，前端"选第 2 条"就用它）；
        `title`  按标题文本精确匹配（兜底：候选列表被前端重排过时用它）。
      两个都没给 → 422；两个都给 → 以 `index` 为准。
    """

    index: int | None = Field(None, ge=0, description="候选下标（0 起），与返回列表顺序一致")
    title: str | None = Field(None, description="候选标题原文（index 缺失时按此精确匹配）")
    note: str | None = Field(None, description="选择备注（可选，写审计）")


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
