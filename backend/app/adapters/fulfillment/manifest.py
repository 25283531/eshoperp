"""能力声明 manifest 与统一结果信封（§5.3）。

★ 核心设计：
    * `AdapterResult` 是所有适配器方法的**唯一**返回类型 —— 禁止抛裸异常；
    * `ResultCode.UNSUPPORTED` 表示"能力未开放"，调度层按 `fallback` 静默降级；
    * `CapabilitySpec.unverified=True` 表示端点/字段未实测确认（PRD Q2），前端展示"待实测"。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from app.models.enums import Capability, CapabilityLevel, ResultCode

T = TypeVar("T")

__all__ = [
    "AdapterConfig",
    "AdapterManifest",
    "AdapterResult",
    "Capability",
    "CapabilityLevel",
    "CapabilitySpec",
    "CredentialBundle",
    "HealthStatus",
    "ResultCode",
    "normalize_capability",
]


def normalize_capability(capability: Any) -> Capability | None:
    """把 `Capability | str | None` 归一化为 `Capability` 枚举；无法识别返回 `None`。

    ★ 踩坑记录（Bug 3，系统级：履约链路全断）：
      `FulfillmentAdapter.invoke()` 内部一律按 `capability.value` 取值，
      而 7 个业务调用点传的是**字符串**（`"fetch_orders"` / `"match_sku"` /
      `"place_purchase_order"` / `"submit_refund"` / `"push_inventory_change"` ...），
      `str` 没有 `.value` → `AttributeError` → 订单同步任务 3 次重试后 failed，
      整个履约链路（订单同步 / SKU 匹配 / 采购下单 / 退款 / 退货地址 / 库存推送）不可用。

      ★ 修法沿用 `normalize_platform` 的同一原则：**在 `invoke()` 入口做唯一一次归一化**，
        而不是去 7 个调用点逐个改 —— 以后新增调用点传枚举或字符串都能工作。

    Args:
        capability: 枚举 / 字符串 / None。

    Returns:
        归一化后的 `Capability`；无法识别时返回 `None`（由调用方转 UNSUPPORTED 信封）。
    """
    if isinstance(capability, Capability):
        return capability
    if capability is None:
        return None
    raw = str(capability).strip().lower()
    try:
        return Capability(raw)
    except ValueError:
        return None


# ============================================================================
#  适配器配置与凭证
# ============================================================================


@dataclass
class CredentialBundle:
    """解密后的凭证集合（★ 明文只在内存中流转，永不落库、永不入日志）。"""

    owner_type: str = ""
    owner_key: str = ""
    values: dict[str, str] = field(default_factory=dict)

    def get(self, key: str, default: str = "") -> str:
        """安全取用凭证项。"""
        return self.values.get(key, default)

    def keys(self) -> list[str]:
        """返回凭证项名称列表（不含值）。"""
        return sorted(self.values.keys())

    def __bool__(self) -> bool:
        """是否存在有效凭证。"""
        return bool(self.values)


@dataclass
class AdapterConfig:
    """适配器运行时配置（来自 `fulfillment_adapter.config_json` + profile YAML）。"""

    adapter_name: str
    display_name: str = ""
    base_url: str = ""
    timeout_sec: float = 20.0
    max_retry: int = 3
    retry_backoff_sec: float = 0.5
    verify_ssl: bool = True
    endpoints: dict[str, Any] = field(default_factory=dict)
    request_templates: dict[str, Any] = field(default_factory=dict)
    response_mapping: dict[str, Any] = field(default_factory=dict)
    auth: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None, *, adapter_name: str, display_name: str = "") -> "AdapterConfig":
        """从配置字典构造，缺省项自动填充。"""
        raw = raw or {}
        return cls(
            adapter_name=adapter_name,
            display_name=display_name or raw.get("display_name", adapter_name),
            base_url=str(raw.get("base_url", "")),
            timeout_sec=float(raw.get("timeout_sec", 20.0)),
            max_retry=int(raw.get("max_retry", 3)),
            retry_backoff_sec=float(raw.get("retry_backoff_sec", 0.5)),
            verify_ssl=bool(raw.get("verify_ssl", True)),
            endpoints=dict(raw.get("endpoints", {})),
            request_templates=dict(raw.get("request_templates", {})),
            response_mapping=dict(raw.get("response_mapping", {})),
            auth=dict(raw.get("auth", {})),
            extra={k: v for k, v in raw.items() if k not in {
                "display_name", "base_url", "timeout_sec", "max_retry", "retry_backoff_sec",
                "verify_ssl", "endpoints", "request_templates", "response_mapping", "auth",
            }},
        )


# ============================================================================
#  能力声明
# ============================================================================


@dataclass
class CapabilitySpec:
    """单项能力声明。"""

    name: Capability
    level: CapabilityLevel
    fallback: str | None = None  # "local_csv" | "manual" | "csv_export" | None(直接跳过)
    note: str = ""  # 中文说明，前端能力矩阵表直接展示
    unverified: bool = False  # TODO：真实端点/字段未确认时置 True，前端显示"待实测"

    def to_dict(self) -> dict[str, Any]:
        """序列化（持久化到 fulfillment_adapter.capability_json）。"""
        return {
            "name": self.name.value,
            "level": self.level.value,
            "fallback": self.fallback,
            "note": self.note,
            "unverified": self.unverified,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CapabilitySpec":
        """反序列化。"""
        name_value = str(raw.get("name", ""))
        try:
            name = Capability(name_value)
        except ValueError:
            name = Capability.MATCH_SKU
        try:
            level = CapabilityLevel(str(raw.get("level", CapabilityLevel.UNSUPPORTED.value)))
        except ValueError:
            level = CapabilityLevel.UNSUPPORTED
        return cls(
            name=name,
            level=level,
            fallback=raw.get("fallback"),
            note=str(raw.get("note", "")),
            unverified=bool(raw.get("unverified", False)),
        )


@dataclass
class AdapterManifest:
    """适配器能力矩阵。"""

    adapter_name: str
    display_name: str
    version: str = "0.1.0"
    capabilities: dict[Capability, CapabilitySpec] = field(default_factory=dict)
    required_scopes: list[str] = field(default_factory=list)
    config_schema: dict[str, Any] = field(default_factory=dict)
    docs_url: str | None = None

    def supports(self, capability: Capability) -> bool:
        """是否支持该能力（DEGRADED 也算支持，只是走降级通道）。"""
        spec = self.capabilities.get(capability)
        return spec is not None and spec.level != CapabilityLevel.UNSUPPORTED

    def spec(self, capability: Capability) -> CapabilitySpec | None:
        """取能力规格。"""
        return self.capabilities.get(capability)

    def level_of(self, capability: Capability) -> CapabilityLevel:
        """取能力级别，未声明则 UNSUPPORTED。"""
        spec = self.capabilities.get(capability)
        return spec.level if spec else CapabilityLevel.UNSUPPORTED

    def fallback_of(self, capability: Capability) -> str | None:
        """取能力降级目标。"""
        spec = self.capabilities.get(capability)
        return spec.fallback if spec else None

    def to_dict(self) -> dict[str, Any]:
        """序列化（持久化到 fulfillment_adapter.capability_json）。"""
        return {
            "adapter_name": self.adapter_name,
            "display_name": self.display_name,
            "version": self.version,
            "capabilities": {c.value: spec.to_dict() for c, spec in self.capabilities.items()},
            "required_scopes": list(self.required_scopes),
            "config_schema": self.config_schema,
            "docs_url": self.docs_url,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AdapterManifest":
        """反序列化。"""
        capabilities: dict[Capability, CapabilitySpec] = {}
        for key, spec_raw in dict(raw.get("capabilities", {})).items():
            try:
                capability = Capability(str(key))
            except ValueError:
                continue
            if isinstance(spec_raw, dict):
                spec_raw = {**spec_raw, "name": capability.value}
                capabilities[capability] = CapabilitySpec.from_dict(spec_raw)
            elif isinstance(spec_raw, str):
                capabilities[capability] = CapabilitySpec(
                    name=capability,
                    level=CapabilityLevel(spec_raw),
                )
        return cls(
            adapter_name=str(raw.get("adapter_name", "")),
            display_name=str(raw.get("display_name", "")),
            version=str(raw.get("version", "0.1.0")),
            capabilities=capabilities,
            required_scopes=list(raw.get("required_scopes", [])),
            config_schema=dict(raw.get("config_schema", {})),
            docs_url=raw.get("docs_url"),
        )


# ============================================================================
#  统一结果信封
# ============================================================================


@dataclass
class AdapterResult(Generic[T]):
    """★ 所有适配器方法的唯一返回类型，禁止抛裸异常（红线 R3）。"""

    ok: bool
    code: ResultCode
    data: T | None = None
    message: str = ""
    fallback: str | None = None  # 建议的降级通道
    trace_id: str = ""
    elapsed_ms: int = 0

    @classmethod
    def success(cls, data: T, message: str = "ok", **kwargs: Any) -> "AdapterResult[T]":
        """成功结果。"""
        return cls(ok=True, code=ResultCode.OK, data=data, message=message, **kwargs)

    @classmethod
    def unsupported(cls, capability: str, fallback: str | None = None, message: str = "") -> "AdapterResult[Any]":
        """能力不支持（★ 不抛异常，由调度层按 fallback 降级）。"""
        return cls(
            ok=False,
            code=ResultCode.UNSUPPORTED,
            data=None,
            message=message or f"适配器未开放能力：{capability}",
            fallback=fallback,
        )

    @classmethod
    def degraded(cls, data: T, message: str, **kwargs: Any) -> "AdapterResult[T]":
        """走了降级通道，结果可用但延迟高 / 需人工介入。"""
        return cls(ok=True, code=ResultCode.DEGRADED, data=data, message=message, **kwargs)

    @classmethod
    def retryable(cls, method: str, error: Exception | str, **kwargs: Any) -> "AdapterResult[Any]":
        """可重试错误（网络 / 限流）。"""
        message = str(error) if isinstance(error, Exception) else error
        return cls(ok=False, code=ResultCode.RETRYABLE, data=None, message=f"{method} 可重试失败：{message}", **kwargs)

    @classmethod
    def fatal(cls, method: str, error: Exception | str, **kwargs: Any) -> "AdapterResult[Any]":
        """不可重试错误（参数 / 鉴权）。★ 异常统一转为此结果，绝不向上抛裸异常。"""
        message = str(error) if isinstance(error, Exception) else error
        return cls(ok=False, code=ResultCode.FATAL, data=None, message=f"{method} 执行失败：{message}", **kwargs)

    @classmethod
    def scope_denied(cls, scopes: list[str], message: str = "") -> "AdapterResult[Any]":
        """★ 越权被拒（红线 R1）。"""
        return cls(
            ok=False,
            code=ResultCode.SCOPE_DENIED,
            data=None,
            message=message or f"越权 scope 被拒绝：{', '.join(scopes)}",
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化（写入日志 / API 响应）。"""
        return {
            "ok": self.ok,
            "code": self.code.value,
            "data": self.data,
            "message": self.message,
            "fallback": self.fallback,
            "trace_id": self.trace_id,
            "elapsed_ms": self.elapsed_ms,
        }

    def unwrap(self) -> T:
        """取数据；失败时抛 ValueError（仅供明确预期成功的内部调用使用）。"""
        if not self.ok or self.data is None:
            raise ValueError(f"适配器返回失败结果：{self.code.value} {self.message}")
        return self.data


@dataclass
class HealthStatus:
    """适配器连通性自检结果。"""

    healthy: bool
    status: str  # healthy / degraded / down / unknown
    message: str
    latency_ms: int = 0
    checked_at: str = ""

    @classmethod
    def unknown(cls, message: str = "未进行连通性自检") -> "HealthStatus":
        """未知状态。"""
        from app.utils.kit import iso_utc

        return cls(healthy=False, status="unknown", message=message, latency_ms=0, checked_at=iso_utc())

    @classmethod
    def build(cls, healthy: bool, status: str, message: str, started: float) -> "HealthStatus":
        """按开始时间计算耗时构造。"""
        from app.utils.kit import iso_utc

        return cls(
            healthy=healthy,
            status=status,
            message=message,
            latency_ms=int((time.perf_counter() - started) * 1000),
            checked_at=iso_utc(),
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "healthy": self.healthy,
            "status": self.status,
            "message": self.message,
            "latency_ms": self.latency_ms,
            "checked_at": self.checked_at,
        }
