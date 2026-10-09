"""★ 红线 R1：权限最小化 —— scope 白名单硬校验（§5.4）。

规则：
    1. 命中任一 `FORBIDDEN_SCOPES` → 立即拒绝（优先于白名单，双保险）；
    2. 存在不在 `ALLOWED_SCOPES` 中的 scope → 拒绝；
    3. 通过则返回生效 scope 集合（frozenset，防止后续篡改）。

拒绝时：抛 `ScopeViolationError`（code 5003）+ 写审计（action_type=permission_change）
        + 触发告警日志（PRD SYS-P0-02 验收标准）。

调用点（三处，缺一不可）：
    1. `FulfillmentAdapterFactory.create()` —— 每次实例化适配器（★ 公共路径，任何适配器绕不过）；
    2. `PUT /api/v1/adapters/fulfillment/{name}/config` —— 保存声明 scope 时；
    3. `POST /api/v1/platform-accounts/{id}/authorize` —— 授权回调落地时。
"""

from __future__ import annotations

from typing import Any, Iterable

from app.core.errors import AdapterScopeDeniedError, ErrorCode
from app.core.logging import get_logger, get_trace_id
from app.models.enums import AuditActionType, AuditObjectType
from app.utils.kit import utc_now

logger = get_logger(__name__)

# ★ 去重缓冲：同进程内「(适配器, scope 集合) 校验通过」只写一条审计。
#   拒绝记录**永不去重**（每次越权都必须留痕，这是红线 R1 的证据）。
#   目的：订单同步等高频路径每次实例化适配器都会走 scope 校验，
#        若每次都写一条"通过"审计，审计列表会被灌满、顶栏红点语义也会被稀释。
_PASSED_AUDIT_CACHE: set[tuple[str, tuple[str, ...]]] = set()
_PASSED_AUDIT_CACHE_MAX = 512


# ★ 白名单：只允许这两项，其余一律拒绝
ALLOWED_SCOPES: frozenset[str] = frozenset(
    {
        "order.read",  # 订单读取
        "logistics.write",  # 发货 / 物流回填
    }
)

# ★ 黑名单：出现任一即拒绝启用，与白名单双保险（防止白名单被误改）
FORBIDDEN_SCOPES: frozenset[str] = frozenset(
    {
        "item.write",
        "item.create",
        "item.update",
        "item.delete",
        "price.update",
        "item.publish",
        "item.offline",
        "item.edit",
        "product.write",
        "product.create",
        "product.update",
    }
)


class ScopeViolationError(AdapterScopeDeniedError):
    """越权 scope 异常。错误码 5003，必须落审计 + 告警。"""

    code = ErrorCode.ADAPTER_SCOPE_DENIED

    def __init__(
        self,
        message: str | None = None,
        *,
        adapter_name: str = "",
        forbidden: Iterable[str] = (),
        unknown: Iterable[str] = (),
        detail: Any = None,
    ) -> None:
        self.adapter_name = adapter_name
        self.forbidden_scopes = sorted(set(forbidden))
        self.unknown_scopes = sorted(set(unknown))
        detail_payload = {
            "adapter_name": adapter_name,
            "forbidden_scopes": self.forbidden_scopes,
            "unknown_scopes": self.unknown_scopes,
        }
        if isinstance(detail, dict):
            detail_payload.update(detail)
        super().__init__(
            message or f"适配器 {adapter_name} 声明了越权 scope：{', '.join(self.forbidden_scopes or self.unknown_scopes)}",
            code=ErrorCode.ADAPTER_SCOPE_DENIED,
            http_status=403,
            detail=detail_payload,
        )


# ---------------------------------------------------------------------------
#  越权记录（内存告警缓冲，供 /adapters/violations 与日志告警使用）
# ---------------------------------------------------------------------------

VIOLATION_LOG: list[dict[str, Any]] = []
_VIOLATION_LOG_MAX = 200


def record_violation(adapter_name: str, *, actor: str, forbidden: list[str], unknown: list[str],
                     trace_id: str = "") -> dict[str, Any]:
    """记录越权事件（内存缓冲 + 告警日志）。"""
    entry = {
        "adapter_name": adapter_name,
        "actor": actor,
        "forbidden_scopes": forbidden,
        "unknown_scopes": unknown,
        "trace_id": trace_id or get_trace_id(),
    }
    VIOLATION_LOG.append(entry)
    if len(VIOLATION_LOG) > _VIOLATION_LOG_MAX:
        del VIOLATION_LOG[: len(VIOLATION_LOG) - _VIOLATION_LOG_MAX]
    logger.error(
        "scope_violation",
        adapter_name=adapter_name,
        actor=actor,
        forbidden_scopes=forbidden,
        unknown_scopes=unknown,
        trace_id=entry["trace_id"],
    )
    return entry


