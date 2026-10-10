"""AI 重构客户端接口（ARCH §11.2 N2 裁决落地）。

三种实现：
    mock          返回占位图 + 改写标题
    file_bridge   把任务写成 data/ai_queue/<task_id>/{task.json,prompt.md}，
                  轮询回读 data/ai_output/<task_id>/result.json（与 WorkBuddy 协作的真实路径）
    http          OpenAI 兼容接口（base_url / api_key / model 从配置读）

★ 三种**新能力**（本轮落地，对应 AiTaskType 的后三个取值）：
    redraw_images(ctx)        图片重绘 —— 主图 / 详情页图重画，**逐图可单独给提示词**
    suggest_titles(ctx)       商品标题建议 —— **≥3 条互不相同**的候选供挑选
    suggest_video_script(ctx) 短视频**拍摄脚本文案**（只出文案，不生成视频）
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
    "AiRedrawResult",
    "AiReworkResult",
    "AiTaskContext",
    "AiTitleCandidate",
    "AiTitleResult",
    "AiTitleSuggestResult",
    "AiAttributeResult",
    "AiTimeoutError",
    "AiVideoScriptResult",
    "AiVideoScriptScene",
    "MIN_TITLE_CANDIDATES",
    "PROMPT_SOURCE_DEFAULT",
    "PROMPT_SOURCE_GLOBAL",
    "PROMPT_SOURCE_PER_IMAGE",
]

# ★ 提示词来源标记（填 `AiImageResult.prompt_source`）：回答"这张图到底按哪句提示词画的"。
#   没有它，回显页面只能看到一句 prompt，却说不清它是使用者针对这张图单独写的，
#   还是没有逐图提示词时从全局回退来的 —— 复盘时无法判断"AI 没照做"是谁的问题。
PROMPT_SOURCE_PER_IMAGE = "per_image"  # 使用者为这张图单独输入
PROMPT_SOURCE_GLOBAL = "global"  # 该图未单独指定，回落到全局提示词
PROMPT_SOURCE_DEFAULT = "default"  # 连全局提示词都没有，用客户端默认口径


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

    ★★ 键名口径（写这段是为了防止后人按错键写代码）★★
        落库 / 序列化的**唯一**键名是 `images`（见 `to_dict()`）；
        `from_dict()` **同时接受** `image_prompts` 作为它的别名（与 `AiTaskContext.image_prompts`
        同名，便于两侧互灌）。别名只存在于**读**方向，写出去永远是 `images`
        —— 否则同一份数据在库里会有两种形态，那又是"两个真相源"。
    """

    global_prompt: str = ""
    images: list[AiImagePrompt] = field(default_factory=list)
    title_prompt: str = ""
    video_script_prompt: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def image_prompts(self) -> list[AiImagePrompt]:
        """`images` 的**只读**别名（与 `AiTaskContext.image_prompts` 同名，便于两侧互灌）。"""
        return self.images

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
        # ★ 兼容别名：`images` 是唯一落库键，`image_prompts` 只是读方向的别名。
        raw_images = payload.get("images")
        if not isinstance(raw_images, list):
            raw_images = payload.get("image_prompts")
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
    """单张重构 / 重绘图结果。

    ★ 后四个字段是**图片重绘**新能力要求补的：
        `image_role`     区分这张是主图还是详情图（取值同 `AiReworkItem`，空 = 产出方未标）；
        `index` / `source_path`  与 `AiTaskContext.source_image_paths` 的下标 / 路径对应，
                         回答"这张产出是由哪张原图来的"；
        `prompt_source`  这张图的 `prompt` 从哪来（per_image / global / default，见模块常量），
                         用于回显与复盘。
    """

    local_path: str
    width: int = 0
    height: int = 0
    size_bytes: int = 0
    content_hash: str = ""
    is_placeholder: bool = False
    prompt: str = ""
    index: int = 0
    source_path: str = ""
    image_role: str = ""
    prompt_source: str = ""

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
            "index": int(self.index),
            "source_path": self.source_path,
            "image_role": self.image_role,
            "prompt_source": self.prompt_source,
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
        return cls(
            title=str(payload.get("title", "") or ""),
            selling_points=[str(s) for s in (payload.get("selling_points") or [])],
            style=str(payload.get("style", "") or ""),
            reason=str(payload.get("reason", "") or ""),
            platform_fit=str(payload.get("platform_fit", "") or ""),
            score=_safe_int(payload.get("score"), 0) or 0,
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


# ============================================================================
#  新能力契约（图片重绘 / 标题建议 / 短视频脚本）
# ============================================================================


def _safe_float(value: Any, default: float = 0.0) -> float:
    """把外部 JSON 值安全地转成 float（★ 边界数据：失败退化为兜底值，绝不抛异常）。"""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if number == number else default  # NaN → default


@dataclass
class AiRedrawResult:
    """**图片重绘**的产出（`redraw_images()` 的返回类型）。

    ★ 与 `AiReworkResult` 的分工：
        `AiReworkResult`  老口径「图文重构」一体产出（图 + 标题 + 属性），`persist_result()` 消费它；
        本类             只承载**图片**这一件事（主图 / 详情图重绘），不掺标题与属性。
    ★ 下游落 `asset` 只看 `images[].local_path`（`persist_result()` 现成逻辑即可消费）。
    """

    images: list[AiImageResult] = field(default_factory=list)
    model_name: str = ""
    prompt_snapshot: str = ""
    elapsed_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    def main_images(self) -> list[AiImageResult]:
        """主图产出（`image_role == "main_image"`）。"""
        return [img for img in self.images if img.image_role == "main_image"]

    def detail_images(self) -> list[AiImageResult]:
        """详情图产出（`image_role == "detail_image"`）。"""
        return [img for img in self.images if img.image_role == "detail_image"]

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "images": [img.to_dict() for img in self.images],
            "model_name": self.model_name,
            "prompt_snapshot": self.prompt_snapshot,
            "elapsed_ms": self.elapsed_ms,
            "raw": dict(self.raw),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "AiRedrawResult":
        """反序列化（`result.json` 的 `redraw` 块；脏数据一律退化为空产出，绝不抛异常）。"""
        payload = raw if isinstance(raw, dict) else {}
        images: list[AiImageResult] = []
        for item in payload.get("images") or []:
            if not isinstance(item, dict):
                continue
            images.append(
                AiImageResult(
                    local_path=str(item.get("local_path", "") or ""),
                    width=_safe_int(item.get("width"), 0) or 0,
                    height=_safe_int(item.get("height"), 0) or 0,
                    size_bytes=_safe_int(item.get("size_bytes"), 0) or 0,
                    content_hash=str(item.get("content_hash", "") or ""),
                    is_placeholder=bool(item.get("is_placeholder", False)),
                    prompt=str(item.get("prompt", "") or ""),
                    index=_safe_int(item.get("index"), 0) or 0,
                    source_path=str(item.get("source_path", "") or ""),
                    image_role=str(item.get("image_role", "") or ""),
                    prompt_source=str(item.get("prompt_source", "") or ""),
                )
            )
        return cls(
            images=images,
            model_name=str(payload.get("model_name", "") or ""),
            prompt_snapshot=str(payload.get("prompt_snapshot", "") or ""),
            elapsed_ms=_safe_int(payload.get("elapsed_ms"), 0) or 0,
            raw=payload,
        )


@dataclass
class AiTitleSuggestResult:
    """**商品标题建议**的产出（`suggest_titles()` 的返回类型）。

    ★★ 与 `AiTitleResult` 的关系（向后兼容口径）★★
        `AiTitleResult`        老口径「改写标题」，`title` 是**单条首选**，被 `persist_result()` 消费；
        本类                  新口径「给候选」，要求**≥3 条互不相同**且每条带选择依据。
        两者用 `to_title_result()` 打通：把候选里最高分那条降级成老口径的单条结果，
        因此**新增能力也能直接喂给现有的 `persist_result()`**，不必等下一棒改落库。
    """

    candidates: list[AiTitleCandidate] = field(default_factory=list)
    platform: str = ""
    model_name: str = ""
    prompt_snapshot: str = ""
    elapsed_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    def preferred(self) -> AiTitleCandidate | None:
        """最高分候选（无候选返回 None —— ★ 不伪造一条假标题出来）。"""
        if not self.candidates:
            return None
        return max(self.candidates, key=lambda c: int(c.score))

    def to_title_result(self) -> AiTitleResult:
        """★ 降级成老口径的单条结果（`persist_result()` 可直接消费）。

        `candidates` 为空时返回 `title=""` 的空结果 —— 宁可让上层看到"没有产出"，
        也不静默拿原标题冒充 AI 建议（冒充会让使用者以为 AI 真的给过意见）。
        """
        best = self.preferred()
        if best is None:
            return AiTitleResult(
                title="",
                model_name=self.model_name,
                prompt_snapshot=self.prompt_snapshot,
            )
        return AiTitleResult(
            title=best.title,
            selling_points=list(best.selling_points),
            banned_words=list(best.banned_words),
            model_name=self.model_name,
            prompt_snapshot=self.prompt_snapshot,
            candidates=list(self.candidates),
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "candidates": [c.to_dict() for c in self.candidates],
            "platform": self.platform,
            "model_name": self.model_name,
            "prompt_snapshot": self.prompt_snapshot,
            "elapsed_ms": self.elapsed_ms,
            "raw": dict(self.raw),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "AiTitleSuggestResult":
        """反序列化（`result.json` 的 `title_suggest` 块；脏数据退化为空候选）。"""
        payload = raw if isinstance(raw, dict) else {}
        raw_candidates = payload.get("candidates")
        candidates: list[AiTitleCandidate] = []
        if isinstance(raw_candidates, list):
            candidates = [AiTitleCandidate.from_dict(c) for c in raw_candidates if isinstance(c, dict)]
        return cls(
            candidates=candidates,
            platform=str(payload.get("platform", "") or ""),
            model_name=str(payload.get("model_name", "") or ""),
            prompt_snapshot=str(payload.get("prompt_snapshot", "") or ""),
            elapsed_ms=_safe_int(payload.get("elapsed_ms"), 0) or 0,
            raw=payload,
        )


@dataclass
class AiVideoScriptScene:
    """短视频脚本的**一个分镜**（`AiVideoScriptResult.scenes` 的元素）。

    ★ 使用者要的是"拿去就能拍"的文案，所以每个分镜必须同时回答四件事：
        拍什么（`shot`）/ 怎么拍（`camera` + `shooting_tips`）/ 说啥（`narration`）/ 多长（`duration_sec`）。
    """

    index: int = 0  # 分镜序号（1 起）
    duration_sec: float = 0.0  # 建议时长（秒）
    shot: str = ""  # 画面内容（观众看到什么）
    camera: str = ""  # 机位 / 运镜
    shooting_tips: str = ""  # 拍摄要点（布光、道具、注意事项）
    narration: str = ""  # 口播台词

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "index": int(self.index),
            "duration_sec": float(self.duration_sec),
            "shot": self.shot,
            "camera": self.camera,
            "shooting_tips": self.shooting_tips,
            "narration": self.narration,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "AiVideoScriptScene":
        """反序列化（脏数据退化为空分镜）。"""
        payload = raw if isinstance(raw, dict) else {}
        return cls(
            index=_safe_int(payload.get("index"), 0) or 0,
            duration_sec=_safe_float(payload.get("duration_sec")),
            shot=str(payload.get("shot", "") or ""),
            camera=str(payload.get("camera", "") or ""),
            shooting_tips=str(payload.get("shooting_tips", "") or ""),
            narration=str(payload.get("narration", "") or ""),
        )


@dataclass
class AiVideoScriptResult:
    """**短视频拍摄脚本**的产出（`suggest_video_script()` 的返回类型）。

    ★★ 只出文案，**不生成视频** ★★
        使用者拿这份脚本自己去拍；因此本结构不含任何视频文件路径 / 二进制。
        `style` 回填使用者输入的拍摄风格（`ctx.video_script_prompt`），便于回显"按哪句话写的"。
    """

    title: str = ""
    style: str = ""
    scenes: list[AiVideoScriptScene] = field(default_factory=list)
    total_duration_sec: float = 0.0
    model_name: str = ""
    prompt_snapshot: str = ""
    elapsed_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def scene_count(self) -> int:
        """分镜数量。"""
        return len(self.scenes)

    @property
    def effective_duration_sec(self) -> float:
        """**最终生效**的总时长：产出方给了就用给的，没给就按分镜累加。"""
        if self.total_duration_sec > 0:
            return float(self.total_duration_sec)
        return round(sum(float(s.duration_sec) for s in self.scenes), 2)

    def to_text(self) -> str:
        """渲染成**人可直接照着拍**的纯文本（只出文案，不产生任何媒体文件）。"""
        lines = [f"# {self.title or '短视频拍摄脚本'}"]
        if self.style:
            lines.append(f"- 拍摄风格：{self.style}")
        lines.append(f"- 分镜数：{self.scene_count}；总时长：约 {self.effective_duration_sec:g} 秒")
        for scene in self.scenes:
            lines.append("")
            lines.append(f"## 分镜 {scene.index}（{scene.duration_sec:g}s）")
            lines.append(f"- 画面：{scene.shot}")
            if scene.camera:
                lines.append(f"- 机位/运镜：{scene.camera}")
            if scene.shooting_tips:
                lines.append(f"- 拍摄要点：{scene.shooting_tips}")
            if scene.narration:
                lines.append(f"- 口播：{scene.narration}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "title": self.title,
            "style": self.style,
            "scenes": [s.to_dict() for s in self.scenes],
            "total_duration_sec": float(self.total_duration_sec),
            "scene_count": self.scene_count,
            "model_name": self.model_name,
            "prompt_snapshot": self.prompt_snapshot,
            "elapsed_ms": self.elapsed_ms,
            "raw": dict(self.raw),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "AiVideoScriptResult":
        """反序列化（`result.json` 的 `video_script` 块；脏数据退化为空脚本）。"""
        payload = raw if isinstance(raw, dict) else {}
        raw_scenes = payload.get("scenes")
        scenes: list[AiVideoScriptScene] = []
        if isinstance(raw_scenes, list):
            scenes = [AiVideoScriptScene.from_dict(s) for s in raw_scenes if isinstance(s, dict)]
        return cls(
            title=str(payload.get("title", "") or ""),
            style=str(payload.get("style", "") or ""),
            scenes=scenes,
            total_duration_sec=_safe_float(payload.get("total_duration_sec")),
            model_name=str(payload.get("model_name", "") or ""),
            prompt_snapshot=str(payload.get("prompt_snapshot", "") or ""),
            elapsed_ms=_safe_int(payload.get("elapsed_ms"), 0) or 0,
            raw=payload,
        )


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

    # ---------------- 新能力（AiTaskType 后三个取值）----------------

    @abstractmethod
    async def redraw_images(self, ctx: AiTaskContext) -> AiRedrawResult:
        """★ 图片重绘：按 `AiTaskType.IMAGE_REDRAW` 重画主图 / 详情页图。

        ★ 逐图提示词从 `ctx.image_prompt(index)` 取（该方法自带"逐图 → 全局"回退），
          实现方**不要**自己去 `ctx.image_prompts` 里找，否则回退口径会在三个客户端里各写一遍。
        """
        raise NotImplementedError

    @abstractmethod
    async def suggest_titles(self, ctx: AiTaskContext) -> AiTitleSuggestResult:
        """★ 商品标题建议：给出 **≥3 条互不相同**的候选，每条带选择依据。

        提示词从 `ctx.title_prompt` 取（为空时按目标平台风格自由发挥）。
        """
        raise NotImplementedError

    @abstractmethod
    async def suggest_video_script(self, ctx: AiTaskContext) -> AiVideoScriptResult:
        """★ 短视频**拍摄脚本文案**（只出文案，不生成视频）。

        拍摄风格 / 内容倾向从 `ctx.video_script_prompt` 取。
        """
        raise NotImplementedError

    async def health_check(self) -> dict[str, Any]:
        """自检（默认实现：返回客户端名）。"""
        return {"client": self.client_name, "healthy": True}

    def describe(self) -> dict[str, Any]:
        """客户端自述。"""
        return {"client": self.client_name, "class": type(self).__name__}
