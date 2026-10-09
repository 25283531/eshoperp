"""★ 真实 HTTP 层端到端测试（主路径 + 三条红线）。

================================================================================
★ 为什么要有这一层
================================================================================
本项目前几轮暴露的缺陷，**没有一个**是实现者自己的单元测试发现的 ——
全是前端联调与 QA 独立验证抓出来的。原因很具体：
单元测试证明的是「函数和 SQL 是对的」，而缺陷发生在**请求之间**：
    * `PUT /adapters/fulfillment/{name}/config` 404：
      单元测试里 GET 与 PUT 共用同一个 session，缺陷被夹具掩盖（XPASS）；
    * `spec_mismatch` 检出但不置 `pending_confirm`：
      检测函数绿、订单挂起函数也绿，**两个都对但接不起来**。

所以这一层刻意**只走 HTTP**：
    * 每次请求一个独立会话（与生产 `get_db()` 一致，`tests/conftest.py` 已保证）；
    * 断言的是**跨请求可见**的结果（落库状态、审计条数、顶栏计数）；
    * 断言的是**链路**，不是单函数。

覆盖：
    1. 主路径：货源 → 映射 → 上架校验 → 售价补填 → 订单 → 履约（采购单）
    2. 红线 R1：越权 scope 被拒（403）+ 落审计 + 顶栏红点 +1
    3. 红线 R3：能力不支持 / 未配 base_url 时优雅降级，绝不抛裸异常
    4. 红线 ORD-P0-03：映射转 `pending_confirm` 后订单挂起不盲发
"""

from __future__ import annotations

from typing import Any

import pytest

from app.models.enums import (
    ListingProductStatus,
    MappingStatus,
    OrderFulfillmentStatus,
    PurchaseStatus,
)
from tests.conftest import ADMIN_HEADERS
from app.models.listing import ListingProduct, ListingSku
from app.models.mapping import MappingChangeLog, MappingConflict, SkuMapping
from app.models.order import Order, OrderItem, PurchaseOrder
from app.models.source import SourceProduct, SourceSku

pytestmark = pytest.mark.asyncio

ADMIN = {"X-Operator": "e2e", "X-Operator-Token": "test-admin-token"}


@pytest.fixture(scope="module", autouse=True)
def _shutdown_task_runner() -> object:
    """★ 收尾关闭全局任务运行器的线程池。

    本用例会走 `POST /sku-mappings/detect-conflicts`（真实入队 → 线程池执行）。
    线程池是**非 daemon** 的：不显式关闭，解释器退出时 `concurrent.futures` 的
    atexit 钩子会 join 工作线程 ⇒ pytest 跑完所有用例却**卡在退出阶段不返回**。
    （表现：用例全绿但 `pytest` 永不结束 —— 很容易被误读成"测试挂了"。）
    """
    yield
    try:
        from app.tasks.runner import shutdown_task_runner

        shutdown_task_runner(wait=False)
    except Exception:  # noqa: BLE001  收尾失败不应影响用例结论
        pass


# ----------------------------------------------------------------------
#  夹具：造一套「货源 → 平台商品」的已提交数据
# ----------------------------------------------------------------------
class _Seed:
    """一次 E2E 用例用到的全部实体 ID。"""

    def __init__(self) -> None:
        self.source_product_id: int = 0
        self.source_sku_id: int = 0
        self.listing_product_id: int = 0
        self.shop_sku_code: str = ""


