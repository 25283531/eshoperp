"""履约适配层服务（§5.5.12 —— 适配器配置、切换、健康检查、越权策略）。

★ 红线 R1：所有适配器实例化**必须**走 `FulfillmentAdapterFactory.create()`
  （scope 校验在工厂内强制生效）。本模块是业务层获取适配器的唯一入口，
  **禁止**任何 service 直接 `AdapterClass(...)` 实例化。

★ 切换语义（FUL-P0-05）：`switch()` 只改 `SystemSetting['fulfillment.active_adapter']`。
  已落库订单的 `Order.adapter_name` **不变**，在途订单按原渠道跑完。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.adapters.fulfillment.factory import (
    FulfillmentAdapterFactory,
    _ensure_registered,
    get_active_adapter_name,
)
from app.adapters.fulfillment.registry import ADAPTER_REGISTRY
from app.adapters.fulfillment.scope_guard import (
    ScopeViolationError,
    check_scope,
    enforce_scope_with_audit,
    scope_policies,
)
from app.core.errors import BusinessError, ErrorCode, NotFoundError
from app.core.logging import get_logger, get_trace_id
from app.models.enums import (
    AuditActionType,
    AuditObjectType,
    ScopeCheckStatus,
    SettingKey,
)
from app.models.order import FulfillmentAdapterConfig as AdapterConfigModel
from app.models.order import Order
from app.models.system import SystemSetting
from app.services.audit_service import AuditService
from app.utils.kit import iso_utc, utc_now

logger = get_logger(__name__)

__all__ = ["FulfillmentService"]


class FulfillmentService:
    """履约适配器管理。"""

    # ==================================================================
    #  ★ 获取适配器（唯一入口，必经 scope 校验）
    # ==================================================================

    @staticmethod
    async def get_adapter(
        session: Any,
        *,
        adapter_name: str | None = None,
        actor: str = "system",
    ) -> Any:
        """★ 获取履约适配器实例 —— 业务层的**唯一**入口。

        内部走 `FulfillmentAdapterFactory.create()`，scope 红线在此强制生效。
        """
        return await FulfillmentAdapterFactory.create(
            adapter_name,
            session=session,
            actor=actor,
            audit_service=AuditService,
        )

    @staticmethod
    async def diagnostic_adapter(session: Any, adapter_name: str) -> Any:
        """诊断实例（**跳过** scope 校验），仅供能力矩阵预览与连通性自检。"""
        return await FulfillmentAdapterFactory.create_diagnostic(adapter_name, session=session)

    # ==================================================================
    #  列表 / 能力矩阵
    # ==================================================================

    @staticmethod
    async def list_adapters(session: Any) -> tuple[str, list[AdapterConfigModel]]:
        """列出全部适配器配置。

        ★ QA-05 修复：初始化不再依赖「第一次 GET 时 flush 一下」这种隐式行为。
          正规的播种在**启动期**由 `bootstrap.bootstrap_all()` 完成并 commit
          （见 `app/main.py` lifespan 与 `scripts/seed.py`）。
          此处保留一次**幂等且带 commit**的兜底调用，仅用于「库是老的 / 播种没跑过」
          这种异常情形 —— 它不再是"只活在当前请求内存里"的幻影行。

        Returns:
            `(当前生效适配器名, 配置列表)`。
        """
        active = await get_active_adapter_name(session)
        rows = (
            (await session.execute(select(AdapterConfigModel).order_by(AdapterConfigModel.priority)))
            .scalars()
            .all()
        )
        if not rows:
            # ★ 兜底：库里一行都没有（正常启动不会再走到这里）→ 显式播种并 commit
            await FulfillmentService._ensure_rows(session)
            rows = (
                (await session.execute(select(AdapterConfigModel).order_by(AdapterConfigModel.priority)))
                .scalars()
                .all()
            )
        return active, list(rows)

    @staticmethod
    async def _ensure_rows(session: Any) -> dict[str, Any]:
        """确保三个内置适配器都有配置行 —— ★ 幂等 + **显式 commit**。

        ★★ QA-05 根因就在这里 ★★
        旧实现只有 `session.add()` + `session.flush()`，**不 commit**；
        而 `get_db()` 也从不 commit（只 rollback / close）⇒ 这三行只活在单次请求的内存里，
        下一个请求的 `update_config()` 查表必然查不到 ⇒ `PUT .../config` **恒定 404**。

        Returns:
            `bootstrap_fulfillment_adapters()` 的结果字典。
        """
        from app.services.bootstrap import bootstrap_fulfillment_adapters

        return await bootstrap_fulfillment_adapters(session, commit=True)

    @staticmethod
    async def capabilities(session: Any, adapter_name: str) -> dict[str, Any]:
        """取适配器能力矩阵（诊断实例，不触发 scope 校验）。"""
        try:
            adapter = await FulfillmentService.diagnostic_adapter(session, adapter_name)
        except BusinessError:
            raise NotFoundError(f"履约适配器 {adapter_name} 不存在")
        return {
            "adapter_name": adapter.adapter_name,
            "display_name": adapter.display_name,
            "manifest": {
                "capabilities": adapter.capability_matrix(),
                "required_scopes": list(adapter.manifest.required_scopes),
                "version": getattr(adapter.manifest, "version", "0.0.0"),
            },
        }

    # ==================================================================
    #  配置更新（★ 保存声明 scope 时走 scope_guard，越权 403 / 5003）
    # ==================================================================

    @staticmethod
    async def _guard_enable(
        session: Any,
        row: AdapterConfigModel,
        *,
        declared_scopes: list[str] | None = None,
        is_enabled: bool | None = None,
        operator: str = "system",
    ) -> None:
        """★ R1 启用守门：**只要适配器最终处于启用状态，scope 白名单校验就必须真的发生过**。

        ------------------------------------------------------------------
        被堵死的绕过通道
        ------------------------------------------------------------------
        原实现是 `if declared_scopes is not None:` —— 只在请求体**携带** scope 时才校验。
        于是存在一条两步绕过：
            ① `PUT {"declared_scopes": ["item.write"]}` → 被拒，
               `scope_check_status = rejected`（但 `is_enabled` 仍为 False，看起来"没上线"）；
            ② `PUT {"is_enabled": true}`（**不带** scope）→ 校验整段被跳过 → 越权适配器上线。
        对权限最小化红线 R1 而言这是致命的：第三方一旦持有 `item.write`，
        就能直接改店铺商品，本项目"第三方只做订单履约"的前提整个崩塌。

        ------------------------------------------------------------------
        守门规则
        ------------------------------------------------------------------
        1. 触发条件 = 请求体带了 scope **或** 本次请求意图启用
                    **或** 当前已是启用态（防止对已启用行做越权改动）；
        2. 请求体带了 scope → **以请求体为准**校验，通过即放行（★ 这是唯一的修复路径）；
        3. 请求体没带 scope → 校验**库内已存的** scope；
           若上次已被判 `rejected` 且本次会让/让它保持启用态 → 一律 403 / 5003；
        4. 拒绝路径：写审计 → **显式 commit** → 再 raise。
           否则审计会随 `get_db()` 的 `rollback()` 一起消失，
           变成「拦住了却没留痕」（顶栏红点恒 0，比不拦更危险）。

        ★★ 规则 2 必须先于规则 3 执行：**不能**因为当前是 `rejected` 就把
           「提交合规 scope」这条修复路径也一起堵死，否则越权状态不可逆，
           运营无法自助修复，只会被逼去直接改库 —— 那才是真正毁掉 R1 证据链的行为。
        """
        adapter_name = str(row.adapter_name)
        intent_enable = is_enabled is True
        already_enabled = bool(row.is_enabled)
        # is_enabled 未传且当前已启用 → 仍然处于启用态，必须校验
        will_be_enabled = intent_enable or (is_enabled is None and already_enabled)

        if declared_scopes is None and not will_be_enabled:
            return  # 既不改 scope，也不会处于启用态 → 无需校验

        async def _reject(
            scopes: list[str],
            forbidden_hits: list[str],
            unknown: list[str],
            *,
            reason: str,
        ) -> None:
            """★ 统一拒绝出口：置 rejected → 写审计 → **显式 commit** → 抛 403。

            commit 必须在 raise 之前：拒绝路径的 `raise` 会让路由层的
            `await session.commit()` 执行不到，`get_db()` 的 finally 又会 `rollback()`，
            挂在请求会话上的审计会**随事务一起消失**，变成「拦住了却没留痕」
            （顶栏红点恒 0，比不拦更危险）。
            """
            denied = forbidden_hits or unknown
            row.scope_check_status = ScopeCheckStatus.REJECTED.value
            row.scope_check_message = f"声明了越权 scope：{', '.join(denied) or '<空>'}"
            await session.flush()
            try:
                await enforce_scope_with_audit(
                    scopes,
                    adapter_name=adapter_name,
                    actor=operator,
                    session=session,
                    audit_service=AuditService,
                    trace_id=get_trace_id(),
                )
            except ScopeViolationError:
                pass  # 预期内：审计已写入并 commit，下面统一抛业务错误
            except Exception as exc:  # noqa: BLE001  审计失败不得吞掉 403
                logger.warning("scope_violation_audit_failed", adapter=adapter_name, error=str(exc))
            await session.commit()
            raise BusinessError(
                f"适配器 {adapter_name} 声明了越权 scope（{', '.join(denied) or '<空>'}）。"
                f"第三方不得持有商品编辑 / 上架 / 下架 / 改价权限",
                code=ErrorCode.ADAPTER_SCOPE_DENIED,
                http_status=403,
                detail={
                    "forbidden_scopes": forbidden_hits,
                    "unknown_scopes": unknown,
                    "reason": reason,
                },
            )

        # ==============================================================
        # ★ 分支 A：请求体带了 scope → **以请求体为准**，校验通过即放行
        # ==============================================================
        # ★★ 顺序至关重要：这一支必须排在「rejected 一律拒绝」之前。
        #    否则适配器一旦被判越权就**永远改不回来** —— 连提交合规 scope 都会被 403，
        #    运营无法自助修复，只会被逼去直接改库（那才是真正破坏 R1 证据链的行为）。
        #    这正是 R1 想要的终态：越权适配器必须回到合规 scope，才谈得上启用。
        if declared_scopes is not None:
            passed, forbidden_hits, unknown = check_scope(declared_scopes)
            if passed:
                row.declared_scopes_json = [str(s) for s in declared_scopes]
                row.scope_check_status = ScopeCheckStatus.PASSED.value
                row.scope_check_message = "scope 校验通过"
                await session.flush()
                return
            await _reject(
                [str(s) for s in declared_scopes],
                forbidden_hits,
                unknown,
                reason="declared_scopes_rejected",
            )

        # ==============================================================
        # ★ 分支 B：请求体**没带** scope → 校验对象是库内已存的 scope
        # ==============================================================
        # 这就是原来那条绕过通道的堵点：只发 `{"is_enabled": true}` 不带 scope，
        # 老实现会整段跳过校验，让「已存越权 scope 的适配器」直接上线。
        existing_scopes = [str(s) for s in (row.declared_scopes_json or [])]

        # 规则 3：上次已被判越权，且本次请求会让/让它保持启用态 → 一律拒绝
        if row.scope_check_status == ScopeCheckStatus.REJECTED.value and will_be_enabled:
            denied = existing_scopes or ["<未记录>"]
            logger.warning(
                "rejected_scope_enable_blocked",
                adapter=adapter_name,
                declared_scopes=denied,
                reason="越权适配器不得启用；需先提交合规 scope 使状态回到 passed",
            )
            await _reject(
                denied, denied, [], reason="rejected_scope_enable_blocked"
            )

        if not existing_scopes:
            return  # 空 scope 不构成越权（同时也不授予任何权限）

        passed, forbidden_hits, unknown = check_scope(existing_scopes)
        if passed:
            row.scope_check_status = ScopeCheckStatus.PASSED.value
            row.scope_check_message = "scope 校验通过"
            await session.flush()
            return
        await _reject(existing_scopes, forbidden_hits, unknown, reason="stored_scopes_rejected")

    @staticmethod
    async def update_config(
        session: Any,
        adapter_name: str,
        *,
        credential_id: int | None = None,
        config_json: dict[str, Any] | None = None,
        declared_scopes: list[str] | None = None,
        is_enabled: bool | None = None,
        operator: str = "system",
    ) -> AdapterConfigModel:
        """★ 更新适配器配置。

        校验由 `FulfillmentService._guard_enable()` 统一执行，**不再**只在请求体携带
        `declared_scopes` 时才校验（那条老规则留下过"保存越权 scope → 只发 is_enabled 启用"
        的两步绕过通道）：
            - 命中 `FORBIDDEN_SCOPES`（如 `item.write`）→ 403 / 5003 + 写审计 + 告警；
            - 存在白名单外 scope → 同样拒绝；
            - 请求体不传 scope 但意图启用 → 校验**库内已存的** scope；
            - 当前 `scope_check_status = rejected` 且最终会处于启用态 → 一律拒绝。

        ★★ 越权被拒时审计**必须真的落库**（红线 R1 的证据链）★★
        拒绝路径会 `raise`，而路由层 `await session.commit()` 在 raise 之后根本执行不到，
        `get_db()` 的 finally 会 `rollback()` —— 挂在请求会话上的审计会**随事务一起消失**，
        变成「拦住了却没留痕」（顶栏红点恒 0，比不拦更危险）。
        因此守门内部：写审计 → **显式 commit** → 再 raise。
        """
        row = (
            await session.execute(
                select(AdapterConfigModel).where(AdapterConfigModel.adapter_name == adapter_name)
            )
        ).scalars().first()
        if row is None:
            # ★ 兜底：行不存在时先播种一次（幂等 + commit），避免老库直接 404
            await FulfillmentService._ensure_rows(session)
            row = (
                await session.execute(
                    select(AdapterConfigModel).where(AdapterConfigModel.adapter_name == adapter_name)
                )
            ).scalars().first()
        if row is None:
            raise NotFoundError(f"履约适配器 {adapter_name} 不存在")

        # ★★ R1 启用守门：只要适配器最终处于启用态，scope 校验就必须真的发生过。
        #    完整规则与绕过通道说明见 `FulfillmentService._guard_enable`。
        await FulfillmentService._guard_enable(
            session,
            row,
            declared_scopes=declared_scopes,
            is_enabled=is_enabled,
            operator=operator,
        )

        if credential_id is not None:
            row.credential_id = int(credential_id)
        if config_json is not None:
            row.config_json = dict(config_json)
        if is_enabled is not None:
            row.is_enabled = bool(is_enabled)
        await session.flush()

        await AuditService.write(
            session,
            action_type=AuditActionType.ADAPTER_SWITCH.value,
            object_type=AuditObjectType.ADAPTER.value,
            object_id=adapter_name,
            operator=operator,
            new_value={
                "declared_scopes": row.declared_scopes_json,
                "is_enabled": row.is_enabled,
                "config_updated": config_json is not None,
            },
            trace_id=get_trace_id(),
            remark=f"更新适配器 {adapter_name} 配置",
        )
        await session.flush()
        return row

    # ==================================================================
    #  ★ 热切换（FUL-P0-05：只改配置，在途订单按原渠道跑完）
    # ==================================================================

    @staticmethod
    async def switch(
        session: Any,
        adapter_name: str,
        *,
        reason: str = "",
        drain_inflight: bool = True,
        operator: str = "system",
    ) -> dict[str, Any]:
        """★ 切换当前生效的履约适配器。

        语义：
            1. 只改 `SystemSetting['fulfillment.active_adapter']`；
            2. **已落库订单的 `Order.adapter_name` 不变** —— 在途订单按原渠道跑完；
            3. `drain_inflight=True` 时不清理任何在途状态，仅返回在途数量供前端提示。
        """
        _ensure_registered()
        if adapter_name not in ADAPTER_REGISTRY:
            raise BusinessError(
                f"履约适配器 {adapter_name} 未注册（可选：{', '.join(sorted(ADAPTER_REGISTRY))}）",
                code=ErrorCode.ADAPTER_UNAVAILABLE,
            )

        previous = await get_active_adapter_name(session)
        stmt = select(SystemSetting).where(
            SystemSetting.setting_key == SettingKey.FULFILLMENT_ACTIVE_ADAPTER.value
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            row = SystemSetting(
                setting_key=SettingKey.FULFILLMENT_ACTIVE_ADAPTER.value,
                setting_value=adapter_name,
                value_type="string",
                description="当前生效履约适配器（热切换核心）",
                updated_by=operator,
            )
            session.add(row)
        else:
            row.setting_value = adapter_name
            row.updated_by = operator
        await session.flush()

        # 在途订单数（仍走原渠道）
        inflight = 0
        if drain_inflight:
            from app.models.enums import TERMINAL_ORDER_STATUSES

            inflight = int(
                (
                    await session.execute(
                        select(Order.id).where(
                            Order.adapter_name == previous,
                            Order.fulfillment_status.notin_(sorted(TERMINAL_ORDER_STATUSES)),
                        )
                    )
                )
                .scalars()
                .all()
                .__len__()
            )

        audit = await AuditService.write(
            session,
            action_type=AuditActionType.ADAPTER_SWITCH.value,
            object_type=AuditObjectType.ADAPTER.value,
            object_id=adapter_name,
            operator=operator,
            old_value={"active_adapter": previous},
            new_value={"active_adapter": adapter_name, "reason": reason, "inflight": inflight},
            trace_id=get_trace_id(),
            remark=f"履约适配器 {previous} → {adapter_name}：{reason}",
        )
        await session.flush()

        logger.info("fulfillment_adapter_switched", previous=previous, current=adapter_name, inflight=inflight)
        return {
            "active_adapter": adapter_name,
            "previous_adapter": previous,
            "inflight_order_count": inflight,
            "audit_id": int(audit.id) if audit.id else None,
        }

    # ==================================================================
    #  连通性自检
    # ==================================================================

    @staticmethod
    async def test_adapter(session: Any, adapter_name: str, *, actor: str = "system") -> dict[str, Any]:
        """连通性自检（★ 走工厂 `create()`，scope 越权会在创建阶段被拒）。"""
        started = utc_now()
        try:
            adapter = await FulfillmentService.get_adapter(session, adapter_name=adapter_name, actor=actor)
            # ★ `health_check` 不在 8 项履约能力（Capability）之内，是适配器自身的连通性方法，
            #   直接调用；走 invoke() 会被判为"未知能力"而永远返回 UNSUPPORTED。
            result = await adapter.health_check()
            status = getattr(result.data, "status", "unknown") if result and result.data else "unknown"
            return {
                "ok": bool(result is not None and result.code == "OK" and status == "healthy"),
                "latency_ms": int(getattr(result, "elapsed_ms", 0) or 0) if result else 0,
                "message": (result.message if result else "适配器无响应") or status,
                "checked_at": iso_utc(started),
            }
        except BusinessError as exc:
            return {"ok": False, "latency_ms": 0, "message": exc.message, "checked_at": iso_utc(started)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "latency_ms": 0, "message": str(exc), "checked_at": iso_utc(started)}

    # ==================================================================
    #  越权策略
    # ==================================================================

    @staticmethod
    def scope_policies() -> dict[str, Any]:
        """返回 scope 白 / 黑名单策略（供 `GET /adapters/scope-policies`）。"""
        return scope_policies()

    @staticmethod
    async def heartbeat(session: Any, adapter_name: str) -> dict[str, Any]:
        """心跳：更新 `health_status` / `last_heartbeat_at` / `heartbeat_fail_count`。"""
        row = (
            await session.execute(
                select(AdapterConfigModel).where(AdapterConfigModel.adapter_name == adapter_name)
            )
        ).scalars().first()
        if row is None:
            return {"ok": False, "message": f"适配器 {adapter_name} 未配置"}
        result = await FulfillmentService.test_adapter(session, adapter_name)
        row.last_heartbeat_at = utc_now()
        if result["ok"]:
            row.health_status = "healthy"
            row.heartbeat_fail_count = 0
        else:
            row.heartbeat_fail_count = int(row.heartbeat_fail_count or 0) + 1
            row.health_status = "down" if row.heartbeat_fail_count >= 3 else "degraded"
        row.last_heartbeat_msg = result["message"]
        await session.flush()
        return result