def list_violations() -> list[dict[str, Any]]:
    """返回越权告警记录（供 API 查询）。"""
    return list(VIOLATION_LOG)


# ---------------------------------------------------------------------------
#  核心校验
# ---------------------------------------------------------------------------


def check_scope(declared_scopes: Iterable[str] | None) -> tuple[bool, list[str], list[str]]:
    """纯函数式 scope 校验，不做 IO，便于测试与复用。

    Returns:
        `(passed, forbidden_hits, unknown_scopes)`。
    """
    scopes = {str(s).strip() for s in (declared_scopes or []) if str(s).strip()}
    forbidden_hits = sorted(scopes & FORBIDDEN_SCOPES)
    unknown = sorted(scopes - ALLOWED_SCOPES - FORBIDDEN_SCOPES)
    passed = not forbidden_hits and not unknown
    return passed, forbidden_hits, unknown


def enforce_scope(
    declared_scopes: Iterable[str] | None,
    *,
    adapter_name: str,
    actor: str = "system",
    trace_id: str = "",
    audit_service: Any = None,
) -> frozenset[str]:
    """★ 权限白名单硬校验 —— 必须放在适配器初始化的公共路径上。

    Args:
        declared_scopes: 适配器声明的 scope 列表。
        adapter_name: 适配器名称（用于审计与告警）。
        actor: 操作者。
        trace_id: 链路 ID。
        audit_service: 可选审计服务（T-A06 提供）；传入时同步写审计。

    Returns:
        生效 scope 集合（frozenset，冻结防止后续篡改）。

    Raises:
        ScopeViolationError: 命中黑名单或存在白名单外 scope（code 5003）。
    """
    passed, forbidden_hits, unknown = check_scope(declared_scopes)
    if not passed:
        record_violation(
            adapter_name,
            actor=actor,
            forbidden=forbidden_hits,
            unknown=unknown,
            trace_id=trace_id,
        )
        raise ScopeViolationError(
            adapter_name=adapter_name,
            forbidden=forbidden_hits,
            unknown=unknown,
        )

    # 通过：返回冻结的生效 scope
    scopes = frozenset({str(s).strip() for s in (declared_scopes or []) if str(s).strip()})
    logger.info(
        "scope_check_passed",
        adapter_name=adapter_name,
        actor=actor,
        granted_scopes=sorted(scopes),
    )
    return scopes


async def enforce_scope_with_audit(
    declared_scopes: Iterable[str] | None,
    *,
    adapter_name: str,
    actor: str = "system",
    session: Any = None,
    audit_service: Any = None,
    trace_id: str = "",
    ip: str = "",
) -> frozenset[str]:
    """带审计落库的 scope 校验（异步路径，供 `FulfillmentAdapterFactory.create()` 调用）。

    校验核心仍是 `enforce_scope()`：
        - **拒绝**：每次必写 `permission_change` 审计（`is_handled=0`，顶栏红点）；
        - **通过**：同进程内按 (适配器, scope 集合) 去重写一条（`is_handled=1`，不计入红点），
          避免高频实例化把审计列表灌满。

    ★ 审计写入失败**绝不阻断主流程**（`_write_audit` 内部兜底）。
    """
    try:
        scopes = enforce_scope(
            declared_scopes,
            adapter_name=adapter_name,
            actor=actor,
            trace_id=trace_id,
            audit_service=audit_service,
        )
        cache_key = (str(adapter_name), tuple(sorted(scopes)))
        if cache_key not in _PASSED_AUDIT_CACHE:
            if len(_PASSED_AUDIT_CACHE) >= _PASSED_AUDIT_CACHE_MAX:
                _PASSED_AUDIT_CACHE.clear()
            _PASSED_AUDIT_CACHE.add(cache_key)
            await _write_audit(
                session=session,
                audit_service=audit_service,
                adapter_name=adapter_name,
                actor=actor,
                passed=True,
                message="scope 校验通过",
                new_value=sorted(scopes),
                trace_id=trace_id,
                ip=ip,
            )
        else:
            logger.debug(
                "scope_passed_audit_skipped_duplicate",
                adapter_name=adapter_name,
                scopes=sorted(scopes),
            )
        return scopes
    except ScopeViolationError as exc:
        await _write_audit(
            session=session,
            audit_service=audit_service,
            adapter_name=adapter_name,
            actor=actor,
            passed=False,
            message=exc.message,
            new_value={
                "declared_scopes": sorted({str(s) for s in (declared_scopes or [])}),
                "forbidden_scopes": exc.forbidden_scopes,
                "unknown_scopes": exc.unknown_scopes,
            },
            trace_id=trace_id,
            ip=ip,
        )
        raise