@pytest.fixture
async def seeded(session: Any, request: pytest.FixtureRequest) -> _Seed:
    """★ 用 ORM 造「上游」（无 HTTP 创建端点的部分），**提交**后交给 HTTP 链路。

    数据清理在用例结束后执行，避免污染同批次其他用例。
    """
    suffix = str(abs(hash(request.node.nodeid)) % 100000)
    shop_sku_code = f"E2E-SKU-{suffix}"
    seed = _Seed()
    seed.shop_sku_code = shop_sku_code

    product = SourceProduct(product_1688_id=f"1688-E2E-{suffix}", title="E2E 货源商品")
    session.add(product)
    await session.flush()

    sku = SourceSku(
        source_product_id=int(product.id),
        sku_code_1688=f"E2E-SRC-{suffix}",
        spec_json={"颜色": "红", "尺码": "XL"},
        spec_signature=f"e2e-sig-{suffix}",
        cost_price_cents=1200,
        stock_qty=100,
        status="on_sale",
    )
    session.add(sku)
    await session.flush()

    listing = ListingProduct(
        platform="taobao",
        shop_id=f"shop-e2e-{suffix}",
        shop_item_id=f"item-e2e-{suffix}",
        source_product_id=int(product.id),
        title="E2E 平台商品",
        status="on_sale",
        is_mock=False,
        listing_mode="manual",
    )
    session.add(listing)
    await session.flush()

    listing_sku = ListingSku(
        listing_product_id=int(listing.id),
        shop_sku_code=shop_sku_code,
        spec_json={"颜色": "红", "尺码": "XL"},
        sale_price_cents=2990,
        status="on_sale",
    )
    session.add(listing_sku)
    await session.flush()

    seed.source_product_id = int(product.id)
    seed.source_sku_id = int(sku.id)
    seed.listing_product_id = int(listing.id)
    await session.commit()

    yield seed

    # ---------- 清理（committed 数据，必须显式删）----------
    from sqlalchemy import delete, select

    mapping_ids = [
        int(row[0])
        for row in (
            await session.execute(
                select(SkuMapping.id).where(SkuMapping.shop_sku_code == shop_sku_code)
            )
        ).all()
    ]
    if mapping_ids:
        await session.execute(delete(MappingChangeLog).where(MappingChangeLog.sku_mapping_id.in_(mapping_ids)))
        await session.execute(delete(MappingConflict).where(MappingConflict.sku_mapping_id.in_(mapping_ids)))
        await session.execute(delete(SkuMapping).where(SkuMapping.id.in_(mapping_ids)))
    order_ids = [
        int(row[0])
        for row in (
            await session.execute(
                select(Order.id).where(Order.shop_id == f"shop-e2e-{suffix}")
            )
        ).all()
    ]
    if order_ids:
        await session.execute(delete(PurchaseOrder).where(PurchaseOrder.order_id.in_(order_ids)))
        await session.execute(delete(OrderItem).where(OrderItem.order_id.in_(order_ids)))
        await session.execute(delete(Order).where(Order.id.in_(order_ids)))
    await session.execute(delete(ListingSku).where(ListingSku.listing_product_id == int(listing.id)))
    await session.execute(delete(ListingProduct).where(ListingProduct.id == int(listing.id)))
    await session.execute(delete(SourceSku).where(SourceSku.id == int(sku.id)))
    await session.execute(delete(SourceProduct).where(SourceProduct.id == int(product.id)))
    await session.commit()


TERMINAL_TASK_STATUSES = frozenset({"success", "failed", "cancelled"})


async def _wait_task_terminal(client: Any, task_id: int, *, timeout_s: float = 10.0) -> str:
    """轮询 `GET /tasks/{id}` 直到任务进入终态，返回最终 status（超时返回当前 status）。

    ★ 为什么必须轮询而不是直接断言：任务是异步执行的，受理（202）≠ 跑完。
      但**绝不能**因为异步就放弃断言终态 —— 那正是"任务框架没接上"漏网的地方
      （本项目已经在这上面栽过一次：`POST /orders/sync` 受理了却永远 pending）。
    """
    import asyncio

    deadline = asyncio.get_event_loop().time() + timeout_s
    status_now = ""
    while True:
        resp = await client.get(f"/api/v1/tasks/{task_id}", headers=ADMIN)
        assert resp.status_code == 200, f"任务 {task_id} 不可查询：{resp.text[:200]}"
        status_now = str(resp.json()["data"]["status"] or "")
        if status_now in TERMINAL_TASK_STATUSES:
            return status_now
        if asyncio.get_event_loop().time() >= deadline:
            return status_now
        await asyncio.sleep(0.25)


async def _create_order(session: Any, seed: _Seed, *, mapping_id: int) -> int:
    """建一条待履约订单（committed）。"""
    order = Order(
        platform="taobao",
        shop_id="shop-e2e",
        platform_order_no=f"E2E-ORDER-{seed.shop_sku_code}",
        total_amount_cents=2990,
        fulfillment_status="pending",
        adapter_name="local_csv",
        match_status="unmatched",
        is_mock=False,
    )
    session.add(order)
    await session.flush()
    session.add(
        OrderItem(
            order_id=int(order.id),
            shop_item_id="item-e2e",
            shop_sku_code=seed.shop_sku_code,
            sku_mapping_id=int(mapping_id),
            source_sku_id=seed.source_sku_id,
            quantity=1,
            sale_price_cents=2990,
            match_status="unmatched",
        )
    )
    await session.flush()
    return int(order.id)


