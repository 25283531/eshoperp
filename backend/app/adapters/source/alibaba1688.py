"""1688 货源采集适配器（§3 目录结构 / §4.4.1）。

职责：
    1. 采集 1688 商品详情（HTTP 部分为可配置 **骨架 + TODO**，需 AppKey 与配额）；
    2. **SKU 规格树笛卡尔展开** —— 纯函数，可离线使用；
    3. **规格指纹计算** —— 与 `SkuMapping.spec_signature` 同源（utils.kit.spec_signature）。

★ 采集配额与接口字段未实测（ARCH §11.1 Q4），真实调用前请先填凭证并做小批量验证。
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.utils.kit import content_hash_bytes, iso_utc, spec_signature, utc_now

logger = get_logger(__name__)

__all__ = [
    "Alibaba1688Adapter",
    "CollectedProduct",
    "CollectedSku",
    "expand_spec_tree",
    "normalize_spec_json",
]

# TODO: 需实测确认 —— 1688 开放平台商品详情接口路径与响应结构
DEFAULT_ENDPOINT = "https://openapi.1688.com/openapi/param2/1/com.alibaba.product/alibaba.product.get"


@dataclass
class CollectedSku:
    """采集到的单个货源 SKU。"""

    sku_code_1688: str
    spec_json: dict[str, str] = field(default_factory=dict)
    spec_signature: str = ""
    cost_price_cents: int = 0
    stock_qty: int = 0
    status: str = "on_sale"

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "sku_code_1688": self.sku_code_1688,
            "spec_json": dict(self.spec_json),
            "spec_signature": self.spec_signature,
            "cost_price_cents": self.cost_price_cents,
            "stock_qty": self.stock_qty,
            "status": self.status,
        }


@dataclass
class CollectedProduct:
    """采集到的货源商品（含 SKU 列表）。"""

    product_1688_id: str
    title: str = ""
    category_path: str = ""
    supplier_1688_id: str = ""
    cost_price_cents: int = 0
    origin_url: str = ""
    main_image_url: str = ""
    params_json: dict[str, Any] = field(default_factory=dict)
    skus: list[CollectedSku] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    collected_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化（不含 raw，避免日志膨胀）。"""
        return {
            "product_1688_id": self.product_1688_id,
            "title": self.title,
            "category_path": self.category_path,
            "supplier_1688_id": self.supplier_1688_id,
            "cost_price_cents": self.cost_price_cents,
            "origin_url": self.origin_url,
            "main_image_url": self.main_image_url,
            "params_json": dict(self.params_json),
            "skus": [sku.to_dict() for sku in self.skus],
            "collected_at": self.collected_at or iso_utc(utc_now()),
        }


def normalize_spec_json(spec: dict[str, Any] | None) -> dict[str, str]:
    """规范化规格名值对：去空白、去空值、按键排序。"""
    if not spec:
        return {}
    normalized: dict[str, str] = {}
    for key, value in spec.items():
        k = str(key).strip()
        v = "" if value is None else str(value).strip()
        if k:
            normalized[k] = v
    return dict(sorted(normalized.items(), key=lambda kv: kv[0]))


def expand_spec_tree(spec_tree: dict[str, list[Any]] | None) -> list[dict[str, str]]:
    """★ SKU 规格树笛卡尔展开。

    输入示例：
        {"颜色": ["红", "蓝"], "尺码": ["S", "XL"]}

    输出：
        [{"颜色": "红", "尺码": "S"}, {"颜色": "红", "尺码": "XL"},
         {"颜色": "蓝", "尺码": "S"}, {"颜色": "蓝", "尺码": "XL"}]

    Args:
        spec_tree: `{规格名: [规格值, ...]}`。

    Returns:
        笛卡尔积后的规格组合列表（顺序稳定：按规格名排序后逐级展开）。
    """
    if not spec_tree:
        return []
    cleaned: dict[str, list[str]] = {}
    for key, values in spec_tree.items():
        name = str(key).strip()
        if not name:
            continue
        if not isinstance(values, (list, tuple)):
            values = [values]
        cleaned[name] = [str(v).strip() for v in values if str(v).strip()]

    if not cleaned:
        return []

    names = sorted(cleaned.keys())
    combos: list[dict[str, str]] = []
    for values in itertools.product(*(cleaned[name] for name in names)):
        combos.append({name: value for name, value in zip(names, values)})
    return combos


def sku_code_from_spec(product_1688_id: str, spec_json: dict[str, str]) -> str:
    """由商品 ID 与规格生成稳定的 1688 SKU 编码（无官方编码时的兜底）。"""
    normalized = normalize_spec_json(spec_json)
    if not normalized:
        return f"{product_1688_id}-DEFAULT"
    signature = spec_signature(normalized)
    return f"{product_1688_id}-{signature[:12].upper()}"


