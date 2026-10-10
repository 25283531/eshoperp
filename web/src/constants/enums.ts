/**
 * 枚举常量与中文标签映射。
 *
 * 约定（ARCHITECTURE.md §5.5.13）：
 *  - 枚举字典的唯一权威来源是后端 `GET /settings/enums`；
 *  - 本文件提供**与后端 enums.py 对齐的本地兜底**，仅在后端不可达时生效；
 *  - 页面一律通过 `useEnumOptions()` / `useEnumLabel()` 取标签，
 *    禁止在页面里硬编码中文标签，避免前后端漂移。
 */

/** 后端枚举字典项结构，与 GET /settings/enums 返回一致 */
export interface EnumOption {
  value: string;
  label: string;
  /** 仅 ConflictType 下发：P0 / P1 */
  level?: string;
  /** 仅 ConflictType 下发：true 表示拦截上架 */
  blocking?: boolean;
}

/** 全部枚举字典：{ 枚举名: 选项列表 } */
export type EnumsMap = Record<string, EnumOption[]>;

// ---------------------------------------------------------------------------
// 枚举值字面量类型（与后端 enums.py 逐字对齐）
// ---------------------------------------------------------------------------

export type Platform = 'taobao' | 'douyin' | 'pdd';
export type ListingMode = 'real' | 'mock' | 'manual';
export type MappingStatus = 'valid' | 'pending_confirm' | 'invalid' | 'archived';
export type ConflictType =
  | 'one_to_many'
  | 'many_to_one'
  | 'duplicate'
  | 'duplicate_item'
  | 'cost_invalid'
  | 'spec_mismatch'
  | 'cost_underwater';
export type ConflictLevel = 'P0' | 'P1';
export type PublishStatus =
  | 'pending_precheck'
  | 'precheck_failed'
  | 'pending_validate'
  | 'validate_failed'
  | 'pending_publish'
  | 'publishing'
  | 'publish_success'
  | 'publish_failed'
  | 'offline'
  | 'cancelled';
export type OrderFulfillmentStatus =
  | 'pending_match'
  | 'exception_unmatched'
  | 'matched'
  | 'exception_purchase_failed'
  | 'purchased'
  | 'exception_out_of_stock'
  | 'shipped'
  | 'exception_writeback_failed'
  | 'completed'
  | 'after_sale'
  | 'refunded'
  | 'cancelled';
export type AiTaskStatus =
  | 'queued'
  | 'running'
  | 'pending_review'
  | 'approved'
  | 'rejected'
  | 'failed'
  | 'cancelled';
export type ReviewStatus = 'pending' | 'approved' | 'rejected';
export type AdapterName = 'miaoshou' | 'yitao' | 'local_csv';
export type CapabilityLevel = 'supported' | 'degraded' | 'unsupported';
export type ExceptionType =
  | 'unmatched'
  | 'purchase_failed'
  | 'out_of_stock'
  | 'writeback_failed'
  | 'other';
export type ChangeSource = 'manual' | 'system' | 'third_party';
export type AssetOrigin = 'raw' | 'ai_rework';
export type TaskStatus = 'pending' | 'running' | 'success' | 'failed' | 'cancelled';
export type Responsibility = 'our_shop' | 'supplier' | 'buyer' | 'platform';
export type AfterSaleHandlingStatus = 'pending' | 'handling' | 'refunded' | 'closed';
export type HealthState = 'healthy' | 'degraded' | 'down' | 'unknown';
export type InventoryAction = 'offline' | 'notify_only';
export type AuditActionType =
  | 'mapping_change'
  | 'publish'
  | 'offline'
  | 'online'
  | 'adapter_switch'
  | 'credential_change'
  | 'permission_change'
  | 'order_action';
export type AuditObjectType =
  | 'sku_mapping'
  | 'publish_task'
  | 'listing_product'
  | 'adapter'
  | 'credential'
  | 'platform_account'
  | 'erp_order';

// ---------------------------------------------------------------------------
// 本地兜底字典（后端 /settings/enums 不可达时使用）
// ---------------------------------------------------------------------------