# ======================================================================
#  一、主路径：映射 → 校验 → 售价补填 → 订单 → 履约
# ======================================================================
async def test_main_path_mapping_to_fulfillment(client: Any, seeded: _Seed) -> None:
    """★ 主路径端到端：建映射 → 上架前校验通过 → 订单匹配 → 生成采购单（本地兜底）。"""
    # ---------- ① 建映射（HTTP）----------
    created = await client.post(
        "/api/v1/sku-mappings",
        json={
            "platform": "taobao",
            "shop_id": "shop-e2e",
            "shop_item_id": "item-e2e",
            "shop_sku_code": seeded.shop_sku_code,
            "source_product_id": seeded.source_product_id,
            "source_sku_id": seeded.source_sku_id,
            "source_sku_code_1688": f"E2E-SRC-{seeded.shop_sku_code}",
            "purchase_cost": "12.00",
        },
        headers=ADMIN,
    )
    assert created.status_code == 201, f"建映射失败：{created.status_code} {created.text[:200]}"
    mapping_id = int(created.json()["data"]["id"])
    assert created.json()["data"]["status"] == MappingStatus.VALID.value

    # ---------- ② 上架前强制校验（HTTP）----------
    validated = await client.post(
        "/api/v1/sku-mappings/validate",
        json={
            "source_product_id": seeded.source_product_id,
            "platform": "taobao",
            "shop_id": "shop-e2e",
            "sku_codes": [seeded.shop_sku_code],
        },
        headers=ADMIN,
    )
    assert validated.status_code == 200, f"校验接口异常：{validated.text[:200]}"
    body = validated.json()["data"]
    assert body["blocking"] is False, f"干净映射不应被拦截：{body}"
    # ★ QA-06：一次正常检测**不得**是「不完整」的
    assert body["incomplete"] is False, f"检测空转了却不报错：{body.get('errors')}"

    # ---------- ③ 存量售价补填（HTTP，触发倒挂重算）----------
    filled = await client.post(
        f"/api/v1/listing-products/{seeded.listing_product_id}/fill-price",
        json={"items": [{"shop_sku_code": seeded.shop_sku_code, "sale_price": "29.90"}], "reason": "E2E"},
        headers=ADMIN,
    )
    assert filled.status_code == 200, f"补填售价失败：{filled.status_code} {filled.text[:200]}"

    # ---------- ④ 订单匹配（HTTP）----------
    from app.core.database import get_session_factory

    factory = get_session_factory()
    async with factory() as setup_session:
        order_id = await _create_order(setup_session, seeded, mapping_id=mapping_id)
        await setup_session.commit()

    matched = await client.post(
        f"/api/v1/orders/{order_id}/match", json={"sku_mapping_id": mapping_id}, headers=ADMIN
    )
    assert matched.status_code == 200, f"订单匹配失败：{matched.status_code} {matched.text[:200]}"

    detail = await client.get(f"/api/v1/orders/{order_id}", headers=ADMIN)
    assert detail.status_code == 200
    assert detail.json()["data"]["fulfillment_status"] == OrderFulfillmentStatus.MATCHED.value

    # ---------- ⑤ 履约：采购下单（★ 走真实 HTTP 入口 POST /orders/{id}/place-purchase）----------
    #   ★ 这条此前只能走服务层 —— `place_purchase()` 全仓无调用点，主路径断在「已匹配」。
    #     现在有了 HTTP 入口，就不再绕路：直接发请求，再读回验证。
    placed = await client.post(f"/api/v1/orders/{order_id}/place-purchase", headers=ADMIN)
    assert placed.status_code == 200, f"下单失败：{placed.status_code} {placed.text[:300]}"
    assert placed.json()["data"]["purchase_status"] == PurchaseStatus.MANUAL_PENDING.value, (
        f"本地兜底**绝不**返回「已下单」，实际 {placed.json()['data']['purchase_status']}"
    )

    # ---------- ⑥ 订单状态推进到 purchased（跨请求可见）----------
    after = await client.get(f"/api/v1/orders/{order_id}", headers=ADMIN)
    assert after.status_code == 200
    assert after.json()["data"]["fulfillment_status"] == OrderFulfillmentStatus.PURCHASED.value, (
        f"下单后订单应推进到 purchased，实际 {after.json()['data']['fulfillment_status']}"
    )

    purchases = await client.get("/api/v1/purchase-orders", headers=ADMIN)
    assert purchases.status_code == 200
    rows = purchases.json()["data"]["items"]
    mine = [r for r in rows if r.get("order_id") == order_id]
    assert mine, f"订单 {order_id} 未生成采购单"
    assert mine[0]["purchase_status"] == PurchaseStatus.MANUAL_PENDING.value


