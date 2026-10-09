"""结构化日志、trace_id 上下文与敏感信息脱敏（§10.8）。

约定：
    * 每条日志固定携带 `trace_id`；
    * 买家手机号 / 收件人姓名 / 收货地址 / 凭证明文一律脱敏后输出；
    * 中间件自动生成 trace_id（或沿用请求头 `X-Trace-Id`）。
"""

from __future__ import annotations

import contextvars
import logging
import logging.config
import re
import sys
import time
import uuid
from typing import Any

import structlog

TRACE_ID_HEADER = "X-Trace-Id"

_trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")

# ---------------- 脱敏规则（§10.8） ----------------
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
_ID_CARD_RE = re.compile(r"(?<!\d)\d{6}\d{8}(\d{4})(?!\d)")
_SECRET_KV_RE = re.compile(
    r"(?i)\b(app_key|app_secret|access_token|refresh_token|secret|password|token|api_key|buyer_info_enc|receiver_addr_enc)\b\s*[:=]\s*[\"']?([A-Za-z0-9_\-\.]{4,})[\"']?"
)


def get_trace_id() -> str:
    """获取当前上下文的 trace_id（不存在时返回空串）。"""
    return _trace_id_var.get()


def set_trace_id(trace_id: str | None = None) -> str:
    """设置当前上下文 trace_id，返回实际生效值。"""
    value = trace_id or new_trace_id()
    _trace_id_var.set(value)
    return value


def bind_trace_id(trace_id: str | None = None) -> str:
    """兼容别名：绑定 trace_id 到上下文并返回。"""
    return set_trace_id(trace_id)


def new_trace_id() -> str:
    """生成新的 trace_id（UUID4 十六进制，无横杠）。"""
    return uuid.uuid4().hex


def mask_phone(value: str | None) -> str:
    """手机号脱敏：`13812348888` → `138****8888`。"""
    if not value:
        return ""
    return _PHONE_RE.sub(lambda m: f"{m.group(1)}****{m.group(2)}", value)


def mask_name(value: str | None) -> str:
    """姓名脱敏：仅保留姓氏，其余以 * 替代。"""
    if not value:
        return ""
    if len(value) == 1:
        return "*"
    return value[0] + "*" * (len(value) - 1)


def mask_address(value: str | None, keep_district: bool = True) -> str:
    """地址脱敏：默认保留到区县，去掉门牌与详细地址。"""
    if not value:
        return ""
    # 粗略切分：省 / 市 / 区县 之后的部分视为详细地址
    keywords = ("省", "市", "区", "县", "镇", "街道")
    cut = 0
    for keyword in keywords:
        pos = value.find(keyword)
        if pos > cut:
            cut = pos + len(keyword)
    if cut == 0:
        cut = min(len(value), 6)
    if keep_district:
        return value[:cut] + "***"
    return value[:cut]


def sanitize_text(value: str | None) -> str:
    """对任意文本做脱敏：手机号 / 身份证 / 凭证键值对。"""
    if not value:
        return ""
    text = _PHONE_RE.sub(lambda m: f"{m.group(1)}****{m.group(2)}", value)
    text = _ID_CARD_RE.sub(lambda m: "******" + m.group(1), text)
    text = _SECRET_KV_RE.sub(lambda m: f"{m.group(1)}=***", text)
    return text


def sanitize_payload(payload: Any, depth: int = 0) -> Any:
    """递归脱敏：对 dict / list / str 中的敏感内容做掩码。"""
    if depth > 6:
        return payload
    if isinstance(payload, str):
        return sanitize_text(payload)
    if isinstance(payload, dict):
        return {k: sanitize_payload(v, depth + 1) for k, v in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [sanitize_payload(v, depth + 1) for v in payload]
    return payload


class SensitiveFilter(logging.Filter):
    """标准库日志过滤器：对日志正文做脱敏。"""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D102
        try:
            if isinstance(record.msg, str):
                record.msg = sanitize_text(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: sanitize_payload(v) for k, v in record.args.items()}
                else:
                    record.args = tuple(sanitize_payload(a) for a in record.args)
        except Exception:  # noqa: BLE001  脱敏失败不应影响日志输出
            return True
        return True


def _add_trace_id(_logger: Any, _method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor：注入 trace_id。"""
    event_dict.setdefault("trace_id", get_trace_id())
    return event_dict


def _sanitize_event(_logger: Any, _method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor：脱敏事件字典中的敏感值。"""
    for key, value in list(event_dict.items()):
        if isinstance(value, str):
            event_dict[key] = sanitize_text(value)
        elif isinstance(value, (dict, list, tuple)):
            event_dict[key] = sanitize_payload(value)
    return event_dict


def setup_logging(level: str = "INFO", json_logs: bool = False) -> None:
    """初始化 structlog 与标准库 logging。"""
    numeric_level = getattr(logging, str(level).upper(), logging.INFO)
    renderer: Any = structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer(colors=False)

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "plain": {"format": "%(asctime)s %(levelname)s [%(name)s] %(message)s"},
            },
            "filters": {"sensitive": {"()": f"{SensitiveFilter.__module__}.{SensitiveFilter.__qualname__}"}},
            "handlers": {
                "default": {
                    "level": numeric_level,
                    "class": "logging.StreamHandler",
                    "stream": sys.stdout,
                    "formatter": "plain",
                    "filters": ["sensitive"],
                },
            },
            "root": {"handlers": ["default"], "level": numeric_level},
            "loggers": {
                "uvicorn": {"handlers": ["default"], "level": numeric_level, "propagate": False},
                "uvicorn.access": {"handlers": ["default"], "level": numeric_level, "propagate": False},
                "apscheduler": {"handlers": ["default"], "level": logging.WARNING, "propagate": False},
                "sqlalchemy.engine": {"handlers": ["default"], "level": logging.WARNING, "propagate": False},
            },
        }
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_trace_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _sanitize_event,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> Any:
    """获取 structlog 日志器。"""
    return structlog.get_logger(name or "app")


class TraceIdMiddleware:
    """ASGI 中间件：为每次请求生成 / 继承 trace_id 并注入上下文与响应头。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        trace_id = headers.get(TRACE_ID_HEADER.lower()) or new_trace_id()
        token = _trace_id_var.set(trace_id)
        started = time.perf_counter()

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                raw_headers = list(message.get("headers", []))
                raw_headers.append((b"x-trace-id", trace_id.encode("utf-8")))
                message["headers"] = raw_headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            get_logger("app.access").info(
                "http_request",
                path=scope.get("path", ""),
                method=scope.get("method", ""),
                status="completed",
                elapsed_ms=elapsed_ms,
            )
            _trace_id_var.reset(token)


__all__ = [
    "TRACE_ID_HEADER",
    "SensitiveFilter",
    "TraceIdMiddleware",
    "bind_trace_id",
    "get_logger",
    "get_trace_id",
    "mask_address",
    "mask_name",
    "mask_phone",
    "new_trace_id",
    "sanitize_payload",
    "sanitize_text",
    "set_trace_id",
    "setup_logging",
]
