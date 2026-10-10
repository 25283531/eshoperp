/**
 * 全局 DTO 类型定义。
 * 与 ARCHITECTURE.md §5.5 的 VO 字段**逐字对齐**（蛇形命名，不做 camelCase 转换）。
 * 金额统一为字符串「元」；时间统一为 ISO8601 UTC 字符串。
 */

/** 金额：字符串表示的「元」，如 "29.90" */
export type MoneyStr = string;

/** 时间：ISO8601 UTC 字符串，如 "2026-10-08T02:30:00Z" */
export type IsoTimeStr = string;

/** 分页出参 */
export interface PageResult<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

/**
 * 后端枚举字典项结构，与 GET /settings/enums 返回一致。
 * ConflictType 额外携带 level（P0/P1）与 blocking（是否拦截），
 * 前端一律以后端下发为准，禁止在页面里自行推断冲突级别。
 */
export interface EnumItemLike {
  value: string;
  label: string;
  /** 仅 ConflictType 下发：P0 / P1 */
  level?: string;
  /** 仅 ConflictType 下发：true 表示拦截上架 */
  blocking?: boolean;
}

/** GET /settings/enums 返回结构：{ 枚举名: [{value,label}] } */
export type EnumsMapLike = Record<string, EnumItemLike[]>;

// ---------------------------------------------------------------------------
// 5.5.1 工作台 / 健康检查
// ---------------------------------------------------------------------------

export interface HealthLightVo {
  name: string;
  status: string;
  message: string;
}

export interface TodoItemVo {
  type: string;
  title: string;
  count: number;
  link: string;
}

export interface DashboardSummaryVo {
  today_order_count: number;
  pending_exception_count: number;
  mapping_alert_count: number;
  queue_backlog_count: number;
  /** 未处置越权告警数（与 /system/status-bar 同源） */
  unhandled_violation_count: number;
  health_lights: HealthLightVo[];
  todo_list: TodoItemVo[];
}

export interface HealthAdapterVo {
  name: string;
  status: string;
  latency_ms: number;
}

export interface HealthVo {
  status: string;
  db: string;
  storage: string;
  adapters: HealthAdapterVo[];
  queue: {
    pending: number;
    running: number;
    failed_1h: number;
  };
}

// ---------------------------------------------------------------------------
// 5.5.2 平台账号与凭证
// ---------------------------------------------------------------------------

export interface PlatformAccountVo {
  id: number;
  platform: string;
  shop_id: string;
  shop_name: string | null;
  credential_id: number | null;
  granted_scopes: string[];
  token_masked: string;
  token_expires_at: IsoTimeStr | null;
  status: string;
}

export interface PlatformAccountCreateBody {
  platform: string;
  shop_id: string;
  shop_name: string;
  granted_scopes: string[];
  credential: {
    app_key: string;
    app_secret: string;
    access_token: string;
  };
}

export interface AuthorizeResultVo {
  accepted: boolean;
  effective_scopes: string[];
  denied: string[];
}

export interface CredentialVo {
  id: number;
  owner_type: string;
  owner_key: string;
  credential_key: string;
  value_masked: string | null;
  expires_at: IsoTimeStr | null;
  status: string;
  last_verified_at: IsoTimeStr | null;
}

export interface CredentialCreateBody {
  owner_type: string;
  owner_key: string;
  credential_key: string;
  value: string;
}

export interface CredentialTestVo {
  ok: boolean;
  latency_ms: number;
  message: string;
}

export interface CredentialRevealVo {
  value_plain: string;
  expires_in_sec: number;
}

// ---------------------------------------------------------------------------
// 5.5.3 供应商与货源商品
// ---------------------------------------------------------------------------

export interface SupplierVo {
  id: number;
  supplier_1688_id: string;
  name: string;
  location: string | null;
  lead_time_hours: number | null;
  moq: number | null;
  cooperation_score: number | null;
  status: string;
  created_at: IsoTimeStr;
  updated_at: IsoTimeStr;
}

export interface SupplierCreateBody {
  supplier_1688_id: string;
  name: string;
  location?: string;
  lead_time_hours?: number;
  moq?: number;
  cooperation_score?: number;
}

