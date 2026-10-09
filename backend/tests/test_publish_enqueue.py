"""★ 上架任务入队 —— **批量入口**的回归护栏。

================================================================================
★ 为什么只有这两个用例（而不是把单条入口的用例也抄一遍）
================================================================================
同一个缺陷（入队失败被 `except Exception: return None` 吞掉，接口照样返回 202
+ `task_record_ids: []`）的**单条入口**护栏已经在
`tests/test_publish_enqueue_visibility.py` 里了：
    · `test_publish_task_really_enqueues_and_advances`
    · `test_publish_enqueue_failure_returns_500_not_202`
重复实现一遍同样的断言只会让"改一次要维护两处"，没有任何额外保护。

本文件补的是**那两份没覆盖的两块**：

1. **`/publish-tasks/batch` 是另一条独立路由**。它和单条入口共用同一个服务方法，
   所以今天的结论对它也成立；但路由是各自声明的，将来有人在批量路由上"顺手
   改回 swallow"或漏改某一处，单条入口的护栏**一条都不会红**。故单独锁住。

2. **"部分入队失败"这个分支**。上面那份只验证了"全部失败"，而批量场景下
   更阴险的是 **1 条成功、1 条失败**：响应若仍按 202 返回，运营看到的是
   "2 个任务都受理了"，实际只有 1 个在跑 —— 这是静默失效最难被发现的一种形态。

================================================================================
★ 断言刻意不写死错误码
================================================================================
不锁 `1098` / `4008` 这类具体码值：错误码是实现细节，锁死它只会让"换个码"
这种无害改动把用例打红。真正要锁的是两件事：
    · **不是 2xx**（尤其不是 202）；
    · **响应 / 任务行上带可读原因**（前端能显示、运营能看懂）。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.tasks.runner import LocalTaskRunner
from tests.conftest import ADMIN_HEADERS

API = "/api/v1"


def uniq(prefix: str = "PUB") -> str:
    """本次运行唯一的商品编码（用例之间互不污染）。"""
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


async def create_source_product(client: Any, prefix: str = "PUB") -> int:
    """通过**真实 HTTP** 建一个手工货源商品，返回其 id。"""
    payload = {
        "title": "批量入队回归测试用商品",
        "product_code": uniq(prefix),
        "category_path": "T恤",
        "cost_price": "19.90",
        "skus": [
            {
                "spec_name": "颜色",
                "spec_value": "白色",
                "cost_price": "19.90",
                "sale_price": "39.90",
                "stock_qty": 10,
            }
        ],
    }
    response = await client.post(
        f"{API}/source-products/manual", json=payload, headers=ADMIN_HEADERS
    )
    assert response.status_code == 201, f"货源录入失败：{response.status_code} {response.text[:300]}"
    return int(response.json()["data"]["id"])


# ======================================================================
#  ① 批量入口：202 ⇒ 每个任务都真的有 task_record
# ======================================================================
async def test_batch_publish_enqueue_count_matches(client: Any) -> None:
    """★ `/publish-tasks/batch` 与单条入口共用服务方法，但路由独立，故单独锁不变式。

    不变式：只要返回 202，`len(task_record_ids) == len(task_ids)`。
    修复前这里会是 `task_record_ids: []`（任务"创建成功"却永远不会执行）。
    """
    first = await create_source_product(client, "BAT1")
    second = await create_source_product(client, "BAT2")

    response = await client.post(
        f"{API}/publish-tasks/batch",
        json={
            "source_product_ids": [first, second],
            "platform": "taobao",
            "shop_id": "shop-batch-enqueue",
            "mode": "manual",
        },
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 202, f"批量上架未受理：{response.status_code} {response.text[:300]}"

    data = response.json()["data"]
    task_ids = [int(i) for i in data.get("task_ids") or []]
    record_ids = [int(i) for i in data.get("task_record_ids") or []]
    assert len(task_ids) == 2, f"批量创建应返回 2 个任务，实际 {task_ids}"
    assert len(record_ids) == len(task_ids), (
        f"批量上架存在「创建成功但没有 task_record」的任务（静默失效）："
        f"task_ids={task_ids}, task_record_ids={record_ids}"
    )


# ======================================================================
#  ② 部分入队失败：绝不允许"2 个都受理了"的假象
# ======================================================================
async def test_batch_publish_partial_enqueue_failure_is_visible(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 批量里 **1 条成功、1 条失败** 时，响应必须失败且原因可见。

    这是最难被发现的形态：运营看到"已创建 2 个上架任务"，实际只有 1 个在跑，
    另一个永远停在 `pending_precheck` 且没有任何提示。
    """
    first = await create_source_product(client, "PRT1")
    second = await create_source_product(client, "PRT2")

    original_submit = LocalTaskRunner.submit
    calls = {"n": 0}

    async def _fail_from_second(self: Any, *args: Any, **kwargs: Any) -> int:
        """第 1 条走真实入队（成功），从第 2 条开始模拟写 `task_record` 失败。"""
        calls["n"] += 1
        if calls["n"] == 1:
            return int(await original_submit(self, *args, **kwargs))
        raise RuntimeError("模拟：写 task_record 失败（database is locked）")

    monkeypatch.setattr(LocalTaskRunner, "submit", _fail_from_second)

    response = await client.post(
        f"{API}/publish-tasks/batch",
        json={
            "source_product_ids": [first, second],
            "platform": "taobao",
            "shop_id": "shop-batch-partial",
            "mode": "manual",
        },
        headers=ADMIN_HEADERS,
    )

    # ★★ 硬断言：只要有任何一条没入队，就绝不能返回 202 / 任何 2xx ★★
    assert response.status_code != 202, "部分入队失败却返回 202 —— 这是假装成功的静默失效"
    assert not 200 <= response.status_code < 300, (
        f"部分入队失败必须返回错误状态码，实际 {response.status_code} {response.text[:300]}"
    )
    assert calls["n"] == 2, f"两条任务都应尝试入队，实际尝试 {calls['n']} 次"

    body = response.json()
    assert int(body.get("code") or 0) != 0, "失败响应的业务 code 不应为 0"
    assert str(body.get("message") or "").strip(), "失败响应必须带可读的中文原因"

    # ★ 不允许留下「既没入队、又没有任何提示」的死任务：
    #   实现可以整批撤销（0 行），也可以保留行但必须写明原因；
    #   唯一不接受的是"停在 pending_precheck 且什么都不说"。
    listed = await client.get(
        f"{API}/publish-tasks",
        params={"shop_id": "shop-batch-partial", "page": 1, "page_size": 20},
        headers=ADMIN_HEADERS,
    )
    assert listed.status_code == 200, f"上架任务列表不可达：{listed.text[:300]}"
    for item in listed.json()["data"]["items"]:
        assert item.get("task_record_id") or str(item.get("error_advice") or "").strip(), (
            f"上架任务 {item.get('id')} 既没有 task_record_id、也没有 error_advice —— "
            f"运营会一直等一个永远不会执行的任务（静默失效）"
        )


if __name__ == "__main__":  # pragma: no cover  便于单独调试
    pytest.main([__file__, "-v"])