export const PLATFORM_OPTIONS: EnumOption[] = [
  { value: 'taobao', label: '淘宝' },
  { value: 'douyin', label: '抖店' },
  { value: 'pdd', label: '拼多多' },
];

export const LISTING_MODE_OPTIONS: EnumOption[] = [
  { value: 'real', label: '真实API' },
  { value: 'mock', label: 'Mock' },
  { value: 'manual', label: '半自动' },
];

export const MAPPING_STATUS_OPTIONS: EnumOption[] = [
  { value: 'valid', label: '有效' },
  { value: 'pending_confirm', label: '待确认' },
  { value: 'invalid', label: '失效' },
  { value: 'archived', label: '归档' },
];

export const SCOPE_CHECK_STATUS_OPTIONS: EnumOption[] = [
  { value: 'passed', label: '校验通过' },
  { value: 'failed', label: '校验失败（已拒绝）' },
  { value: 'unknown', label: '未校验' },
];

export const MAPPING_CHANGE_TYPE_OPTIONS: EnumOption[] = [
  { value: 'rename', label: '名称变更' },
  { value: 'out_of_stock', label: '货源缺货' },
  { value: 'spec_change', label: '规格变更' },
  { value: 'off_shelf', label: '货源下架' },
];

export const SOURCE_PRODUCT_STATUS_OPTIONS: EnumOption[] = [
  { value: 'collected', label: '已采集' },
  { value: 'reworked', label: '已重构' },
  { value: 'published', label: '已上架' },
  { value: 'off_shelf', label: '已下架' },
  { value: 'invalid', label: '失效' },
];

export const STOCK_STATUS_OPTIONS: EnumOption[] = [
  { value: 'in_stock', label: '有货' },
  { value: 'low_stock', label: '库存偏低' },
  { value: 'out_of_stock', label: '缺货' },
  { value: 'unknown', label: '未知' },
];

export const LISTING_STATUS_OPTIONS: EnumOption[] = [
  { value: 'on_sale', label: '在售' },
  { value: 'off_shelf', label: '已下架' },
  { value: 'pending', label: '待上架' },
  { value: 'failed', label: '上架失败' },
];

export const PUBLISH_STATUS_OPTIONS: EnumOption[] = [
  { value: 'pending_precheck', label: '待预检' },
  { value: 'precheck_failed', label: '预检失败' },
  { value: 'pending_validate', label: '待校验' },
  { value: 'validate_failed', label: '校验失败' },
  { value: 'pending_publish', label: '待发布' },
  { value: 'publishing', label: '发布中' },
  { value: 'publish_success', label: '发布成功' },
  { value: 'publish_failed', label: '发布失败' },
  { value: 'offline', label: '已下架' },
  { value: 'cancelled', label: '已取消' },
];

export const ORDER_STATUS_OPTIONS: EnumOption[] = [
  { value: 'pending_match', label: '待匹配' },
  { value: 'exception_unmatched', label: '异常-待匹配' },
  { value: 'matched', label: '已匹配待下单' },
  // 注：`pending_purchase` 曾因 seed 脚本写入非法状态而出现在订单数据里，
  // 后端已修正（OrderFulfillmentStatus 状态机只有下方 12 个合法值）。
  // 此处不再保留兜底，避免筛选下拉里出现永远选不中的状态。
  { value: 'exception_purchase_failed', label: '异常-下单失败' },
  { value: 'purchased', label: '已下单待发货' },
  { value: 'exception_out_of_stock', label: '异常-缺货' },
  { value: 'shipped', label: '已发货待回填' },
  { value: 'exception_writeback_failed', label: '异常-回填失败' },
  { value: 'completed', label: '已完成' },
  { value: 'after_sale', label: '售后中' },
  { value: 'refunded', label: '已退款' },
  { value: 'cancelled', label: '已取消' },
];

/**
 * AI 任务类型（AiTaskType）：回答"这条 AI 任务要产出什么"。
 *
 * ★ 与 `AiTaskStatus`（执行状态）是两个维度，不要混用。
 * ★ `ai_rework` 是存量数据的默认值，排在第一个。
 */