export interface SourceProductVo {
  id: number;
  product_1688_id: string;
  title: string;
  category_path: string | null;
  supplier_id: number | null;
  supplier_name: string | null;
  cost_price: MoneyStr;
  origin_url: string | null;
  main_image_url: string | null;
  stock_status: string | null;
  status: string;
  collected_at: IsoTimeStr | null;
  sku_count: number;
}

export interface SourceSkuVo {
  id: number;
  source_product_id: number;
  sku_code_1688: string;
  spec_json: Record<string, string> | null;
  spec_signature: string | null;
  cost_price: MoneyStr;
  stock_qty: number | null;
  status: string;
  last_checked_at: IsoTimeStr | null;
}

export interface SourceProductDetailVo extends SourceProductVo {
  skus: SourceSkuVo[];
  assets: AssetVo[];
  params_json: Record<string, unknown> | null;
}

/** 手工录入的单个货源 SKU（POST /source-products/manual） */
export interface ManualSourceSkuBody {
  /** 多维规格用英文分号分隔，如 "颜色;尺码" */
  spec_name?: string | null;
  /** 与 spec_name 一一对应，如 "红色;XL" */
  spec_value?: string | null;
  /** 留空按规格自动生成 */
  sku_code?: string | null;
  sale_price?: MoneyStr | number | null;
  cost_price?: MoneyStr | number | null;
  stock_qty?: number;
  status?: string;
}

/** POST /source-products/manual 请求体 */
export interface ManualSourceProductBody {
  title: string;
  /** 存入前加 MANUAL- 前缀，≤64 */
  product_code?: string | null;
  category_path?: string | null;
  supplier_id?: number | null;
  cost_price?: MoneyStr | number | null;
  origin_url?: string | null;
  main_image_url?: string | null;
  status?: string;
  stock_status?: string | null;
  /** ★ 没有 SKU 建不了映射、上不了架，后端强制要求至少一个 */
  skus?: ManualSourceSkuBody[];
}

/** POST /source-products/manual 返回体（实测对齐，201） */
export interface ManualSourceProductVo {
  id: number;
  product_1688_id: string;
  source_platform: string;
  title: string;
  created: boolean;
  created_skus: number;
  updated_skus: number;
  skus: SourceSkuVo[];
}

/** CSV 导入失败行（★ 逐行展示，不能只说"导入完成"） */
export interface CsvImportFailedRowVo {
  /** CSV 行号（含表头，第 1 行为表头） */
  row: number;
  /** 该行的商品编码 / 标识 */
  identifier: string | null;
  /** 中文原因，后端已带「第N行：」前缀 */
  reason: string;
}

/**
 * POST /source-products/import-csv 返回体（实测对齐）。
 * 后端逐行解析：合法行入库，非法行跳过并记入 failed，绝不全批次回滚。
 */
export interface CsvImportResultVo {
  total: number;
  created: number;
  updated: number;
  created_skus: number;
  updated_skus: number;
  failed: CsvImportFailedRowVo[];
}

export interface CollectBody {
  source: string;
  identifiers: string[];
}

export interface AcceptedTaskVo {
  accepted: number;
  task_record_id: number;
}

// ---------------------------------------------------------------------------
// 5.5.4 素材库
// ---------------------------------------------------------------------------

export interface AssetVo {
  id: number;
  source_product_id: number | null;
  source_sku_id: number | null;
  asset_type: string;
  origin: string;
  storage_path: string;
  origin_url: string | null;
  content_hash: string | null;
  version: number;
  lineage_id: string | null;
  is_current: boolean;
  width: number | null;
  height: number | null;
  size_bytes: number | null;
  tags: string[];
  /** 形如 /api/v1/assets/{id}/download，可直接用于预览与下载 */
  preview_url: string | null;
  ai_task_id: number | null;
  created_at: IsoTimeStr | null;
}

export interface BatchDownloadVo {
  download_url: string;
}

// ---------------------------------------------------------------------------
// 5.5.5 AI 重构任务与审核
// ---------------------------------------------------------------------------

export interface AiTaskVo {
  id: number;
  source_product_id: number;
  source_product_title: string;
  target_platform: string;
  rework_items: string[];
  template_version: string | null;
  status: string;
  priority: number;
  retry_count: number;
  duration_ms: number | null;
  error_message: string | null;
  created_by: string | null;
  created_at: IsoTimeStr;
}