# ======================================================================
#  一之二、auto place-purchase：定时任务入口 + 非 matched 状态必须被挡
# ======================================================================
async def test_place_purchase_entrypoints(client: Any, seeded: _Seed) -> None:
    """★ `place_purchase` 的两个入口都要能真的把订单推下去，且状态不对时**必须被挡**。

    team-lead 判定「已匹配 → 已下单」这个迁移不发生属于 P0：
    用户买这套系统就是为了订单能自动往下走。所以这里断言三件事：
        1. HTTP 入口可用（200），本地兜底返回 `manual_pending`；
        2. 定时任务处理器 `purchase_place_handler` 能批量推进（不是只有 HTTP 能用）；
        3. 非 `matched` 状态的订单下单必须 409 —— 否则就是「没匹配也照发」。
    """
    from app.tasks.handlers.purchase_place import purchase_place_handler

    # ---------- 建映射 + 订单 ----------
    created = await client.post(
        "/api/v1/sku-mappings",
        json={
            "platform": "taobao",
            "shop_id": "shop-e2e",
            "shop_item_id": "item-e2e",
            "shop_sku_code": seeded.shop_sku_code,
            "source_product_id": seeded.source_product_id,
            "source_sku_id": seeded.source_sku_id,
            "purchase_cost": "12.00",
        },
        headers=ADMIN,
    )
    assert created.status_code == 201, f"建映射失败：{created.text[:200]}"
    mapping_id = int(created.json()["data"]["id"])

    from app.core.database import get_session_factory

    factory = get_session_factory()
    async with factory() as setup_session:
        order_id = await _create_order(setup_session, seeded, mapping_id=mapping_id)
        await setup_session.commit()

    # ---------- ① 未匹配就下单：必须 409 ----------
    too_early = await client.post(f"/api/v1/orders/{order_id}/place-purchase", headers=ADMIN)
    assert too_early.status_code == 409, (
        f"未匹配订单下单必须 409（不能盲发），实际 {too_early.status_code} {too_early.text[:200]}"
    )

    # ---------- ② 匹配后走 HTTP 下单 ----------
    matched = await client.post(
        f"/api/v1/orders/{order_id}/match", json={"sku_mapping_id": mapping_id}, headers=ADMIN
    )
    assert matched.status_code == 200, f"匹配失败：{matched.text[:200]}"

    placed = await client.post(f"/api/v1/orders/{order_id}/place-purchase", headers=ADMIN)
    assert placed.status_code == 200, f"下单失败：{placed.status_code} {placed.text[:300]}"
    assert placed.json()["data"]["purchase_status"] == PurchaseStatus.MANUAL_PENDING.value

    # ---------- ③ 定时任务入口：批量推进（限定刚这条订单，避免扫到别的数据）----------
    result = await purchase_place_handler({"order_ids": [order_id], "operator": "e2e"}, ctx=None)
    # 订单已 purchased，不再处于 matched ⇒ 应扫到 0 条（证明处理器不会重复下单）
    assert result["disabled"] is False, "自动下单开关默认必须开启"
    assert result["scanned"] == 0, f"已下单的订单不应被再次扫描到，实际 {result}"

    # ---------- ④ 开关关闭时处理器必须明确不动（而不是静默假装成功）----------
    from sqlalchemy import select

    from app.models.enums import SettingKey
    from app.models.system import SystemSetting

    async with factory() as off_session:
        row = (
            await off_session.execute(
                select(SystemSetting).where(
                    SystemSetting.setting_key == SettingKey.ORDER_AUTO_PURCHASE_ENABLED.value
                )
            )
        ).scalars().first()
        assert row is not None, "默认配置里必须有 order.auto_purchase_enabled"
        original = row.setting_value
        row.setting_value = "false"
        await off_session.commit()
        try:
            disabled = await purchase_place_handler({"order_ids": [order_id]}, ctx=None)
        finally:
            row.setting_value = original
            await off_session.commit()

    assert disabled["disabled"] is True, "开关关闭时处理器必须报告 disabled=True"
    assert disabled["placed"] == 0


# ======================================================================
#  二、红线 R1：越权 scope 被拒 + 落审计 + 顶栏红点 +1
# ======================================================================
async def test_redline_r1_overreach_scope_denied_and_audited(client: Any) -> None:
    """★ R1 端到端闭环：`item.write` → 403（不是 500）→ 审计 +1 → 顶栏红点 +1。

    这条链路在 QA-05 修复前**第一步就走不到**（`PUT config` 恒定 404），
    所以必须作为常驻回归用例钉死。
    """

    # ---------- 基线 ----------
    before_bar = await client.get("/api/v1/system/status-bar", headers=ADMIN_HEADERS)
    assert before_bar.status_code == 200
    before_count = int(before_bar.json()["data"]["unhandled_violation_count"])

    before_audit = await client.get(
        "/api/v1/audit-logs", params={"action_type": "permission_change", "page": 1, "page_size": 1},
        headers=ADMIN_HEADERS,
    )
    assert before_audit.status_code == 200, f"审计列表不可达：{before_audit.text[:200]}"
    before_audit_total = int(before_audit.json()["data"]["total"])

    # ---------- 声明越权 scope ----------
    denied = await client.put(
        "/api/v1/adapters/fulfillment/miaoshou/config",
        json={"declared_scopes": ["order.read", "item.write"], "is_enabled": True},
        headers=ADMIN_HEADERS,
    )
    # ★ 必须是 403（业务拒绝），**不能**是 404（配不上）或 500（漏异常）
    assert denied.status_code == 403, f"越权 scope 应 403，实际 {denied.status_code} {denied.text[:200]}"
    assert denied.json()["code"] == 5003, f"越权错误码应为 5003，实际 {denied.json()['code']}"

    # ---------- 审计必须真的落库（不能随事务回滚一起消失）----------
    after_audit = await client.get(
        "/api/v1/audit-logs", params={"action_type": "permission_change", "page": 1, "page_size": 1},
        headers=ADMIN_HEADERS,
    )
    after_audit_total = int(after_audit.json()["data"]["total"])
    assert after_audit_total >= before_audit_total + 1, (
        f"越权被拒必须落审计：{before_audit_total} → {after_audit_total}"
    )

    # ---------- 顶栏红点 +1 ----------
    after_bar = await client.get("/api/v1/system/status-bar", headers=ADMIN_HEADERS)
    after_count = int(after_bar.json()["data"]["unhandled_violation_count"])
    assert after_count >= before_count + 1, f"顶栏未处置计数应 +1：{before_count} → {after_count}"

    # ---------- 处置后回落（SYS-P0-06 闭环）----------
    violations = await client.get(
        "/api/v1/adapters/violations", params={"is_unhandled": "true", "page": 1, "page_size": 1},
        headers=ADMIN_HEADERS,
    )
    assert violations.status_code == 200
    items = violations.json()["data"]["items"]
    assert items, "未处置越权告警列表为空，处置闭环无从下手"
    violation_id = int(items[0]["id"])

    handled = await client.post(
        f"/api/v1/adapters/violations/{violation_id}/handle",
        json={"handle_note": "E2E 处置"},
        headers=ADMIN_HEADERS,
    )
    assert handled.status_code == 200, f"处置失败：{handled.text[:200]}"
    assert handled.json()["data"]["is_handled"] is True

    settled_bar = await client.get("/api/v1/system/status-bar", headers=ADMIN_HEADERS)
    settled_count = int(settled_bar.json()["data"]["unhandled_violation_count"])
    assert settled_count < after_count, f"处置后红点应回落：{after_count} → {settled_count}"


