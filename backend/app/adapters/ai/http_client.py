"""HttpAiClient —— OpenAI 兼容接口的 AI 客户端（base_url / api_key / model 从配置读）。

支持：
    * `POST {base_url}/chat/completions` 做标题改写与属性建议；
    * `POST {base_url}/images/generations` 做图片重构（未配置 image_model 时降级为 UNSUPPORTED 提示 + 空图列表）。

★ 绝不抛裸异常：HTTP 失败时回退到"原标题 + 空图"，由上层按任务失败处理。
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any

import httpx

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

__all__ = ["HttpAiClient"]

TITLE_SYSTEM_PROMPT = (
    "你是电商商品标题优化专家。给定原始标题与目标平台，输出："
    "1) 符合平台风格的标题（不超过 30 个汉字）；"
    "2) 3-5 条卖点；"
    "3) 命中的违禁词/极限词列表。"
    "严格输出 JSON：{\"title\": \"\", \"selling_points\": [], \"banned_words\": []}，不要输出多余文字。"
)

ATTRIBUTE_SYSTEM_PROMPT = (
    "你是电商平台类目属性填充专家。给定商品信息，输出可直接填入后台表单的属性键值对。"
    "严格输出 JSON：{\"attributes\": {}, \"category_id\": \"\"}，不要输出多余文字。"
)


class HttpAiClient(AiClient):
    """OpenAI 兼容 HTTP AI 客户端。"""

    client_name = "http"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        image_model: str | None = None,
        timeout_sec: float | None = None,
        output_dir: str | Path | None = None,
    ) -> None:
        """初始化 HTTP AI 客户端（缺省值来自 Settings）。"""
        settings = get_settings()
        self.base_url = (base_url or settings.ai_base_url or "").rstrip("/")
        self.api_key = api_key or settings.ai_api_key
        self.model = model or settings.ai_model
        self.image_model = image_model or settings.ai_image_model
        self.timeout_sec = float(timeout_sec or settings.ai_timeout_sec)
        self.output_dir = ensure_dir(Path(output_dir) if output_dir else settings.assets_dir / "ai")

    # ---------------- 底层调用 ----------------

    def _headers(self) -> dict[str, str]:
        """构造鉴权请求头。"""
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def _chat(self, system_prompt: str, user_prompt: str) -> str:
        """调用 chat/completions，返回 assistant 文本；失败返回空串（★ 不抛异常）。"""
        if not self.base_url or not self.api_key:
            logger.warning("ai_http_not_configured", base_url=bool(self.base_url), api_key=bool(self.api_key))
            return ""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.7,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions", json=payload, headers=self._headers()
                )
                response.raise_for_status()
                data = response.json()
        except Exception as exc:  # noqa: BLE001
            logger.error("ai_http_chat_failed", error=str(exc))
            return ""
        try:
            return str(data["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError):
            return ""

    async def _generate_image(self, prompt: str) -> bytes | None:
        """调用 images/generations，返回图片字节；失败返回 None。"""
        if not self.base_url or not self.api_key or not self.image_model:
            return None
        payload = {"model": self.image_model, "prompt": prompt, "n": 1, "size": "1024x1024"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
                response = await client.post(
                    f"{self.base_url}/images/generations", json=payload, headers=self._headers()
                )
                response.raise_for_status()
                data = response.json()
        except Exception as exc:  # noqa: BLE001
            logger.error("ai_http_image_failed", error=str(exc))
            return None

        item = (data.get("data") or [{}])[0] if isinstance(data, dict) else {}
        b64 = item.get("b64_json") if isinstance(item, dict) else None
        url = item.get("url") if isinstance(item, dict) else None
        if b64:
            try:
                return base64.b64decode(b64)
            except Exception:  # noqa: BLE001
                return None
        if url:
            try:
                async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    return resp.content
            except Exception:  # noqa: BLE001
                return None
        return None

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        """从模型输出中提取 JSON（★ 防御性：允许被 ```json 包裹）。"""
        if not text:
            return {}
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.strip()
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            return {}
        try:
            parsed = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    # ---------------- 接口实现 ----------------

    async def rework_images(self, ctx: AiTaskContext) -> AiReworkResult:
        """图片重构：逐张调用图像生成接口；未配置 image_model 时返回空图列表。"""
        started = time.perf_counter()
        task_dir = ensure_dir(self.output_dir / safe_filename(ctx.task_id or "unknown"))
        images: list[AiImageResult] = []

        count = max(len(ctx.source_image_paths), 1)
        for index in range(count):
            prompt = f"电商商品图：{ctx.original_title}；平台：{ctx.target_platform}；白底、主体居中、无水印、无文字"
            data = await self._generate_image(prompt)
            if not data:
                continue  # ★ 防御性：单张失败不影响其他张
            name = "main_01.png" if index == 0 else f"detail_{index:02d}.png"
            target = task_dir / name
            target.write_bytes(data)
            images.append(
                AiImageResult(
                    local_path=str(target),
                    size_bytes=len(data),
                    content_hash=content_hash_bytes(data),
                    is_placeholder=False,
                    prompt=prompt,
                )
            )
            await asyncio.sleep(0)  # 让出事件循环，避免长任务阻塞

        return AiReworkResult(
            images=images,
            model_name=self.image_model or self.model,
            prompt_snapshot=f"image_model={self.image_model}",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            raw={"mode": "http", "image_count": len(images)},
        )

    async def rewrite_title(self, ctx: AiTaskContext) -> AiTitleResult:
        """标题改写：chat 接口 + JSON 解析，失败回退原标题。"""
        user_prompt = (
            f"目标平台：{ctx.target_platform}\n"
            f"原始标题：{ctx.original_title}\n"
            f"卖点参考：{'/'.join(ctx.selling_points) or '无'}"
        )
        content = await self._chat(TITLE_SYSTEM_PROMPT, user_prompt)
        parsed = self._extract_json(content)
        if not parsed:
            return AiTitleResult(
                title=ctx.original_title,
                selling_points=list(ctx.selling_points),
                model_name=self.model,
                prompt_snapshot=user_prompt,
            )
        return AiTitleResult(
            title=str(parsed.get("title", ctx.original_title) or ctx.original_title),
            selling_points=[str(s) for s in (parsed.get("selling_points") or ctx.selling_points)],
            banned_words=[str(s) for s in (parsed.get("banned_words") or [])],
            model_name=self.model,
            prompt_snapshot=user_prompt,
        )

    async def suggest_attributes(self, ctx: AiTaskContext) -> AiAttributeResult:
        """属性建议：chat 接口 + JSON 解析，失败回退上下文属性。"""
        user_prompt = (
            f"目标平台：{ctx.target_platform}\n"
            f"商品标题：{ctx.original_title}\n"
            f"已有属性：{json.dumps(ctx.attributes_json or {}, ensure_ascii=False)}"
        )
        content = await self._chat(ATTRIBUTE_SYSTEM_PROMPT, user_prompt)
        parsed = self._extract_json(content)
        attrs = parsed.get("attributes") if isinstance(parsed.get("attributes"), dict) else dict(ctx.attributes_json or {})
        return AiAttributeResult(
            attributes_json=dict(attrs),
            category_id=str(parsed.get("category_id", "") or ""),
            model_name=self.model,
        )

    async def health_check(self) -> dict[str, Any]:
        """自检：GET {base_url}/models。"""
        if not self.base_url or not self.api_key:
            return {"client": self.client_name, "healthy": False, "message": "未配置 base_url / api_key"}
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
                response = await client.get(f"{self.base_url}/models", headers=self._headers())
                healthy = response.status_code < 400
                message = f"HTTP {response.status_code}"
        except Exception as exc:  # noqa: BLE001
            healthy = False
            message = f"连通性失败：{exc}"
        return {
            "client": self.client_name,
            "healthy": healthy,
            "message": message,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "model": self.model,
        }
