"""WorkBuddyFileBridgeClient —— 通过文件系统与 WorkBuddy AI 协作（真实路径）。

工作流：
    1. 写任务：`data/ai_queue/<task_id>/task.json`（机器可读）
                 `data/ai_queue/<task_id>/prompt.md`（人类可读，含原图本地路径 / 原始标题 / 目标平台 / 重构项）
    2. 轮询回读：`data/ai_output/<task_id>/result.json`
    3. 超时未产出 → 抛 `AiTimeoutError`（可重试，由 TaskRunner 按 max_retry 处理）

★ 这是与 WorkBuddy 协作的真实路径：AI 侧只需按目录约定读写文件，无需 API 对接。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from app.adapters.ai.base import (
    AiAttributeResult,
    AiClient,
    AiImageResult,
    AiReworkResult,
    AiTaskContext,
    AiTimeoutError,
    AiTitleResult,
)
from app.core.config import get_settings
from app.core.logging import get_logger
from app.utils.kit import content_hash_file, ensure_dir, iso_utc, utc_now

logger = get_logger(__name__)

__all__ = ["WorkBuddyFileBridgeClient", "PROMPT_TEMPLATE", "RESULT_FILENAME"]

RESULT_FILENAME = "result.json"
TASK_FILENAME = "task.json"
PROMPT_FILENAME = "prompt.md"

# ★ 等待上界（事务纪律 B）：文件桥是**人工 / WorkBuddy 在环**的异步协作，
#   等待必须有确定的有限上界，绝不允许"一直等下去"——
#   否则一个卡住的任务会把整个任务队列（以及 SQLite 的写能力）拖死。
MIN_POLL_INTERVAL_SEC: float = 0.1  # 轮询间隔下限（防止 0 造成忙等烧 CPU）
MAX_POLL_INTERVAL_SEC: float = 60.0  # 轮询间隔上限（防止配成小时级导致错过产出）
MIN_POLL_TIMEOUT_SEC: float = 1.0  # 超时下限（≤0 一律按 1s 处理，立即超时而不是死等）
HARD_MAX_TIMEOUT_SEC: float = 3600.0  # 代码级硬上界：配置再大也压到这里


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    """把配置值夹到 `[low, high]`；非法值（None / NaN / 非数字）回退 `default`。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number != number:  # NaN
        return float(default)
    return max(low, min(high, number))

PROMPT_TEMPLATE = """# AI 图文重构任务

- **任务 ID**：{task_id}
- **目标平台**：{platform_label}
- **货源商品 ID**：{source_product_id}
- **原始标题**：{original_title}
- **重构项**：{rework_items}
- **创建时间**：{created_at}（UTC）

## 原始图片（本地路径）

{image_list}

## 卖点参考

{selling_points}

## 参考属性

```json
{attributes_json}
```

## 交付要求

1. 重构后的图片保存到 `data/ai_output/{task_id}/` 目录（主图 `main_01.png`，详情图 `detail_01.png` …）；
2. 在同目录写 `result.json`，结构如下（缺字段将按默认值处理）：

```json
{{
  "images": [
    {{"local_path": "data/ai_output/{task_id}/main_01.png", "width": 800, "height": 800, "prompt": "..."}}
  ],
  "title_result": {{"title": "改写后的标题", "selling_points": ["..."], "banned_words": []}},
  "attribute_result": {{"attributes_json": {{"品牌": "..."}}, "category_id": "..."}},
  "model_name": "使用的模型名",
  "prompt_snapshot": "实际使用的 prompt"
}}
```

3. 图片不得含水印与极限词；标题不得超过平台字数上限。
"""

PLATFORM_LABELS = {"taobao": "淘宝", "douyin": "抖店", "pdd": "拼多多"}


