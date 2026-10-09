"""★ 红线 R1「启用守门」的对照实验（不是读代码，是跑出来的结果）。

================================================================================
被堵死的绕过通道（本文件的存在理由）
================================================================================
老规则是 `if declared_scopes is not None:` —— 只在**请求体携带** scope 时才校验。
于是存在一条两步绕过：
    ① `PUT {"declared_scopes": ["item.write"]}` → 被拒，
       `scope_check_status = rejected`（此刻 `is_enabled` 仍是 False，看起来"没上线"）；
    ② `PUT {"is_enabled": true}`（**不带** scope）→ 校验整段被跳过 → 越权适配器上线。
一条红线如果被"两步走"绕过去，等于没有。所以本文件用**对照组**证明守门真的生效：

    [A] 负向：rejected 行 + 只发 is_enabled=true  → 必须 403 / 5003（**核心通道**）
    [B] 正向：passed  行 + 只发 is_enabled=true  → 必须放行（证明 A 不是"一律拒绝"）
    [C] 留痕：A 被拒后 audit_log 必须**真有一条**（证明不是"拦住了没留痕"）
    [D] 回归：同请求带 item.write + is_enabled → 也必须 403（老路径没被改坏）

★ B 是 A 的对照组，缺了 B，"A 通过"可能只是把启用功能整个禁用了 —— 那不是修好，是砍掉。
★ C 必须用**全新 session** 查：守门内部是「写审计 → 显式 commit → 再 raise」，
  审计在独立事务里；用被 rollback 的那个 session 查只会查到 0 条，得出假结论。
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from app.core.database import get_session_factory
from app.core.errors import BusinessError, ErrorCode
from app.models.enums import AuditActionType, ScopeCheckStatus
from app.models.order import FulfillmentAdapterConfig
from app.models.system import AuditLog
from app.services.fulfillment_service import FulfillmentService
from tests.conftest import ADMIN_HEADERS

pytestmark = pytest.mark.asyncio

ADAPTER = "miaoshou"
# ★ 白名单取自 `app/adapters/fulfillment/scope_guard.py` 的 ALLOWED_SCOPES
ALLOWED_SCOPES = ["order.read", "logistics.write"]
# ★ 黑名单取自 FORBIDDEN_SCOPES
FORBIDDEN_SCOPES = ["order.read", "item.write"]

CONFIG_URL = f"/api/v1/adapters/fulfillment/{ADAPTER}/config"


async def _reset_row(
    session: Any,
    *,
    declared_scopes: list[str] | None,
    status: ScopeCheckStatus,
    is_enabled: bool,
) -> FulfillmentAdapterConfig:
    """把 `miaoshou` 行重置到用例需要的初值并**落库**（用例之间不互相污染）。"""
    row = (
        await session.execute(
            select(FulfillmentAdapterConfig).where(FulfillmentAdapterConfig.adapter_name == ADAPTER)
        )
    ).scalars().first()
    assert row is not None, f"前置条件失败：bootstrap 未种入 {ADAPTER} 行"
    row.declared_scopes_json = list(declared_scopes) if declared_scopes is not None else None
    row.scope_check_status = status.value
    row.scope_check_message = None
    row.is_enabled = bool(is_enabled)
    await session.commit()
    await session.refresh(row)
    return row


async def _count_permission_audit(adapter_name: str = ADAPTER) -> int:
    """用**全新 session** 数该适配器的 `permission_change` 审计条数。"""
    factory = get_session_factory()
    async with factory() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(AuditLog.id).where(
                        AuditLog.action_type == AuditActionType.PERMISSION_CHANGE.value,
                        AuditLog.object_id == adapter_name,
                    )
                )
            )
            .scalars()
            .all()
        )
        return len(rows)


# ===========================================================================
#  [A] 负向 —— 核心绕过通道必须被堵死
# ===========================================================================


async def test_a_rejected_row_cannot_be_enabled_without_scopes(session: Any) -> None:
    """[A-服务层] `rejected` 的行只发 `is_enabled=True`（**不带** scope）→ 必须抛 403 / 5003。"""
    await _reset_row(
        session,
        declared_scopes=FORBIDDEN_SCOPES,
        status=ScopeCheckStatus.REJECTED,
        is_enabled=False,
    )

    with pytest.raises(BusinessError) as excinfo:
        # ★★ 关键点：这里**绝不能**传 declared_scopes —— 传了就变成 [D]，测不到绕过通道
        await FulfillmentService.update_config(session, ADAPTER, is_enabled=True, operator="tester")

    err = excinfo.value
    assert err.code == ErrorCode.ADAPTER_SCOPE_DENIED, (
        f"[A] 期望错误码 {ErrorCode.ADAPTER_SCOPE_DENIED}（5003），实际 {err.code}；"
        f"消息={err.message}"
    )
    assert err.http_status == 403, f"[A] 期望 http_status=403，实际 {err.http_status}"
    assert "item.write" in str(err.detail), (
        f"[A] 拒绝原因必须点名越权 scope，实际 detail={err.detail}"
    )


async def test_a_prime_without_guard_the_bypass_is_wide_open(
    session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[A′] 反证对照：**去掉** `_guard_enable` 后，同一条绕过通道必须**敞开**。

    ★ 这条是 [A] 的"空白对照"。没有它，"[A] 被拒"可能只是因为别的校验顺手挡了一下，
      而 `_guard_enable` 本身是个摆设 —— 这正是"声称修好过两次、实际没有"能混过去的原因。
      这里用 monkeypatch 把守门换成空实现（**不改源码**），如果通道立刻通了，
      就证明 [A] 的 403 确实由 `_guard_enable` 造成。
    """
    await _reset_row(
        session,
        declared_scopes=FORBIDDEN_SCOPES,
        status=ScopeCheckStatus.REJECTED,
        is_enabled=False,
    )

    async def _no_guard(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(FulfillmentService, "_guard_enable", staticmethod(_no_guard))

    row = await FulfillmentService.update_config(session, ADAPTER, is_enabled=True, operator="tester")
    assert bool(row.is_enabled) is True, (
        "[A′] 撤掉守门后越权适配器仍未能启用 —— 说明 [A] 的 403 另有来源，"
        "`_guard_enable` 可能只是个摆设，需要重新定位"
    )


async def test_a_http_rejected_row_enable_returns_403(client: Any, session: Any) -> None:
    """[A-HTTP 层] 真实打接口：body 只有 `{"is_enabled": true}` → 必须 403（不是 200）。"""
    await _reset_row(
        session,
        declared_scopes=FORBIDDEN_SCOPES,
        status=ScopeCheckStatus.REJECTED,
        is_enabled=False,
    )

    resp = await client.put(CONFIG_URL, json={"is_enabled": True}, headers=ADMIN_HEADERS)

    assert resp.status_code == 403, (
        f"[A-HTTP] 越权适配器被「只发 is_enabled」启用了 —— 两步绕过通道仍然敞开。"
        f"实际 HTTP {resp.status_code}，响应={resp.text[:300]}"
    )
    body = resp.json()
    code = body.get("code", body.get("data", {}).get("code") if isinstance(body.get("data"), dict) else None)
    assert code in (5003, int(ErrorCode.ADAPTER_SCOPE_DENIED)), (
        f"[A-HTTP] 期望业务码 5003，实际 {code}；响应={resp.text[:300]}"
    )


# ===========================================================================
#  [B] 正向对照 —— 合规 scope 的行必须能正常启用
# ===========================================================================


async def test_b_passed_row_can_be_enabled_without_scopes(session: Any) -> None:
    """[B] 合规 scope + passed 的行只发 `is_enabled=True` → 必须**放行**（证明 A 不是一律拒绝）。"""
    await _reset_row(
        session,
        declared_scopes=ALLOWED_SCOPES,
        status=ScopeCheckStatus.PASSED,
        is_enabled=False,
    )

    # ★ 同样不传 declared_scopes —— 与 [A] 唯一的差别就是库内 scope 是否合规
    row = await FulfillmentService.update_config(session, ADAPTER, is_enabled=True, operator="tester")

    assert bool(row.is_enabled) is True, (
        f"[B] 合规 scope 的适配器竟无法启用 —— 守门被做成了一律拒绝，"
        f"那不是修好而是砍功能。is_enabled={row.is_enabled}"
    )
    assert row.scope_check_status == ScopeCheckStatus.PASSED.value, (
        f"[B] 合规 scope 不应被改判，实际 {row.scope_check_status}（{row.scope_check_message}）"
    )


async def test_b_http_passed_row_enable_returns_200(client: Any, session: Any) -> None:
    """[B-HTTP 层] 合规行只发 `{"is_enabled": true}` → 必须 200。"""
    await _reset_row(
        session,
        declared_scopes=ALLOWED_SCOPES,
        status=ScopeCheckStatus.PASSED,
        is_enabled=False,
    )

    resp = await client.put(CONFIG_URL, json={"is_enabled": True}, headers=ADMIN_HEADERS)

    assert resp.status_code == 200, (
        f"[B-HTTP] 合规适配器启用被拒 —— 实际 HTTP {resp.status_code}，响应={resp.text[:300]}"
    )
    assert resp.json().get("data", {}).get("is_enabled") is True, (
        f"[B-HTTP] 响应里 is_enabled 应为 True，实际={resp.text[:300]}"
    )


# ===========================================================================
#  [C] 留痕 —— 拦住了必须真的留痕（不是"拦住了没留痕"）
# ===========================================================================


async def test_c_rejection_really_writes_audit(session: Any) -> None:
    """[C] [A] 被拒之后，`audit_log` 必须**真的多出一条** permission_change 记录。

    ★★ 守门内部是「写审计 → 显式 commit → 再 raise」：审计落在**独立事务**里，
       而调用方的 session 随后会被 `get_db()` 的 finally `rollback()`。
       所以这里必须用**全新 session** 去查，用当前 session 查会得到 0 条假结论。
    """
    await _reset_row(
        session,
        declared_scopes=FORBIDDEN_SCOPES,
        status=ScopeCheckStatus.REJECTED,
        is_enabled=False,
    )
    before = await _count_permission_audit()

    with pytest.raises(BusinessError):
        await FulfillmentService.update_config(session, ADAPTER, is_enabled=True, operator="tester")

    after = await _count_permission_audit()
    assert after > before, (
        f"[C] 「拦住了却没留痕」—— 越权被拒但审计条数没有增加（{before} → {after}）。"
        f"顶栏红点会恒为 0，比不拦更危险"
    )


async def test_c_http_rejection_really_writes_audit(client: Any, session: Any) -> None:
    """[C-HTTP 层] 走真实接口被 403 之后，审计同样必须留痕（独立事务已 commit）。"""
    await _reset_row(
        session,
        declared_scopes=FORBIDDEN_SCOPES,
        status=ScopeCheckStatus.REJECTED,
        is_enabled=False,
    )
    before = await _count_permission_audit()

    resp = await client.put(CONFIG_URL, json={"is_enabled": True}, headers=ADMIN_HEADERS)

    assert resp.status_code == 403, f"[C-HTTP] 前置条件：本次请求应被 403，实际 {resp.status_code}"
    after = await _count_permission_audit()
    assert after > before, (
        f"[C-HTTP] 接口层被拒却没写审计（{before} → {after}）—— "
        f"审计挂在请求 session 上被 rollback 一起撤掉了"
    )


# ===========================================================================
#  [D] 补充负向 —— 老路径（同请求带越权 scope）没被改坏
# ===========================================================================


async def test_d_declared_forbidden_scope_with_enable_is_403(session: Any) -> None:
    """[D] 同一请求里 `declared_scopes=["item.write"]` + `is_enabled=True` → 必须 403。"""
    await _reset_row(
        session,
        declared_scopes=ALLOWED_SCOPES,
        status=ScopeCheckStatus.PASSED,
        is_enabled=False,
    )

    with pytest.raises(BusinessError) as excinfo:
        await FulfillmentService.update_config(
            session,
            ADAPTER,
            declared_scopes=["item.write"],
            is_enabled=True,
            operator="tester",
        )

    err = excinfo.value
    assert err.code == ErrorCode.ADAPTER_SCOPE_DENIED, (
        f"[D] 期望 {ErrorCode.ADAPTER_SCOPE_DENIED}，实际 {err.code}（{err.message}）"
    )
    assert err.http_status == 403, f"[D] 期望 http_status=403，实际 {err.http_status}"


async def test_d_http_declared_forbidden_scope_is_403(client: Any, session: Any) -> None:
    """[D-HTTP 层] 同请求带越权 scope → 接口必须 403。"""
    await _reset_row(
        session,
        declared_scopes=ALLOWED_SCOPES,
        status=ScopeCheckStatus.PASSED,
        is_enabled=False,
    )

    resp = await client.put(
        CONFIG_URL,
        json={"declared_scopes": ["item.write"], "is_enabled": True},
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == 403, (
        f"[D-HTTP] 越权 scope 竟然被接受 —— 实际 HTTP {resp.status_code}，响应={resp.text[:300]}"
    )
