"""MockAiClient —— MVP 默认 AI 客户端（ARCH §11.2 N2）。

行为：
    * 生成确定性占位图（纯 Python 写 PNG，无第三方依赖），保证素材库可跑通；
    * 标题改写 = 在原标题基础上加平台前缀与卖点后缀，并做违禁词扫描；
    * 属性建议 = 从上下文拷贝 + 补充通用字段。

★ 产出一律标记 `is_placeholder=True`，前端与审核流程据此提示"未经过真实 AI 重构"。
"""

from __future__ import annotations

import struct
import time
import zlib
from pathlib import Path
from typing import Any

from app.adapters.ai.base import (
    AiAttributeResult,
    AiClient,
    AiImageResult,
    AiReworkResult,
    AiTaskContext,
    AiTitleResult,
)
from app.core.config import get_settings
from app.core.logging import get_logger
from app.utils.kit import content_hash_bytes, ensure_dir, safe_filename

logger = get_logger(__name__)

__all__ = ["MockAiClient", "BANNED_WORDS", "write_placeholder_png"]

# 常见极限词 / 违禁词（AIR-P0-04，真实实现应由模型或词库产出）
BANNED_WORDS: list[str] = [
    "最",
    "第一",
    "国家级",
    "顶级",
    "极致",
    "永久",
    "万能",
    "包治",
    "绝对",
    "100%",
]

PLATFORM_PREFIX = {
    "taobao": "【淘宝专供】",
    "douyin": "【抖店爆款】",
    "pdd": "【拼多多实惠】",
}


def write_placeholder_png(path: str | Path, width: int = 800, height: int = 800, rgb: tuple[int, int, int] = (240, 240, 245)) -> int:
    """纯 Python 生成占位 PNG（无 Pillow 依赖），返回文件字节数。

    图片中央绘制一条对角线与纯色底，肉眼可辨为占位图。
    """
    width = max(int(width), 1)
    height = max(int(height), 1)
    raw_rows = bytearray()
    for y in range(height):
        raw_rows.append(0)  # filter type 0
        for x in range(width):
            # 对角线 + 边框，颜色加深
            on_edge = x < 4 or y < 4 or x >= width - 4 or y >= height - 4
            on_diagonal = abs(x - y) < 6 or abs((width - x) - y) < 6
            if on_edge:
                pixel = (180, 180, 190)
            elif on_diagonal:
                pixel = (200, 200, 210)
            else:
                pixel = rgb
            raw_rows.extend(pixel)

    def _chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8bit truecolor
    png = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(bytes(raw_rows), 6)) + _chunk(b"IEND", b"")

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(png)
    return len(png)


class MockAiClient(AiClient):
    """Mock AI 客户端：占位图 + 标题改写。"""

    client_name = "mock"

    def __init__(self, output_dir: str | Path | None = None) -> None:
        """初始化 Mock 客户端（产出目录默认 `data/assets/mock`）。"""
        settings = get_settings()
        self.output_dir = ensure_dir(Path(output_dir) if output_dir else settings.assets_dir / "mock")

    # ---------------- 图片重构 ----------------

    async def rework_images(self, ctx: AiTaskContext) -> AiReworkResult:
        """生成占位重构图（主图 + 详情图）。"""
        started = time.perf_counter()
        task_dir = ensure_dir(self.output_dir / safe_filename(ctx.task_id or "unknown"))
        images: list[AiImageResult] = []

        wants_detail = "detail_image" in ctx.rework_items or not ctx.rework_items
        count = max(len(ctx.source_image_paths), 1)
        for index in range(count):
            name = "main_01.png" if index == 0 else f"detail_{index:02d}.png"
            target = task_dir / name
            size = write_placeholder_png(target, width=800, height=800 if index == 0 else 1000)
            prompt = self._image_prompt(ctx, index)
            images.append(
                AiImageResult(
                    local_path=str(target),
                    width=800,
                    height=800 if index == 0 else 1000,
                    size_bytes=size,
                    content_hash=content_hash_bytes(target.read_bytes()),
                    is_placeholder=True,
                    prompt=prompt,
                )
            )
            if index == 0 and not wants_detail:
                break

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        result = AiReworkResult(
            images=images,
            model_name="mock-placeholder",
            prompt_snapshot=self._image_prompt(ctx, 0),
            elapsed_ms=elapsed_ms,
            raw={"mode": "mock", "image_count": len(images)},
        )
        logger.info("mock_ai_rework_images", task_id=ctx.task_id, count=len(images))
        return result

    def _image_prompt(self, ctx: AiTaskContext, index: int) -> str:
        """生成可读的 Prompt 快照（供复现与成本归因）。"""
        role = "商品主图" if index == 0 else "详情图"
        return (
            f"[{role}] 平台={ctx.target_platform or '未指定'}；"
            f"原标题={ctx.original_title}；"
            f"卖点={'/'.join(ctx.selling_points) or '无'}；"
            f"要求：白底 / 主体居中 / 无水印 / 无中文极限词。"
        )

    # ---------------- 标题改写 ----------------

    async def rewrite_title(self, ctx: AiTaskContext) -> AiTitleResult:
        """改写标题（加平台前缀 + 卖点后缀），并扫描违禁词。"""
        prefix = PLATFORM_PREFIX.get(ctx.target_platform, "")
        base = (ctx.original_title or "未命名商品").strip()
        suffix = " ".join(ctx.selling_points[:3])
        title = f"{prefix}{base}"
        if suffix:
            title = f"{title} {suffix}"
        title = title[:60]

        return AiTitleResult(
            title=title,
            selling_points=list(ctx.selling_points) or ["品质保障", "现货速发"],
            banned_words=scan_banned_words(title),
            model_name="mock-rewrite",
            prompt_snapshot=f"改写标题：平台={ctx.target_platform}，原标题={base}",
        )

    # ---------------- 属性建议 ----------------

    async def suggest_attributes(self, ctx: AiTaskContext) -> AiAttributeResult:
        """建议平台类目属性（基于上下文 + 通用字段）。"""
        attributes: dict[str, Any] = dict(ctx.attributes_json or {})
        attributes.setdefault("品牌", "未填写")
        attributes.setdefault("产地", "中国")
        attributes.setdefault("是否支持一件代发", "是")
        return AiAttributeResult(
            attributes_json=attributes,
            category_id=str(ctx.extra.get("category_id", "")),
            model_name="mock-attribute",
        )

    async def health_check(self) -> dict[str, Any]:
        """Mock 客户端始终健康。"""
        return {"client": self.client_name, "healthy": True, "output_dir": str(self.output_dir)}


def scan_banned_words(text: str) -> list[str]:
    """扫描违禁词 / 极限词（AIR-P0-04）。"""
    if not text:
        return []
    return [word for word in BANNED_WORDS if word in text]
