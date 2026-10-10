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
    _safe_int,
    AiAttributeResult,
    AiClient,
    AiImageResult,
    AiRedrawResult,
    AiReworkResult,
    AiTaskContext,
    AiTimeoutError,
    AiTitleResult,
    AiTitleSuggestResult,
    AiVideoScriptResult,
)
from app.core.config import get_settings
from app.core.logging import get_logger
from app.utils.kit import content_hash_file, ensure_dir, iso_utc, utc_now

logger = get_logger(__name__)

__all__ = [
    "DELIVERY_TEMPLATES",
    "PROMPT_TEMPLATE",
    "RESULT_FILENAME",
    "TASK_TYPE_LABELS",
    "WorkBuddyFileBridgeClient",
]

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

# ★★ 这个 Markdown 是**给外部 AI 执行方（WorkBuddy）读的**，不是给程序读的 ★★
#    机器可读的那份是同目录的 `task.json`；这里每一句话都要让执行方看得懂"要做什么、
#    按哪句提示词做、做完往哪写、写成什么结构"。因此下面的"交付格式"必须写全，
#    含每个字段的取值口径（例如 image_role 只能取 main_image / detail_image）。
PROMPT_TEMPLATE = """# {task_type_label}

- **任务 ID**：{task_id}
- **任务类型**：{task_type_label}（`{task_type}`）
- **目标平台**：{platform_label}
- **货源商品 ID**：{source_product_id}
- **原始标题**：{original_title}
- **重构项**：{rework_items}
- **创建时间**：{created_at}（UTC）

## 全局提示词

{global_prompt}

## 原始图片与**逐图提示词**

★ 下表**一行一张图**，`提示词` 列是使用者针对**这一张**单独输入的要求。
  请**逐张按它自己那行的提示词**处理，不要把某张图的提示词套到别的图上。
  标了「沿用全局提示词」的行表示该图没单独指定，按上面的全局提示词处理。

{image_prompt_list}

## 卖点参考

{selling_points}

## 参考属性

```json
{attributes_json}
```

{delivery}
"""

# ★ 按 `AiTaskType` 分派的「交付要求」段：写清产出目录、result.json 结构与字段口径。
DELIVERY_TEMPLATES: dict[str, str] = {
    "ai_rework": """## 交付要求

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

3. 图片不得含水印与极限词；标题不得超过平台字数上限。""",
    "image_redraw": """## 交付要求（图片重绘）

1. **逐张**重绘上表的图片，每张严格按它自己那行的提示词画，产出保存到 `data/ai_output/{task_id}/`
   （主图 `main_01.png`，详情图 `detail_01.png` …）；
2. 在同目录写 `result.json`，结构如下（缺字段将按默认值处理）：

```json
{{
  "redraw": {{
    "images": [
      {{
        "index": 0,
        "local_path": "data/ai_output/{task_id}/main_01.png",
        "source_path": "原图本地路径（照抄上表）",
        "image_role": "main_image",
        "width": 800,
        "height": 800,
        "prompt": "这张图实际使用的提示词（照抄上表对应行）",
        "prompt_source": "per_image"
      }}
    ],
    "model_name": "使用的模型名"
  }}
}}
```

3. 字段取值口径（填错会导致下游无法区分主图 / 详情图）：
   - `image_role` 只能是 `main_image`（主图）或 `detail_image`（详情图）；
   - `prompt_source` 只能是 `per_image`（该图有逐图提示词）、`global`（沿用全局提示词）、`default`（都没给，按你的默认口径）；
   - `index` 必须与上表的序号一致，`source_path` 照抄上表该行的原图路径。
4. 图片不得含水印与极限词。""",
    "title_suggest": """## 交付要求（商品标题建议）

1. 给出 **至少 3 条**标题候选，**每条侧重点必须不同**（不要三条换汤不换药）；
2. 按目标平台的用词风格写（**没有外部热词数据源**，请凭你对该平台用词习惯的理解，
   写出带该平台热词风格的标题）；
3. 每条候选**必须**写清 `style`（这条走什么风格 / 侧重）与 `reason`（为什么选它、
   适合什么人群或场景）—— 只给一串标题等于让使用者盲选；
4. 在同目录写 `result.json`，结构如下（缺字段将按默认值处理）：

```json
{{
  "title_suggest": {{
    "candidates": [
      {{
        "title": "候选标题一",
        "style": "促销感 / 专业参数流 / 抖音热词风 …",
        "reason": "适合大促场景，突出价格力",
        "platform_fit": "taobao",
        "score": 92,
        "selling_points": ["卖点一", "卖点二"],
        "banned_words": []
      }}
    ],
    "model_name": "使用的模型名"
  }}
}}
```

5. 标题不得含极限词 / 违禁词，命中请填进 `banned_words`；`score` 只用于候选之间排序（0-100）。""",
    "video_script": """## 交付要求（短视频拍摄脚本 —— **只出文案，不要生成视频**）

1. 输出的是**拍摄脚本文案**，供人照着拍；**不要**产出任何视频文件，也不要填视频路径；
2. 按**分镜**组织，每个分镜必须同时给出：分镜序号、建议时长（秒）、画面内容、
   机位 / 运镜、拍摄要点、口播台词；
3. 拍摄风格 / 内容倾向以上面的「全局提示词」与 `task.json` 的 `video_script_prompt` 为准；
4. 在同目录写 `result.json`，结构如下（缺字段将按默认值处理）：

```json
{{
  "video_script": {{
    "title": "脚本标题",
    "style": "拍摄风格（回填使用者的 video_script_prompt）",
    "scenes": [
      {{
        "index": 1,
        "duration_sec": 3,
        "shot": "画面里出现什么",
        "camera": "机位 / 运镜（如 固定机位俯拍、手持跟随）",
        "shooting_tips": "布光 / 道具 / 注意事项",
        "narration": "口播台词"
      }}
    ],
    "total_duration_sec": 15,
    "model_name": "使用的模型名"
  }}
}}
```

5. `index` 从 1 起递增；`total_duration_sec` 不给时按各分镜时长累加。""",
}

