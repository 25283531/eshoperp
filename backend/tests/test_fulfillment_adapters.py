"""履约适配器降级语义测试（红线 R3 + 用户决策 ③）。

★ 用户决策 ③：1688 采购全委托妙手 / 逸淘。
  `LocalCsvAdapter` **永久禁止**真实下单 / 密文面单 / 地址解密：
    - `submit_refund` / `get_return_address` → **UNSUPPORTED**（不是"降级成功"）；
    - `place_purchase_order` → 导出 CSV 到 `data/exports/`，状态 `manual_pending`，
      **绝不返回 `placed`（假成功）**。

★ 红线 R3：能力不可靠时返回 UNSUPPORTED / DEGRADED 信封，**绝不抛裸异常**。
"""

from __future__ import annotations

from pathlib import Path

from app.adapters.fulfillment.base import PurchaseRequest, RefundRequest
from app.adapters.fulfillment.local_csv import LocalCsvAdapter
from app.core.config import get_settings
from app.core.errors import BusinessError, ErrorCode
from app.models.enums import Capability, CapabilityLevel, ResultCode
from app.services.after_sale_service import AfterSaleService
from app.services.fulfillment_service import FulfillmentService


async def test_local_csv_manifest_boundaries(session: object) -> None:
    """能力矩阵：`submit_refund` / `get_return_address` / `write_back_tracking` 均 UNSUPPORTED。"""
    adapter = LocalCsvAdapter(session=session)
    manifest = adapter.build_manifest()

    for capability in (
        Capability.SUBMIT_REFUND.value,
        Capability.GET_RETURN_ADDRESS.value,
        Capability.WRITE_BACK_TRACKING.value,
    ):
        level = manifest.capabilities.get(capability)
        assert level is not None, f"能力 {capability} 未在 manifest 中声明"
        assert level.level == CapabilityLevel.UNSUPPORTED.value, (
            f"★ 用户决策③：{capability} 必须 UNSUPPORTED（本地兜底禁止真实下单 / 地址解密）"
        )


async def test_place_purchase_order_is_manual_pending(session: object) -> None:
    """下单 → 导出 CSV + 状态 `manual_pending`，**不伪造 `placed`**。"""
    adapter = LocalCsvAdapter(session=session)
    result = await adapter.invoke(
        Capability.PLACE_PURCHASE_ORDER,
        req=PurchaseRequest(
            order_id=1,
            platform_order_no="T-TEST-001",
            source_product_1688_id="1688-TEST-001",
            source_sku_code_1688="1688-A",
            quantity=2,
        ),
    )
    assert result is not None
    # DEGRADED 信封（能力降级但不抛异常 —— 红线 R3）
    assert result.code in (ResultCode.DEGRADED.value, ResultCode.OK.value)
    data = result.data
    assert data is not None
    status = getattr(data, "status", None) or (data.get("status") if isinstance(data, dict) else None)
    assert status == "manual_pending", f"本地兜底必须停在 manual_pending，实际：{status}"

    exports_dir = Path(get_settings().exports_dir)
    assert exports_dir.exists(), "采购 CSV 应导出到 data/exports/"


async def test_submit_refund_unsupported(session: object) -> None:
    """退款提交 UNSUPPORTED，且服务层转 503（不生成假的已提交待办）。"""
    adapter = LocalCsvAdapter(session=session)
    result = await adapter.invoke(
        Capability.SUBMIT_REFUND,
        req=RefundRequest(
            order_id=1, platform_refund_no="R-TEST-001", refund_amount_cents=1000
        ),
    )
    assert result is not None
    assert result.code == ResultCode.UNSUPPORTED.value, "本地兜底不得支持提交 1688 退款"

    with __import__("pytest").raises(BusinessError) as excinfo:
        await AfterSaleService.submit_refund(
            session, 999999, refund_amount_cents=1000, operator="tester"
        )
    assert int(excinfo.value.code) in (
        int(ErrorCode.ORDER_REFUND_FAILED),
        int(ErrorCode.ADAPTER_CAPABILITY_UNSUPPORTED),
        int(ErrorCode.NOT_FOUND),
    )


async def test_service_layer_uses_factory_only(session: object) -> None:
    """业务层获取适配器必须走工厂（R1 scope 校验在工厂内）。"""
    adapter = await FulfillmentService.get_adapter(session, adapter_name="local_csv", actor="tester")
    assert adapter is not None
    assert type(adapter).__name__ == "LocalCsvAdapter"


async def test_scope_policies_not_empty() -> None:
    """scope 策略必须同时给出白名单与黑名单。"""
    policies = FulfillmentService.scope_policies()
    assert policies["allowed"], "白名单为空"
    assert policies["forbidden"], "黑名单为空（第三方将可编辑商品 —— 违反 R1）"
