"""1688 采集链路回归（T-1688）。

★ 为什么必须有一份不带网络的回归：
    `alibaba.product.get` 的 ACL 权限 2026-10-10 尚未开通，**拿不到真实商品响应**，
    一旦有人在等权限期间改动签名 / 解析 / 下载代码，没有测试就发现不了回归。
    因此这里把「能离线验证的部分」全部钉死：
        1. 签名算法（用固定向量防"改回 MD5"这类静默退化）；
        2. 商品链接解析（前端粘贴的是整条 URL）；
        3. 详情图多别名提取（字段名 PROBE-PENDING ⇒ 必须多别名）；
        4. `download_image` 返回**字节**（历史 bug：三元组被当字节喂给 sha256 → TypeError）；
        5. `_download_assets` 写盘 / 算 hash / 落 asset 且同批次去重。

★ 全量 pytest **一次只开一个进程**（SQLite 单写者，并发跑会产生假故障）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.adapters.source.alibaba1688 import (
    GATEWAY_URL,
    Alibaba1688Adapter,
    extract_image_urls,
)
from app.models.enums import AssetOrigin, AssetType

FAKE_MAIN = "https://cbu01.alicdn.com/img/ibank/main_400x400.jpg"
FAKE_D1 = "https://cbu01.alicdn.com/img/ibank/detail_01.jpg"
FAKE_D2 = "https://cbu01.alicdn.com/img/ibank/detail_02.jpg"
FAKE_D3 = "https://cbu01.alicdn.com/img/ibank/detail_03.jpg"

# 固定签名向量：urlPath + 排序后的 k/v 串 → UPPER(HMAC-SHA1)
SIGN_APP_KEY = "TEST_APPKEY_0001"
SIGN_APP_SECRET = "UNIT-TEST-SECRET"
SIGN_URL_PATH = "param2/1/system/currentTime/TEST_APPKEY_0001"
SIGN_PARAMS = {
    "_aop_timestamp": "1700000000000",
    "access_token": "UNIT-TEST-TOKEN-abc123",
    "productId": "694567890123",
}
SIGN_EXPECTED = "75CD0EB77C5DCB393BD2C2E4DB4CE09849780EC9"


def _adapter() -> Alibaba1688Adapter:
    return Alibaba1688Adapter(
        app_key=SIGN_APP_KEY, app_secret=SIGN_APP_SECRET, access_token="unit-token"
    )


# ---------------------------------------------------------------------------
#  1. 签名
# ---------------------------------------------------------------------------


def test_signature_matches_official_hmac_sha1_vector() -> None:
    """★ 官方签名必须是 HMAC-SHA1 + 大写十六进制（**不是 MD5**）。

    向量离线算定：改算法 / 改拼接方式都会让这条挂掉。
    """
    adapter = _adapter()
    assert adapter._url_path("system", "currentTime") == SIGN_URL_PATH
    assert adapter._sign(SIGN_URL_PATH, SIGN_PARAMS) == SIGN_EXPECTED


def test_sign_sorts_params_and_excludes_aop_signature() -> None:
    """`_aop_signature` 不参与签名；其余参数按 key 升序直接拼 `k` + `v`。"""
    adapter = _adapter()
    url_path = adapter._url_path("system", "currentTime")
    a = adapter._sign(url_path, {"b": "2", "a": "1", "c": "3"})
    b = adapter._sign(url_path, {"c": "3", "b": "2", "a": "1"})
    assert a == b, "同一组参数换顺序必须得到同一签名（按 key 排序）"
    assert a != adapter._sign(url_path, {"a": "1", "b": "2"}), "缺参数签名必须变化"

    url, query = adapter._signed_query("system", "currentTime", {})
    assert url.startswith(f"{GATEWAY_URL}/openapi/param2/1/system/currentTime/{SIGN_APP_KEY}")
    assert query["_aop_signature"] == adapter._sign(
        "param2/1/system/currentTime/TEST_APPKEY_0001",
        {"access_token": "unit-token", "_aop_timestamp": query["_aop_timestamp"]},
    )
    assert "_aop_signature" not in SIGN_PARAMS  # 签名串里不含它本身
    assert query["_aop_timestamp"].isdigit() and len(query["_aop_timestamp"]) == 13, "必须是毫秒时间戳"


# ---------------------------------------------------------------------------
#  2. 商品链接解析
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "https://detail.1688.com/offer/694567890123.html",
        "https://detail.1688.com/offer/694567890123.html?spm=a260k.1.b1016.2.a1b2c3",
        "https://detail.1688.com/offer/694567890123.html?x=1#anchor",
        "http://m.1688.com/offer/694567890123.htm",
        "detail.1688.com/offer/694567890123.html",
        "https://detail.1688.com/offer/694567890123",
        "https://detail.1688.com/?offerId=694567890123",
        "694567890123",
        "  694567890123  ",
    ],
)
def test_parse_1688_product_id_accepts_common_forms(raw: str) -> None:
    """★ 前端 placeholder 就是整条链接：贴链接必须能采集，不能整串当 ID。"""
    from app.utils.kit import parse_1688_product_id

    offer_id, reason = parse_1688_product_id(raw)
    assert offer_id == "694567890123", f"{raw!r} 解析失败：{reason}"
    assert reason == ""


@pytest.mark.parametrize(
    "raw",
    ["https://item.taobao.com/item.htm?id=123456", "复制一下这个", ""],
)
def test_parse_1688_product_id_reports_human_readable_errors(raw: str) -> None:
    """解析失败要给**使用者照着做**的下一步，而不是裸 None。"""
    from app.utils.kit import parse_1688_product_id

    offer_id, reason = parse_1688_product_id(raw)
    assert offer_id == ""
    assert "无法从链接中识别商品 ID" in reason or "不能为空" in reason


# ---------------------------------------------------------------------------
#  3. 详情图多别名提取
# ---------------------------------------------------------------------------


def test_extract_image_urls_is_alias_agnostic() -> None:
    """★ 详情图字段名 PROBE-PENDING ⇒ 必须同时吃下 `descImageList` / `detailImageList` / 富文本。"""
    payload = {
        "productID": "1",
        "imageUrl": FAKE_MAIN,
        "descImageList": [{"url": FAKE_D1}],  # 别名 1：对象数组
        "detailImageList": [FAKE_D2],  # 别名 2：字符串数组
        "description": f'<p>文字</p><img src="{FAKE_D3}" />',  # 别名 3：富文本 HTML
    }
    urls = extract_image_urls(payload)
    assert FAKE_MAIN in urls
    assert FAKE_D1 in urls and FAKE_D2 in urls and FAKE_D3 in urls


def test_parse_product_splits_main_and_detail_images() -> None:
    """主图单独取值，其余为详情图，且主图不重复出现在详情里。"""
    adapter = _adapter()
    product = adapter.parse_product(
        {
            "product": {
                "productID": "999999888888",
                "subject": "联调样例",
                "price": "29.90",
                "imageUrl": FAKE_MAIN,
                "images": [FAKE_MAIN, FAKE_D1, FAKE_D2],
                "skuList": [{"skuCode": "A1", "price": "29.90", "amountOnSale": 10}],
            }
        },
        fallback_id="999999888888",
    )
    assert product.product_1688_id == "999999888888"
    assert product.main_image_url == FAKE_MAIN
    assert FAKE_MAIN not in product.detail_image_urls
    assert product.detail_image_urls == [FAKE_D1, FAKE_D2]
    assert product.cost_price_cents == 2990
    assert [sku.sku_code_1688 for sku in product.skus] == ["A1"]


def test_parse_product_falls_back_to_first_image_as_main() -> None:
    """没有显式主图字段时，取第一张图片当主图（不能主图为空导致素材全丢）。"""
    adapter = _adapter()
    product = adapter.parse_product({"product": {"images": [FAKE_D1, FAKE_D2]}}, fallback_id="7")
    assert product.main_image_url == FAKE_D1
    assert product.detail_image_urls == [FAKE_D2]


# ---------------------------------------------------------------------------
#  4. 网关错误分级
# ---------------------------------------------------------------------------


def test_acl_decline_message_is_actionable() -> None:
    """★ ACLDecline 必须指明「去 open.1688.com 给 AppKey 申请权限」，不是笼统的采集失败。"""
    adapter = _adapter()
    message = adapter._explain_error(
        400,
        '{"error_code":"gw.APIACLDecline","error_message":"AppKey is not allowed(acl)"}',
        namespace="com.alibaba.product",
        method="alibaba.product.get",
    )
    assert "open.1688.com" in message
    assert "alibaba.product.get" in message
    assert SIGN_APP_KEY in message
    assert "不是系统故障" in message


@pytest.mark.parametrize(
    ("code", "expect"),
    [
        ("gw.APIUnsupported", "不认识这个接口名"),
        ("gw.SignatureMissing", "签名错误"),
        ("gw.AppKeyMissing", "URL 路径段末尾"),
    ],
)
def test_gateway_errors_are_classified(code: str, expect: str) -> None:
    adapter = _adapter()
    message = adapter._explain_error(
        400, f'{{"error_code":"{code}","error_message":"boom"}}', namespace="ns", method="meth"
    )
    assert expect in message


def test_unauthenticated_message_points_to_refresh_token() -> None:
    adapter = _adapter()
    message = adapter._explain_error(
        401, "Request need user authenticated", namespace="ns", method="meth"
    )
    assert "access_token" in message and "重新" in message


# ---------------------------------------------------------------------------
#  5. download_image 语义 + 素材落盘
# ---------------------------------------------------------------------------


async def test_download_image_returns_bytes_not_triple(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ `download_image` 必须返回 `(ok, bytes, msg)`：历史 bug 是 `(ok, hash, msg)`，
    调用方把返回值当字节喂给 sha256 ⇒ TypeError 把任务打挂。
    """

    class _Resp:
        status_code = 200
        content = b"\x89PNG\r\n\x1a\nfake"

    class _Client:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):  # noqa: ANN002
            return False

        async def get(self, url, headers=None):  # noqa: ANN001, ARG002
            return _Resp()

    monkeypatch.setattr("app.adapters.source.alibaba1688.httpx.AsyncClient", _Client)
    ok, data, message = await Alibaba1688Adapter.download_image(FAKE_MAIN)
    assert ok is True
    assert isinstance(data, bytes), f"必须返回字节，实际是 {type(data)}"
    from app.utils.kit import content_hash_bytes

    assert content_hash_bytes(data)  # 历史 bug 在这一行 TypeError
    assert message == "下载成功"


