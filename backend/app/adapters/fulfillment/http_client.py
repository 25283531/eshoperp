"""可配置 HTTP 客户端：签名、超时、重试、响应映射、trace_id 透传。

设计：
    * 端点路径 / 请求模板 / 响应字段映射全部外置到 `profiles/*.yaml`，改配置不改代码；
    * **绝不向上抛异常**：任何错误都转成 `(ok, data, error)` 三元组或 `AdapterResult`；
    * YAML 中标注 `# TODO: 需实测确认` 的字段在 `extract()` 时做防御性解析（缺失不崩）。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx

from app.core.logging import get_logger, get_trace_id
from app.utils.kit import iso_utc, utc_now

logger = get_logger(__name__)

__all__ = [
    "HttpClient",
    "HttpRequestSpec",
    "HttpResult",
    "PROFILES_DIR",
    "build_spec",
    "endpoint_field_map",
    "load_profile",
    "pick",
    "pick_first",
    "profile_capabilities",
    "profile_required_scopes",
    "render_path",
]


class HttpRequestSpec:
    """由 profile YAML 描述的一次 HTTP 请求。"""

    __slots__ = ("method", "path", "body_template", "query_template", "response_mapping", "note")

    def __init__(
        self,
        method: str = "POST",
        path: str = "",
        body_template: dict[str, Any] | None = None,
        query_template: dict[str, Any] | None = None,
        response_mapping: dict[str, Any] | None = None,
        note: str = "",
    ) -> None:
        self.method = (method or "POST").upper()
        self.path = path or ""
        self.body_template = dict(body_template or {})
        self.query_template = dict(query_template or {})
        self.response_mapping = dict(response_mapping or {})
        self.note = note


class HttpResult:
    """HTTP 调用结果（永不抛异常）。"""

    __slots__ = ("ok", "status_code", "data", "raw", "error", "retryable", "elapsed_ms")

    def __init__(
        self,
        ok: bool,
        status_code: int = 0,
        data: Any = None,
        raw: Any = None,
        error: str = "",
        retryable: bool = False,
        elapsed_ms: int = 0,
    ) -> None:
        self.ok = ok
        self.status_code = status_code
        self.data = data
        self.raw = raw
        self.error = error
        self.retryable = retryable
        self.elapsed_ms = elapsed_ms

    def __bool__(self) -> bool:
        """是否成功。"""
        return self.ok


def render_path(path: str, params: dict[str, Any]) -> str:
    """渲染路径模板：`/order/{order_id}` + `{"order_id": 123}` → `/order/123`。

    未提供的占位符会被移除（防御性：第三方字段不确定时不崩）。
    """
    result = path or ""
    for key, value in params.items():
        result = result.replace("{%s}" % key, str(value))
    # 清理未替换的占位符
    while "{" in result and "}" in result:
        start = result.find("{")
        end = result.find("}", start)
        if end == -1:
            break
        result = result[:start] + result[end + 1:]
    return result.rstrip("/") or "/"


def pick(payload: Any, dotted_path: str, default: Any = None) -> Any:
    """按点分路径安全取值，支持列表下标（如 `data.list.0.tracking_no`）。

    ★ 防御性解析：路径不存在 / 类型不匹配均返回 default，绝不抛异常。
    """
    if payload is None or not dotted_path:
        return default
    current: Any = payload
    for part in str(dotted_path).split("."):
        if current is None:
            return default
        if isinstance(current, dict):
            if part not in current:
                return default
            current = current[part]
        elif isinstance(current, (list, tuple)):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return current if current is not None else default


def pick_first(payload: Any, dotted_paths: list[str], default: Any = None) -> Any:
    """按候选路径依次尝试取值（应对第三方字段名不确定）。"""
    for path in dotted_paths:
        value = pick(payload, path, None)
        if value not in (None, "", [], {}):
            return value
    return default


class HttpClient:
    """可配置 HTTP 客户端（httpx.AsyncClient 封装）。"""

    def __init__(
        self,
        base_url: str = "",
        *,
        timeout_sec: float = 20.0,
        max_retry: int = 3,
        retry_backoff_sec: float = 0.5,
        verify_ssl: bool = True,
        headers: dict[str, str] | None = None,
        auth: dict[str, Any] | None = None,
        credential: Any = None,
    ) -> None:
        """初始化 HTTP 客户端。

        Args:
            base_url: 第三方接口根地址（来自 profile 或后台配置）。
            timeout_sec: 单次请求超时。
            max_retry: 最大重试次数（仅对网络错误与 5xx / 429 重试）。
            retry_backoff_sec: 指数退避基数。
            verify_ssl: 是否校验 SSL。
            headers: 固定请求头。
            auth: 签名配置（来自 profile 的 `auth` 段）。
            credential: 可选凭证包（签名取值来源）。
        """
        self.base_url = (base_url or "").rstrip("/")
        self.timeout_sec = float(timeout_sec)
        self.max_retry = max(int(max_retry), 0)
        self.retry_backoff_sec = float(retry_backoff_sec)
        self.verify_ssl = bool(verify_ssl)
        self.headers = dict(headers or {})
        self.auth = dict(auth or {})
        self.credential = credential
        self._client: httpx.AsyncClient | None = None

    # ---------------- 生命周期 ----------------

    async def _ensure_client(self) -> httpx.AsyncClient:
        """惰性创建 AsyncClient（连接池复用）。"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url or "",
                timeout=self.timeout_sec,
                verify=self.verify_ssl,
                headers=self.headers,
            )
        return self._client

    async def aclose(self) -> None:
        """关闭连接。"""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # ---------------- 签名 ----------------

    def _build_signed_payload(self, body: dict[str, Any]) -> dict[str, Any]:
        """按 profile 的 auth 段注入签名字段（app_key / timestamp / sign）。

        ★ 签名算法未经实测（PRD Q2），默认实现为 `md5(secret + sorted(params) + secret)` 的常见变体；
          真实算法请在 profile YAML 的 `auth.sign_algo` 中切换（`md5` / `hmac_sha256` / `none`）。
        """
        if not self.auth:
            return body
        payload = dict(body)
        if self.auth.get("app_key_param"):
            payload[str(self.auth["app_key_param"])] = self._credential_value(
                self.auth.get("app_key_credential", "app_key")
            )
        if self.auth.get("timestamp_param"):
            payload[str(self.auth["timestamp_param"])] = iso_utc(utc_now())
        algo = str(self.auth.get("sign_algo", "md5")).lower()
        if algo != "none" and self.auth.get("sign_param"):
            secret = self._credential_value(self.auth.get("secret_credential", "app_secret"))
            canonical = "".join(f"{k}{payload[k]}" for k in sorted(payload))
            if algo == "hmac_sha256":
                import hashlib
                import hmac

                signature = hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
            else:
                import hashlib

                signature = hashlib.md5(f"{secret}{canonical}{secret}".encode("utf-8")).hexdigest()
            payload[str(self.auth["sign_param"])] = signature
        return payload

    def _credential_value(self, key: str) -> str:
        """从凭证包取值（缺失返回空串，绝不抛异常）。"""
        if self.credential is None:
            return ""
        getter = getattr(self.credential, "get", None)
        if callable(getter):
            try:
                return str(getter(key, "") or "")
            except Exception:  # noqa: BLE001
                return ""
        return ""

    # ---------------- 核心请求 ----------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        path_params: dict[str, Any] | None = None,
        trace_id: str = "",
    ) -> HttpResult:
        """发起请求，返回 `HttpResult`（永不抛异常）。

        失败分类：
            * 网络错误 / 超时 / 429 / 5xx → `retryable=True`；
            * 4xx（除 429） / 业务失败码 → `retryable=False`。
        """
        if not self.base_url:
            return HttpResult(ok=False, error="未配置 base_url（适配器未接入凭证与端点）", retryable=False)

        url = render_path(path, path_params or {})
        payload = self._build_signed_payload(dict(body or {}))
        headers = {"X-Trace-Id": trace_id or get_trace_id()}
        client = await self._ensure_client()

        last_error = ""
        last_status = 0
        for attempt in range(self.max_retry + 1):
            started = time.perf_counter()
            try:
                response = await client.request(
                    method.upper(),
                    url,
                    json=payload if method.upper() != "GET" else None,
                    params=dict(params or {}) if method.upper() == "GET" else None,
                    headers=headers,
                )
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                last_status = response.status_code
                try:
                    raw: Any = response.json()
                except Exception:  # noqa: BLE001
                    raw = {"text": response.text[:500]}

                if response.status_code == 429 or response.status_code >= 500:
                    last_error = f"HTTP {response.status_code}"
                    if attempt < self.max_retry:
                        await asyncio.sleep(self.retry_backoff_sec * (2**attempt))
                        continue
                    return HttpResult(
                        ok=False,
                        status_code=response.status_code,
                        raw=raw,
                        error=last_error,
                        retryable=True,
                        elapsed_ms=elapsed_ms,
                    )

                if 400 <= response.status_code < 500:
                    return HttpResult(
                        ok=False,
                        status_code=response.status_code,
                        raw=raw,
                        error=f"HTTP {response.status_code}",
                        retryable=False,
                        elapsed_ms=elapsed_ms,
                    )

                return HttpResult(ok=True, status_code=response.status_code, data=raw, raw=raw, elapsed_ms=elapsed_ms)

            except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
                last_error = f"网络错误：{exc}"
                if attempt < self.max_retry:
                    await asyncio.sleep(self.retry_backoff_sec * (2**attempt))
                    continue
                return HttpResult(ok=False, error=last_error, retryable=True, status_code=last_status)
            except Exception as exc:  # noqa: BLE001  ★ 绝不抛裸异常
                return HttpResult(ok=False, error=f"{type(exc).__name__}: {exc}", retryable=False, status_code=last_status)

        return HttpResult(ok=False, error=last_error or "请求失败", retryable=True)

    # ---------------- profile 驱动的便捷调用 ----------------

    async def call_spec(
        self,
        spec: HttpRequestSpec,
        *,
        context: dict[str, Any] | None = None,
        path_params: dict[str, Any] | None = None,
        trace_id: str = "",
    ) -> HttpResult:
        """按 `HttpRequestSpec` 执行：渲染 body 模板 → 请求 → 按响应映射抽取字段。"""
        context = dict(context or {})
        body = _render_template(spec.body_template, context)
        params = _render_template(spec.query_template, context)
        result = await self.request(
            spec.method,
            spec.path,
            body=body,
            params=params,
            path_params=path_params or context,
            trace_id=trace_id,
        )
        if result.ok and spec.response_mapping:
            result.data = _map_response(result.raw, spec.response_mapping)
        return result

    async def health_ping(self, path: str = "", method: str = "GET") -> HttpResult:
        """连通性自检。"""
        return await self.request(method, path or "/", body={} if method.upper() != "GET" else None)


