"""MockAiClient —— 离线可跑的占位 AI 客户端（ARCH §11.2 N2）。

行为：
    * 生成确定性占位图（纯 Python 写 PNG，无第三方依赖），保证素材库可跑通；
    * 标题改写 = 在原标题基础上加平台前缀与卖点后缀，并做违禁词扫描；
    * 属性建议 = 从上下文拷贝 + 补充通用字段；
    * **新能力**：图片重绘 / 标题建议（≥3 条互不相同候选）/ 短视频拍摄脚本，全部离线可跑。

★ 产出一律标记 `is_placeholder=True`，前端与审核流程据此提示"未经过真实 AI 重构"。
★ 本客户端的价值是"链路可跑通 + 契约可验证"，不是"内容好看"：文案均为确定性模板拼装。
"""

from __future__ import annotations

import struct
import time
import zlib
from pathlib import Path
from typing import Any

from app.adapters.ai.base import (
    PROMPT_SOURCE_DEFAULT,
    PROMPT_SOURCE_GLOBAL,
    PROMPT_SOURCE_PER_IMAGE,
    AiAttributeResult,
    AiClient,
    AiImageResult,
    AiRedrawResult,
    AiReworkResult,
    AiTaskContext,
    AiTitleCandidate,
    AiTitleResult,
    AiTitleSuggestResult,
    AiVideoScriptResult,
    AiVideoScriptScene,
)
from app.core.config import get_settings
from app.core.logging import get_logger
from app.utils.kit import content_hash_bytes, ensure_dir, safe_filename

logger = get_logger(__name__)

__all__ = [
    "BANNED_WORDS",
    "MockAiClient",
    "TITLE_CANDIDATE_TEMPLATES",
    "VIDEO_SCENE_TEMPLATES",
    "write_placeholder_png",
]

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

# ★ 标题候选模板：(适配平台, 风格, 选择依据, 标题模板, 基础分)
#   ★ 存在的理由：使用者要的是"能挑一条"，三条长得一样的标题等于没给建议。
#     因此每条的**风格与依据必须不同**，并按目标平台给分（命中目标平台 +10 分，
#     于是 `preferred()` 默认选中的就是最贴合当前平台那条）。
TITLE_CANDIDATE_TEMPLATES: list[tuple[str, str, str, str, int]] = [
    (
        "taobao",
        "搜索词堆砌流",
        "关键词前置、覆盖淘宝搜索习惯，适合吃搜索流量",
        "{base} {points} 官方标配",
        80,
    ),
    (
        "douyin",
        "热词口语流",
        "口语化 + 平台热词，适合短视频挂车与推荐流",
        "这款{base}真香 {first_point}",
        78,
    ),
    (
        "pdd",
        "价格力直给流",
        "直给低价心智与规格，适合比价场景",
        "{base} 源头工厂 {first_point} 超值装",
        76,
    ),
    (
        "",
        "人群场景流",
        "点名适用人群与使用场景，适合精准人群投放（平台通用）",
        "{base}｜{first_point} 日常通用",
        72,
    ),
]

