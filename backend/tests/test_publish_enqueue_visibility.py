"""★ 上架任务「入队可见性」+「映射校验作用域」回归护栏（2026-10-08 修复）。

================================================================================
★ 为什么要专门建这个文件（全是实测结论，不是推测）
================================================================================
1. `POST /publish-tasks` 长期返回 **202**，但 `data.task_record_ids` 是**空数组**；
   开发库 18/18 条 `publish_task.task_record_id` 全 NULL，`task_record` 表里
   **0 条** `publish` 类型 ⇒ 上架任务**从未真正入队**，16 条永远停在 `pending_precheck`，
   只有靠手工「回填商品 ID」才推进过 2 条。
   根因是两件事叠加：
     a. 请求会话 `add(task) → flush` 后**未提交**，写事务仍开着，
        而 `TaskRunner.submit()` 另开会话写 `task_record` 并 commit
        ⇒ SQLite 单写者模型下必然 `database is locked`；
     b. 这个异常被 `except Exception: return None` **吞掉** ⇒ 接口照样报"受理成功"。
   → 典型静默失效：返回体说成功，任务永远不跑。

2. 上架前的 `MappingValidator.validate()` 拿的是一组**尚不存在**的店铺 SKU 编码
   （按 `{平台}-{任务ID}-{序号}` 拼出来的预测值）去查映射是否存在，
   而映射只在 `publish()` / `fill_back()` **成功之后**才自动建立
   ⇒ 首次上架**必然**命中「N 项映射缺失」被硬拦截，半自动主路径永远走不通。
   → 「查一个还不存在的东西是否存在」不是校验，是恒失败。

本文件把这两条锁死：
    * 入队成功 → `task_record_ids` 非空 + `task_record` 真的跑到终态；
    * 入队失败 → **500 / 1098**（绝不再是 202）+ 失败原因落到任务行上（前端可见）；
    * 首次上架不被「预测编码」误判为映射缺失；
    * 已有映射但状态无效 → **仍然拦截**（证明闸门没被削弱）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import select

from app.models.asset import Asset
from app.models.enums import ListingMode, MappingStatus, PublishStatus
from app.models.mapping import SkuMapping
from app.models.publish import PublishTask
from app.models.source import SourceProduct, SourceSku
from app.services.publish_service import PublishService
from tests.conftest import ADMIN_HEADERS

pytestmark = pytest.mark.asyncio

TERMINAL_TASK_STATUSES = {"success", "failed", "cancelled"}


async def _wait_task_terminal(client: Any, task_id: int, *, timeout_s: float = 10.0) -> str:
    """轮询 `GET /api/v1/tasks/{id}` 到终态；超时返回当前状态（由断言去判定）。"""
    deadline = asyncio.get_event_loop().time() + timeout_s
    status_now = ""
    while True:
        resp = await client.get(f"/api/v1/tasks/{task_id}", headers=ADMIN_HEADERS)
        assert resp.status_code == 200, f"任务 {task_id} 不可查询：{resp.text[:200]}"
        status_now = str(resp.json()["data"]["status"] or "")
        if status_now in TERMINAL_TASK_STATUSES:
            return status_now
        if asyncio.get_event_loop().time() >= deadline:
            return status_now
        await asyncio.sleep(0.25)


async def _create_manual_product(client: Any, suffix: str) -> int:
    """用「手工录入」端点造一个可上架的货源商品（★ 不碰 1688 适配器）。"""
    resp = await client.post(
        "/api/v1/source-products/manual",
        json={
            "title": f"入队可见性用例商品 {suffix}",
            "product_code": f"ENQ-{suffix}",
            "cost_price": "12.00",
            "skus": [
                {
                    "spec_name": "颜色",
                    "spec_value": "红色",
                    "cost_price": "12.00",
                    "sale_price": "29.90",
                    "stock_qty": 100,
                }
            ],
        },
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 201, f"手工录入失败：{resp.status_code} {resp.text[:300]}"
    return int(resp.json()["data"]["id"])


# ======================================================================
#  一、入队成功：task_record 必须有、且必须真的推进
# ======================================================================
async def test_publish_task_really_enqueues_and_advances(client: Any, session: Any) -> None:
    """★ `POST /publish-tasks` 返回的 `task_record_ids` **不得为空**，且任务必须跑到终态。

    ★ 这是本缺陷的核心断言：旧实现同样返回 202，只是 `task_record_ids: []`
      —— 所以断言绝不能停在「202 就算过」，必须一路追到 `task_record` 终态
      与 `publish_task` 的状态推进。
    """
    product_id = await _create_manual_product(client, "ENQOK")

    resp = await client.post(
        "/api/v1/publish-tasks",
        json={
            "source_product_ids": [product_id],
            "platform": "taobao",
            "shop_id": "shop-enqueue-ok",
            "mode": "manual",
        },
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 202, f"上架未受理：{resp.status_code} {resp.text[:300]}"
    body = resp.json()["data"]

    # ---------- ★ 关键：入队记录不得为空 ----------
    assert body["task_record_ids"], (
        "★ 上架任务入队失败却被当成成功返回（task_record_ids 为空）——静默失效复发"
    )
    assert len(body["task_record_ids"]) == len(body["task_ids"])

    publish_task_id = int(body["task_ids"][0])
    record_id = int(body["task_record_ids"][0])

    # ---------- 任务必须跑到终态（不是永远 pending）----------
    status_now = await _wait_task_terminal(client, record_id)
    assert status_now in {"success", "failed"}, (
        f"上架任务未跑到终态（status={status_now}）—— 任务框架没接上"
    )

    # ---------- publish_task 必须真的推进（不再停在 pending_precheck）----------
    task = (
        await session.execute(select(PublishTask).where(PublishTask.id == publish_task_id))
    ).scalars().first()
    assert task is not None, "上架任务未落库"
    assert task.task_record_id == record_id, "task_record_id 未回写，前端无法追溯任务"
    assert task.status != PublishStatus.PENDING_PRECHECK.value, (
        f"上架任务未推进（status={task.status}）—— 任务没被执行"
    )


# ======================================================================
#  二、入队失败：必须 500 / 1098，绝不再是 202
# ======================================================================
async def test_publish_enqueue_failure_returns_500_not_202(
    client: Any, session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 队列投递失败 → **500 / 1098**，且失败原因落到 `publish_task.error_advice`。

    ★ 为什么必须同时断言"任务行上有原因"：
        只靠 HTTP 500，运营在列表页看到 16 条 `pending_precheck` 时仍然无从判断
        哪些真的在跑、哪些根本没入队（本缺陷正是这么隐藏了很久）。
    """
    import app.tasks.runner as runner_module

    def _broken_runner(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("模拟：任务队列不可用（runner 已关闭）")

    # `_enqueue_task()` 内部 `from app.tasks.runner import get_task_runner`
    # ⇒ 调用时才解析模块属性，monkeypatch 生效。
    monkeypatch.setattr(runner_module, "get_task_runner", _broken_runner)

    product_id = await _create_manual_product(client, "ENQFAIL")

    resp = await client.post(
        "/api/v1/publish-tasks",
        json={
            "source_product_ids": [product_id],
            "platform": "taobao",
            "shop_id": "shop-enqueue-fail",
            "mode": "manual",
        },
        headers=ADMIN_HEADERS,
    )

    # ---------- ★ 关键：不再返回"假装成功"的 202 ----------
    assert resp.status_code == 500, (
        f"入队失败必须返回 500，实际 {resp.status_code} {resp.text[:300]}"
    )
    body = resp.json()
    assert body["code"] == 1098, f"入队失败的业务码应为 1098，实际 {body['code']}"
    assert "入队失败" in body["message"], f"错误信息必须说清是入队失败：{body['message']}"

    failed = (body.get("data") or {}).get("failed") or []
    assert failed, "错误响应必须携带失败明细（哪个任务没入队）"
    publish_task_id = int(failed[0]["publish_task_id"])

    # ---------- 失败原因必须在任务行上可见 ----------
    task = (
        await session.execute(select(PublishTask).where(PublishTask.id == publish_task_id))
    ).scalars().first()
    assert task is not None
    assert task.task_record_id is None, "入队失败就不该有 task_record_id"
    assert "入队失败" in str(task.error_advice or ""), (
        f"任务行必须写明入队失败原因（前端可见），实际：{task.error_advice!r}"
    )


# ======================================================================
#  三、映射校验作用域：首次上架不得被「尚不存在的编码」拦死
# ======================================================================
async def _seed_publishable(session: Any, suffix: str) -> int:
    """造「货源商品 + SKU + 一张主图」并**提交**（预检要过必须得有素材）。"""
    product = SourceProduct(
        product_1688_id=f"MANUAL-VAL-{suffix}", title=f"校验作用域用例 {suffix}"
    )
    session.add(product)
    await session.flush()

    session.add(
        SourceSku(
            source_product_id=int(product.id),
            sku_code_1688=f"VAL-SKU-{suffix}",
            spec_json={"颜色": "红色"},
            spec_signature=f"val-sig-{suffix}",
            cost_price_cents=1200,
            stock_qty=50,
            status="on_sale",
        )
    )
    session.add(
        Asset(
            source_product_id=int(product.id),
            asset_type="main_image",
            origin="raw",
            storage_path=f"data/assets/val-{suffix}.jpg",
            content_hash=f"val-hash-{suffix}",
            is_current=True,
        )
    )
    await session.commit()
    return int(product.id)


async def test_first_publish_not_blocked_by_nonexistent_shop_sku_codes(session: Any) -> None:
    """★ 首次上架：没有任何映射时，不得因「预测编码查不到」被判 `validate_failed`。

    旧实现会拿 `{平台}-{任务ID}-{序号}` 这组**还不存在的**编码去查映射，
    必得「N 项映射缺失」→ 硬拦截 ⇒ 半自动主路径（映射靠回填才产生）永远走不到
    `pending_publish`。修复后校验对象是**已存在**的映射：一个都没有就不算缺失。
    """
    product_id = await _seed_publishable(session, "FIRST")

    tasks, _ = await PublishService.create_tasks(
        session,
        source_product_ids=[product_id],
        platform="taobao",
        shop_id="shop-validate-scope",
        mode=ListingMode.MANUAL.value,
        operator="tester",
        submit_async=False,
    )
    assert tasks, "上架任务未创建"
    task = await PublishService.execute(session, int(tasks[0].id), operator="tester")

    assert task.status != PublishStatus.VALIDATE_FAILED.value, (
        f"首次上架被映射校验拦死（status={task.status}）：{task.error_advice}"
    )
    validate_result = dict(task.validate_result_json or {})
    assert validate_result, "必须留下校验结果（可读回、可追溯）"
    assert validate_result.get("blocking") is False, f"首次上架不应阻断：{validate_result}"
    assert validate_result.get("missing_mappings") == [], (
        f"没有任何映射时不应报「映射缺失」：{validate_result.get('missing_mappings')}"
    )


async def test_existing_invalid_mapping_still_blocks_publish(session: Any) -> None:
    """★ 闸门未被削弱：已存在映射但状态非 valid → **仍然硬拦截**。

    上一条用例放宽的是「用预测编码查缺失」这类恒失败的误报，
    **不是**放宽拦截本身：真有映射且状态无效时必须照旧拦住。
    """
    product_id = await _seed_publishable(session, "INVALID")
    shop_sku_code = "MAP-INVALID-VAL"
    shop_id = "shop-validate-block"

    mapping = SkuMapping(
        platform="taobao",
        shop_id=shop_id,
        shop_item_id="item-invalid",
        shop_sku_code=shop_sku_code,
        source_product_id=product_id,
        source_sku_code_1688="VAL-SKU-INVALID",
        spec_signature="val-sig-INVALID",
        purchase_cost_cents=1200,
        status=MappingStatus.INVALID.value,
    )
    session.add(mapping)
    await session.commit()

    tasks, _ = await PublishService.create_tasks(
        session,
        source_product_ids=[product_id],
        platform="taobao",
        shop_id=shop_id,
        mode=ListingMode.MANUAL.value,
        operator="tester",
        submit_async=False,
    )
    task = await PublishService.execute(session, int(tasks[0].id), operator="tester")

    assert task.status == PublishStatus.VALIDATE_FAILED.value, (
        f"已存在无效映射必须硬拦截，实际 status={task.status}"
    )
    assert "映射校验未通过" in str(task.error_advice or ""), (
        f"必须给出可读的拦截原因，实际：{task.error_advice!r}"
    )
    validate_result = dict(task.validate_result_json or {})
    assert validate_result.get("blocking") is True
    assert validate_result.get("missing_mappings"), "必须点名是哪条映射无效"
