"""全系统枚举集中定义（§10.1）。

★ 任何枚举只允许定义在此文件，禁止在各模块散落定义，避免前后端枚举漂移。
所有枚举值均为蛇形小写字符串，DB / API 层统一。
"""

from __future__ import annotations

from enum import Enum


class _StrEnum(str, Enum):
    """字符串枚举基类：`Model.status == Status.VALID` 与 `"valid"` 均可比较。"""

    def __str__(self) -> str:  # noqa: D105
        return str(self.value)

    @classmethod
    def values(cls) -> list[str]:
        """返回全部枚举值列表。"""
        return [item.value for item in cls]


# ============================================================================
#  平台与上架
# ============================================================================


class Platform(_StrEnum):
    """销售平台。"""

    TAOBAO = "taobao"
    DOUYIN = "douyin"
    PDD = "pdd"


class ListingMode(_StrEnum):
    """上架模式。"""

    REAL = "real"  # 真实平台 API（需资质，见 PRD Q1）
    MOCK = "mock"  # ★ MVP 默认：返回模拟 ID，is_mock=True
    MANUAL = "manual"  # 半自动：素材包 + 预填表单，人工发布后回填


class ListingProductStatus(_StrEnum):
    """平台商品状态。"""

    ON_SALE = "on_sale"
    OFF_SHELF = "off_shelf"
    PUBLISHING = "publishing"
    FAILED = "failed"


class ListingSkuStatus(_StrEnum):
    """平台 SKU 状态。"""

    ON_SALE = "on_sale"
    OFF_SHELF = "off_shelf"


class PlatformAccountStatus(_StrEnum):
    """平台账号授权状态。"""

    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


class SourcePlatform(_StrEnum):
    """货源平台（PlatformAccount.platform 可取值范围）。"""

    ALIBABA1688 = "alibaba1688"
    TAOBAO = "taobao"
    DOUYIN = "douyin"
    PDD = "pdd"


# ============================================================================
#  货源
# ============================================================================


class SupplierStatus(_StrEnum):
    """供应商状态。"""

    ACTIVE = "active"
    INACTIVE = "inactive"
    BLACKLIST = "blacklist"


class SourceStatus(_StrEnum):
    """货源商品 / SKU 状态。"""

    ON_SALE = "on_sale"
    OFF_SHELF = "off_shelf"
    OUT_OF_STOCK = "out_of_stock"


# ============================================================================
#  素材与 AI
# ============================================================================


class AssetType(_StrEnum):
    """素材类型。"""

    MAIN_IMAGE = "main_image"
    DETAIL_IMAGE = "detail_image"
    VIDEO = "video"


class AssetOrigin(_StrEnum):
    """素材来源。"""

    RAW = "raw"  # 1688 原始
    AI_REWORK = "ai_rework"  # AI 重构