export const AI_TASK_TYPE_OPTIONS: EnumOption[] = [
  { value: 'ai_rework', label: '图文重构' },
  { value: 'image_redraw', label: '图片重绘' },
  { value: 'title_suggest', label: '商品标题建议' },
  { value: 'video_script', label: '短视频脚本建议' },
];

/**
 * 素材类型（AssetType）。
 *
 * ★★ 这是前端区分「主图 / 详情页」的**唯一可靠依据** ★★
 *    后端 `AssetVo` 会把 `tags_json` 拍平成 `tags: string[]`（`image_role` / `index`
 *    这些同层字段不透出），`storage_path` 的文件命名规则也还在改成中文目录 ——
 *    任何一种"按文件名猜角色"的写法都会在后端换命名那天崩掉。
 */
export const ASSET_TYPE_OPTIONS: EnumOption[] = [
  { value: 'main_image', label: '主图' },
  { value: 'detail_image', label: '详情图' },
  { value: 'video', label: '视频' },
];

export const AI_TASK_STATUS_OPTIONS: EnumOption[] = [
  { value: 'queued', label: '排队中' },
  { value: 'running', label: '执行中' },
  { value: 'pending_review', label: '待审核' },
  { value: 'approved', label: '已通过' },
  { value: 'rejected', label: '已打回' },
  { value: 'failed', label: '执行失败' },
  { value: 'cancelled', label: '已取消' },
];

export const REVIEW_STATUS_OPTIONS: EnumOption[] = [
  { value: 'pending', label: '待审核' },
  { value: 'approved', label: '审核通过' },
  { value: 'rejected', label: '审核打回' },
];

/**
 * 六类冲突（type key 严格与后端 enums.py 对齐，标签以后端 /settings/enums 下发为准）。
 *
 * P0（blocking=true）：禁止上架，无绕过路径。
 * P1（blocking=false）：仅提示不拦截，映射照常生效、不影响上架。
 *   其中 `many_to_one`（跨平台铺货）是用户**正常主营业务**——
 *   同一货源铺到淘宝 + 抖店 + 拼多多，UI 上绝不能表达成错误。
 */
export const CONFLICT_TYPE_OPTIONS: EnumOption[] = [
  { value: 'one_to_many', label: '一平台SKU对多货源' },
  { value: 'duplicate', label: '重复映射（同店铺内）' },
  { value: 'duplicate_item', label: '同一SKU编码挂多商品' },
  { value: 'cost_invalid', label: '采购成本异常' },
  { value: 'spec_mismatch', label: '规格指纹不匹配' },
  { value: 'many_to_one', label: '跨平台铺货' },
  { value: 'cost_underwater', label: '成本倒挂' },
];

/** 冲突类型 → 级别（P0 拦截 / P1 仅提示）；后端未下发 level 时兜底 */
export const CONFLICT_LEVEL_BY_TYPE: Record<string, ConflictLevel> = {
  one_to_many: 'P0',
  duplicate: 'P0',
  duplicate_item: 'P0',
  cost_invalid: 'P0',
  spec_mismatch: 'P0',
  many_to_one: 'P1',
  cost_underwater: 'P1',
};

/** 冲突类型 → 是否拦截上架；后端未下发 blocking 时兜底 */
export const CONFLICT_BLOCKING_BY_TYPE: Record<string, boolean> = {
  one_to_many: true,
  duplicate: true,
  duplicate_item: true,
  cost_invalid: true,
  spec_mismatch: true,
  many_to_one: false,
  cost_underwater: false,
};

/** P1 的统一文案：明确「不拦截」，避免运营误以为被拦 */
export const CONFLICT_P1_COPY =
  'P1 · 仅提示，不拦截：这是正常铺货或利润提示，映射照常生效，不影响上架。';

/** P0 的统一文案 */
export const CONFLICT_P0_COPY = 'P0 · 拦截：必须解决后才能上架，系统无绕过路径。';

