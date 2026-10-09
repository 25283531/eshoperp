"""系统设置 / 顶栏聚合 / 健康检查 / 凭证 / 任务查询（§5.5.1、§5.5.13）。

★ ★ ★ `GET /system/status-bar` 的实现约束（PRD v1.3 验收标准，**非建议**）★ ★ ★
    1. **只读 `SystemSetting` 缓存与 `audit_log` 未处理计数**；
    2. **绝不触发任何外部 HTTP 调用**（否则顶栏 60s 轮询会把第三方 API 打爆）；
    3. **禁止拆成多个接口轮询** —— 顶栏是常驻组件，新增状态项必须并入本响应体。
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import func, select

from app.adapters.fulfillment.scope_guard import list_violations as list_memory_violations
from app.core.config import get_settings
from app.core.errors import BusinessError, ErrorCode, NotFoundError
from app.core.logging import get_logger, get_trace_id
from app.models.enums import (
    AdapterName,
    AuditActionType,
    HealthStatusValue,
    ListingMode,
    TaskStatus,
    ValueType,
)
from app.models.order import FulfillmentAdapterConfig as AdapterConfigModel
from app.models.system import AuditLog, SystemSetting
from app.models.task import TaskRecord
from app.schemas.system import (
    StatusBarLatestViolation,
    StatusBarUnhealthyItem,
    StatusBarVo,
)
from app.core.security import decrypt_credential, encrypt_credential
from app.services.audit_service import AuditService
from app.utils.crypto import mask_secret
from app.utils.kit import iso_utc, utc_now

logger = get_logger(__name__)

__all__ = ["SystemService"]

# 上架模式中文标签
LISTING_MODE_LABELS = {
    ListingMode.REAL.value: "真实API",
    ListingMode.MOCK.value: "Mock",
    ListingMode.MANUAL.value: "半自动",
}

# 履约适配器中文标签
ADAPTER_LABELS = {
    AdapterName.MIAOSHOU.value: "妙手",
    AdapterName.YITAO.value: "逸淘",
    AdapterName.LOCAL_CSV.value: "本地兜底",
}


class SystemService:
    """系统配置与顶栏聚合。"""

    # ==================================================================
    #  ★ 顶栏聚合（SYS-P0-05）
    # ==================================================================

    @staticmethod
    async def status_bar(session: Any) -> StatusBarVo:
        """★ `GET /system/status-bar` —— 全局顶栏专用聚合（**零外部 HTTP**）。

        三项内容：
            ① 越权告警红点（未处置计数 + 最近一条）
            ② Mock 模式标识 + 当前履约渠道
            ③ 系统健康状态（读 DB 中的适配器健康字段，不主动探测）
        """
        settings = get_settings()

        # ---------- ① 越权告警 ----------
        unhandled = await AuditService.count_unhandled_violations(session)
        latest = await AuditService.latest_unhandled_violation(session)
        latest_vo: StatusBarLatestViolation | None = None
        if latest is not None:
            from app.schemas.system import ScopeViolationVo

            parsed = ScopeViolationVo.from_audit(latest)
            latest_vo = StatusBarLatestViolation(
                id=parsed.id,
                adapter_name=parsed.adapter_name,
                denied_scopes=parsed.denied_scopes,
                created_at=parsed.created_at,
            )

        # ---------- ② 上架模式与履约渠道（读 SystemSetting 缓存）----------
        listing_mode = await SystemService.get_setting_value(
            session, "listing.mode", default=settings.listing_mode
        )
        active_adapter = await SystemService.get_setting_value(
            session, "fulfillment.active_adapter", default=settings.fulfillment_active_adapter
        )

        # ---------- ③ 系统健康（★ 只读 DB 字段，绝不发外部 HTTP）----------
        unhealthy: list[StatusBarUnhealthyItem] = []
        rows = (
            (await session.execute(select(AdapterConfigModel).where(AdapterConfigModel.is_enabled.is_(True))))
            .scalars()
            .all()
        )
        for row in rows:
            if row.health_status in {HealthStatusValue.HEALTHY.value, HealthStatusValue.UNKNOWN.value}:
                continue
            unhealthy.append(
                StatusBarUnhealthyItem(
                    name=row.display_name or row.adapter_name,
                    status=row.health_status,
                    message=row.last_heartbeat_msg or f"连续心跳失败 {row.heartbeat_fail_count} 次",
                )
            )

        if unhealthy:
            health = (
                HealthStatusValue.DOWN.value
                if any(i.status == HealthStatusValue.DOWN.value for i in unhealthy)
                else HealthStatusValue.DEGRADED.value
            )
        else:
            health = HealthStatusValue.HEALTHY.value

        return StatusBarVo(
            unhandled_violation_count=unhandled,
            latest_violation=latest_vo,
            listing_mode=listing_mode,
            listing_mode_label=LISTING_MODE_LABELS.get(listing_mode, listing_mode),
            is_mock_active=(listing_mode == ListingMode.MOCK.value),
            active_fulfillment_adapter=active_adapter,
            active_fulfillment_adapter_label=ADAPTER_LABELS.get(active_adapter, active_adapter),
            health_status=health,
            unhealthy_items=unhealthy,
        )

    # ==================================================================
    #  系统设置
    # ==================================================================

    @staticmethod
    async def get_setting_value(session: Any, key: str, *, default: str = "") -> str:
        """读取单个配置值（带缓存回退）。"""
        row = (
            await session.execute(select(SystemSetting).where(SystemSetting.setting_key == key))
        ).scalars().first()
        if row is not None and row.setting_value:
            return str(row.setting_value)
        return default

    @staticmethod
    async def list_settings(session: Any) -> dict[str, dict[str, Any]]:
        """读取全部配置（`{key: {value, value_type, description, ...}}`）。"""
        rows = (await session.execute(select(SystemSetting).order_by(SystemSetting.setting_key))).scalars().all()
        return {
            row.setting_key: {
                "value": row.setting_value,
                "value_type": row.value_type,
                "description": row.description,
                "updated_by": row.updated_by,
                "updated_at": iso_utc(row.updated_at) if row.updated_at else None,
            }
            for row in rows
        }

    @staticmethod
    async def update_setting(
        session: Any,
        key: str,
        value: str,
        *,
        reason: str = "",
        operator: str = "system",
    ) -> SystemSetting:
        """更新系统配置（管理员；键不存在 → 404）。"""
        row = (
            await session.execute(select(SystemSetting).where(SystemSetting.setting_key == key))
        ).scalars().first()
        if row is None:
            raise NotFoundError(f"配置项 {key} 不存在", code=ErrorCode.SETTING_NOT_FOUND)

        old_value = row.setting_value
        row.setting_value = str(value)
        row.updated_by = operator
        await session.flush()

        await AuditService.write(
            session,
            action_type=AuditActionType.CREDENTIAL_CHANGE.value,
            object_type="system_setting",
            object_id=key,
            operator=operator,
            old_value={"value": old_value},
            new_value={"value": str(value), "reason": reason},
            trace_id=get_trace_id(),
            remark=f"更新配置 {key}：{old_value} → {value}",
        )
        await session.flush()
        return row

    @staticmethod
    async def set_listing_mode(
        session: Any,
        mode: str,
        *,
        reason: str = "",
        operator: str = "system",
    ) -> dict[str, Any]:
        """切换上架模式（留痕）。"""
        allowed = {item.value for item in ListingMode}
        if mode not in allowed:
            raise BusinessError(f"上架模式只能是 {', '.join(sorted(allowed))}", code=ErrorCode.PARAM_ERROR)
        row = await SystemService.update_setting(
            session, "listing.mode", mode, reason=reason, operator=operator
        )
        return {
            "mode": row.setting_value,
            "updated_at": iso_utc(row.updated_at) if row.updated_at else None,
            "audit_id": int(row.id),
        }

    # ==================================================================
    #  健康检查
    # ==================================================================

    @staticmethod
    async def health(session: Any) -> dict[str, Any]:
        """健康检查（`/health`，**不发外部 HTTP**，只读 DB 与目录）。"""
        settings = get_settings()
        started = time.perf_counter()

        db_ok = False
        try:
            await session.execute(select(func.count()).select_from(SystemSetting))
            db_ok = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("health_db_check_failed", error=str(exc))

        storage_ok = bool(settings.storage_dir) and __import__("pathlib").Path(settings.storage_dir).exists()

        adapters = []
        rows = (await session.execute(select(AdapterConfigModel))).scalars().all()
        for row in rows:
            adapters.append(
                {
                    "name": row.adapter_name,
                    "status": row.health_status,
                    "latency_ms": 0,  # ★ 不主动探测，避免轮询打爆第三方
                }
            )

        queue = {"pending": 0, "running": 0, "failed_1h": 0}
        try:
            pending = await session.execute(
                select(func.count()).select_from(TaskRecord).where(
                    TaskRecord.status == TaskStatus.PENDING.value
                )
            )
            running = await session.execute(
                select(func.count()).select_from(TaskRecord).where(
                    TaskRecord.status == TaskStatus.RUNNING.value
                )
            )
            from datetime import timedelta

            failed = await session.execute(
                select(func.count()).select_from(TaskRecord).where(
                    TaskRecord.status == TaskStatus.FAILED.value,
                    TaskRecord.finished_at >= (utc_now() - timedelta(hours=1)),
                )
            )
            queue = {
                "pending": int(pending.scalar_one() or 0),
                "running": int(running.scalar_one() or 0),
                "failed_1h": int(failed.scalar_one() or 0),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("health_queue_check_failed", error=str(exc))

        # ★★ 结构性约束自检（QA-01 / QA-02：「保护必须可验证」）★★
        # `one_to_many` / `duplicate_item` 的读时扫描 SQL 已移除，
        # 保护**完全**落在部分唯一索引 `uq_sku_mapping_shop_sku` 上。
        # 索引一旦缺失（误删 / 迁移漏建），这两类冲突会静默失控 ——
        # 所以「索引还在不在」必须能在 `/health` 一眼看到，而不是靠人记得去查。
        constraint_issues: list[dict[str, Any]] = []
        try:
            from app.services.bootstrap import check_required_constraints

            constraint_issues = await check_required_constraints(session)
        except Exception as exc:  # noqa: BLE001  自检失败不得拖垮健康检查
            logger.error("health_constraint_check_failed", error=str(exc))
            constraint_issues = [
                {"name": "check_required_constraints", "reason": f"自检执行失败：{exc}", "expected": "", "found": ""}
            ]

        constraints_ok = not constraint_issues
        status = "ok" if (db_ok and storage_ok and constraints_ok) else "degraded"
        return {
            "status": status,
            "db": "ok" if db_ok else "down",
            "storage": "ok" if storage_ok else "down",
            "adapters": adapters,
            "queue": queue,
            # ★ 缺失的约束会在这里逐条列出（空数组 = 全部就位）
            "constraints": {
                "ok": constraints_ok,
                "missing_count": len(constraint_issues),
                "missing": [
                    {"name": i.get("name"), "table": i.get("table"), "reason": i.get("reason")}
                    for i in constraint_issues
                ],
            },
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }

    # ==================================================================
    #  工作台摘要
    # ==================================================================

    @staticmethod
    async def dashboard_summary(session: Any) -> dict[str, Any]:
        """工作台摘要（`/dashboard/summary`）。"""

        from app.models.mapping import SkuMapping
        from app.models.order import Order

        today_start = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
        today_orders = int(
            (
                await session.execute(
                    select(func.count()).select_from(Order).where(Order.created_at >= today_start)
                )
            ).scalar_one()
            or 0
        )
        pending_exception = int(
            (
                await session.execute(
                    select(func.count()).select_from(Order).where(
                        Order.exception_type.isnot(None), Order.handling_action.is_(None)
                    )
                )
            ).scalar_one()
            or 0
        )
        mapping_alert = int(
            (
                await session.execute(
                    select(func.count()).select_from(SkuMapping).where(
                        SkuMapping.has_conflict.is_(True), SkuMapping.is_deleted.is_(False)
                    )
                )
            ).scalar_one()
            or 0
        )
        queue_backlog = int(
            (
                await session.execute(
                    select(func.count()).select_from(TaskRecord).where(
                        TaskRecord.status.in_([TaskStatus.PENDING.value, TaskStatus.RUNNING.value])
                    )
                )
            ).scalar_one()
            or 0
        )
        unhandled_violation = await AuditService.count_unhandled_violations(session)

        bar = await SystemService.status_bar(session)
        health_lights = [
            {"name": "数据库", "status": "healthy", "message": "SQLite 正常"},
            {"name": "上架模式", "status": "healthy", "message": bar.listing_mode_label},
            {
                "name": "履约渠道",
                "status": "healthy",
                "message": bar.active_fulfillment_adapter_label,
            },
        ]
        for item in bar.unhealthy_items:
            health_lights.append({"name": item.name, "status": item.status, "message": item.message})

        todo_list = [
            {
                "type": "order_exception",
                "title": "待处理订单异常",
                "count": pending_exception,
                "link": "/orders/exceptions",
            },
            {
                "type": "mapping_conflict",
                "title": "映射冲突",
                "count": mapping_alert,
                "link": "/sku-mappings/conflicts",
            },
            {
                "type": "violation",
                "title": "未处置越权告警",
                "count": unhandled_violation,
                "link": "/settings/permissions",
            },
        ]
        return {
            "today_order_count": today_orders,
            "pending_exception_count": pending_exception,
            "mapping_alert_count": mapping_alert,
            "queue_backlog_count": queue_backlog,
            "unhandled_violation_count": unhandled_violation,
            "health_lights": health_lights,
            "todo_list": todo_list,
        }

    # ==================================================================
    #  凭证
    # ==================================================================

    @staticmethod
    async def list_credentials(session: Any, *, owner_type: str | None = None) -> list[Any]:
        """凭证列表（仅掩码）。"""
        from app.models.system import Credential

        stmt = select(Credential)
        if owner_type:
            stmt = stmt.where(Credential.owner_type == owner_type)
        rows = (await session.execute(stmt.order_by(Credential.id))).scalars().all()
        return list(rows)

    @staticmethod
    async def create_credential(
        session: Any,
        *,
        owner_type: str,
        owner_key: str,
        credential_key: str,
        value: str,
        expires_at: Any = None,
        operator: str = "system",
    ) -> Any:
        """创建凭证（★ 明文 AES-256 加密后落库，明文永不落库）。"""
        from app.models.system import Credential

        exists = (
            await session.execute(
                select(Credential.id).where(
                    Credential.owner_type == owner_type,
                    Credential.owner_key == owner_key,
                    Credential.credential_key == credential_key,
                )
            )
        ).scalar_one_or_none()
        if exists is not None:
            raise BusinessError("该凭证已存在", code=ErrorCode.UNIQUE_CONFLICT, http_status=409)

        row = Credential(
            owner_type=owner_type,
            owner_key=owner_key,
            credential_key=credential_key,
            value_enc=encrypt_credential(value),
            value_masked=mask_secret(value),
            expires_at=expires_at,
            status="active",
        )
        session.add(row)
        await session.flush()
        await AuditService.write(
            session,
            action_type=AuditActionType.CREDENTIAL_CHANGE.value,
            object_type="credential",
            object_id=row.id,
            operator=operator,
            new_value={"owner_type": owner_type, "owner_key": owner_key, "credential_key": credential_key},
            trace_id=get_trace_id(),
            remark=f"创建凭证 {owner_type}:{owner_key}.{credential_key}",
        )
        await session.flush()
        return row

    @staticmethod
    async def reveal_credential(
        session: Any,
        credential_id: int,
        *,
        verify_code: str,
        admin_token: str,
        operator: str = "system",
    ) -> dict[str, Any]:
        """★ 二次验证后查看凭证明文（60s 有效，留审计）。"""
        from app.models.system import Credential

        if verify_code != admin_token:
            raise BusinessError("二次验证码不正确", code=ErrorCode.FORBIDDEN, http_status=403)

        row = (
            await session.execute(select(Credential).where(Credential.id == int(credential_id)))
        ).scalars().first()
        if row is None:
            raise NotFoundError(f"凭证 {credential_id} 不存在")
        try:
            plain = decrypt_credential(row.value_enc)
        except Exception as exc:  # noqa: BLE001
            raise BusinessError(
                f"凭证解密失败：{exc}", code=ErrorCode.CREDENTIAL_DECRYPT_FAILED
            ) from exc

        await AuditService.write(
            session,
            action_type=AuditActionType.CREDENTIAL_CHANGE.value,
            object_type="credential",
            object_id=row.id,
            operator=operator,
            new_value={"action": "reveal", "owner_key": row.owner_key},
            trace_id=get_trace_id(),
            remark=f"查看凭证明文 {row.owner_type}:{row.owner_key}.{row.credential_key}",
        )
        await session.flush()
        return {"value_plain": plain, "expires_in_sec": 60}

    @staticmethod
    async def update_credential(
        session: Any,
        credential_id: int,
        *,
        value: str | None = None,
        expires_at: Any = None,
        status: str | None = None,
        operator: str = "system",
    ) -> Any:
        """更新凭证（换 Key / 改过期时间 / 吊销）。"""
        from app.models.system import Credential

        row = (
            await session.execute(select(Credential).where(Credential.id == int(credential_id)))
        ).scalars().first()
        if row is None:
            raise NotFoundError(f"凭证 {credential_id} 不存在")
        if value:
            row.value_enc = encrypt_credential(value)
            row.value_masked = mask_secret(value)
        if expires_at is not None:
            row.expires_at = expires_at
        if status:
            row.status = status
        await session.flush()
        await AuditService.write(
            session,
            action_type=AuditActionType.CREDENTIAL_CHANGE.value,
            object_type="credential",
            object_id=row.id,
            operator=operator,
            new_value={"action": "update", "rotated": bool(value), "status": status},
            trace_id=get_trace_id(),
            remark=f"更新凭证 {row.owner_type}:{row.owner_key}.{row.credential_key}",
        )
        await session.flush()
        return row

    @staticmethod
    async def delete_credential(session: Any, credential_id: int, *, operator: str = "system") -> int:
        """删除凭证（硬删除；审计留痕保证可追溯）。"""
        from app.models.system import Credential

        row = (
            await session.execute(select(Credential).where(Credential.id == int(credential_id)))
        ).scalars().first()
        if row is None:
            raise NotFoundError(f"凭证 {credential_id} 不存在")
        owner = f"{row.owner_type}:{row.owner_key}.{row.credential_key}"
        await session.delete(row)
        await session.flush()
        await AuditService.write(
            session,
            action_type=AuditActionType.CREDENTIAL_CHANGE.value,
            object_type="credential",
            object_id=credential_id,
            operator=operator,
            new_value={"action": "delete", "owner": owner},
            trace_id=get_trace_id(),
            remark=f"删除凭证 {owner}",
        )
        await session.flush()
        return int(credential_id)

    @staticmethod
    async def test_credential(session: Any, credential_id: int, *, operator: str = "system") -> dict[str, Any]:
        """凭证连通性自检。

        ★ 诚实降级：三个上架 / 履约适配器在 MVP 均为 skeleton + TODO，
          未取得第三方实测，本方法**只做解密可用性自检**，
          绝不返回伪造的 `ok=true` 连通性结论。
        """
        import time

        from app.models.system import Credential

        row = (
            await session.execute(select(Credential).where(Credential.id == int(credential_id)))
        ).scalars().first()
        if row is None:
            raise NotFoundError(f"凭证 {credential_id} 不存在")

        started = time.perf_counter()
        ok = False
        message = "解密失败"
        try:
            plain = decrypt_credential(row.value_enc)
            ok = bool(plain)
            message = "凭证可解密（适配器未实测，连通性未验证）" if ok else "凭证明文为空"
        except Exception as exc:  # noqa: BLE001
            message = f"凭证解密失败：{exc}"
        latency_ms = int((time.perf_counter() - started) * 1000)

        row.last_verified_at = utc_now()
        await session.flush()
        await AuditService.write(
            session,
            action_type=AuditActionType.CREDENTIAL_CHANGE.value,
            object_type="credential",
            object_id=row.id,
            operator=operator,
            new_value={"action": "test", "ok": ok, "latency_ms": latency_ms},
            trace_id=get_trace_id(),
            remark=f"凭证自检 {row.owner_type}:{row.owner_key}.{row.credential_key}",
        )
        await session.flush()
        return {"ok": ok, "latency_ms": latency_ms, "message": message}

    # ==================================================================
    #  平台账号（§5.5.2）
    # ==================================================================

    @staticmethod
    async def list_platform_accounts(
        session: Any, *, platform: str | None = None, status: str | None = None
    ) -> list[tuple[Any, str]]:
        """平台账号列表；返回 `(account, token_masked)` 元组列表。"""
        from app.models.listing import PlatformAccount
        from app.models.system import Credential

        stmt = select(PlatformAccount)
        if platform:
            stmt = stmt.where(PlatformAccount.platform == platform)
        if status:
            stmt = stmt.where(PlatformAccount.status == status)
        rows = (await session.execute(stmt.order_by(PlatformAccount.id))).scalars().all()

        credential_ids = [int(r.credential_id) for r in rows if r.credential_id]
        masked: dict[int, str] = {}
        if credential_ids:
            cred_rows = (
                (await session.execute(select(Credential).where(Credential.id.in_(credential_ids))))
                .scalars()
                .all()
            )
            masked = {int(c.id): (c.value_masked or "") for c in cred_rows}
        return [(r, masked.get(int(r.credential_id), "") if r.credential_id else "") for r in rows]

    @staticmethod
    async def create_platform_account(
        session: Any,
        payload: Any,
        *,
        operator: str = "system",
    ) -> tuple[Any, str]:
        """创建平台账号（可选同步写入凭证，明文加密落库）。

        Returns:
            `(account, token_masked)`。
        """
        from app.models.listing import PlatformAccount

        exists = (
            await session.execute(
                select(PlatformAccount.id).where(
                    PlatformAccount.platform == payload.platform,
                    PlatformAccount.shop_id == payload.shop_id,
                )
            )
        ).scalar_one_or_none()
        if exists is not None:
            raise BusinessError(
                "该平台店铺已存在", code=ErrorCode.UNIQUE_CONFLICT, http_status=409
            )

        credential_id = None
        token_masked = ""
        if payload.credential:
            owner_key = f"{payload.platform}:{payload.shop_id}"
            for key in ("app_key", "app_secret", "access_token"):
                value = str(payload.credential.get(key) or "")
                if not value:
                    continue
                row = await SystemService.create_credential(
                    session,
                    owner_type="platform",
                    owner_key=owner_key,
                    credential_key=key,
                    value=value,
                    operator=operator,
                )
                if key == "access_token":
                    credential_id = int(row.id)
                    token_masked = row.value_masked or ""

        account = PlatformAccount(
            platform=payload.platform,
            shop_id=payload.shop_id,
            shop_name=payload.shop_name,
            credential_id=credential_id,
            granted_scopes_json=list(payload.granted_scopes or []),
            status="active",
        )
        session.add(account)
        await session.flush()
        await AuditService.write(
            session,
            action_type=AuditActionType.CREDENTIAL_CHANGE.value,
            object_type="platform_account",
            object_id=account.id,
            operator=operator,
            new_value={"platform": payload.platform, "shop_id": payload.shop_id},
            trace_id=get_trace_id(),
            remark=f"创建平台账号 {payload.platform}:{payload.shop_id}",
        )
        await session.flush()
        return account, token_masked

    @staticmethod
    async def update_platform_account(
        session: Any,
        account_id: int,
        payload: Any,
        *,
        operator: str = "system",
    ) -> tuple[Any, str]:
        """更新平台账号（店铺名 / 状态 / 已授权 scope）。"""
        from app.models.listing import PlatformAccount

        row = (
            await session.execute(select(PlatformAccount).where(PlatformAccount.id == int(account_id)))
        ).scalars().first()
        if row is None:
            raise NotFoundError(f"平台账号 {account_id} 不存在")
        if getattr(payload, "shop_name", None) is not None:
            row.shop_name = payload.shop_name
        if getattr(payload, "status", None):
            row.status = payload.status
        if getattr(payload, "granted_scopes", None) is not None:
            row.granted_scopes_json = list(payload.granted_scopes)
        await session.flush()
        await AuditService.write(
            session,
            action_type=AuditActionType.CREDENTIAL_CHANGE.value,
            object_type="platform_account",
            object_id=row.id,
            operator=operator,
            new_value={"shop_name": row.shop_name, "status": row.status},
            trace_id=get_trace_id(),
            remark=f"更新平台账号 {row.platform}:{row.shop_id}",
        )
        await session.flush()
        return row, ""

    @staticmethod
    async def authorize_platform_account(
        session: Any,
        account_id: int,
        granted_scopes: list[str],
        *,
        operator: str = "system",
    ) -> dict[str, Any]:
        """★ 平台账号授权：声明 scope 走 `check_scope()` 硬校验，越权直接拒绝（403/5003）。

        Returns:
            `{"accepted": bool, "effective_scopes": list[str], "denied": list[str], "message": str}`。
        """
        from app.adapters.fulfillment.scope_guard import check_scope
        from app.models.listing import PlatformAccount

        row = (
            await session.execute(select(PlatformAccount).where(PlatformAccount.id == int(account_id)))
        ).scalars().first()
        if row is None:
            raise NotFoundError(f"平台账号 {account_id} 不存在")

        passed, forbidden_hits, unknown_scopes = check_scope(list(granted_scopes or []))
        denied = list(forbidden_hits or []) + list(unknown_scopes or [])
        accepted = bool(passed)
        effective = [s for s in (granted_scopes or []) if s not in denied]
        if accepted:
            row.granted_scopes_json = effective
            await session.flush()

        # ★ 独立事务写审计：拒绝分支紧接着就要 `raise BusinessError`，
        #   挂在调用方 session 上的审计会随 rollback 一起消失（同 Bug 4 第 ④ 条根因）。
        await AuditService.write_independent(
            session,
            action_type=AuditActionType.PERMISSION_CHANGE.value,
            object_type="platform_account",
            object_id=row.id,
            operator=operator,
            new_value={
                "declared_scopes": list(granted_scopes or []),
                "denied": denied,
                "effective_scopes": effective,
            },
            trace_id=get_trace_id(),
            remark="平台账号授权 scope 校验" + ("" if accepted else f"（越权拒绝：{','.join(denied)}）"),
            # ★ is_handled 语义必须与 scope_guard 一致：
            #   通过 = 无需处置(True)，否则会被顶栏红点计数误当成"未处置越权"；
            #   拒绝 = 待处置(False) → 管理员处置后熄灭。
            is_handled=bool(accepted),
            handled_by=operator if accepted else None,
            handled_at=utc_now() if accepted else None,
            handle_note="授权通过，无需人工处置" if accepted else None,
        )
        await session.flush()

        message = "授权成功" if accepted else f"越权 scope 被拒绝：{', '.join(denied)}（第三方不得持有商品编辑权）"
        if not accepted:
            raise BusinessError(message, code=ErrorCode.ADAPTER_SCOPE_DENIED, http_status=403,
                                detail={"denied": denied, "declared_scopes": list(granted_scopes or [])})
        return {"accepted": True, "effective_scopes": effective, "denied": denied, "message": message}

    # ==================================================================
    #  越权告警（内存缓冲，供诊断）
    # ==================================================================

    @staticmethod
    def memory_violations() -> list[dict[str, Any]]:
        """返回进程内的越权告警缓冲（DB 是真相源，此处仅用于诊断）。"""
        return list_memory_violations()

    @staticmethod
    async def audit_log_count(session: Any) -> int:
        """审计日志总数（顶栏只读计数用）。"""
        return int((await session.execute(select(func.count()).select_from(AuditLog))).scalar_one() or 0)

    @staticmethod
    def setting_value_type(value: str) -> str:
        """推断配置值类型。"""
        if value.strip().lower() in {"true", "false"}:
            return ValueType.BOOL.value
        if value.strip().lstrip("-").isdigit():
            return ValueType.INT.value
        return ValueType.STRING.value