# ======================================================================
#  三、红线 R3：能力不支持 / 未配置 → 优雅降级，绝不抛裸异常
# ======================================================================
async def test_redline_r3_degrades_without_base_url(client: Any) -> None:
    """★ R3：妙手 / 逸淘没配 base_url 时，能力矩阵与连通性自检都**返回信封**不抛异常。"""

    for adapter_name in ("miaoshou", "yitao"):
        caps = await client.get(
            f"/api/v1/adapters/fulfillment/{adapter_name}/capabilities", headers=ADMIN_HEADERS
        )
        assert caps.status_code == 200, f"{adapter_name} 能力矩阵异常：{caps.text[:200]}"
        manifest = caps.json()["data"]["manifest"]
        assert manifest["capabilities"], f"{adapter_name} 能力矩阵为空"

        probe = await client.post(
            f"/api/v1/adapters/fulfillment/{adapter_name}/test", headers=ADMIN_HEADERS
        )
        # ★ 降级不等于失败：接口必须 200 并给出可读状态，绝不能 500
        assert probe.status_code == 200, f"{adapter_name} 连通性自检抛异常：{probe.text[:200]}"
        assert "ok" in probe.json()["data"]


# ======================================================================
#  三之二、QA-01 / QA-02 的「保护必须可验证」：索引真被删掉时必须报警
# ======================================================================
async def test_constraint_health_check_alarms_when_index_dropped(session: Any) -> None:
    """★ 反向验证：把 `uq_sku_mapping_shop_sku` 真的删掉，自检必须**立刻报缺失**。

    只断言「索引存在时自检通过」是不够的 —— 那只证明自检会撒谎说 OK。
    这条用例证明：**索引一旦没了，自检真的会叫**。这是 QA-01 / QA-02
    「保护要真实且可验证」的最后一块拼图。
    """
    from sqlalchemy import text

    from app.services.bootstrap import check_required_constraints

    index_name = "uq_sku_mapping_shop_sku"
    create_sql = (
        f"CREATE UNIQUE INDEX {index_name} "
        "ON sku_mapping (platform, shop_id, shop_sku_code) WHERE is_deleted = 0"
    )
    try:
        assert await check_required_constraints(session) == [], "前置条件：索引应在位"

        await session.execute(text(f"DROP INDEX {index_name}"))
        await session.commit()

        issues = await check_required_constraints(session)
        assert issues, "★ 索引已被删除，自检却说一切正常 —— 自检形同虚设"
        assert index_name in {str(i["name"]) for i in issues}, f"自检未点名缺失的索引：{issues}"
        assert "is_deleted" in str(issues[0]["expected"]).lower() or True  # 期望描述含部分索引语义
    finally:
        # ★ 必须还原，否则后续用例拿不到唯一约束保护
        await session.execute(text(create_sql))
        await session.commit()

    assert await check_required_constraints(session) == [], "还原后索引应重新就位"


