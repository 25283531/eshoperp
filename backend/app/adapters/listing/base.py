"""上架适配器抽象基类（§5.1）。

★ 红线 R2 的另一半：`offline()` 与 `update_stock_price()` **只在本基类及其子类中定义**，
  是"写店铺商品"能力的唯一入口。第三方（FulfillmentAdapter）永远拿不到。

约定：
    1. 所有方法返回 `AdapterResult`，绝不向上抛业务异常（异常由 `invoke()` 兜底转 FATAL）；
    2. 真实适配器未取得资质时，由工厂按 `SystemSetting['listing.mode']` 返回 `MockListingAdapter`；
    3. Mock 模式产出的所有数据必须带 `is_mock=True`，且不参与真实履约。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.adapters.fulfillment.manifest import AdapterResult, HealthStatus  # 复用统一结果信封
from app.core.logging import get_logger, get_trace_id
from app.models.enums import ListingMode, Platform

logger = get_logger(__name__)

__all__ = [
    "Platform",
    "ListingMode",
    "ListingSkuPayload",
    "ListingPayload",
    "ListingPublishResult",
    "ListingStatus",
    "ManualPackage",
    "ListingAdapter",
    "normalize_platform",
]


def normalize_platform(platform: Any, default: Platform = Platform.TAOBAO) -> Platform:
    """把 `Platform | str | None` 统一归一化为 `Platform` 枚举。

    ★ 踩坑记录（QA 探针 B 实测复现，P0）：
      数据库列 `publish_task.platform` 是**字符串**，服务层直接写
      `ManualListingAdapter(session=session, platform=task.platform)` 会把 str 塞进来；
      而适配器内部一律按 `self.platform.value` 取值 →
      `AttributeError: 'str' object has no attribute 'value'`，
      半自动主路径的 `form-data` / `package` 两个运营高频接口 100% 500。
      （决策①把半自动升级为主路径后，这就是最常用的一条链，必须堵死。）

      因此所有接受 `platform` 的适配器**必须**在 `__init__` 里走本函数做唯一一次归一化，
      而不是依赖调用方"记得传枚举"。

    Args:
        platform: 枚举 / 字符串 / None。
        default: 无法识别时的兜底平台（默认淘宝）。

    Returns:
        归一化后的 `Platform` 枚举。
    """
    if isinstance(platform, Platform):
        return platform
    if platform is None or (isinstance(platform, str) and not platform.strip()):
        return default
    raw = str(platform).strip().lower()
    try:
        return Platform(raw)
    except ValueError:
        logger.warning("unknown_platform_fallback", raw=raw, fallback=default.value)
        return default


# ---------------------------------------------------------------------------
#  载荷定义
# ---------------------------------------------------------------------------


@dataclass
class ListingSkuPayload:
    """单个 SKU 的上架载荷。"""

    spec_json: dict[str, str]
    sale_price_cents: int
    stock_qty: int
    source_sku_id: int | None = None  # 关联货源 SKU（用于回建映射）
    source_sku_code_1688: str | None = None
    purchase_cost_cents: int | None = None  # 采购成本（分），必须 > 0


@dataclass
class ListingPayload:
    """上架请求载荷（由 PublishService 组装，适配器不感知业务表）。"""

    shop_id: str
    title: str
    selling_points: list[str]
    attributes_json: dict[str, Any]
    category_id: str | None
    main_images: list[str]
    detail_images: list[str]
    skus: list[ListingSkuPayload]
    source_product_id: int
    ai_task_result_id: int | None = None
    trace_id: str = ""


@dataclass
class ListingPublishResult:
    """上架结果。"""

    shop_item_id: str
    sku_results: list[dict[str, Any]] = field(default_factory=list)
    # sku_results 元素: {"spec_json": {...}, "shop_sku_code": "...", "success": True}
    is_mock: bool = False
    raw_response: dict[str, Any] | None = None


@dataclass
class ListingStatus:
    """平台商品状态查询结果。"""

    shop_item_id: str
    status: str  # on_sale / off_shelf / publishing / failed
    shop_sku_codes: list[str] = field(default_factory=list)
    updated_at: str | None = None  # ISO8601 UTC


@dataclass
class ManualPackage:
    """半自动模式产物：素材包 ZIP + 预填表单数据。"""

    package_path: str  # data/packages/{task_id}.zip
    form_data: dict[str, Any]  # 可直接复制到平台后台的标题/卖点/属性/价格
    image_files: list[str]
    instructions: str  # 人工操作指引（中文）

    def to_dict(self) -> dict[str, Any]:
        """序列化（API 响应）。"""
        return {
            "package_path": self.package_path,
            "form_data": self.form_data,
            "image_files": self.image_files,
            "instructions": self.instructions,
        }


# ---------------------------------------------------------------------------
#  抽象基类
# ---------------------------------------------------------------------------


class ListingAdapter(ABC):
    """上架适配器抽象基类。"""

    platform: Platform
    mode: ListingMode

    def __init__(
        self,
        account: Any = None,
        config: dict[str, Any] | None = None,
        session: Any = None,
        http: Any = None,
    ) -> None:
        """初始化：平台账号 / 配置 / 会话 / HTTP 客户端。"""
        self.account = account
        self.config = dict(config or {})
        self.session = session
        self.http = http

    # ---------------- 必须实现 ----------------

    @abstractmethod
    async def publish(self, payload: ListingPayload) -> AdapterResult[ListingPublishResult]:
        """发布商品到平台。"""
        raise NotImplementedError

    @abstractmethod
    async def query_status(self, shop_item_ids: Sequence[str]) -> AdapterResult[list[ListingStatus]]:
        """查询平台商品状态。"""
        raise NotImplementedError

    @abstractmethod
    async def offline(self, shop_item_ids: Sequence[str], reason: str) -> AdapterResult[dict]:
        """下架。★ 这是"下架"能力的唯一入口，第三方永远拿不到（红线 R2）。"""
        raise NotImplementedError

    @abstractmethod
    async def update_stock_price(self, items: Sequence[dict]) -> AdapterResult[dict]:
        """改库存与价格。items: [{"shop_item_id","shop_sku_code","stock_qty","price_cents"}]"""
        raise NotImplementedError

    @abstractmethod
    async def health_check(self) -> AdapterResult[HealthStatus]:
        """连通性自检。"""
        raise NotImplementedError

    # ---------------- 可选实现 ----------------

    async def build_manual_package(self, payload: ListingPayload) -> AdapterResult[ManualPackage]:
        """仅 `ManualListingAdapter` 实现；其他模式返回 UNSUPPORTED。"""
        return AdapterResult.unsupported("build_manual_package")

    def available(self) -> bool:
        """适配器是否可用（真实适配器未取得资质时返回 False，由工厂降级到 Mock）。"""
        return True

    def note(self) -> str:
        """前端展示的适配器说明。"""
        return f"{getattr(self.platform, 'value', '')}-{getattr(self.mode, 'value', '')}"

    # ---------------- 统一调用入口（异常兜底，禁止裸异常打断流程）----------------

    async def invoke(self, method: str, **kwargs: Any) -> AdapterResult[Any]:
        """统一入口：异常转 FATAL 结果信封。"""
        started = time.perf_counter()
        try:
            fn = getattr(self, method)
            result = await fn(**kwargs)
        except Exception as exc:  # noqa: BLE001
            result = AdapterResult.fatal(method=method, error=exc)
            logger.exception(
                "listing_adapter_error",
                platform=getattr(self.platform, "value", ""),
                mode=getattr(self.mode, "value", ""),
                method=method,
                error=str(exc),
            )
        if isinstance(result, AdapterResult):
            result.elapsed_ms = result.elapsed_ms or int((time.perf_counter() - started) * 1000)
            result.trace_id = result.trace_id or get_trace_id()
        return result

    def __repr__(self) -> str:  # noqa: D105
        return f"<{type(self).__name__} {getattr(self.platform, 'value', '')}/{getattr(self.mode, 'value', '')}>"
