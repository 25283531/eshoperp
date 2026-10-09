"""权限最小化红线 R1 测试（附录 A 第 1 / 12 条）+ 越权处置闭环（SYS-P0-06）。

断言：
    1. scope 白名单内放行、黑名单内拒绝；
    2. 适配器声明越权 scope → **403 / 5003** 且写 `permission_change` 审计（`is_handled=0`）；
    3. 管理员处置后 `is_handled=1`，且**再埋一条**同类型审计（「被拒」与「被处置」成对）。
"""

from __future__ import annotations

import pytest

from app.adapters.fulfillment.scope_guard import check_scope
from app.core.errors import BusinessError, ErrorCode
from app.models.enums import AuditActionType
from app.services.audit_service import AuditService
from app.services.fulfillment_service import FulfillmentService


async def test_allowed_scopes_pass(session: object) -> None:
    """白名单 scope 放行。"""
    policies = FulfillmentService.scope_policies()
    allowed = list(policies["allowed"])
    assert allowed, "白名单不得为空"
    passed, forbidden_hits, unknown = check_scope(allowed)
    assert passed is True
    assert forbidden_hits == []
    assert unknown == []


async def test_forbidden_scopes_rejected(session: object) -> None:
    """★ 商品编辑类 scope 一律拒绝（第三方永不持有商品编辑权）。"""
    policies = FulfillmentService.scope_policies()
    forbidden = list(policies["forbidden"])
    assert forbidden, "黑名单不得为空"
    passed, forbidden_hits, _unknown = check_scope([forbidden[0]])
    assert passed is False
    assert forbidden[0] in forbidden_hits


async def test_declare_forbidden_scope_raises_5003(session: object) -> None:
    """声明越权 scope → 403 / 5003 + 审计留痕（未处置）。"""
    await FulfillmentService.list_adapters(session)  # 确保配置行存在
    policies = FulfillmentService.scope_policies()
    forbidden = list(policies["forbidden"])

    with pytest.raises(BusinessError) as excinfo:
        await FulfillmentService.update_config(
            session,
            "local_csv",
            declared_scopes=[forbidden[0]],
            operator="tester",
        )
    assert int(excinfo.value.code) == int(ErrorCode.ADAPTER_SCOPE_DENIED)
    assert excinfo.value.http_status == 403
    await session.flush()

    logs, total = await AuditService.list_violations(session, is_unhandled=True)
    assert total >= 1, "越权必须埋审计（未处置）"
    assert all(log.is_handled is False for log in logs)


async def test_handle_violation_writes_paired_audit(session: object) -> None:
    """★ 处置后 `is_handled=1`，并**再埋一条** `permission_change`（成对留痕）。"""
    await FulfillmentService.list_adapters(session)
    policies = FulfillmentService.scope_policies()
    forbidden = list(policies["forbidden"])

    with pytest.raises(BusinessError):
        await FulfillmentService.update_config(
            session, "local_csv", declared_scopes=[forbidden[0]], operator="tester"
        )
    await session.flush()

    _logs, before = await AuditService.list_violations(session, is_unhandled=True)
    assert before >= 1
    violation_id = int(_logs[0].id)

    row = await AuditService.handle_violation(
        session, violation_id, operator="admin", handle_note="已联系第三方回收该 scope"
    )
    await session.flush()
    assert row.is_handled is True
    assert row.handled_by == "admin"

    all_logs, after = await AuditService.list_violations(session)
    assert after >= before + 1, "处置动作必须再埋一条 permission_change"

    _unhandled, unhandled_total = await AuditService.list_violations(session, is_unhandled=True)
    assert unhandled_total == before - 1, "处置后未处置计数应 -1（顶栏红点熄灭）"


async def test_violation_audit_action_type(session: object) -> None:
    """越权记录的 `action_type` 必须是 `permission_change`（不新建表）。"""
    await FulfillmentService.list_adapters(session)
    policies = FulfillmentService.scope_policies()
    forbidden = list(policies["forbidden"])

    with pytest.raises(BusinessError):
        await FulfillmentService.update_config(
            session, "local_csv", declared_scopes=[forbidden[0]], operator="tester"
        )
    await session.flush()

    logs, _total = await AuditService.list_violations(session)
    assert logs, "越权记录缺失"
    assert logs[0].action_type == AuditActionType.PERMISSION_CHANGE.value
