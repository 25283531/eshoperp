"""AI 客户端工厂：按 `SystemSetting['ai.client']` 选择实现。

默认客户端的**单一真相源是 `Settings.ai_client`**（当前为 `file_bridge`，与用户决策一致），
本模块不再硬编码第二个默认值。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.adapters.ai.base import AiClient
from app.adapters.ai.file_bridge import WorkBuddyFileBridgeClient
from app.adapters.ai.http_client import HttpAiClient
from app.adapters.ai.mock_client import MockAiClient
from app.core.config import get_settings
from app.core.errors import BusinessError, ErrorCode
from app.core.logging import get_logger
from app.models.enums import SettingKey
from app.models.system import SystemSetting

logger = get_logger(__name__)

__all__ = [
    "AI_CLIENT_REGISTRY",
    "DEFAULT_AI_CLIENT",
    "AiClientFactory",
    "create_ai_client",
    "instantiate_ai_client",
    "register_ai_client",
    "resolve_ai_client_name",
]

def _default_ai_client() -> str:
    """默认 AI 客户端：**单一真相源 = `Settings.ai_client`**（当前 `file_bridge`）。

    ★★ 不要在此硬编码另一个默认值 ★★
    史实隐患（产品侧发现）：此处曾硬编码 `"mock"`，而 `Settings.ai_client` 是
    `"file_bridge"` —— 同一件事存在**两个默认值**，实际生效哪个完全取决于读取顺序。
    这属于"看起来没问题"的静默不一致：哪天有人只改一处，默认到底是谁就变成需要
    查代码才能回答的问题。

    附带说明：把兜底从 `mock` 改为 `Settings.ai_client` 后，若管理员配了**不存在的**
    客户端名，将回退到 `file_bridge` 而非 `mock`。这是刻意的——回退到 `mock` 会
    **静默产出占位图文**，有被一路送上架的风险；回退到 `file_bridge` 最多是任务停在
    running，**失败是可见的**。宁可见的失败，不要静默的假内容。
    """
    try:
        return get_settings().ai_client
    except Exception:  # pragma: no cover - 配置不可用时退化为与 Settings 同值
        return "file_bridge"


DEFAULT_AI_CLIENT = _default_ai_client()

AI_CLIENT_REGISTRY: dict[str, Any] = {
    "mock": MockAiClient,
    "file_bridge": WorkBuddyFileBridgeClient,
    "http": HttpAiClient,
}


def register_ai_client(name: str, factory_fn: Any) -> None:
    """注册 / 覆盖 AI 客户端实现。"""
    AI_CLIENT_REGISTRY[name] = factory_fn
    logger.info("ai_client_registered", name=name)


async def resolve_ai_client_name(session: Any = None) -> str:
    """读取当前 AI 客户端名：优先 SystemSetting['ai.client']，回退 Settings.ai_client。

    ★★ session 为 None 时**必须自己开会话读配置**（勿改回）★★
        史实缺陷：`AiTaskService.run_task()` 调的是 `create()`（不传 session），
        于是这里直接落到 `Settings.ai_client` 的**代码默认值 file_bridge**，
        把数据库里 `ai.client` 的配置**整个绕过**了 —— 管理员在页面把客户端改成 mock，
        AI 重构依旧走 file_bridge，在没有外部 AI 代理的环境里**永远停在 running**
        （poll 超时上限 1800s），"AI 重构"这条链路因此 100% 不可用，
        而配置页上明明写着 mock —— 典型的"配置说了不算"。
        现在：没有传入会话就开一个短会话只读一次（读配置是短事务，不违反事务纪律 A）。
    """
    # ★ 自己开的会话才自己关：误关调用方的会话会让后续查询报 "session is closed"
    own_session = session is None
    if own_session:
        session = await _open_short_session()
    if session is not None:
        try:
            stmt = select(SystemSetting).where(SystemSetting.setting_key == SettingKey.AI_CLIENT.value)
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is not None and row.setting_value:
                return str(row.setting_value).strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("ai_client_setting_read_failed", error=str(exc))
        finally:
            if own_session:
                await _close_short_session(session)
    return get_settings().ai_client or DEFAULT_AI_CLIENT


async def _open_short_session() -> Any:
    """开一个只用于读配置的短会话；失败返回 None（随后回退代码默认值）。"""
    try:
        from app.core.database import get_session_factory

        return get_session_factory()()
    except Exception as exc:  # noqa: BLE001
        logger.warning("ai_client_session_open_failed", error=str(exc))
        return None


async def _close_short_session(session: Any) -> None:
    """关闭 `_open_short_session()` 打开的会话（自己开的才自己关）。"""
    try:
        await session.close()
    except Exception:  # noqa: BLE001
        pass


class AiClientFactory:
    """AI 客户端工厂。"""

    @staticmethod
    def is_registered(name: str) -> bool:
        """该客户端名是否已注册（调用方先问一句，比 catch 异常便宜）。"""
        return str(name) in AI_CLIENT_REGISTRY

    @staticmethod
    def instantiate(name: str, **kwargs: Any) -> AiClient:
        """★ **显式按名字**创建客户端：**不读任何配置、不开数据库会话**（同步）。

        ★ 为什么需要它：`AiTaskService.run_task()` 的 ② 阶段（等待 AI 产出，最长几十分钟）
          按事务纪律 A **绝不能持有会话**；而 `create()` 在 name 为空时为了读
          `SystemSetting['ai.client']` 会自己开一个会话 —— 在那里用 `create()` 就是踩线。
          "在途任务按创建时固化的 `ai_client` 跑完"这条口径，靠的就是这里的显式指定。

        Raises:
            BusinessError: 1099 —— 客户端名未注册。**绝不静默回落到默认客户端**：
                静默回落会让"按原通道跑完"的保证形同虚设（看着在跑，其实换了通道）。
        """
        client_name = str(name)
        factory_fn = AI_CLIENT_REGISTRY.get(client_name)
        if factory_fn is None:
            logger.error("ai_client_not_registered", requested=client_name)
            raise BusinessError(
                f"AI 客户端 {client_name} 未注册，可用：{', '.join(sorted(AI_CLIENT_REGISTRY))}",
                code=ErrorCode.INTERNAL_ERROR,
            )
        client = factory_fn(**kwargs) if kwargs else factory_fn()
        logger.info("ai_client_instantiated", client=client_name)
        return client

    @staticmethod
    async def create(name: str | None = None, *, session: Any = None, **kwargs: Any) -> AiClient:
        """创建 AI 客户端实例。

        Args:
            name: 客户端名（mock / file_bridge / http）；None 时读配置。
            session: 可选数据库会话（用于读 SystemSetting）。
            kwargs: 透传给客户端构造函数的参数。

        Raises:
            TypeError: `name` 不是 str/None —— 典型误用是 `create(session)`，
                把会话绑到了 `name` 形参上（`session` 是 keyword-only）。
            BusinessError: 1099 内部错误（未注册的客户端名）。
        """
        if name is not None and not isinstance(name, str):
            # ★ 踩坑记录（QA 探针 B 实测复现，P0）：
            #   `AiClientFactory.create(session)` 会把 AsyncSession 绑到第一个位置参数 name，
            #   随后 `AI_CLIENT_REGISTRY.get(session)` 命中不到 → 抛 1099，AI 重构 100% 失败。
            #   与其静默降级，不如立刻炸出来，逼调用方改成 create(session=session)。
            raise TypeError(
                "AiClientFactory.create() 的第一个位置参数是 name(str)；"
                "要传数据库会话必须用关键字：AiClientFactory.create(session=session)"
            )
        client_name = name or await resolve_ai_client_name(session)
        return AiClientFactory.instantiate(client_name, **kwargs)

    @staticmethod
    def list_clients() -> list[dict[str, Any]]:
        """列出全部 AI 客户端。"""
        return [
            {"name": name, "class": getattr(fn, "__name__", str(fn)), "is_default": name == DEFAULT_AI_CLIENT}
            for name, fn in sorted(AI_CLIENT_REGISTRY.items())
        ]


async def create_ai_client(name: str | None = None, *, session: Any = None, **kwargs: Any) -> AiClient:
    """便捷函数：等价于 `AiClientFactory.create(...)`。"""
    return await AiClientFactory.create(name, session=session, **kwargs)


def instantiate_ai_client(name: str, **kwargs: Any) -> AiClient:
    """便捷函数：等价于 `AiClientFactory.instantiate(...)`（同步、显式按名、不读配置）。"""
    return AiClientFactory.instantiate(name, **kwargs)