class AiTaskStatus(_StrEnum):
    """AI 重构任务状态（§7.3）。"""

    QUEUED = "queued"
    RUNNING = "running"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ReviewStatus(_StrEnum):
    """重构结果审核状态；未 approved 禁止上架（AIR-P0-03）。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class AiReworkItem(_StrEnum):
    """AI 重构项。"""

    MAIN_IMAGE = "main_image"
    DETAIL_IMAGE = "detail_image"
    TITLE = "title"
    ATTRIBUTE = "attribute"


# ============================================================================
#  SKU 映射（最高等级资产）
# ============================================================================


class MappingStatus(_StrEnum):
    """映射状态。"""

    VALID = "valid"  # 有效（唯一允许参与上架与订单匹配）
    PENDING_CONFIRM = "pending_confirm"  # 待确认（货源端变更未确认 → 订单挂起）
    INVALID = "invalid"  # 失效
    ARCHIVED = "archived"  # 归档


class MappingSource(_StrEnum):
    """映射来源。"""

    MANUAL = "manual"
    SYSTEM = "system"
    THIRD_PARTY = "third_party"
    AUTO_PUBLISH = "auto_publish"


class ConflictType(_StrEnum):
    """映射冲突类型（PRD MAP-P0-03 / ARCH §4.3.1 六类）。

    ★★ 命名语义修正（v1.8 对齐，务必按此理解）★★
    ------------------------------------------------------------------------
    早期实现把「同店铺内一个货源 SKU 被多个店铺 SKU 引用」误命名为 `many_to_one`（P0），
    把「跨平台铺货」命名为 `many_to_one_info`（P1）。这与架构文档 §4.3.1 相反，
    且导致「先跑并剔除」的排序约束无法被正确表达（文档要求先跑的是**跨平台**那条）。

    现按 ARCH v1.8 §4.3.1 唯一真源修正为：
        * `many_to_one`  = **跨平台铺货** = P1 = **不拦截**（这才是要「先跑并剔除」的那条）
        * `duplicate`    = 同店铺内的反向重复 = P0 = 拦截（含 ②a / ②b 两个变体）
    ------------------------------------------------------------------------
    """

    ONE_TO_MANY = "one_to_many"  # ① 一平台 SKU → 多货源 SKU（P0，拦截）
    DUPLICATE = "duplicate"  # ② 重复映射（P0，拦截）：②a 同店铺内反向重复 / ②b 同 SKU 挂多商品
    MANY_TO_ONE = "many_to_one"  # ③ 跨平台铺货：一货源 SKU → 多平台店铺 SKU（P1，**永不拦截**）
    COST_INVALID = "cost_invalid"  # ④ 采购成本为空 / 0 / 负（P0，拦截）
    COST_UNDERWATER = "cost_underwater"  # ⑥ 成本倒挂：镜像成本 ≥ 平台售价（P1，可配升 P0）
    SPEC_MISMATCH = "spec_mismatch"  # ⑤ 规格指纹不匹配（P0，拦截）


class ConflictLevel(_StrEnum):
    """冲突级别：P0 禁止上架，P1 仅提示。"""

    P0 = "P0"
    P1 = "P1"


class ChangeAction(_StrEnum):
    """映射变更动作。"""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    RESTORE = "restore"
    STATUS_CHANGE = "status_change"
    PUSH = "push"


class ChangeSource(_StrEnum):
    """变更来源。"""

    MANUAL = "manual"
    SYSTEM = "system"
    THIRD_PARTY = "third_party"


class ResolveAction(_StrEnum):
    """冲突解决方式。"""

    MANUAL_FIX = "manual_fix"
    AUTO_FIX = "auto_fix"
    IGNORED = "ignored"


class PushStatus(_StrEnum):
    """映射推送状态。"""

    SUCCESS = "success"
    FAILED = "failed"
    DEGRADED = "degraded"


# ============================================================================
#  上架任务
# ============================================================================


class PublishStatus(_StrEnum):
    """上架任务状态机（§7.1）。"""

    PENDING_PRECHECK = "pending_precheck"
    PRECHECK_FAILED = "precheck_failed"
    PENDING_VALIDATE = "pending_validate"
    VALIDATE_FAILED = "validate_failed"
    PENDING_PUBLISH = "pending_publish"
    PUBLISHING = "publishing"
    PUBLISH_SUCCESS = "publish_success"
    PUBLISH_FAILED = "publish_failed"
    OFFLINE = "offline"
    CANCELLED = "cancelled"


PUBLISH_TRANSITIONS: dict[str, list[str]] = {
    PublishStatus.PENDING_PRECHECK.value: [
        PublishStatus.PRECHECK_FAILED.value,
        PublishStatus.PENDING_VALIDATE.value,
        PublishStatus.CANCELLED.value,
    ],
    PublishStatus.PRECHECK_FAILED.value: [PublishStatus.PENDING_PRECHECK.value],
    PublishStatus.PENDING_VALIDATE.value: [
        PublishStatus.VALIDATE_FAILED.value,
        PublishStatus.PENDING_PUBLISH.value,
        PublishStatus.CANCELLED.value,
    ],
    PublishStatus.VALIDATE_FAILED.value: [PublishStatus.PENDING_VALIDATE.value],
    PublishStatus.PENDING_PUBLISH.value: [
        PublishStatus.PUBLISHING.value,
        PublishStatus.CANCELLED.value,
    ],
    PublishStatus.PUBLISHING.value: [
        PublishStatus.PUBLISH_SUCCESS.value,
        PublishStatus.PUBLISH_FAILED.value,
    ],
    PublishStatus.PUBLISH_FAILED.value: [
        PublishStatus.PENDING_PUBLISH.value,
        PublishStatus.CANCELLED.value,
    ],
    PublishStatus.PUBLISH_SUCCESS.value: [PublishStatus.OFFLINE.value],
    PublishStatus.OFFLINE.value: [PublishStatus.PENDING_PUBLISH.value],
    PublishStatus.CANCELLED.value: [],
}


# ============================================================================
#  订单与履约
# ============================================================================


class OrderFulfillmentStatus(_StrEnum):
    """订单履约状态机（§7.2）。"""

    PENDING_MATCH = "pending_match"
    EXCEPTION_UNMATCHED = "exception_unmatched"
    MATCHED = "matched"
    EXCEPTION_PURCHASE_FAILED = "exception_purchase_failed"
    PURCHASED = "purchased"
    EXCEPTION_OUT_OF_STOCK = "exception_out_of_stock"
    SHIPPED = "shipped"
    EXCEPTION_WRITEBACK_FAILED = "exception_writeback_failed"
    COMPLETED = "completed"
    AFTER_SALE = "after_sale"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"


ORDER_TRANSITIONS: dict[str, list[str]] = {
    OrderFulfillmentStatus.PENDING_MATCH.value: [
        OrderFulfillmentStatus.MATCHED.value,
        OrderFulfillmentStatus.EXCEPTION_UNMATCHED.value,
    ],
    OrderFulfillmentStatus.EXCEPTION_UNMATCHED.value: [
        OrderFulfillmentStatus.MATCHED.value,
        OrderFulfillmentStatus.CANCELLED.value,
    ],
    OrderFulfillmentStatus.MATCHED.value: [
        OrderFulfillmentStatus.PURCHASED.value,
        OrderFulfillmentStatus.EXCEPTION_PURCHASE_FAILED.value,
    ],
    OrderFulfillmentStatus.EXCEPTION_PURCHASE_FAILED.value: [
        OrderFulfillmentStatus.MATCHED.value,
        OrderFulfillmentStatus.CANCELLED.value,
    ],
    OrderFulfillmentStatus.PURCHASED.value: [
        OrderFulfillmentStatus.SHIPPED.value,
        OrderFulfillmentStatus.EXCEPTION_OUT_OF_STOCK.value,
    ],
    OrderFulfillmentStatus.EXCEPTION_OUT_OF_STOCK.value: [
        OrderFulfillmentStatus.MATCHED.value,
        OrderFulfillmentStatus.CANCELLED.value,
    ],
    OrderFulfillmentStatus.SHIPPED.value: [
        OrderFulfillmentStatus.COMPLETED.value,
        OrderFulfillmentStatus.EXCEPTION_WRITEBACK_FAILED.value,
    ],
    OrderFulfillmentStatus.EXCEPTION_WRITEBACK_FAILED.value: [OrderFulfillmentStatus.SHIPPED.value],
    OrderFulfillmentStatus.COMPLETED.value: [OrderFulfillmentStatus.AFTER_SALE.value],
    OrderFulfillmentStatus.AFTER_SALE.value: [
        OrderFulfillmentStatus.REFUNDED.value,
        OrderFulfillmentStatus.COMPLETED.value,
    ],
    OrderFulfillmentStatus.REFUNDED.value: [],
    OrderFulfillmentStatus.CANCELLED.value: [],
}

TERMINAL_ORDER_STATUSES: frozenset[str] = frozenset(
    {
        OrderFulfillmentStatus.COMPLETED.value,
        OrderFulfillmentStatus.REFUNDED.value,
        OrderFulfillmentStatus.CANCELLED.value,
    }
)

EXCEPTION_ORDER_STATUSES: frozenset[str] = frozenset(
    {
        OrderFulfillmentStatus.EXCEPTION_UNMATCHED.value,
        OrderFulfillmentStatus.EXCEPTION_PURCHASE_FAILED.value,
        OrderFulfillmentStatus.EXCEPTION_OUT_OF_STOCK.value,
        OrderFulfillmentStatus.EXCEPTION_WRITEBACK_FAILED.value,
    }
)


class MatchStatus(_StrEnum):
    """SKU 匹配状态。"""

    MATCHED = "matched"
    UNMATCHED = "unmatched"
    PENDING_CONFIRM = "pending_confirm"


class ExceptionType(_StrEnum):
    """订单异常类型。"""

    UNMATCHED = "unmatched"
    ADDRESS_ERROR = "address_error"
    DECRYPT_FAILED = "decrypt_failed"
    PURCHASE_FAILED = "purchase_failed"
    WRITEBACK_FAILED = "writeback_failed"
    OUT_OF_STOCK = "out_of_stock"


class HandlingAction(_StrEnum):
    """订单处置动作。"""

    RETRY = "retry"
    SWITCH_SOURCE = "switch_source"
    REFUND = "refund"
    IGNORE = "ignore"


class PurchaseStatus(_StrEnum):
    """采购单状态。

    ★ `manual_pending`：本地兜底（LocalCsvAdapter）**不具备真实 1688 下单能力**，
      下单结果一律落为「待人工下单」+ 导出采购清单 CSV，由人工去 1688 完成后回填单号。
      （用户决策 ③：本地兜底禁止真实下单 / 密文面单 / 地址解密）
    """

    PENDING = "pending"
    MANUAL_PENDING = "manual_pending"  # ★ 待人工到 1688 下单（本地兜底专用）
    PLACED = "placed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WriteBackStatus(_StrEnum):
    """物流回填状态。"""

    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"


class Refund1688Status(_StrEnum):
    """1688 退款状态。"""

    PENDING = "pending"
    SUBMITTED = "submitted"
    SUCCESS = "success"
    FAILED = "failed"


class ReturnAddressPushStatus(_StrEnum):
    """退货地址推送状态。"""

    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"


class Responsibility(_StrEnum):
    """售后责任归属。"""

    OUR_SHOP = "our_shop"
    SUPPLIER = "supplier"
    BUYER = "buyer"
    PLATFORM = "platform"


class AfterSaleHandlingStatus(_StrEnum):
    """售后处理状态。"""

    PROCESSING = "processing"
    DONE = "done"
    CLOSED = "closed"


# ============================================================================
#  履约适配器
# ============================================================================


class AdapterName(_StrEnum):
    """履约适配器名称。"""

    MIAOSHOU = "miaoshou"
    YITAO = "yitao"
    LOCAL_CSV = "local_csv"


class Capability(_StrEnum):
    """履约适配层 8 项能力（PRD FUL-P0-01）。"""

    FETCH_ORDERS = "fetch_orders"
    MATCH_SKU = "match_sku"
    PLACE_PURCHASE_ORDER = "place_purchase_order"
    FETCH_TRACKING_NO = "fetch_tracking_no"
    WRITE_BACK_TRACKING = "write_back_tracking"
    SUBMIT_REFUND = "submit_refund"
    GET_RETURN_ADDRESS = "get_return_address"
    PUSH_INVENTORY_CHANGE = "push_inventory_change"


class CapabilityLevel(_StrEnum):
    """能力支持级别。"""

    SUPPORTED = "supported"  # 原生支持，直接调用
    DEGRADED = "degraded"  # 名义支持但实为人工 / CSV 中转，结果可用但延迟高
    UNSUPPORTED = "unsupported"  # 不支持 → 返回 UNSUPPORTED，由调度层降级


class ResultCode(_StrEnum):
    """适配器统一结果码（★ 绝不抛裸异常）。"""

    OK = "OK"
    UNSUPPORTED = "UNSUPPORTED"
    DEGRADED = "DEGRADED"
    RETRYABLE = "RETRYABLE"
    FATAL = "FATAL"
    SCOPE_DENIED = "SCOPE_DENIED"  # ★ 越权被拒（红线 R1）


class ScopeCheckStatus(_StrEnum):
    """scope 校验结果。"""

    PASSED = "passed"
    REJECTED = "rejected"


class HealthStatusValue(_StrEnum):
    """适配器健康状态。"""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"


# ============================================================================
#  库存
# ============================================================================


class InventorySource(_StrEnum):
    """库存/价格快照来源。

    ★ v1.14 §5.8：这个字段是**破坏性动作（自动下架）的唯一判定键**，
      必须先于能力判定存在 —— 见 `InventoryService._snapshot_source_is_auto()`。

      - `erp_poll` / `third_party_push`：机器自动同步（可信到可以触发自动下架）
      - `manual_import` / `manual_edit`：手工录入 / 人工改过库存（**不可信**，只告警）
    """

    THIRD_PARTY_PUSH = "third_party_push"
    ERP_POLL = "erp_poll"
    MANUAL_IMPORT = "manual_import"
    MANUAL_EDIT = "manual_edit"


class InventoryChangeType(_StrEnum):
    """库存变动类型。"""

    STOCK = "stock"
    PRICE = "price"
    OFF_SHELF = "off_shelf"


# ============================================================================
#  系统
# ============================================================================


class SettingKey(_StrEnum):
    """核心系统配置键（PRD 8.5 配置驱动，禁止硬编码）。"""

    LISTING_MODE = "listing.mode"
    FULFILLMENT_ACTIVE_ADAPTER = "fulfillment.active_adapter"
    FULFILLMENT_HEARTBEAT_INTERVAL_SEC = "fulfillment.heartbeat_interval_sec"
    FULFILLMENT_HEARTBEAT_FAIL_THRESHOLD = "fulfillment.heartbeat_fail_threshold"
    INVENTORY_POLL_INTERVAL_MIN = "inventory.poll_interval_min"
    INVENTORY_PRICE_INCREASE_THRESHOLD = "inventory.price_increase_threshold"
    INVENTORY_OUT_OF_STOCK_ACTION = "inventory.out_of_stock_action"
    INVENTORY_PRICE_INCREASE_ACTION = "inventory.price_increase_action"
    AI_MAX_CONCURRENCY = "ai.max_concurrency"
    AI_MAX_RETRY = "ai.max_retry"
    AI_CLIENT = "ai.client"
    PUBLISH_BATCH_SIZE = "publish.batch_size"
    PUBLISH_RATE_LIMIT_PER_MIN = "publish.rate_limit_per_min"
    ORDER_SYNC_INTERVAL_MIN = "order.sync_interval_min"
    # ★ 订单自动下单开关（默认开启）：已匹配订单由定时任务自动触发 1688 采购下单
    ORDER_AUTO_PURCHASE_ENABLED = "order.auto_purchase_enabled"
    ORDER_PURCHASE_INTERVAL_MIN = "order.purchase_interval_min"
    ORDER_PURCHASE_BATCH_LIMIT = "order.purchase_batch_limit"
    MAPPING_RETENTION_DAYS = "mapping.retention_days"
    MAPPING_AUTO_PUSH_ENABLED = "mapping.auto_push_enabled"
    # ★ ARCH v1.4：cost_underwater 级别可配（默认 P1 仅提示；改为 P0 则硬拦截，不改代码）
    MAPPING_CONFLICT_COST_UNDERWATER_LEVEL = "mapping.conflict_cost_underwater_level"
    # ★ ARCH v1.4：最低利润率缓冲，判定倒挂时 cost >= price * (1 - margin) 才算倒挂
    MAPPING_MIN_PROFIT_MARGIN = "mapping.min_profit_margin"

    def __str__(self) -> str:  # noqa: D105
        return str(self.value)


class ValueType(_StrEnum):
    """配置值类型。"""

    STRING = "string"
    INT = "int"
    BOOL = "bool"
    JSON = "json"


class CredentialOwnerType(_StrEnum):
    """凭证归属类型。"""

    PLATFORM = "platform"
    FULFILLMENT = "fulfillment"
    SOURCE = "source"


class CredentialStatus(_StrEnum):
    """凭证状态。"""

    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


class AuditActionType(_StrEnum):
    """审计动作类型（§10.7）。

    ★ `permission_change` 是唯一承载「越权告警处置态」的类型：
      被拒记录 `is_handled=0` → 管理员处置后 `is_handled=1`，且**处置动作再埋一条**
      同类型记录（§10.7 第 6 条：「被拒」与「被处置」成对存在）。
    """

    MAPPING_CHANGE = "mapping_change"
    MAPPING_COST_CONFIRM = "mapping_cost_confirm"  # ★ v1.4：人工覆盖成本后货源变动 → 「成本待确认」工单
    MAPPING_PUSH = "mapping_push"        # 映射到第三方的推送（降级为人工导入时同样留痕）
    MAPPING_IMPORT = "mapping_import"    # 从第三方导入映射
    PUBLISH = "publish"
    OFFLINE = "offline"
    ONLINE = "online"
    ADAPTER_SWITCH = "adapter_switch"
    CREDENTIAL_CHANGE = "credential_change"
    PERMISSION_CHANGE = "permission_change"
    ORDER_ACTION = "order_action"
    COST_SYNC = "cost_sync"  # ★ v1.4：镜像成本自动同步（系统产生，审计列表默认过滤）


class AuditObjectType(_StrEnum):
    """审计对象类型。"""

    SKU_MAPPING = "sku_mapping"
    PUBLISH_TASK = "publish_task"
    ORDER = "order"
    ADAPTER = "adapter"
    CREDENTIAL = "credential"
    PLATFORM_ACCOUNT = "platform_account"
    LISTING_PRODUCT = "listing_product"
    SYSTEM_SETTING = "system_setting"


class OperatorRole(_StrEnum):
    """操作者角色。"""

    ADMIN = "admin"
    OPERATOR = "operator"
    SYSTEM = "system"


# ============================================================================
#  异步任务
# ============================================================================


class TaskType(_StrEnum):
    """异步任务类型。"""

    SOURCE_COLLECT = "source_collect"
    AI_REWORK = "ai_rework"
    PUBLISH = "publish"
    ORDER_SYNC = "order_sync"
    INVENTORY_SYNC = "inventory_sync"
    MAPPING_CHECK = "mapping_check"
    PURCHASE_PLACE = "purchase_place"


class TaskStatus(_StrEnum):
    """异步任务状态（重启恢复的判定依据）。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_TASK_STATUSES: tuple[str, ...] = (TaskStatus.PENDING.value, TaskStatus.RUNNING.value)


