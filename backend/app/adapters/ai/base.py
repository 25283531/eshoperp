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
    "AiImageResult",
    "AiReworkResult",
    "AiTaskContext",
    "AiTitleResult",
    "AiAttributeResult",
    "AiTimeoutError",
]


class AiTimeoutError(Exception):
    """等待 AI 产出超时（可重试）。"""


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
    extra: dict[str, Any] = field(default_factory=dict)

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
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AiTaskContext":
        """反序列化。"""
        return cls(
            task_id=str(raw.get("task_id", "")),
            target_platform=str(raw.get("target_platform", "")),
            source_product_id=int(raw.get("source_product_id", 0) or 0),
            original_title=str(raw.get("original_title", "")),
            rework_items=list(raw.get("rework_items", [])),
            source_image_paths=list(raw.get("source_image_paths", [])),
            selling_points=list(raw.get("selling_points", [])),
            attributes_json=dict(raw.get("attributes_json", {})),
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
class AiTitleResult:
    """标题改写结果。"""

    title: str
    selling_points: list[str] = field(default_factory=list)
    banned_words: list[str] = field(default_factory=list)
    model_name: str = ""
    prompt_snapshot: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "title": self.title,
            "selling_points": list(self.selling_points),
            "banned_words": list(self.banned_words),
            "model_name": self.model_name,
            "prompt_snapshot": self.prompt_snapshot,
        }


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