/** 跨平台铺货（正常主营业务）的专属说明：禁止表达成错误 */
export const CONFLICT_MANY_TO_ONE_COPY =
  '同一货源铺到多个平台（淘宝 / 抖店 / 拼多多）属于正常铺货，不是错误，不拦截上架。';

/**
 * 取一组冲突类型中的最高级别（P0 优先）。
 * @param conflictTypes 冲突类型列表
 * @param levelByType 后端下发的 level 映射（优先级高于本地兜底）
 */
export function resolveConflictLevel(
  conflictTypes?: string[] | null,
  levelByType?: Record<string, string>,
): ConflictLevel | null {
  if (!conflictTypes || conflictTypes.length === 0) return null;
  const levelOf = (type: string): string =>
    levelByType?.[type] ?? CONFLICT_LEVEL_BY_TYPE[type] ?? '';
  if (conflictTypes.some((type) => levelOf(type) === 'P0')) return 'P0';
  if (conflictTypes.some((type) => levelOf(type) === 'P1')) return 'P1';
  return null;
}

/**
 * 判断一组冲突类型是否拦截上架（只要有一个 blocking 即拦截）。
 * @param conflictTypes 冲突类型列表
 * @param blockingByType 后端下发的 blocking 映射（优先级高于本地兜底）
 */
export function resolveConflictBlocking(
  conflictTypes?: string[] | null,
  blockingByType?: Record<string, boolean>,
): boolean {
  if (!conflictTypes || conflictTypes.length === 0) return false;
  return conflictTypes.some(
    (type) => blockingByType?.[type] ?? CONFLICT_BLOCKING_BY_TYPE[type] ?? false,
  );
}

export const CONFLICT_LEVEL_OPTIONS: EnumOption[] = [
  { value: 'P0', label: 'P0（禁止上架）' },
  { value: 'P1', label: 'P1（提示，不拦截）' },
];

export const ADAPTER_NAME_OPTIONS: EnumOption[] = [
  { value: 'miaoshou', label: '妙手' },
  { value: 'yitao', label: '逸淘' },
  { value: 'local_csv', label: '本地兜底' },
];

export const CAPABILITY_LEVEL_OPTIONS: EnumOption[] = [
  { value: 'supported', label: '支持' },
  { value: 'degraded', label: '降级' },
  { value: 'unsupported', label: '不支持' },
];

export const EXCEPTION_TYPE_OPTIONS: EnumOption[] = [
  { value: 'unmatched', label: '匹配失败' },
  { value: 'address_error', label: '地址异常' },
  { value: 'decrypt_failed', label: '解密失败' },
  { value: 'purchase_failed', label: '采购失败' },
  { value: 'writeback_failed', label: '回填失败' },
  { value: 'out_of_stock', label: '缺货' },
];

/** 采购单状态（本地兜底通道 manual_pending 表示「待人工在 1688 下单」） */
export const PURCHASE_STATUS_OPTIONS: EnumOption[] = [
  { value: 'manual_pending', label: '待人工处理' },
  { value: 'pending', label: '待下单' },
  { value: 'ordered', label: '已下单' },
  { value: 'shipped', label: '已发货' },
  { value: 'failed', label: '下单失败' },
];

/** 物流回填状态 */
export const WRITEBACK_STATUS_OPTIONS: EnumOption[] = [
  { value: 'pending', label: '待回填' },
  { value: 'success', label: '回填成功' },
  { value: 'failed', label: '回填失败' },
];

/** 凭证状态 */
export const CREDENTIAL_STATUS_OPTIONS: EnumOption[] = [
  { value: 'active', label: '启用' },
  { value: 'disabled', label: '停用' },
  { value: 'expired', label: '已过期' },
];

export const CHANGE_SOURCE_OPTIONS: EnumOption[] = [
  { value: 'manual', label: '人工' },
  { value: 'system', label: '系统' },
  { value: 'third_party', label: '第三方' },
];

export const ASSET_ORIGIN_OPTIONS: EnumOption[] = [
  { value: 'raw', label: '原始素材' },
  { value: 'ai_rework', label: 'AI 重构' },
];

