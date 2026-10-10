"""★ AI 子系统数据层（0004）回归：`task_type` / `input_prompt_json` / `ai_client` + 新数据契约。

本次交付的**全部**内容都在数据层：三列、迁移 0004、枚举与 ENUM_DICT、标题多候选契约、
`AiTaskContext` 逐图提示词契约。因此本文件只验证"结构对不对、旧契约有没有被改坏"，
**不**涉及任何调用 AI / 生成任务的业务逻辑（那属于下一棒）。

逐条对应交付项：
    ① `AiTask` 三列可写可读、存量口径默认值为 `ai_rework` / `file_bridge`；
    ② `AiInputPrompt` 能同时表达**全局**与**逐图**提示词，且 JSON 往返无损；
    ③ `AiTitleResult` 新增 `candidates`，但**旧五个字段与旧消费方口径不变**；
    ④ `AiTaskContext` 带上逐图提示词，且**旧版 task.json（无新键）仍能反序列化**；
    ⑤ `ENUM_DICT` 补齐 AI 相关枚举，`GET /settings/enums` 能取到。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.adapters.ai import (
    AiImagePrompt,
    AiInputPrompt,
    AiReworkResult,
    AiTaskContext,
    AiTitleCandidate,
    AiTitleResult,
)
from app.adapters.ai.mock_client import MockAiClient
from app.models.asset import AiTask, AiTaskResult
from app.models.enums import AiClientName, AiTaskStatus, AiTaskType, SettingKey
from app.models.source import SourceProduct
from app.models.system import SystemSetting
from app.services.ai_task_service import AiTaskService


def _uniq(prefix: str) -> str:
    """生成唯一串（避开 `product_1688_id` 的唯一约束）。"""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


async def _make_product(session: object) -> SourceProduct:
    """建一个货源商品（AI 任务的外键宿主）。"""
    product = SourceProduct(product_1688_id=_uniq("ai-layer"), title="0004 数据层自检商品")
    session.add(product)
    await session.commit()
    return product


# ======================================================================
#  ① AiTask 三列
# ======================================================================


async def test_new_columns_persist_and_read_back(session: object) -> None:
    """三列必须真的落库并读得回来（图片重绘任务 + 逐图提示词 + mock 客户端）。"""
    product = await _make_product(session)
    prompt = AiInputPrompt(
        global_prompt="整体偏日式极简",
        images=[AiImagePrompt(index=0, prompt="主图换成暖色背景", asset_id=11, tag="main_image"),
                AiImagePrompt(index=1, prompt="详情图突出尺寸对比")],
        title_prompt="偏抖音热词风",
    )
    task = AiTask(
        source_product_id=int(product.id),
        target_platform="taobao",
        task_type=AiTaskType.IMAGE_REDRAW.value,
        input_prompt_json=prompt.to_dict(),
        ai_client=AiClientName.MOCK.value,
        rework_items_json=["main_image", "detail_image"],
        status=AiTaskStatus.QUEUED.value,
    )
    session.add(task)
    await session.commit()
    task_id = int(task.id)

    row = (await session.execute(select(AiTask).where(AiTask.id == task_id))).scalars().first()
    assert row is not None
    assert row.task_type == AiTaskType.IMAGE_REDRAW.value
    assert row.ai_client == AiClientName.MOCK.value

    back = AiInputPrompt.from_dict(row.input_prompt_json)
    assert back.global_prompt == "整体偏日式极简"
    assert back.image_prompt(0) == "主图换成暖色背景"
    assert back.image_prompt(1) == "详情图突出尺寸对比"
    assert back.effective_prompt(0) == "主图换成暖色背景"  # 逐图优先
    assert back.effective_prompt(2) == "整体偏日式极简"  # 该图没给 → 回退全局
    assert back.title_prompt == "偏抖音热词风"
    assert back.images[0].asset_id == 11
    # ★ 子项列表语义不变：仍是"要做哪几项"，与 task_type 正交
    assert list(row.rework_items_json or []) == ["main_image", "detail_image"]


async def test_legacy_shape_gets_documented_defaults(session: object) -> None:
    """★ 存量口径：不传新列时，行必须用**与历史语义相符**的默认值补齐。

    `ai_rework` = 0004 之前创建的任务全部是老"图文重构"；
    `file_bridge` = README 第三节文档默认客户端（迁移 0004 用同一个值回填老行）。
    """
    product = await _make_product(session)
    task = AiTask(
        source_product_id=int(product.id),
        target_platform="pdd",
        rework_items_json=["title"],
        status=AiTaskStatus.QUEUED.value,
    )
    session.add(task)
    await session.flush()

    assert task.task_type == AiTaskType.AI_REWORK.value
    assert task.ai_client == AiClientName.FILE_BRIDGE.value
    await session.commit()

    row = (await session.execute(select(AiTask).where(AiTask.id == int(task.id)))).scalars().first()
    assert row is not None and row.task_type == "ai_rework" and row.ai_client == "file_bridge"


async def test_tasks_of_different_types_coexist(session: object) -> None:
    """同一商品下三种新能力各建一条 —— 证明 `task_type` 真的把它们区分开了。"""
    product = await _make_product(session)
    for task_type in (
        AiTaskType.AI_REWORK.value,
        AiTaskType.IMAGE_REDRAW.value,
        AiTaskType.TITLE_SUGGEST.value,
        AiTaskType.VIDEO_SCRIPT.value,
    ):
        session.add(
            AiTask(
                source_product_id=int(product.id),
                target_platform="douyin",
                task_type=task_type,
                rework_items_json=["title"],  # ★ 故意全部相同：子项列表不该被当成类型
                status=AiTaskStatus.QUEUED.value,
            )
        )
    await session.commit()

    rows = (
        (await session.execute(select(AiTask).where(AiTask.source_product_id == int(product.id))))
        .scalars()
        .all()
    )
    assert {r.task_type for r in rows} == set(AiTaskType.values())


# ======================================================================
#  ② AiInputPrompt 契约
# ======================================================================


def test_input_prompt_roundtrip_and_dirty_data() -> None:
    """JSON 往返无损；脏数据 / None 一律退化为**空契约**而不是抛异常。"""
    raw = {
        "global_prompt": "日式极简",
        "images": [{"index": 2, "prompt": "这张换成暖色", "asset_id": 7}],
        "video_script_prompt": "竖屏快节奏",
        "extra": {"source": "ui"},
    }
    prompt = AiInputPrompt.from_dict(raw)
    assert prompt.image_prompt(2) == "这张换成暖色"
    assert prompt.image_prompt(0) == ""  # 没给 → 空串，由调用方决定是否回退
    assert AiInputPrompt.from_dict(raw).to_dict() == AiInputPrompt.from_dict(prompt.to_dict()).to_dict()

    empty = AiInputPrompt.from_dict(None)
    assert empty.global_prompt == "" and empty.images == []
    assert AiInputPrompt.from_dict({"images": "not-a-list"}).images == []
    assert AiInputPrompt.from_dict({"images": [1, {"index": "x", "prompt": "p"}]}).images[
        0
    ].index == 0  # 非法 index 退化为 0


# ======================================================================
#  ③ 标题多候选契约（★ 向后兼容是重点）
# ======================================================================


def test_title_candidates_carry_choice_hints() -> None:
    """3 条以上候选，每条必须带"该选哪条"的区分信息。"""
    result = AiTitleResult(
        title="A 通用标题",
        selling_points=["包邮"],
        candidates=[
            AiTitleCandidate(title="A 通用标题", style="通用", reason="覆盖人群最广", score=80),
            AiTitleCandidate(
                title="B 抖音热词标题", style="抖音热词风", reason="抖店搜索热度高", score=90, platform_fit="douyin"
            ),
            AiTitleCandidate(title="C 参数流标题", style="参数流", reason="突出规格与材质", score=70),
        ],
    )
    assert len(result.candidates) >= 3
    payload = result.to_dict()
    assert len(payload["candidates"]) == 3
    assert payload["candidates"][0]["char_count"] == len("A 通用标题")
    # preferred() 取最高分那条，而不是写死第一条
    assert result.preferred().title == "B 抖音热词标题"
    assert AiTitleResult.from_dict(payload).preferred().title == "B 抖音热词标题"


def test_title_single_result_still_works() -> None:
    """★ 旧消费方保护：`title` / `selling_points` / `banned_words` 原位不变，candidates 可空。"""
    old = AiTitleResult(title="原标题", selling_points=["卖点A"], banned_words=["最"], model_name="mock")
    payload = old.to_dict()
    assert payload["title"] == "原标题"
    assert payload["selling_points"] == ["卖点A"]
    assert payload["banned_words"] == ["最"]
    assert payload["candidates"] == []
    # 旧客户端（单条口径）的 preferred() 仍能给出一条可用候选
    preferred = old.preferred()
    assert preferred.title == "原标题" and preferred.selling_points == ["卖点A"]
    # 缺 candidates 的旧 result.json 也能读回来
    assert AiTitleResult.from_dict({"title": "旧文件"}).candidates == []


async def test_mock_client_rewrite_title_still_instantiable() -> None:
    """★ 三个客户端不许改，但要确认它们产出的 `AiTitleResult` 在新契约下仍然合法。"""
    client = MockAiClient()
    result = await client.rewrite_title(
        AiTaskContext(task_id="1", original_title="测试标题", rework_items=["title"])
    )
    assert isinstance(result, AiTitleResult)
    assert result.title  # 旧字段仍然有值
    assert not result.candidates  # 本轮没让它们产多候选（下一棒）
    assert result.preferred().title == result.title


def test_rework_result_dict_contains_candidates() -> None:
    """`AiReworkResult.to_dict()` 必须把候选带出去（落库 / 写 result.json 的通道）。"""
    payload = AiReworkResult(
        title_result=AiTitleResult(
            title="X",
            candidates=[AiTitleCandidate(title="X", style="通用", reason="稳", score=1)],
        )
    ).to_dict()
    assert payload["title_result"] is not None
    assert payload["title_result"]["candidates"][0]["title"] == "X"


# ======================================================================
#  ④ AiTaskContext 逐图提示词
# ======================================================================


def test_context_prompts_and_backward_compat() -> None:
    """新版上下文带上逐图提示词；**旧版 task.json 没有这些键也必须能读回来**。"""
    ctx = AiTaskContext(
        task_id="42",
        source_image_paths=["a.jpg", "b.jpg"],
        global_prompt="全局：日式极简",
        image_prompts=[AiImagePrompt(index=1, prompt="第二张突出细节")],
        task_type=AiTaskType.IMAGE_REDRAW.value,
        video_script_prompt="竖屏快节奏",
    )
    assert ctx.image_prompt(0) == "全局：日式极简"  # 没给 → 回退全局
    assert ctx.image_prompt(1) == "第二张突出细节"

    restored = AiTaskContext.from_dict(ctx.to_dict())
    assert restored.image_prompts[0].index == 1
    assert restored.global_prompt == "全局：日式极简"
    assert restored.task_type == AiTaskType.IMAGE_REDRAW.value

    old_style = AiTaskContext.from_dict(
        {"task_id": "7", "source_image_paths": ["a.jpg"], "rework_items": ["title"]}
    )
    assert old_style.image_prompts == []
    assert old_style.global_prompt == ""
    assert old_style.image_prompt(0) == ""


# ======================================================================
#  ⑤ 枚举与 ENUM_DICT
# ======================================================================


async def test_settings_enums_exposes_ai_options(client: object) -> None:
    """补齐动机：此前前端只能本地硬编码这些选项，后端改枚举前端不同步（枚举漂移）。"""
    resp = await client.get("/api/v1/settings/enums")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    data = payload.get("data", payload)
    assert set(AiTaskType.values()) == {item["value"] for item in data["AiTaskType"]}
    assert set(AiClientName.values()) == {item["value"] for item in data["AiClientName"]}
    for key in ("AiTaskStatus", "AiReworkItem", "ReviewStatus"):
        assert data[key], f"{key} 必须出现在 ENUM_DICT 里"
    assert {item["value"] for item in data["AiReworkItem"]} == {
        "main_image",
        "detail_image",
        "title",
        "attribute",
    }


async def test_create_tasks_solidifies_client_and_type(session: object) -> None:
    """★ README 第九节第 17 条：创建时必须把**当时生效的客户端**固化到行上。"""
    product = await _make_product(session)
    setting = (
        await session.execute(
            select(SystemSetting).where(SystemSetting.setting_key == SettingKey.AI_CLIENT.value)
        )
    ).scalars().first()
    assert setting is not None, "ai.client 配置项缺失，测试结论不可信"
    original = setting.setting_value
    setting.setting_value = AiClientName.MOCK.value
    await session.commit()

    tasks = await AiTaskService.create_tasks(
        session,
        source_product_ids=[int(product.id)],
        target_platform="taobao",
        rework_items=["title"],
        operator="tester",
    )
    await session.commit()

    assert len(tasks) == 1
    task_id = int(tasks[0].id)
    row = (await session.execute(select(AiTask).where(AiTask.id == task_id))).scalars().first()
    assert row is not None
    assert row.ai_client == AiClientName.MOCK.value, "必须固化创建时读到的客户端，而不是代码默认值"
    assert row.task_type == AiTaskType.AI_REWORK.value, "现有 create_tasks 走的仍是老图文重构口径"

    setting.setting_value = original
    await session.commit()


async def test_task_result_model_unchanged(session: object) -> None:
    """产出侧表未被本次改动波及（只动了 `ai_task`，没动 `ai_task_result`）。"""
    product = await _make_product(session)
    task = AiTask(
        source_product_id=int(product.id),
        target_platform="taobao",
        task_type=AiTaskType.TITLE_SUGGEST.value,
        rework_items_json=["title"],
        status=AiTaskStatus.QUEUED.value,
    )
    session.add(task)
    await session.flush()
    result = AiTaskResult(ai_task_id=int(task.id), review_status="pending", model_name="mock")
    session.add(result)
    await session.commit()
    assert int(result.id) > 0
    assert not result.approved
