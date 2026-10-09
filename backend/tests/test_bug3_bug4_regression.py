"""★ Bug 3 / Bug 4 回归锁（team-lead 指派，优先级高于 QA 探针类问题）。

Bug 3（系统级：履约链路全断）
    `FulfillmentAdapter.invoke()` 内部一律按 `capability.value` 取值，而 7 个业务调用点
    传的是字符串（`"fetch_orders"` / `"match_sku"` / `"place_purchase_order"` / …）→
    `AttributeError: 'str' object has no attribute 'value'` → 订单同步任务 3 次重试后 failed。
    修法：在 `invoke()` 入口用 `normalize_capability()` 做唯一一次归一化（同 normalize_platform 思路）。

Bug 4（★ 红线 R1：越权被拒但审计 0 条 → 安全功能静默失效）
    三个根因：① `audit_service.write(**payload)` 漏了位置参数 `session`；
    ② 与"独立会话兜底写 AuditLog"在同一个 try 里，异常把兜底也跳过；
    ③ `fulfillment_service` 传 `audit_service=AuditService` 类本身 → 每次都走死路。
    另修：`is_handled` 语义（通过=True / 拒绝=False），否则"通过"记录会把顶栏红点计数灌爆。

本文件把两条链路钉死：能力归一化、越权必留痕、留痕后顶栏计数随之变化、处置后回落、成对存在。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.adapters.fulfillment.base import FetchOrdersRequest, PurchaseRequest
from app.adapters.fulfillment.manifest import normalize_capability
from app.core.errors import BusinessError, ErrorCode
from app.models.enums import AuditActionType, Capability, ResultCode
from app.models.order import FulfillmentAdapterConfig
from app.services.audit_service import AuditService
from app.services.fulfillment_service import FulfillmentService
from app.services.system_service import SystemService


# ======================================================================
#  Bug 3：invoke() 能力归一化
# ======================================================================


def test_normalize_capability_basic() -> None:
    """归一化：枚举原样 / 字符串转枚举 / 非法值 None。"""
    assert normalize_capability(Capability.FETCH_ORDERS) is Capability.FETCH_ORDERS
    assert normalize_capability("fetch_orders") is Capability.FETCH_ORDERS
    assert normalize_capability("MATCH_SKU") is Capability.MATCH_SKU
    assert normalize_capability(None) is None
    assert normalize_capability("not_a_capability") is None


async def test_invoke_accepts_str_capability(session: object) -> None:
    """★ 回归：业务调用点传字符串能力名，不得再抛 AttributeError。"""
    adapter = await FulfillmentService.get_adapter(session, adapter_name="local_csv", actor="tester")
    result = await adapter.invoke(
        "fetch_orders",  # ★ 字符串，正是 7 个业务调用点的真实写法
        req=FetchOrdersRequest(shop_ids=[], updated_from=""),
    )
    assert result is not None, "invoke 必须返回结果信封，不得抛异常"
    assert result.code in {
        ResultCode.OK.value,
        ResultCode.DEGRADED.value,
        ResultCode.UNSUPPORTED.value,
    }, f"结果码异常：{result.code} / {result.message}"


async def test_invoke_accepts_enum_capability(session: object) -> None:
    """枚举入参同样可用（归一化对两种入参都成立）。"""
    adapter = await FulfillmentService.get_adapter(session, adapter_name="local_csv", actor="tester")
    result = await adapter.invoke(
        Capability.PLACE_PURCHASE_ORDER,
        req=PurchaseRequest(
            order_id=1,
            platform_order_no="T-REGRESSION-001",
            source_product_1688_id="1688-REGRESSION",
            source_sku_code_1688="1688-A",
            quantity=1,
        ),
    )
    assert result is not None
    assert result.code in {ResultCode.OK.value, ResultCode.DEGRADED.value, ResultCode.UNSUPPORTED.value}
    status = getattr(result.data, "status", None)
    if result.data is not None:
        assert status == "manual_pending", f"本地兜底必须停在 manual_pending，实际 {status}"


async def test_invoke_unknown_capability_returns_envelope(session: object) -> None:
    """未知能力名 → UNSUPPORTED 信封（绝不抛 AttributeError，红线 R3）。"""
    adapter = await FulfillmentService.get_adapter(session, adapter_name="local_csv", actor="tester")
    result = await adapter.invoke("no_such_capability")
    assert result is not None
    assert result.code == ResultCode.UNSUPPORTED.value
    assert "未知能力" in (result.message or "")


async def test_health_check_is_not_a_capability(session: object) -> None:
    """`health_check` 不在 8 项履约能力内，连通性自检改直调方法，结果必须是真实信封。"""
    outcome = await FulfillmentService.test_adapter(session, "local_csv", actor="tester")
    assert isinstance(outcome, dict)
    assert "ok" in outcome
    message = str(outcome.get("message", ""))
    assert "未知能力" not in message, "不得再把 health_check 当能力走 invoke"
    # ★ Bug 3 的典型症状：str 没有 .value → AttributeError 被兜底转成一条错误文案
    assert "has no attribute" not in message, f"仍在触发 Bug 3 的属性错误：{message}"


# ======================================================================
#  Bug 4：越权被拒必须落审计（红线 R1 闭环）
# ======================================================================


async def _set_declared_scopes(session: object, adapter_name: str, scopes: list[str]) -> None:
    """直接改写适配器声明 scope（绕过 `update_config` 的业务校验，模拟"配置里就是越权"）。"""
    row = (
        await session.execute(
            select(FulfillmentAdapterConfig).where(FulfillmentAdapterConfig.adapter_name == adapter_name)
        )
    ).scalars().first()
    assert row is not None, f"适配器 {adapter_name} 配置行缺失"
    row.declared_scopes_json = list(scopes)
    # ★ 提交以释放 SQLite 写锁：真实环境里 `_write_audit` 会开**独立事务**落审计，
    #   若调用方事务仍持有写锁，独立会话会撞 `database is locked` 而退化。
    await session.commit()


async def test_factory_rejection_writes_permission_change_audit(session: object) -> None:
    """★ 回归核心：工厂路径（`enforce_scope_with_audit`）越权被拒 → 必写 `permission_change`。

    这条以前是 0 条（安全功能静默失效：顶栏告警恒 0、权限页恒空）。
    """
    await FulfillmentService.list_adapters(session)
    forbidden = list(FulfillmentService.scope_policies()["forbidden"])
    allowed = list(FulfillmentService.scope_policies()["allowed"])
    await _set_declared_scopes(session, "local_csv", [forbidden[0]])

    try:
        before = await AuditService.count_unhandled_violations(session)

        with pytest.raises(BusinessError) as excinfo:
            await FulfillmentService.get_adapter(session, adapter_name="local_csv", actor="tester")
        assert int(excinfo.value.code) == int(ErrorCode.ADAPTER_SCOPE_DENIED)
        await session.flush()

        after = await AuditService.count_unhandled_violations(session)
        assert after == before + 1, f"越权被拒必须新增 1 条未处置审计（{before} → {after}）"

        logs, _total = await AuditService.list_violations(session, is_unhandled=True)
        latest = logs[0]
        assert latest.action_type == AuditActionType.PERMISSION_CHANGE.value
        assert latest.is_handled is False, "拒绝记录必须待处置（顶栏红点亮）"
    finally:
        # ★ 用例会提交 DB 直写，必须还原，否则越权 scope 漏给后续用例
        await _set_declared_scopes(session, "local_csv", allowed)


async def test_passed_scope_check_does_not_inflate_violation_count(session: object) -> None:
    """★ `is_handled` 语义：校验**通过**的记录不得被计入未处置越权（否则红点恒亮）。"""
    await FulfillmentService.list_adapters(session)
    allowed = list(FulfillmentService.scope_policies()["allowed"])
    await _set_declared_scopes(session, "local_csv", allowed)

    before = await AuditService.count_unhandled_violations(session)
    adapter = await FulfillmentService.get_adapter(session, adapter_name="local_csv", actor="tester")
    assert adapter is not None, "白名单 scope 必须放行"
    await session.flush()

    after = await AuditService.count_unhandled_violations(session)
    assert after == before, f"通过的校验不得增加未处置计数（{before} → {after}）"


async def test_status_bar_reflects_violation_and_drops_after_handling(session: object) -> None:
    """★ 闭环证据：越权 → 顶栏计数 +1；处置后回落，且「被拒」与「被处置」成对存在。"""
    await FulfillmentService.list_adapters(session)
    forbidden = list(FulfillmentService.scope_policies()["forbidden"])
    allowed = list(FulfillmentService.scope_policies()["allowed"])
    await _set_declared_scopes(session, "local_csv", [forbidden[0]])

    try:
        base_bar = await SystemService.status_bar(session)
        base_count = int(base_bar.unhandled_violation_count)

        with pytest.raises(BusinessError):
            await FulfillmentService.get_adapter(session, adapter_name="local_csv", actor="tester")
        await session.flush()

        alert_bar = await SystemService.status_bar(session)
        alert_count = int(alert_bar.unhandled_violation_count)
        assert alert_count == base_count + 1, f"顶栏红点必须 +1（{base_count} → {alert_count}）"
        assert alert_bar.latest_violation is not None, "顶栏必须能给出最近一条越权"
        assert alert_bar.latest_violation.adapter_name == "local_csv"

        violation_id = int(alert_bar.latest_violation.id)
        handled = await AuditService.handle_violation(
            session, violation_id, operator="admin", handle_note="已回收越权 scope"
        )
        await session.flush()
        assert handled.is_handled is True

        settled_bar = await SystemService.status_bar(session)
        assert int(settled_bar.unhandled_violation_count) == base_count, "处置后顶栏计数必须回落"

        # 「被拒」与「被处置」成对存在（附录 A 第 16 条）
        logs, total = await AuditService.list_violations(session)
        assert total >= 2, "必须成对：1 条被拒 + 1 条被处置"
        statuses = [
            str(log.new_value or "") + str(log.remark or "")
            for log in logs
            if log.action_type == AuditActionType.PERMISSION_CHANGE.value
        ]
        assert any("rejected" in item for item in statuses), "缺少「被拒」记录"
        assert any("handled" in item for item in statuses), "缺少「被处置」记录"
    finally:
        # ★ 用例会提交 DB 直写，必须还原，否则越权 scope 漏给后续用例
        await _set_declared_scopes(session, "local_csv", allowed)