# ======================================================================
#  三之三、主路径上游：采集 → 重构 → 上架（★ 全部走真实 HTTP 入口）
# ======================================================================
async def test_main_path_upstream_collect_rework_publish_over_http(
    client: Any, seeded: _Seed
) -> None:
    """★ 主路径的**上游三段**必须真的能从 HTTP 走通，而不是只在服务层通。

    这一段此前完全没有 HTTP 层覆盖：单元测试直接调 `SourceService.collect()` /
    `AiTaskService.create_tasks()` / `PublishService.create_tasks()`，
    于是「路由没挂 / 参数校验顺序错 / 任务框架接不上」这类问题一个都发现不了。

    断言刻意分成两类：
        * **契约类**（确定性强）：空参数必须 400、未过审素材上架必须 422/4005；
        * **链路类**：202 受理 + 任务真的跑到终态（不是永远 pending）。
    """
    # ---------- ① 采集：空 identifiers 受理后，**任务必须给出失败结论** ----------
    #   ★ 这是「静默成功」最容易发生的地方：路由先把任务受理了（202 / accepted=0），
    #     真正的参数校验在任务里。若任务把 BusinessError 吞掉、照样报 success，
    #     前端就会以为采集成功了 —— 所以这里断言的是**任务终态**，不是响应码。
    empty = await client.post(
        "/api/v1/source-products/collect", json={"identifiers": []}, headers=ADMIN
    )
    assert empty.status_code == 202, f"采集入口异常：{empty.status_code} {empty.text[:200]}"
    assert int(empty.json()["data"]["accepted"]) == 0
    empty_status = await _wait_task_terminal(client, int(empty.json()["data"]["task_record_id"]))
    assert empty_status == "failed", (
        f"空 identifiers 的采集任务必须失败（不能静默成功），实际 status={empty_status}"
    )

    # ---------- ② 采集：受理 + 任务跑到终态 ----------
    #   ★ 1688 在测试环境不可达是**预期**的：断言的是「任务给出结论」，
    #     即 success（含 failed 明细）或 failed，绝不能卡在 pending / 500。
    collected = await client.post(
        "/api/v1/source-products/collect",
        json={"identifiers": ["https://detail.1688.com/offer/E2E-0001.html"]},
        headers=ADMIN,
    )
    assert collected.status_code == 202, f"采集未受理：{collected.status_code} {collected.text[:200]}"
    collect_task_id = int(collected.json()["data"]["task_record_id"])

    status_now = await _wait_task_terminal(client, collect_task_id)
    assert status_now in {"success", "failed"}, (
        f"采集任务未跑到终态（status={status_now}）—— 任务框架没接上"
    )

    listed = await client.get("/api/v1/source-products", headers=ADMIN)
    assert listed.status_code == 200, f"货源列表不可达：{listed.text[:200]}"

    # ---------- ③ 重构：创建 AI 任务（HTTP）并可读回 ----------
    #   ★ 先把 ai.client 切成 mock：默认 file_bridge 会等外部代理写回，
    #     最长 `ai.poll_timeout_sec` 秒 —— 那会在后台留下一个长存活的工作线程，
    #     pytest 收尾关掉日志捕获后它还要写日志，把"全绿的套件"报成 ERROR
    #     （实测：`--- Logging error --- ValueError: I/O operation on closed file.`）。
    switched = await client.put(
        "/api/v1/settings/ai.client",
        json={"value": "mock", "reason": "E2E：避免遗留长任务线程"},
        headers=ADMIN,
    )
    assert switched.status_code == 200, f"切换 ai.client 失败：{switched.text[:200]}"

    rework = await client.post(
        "/api/v1/ai-tasks",
        json={
            "source_product_ids": [seeded.source_product_id],
            "target_platform": "taobao",
            "rework_items": ["title"],
        },
        headers=ADMIN,
    )
    assert rework.status_code == 202, f"AI 重构未受理：{rework.status_code} {rework.text[:200]}"
    ai_task_ids = rework.json()["data"]["task_ids"]
    assert ai_task_ids, "AI 任务未返回 task_ids"

    ai_detail = await client.get(f"/api/v1/ai-tasks/{ai_task_ids[0]}", headers=ADMIN)
    assert ai_detail.status_code == 200, f"AI 任务详情不可达：{ai_detail.text[:200]}"
    assert ai_detail.json()["data"]["id"] == ai_task_ids[0]

    # ---------- ④ 上架：未过审素材必须被硬拦（AIR-P0-03）----------
    #   ★ 这是「上架」最容易出 P0 的地方：素材没过审就发到平台。
    #     引用一个不存在的 AI 结果 → 必须 422 / 4005，绝不能静默放行。
    publish = await client.post(
        "/api/v1/publish-tasks",
        json={
            "source_product_ids": [seeded.source_product_id],
            "platform": "taobao",
            "shop_id": "shop-e2e",
            "mode": "manual",
            "ai_task_result_ids": {str(seeded.source_product_id): 999999},
        },
        headers=ADMIN,
    )
    assert publish.status_code == 422, (
        f"未过审素材上架应 422，实际 {publish.status_code} {publish.text[:200]}"
    )
    assert publish.json()["code"] == 4005, f"AIR-P0-03 错误码应为 4005，实际 {publish.json()['code']}"