export interface BannedWordVo {
  word: string;
  type: string;
  suggestion: string | null;
}

export interface AiTaskResultVo {
  id: number;
  ai_task_id: number;
  output_title: string | null;
  output_selling_points: string | null;
  output_attributes_json: Record<string, unknown> | null;
  banned_words: BannedWordVo[];
  output_assets: AssetVo[];
  review_status: string;
  review_note: string | null;
  reviewed_by: string | null;
  reviewed_at: IsoTimeStr | null;
  model_name: string | null;
  created_at: IsoTimeStr | null;
}

export interface AiTaskDetailVo extends AiTaskVo {
  result: AiTaskResultVo | null;
  assets: AssetVo[];
}

export interface AiTaskCreateBody {
  source_product_ids: number[];
  target_platform: string;
  rework_items: string[];
  template_version?: string;
}

export interface AiTaskCreateVo {
  task_ids: number[];
  task_record_ids: number[];
}

export interface AiReviewBody {
  action: 'approve' | 'reject' | 'edit';
  note?: string;
  edited?: {
    title?: string;
    selling_points?: string;
    attributes_json?: Record<string, unknown>;
  };
}

export interface AiConcurrencyVo {
  max_concurrency: number;
  max_retry: number;
}

// ---------------------------------------------------------------------------
// 5.5.6 SKU 映射（核心模块）
// ---------------------------------------------------------------------------

export interface SkuMappingVo {
  id: number;
  platform: string;
  shop_id: string;
  shop_item_id: string;
  shop_sku_code: string;
  shop_sku_name: string | null;
  source_product_id: number | null;
  source_sku_id: number | null;
  source_product_1688_id: string | null;
  source_sku_code_1688: string | null;
  source_sku_name: string | null;
  spec_signature: string | null;
  purchase_cost: MoneyStr;
  cost_currency: string;
  cost_source: string | null;
  cost_overridden_at: IsoTimeStr | null;
  cost_overridden_by: string | null;
  last_cost_check_at: IsoTimeStr | null;
  status: string;
  has_conflict: boolean;
  conflict_types: string[];
  conflict_level: string | null;
  is_mock: boolean;
  source: string;
  effective_at: IsoTimeStr | null;
  last_pushed_at: IsoTimeStr | null;
  last_push_status: string | null;
  is_deleted: boolean;
  deleted_at: IsoTimeStr | null;
  version: number;
  created_by: string | null;
  updated_by: string | null;
  created_at: IsoTimeStr;
  updated_at: IsoTimeStr;
  remark: string | null;
}

export interface SkuMappingCreateBody {
  platform: string;
  shop_id: string;
  shop_item_id: string;
  shop_sku_code: string;
  source_product_1688_id: string;
  source_sku_code_1688: string;
  purchase_cost: MoneyStr;
  status?: string;
  remark?: string;
}

export interface SkuMappingBatchCreateVo {
  created: number;
  updated: number;
  failed: { index: number; reason: string }[];
}

export interface MissingMappingVo {
  shop_sku_code: string;
  reason: string;
}

export interface MappingConflictDetailVo {
  conflict_type: string;
  level: string;
  shop_sku_code: string;
  description: string;
  detail: Record<string, unknown> | null;
}

export interface MappingValidationVo {
  passed: boolean;
  source_product_id: number;
  platform: string;
  shop_id: string;
  checked_sku_count: number;
  missing_mappings: MissingMappingVo[];
  conflicts: MappingConflictDetailVo[];
  blocking: boolean;
  blocked_reason: string | null;
}

export interface MappingConflictVo {
  id: number;
  sku_mapping_id: number | null;
  conflict_type: string;
  level: string;
  shop_sku_code: string | null;
  description: string | null;
  is_resolved: boolean;
  // ★ 原 `non_blocking?` 字段已删除：后端 `MappingConflictVo` 不产出该字段，
  //   保留会让 `if (x.non_blocking)` 恒为 falsy，把 P1 非阻塞冲突误判成阻塞。
  //   “是否阻塞”由 `ConflictBadge` 的 `resolveConflictBlocking(conflictTypes, blockingByValue)`
  //   从枚举元数据现算，不需要后端下发。
  resolved_by: string | null;
  resolved_at: IsoTimeStr | null;
  detected_at: IsoTimeStr;
}

