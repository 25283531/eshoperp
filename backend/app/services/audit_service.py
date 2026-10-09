"""★ 审计服务 —— 全系统审计埋点的**唯一入口**（§10.7）。

纪律：
    1. 各 Service **禁止**自己 `session.add(AuditLog(...))`，一律调 `AuditService.write()`；
    2. 审计保留 ≥180 天，不软删除；
    3. 越权拦截必须埋点（`permission_change`），这是红线 R1 的审计证据；
    4. 审计写入失败**不得阻断主流程**。

★ v1.2 处置态闭环（`permission_change` 专用）：
    - 越权被拒写入时 `is_handled=0` → 顶栏红点亮；
    - 管理员处置后 `is_handled=1` → 红点熄灭；
    - **处置动作本身再埋一条 `permission_change`**（`handle_note` 记处置说明），
      即「被拒」与「被处置」两条记录**成对存在**（附录 A 第 16 条）。

★ v1.4 审计噪音控制：
    系统自动产生的记录（`cost_sync` 等）默认从列表排除，
    传 `include_system=true` 才看全量。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from app.core.logging import get_logger, get_trace_id
from app.models.enums import AuditActionType
from app.models.system import AuditLog
from app.utils.kit import utc_now

logger = get_logger(__name__)

__all__ = ["AuditService", "SYSTEM_ACTION_TYPES"]

# ★ 系统自动产生的动作类型：审计列表默认排除（v1.4 噪音控制）
#   成本同步等系统记录量极大，会淹没人工改动记录。
SYSTEM_ACTION_TYPES: frozenset[str] = frozenset(
    {
        AuditActionType.COST_SYNC.value,
    }
)


class AuditService:
    """审计日志写入与查询。"""

    # ------------------------------------------------------------------
    #  写入
    # ------------------------------------------------------------------

    @staticmethod
    async def write(
        session: Any,
        *,
        action_type: str,
        object_type: str,
        object_id: Any = None,
        operator: str = "system",
        operator_role: str = "system",
        old_value: Any = None,
        new_value: Any = None,
        ip: str = "",
        trace_id: str = "",
        remark: str = "",
        is_handled: bool = False,
        handled_by: str | None = None,
        handled_at: Any = None,
        handle_note: str | None = None,
        commit: bool = False,
    ) -> AuditLog:
        """★ 统一审计写入入口。

        Args:
            session: AsyncSession（调用方事务）。
            action_type: 见 `AuditActionType`。
            object_type: 见 `AuditObjectType`。
            object_id: 对象 ID（越权记录传 adapter_name）。
            operator: 操作人。
            operator_role: admin / operator / system。
            old_value / new_value: 变更前后的值（dict 会自动 JSON 序列化）。
            ip / trace_id / remark: 链路信息。
            is_handled / handled_by / handled_at / handle_note: ★ 越权处置态四字段。
            commit: 是否立即提交（默认 False，随调用方事务提交）。

        Returns:
            已 add 的 `AuditLog` 实例（未 flush，除非 commit=True）。
        """
        entry = AuditLog.build(
            action_type=action_type,
            object_type=object_type,
            object_id=object_id,
            operator=operator,
            operator_role=operator_role,
            old_value=old_value,
            new_value=new_value,
            ip=ip,
            trace_id=trace_id or get_trace_id(),
            remark=remark,
            is_handled=is_handled,
            handled_by=handled_by,
            handled_at=handled_at,
            handle_note=handle_note,
        )
        try:
            session.add(entry)
            if commit:
                await session.flush()
                await session.commit()
        except Exception as exc:  # noqa: BLE001  ★ 审计失败不得阻断主流程
            logger.warning("audit_write_failed", error=str(exc), action_type=action_type)
        return entry

    @staticmethod
    async def write_independent(fallback_session: Any = None, **kwargs: Any) -> AuditLog | None:
        """★ **独立事务**写审计 —— 专供「写完就要 raise」的拒绝路径。

        ★★ 为什么必须有这个方法（Bug 4 第 ④ 条根因的通解）★★
            拒绝路径的典型形状是：写审计 → `raise BusinessError`。
            若审计挂在**调用方** session 上，调用方随后要么显式 rollback、
            要么根本不 commit（`get_db()` 只在异常时才 rollback，正常路径也不 commit），
            这条审计就会随事务一起消失 —— 「拦住了，却没留痕」，
            顶栏红点恒为 0，安全功能伪装成一切正常。

            典型的漏痕点（均已改用本方法）：
                * `PUT /adapters/fulfillment/{name}/config` 声明越权 scope → 403；
                * `POST /platform-accounts/{id}/authorize` 索要越权 scope → 403。

        Args:
            fallback_session: 独立事务也写不进去时（如 SQLite 被他人持写锁）
                退回补写的会话；不传则放弃但记 warning。
            **kwargs: 与 `write()` 完全相同的审计字段。

        ★★ SQLite 下为何**不走**独立连接（实测结论，勿改回）★★
            SQLite 是单写者：调用方 session 只要持有未提交的写事务，
            独立连接的 INSERT 就会一直阻塞到 `busy_timeout`（本项目 8000ms）
            然后抛 `database is locked`。取证脚本里实测：拒绝路径因此**卡 8 秒**
            且必然失败，再退回调 fallback —— 留痕虽然成功，但每个越权请求
            白白多等 8 秒并刷一条 WARNING。
            SQLite 单进程部署下独立事务本来就换不来「抗回滚」的收益，
            故此处按方言分流：SQLite 直接写在调用方 session 上；PostgreSQL
            才走独立连接。

        ★★ SQLite 分支为什么必须 `commit=True` ★★
            不是所有拒绝路径的调用方都会「先 commit 再 raise」。
            反例 `SystemService.authorize_platform_account()`：它在服务层
            直接 `raise BusinessError`，路由里的 `await session.commit()`
            **永远执行不到**，而 `get_db()` 在异常路径上是 rollback ——
            只 flush 不 commit 的审计会被回滚掉（取证脚本 ⑥ 因此从 ✅ 变 ❌）。
            所以这里显式 commit：拒绝路径此时除审计外没有别的待写状态，
            立即提交不会误提交业务数据，却能让审计活过紧随其后的 `raise`。

        Returns:
            写入的 `AuditLog`；彻底失败时返回 `None`（★ 审计失败绝不阻断主流程）。
        """
        from app.core.config import get_settings
        from app.core.database import get_session_factory

        if get_settings().is_sqlite:
            if fallback_session is None:
                logger.warning(
                    "audit_write_skipped_no_session",
                    action_type=kwargs.get("action_type"),
                )
                return None
            try:
                return await AuditService.write(fallback_session, commit=True, **kwargs)
            except Exception as exc:  # noqa: BLE001
                logger.warning("audit_fallback_write_failed", error=str(exc))
                return None

        try:
            factory = get_session_factory()
            async with factory() as own_session:
                entry = await AuditService.write(own_session, commit=True, **kwargs)
                logger.info(
                    "audit_written_independently",
                    action_type=kwargs.get("action_type"),
                    audit_id=int(entry.id) if getattr(entry, "id", None) else None,
                )
                return entry
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "audit_independent_write_failed",
                error=str(exc),
                action_type=kwargs.get("action_type"),
            )

        if fallback_session is None:
            return None
        try:
            return await AuditService.write(fallback_session, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("audit_fallback_write_failed", error=str(exc))
            return None

    # ------------------------------------------------------------------
    #  查询
    # ------------------------------------------------------------------

    @staticmethod
    async def list_logs(
        session: Any,
        *,
        action_type: str | None = None,
        object_type: str | None = None,
        object_id: str | None = None,
        operator: str | None = None,
        created_from: Any = None,
        created_to: Any = None,
        include_system: bool = False,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[AuditLog], int]:
        """分页查询审计日志。

        Args:
            include_system: 默认 False → **排除系统自动产生的记录**（v1.4 噪音控制）。

        Returns:
            `(日志列表, 总数)`。
        """
        stmt = select(AuditLog)
        if action_type:
            stmt = stmt.where(AuditLog.action_type == action_type)
        if object_type:
            stmt = stmt.where(AuditLog.object_type == object_type)
        if object_id:
            stmt = stmt.where(AuditLog.object_id == str(object_id))
        if operator:
            stmt = stmt.where(AuditLog.operator == operator)
        if created_from is not None:
            stmt = stmt.where(AuditLog.created_at >= created_from)
        if created_to is not None:
            stmt = stmt.where(AuditLog.created_at <= created_to)
        if not include_system:
            stmt = stmt.where(AuditLog.action_type.notin_(sorted(SYSTEM_ACTION_TYPES)))

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await session.execute(count_stmt)).scalar_one() or 0)
        rows = (
            (await session.execute(stmt.order_by(AuditLog.created_at.desc()).offset((page - 1) * page_size).limit(page_size)))
            .scalars()
            .all()
        )
        return list(rows), total

    # ------------------------------------------------------------------
    #  越权告警处置闭环（SYS-P0-05 / SYS-P0-06）
    # ------------------------------------------------------------------

    @staticmethod
    async def count_unhandled_violations(session: Any) -> int:
        """统计未处置的越权告警数（顶栏红点计数）。

        ★ 只读计数，不触发任何外部 HTTP。
        """
        stmt = select(func.count()).select_from(AuditLog).where(
            AuditLog.action_type == AuditActionType.PERMISSION_CHANGE.value,
            AuditLog.is_handled.is_(False),
        )
        return int((await session.execute(stmt)).scalar_one() or 0)

    @staticmethod
    async def latest_unhandled_violation(session: Any) -> AuditLog | None:
        """取最近一条未处置的越权告警。"""
        stmt = (
            select(AuditLog)
            .where(
                AuditLog.action_type == AuditActionType.PERMISSION_CHANGE.value,
                AuditLog.is_handled.is_(False),
            )
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalars().first()

    @staticmethod
    async def list_violations(
        session: Any,
        *,
        is_unhandled: bool | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[AuditLog], int]:
        """分页查询越权告警记录。

        Args:
            is_unhandled: True → 只看未处置（顶栏红点判定用）。
        """
        stmt = select(AuditLog).where(
            AuditLog.action_type == AuditActionType.PERMISSION_CHANGE.value
        )
        if is_unhandled:
            stmt = stmt.where(AuditLog.is_handled.is_(False))

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await session.execute(count_stmt)).scalar_one() or 0)
        rows = (
            (await session.execute(stmt.order_by(AuditLog.created_at.desc()).offset((page - 1) * page_size).limit(page_size)))
            .scalars()
            .all()
        )
        return list(rows), total

    @staticmethod
    async def handle_violation(
        session: Any,
        violation_id: int,
        *,
        operator: str,
        handle_note: str = "",
        trace_id: str = "",
        ip: str = "",
    ) -> AuditLog:
        """★ 处置越权告警（仅管理员可调，权限校验在 API 层）。

        处置后：
            1. 原记录 `is_handled=1` + `handled_by/handled_at/handle_note`；
            2. **再埋一条 `permission_change` 审计**（处置说明），
               使「被拒」与「被处置」成对存在（附录 A 第 16 条）。

        Raises:
            BusinessError: 1004 记录不存在。
        """
        from app.core.errors import NotFoundError

        stmt = select(AuditLog).where(
            AuditLog.id == int(violation_id),
            AuditLog.action_type == AuditActionType.PERMISSION_CHANGE.value,
        )
        entry = (await session.execute(stmt)).scalars().first()
        if entry is None:
            raise NotFoundError(f"越权告警记录 {violation_id} 不存在")

        if entry.is_handled:
            return entry

        entry.mark_handled(operator=operator, note=handle_note)

        # ★ 处置动作本身也要埋点 → 成对闭环
        await AuditService.write(
            session,
            action_type=AuditActionType.PERMISSION_CHANGE.value,
            object_type=entry.object_type,
            object_id=entry.object_id,
            operator=operator,
            operator_role="admin",
            old_value={"is_handled": False},
            new_value={
                "scope_check_status": "handled",
                "handled_violation_id": int(violation_id),
                "handle_note": handle_note,
            },
            ip=ip,
            trace_id=trace_id,
            remark=f"管理员 {operator} 处置了越权告警 #{violation_id}",
            is_handled=True,
            handled_by=operator,
            handled_at=utc_now(),
            handle_note=handle_note,
        )
        await session.flush()
        return entry
