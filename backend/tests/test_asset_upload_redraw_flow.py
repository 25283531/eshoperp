"""★ 手工上传 → 素材库 → AI 重绘闭环（补齐 5 个后端缺口的验收）。

逐条对应本次补齐的内容：
    ① `POST /api/v1/assets/upload`：真实 HTTP 上传，落盘必须是「中文子目录 + 序号」
      （`主图/01.png`、`详情页/01.png`），且 `asset` 记录逐字段正确；
    ② 上传的素材能被 AI 重绘链路取到：素材列表可见 → 作为 `image_prompts` 的来源
      提交重绘任务 → 任务详情能同时看到「原图」与「AI 产出」；
    ③ 序号顺延与内容去重；
    ④ 安全：非图片 / 超大 / 恶意文件名全部被挡住，且不产生路径穿越；
    ⑤ `GET /ai-tasks?task_type=` 筛选真的生效（此前是被静默忽略的）。

★ 为什么这一组验证必须**肉眼可见**（列目录 + 查库）：
    1688 详情接口权限未开通（`gw.APIACLDecline`）期间，手工上传是唯一入口。
    它要是"看着成功、其实没落盘/没入库"，AI 重绘这条主链路就仍是死的 ——
    而这条链路恰恰是这次要解开的死结。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.adapters.ai.mock_client import write_placeholder_png
from app.core.config import get_settings
from app.models.asset import AiTask, AiTaskResult, Asset
from app.models.enums import AiTaskStatus, AiTaskType, AssetOrigin, AssetType
from app.models.source import SourceProduct
from app.services.ai_task_service import AiTaskService
from app.utils.kit import content_hash_bytes
from tests.conftest import ADMIN_HEADERS

__all__ = ["test_upload_sequence_increments_and_duplicate_is_idempotent"]


def _uniq(prefix: str) -> str:
    """生成唯一串（避开 `product_1688_id` / `content_hash` 的唯一约束，也不撞别人的目录）。"""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _real_png(tmp_path: Path, *, rgb: tuple[int, int, int], name: str = "pic.png", size: int = 48) -> bytes:
    """在磁盘上生成一张**真实存在**的 PNG 图片并返回其字节（纯 Python 生成，无 Pillow 依赖）。"""
    target = tmp_path / f"{_uniq(name.rsplit('.', 1)[0])}.png"
    write_placeholder_png(target, width=size, height=size, rgb=rgb)
    return target.read_bytes()


async def _make_product(session: Any) -> tuple[int, str]:
    """建货源商品（已提交），返回 `(id, product_1688_id)`。"""
    key = _uniq("upload")
    product = SourceProduct(product_1688_id=key, title="上传验证商品", params_json={"材质": "纯棉"})
    session.add(product)
    await session.commit()
    return int(product.id), key


async def _post_upload(client: Any, form: dict[str, Any], files: list[tuple[str, tuple[str, Any, str]]]) -> Any:
    """发一次上传请求，返回原始响应（成功与失败两种情况都要能断言 HTTP 状态码）。"""
    return await client.post(
        "/api/v1/assets/upload", data=form, files=files, headers=ADMIN_HEADERS
    )


async def _upload_ok(
    client: Any, form: dict[str, Any], files: list[tuple[str, tuple[str, Any, str]]]
) -> dict[str, Any]:
    """上传并返回 `data`（业务码必须为 0）。"""
    response = await _post_upload(client, form, files)
    assert response.status_code == 200, f"上传应返回 200，实际 {response.status_code}：{response.text[:400]}"
    body = response.json()
    assert body["code"] == 0, f"上传业务失败：{body}"
    return dict(body["data"])


async def _cleanup(session: Any, product_id: int, task_id: int | None = None) -> None:
    """清掉本用例写入的行（用例内部 commit 过，不清理会污染后续用例）。"""
    await session.rollback()
    if task_id is not None:
        for row in (
            (await session.execute(select(AiTaskResult).where(AiTaskResult.ai_task_id == task_id))).scalars()
        ):
            await session.delete(row)
        for row in (await session.execute(select(Asset).where(Asset.ai_task_id == task_id))).scalars():
            await session.delete(row)
        task = await session.get(AiTask, task_id)
        if task is not None:
            await session.delete(task)
    for row in (await session.execute(select(Asset).where(Asset.source_product_id == product_id))).scalars():
        await session.delete(row)
    product = await session.get(SourceProduct, product_id)
    if product is not None:
        await session.delete(product)
    await session.commit()


# ======================================================================
#  ① 真实上传：列目录 + 查库
# ======================================================================


async def test_upload_image_lands_in_chinese_dir_with_db_row(
    client: Any, session: Any, tmp_path: Path, settings: Any
) -> None:
    """上传一张真图 → 落在 `data/assets/raw/{商品ID}/主图/01.png`，且入库字段全对。"""
    product_id, product_key = await _make_product(session)
    image = _real_png(tmp_path, rgb=(12, 34, 56), name="主图A.png")

    try:
        data = await _upload_ok(
            client,
            {"source_product_id": str(product_id), "role": "main_image"},
            [("files", ("主图A.png", image, "image/png"))],
        )
        assert data["created_count"] == 1, f"应新增 1 张：{data}"
        assert data["failed"] == [], f"合法图片不应被拒：{data['failed']}"

        created = data["created"][0]
        path = Path(str(created["storage_path"]))

        # ★ 落盘：**列目录**证明是「中文子目录 + 序号」，而不是拼出来的字符串
        assert path.exists(), f"落库路径在磁盘上不存在（悬空引用）：{path}"
        assert path.read_bytes() == image, "磁盘内容与上传字节不一致"
        assert path.parent.name == "主图", f"应在中文角色目录下，实际 {path.parent.name}"
        assert path.name == "01.png", f"文件名应为两位序号，实际 {path.name}"
        assert path.parent.parent.name == product_key, f"应归到该商品目录，实际 {path.parent.parent.name}"
        assert path.parent.parent.parent == settings.assets_dir / "raw"
        # ★ 客户端文件名不参与拼路径（只用于展示）
        assert str(path).startswith(str(settings.assets_dir / "raw" / product_key)), f"落盘路径越界：{path}"

        # ★ 查库：`asset` 行逐字段核对（不信 VO，直接 select）
        await session.rollback()
        asset = (
            await session.execute(select(Asset).where(Asset.id == int(created["id"])))
        ).scalars().first()
        assert asset is not None, f"资产未入库：id={created['id']}"
        assert str(asset.storage_path) == str(path)
        assert asset.asset_type == AssetType.MAIN_IMAGE.value, f"asset_type 错：{asset.asset_type}"
        assert asset.origin == AssetOrigin.RAW.value, f"手工上传 origin 必须是 raw：{asset.origin}"
        assert asset.content_hash == content_hash_bytes(image), "content_hash 与文件内容不符"
        assert int(asset.size_bytes or 0) == len(image)
        tags = dict(asset.tags_json or {})
        assert tags.get("image_role") == "main_image", f"tags_json 缺角色：{tags}"
        assert tags.get("index") == 1, f"tags_json 缺序号：{tags}"

        # ★ VO 必须透出 image_role / index（此前被拍平丢失）
        assert created.get("image_role") == "main_image", f"VO 未透出 image_role：{created}"
        assert created.get("index") == 1, f"VO 未透出 index：{created}"
        assert created.get("tags") == ["main_image"], f"向后兼容：tags 仍需返回 {created.get('tags')}"
    finally:
        await _cleanup(session, product_id)


async def test_upload_detail_image_uses_detail_dir(client: Any, session: Any, tmp_path: Path) -> None:
    """角色切到 `detail_image`（也认中文"详情页"）→ 落在 `详情页/01.png`。"""
    product_id, product_key = await _make_product(session)
    first = _real_png(tmp_path, rgb=(90, 90, 120), name="详情1.png")
    second = _real_png(tmp_path, rgb=(70, 70, 100), name="详情2.png")

    try:
        # ★ 传中文角色名也要认（前端不必区分中英文两套取值）
        data = await _upload_ok(
            client,
            {"source_product_id": str(product_id), "role": "详情页"},
            [("files", ("详情1.png", first, "image/png"))],
        )
        path = Path(str(data["created"][0]["storage_path"]))
        assert path.parent.name == "详情页", f"中文角色应落到详情页目录，实际 {path.parent.name}"
        assert path.name == "01.png"

        data = await _upload_ok(
            client,
            {"source_product_id": str(product_id), "role": "detail_image"},
            [("files", ("详情2.png", second, "image/png"))],
        )
        path = Path(str(data["created"][0]["storage_path"]))
        assert path.parent.name == "详情页" and path.name == "02.png", f"同角色应顺延：{path}"
        # ★ 主图目录没有被误建
        assert not (Path(get_settings().assets_dir) / "raw" / product_key / "主图").exists()
    finally:
        await _cleanup(session, product_id)


# ======================================================================
#  ③ 序号顺延 + 内容去重
# ======================================================================


async def test_upload_sequence_increments_and_duplicate_is_idempotent(
    client: Any, session: Any, tmp_path: Path
) -> None:
    """一次传 3 张同角色 → 01/02/03；重复传同一张 → 按 `content_hash` 去重，不报错也不覆盖。"""
    product_id, product_key = await _make_product(session)
    images = [_real_png(tmp_path, rgb=(200, 10 + i * 60, 10), name=f"批次{i}.png") for i in range(3)]

    try:
        data = await _upload_ok(
            client,
            {"source_product_id": str(product_id), "role": "main_image"},
            [("files", (f"批次{i}.png", blob, "image/png")) for i, blob in enumerate(images)],
        )
        assert data["created_count"] == 3, f"一次上传 3 张应全部成功：{data}"
        names = sorted(Path(str(item["storage_path"])).name for item in data["created"])
        assert names == ["01.png", "02.png", "03.png"], f"序号未逐一递增：{names}"
        indexes = sorted(int(item["index"]) for item in data["created"])
        assert indexes == [1, 2, 3], f"记录的序号应分别为 1/2/3：{indexes}"

        # ★ 同一个目录里真的只有 3 个文件（顺延，不是覆盖）
        main_dir = Path(get_settings().assets_dir) / "raw" / product_key / "主图"
        assert sorted(p.name for p in main_dir.iterdir()) == ["01.png", "02.png", "03.png"]
        first_id = int(data["created"][0]["id"])

        # ★ 重复上传同一张（换文件名）→ 幂等命中，返回既有素材 ID
        data = await _upload_ok(
            client,
            {"source_product_id": str(product_id), "role": "main_image"},
            [("files", ("换个名字再传.png", images[0], "image/png"))],
        )
        assert data["created_count"] == 0, f"相同内容不应再建一条：{data}"
        assert data["duplicated_count"] == 1, f"应识别为重复内容：{data}"
        assert int(data["duplicated"][0]["id"]) == first_id, "去重应指回原来那条素材"
        assert data["failed"] == [], "重复上传不是错误，不该进 failed"

        await session.rollback()
        rows = (
            (await session.execute(select(Asset).where(Asset.source_product_id == product_id))).scalars().all()
        )
        assert len(rows) == 3, f"重复上传后库里仍应是 3 条，实际 {len(rows)}"
        # ★ 原文件没被覆盖：落盘字节仍是第一张
        assert (main_dir / "01.png").read_bytes() == images[0], "去重时不得覆盖原文件"
        assert sorted(p.name for p in main_dir.iterdir()) == ["01.png", "02.png", "03.png"]
    finally:
        await _cleanup(session, product_id)


# ======================================================================
#  ④ 安全
# ======================================================================


async def test_upload_rejects_bad_input_and_blocks_path_traversal(
    client: Any, session: Any, tmp_path: Path, settings: Any
) -> None:
    """伪装扩展名 / 非法类型 / 超大文件 / `../` 文件名：全部拦下，且文件名不参与拼路径。"""
    product_id, product_key = await _make_product(session)
    good = _real_png(tmp_path, rgb=(30, 60, 90), name="正常图.png")
    base_dir = settings.assets_dir / "raw" / product_key

    try:
        # ① 扩展名叫 .jpg，内容其实是可执行正文 ⇒ 必须**按文件头**识破
        data = await _upload_ok(
            client,
            {"source_product_id": str(product_id), "role": "main_image"},
            [("files", ("cat.jpg", b"MZ\x90\x00" + b"\x00" * 128, "image/jpeg"))],
        )
        assert data["created_count"] == 0 and data["failed_count"] == 1, f"伪装扩展名未被识破：{data}"
        assert "文件头" in data["failed"][0]["reason"], f"拒绝原因要说清：{data['failed']}"

        # ② 不在白名单里的扩展名
        data = await _upload_ok(
            client,
            {"source_product_id": str(product_id), "role": "main_image"},
            [("files", ("说明.txt", good, "text/plain"))],
        )
        assert data["created_count"] == 0, f"非白名单类型应被拒：{data}"
        assert "不支持的文件类型" in data["failed"][0]["reason"], f"拒绝原因不对：{data['failed']}"

        # ③ 超大文件（超过 10MB 上限）
        blob = b"\x89PNG\r\n\x1a\n" + b"P" * (10 * 1024 * 1024 + 1)
        data = await _upload_ok(
            client,
            {"source_product_id": str(product_id), "role": "main_image"},
            [("files", ("巨大.png", blob, "image/png"))],
        )
        assert data["created_count"] == 0, f"超限文件应被拒：{data}"
        assert "MB" in data["failed"][0]["reason"], f"必须给出明确超限提示：{data['failed']}"

        # ④ 恶意文件名：**内容合法可以收**，但路径必须由服务端生成
        data = await _upload_ok(
            client,
            {"source_product_id": str(product_id), "role": "main_image"},
            [("files", ("../../../../evil.png", good, "image/png"))],
        )
        assert data["created_count"] == 1, f"图片内容合法应正常收下：{data}"
        path = Path(str(data["created"][0]["storage_path"]))
        assert path.is_relative_to(base_dir), f"落盘路径逃出了商品目录（路径穿越）：{path}"
        assert path.parent.name == "主图" and path.name == "01.png", f"文件名必须服务端生成：{path}"
        assert "evil" not in str(path) and ".." not in str(path), f"客户端文件名不得出现在路径里：{path}"
        for candidate in (
            settings.assets_dir / "raw" / "evil.png",
            settings.assets_dir / "evil.png",
            base_dir / "evil.png",
        ):
            assert not candidate.exists(), f"恶意文件名写到了目录外：{candidate}"

        # ⑤ 超过单次数量上限 ⇒ 显式报错（不静默截断）
        too_many = [("files", (f"图{i}.png", good, "image/png")) for i in range(21)]
        response = await _post_upload(client, {"source_product_id": str(product_id)}, too_many)
        assert response.status_code == 400, f"超量应报 400，实际 {response.status_code}"
        body = response.json()
        assert body["code"] == 1001 and "21" in body["message"], f"错误要说清是超限：{body}"

        # ⑥ 不带文件字段 ⇒ 在入参层就被挡下（`files` 是必填，422）
        response = await _post_upload(client, {"source_product_id": str(product_id)}, [])
        assert response.status_code == 422, f"缺文件应报 422，实际 {response.status_code}"
        # ⑦ 角色非法
        response = await _post_upload(
            client,
            {"source_product_id": str(product_id), "role": "随便一个角色"},
            [("files", ("正常图.png", good, "image/png"))],
        )
        assert response.status_code == 400 and "不支持的素材角色" in response.json()["message"]
        # ⑧ 商品不存在
        response = await _post_upload(
            client,
            {"source_product_id": "99999999", "role": "main_image"},
            [("files", ("正常图.png", good, "image/png"))],
        )
        assert response.status_code == 404, f"商品不存在应 404，实际 {response.status_code}"
    finally:
        await _cleanup(session, product_id)


# ======================================================================
#  ② + ⑤：上传 → 素材列表 → 重绘任务 → 任务详情
# ======================================================================


async def test_uploaded_images_flow_into_image_redraw_task(client: Any, session: Any, tmp_path: Path) -> None:
    """解死结的硬证据：上传的图能被 AI 重绘链路取到，且任务详情里原图 / 产出都在。"""
    product_id, product_key = await _make_product(session)
    first = _real_png(tmp_path, rgb=(210, 40, 40), name="重绘输入1.png", size=32)
    second = _real_png(tmp_path, rgb=(40, 210, 90), name="重绘输入2.png", size=40)
    task_id: int | None = None

    try:
        # ① 上传（两条原图）
        data = await _upload_ok(
            client,
            {"source_product_id": str(product_id), "role": "main_image"},
            [
                ("files", ("重绘输入1.png", first, "image/png")),
                ("files", ("重绘输入2.png", second, "image/png")),
            ],
        )
        raw_paths = [str(item["storage_path"]) for item in data["created"]]
        raw_ids = [int(item["id"]) for item in data["created"]]

        # ② 素材列表可见（前端素材页 / 任务详情的资源来源）
        listing = await client.get(
            f"/api/v1/assets?source_product_id={product_id}&origin=raw", headers=ADMIN_HEADERS
        )
        assert listing.status_code == 200, listing.text[:400]
        listed_ids = {int(item["id"]) for item in listing.json()["data"]["items"]}
        assert set(raw_ids).issubset(listed_ids), f"上传的素材在列表里看不到：{raw_ids} vs {listed_ids}"

        # ③ 以这些素材作为 `image_prompts` 的来源提交重绘任务
        #    ★ 先把 AI 客户端切成 mock（本机没有 WorkBuddy 代理）：
        #      任务的 ai_client 在**创建时固化**，改动此项才能让入队的任务也跑 mock 产出。
        switched = await client.put(
            "/api/v1/settings/ai.client",
            json={"value": "mock", "reason": "上传链路验证"},
            headers=ADMIN_HEADERS,
        )
        assert switched.status_code == 200, switched.text[:400]
        created = await client.post(
            "/api/v1/ai-tasks",
            json={
                "source_product_ids": [product_id],
                "target_platform": "taobao",
                "task_type": AiTaskType.IMAGE_REDRAW.value,
                "global_prompt": "整体偏日式极简",
                "image_prompts": [
                    {"index": 0, "asset_id": raw_ids[0], "source_path": raw_paths[0], "prompt": "换成米白背景"},
                    {"index": 1, "asset_id": raw_ids[1], "source_path": raw_paths[1], "prompt": "补充场景摆拍"},
                ],
            },
            headers=ADMIN_HEADERS,
        )
        assert created.status_code == 202, f"提交重绘任务失败：{created.status_code} {created.text[:400]}"
        body = created.json()
        assert body["data"]["task_type"] == AiTaskType.IMAGE_REDRAW.value
        task_id = int(body["data"]["task_ids"][0])

        # ★ 入参真的落进了 prompt 契约：这批上传的素材 ID 与重绘任务绑在一起
        await session.rollback()
        task = await session.get(AiTask, task_id)
        assert task is not None, f"AI 任务未落库：{task_id}"
        images = list((task.input_prompt_json or {}).get("images") or [])
        assert [int(i["asset_id"]) for i in images] == raw_ids, f"逐图提示词未带上上传的素材：{images}"
        assert str(task.task_type) == AiTaskType.IMAGE_REDRAW.value

        # ★ 上传的本地路径真的被 AI 重绘链路读走（`collect_source_image_paths` 的输入）
        paths = await AiTaskService.collect_source_image_paths(session, product_id)
        assert set(raw_paths).issubset(set(paths)), f"重绘上下文拿不到上传的图：{paths} vs {raw_paths}"

        # ④ 等这条重绘任务真正跑完产出
        #    ★ 上传的材料是什么 → AI 取到的就是什么，这一段是真实链路；只有"谁来产出"
        #      被换成了 mock（本机没有 WorkBuddy 代理）。
        await _ensure_task_finished(session, task_id)

        # ⑤ 任务详情：assets 不再恒为 []，且能区分原图与 AI 产出
        detail = await client.get(f"/api/v1/ai-tasks/{task_id}", headers=ADMIN_HEADERS)
        assert detail.status_code == 200, detail.text[:400]
        payload = detail.json()["data"]
        assets = payload["assets"]
        assert assets, "任务详情的 assets 不能是空数组（此前硬编码 []）"

        raw_in_detail = [a for a in assets if a["origin"] == AssetOrigin.RAW.value]
        ai_in_detail = [a for a in assets if a["origin"] == AssetOrigin.AI_REWORK.value]
        assert {int(a["id"]) for a in raw_in_detail} == set(raw_ids), (
            f"详情里应能看到这次上传的原图：{[a['id'] for a in raw_in_detail]}"
        )
        assert ai_in_detail, "详情里应有 AI 重绘产出"

        # ★ 产出方记录的 `source_path` 应指向**这次上传**的原图
        #   —— 这是"上传的图被 AI 重绘链路真的取走了"的硬证据（不是碰巧同名）。
        result_rows = [
            dict(row.tags_json or {})
            for row in (await session.execute(select(Asset).where(Asset.ai_task_id == task_id))).scalars().all()
        ]
        produced_source_paths = {str(row.get("source_path")) for row in result_rows if row.get("source_path")}
        assert set(raw_paths) & produced_source_paths, (
            f"AI 产出的 source_path 应指向上传的原图：{produced_source_paths} vs {raw_paths}"
        )

        # ★ result.output_assets 只装 AI 产出（原图是输入，不是这次的产出）
        output_assets = payload["result"]["output_assets"]
        assert output_assets, "result.output_assets 不应为空"
        assert all(a["origin"] == AssetOrigin.AI_REWORK.value for a in output_assets), (
            f"output_assets 混入了非 AI 产出：{[a['origin'] for a in output_assets]}"
        )
        # ★ 每个素材都能拿到 image_role / index（前端据此分组排序）
        for asset in assets:
            assert asset.get("image_role") in ("main_image", "detail_image", None), asset
        assert all(a["image_role"] == "main_image" for a in raw_in_detail)
        assert [a["index"] for a in sorted(raw_in_detail, key=lambda x: int(x["id"]))] == [1, 2]
    finally:
        if task_id is not None:
            await _cleanup(session, product_id, task_id=task_id)
        else:
            await _cleanup(session, product_id)
        restored = await client.put(
            "/api/v1/settings/ai.client",
            json={"value": "file_bridge", "reason": "验证结束还原"},
            headers=ADMIN_HEADERS,
        )
        assert restored.status_code == 200, restored.text[:200]


async def _ensure_task_finished(session: Any, task_id: int, budget: float = 20.0) -> None:
    """等 AI 任务跑到有产出为止；**绝不无条件自己再跑一遍**。

    ★★ 为什么必须先等 ★★
        `POST /ai-tasks` 除了建行还会**顺并入队一条 TaskRecord**，后台 `LocalTaskRunner`
        会执行同一个 ai_task。此时再手动 `run_task()` 一次 ⇒ 两个会话先后查
        `content_hash`（都查不到）、再各插一条**字节完全相同**的占位图
        ⇒ `uq_asset_content_hash` 冲突 ⇒ IntegrityError。
        这个坑只在全量跑时露头（单跑时序不同，看着是绿的），属于典型的假故障，
        修法是"让且只让一处执行"，而不是改宽并发。

    ★ 什么情况下才补一次手动执行：后台 runner 压根没起来
      （`ASGITransport` 不触发 lifespan），任务一直停在 queued。
      `running` 说明已经有人在处理，再补一次就是上面那个冲突。
    """
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        await session.rollback()
        row = await session.get(AiTask, task_id)
        if row is not None and str(row.status) in {
            AiTaskStatus.PENDING_REVIEW.value,
            AiTaskStatus.APPROVED.value,
            AiTaskStatus.REJECTED.value,
            AiTaskStatus.FAILED.value,
        }:
            return
        await asyncio.sleep(0.2)
    await session.rollback()
    row = await session.get(AiTask, task_id)
    if row is not None and str(row.status) == AiTaskStatus.QUEUED.value:
        await AiTaskService.run_task(task_id)


# ======================================================================
#  ⑤ 任务列表可按 task_type 筛选
# ======================================================================


async def test_ai_task_list_supports_task_type_filter(client: Any, session: Any) -> None:
    """`GET /ai-tasks?task_type=image_redraw` 必须真的过滤（此前传了被静默忽略）。"""
    product_id, _ = await _make_product(session)
    task_ids: list[int] = []
    try:
        for kind in (AiTaskType.AI_REWORK.value, AiTaskType.IMAGE_REDRAW.value):
            task = AiTask(
                source_product_id=product_id,
                target_platform="taobao",
                task_type=kind,
                ai_client="mock",
                status=AiTaskStatus.QUEUED.value,
                rework_items_json=["main_image"],
                created_by="tester",
            )
            session.add(task)
            await session.flush()
            task_ids.append(int(task.id))
        await session.commit()

        redraw_only = await client.get(
            f"/api/v1/ai-tasks?task_type={AiTaskType.IMAGE_REDRAW.value}", headers=ADMIN_HEADERS
        )
        assert redraw_only.status_code == 200, redraw_only.text[:400]
        items = redraw_only.json()["data"]["items"]
        assert items, "筛选 image_redraw 不应返回空（数据是自己刚建的）"
        assert all(item["task_type"] == AiTaskType.IMAGE_REDRAW.value for item in items), (
            f"筛选没生效，混入了别的类型：{[i['task_type'] for i in items]}"
        )
        assert task_ids[1] in {int(i["id"]) for i in items}, "筛选结果应包含刚建的那条重绘任务"
        assert task_ids[0] not in {int(i["id"]) for i in items}, "图文重构任务必须被过滤掉"

        # ★ 反证：不带筛选时两条都还在（不是"恰好库里只有重绘任务"）
        everything = await client.get("/api/v1/ai-tasks", headers=ADMIN_HEADERS)
        all_ids = {int(i["id"]) for i in everything.json()["data"]["items"]}
        assert all_ids.issuperset(set(task_ids)), f"未筛选时应看到全部任务：{all_ids} vs {task_ids}"
    finally:
        for tid in task_ids:
            row = await session.get(AiTask, tid)
            if row is not None:
                await session.delete(row)
        product = await session.get(SourceProduct, product_id)
        if product is not None:
            await session.delete(product)
        await session.commit()