export interface DetectConflictsVo {
  detected: number;
  by_type: Record<string, number>;
}

export interface MappingPendingVo {
  id: number;
  mapping_id: number;
  change_type: 'rename' | 'out_of_stock' | 'spec_change' | 'off_shelf';
  old_value: string | null;
  new_value: string | null;
  detected_at: IsoTimeStr;
  source_product_title: string | null;
  shop_sku_code: string | null;
  affected_order_count: number;
  source: string;
}

export interface MappingPendingResolveBody {
  action: 'confirm' | 'reject' | 'manual_assign';
  new_source_sku_code_1688?: string;
  note?: string;
}

export interface MappingChangeLogVo {
  id: number;
  sku_mapping_id: number;
  change_action: string;
  field_name: string;
  old_value: string | null;
  new_value: string | null;
  change_source: string;
  operator: string | null;
  reason: string | null;
  created_at: IsoTimeStr;
  trace_id: string | null;
}

export interface MappingExportVo {
  download_url: string;
  row_count: number;
  exported_at: IsoTimeStr;
}

export interface MappingImportVo {
  added: number;
  modified: number;
  conflict: number;
  orphan: number;
  report_url: string | null;
}

export interface MappingPushVo {
  pushed: number;
  success: number;
  failed: number;
  degraded: boolean;
  download_url?: string;
}

export interface MappingStatsVo {
  total: number;
  valid: number;
  pending_confirm: number;
  invalid: number;
  conflict_p0: number;
  conflict_p1: number;
  deleted_recent: number;
}

// ---------------------------------------------------------------------------
// 5.5.7 上架任务与半自动模式
// ---------------------------------------------------------------------------

export interface PublishTaskVo {
  id: number;
  source_product_id: number;
  source_product_title: string;
  ai_task_result_id: number | null;
  platform: string;
  shop_id: string;
  listing_mode: string;
  status: string;
  is_mock: boolean;
  shop_item_id: string | null;
  sku_count: number;
  precheck_passed: boolean | null;
  validate_passed: boolean | null;
  platform_error_code: string | null;
  error_advice: string | null;
  package_path: string | null;
  task_record_id: number | null;
  created_by: string | null;
  created_at: IsoTimeStr;
}

export interface PrecheckFailedItemVo {
  item: string;
  reason: string;
  suggestion: string;
}

export interface PrecheckVo {
  passed: boolean;
  failed_items: PrecheckFailedItemVo[];
}

export interface PublishTaskDetailVo extends PublishTaskVo {
  precheck_result: PrecheckVo | null;
  validate_result: MappingValidationVo | null;
  error_advice: string | null;
  platform_error_msg: string | null;
  shop_sku_codes: string[];
  published_at: IsoTimeStr | null;
  updated_at: IsoTimeStr | null;
}

export interface PublishTaskCreateBody {
  source_product_ids: number[];
  platform: string;
  shop_id: string;
  mode?: 'real' | 'mock' | 'manual';
  ai_task_result_ids?: Record<string, number>;
  scheduled_at?: IsoTimeStr;
}

export interface PublishTaskCreateVo {
  task_ids: number[];
  task_record_ids: number[];
}

export interface PublishTaskBatchCreateVo {
  task_ids: number[];
}

/**
 * 半自动预填表单的单条 SKU。
 * 字段与后端 `GET /publish-tasks/manual/{id}/form-data` 实测返回逐字对齐；
 * 金额一律是「元」的字符串（如 "99.00"），不是分。
 */
export interface ManualFormDataSkuVo {
  seq: number;
  spec_json: Record<string, string>;
  /** 已拼好的规格文本，如「颜色:红 / 尺码:XL」，可直接粘到平台后台 */
  spec_text: string;
  sale_price: MoneyStr;
  stock_qty: number;
  source_sku_code_1688: string | null;
  purchase_cost: MoneyStr;
}

/**
 * 半自动预填表单数据（后端实测结构）。
 * `copy_text` 是后端格式化好的 JSON 字符串，前端直接做「一键复制」。
 */
export interface ManualFormDataVo {
  platform: string;
  shop_id: string;
  category_id: string | null;
  title: string;
  /** 后端返回数组（可能为空），不是字符串 */
  selling_points: string[];
  attributes: Record<string, unknown> | null;
  main_image_count: number;
  detail_image_count: number;
  sku_list: ManualFormDataSkuVo[];
  source_product_id: number | null;
  generated_at: IsoTimeStr | null;
  copy_text: string;
}