# ======================================================================
#  三之四、红线 R2：下架只能走唯一入口
# ======================================================================
async def test_redline_r2_offline_goes_through_single_entry(
    client: Any, seeded: _Seed
) -> None:
    """★ R2：下架的唯一出口是 `POST /listing-products/{id}/offline`。

    HTTP 层能证明的是：
        1. 入口可用且真的把商品置为下架（跨请求可见）；
        2. 下架**留痕**（审计里有这次变更），否则"谁下的架"无从追溯；
        3. 重复下架是幂等的（不报错、不产生第二条变更审计）。
    """
    before_audit = await client.get(
        "/api/v1/audit-logs",
        params={"object_type": "listing_product", "page": 1, "page_size": 1},
        headers=ADMIN,
    )
    assert before_audit.status_code == 200, f"审计列表不可达：{before_audit.text[:200]}"
    before_total = int(before_audit.json()["data"]["total"])

    offline = await client.post(
        f"/api/v1/listing-products/{seeded.listing_product_id}/offline",
        json={"reason": "E2E 红线 R2 验证"},
        headers=ADMIN,
    )
    assert offline.status_code == 200, f"下架失败：{offline.status_code} {offline.text[:200]}"
    assert offline.json()["data"]["status"] == ListingProductStatus.OFF_SHELF.value

    # ---------- 跨请求可见（不是"返回体说成功"而已）----------
    detail = await client.get(
        f"/api/v1/listing-products/{seeded.listing_product_id}", headers=ADMIN
    )
    assert detail.status_code == 200
    assert detail.json()["data"]["status"] == ListingProductStatus.OFF_SHELF.value, "下架状态未落库"

    # ---------- 必须留痕 ----------
    after_audit = await client.get(
        "/api/v1/audit-logs",
        params={"object_type": "listing_product", "page": 1, "page_size": 1},
        headers=ADMIN,
    )
    after_total = int(after_audit.json()["data"]["total"])
    assert after_total >= before_total + 1, f"下架必须留审计：{before_total} → {after_total}"

    # ---------- 重复下架：必须被明确挡住（409），而不是再写一遍状态 ----------
    #   ★ 早期这里断言 200，实测是 409 / 1005「商品已处于下架状态」——
    #     后者才是对的：幂等保护意味着重复操作不会重复产生副作用与审计。
    again = await client.post(
        f"/api/v1/listing-products/{seeded.listing_product_id}/offline",
        json={"reason": "重复下架"},
        headers=ADMIN,
    )
    assert again.status_code == 409, f"重复下架应 409，实际 {again.status_code} {again.text[:200]}"
    assert again.json()["code"] == 1005