class Alibaba1688Adapter:
    """1688 商品详情采集适配器。

    ★ 采集为只读能力（`order.read` 范畴之外但同样不涉及商品编辑），
      不持有任何写店铺的能力（红线 R2）。
    """

    adapter_name = "alibaba1688"

    def __init__(
        self,
        *,
        app_key: str = "",
        app_secret: str = "",
        access_token: str = "",
        endpoint: str = DEFAULT_ENDPOINT,
        timeout_sec: float = 20.0,
        max_retry: int = 3,
    ) -> None:
        """初始化 1688 适配器（凭证为空时只能使用离线解析能力）。"""
        settings = get_settings()
        self.app_key = app_key
        self.app_secret = app_secret
        self.access_token = access_token
        self.endpoint = endpoint
        self.timeout_sec = timeout_sec or settings.http_timeout_sec
        self.max_retry = max(int(max_retry), 0)

    @property
    def configured(self) -> bool:
        """是否已配置凭证（未配置时 `collect()` 返回 FATAL，不抛异常）。"""
        return bool(self.app_key and self.access_token)

    # ---------------- 采集 ----------------

    async def collect(self, product_1688_id: str) -> tuple[bool, CollectedProduct | None, str]:
        """采集商品详情。

        Returns:
            `(ok, product, message)`。★ 绝不抛异常，失败返回 `(False, None, 原因)`。
        """
        if not product_1688_id:
            return False, None, "缺少 1688 商品 ID"
        if not self.configured:
            return False, None, "未配置 1688 AppKey / AccessToken（TODO: 需在系统设置 → 凭证中填写）"

        payload = {
            # TODO: 需实测确认 —— 1688 开放接口的参数名与签名方式
            "productID": product_1688_id,
            "webSite": "1688",
        }
        headers = {"Authorization": f"Bearer {self.access_token}"} if self.access_token else {}

        last_error = ""
        for attempt in range(self.max_retry + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
                    response = await client.post(self.endpoint, json=payload, headers=headers)
                    if response.status_code >= 500:
                        last_error = f"HTTP {response.status_code}"
                        if attempt < self.max_retry:
                            await _sleep_backoff(attempt)
                            continue
                        return False, None, last_error
                    if response.status_code >= 400:
                        return False, None, f"HTTP {response.status_code}（可能凭证无效或配额超限）"
                    raw = response.json()
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.max_retry:
                    await _sleep_backoff(attempt)
                    continue
                return False, None, last_error

            product = self.parse_product(raw, fallback_id=product_1688_id)
            return True, product, f"采集成功，SKU {len(product.skus)} 个"

        return False, None, last_error or "采集失败"

    async def collect_batch(self, product_ids: list[str], *, delay_sec: float = 0.5) -> list[tuple[str, CollectedProduct | None, str]]:
        """批量采集（内置简单限流，防配额超限，ARCH §11.1 Q4）。"""
        results: list[tuple[str, CollectedProduct | None, str]] = []
        for pid in product_ids:
            ok, product, message = await self.collect(pid)
            results.append((pid, product if ok else None, message))
            if delay_sec > 0:
                await _sleep_backoff(0, base=delay_sec)
        return results

    # ---------------- 解析（纯函数，可离线使用）----------------

    def parse_product(self, raw: dict[str, Any], *, fallback_id: str = "") -> CollectedProduct:
        """★ 防御性解析 1688 响应：字段缺失不崩，缺什么补默认。"""
        product = raw.get("product") if isinstance(raw.get("product"), dict) else raw

        product_id = str(
            product.get("productID") or product.get("product_id") or product.get("offerId") or fallback_id or ""
        )
        title = str(product.get("subject") or product.get("title") or "")
        category_path = str(product.get("categoryName") or product.get("category_path") or "")

        # 价格（1688 多为元字符串，转换为分）
        price_raw = product.get("price") or product.get("salePrice") or product.get("priceInfo")
        cost_cents = _to_cents(price_raw)

        image_url = str(product.get("imageUrl") or product.get("main_image_url") or "")
        supplier_id = str(product.get("supplierUserId") or product.get("memberId") or "")

        skus = self.parse_skus(product, fallback_product_id=product_id)
        if not skus:
            # 无 SKU 结构时构造一个默认 SKU，保证映射链路可用
            skus = [
                CollectedSku(
                    sku_code_1688=f"{product_id}-DEFAULT" if product_id else "DEFAULT",
                    spec_json={},
                    spec_signature="",
                    cost_price_cents=cost_cents,
                    stock_qty=int(product.get("amountOnSale", 0) or 0),
                )
            ]

        return CollectedProduct(
            product_1688_id=product_id,
            title=title,
            category_path=category_path,
            supplier_1688_id=supplier_id,
            cost_price_cents=cost_cents,
            origin_url=f"https://detail.1688.com/offer/{product_id}.html" if product_id else "",
            main_image_url=image_url,
            params_json=_extract_params(product),
            skus=skus,
            raw=raw if isinstance(raw, dict) else {},
            collected_at=iso_utc(utc_now()),
        )

    def parse_skus(self, product: dict[str, Any], *, fallback_product_id: str = "") -> list[CollectedSku]:
        """解析 SKU 列表：支持显式 `skuList` 与 `specTree` 笛卡尔展开两种结构。"""
        skus: list[CollectedSku] = []

        raw_skus = product.get("skuList") or product.get("skus") or product.get("skuInfos")
        if isinstance(raw_skus, list):
            for item in raw_skus:
                if not isinstance(item, dict):
                    continue
                spec = _extract_spec(item)
                code = str(
                    item.get("skuCode")
                    or item.get("sku_code")
                    or item.get("specId")
                    or sku_code_from_spec(fallback_product_id, spec)
                )
                skus.append(
                    CollectedSku(
                        sku_code_1688=code,
                        spec_json=normalize_spec_json(spec),
                        spec_signature=spec_signature(normalize_spec_json(spec)),
                        cost_price_cents=_to_cents(item.get("price") or item.get("salePrice")),
                        stock_qty=int(item.get("amountOnSale", item.get("stock", 0)) or 0),
                        status=_status_from_stock(item.get("amountOnSale", item.get("stock"))),
                    )
                )

        if not skus:
            spec_tree = product.get("specTree") or product.get("spec_tree") or product.get("specs")
            if isinstance(spec_tree, dict):
                for spec in expand_spec_tree(spec_tree):
                    skus.append(
                        CollectedSku(
                            sku_code_1688=sku_code_from_spec(fallback_product_id, spec),
                            spec_json=normalize_spec_json(spec),
                            spec_signature=spec_signature(normalize_spec_json(spec)),
                            cost_price_cents=_to_cents(product.get("price")),
                            stock_qty=int(product.get("amountOnSale", 0) or 0),
                        )
                    )
        return skus

    # ---------------- 辅助：本地下载与哈希 ----------------

    @staticmethod
    async def download_image(url: str, dest_path: str) -> tuple[bool, str, str]:
        """下载原图到本地；返回 `(ok, content_hash, message)`。"""
        if not url:
            return False, "", "图片 URL 为空"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url)
                response.raise_for_status()
                data = response.content
        except Exception as exc:  # noqa: BLE001
            return False, "", f"下载失败：{exc}"
        from pathlib import Path

        path = Path(dest_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return True, content_hash_bytes(data), "下载成功"

    async def health_check(self) -> dict[str, Any]:
        """连通性自检（未配置凭证时直接报 unknown，不发请求）。"""
        started = time.perf_counter()
        if not self.configured:
            return {
                "adapter": self.adapter_name,
                "healthy": False,
                "status": "unknown",
                "message": "未配置 1688 AppKey / AccessToken",
                "latency_ms": 0,
            }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
                response = await client.get(self.endpoint.rsplit("/", 1)[0] or self.endpoint)
                healthy = response.status_code < 500
                message = f"HTTP {response.status_code}"
        except Exception as exc:  # noqa: BLE001
            healthy = False
            message = f"连通性失败：{exc}"
        return {
            "adapter": self.adapter_name,
            "healthy": healthy,
            "status": "healthy" if healthy else "down",
            "message": message,
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }


async def _sleep_backoff(attempt: int, base: float = 0.5) -> None:
    """指数退避等待。"""
    import asyncio

    await asyncio.sleep(base * (2**attempt))


def _to_cents(value: Any) -> int:
    """价格转分：支持 `12.50` / `"12.50"` / `{"price": 12.5}` / 已是分的整数。"""
    if value is None:
        return 0
    if isinstance(value, dict):
        for key in ("price", "amount", "value"):
            if key in value:
                return _to_cents(value[key])
        return 0
    try:
        return int(round(float(str(value).strip()) * 100))
    except (TypeError, ValueError):
        return 0


def _extract_spec(item: dict[str, Any]) -> dict[str, str]:
    """从 SKU 项中提取规格名值对（★ 防御性：多种结构兼容）。"""
    spec: dict[str, str] = {}
    raw_spec = item.get("specJson") or item.get("spec_json") or item.get("attributes") or item.get("specAttrs")
    if isinstance(raw_spec, dict):
        for key, value in raw_spec.items():
            spec[str(key)] = "" if value is None else str(value)
    elif isinstance(raw_spec, list):
        for entry in raw_spec:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("attributeName") or "")
            value = str(entry.get("value") or entry.get("attributeValue") or "")
            if name:
                spec[name] = value
    return normalize_spec_json(spec)


def _status_from_stock(stock: Any) -> str:
    """库存 → 状态。"""
    try:
        quantity = int(stock or 0)
    except (TypeError, ValueError):
        return "on_sale"
    if quantity <= 0:
        return "out_of_stock"
    return "on_sale"


def _extract_params(product: dict[str, Any]) -> dict[str, Any]:
    """提取商品参数表（防御性）。"""
    raw = product.get("attributes") or product.get("params") or product.get("productFeatureList")
    if isinstance(raw, dict):
        return {str(k): ("" if v is None else str(v)) for k, v in raw.items()}
    if isinstance(raw, list):
        params: dict[str, Any] = {}
        for entry in raw:
            if isinstance(entry, dict):
                name = str(entry.get("name") or entry.get("attributeName") or "")
                value = entry.get("value") or entry.get("attributeValue") or ""
                if name:
                    params[name] = "" if value is None else str(value)
        return params
    return {}