export interface ManualFillBackSkuBody {
  spec_json: Record<string, string>;
  shop_sku_code: string;
  source_sku_code_1688: string;
  purchase_cost: MoneyStr;
  /** 售价（元）：半自动为主路径后必填，缺失会导致成本倒挂无法检测 */
  sale_price: MoneyStr;
}

export interface ManualFillBackBody {
  shop_item_id: string;
  skus: ManualFillBackSkuBody[];
}

export interface ManualFillBackVo {
  publish_task_id: number;
  mapping_ids: number[];
}

// ---------------------------------------------------------------------------
// 5.5.8 平台商品管理
// ---------------------------------------------------------------------------

/** 平台商品 SKU（售价在 SKU 级，missing_price=true 表示缺售价） */
export interface ListingSkuVo {
  id: number;
  listing_product_id: number;
  shop_sku_code: string;
  spec_json: Record<string, string> | null;
  sale_price: MoneyStr | null;
  status: string;
  missing_price: boolean;
}

export interface ListingProductVo {
  id: number;
  platform: string;
  shop_id: string;
  shop_item_id: string;
  title: string | null;
  source_product_id: number | null;
  source_product_title: string | null;
  status: string;
  is_mock: boolean;
  /** 产出该商品时的上架模式 real / mock / manual */
  listing_mode: string;
  /** 缺售价的 SKU 数量（>0 时无法做成本倒挂检测） */
  missing_price_count: number;
  offline_reason: string | null;
  offline_at: IsoTimeStr | null;
  published_at: IsoTimeStr | null;
  created_at: IsoTimeStr | null;
  updated_at: IsoTimeStr | null;
  sku_count: number;
}

export interface ListingProductDetailVo extends ListingProductVo {
  skus: ListingSkuVo[];
  mappings: SkuMappingVo[];
}

export interface ListingOfflineBody {
  reason: string;
}

/** POST /listing-products/{id}/fill-price 的单条售价 */
export interface FillPriceItemBody {
  shop_sku_code: string;
  /** 售价（元），必填且 > 0，否则后端 422（code 1001） */
  sale_price: MoneyStr;
}

/** POST /listing-products/{id}/fill-price 请求体 */
export interface FillPriceBody {
  items: FillPriceItemBody[];
  reason?: string;
}

/** 补填后重算出的新增倒挂冲突 */
export interface FillPriceNewConflictVo {
  sku_code: string;
  conflict_type: string;
  level: string;
}

/** POST /listing-products/{id}/fill-price 返回体（实测对齐） */
export interface FillPriceVo {
  /** 成功补填的 SKU 数 */
  updated: number;
  /** 触发重算的 SKU 数 */
  recomputed: number;
  /** 重算后新产生的成本倒挂冲突 */
  new_conflicts: FillPriceNewConflictVo[];
}

export interface BatchOfflineVo {
  success: number[];
  failed: { id: number; reason: string }[];
}

// ---------------------------------------------------------------------------
// 5.5.9 订单与履约
// ---------------------------------------------------------------------------

export interface OrderVo {
  id: number;
  platform: string;
  shop_id: string;
  platform_order_no: string;
  buyer_masked: string | null;
  receiver_masked: string | null;
  total_amount: MoneyStr;
  paid_at: IsoTimeStr | null;
  fulfillment_status: string;
  adapter_name: string;
  match_status: string;
  exception_type: string | null;
  exception_note: string | null;
  is_mock: boolean;
  item_count: number;
  created_at: IsoTimeStr;
}

export interface OrderItemVo {
  id: number;
  order_id: number;
  platform_order_item_no: string | null;
  shop_item_id: string;
  shop_sku_code: string;
  sku_mapping_id: number | null;
  source_sku_code_1688: string | null;
  quantity: number;
  purchase_cost: MoneyStr | null;
  sale_price: MoneyStr | null;
  match_status: string;
  purchase_order_id: number | null;
}

