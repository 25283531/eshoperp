"""履约适配器注册表（ADR-6：配置驱动热切换）。

用法：

    @register("miaoshou", display_name="妙手")
    class MiaoshouAdapter(FulfillmentAdapter): ...
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "ADAPTER_REGISTRY",
    "AdapterMeta",
    "get_adapter_class",
    "get_adapter_meta",
    "list_adapter_names",
    "list_adapters",
    "register",
    "unregister",
]


class AdapterMeta:
    """注册表条目元信息。"""

    __slots__ = ("name", "display_name", "cls")

    def __init__(self, name: str, display_name: str, cls: type[Any]) -> None:
        self.name = name
        self.display_name = display_name
        self.cls = cls

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {"name": self.name, "display_name": self.display_name, "class": self.cls.__name__}


# 全局注册表：{adapter_name: AdapterMeta}
ADAPTER_REGISTRY: dict[str, AdapterMeta] = {}


def register(name: str, display_name: str = "") -> Any:
    """适配器注册装饰器。

    Args:
        name: 适配器唯一标识（如 `miaoshou`）。
        display_name: 中文展示名（如 `妙手`）。
    """

    def _decorator(cls: type[Any]) -> type[Any]:
        ADAPTER_REGISTRY[name] = AdapterMeta(name=name, display_name=display_name or getattr(cls, "display_name", name), cls=cls)
        logger.info("fulfillment_adapter_registered", name=name, display_name=display_name, cls=cls.__name__)
        return cls

    return _decorator


def unregister(name: str) -> None:
    """注销适配器（测试用）。"""
    ADAPTER_REGISTRY.pop(name, None)


def get_adapter_class(name: str) -> type[Any] | None:
    """取适配器类，未注册返回 None。"""
    meta = ADAPTER_REGISTRY.get(name)
    return meta.cls if meta else None


def get_adapter_meta(name: str) -> AdapterMeta | None:
    """取适配器元信息。"""
    return ADAPTER_REGISTRY.get(name)


def list_adapter_names() -> list[str]:
    """已注册的适配器名称列表。"""
    return sorted(ADAPTER_REGISTRY.keys())


def list_adapters() -> list[dict[str, Any]]:
    """列出全部注册适配器（供 API 展示）。"""
    return [ADAPTER_REGISTRY[name].to_dict() for name in list_adapter_names()]


def is_registered(name: str) -> bool:
    """是否已注册。"""
    return name in ADAPTER_REGISTRY
