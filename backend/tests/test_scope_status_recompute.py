"""★ 启动期重算 `scope_check_status`：修掉「声明合法却亮红」的不一致。

背景（team-lead 决策 2 批准修复）：
    `scope_check_status` 是 `declared_scopes` 的**派生值**，但历史上只在「保存配置」
    时写入一次。于是运营把越权 scope 改回合法后，状态仍停在 rejected ——
    **页面/顶栏照旧亮红**。这比"亮红没人看"更危险：它会训练运营忽略告警。

    修复：`bootstrap_fulfillment_adapters()` 在启动期按声明**重算一次**。

★ 这条用例最关键的一条断言是「重算**不得**写审计」：
    顶栏红点计的是 `permission_change` 且 `is_handled=0` 的审计条数。
    重算时若顺手写一条审计，**每次重启都会点亮一次红点** ——
    那才是真正的误报，等于用一个 bug 换另一个 bug。
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from app.models.enums import AuditActionType, ScopeCheckStatus
from app.models.order import FulfillmentAdapterConfig
from app.models.system import AuditLog
from app.services.audit_service import AuditService
from app.services.bootstrap import bootstrap_fulfillment_adapters

pytestmark = pytest.mark.asyncio


async def _set_dirty(session: Any, name: str = "miaoshou") -> FulfillmentAdapterConfig:
    """造一条脏数据：声明**合法** scope，但状态是 rejected（历史上遗留的不一致）。"""
    row = (
        await session.execute(
            select(FulfillmentAdapterConfig).where(FulfillmentAdapterConfig.adapter_name == name)
        )
    ).scalars().first()
    assert row is not None, "前置条件：bootstrap 已种入 miaoshou 行"

    row.declared_scopes_json = ["order.read", "logistics.write"]  # ★ 合法
    row.scope_check_status = ScopeCheckStatus.REJECTED.value       # ★ 但状态是脏的
    row.scope_check_message = "声明了越权 scope：item.write"
    await session.commit()
    return row


async def test_bootstrap_corrects_stale_rejected_status(session: Any) -> None:
    """★ 声明合法 + 状态 rejected 的脏数据，启动 bootstrap 后必须被纠正为 passed。"""
    row = await _set_dirty(session)

    result = await bootstrap_fulfillment_adapters(session, commit=True)

    await session.refresh(row)
    assert row.scope_check_status == ScopeCheckStatus.PASSED.value, (
        f"声明合法却仍是 {row.scope_check_status}（{row.scope_check_message}）—— "
        "运营把越权 scope 改回合法后仍会误亮红灯"
    )
    assert row.adapter_name in result["recomputed"], f"应记录被纠正的适配器：{result}"


async def test_recompute_does_not_light_the_red_dot(session: Any) -> None:
    """★ 重算**不得**写 `permission_change` 审计 —— 否则每次重启都会点亮红点。"""
    await _set_dirty(session)

    before = await AuditService.count_unhandled_violations(session)
    await bootstrap_fulfillment_adapters(session, commit=True)
    after = await AuditService.count_unhandled_violations(session)

    assert after == before, f"启动期重算不得新增越权告警：{before} → {after}"

    # 顺带确认：重算没有往审计表里写任何 permission_change 记录
    logs = (
        await session.execute(
            select(AuditLog).where(
                AuditLog.action_type == AuditActionType.PERMISSION_CHANGE.value,
                AuditLog.remark.like("%启动期%"),
            )
        )
    ).scalars().all()
    assert not logs, "重算不留审计（红点只应来自真实越权，不是来自重启）"


async def test_recompute_keeps_real_violation_red(session: Any) -> None:
    """★ 反向验证：声明**真**越权的行，重算后必须**保持** rejected（该亮的还得亮）。"""
    row = (
        await session.execute(
            select(FulfillmentAdapterConfig).where(FulfillmentAdapterConfig.adapter_name == "yitao")
        )
    ).scalars().first()
    assert row is not None

    row.declared_scopes_json = ["order.read", "item.write"]  # ★ 真越权
    row.scope_check_status = ScopeCheckStatus.PASSED.value   # ★ 状态是脏的（漏报）
    await session.commit()

    result = await bootstrap_fulfillment_adapters(session, commit=True)

    await session.refresh(row)
    assert row.scope_check_status == ScopeCheckStatus.REJECTED.value, (
        "真越权却显示 passed —— 重算把保护抹掉了，比不改更危险"
    )
    assert "item.write" in (row.scope_check_message or ""), (
        f"被拒原因必须点名越权 scope：{row.scope_check_message}"
    )
    assert "yitao" in result["recomputed"]