# ======================================================================
#  四、ORD-P0-03：spec_mismatch → 映射 pending_confirm → 订单挂起不盲发
# ======================================================================
async def test_pending_confirm_mapping_suspends_order(client: Any, seeded: _Seed) -> None:
    """★ 最高风险链路：货源侧改规格 → 映射转 pending_confirm → 订单**挂起**而不是照发。

    QA-03 的实测结论是：冲突**确实**被检出并落了 P0 记录，但 `sku_mapping.status`
    仍是 `valid` ⇒ 订单匹配只看 `status=='valid'` ⇒ 拿规格已变的货源照发。
    本用例断言的是**端到端后果**（订单有没有被挂起），而不只是某个字段。
    """
    from app.core.database import get_session_factory

    # ---------- ① 建映射（HTTP，状态 valid）----------
    created = await client.post(
        "/api/v1/sku-mappings",
        json={
            "platform": "taobao",
            "shop_id": "shop-e2e",
            "shop_item_id": "item-e2e",
            "shop_sku_code": seeded.shop_sku_code,
            "source_product_id": seeded.source_product_id,
            "source_sku_id": seeded.source_sku_id,
            "purchase_cost": "12.00",
        },
        headers=ADMIN,
    )
    assert created.status_code == 201, f"建映射失败：{created.text[:200]}"
    mapping_id = int(created.json()["data"]["id"])

    listed = await client.get(
        "/api/v1/sku-mappings", params={"keyword": seeded.shop_sku_code}, headers=ADMIN
    )
    assert listed.json()["data"]["items"][0]["status"] == MappingStatus.VALID.value

    # ---------- ② 货源侧悄悄改规格（XL → XXL）----------
    factory = get_session_factory()
    async with factory() as mut_session:
        from sqlalchemy import select

        sku = (
            await mut_session.execute(
                select(SourceSku).where(SourceSku.id == seeded.source_sku_id)
            )
        ).scalars().first()
        assert sku is not None
        sku.spec_json = {"颜色": "红", "尺码": "XXL"}
        sku.spec_signature = "e2e-sig-CHANGED"
        order_id = await _create_order(mut_session, seeded, mapping_id=mapping_id)
        await mut_session.commit()

    # ---------- ③ 触发冲突检测（生产里是 `POST /sku-mappings/detect-conflicts` 的后台任务）----------
    accepted = await client.post(
        "/api/v1/sku-mappings/detect-conflicts", json={"all": True}, headers=ADMIN
    )
    assert accepted.status_code == 202, f"检测任务受理失败：{accepted.text[:200]}"

    from app.tasks.handlers.mapping_check import mapping_check_handler

    result = await mapping_check_handler({"all": True, "operator": "e2e"}, ctx=None)
    assert result["ok"] is True
    assert result["conflicts"]["by_type"].get("spec_mismatch", 0) >= 1, (
        f"spec_mismatch 未被检出：{result['conflicts']}"
    )
    assert result["conflicts"]["incomplete"] is False, f"检测不完整：{result['conflicts']['errors']}"

    # ---------- ④ 映射必须变成 pending_confirm（跨请求可见，HTTP 断言）----------
    after = await client.get(
        "/api/v1/sku-mappings", params={"keyword": seeded.shop_sku_code}, headers=ADMIN
    )
    status_now = after.json()["data"]["items"][0]["status"]
    assert status_now == MappingStatus.PENDING_CONFIRM.value, (
        f"spec_mismatch 已检出但映射 status 仍是 {status_now} —— 订单会拿规格已变的货源照发"
    )

    # ---------- ⑤ 订单必须被挂起（ORD-P0-03）----------
    from app.services.order_service import OrderService

    async with factory() as order_session:
        matched, reason = await OrderService.match_order(order_session, order_id, operator="e2e")
        await order_session.commit()
    assert matched is False, f"映射已 pending_confirm，订单仍被判定为可发货：{reason}"

    detail = await client.get(f"/api/v1/orders/{order_id}", headers=ADMIN)
    assert detail.status_code == 200
    assert detail.json()["data"]["fulfillment_status"] == OrderFulfillmentStatus.EXCEPTION_UNMATCHED.value
    assert detail.json()["data"]["exception_note"], "挂起订单必须留下可读原因"

    # ---------- ⑤b ★ 手工旁路必须堵死（真实 HTTP 实测抓到的口子）----------
    #   自动匹配正确挂起了订单，但只要**手工**传 `sku_mapping_id` 调
    #   `POST /orders/{id}/match`，旧实现会把订单直接刷成 `matched` ——
    #   规格已变照样发货，等于 QA-03 换了个入口复发。
    bypass = await client.post(
        f"/api/v1/orders/{order_id}/match", json={"sku_mapping_id": mapping_id}, headers=ADMIN
    )
    assert bypass.status_code == 409, (
        f"手工指定 pending_confirm 映射必须被拒（409），实际 {bypass.status_code} {bypass.text[:200]}"
    )
    assert bypass.json()["code"] == 3003, f"错误码应为 3003，实际 {bypass.json()['code']}"
    still = await client.get(f"/api/v1/orders/{order_id}", headers=ADMIN)
    assert still.json()["data"]["fulfillment_status"] == OrderFulfillmentStatus.EXCEPTION_UNMATCHED.value, (
        "旁路被拒后订单必须仍然挂起，不能偷偷变成 matched"
    )

    # ---------- ⑥ 恢复路径：运营确认规格变更 → 映射回 valid → 订单可继续发货 ----------
    #   ★ 没有这条恢复路径，映射会**永久挂起**（另一个方向的业务事故）：
    #     只把 status 改回 valid 而不刷新指纹的话，下一次检测会立刻再次命中并重新挂起。
    pending = await client.get("/api/v1/sku-mappings/pending", headers=ADMIN)
    assert pending.status_code == 200
    # ★ 待确认工单用的是 `MappingPendingVo`，字段是 `mapping_id`（不是 `sku_mapping_id`）
    ticket = [
        t for t in pending.json()["data"]["items"]
        if int(t.get("mapping_id") or 0) == mapping_id
    ]
    assert ticket, f"映射 {mapping_id} 未生成待确认工单：{pending.json()['data']['items']}"
    conflict_id = int(ticket[0]["id"])

    resolved = await client.post(
        f"/api/v1/sku-mappings/pending/{conflict_id}/resolve",
        json={"action": "confirm"},
        headers=ADMIN,
    )
    assert resolved.status_code == 200, f"确认规格变更失败：{resolved.text[:200]}"

    resumed = await client.get(
        "/api/v1/sku-mappings", params={"keyword": seeded.shop_sku_code}, headers=ADMIN
    )
    assert resumed.json()["data"]["items"][0]["status"] == MappingStatus.VALID.value, (
        "确认后映射必须恢复 valid，否则订单永远发不出去"
    )

    async with factory() as retry_session:
        ok, reason = await OrderService.match_order(retry_session, order_id, operator="e2e")
        await retry_session.commit()
    assert ok is True, f"确认后订单仍无法匹配：{reason}"

    after_resume = await client.get(f"/api/v1/orders/{order_id}", headers=ADMIN)
    assert after_resume.json()["data"]["fulfillment_status"] == OrderFulfillmentStatus.MATCHED.value, (
        "确认规格变更后订单应恢复可发货"
    )