# 未知 / 缺省任务类型一律按老口径「图文重构」交付（★ 与 `AiTaskType.AI_REWORK` 是存量默认值一致）
DEFAULT_DELIVERY_KEY = "ai_rework"

TASK_TYPE_LABELS = {
    "ai_rework": "AI 图文重构任务",
    "image_redraw": "AI 图片重绘任务",
    "title_suggest": "AI 商品标题建议任务",
    "video_script": "AI 短视频拍摄脚本任务",
}

PLATFORM_LABELS = {"taobao": "淘宝", "douyin": "抖店", "pdd": "拼多多"}

# result.json 里三块新产物的键名（解析与落盘的唯一口径）
REDRAW_KEY = "redraw"
TITLE_SUGGEST_KEY = "title_suggest"
VIDEO_SCRIPT_KEY = "video_script"


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

    @staticmethod
    def _render_image_prompt_list(ctx: AiTaskContext) -> str:
        """★ 渲染「一行一张图 + 该图提示词」表格 —— 逐图提示词的**对外可见证据**。

        ★ 为什么必须逐行列全：使用者是针对"这一张图"输入的提示词，
          压缩成"提示词如下：A / B / C"会让执行方无法把提示词对回具体那张图，
          于是逐图提示词这个核心交互在桥接环节就被悄悄丢掉了。
        """
        if not ctx.source_image_paths:
            return "- （无本地原图，请先采集）"
        lines: list[str] = []
        for index, path in enumerate(ctx.source_image_paths):
            role = "主图" if index == 0 else "详情图"
            role_key = "main_image" if index == 0 else "detail_image"
            raw = next(
                (p.prompt for p in ctx.image_prompts if int(p.index) == int(index) and p.prompt),
                "",
            )
            if raw:
                prompt_cell = raw
                source_note = "（逐图提示词）"
            elif ctx.global_prompt:
                prompt_cell = f"沿用全局提示词：{ctx.global_prompt}"
                source_note = "（该图未单独指定）"
            else:
                prompt_cell = "（无提示词，按你的默认口径处理）"
                source_note = ""
            lines.append(
                f"{index}. 原图 `{path}` —— 角色：{role}（`{role_key}`）{source_note}\n   提示词：{prompt_cell}"
            )
        return "\n".join(lines)

    def write_task(self, ctx: AiTaskContext) -> dict[str, str]:
        """写 `task.json` + `prompt.md`，返回两者路径。"""
        directory = self.task_dir(ctx.task_id)
        task_path = directory / TASK_FILENAME
        prompt_path = directory / PROMPT_FILENAME

        payload = ctx.to_dict()
        payload["created_at"] = iso_utc(utc_now())
        payload["output_dir"] = str(self.output_task_dir(ctx.task_id))
        task_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        task_type = str(ctx.task_type or DEFAULT_DELIVERY_KEY)
        delivery_template = DELIVERY_TEMPLATES.get(task_type, DELIVERY_TEMPLATES[DEFAULT_DELIVERY_KEY])
        prompt_path.write_text(
            PROMPT_TEMPLATE.format(
                task_id=ctx.task_id,
                task_type=task_type,
                task_type_label=TASK_TYPE_LABELS.get(task_type, TASK_TYPE_LABELS[DEFAULT_DELIVERY_KEY]),
                platform_label=PLATFORM_LABELS.get(ctx.target_platform, ctx.target_platform or "未指定"),
                source_product_id=ctx.source_product_id,
                original_title=ctx.original_title or "（无）",
                rework_items="、".join(ctx.rework_items) or "（全部）",
                created_at=payload["created_at"],
                global_prompt=ctx.global_prompt or "（无全局提示词）",
                image_prompt_list=self._render_image_prompt_list(ctx),
                selling_points="\n".join(f"- {p}" for p in ctx.selling_points) or "- （无）",
                attributes_json=json.dumps(ctx.attributes_json or {}, ensure_ascii=False, indent=2),
                delivery=delivery_template.format(task_id=ctx.task_id),
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
            size_bytes = _safe_int(item.get("size_bytes"), 0) or 0
            if local_path and Path(local_path).exists():
                try:
                    content_hash = content_hash_file(local_path)
                    size_bytes = size_bytes or Path(local_path).stat().st_size
                except OSError:
                    content_hash = ""
            images.append(
                AiImageResult(
                    local_path=local_path,
                    width=_safe_int(item.get("width"), 0) or 0,
                    height=_safe_int(item.get("height"), 0) or 0,
                    size_bytes=size_bytes,
                    content_hash=content_hash,
                    is_placeholder=bool(item.get("is_placeholder", False)),
                    prompt=str(item.get("prompt", "") or ""),
                )
            )

        raw_title = raw.get("title_result")
        title_result: AiTitleResult | None = None
        if isinstance(raw_title, dict):
            # ★ 走 `from_dict`：与旧实现字段一致，但额外接住 `candidates` 多候选
            #   （旧 result.json 没有这个键 → 退化成单条口径，行为不变）。
            title_result = AiTitleResult.from_dict(raw_title)

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
            elapsed_ms=_safe_int(raw.get("elapsed_ms"), 0) or 0,
            raw=raw,
        )

    # ---------------- 新能力产出解析 ----------------

    @staticmethod
    def parse_redraw_result(raw: dict[str, Any] | None) -> AiRedrawResult:
        """★ 解析 `redraw` 块；再补算文件哈希 / 体积（本地文件在，就直接量，不信外部填的值）。"""
        payload = raw if isinstance(raw, dict) else {}
        block = payload.get(REDRAW_KEY)
        if not isinstance(block, dict):
            return AiRedrawResult()
        result = AiRedrawResult.from_dict(block)
        for image in result.images:
            if image.local_path and Path(image.local_path).exists():
                try:
                    image.content_hash = image.content_hash or content_hash_file(image.local_path)
                    image.size_bytes = image.size_bytes or Path(image.local_path).stat().st_size
                except OSError:
                    pass
        return result

    @staticmethod
    def parse_title_suggest_result(raw: dict[str, Any] | None) -> AiTitleSuggestResult:
        """★ 解析 `title_suggest` 块；缺块 / 坏 JSON 一律返回空候选（绝不抛异常）。"""
        payload = raw if isinstance(raw, dict) else {}
        block = payload.get(TITLE_SUGGEST_KEY)
        if not isinstance(block, dict):
            return AiTitleSuggestResult()
        return AiTitleSuggestResult.from_dict(block)

    @staticmethod
    def parse_video_script_result(raw: dict[str, Any] | None) -> AiVideoScriptResult:
        """★ 解析 `video_script` 块；缺块 / 坏 JSON 一律返回空脚本（绝不抛异常）。"""
        payload = raw if isinstance(raw, dict) else {}
        block = payload.get(VIDEO_SCRIPT_KEY)
        if not isinstance(block, dict):
            return AiVideoScriptResult()
        return AiVideoScriptResult.from_dict(block)

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

    # ---------------- 新能力 ----------------

    async def redraw_images(self, ctx: AiTaskContext) -> AiRedrawResult:
        """图片重绘：写任务（含逐图提示词）→ 等产出 → 解析 `redraw` 块。"""
        started = time.perf_counter()
        self.write_task(ctx)
        raw = await self.wait_result(ctx.task_id)
        result = self.parse_redraw_result(raw)
        result.elapsed_ms = result.elapsed_ms or int((time.perf_counter() - started) * 1000)
        return result

    async def suggest_titles(self, ctx: AiTaskContext) -> AiTitleSuggestResult:
        """标题建议：读产出（无则写任务并等待）→ 解析 `title_suggest` 块。"""
        started = time.perf_counter()
        raw = self.read_result(ctx.task_id) or await self._ensure_result(ctx)
        result = self.parse_title_suggest_result(raw)
        result.elapsed_ms = result.elapsed_ms or int((time.perf_counter() - started) * 1000)
        return result

    async def suggest_video_script(self, ctx: AiTaskContext) -> AiVideoScriptResult:
        """视频脚本：读产出（无则写任务并等待）→ 解析 `video_script` 块。"""
        started = time.perf_counter()
        raw = self.read_result(ctx.task_id) or await self._ensure_result(ctx)
        result = self.parse_video_script_result(raw)
        result.elapsed_ms = result.elapsed_ms or int((time.perf_counter() - started) * 1000)
        return result

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