async def test_download_assets_writes_files_and_dedupes(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """解析 → 下载 → 落 asset 全链路：主图 / 详情图分类型、同内容去重、文件真实落盘。"""
    from sqlalchemy import select

    from app.models.asset import Asset
    from app.services.source_service import SourceService

    product_id = 9876543210
    assets_dir = None

    async def _fake_download(url: str, *args, **kwargs):  # noqa: ANN002, ANN003
        # 与主图完全相同的字节 ⇒ 应被 content_hash 去重
        payload = b"MAIN-BYTES" if url.endswith(("main.jpg", "dup.jpg")) else url.encode()
        return True, payload, "下载成功"

    monkeypatch.setattr(Alibaba1688Adapter, "download_image", staticmethod(_fake_download))

    class _Product:
        id = 4242
        product_1688_id = str(product_id)

    items = [
        ("https://x.test/main.jpg", AssetType.MAIN_IMAGE.value),
        ("https://x.test/d1.jpg", AssetType.DETAIL_IMAGE.value),
        ("https://x.test/d2.jpg", AssetType.DETAIL_IMAGE.value),
        ("https://x.test/dup.jpg", AssetType.DETAIL_IMAGE.value),
        ("ftp://x.test/ignored.jpg", AssetType.DETAIL_IMAGE.value),  # 非法协议必须跳过
    ]
    saved = await SourceService._download_assets(
        session, adapter=_adapter(), product=_Product(), items=items
    )
    assert saved == 3, "dup 与主图同字节应被去重，ftp 应被跳过"

    rows = (
        (await session.execute(select(Asset).where(Asset.source_product_id == 4242))).scalars().all()
    )
    assert len(rows) == 3
    types = [row.asset_type for row in rows]
    assert types.count(AssetType.MAIN_IMAGE.value) == 1
    assert types.count(AssetType.DETAIL_IMAGE.value) == 2
    assert {row.origin for row in rows} == {AssetOrigin.RAW.value}

    for row in rows:
        path = Path(row.storage_path)
        assets_dir = path.parent
        assert path.exists(), f"素材未真实落盘：{path}"
        assert path.read_bytes()
    assert assets_dir is not None and "9876543210" in str(assets_dir)

    main_row = next(row for row in rows if row.asset_type == AssetType.MAIN_IMAGE.value)
    assert main_row.origin_url == "https://x.test/main.jpg"
    assert main_row.storage_path.endswith("main_00.jpg")
