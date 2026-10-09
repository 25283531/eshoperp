"""API 契约回归测试（FE 联调暴露的两个 500 bug + 枚举完整性）。

覆盖三个曾经真实出错的点，防止回归：
    1. ★ `AdminOperator` 依赖必须被解析为**子依赖**，不能被当成必填 query 参数 `op`；
       否则 `require_admin()` 收到字符串 → AttributeError → 全部管理端接口 500。
    2. ★ `POST /listing-products/{id}/fill-price` 必须传 Pydantic 对象（不是 `model_dump()` 的 dict）；
       否则服务层属性访问 → AttributeError → 500。
    3. `/settings/enums` 必须包含检测键 `duplicate_item`，且订单数据不得出现枚举外状态。
"""

from __future__ import annotations

from typing import Any

from tests.conftest import ADMIN_HEADERS, OPERATOR_HEADERS


async def test_admin_dependency_is_sub_dependency(client: Any) -> None:
    """★ OpenAPI 中不得出现名为 `op` 的 query 参数（FE 报告的 500 根因）。"""
    spec = client._transport.app.openapi()  # type: ignore[attr-defined]
    op_params = [
        param
        for path in spec["paths"].values()
        for operation in path.values()
        for param in operation.get("parameters", [])
        if param.get("name") == "op"
    ]
    assert op_params == [], (
        "AdminOperator 被解析成了 query 参数 op —— "
        "必须用带类型注解的函数作为依赖（lambda 无注解不会被解析为子依赖）"
    )


async def test_admin_endpoint_ok_with_admin_token(client: Any) -> None:
    """管理员 Token → 200（此前一律 500）。"""
    response = await client.put(
        "/api/v1/settings/inventory.poll_interval_min",
        json={"value": "30", "reason": "回归验证"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200, f"管理员改配置期望 200，实际 {response.status_code}"
    assert response.json()["code"] == 0


async def test_admin_endpoint_403_without_admin_token(client: Any) -> None:
    """非管理员 → 403（而不是 500）。"""
    response = await client.put(
        "/api/v1/settings/inventory.poll_interval_min",
        json={"value": "30"},
        headers=OPERATOR_HEADERS,
    )
    assert response.status_code == 403, f"非管理员期望 403，实际 {response.status_code}"


async def test_listing_mode_switch_ok(client: Any) -> None:
    """切换上架模式（管理员）→ 200。"""
    response = await client.put(
        "/api/v1/adapters/listing/mode",
        json={"mode": "manual", "reason": "回归验证"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200, f"切换上架模式期望 200，实际 {response.status_code}"


async def test_fill_price_accepts_valid_body(client: Any) -> None:
    """★ 合法补填 → 200（此前因传 dict 而 500）。"""
    products = (await client.get("/api/v1/listing-products?missing_price=true")).json()
    items = (products.get("data") or {}).get("items") or []
    if not items:
        # 测试库可能没有种子数据：此时只验证「不 500」，改用一个不存在的 ID 也应该 404 而非 500
        response = await client.post(
            "/api/v1/listing-products/999999/fill-price",
            json={"items": [{"shop_sku_code": "X", "sale_price": "1.00"}]},
            headers=OPERATOR_HEADERS,
        )
        assert response.status_code in (404, 422), f"不存在的商品期望 404/422，实际 {response.status_code}"
        return

    product_id = int(items[0]["id"])
    detail = (await client.get(f"/api/v1/listing-products/{product_id}")).json()
    sku_code = ((detail.get("data") or {}).get("skus") or [{}])[0].get("shop_sku_code")
    response = await client.post(
        f"/api/v1/listing-products/{product_id}/fill-price",
        json={"items": [{"shop_sku_code": sku_code, "sale_price": "39.90"}]},
        headers=OPERATOR_HEADERS,
    )
    assert response.status_code == 200, f"售价补填期望 200，实际 {response.status_code}"
    body = response.json()["data"]
    assert body["updated"] == 1
    assert "recomputed" in body and "new_conflicts" in body


async def test_fill_price_missing_price_is_422(client: Any) -> None:
    """缺 `sale_price` → 422（不是 500）。"""
    response = await client.post(
        "/api/v1/listing-products/1/fill-price",
        json={"items": [{"shop_sku_code": "ANY"}]},
        headers=OPERATOR_HEADERS,
    )
    assert response.status_code == 422, f"缺售价期望 422，实际 {response.status_code}"


async def test_enums_include_duplicate_item(client: Any) -> None:
    """`ConflictType` 必须包含检测键 `duplicate_item`（②b 同一 SKU 编码挂多商品）。"""
    enums = (await client.get("/api/v1/settings/enums")).json()["data"]
    values = {item["value"] for item in enums["ConflictType"]}
    assert "duplicate_item" in values, f"ConflictType 缺 duplicate_item，实际：{sorted(values)}"
    for item in enums["ConflictType"]:
        assert "level" in item and "blocking" in item, "每个冲突类型必须带 level / blocking"
        if item["value"] == "many_to_one":
            assert item["level"] == "P1" and item["blocking"] is False


async def test_order_statuses_are_enum_known(client: Any) -> None:
    """订单数据的 `fulfillment_status` 不得出现枚举外的值（否则界面显示原始英文）。"""
    enums = (await client.get("/api/v1/settings/enums")).json()["data"]
    known = {item["value"] for item in enums["OrderStatus"]}
    orders = (await client.get("/api/v1/orders")).json()["data"]["items"]
    unknown = {o["fulfillment_status"] for o in orders} - known
    assert unknown == set(), f"订单出现枚举外状态：{sorted(unknown)}"