# ★ 视频脚本的分镜骨架：(建议时长, 画面, 机位/运镜, 拍摄要点, 口播台词模板)
VIDEO_SCENE_TEMPLATES: list[tuple[float, str, str, str, str]] = [
    (
        3.0,
        "商品正面特写，从桌面推向镜头",
        "固定机位，缓慢推近",
        "纯色背景，主光源 45° 补光，避免反光",
        "别再挑了，这款{base}我劝你直接看这一条。",
    ),
    (
        5.0,
        "依次展示卖点：{points}",
        "手持俯拍 45°，随卖点平移",
        "每个卖点停留不少于 1.5 秒，配字幕",
        "{points}，这几个点才是它值这个价的原因。",
    ),
    (
        4.0,
        "材质与做工细节微距特写",
        "微距特写，浅景深",
        "对准最能体现质感的一处，手部保持稳定",
        "细节看这里，做工到底怎么样一目了然。",
    ),
    (
        3.0,
        "商品 + 使用场景全景收尾",
        "固定机位缓慢拉远",
        "结尾留 1 秒空白，便于加引导贴纸",
        "{first_point}，需要的直接下手，别等补货。",
    ),
]


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

    # ---------------- 新能力：图片重绘 ----------------

    def _resolve_image_prompt(self, ctx: AiTaskContext, index: int) -> tuple[str, str]:
        """★ 取第 `index` 张图"最终生效"的提示词，并**说清它从哪来**。

        Returns:
            `(prompt, prompt_source)`：逐图 → 全局 → 客户端默认，三级回落。

        ★ 为什么不直接用 `ctx.image_prompt(index)`：那个方法把"逐图"与"全局"回落后的结果
          混成一个字符串，回显时就说不清这张图到底有没有被单独指定过 —— 而"使用者明明写了
          逐图提示词却没生效"是本能力最需要能复盘的场景。
        """
        for item in ctx.image_prompts:
            if int(item.index) == int(index) and item.prompt:
                return item.prompt, PROMPT_SOURCE_PER_IMAGE
        if ctx.global_prompt:
            return ctx.global_prompt, PROMPT_SOURCE_GLOBAL
        return self._image_prompt(ctx, index), PROMPT_SOURCE_DEFAULT

    async def redraw_images(self, ctx: AiTaskContext) -> AiRedrawResult:
        """图片重绘：逐张产出占位图，**每张按它自己的提示词**（逐图 → 全局 → 默认）。"""
        started = time.perf_counter()
        task_dir = ensure_dir(self.output_dir / safe_filename(ctx.task_id or "unknown"))
        images: list[AiImageResult] = []

        count = max(len(ctx.source_image_paths), 1)
        for index in range(count):
            is_main = index == 0
            name = "main_01.png" if is_main else f"detail_{index:02d}.png"
            target = task_dir / f"redraw_{name}"
            size = write_placeholder_png(target, width=800, height=800 if is_main else 1000)
            prompt, prompt_source = self._resolve_image_prompt(ctx, index)
            images.append(
                AiImageResult(
                    local_path=str(target),
                    width=800,
                    height=800 if is_main else 1000,
                    size_bytes=size,
                    content_hash=content_hash_bytes(target.read_bytes()),
                    is_placeholder=True,
                    prompt=prompt,
                    index=index,
                    source_path=ctx.source_image_paths[index] if index < len(ctx.source_image_paths) else "",
                    image_role="main_image" if is_main else "detail_image",
                    prompt_source=prompt_source,
                )
            )

        result = AiRedrawResult(
            images=images,
            model_name="mock-placeholder",
            prompt_snapshot="; ".join(f"#{i}={img.prompt}" for i, img in enumerate(images)),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            raw={"mode": "mock", "image_count": len(images)},
        )
        logger.info("mock_ai_redraw_images", task_id=ctx.task_id, count=len(images))
        return result

    # ---------------- 新能力：标题建议 ----------------

    async def suggest_titles(self, ctx: AiTaskContext) -> AiTitleSuggestResult:
        """标题建议：**≥3 条互不相同**的候选，每条带风格与选择依据。"""
        started = time.perf_counter()
        base = (ctx.original_title or "未命名商品").strip()
        points = list(ctx.selling_points) or ["品质保障", "现货速发"]
        first_point = str(points[0])
        joined_points = " ".join(str(p) for p in points[:3])

        candidates: list[AiTitleCandidate] = []
        for platform_fit, style, reason, pattern, score in TITLE_CANDIDATE_TEMPLATES:
            title = pattern.format(
                base=base, points=joined_points, first_point=first_point, scene="日常通勤"
            )[:60]
            # ★ 命中目标平台的候选加分：让 `preferred()` 默认选中就是最贴合当前平台那条
            bonus = 10 if (platform_fit and platform_fit == ctx.target_platform) else 0
            if ctx.title_prompt:
                reason = f"{reason}；已按你的补充要求「{ctx.title_prompt}」调整"
            candidates.append(
                AiTitleCandidate(
                    title=title,
                    selling_points=list(points),
                    style=style,
                    reason=reason,
                    platform_fit=platform_fit,
                    score=score + bonus,
                    banned_words=scan_banned_words(title),
                )
            )

        # ★ 去重兜底：模板正常情况下不会撞车，但原标题为空 / 卖点为空等边界下可能撞，
        #   撞了就必须改名 —— "≥3 条互不相同"是本能力的硬承诺，不能靠运气。
        seen: set[str] = set()
        for candidate in candidates:
            title = candidate.title
            seq = 1
            while title in seen or not title.strip():
                seq += 1
                title = f"{candidate.title}（方案{seq}）"[:60]
            candidate.title = title
            seen.add(title)

        return AiTitleSuggestResult(
            candidates=candidates,
            platform=ctx.target_platform,
            model_name="mock-title-suggest",
            prompt_snapshot=ctx.title_prompt or f"标题建议：平台={ctx.target_platform}，原标题={base}",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            raw={"mode": "mock", "candidate_count": len(candidates)},
        )

    # ---------------- 新能力：短视频脚本 ----------------

    async def suggest_video_script(self, ctx: AiTaskContext) -> AiVideoScriptResult:
        """短视频拍摄脚本：**只出文案**（分镜 / 时长 / 机位 / 台词），不生成任何视频。"""
        started = time.perf_counter()
        base = (ctx.original_title or "未命名商品").strip()
        points = list(ctx.selling_points) or ["品质保障", "现货速发"]
        first_point = str(points[0])
        joined_points = "、".join(str(p) for p in points[:3])

        scenes = [
            AiVideoScriptScene(
                index=position,
                duration_sec=duration,
                shot=shot.format(base=base, points=joined_points, first_point=first_point),
                camera=camera,
                shooting_tips=tips,
                narration=narration.format(base=base, points=joined_points, first_point=first_point),
            )
            for position, (duration, shot, camera, tips, narration) in enumerate(VIDEO_SCENE_TEMPLATES, start=1)
        ]

        return AiVideoScriptResult(
            title=f"{base} 短视频拍摄脚本",
            style=ctx.video_script_prompt or "通用电商短视频（竖屏、节奏明快）",
            scenes=scenes,
            total_duration_sec=round(sum(s.duration_sec for s in scenes), 2),
            model_name="mock-video-script",
            prompt_snapshot=ctx.video_script_prompt or "（使用者未指定拍摄风格，按通用口径生成）",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            raw={"mode": "mock", "scene_count": len(scenes)},
        )

    async def health_check(self) -> dict[str, Any]:
        """Mock 客户端始终健康。"""
        return {"client": self.client_name, "healthy": True, "output_dir": str(self.output_dir)}


def scan_banned_words(text: str) -> list[str]:
    """扫描违禁词 / 极限词（AIR-P0-04）。"""
    if not text:
        return []
    return [word for word in BANNED_WORDS if word in text]