# ============================================================================
#  前端枚举字典：`GET /settings/enums` 的唯一数据源
# ============================================================================

ENUM_DICT: dict[str, list[dict[str, str]]] = {
    "Platform": [
        {"value": Platform.TAOBAO.value, "label": "淘宝"},
        {"value": Platform.DOUYIN.value, "label": "抖店"},
        {"value": Platform.PDD.value, "label": "拼多多"},
    ],
    "ListingMode": [
        {"value": ListingMode.REAL.value, "label": "真实API"},
        {"value": ListingMode.MOCK.value, "label": "Mock"},
        {"value": ListingMode.MANUAL.value, "label": "半自动"},
    ],
    "MappingStatus": [
        {"value": MappingStatus.VALID.value, "label": "有效"},
        {"value": MappingStatus.PENDING_CONFIRM.value, "label": "待确认"},
        {"value": MappingStatus.INVALID.value, "label": "失效"},
        {"value": MappingStatus.ARCHIVED.value, "label": "归档"},
    ],
    "PublishStatus": [
        {"value": PublishStatus.PENDING_PRECHECK.value, "label": "待预检"},
        {"value": PublishStatus.PRECHECK_FAILED.value, "label": "预检失败"},
        {"value": PublishStatus.PENDING_VALIDATE.value, "label": "待校验"},
        {"value": PublishStatus.VALIDATE_FAILED.value, "label": "校验失败"},
        {"value": PublishStatus.PENDING_PUBLISH.value, "label": "待发布"},
        {"value": PublishStatus.PUBLISHING.value, "label": "发布中"},
        {"value": PublishStatus.PUBLISH_SUCCESS.value, "label": "发布成功"},
        {"value": PublishStatus.PUBLISH_FAILED.value, "label": "发布失败"},
        {"value": PublishStatus.OFFLINE.value, "label": "已下架"},
        {"value": PublishStatus.CANCELLED.value, "label": "已取消"},
    ],
    "OrderStatus": [
        {"value": OrderFulfillmentStatus.PENDING_MATCH.value, "label": "待匹配"},
        {"value": OrderFulfillmentStatus.EXCEPTION_UNMATCHED.value, "label": "异常-待匹配"},
        {"value": OrderFulfillmentStatus.MATCHED.value, "label": "已匹配待下单"},
        {"value": OrderFulfillmentStatus.EXCEPTION_PURCHASE_FAILED.value, "label": "异常-下单失败"},
        {"value": OrderFulfillmentStatus.PURCHASED.value, "label": "已下单待发货"},
        {"value": OrderFulfillmentStatus.EXCEPTION_OUT_OF_STOCK.value, "label": "异常-缺货"},
        {"value": OrderFulfillmentStatus.SHIPPED.value, "label": "已发货待回填"},
        {"value": OrderFulfillmentStatus.EXCEPTION_WRITEBACK_FAILED.value, "label": "异常-回填失败"},
        {"value": OrderFulfillmentStatus.COMPLETED.value, "label": "已完成"},
        {"value": OrderFulfillmentStatus.AFTER_SALE.value, "label": "售后中"},
        {"value": OrderFulfillmentStatus.REFUNDED.value, "label": "已退款"},
        {"value": OrderFulfillmentStatus.CANCELLED.value, "label": "已取消"},
    ],
    "ConflictType": [
        {"value": ConflictType.ONE_TO_MANY.value, "label": "一平台SKU对多货源", "level": "P0", "blocking": True},
        {"value": ConflictType.DUPLICATE.value, "label": "重复映射（同店铺内）", "level": "P0", "blocking": True},
        # ★ ②b「同一 SKU 编码命中多个商品」：检测键为字面量 `duplicate_item`
        #   （CONFLICT_LEVELS / DETECTION_ORDER / get_conflict_queries 均用该键），
        #   此处一并返回给前端，避免前端只看到 duplicate 而漏掉这一类。
        {"value": "duplicate_item", "label": "同一SKU编码挂多商品", "level": "P0", "blocking": True},
        {"value": ConflictType.MANY_TO_ONE.value, "label": "跨平台铺货", "level": "P1", "blocking": False},
        {"value": ConflictType.COST_INVALID.value, "label": "采购成本异常", "level": "P0", "blocking": True},
        {"value": ConflictType.COST_UNDERWATER.value, "label": "成本倒挂", "level": "P1", "blocking": False},
        {"value": ConflictType.SPEC_MISMATCH.value, "label": "规格指纹不匹配", "level": "P0", "blocking": True},
    ],
    "AdapterName": [
        {"value": AdapterName.MIAOSHOU.value, "label": "妙手"},
        {"value": AdapterName.YITAO.value, "label": "逸淘"},
        {"value": AdapterName.LOCAL_CSV.value, "label": "本地兜底"},
    ],
    "CapabilityLevel": [
        {"value": CapabilityLevel.SUPPORTED.value, "label": "支持"},
        {"value": CapabilityLevel.DEGRADED.value, "label": "降级"},
        {"value": CapabilityLevel.UNSUPPORTED.value, "label": "不支持"},
    ],
    "ExceptionType": [
        {"value": ExceptionType.UNMATCHED.value, "label": "匹配失败"},
        {"value": ExceptionType.ADDRESS_ERROR.value, "label": "地址异常"},
        {"value": ExceptionType.DECRYPT_FAILED.value, "label": "解密失败"},
        {"value": ExceptionType.PURCHASE_FAILED.value, "label": "采购失败"},
        {"value": ExceptionType.WRITEBACK_FAILED.value, "label": "回填失败"},
        {"value": ExceptionType.OUT_OF_STOCK.value, "label": "缺货"},
    ],
    "ChangeSource": [
        {"value": ChangeSource.MANUAL.value, "label": "人工"},
        {"value": ChangeSource.SYSTEM.value, "label": "系统"},
        {"value": ChangeSource.THIRD_PARTY.value, "label": "第三方"},
    ],
}


