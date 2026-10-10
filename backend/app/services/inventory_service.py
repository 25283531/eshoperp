"""库存与价格服务（§5.5.11、§6.4）。

================================================================================
★ ★ ★ 红线 R2 / INV-P0-02：下架只能由本服务发起 ★ ★ ★
================================================================================
库存归零 / 成本涨价触发的自动下架，**必须**经 `ListingService.offline()`
（它是 `ListingAdapter.offline()` 的唯一调用点）。
本服务**不得**自己调适配器下架方法 —— 第三方更不得直写店铺。

链路（§6.4）：
    ERP 内部事件（库存快照 / 价格快照）
        → InventoryService 判定阈值
        → ListingService.offline()  ← 唯一出口
        → ListingAdapter.offline()

★ 附录 A 第 18 条：本服务只处理**当前**商品与映射的成本，
  历史订单利润核算**不在**本模块（见 `OrderService.profit_cents`，只读快照）。
================================================================================
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from app.core.errors import BusinessError, ErrorCode, NotFoundError
from app.core.logging import get_logger, get_trace_id
from app.models.enums import (
    AuditActionType,
    AuditObjectType,
    InventoryChangeType,
    InventorySource,
    ListingProductStatus,
    SettingKey,
)
from app.models.inventory import InventorySnapshot, PriceSnapshot
from app.models.listing import ListingProduct, ListingSku
from app.models.mapping import SkuMapping
from app.models.source import SourceProduct, SourceSku
from app.models.system import SystemSetting
from app.schemas.inventory import (
    AutoOfflineRecordVo,
    InventoryAlertVo,
    InventoryConfigVo,
)
from app.services.audit_service import AuditService
from app.services.listing_service import ListingService
from app.services.mapping_validator import MappingValidator
from app.utils.csvio import cents_to_yuan_str
from app.utils.kit import (
    SOURCE_PLATFORM_MANUAL,
    derive_source_platform,
    iso_utc,
    utc_now,
)

logger = get_logger(__name__)

__all__ = ["InventoryService"]


class InventoryService:
    """库存 / 价格监控与自动下架。"""

    # ==================================================================
    #  同步
    # ==================================================================

    @staticmethod
    async def sync(
        session: Any,
        *,
        source_sku_ids: list[int] | None = None,
        force: bool = False,
        operator: str = "system",
    ) -> dict[str, Any]:
        """★ 采集库存与成本快照，并触发告警 / 自动下架。

        Returns:
            `{"task_record_id": int|None, "snapshot_count": int, "alerts": int, "auto_offline": int}`。
        """
        stmt = select(SourceSku).where(SourceSku.is_deleted.is_(False))
        if source_sku_ids:
            stmt = stmt.where(SourceSku.id.in_([int(i) for i in source_sku_ids]))
        skus = (await session.execute(stmt)).scalars().all()

        snapshot_count = 0

        # ★ v1.14 §5.8：快照来源必须**如实**标注，否则下游的判定键形同虚设。
        #   手工 / CSV 录入的商品**没有**可轮询的上游（1688 采集才是 erp_poll 的对象），
        #   把它们的库存也标成 `erp_poll` 等于替数据伪造"我是自动同步来的"授权书 ——
        #   这一步若不改，下游只对 `inventory_snapshot.source` 做门槛判定会是**空转**：
        #   每个手工 SKU 的最新快照都写着 erp_poll，照样被批量下架。
        #   因此这里按「这个数到底是怎么进系统的」逐 SKU 取值：
        #       * 手工 / CSV 录入的商品 → `manual_import`
        #       * 其余（1688 采集等有真实上游的）→ `erp_poll`
        platform_by_product = await InventoryService._source_platform_by_product(
            session, [int(s.source_product_id) for s in skus]
        )

        for sku in skus:
            # ★ 查不到归属商品时**默认按手工处理**（fail-close）：
            #   这个 SKU 的库存来源无从证明是自动同步的，而本标注下游会用于
            #   「是否允许自动下架」这一破坏性动作的门槛 —— 未知必须落到不可信一侧，
            #   与 `_snapshot_is_auto` 中「无快照 → False」是同一条原则。
            #   若默认取 1688，数据异常时会变成「自动下架」的放行口。
            platform = platform_by_product.get(int(sku.source_product_id), SOURCE_PLATFORM_MANUAL)
            snapshot_source = (
                InventorySource.MANUAL_IMPORT.value
                if platform == SOURCE_PLATFORM_MANUAL
                else InventorySource.ERP_POLL.value
            )
            session.add(
                InventorySnapshot(
                    source_sku_id=int(sku.id),
                    stock_qty=int(sku.stock_qty or 0),
                    source=snapshot_source,
                    collected_at=utc_now(),
                )
            )
            prev = (
                await session.execute(
                    select(PriceSnapshot)
                    .where(PriceSnapshot.source_sku_id == sku.id)
                    .order_by(PriceSnapshot.id.desc())
                    .limit(1)
                )
            ).scalars().first()
            prev_cents = int(prev.cost_price_cents or 0) if prev else None
            current = int(sku.cost_price_cents or 0)
            change_rate = None
            if prev_cents and prev_cents > 0 and current != prev_cents:
                change_rate = round((current - prev_cents) / prev_cents, 4)
            session.add(
                PriceSnapshot(
                    source_sku_id=int(sku.id),
                    cost_price_cents=current,
                    prev_price_cents=prev_cents,
                    change_rate=change_rate,
                    collected_at=utc_now(),
                )
            )
            snapshot_count += 1
        await session.flush()

        # ★ 真源变动 → 镜像同步（人工覆盖的不静默覆盖，走「成本待确认」工单）
        from app.services.mapping_service import MappingService

        await MappingService.sync_cost_from_source(session, source_sku_ids=source_sku_ids, operator=operator)

        alerts = await InventoryService._detect_alerts(session)
        auto_offline = await InventoryService._apply_actions(session, alerts, operator=operator)
        await session.flush()

        task_record_id = None
        try:
            from app.models.enums import TaskType

            from app.tasks.runner import get_task_runner

            task_record_id = await get_task_runner().submit(
                task_type=TaskType.INVENTORY_SYNC.value,
                payload={"source_sku_ids": source_sku_ids or [], "snapshots": snapshot_count},
                task_key=f"inventory_sync:{iso_utc(utc_now())[:13]}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("inventory_sync_task_record_failed", error=str(exc))

        return {
            "task_record_id": task_record_id,
            "snapshot_count": snapshot_count,
            "alerts": len(alerts),
            "auto_offline": auto_offline,
        }

    # ==================================================================
    #  告警检测
    # ==================================================================

    @staticmethod
    async def _detect_alerts(session: Any) -> list[InventoryAlertVo]:
        """检测缺货与涨价告警。"""
        config = await InventoryService.get_config(session)
        threshold = float(config.price_increase_threshold or 0.10)

        alerts: list[InventoryAlertVo] = []

        # ① 缺货：最新库存快照 = 0
        #
        # ★★ `ALERT_SCAN_LIMIT` 是**全局上限，不是按 SKU 去重后的上限** ★★
        #    去重（`seen_skus`）发生在取数**之后**，所以同一 SKU 的多条历史快照会先占掉名额。
        #    后果：同时缺货的 SKU 超过本上限时，超出部分**不产生告警、不参与自动下架**。
        #    本轮先消除"静默"（触及上限即告警），**不**重写查询 —— 完整修法是：
        #    先按 `source_sku_id` 分组取各自最新快照，再判断该快照是否为 0。
        #    在完整修复前，任何人不得把这个常数当成"够用的经验值"顺手调大。
        zero_rows = (
            await session.execute(
                select(InventorySnapshot)
                .where(InventorySnapshot.stock_qty <= 0)
                .order_by(InventorySnapshot.collected_at.desc())
                .limit(InventoryService.ALERT_SCAN_LIMIT)
            )
        ).scalars().all()
        if len(zero_rows) >= InventoryService.ALERT_SCAN_LIMIT:
            logger.warning(
                "inventory_alert_scan_truncated",
                alert_type="out_of_stock",
                limit=InventoryService.ALERT_SCAN_LIMIT,
                scanned=len(zero_rows),
                hint=(
                    "本轮检测结果可能不完整：超出上限的部分不会告警、也不会参与自动下架。"
                    "完整修法见 _detect_alerts 内注释（先按 source_sku_id 取各自最新快照再判 0）"
                ),
            )
        seen_skus: set[int] = set()
        for snap in zero_rows:
            if snap.source_sku_id in seen_skus:
                continue
            seen_skus.add(int(snap.source_sku_id))
            sku = (
                await session.execute(select(SourceSku).where(SourceSku.id == snap.source_sku_id))
            ).scalars().first()
            title = await InventoryService._product_title(session, sku.source_product_id if sku else None)
            alerts.append(
                InventoryAlertVo(
                    id=int(snap.id),
                    source_sku_id=int(snap.source_sku_id),
                    source_sku_name=sku.sku_code_1688 if sku else None,
                    source_product_title=title,
                    type="out_of_stock",
                    current_stock=0,
                    threshold="0",
                    suggested_action="offline" if config.out_of_stock_action == "offline" else "notify",
                    detected_at=iso_utc(snap.collected_at),
                )
            )

        # ② 涨价：环比涨幅 ≥ 阈值
        #   同样受 `ALERT_SCAN_LIMIT` 全局上限约束，语义见 ① 的注释。
        price_rows = (
            await session.execute(
                select(PriceSnapshot)
                .where(PriceSnapshot.change_rate.isnot(None))
                .order_by(PriceSnapshot.id.desc())
                .limit(InventoryService.ALERT_SCAN_LIMIT)
            )
        ).scalars().all()
        if len(price_rows) >= InventoryService.ALERT_SCAN_LIMIT:
            logger.warning(
                "inventory_alert_scan_truncated",
                alert_type="price_increase",
                limit=InventoryService.ALERT_SCAN_LIMIT,
                scanned=len(price_rows),
                hint=(
                    "本轮检测结果可能不完整：超出上限的部分不会告警、也不会参与自动下架。"
                    "完整修法见 _detect_alerts 内注释（先按 source_sku_id 取各自最新快照再判 0）"
                ),
            )
        for snap in price_rows:
            rate = float(snap.change_rate_float)
            if rate < threshold:
                continue
            sku = (
                await session.execute(select(SourceSku).where(SourceSku.id == snap.source_sku_id))
            ).scalars().first()
            title = await InventoryService._product_title(session, sku.source_product_id if sku else None)
            alerts.append(
                InventoryAlertVo(
                    id=int(snap.id),
                    source_sku_id=int(snap.source_sku_id),
                    source_sku_name=sku.sku_code_1688 if sku else None,
                    source_product_title=title,
                    type="price_increase",
                    current_cost=cents_to_yuan_str(snap.cost_price_cents),
                    prev_cost=cents_to_yuan_str(snap.prev_price_cents),
                    change_rate=f"{rate:.4f}",
                    threshold=f"{threshold:.2f}",
                    suggested_action="offline" if config.price_increase_action == "offline" else "notify",
                    detected_at=iso_utc(snap.collected_at),
                )
            )

        # ③ 回填数据源标记（★ UI 的「数据源 + 会不会自动下架」来自这里，ARCH §5.8 / F11）
        await InventoryService._decorate_alerts_with_source(session, alerts)
        return alerts

    @staticmethod
    async def _decorate_alerts_with_source(
        session: Any, alerts: list[InventoryAlertVo]
    ) -> list[InventoryAlertVo]:
        """给告警回填 `data_source`、`auto_offline_allowed`、`listing_product_ids`（各一次批量查询）。

        ★ `data_source` / `auto_offline_allowed` 两个字段都由 `inventory_snapshot.source`
          推出，与 `_apply_actions` 的裁决**同源同键**，UI 展示的结论与系统真正的行为
          不会两张皮。

        ★ `listing_product_ids` 由 `_candidate_products_by_source_sku()` **一次**批量预取
          （不是逐条查），供告警页直接发起人工下架 —— 见该方法的注释。
        """
        if not alerts:
            return alerts
        sku_ids = [int(a.source_sku_id) for a in alerts]
        sources = await InventoryService._latest_snapshot_sources(session, sku_ids)
        candidates = await InventoryService._candidate_products_by_source_sku(session, sku_ids)
        for alert in alerts:
            alert.listing_product_ids = list(candidates.get(int(alert.source_sku_id), []))
            snapshot_source = sources.get(int(alert.source_sku_id), "")
            alert.data_source = snapshot_source or InventoryService.UNKNOWN_SOURCE_LABEL
            # ★ 与 `_apply_actions` 的裁决规则逐字一致：缺货看数据源，涨价不看
            #   （UI 给出的结论必须等于系统真正会做的事，否则就是新的误导源）。
            if alert.type == "out_of_stock":
                alert.auto_offline_allowed = (
                    alert.suggested_action == "offline"
                    and str(snapshot_source).strip().lower() in InventoryService.AUTO_OFFLINE_SOURCES
                )
            else:
                alert.auto_offline_allowed = alert.suggested_action == "offline"
        return alerts

    # ==================================================================
    #  ★ 自动下架（唯一出口：ListingService.offline）
    # ==================================================================

    # ★ 单次告警检测的扫描上限（**全局上限**，不是按 SKU 去重后的上限）。
    #   触及即打 `inventory_alert_scan_truncated` 告警，避免"检测被截断却无人知晓"。
    #   完整修法（下一迭代）：先按 `source_sku_id` 分组取各自最新快照，再判断是否为 0。
    ALERT_SCAN_LIMIT: int = 200

    # 可信到可以触发**自动下架**的快照来源：机器自动生成，不含人工录入。
    # ★ 判定键是 `inventory_snapshot.source`（库存数据自身的来源），
    #   **不是** `source_platform`（商品来源）——见 ARCH §5.8 与铁律 R4 的相容性论证：
    #   R4 禁止的是「能力可用性」分支，本处是「破坏性动作门槛」分支。
    AUTO_OFFLINE_SOURCES: frozenset[str] = frozenset(
        {
            InventorySource.ERP_POLL.value,
            InventorySource.THIRD_PARTY_PUSH.value,
        }
    )
    # 无快照时的展示标签（= unknown）
    UNKNOWN_SOURCE_LABEL: str = "unknown"

    @staticmethod
    async def _snapshot_is_auto(session: Any, source_sku_id: int) -> bool:
        """★ 缺货自动下架的前置条件：**最近一次库存快照来自自动同步**。

        ------------------------------------------------------------------
        判定键为什么是 `InventorySnapshot.source`
        ------------------------------------------------------------------
        ARCH §5.8 定稿：破坏性动作的触发门槛必须与**数据源可信度**匹配。
            - `erp_poll` / `third_party_push` → 机器同步来的 0 = 真缺货 → 允许下架；
            - `manual_import` / `manual_edit` → 人工一次性录入且不常更新的 0
              很可能只是"没填/填错" → **只告警 + 一键下架入口**；
            - 无快照（unknown）→ 保守优先，**不自动执行**。

        ★ 前置修复（本文件 `sync()`）：在此之前 `sync()` 对**所有** SKU 一律写
          `source="erp_poll"`，于是这个字段恒为同值 —— 拿它当门槛会**空转**
          （允许 erp_poll 等于没门槛；只允许 third_party_push 等于永远不下架）。
          `sync()` 现在按「这个库存数到底是怎么进系统的」如实标注：
          手工/CSV 录入的商品写 `manual_import`，1688 采集等有真实上游的写 `erp_poll`。
          **标签变诚实之后，本判定键才真正生效** —— 两者是同一个修复的两半。

        ★★ 铁律 R4 合规说明：判定键是库存快照自身的来源，`source_platform`
            只通过 `derive_source_platform()` 参与"写快照时如实标注"，
            **不出现在本分支里**；且本判定只抬高「是否自动下架」这一破坏性动作门槛，
            不影响「能否上架」这类能力可用性 —— 符合架构文档的通用判据。

        Args:
            session: 数据库会话。
            source_sku_id: 货源 SKU ID。

        Returns:
            True 表示允许对该 SKU 执行自动下架。
        """
        row = (
            await session.execute(
                select(InventorySnapshot.source)
                .where(InventorySnapshot.source_sku_id == int(source_sku_id))
                .order_by(InventorySnapshot.collected_at.desc(), InventorySnapshot.id.desc())
                .limit(1)
            )
        ).scalars().first()
        if row is None:
            return False  # ★ 无快照 = unknown：保守优先，不自动执行
        return str(row).strip().lower() in InventoryService.AUTO_OFFLINE_SOURCES

    @staticmethod
    async def _source_platform_by_product(
        session: Any, source_product_ids: list[int]
    ) -> dict[int, str]:
        """批量取货源商品的来源平台标记（`manual` / `alibaba1688`）。

        ★ 表上没有 `source_platform` 列（项目约束：不改表结构），统一走
          `derive_source_platform()` —— 与 `SourceProductVo` / 列表筛选同一套口径：
          判定优先级为 `params_json["source_platform"]` → `MANUAL-` ID 前缀 → 1688。

        ★ 这个 helper **只服务于写快照时如实标注数据来源**（见 `sync()`），
          不参与任何能力可用性分支（铁律 R4）。
        """
        ids = sorted({int(i) for i in source_product_ids if i is not None})
        if not ids:
            return {}
        rows = (
            await session.execute(
                select(SourceProduct.id, SourceProduct.product_1688_id, SourceProduct.params_json).where(
                    SourceProduct.id.in_(ids)
                )
            )
        ).all()
        return {int(r[0]): derive_source_platform(r[1], r[2]) for r in rows}

    @staticmethod
    async def _latest_snapshot_sources(session: Any, source_sku_ids: list[int]) -> dict[int, str]:
        """批量取每个 SKU **最近一次**库存快照的来源；无快照返回空串。

        为什么按 `collected_at desc, id desc` 取第一条而不是 aggregate：
        同一 SKU 同一刻可能落下多条（并发同步），要看**最新那条**的标签，
        而 SQL 的 `group by` 无法在并列时稳定挑出 id 最大的一行。

        ★ 供告警填充 `data_source` / `auto_offline_allowed`，使 UI 能显示
          「数据源 + 是否会自动下架」（ARCH §5.8 / 前端约束 F11）。
        """
        ids = sorted({int(i) for i in source_sku_ids if i is not None})
        result: dict[int, str] = {key: "" for key in ids}
        if not ids:
            return result
        rows = (
            await session.execute(
                select(InventorySnapshot.source_sku_id, InventorySnapshot.source)
                .where(InventorySnapshot.source_sku_id.in_(ids))
                .order_by(InventorySnapshot.collected_at.desc(), InventorySnapshot.id.desc())
            )
        ).all()
        for sku_id, source in rows:
            key = int(sku_id)
            if not result.get(key):  # 按时间倒序：首次出现的就是该 SKU 的最新快照
                result[key] = str(source or "")
        return result

    @staticmethod
    async def _candidate_products_by_source_sku(
        session: Any, source_sku_ids: list[int]
    ) -> dict[int, list[int]]:
        """★ 批量预取「可人工下架」的平台商品 ID：`{source_sku_id: [listing_product_id, ...]}`。

        供 `InventoryAlertVo.listing_product_ids` 回填，使告警页能直接发起人工下架。

        ------------------------------------------------------------------
        为什么必须一次批量查，不能在循环里逐条调 `_products_by_source_sku()`
        ------------------------------------------------------------------
        `_products_by_source_sku()` 内部是 **3 次查询**（映射 id → 商品 id → 商品对象）。
        逐条调用就是 N+1：`ALERT_SCAN_LIMIT=200` 的告警扫描上限下，最坏放大到上千次查询。
        这里改为一次 `IN (...)` + 内存聚合（写法同 `_latest_snapshot_sources()`）。

        ------------------------------------------------------------------
        为什么不直接复用 `_products_by_source_sku()` 来填这个字段
        ------------------------------------------------------------------
        它过滤的是 `status == "on_sale"`（服务于**自动**下架：只下在售的），
        与本次口径「排除 `off_shelf`」不同。用它填会把「只剩 publishing/failed 商品的告警」
        显示成「无关联商品」，运营看到「建议下架」却没有下架入口 —— 新的误导源。

        ------------------------------------------------------------------
        过滤口径（逐条对应 `offline()` 的真实前置）
        ------------------------------------------------------------------
        * `sku_mapping`：排除 `is_deleted` / `is_mock`，且 `listing_product_id` 非空；
        * `listing_product`：排除 `is_deleted` / `is_mock`；
        * `status != 'off_shelf'` —— `ListingService.offline()` 对已下架商品抛
          `StateConflictError`（HTTP 409 / code 1005，不幂等），放进列表一点就报错。
          注意是「排除 off_shelf」而**不是**「只留 on_sale」：`publishing` / `failed`
          都能正常下架，只留 on_sale 会漏掉真实可下的商品。

        Args:
            session: 数据库会话。
            source_sku_ids: 货源 SKU ID 列表。

        Returns:
            每个 SKU 对应的**去重后**平台商品 ID 列表（升序）；无关联则为空列表。
        """
        ids = sorted({int(i) for i in source_sku_ids if i is not None})
        if not ids:
            # ★ 空输入直接返回，不发 SQL：`IN ()` 在部分方言下是语法错误。
            return {}
        rows = (
            await session.execute(
                select(SkuMapping.source_sku_id, ListingProduct.id)
                .join(ListingProduct, ListingProduct.id == SkuMapping.listing_product_id)
                .where(
                    SkuMapping.source_sku_id.in_(ids),
                    SkuMapping.listing_product_id.isnot(None),
                    SkuMapping.is_deleted.is_(False),
                    SkuMapping.is_mock.is_(False),
                    ListingProduct.is_deleted.is_(False),
                    ListingProduct.is_mock.is_(False),
                    ListingProduct.status != ListingProductStatus.OFF_SHELF.value,
                )
            )
        ).all()
        # 同一个 listing_product 可能经多条 mapping 命中（一货多铺 / 一商品多 SKU），需去重
        buckets: dict[int, set[int]] = {key: set() for key in ids}
        for source_sku_id, product_id in rows:
            key = int(source_sku_id)
            if product_id is not None:
                buckets.setdefault(key, set()).add(int(product_id))
        return {key: sorted(value) for key, value in buckets.items()}

    @staticmethod
    async def _apply_actions(
        session: Any, alerts: list[InventoryAlertVo], *, operator: str = "system"
    ) -> int:
        """按配置执行处置：`offline` 的走 `ListingService.offline()`。

        ★ 红线 R2：下架**只能**经 `ListingService.offline()`，本方法不得自调适配器。

        ★ **缺货类**自动下架有前置条件（见 `_snapshot_is_auto`）：
          最近一次库存快照**不是**自动同步来源（`manual_import` / `manual_edit` /
          无快照）的 SKU —— 典型是手工录入 / CSV 导入且从不自动更新库存的商品 ——
          **不会被自动下架**，只告警 + 一键下架入口。
          未通过门槛的条数计入审计 `skipped_untrusted_source`。

        ★ 涨价类告警**不受**此门槛限制（`PriceSnapshot` 是另一份数据，
          且把运营显式配置的下架静默降级成告警本身就是静默失效）——
          完整理由见下方代码块内注释与 `tests/test_auto_offline_gaurd.py` [G]。
        """
        executed = 0
        skipped_untrusted = 0
        for alert in alerts:
            if alert.suggested_action != "offline":
                continue
            listing_products = await InventoryService._products_by_source_sku(
                session, alert.source_sku_id
            )
            if not listing_products:
                continue
            # ★★ 数据源前置门槛 ★★
            #   适用范围：**只给缺货类（out_of_stock）告警**（§5.8 的 INV-P0-03）。
            #
            #   ★ 为什么**不**顺手把涨价类告警也挡掉（曾写错过一次，见 `tests/test_auto_offline_gaurd.py` [G]）：
            #     ① 涨价告警的判据是 `PriceSnapshot`（prev → current 环比），与 `InventorySnapshot`
            #        **不是同一份数据** —— 拿库存快照的来源去否决价格结论，是把两个数据源搅在一起；
            #     ② 运营显式把 `price_increase_action` 配成 `offline`，是在说「别让我亏本卖」。
            #        若被这道门槛悄无声息地降级成只告警，等于用「防误下架」悄悄换掉「防亏本卖」，
            #        而这正是本仓反复提防的**静默失效**；
            #     ③ §5.8 明确把"手工商品失去的自动防护"限定在**超卖/缺货**这一类。
            #   ⇒ 结论：门槛只管缺货；涨价保持原有行为。
            if alert.type == "out_of_stock" and not await InventoryService._snapshot_is_auto(
                session, alert.source_sku_id
            ):
                skipped_untrusted += 1
                snapshot_source = (
                    await InventoryService._latest_snapshot_sources(session, [int(alert.source_sku_id)])
                ).get(int(alert.source_sku_id), "")
                logger.warning(
                    "auto_offline_skipped_untrusted_source",
                    source_sku_id=alert.source_sku_id,
                    source_sku_name=alert.source_sku_name,
                    alert_type=alert.type,
                    snapshot_source=snapshot_source or "none",
                    listing_product_ids=[int(p.id) for p in listing_products],
                    manual_offline_entry="POST /api/v1/listing-products/{id}/offline",
                    reason=(
                        "最近一次库存快照非自动同步来源（手工维护 / 无快照），"
                        "按 §5.8 不自动下架 —— 只告警，由人工确认后一键下架"
                    ),
                )
                continue
            for product in listing_products:
                try:
                    await ListingService.offline(
                        session,
                        int(product.id),
                        reason=f"{'缺货' if alert.type == 'out_of_stock' else '成本涨价'}自动下架"
                        f"（{alert.source_sku_name or alert.source_sku_id}）",
                        operator=operator,
                        trigger=f"auto_{alert.type}",
                    )
                    executed += 1
                except Exception as exc:  # noqa: BLE001  单条失败不影响其他
                    logger.warning(
                        "auto_offline_failed",
                        listing_product_id=product.id,
                        source_sku_id=alert.source_sku_id,
                        error=str(exc),
                    )
        if executed or skipped_untrusted:
            await AuditService.write(
                session,
                action_type=AuditActionType.OFFLINE.value,
                object_type=AuditObjectType.LISTING_PRODUCT.value,
                object_id="auto_offline",
                operator=operator,
                new_value={
                    "count": executed,
                    "alerts": len(alerts),
                    "skipped_untrusted_source": skipped_untrusted,
                },
                trace_id=get_trace_id(),
                remark=(
                    f"库存/价格告警触发自动下架 {executed} 个商品"
                    + (
                        f"；{skipped_untrusted} 条告警因「最近库存快照非自动同步来源」"
                        f"只告警不下架（手工维护 / 无快照）"
                        if skipped_untrusted
                        else ""
                    )
                ),
            )
        return executed

    @staticmethod
    async def _products_by_source_sku(session: Any, source_sku_id: int) -> list[ListingProduct]:
        """按货源 SKU 反查在售的平台商品（经 `sku_mapping`）。"""
        mapping_ids = [
            int(row[0])
            for row in (
                await session.execute(
                    select(SkuMapping.id).where(
                        SkuMapping.source_sku_id == int(source_sku_id),
                        SkuMapping.is_deleted.is_(False),
                        SkuMapping.is_mock.is_(False),
                    )
                )
            ).all()
        ]
        if not mapping_ids:
            return []
        product_ids = [
            int(row[0])
            for row in (
                await session.execute(
                    select(SkuMapping.listing_product_id).where(
                        SkuMapping.id.in_(mapping_ids), SkuMapping.listing_product_id.isnot(None)
                    )
                )
            ).all()
        ]
        if not product_ids:
            return []
        rows = (
            await session.execute(
                select(ListingProduct).where(
                    ListingProduct.id.in_(product_ids),
                    ListingProduct.is_deleted.is_(False),
                    ListingProduct.status == "on_sale",
                )
            )
        ).scalars().all()
        return list(rows)

    @staticmethod
    async def _product_title(session: Any, source_product_id: int | None) -> str | None:
        """取货源商品标题。"""
        if source_product_id is None:
            return None
        product = (
            await session.execute(select(SourceProduct).where(SourceProduct.id == int(source_product_id)))
        ).scalars().first()
        return product.title if product else None

    # ==================================================================
    #  查询
    # ==================================================================

    @staticmethod
    async def list_snapshots(
        session: Any,
        *,
        source_sku_id: int | None = None,
        source_product_id: int | None = None,
        stock_zero_only: bool = False,
        collected_from: Any = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[InventorySnapshot], dict[int, str], dict[int, str], int]:
        """分页查询库存快照。"""
        stmt = select(InventorySnapshot)
        if source_sku_id is not None:
            stmt = stmt.where(InventorySnapshot.source_sku_id == int(source_sku_id))
        if source_product_id is not None:
            sku_ids = [
                int(row[0])
                for row in (
                    await session.execute(
                        select(SourceSku.id).where(SourceSku.source_product_id == int(source_product_id))
                    )
                ).all()
            ]
            stmt = stmt.where(InventorySnapshot.source_sku_id.in_(sku_ids) if sku_ids else False)
        if stock_zero_only:
            stmt = stmt.where(InventorySnapshot.stock_qty <= 0)
        if collected_from is not None:
            stmt = stmt.where(InventorySnapshot.collected_at >= collected_from)

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await session.execute(count_stmt)).scalar_one() or 0)
        rows = (
            (
                await session.execute(
                    stmt.order_by(InventorySnapshot.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        names, titles = await InventoryService._sku_meta(session, [int(r.source_sku_id) for r in rows])
        return list(rows), names, titles, total

    @staticmethod
    async def list_price_snapshots(
        session: Any,
        *,
        source_sku_id: int | None = None,
        over_threshold_only: bool = False,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[PriceSnapshot], dict[int, str], dict[int, str], int]:
        """分页查询成本价快照。"""
        stmt = select(PriceSnapshot)
        if source_sku_id is not None:
            stmt = stmt.where(PriceSnapshot.source_sku_id == int(source_sku_id))
        if over_threshold_only:
            config = await InventoryService.get_config(session)
            threshold = float(config.price_increase_threshold or 0.10)
            stmt = stmt.where(PriceSnapshot.change_rate >= threshold)

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await session.execute(count_stmt)).scalar_one() or 0)
        rows = (
            (
                await session.execute(
                    stmt.order_by(PriceSnapshot.id.desc()).offset((page - 1) * page_size).limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        names, titles = await InventoryService._sku_meta(session, [int(r.source_sku_id) for r in rows])
        return list(rows), names, titles, total

    @staticmethod
    async def _sku_meta(session: Any, sku_ids: list[int]) -> tuple[dict[int, str], dict[int, str]]:
        """批量取 SKU 编码与货源商品标题。"""
        if not sku_ids:
            return {}, {}
        skus = (
            await session.execute(select(SourceSku).where(SourceSku.id.in_(sorted(set(sku_ids)))))
        ).scalars().all()
        names = {int(s.id): s.sku_code_1688 for s in skus}
        product_ids = sorted({int(s.source_product_id) for s in skus})
        titles: dict[int, str] = {}
        if product_ids:
            products = (
                (await session.execute(select(SourceProduct).where(SourceProduct.id.in_(product_ids))))
                .scalars()
                .all()
            )
            product_titles = {int(p.id): p.title for p in products}
            titles = {int(s.id): product_titles.get(int(s.source_product_id), "") for s in skus}
        return names, titles

    @staticmethod
    async def list_alerts(
        session: Any,
        *,
        alert_type: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[InventoryAlertVo], int]:
        """告警列表（实时计算，不落表）。"""
        alerts = await InventoryService._detect_alerts(session)
        if alert_type:
            alerts = [a for a in alerts if a.type == alert_type]
        total = len(alerts)
        start = (page - 1) * page_size
        return alerts[start : start + page_size], total

    @staticmethod
    async def list_auto_offline_records(
        session: Any, *, page: int = 1, page_size: int = 20
    ) -> tuple[list[AutoOfflineRecordVo], int]:
        """自动下架记录：从审计日志中筛 `offline` + `auto_` 触发源。

        ★ 不新建表：下架记录本身就是审计事件，复用 `audit_log` 避免双份真相。
        """
        from app.models.system import AuditLog

        stmt = select(AuditLog).where(
            AuditLog.action_type == AuditActionType.OFFLINE.value,
            AuditLog.remark.like("%自动下架%"),
        )
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await session.execute(count_stmt)).scalar_one() or 0)
        rows = (
            (
                await session.execute(
                    stmt.order_by(AuditLog.id.desc()).offset((page - 1) * page_size).limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        records = [
            AutoOfflineRecordVo(
                id=int(row.id),
                listing_product_id=int(row.object_id) if str(row.object_id or "").isdigit() else 0,
                shop_item_id="",
                platform="",
                reason=row.remark or "",
                trigger_type="auto",
                executed_at=iso_utc(row.created_at),
                success=True,
                message=row.remark or "",
            )
            for row in rows
        ]
        return records, total

    # ==================================================================
    #  配置
    # ==================================================================

    @staticmethod
    async def get_config(session: Any) -> InventoryConfigVo:
        """读取库存配置。"""
        keys = [
            SettingKey.INVENTORY_POLL_INTERVAL_MIN.value,
            SettingKey.INVENTORY_PRICE_INCREASE_THRESHOLD.value,
            SettingKey.INVENTORY_OUT_OF_STOCK_ACTION.value,
            SettingKey.INVENTORY_PRICE_INCREASE_ACTION.value,
        ]
        rows = (
            (await session.execute(select(SystemSetting).where(SystemSetting.setting_key.in_(keys))))
            .scalars()
            .all()
        )
        found = {row.setting_key: (row.setting_value or "") for row in rows}
        return InventoryConfigVo(
            poll_interval_min=int(found.get(SettingKey.INVENTORY_POLL_INTERVAL_MIN.value, 30) or 30),
            price_increase_threshold=str(
                found.get(SettingKey.INVENTORY_PRICE_INCREASE_THRESHOLD.value, "0.10") or "0.10"
            ),
            out_of_stock_action=str(found.get(SettingKey.INVENTORY_OUT_OF_STOCK_ACTION.value, "offline") or "offline"),
            price_increase_action=str(
                found.get(SettingKey.INVENTORY_PRICE_INCREASE_ACTION.value, "notify_only") or "notify_only"
            ),
        )

    @staticmethod
    async def update_config(
        session: Any,
        *,
        poll_interval_min: int | None = None,
        price_increase_threshold: str | None = None,
        out_of_stock_action: str | None = None,
        price_increase_action: str | None = None,
        operator: str = "system",
    ) -> InventoryConfigVo:
        """更新库存配置（管理员）。"""
        pairs = {
            SettingKey.INVENTORY_POLL_INTERVAL_MIN.value: (
                str(int(poll_interval_min)) if poll_interval_min is not None else None
            ),
            SettingKey.INVENTORY_PRICE_INCREASE_THRESHOLD.value: price_increase_threshold,
            SettingKey.INVENTORY_OUT_OF_STOCK_ACTION.value: out_of_stock_action,
            SettingKey.INVENTORY_PRICE_INCREASE_ACTION.value: price_increase_action,
        }
        for key, value in pairs.items():
            if value is None:
                continue
            if key.endswith("_action") and value not in {"offline", "notify_only"}:
                raise BusinessError(f"{key} 只能是 offline 或 notify_only", code=ErrorCode.PARAM_ERROR)
            row = (
                await session.execute(select(SystemSetting).where(SystemSetting.setting_key == key))
            ).scalars().first()
            if row is None:
                session.add(
                    SystemSetting(
                        setting_key=key, setting_value=value, value_type="string", updated_by=operator
                    )
                )
            else:
                row.setting_value = value
                row.updated_by = operator
        await session.flush()
        await AuditService.write(
            session,
            action_type=AuditActionType.ADAPTER_SWITCH.value,
            object_type=AuditObjectType.SYSTEM_SETTING.value,
            object_id="inventory.config",
            operator=operator,
            new_value={k: v for k, v in pairs.items() if v is not None},
            trace_id=get_trace_id(),
            remark="更新库存与价格配置",
        )
        return await InventoryService.get_config(session)

    # ==================================================================
    #  变动推送（本地兜底即源头，无需推送）
    # ==================================================================

    @staticmethod
    async def push_change(
        session: Any,
        *,
        source_sku_code_1688: str,
        change_type: str = InventoryChangeType.STOCK.value,
        stock_qty: int | None = None,
        cost_cents: int | None = None,
        operator: str = "system",
    ) -> dict[str, Any]:
        """向第三方推送库存 / 价格变动（本地兜底为 no-op 成功）。"""
        from app.adapters.fulfillment.base import InventoryChangeEvent

        adapter_name = None
        try:
            from app.adapters.fulfillment.factory import get_active_adapter_name

            adapter_name = await get_active_adapter_name(session)
        except Exception:  # noqa: BLE001
            adapter_name = None

        from app.services.fulfillment_service import FulfillmentService

        adapter = await FulfillmentService.get_adapter(session, adapter_name=adapter_name, actor=operator)
        result = await adapter.invoke(
            "push_inventory_change",
            req=InventoryChangeEvent(
                source_sku_code_1688=source_sku_code_1688,
                stock_qty=stock_qty,
                cost_cents=cost_cents,
                change_type=change_type,
            ),
        )
        return {
            "pushed": bool(result and result.code == "OK"),
            "code": str(result.code) if result else "UNKNOWN",
            "message": result.message if result else "",
        }

    @staticmethod
    async def recompute_underwater_for_product(
        session: Any, product_id: int, sku_codes: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """供外部触发的倒挂重算入口（复用 `fill_price` 的同一机制）。"""
        conflicts = await MappingValidator.recompute_cost_underwater(
            session, listing_product_id=product_id, sku_codes=sku_codes
        )
        return [
            {"sku_code": c.shop_sku_code, "conflict_type": c.conflict_type, "level": c.level}
            for c in conflicts
        ]

    @staticmethod
    async def listing_sku_price(session: Any, listing_sku_id: int) -> int:
        """取平台 SKU 当前售价（分）。"""
        sku = (
            await session.execute(select(ListingSku).where(ListingSku.id == int(listing_sku_id)))
        ).scalars().first()
        if sku is None:
            raise NotFoundError(f"平台 SKU {listing_sku_id} 不存在")
        return int(sku.sale_price_cents or 0)
