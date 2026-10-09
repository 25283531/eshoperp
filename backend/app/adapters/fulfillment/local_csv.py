"""LocalCsvAdapter —— 本地兜底履约适配器（★ MVP 默认，`fulfillment.active_adapter=local_csv`）。

定位（ARCH §11.2 N3）：零依赖、无需第三方凭证即可跑通全链路。

================================================================================
★ ★ ★ 本适配器的能力边界（用户决策 ③：1688 采购全部委托给妙手 / 逸淘）★ ★ ★
================================================================================
用户已明确：**1688 的真实下单、密文面单获取、收货地址解密，全部交给妙手 / 逸淘。**
LocalCsvAdapter **禁止**尝试任何"真实 1688 下单 / 密文面单 / 地址解密"行为 ——
这不是"暂时没实现"，而是**永久性的能力边界**，理由如下：

    * 真实下单需要 1688 开放平台资质 + 下单签名，本地兜底不持有任何第三方凭证；
    * 密文面单 / 地址解密涉及平台数据合规，本地实现等同于自建一套解密链路，风险与收益不成比例；
    * 越界实现会让"本地兜底"变成"半成品第三方"，既跑不通又不报错，是最难排查的失效形态。

因此本适配器的能力矩阵被**刻意固定**为：

    fetch_orders            DEGRADED   从 `data/fulfillment/orders_import.csv` 导入订单
    match_sku               SUPPORTED  ★ 查本地 sku_mapping（权威源始终在自研侧，硬过滤 is_mock=0）
    place_purchase_order    DEGRADED   ★ 导出采购清单 CSV 到 `data/exports/`
                                        → 采购单状态固定为 **manual_pending（待人工下单）**
    fetch_tracking_no       DEGRADED   从 `data/fulfillment/tracking_import.csv` 读取人工录入的单号
    write_back_tracking     UNSUPPORTED ★ 红线 R2：转 ListingAdapter 由 ERP 自主回填
    submit_refund           UNSUPPORTED ★ 退款是 1688 侧能力 → 请用妙手 / 逸淘
    get_return_address      UNSUPPORTED ★ 退货地址同样是 1688 侧能力（含解密）→ 请用妙手 / 逸淘
    push_inventory_change   SUPPORTED  本地即源头，无需推送（no-op 成功）

⚠️ 后续维护者注意（三条，违反即静默失效）：
   1. **不要**把 submit_refund / get_return_address "顺手实现成读本地 JSON 文件"。
      那会让运营以为退款已提交、地址已获取，实际 1688 侧什么都没发生。
      要就明确 UNSUPPORTED，并在 message 里给出去妙手 / 逸淘的指引。
   2. **不要**让 place_purchase_order 返回 `status='placed'` 之类的"看起来成功了"的状态。
      本地兜底从未真正下过单，一律 `manual_pending`。
   3. **不要**实现 write_back_tracking —— 那是写店铺，属红线 R2，只能由 ListingAdapter 做。
================================================================================
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.adapters.fulfillment.base import (
    FetchOrdersRequest,
    FulfillmentAdapter,
    InventoryChangeEvent,
    MatchSkuRequest,
    MatchSkuResult,
    OrderPayload,
    PurchaseOrderPayload,
    PurchaseRequest,
    RefundRequest,
    RefundResult,
    ReturnAddress,
    ReturnAddressQuery,
    TrackingPayload,
    TrackingQuery,
    WriteBackRequest,
    WriteBackResult,
)
from app.adapters.fulfillment.manifest import (
    AdapterManifest,
    AdapterResult,
    Capability,
    CapabilityLevel,
    CapabilitySpec,
    HealthStatus,
)
from app.adapters.fulfillment.registry import register
from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.enums import AdapterName, MappingStatus, PurchaseStatus
from app.models.mapping import SkuMapping
from app.utils.csvio import (
    ORDER_IMPORT_CSV_HEADERS,
    PURCHASE_CSV_HEADERS,
    append_csv_rows,
    read_csv_dicts,
    yuan_str_to_cents,
)
from app.utils.kit import ensure_dir, iso_utc, utc_now

logger = get_logger(__name__)

__all__ = ["LocalCsvAdapter", "LOCAL_CSV_FILES"]

LOCAL_CSV_FILES = {
    "orders_import": "orders_import.csv",
    "purchase_list": "purchase_list.csv",
    "tracking_import": "tracking_import.csv",
    "refund_todo": "refund_todo.csv",
    "return_address": "return_address.json",
}


@register(AdapterName.LOCAL_CSV.value, display_name="本地兜底")
class LocalCsvAdapter(FulfillmentAdapter):
    """本地兜底履约适配器：不依赖第三方，CSV 导入导出 + ERP 自主记录采购单 / 物流单号。

    ★ 红线 R2：本类**没有**也不允许有 `offline` / `update_stock_price` 方法。
      `write_back_tracking` 返回 UNSUPPORTED，由 ERP 侧通过 ListingAdapter 自主回填。
    """

    adapter_name = AdapterName.LOCAL_CSV.value
    display_name = "本地兜底"

    def __init__(self, config: Any = None, credential: Any = None, http: Any = None, session: Any = None) -> None:
        """初始化本地兜底适配器。"""
        super().__init__(config=config, credential=credential, http=http, session=session)
        self.data_dir = ensure_dir(get_settings().fulfillment_dir)
        # ★ 采购清单导出目录（用户决策 ③ 指定 data/exports/）
        self.exports_dir = ensure_dir(get_settings().exports_dir)
        self._seq = 0

    # ---------------- 能力声明 ----------------

    def build_manifest(self) -> AdapterManifest:
        """声明本地兜底的能力矩阵。"""
        return AdapterManifest(
            adapter_name=self.adapter_name,
            display_name=self.display_name,
            version="0.1.0",
            capabilities={
                Capability.FETCH_ORDERS: CapabilitySpec(
                    name=Capability.FETCH_ORDERS,
                    level=CapabilityLevel.DEGRADED,
                    fallback="manual",
                    note="从 data/fulfillment/orders_import.csv 导入订单（人工从店铺后台导出后放入）",
                ),
                Capability.MATCH_SKU: CapabilitySpec(
                    name=Capability.MATCH_SKU,
                    level=CapabilityLevel.SUPPORTED,
                    fallback=None,
                    note="查本地 sku_mapping（★ 权威源，硬过滤 is_mock=0 与 is_deleted=0）",
                ),
                Capability.PLACE_PURCHASE_ORDER: CapabilitySpec(
                    name=Capability.PLACE_PURCHASE_ORDER,
                    level=CapabilityLevel.DEGRADED,
                    fallback="csv_export",
                    note="★ 导出采购清单 CSV 到 data/exports/，采购单状态=manual_pending；"
                         "真实 1688 下单委托妙手 / 逸淘（用户决策 ③）",
                ),
                Capability.FETCH_TRACKING_NO: CapabilitySpec(
                    name=Capability.FETCH_TRACKING_NO,
                    level=CapabilityLevel.DEGRADED,
                    fallback="manual",
                    note="从 data/fulfillment/tracking_import.csv 读取人工录入的物流单号",
                ),
                Capability.WRITE_BACK_TRACKING: CapabilitySpec(
                    name=Capability.WRITE_BACK_TRACKING,
                    level=CapabilityLevel.UNSUPPORTED,
                    fallback="manual",
                    note="★ 不支持：转由 ListingAdapter 侧 ERP 自主回填（红线 R2，第三方不得写店铺）",
                ),
                # ★ 用户决策 ③：退款提交与退货地址（含解密）是 1688 侧能力，
                #   本地兜底**明确不支持**，一律转妙手 / 逸淘。
                Capability.SUBMIT_REFUND: CapabilitySpec(
                    name=Capability.SUBMIT_REFUND,
                    level=CapabilityLevel.UNSUPPORTED,
                    fallback="miaoshou_or_yitao",
                    note="★ 不支持：1688 退款提交已全部委托妙手 / 逸淘，本地兜底不碰（用户决策 ③）",
                ),
                Capability.GET_RETURN_ADDRESS: CapabilitySpec(
                    name=Capability.GET_RETURN_ADDRESS,
                    level=CapabilityLevel.UNSUPPORTED,
                    fallback="miaoshou_or_yitao",
                    note="★ 不支持：退货地址获取含平台地址解密，已全部委托妙手 / 逸淘（用户决策 ③）",
                ),
                Capability.PUSH_INVENTORY_CHANGE: CapabilitySpec(
                    name=Capability.PUSH_INVENTORY_CHANGE,
                    level=CapabilityLevel.SUPPORTED,
                    fallback=None,
                    note="本地即源头，无需推送（no-op）",
                ),
            },
            required_scopes=[],  # 本地兜底无需任何第三方授权
            config_schema={
                "type": "object",
                "properties": {
                    "data_dir": {"type": "string", "title": "本地履约数据目录"},
                },
            },
        )

    # ---------------- 8 项能力 ----------------

    async def fetch_orders(self, req: FetchOrdersRequest) -> AdapterResult[list[OrderPayload]]:
        """从 CSV 导入订单（DEGRADED）。"""
        path = self.data_dir / LOCAL_CSV_FILES["orders_import"]
        try:
            rows = read_csv_dicts(path, required_headers=list(ORDER_IMPORT_CSV_HEADERS))
        except ValueError as exc:
            return AdapterResult.retryable("fetch_orders", str(exc))
        except Exception as exc:  # noqa: BLE001
            return AdapterResult.fatal("fetch_orders", exc)

        orders: list[OrderPayload] = []
        for row in rows:
            if req.shop_ids and row.get("shop_id") not in req.shop_ids:
                continue
            paid_at = row.get("paid_at") or ""
            if req.updated_from and paid_at and paid_at < req.updated_from:
                continue
            orders.append(
                OrderPayload(
                    platform=row.get("platform", ""),
                    shop_id=row.get("shop_id", ""),
                    platform_order_no=row.get("platform_order_no", ""),
                    buyer_info_enc=row.get("receiver_name", ""),
                    receiver_addr_enc=row.get("receiver_address", ""),
                    total_amount_cents=yuan_str_to_cents(row.get("total_amount")),
                    paid_at=paid_at or iso_utc(utc_now()),
                    items=[
                        {
                            "shop_item_id": row.get("shop_item_id", ""),
                            "shop_sku_code": row.get("shop_sku_code", ""),
                            "quantity": int(row.get("quantity") or 1),
                            "price_cents": yuan_str_to_cents(row.get("price")),
                        }
                    ],
                    raw=dict(row),
                )
            )

        # 分页（内存分页，满足接口契约）
        start = max((req.page - 1) * req.page_size, 0)
        page_items = orders[start : start + req.page_size]
        return AdapterResult.degraded(
            page_items,
            message=f"本地兜底：已从 {path.name} 导入 {len(orders)} 条订单（人工导出后放入该目录）",
        )

    async def match_sku(self, req: MatchSkuRequest) -> AdapterResult[MatchSkuResult]:
        """★ 权威匹配：查本地 sku_mapping，硬过滤 `is_mock=0` 与 `is_deleted=0`。"""
        if self.session is None:
            return AdapterResult.fatal("match_sku", ValueError("缺少数据库会话，无法查本地映射"))

        try:
            stmt = (
                select(SkuMapping)
                .where(
                    SkuMapping.platform == req.platform,
                    SkuMapping.shop_id == req.shop_id,
                    SkuMapping.shop_sku_code == req.shop_sku_code,
                    SkuMapping.is_deleted.is_(False),
                    SkuMapping.is_mock.is_(False),  # ★ Mock 映射不参与真实履约
                )
                .order_by(SkuMapping.updated_at.desc())
            )
            if req.shop_item_id:
                stmt = stmt.where(SkuMapping.shop_item_id == req.shop_item_id)
            mapping = (await self.session.execute(stmt)).scalars().first()
        except Exception as exc:  # noqa: BLE001
            return AdapterResult.fatal("match_sku", exc)

        if mapping is None:
            return AdapterResult.success(
                MatchSkuResult(matched=False, reason="本地未找到有效映射（可能缺失或为 Mock/已软删除）"),
                message="未匹配",
            )

        if mapping.status != MappingStatus.VALID.value:
            return AdapterResult.success(
                MatchSkuResult(
                    matched=False,
                    source_product_1688_id=mapping.source_product_1688_id,
                    source_sku_code_1688=mapping.source_sku_code_1688,
                    purchase_cost_cents=mapping.purchase_cost_cents,
                    mapping_status=mapping.status,
                    reason=f"映射状态为 {mapping.status}，订单需挂起待确认",
                ),
                message=f"映射状态 {mapping.status}，订单挂起",
            )

        return AdapterResult.success(
            MatchSkuResult(
                matched=True,
                source_product_1688_id=mapping.source_product_1688_id,
                source_sku_code_1688=mapping.source_sku_code_1688,
                purchase_cost_cents=mapping.purchase_cost_cents,
                mapping_status=mapping.status,
            ),
            message="本地映射命中",
        )

    async def place_purchase_order(self, req: PurchaseRequest) -> AdapterResult[PurchaseOrderPayload]:
        """★ 导出采购清单 CSV 到 `data/exports/`，采购单状态固定 `manual_pending`（DEGRADED）。

        边界（用户决策 ③）：本地兜底**不做真实 1688 下单**。
        返回 `status='manual_pending'` 而不是 `'placed'` —— 本地从未真正下过单，
        返回"看起来成功了"的状态就是静默失效。
        """
        self._seq += 1
        pseudo_no = f"LOCAL-{req.order_id}-{int(time.time())}-{self._seq}"
        row = {
            "order_id": req.order_id,
            "platform_order_no": req.platform_order_no,
            "source_product_1688_id": req.source_product_1688_id,
            "source_sku_code_1688": req.source_sku_code_1688,
            "quantity": req.quantity,
            "receiver_name": "",  # 密文不落地，人工从店铺后台查看
            "receiver_phone": "",
            "receiver_address": "",
            "remark": req.remark or "ERP 本地兜底：请人工到 1688 下单后回填采购单号",
        }
        try:
            # ★ 采购清单导出到 data/exports/（用户决策 ③ 指定的落盘位置）
            path = append_csv_rows(
                [row],
                self.exports_dir / LOCAL_CSV_FILES["purchase_list"],
                headers=list(PURCHASE_CSV_HEADERS),
            )
        except Exception as exc:  # noqa: BLE001
            return AdapterResult.fatal("place_purchase_order", exc)

        logger.info("local_purchase_list_written", path=path, platform_order_no=req.platform_order_no)
        return AdapterResult.degraded(
            PurchaseOrderPayload(
                purchase_order_no=pseudo_no,
                amount_cents=0,  # 成本未知，由上层从映射快照补齐
                status=PurchaseStatus.MANUAL_PENDING.value,  # ★ 待人工下单，绝不返回 placed
                raw={"csv_path": path, "need_manual_action": True},
            ),
            message=f"本地兜底：已导出采购清单 {Path(path).name} 到 data/exports/，请人工到 1688 下单后回填单号",
        )

    async def fetch_tracking_no(self, req: TrackingQuery) -> AdapterResult[TrackingPayload]:
        """从人工录入的 CSV 读取物流单号（DEGRADED）。"""
        path = self.data_dir / LOCAL_CSV_FILES["tracking_import"]
        try:
            rows = read_csv_dicts(path)
        except Exception as exc:  # noqa: BLE001
            return AdapterResult.fatal("fetch_tracking_no", exc)

        for row in rows:
            if req.purchase_order_no and row.get("purchase_order_no") == req.purchase_order_no:
                return AdapterResult.degraded(
                    TrackingPayload(
                        logistics_company=row.get("logistics_company", ""),
                        tracking_no=row.get("tracking_no", ""),
                        shipped_at=row.get("shipped_at") or iso_utc(utc_now()),
                    ),
                    message="本地兜底：物流单号来自人工录入的 CSV",
                )
            if req.order_id and row.get("order_id") == str(req.order_id):
                return AdapterResult.degraded(
                    TrackingPayload(
                        logistics_company=row.get("logistics_company", ""),
                        tracking_no=row.get("tracking_no", ""),
                        shipped_at=row.get("shipped_at") or iso_utc(utc_now()),
                    ),
                    message="本地兜底：物流单号来自人工录入的 CSV",
                )

        return AdapterResult.unsupported(
            Capability.FETCH_TRACKING_NO.value,
            fallback="manual",
            message=f"未在 {LOCAL_CSV_FILES['tracking_import']} 中找到单号，请人工录入",
        )

    async def write_back_tracking(self, req: WriteBackRequest) -> AdapterResult[WriteBackResult]:
        """★ UNSUPPORTED：本地兜底不写店铺，转由 ERP + ListingAdapter 自主回填（红线 R2）。"""
        return AdapterResult.unsupported(
            Capability.WRITE_BACK_TRACKING.value,
            fallback="manual",
            message="本地兜底不支持回填店铺：请由 ERP 通过 ListingAdapter 自主回填（第三方不得写店铺商品）",
        )

    async def submit_refund(self, req: RefundRequest) -> AdapterResult[RefundResult]:
        """★ UNSUPPORTED：1688 退款提交已全部委托妙手 / 逸淘（用户决策 ③）。

        不做"生成退款待办 CSV"这种降级实现 —— 它会让运营以为退款已提交，
        实际 1688 侧什么都没发生（静默失效）。明确 UNSUPPORTED + 给出替代路径。
        """
        return AdapterResult.unsupported(
            Capability.SUBMIT_REFUND.value,
            fallback="miaoshou_or_yitao",
            message="本地兜底不支持提交 1688 退款：请将履约适配器切换到妙手 / 逸淘后重试",
        )

    async def get_return_address(self, req: ReturnAddressQuery) -> AdapterResult[ReturnAddress]:
        """★ UNSUPPORTED：退货地址获取含平台地址解密，已委托妙手 / 逸淘（用户决策 ③）。"""
        return AdapterResult.unsupported(
            Capability.GET_RETURN_ADDRESS.value,
            fallback="miaoshou_or_yitao",
            message="本地兜底不支持获取 1688 退货地址（含地址解密）：请切换到妙手 / 逸淘后重试",
        )

    async def push_inventory_change(self, req: InventoryChangeEvent) -> AdapterResult[dict]:
        """本地即源头：无需推送，no-op 成功。"""
        return AdapterResult.success(
            {
                "source_sku_code_1688": req.source_sku_code_1688,
                "change_type": req.change_type,
                "pushed": False,
                "reason": "本地兜底即库存与价格的源头，无需向第三方推送",
            },
            message="本地即源头，无需推送",
        )

    # ---------------- 健康检查 ----------------

    async def health_check(self) -> AdapterResult[HealthStatus]:
        """本地兜底健康检查：履约数据目录可读写即为 healthy。"""
        started = time.perf_counter()
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            probe = self.data_dir / ".healthcheck"
            probe.write_text(iso_utc(utc_now()), encoding="utf-8")
            probe.unlink(missing_ok=True)
            healthy = True
            message = f"本地兜底可用，数据目录：{self.data_dir}"
        except Exception as exc:  # noqa: BLE001
            healthy = False
            message = f"履约数据目录不可用：{exc}"
        return AdapterResult.success(
            HealthStatus.build(
                healthy=healthy,
                status="healthy" if healthy else "down",
                message=message,
                started=started,
            )
        )

    def csv_paths(self) -> dict[str, str]:
        """返回本地兜底使用的全部 CSV 路径（供后台展示与下载）。"""
        return {key: str(self.data_dir / name) for key, name in LOCAL_CSV_FILES.items()}