export interface PurchaseOrderVo {
  id: number;
  order_id: number;
  purchase_order_no: string | null;
  supplier_id: number | null;
  amount: MoneyStr | null;
  /**
   * 采购状态。local_csv 本地兜底通道下为 `manual_pending`——
   * 语义是「待人工在 1688 下单」，前端必须展示为待人工处理，禁止美化成「已下单」。
   */
  purchase_status: string;
  writeback_status: string | null;
  writeback_retry: number;
  logistics_company: string | null;
  tracking_no: string | null;
  shipped_at: IsoTimeStr | null;
  adapter_name: string | null;
  created_at: IsoTimeStr | null;
}

export interface AfterSaleVo {
  id: number;
  order_id: number;
  platform_refund_no: string | null;
  refund_amount: MoneyStr | null;
  refund_reason: string | null;
  handling_status: string;
  responsibility: string | null;
  refund_1688_status: string | null;
  refund_1688_no: string | null;
  return_address_json: Record<string, string> | null;
  return_address_push_status: string | null;
  created_at: IsoTimeStr | null;
}

export interface OrderDetailVo extends OrderVo {
  items: OrderItemVo[];
  purchase_orders: PurchaseOrderVo[];
  after_sales: AfterSaleVo[];
  handling_action: string | null;
  handled_by: string | null;
  handled_at: IsoTimeStr | null;
  updated_at: IsoTimeStr | null;
}

export interface OrderSyncVo {
  task_record_id: number;
  fetched: number;
  new: number;
  duplicated: number;
}

export interface OrderBoardColumnVo {
  status: string;
  count: number;
  orders: OrderVo[];
}

export interface OrderBoardVo {
  columns: OrderBoardColumnVo[];
  total: number;
}

export interface OrderMatchBody {
  sku_mapping_id?: number;
  source_sku_id?: number;
  create_mapping?: boolean;
}

export interface OrderMatchVo {
  order_id: number;
  match_status: string;
  mapping_id: number | null;
}

export interface OrderExceptionVo {
  id: number;
  order_id: number;
  platform: string;
  platform_order_no: string;
  exception_type: string | null;
  exception_note: string | null;
  fulfillment_status: string;
  adapter_name: string;
  handled: boolean;
  handled_by: string | null;
  handled_at: IsoTimeStr | null;
  detected_at: IsoTimeStr;
}

export interface OrderActionBody {
  action: 'retry' | 'switch_source' | 'refund' | 'ignore';
  payload?: {
    source_sku_code_1688?: string;
    reason?: string;
  };
}

export interface OrderActionVo {
  order_id: number;
  fulfillment_status: string;
  message: string;
}

export interface OrderTimelineEventVo {
  at: IsoTimeStr;
  from_status: string | null;
  to_status: string | null;
  operator: string | null;
  note: string | null;
  trace_id: string | null;
}

export interface OrderTimelineVo {
  events: OrderTimelineEventVo[];
}

export interface PurchaseWritebackVo {
  writeback_status: string;
  retry: number;
}

export interface TrackingBody {
  logistics_company: string;
  tracking_no: string;
}

// ---------------------------------------------------------------------------
// 5.5.10 售后
// ---------------------------------------------------------------------------

export interface SubmitRefundBody {
  refund_amount: MoneyStr;
  reason: string;
}

export interface SubmitRefundVo {
  refund_1688_status: string;
  refund_1688_no: string | null;
}

export interface ReturnAddressVo {
  return_address_json: Record<string, string> | null;
  return_address_push_status: string;
  /** 能力不支持时后端返回 503 并在 message 中给出人工兜底提示 */
  degraded?: boolean;
}

export interface ResponsibilityBody {
  responsibility: 'our_shop' | 'supplier' | 'buyer' | 'platform';
  note?: string;
}

// ---------------------------------------------------------------------------
// 5.5.11 库存与价格
// ---------------------------------------------------------------------------

export interface InventorySnapshotVo {
  id: number;
  source_sku_id: number;
  source_sku_name: string | null;
  source_product_title: string | null;
  stock_qty: number | null;
  /** 快照来源：erp_poll / adapter */
  source: string | null;
  collected_at: IsoTimeStr;
}

export interface PriceSnapshotVo {
  id: number;
  source_sku_id: number;
  source_sku_name: string | null;
  cost: MoneyStr;
  prev_cost: MoneyStr | null;
  change_rate: string;
  over_threshold: boolean;
  collected_at: IsoTimeStr;
}