async def _write_audit(
    *,
    session: Any,
    audit_service: Any,
    adapter_name: str,
    actor: str,
    passed: bool,
    message: str,
    new_value: Any,
    trace_id: str,
    ip: str,
) -> None:
    """写审计日志：优先用 AuditService，失败则直接写 AuditLog 表（绝不因审计失败阻断主流程）。

    ★ Bug 4 修复（越权被拒但审计 0 条，红线 R1 静默失效）的四个根因，逐条对应下面 ①②③④：

        ① `AuditService.write()` 的第一个参数是**位置参数 `session`**，
           旧代码写的是 `audit_service.write(**payload)` → 必抛 TypeError
           → "拒绝"审计一条都写不进去（顶栏告警恒为 0、权限页恒空，
             安全功能伪装成"一切正常"，比功能不可用更危险）。
        ② 旧代码把「AuditService 写入」和「独立会话兜底写 AuditLog」放在**同一个 try** 里，
           前者一抛异常就跳到 except，**兜底路径也被跳过**。现在两段各自独立 try：
           ① 失败 → 记 warning 后**继续**走 ②，绝不因为一条路失败就不留痕。
        ③ `is_handled` 语义：**拒绝 → False**（顶栏红点待处置），
           **通过 → True**（本就无需处置），否则"通过"记录会被
           `count_unhandled_violations()` 误计成未处置越权、把红点计数灌爆。
        ④ ★ 最关键的一条：审计**必须用自己的独立事务并立即 commit**，
           绝不能挂在调用方 session 上 —— 拒绝路径会 `raise`，调用方要么回滚、
           要么（如 `/adapters/fulfillment/{name}/test`）根本不 commit，
           挂在它身上的审计会随事务一起消失。这正是"拦住了却没留痕"的直接成因。

    ★★ 实现说明：第 ④ 条的具体做法（独立事务 / SQLite 单写者下的方言分流 / commit 时机）
        统一收敛在 `AuditService.write_independent()` 里，本函数只负责拼装字段。
        此前这里有一份**平行的独立会话实现**，两份实现已经出现行为漂移：
        本份在 SQLite 下会白等满 `busy_timeout`（8s）再失败，另一份不会。
        保留一份实现，避免"修了一处、另一处继续静默失效"——
        这正是本项目已经踩过三次的同类事故。

    Args:
        audit_service: 保留参数以兼容既有调用点；审计写入已统一走 `AuditService`。
    """
    payload = {
        "action_type": AuditActionType.PERMISSION_CHANGE.value,
        "object_type": AuditObjectType.ADAPTER.value,
        "object_id": adapter_name,
        "operator": actor,
        "operator_role": "system",
        "new_value": {
            "scope_check_status": "passed" if passed else "rejected",
            "detail": new_value,
        },
        "remark": message,
        "trace_id": trace_id or get_trace_id(),
        "ip": ip,
        # ★ ③ is_handled 语义：通过=无需处置(True)；拒绝=待处置(False)
        "is_handled": bool(passed),
        "handled_by": actor if passed else None,
        "handled_at": utc_now() if passed else None,
        "handle_note": "scope 校验通过，无需人工处置" if passed else None,
    }

    # ★ ④ 独立事务写审计 —— 唯一实现在 AuditService.write_independent()：
    #     SQLite 单写者方言分流、commit 时机、失败兜底都在那里，此处不再重复造一份。
    from app.services.audit_service import AuditService

    try:
        await AuditService.write_independent(session, **payload)
    except Exception as exc:  # noqa: BLE001  审计失败绝不阻断主流程
        logger.error(
            "audit_write_failed",
            error=str(exc),
            adapter_name=adapter_name,
            passed=passed,
        )


def scope_policies() -> dict[str, Any]:
    """返回 scope 策略（供 `GET /adapters/scope-policies`）。"""
    return {
        "allowed": sorted(ALLOWED_SCOPES),
        "forbidden": sorted(FORBIDDEN_SCOPES),
        "description": "第三方分销工具仅允许订单读取与物流回填，任何商品编辑 / 上架 / 下架 / 改价权限一律拒绝。",
    }


__all__ = [
    "ALLOWED_SCOPES",
    "FORBIDDEN_SCOPES",
    "ScopeViolationError",
    "VIOLATION_LOG",
    "check_scope",
    "enforce_scope",
    "enforce_scope_with_audit",
    "list_violations",
    "record_violation",
    "scope_policies",
]