def _render_template(template: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """渲染请求体模板：`{"order_no": "{platform_order_no}"}` + context → 实际值。

    模板中未提供的键会被跳过（防御性：第三方字段不确定时不崩）。
    """
    rendered: dict[str, Any] = {}
    for key, raw in (template or {}).items():
        if isinstance(raw, str) and "{" in raw and "}" in raw:
            value: Any = raw
            for ctx_key, ctx_value in context.items():
                value = str(value).replace("{%s}" % ctx_key, str(ctx_value))
            if "{" in str(value) and "}" in str(value):
                continue  # 占位符未全部提供 → 跳过该字段
            rendered[key] = value
        else:
            rendered[key] = raw
    return rendered


def _map_response(raw: Any, mapping: dict[str, Any]) -> dict[str, Any]:
    """按响应映射抽取字段（支持候选路径列表）。"""
    mapped: dict[str, Any] = {}
    for key, path_spec in (mapping or {}).items():
        if isinstance(path_spec, list):
            mapped[key] = pick_first(raw, [str(p) for p in path_spec])
        else:
            mapped[key] = pick(raw, str(path_spec))
    return mapped


# ---------------------------------------------------------------------------
#  profile YAML 加载（端点与字段映射外置，改配置不改代码）
# ---------------------------------------------------------------------------

PROFILES_DIR = Path(__file__).resolve().parent / "profiles"
_profile_cache: dict[str, dict[str, Any]] = {}


def load_profile(adapter_name: str, *, reload: bool = False) -> dict[str, Any]:
    """加载适配器 profile YAML。

    Args:
        adapter_name: 适配器名（与 `profiles/{adapter}.yaml` 同名）。
        reload: 是否忽略缓存重新读取。

    Returns:
        profile 字典；文件不存在或解析失败时返回空字典（★ 绝不抛异常）。
    """
    if not reload and adapter_name in _profile_cache:
        return _profile_cache[adapter_name]

    path = PROFILES_DIR / f"{adapter_name}.yaml"
    if not path.exists():
        logger.warning("adapter_profile_missing", adapter=adapter_name, path=str(path))
        _profile_cache[adapter_name] = {}
        return {}

    try:
        import yaml

        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        if not isinstance(data, dict):
            data = {}
    except Exception as exc:  # noqa: BLE001
        logger.error("adapter_profile_parse_failed", adapter=adapter_name, error=str(exc))
        data = {}

    _profile_cache[adapter_name] = data
    return data


def build_spec(profile: dict[str, Any], capability: str) -> HttpRequestSpec | None:
    """从 profile 构建 `HttpRequestSpec`；端点未配置返回 None。"""
    endpoint = (profile.get("endpoints") or {}).get(capability)
    if not isinstance(endpoint, dict) or not endpoint.get("path"):
        return None
    return HttpRequestSpec(
        method=str(endpoint.get("method", "POST")),
        path=str(endpoint.get("path", "")),
        body_template=dict(endpoint.get("request") or {}),
        response_mapping=dict(endpoint.get("response") or {}),
        note=str(endpoint.get("note", "")),
    )


def endpoint_field_map(profile: dict[str, Any], capability: str, section: str) -> dict[str, Any]:
    """取端点内的额外字段映射（如 `order_mapping`）。"""
    endpoint = (profile.get("endpoints") or {}).get(capability)
    if not isinstance(endpoint, dict):
        return {}
    section_data = endpoint.get(section)
    return dict(section_data) if isinstance(section_data, dict) else {}


def profile_capabilities(profile: dict[str, Any]) -> dict[str, Any]:
    """取 profile 中声明的能力矩阵。"""
    caps = profile.get("capabilities")
    return dict(caps) if isinstance(caps, dict) else {}


def profile_required_scopes(profile: dict[str, Any]) -> list[str]:
    """取 profile 中声明的 scope 列表。"""
    scopes = profile.get("required_scopes")
    return [str(s) for s in scopes] if isinstance(scopes, list) else []
