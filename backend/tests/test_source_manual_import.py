"""★ 货源手工录入 / CSV 导入 + 采集任务失败的语义修正（回归锁）。

================================================================================
★ 为什么这批用例存在
================================================================================
1. **手工录入是真实主路径，不是降级预案**：1688 采集需要开放平台凭证（AppKey/AccessToken），
   个体户 / 个人身份证店拿不到。此前系统只有一个采集入口 —— 凭证一缺，
   **一条货源都进不来**，AI 重构 / SKU 映射 / 上架 / 订单匹配全部起不了步。

2. **必须绕开 1688 适配器**：新端点若仍走 `Alibaba1688Adapter`，
   在真实环境里就是"录得进去但永远写不进去"，等于没做。

3. **录进去的数据必须能被下游消费**（★ 本文件的核心断言）：
   断言链路是「手工录入 → 建 SKU 映射 → 映射校验 non-blocking」，
   而不是只断言"201 创建成功" —— 后者正是"录得进去但后面走不通"的死路。

4. **CSV 失败必须逐行可读**：`failed[{row, identifier, reason}]`，
   不允许静默吞掉任何一行，也不允许失败行污染成功行。

5. **采集全部失败必须报 failed**（语义修正）：早期一律 success，
   前端只看 status 会误以为采集成功了，这是静默失效。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from tests.conftest import ADMIN_HEADERS

API = "/api/v1"

CSV_COLUMNS = (
    "商品编码",
    "商品标题",
    "类目",
    "供应商ID",
    "商品成本价",
    "原链接",
    "主图URL",
    "商品状态",
    "SKU编码",
    "规格名",
    "规格值",
    "SKU成本价",
    "售价",
    "库存",
    "备注",
)
CSV_HEADER = ",".join(CSV_COLUMNS)


def csv_row(**fields: Any) -> str:
    """按列名拼一行 CSV（避免手写逗号时列数错位，曾导致断言全部串行到相邻列）。"""
    values = [str(fields.get(name) or "") for name in CSV_COLUMNS]
    return ",".join(values)


def csv_text(*rows: str) -> bytes:
    """拼一份 UTF-8 CSV 文本（不含 BOM）。"""
    return ("\n".join([CSV_HEADER, *rows]) + "\n").encode("utf-8")


def uniq(prefix: str = "MAN") -> str:
    """本次运行时唯一的商品编码（用例之间互不污染）。"""
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


def manual_payload(code: str | None = None, **overrides: Any) -> dict[str, Any]:
    """构造手工录入请求体。"""
    payload: dict[str, Any] = {
        "title": "纯棉圆领短袖T恤 夏季薄款",
        "product_code": code or uniq(),
        "category_path": "女装/上装/T恤",
        "cost_price": "18.00",
        "origin_url": "https://example.com/item/demo",
        "main_image_url": "https://example.com/img/demo.jpg",
        "skus": [
            {
                "spec_name": "颜色;尺码",
                "spec_value": "红色;XL",
                "sku_code": f"{code or 'SKU'}-RED-XL",
                "cost_price": "18.00",
                "sale_price": "59.90",
                "stock_qty": 200,
            }
        ],
    }
    payload.update(overrides)
    return payload


async def wait_task(client: Any, task_id: int, *, timeout_s: float = 15.0) -> dict[str, Any]:
    """轮询任务到终态；超时返回当前状态（不假装成功）。"""
    deadline = asyncio.get_event_loop().time() + timeout_s
    body: dict[str, Any] = {}
    while True:
        response = await client.get(f"{API}/tasks/{task_id}")
        body = dict((response.json().get("data") or {}))
        if str(body.get("status")) in {"success", "failed", "cancelled"}:
            return body
        if asyncio.get_event_loop().time() >= deadline:
            return body
        await asyncio.sleep(0.05)


# =====================================================================
#  ① 手工录入
# =====================================================================


async def test_manual_create_persists_skus_and_platform(client: Any) -> None:
    """手工录入：落商品 + SKU，`source_platform='manual'`，`product_1688_id` 带 MANUAL- 前缀。"""
    code = uniq()
    response = await client.post(f"{API}/source-products/manual", json=manual_payload(code), headers=ADMIN_HEADERS)
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["source_platform"] == "manual"
    assert data["product_1688_id"] == f"MANUAL-{code}"
    assert data["created"] is True and data["created_skus"] == 1
    assert len(data["skus"]) == 1

    sku = data["skus"][0]
    assert sku["spec_json"] == {"颜色": "红色", "尺码": "XL"}
    assert sku["cost_price"] == "18.00"
    assert sku["suggested_sale_price"] == "59.90"  # ★ 建议售价要能取回，回填时有据可依
    assert sku["spec_signature"], "规格指纹必须落库，否则规格变更检测（MAP-P0-04）无从判定"


async def test_manual_create_is_idempotent_by_product_code(client: Any) -> None:
    """同一商品编码重复录入 ⇒ 更新而非新建（避免运营重复导入造出一堆垃圾数据）。"""
    code = uniq()
    await client.post(f"{API}/source-products/manual", json=manual_payload(code), headers=ADMIN_HEADERS)
    second = await client.post(f"{API}/source-products/manual", json=manual_payload(code), headers=ADMIN_HEADERS)
    assert second.status_code == 201, second.text
    data = second.json()["data"]
    assert data["created"] is False
    assert data["updated_skus"] == 1
    assert len(data["skus"]) == 1, "幂等更新不得把同一个 SKU 复制成两条"


async def test_manual_create_auto_generates_code_when_blank(client: Any) -> None:
    """不填商品编码 ⇒ 自动生成 `MANUAL-<时间戳>-<随机>` 且库内唯一。"""
    payload = manual_payload()
    payload.pop("product_code")
    payload["skus"][0].pop("sku_code")
    response = await client.post(f"{API}/source-products/manual", json=payload, headers=ADMIN_HEADERS)
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["product_1688_id"].startswith("MANUAL-")
    assert data["skus"][0]["sku_code_1688"].startswith(data["product_1688_id"])
    assert data["skus"][0]["spec_json"] == {"颜色": "红色", "尺码": "XL"}


async def test_manual_create_rejects_empty_skus_with_readable_reason(client: Any) -> None:
    """★ 没有 SKU 的货源是"后面走不通的死路"（建不了映射、上不了架），录入阶段必须拦住。"""
    payload = manual_payload()
    payload["skus"] = []
    response = await client.post(f"{API}/source-products/manual", json=payload, headers=ADMIN_HEADERS)
    assert response.status_code == 400, response.text
    detail = response.json()["data"] or {}
    failed = detail.get("failed") or []
    assert failed and "SKU" in failed[0]["reason"], f"失败原因必须可读且指向真问题：{failed}"


@pytest.mark.parametrize(
    ("override", "expected_keyword"),
    [
        ({"skus": [{"spec_name": "颜色", "spec_value": "红色", "cost_price": "十八块"}]}, "不是合法金额"),
        ({"supplier_id": 999999}, "不存在"),
        ({"origin_url": "www.example.com/x"}, "http"),
        ({"skus": [{"spec_name": "颜色;尺码", "spec_value": "红色"}]}, "数量不一致"),
        ({"skus": [{"sku_code": "A-Costly", "cost_price": "-5"}]}, "不能为负数"),
    ],
)
async def test_manual_create_rejects_bad_input_with_chinese_reason(
    client: Any, override: dict[str, Any], expected_keyword: str
) -> None:
    """★ 参数非法必须给**中文可读原因**，而不是 500 / 空 message。"""
    payload = manual_payload()
    payload.update(override)
    response = await client.post(f"{API}/source-products/manual", json=payload, headers=ADMIN_HEADERS)
    assert response.status_code == 400, response.text
    failed = (response.json()["data"] or {}).get("failed") or []
    assert failed, "校验失败必须带 failed 明细"
    assert expected_keyword in failed[0]["reason"], failed[0]["reason"]


async def test_manual_sku_cost_falls_back_to_product_cost(client: Any) -> None:
    """SKU 没填成本 ⇒ 回落商品级成本（避免落库即 cost_invalid 卡死下游）。"""
    payload = manual_payload()
    payload["cost_price"] = "12.50"
    payload["skus"][0].pop("cost_price")
    response = await client.post(f"{API}/source-products/manual", json=payload, headers=ADMIN_HEADERS)
    assert response.status_code == 201, response.text
    assert response.json()["data"]["skus"][0]["cost_price"] == "12.50"


# =====================================================================
#  ② CSV 导入
# =====================================================================


async def test_import_csv_creates_products_and_groups_skus(client: Any) -> None:
    """CSV：两行同商品编码 ⇒ 1 个商品 2 个 SKU；另一编码 ⇒ 另 1 个商品。"""
    first, second = uniq("CSV-A"), uniq("CSV-B")
    payload = csv_text(
        csv_row(
            商品编码=first, 商品标题="纯棉T恤", 类目="女装/T恤", 商品成本价="18.00",
            SKU编码=f"{first}-RED", 规格名="颜色;尺码", 规格值="红色;XL",
            SKU成本价="18.00", 售价="59.90", 库存="100",
        ),
        csv_row(
            商品编码=first, SKU编码=f"{first}-BLU", 规格名="颜色;尺码", 规格值="蓝色;L",
            SKU成本价="17.50", 售价="55.90", 库存="80",
        ),
        csv_row(
            商品编码=second, 商品标题="保温杯", 类目="家居/水具", 商品成本价="22.00",
            SKU编码=f"{second}-500", 规格名="容量", 规格值="500ml", 售价="49.00", 库存="50",
        ),
    )
    response = await client.post(
        f"{API}/source-products/import-csv",
        files={"file": ("sources.csv", payload, "text/csv")},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data == {**data, "created": 2, "updated": 0, "failed": []}, data
    assert data["created_skus"] == 3

    listed = await client.get(
        f"{API}/source-products",
        params={"page_size": 50, "source_platform": "manual", "product_1688_id": f"MANUAL-{first}"},
    )
    items = listed.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["source_platform"] == "manual"
    assert items[0]["sku_count"] == 2, "同编码多行必须合并进同一个商品"


async def test_import_csv_is_idempotent_and_counts_updates(client: Any) -> None:
    """重复导入同一份 CSV ⇒ 第二次全部走更新路径（幂等）。"""
    code = uniq("CSV-C")
    payload = csv_text(
        csv_row(
            商品编码=code, 商品标题="双肩包", 类目="箱包/背包", 商品成本价="30.00",
            SKU编码=f"{code}-BLK", 规格名="颜色", 规格值="黑色",
            SKU成本价="30.00", 售价="89.00", 库存="20",
        )
    )
    first = await client.post(
        f"{API}/source-products/import-csv",
        files={"file": ("sources.csv", payload, "text/csv")},
        headers=ADMIN_HEADERS,
    )
    assert first.json()["data"]["created"] == 1
    second = await client.post(
        f"{API}/source-products/import-csv",
        files={"file": ("sources.csv", payload, "text/csv")},
        headers=ADMIN_HEADERS,
    )
    assert second.status_code == 200, second.text
    data = second.json()["data"]
    assert data["created"] == 0 and data["updated"] == 1
    assert data["updated_skus"] == 1


async def test_import_csv_reports_every_failed_row_and_keeps_good_rows(client: Any) -> None:
    """★ 失败必须逐行给中文原因，且**不得**连坐同一份 CSV 里的正常行。"""
    good, blank_title, dup = uniq("CSV-D"), uniq("CSV-E"), uniq("CSV-F")
    payload = csv_text(
        # 第 2 行：正常行（必须照常写入）
        csv_row(
            商品编码=good, 商品标题="好商品", 类目="女装", 商品成本价="15.00",
            SKU编码=f"{good}-01", 规格名="颜色", 规格值="红色",
            SKU成本价="15.00", 售价="49.00", 库存="10",
        ),
        # 第 3 行：商品标题为空
        csv_row(
            商品编码=blank_title, 类目="女装", 商品成本价="15.00",
            SKU编码=f"{blank_title}-01", 规格名="颜色", 规格值="红色",
            SKU成本价="15.00", 售价="49.00", 库存="10",
        ),
        # 第 4 行：商品成本价不是数字
        csv_row(
            商品编码=dup, 商品标题="成本非法", 类目="女装", 商品成本价="十五块",
            SKU编码=f"{dup}-01", 规格名="颜色", 规格值="红色",
            SKU成本价="15.00", 售价="49.00", 库存="10",
        ),
        # 第 5 行：与前一行同商品且 SKU 编码重复
        csv_row(
            商品编码=dup, SKU编码=f"{dup}-01", 规格名="颜色", 规格值="蓝色",
            SKU成本价="15.00", 售价="49.00", 库存="10",
        ),
    )
    response = await client.post(
        f"{API}/source-products/import-csv",
        files={"file": ("sources.csv", payload, "text/csv")},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]

    failed_rows = {int(item["row"]): item["reason"] for item in data["failed"]}
    assert data["created"] == 1, f"正常行必须照常写入：{data}"
    assert 3 in failed_rows and "商品标题不能为空" in failed_rows[3], failed_rows
    assert 4 in failed_rows and "不是合法金额" in failed_rows[4], failed_rows
    assert 5 in failed_rows and "重复" in failed_rows[5], failed_rows
    # 原因文案里必须带行号（运营照着改，不需要自己去数行）
    assert all(f"第{row}行" in reason for row, reason in failed_rows.items()), failed_rows


async def test_import_csv_supports_gbk_and_utf8_bom(client: Any) -> None:
    """★ Excel 导出的中文 CSV 常见 GBK / UTF-8-BOM 两种编码，都必须能读。"""
    for encoding in ("utf-8-sig", "gbk"):
        probe = f"{uniq('CSV-H')}-{encoding}"
        body = ("\n".join([CSV_HEADER, csv_row(
            商品编码=probe, 商品标题="陶瓷杯", 类目="家居", 商品成本价="9.90",
            SKU编码=f"{probe}-01", 规格名="容量", 规格值="300ml",
            SKU成本价="9.90", 售价="29.90", 库存="30",
        )]) + "\n").encode(encoding)
        response = await client.post(
            f"{API}/source-products/import-csv",
            files={"file": ("sources.csv", body, "text/csv")},
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["created"] == 1, f"{encoding} 编码未被正确识别"

    # utf-8（无 BOM）同样要能用
    code = uniq("CSV-G")
    plain = csv_text(csv_row(
        商品编码=code, 商品标题="陶瓷杯", 类目="家居", 商品成本价="9.90",
        SKU编码=f"{code}-01", 规格名="容量", 规格值="300ml",
        SKU成本价="9.90", 售价="29.90", 库存="30",
    ))
    response = await client.post(
        f"{API}/source-products/import-csv",
        files={"file": ("sources.csv", plain, "text/csv")},
        headers=ADMIN_HEADERS,
    )
    assert response.json()["data"]["created"] == 1


async def test_import_csv_rejects_unusable_file(client: Any) -> None:
    """整份文件不可用时（空 / 缺必填列 / 超行数上限）必须在**写库前**报 400，一行都不写。"""
    empty = await client.post(
        f"{API}/source-products/import-csv",
        files={"file": ("empty.csv", b"", "text/csv")},
        headers=ADMIN_HEADERS,
    )
    assert empty.status_code == 400, empty.text
    assert "为空" in empty.json()["message"]

    no_title_column = await client.post(
        f"{API}/source-products/import-csv",
        files={"file": ("bad.csv", "商品编码,SKU编码\nA,1\n".encode("utf-8"), "text/csv")},
        headers=ADMIN_HEADERS,
    )
    assert no_title_column.status_code == 400, no_title_column.text
    assert "商品标题" in no_title_column.json()["message"]

    rows = [
        csv_row(
            商品编码=f"{uniq('CSV-X')}-{i}", 商品标题=f"标题{i}", 商品成本价="1.00",
            SKU编码=f"SKU-{i}", 规格名="颜色", 规格值="红色",
            SKU成本价="1.00", 售价="2.00", 库存="1",
        )
        for i in range(501)
    ]
    too_many = await client.post(
        f"{API}/source-products/import-csv",
        files={"file": ("big.csv", csv_text(*rows), "text/csv")},
        headers=ADMIN_HEADERS,
    )
    assert too_many.status_code == 400, too_many.text
    assert "500" in too_many.json()["message"]


# =====================================================================
#  ③ 下游可消费性（★ 不允许"录得进去但后面走不通"）
# =====================================================================


async def test_manual_source_can_build_valid_mapping_and_pass_validation(client: Any) -> None:
    """★ 主路径闭环：手工录入 → 建 SKU 映射 → 上架前映射校验 non-blocking。"""
    code = uniq("MP")
    created = await client.post(
        f"{API}/source-products/manual", json=manual_payload(code), headers=ADMIN_HEADERS
    )
    assert created.status_code == 201, created.text
    payload = created.json()["data"]
    product_id = int(payload["id"])
    sku_code = str(payload["skus"][0]["sku_code_1688"])

    mapping = await client.post(
        f"{API}/sku-mappings",
        json={
            "platform": "taobao",
            "shop_id": "shop-manual-001",
            "shop_item_id": f"item-{code}",
            "shop_sku_code": f"{code}-SHOP",
            "source_product_id": product_id,
            "source_sku_id": int(payload["skus"][0]["id"]),
            "source_product_1688_id": payload["product_1688_id"],
            "source_sku_code_1688": sku_code,
            "purchase_cost": "18.00",
            "status": "valid",
        },
        headers=ADMIN_HEADERS,
    )
    assert mapping.status_code == 201, mapping.text
    assert mapping.json()["data"]["status"] == "valid"

    validate = await client.post(
        f"{API}/sku-mappings/validate",
        json={
            "source_product_id": product_id,
            "platform": "taobao",
            "shop_id": "shop-manual-001",
            "sku_codes": [f"{code}-SHOP"],
        },
        headers=ADMIN_HEADERS,
    )
    assert validate.status_code == 200, validate.text
    body = validate.json()["data"]
    assert body["blocking"] is False, f"手工录入的数据必须能被下游消费：{body}"


async def test_manual_source_is_visible_in_list_and_detail(client: Any) -> None:
    """手工录入的商品必须能在列表（可按 manual 过滤）与详情里查到。"""
    code = uniq("DT")
    created = await client.post(
        f"{API}/source-products/manual", json=manual_payload(code), headers=ADMIN_HEADERS
    )
    product_id = int(created.json()["data"]["id"])

    detail = await client.get(f"{API}/source-products/{product_id}")
    assert detail.status_code == 200, detail.text
    data = detail.json()["data"]
    assert data["source_platform"] == "manual"
    assert data["params_json"].get("source_platform") == "manual"
    assert data["skus"] and data["skus"][0]["suggested_sale_price"] == "59.90"

    filtered = await client.get(
        f"{API}/source-products", params={"page_size": 200, "source_platform": "manual"}
    )
    ids = {int(item["id"]) for item in filtered.json()["data"]["items"]}
    assert product_id in ids
    everything_1688 = await client.get(
        f"{API}/source-products", params={"page_size": 200, "source_platform": "alibaba1688"}
    )
    opposite = {int(item["id"]) for item in everything_1688.json()["data"]["items"]}
    assert product_id not in opposite, "手工录入的商品不能被算成 1688 采集"


# =====================================================================
#  ④ 采集任务语义：全部失败 ⇒ failed（不是 success）
# =====================================================================


async def test_collect_total_failure_marks_task_failed(client: Any) -> None:
    """★ 全部采集失败（本环境即未配置 1688 凭证）⇒ `task_record.status == 'failed'`。

    此前一律 success：前端只看 status 会显示"采集成功"，而实际一条货源都没进来 —— 静默失效。
    """
    submitted = await client.post(
        f"{API}/source-products/collect",
        json={"identifiers": ["https://detail.1688.com/offer/888888888.html"]},
        headers=ADMIN_HEADERS,
    )
    assert submitted.status_code == 202, submitted.text
    task_id = int(submitted.json()["data"]["task_record_id"])

    record = await wait_task(client, task_id)
    assert record.get("status") == "failed", f"全部失败必须报 failed：{record}"
    failed_rows = list((record.get("result_json") or {}).get("failed") or [])
    assert failed_rows, "失败明细必须保留在 result_json.failed[] 里"
    assert failed_rows[0].get("reason"), "每一行失败都要有可读原因"
