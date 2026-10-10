"""★ 三种新 AI 能力接入服务端的**端到端落库**验证（0005 的服务层 / HTTP 层）。

上一棒（0004）只做到数据层，本棒把能力真正接进链路：
    `run_task()` 按 `ai_task.task_type` 分派 → 产出落库 → HTTP 端点可取回 / 可选定。

本文件的立场：**只信数据库**。
接口返回 200 不等于数据落对了（历史上"看着成功、其实没写库"的坑在本项目出过多次），
因此每一项都用 `select(...)` 直查 `asset` / `ai_task_result` 行来断言。

逐条对应验收项：
    ① 三种能力各自端到端（image_redraw / title_suggest / video_script）→ 落库字段正确；
    ② 选定标题候选 → `output_title` 变成选中那条，且**发布链路 `_resolve_title()` 读到它**；
    ③ 存量 `ai_rework` 路径无回归（不带新参数，产出与改动前一致）；
    ④ 逐图提示词闭环（3 张图各一句，每张 `prompt_source == per_image`）；
    ⑤ 未知 `task_type` 显式报错、不静默；
    ⑥ HTTP 端点：创建（带 task_type / 提示词）→ 详情（候选 / 脚本）→ 选定标题。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from app.adapters.ai.base import (
    PROMPT_SOURCE_PER_IMAGE,
    AiImagePrompt,
    AiInputPrompt,
)
from app.adapters.ai.factory import AI_CLIENT_REGISTRY, register_ai_client
from app.adapters.ai.mock_client import MockAiClient, write_placeholder_png
from app.models.asset import AiTask, AiTaskResult, Asset
from app.models.enums import AiTaskStatus, AiTaskType, AssetOrigin, AssetType
from app.models.source import SourceProduct
from app.services.ai_task_service import AiTaskService
from app.services.publish_service import PublishService
from app.utils.kit import content_hash_bytes
from tests.conftest import ADMIN_HEADERS

__all__ = ["test_unknown_task_type_is_rejected"]


# ======================================================================
#  测试夹具辅助（全部真实落库、真实执行）
# ======================================================================


def _uniq(prefix: str) -> str:
    """生成唯一串（避开 `product_1688_id` / `content_hash` 的唯一约束）。"""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class _DistinctImageMockClient(MockAiClient):
    """★ 仅测试用：让每张产出图的**字节互不相同**。

    ★ 为什么需要它：MockAiClient 的占位图按尺寸生成，三张详情图字节完全一致
      ⇒ `content_hash` 相同 ⇒ `persist_result()` 的去重逻辑会把 3 张压成 2 张 asset
      （去重本身是对的，但会让"逐图提示词是否各自生效"无从验证）。
      这里只改像素颜色（按 `task_id` 派生，保证跨用例也不撞），
      **不改**任何提示词 / 角色 / 序号逻辑 —— 被测的那部分仍是真实实现。
    """

    async def redraw_images(self, ctx: Any) -> Any:
        """复用父类产出，只把每张图的字节改得互不相同（哈希随之唯一）。"""
        result = await super().redraw_images(ctx)
        offset = sum(ord(char) for char in str(ctx.task_id)) % 180
        for position, image in enumerate(result.images):
            path = Path(image.local_path)
            write_placeholder_png(
                path,
                width=800,
                height=800 if position == 0 else 1000,
                rgb=(offset + position * 7, 120, 200),
            )
            image.size_bytes = path.stat().st_size
            image.content_hash = content_hash_bytes(path.read_bytes())
        return result


DISTINCT_CLIENT_NAME = "mock_distinct"


def _ensure_distinct_client() -> None:
    """注册测试用客户端（供 `ai_task.ai_client` 固化后由 `instantiate()` 取到）。"""
    if DISTINCT_CLIENT_NAME not in AI_CLIENT_REGISTRY:
        register_ai_client(DISTINCT_CLIENT_NAME, _DistinctImageMockClient)


def _drop_distinct_client() -> None:
    """注销测试用客户端（避免污染其他用例的注册表）。"""
    AI_CLIENT_REGISTRY.pop(DISTINCT_CLIENT_NAME, None)


async def _make_product(session: Any, title: str = "纯棉四件套 1.8m 床") -> int:
    """建一个货源商品（已提交）。"""
    product = SourceProduct(product_1688_id=_uniq("pipe"), title=title, params_json={"材质": "纯棉"})
    session.add(product)
    await session.commit()
    return int(product.id)


async def _seed_raw_images(session: Any, product_id: int, count: int = 3) -> list[str]:
    """建 `count` 张**真实存在**的 `origin=raw` 素材（AI 任务的输入图）。

    ★ 必须是真实文件：`persist_result()` 对不存在的 `local_path` 直接跳过。
    """
    from app.core.config import get_settings

    raw_dir = Path(get_settings().assets_dir) / "raw" / _uniq("seed")
    raw_dir.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for index in range(count):
        target = raw_dir / f"raw_{index:02d}.png"
        write_placeholder_png(target, width=640, height=640, rgb=(30 + index * 30, 80, 90))
        asset = Asset(
            source_product_id=int(product_id),
            asset_type=AssetType.MAIN_IMAGE.value if index == 0 else AssetType.DETAIL_IMAGE.value,
            origin=AssetOrigin.RAW.value,
            storage_path=str(target),
            content_hash=content_hash_bytes(target.read_bytes()),
            version=1,
            is_current=True,
        )
        session.add(asset)
        paths.append(str(target))
    await session.commit()
    return paths


async def _make_task(
    session: Any,
    product_id: int,
    *,
    task_type: str,
    ai_client: str = "mock",
    input_prompt_json: dict[str, Any] | None = None,
    target_platform: str = "taobao",
) -> int:
    """建一条 AI 任务（已提交 —— `prepare()` 走自己的会话，未提交读不到）。"""
    task = AiTask(
        source_product_id=int(product_id),
        target_platform=target_platform,
        task_type=task_type,
        ai_client=ai_client,
        input_prompt_json=input_prompt_json,
        rework_items_json=["main_image", "detail_image"],
        status=AiTaskStatus.QUEUED.value,
        created_by="tester",
    )
    session.add(task)
    await session.commit()
    return int(task.id)


async def _cleanup(session: Any, product_id: int, task_id: int) -> None:
    """清掉用例写入的数据（本用例 commit 过，不清理会污染后续用例）。"""
    for row in (
        (await session.execute(select(AiTaskResult).where(AiTaskResult.ai_task_id == task_id))).scalars()
    ):
        await session.delete(row)
    for row in (await session.execute(select(Asset).where(Asset.ai_task_id == task_id))).scalars():
        await session.delete(row)
    task = await session.get(AiTask, task_id)
    if task is not None:
        await session.delete(task)
    for row in (
        (await session.execute(select(Asset).where(Asset.source_product_id == product_id))).scalars()
    ):
        await session.delete(row)
    product = await session.get(SourceProduct, product_id)
    if product is not None:
        await session.delete(product)
    await session.commit()


async def _latest_result(session: Any, task_id: int) -> AiTaskResult:
    """取任务最近一条产出（查库，不走缓存）。"""
    await session.rollback()  # ★ 结束本会话的读快照，确保读到别的会话已提交的数据
    row = (
        (
            await session.execute(
                select(AiTaskResult).where(AiTaskResult.ai_task_id == task_id).order_by(AiTaskResult.id.desc())
            )
        )
        .scalars()
        .first()
    )
    assert row is not None, f"AI 任务 {task_id} 没有产出落库"
    return row


# ======================================================================
#  验收 ①：图片重绘 —— 落 asset，且能可靠区分主图 / 详情图与序号
# ======================================================================


async def test_image_redraw_persists_assets_with_role_and_index(session: Any) -> None:
    """`image_redraw`：产出图落 `asset`（`origin=ai_rework`），角色与序号可区分。"""
    _ensure_distinct_client()
    product_id = await _make_product(session)
    await _seed_raw_images(session, product_id, count=3)
    task_id = await _make_task(
        session,
        product_id,
        task_type=AiTaskType.IMAGE_REDRAW.value,
        ai_client=DISTINCT_CLIENT_NAME,
        input_prompt_json=AiInputPrompt(global_prompt="整体偏日式极简").to_dict(),
    )
    try:
        await AiTaskService.run_task(task_id)

        result = await _latest_result(session, task_id)
        asset_ids = [int(i) for i in (result.output_asset_ids_json or [])]
        assert len(asset_ids) == 3, f"重绘 3 张图应落 3 个 asset，实际 {len(asset_ids)}：{asset_ids}"

        rows = (await session.execute(select(Asset).where(Asset.id.in_(asset_ids)))).scalars().all()
        by_id = {int(a.id): a for a in rows}
        ordered = [by_id[i] for i in asset_ids]
        assert len(rows) == 3, f"3 张图的 content_hash 必须互不相同（否则被去重），实际 {len(rows)} 行"

        assert ordered[0].asset_type == AssetType.MAIN_IMAGE.value, (
            f"第 0 张应是主图，实际 {ordered[0].asset_type}"
        )
        assert [a.asset_type for a in ordered[1:]] == [AssetType.DETAIL_IMAGE.value] * 2, (
            f"后两张应是详情图，实际 {[a.asset_type for a in ordered[1:]]}"
        )
        assert all(a.origin == "ai_rework" for a in ordered), "重绘产物 origin 必须是 ai_rework"

        # ★ 角色 / 序号必须落在记录里：将来换文件命名规则时不必重做管线
        roles = [(a.tags_json or {}).get("image_role") for a in ordered]
        indexes = [(a.tags_json or {}).get("index") for a in ordered]
        assert roles == ["main_image", "detail_image", "detail_image"], f"image_role 落库不对：{roles}"
        assert indexes == [0, 1, 2], f"序号落库不对：{indexes}"

        # ★ 全局提示词生效（未给逐图提示词 ⇒ 回落到全局）
        assert all((a.tags_json or {}).get("prompt") == "整体偏日式极简" for a in ordered), (
            "未给逐图提示词时应回落到全局提示词"
        )
        assert all((a.tags_json or {}).get("prompt_source") == "global" for a in ordered), (
            f"prompt_source 应为 global，实际 {[(a.tags_json or {}).get('prompt_source') for a in ordered]}"
        )
    finally:
        await _cleanup(session, product_id, task_id)
        _drop_distinct_client()


# ======================================================================
#  验收 ①：标题建议 —— 候选数组落库，≥3 条，首选进 output_title
# ======================================================================


async def test_title_suggest_persists_candidates_and_preferred_title(session: Any) -> None:
    """`title_suggest`：候选数组落 `output_title_candidates_json`，`output_title` 是首选那条。"""
    product_id = await _make_product(session)
    task_id = await _make_task(
        session,
        product_id,
        task_type=AiTaskType.TITLE_SUGGEST.value,
        input_prompt_json=AiInputPrompt(title_prompt="偏抖音热词风").to_dict(),
    )
    try:
        await AiTaskService.run_task(task_id)

        result = await _latest_result(session, task_id)
        candidates = result.output_title_candidates_json
        assert isinstance(candidates, list) and len(candidates) >= 3, (
            f"标题候选必须 ≥3 条供挑选，实际 {len(candidates) if isinstance(candidates, list) else candidates}"
        )
        titles = [str(c.get("title", "")) for c in candidates if isinstance(c, dict)]
        assert len(set(titles)) == len(titles), f"候选必须互不相同，实际 {titles}"
        for candidate in candidates:
            assert str(candidate.get("style", "")).strip(), f"候选缺 style（无从比较）：{candidate}"
            assert str(candidate.get("reason", "")).strip(), f"候选缺 reason（无从选择）：{candidate}"

        # ★ `output_title` = 首选（最高分那条）：即便使用者一条都不挑，下游也拿得到标题
        best = max(candidates, key=lambda c: int(c.get("score", 0)))
        assert result.output_title == best["title"], (
            f"output_title 应为最高分候选，实际 {result.output_title!r} vs {best['title']!r}"
        )
        # ★ 未发生选择 ⇒ 选中留痕必须是"没有"，不能伪造
        assert result.selected_title_index is None, "未选择时 selected_title_index 必须为 None"
        assert result.selected_title_at is None, "未选择时 selected_title_at 必须为 None"
        assert "抖音" in str(result.prompt_snapshot or ""), (
            f"标题提示词应进快照（可复盘），实际 {result.prompt_snapshot!r}"
        )
    finally:
        await _cleanup(session, product_id, task_id)


# ======================================================================
#  验收 ①：视频脚本 —— 脚本结构落库，且不含任何媒体文件
# ======================================================================


async def test_video_script_persists_script_without_media(session: Any) -> None:
    """`video_script`：脚本落 `output_video_script_json`（只文案，不生成视频）。"""
    product_id = await _make_product(session)
    task_id = await _make_task(
        session,
        product_id,
        task_type=AiTaskType.VIDEO_SCRIPT.value,
        input_prompt_json=AiInputPrompt(video_script_prompt="竖屏、快节奏、强调性价比").to_dict(),
    )
    try:
        await AiTaskService.run_task(task_id)

        result = await _latest_result(session, task_id)
        script = result.output_video_script_json
        assert isinstance(script, dict) and script, f"视频脚本未落库：{script!r}"
        scenes = script.get("scenes")
        assert isinstance(scenes, list) and len(scenes) >= 1, f"脚本必须含分镜，实际 {scenes!r}"
        for scene in scenes:
            assert str(scene.get("shot", "")).strip(), f"分镜缺画面描述：{scene}"
            assert str(scene.get("narration", "")).strip(), f"分镜缺口播台词：{scene}"
        assert float(script.get("total_duration_sec", 0)) > 0, "脚本必须给出总时长"
        assert str(script.get("text", "")).strip(), "脚本应带渲染好的纯文本（可直接照着拍）"
        assert "竖屏" in str(script.get("style", "")), f"拍摄风格应回填使用者输入，实际 {script.get('style')!r}"

        # ★ 只出文案：不能出现任何视频文件痕迹
        serialized = str(script).lower()
        for forbidden in ("video_path", "file_path", ".mp4", ".mov", "storage_path"):
            assert forbidden not in serialized, f"脚本里不该出现媒体文件字段：{forbidden}"
        assert not (result.output_asset_ids_json or []), "视频脚本不产出素材"
    finally:
        await _cleanup(session, product_id, task_id)


# ======================================================================
#  验收 ②：选定标题候选 → 下游发布链路读到选中的那条
# ======================================================================


async def test_select_title_candidate_writes_output_title_and_publish_reads_it(session: Any) -> None:
    """选定候选后：`output_title` 变选中那条，**`PublishService._resolve_title()` 读到它**。"""
    product_id = await _make_product(session)
    task_id = await _make_task(
        session, product_id, task_type=AiTaskType.TITLE_SUGGEST.value
    )
    try:
        await AiTaskService.run_task(task_id)
        result = await _latest_result(session, task_id)
        candidates = result.output_title_candidates_json or []
        assert len(candidates) >= 3, f"候选不足 3 条：{candidates}"

        # ★ 故意选**非首选**那条：选了首选也能过的话，这条断言证明不了任何事
        best_index = max(range(len(candidates)), key=lambda i: int(candidates[i].get("score", 0)))
        picked_index = 1 if best_index != 1 else 2
        picked_title = str(candidates[picked_index]["title"])
        assert picked_title != result.output_title, "本用例必须选一条与首选不同的候选才有意义"

        updated = await AiTaskService.select_title_candidate(
            session, task_id, index=picked_index, operator="tester"
        )
        await session.commit()

        # ① 直接查库：output_title 确实变成选中的那条
        row = await _latest_result(session, task_id)
        assert row.output_title == picked_title, (
            f"选定后 output_title 应为 {picked_title!r}，实际 {row.output_title!r}"
        )
        assert int(updated.selected_title_index or -1) == picked_index
        assert int(row.selected_title_index or -1) == picked_index, "选中下标未落库"
        assert row.selected_title_at is not None, "选中时间未落库（无法追溯何时定的）"
        assert row.selected_title_by == "tester", f"选中人未落库：{row.selected_title_by!r}"

        # ② 发布链路：Basic 上架取标题的真实入口必须拿到选中的那条
        product = await session.get(SourceProduct, product_id)
        publish_task = SimpleNamespace(ai_task_result_id=int(row.id))
        title_used = await PublishService._resolve_title(session, publish_task, product)
        assert title_used == picked_title, (
            f"发布链路取到的标题应是选中的那条，实际 {title_used!r}"
        )
        assert title_used != product.title, "发布链路仍在使用货源原标题 ⇒ 选定没闭环"
    finally:
        await _cleanup(session, product_id, task_id)


# ======================================================================
#  验收 ③：存量 `ai_rework` 路径无回归
# ======================================================================


async def test_ai_rework_legacy_path_unchanged(session: Any) -> None:
    """不带任何新参数的老任务：仍走 `rework_images()`，产出与改动前一致。"""
    product_id = await _make_product(session, title="老链路回归商品")
    await _seed_raw_images(session, product_id, count=2)
    # ★ 完全按老口径构造：不传 task_type / input_prompt（默认 ai_rework）
    task_id = await _make_task(session, product_id, task_type=AiTaskType.AI_REWORK.value)
    try:
        task = await session.get(AiTask, task_id)
        assert str(task.task_type) == AiTaskType.AI_REWORK.value
        assert task.input_prompt_json is None, "不带提示词时不应落空壳 {}"

        await AiTaskService.run_task(task_id)

        await session.rollback()
        task = await session.get(AiTask, task_id)
        assert task is not None and task.status == AiTaskStatus.PENDING_REVIEW.value, (
            f"老链路跑完应转 pending_review，实际 {task.status}"
        )

        result = await _latest_result(session, task_id)
        asset_ids = [int(i) for i in (result.output_asset_ids_json or [])]
        assert asset_ids, "老链路必须有素材落库"
        rows = (
            (await session.execute(select(Asset).where(Asset.id.in_(asset_ids)).order_by(Asset.id)))
            .scalars()
            .all()
        )
        assert rows[0].asset_type == AssetType.MAIN_IMAGE.value, (
            f"老链路第一张仍是主图（存量口径不变），实际 {rows[0].asset_type}"
        )
        assert all(a.origin == "ai_rework" for a in rows)
        # ★ 标题回落：老产出不含 title_result ⇒ 取货源原标题（不改既有回落逻辑）
        product = await session.get(SourceProduct, product_id)
        assert result.output_title == product.title, (
            f"老链路标题回落应为货源原标题，实际 {result.output_title!r}"
        )
        # ★ 新列对存量路径保持"没有就是没有"
        assert result.output_title_candidates_json is None, "老链路不该产出标题候选"
        assert result.output_video_script_json is None, "老链路不该产出视频脚本"
    finally:
        await _cleanup(session, product_id, task_id)


# ======================================================================
#  验收 ④：逐图提示词闭环
# ======================================================================


async def test_per_image_prompts_reach_each_image(session: Any) -> None:
    """3 张图各一句不同提示词 ⇒ 每张产出各自对应，`prompt_source == per_image`。"""
    _ensure_distinct_client()
    product_id = await _make_product(session)
    await _seed_raw_images(session, product_id, count=3)
    per_image = ["主图换成暖色背景", "详情图强调面料纹理", "详情图突出尺寸对比"]
    payload = AiInputPrompt(
        global_prompt="整体偏日式极简",
        images=[AiImagePrompt(index=i, prompt=text) for i, text in enumerate(per_image)],
    )
    task_id = await _make_task(
        session,
        product_id,
        task_type=AiTaskType.IMAGE_REDRAW.value,
        ai_client=DISTINCT_CLIENT_NAME,
        input_prompt_json=payload.to_dict(),
    )
    try:
        await AiTaskService.run_task(task_id)

        result = await _latest_result(session, task_id)
        asset_ids = [int(i) for i in (result.output_asset_ids_json or [])]
        assert len(asset_ids) == 3, f"应为 3 张图各落一个 asset，实际 {len(asset_ids)}"
        rows = (await session.execute(select(Asset).where(Asset.id.in_(asset_ids)))).scalars().all()
        by_id = {int(a.id): a for a in rows}
        ordered = [by_id[i] for i in asset_ids]

        actual_prompts = [str((a.tags_json or {}).get("prompt", "")) for a in ordered]
        assert actual_prompts == per_image, f"逐图提示词未一一对应：{actual_prompts} != {per_image}"
        sources = [(a.tags_json or {}).get("prompt_source") for a in ordered]
        assert sources == [PROMPT_SOURCE_PER_IMAGE] * 3, f"prompt_source 应全为 per_image，实际 {sources}"

        # ★ 提示词快照里也要看得到（跨产次复盘用）
        snapshot = str(result.prompt_snapshot or "")
        for text in per_image:
            assert text in snapshot, f"提示词快照缺 {text!r}：{snapshot!r}"
    finally:
        await _cleanup(session, product_id, task_id)
        _drop_distinct_client()


# ======================================================================
#  验收 ⑤：未知 task_type 显式报错，不静默
# ======================================================================


async def test_unknown_task_type_is_rejected(session: Any) -> None:
    """未知类型：创建时直接拒；任务行上是未知类型时执行报错 + 标失败。"""
    from app.core.errors import BusinessError

    product_id = await _make_product(session)

    with pytest.raises(BusinessError) as excinfo:
        await AiTaskService.create_tasks(
            session, source_product_ids=[product_id], target_platform="taobao", task_type="no_such_type"
        )
    assert "不支持的 AI 任务类型" in str(excinfo.value), f"报错信息不明确：{excinfo.value}"
    await session.rollback()

    # ★ 行上被塞了未知类型（例如手工改库 / 未来枚举下线）：不能静默按默认跑
    task_id = await _make_task(session, product_id, task_type="no_such_type")
    try:
        with pytest.raises(BusinessError):
            await AiTaskService.run_task(task_id)
        await session.rollback()
        task = await session.get(AiTask, task_id)
        assert task is not None and task.status == AiTaskStatus.FAILED.value, (
            f"未知类型必须标失败（不能停在 running），实际 {task.status}"
        )
    finally:
        await _cleanup(session, product_id, task_id)


# ======================================================================
#  验收 ⑥：HTTP 端点（创建 → 详情 → 选定）
# ======================================================================


async def test_http_create_detail_and_select_title(
    client: Any, session: Any, monkeypatch: Any
) -> None:
    """HTTP：创建（带 task_type / 提示词）→ 详情返回候选 → 选定写入 `output_title`。

    ★ 为什么把 `enqueue_task` 打桩成空操作：本用例验证的是**端点契约**（创建 / 详情 / 选定），
      不是调度器。真入队会拉起常驻 runner 线程，它在本用例结束后仍然活着，
      与后续 `test_task_runner_hygiene.py` 的 runner 抢 SQLite 单写者
      ⇒ 那条用例会**闪断**（实测：不打桩时全量套件偶发失败，单跑却总是绿）。
      执行改为同步调用 `run_task()`，断言因此是确定性的。
    """

    async def _noop_enqueue(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("app.api.v1.ai_tasks.enqueue_task", _noop_enqueue)

    # ★ 先切 mock：默认 file_bridge 会真等外部代理产出（最长 1800s），测试里跑不完
    switched = await client.put(
        "/api/v1/settings/ai.client",
        json={"value": "mock", "reason": "能力接入验收：离线跑通"},
        headers=ADMIN_HEADERS,
    )
    assert switched.status_code == 200, f"切换 ai.client 失败：{switched.text[:200]}"

    product_id = await _make_product(session, title="HTTP 链路验收商品")
    created = await client.post(
        "/api/v1/ai-tasks",
        json={
            "source_product_ids": [product_id],
            "target_platform": "taobao",
            "task_type": AiTaskType.TITLE_SUGGEST.value,
            "title_prompt": "偏搜索词堆砌",
        },
        headers=ADMIN_HEADERS,
    )
    assert created.status_code == 202, f"创建 AI 任务未受理：{created.status_code} {created.text[:300]}"
    assert created.json()["data"]["task_type"] == AiTaskType.TITLE_SUGGEST.value
    task_ids = created.json()["data"]["task_ids"]
    assert task_ids, "创建接口未返回 task_ids"
    task_id = int(task_ids[0])

    try:
        # ★ 入队被打桩 ⇒ 同步跑一次，让"HTTP 创建的任务能被能力链路执行"也被证明
        #   （顺带验证 `task_type` / 提示词确实从 HTTP 落到了任务行上）
        await AiTaskService.run_task(task_id)
        await session.rollback()  # ★ 结束本会话读快照，读到 run_task 已提交的状态
        task_row = await session.get(AiTask, task_id)
        assert task_row is not None and task_row.status == AiTaskStatus.PENDING_REVIEW.value, (
            f"AI 任务未跑完，实际 status={task_row.status if task_row else None}"
        )

        # ① 详情必须返回标题候选（前端要展示）
        detail = await client.get(f"/api/v1/ai-tasks/{task_id}", headers=ADMIN_HEADERS)
        assert detail.status_code == 200, f"详情不可达：{detail.text[:200]}"
        payload = detail.json()["data"]
        assert payload["task_type"] == AiTaskType.TITLE_SUGGEST.value, f"详情未回显 task_type：{payload}"
        candidates = payload["result"]["output_title_candidates"]
        assert len(candidates) >= 3, f"详情返回的候选不足 3 条：{candidates}"

        # ② 选定第 2 条 → `output_title` 写回（下游发布链路读的就是它）
        picked_title = str(candidates[1]["title"])
        selected = await client.post(
            f"/api/v1/ai-tasks/{task_id}/select-title",
            json={"index": 1, "note": "验收：选第 2 条"},
            headers=ADMIN_HEADERS,
        )
        assert selected.status_code == 200, f"选定标题失败：{selected.status_code} {selected.text[:300]}"
        assert selected.json()["data"]["output_title"] == picked_title, (
            f"接口返回的 output_title 不对：{selected.json()['data']['output_title']!r} != {picked_title!r}"
        )
        assert selected.json()["data"]["selected_title_index"] == 1

        # ③ 直接查库：接口说成功不等于写进去了
        row = await _latest_result(session, task_id)
        assert row.output_title == picked_title, f"库里 output_title 未更新：{row.output_title!r}"
        assert int(row.selected_title_index or -1) == 1, "库里未记录选中下标"
        assert row.selected_title_at is not None, "库里未记录选中时间"

        # ④ 发布链路拿到的就是选中的那条
        product = await session.get(SourceProduct, product_id)
        title_used = await PublishService._resolve_title(
            session, SimpleNamespace(ai_task_result_id=int(row.id)), product
        )
        assert title_used == picked_title, f"发布链路取到的标题不对：{title_used!r}"
    finally:
        await _cleanup(session, product_id, task_id)
