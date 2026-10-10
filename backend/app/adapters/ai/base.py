"""AI 重构客户端接口（ARCH §11.2 N2 裁决落地）。

三种实现：
    mock          返回占位图 + 改写标题（★ MVP 默认）
    file_bridge   把任务写成 data/ai_queue/<task_id>/{task.json,prompt.md}，
                  轮询回读 data/ai_output/<task_id>/result.json（与 WorkBuddy 协作的真实路径）
    http          OpenAI 兼容接口（base_url / api_key / model 从配置读）
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "AiClient",
    "AiImagePrompt",
    "AiImageResult",
    "AiInputPrompt",
    "AiReworkResult",
    "AiTaskContext",
    "AiTitleCandidate",
    "AiTitleResult",
    "AiAttributeResult",
    "AiTimeoutError",
    "MIN_TITLE_CANDIDATES",
]


def _safe_int(value: Any, default: int | None) -> int | None:
    """把外部 JSON 值安全地转成 int（★ 边界数据：失败退化为兜底值，绝不抛异常）。"""
    if value is None:
        return default
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


class AiTimeoutError(Exception):
    """等待 AI 产出超时（可重试）。"""


# ★ 商品标题建议要求「3 条以上候选」：这里只把下限钉成一个常量供产出方 / 展示方共同引用，
#   **不做运行时强制** —— 旧的三个客户端仍只产出单条，硬性要求会让它们全部失败。
#   何时真正强制：下一棒改造三个客户端的 `rewrite_title()` 之后再收紧。
MIN_TITLE_CANDIDATES: int = 3


@dataclass
class AiImagePrompt:
    """**逐图提示词**（使用者针对某一张图单独输入的提示词）。

    ★ 这是**输入侧**数据，别和 `AiImageResult.prompt`（产出侧回填的实际 Prompt）弄混。

    `index`    是唯一的定位键：与 `AiTaskContext.source_image_paths` 的下标一一对应；
    `asset_id` 冗余该图的素材 ID，便于 UI 回显"这张图是哪张"；
    两者都给是为了兼容"只有路径列表"与"素材已入库"两种前端形态。
    """

    index: int = 0
    prompt: str = ""
    asset_id: int | None = None
    source_path: str = ""
    tag: str = ""  # main_image / detail_image …（与 AiReworkItem 取值同口径，非必填）

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "index": int(self.index),
            "prompt": self.prompt,
            "asset_id": self.asset_id,
            "source_path": self.source_path,
            "tag": self.tag,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AiImagePrompt":
        """反序列化（脏数据一律退化为空提示词，不抛异常）。"""
        asset_id = raw.get("asset_id")
        return cls(
            index=_safe_int(raw.get("index"), 0),
            prompt=str(raw.get("prompt", "") or ""),
            asset_id=_safe_int(asset_id, None),
            source_path=str(raw.get("source_path", "") or ""),
            tag=str(raw.get("tag", "") or ""),
        )


@dataclass
class AiInputPrompt:
    """使用者输入的**全部**提示词（`ai_task.input_prompt_json` 的唯一读写契约）。

    一个 JSON 列同时表达两种粒度：
        * `global_prompt`  全局提示词 —— 对本次任务所有产出生效；
        * `images`         逐图提示词 —— 每张图一个（图片重绘的核心诉求）；
        * `title_prompt` / `video_script_prompt` 分能力补充提示词（标题风格 / 拍摄风格）。
    形如：::

        {
          "global_prompt": "整体偏日式极简",
          "images": [{"index": 0, "prompt": "这张主图换成暖色背景", "asset_id": 12}],
          "title_prompt": "偏抖音热词风",
          "video_script_prompt": "竖屏、快节奏、强调性价比",
          "extra": {}
        }
    """

    global_prompt: str = ""
    images: list[AiImagePrompt] = field(default_factory=list)
    title_prompt: str = ""
    video_script_prompt: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def image_prompt(self, index: int) -> str:
        """取第 `index` 张图的提示词；未单独给则返回空串（由调用方决定是否回退全局）。"""
        for item in self.images:
            if int(item.index) == int(index):
                return item.prompt
        return ""

    def effective_prompt(self, index: int | None = None) -> str:
        """★ 取"最终生效"的提示词：逐图提示词优先，缺省回退全局提示词。

        Args:
            index: 图片下标；传 None 表示要的是全局口径（如标题 / 视频脚本）。
        """
        if index is None:
            return self.global_prompt
        return self.image_prompt(index) or self.global_prompt

    def to_dict(self) -> dict[str, Any]:
        """序列化（写入 `ai_task.input_prompt_json`）。"""
        return {
            "global_prompt": self.global_prompt,
            "images": [img.to_dict() for img in self.images],
            "title_prompt": self.title_prompt,
            "video_script_prompt": self.video_script_prompt,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "AiInputPrompt":
        """反序列化；`None` / 非 dict / 脏数据一律退化为**空契约**（不抛异常）。"""
        payload = raw if isinstance(raw, dict) else {}
        raw_images = payload.get("images")
        images: list[AiImagePrompt] = []
        if isinstance(raw_images, list):
            images = [AiImagePrompt.from_dict(i) for i in raw_images if isinstance(i, dict)]
        return cls(
            global_prompt=str(payload.get("global_prompt", "") or ""),
            images=images,
            title_prompt=str(payload.get("title_prompt", "") or ""),
            video_script_prompt=str(payload.get("video_script_prompt", "") or ""),
            extra=dict(payload.get("extra", {}) or {}),
        )


@dataclass
class AiTaskContext:
    """AI 重构任务上下文。"""

    task_id: str
    target_platform: str = ""
    source_product_id: int = 0
    original_title: str = ""
    rework_items: list[str] = field(default_factory=list)
    source_image_paths: list[str] = field(default_factory=list)
    selling_points: list[str] = field(default_factory=list)
    attributes_json: dict[str, Any] = field(default_factory=dict)
    # ★ 本轮只把结构纳进契约；`file_bridge` 的 prompt.md 怎么拼这些提示词属于下一棒，此处不动。
    image_prompts: list[AiImagePrompt] = field(default_factory=list)
    # ★ 全局提示词（输入侧）：单张图未单独指定时的回退值。
    global_prompt: str = ""
    title_prompt: str = ""
    video_script_prompt: str = ""
    task_type: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def image_prompt(self, index: int) -> str:
        """取第 `index` 张图"最终生效"的提示词：逐图提示词优先，缺省回退全局提示词。"""
        for item in self.image_prompts:
            if int(item.index) == int(index):
                return item.prompt
        return self.global_prompt

    def to_dict(self) -> dict[str, Any]:
        """序列化（写入 task.json）。"""
        return {
            "task_id": self.task_id,
            "target_platform": self.target_platform,
            "source_product_id": self.source_product_id,
            "original_title": self.original_title,
            "rework_items": list(self.rework_items),
            "source_image_paths": list(self.source_image_paths),
            "selling_points": list(self.selling_points),
            "attributes_json": dict(self.attributes_json),
            "image_prompts": [p.to_dict() for p in self.image_prompts],
            "global_prompt": self.global_prompt,
            "title_prompt": self.title_prompt,
            "video_script_prompt": self.video_script_prompt,
            "task_type": self.task_type,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AiTaskContext":
        """反序列化。★ 旧版 task.json 没有这几个键，取不到即为空，保持向后兼容。"""
        raw_prompts = raw.get("image_prompts")
        prompts: list[AiImagePrompt] = []
        if isinstance(raw_prompts, list):
            prompts = [AiImagePrompt.from_dict(p) for p in raw_prompts if isinstance(p, dict)]
        return cls(
            task_id=str(raw.get("task_id", "")),
            target_platform=str(raw.get("target_platform", "")),
            source_product_id=int(raw.get("source_product_id", 0) or 0),
            original_title=str(raw.get("original_title", "")),
            rework_items=list(raw.get("rework_items", [])),
            source_image_paths=list(raw.get("source_image_paths", [])),
            selling_points=list(raw.get("selling_points", [])),
            attributes_json=dict(raw.get("attributes_json", {})),
            image_prompts=prompts,
            global_prompt=str(raw.get("global_prompt", "") or ""),
            title_prompt=str(raw.get("title_prompt", "") or ""),
            video_script_prompt=str(raw.get("video_script_prompt", "") or ""),
            task_type=str(raw.get("task_type", "") or ""),
            extra=dict(raw.get("extra", {})),
        )


@dataclass
class AiImageResult:
    """单张重构图结果。"""

    local_path: str
    width: int = 0
    height: int = 0
    size_bytes: int = 0
    content_hash: str = ""
    is_placeholder: bool = False
    prompt: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "local_path": self.local_path,
            "width": self.width,
            "height": self.height,
            "size_bytes": self.size_bytes,
            "content_hash": self.content_hash,
            "is_placeholder": self.is_placeholder,
            "prompt": self.prompt,
        }


@dataclass
class AiTitleCandidate:
    """**单条**标题候选（`AiTitleResult.candidates` 的元素）。

    ★ 每条都必须带"让使用者知道该选哪条"的区分信息，光有 `title` 等于让使用者盲选：
        `style`   这条走的是什么风格 / 表达侧重（如"促销感" "专业参数流" "抖音热词风"）；
        `reason`  为什么推荐它（用于界面副标题，人话，不要只写模型名）；
        `platform_fit` 适配的目标平台（与 Platform 取值同口径，可空 = 通用）；
        `score`   产出方给的相对分（0-100，**只用于排序**，不同产次之间不可比）。
    """

    title: str = ""
    selling_points: list[str] = field(default_factory=list)
    style: str = ""
    reason: str = ""
    platform_fit: str = ""
    score: int = 0
    banned_words: list[str] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        """标题字数（排序 / 平台字数上限校验用）。"""
        return len(self.title)

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "title": self.title,
            "selling_points": list(self.selling_points),
            "style": self.style,
            "reason": self.reason,
            "platform_fit": self.platform_fit,
            "score": int(self.score),
            "banned_words": list(self.banned_words),
            "char_count": self.char_count,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AiTitleCandidate":
        """反序列化（脏数据退化为默认值）。"""
        payload = raw if isinstance(raw, dict) else {}
        score = payload.get("score", 0) or 0
        return cls(
            title=str(payload.get("title", "") or ""),
            selling_points=[str(s) for s in (payload.get("selling_points") or [])],
            style=str(payload.get("style", "") or ""),
            reason=str(payload.get("reason", "") or ""),
            platform_fit=str(payload.get("platform_fit", "") or ""),
            score=int(score),
            banned_words=[str(w) for w in (payload.get("banned_words") or [])],
        )


@dataclass
class AiTitleResult:
    """标题改写结果。

    ★★ 向后兼容约定（务必遵守）★★
        `title` / `selling_points` / `banned_words` / `model_name` / `prompt_snapshot` 五个字段
        **保持原位不变**，含义始终是"首选那条"（= `candidates[0]`），
        `persist_result()` 等既有消费方因此不需要改。
        新增的 `candidates` 只是**多候选**的承载容器，为空表示"产出方仍是老的单条口径"。
    """

    title: str
    selling_points: list[str] = field(default_factory=list)
    banned_words: list[str] = field(default_factory=list)
    model_name: str = ""
    prompt_snapshot: str = ""
    candidates: list[AiTitleCandidate] = field(default_factory=list)

    def preferred(self) -> AiTitleCandidate:
        """返回首选候选。

        `candidates` 非空时取最高分那条；为空则按旧口径把 `title` 包成一个候选返回
        （这就是"旧客户端也能无感升级"的兼容点）。
        """
        if self.candidates:
            return max(self.candidates, key=lambda c: int(c.score))
        return AiTitleCandidate(
            title=self.title,
            selling_points=list(self.selling_points),
            banned_words=list(self.banned_words),
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "title": self.title,
            "selling_points": list(self.selling_points),
            "banned_words": list(self.banned_words),
            "model_name": self.model_name,
            "prompt_snapshot": self.prompt_snapshot,
            "candidates": [c.to_dict() for c in self.candidates],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AiTitleResult":
        """反序列化（`result.json` 回读；缺 `candidates` 的旧文件按单条口径处理）。"""
        payload = raw if isinstance(raw, dict) else {}
        raw_candidates = payload.get("candidates")
        candidates: list[AiTitleCandidate] = []
        if isinstance(raw_candidates, list):
            candidates = [AiTitleCandidate.from_dict(c) for c in raw_candidates if isinstance(c, dict)]
        return cls(
            title=str(payload.get("title", "") or ""),
            selling_points=[str(s) for s in (payload.get("selling_points") or [])],
            banned_words=[str(w) for w in (payload.get("banned_words") or [])],
            model_name=str(payload.get("model_name", "") or ""),
            prompt_snapshot=str(payload.get("prompt_snapshot", "") or ""),
            candidates=candidates,
        )


@dataclass
class AiAttributeResult:
    """属性建议结果。"""

    attributes_json: dict[str, Any] = field(default_factory=dict)
    category_id: str = ""
    model_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "attributes_json": dict(self.attributes_json),
            "category_id": self.category_id,
            "model_name": self.model_name,
        }


@dataclass
class AiReworkResult:
    """一次完整重构的产出。"""

    images: list[AiImageResult] = field(default_factory=list)
    title_result: AiTitleResult | None = None
    attribute_result: AiAttributeResult | None = None
    model_name: str = ""
    prompt_snapshot: str = ""
    elapsed_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化（写入 ai_output/result.json 与 ai_task_result）。"""
        return {
            "images": [img.to_dict() for img in self.images],
            "title_result": self.title_result.to_dict() if self.title_result else None,
            "attribute_result": self.attribute_result.to_dict() if self.attribute_result else None,
            "model_name": self.model_name,
            "prompt_snapshot": self.prompt_snapshot,
            "elapsed_ms": self.elapsed_ms,
            "raw": dict(self.raw),
        }


class AiClient(ABC):
    """AI 重构客户端接口。

    约定：**绝不抛裸异常**打断任务流程；失败时返回空结果或抛 `AiTimeoutError`（可重试）。
    """

    client_name: str = ""

    @abstractmethod
    async def rework_images(self, ctx: AiTaskContext) -> AiReworkResult:
        """重构图片（主图 / 详情图）。"""
        raise NotImplementedError

    @abstractmethod
    async def rewrite_title(self, ctx: AiTaskContext) -> AiTitleResult:
        """改写标题与卖点。"""
        raise NotImplementedError

    @abstractmethod
    async def suggest_attributes(self, ctx: AiTaskContext) -> AiAttributeResult:
        """建议平台类目属性。"""
        raise NotImplementedError

    async def health_check(self) -> dict[str, Any]:
        """自检（默认实现：返回客户端名）。"""
        return {"client": self.client_name, "healthy": True}

    def describe(self) -> dict[str, Any]:
        """客户端自述。"""
        return {"client": self.client_name, "class": type(self).__name__}
