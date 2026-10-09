"""采购下单任务处理器（`purchase_place`）。

★ 为什么必须有这个处理器
    `OrderService.place_purchase()` 此前**没有任何调用点**（全仓 grep 为零）：
    既没有 HTTP 入口，也没有定时任务入口。后果是订单流转停在 `matched`（已匹配），
    永远走不到 `purchased`（已下单）—— 履约主路径在半路断掉，
    而"用户买这套系统就是为了订单能自动往下走"。

    本处理器负责**批量**把「已匹配」的订单推进到「已下单」：
        * 开关：`order.auto_purchase_enabled`（默认 true，关掉则只能手工下单）；
        * 批量上限：`order.purchase_batch_limit`（默认 50，防瞬时打爆 1688）；
        * 单条失败**不得中断整批**（一条订单的适配器异常不该拖垮其它订单）。

Returns:
    `{"scanned": int, "placed": int, "failed": list, "disabled": bool}`。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.core.database import get_session_factory
from app.core.logging import get_logger
from app.models.enums import OrderFulfillmentStatus, SettingKey, TaskType
from app.models.order import Order
from app.models.system import SystemSetting
from app.services.order_service import OrderService
from app.tasks.registry import task_handler

logger = get_logger(__name__)

__all__ = ["purchase_place_handler"]

DEFAULT_BATCH_LIMIT = 50


async def _read_setting(session: Any, key: str, default: str) -> str:
    """读系统配置，缺失 / 异常时回退默认值（配置缺失不得阻断下单）。"""
    try:
        row = (
            await session.execute(select(SystemSetting).where(SystemSetting.setting_key == key))
        ).scalars().first()
        if row is None or row.setting_value is None:
            return default
        return str(row.setting_value).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("purchase_setting_read_failed", key=key, error=str(exc))
        return default


def _as_bool(value: str, *, default: bool = True) -> bool:
    """字符串 → bool（`true/1/yes/on` 为真）。"""
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    return default


@task_handler(TaskType.PURCHASE_PLACE.value)
async def purchase_place_handler(payload: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """批量给「已匹配」的订单下采购单。

    payload:
        order_ids: list[int]?  限定订单（为空则扫描全部已匹配订单）
        operator: str?         操作人
    """
    wanted_ids = [int(i) for i in (payload.get("order_ids") or []) if str(i).strip()]
    operator = str(payload.get("operator") or "system")

    factory = get_session_factory()
    async with factory() as session:
        enabled = _as_bool(
            await _read_setting(
                session, SettingKey.ORDER_AUTO_PURCHASE_ENABLED.value, "true"
            )
        )
        if not enabled:
            logger.info("purchase_place_disabled", reason="order.auto_purchase_enabled=false")
            return {"scanned": 0, "placed": 0, "failed": [], "disabled": True}

        limit_raw = await _read_setting(
            session, SettingKey.ORDER_PURCHASE_BATCH_LIMIT.value, str(DEFAULT_BATCH_LIMIT)
        )
        try:
            limit = max(int(limit_raw), 1)
        except (TypeError, ValueError):
            limit = DEFAULT_BATCH_LIMIT

        stmt = select(Order).where(
            Order.fulfillment_status == OrderFulfillmentStatus.MATCHED.value
        )
        if wanted_ids:
            stmt = stmt.where(Order.id.in_(wanted_ids))
        stmt = stmt.order_by(Order.id.asc()).limit(limit)
        orders = (await session.execute(stmt)).scalars().all()

        placed = 0
        failed: list[dict[str, Any]] = []
        for order in orders:
            try:
                purchase = await OrderService.place_purchase(
                    session, int(order.id), operator=operator
                )
                await session.commit()
                placed += 1
                logger.info(
                    "purchase_placed",
                    order_id=int(order.id),
                    purchase_status=str(getattr(purchase, "purchase_status", "")),
                )
            except Exception as exc:  # noqa: BLE001  ★ 单条失败不得中断整批
                await session.rollback()
                failed.append({"order_id": int(order.id), "reason": str(exc)[:200]})
                logger.warning(
                    "purchase_place_one_failed", order_id=int(order.id), error=str(exc)
                )

    result = {
        "scanned": len(orders),
        "placed": placed,
        "failed": failed,
        "disabled": False,
    }
    logger.info(
        "purchase_place_handler_done",
        task_id=getattr(ctx, "task_id", 0),
        scanned=result["scanned"],
        placed=placed,
        failed=len(failed),
    )
    return result
