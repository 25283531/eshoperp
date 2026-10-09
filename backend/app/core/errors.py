"""统一错误码体系与异常基类。

约定（§10.3）：
    * 错误码为 4 位整数，按模块分段；`0` 表示成功；
    * HTTP 状态码与业务 code 分离：HTTP 表达协议语义，code 表达业务语义；
    * 所有业务异常必须继承 `BusinessError`，由全局异常处理器统一转成 `ApiResponse`。
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger, get_trace_id
from app.core.response import ApiResponse

logger = get_logger(__name__)

# ★★ QA-01 / QA-02：唯一约束冲突必须翻译成**可读的业务错误**（409 / 1006）★★
#
# 背景：`one_to_many`（一平台 SKU → 多货源 SKU）与 `duplicate_item`
# （同一 shop_sku_code 挂多个 shop_item_id）这两类冲突，
# 由部分唯一索引 `uq_sku_mapping_shop_sku` 在**写入边界**结构性阻断；
# 恒空的读时扫描 SQL 已移除（详见 `app/models/mapping.py` 头部注释）。
#
# 那么"撞键"就成了这两类冲突**唯一的**表现形式：
#   - 让它漏成 500 → 运营看到"服务器内部错误"，不知道自己哪里做错了；
#   - 翻译成 409 + 中文文案 → 运营立刻知道"这个店铺 SKU 已经有映射了"。
# 因此这里做**全局兜底**：任何 Service 漏抓的 IntegrityError 都不会变成 500。
UNIQUE_CONSTRAINT_MESSAGES: dict[str, str] = {
    "uq_sku_mapping_shop_sku": (
        "该店铺 SKU 已存在有效映射（同一 平台+店铺+SKU 编码 只能有一条），"
        "若要改绑货源请直接编辑原映射，不要重复新建"
    ),
    "uq_platform_account_platform_shop": "该平台的这个店铺账号已存在，请勿重复录入",
    "uq_credential_owner_key": "该凭证项已存在（同一 归属类型+归属键+凭证键 只能有一条）",
    "uq_erp_order_platform_no": "该平台订单号已存在（订单幂等去重，无需重复同步）",
    "uq_task_record_active_key": "同键任务已在处理中，请勿重复提交",
    "uq_listing_product_platform_shop_item": "该平台商品已存在（同一 平台+店铺+商品ID 只能有一条）",
    "uq_listing_sku_product_code": "该商品下已存在同名 SKU 编码",
    "uq_source_product_1688_id": "该 1688 商品已采集过",
}


def describe_integrity_error(exc: Exception) -> str:
    """把 `IntegrityError` 翻译成一句运营能看懂的中文（含兜底文案）。"""
    detail = str(getattr(exc, "orig", exc) or exc)
    for name, message in UNIQUE_CONSTRAINT_MESSAGES.items():
        if name in detail:
            return message
    if "UNIQUE constraint failed" in detail or "duplicate key" in detail.lower():
        return "数据与已有记录冲突（违反唯一约束），请检查是否重复提交"
    if "FOREIGN KEY constraint failed" in detail or "foreign key" in detail.lower():
        return "关联的数据不存在（外键约束），请先创建被引用的记录"
    if "NOT NULL constraint failed" in detail or "not-null" in detail.lower():
        return "必填字段为空（非空约束），请补齐后重试"
    return "数据写入违反数据库约束，请检查输入后重试"


class ErrorCode(IntEnum):
    """业务错误码（4 位，按模块分段）。"""

    OK = 0

    # ---------- 1xxx 通用 ----------
    PARAM_ERROR = 1001
    UNAUTHORIZED = 1002
    FORBIDDEN = 1003
    NOT_FOUND = 1004
    STATE_CONFLICT = 1005
    UNIQUE_CONFLICT = 1006
    RATE_LIMITED = 1007
    # ★ 异步任务入队失败（1098）：受理了业务单、却没真正投递到任务队列。
    #   这类失败此前被 `except Exception: return None` 吞掉，接口照样返回 202，
    #   前端看到"受理成功"而任务永远不会跑 —— 必须独立成码，让它**可观测**。
    TASK_SUBMIT_FAILED = 1098
    INTERNAL_ERROR = 1099

    # ---------- 2xxx 货源与素材 ----------
    SOURCE_COLLECT_FAILED = 2001
    SOURCE_PRODUCT_NOT_FOUND = 2002
    SOURCE_QUOTA_EXCEEDED = 2003
    SOURCE_SPEC_PARSE_FAILED = 2004
    ASSET_NOT_FOUND = 2101
    ASSET_ROLLBACK_FAILED = 2102

    # ---------- 3xxx SKU 映射 ----------
    MAPPING_MISSING = 3001
    MAPPING_CONFLICT = 3002
    MAPPING_STATUS_INVALID = 3003
    MAPPING_DUPLICATE = 3004
    MAPPING_COST_INVALID = 3005
    MAPPING_DELETED = 3006
    MAPPING_SPEC_MISMATCH = 3007

    # ---------- 4xxx 上架 ----------
    PUBLISH_PRECHECK_FAILED = 4001
    PUBLISH_PLATFORM_FAILED = 4002
    PUBLISH_UNKNOWN_PLATFORM_ERROR = 4003
    PUBLISH_MOCK_RESTRICTED = 4004
    PUBLISH_ASSET_NOT_APPROVED = 4005
    PUBLISH_ITEM_ID_DUPLICATE = 4006
    PUBLISH_MODE_UNAVAILABLE = 4007

    # ---------- 5xxx 履约适配层 ----------
    ADAPTER_UNAVAILABLE = 5001
    ADAPTER_CAPABILITY_UNSUPPORTED = 5002
    ADAPTER_SCOPE_DENIED = 5003
    ADAPTER_PURCHASE_FAILED = 5004
    ADAPTER_WRITEBACK_FAILED = 5005
    ADAPTER_HEALTH_CHECK_FAILED = 5006
    ADAPTER_CONFIG_MISSING = 5007

    # ---------- 6xxx 订单与售后 ----------
    ORDER_MATCH_FAILED = 6001
    ORDER_ILLEGAL_TRANSITION = 6002
    ORDER_REFUND_FAILED = 6003
    ORDER_RETURN_ADDRESS_FAILED = 6004
    ORDER_TRACKING_INVALID = 6005

    # ---------- 7xxx 库存 ----------
    INVENTORY_SNAPSHOT_MISSING = 7001
    INVENTORY_OFFLINE_FAILED = 7002
    INVENTORY_RELIST_BLOCKED = 7003

    # ---------- 8xxx 系统 ----------
    SETTING_NOT_FOUND = 8001
    CREDENTIAL_DECRYPT_FAILED = 8002
    CREDENTIAL_EXPIRED = 8003
    PROFILE_FIELD_UNVERIFIED = 8004

    def describe(self) -> str:
        """返回错误码的中文默认描述。"""
        return ERROR_MESSAGES.get(int(self), "未知错误")


ERROR_MESSAGES: dict[int, str] = {
    0: "ok",
    1001: "参数错误",
    1002: "未认证",
    1003: "禁止访问",
    1004: "资源不存在",
    1005: "状态冲突：非法的状态迁移",
    1006: "唯一约束冲突",
    1007: "请求过于频繁，已限流",
    1098: "异步任务入队失败：业务单已受理，但任务未真正投递到队列（请重试）",
    1099: "服务器内部错误",
    2001: "1688 采集失败",
    2002: "货源商品不存在",
    2003: "采集配额超限",
    2004: "SKU 规格树解析失败",
    2101: "素材不存在",
    2102: "素材版本回滚失败",
    3001: "SKU 映射缺失",
    3002: "SKU 映射冲突",
    3003: "SKU 映射状态无效",
    3004: "重复映射",
    3005: "采购成本异常（为空 / 为 0 / 为负）",
    3006: "映射已被软删除",
    3007: "规格指纹不匹配，映射需重新确认",
    4001: "合规预检失败",
    4002: "平台发布失败",
    4003: "未知平台错误码",
    4004: "Mock 模式限制：禁止真实履约",
    4005: "素材未审核通过，禁止上架",
    4006: "商品 ID 回填重复",
    4007: "上架模式不可用（未取得平台资质）",
    5001: "适配器不可用",
    5002: "适配器能力不支持，已降级处理",
    5003: "越权 scope 被拒绝：第三方不得持有商品编辑权",
    5004: "采购下单失败",
    5005: "物流单号回填失败",
    5006: "适配器连通性自检失败",
    5007: "适配器配置缺失",
    6001: "SKU 匹配失败",
    6002: "订单状态非法迁移",
    6003: "退款提交失败",
    6004: "退货地址获取失败",
    6005: "物流单号格式校验失败",
    7001: "库存/价格快照数据缺失",
    7002: "自动下架失败",
    7003: "重新上架被映射校验拦截",
    8001: "配置项不存在",
    8002: "凭证解密失败",
    8003: "凭证已过期",
    8004: "适配器 profile 字段未实测确认（TODO）",
}

# HTTP 状态码 ↔ 业务 code 的默认映射（§10.3）
HTTP_STATUS_TO_CODE: dict[int, ErrorCode] = {
    400: ErrorCode.PARAM_ERROR,
    401: ErrorCode.UNAUTHORIZED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    409: ErrorCode.UNIQUE_CONFLICT,
    422: ErrorCode.PARAM_ERROR,
    429: ErrorCode.RATE_LIMITED,
    500: ErrorCode.INTERNAL_ERROR,
    503: ErrorCode.ADAPTER_UNAVAILABLE,
}


class BusinessError(Exception):
    """业务异常基类。

    Attributes:
        code: 业务错误码，取 `ErrorCode`。
        message: 可直接展示给前端的中文提示。
        http_status: 对应的 HTTP 状态码。
        detail: 结构化补充信息（写入响应 `data`）。
    """

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    http_status: int = status.HTTP_400_BAD_REQUEST

    def __init__(
        self,
        message: str | None = None,
        *,
        code: ErrorCode | int | None = None,
        http_status: int | None = None,
        detail: Any = None,
    ) -> None:
        if code is not None:
            self.code = ErrorCode(int(code))
        self.message = message or ERROR_MESSAGES.get(int(self.code), "业务异常")
        if http_status is not None:
            self.http_status = http_status
        self.detail = detail
        super().__init__(self.message)

    def to_response(self, trace_id: str = "") -> ApiResponse[Any]:
        """转换为统一响应体。"""
        return ApiResponse[Any](code=int(self.code), message=self.message, data=self.detail, trace_id=trace_id)

    def __str__(self) -> str:
        return f"[{int(self.code)}] {self.message}"


class ParamError(BusinessError):
    """参数校验失败（1001 / 400）。"""

    code = ErrorCode.PARAM_ERROR
    http_status = status.HTTP_400_BAD_REQUEST


class UnauthorizedError(BusinessError):
    """未认证（1002 / 401）。"""

    code = ErrorCode.UNAUTHORIZED
    http_status = status.HTTP_401_UNAUTHORIZED


class ForbiddenError(BusinessError):
    """禁止访问（1003 / 403）。"""

    code = ErrorCode.FORBIDDEN
    http_status = status.HTTP_403_FORBIDDEN


class NotFoundError(BusinessError):
    """资源不存在（1004 / 404）。"""

    code = ErrorCode.NOT_FOUND
    http_status = status.HTTP_404_NOT_FOUND


class StateConflictError(BusinessError):
    """状态机非法迁移（1005 / 409）。"""

    code = ErrorCode.STATE_CONFLICT
    http_status = status.HTTP_409_CONFLICT


class UniqueConflictError(BusinessError):
    """唯一约束冲突（1006 / 409）。"""

    code = ErrorCode.UNIQUE_CONFLICT
    http_status = status.HTTP_409_CONFLICT


class RateLimitedError(BusinessError):
    """限流（1007 / 429）。"""

    code = ErrorCode.RATE_LIMITED
    http_status = status.HTTP_429_TOO_MANY_REQUESTS


class InternalError(BusinessError):
    """服务器内部错误（1099 / 500）。"""

    code = ErrorCode.INTERNAL_ERROR
    http_status = status.HTTP_500_INTERNAL_SERVER_ERROR


class AdapterScopeDeniedError(BusinessError):
    """★ 红线 R1：越权 scope 被拒（5003 / 403）。"""

    code = ErrorCode.ADAPTER_SCOPE_DENIED
    http_status = status.HTTP_403_FORBIDDEN


def _error_json(code: int, message: str, data: Any, trace_id: str, http_status: int) -> JSONResponse:
    """构造统一错误响应。"""
    payload = ApiResponse[Any](code=code, message=message, data=data, trace_id=trace_id)
    return JSONResponse(status_code=http_status, content=payload.model_dump(mode="json"))


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器，保证任何异常都以 `{code,message,data,trace_id}` 返回。"""

    @app.exception_handler(BusinessError)
    async def _handle_business_error(request: Request, exc: BusinessError) -> JSONResponse:  # noqa: ANN202
        trace_id = get_trace_id()
        logger.warning(
            "business_error",
            extra={"code": int(exc.code), "path": request.url.path, "trace_id": trace_id, "message": exc.message},
        )
        return _error_json(int(exc.code), exc.message, exc.detail, trace_id, exc.http_status)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:  # noqa: ANN202
        trace_id = get_trace_id()
        errors = [
            {"loc": list(e.get("loc", [])), "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in exc.errors()
        ]
        return _error_json(
            int(ErrorCode.PARAM_ERROR),
            "参数校验失败",
            {"errors": errors},
            trace_id,
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:  # noqa: ANN202
        trace_id = get_trace_id()
        code = HTTP_STATUS_TO_CODE.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        return _error_json(int(code), str(exc.detail), None, trace_id, exc.status_code)

    @app.exception_handler(IntegrityError)
    async def _handle_integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:  # noqa: ANN202
        """★ QA-01 / QA-02：唯一约束冲突 → 409 / 1006（绝不让 IntegrityError 漏成 500）。"""
        trace_id = get_trace_id()
        message = describe_integrity_error(exc)
        logger.warning(
            "integrity_error_translated",
            extra={"path": request.url.path, "trace_id": trace_id, "error": str(exc)[:200]},
        )
        return _error_json(
            int(ErrorCode.UNIQUE_CONFLICT),
            message,
            {"constraint_error": str(exc)[:300]},
            trace_id,
            status.HTTP_409_CONFLICT,
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:  # noqa: ANN202
        trace_id = get_trace_id()
        logger.exception(
            "unhandled_exception",
            extra={"path": request.url.path, "trace_id": trace_id, "error": type(exc).__name__},
        )
        return _error_json(
            int(ErrorCode.INTERNAL_ERROR),
            ERROR_MESSAGES[int(ErrorCode.INTERNAL_ERROR)],
            None,
            trace_id,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


__all__ = [
    "AdapterScopeDeniedError",
    "BusinessError",
    "ErrorCode",
    "ERROR_MESSAGES",
    "ForbiddenError",
    "HTTP_STATUS_TO_CODE",
    "InternalError",
    "NotFoundError",
    "ParamError",
    "RateLimitedError",
    "StateConflictError",
    "UNIQUE_CONSTRAINT_MESSAGES",
    "UnauthorizedError",
    "UniqueConflictError",
    "describe_integrity_error",
    "register_exception_handlers",
]
