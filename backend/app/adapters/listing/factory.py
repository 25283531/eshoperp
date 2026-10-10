"""ListingAdapterFactory：按 (platform, mode) 从注册表取类并实例化（§5.1）。

热切换：`mode=None` 时读 `SystemSetting['listing.mode']`（默认 `mock`，ADR-4）。
降级：真实适配器 `available() == False`（未取得资质）时自动降级到 `MockListingAdapter`。
"""

from __future__ import annotations

import inspect
from typing import Any

from sqlalchemy import select

from app.adapters.listing.base import ListingAdapter
from app.adapters.listing.douyin import DouyinAdapter
from app.adapters.listing.manual import ManualListingAdapter
from app.adapters.listing.mock import MockListingAdapter
from app.adapters.listing.pdd import PddAdapter
from app.adapters.listing.taobao import TaobaoAdapter
from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.enums import ListingMode, Platform, SettingKey
from app.models.system import SystemSetting

logger = get_logger(__name__)

__all__ = [
    "LISTING_ADAPTER_REGISTRY",
    "ListingAdapterFactory",
    "get_listing_adapter",
    "register_listing_adapter",
    "resolve_listing_mode",
]


# 注册表：{(platform, mode): adapter_class}
LISTING_ADAPTER_REGISTRY: dict[tuple[str, str], type[ListingAdapter]] = {
    (Platform.TAOBAO.value, ListingMode.REAL.value): TaobaoAdapter,
    (Platform.DOUYIN.value, ListingMode.REAL.value): DouyinAdapter,
    (Platform.PDD.value, ListingMode.REAL.value): PddAdapter,
    (Platform.TAOBAO.value, ListingMode.MOCK.value): MockListingAdapter,
    (Platform.DOUYIN.value, ListingMode.MOCK.value): MockListingAdapter,
    (Platform.PDD.value, ListingMode.MOCK.value): MockListingAdapter,
    (Platform.TAOBAO.value, ListingMode.MANUAL.value): ManualListingAdapter,
    (Platform.DOUYIN.value, ListingMode.MANUAL.value): ManualListingAdapter,
    (Platform.PDD.value, ListingMode.MANUAL.value): ManualListingAdapter,
}


def register_listing_adapter(platform: str, mode: str, adapter_cls: type[ListingAdapter]) -> None:
    """注册 / 覆盖上架适配器实现（新增平台零改动核心流程）。"""
    LISTING_ADAPTER_REGISTRY[(platform, mode)] = adapter_cls
    logger.info("listing_adapter_registered", platform=platform, mode=mode, cls=adapter_cls.__name__)


async def resolve_listing_mode(session: Any = None) -> str:
    """读取当前上架模式：优先 SystemSetting['listing.mode']，回退 Settings.listing_mode。"""
    if session is not None:
        try:
            stmt = select(SystemSetting).where(SystemSetting.setting_key == SettingKey.LISTING_MODE.value)
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is not None and row.setting_value:
                return str(row.setting_value).strip()
        except Exception as exc:  # noqa: BLE001  表不存在（迁移前）时回退默认
            logger.warning("listing_mode_read_failed", error=str(exc))
    return get_settings().listing_mode


def _instantiate(
    adapter_cls: type[ListingAdapter],
    *,
    platform_value: str,
    account: Any = None,
    config: dict[str, Any] | None = None,
    session: Any = None,
    http: Any = None,
) -> ListingAdapter:
    """★ 实例化适配器，并在类支持时把 `platform` 传进去。

    ★★ 踩坑记录（P0，实测复现）★★
        旧实现统一 `adapter_cls(account=..., config=..., session=..., http=...)`，
        **不传 platform**。而 `ManualListingAdapter.__init__` 的 `platform` 默认值是
        `Platform.TAOBAO` ⇒ 经本工厂创建的半自动适配器**无论什么平台都是淘宝**：
        抖店 / 拼多多的任务会被打成 `manual-taobao-*.zip`，README.txt 里写
        「登录淘宝商家后台」，运营拿着抖店的包去登淘宝，属于直接误导。

        只给「构造函数接受 platform」的类传参（`ManualListingAdapter` /
        `MockListingAdapter`）；`TaobaoAdapter` / `DouyinAdapter` / `PddAdapter`
        的 `__init__` 不收该参数且内部硬编码 `self.platform`，按签名过滤对它们零影响。

    Args:
        adapter_cls: 注册表里取出的适配器类。
        platform_value: 归一化后的平台字符串（`taobao` / `douyin` / `pdd`）。
    """
    kwargs: dict[str, Any] = {"account": account, "config": config, "session": session, "http": http}
    try:
        params = inspect.signature(adapter_cls).parameters
    except (TypeError, ValueError):  # noqa: BLE001  签名不可解析时按不支持处理
        params = {}
    if "platform" in params:
        kwargs["platform"] = platform_value
    return adapter_cls(**kwargs)


