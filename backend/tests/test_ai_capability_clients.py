"""★ 三种新 AI 能力（图片重绘 / 标题建议 / 短视频脚本）的客户端层验证。

覆盖的硬指标（对应任务验收 1-5）：
    1. 三个客户端（mock / file_bridge / http）都能实例化、三种能力都能调通；
    2. **逐图提示词**真的传到了 file_bridge 的 `prompt.md`，且逐图 → 全局回退正确；
    3. 标题候选 **≥3 条且互不相同**，每条带选择依据；
    4. 空 / 脏 `input_prompt_json` 与脏 `result.json` 一律不抛异常；
    5. `ai_client` 固化生效：`run_task()` 按任务行的值取客户端，取不到就报错、绝不静默回落。

★ 为什么 `file_bridge` 的用例要先手写 `result.json` 再调能力方法：
   文件桥的语义是"把任务写给外部 AI，再等它把 result.json 放回来"，
   而 CI 里没有人来放。预先放好产出 = 模拟"AI 已经交付"，于是等待立刻返回，
   测的是**解析与模板**，不是真等 600 秒。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import select

from app.adapters.ai.base import (
    AiImagePrompt,
    AiInputPrompt,
    AiRedrawResult,
    AiTaskContext,
    AiTitleSuggestResult,
    AiVideoScriptResult,
)
from app.adapters.ai.factory import AiClientFactory
from app.adapters.ai.file_bridge import WorkBuddyFileBridgeClient
from app.adapters.ai.http_client import HttpAiClient
from app.adapters.ai.mock_client import MockAiClient
from app.core.errors import BusinessError
from app.models.asset import AiTask, AiTaskResult, Asset
from app.models.enums import AiTaskStatus, AiTaskType
from app.models.source import SourceProduct
from app.services.ai_task_service import AiTaskService

# ★ 不写 `pytestmark = pytest.mark.asyncio`：pytest.ini 已是 `asyncio_mode = auto`，
#   加了反而会给同步用例刷一堆 "marked with asyncio but is not an async function" 警告。
ALL_CLIENTS = ["mock", "file_bridge", "http"]


def _ctx(**kwargs: Any) -> AiTaskContext:
    """构造一个默认可用的任务上下文。"""
    base: dict[str, Any] = {
        "task_id": "cap-001",
        "target_platform": "taobao",
        "source_product_id": 1,
        "original_title": "纯棉四件套 1.8m 床",
        "selling_points": ["亲肤透气", "不起球"],
        "source_image_paths": ["data/a.png", "data/b.png", "data/c.png"],
    }
    base.update(kwargs)
    return AiTaskContext(**base)  # type: ignore[arg-type]


# ============================================================================
#  验收 1：三个客户端都能实例化 + 三种能力都能调通
# ============================================================================


@pytest.mark.parametrize("name", ALL_CLIENTS)
def test_all_clients_instantiable(name: str) -> None:
    """★ 三个客户端都能实例化 —— 等于证明三个新抽象方法**没有漏实现**（漏了 ABC 会拒绝实例化）。"""
    client = AiClientFactory.instantiate(name)
    assert client.client_name == name
    for method in ("redraw_images", "suggest_titles", "suggest_video_script"):
        assert callable(getattr(client, method)), f"{name} 缺少新方法 {method}"


def test_instantiate_unknown_client_raises_instead_of_fallback() -> None:
    """★ 未注册的客户端名必须报错，**绝不静默回落到默认客户端**。"""
    with pytest.raises(BusinessError) as excinfo:
        AiClientFactory.instantiate("no_such_client")
    assert "未注册" in str(excinfo.value)


async def test_mock_client_three_capabilities() -> None:
    """Mock：三种能力全部离线跑通，且产出符合契约。"""
    client = MockAiClient()
    ctx = _ctx(
        task_type=AiTaskType.IMAGE_REDRAW.value,
        global_prompt="整体偏日式极简",
        image_prompts=[AiImagePrompt(index=1, prompt="第二张换成暖色背景")],
    )

    redraw = await client.redraw_images(ctx)
    assert isinstance(redraw, AiRedrawResult)
    assert len(redraw.images) == 3
    assert len(redraw.main_images()) == 1 and len(redraw.detail_images()) == 2
    for image in redraw.images:
        assert image.image_role in {"main_image", "detail_image"}
        assert image.prompt, "每张图必须回显它实际使用的提示词"
        assert image.prompt_source in {"per_image", "global", "default"}
        assert image.local_path

    titles = await client.suggest_titles(ctx)
    assert isinstance(titles, AiTitleSuggestResult)
    assert len(titles.candidates) >= 3

    script = await client.suggest_video_script(ctx)
    assert isinstance(script, AiVideoScriptResult)
    assert script.scene_count >= 1
    assert script.effective_duration_sec > 0
    for scene in script.scenes:
        assert scene.index >= 1 and scene.duration_sec > 0
        assert scene.shot and scene.narration


async def test_http_client_three_capabilities_without_network() -> None:
    """HTTP：未配置 base_url / api_key 时三种能力**照常可调用且不抛异常**（产出为空）。

    ★ 这里刻意不配接口：验的是"没有外部 AI 时这条链路不会崩"，
      而不是"AI 真的返回了内容"（那属于联调，不该进单元测试）。
    """
    client = HttpAiClient(output_dir=None)  # type: ignore[arg-type]
    ctx = _ctx()
    redraw = await client.redraw_images(ctx)
    titles = await client.suggest_titles(ctx)
    script = await client.suggest_video_script(ctx)

    assert isinstance(redraw, AiRedrawResult)
    assert isinstance(titles, AiTitleSuggestResult)
    assert isinstance(script, AiVideoScriptResult)
    # ★ 没有产出就是没有产出，绝不能编一条假标题出来
    assert titles.candidates == []
    assert script.scenes == []


async def test_file_bridge_three_capabilities(tmp_path: Any) -> None:
    """file_bridge：预置 `result.json`（模拟 AI 已交付）后三种能力都能解析出产物。"""
    client = WorkBuddyFileBridgeClient(
        queue_dir=tmp_path / "queue",
        output_dir=tmp_path / "output",
        poll_timeout_sec=2.0,
        poll_interval_sec=0.1,
    )
    ctx = _ctx(task_type=AiTaskType.IMAGE_REDRAW.value)
    (tmp_path / "output" / ctx.task_id).mkdir(parents=True, exist_ok=True)
    (tmp_path / "output" / ctx.task_id / "result.json").write_text(
        json.dumps(
            {
                "redraw": {
                    "images": [
                        {
                            "index": 0,
                            "local_path": "data/ai_output/cap-001/main_01.png",
                            "image_role": "main_image",
                            "prompt": "白底主图",
                            "prompt_source": "per_image",
                        },
                        {
                            "index": 1,
                            "local_path": "data/ai_output/cap-001/detail_01.png",
                            "image_role": "detail_image",
                            "prompt": "场景图",
                        },
                    ],
                    "model_name": "bridge-model",
                },
                "title_suggest": {
                    "candidates": [
                        {"title": "标题A", "style": "搜索流", "reason": "吃搜索流量", "score": 90},
                        {"title": "标题B", "style": "热词流", "reason": "适合推荐流", "score": 80},
                        {"title": "标题C", "style": "价格力", "reason": "适合比价", "score": 70},
                    ]
                },
                "video_script": {
                    "title": "脚本",
                    "style": "快节奏",
                    "scenes": [
                        {
                            "index": 1,
                            "duration_sec": 3,
                            "shot": "正面特写",
                            "camera": "固定机位",
                            "shooting_tips": "补光",
                            "narration": "看这里",
                        }
                    ],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    redraw = await client.redraw_images(ctx)
    assert len(redraw.images) == 2
    assert redraw.main_images()[0].prompt == "白底主图"
    assert redraw.detail_images()[0].image_role == "detail_image"

    titles = await client.suggest_titles(ctx)
    assert len(titles.candidates) == 3
    assert titles.preferred() is not None and titles.preferred().title == "标题A"

    script = await client.suggest_video_script(ctx)
    assert script.scene_count == 1
    assert script.scenes[0].narration == "看这里"
    assert script.effective_duration_sec == 3


# ============================================================================
#  验收 2：逐图提示词真的传下去了
# ============================================================================


async def test_per_image_prompt_reaches_prompt_md(tmp_path: Any) -> None:
    """★ 3 张图、3 条不同提示词 —— 证明 `prompt.md` 里三者**一一对应**。"""
    client = WorkBuddyFileBridgeClient(
        queue_dir=tmp_path / "queue",
        output_dir=tmp_path / "output",
        poll_timeout_sec=2.0,
        poll_interval_sec=0.1,
    )
    paths = ["data/img/a.png", "data/img/b.png", "data/img/c.png"]
    prompts = ["第一张换成暖色背景", "第二张加生活场景", "第三张突出面料细节"]
    ctx = _ctx(
        task_type=AiTaskType.IMAGE_REDRAW.value,
        global_prompt="整体偏日式极简",
        source_image_paths=paths,
        image_prompts=[AiImagePrompt(index=i, prompt=p) for i, p in enumerate(prompts)],
    )
    written = client.write_task(ctx)
    text = open(written["prompt_md"], encoding="utf-8").read()

    for path, prompt in zip(paths, prompts):
        path_at = text.find(f"`{path}`")
        prompt_at = text.find(f"提示词：{prompt}")
        assert path_at >= 0, f"prompt.md 里找不到原图 {path}"
        assert prompt_at >= 0, f"prompt.md 里找不到该图的提示词：{prompt}"
        assert path_at < prompt_at, "提示词必须紧跟在它自己那张原图之后（不能错位）"

    # ★ 同一张图的提示词不能串到下一张图上：后一张原图出现之前，必须已经出现本张的提示词
    for i in range(len(paths) - 1):
        assert text.find(f"提示词：{prompts[i]}") < text.find(f"`{paths[i + 1]}`")

    # 逐图提示词会原样落到 task.json（机器可读那份）
    payload = json.loads(open(written["task_json"], encoding="utf-8").read())
    assert [p["prompt"] for p in payload["image_prompts"]] == prompts


async def test_missing_per_image_prompt_falls_back_to_global(tmp_path: Any) -> None:
    """★ 该图没有逐图提示词时，`prompt.md` 必须写明"沿用全局提示词"（不能悄悄留空）。"""
    client = WorkBuddyFileBridgeClient(
        queue_dir=tmp_path / "queue",
        output_dir=tmp_path / "output",
        poll_timeout_sec=2.0,
        poll_interval_sec=0.1,
    )
    ctx = _ctx(
        source_image_paths=["data/img/a.png", "data/img/b.png"],
        global_prompt="整体偏日式极简",
        image_prompts=[AiImagePrompt(index=0, prompt="第一张换成暖色背景")],
    )
    written = client.write_task(ctx)
    text = open(written["prompt_md"], encoding="utf-8").read()

    assert "提示词：第一张换成暖色背景" in text
    assert "沿用全局提示词：整体偏日式极简" in text


def test_input_prompt_image_prompt_lookup_and_fallback() -> None:
    """★ 逐图提示词的**取值口径**：有逐图取逐图，没有则回落到全局。

    ★ 两个方法的分工（与上一棒 `test_ai_task_data_layer.py:161` 钉死的口径一致，不要互换）：
        `image_prompt(i)`     **原值**：该图有没有被单独指定（没给 = 空串），
                              用于判断"要不要回退"，不能自己回退，否则调用方无从分辨；
        `effective_prompt(i)` **最终生效值**：逐图优先，没给就回退全局 —— 产出方一律用它。
    """
    prompt = AiInputPrompt.from_dict(
        {
            "global_prompt": "全局：日式极简",
            "images": [
                {"index": 0, "prompt": "第0张专属"},
                {"index": 2, "prompt": "第2张专属"},
            ],
        }
    )
    assert prompt.image_prompt(0) == "第0张专属"
    assert prompt.image_prompt(2) == "第2张专属"
    assert prompt.image_prompt(1) == ""  # 原值口径：这张没被单独指定
    # ★ 最终生效值口径：没给 → 回退全局（逐图能力在缺项时不会静默失效）
    assert prompt.effective_prompt(0) == "第0张专属"
    assert prompt.effective_prompt(1) == "全局：日式极简"
    assert prompt.effective_prompt(9) == "全局：日式极简"
    assert prompt.effective_prompt(None) == "全局：日式极简"

    # 上下文侧：`AiTaskContext.image_prompt(i)` 直接就是"带回落"的口径
    ctx = _ctx(global_prompt="全局：日式极简", image_prompts=[AiImagePrompt(index=0, prompt="第0张专属")])
    assert ctx.image_prompt(0) == "第0张专属"
    assert ctx.image_prompt(1) == "全局：日式极简"


async def test_mock_redraw_records_prompt_source() -> None:
    """★ 图重绘产出必须说清"这张图按哪句提示词画的"，否则无法复盘。"""
    client = MockAiClient()
    ctx = _ctx(
        global_prompt="全局：日式极简",
        image_prompts=[AiImagePrompt(index=1, prompt="第1张专属")],
    )
    redraw = await client.redraw_images(ctx)
    by_index = {img.index: img for img in redraw.images}
    assert by_index[0].prompt == "全局：日式极简"
    assert by_index[0].prompt_source == "global"
    assert by_index[1].prompt == "第1张专属"
    assert by_index[1].prompt_source == "per_image"
    assert by_index[2].prompt_source == "global"


# ============================================================================
#  验收 3：标题候选 ≥3 且互不相同、每条带选择依据
# ============================================================================


async def test_mock_title_candidates_are_distinct_and_justified() -> None:
    """★ 标题建议的**全部价值**是"能挑一条"：三条长得一样等于没给建议。"""
    client = MockAiClient()
    result = await client.suggest_titles(
        _ctx(task_type=AiTaskType.TITLE_SUGGEST.value, target_platform="douyin", title_prompt="偏热词风")
    )

    assert len(result.candidates) >= 3, f"候选数不足 3：{len(result.candidates)}"
    titles = [c.title for c in result.candidates]
    assert len(set(titles)) == len(titles), f"候选标题出现重复：{titles}"
    for candidate in result.candidates:
        assert candidate.title.strip()
        assert candidate.style, "每条必须说明这条走什么风格"
        assert candidate.reason, "每条必须说明为什么选它"
        assert 0 <= candidate.score <= 100

    # ★ 目标平台那条应当被选中（使用者最可能要的就是它）
    best = result.preferred()
    assert best is not None and best.platform_fit == "douyin"
    # 使用者的补充要求要能被看到（写进依据，而不是石沉大海）
    assert "偏热词风" in best.reason


async def test_title_suggest_bridges_to_legacy_title_result() -> None:
    """★ 新能力必须能退化成老口径单条结果 —— 现有 `persist_result()` 因此可以照旧消费。"""
    client = MockAiClient()
    result = await client.suggest_titles(_ctx(target_platform="pdd"))
    legacy = result.to_title_result()
    assert legacy.title == result.preferred().title
    assert legacy.model_name == result.model_name
    assert len(legacy.candidates) == len(result.candidates)

    # 空候选时**不伪造**标题
    empty = AiTitleSuggestResult()
    assert empty.preferred() is None
    assert empty.to_title_result().title == ""


# ============================================================================
#  验收 4：空 / 脏数据不得抛异常
# ============================================================================


@pytest.mark.parametrize(
    "dirty",
    [
        None,
        {},
        {"images": "乱码"},
        {"images": [None, 3, {"index": "x"}, {"prompt": 123}]},
        {"global_prompt": None, "title_prompt": 5, "video_script_prompt": []},
        {"images": [{"index": 0, "prompt": {"nested": 1}}]},
        "i am not even a dict",
        [1, 2, 3],
    ],
)
def test_dirty_input_prompt_json_never_raises(dirty: Any) -> None:
    """★ 脏 `input_prompt_json` 一律退化为**空契约**，绝不抛异常。

    ★ 为什么单列这一条：这一列是**使用者手填**的，也是外部写入的唯一入口，
      一句坏 JSON 若能把任务打崩，整个 AI 链路就成了"谁都能弄坏"的脆弱环节。
    """
    prompt = AiInputPrompt.from_dict(dirty)
    assert isinstance(prompt.global_prompt, str)
    assert isinstance(prompt.images, list)
    assert isinstance(prompt.to_dict(), dict)
    # 取值方法同样不得抛
    assert isinstance(prompt.image_prompt(0), str)
    assert isinstance(prompt.effective_prompt(0), str)

    ctx = _ctx()
    ctx.image_prompts = list(prompt.image_prompts)
    ctx.global_prompt = prompt.global_prompt
    assert isinstance(ctx.to_dict(), dict)


def test_dirty_result_json_never_raises() -> None:
    """★ 外部 AI 写回来的 `result.json` 想怎么写就怎么写，解析必须扛住。"""
    client = WorkBuddyFileBridgeClient  # 只取静态方法，不碰文件系统

    for dirty in (None, {}, "x", 5, [], {"redraw": "oops"}, {"redraw": {"images": "oops"}}):
        assert isinstance(client.parse_redraw_result(dirty), AiRedrawResult)  # type: ignore[arg-type]

    assert client.parse_redraw_result(
        {"redraw": {"images": [None, "x", {"local_path": 5, "index": "bad", "width": "nope"}]}}
    ).images[0].index == 0

    for dirty in (None, {}, {"title_suggest": None}, {"title_suggest": {"candidates": "oops"}}):
        assert isinstance(client.parse_title_suggest_result(dirty), AiTitleSuggestResult)  # type: ignore[arg-type]

    assert (
        client.parse_title_suggest_result(
            {"title_suggest": {"candidates": [None, {"title": 5, "score": "bad", "selling_points": "x"}]}}
        ).candidates[0].title
        == "5"
    )

    for dirty in (None, {}, {"video_script": 7}, {"video_script": {"scenes": [{"duration_sec": "abc"}]}}):
        assert isinstance(client.parse_video_script_result(dirty), AiVideoScriptResult)  # type: ignore[arg-type]

    script = client.parse_video_script_result(
        {"video_script": {"scenes": [None, {"index": "x", "duration_sec": "abc", "shot": None}]}}
    )
    assert script.scene_count == 1
    assert script.scenes[0].duration_sec == 0.0
    assert script.scenes[0].shot == ""


def test_broken_result_file_returns_none(tmp_path: Any) -> None:
    """★ 坏 JSON 文件不能让等待逻辑崩 —— `read_result()` 返回 None（保持既有语义）。"""
    client = WorkBuddyFileBridgeClient(
        queue_dir=tmp_path / "queue",
        output_dir=tmp_path / "output",
        poll_timeout_sec=1.0,
        poll_interval_sec=0.1,
    )
    (tmp_path / "output" / "broken").mkdir(parents=True)
    (tmp_path / "output" / "broken" / "result.json").write_text("{not json at all", encoding="utf-8")
    assert client.read_result("broken") is None


# ============================================================================
#  验收 5：ai_client 固化生效
# ============================================================================


async def _make_task(session: Any, *, ai_client: str, input_prompt_json: Any = None) -> tuple[int, int]:
    """建一个货源商品 + AI 任务并**提交**（`prepare()` 走自己的会话，未提交读不到）。"""
    import uuid

    product = SourceProduct(
        product_1688_id=f"cap-{uuid.uuid4().hex[:10]}",
        title="纯棉四件套 1.8m 床",
        params_json={"材质": "纯棉"},
    )
    session.add(product)
    await session.flush()
    task = AiTask(
        source_product_id=int(product.id),
        target_platform="taobao",
        task_type=AiTaskType.AI_REWORK.value,
        ai_client=ai_client,
        input_prompt_json=input_prompt_json,
        rework_items_json=["main_image"],
        status=AiTaskStatus.QUEUED.value,
        created_by="tester",
    )
    session.add(task)
    await session.commit()
    return int(product.id), int(task.id)


async def _cleanup(session: Any, product_id: int, task_id: int) -> None:
    """清掉用例写入的数据（本用例自己 commit 过，不清理会污染后续用例）。"""
    await session.execute(select(AiTaskResult).where(AiTaskResult.ai_task_id == task_id))
    for row in (await session.execute(select(AiTaskResult).where(AiTaskResult.ai_task_id == task_id))).scalars():
        await session.delete(row)
    for row in (await session.execute(select(Asset).where(Asset.ai_task_id == task_id))).scalars():
        await session.delete(row)
    task = await session.get(AiTask, task_id)
    if task:
        await session.delete(task)
    product = await session.get(SourceProduct, product_id)
    if product:
        await session.delete(product)
    await session.commit()


async def test_run_task_uses_pinned_client(session: Any, monkeypatch: Any) -> None:
    """★ `run_task()` 必须按任务行固化的 `ai_client` 取客户端，**不看当前配置**。"""
    product_id, task_id = await _make_task(session, ai_client="mock")
    seen: list[str] = []
    original = AiClientFactory.instantiate

    def spy(name: str, **kwargs: Any) -> Any:
        seen.append(name)
        return original(name, **kwargs)

    monkeypatch.setattr(AiClientFactory, "instantiate", staticmethod(spy))
    try:
        await AiTaskService.run_task(task_id)
    finally:
        await _cleanup(session, product_id, task_id)

    assert seen == ["mock"], f"run_task 实际取的客户端是 {seen}，应为 ['mock']"


async def test_run_task_fails_loudly_when_pinned_client_unavailable(session: Any) -> None:
    """★ 固化的客户端不可用时**报错 + 标失败**，绝不静默回落到默认客户端。"""
    product_id, task_id = await _make_task(session, ai_client="no_such_client")
    try:
        with pytest.raises(BusinessError) as excinfo:
            await AiTaskService.run_task(task_id)
        assert "未注册" in str(excinfo.value)

        task = await session.get(AiTask, task_id)
        assert task is not None and task.status == AiTaskStatus.FAILED.value
        assert task.error_code == "BusinessError"
        # ★ 没有产出就是没有产出，不能留下一条"看起来成功"的结果
        result = (
            await session.execute(select(AiTaskResult).where(AiTaskResult.ai_task_id == task_id))
        ).scalars().first()
        assert result is None
    finally:
        await _cleanup(session, product_id, task_id)


async def test_prepare_feeds_prompts_from_input_prompt_json(session: Any) -> None:
    """★ `prepare()` 必须把 `input_prompt_json` 灌进上下文，包括脏数据也不许炸。"""
    product_id, task_id = await _make_task(
        session,
        ai_client="mock",
        input_prompt_json={
            "global_prompt": "整体偏日式极简",
            "images": [{"index": 0, "prompt": "主图换成暖色背景"}],
            "title_prompt": "偏热词风",
            "video_script_prompt": "竖屏快节奏",
        },
    )
    try:
        plan = await AiTaskService.prepare(task_id)
        assert plan.context.global_prompt == "整体偏日式极简"
        assert plan.context.title_prompt == "偏热词风"
        assert plan.context.video_script_prompt == "竖屏快节奏"
        assert [p.prompt for p in plan.context.image_prompts] == ["主图换成暖色背景"]
        assert plan.context.task_type == AiTaskType.AI_REWORK.value
        assert plan.ai_client_name == "mock"
        assert plan.task_type == AiTaskType.AI_REWORK.value
    finally:
        await _cleanup(session, product_id, task_id)


async def test_prepare_survives_dirty_input_prompt_json(session: Any) -> None:
    """★ 库里存着乱码结构的 `input_prompt_json` 时，`prepare()` 照样要能组出上下文。"""
    product_id, task_id = await _make_task(
        session, ai_client="mock", input_prompt_json={"images": "完全不是列表", "global_prompt": None}
    )
    try:
        plan = await AiTaskService.prepare(task_id)
        assert plan.context.image_prompts == []
        assert plan.context.global_prompt == ""
    finally:
        await _cleanup(session, product_id, task_id)