export interface InventoryAlertVo {
  id: number;
  source_sku_id: number;
  source_sku_name: string | null;
  source_product_title: string | null;
  type: 'out_of_stock' | 'price_increase';
  current_stock: number | null;
  current_cost: MoneyStr | null;
  prev_cost: MoneyStr | null;
  change_rate: string;
  threshold: string;
  suggested_action: 'offline' | 'notify';
  detected_at: IsoTimeStr;
  /** ★ F11 库存数据源。后端 `InventoryService._fill_alert_source()` 回填。 */
  data_source: string;
  /** ★ F11 可否自动下架。false ⇒ 只告警，不自动执行，需人工一键下架。 */
  auto_offline_allowed: boolean;
  /**
   * 与该告警 source_sku_id 关联、且**当前不是已下架状态**的平台商品 ID 列表。
   * 已排除已下架商品；**可能为空**（没有关联平台商品，或关联商品都已下架）。
   * 后端未落盘时该字段不返回，前端一律用 `?? []` 兜底，禁止 `!` 断言。
   */
  listing_product_ids: number[];
}

export interface AutoOfflineRecordVo {
  id: number;
  listing_product_id: number;
  shop_item_id: string | null;
  platform: string | null;
  title: string | null;
  reason: string | null;
  trigger_type: string | null;
  success: boolean;
  message: string | null;
  created_at: IsoTimeStr;
}

export interface InventorySyncVo {
  task_record_id: number;
  snapshot_count: number;
}

export interface InventoryConfigVo {
  poll_interval_min: number;
  price_increase_threshold: string;
  out_of_stock_action: string;
  price_increase_action: string;
}

// ---------------------------------------------------------------------------
// 5.5.12 适配器与权限
// ---------------------------------------------------------------------------

export interface ListingAdapterItemVo {
  platform: string;
  mode: string;
  available: boolean;
  note: string | null;
}

export interface ListingAdaptersVo {
  current_mode: string;
  adapters: ListingAdapterItemVo[];
}

export interface ListingModeUpdateVo {
  mode: string;
  updated_at: IsoTimeStr;
  audit_id: number | null;
}

export interface FulfillmentAdapterVo {
  id: number;
  adapter_name: string;
  display_name: string;
  capability_json: Record<string, unknown> | null;
  declared_scopes: string[];
  scope_check_status: string;
  scope_check_message: string | null;
  is_enabled: boolean;
  is_active: boolean;
  health_status: string;
  last_heartbeat_at: IsoTimeStr | null;
  heartbeat_fail_count: number;
  config_json: Record<string, unknown> | null;
}

export interface FulfillmentAdaptersVo {
  active_adapter: string;
  adapters: FulfillmentAdapterVo[];
}

export interface CapabilitySpecVo {
  name: string;
  level: string;
  fallback: string | null;
  note: string;
  unverified: boolean;
}

export interface AdapterCapabilitiesVo {
  adapter_name: string;
  manifest: {
    capabilities: CapabilitySpecVo[];
  };
}

export interface AdapterConfigUpdateBody {
  credential_id?: number;
  config_json?: Record<string, unknown>;
  declared_scopes?: string[];
  is_enabled?: boolean;
}

export interface AdapterTestVo {
  ok: boolean;
  latency_ms: number;
  message: string;
  checked_at: IsoTimeStr;
}

export interface AdapterSwitchBody {
  adapter_name: string;
  reason: string;
  drain_inflight?: boolean;
}

export interface AdapterSwitchVo {
  active_adapter: string;
  inflight_order_count: number;
  audit_id: number | null;
}

export interface ScopePoliciesVo {
  allowed: string[];
  forbidden: string[];
  description: string;
}

export interface ScopeViolationVo {
  id: number;
  adapter_name: string | null;
  /** 归属平台（后端可为 null） */
  platform: string | null;
  /** 适配器当次声明的全部 scope */
  declared_scopes: string[];
  /** 被系统拒绝的 scope —— 非空才算真正的越权事件 */
  denied_scopes: string[];
  /** 白名单外的未知 scope（后端一并拒绝） */
  unknown_scopes?: string[];
  operator: string | null;
  message: string | null;
  trace_id: string | null;
  /** 是否已处置（未处置的行需要运营在 P17 页面逐条确认） */
  is_handled: boolean;
  /** 未处置标记（后端 GET /adapters/violations?is_unhandled=true 使用同一语义） */
  is_unhandled?: boolean;
  handle_note: string | null;
  handled_by: string | null;
  handled_at: IsoTimeStr | null;
  created_at: IsoTimeStr;
}