export const TASK_STATUS_OPTIONS: EnumOption[] = [
  { value: 'pending', label: '待执行' },
  { value: 'running', label: '执行中' },
  { value: 'success', label: '成功' },
  { value: 'failed', label: '失败' },
  { value: 'cancelled', label: '已取消' },
];

export const RESPONSIBILITY_OPTIONS: EnumOption[] = [
  { value: 'our_shop', label: '本店责任' },
  { value: 'supplier', label: '供应商责任' },
  { value: 'buyer', label: '买家责任' },
  { value: 'platform', label: '平台责任' },
];

export const AFTER_SALE_STATUS_OPTIONS: EnumOption[] = [
  { value: 'pending', label: '待处理' },
  { value: 'handling', label: '处理中' },
  { value: 'refunded', label: '已退款' },
  { value: 'closed', label: '已关闭' },
];

export const HEALTH_STATE_OPTIONS: EnumOption[] = [
  { value: 'healthy', label: '正常' },
  { value: 'degraded', label: '降级' },
  { value: 'down', label: '不可用' },
  { value: 'unknown', label: '未知' },
];

export const INVENTORY_ACTION_OPTIONS: EnumOption[] = [
  { value: 'offline', label: '自动下架' },
  { value: 'notify_only', label: '仅通知' },
];

export const AUDIT_ACTION_OPTIONS: EnumOption[] = [
  { value: 'mapping_change', label: '映射变更' },
  { value: 'publish', label: '上架' },
  { value: 'offline', label: '下架' },
  { value: 'online', label: '重新上架' },
  { value: 'adapter_switch', label: '适配器切换' },
  { value: 'credential_change', label: '凭证变更' },
  { value: 'permission_change', label: '权限变更/越权' },
  { value: 'order_action', label: '订单处置' },
];

export const AUDIT_OBJECT_OPTIONS: EnumOption[] = [
  { value: 'sku_mapping', label: 'SKU 映射' },
  { value: 'publish_task', label: '上架任务' },
  { value: 'listing_product', label: '平台商品' },
  { value: 'adapter', label: '适配器' },
  { value: 'credential', label: '凭证' },
  { value: 'platform_account', label: '平台账号' },
  { value: 'erp_order', label: '订单' },
];

/** 8 项履约能力（PRD FUL-P0-01）中文标签 */
export const CAPABILITY_OPTIONS: EnumOption[] = [
  { value: 'fetch_orders', label: '拉取订单' },
  { value: 'match_sku', label: 'SKU 匹配' },
  { value: 'place_purchase_order', label: '1688 采购下单' },
  { value: 'fetch_tracking_no', label: '获取物流单号' },
  { value: 'write_back_tracking', label: '物流回填' },
  { value: 'submit_refund', label: '提交退款' },
  { value: 'get_return_address', label: '获取退货地址' },
  { value: 'push_inventory_change', label: '推送库存变动' },
];

/** 订单处置动作（POST /orders/{id}/actions） */
export const ORDER_ACTION_OPTIONS: EnumOption[] = [
  { value: 'retry', label: '重试' },
  { value: 'switch_source', label: '换货源' },
  { value: 'refund', label: '退款' },
  { value: 'ignore', label: '忽略' },
];

/** 审核动作（POST /ai-tasks/{id}/review） */
export const AI_REVIEW_ACTION_OPTIONS: EnumOption[] = [
  { value: 'approve', label: '通过' },
  { value: 'reject', label: '打回' },
  { value: 'edit', label: '编辑后通过' },
];

/** 映射待确认工单处理动作（POST /sku-mappings/pending/{id}/resolve） */
export const MAPPING_PENDING_ACTION_OPTIONS: EnumOption[] = [
  { value: 'confirm', label: '确认变更' },
  { value: 'reject', label: '驳回（置失效）' },
  { value: 'manual_assign', label: '手工指定货源 SKU' },
];

