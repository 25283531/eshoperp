"""SKU 映射服务（§5.5.6 —— 最高等级资产）。

================================================================================
★ ★ ★ 成本三层语义（ARCH v1.4 / PRD v1.4）★ ★ ★
================================================================================
| 层   | 位置                              | 语义                                   |
| ---- | --------------------------------- | -------------------------------------- |
| 真源 | `source_sku.cost_price_cents`     | 1688 侧成本，**唯一真相源**             |
| 镜像 | `sku_mapping.purchase_cost_cents` | 随真源**自动同步**；人工可覆盖           |
| 快照 | `order_item.purchase_cost_cents`  | 下单时点固化，**永不可变**               |

三条硬约束：
    1. **历史订单利润禁止 join 回 `sku_mapping` / `source_sku` 取成本**（附录 A 第 18 条）
       —— 只能读 `order_item.purchase_cost_cents` 快照。
    2. **人工覆盖（`cost_source='manual'`）不静默冲掉**
       —— 货源再变动时**不覆盖**，改为生成「成本待确认」工单。
    3. **审计噪音控制**
       —— 系统自动同步产生的变更日志 / 审计默认过滤（`change_source='system'`）。
================================================================================
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from app.core.errors import (
    BusinessError,
    ErrorCode,
    NotFoundError,
    UniqueConflictError,
    describe_integrity_error,
)
from app.core.logging import get_logger, get_trace_id
from app.models.asset import Asset
from app.models.enums import (
    AuditActionType,
    AuditObjectType,
    ChangeAction,
    ChangeSource,
    ConflictLevel,
    MappingStatus,
    PushStatus,
)
from app.models.mapping import MappingChangeLog, MappingConflict, SkuMapping
from app.models.source import SourceProduct, SourceSku
from app.schemas.mapping import (
    MappingStatsVo,
    SkuMappingCreate,
    SkuMappingUpdate,
    SkuMappingVo,
)
from app.services.audit_service import AuditService
from app.utils.csvio import (
    MAPPING_CSV_HEADERS,
    cents_to_yuan_str,
    export_csv,
    read_csv_dicts,
    yuan_str_to_cents,
)
from app.utils.kit import iso_utc, spec_signature, utc_now

logger = get_logger(__name__)

__all__ = ["MappingService"]


class MappingService:
    """SKU 映射的 CRUD、成本同步、冲突与变更日志。"""

    # ==================================================================
    #  查询
    # ==================================================================

    @staticmethod
    async def get(session: Any, mapping_id: int) -> SkuMapping:
        """按 ID 取映射（含已软删除）。

        Raises:
            NotFoundError: 1004 不存在。
        """
        stmt = select(SkuMapping).where(SkuMapping.id == int(mapping_id))
        mapping = (await session.execute(stmt)).scalars().first()
        if mapping is None:
            raise NotFoundError(f"SKU 映射 {mapping_id} 不存在")
        return mapping

    @staticmethod
    async def list_mappings(
        session: Any,
        *,
        platform: str | None = None,
        shop_id: str | None = None,
        shop_item_id: str | None = None,
        source_product_id: int | None = None,
        status: str | None = None,
        has_conflict: bool | None = None,
        keyword: str | None = None,
        include_deleted: bool = False,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[SkuMapping], int]:
        """分页查询映射。"""
        stmt = select(SkuMapping)
        if not include_deleted:
            stmt = stmt.where(SkuMapping.is_deleted.is_(False))
        if platform:
            stmt = stmt.where(SkuMapping.platform == platform)
        if shop_id:
            stmt = stmt.where(SkuMapping.shop_id == shop_id)
        if shop_item_id:
            stmt = stmt.where(SkuMapping.shop_item_id == shop_item_id)
        if source_product_id is not None:
            stmt = stmt.where(SkuMapping.source_product_id == int(source_product_id))
        if status:
            stmt = stmt.where(SkuMapping.status == status)
        if has_conflict is not None:
            stmt = stmt.where(SkuMapping.has_conflict.is_(has_conflict))
        if keyword:
            like = f"%{keyword.strip()}%"
            stmt = stmt.where(
                or_(
                    SkuMapping.shop_sku_code.like(like),
                    SkuMapping.shop_sku_name.like(like),
                    SkuMapping.source_sku_code_1688.like(like),
                    SkuMapping.shop_item_id.like(like),
                )
            )

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await session.execute(count_stmt)).scalar_one() or 0)
        rows = (
            (await session.execute(stmt.order_by(SkuMapping.id.desc()).offset((page - 1) * page_size).limit(page_size)))
            .scalars()
            .all()
        )
        return list(rows), total

    @staticmethod
    async def stats(session: Any) -> MappingStatsVo:
        """映射统计卡片数据。"""
        base = select(func.count()).select_from(SkuMapping).where(SkuMapping.is_deleted.is_(False))
        total = int((await session.execute(base)).scalar_one() or 0)

        async def _count(*extra: Any) -> int:
            stmt = select(func.count()).select_from(SkuMapping).where(
                SkuMapping.is_deleted.is_(False), *extra
            )
            return int((await session.execute(stmt)).scalar_one() or 0)

        deleted_recent = int(
            (
                await session.execute(
                    select(func.count()).select_from(SkuMapping).where(SkuMapping.is_deleted.is_(True))
                )
            ).scalar_one()
            or 0
        )
        return MappingStatsVo(
            total=total,
            valid=await _count(SkuMapping.status == MappingStatus.VALID.value),
            pending_confirm=await _count(SkuMapping.status == MappingStatus.PENDING_CONFIRM.value),
            invalid=await _count(SkuMapping.status == MappingStatus.INVALID.value),
            conflict_p0=await _count(SkuMapping.conflict_level == ConflictLevel.P0.value, SkuMapping.has_conflict.is_(True)),
            conflict_p1=await _count(SkuMapping.conflict_level == ConflictLevel.P1.value, SkuMapping.has_conflict.is_(True)),
            deleted_recent=deleted_recent,
        )

    # ==================================================================
    #  创建 / 更新 / 删除
    # ==================================================================

    @staticmethod
    async def create(
        session: Any,
        payload: SkuMappingCreate,
        *,
        operator: str = "system",
        is_mock: bool = False,
    ) -> SkuMapping:
        """创建映射（唯一键冲突 → 409 / 1006）。

        ★★ QA-01 / QA-02：这里是 `one_to_many` / `duplicate_item` 的**真实保护点** ★★
        部分唯一索引 `uq_sku_mapping_shop_sku (platform, shop_id, shop_sku_code)
        WHERE is_deleted=0` 在写入边界结构性阻断了「一平台 SKU 映射多个货源 SKU」
        与「同一 SKU 编码挂多个商品」——这两类冲突在库里根本存不下来，
        所以读时扫描 SQL（恒为空）已删除，保护改由这里 + 数据库约束共同承担：
        撞键必须变成**明确的业务错误**，绝不能让 `IntegrityError` 漏成 500。
        """
        cost_cents = payload.cost_cents()

        await MappingService._assert_shop_sku_free(
            session,
            platform=payload.platform,
            shop_id=payload.shop_id,
            shop_sku_code=payload.shop_sku_code,
        )

        # 规格指纹：优先取货源 SKU 的指纹，保证与货源侧可比
        signature = await MappingService._resolve_spec_signature(
            session, source_sku_id=payload.source_sku_id, source_sku_code=payload.source_sku_code_1688
        )

        mapping = SkuMapping(
            platform=payload.platform,
            shop_id=payload.shop_id,
            shop_item_id=payload.shop_item_id,
            shop_sku_code=payload.shop_sku_code,
            shop_sku_name=payload.shop_sku_name,
            source_product_id=payload.source_product_id,
            source_sku_id=payload.source_sku_id,
            source_product_1688_id=payload.source_product_1688_id,
            source_sku_code_1688=payload.source_sku_code_1688,
            source_sku_name=payload.source_sku_name,
            spec_signature=signature,
            purchase_cost_cents=cost_cents,
            cost_source="manual" if cost_cents > 0 else "auto",
            status=payload.status or MappingStatus.VALID.value,
            is_mock=bool(is_mock),
            source="manual",
            created_by=operator,
            updated_by=operator,
            remark=payload.remark,
        )
        session.add(mapping)
        await MappingService._flush_or_unique_conflict(
            session, hint=f"{payload.platform}/{payload.shop_id}/{payload.shop_sku_code}"
        )

        session.add(
            MappingChangeLog.build(
                sku_mapping_id=mapping.id,
                change_action=ChangeAction.CREATE.value,
                change_source=ChangeSource.MANUAL.value,
                operator=operator,
                new_value=SkuMappingVo.from_model(mapping).model_dump_json(),
                trace_id=get_trace_id(),
            )
        )
        await AuditService.write(
            session,
            action_type=AuditActionType.MAPPING_CHANGE.value,
            object_type=AuditObjectType.SKU_MAPPING.value,
            object_id=mapping.id,
            operator=operator,
            new_value={"action": "create", "shop_sku_code": mapping.shop_sku_code},
            trace_id=get_trace_id(),
            remark=f"创建映射 {mapping.platform}/{mapping.shop_sku_code}",
        )
        return mapping

    @staticmethod
    async def update(
        session: Any,
        mapping_id: int,
        payload: SkuMappingUpdate,
        *,
        operator: str = "system",
    ) -> SkuMapping:
        """更新映射（逐字段写变更日志 + 审计）。

        ★ 成本写入语义（v1.4）：body 含 `purchase_cost` 且未显式传 `cost_source`
          → 自动置 `cost_source='manual'`（人工覆盖），此后自动同步**不得静默覆盖**。
          显式传 `cost_source='auto'` 可恢复自动同步。
        """
        mapping = await MappingService.get(session, mapping_id)
        before = SkuMappingVo.from_model(mapping).model_dump()

        updates = payload.model_dump(exclude_unset=True, exclude_none=True)
        cost_provided = "purchase_cost" in updates
        cost_source_provided = "cost_source" in updates

        # ★ 写入边界：改唯一键前先检查（QA-01 / QA-02 的保护落点）
        await MappingService._assert_key_available(session, mapping, payload)

        for field, value in updates.items():
            if field == "purchase_cost":
                continue  # 单独处理（需转分 + 写 cost_source）
            if hasattr(mapping, field):
                setattr(mapping, field, value)

        if cost_provided:
            from app.schemas.common import parse_money

            cents = parse_money(payload.purchase_cost, field_name="采购成本")
            if cents <= 0:
                raise BusinessError(
                    "采购成本必须大于 0", code=ErrorCode.MAPPING_COST_INVALID, http_status=422
                )
            old_cost = int(mapping.purchase_cost_cents or 0)
            mapping.purchase_cost_cents = cents
            if cost_source_provided:
                mapping.cost_source = str(payload.cost_source or "auto").lower()
            else:
                # ★ 未显式声明来源 → 视为人工覆盖
                mapping.cost_source = "manual"
            if mapping.cost_source == "manual":
                mapping.cost_overridden_at = utc_now()
                mapping.cost_overridden_by = operator
            session.add(
                MappingChangeLog.build(
                    sku_mapping_id=mapping.id,
                    change_action=ChangeAction.UPDATE.value,
                    field_name="purchase_cost",
                    old_value=cents_to_yuan_str(old_cost),
                    new_value=cents_to_yuan_str(cents),
                    change_source=ChangeSource.MANUAL.value,
                    operator=operator,
                    trace_id=get_trace_id(),
                )
            )
        elif cost_source_provided:
            mapping.cost_source = str(payload.cost_source or "auto").lower()
            if mapping.cost_source == "manual":
                mapping.cost_overridden_at = utc_now()
                mapping.cost_overridden_by = operator

        mapping.bump_version()
        mapping.updated_by = operator
        mapping.updated_at = utc_now()
        await MappingService._flush_or_unique_conflict(
            session,
            hint=f"{mapping.platform}/{mapping.shop_id}/{mapping.shop_sku_code}",
        )

        after = SkuMappingVo.from_model(mapping).model_dump()
        for field, new_value in after.items():
            if field in {"updated_at", "version"}:
                continue
            old_value = before.get(field)
            if old_value == new_value:
                continue
            session.add(
                MappingChangeLog.build(
                    sku_mapping_id=mapping.id,
                    change_action=ChangeAction.UPDATE.value,
                    field_name=field,
                    old_value=old_value,
                    new_value=new_value,
                    change_source=ChangeSource.MANUAL.value,
                    operator=operator,
                    trace_id=get_trace_id(),
                )
            )

        await AuditService.write(
            session,
            action_type=AuditActionType.MAPPING_CHANGE.value,
            object_type=AuditObjectType.SKU_MAPPING.value,
            object_id=mapping.id,
            operator=operator,
            old_value={"fields": list(updates.keys())},
            new_value={"cost_source": mapping.cost_source},
            trace_id=get_trace_id(),
            remark=f"更新映射 {mapping.shop_sku_code}",
        )
        await session.flush()
        return mapping

    @staticmethod
    async def delete(
        session: Any,
        mapping_id: int,
        *,
        reason: str = "",
        confirm: bool = False,
        operator: str = "system",
    ) -> int:
        """软删除映射（**二次确认**；未确认 → 400）。"""
        if not confirm:
            raise BusinessError("删除映射需要二次确认（confirm=true）", code=ErrorCode.PARAM_ERROR)

        mapping = await MappingService.get(session, mapping_id)
        if mapping.is_deleted:
            raise BusinessError("映射已被软删除", code=ErrorCode.MAPPING_DELETED)
        mapping.mark_deleted(operator=operator, reason=reason)
        mapping.updated_by = operator
        await session.flush()

        session.add(
            MappingChangeLog.build(
                sku_mapping_id=mapping.id,
                change_action=ChangeAction.DELETE.value,
                change_source=ChangeSource.MANUAL.value,
                operator=operator,
                reason=reason,
                trace_id=get_trace_id(),
            )
        )
        await AuditService.write(
            session,
            action_type=AuditActionType.MAPPING_CHANGE.value,
            object_type=AuditObjectType.SKU_MAPPING.value,
            object_id=mapping.id,
            operator=operator,
            new_value={"action": "delete", "reason": reason},
            trace_id=get_trace_id(),
            remark=f"软删除映射 {mapping.shop_sku_code}",
        )
        await session.flush()
        return int(mapping.id)

    @staticmethod
    async def restore(session: Any, mapping_id: int, *, operator: str = "system") -> SkuMapping:
        """恢复软删除的映射（唯一键被占用 → 409）。"""
        mapping = await MappingService.get(session, mapping_id)
        if not mapping.is_deleted:
            raise BusinessError("映射未被删除，无需恢复", code=ErrorCode.STATE_CONFLICT)

        conflict_stmt = select(SkuMapping.id).where(
            SkuMapping.platform == mapping.platform,
            SkuMapping.shop_id == mapping.shop_id,
            SkuMapping.shop_sku_code == mapping.shop_sku_code,
            SkuMapping.is_deleted.is_(False),
        )
        if (await session.execute(conflict_stmt)).scalar_one_or_none() is not None:
            raise UniqueConflictError("同键映射已存在，无法恢复（请先删除现存映射）")

        mapping.restore()
        mapping.updated_by = operator
        await session.flush()
        session.add(
            MappingChangeLog.build(
                sku_mapping_id=mapping.id,
                change_action=ChangeAction.RESTORE.value,
                change_source=ChangeSource.MANUAL.value,
                operator=operator,
                trace_id=get_trace_id(),
            )
        )
        await session.flush()
        return mapping

    # ==================================================================
    #  ★ 成本三层语义：镜像层自动同步
    # ==================================================================

    @staticmethod
    async def sync_cost_from_source(
        session: Any,
        *,
        source_sku_ids: Iterable[int] | None = None,
        operator: str = "system",
    ) -> dict[str, int]:
        """★ 真源 → 镜像：把 `source_sku.cost_price_cents` 同步到 `sku_mapping.purchase_cost_cents`。

        硬约束：
            - `cost_source='manual'`（人工覆盖）的映射**绝不静默覆盖**，
              改为生成「成本待确认」工单（写 `mapping_conflict` + 审计 `mapping_cost_confirm`）；
            - 自动同步产生的变更日志用 `change_source='system'`，审计列表默认过滤。

        Returns:
            `{"synced": int, "skipped_manual": int, "pending_confirm": int}`。
        """
        stmt = select(SkuMapping).where(
            SkuMapping.is_deleted.is_(False),
            SkuMapping.source_sku_id.isnot(None),
        )
        ids = [int(i) for i in (source_sku_ids or [])]
        if ids:
            stmt = stmt.where(SkuMapping.source_sku_id.in_(ids))
        mappings = (await session.execute(stmt)).scalars().all()

        source_ids = sorted({int(m.source_sku_id) for m in mappings if m.source_sku_id is not None})
        costs: dict[int, int] = {}
        if source_ids:
            sku_rows = (
                (await session.execute(select(SourceSku).where(SourceSku.id.in_(source_ids)))).scalars().all()
            )
            costs = {int(s.id): int(s.cost_price_cents or 0) for s in sku_rows}

        synced = 0
        skipped_manual = 0
        pending_confirm = 0

        for mapping in mappings:
            true_cost = costs.get(int(mapping.source_sku_id or 0))
            if true_cost is None or true_cost <= 0:
                continue
            if int(mapping.purchase_cost_cents or 0) == true_cost:
                continue

            if mapping.cost_source == "manual":
                # ★ 人工覆盖过 → 不静默覆盖，生成「成本待确认」工单
                skipped_manual += 1
                exists = (
                    await session.execute(
                        select(MappingConflict.id).where(
                            MappingConflict.sku_mapping_id == mapping.id,
                            MappingConflict.conflict_type == "cost_manual_confirm",
                            MappingConflict.is_resolved.is_(False),
                        )
                    )
                ).scalar_one_or_none()
                if exists is None:
                    session.add(
                        MappingConflict(
                            sku_mapping_id=mapping.id,
                            conflict_type="cost_manual_confirm",
                            level="P1",
                            description=(
                                f"货源成本已变动为 {true_cost / 100:.2f} 元，但本映射成本为人工覆盖"
                                f"（{int(mapping.purchase_cost_cents or 0) / 100:.2f} 元，"
                                f"由 {mapping.cost_overridden_by or '人工'} 设置），请确认是否同步"
                            ),
                            detail_json={
                                "new_cost_cents": true_cost,
                                "current_cost_cents": int(mapping.purchase_cost_cents or 0),
                                "old_value": int(mapping.purchase_cost_cents or 0),
                                "new_value": true_cost,
                            },
                            is_resolved=False,
                        )
                    )
                    await AuditService.write(
                        session,
                        action_type=AuditActionType.MAPPING_COST_CONFIRM.value,
                        object_type=AuditObjectType.SKU_MAPPING.value,
                        object_id=mapping.id,
                        operator=operator,
                        old_value={"purchase_cost_cents": int(mapping.purchase_cost_cents or 0)},
                        new_value={"source_cost_cents": true_cost},
                        trace_id=get_trace_id(),
                        remark="人工覆盖成本未被自动同步，已生成「成本待确认」工单",
                    )
                    pending_confirm += 1
                continue

            old_cost = int(mapping.purchase_cost_cents or 0)
            mapping.purchase_cost_cents = true_cost
            mapping.last_cost_check_at = utc_now()
            mapping.bump_version()
            session.add(
                MappingChangeLog.build(
                    sku_mapping_id=mapping.id,
                    change_action=ChangeAction.UPDATE.value,
                    field_name="purchase_cost",
                    old_value=cents_to_yuan_str(old_cost),
                    new_value=cents_to_yuan_str(true_cost),
                    change_source=ChangeSource.SYSTEM.value,  # ★ 系统同步，审计默认过滤
                    operator="system",
                    reason="真源成本自动同步",
                    trace_id=get_trace_id(),
                )
            )
            synced += 1

        await session.flush()
        logger.info("cost_sync_done", synced=synced, skipped_manual=skipped_manual, pending_confirm=pending_confirm)
        return {"synced": synced, "skipped_manual": skipped_manual, "pending_confirm": pending_confirm}

    @staticmethod
    async def resolve_cost_confirm(
        session: Any,
        mapping_id: int,
        *,
        accept_source_cost: bool,
        operator: str = "system",
    ) -> SkuMapping:
        """处理「成本待确认」工单：接受货源新成本 或 保留人工覆盖值。"""
        mapping = await MappingService.get(session, mapping_id)
        conflicts = (
            (
                await session.execute(
                    select(MappingConflict).where(
                        MappingConflict.sku_mapping_id == mapping.id,
                        MappingConflict.conflict_type == "cost_manual_confirm",
                        MappingConflict.is_resolved.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )
        for conflict in conflicts:
            conflict.resolve("manual_fix", operator=operator)

        if accept_source_cost:
            detail = (conflicts[0].detail_json if conflicts else {}) or {}
            new_cost = int(detail.get("new_cost_cents") or 0)
            if new_cost > 0:
                old_cost = int(mapping.purchase_cost_cents or 0)
                mapping.purchase_cost_cents = new_cost
                mapping.cost_source = "auto"  # ★ 恢复自动同步
                mapping.cost_overridden_at = None
                mapping.cost_overridden_by = None
                mapping.last_cost_check_at = utc_now()
                mapping.bump_version()
                session.add(
                    MappingChangeLog.build(
                        sku_mapping_id=mapping.id,
                        change_action=ChangeAction.UPDATE.value,
                        field_name="purchase_cost",
                        old_value=cents_to_yuan_str(old_cost),
                        new_value=cents_to_yuan_str(new_cost),
                        change_source=ChangeSource.MANUAL.value,
                        operator=operator,
                        reason="确认同步货源成本，恢复自动同步",
                        trace_id=get_trace_id(),
                    )
                )
        await AuditService.write(
            session,
            action_type=AuditActionType.MAPPING_COST_CONFIRM.value,
            object_type=AuditObjectType.SKU_MAPPING.value,
            object_id=mapping.id,
            operator=operator,
            new_value={"accept_source_cost": accept_source_cost},
            trace_id=get_trace_id(),
            remark=("接受货源新成本并恢复自动同步" if accept_source_cost else "保留人工覆盖成本"),
        )
        await session.flush()
        return mapping

    # ==================================================================
    #  变更日志 / 冲突列表 / 导出
    # ==================================================================

    @staticmethod
    async def list_change_logs(
        session: Any,
        mapping_id: int,
        *,
        include_system: bool = False,
        limit: int = 200,
    ) -> list[MappingChangeLog]:
        """映射变更历史。

        Args:
            include_system: 默认 False → **排除 `change_source='system'` 的自动同步记录**
                            （v1.4 审计噪音控制：自动同步会淹没人工改动）。
        """
        stmt = select(MappingChangeLog).where(MappingChangeLog.sku_mapping_id == int(mapping_id))
        if not include_system:
            stmt = stmt.where(MappingChangeLog.change_source != ChangeSource.SYSTEM.value)
        rows = (
            (await session.execute(stmt.order_by(MappingChangeLog.created_at.desc()).limit(limit)))
            .scalars()
            .all()
        )
        return list(rows)

    @staticmethod
    async def list_conflicts(
        session: Any,
        *,
        level: str | None = None,
        conflict_type: str | None = None,
        is_resolved: bool | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[MappingConflict], int]:
        """分页查询冲突。"""
        stmt = select(MappingConflict)
        if level:
            stmt = stmt.where(MappingConflict.level == level)
        if conflict_type:
            stmt = stmt.where(MappingConflict.conflict_type == conflict_type)
        if is_resolved is not None:
            stmt = stmt.where(MappingConflict.is_resolved.is_(is_resolved))

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await session.execute(count_stmt)).scalar_one() or 0)
        rows = (
            (
                await session.execute(
                    stmt.order_by(MappingConflict.id.desc()).offset((page - 1) * page_size).limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        return list(rows), total

    @staticmethod
    async def list_pending(
        session: Any,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[MappingConflict], dict[int, SkuMapping], dict[int, str], int]:
        """待确认工单：未解决的冲突 + 关联映射 + 货源商品标题。

        Returns:
            `(冲突列表, {mapping_id: SkuMapping}, {source_product_id: 标题}, 总数)`。
        """
        stmt = select(MappingConflict).where(MappingConflict.is_resolved.is_(False))
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await session.execute(count_stmt)).scalar_one() or 0)
        rows = (
            (
                await session.execute(
                    stmt.order_by(MappingConflict.id.desc()).offset((page - 1) * page_size).limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        mapping_ids = sorted({int(c.sku_mapping_id) for c in rows if c.sku_mapping_id})
        mappings: dict[int, SkuMapping] = {}
        if mapping_ids:
            found = (
                (await session.execute(select(SkuMapping).where(SkuMapping.id.in_(mapping_ids)))).scalars().all()
            )
            mappings = {int(m.id): m for m in found}

        product_ids = sorted(
            {int(m.source_product_id) for m in mappings.values() if m.source_product_id is not None}
        )
        titles: dict[int, str] = {}
        if product_ids:
            products = (
                (await session.execute(select(SourceProduct).where(SourceProduct.id.in_(product_ids))))
                .scalars()
                .all()
            )
            titles = {int(p.id): p.title for p in products}
        return list(rows), mappings, titles, total  # type: ignore[return-value]

    @staticmethod
    async def export_mappings(
        session: Any,
        *,
        platform: str | None = None,
        shop_id: str | None = None,
        adapter_name: str = "generic",
        updated_from: Any = None,
        updated_to: Any = None,
        export_dir: Any = None,
    ) -> dict[str, Any]:
        """导出映射 CSV（供第三方导入）。

        Returns:
            `{"download_url": str, "row_count": int, "exported_at": str, "path": str}`。
        """
        from app.core.config import get_settings

        stmt = select(SkuMapping).where(SkuMapping.is_deleted.is_(False))
        if platform:
            stmt = stmt.where(SkuMapping.platform == platform)
        if shop_id:
            stmt = stmt.where(SkuMapping.shop_id == shop_id)
        if updated_from is not None:
            stmt = stmt.where(SkuMapping.updated_at >= updated_from)
        if updated_to is not None:
            stmt = stmt.where(SkuMapping.updated_at <= updated_to)
        rows = (await session.execute(stmt.order_by(SkuMapping.id))).scalars().all()

        payload = [
            {
                "platform": m.platform,
                "shop_id": m.shop_id,
                "shop_item_id": m.shop_item_id,
                "shop_sku_code": m.shop_sku_code,
                "shop_sku_name": m.shop_sku_name or "",
                "source_product_1688_id": m.source_product_1688_id or "",
                "source_sku_code_1688": m.source_sku_code_1688 or "",
                "source_sku_name": m.source_sku_name or "",
                "spec_signature": m.spec_signature or "",
                "purchase_cost": cents_to_yuan_str(m.purchase_cost_cents),
                "cost_currency": m.cost_currency or "CNY",
                "status": m.status,
                "is_mock": 1 if m.is_mock else 0,
                "remark": m.remark or "",
            }
            for m in rows
        ]

        directory = Path(export_dir) if export_dir else get_settings().exports_dir
        filename = f"sku_mappings_{adapter_name}_{utc_now().strftime('%Y%m%d%H%M%S')}.csv"
        path = export_csv(payload, directory / filename, headers=list(MAPPING_CSV_HEADERS))
        return {
            "download_url": f"/api/v1/files/exports/{filename}",
            "row_count": len(payload),
            "exported_at": iso_utc(utc_now()),
            "path": path,
        }

    @staticmethod
    async def push_mappings(
        session: Any,
        *,
        adapter_name: str = "generic",
        ids: list[int] | None = None,
        all_valid: bool = False,
        operator: str = "system",
    ) -> dict[str, Any]:
        """推送映射到第三方（`POST /sku-mappings/push`）。

        ★ 诚实降级：三个履约 / 第三方适配器（`miaoshou` / `yitao` / `local_csv`）
          的映射推送能力在 MVP 均为 **skeleton + TODO**（未取得第三方 profile 实测），
          因此本方法**只导出 CSV 并把推送状态标为 `degraded`**，
          **绝不伪装成 `success`** —— 静默假成功是本项目明令禁止的失效模式。

        Returns:
            `{"pushed": int, "success": int, "failed": int, "degraded": bool,
              "download_url": str, "row_count": int}`。
        """
        stmt = select(SkuMapping).where(SkuMapping.is_deleted.is_(False))
        if ids:
            stmt = stmt.where(SkuMapping.id.in_([int(i) for i in ids]))
        if all_valid:
            stmt = stmt.where(SkuMapping.status == MappingStatus.VALID.value)
        rows = (await session.execute(stmt.order_by(SkuMapping.id))).scalars().all()
        if not rows:
            raise BusinessError("没有可推送的映射", code=ErrorCode.PARAM_ERROR)

        exported = await MappingService.export_mappings(
            session,
            adapter_name=adapter_name,
            updated_from=None,
            updated_to=None,
        )
        now = utc_now()
        for mapping in rows:
            mapping.last_pushed_at = now
            mapping.last_push_status = PushStatus.DEGRADED.value
        await session.flush()

        await AuditService.write(
            session,
            action_type=AuditActionType.MAPPING_PUSH.value,
            object_type=AuditObjectType.SKU_MAPPING.value,
            object_id=f"push:{adapter_name}",
            operator=operator,
            new_value={"adapter_name": adapter_name, "pushed": len(rows), "row_count": exported["row_count"]},
            trace_id=get_trace_id(),
            remark=f"导出映射 CSV 供第三方导入（适配器 {adapter_name} 推送能力未实测，降级为人工导入）",
        )
        await session.flush()
        return {
            "pushed": len(rows),
            "success": 0,
            "failed": 0,
            "degraded": True,
            "download_url": exported["download_url"],
            "row_count": exported["row_count"],
            "message": (
                f"适配器 {adapter_name} 的映射推送能力尚未实测（skeleton+TODO），"
                f"已导出 CSV 供人工导入，推送状态标记为 degraded（不伪装成功）"
            ),
        }

    @staticmethod
    async def import_mappings(
        session: Any,
        content: bytes | str,
        *,
        operator: str = "system",
    ) -> dict[str, Any]:
        """导入第三方导出的映射 CSV（`POST /sku-mappings/import`）。

        Returns:
            `{"added": int, "modified": int, "conflict": int, "orphan": int, "report_url": str}`。

        Raises:
            BusinessError: CSV 缺少必需列（1001）。
        """
        from tempfile import NamedTemporaryFile

        raw = content.decode("utf-8-sig", errors="replace") if isinstance(content, bytes) else content
        required = ["platform", "shop_id", "shop_item_id", "shop_sku_code"]
        with NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8", newline="") as handle:
            handle.write(raw)
            temp_path = handle.name

        try:
            rows = read_csv_dicts(temp_path, required_headers=required)
        except ValueError as exc:
            raise BusinessError(str(exc), code=ErrorCode.PARAM_ERROR) from exc
        finally:
            Path(temp_path).unlink(missing_ok=True)

        added = 0
        modified = 0
        conflict = 0
        orphan = 0
        for row in rows:
            # ★ 按**唯一键**查重（platform+shop_id+shop_sku_code），
            #   不再含 shop_item_id —— 否则「同编码挂在不同商品下」会查不到、
            #   插入时撞唯一索引抛 IntegrityError → 整批导入 500。
            stmt = select(SkuMapping).where(
                SkuMapping.platform == row.get("platform", ""),
                SkuMapping.shop_id == row.get("shop_id", ""),
                SkuMapping.shop_sku_code == row.get("shop_sku_code", ""),
                SkuMapping.is_deleted.is_(False),
            )
            existing = (await session.execute(stmt)).scalars().first()
            cost_text = row.get("purchase_cost") or ""
            if not cost_text:
                orphan += 1
                continue
            if existing is None:
                session.add(
                    SkuMapping(
                        platform=row.get("platform", ""),
                        shop_id=row.get("shop_id", ""),
                        shop_item_id=row.get("shop_item_id", ""),
                        shop_sku_code=row.get("shop_sku_code", ""),
                        shop_sku_name=row.get("shop_sku_name") or None,
                        source_product_1688_id=row.get("source_product_1688_id") or None,
                        source_sku_code_1688=row.get("source_sku_code_1688") or None,
                        source_sku_name=row.get("source_sku_name") or None,
                        purchase_cost_cents=yuan_str_to_cents(cost_text),
                        spec_signature=row.get("spec_signature") or None,
                        status=row.get("status") or MappingStatus.PENDING_CONFIRM.value,
                        source="import",
                        created_by=operator,
                        updated_by=operator,
                    )
                )
                added += 1
            else:
                if existing.purchase_cost_cents and existing.purchase_cost_cents != yuan_str_to_cents(cost_text):
                    conflict += 1
                    continue
                existing.purchase_cost_cents = yuan_str_to_cents(cost_text)
                existing.shop_sku_name = row.get("shop_sku_name") or existing.shop_sku_name
                existing.shop_item_id = row.get("shop_item_id") or existing.shop_item_id
                existing.updated_by = operator
                modified += 1
        await MappingService._flush_or_unique_conflict(session, hint="CSV 批量导入")

        await AuditService.write(
            session,
            action_type=AuditActionType.MAPPING_IMPORT.value,
            object_type=AuditObjectType.SKU_MAPPING.value,
            object_id="import",
            operator=operator,
            new_value={"added": added, "modified": modified, "conflict": conflict, "orphan": orphan},
            trace_id=get_trace_id(),
            remark=f"导入第三方映射 CSV：新增 {added} / 更新 {modified} / 冲突 {conflict} / 跳过 {orphan}",
        )
        await session.flush()
        return {
            "added": added,
            "modified": modified,
            "conflict": conflict,
            "orphan": orphan,
            "report_url": "",
        }

    @staticmethod
    async def batch_upsert(
        session: Any,
        items: Iterable[Any],
        *,
        operator: str = "system",
    ) -> dict[str, Any]:
        """批量创建 / 更新映射（`POST /sku-mappings/batch`，≤200 条）。

        唯一键：`(platform, shop_id, shop_item_id, shop_sku_code)`。
        单条失败**不影响整批**，失败项以 `{index, reason}` 返回。

        Returns:
            `{"created": int, "updated": int, "failed": list[dict]}`。
        """
        created = 0
        updated = 0
        failed: list[dict[str, Any]] = []
        for index, item in enumerate(items or []):
            try:
                stmt = select(SkuMapping).where(
                    SkuMapping.platform == item.platform,
                    SkuMapping.shop_id == item.shop_id,
                    SkuMapping.shop_item_id == item.shop_item_id,
                    SkuMapping.shop_sku_code == item.shop_sku_code,
                )
                existing = (await session.execute(stmt)).scalars().first()
                if existing is None:
                    await MappingService.create(session, item, operator=operator)
                    created += 1
                else:
                    payload = SkuMappingUpdate(
                        **{
                            k: v
                            for k, v in item.model_dump(exclude_none=True).items()
                            if k in SkuMappingUpdate.model_fields
                        }
                    )
                    await MappingService.update(session, int(existing.id), payload, operator=operator)
                    updated += 1
            except Exception as exc:  # noqa: BLE001  单条失败不阻断整批
                logger.warning("mapping_batch_item_failed", index=index, error=str(exc))
                failed.append({"index": index, "reason": str(exc)})
        await session.flush()
        return {"created": created, "updated": updated, "failed": failed}

    @staticmethod
    async def resolve_pending(
        session: Any,
        conflict_id: int,
        *,
        action: str = "confirm",
        new_source_sku_code_1688: str | None = None,
        note: str | None = None,
        operator: str = "system",
    ) -> SkuMapping:
        """处理「待确认工单」（`POST /sku-mappings/pending/{id}/resolve`）。

        ★★ QA-03 配套：**挂起必须有恢复路径**，否则「确认」之后订单永远发不出去 ★★
        映射被自动置为 `pending_confirm` 后，运营在「待确认工单」里点确认，
        必须能把映射**恢复成 valid**，否则就变成"永久挂起"（另一个方向的业务事故）。
        对 `spec_mismatch` 而言，恢复的正确动作是**以货源侧当前规格为准刷新指纹**
        （只改 status 不改指纹的话，下一次检测会立刻再次命中并重新挂起）。

        Args:
            action: `confirm`（接受货源侧新值）/ `reject`（保留人工值）/ `manual_assign`（人工改绑货源 SKU）。
            new_source_sku_code_1688: `manual_assign` 时的新货源 SKU 编码。
        """
        conflict = (
            await session.execute(select(MappingConflict).where(MappingConflict.id == int(conflict_id)))
        ).scalars().first()
        if conflict is None:
            raise NotFoundError(f"待确认工单 {conflict_id} 不存在")

        mapping_id = int(conflict.sku_mapping_id or 0)
        if conflict.conflict_type == "spec_mismatch" and action == "confirm":
            # ★ 规格漂移：以货源侧当前规格为准刷新指纹 → 恢复 valid → 订单可继续发货
            mapping = await MappingService.confirm_spec_change(session, mapping_id, operator=operator)
        elif action == "manual_assign":
            if not new_source_sku_code_1688:
                raise BusinessError(
                    "manual_assign 必须提供 new_source_sku_code_1688",
                    code=ErrorCode.PARAM_ERROR,
                )
            mapping = await MappingService.update(
                session,
                mapping_id,
                SkuMappingUpdate(source_sku_code_1688=new_source_sku_code_1688, remark=note),
                operator=operator,
            )
        else:
            mapping = await MappingService.resolve_cost_confirm(
                session,
                mapping_id,
                accept_source_cost=(action == "confirm"),
                operator=operator,
            )

        conflict.is_resolved = True
        conflict.resolved_by = operator
        conflict.resolved_at = utc_now()
        conflict.resolve_action = action
        await session.flush()
        return mapping

    @staticmethod
    async def confirm_spec_change(
        session: Any,
        mapping_id: int,
        *,
        operator: str = "system",
    ) -> SkuMapping:
        """★ 确认规格变更：以货源侧当前规格为准刷新指纹，并把映射恢复为 `valid`。

        QA-03 的**恢复路径**。没有它，被自动挂起的映射只能靠人工改库恢复，
        而 `PUT /sku-mappings/{id}` 只改 `status` 不改指纹的话，
        下一次冲突检测会**立刻再次命中**并重新挂起 —— 运营会被困在死循环里。

        Returns:
            恢复后的 `SkuMapping`（`status='valid'`，指纹已同步为货源侧当前值）。
        """
        mapping = await MappingService.get(session, mapping_id)
        source_sku = None
        if mapping.source_sku_id is not None:
            source_sku = (
                await session.execute(select(SourceSku).where(SourceSku.id == int(mapping.source_sku_id)))
            ).scalars().first()
        if source_sku is None:
            raise BusinessError(
                f"映射 {mapping_id} 未绑定货源 SKU，无法确认规格变更（请改用 manual_assign 改绑）",
                code=ErrorCode.MAPPING_MISSING,
            )

        old_signature = mapping.spec_signature or ""
        new_signature = source_sku.spec_signature or spec_signature(source_sku.spec_json)
        mapping.spec_signature = new_signature
        old_status = mapping.status
        mapping.status = MappingStatus.VALID.value
        mapping.bump_version()
        mapping.updated_by = operator
        mapping.updated_at = utc_now()
        await session.flush()

        session.add(
            MappingChangeLog.build(
                sku_mapping_id=int(mapping_id),
                change_action=ChangeAction.STATUS_CHANGE.value,
                field_name="spec_signature",
                old_value=old_signature,
                new_value=new_signature,
                change_source=ChangeSource.MANUAL.value,
                operator=operator,
                reason="确认货源侧规格变更，映射恢复有效",
                trace_id=get_trace_id(),
            )
        )
        session.add(
            MappingChangeLog.build(
                sku_mapping_id=int(mapping_id),
                change_action=ChangeAction.STATUS_CHANGE.value,
                field_name="status",
                old_value=old_status,
                new_value=MappingStatus.VALID.value,
                change_source=ChangeSource.MANUAL.value,
                operator=operator,
                reason="规格已确认，恢复 valid（订单可继续发货）",
                trace_id=get_trace_id(),
            )
        )
        await AuditService.write(
            session,
            action_type=AuditActionType.MAPPING_CHANGE.value,
            object_type=AuditObjectType.SKU_MAPPING.value,
            object_id=int(mapping_id),
            operator=operator,
            old_value={"spec_signature": old_signature, "status": old_status},
            new_value={"spec_signature": new_signature, "status": MappingStatus.VALID.value},
            trace_id=get_trace_id(),
            remark=f"★ 已确认规格变更：{mapping.shop_sku_code} 指纹同步为货源侧当前值，映射恢复 valid",
        )
        await session.flush()
        logger.info(
            "spec_change_confirmed",
            mapping_id=mapping_id,
            shop_sku_code=mapping.shop_sku_code,
            old_signature=old_signature,
            new_signature=new_signature,
            operator=operator,
        )
        return mapping

    # ==================================================================
    #  内部工具
    # ==================================================================

    @staticmethod
    async def _assert_shop_sku_free(
        session: Any, *, platform: str, shop_id: str, shop_sku_code: str
    ) -> None:
        """★ 创建路径的唯一键检查（QA-01 / QA-02 的保护落点之一）。

        「一平台 SKU 映射多个货源 SKU」（`one_to_many`）与
        「同一 SKU 编码挂多个商品」（`duplicate_item`）在当前 schema 下**不可能存在于库中**：
        部分唯一索引 `uq_sku_mapping_shop_sku (platform, shop_id, shop_sku_code)
        WHERE is_deleted=0` 会把第二行直接挡掉。因此读时扫描 SQL 已删除，
        保护改由**写入边界**承担：撞键必须变成明确的 409 业务错误，
        绝不能让 `IntegrityError` 漏出去变成 500。

        Raises:
            UniqueConflictError: 409 / 1006，文案可直接展示给运营。
        """
        stmt = select(SkuMapping).where(
            SkuMapping.platform == platform,
            SkuMapping.shop_id == shop_id,
            SkuMapping.shop_sku_code == shop_sku_code,
            SkuMapping.is_deleted.is_(False),
        )
        existing = (await session.execute(stmt)).scalars().first()
        if existing is None:
            return
        raise UniqueConflictError(
            f"该平台 SKU 已存在映射：{platform}/{shop_id}/{shop_sku_code}"
            f"（当前指向货源 SKU {existing.source_sku_code_1688 or '未知'}；"
            "同一 平台+店铺+SKU 编码 只能有一条有效映射，请先删除或改名现存映射）"
        )

    @staticmethod
    async def _assert_key_available(session: Any, mapping: SkuMapping, payload: Any) -> None:
        """★ 改唯一键前先检查：`(platform, shop_id, shop_sku_code)` 是否已被别人占用。

        这条检查是 QA-01 / QA-02 的**保护落点**：
        把映射 2 的 SKU 编码改成映射 1 的编码，等价于制造
        「一平台 SKU → 多货源 SKU」/「同编码挂多商品」—— 必须在写入边界拒绝。
        """
        new_platform = str(getattr(payload, "platform", None) or mapping.platform or "")
        new_shop_id = str(getattr(payload, "shop_id", None) or mapping.shop_id or "")
        new_sku_code = str(getattr(payload, "shop_sku_code", None) or mapping.shop_sku_code or "")
        if (
            new_platform == mapping.platform
            and new_shop_id == mapping.shop_id
            and new_sku_code == mapping.shop_sku_code
        ):
            return  # 唯一键未变动
        stmt = select(SkuMapping.id).where(
            SkuMapping.platform == new_platform,
            SkuMapping.shop_id == new_shop_id,
            SkuMapping.shop_sku_code == new_sku_code,
            SkuMapping.is_deleted.is_(False),
            SkuMapping.id != int(mapping.id or 0),
        )
        if (await session.execute(stmt)).scalar_one_or_none() is not None:
            raise UniqueConflictError(
                f"映射已存在：{new_platform}/{new_shop_id}/{new_sku_code}"
                "（同一 平台+店铺+SKU 编码 只能有一条有效映射，请先删除或改名现存映射）"
            )

    @staticmethod
    async def _flush_or_unique_conflict(session: Any, *, hint: str = "") -> None:
        """★ 写入边界：把唯一约束冲突翻译成明确的业务错误（409 / 1006）。

        ★★ 为什么必须在服务层就拦一次（而不是只靠全局异常处理器）★★
            `one_to_many` / `duplicate_item` 两类冲突的**唯一**表现形式就是撞键
            （读时扫描 SQL 因部分唯一索引恒为空，已按 QA-01/QA-02 移除）。
            在服务层翻译可以带上**业务上下文**（哪个店铺 SKU 撞了），
            全局 `IntegrityError` 处理器只是兜底第二道网。

        Raises:
            UniqueConflictError: 409 / 1006，文案可直接展示给运营。
        """
        try:
            await session.flush()
        except IntegrityError as exc:
            message = describe_integrity_error(exc)
            if hint:
                message = f"{message}（冲突键：{hint}）"
            raise UniqueConflictError(message) from exc

    @staticmethod
    async def _resolve_spec_signature(
        session: Any, *, source_sku_id: int | None, source_sku_code: str | None
    ) -> str:
        """解析规格指纹：优先用货源 SKU 的已算指纹，缺失则现算。"""
        if source_sku_id is not None:
            stmt = select(SourceSku).where(SourceSku.id == int(source_sku_id))
            sku = (await session.execute(stmt)).scalars().first()
            if sku is not None:
                return sku.spec_signature or spec_signature(sku.spec_json)
        if source_sku_code:  # 仅有编码时无法算指纹，留空（不做误判）
            return ""
        return ""

    @staticmethod
    async def attach_assets_count(session: Any, source_product_id: int) -> int:
        """取货源商品的素材数量（详情返回体用）。"""
        stmt = select(func.count()).select_from(Asset).where(
            Asset.source_product_id == int(source_product_id), Asset.is_deleted.is_(False)
        )
        return int((await session.execute(stmt)).scalar_one() or 0)