export interface ViolationHandleBody {
  handle_note: string;
}

export interface ViolationHandleVo {
  id: number;
  is_handled: boolean;
  handled_by: string | null;
  handled_at: IsoTimeStr | null;
}

/** 顶栏展示用的「最近一条越权告警」摘要 */
export interface StatusBarLatestViolationVo {
  id: number;
  adapter_name: string | null;
  denied_scopes: string[];
  created_at: IsoTimeStr | null;
}

/** 顶栏健康明细项 */
export interface StatusBarUnhealthyItemVo {
  name: string;
  status: string;
  message: string;
}

/**
 * GET /system/status-bar 返回体（顶栏聚合数据）。
 * 字段名与后端 StatusBarVo **逐字对齐**；该端点只读 SystemSetting 缓存 + audit_log 计数，
 * 不触发外部 HTTP，可 60s 安全轮询（实测 ~70ms）。顶栏禁止再拆成 /health + /adapters/* 多个轮询。
 */
export interface SystemStatusBarVo {
  /** 未处置的越权告警数（>0 顶栏红点告警） */
  unhandled_violation_count: number;
  /** 最近一条越权告警（用于 Tooltip 提示） */
  latest_violation: StatusBarLatestViolationVo | null;
  /** 当前上架模式 real / mock / manual */
  listing_mode: string;
  /** 上架模式中文标签（后端下发） */
  listing_mode_label: string;
  /** 当前生效履约适配器 */
  active_fulfillment_adapter: string;
  /** 履约适配器中文标签（后端下发） */
  active_fulfillment_adapter_label: string;
  /** Mock 模式是否生效（决定顶栏 Mock 提示条） */
  is_mock_active: boolean;
  /** 健康状态：healthy / degraded / down */
  health_status: string;
  /** 不健康项明细（名称 + 状态 + 说明） */
  unhealthy_items: StatusBarUnhealthyItemVo[];
}

// ---------------------------------------------------------------------------
// 5.5.13 系统设置 / 审计 / 任务
// ---------------------------------------------------------------------------

export interface SettingItemVo {
  value: string;
  value_type: string;
  description: string;
  updated_by: string | null;
  updated_at: IsoTimeStr | null;
}

export interface SettingsVo {
  settings: Record<string, SettingItemVo>;
}

export interface SettingUpdateVo {
  key: string;
  value: string;
  updated_at: IsoTimeStr;
  audit_id: number | null;
}

export interface AuditLogVo {
  id: number;
  action_type: string;
  object_type: string;
  object_id: string | null;
  operator: string | null;
  operator_role: string | null;
  old_value: string | null;
  new_value: string | null;
  reason: string | null;
  ip: string | null;
  trace_id: string | null;
  created_at: IsoTimeStr;
}

export interface AuditExportVo {
  download_url: string;
  row_count: number;
}

export interface TaskRecordVo {
  id: number;
  task_type: string;
  task_key: string | null;
  status: string;
  priority: number;
  retry_count: number;
  max_retry: number;
  scheduled_at: IsoTimeStr | null;
  started_at: IsoTimeStr | null;
  finished_at: IsoTimeStr | null;
  duration_ms: number | null;
  error_code: string | null;
  error_message: string | null;
  result_json: Record<string, unknown> | null;
  trace_id: string | null;
  created_at: IsoTimeStr | null;
}

/** POST /system/backup 的返回体 */
export interface BackupResultVo {
  ok: boolean;
  file_name: string;
  /** 相对 data/ 的路径，方便直接到宿主机上取文件 */
  path: string;
  size_bytes: number;
  /** 主库是否纳入了备份（非 SQLite 时为 false） */
  db_included: boolean;
  /** 超出保留上限被自动清理的旧备份数量 */
  pruned: number;
  operator: string | null;
}

/** GET /system/backups 的单条备份记录 */
export interface BackupItemVo {
  file_name: string;
  path: string;
  size_bytes: number;
  created_at: IsoTimeStr;
}

export interface BackupListVo {
  items: BackupItemVo[];
  total: number;
  max_keep: number;
}
