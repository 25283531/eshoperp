"""平台商品服务（§5.5.8）。

================================================================================
★ ★ ★ 红线 R2 落地：下架能力的唯一出口 ★ ★ ★
================================================================================
本模块 `ListingService.offline()` 是全系统**唯一**调用 `ListingAdapter.offline()` 的地方。
`InventoryService` 的自动下架（INV-P0-03）**必须**经本方法，不得自己调适配器。
第三方履约适配器永远拿不到下架能力（它们在 `app/adapters/fulfillment/` 下，
禁止 import `app.adapters.listing.*`，该约束写在 `fulfillment/base.py` 顶部）。

★ 全仓检索自检：`grep -rn "\.offline(" app/ | grep -v listing_service` 应**无结果**。
================================================================================
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from app.adapters.listing.factory import ListingAdapterFactory
from app.core.errors import BusinessError, ErrorCode, NotFoundError, StateConflictError
from app.core.logging import get_logger, get_trace_id
from app.models.enums import (
    AuditActionType,
    AuditObjectType,
    ListingProductStatus,
    ListingSkuStatus,
)
from app.models.listing import ListingProduct, ListingSku
from app.models.mapping import SkuMapping
from app.models.source import SourceProduct
from app.schemas.listing import FillPriceItem
from app.services.audit_service import AuditService
from app.services.mapping_validator import MappingValidator
from app.utils.kit import iso_utc, utc_now

logger = get_logger(__name__)

__all__ = ["ListingService"]


class ListingService:
    """平台商品管理（上架 / 下架 / 售价补填）。"""

    # ==================================================================
    #  查询
    # ==================================================================

    @staticmethod
    async def list_products(
        session: Any,
        *,
        platform: str | None = None,
        shop_id: str | None = None,
        status: str | None = None,
        source_product_id: int | None = None,
        is_mock: bool | None = None,
        missing_price: bool | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[ListingProduct], dict[int, str], dict[int, int], dict[int, int], int]:
        """分页查询平台商品。

        Args:
            missing_price: ★ v1.5 True → 只筛出「存在售价为空 SKU」的在售商品
                           （`GET /listing-products?missing_price=true`，存量补填入口数据源）。

        Returns:
            `(商品列表, {product_id: 货源标题}, {product_id: sku_count},
              {product_id: missing_price_count}, 总数)`。
        """
        stmt = select(ListingProduct).where(ListingProduct.is_deleted.is_(False))
        if platform:
            stmt = stmt.where(ListingProduct.platform == platform)
        if shop_id:
            stmt = stmt.where(ListingProduct.shop_id == shop_id)
        if status:
            stmt = stmt.where(ListingProduct.status == status)
        if source_product_id is not None:
            stmt = stmt.where(ListingProduct.source_product_id == int(source_product_id))
        if is_mock is not None:
            stmt = stmt.where(ListingProduct.is_mock.is_(is_mock))

        if missing_price:
            # ★ 存在「售价为空 / 为 0」SKU 的商品（含无 SKU 记录的商品）
            empty_price_ids = select(ListingSku.listing_product_id).where(
                ListingSku.is_deleted.is_(False),
                (ListingSku.sale_price_cents.is_(None)) | (ListingSku.sale_price_cents <= 0),
            )
            stmt = stmt.where(ListingProduct.id.in_(empty_price_ids))

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await session.execute(count_stmt)).scalar_one() or 0)
        rows = (
            (
                await session.execute(
                    stmt.order_by(ListingProduct.id.desc()).offset((page - 1) * page_size).limit(page_size)
                )
            )
            .scalars()
            .all()
        )

        product_ids = [int(p.id) for p in rows]
        sku_counts: dict[int, int] = {}
        missing_counts: dict[int, int] = {}
        if product_ids:
            counted = (
                await session.execute(
                    select(ListingSku.listing_product_id, func.count(ListingSku.id))
                    .where(ListingSku.listing_product_id.in_(product_ids), ListingSku.is_deleted.is_(False))
                    .group_by(ListingSku.listing_product_id)
                )
            ).all()
            sku_counts = {int(pid): int(cnt) for pid, cnt in counted}

            empty = (
                await session.execute(
                    select(ListingSku.listing_product_id, func.count(ListingSku.id))
                    .where(
                        ListingSku.listing_product_id.in_(product_ids),
                        ListingSku.is_deleted.is_(False),
                        (ListingSku.sale_price_cents.is_(None)) | (ListingSku.sale_price_cents <= 0),
                    )
                    .group_by(ListingSku.listing_product_id)
                )
            ).all()
            missing_counts = {int(pid): int(cnt) for pid, cnt in empty}

        source_ids = sorted({int(p.source_product_id) for p in rows if p.source_product_id is not None})
        titles: dict[int, str] = {}
        if source_ids:
            products = (
                (await session.execute(select(SourceProduct).where(SourceProduct.id.in_(source_ids))))
                .scalars()
                .all()
            )
            titles = {int(p.id): p.title for p in products}

        return list(rows), titles, sku_counts, missing_counts, total

    @staticmethod
    async def get_product(session: Any, product_id: int) -> ListingProduct:
        """取平台商品（404）。"""
        product = (
            await session.execute(
                select(ListingProduct).where(
                    ListingProduct.id == int(product_id), ListingProduct.is_deleted.is_(False)
                )
            )
        ).scalars().first()
        if product is None:
            raise NotFoundError(f"平台商品 {product_id} 不存在")
        return product

    @staticmethod
    async def list_skus(session: Any, product_id: int) -> list[ListingSku]:
        """取平台商品的 SKU 列表。"""
        rows = (
            await session.execute(
                select(ListingSku)
                .where(ListingSku.listing_product_id == int(product_id), ListingSku.is_deleted.is_(False))
                .order_by(ListingSku.id)
            )
        ).scalars().all()
        return list(rows)

    @staticmethod
    async def list_mappings(session: Any, product_id: int) -> list[SkuMapping]:
        """取平台商品关联的映射。"""
        rows = (
            await session.execute(
                select(SkuMapping)
                .where(SkuMapping.listing_product_id == int(product_id), SkuMapping.is_deleted.is_(False))
                .order_by(SkuMapping.id)
            )
        ).scalars().all()
        return list(rows)

    # ==================================================================
    #  ★ 下架（红线 R2：本方法是 `ListingAdapter.offline()` 的唯一调用点）
    # ==================================================================

    @staticmethod
    async def offline(
        session: Any,
        product_id: int,
        *,
        reason: str = "",
        operator: str = "system",
        trigger: str = "manual",
    ) -> ListingProduct:
        """★ 下架平台商品 —— 全系统**唯一**的 `ListingAdapter.offline()` 调用点。

        `InventoryService` 的自动下架、API 的手动下架、批量下架，全部收敛到本方法。

        Args:
            product_id: 平台商品 ID。
            reason: 下架原因（写 `listing_product.offline_reason` 与审计）。
            operator: 操作人。
            trigger: 触发来源（manual / auto_out_of_stock / auto_price_increase）。

        Returns:
            已下架的 `ListingProduct`。

        Raises:
            BusinessError: 7002 下架失败（适配器返回非 OK 且非 DEGRADED）。
        """
        product = await ListingService.get_product(session, product_id)
        if product.status == ListingProductStatus.OFF_SHELF.value:
            raise StateConflictError("商品已处于下架状态")

        adapter = await ListingAdapterFactory.create(
            product.platform, mode=None, session=session
        )
        result = await adapter.invoke("offline", shop_item_ids=[product.shop_item_id], reason=reason)

        # ★ 能力降级不抛异常：DEGRADED（半自动模式人工去后台下架）同样视为成功
        if result.code not in {"OK", "DEGRADED"}:
            raise BusinessError(
                f"下架失败：{result.message}", code=ErrorCode.INVENTORY_OFFLINE_FAILED, http_status=503
            )

        product.status = ListingProductStatus.OFF_SHELF.value
        product.offline_at = utc_now()
        product.offline_reason = reason
        await session.flush()

        for sku in await ListingService.list_skus(session, product_id):
            sku.status = ListingSkuStatus.OFF_SHELF.value
        await session.flush()

        await AuditService.write(
            session,
            action_type=AuditActionType.OFFLINE.value,
            object_type=AuditObjectType.LISTING_PRODUCT.value,
            object_id=product.id,
            operator=operator,
            new_value={"reason": reason, "trigger": trigger, "shop_item_id": product.shop_item_id},
            trace_id=get_trace_id(),
            remark=f"下架商品 {product.shop_item_id}：{reason}",
        )
        await session.flush()
        logger.info("listing_product_offlined", product_id=product.id, reason=reason, trigger=trigger)
        return product

    @staticmethod
    async def batch_offline(
        session: Any,
        product_ids: list[int],
        *,
        reason: str = "",
        operator: str = "system",
    ) -> dict[str, Any]:
        """批量下架（逐条走 `offline()`，失败不影响其他）。"""
        success: list[int] = []
        failed: list[dict[str, Any]] = []
        for product_id in product_ids:
            try:
                await ListingService.offline(
                    session, int(product_id), reason=reason, operator=operator, trigger="batch"
                )
                success.append(int(product_id))
            except Exception as exc:  # noqa: BLE001  单条失败不影响整批
                logger.warning("batch_offline_item_failed", product_id=product_id, error=str(exc))
                failed.append({"id": int(product_id), "reason": str(exc)})
        return {"success": success, "failed": failed}

    # ==================================================================
    #  上架（重新上架前强制校验映射）
    # ==================================================================

    @staticmethod
    async def online(
        session: Any,
        product_id: int,
        *,
        revalidate_mapping: bool = True,
        operator: str = "system",
    ) -> ListingProduct:
        """重新上架：`revalidate_mapping=True` 时映射无效**只通知不上架**（422 / 7003）。"""
        product = await ListingService.get_product(session, product_id)

        if revalidate_mapping and product.source_product_id is not None:
            codes = [sku.shop_sku_code for sku in await ListingService.list_skus(session, product_id)]
            validation = await MappingValidator.validate(
                session,
                source_product_id=int(product.source_product_id),
                platform=product.platform,
                shop_id=product.shop_id,
                sku_codes=codes,
            )
            if validation.blocking:
                raise BusinessError(
                    f"映射校验未通过，禁止上架：{validation.blocked_reason}",
                    code=ErrorCode.INVENTORY_RELIST_BLOCKED,
                    http_status=422,
                    detail=validation.model_dump(),
                )

        product.status = ListingProductStatus.ON_SALE.value
        product.offline_at = None
        product.offline_reason = None
        await session.flush()

        for sku in await ListingService.list_skus(session, product_id):
            sku.status = ListingSkuStatus.ON_SALE.value
        await session.flush()

        await AuditService.write(
            session,
            action_type=AuditActionType.ONLINE.value,
            object_type=AuditObjectType.LISTING_PRODUCT.value,
            object_id=product.id,
            operator=operator,
            new_value={"revalidate_mapping": revalidate_mapping},
            trace_id=get_trace_id(),
            remark=f"重新上架商品 {product.shop_item_id}",
        )
        return product

    # ==================================================================
    #  ★ 存量售价补填（v1.5，补填后自动触发 cost_underwater 重算）
    # ==================================================================

    @staticmethod
    async def fill_price(
        session: Any,
        product_id: int,
        items: list[FillPriceItem],
        *,
        reason: str = "",
        operator: str = "system",
    ) -> dict[str, Any]:
        """★ 存量售价补填 —— 补填后**立即触发 `cost_underwater` 重算**（附录 A 第 21 条）。

        Args:
            product_id: 平台商品 ID。
            items: 补填项，`sale_price` **必填且 > 0**，否则 422。

        Returns:
            `{"updated": int, "recomputed": int, "new_conflicts": [{sku_code, conflict_type, level}]}`。
        """
        if not items:
            raise BusinessError("补填项不能为空", code=ErrorCode.PARAM_ERROR, http_status=422)

        product = await ListingService.get_product(session, product_id)
        skus = {sku.shop_sku_code: sku for sku in await ListingService.list_skus(session, product_id)}

        updated = 0
        filled_codes: list[str] = []
        missing: list[str] = []

        for raw_item in items:
            from app.schemas.common import parse_money

            # 兼容 dict 入参（任务层 / 未来调用方），统一转成 FillPriceItem
            item = FillPriceItem(**raw_item) if isinstance(raw_item, dict) else raw_item
            try:
                cents = parse_money(item.sale_price, field_name="售价")
            except ValueError as exc:
                raise BusinessError(str(exc), code=ErrorCode.PARAM_ERROR, http_status=422) from exc
            if cents <= 0:
                raise BusinessError(
                    f"SKU {item.shop_sku_code} 的售价必须大于 0（当前 {item.sale_price}）",
                    code=ErrorCode.PARAM_ERROR,
                    http_status=422,
                )

            sku = skus.get(item.shop_sku_code)
            if sku is None:
                missing.append(item.shop_sku_code)
                continue
            sku.sale_price_cents = cents
            sku.updated_at = utc_now()
            updated += 1
            filled_codes.append(item.shop_sku_code)

        if missing:
            raise BusinessError(
                f"以下 SKU 不属于该商品：{', '.join(missing[:10])}",
                code=ErrorCode.PARAM_ERROR,
                http_status=422,
            )

        await session.flush()

        # ★ 补填后立即重算倒挂（复用 INV-P0-05 重算机制）
        new_conflicts = await MappingValidator.recompute_cost_underwater(
            session, listing_product_id=product_id, sku_codes=filled_codes
        )

        await AuditService.write(
            session,
            action_type=AuditActionType.ONLINE.value,
            object_type=AuditObjectType.LISTING_PRODUCT.value,
            object_id=product.id,
            operator=operator,
            new_value={"updated": updated, "codes": filled_codes, "reason": reason},
            trace_id=get_trace_id(),
            remark=f"补填 {updated} 个 SKU 售价并触发倒挂重算",
        )
        await session.flush()

        logger.info(
            "listing_fill_price_done",
            product_id=product.id,
            updated=updated,
            new_conflicts=len(new_conflicts),
        )
        return {
            "updated": updated,
            "recomputed": len(new_conflicts),
            "new_conflicts": [
                {
                    "sku_code": c.shop_sku_code,
                    "conflict_type": c.conflict_type,
                    "level": c.level,
                    "description": c.description,
                }
                for c in new_conflicts
            ],
        }

    @staticmethod
    def product_summary(product: ListingProduct) -> dict[str, Any]:
        """商品摘要（任务回调用）。"""
        return {
            "id": int(product.id),
            "shop_item_id": product.shop_item_id,
            "status": product.status,
            "offlined_at": iso_utc(product.offline_at) if product.offline_at else None,
        }