/** 映射导出/推送目标适配器 */
export const MAPPING_EXPORT_TARGET_OPTIONS: EnumOption[] = [
  { value: 'miaoshou', label: '妙手' },
  { value: 'yitao', label: '逸淘' },
  { value: 'generic', label: '通用 CSV' },
];

/** 本地兜底字典总表：key 与后端 /settings/enums 的枚举名一致 */
export const ENUM_FALLBACK: EnumsMap = {
  Platform: PLATFORM_OPTIONS,
  ListingMode: LISTING_MODE_OPTIONS,
  MappingStatus: MAPPING_STATUS_OPTIONS,
  MappingChangeType: MAPPING_CHANGE_TYPE_OPTIONS,
  ScopeCheckStatus: SCOPE_CHECK_STATUS_OPTIONS,
  SourceProductStatus: SOURCE_PRODUCT_STATUS_OPTIONS,
  StockStatus: STOCK_STATUS_OPTIONS,
  ListingStatus: LISTING_STATUS_OPTIONS,
  PublishStatus: PUBLISH_STATUS_OPTIONS,
  OrderStatus: ORDER_STATUS_OPTIONS,
  PurchaseStatus: PURCHASE_STATUS_OPTIONS,
  WritebackStatus: WRITEBACK_STATUS_OPTIONS,
  CredentialStatus: CREDENTIAL_STATUS_OPTIONS,
  AiTaskType: AI_TASK_TYPE_OPTIONS,
  AiTaskStatus: AI_TASK_STATUS_OPTIONS,
  ReviewStatus: REVIEW_STATUS_OPTIONS,
  ConflictType: CONFLICT_TYPE_OPTIONS,
  ConflictLevel: CONFLICT_LEVEL_OPTIONS,
  AdapterName: ADAPTER_NAME_OPTIONS,
  CapabilityLevel: CAPABILITY_LEVEL_OPTIONS,
  Capability: CAPABILITY_OPTIONS,
  ExceptionType: EXCEPTION_TYPE_OPTIONS,
  ChangeSource: CHANGE_SOURCE_OPTIONS,
  AssetOrigin: ASSET_ORIGIN_OPTIONS,
  AssetType: ASSET_TYPE_OPTIONS,
  TaskStatus: TASK_STATUS_OPTIONS,
  Responsibility: RESPONSIBILITY_OPTIONS,
  AfterSaleStatus: AFTER_SALE_STATUS_OPTIONS,
  HealthState: HEALTH_STATE_OPTIONS,
  InventoryAction: INVENTORY_ACTION_OPTIONS,
  AuditActionType: AUDIT_ACTION_OPTIONS,
  AuditObjectType: AUDIT_OBJECT_OPTIONS,
  OrderAction: ORDER_ACTION_OPTIONS,
  AiReviewAction: AI_REVIEW_ACTION_OPTIONS,
  MappingPendingAction: MAPPING_PENDING_ACTION_OPTIONS,
  MappingExportTarget: MAPPING_EXPORT_TARGET_OPTIONS,
};

/** 已知枚举名（用于类型提示，不限制运行期取值） */
export type EnumKey = keyof typeof ENUM_FALLBACK | string;

// ---------------------------------------------------------------------------
// StatusTag 配色：按枚举名 + 取值映射 antd Tag color
// ---------------------------------------------------------------------------