__all__ = [
    "ACTIVE_TASK_STATUSES",
    "ENUM_DICT",
    "EXCEPTION_ORDER_STATUSES",
    "ORDER_TRANSITIONS",
    "PUBLISH_TRANSITIONS",
    "TERMINAL_ORDER_STATUSES",
    "AdapterName",
    "AfterSaleHandlingStatus",
    "AiReworkItem",
    "AiTaskStatus",
    "AssetOrigin",
    "AssetType",
    "AuditActionType",
    "AuditObjectType",
    "Capability",
    "CapabilityLevel",
    "ChangeAction",
    "ChangeSource",
    "ConflictLevel",
    "ConflictType",
    "CredentialOwnerType",
    "CredentialStatus",
    "ExceptionType",
    "HandlingAction",
    "HealthStatusValue",
    "InventoryChangeType",
    "InventorySource",
    "ListingMode",
    "ListingProductStatus",
    "ListingSkuStatus",
    "MappingSource",
    "MappingStatus",
    "MatchStatus",
    "OperatorRole",
    "OrderFulfillmentStatus",
    "Platform",
    "PlatformAccountStatus",
    "PublishStatus",
    "PurchaseStatus",
    "PushStatus",
    "Refund1688Status",
    "ResolveAction",
    "Responsibility",
    "ResultCode",
    "ReturnAddressPushStatus",
    "ReviewStatus",
    "ScopeCheckStatus",
    "SettingKey",
    "SourcePlatform",
    "SourceStatus",
    "SupplierStatus",
    "TaskStatus",
    "TaskType",
    "ValueType",
    "WriteBackStatus",
]
