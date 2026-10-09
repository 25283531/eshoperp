"""FulfillmentAdapterFactory —— 履约适配器工厂（★ 红线 R1 的唯一公共实例化路径）。

★ ★ 关键设计（ARCH §5.4）★ ★
`enforce_scope()` **必须放在本文件的 `create()` 中**。因为 `create()` 是适配器实例化的
唯一公共路径（API 调用、定时任务、心跳、恢复流程都走它），把校验放在这里，
任何适配器（包括未来新增的）都绕不过 scope 校验。

热切换：读 `SystemSetting['fulfillment.active_adapter']`，默认 `local_csv`（ARCH §11.2 N3）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.adapters.fulfillment.base import FulfillmentAdapter
from app.adapters.fulfillment.manifest import AdapterConfig, CredentialBundle
from app.adapters.fulfillment.registry import ADAPTER_REGISTRY, get_adapter_class
from app.adapters.fulfillment.scope_guard import ScopeViolationError, enforce_scope_with_audit
from app.core.config import get_settings
from app.core.errors import BusinessError, ErrorCode
from app.core.logging import get_logger, get_trace_id
from app.models.enums import AdapterName, SettingKey
from app.models.order import FulfillmentAdapterConfig as FulfillmentAdapterConfigModel
from app.models.system import SystemSetting

logger = get_logger(__name__)

__all__ = [
    "DEFAULT_ADAPTER",
    "FulfillmentAdapterFactory",
    "get_active_adapter_name",
    "get_fulfillment_adapter",
]

DEFAULT_ADAPTER = AdapterName.LOCAL_CSV.value

# ★ QA-13：当「无法确认适配器声明了哪些 scope」时，用它走一遍 scope 校验。
#   它既不在白名单也不在黑名单 ⇒ 必然判为 unknown ⇒ 拒绝 + 落审计（fail-closed）。
UNVERIFIABLE_SCOPE = "<scope.unverifiable>"


def _ensure_registered() -> None:
    """确保三个内置适配器均已注册（惰性导入，避免循环依赖）。

    注册顺序：`local_csv` / `miaoshou` / `yitao`（导入即触发 `@register` 装饰器）。
    """
    if ADAPTER_REGISTRY:
        return
    from app.adapters.fulfillment.local_csv import LocalCsvAdapter  # noqa: F401
    from app.adapters.fulfillment.miaoshou import MiaoshouAdapter  # noqa: F401
    from app.adapters.fulfillment.yitao import YitaoAdapter  # noqa: F401


async def get_active_adapter_name(session: Any = None) -> str:
    """读取当前生效适配器名：优先 SystemSetting，回退 Settings，再回退 local_csv。"""
    if session is not None:
        try:
            stmt = select(SystemSetting).where(
                SystemSetting.setting_key == SettingKey.FULFILLMENT_ACTIVE_ADAPTER.value
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is not None and row.setting_value:
                return str(row.setting_value).strip()
        except Exception as exc:  # noqa: BLE001  迁移未执行时表不存在 → 回退默认
            logger.warning("active_adapter_read_failed", error=str(exc))
    return get_settings().fulfillment_active_adapter or DEFAULT_ADAPTER


class FulfillmentAdapterFactory:
    """履约适配器工厂（★ 所有实例化必经 scope 校验）。"""

    @staticmethod
    async def load_config(session: Any, adapter_name: str) -> tuple[AdapterConfig, list[str] | None]:
        """从 `fulfillment_adapter` 表加载配置与声明 scope。

        ★★ QA-13：「读不到声明 scope」必须**可区分**于「声明了空 scope」★★
            旧实现在 `session is None` / 查表抛异常 / 表里没这行 三种情形下
            一律返回 `[]`，而 `check_scope([])` **恒通过**
            ⇒ 库里明明写着 `item.write` 的适配器，只要这次调用没带 session
              就被静默放行 —— 红线 R1 在「读不到配置」时被悄悄绕过。

            现在用 `None` 明确表达「**无法确认**」：
            调用方（`create()`）必须对 `None` **fail-closed**（拒绝实例化），
            只有真正读到空 scope 列表才算「该适配器不申请任何权限」。

        Returns:
            `(AdapterConfig, declared_scopes)`：
                `declared_scopes=None` 表示**无法确认**（无会话 / 读取失败）；
                `[]` 表示确实声明了零个 scope。
        """
        config = AdapterConfig(adapter_name=adapter_name)
        if session is None:
            return config, None
        try:
            stmt = select(FulfillmentAdapterConfigModel).where(
                FulfillmentAdapterConfigModel.adapter_name == adapter_name
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
        except Exception as exc:  # noqa: BLE001
            logger.error("adapter_config_read_failed", adapter=adapter_name, error=str(exc))
            return config, None
        if row is None:
            # 表里没有这个适配器 ⇒ 无法确认它申请了什么权限（不能当成"没申请"）
            return config, None
        config = AdapterConfig.from_dict(
            row.config_json or {}, adapter_name=adapter_name, display_name=row.display_name
        )
        scopes = [str(s) for s in (row.declared_scopes_json or [])]
        return config, scopes

    @staticmethod
    async def create(
        adapter_name: str | None = None,
        *,
        session: Any = None,
        credential: CredentialBundle | None = None,
        http: Any = None,
        actor: str = "system",
        audit_service: Any = None,
        config: AdapterConfig | None = None,
        strict_scope: bool = True,
        fallback_to_default: bool = True,
    ) -> FulfillmentAdapter:
        """★ 创建履约适配器实例（唯一公共路径 —— scope 红线在此强制生效）。

        流程：
            1. 解析适配器名（None → `SystemSetting['fulfillment.active_adapter']`）；
            2. 注册表取类（未注册 → 5001 或回退默认）；
            3. 加载适配器配置与声明 scope；
            4. **★ `enforce_scope_with_audit()` 校验**（命中黑名单 / 白名单外一律拒绝 + 审计 + 告警）；
            5. 实例化并返回。

        Raises:
            BusinessError: 5001 适配器不存在且不允许回退；5003 越权 scope。
        """
        _ensure_registered()

        name = adapter_name or await get_active_adapter_name(session)
        adapter_cls = get_adapter_class(name)

        if adapter_cls is None:
            if not fallback_to_default:
                raise BusinessError(f"履约适配器 {name} 未注册", code=ErrorCode.ADAPTER_UNAVAILABLE)
            logger.error("fulfillment_adapter_not_found_fallback", requested=name, fallback=DEFAULT_ADAPTER)
            name = DEFAULT_ADAPTER
            adapter_cls = get_adapter_class(name)
            if adapter_cls is None:  # pragma: no cover  内置适配器必然已注册
                raise BusinessError(
                    f"默认履约适配器 {DEFAULT_ADAPTER} 未注册", code=ErrorCode.ADAPTER_UNAVAILABLE
                )

        # 加载配置与声明 scope（★ `None` = 无法确认，不是"没申请权限"）
        loaded_config, declared_scopes = await FulfillmentAdapterFactory.load_config(session, name)
        if config is not None:
            loaded_config = config

        # ★★★ 红线 R1：scope 白名单硬校验（唯一公共路径，任何适配器绕不过）★★★
        if strict_scope:
            if declared_scopes is None:
                # ★★ QA-13：无法确认权限 ⇒ 拒绝（fail-closed）★★
                #    旧行为：读不到配置就当空 scope 放行 —— 库里写着 `item.write`
                #    的适配器，只要调用方没传 session 就被静默创建出来。
                #    红线宁可"拒绝一个本可放行的适配器"，也不能"放行一个越权的"。
                try:
                    await enforce_scope_with_audit(
                        [UNVERIFIABLE_SCOPE],
                        adapter_name=name,
                        actor=actor,
                        session=session,
                        audit_service=audit_service,
                        trace_id=get_trace_id(),
                    )
                except ScopeViolationError as exc:
                    raise ScopeViolationError(
                        f"无法确认适配器 {name} 的声明 scope"
                        f"（缺少数据库会话，或 `fulfillment_adapter` 表读取失败 / 无该行）。"
                        f"红线 R1 按「无法确认权限 = 拒绝」处理，请传入有效 session 并先完成适配器配置",
                        adapter_name=name,
                        unknown=[UNVERIFIABLE_SCOPE],
                    ) from exc
            else:
                await enforce_scope_with_audit(
                    declared_scopes,
                    adapter_name=name,
                    actor=actor,
                    session=session,
                    audit_service=audit_service,
                    trace_id=get_trace_id(),
                )

        adapter = adapter_cls(
            config=loaded_config,
            credential=credential,
            http=http,
            session=session,
        )
        logger.info(
            "fulfillment_adapter_created",
            adapter=name,
            actor=actor,
            scopes=sorted(declared_scopes or []),
        )
        return adapter

    @staticmethod
    async def create_diagnostic(adapter_name: str, *, session: Any = None) -> FulfillmentAdapter:
        """诊断用：跳过 scope 校验实例化（仅供「能力矩阵预览 / 连通性自检」使用）。

        ★ 该实例**不得**进入真实履约链路；真实链路一律用 `create()`。
        """
        _ensure_registered()
        adapter_cls = get_adapter_class(adapter_name)
        if adapter_cls is None:
            raise BusinessError(f"履约适配器 {adapter_name} 未注册", code=ErrorCode.ADAPTER_UNAVAILABLE)
        config, _ = await FulfillmentAdapterFactory.load_config(session, adapter_name)
        return adapter_cls(config=config, credential=None, http=None, session=session)

    @staticmethod
    def registered_names() -> list[str]:
        """已注册适配器名列表。"""
        _ensure_registered()
        return sorted(ADAPTER_REGISTRY.keys())

    @staticmethod
    async def list_manifests(session: Any = None) -> list[dict[str, Any]]:
        """列出全部适配器能力矩阵（供 `GET /adapters/fulfillment`）。"""
        _ensure_registered()
        items: list[dict[str, Any]] = []
        active = await get_active_adapter_name(session)
        for name in sorted(ADAPTER_REGISTRY.keys()):
            try:
                adapter = await FulfillmentAdapterFactory.create_diagnostic(name, session=session)
                items.append(
                    {
                        "adapter_name": name,
                        "display_name": adapter.display_name,
                        "is_active": name == active,
                        "manifest": adapter.manifest.to_dict(),
                        "required_scopes": adapter.manifest.required_scopes,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("adapter_manifest_build_failed", adapter=name, error=str(exc))
                items.append({"adapter_name": name, "display_name": name, "is_active": name == active, "error": str(exc)})
        return items


async def get_fulfillment_adapter(adapter_name: str | None = None, **kwargs: Any) -> FulfillmentAdapter:
    """便捷函数：等价于 `FulfillmentAdapterFactory.create(...)`。"""
    return await FulfillmentAdapterFactory.create(adapter_name, **kwargs)
