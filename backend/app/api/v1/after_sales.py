"""售后（ARCH §5.5.10）。

★ 用户决策 ③：1688 采购全委托妙手 / 逸淘。
  `LocalCsvAdapter` **永久禁止**真实下单、密文面单与地址解密 ——
  因此 `submit_refund` / `return_address` 在本地兜底下返回 **503**（能力不支持），
  **绝不生成假的「已提交」待办**（静默假成功是明令禁止的失效模式）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.api.v1._common import page_of
from app.core.deps import CurrentOperator, DbSession
from app.core.pagination import PageParams, page_params
from app.core.response import ApiResponse
from app.schemas.common import parse_money
from app.schemas.order import (
    AfterSaleRefundRequest,
    AfterSaleResponsibilityUpdate,
    AfterSaleVo,
)
from app.services.after_sale_service import AfterSaleService

router = APIRouter(tags=["售后"])

__all__ = ["router"]


@router.get("/after-sales", summary="售后列表")
async def list_after_sales(
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    order_id: Annotated[int | None, Query()] = None,
    handling_status: Annotated[str | None, Query()] = None,
    responsibility: Annotated[str | None, Query()] = None,
) -> ApiResponse[Any]:
    """分页返回售后单。"""
    rows, total = await AfterSaleService.list_after_sales(
        session,
        order_id=order_id,
        handling_status=handling_status,
        responsibility=responsibility,
        page=params.page,
        page_size=params.page_size,
    )
    return ApiResponse.ok(data=page_of([AfterSaleVo.from_model(r) for r in rows], total, params))


@router.get("/after-sales/{after_sale_id}", summary="售后详情")
async def get_after_sale(
    after_sale_id: int, session: DbSession
) -> ApiResponse[AfterSaleVo]:
    """售后详情。"""
    row = await AfterSaleService.get(session, after_sale_id)
    return ApiResponse.ok(data=AfterSaleVo.from_model(row))


@router.post("/after-sales/{after_sale_id}/submit-refund", summary="提交 1688 退款")
async def submit_refund(
    after_sale_id: int,
    payload: AfterSaleRefundRequest,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """提交退款到本地兜底 → **503**（需切换妙手 / 逸淘）。"""
    amount_cents = parse_money(payload.refund_amount, field_name="退款金额")
    result = await AfterSaleService.submit_refund(
        session,
        after_sale_id,
        refund_amount_cents=amount_cents,
        reason=payload.reason or "",
        operator=operator.name,
    )
    await session.commit()
    return ApiResponse.ok(data=result)


@router.post("/after-sales/{after_sale_id}/return-address", summary="获取退货地址")
async def return_address(
    after_sale_id: int,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[dict[str, Any]]:
    """获取退货地址（涉及密文解密，本地兜底 **503**）。"""
    result = await AfterSaleService.fetch_return_address(
        session, after_sale_id, operator=operator.name
    )
    await session.commit()
    return ApiResponse.ok(data=result)


@router.put("/after-sales/{after_sale_id}/responsibility", summary="判定责任方")
async def update_responsibility(
    after_sale_id: int,
    payload: AfterSaleResponsibilityUpdate,
    session: DbSession,
    operator: CurrentOperator,
) -> ApiResponse[AfterSaleVo]:
    """责任方：our_shop / supplier / buyer / platform。"""
    row = await AfterSaleService.update_responsibility(
        session,
        after_sale_id,
        responsibility=payload.responsibility,
        note=payload.note or "",
        operator=operator.name,
    )
    await session.commit()
    return ApiResponse.ok(data=AfterSaleVo.from_model(row), message="责任方已判定")
