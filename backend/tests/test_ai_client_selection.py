"""★ AI 客户端选择必须真正受 `ai.client` 配置驱动（从零全链路验证暴露的缺陷）。

背景（真实复现，不是推测）：
    从零跑全链路时，AI 重构任务**永远停在 running**。排查结论是：
    `AiTaskService.run_task()` 调的是 `AiClientFactory.create()`（不传 session），
    而旧实现在没有 session 时直接回退到 `Settings.ai_client` 的**代码默认值
    file_bridge**，把数据库里 `ai.client` 的配置**整个绕过**了。
    后果：管理员在配置页把客户端改成 `mock`，AI 重构依旧走 file_bridge，
    在没有外部 AI 代理的环境里一直空等（poll 超时上限 1800s），
    这条链路 100% 不可用，而配置页上明明写着 mock —— 配置说了不算。

这两条用例把「配置必须生效」钉死，防止再退回到"默认值悄悄接管"。
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from app.adapters.ai.factory import AiClientFactory
from app.adapters.ai.file_bridge import WorkBuddyFileBridgeClient
from app.adapters.ai.mock_client import MockAiClient
from app.models.enums import SettingKey
from app.models.system import SystemSetting

pytestmark = pytest.mark.asyncio


async def _set_ai_client(session: Any, value: str) -> None:
    """把 `ai.client` 配置改成指定值并**提交**。

    ★ 必须提交：`AiClientFactory.create()` 不传 session 时会**自己开一个会话**
    （另一条连接）去读配置；只 flush 不提交的话，那条连接根本看不到这次改动
    —— 这正是"改了配置却不生效"在测试里最容易被误判的地方。
    """
    row = (
        await session.execute(
            select(SystemSetting).where(SystemSetting.setting_key == SettingKey.AI_CLIENT.value)
        )
    ).scalars().first()
    if row is None:
        session.add(
            SystemSetting(
                setting_key=SettingKey.AI_CLIENT.value,
                setting_value=value,
                value_type="string",
                description="AI 客户端（测试临时改写）",
            )
        )
    else:
        row.setting_value = value
    await session.commit()


async def test_ai_client_setting_is_honored_without_session(session: Any) -> None:
    """★ 不传 session 时也必须按 `ai.client` 配置选客户端（不能退回代码默认值）。"""
    await _set_ai_client(session, "mock")
    try:
        client = await AiClientFactory.create()  # ★ 刻意不传 session
        assert isinstance(client, MockAiClient), (
            f"配置 ai.client=mock 时不传 session 也应拿到 MockAiClient，实际 {type(client).__name__}"
        )
    finally:
        await _set_ai_client(session, "file_bridge")


async def test_ai_client_follows_setting_change(session: Any) -> None:
    """★ 改回 file_bridge，同一个调用方式必须跟着变 —— 证明读的是配置不是常量。"""
    await _set_ai_client(session, "file_bridge")

    client = await AiClientFactory.create()
    assert isinstance(client, WorkBuddyFileBridgeClient), (
        f"配置 ai.client=file_bridge 时应拿到 WorkBuddyFileBridgeClient，实际 {type(client).__name__}"
    )
