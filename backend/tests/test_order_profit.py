"""订单利润快照语义测试（★ 附录 A 第 18 条）。

核心断言：
    **历史订单利润禁止 join 回 `sku_mapping` / `source_sku` 取成本**
    —— 只能读 `order_item.purchase_cost_cents` 快照。
    货源涨价后，历史订单利润**必须仍按下单时点的成本计算**（不可回溯改写）。
"""

from __future__ import annotations

from sqlalchemy import select

from app.models.enums import MappingStatus
from app.models.mapping import SkuMapping
from app.models.order import Order, OrderItem
from app.models.source import SourceProduct, SourceSku
from app.schemas.order import OrderDetailVo
from app.services.order_service import OrderService

COST_AT_ORDER_TIME = 1200  # 下单时点成本 12.00 元
SALE_PRICE = 2990  # 售价 29.90 元
QUANTITY = 3


async def _seed_order(session: object) -> tuple[int, int, int]:
    """建货源 → 映射 → 订单（成本快照固化），返回 `(order_id, sku_id, mapping_id)`。"""
    product = SourceProduct(product_1688_id="1688-PROFIT-001", title="利润测试商品")
    session.add(product)
    await session.flush()
    sku = SourceSku(
        source_product_id=int(product.id),
        sku_code_1688="SKU-PROFIT",
        spec_json={"颜色": "红"},
        spec_signature="sig-profit",
        cost_price_cents=COST_AT_ORDER_TIME,
        stock_qty=50,
        status="on_sale",
    )
    session.add(sku)
    await session.flush()

    mapping = SkuMapping(
        platform="taobao",
        shop_id="shop-taobao",
        shop_item_id="item-profit",
        shop_sku_code="SKU-PROFIT",
        source_product_id=int(product.id),
        source_sku_id=int(sku.id),
        source_sku_code_1688="SKU-PROFIT",
        purchase_cost_cents=COST_AT_ORDER_TIME,
        status=MappingStatus.VALID.value,
        cost_source="auto",
        source="manual",
    )
    session.add(mapping)
    await session.flush()

    order = Order(
        platform="taobao",
        shop_id="shop-taobao",
        platform_order_no="T-PROFIT-001",
        total_amount_cents=SALE_PRICE * QUANTITY,
        fulfillment_status="pending_match",
        adapter_name="local_csv",
        is_mock=False,
    )
    session.add(order)
    await session.flush()

    session.add(
        OrderItem(
            order_id=int(order.id),
            shop_item_id="item-profit",
            shop_sku_code="SKU-PROFIT",
            sku_mapping_id=int(mapping.id),
            source_sku_id=int(sku.id),
            quantity=QUANTITY,
            purchase_cost_cents=COST_AT_ORDER_TIME,  # ★ 快照：下单时点成本
            sale_price_cents=SALE_PRICE,
            match_status="matched",
        )
    )
    await session.flush()
    return int(order.id), int(sku.id), int(mapping.id)


async def test_profit_uses_snapshot(session: object) -> None:
    """利润 = (售价 - 快照成本) × 数量。"""
    order_id, _sku_id, _mapping_id = await _seed_order(session)
    items = await OrderService.order_items(session, order_id)
    profit = OrderService.profit_cents(items)
    assert profit == (SALE_PRICE - COST_AT_ORDER_TIME) * QUANTITY


async def test_profit_not_rewritten_when_source_cost_changes(session: object) -> None:
    """★ 货源涨价 + 镜像同步后，历史订单利润**不变**（不可回溯改写）。"""
    order_id, sku_id, mapping_id = await _seed_order(session)

    before = OrderService.profit_cents(await OrderService.order_items(session, order_id))

    # 真源涨价：12.00 → 25.00
    sku = (await session.execute(select(SourceSku).where(SourceSku.id == sku_id))).scalars().first()
    sku.cost_price_cents = 2500
    # 镜像同步（auto 状态会跟随真源）
    mapping = (await session.execute(select(SkuMapping).where(SkuMapping.id == mapping_id))).scalars().first()
    mapping.purchase_cost_cents = 2500
    await session.flush()

    after = OrderService.profit_cents(await OrderService.order_items(session, order_id))
    assert after == before, (
        f"历史订单利润被回溯改写：{before} → {after}（违反附录 A 第 18 条）"
    )
    assert after == (SALE_PRICE - COST_AT_ORDER_TIME) * QUANTITY


async def test_order_detail_vo_profit_reads_items_snapshot(session: object) -> None:
    """`OrderDetailVo.profit_cents` 只读 `items[]` 快照。"""
    order_id, _sku_id, _mapping_id = await _seed_order(session)
    order = await OrderService.get_order(session, order_id)
    items = await OrderService.order_items(session, order_id)
    detail = OrderDetailVo.from_model(order, items=items)

    assert detail.profit_cents == (SALE_PRICE - COST_AT_ORDER_TIME) * QUANTITY
    assert detail.profit.startswith("53.70"), f"利润展示异常：{detail.profit}"


def _code_without_docstring(func: object) -> str:
    """取函数源码并剔除 docstring（只校验**真实代码**，避免注释误判）。"""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    node = tree.body[0]
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    if ast.get_docstring(node):
        node.body = node.body[1:]
    return ast.unparse(node).lower()


async def test_no_join_back_in_profit_calculation() -> None:
    """★ 结构性断言：利润计算的**代码**（不含注释）不得引用 `sku_mapping` / `source_sku`。"""
    code = _code_without_docstring(OrderService.profit_cents)
    assert "sku_mapping" not in code, "利润计算 join 回 sku_mapping —— 违反附录 A 第 18 条"
    assert "source_sku" not in code, "利润计算 join 回 source_sku —— 违反附录 A 第 18 条"
    assert "purchase_cost_cents" in code, "利润必须读取 order_item 的成本快照"

    vo_code = _code_without_docstring(OrderDetailVo.profit_cents.fget)
    assert "sku_mapping" not in vo_code
    assert "source_sku" not in vo_code