class ListingAdapterFactory:
    """上架适配器工厂。"""

    @staticmethod
    async def create(
        platform: str | Platform,
        mode: str | ListingMode | None = None,
        *,
        session: Any = None,
        account: Any = None,
        config: dict[str, Any] | None = None,
        http: Any = None,
        allow_fallback: bool = True,
    ) -> ListingAdapter:
        """创建上架适配器实例。

        Args:
            platform: 平台（taobao / douyin / pdd）。
            mode: 上架模式；None 时读 SystemSetting['listing.mode']。
            session: 数据库会话（半自动适配器需要）。
            account: 平台账号（真实适配器需要凭证）。
            config: 适配器私有配置。
            http: HTTP 客户端。
            allow_fallback: 真实适配器不可用时是否降级到 Mock（默认 True）。

        Returns:
            上架适配器实例。
        """
        platform_value = platform.value if isinstance(platform, Platform) else str(platform)
        mode_value = mode.value if isinstance(mode, ListingMode) else (str(mode) if mode else None)
        if not mode_value:
            mode_value = await resolve_listing_mode(session)

        adapter_cls = LISTING_ADAPTER_REGISTRY.get((platform_value, mode_value))
        if adapter_cls is None:
            logger.warning(
                "listing_adapter_not_registered",
                platform=platform_value,
                mode=mode_value,
                fallback=ListingMode.MOCK.value,
            )
            adapter_cls = MockListingAdapter
            mode_value = ListingMode.MOCK.value

        adapter = _instantiate(
            adapter_cls,
            platform_value=platform_value,
            account=account,
            config=config,
            session=session,
            http=http,
        )

        # ★ 降级：真实适配器未取得资质 → 自动退回 Mock
        if allow_fallback and not adapter.available():
            logger.warning(
                "listing_adapter_unavailable_fallback_mock",
                platform=platform_value,
                mode=mode_value,
                cls=adapter_cls.__name__,
            )
            adapter = _instantiate(
                MockListingAdapter,
                platform_value=platform_value,
                account=account,
                config=config,
                session=session,
                http=http,
            )

        return adapter

    @staticmethod
    def list_adapters() -> list[dict[str, Any]]:
        """列出全部上架适配器及其可用性（供 `GET /adapters/listing`）。"""
        items: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for (platform_value, mode_value), cls in LISTING_ADAPTER_REGISTRY.items():
            key = (platform_value, mode_value)
            if key in seen:
                continue
            seen.add(key)
            try:
                probe = cls()
                available = probe.available()
                note = probe.note()
            except Exception:  # noqa: BLE001
                available = False
                note = "初始化失败"
            items.append(
                {
                    "platform": platform_value,
                    "mode": mode_value,
                    "adapter": cls.__name__,
                    "available": available,
                    "note": note,
                }
            )
        return sorted(items, key=lambda i: (i["platform"], i["mode"]))

    @staticmethod
    def current_mode(session: Any = None) -> str:
        """同步获取当前上架模式（配置回退）。"""
        return get_settings().listing_mode


async def get_listing_adapter(
    platform: str | Platform,
    mode: str | ListingMode | None = None,
    **kwargs: Any,
) -> ListingAdapter:
    """便捷函数：等价于 `ListingAdapterFactory.create(...)`。"""
    return await ListingAdapterFactory.create(platform, mode, **kwargs)