export const STATUS_TAG_COLORS: Record<string, Record<string, string>> = {
  PublishStatus: {
    pending_precheck: 'default',
    precheck_failed: 'orange',
    pending_validate: 'blue',
    validate_failed: 'red',
    pending_publish: 'blue',
    publishing: 'processing',
    publish_success: 'success',
    publish_failed: 'red',
    offline: 'default',
    cancelled: 'default',
  },
  OrderStatus: {
    pending_match: 'default',
    exception_unmatched: 'red',
    matched: 'blue',
    exception_purchase_failed: 'red',
    purchased: 'blue',
    exception_out_of_stock: 'volcano',
    shipped: 'cyan',
    exception_writeback_failed: 'red',
    completed: 'success',
    after_sale: 'orange',
    refunded: 'purple',
    cancelled: 'default',
  },
  MappingStatus: {
    valid: 'success',
    pending_confirm: 'orange',
    invalid: 'red',
    archived: 'default',
  },
  AiTaskType: {
    ai_rework: 'default',
    image_redraw: 'blue',
    title_suggest: 'purple',
    video_script: 'cyan',
  },
  AiTaskStatus: {
    queued: 'default',
    running: 'processing',
    pending_review: 'gold',
    approved: 'success',
    rejected: 'red',
    failed: 'red',
    cancelled: 'default',
  },
  AssetType: {
    main_image: 'blue',
    detail_image: 'cyan',
    video: 'purple',
  },
  ReviewStatus: {
    pending: 'gold',
    approved: 'success',
    rejected: 'red',
  },
  ConflictLevel: {
    P0: 'red',
    P1: 'orange',
  },
  PurchaseStatus: {
    manual_pending: 'gold',
    pending: 'blue',
    ordered: 'cyan',
    shipped: 'success',
    failed: 'red',
  },
  WritebackStatus: {
    pending: 'default',
    success: 'success',
    failed: 'red',
  },
  CredentialStatus: {
    active: 'success',
    disabled: 'default',
    expired: 'red',
  },
  CapabilityLevel: {
    supported: 'success',
    degraded: 'orange',
    unsupported: 'default',
  },
  TaskStatus: {
    pending: 'default',
    running: 'processing',
    success: 'success',
    failed: 'red',
    cancelled: 'default',
  },
  HealthState: {
    healthy: 'success',
    degraded: 'orange',
    down: 'red',
    unknown: 'default',
  },
  AfterSaleStatus: {
    pending: 'gold',
    handling: 'processing',
    refunded: 'success',
    closed: 'default',
  },
  ScopeCheckStatus: {
    passed: 'success',
    failed: 'red',
    unknown: 'default',
  },
  ListingMode: {
    real: 'success',
    mock: 'orange',
    manual: 'blue',
  },
};

/** 取某个枚举值的 Tag 颜色，未命中返回 'default' */
export function statusTagColor(enumKey: string, value?: string | null): string {
  if (!value) return 'default';
  return STATUS_TAG_COLORS[enumKey]?.[value] ?? 'default';
}

// ---------------------------------------------------------------------------
// 业务红线常量
// ---------------------------------------------------------------------------

/** 红线 R1：履约适配器**只允许**这两项 scope，其余一律拒绝 */
export const ALLOWED_SCOPES: string[] = ['order.read', 'logistics.write'];

/** 红线 R1：命中任一即拒绝启用（与白名单双保险） */
export const FORBIDDEN_SCOPES: string[] = [
  'item.write',
  'item.create',
  'item.update',
  'item.delete',
  'price.update',
  'item.publish',
  'item.offline',
  'item.edit',
  'product.write',
  'product.create',
  'product.update',
];

/** 授权清单表格：权限最小化展示用（订单读取 ✓ / 发货 ✓ / 商品编辑 ✗ / 上新 ✗ / 改价 ✗） */
export const SCOPE_MATRIX: { scope: string; label: string; allowed: boolean }[] = [
  { scope: 'order.read', label: '订单读取', allowed: true },
  { scope: 'logistics.write', label: '发货 / 物流回填', allowed: true },
  { scope: 'item.write', label: '商品编辑', allowed: false },
  { scope: 'item.publish', label: '上新', allowed: false },
  { scope: 'price.update', label: '改价', allowed: false },
  { scope: 'item.offline', label: '下架', allowed: false },
];

/** 上架校验被拦截时（422 / code 4005 等）的统一文案：强调无绕过路径 */
export const MAPPING_BLOCK_COPY = {
  title: '上架校验未通过，已禁止上架',
  noBypass:
    '这是系统硬拦截（红线）：不存在任何绕过路径。必须先补齐缺失映射或解决 P0 级冲突，再重新发起上架任务。',
  missingTitle: '缺失映射（mapping_not_found）',
  conflictTitle: '冲突明细',
};
