"""演示数据种子脚本（T-A08）。

    python scripts/seed.py            # 幂等写入演示数据
    python scripts/seed.py --reset    # 先清空业务表再写入

用途：前端联调 / 冒烟脚本需要一个「有数据」的库。
★ 幂等：以 `product_1688_id` 等业务唯一键做 upsert，重复执行不会产生脏数据。
★ 安全：默认**只写** `data/erp.db`；可用 `DATABASE_URL` 环境变量切换。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select  # noqa: E402

from app.core.database import DatabaseSession, dispose_engine, get_engine, init_db  # noqa: E402
from app.core.logging import get_logger, setup_logging  # noqa: E402
from app.models.asset import Asset  # noqa: E402
from app.models.base import Base  # noqa: E402
from app.models.enums import (
    AssetOrigin,
    OrderFulfillmentStatus,
    AssetType,
    MappingSource,
    MappingStatus,
    PurchaseStatus,
)  # noqa: E402
from app.models.inventory import InventorySnapshot, PriceSnapshot  # noqa: E402
from app.models.listing import ListingProduct, ListingSku  # noqa: E402
from app.models.mapping import SkuMapping  # noqa: E402
from app.models.order import Order, OrderItem, PurchaseOrder  # noqa: E402
from app.models.source import SourceProduct, SourceSku, Supplier  # noqa: E402
from app.services.mapping_service import MappingService  # noqa: E402
from app.utils.kit import utc_now  # noqa: E402

logger = get_logger("scripts.seed")

PRODUCT_ID = "1688-SEED-1001"
SKU_CODE = "SEED-RED-XL"
PLATFORM = "taobao"
SHOP_ID = "shop-seed-001"


async def _reset(session: object) -> None:
    """清空业务表（保持 schema 不变）。"""
    for model in (
        OrderItem,
        PurchaseOrder,
        Order,
        ListingSku,
        ListingProduct,
        SkuMapping,
        PriceSnapshot,
        InventorySnapshot,
        Asset,
        SourceSku,
        SourceProduct,
        Supplier,
    ):
        await session.execute(model.__table__.delete())
    await session.commit()
    logger.info("seed_reset_done")


async def _seed(session: object) -> dict[str, int]:
    """写入演示数据，返回各类对象计数。"""
    # ---------- 供应商 ----------
    supplier = (
        await session.execute(select(Supplier).where(Supplier.supplier_1688_id == "SUP-SEED-001"))
    ).scalars().first()
    if supplier is None:
        supplier = Supplier(
            supplier_1688_id="SUP-SEED-001",
            name="演示供应商（杭州）",
            location="浙江杭州",
            lead_time_hours=24,
            moq=2,
            cooperation_score=5,
            status="active",
        )
        session.add(supplier)
        await session.flush()

    # ---------- 货源商品 + SKU ----------
    product = (
        await session.execute(select(SourceProduct).where(SourceProduct.product_1688_id == PRODUCT_ID))
    ).scalars().first()
    if product is None:
        product = SourceProduct(product_1688_id=PRODUCT_ID)
        session.add(product)
    product.title = "演示货源商品·纯棉短袖T恤"
    product.category_path = "女装 > T恤"
    product.supplier_id = int(supplier.id)
    product.cost_price_cents = 1200
    product.origin_url = f"https://detail.1688.com/offer/{PRODUCT_ID}.html"
    product.params_json = {"材质": "纯棉", "季节": "夏季"}
    product.status = "on_sale"
    product.collected_at = utc_now()
    await session.flush()

    sku = (
        await session.execute(
            select(SourceSku).where(
                SourceSku.source_product_id == product.id, SourceSku.sku_code_1688 == SKU_CODE
            )
        )
    ).scalars().first()
    if sku is None:
        sku = SourceSku(source_product_id=int(product.id), sku_code_1688=SKU_CODE)
        session.add(sku)
    sku.spec_json = {"颜色": "红", "尺码": "XL"}
    sku.spec_signature = MappingService._resolve_spec_signature.__name__ and "seed-sig-001"
    sku.cost_price_cents = 1200
    sku.stock_qty = 200
    sku.status = "on_sale"
    sku.last_checked_at = utc_now()
    await session.flush()

    # ---------- 映射 ----------
    mapping = (
        await session.execute(
            select(SkuMapping).where(
                SkuMapping.platform == PLATFORM,
                SkuMapping.shop_id == SHOP_ID,
                SkuMapping.shop_sku_code == SKU_CODE,
            )
        )
    ).scalars().first()
    if mapping is None:
        mapping = SkuMapping(platform=PLATFORM, shop_id=SHOP_ID, shop_sku_code=SKU_CODE)
        session.add(mapping)
    mapping.shop_item_id = "seed-item-001"
    mapping.shop_sku_name = "演示SKU-红-XL"
    mapping.source_product_id = int(product.id)
    mapping.source_sku_id = int(sku.id)
    mapping.source_product_1688_id = PRODUCT_ID
    mapping.source_sku_code_1688 = SKU_CODE
    mapping.source_sku_name = "红/XL"
    mapping.spec_signature = "seed-sig-001"
    mapping.purchase_cost_cents = 1200
    mapping.cost_source = "auto"
    mapping.status = MappingStatus.VALID.value
    mapping.source = MappingSource.MANUAL.value
    await session.flush()

    # ---------- 平台商品（含一个「售价为空」的存量商品，供补填入口演示）----------
    listing = (
        await session.execute(
            select(ListingProduct).where(
                ListingProduct.platform == PLATFORM, ListingProduct.shop_item_id == "seed-item-001"
            )
        )
    ).scalars().first()
    if listing is None:
        listing = ListingProduct(
            platform=PLATFORM, shop_id=SHOP_ID, shop_item_id="seed-item-001"
        )
        session.add(listing)
    listing.source_product_id = int(product.id)
    listing.title = "演示商品·纯棉短袖T恤（红 XL）"
    listing.status = "on_sale"
    listing.is_mock = True
    listing.listing_mode = "manual"
    listing.published_at = utc_now()
    await session.flush()

    listing_sku = (
        await session.execute(
            select(ListingSku).where(
                ListingSku.listing_product_id == listing.id, ListingSku.shop_sku_code == SKU_CODE
            )
        )
    ).scalars().first()
    if listing_sku is None:
        listing_sku = ListingSku(listing_product_id=int(listing.id), shop_sku_code=SKU_CODE)
        session.add(listing_sku)
    listing_sku.spec_json = {"颜色": "红", "尺码": "XL"}
    listing_sku.sale_price_cents = None  # ★ 存量空售价：供 /listing-products?missing_price=true 演示
    listing_sku.status = "on_sale"
    await session.flush()

    # 倒挂检测需要 sku_mapping.listing_sku_id 关联
    mapping.listing_sku_id = int(listing_sku.id)
    await session.flush()

    # ---------- 订单 + 成本快照 ----------
    order = (
        await session.execute(select(Order).where(Order.platform_order_no == "SEED-ORDER-001"))
    ).scalars().first()
    if order is None:
        order = Order(platform=PLATFORM, shop_id=SHOP_ID, platform_order_no="SEED-ORDER-001")
        session.add(order)
    order.total_amount_cents = 2990 * 2
    order.fulfillment_status = OrderFulfillmentStatus.MATCHED.value  # ★ 必须是状态机内的合法值
    order.adapter_name = "local_csv"
    order.match_status = "matched"
    order.paid_at = utc_now()
    order.is_mock = False
    await session.flush()

    item = (
        await session.execute(
            select(OrderItem).where(
                OrderItem.order_id == order.id, OrderItem.shop_sku_code == SKU_CODE
            )
        )
    ).scalars().first()
    if item is None:
        item = OrderItem(order_id=int(order.id), shop_item_id="seed-item-001", shop_sku_code=SKU_CODE)
        session.add(item)
    item.sku_mapping_id = int(mapping.id)
    item.source_sku_id = int(sku.id)
    item.quantity = 2
    item.purchase_cost_cents = 1200  # ★ 快照：下单时点成本，不随货源涨价变化
    item.sale_price_cents = 2990
    item.match_status = "matched"
    await session.flush()

    purchase = (
        await session.execute(
            select(PurchaseOrder).where(PurchaseOrder.order_id == order.id)
        )
    ).scalars().first()
    if purchase is None:
        purchase = PurchaseOrder(order_id=int(order.id))
        session.add(purchase)
    purchase.purchase_order_no = None  # 本地兜底：待人工下单后回填
    purchase.supplier_id = int(supplier.id)
    purchase.amount_cents = 1200 * 2
    purchase.adapter_name = "local_csv"
    purchase.purchase_status = PurchaseStatus.MANUAL_PENDING.value
    purchase.writeback_status = "pending"
    await session.flush()

    # ---------- 素材（占位记录，文件不存在时下载接口返回 404）----------
    asset = (
        await session.execute(select(Asset).where(Asset.content_hash == "seed-hash-001"))
    ).scalars().first()
    if asset is None:
        session.add(
            Asset(
                source_product_id=int(product.id),
                asset_type=AssetType.MAIN_IMAGE.value,
                origin=AssetOrigin.RAW.value,
                storage_path="assets/raw/seed-placeholder.jpg",
                content_hash="seed-hash-001",
                version=1,
                lineage_id="seed-lineage-001",
                is_current=True,
                tags_json=["主图"],
            )
        )
        await session.flush()

    # ---------- 库存 / 价格快照 ----------
    session.add(
        InventorySnapshot(
            source_sku_id=int(sku.id), stock_qty=200, source="erp_poll", collected_at=utc_now()
        )
    )
    session.add(
        PriceSnapshot(
            source_sku_id=int(sku.id),
            cost_price_cents=1200,
            prev_price_cents=1200,
            change_rate=None,
            collected_at=utc_now(),
        )
    )
    await session.flush()
    await session.commit()

    return {
        "suppliers": 1,
        "source_products": 1,
        "source_skus": 1,
        "mappings": 1,
        "listing_products": 1,
        "orders": 1,
        "purchase_orders": 1,
    }


async def main(reset: bool) -> int:
    """入口。"""
    setup_logging(level="INFO", json_logs=False)
    await init_db()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    stats: dict[str, int] = {}
    async with DatabaseSession() as session:
        # ★ 与主程序启动期**同一套**初始化（QA-05）：
        #   三张"主路径上必须有数据"的表（履约适配器 / 平台账号 / 凭证）必须真实落库，
        #   否则凭证页、平台账号授权页、`PUT /adapters/.../config` 全部拿不到目标行。
        from app.services.bootstrap import bootstrap_all, check_required_constraints

        boot = await bootstrap_all(session, commit=True)
        issues = await check_required_constraints(session)
        await session.commit()
        print(f"✅ 基础数据初始化：{boot}")
        if issues:
            print(f"❌ 结构性约束缺失（保护不可用）：{[i['name'] for i in issues]}")
        else:
            print("✅ 结构性约束自检通过（uq_sku_mapping_shop_sku 等全部就位）")

        if reset:
            await _reset(session)
        stats = await _seed(session)

    await dispose_engine()
    logger.info("seed_done", **stats)
    print(f"✅ 演示数据已写入：{stats}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="写入演示数据")
    parser.add_argument("--reset", action="store_true", help="先清空业务表")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.reset)))