class WorkBuddyFileBridgeClient(AiClient):
    """文件系统桥接的 AI 客户端（与 WorkBuddy 协作）。"""

    client_name = "file_bridge"

    def __init__(
        self,
        queue_dir: str | Path | None = None,
        output_dir: str | Path | None = None,
        *,
        poll_interval_sec: float | None = None,
        poll_timeout_sec: float | None = None,
    ) -> None:
        """初始化文件桥客户端。"""
        settings = get_settings()
        self.queue_dir = ensure_dir(Path(queue_dir) if queue_dir else settings.ai_queue_dir)
        self.output_dir = ensure_dir(Path(output_dir) if output_dir else settings.ai_output_dir)
        # ★ 三层夹取：显式入参 → 配置项 → 代码常量，保证等待永远有有限上界
        timeout_cap = _clamp(
            settings.ai_poll_timeout_max_sec, MIN_POLL_TIMEOUT_SEC, HARD_MAX_TIMEOUT_SEC, HARD_MAX_TIMEOUT_SEC
        )
        self.poll_interval_sec = _clamp(
            poll_interval_sec if poll_interval_sec is not None else settings.ai_poll_interval_sec,
            MIN_POLL_INTERVAL_SEC,
            MAX_POLL_INTERVAL_SEC,
            MIN_POLL_INTERVAL_SEC,
        )
        self.poll_timeout_sec = _clamp(
            poll_timeout_sec if poll_timeout_sec is not None else settings.ai_poll_timeout_sec,
            MIN_POLL_TIMEOUT_SEC,
            timeout_cap,
            timeout_cap,
        )
        if self.poll_timeout_sec < self.poll_interval_sec:
            # 超时小于一次轮询间隔时，至少要轮询一次（否则产出永远读不到）
            self.poll_interval_sec = max(MIN_POLL_INTERVAL_SEC, min(self.poll_timeout_sec, MAX_POLL_INTERVAL_SEC))

    # ---------------- 任务写入 ----------------

    def task_dir(self, task_id: str) -> Path:
        """队列任务目录。"""
        return ensure_dir(self.queue_dir / str(task_id))

    def output_task_dir(self, task_id: str) -> Path:
        """产出目录。"""
        return ensure_dir(self.output_dir / str(task_id))

    def write_task(self, ctx: AiTaskContext) -> dict[str, str]:
        """写 `task.json` + `prompt.md`，返回两者路径。"""
        directory = self.task_dir(ctx.task_id)
        task_path = directory / TASK_FILENAME
        prompt_path = directory / PROMPT_FILENAME

        payload = ctx.to_dict()
        payload["created_at"] = iso_utc(utc_now())
        payload["output_dir"] = str(self.output_task_dir(ctx.task_id))
        task_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        image_list = "\n".join(f"{i + 1}. `{p}`" for i, p in enumerate(ctx.source_image_paths)) or "- （无本地原图，请先采集）"
        prompt_path.write_text(
            PROMPT_TEMPLATE.format(
                task_id=ctx.task_id,
                platform_label=PLATFORM_LABELS.get(ctx.target_platform, ctx.target_platform or "未指定"),
                source_product_id=ctx.source_product_id,
                original_title=ctx.original_title or "（无）",
                rework_items="、".join(ctx.rework_items) or "（全部）",
                created_at=payload["created_at"],
                image_list=image_list,
                selling_points="\n".join(f"- {p}" for p in ctx.selling_points) or "- （无）",
                attributes_json=json.dumps(ctx.attributes_json or {}, ensure_ascii=False, indent=2),
            ),
            encoding="utf-8",
        )
        logger.info("ai_task_written", task_id=ctx.task_id, task_path=str(task_path))
        return {"task_json": str(task_path), "prompt_md": str(prompt_path)}

    # ---------------- 产出回读 ----------------

    def read_result(self, task_id: str) -> dict[str, Any] | None:
        """读取 `result.json`；不存在或解析失败返回 None（★ 防御性，绝不抛异常）。"""
        path = self.output_task_dir(task_id) / RESULT_FILENAME
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("ai_result_parse_failed", task_id=task_id, error=str(exc))
            return None
        return raw if isinstance(raw, dict) else None

    async def wait_result(self, task_id: str) -> dict[str, Any]:
        """轮询等待产出；超时抛 `AiTimeoutError`（可重试，由 TaskRunner 按 max_retry 处理）。

        ★ 超时提示必须指向人类可读的 `prompt.md`：运营 / WorkBuddy 打开该文件即可知道要做什么，
          而不是去看一个机器读的 result.json 路径（该文件此时根本不存在）。
        """
        # ★ 双重保险：既用绝对 deadline，也用 asyncio 超时兜底
        #   （任何一处 sleep 被异常拉长都不至于把等待变成无上界）。
        deadline = time.perf_counter() + self.poll_timeout_sec
        while time.perf_counter() < deadline:
            raw = self.read_result(task_id)
            if raw:
                return raw
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            await asyncio.sleep(min(self.poll_interval_sec, remaining))
        prompt_path = self.task_dir(task_id) / PROMPT_FILENAME
        raise AiTimeoutError(
            f"等待 WorkBuddy 产出超时，请查看 `data/ai_queue/{task_id}/prompt.md`"
            f"（任务已写入队列，超时 {int(self.poll_timeout_sec)}s 未产出；"
            f"prompt 文件：{prompt_path}）"
        )

    @staticmethod
    def parse_result(raw: dict[str, Any]) -> AiReworkResult:
        """★ 防御性解析产出：缺失字段用默认值，绝不因字段不全崩溃。"""
        images: list[AiImageResult] = []
        for item in raw.get("images") or []:
            if not isinstance(item, dict):
                continue
            local_path = str(item.get("local_path", "") or "")
            content_hash = ""
            size_bytes = int(item.get("size_bytes", 0) or 0)
            if local_path and Path(local_path).exists():
                try:
                    content_hash = content_hash_file(local_path)
                    size_bytes = size_bytes or Path(local_path).stat().st_size
                except OSError:
                    content_hash = ""
            images.append(
                AiImageResult(
                    local_path=local_path,
                    width=int(item.get("width", 0) or 0),
                    height=int(item.get("height", 0) or 0),
                    size_bytes=size_bytes,
                    content_hash=content_hash,
                    is_placeholder=bool(item.get("is_placeholder", False)),
                    prompt=str(item.get("prompt", "") or ""),
                )
            )

        raw_title = raw.get("title_result")
        title_result: AiTitleResult | None = None
        if isinstance(raw_title, dict):
            title_result = AiTitleResult(
                title=str(raw_title.get("title", "") or ""),
                selling_points=[str(s) for s in (raw_title.get("selling_points") or [])],
                banned_words=[str(s) for s in (raw_title.get("banned_words") or [])],
                model_name=str(raw_title.get("model_name", "") or ""),
                prompt_snapshot=str(raw_title.get("prompt_snapshot", "") or ""),
            )

        raw_attr = raw.get("attribute_result")
        attribute_result: AiAttributeResult | None = None
        if isinstance(raw_attr, dict):
            attrs = raw_attr.get("attributes_json")
            attribute_result = AiAttributeResult(
                attributes_json=dict(attrs) if isinstance(attrs, dict) else {},
                category_id=str(raw_attr.get("category_id", "") or ""),
                model_name=str(raw_attr.get("model_name", "") or ""),
            )

        return AiReworkResult(
            images=images,
            title_result=title_result,
            attribute_result=attribute_result,
            model_name=str(raw.get("model_name", "") or ""),
            prompt_snapshot=str(raw.get("prompt_snapshot", "") or ""),
            elapsed_ms=int(raw.get("elapsed_ms", 0) or 0),
            raw=raw,
        )

    # ---------------- 接口实现 ----------------

    async def rework_images(self, ctx: AiTaskContext) -> AiReworkResult:
        """写任务 → 等产出 → 解析。"""
        started = time.perf_counter()
        self.write_task(ctx)
        raw = await self.wait_result(ctx.task_id)
        result = self.parse_result(raw)
        result.elapsed_ms = result.elapsed_ms or int((time.perf_counter() - started) * 1000)
        return result

    async def rewrite_title(self, ctx: AiTaskContext) -> AiTitleResult:
        """复用同一份产出中的 title_result；缺失时回退原标题。"""
        raw = self.read_result(ctx.task_id) or await self._ensure_result(ctx)
        result = self.parse_result(raw)
        if result.title_result is None:
            return AiTitleResult(title=ctx.original_title, selling_points=list(ctx.selling_points))
        return result.title_result

    async def suggest_attributes(self, ctx: AiTaskContext) -> AiAttributeResult:
        """复用同一份产出中的 attribute_result；缺失时回退上下文属性。"""
        raw = self.read_result(ctx.task_id) or await self._ensure_result(ctx)
        result = self.parse_result(raw)
        if result.attribute_result is None:
            return AiAttributeResult(attributes_json=dict(ctx.attributes_json or {}))
        return result.attribute_result

    async def _ensure_result(self, ctx: AiTaskContext) -> dict[str, Any]:
        """产出不存在时写任务并等待。"""
        self.write_task(ctx)
        return await self.wait_result(ctx.task_id)

    async def health_check(self) -> dict[str, Any]:
        """自检：队列与产出目录可读写即健康。"""
        try:
            self.queue_dir.mkdir(parents=True, exist_ok=True)
            self.output_dir.mkdir(parents=True, exist_ok=True)
            healthy = self.queue_dir.is_dir() and self.output_dir.is_dir()
            message = f"队列目录 {self.queue_dir} / 产出目录 {self.output_dir}"
        except Exception as exc:  # noqa: BLE001
            healthy = False
            message = f"目录不可用：{exc}"
        return {
            "client": self.client_name,
            "healthy": healthy,
            "message": message,
            "queue_dir": str(self.queue_dir),
            "output_dir": str(self.output_dir),
        }
