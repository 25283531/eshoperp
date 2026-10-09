/**
 * 履约域 API：订单 / 采购单 / 售后 / 库存与价格。
 * 对应 ARCHITECTURE.md §5.5.9（ORD/FUL）、§5.5.10（ORD-P1-02）、§5.5.11（INV）。
 */
import { http } from './client';
import type {
  AfterSaleVo,
  AutoOfflineRecordVo,
  InventoryAlertVo,
  InventoryConfigVo,
  InventorySnapshotVo,
  InventorySyncVo,
  OrderActionBody,
  OrderActionVo,
  OrderBoardVo,
  OrderDetailVo,
  OrderExceptionVo,
  OrderMatchBody,
  OrderMatchVo,
  OrderSyncVo,
  OrderTimelineVo,
  OrderVo,
  PageResult,
  PriceSnapshotVo,
  PurchaseOrderVo,
  PurchaseWritebackVo,
  ResponsibilityBody,
  ReturnAddressVo,
  SubmitRefundBody,
  SubmitRefundVo,
  TrackingBody,
} from './types';

// ------------------------- 订单 -------------------------

/** POST /orders/sync —— 202 */
export function syncOrders(body: {
  adapter_name?: string;
  shop_ids?: string[];
  force?: boolean;
}): Promise<OrderSyncVo> {
  return http.post<OrderSyncVo>('/orders/sync', body);
}

/** GET /orders */
export function listOrders(params: Record<string, unknown> = {}): Promise<PageResult<OrderVo>> {
  return http.get<PageResult<OrderVo>>('/orders', params);
}

/** GET /orders/board */
export function getOrderBoard(params: Record<string, unknown> = {}): Promise<OrderBoardVo> {
  return http.get<OrderBoardVo>('/orders/board', params);
}

/** GET /orders/{id} */
export function getOrder(id: number): Promise<OrderDetailVo> {
  return http.get<OrderDetailVo>(`/orders/${id}`);
}

/** POST /orders/{id}/match —— 手工指定货源 SKU 并补建映射 */
export function matchOrder(id: number, body: OrderMatchBody = {}): Promise<OrderMatchVo> {
  return http.post<OrderMatchVo>(`/orders/${id}/match`, body);
}

/** GET /orders/exceptions */
export function listOrderExceptions(
  params: Record<string, unknown> = {},
): Promise<PageResult<OrderExceptionVo>> {
  return http.get<PageResult<OrderExceptionVo>>('/orders/exceptions', params);
}

/** POST /orders/{id}/actions —— 重试 / 换货源 / 退款 / 忽略 */
export function submitOrderAction(id: number, body: OrderActionBody): Promise<OrderActionVo> {
  return http.post<OrderActionVo>(`/orders/${id}/actions`, body);
}

/** GET /orders/{id}/timeline */
export function getOrderTimeline(id: number): Promise<OrderTimelineVo> {
  return http.get<OrderTimelineVo>(`/orders/${id}/timeline`);
}

// ------------------------- 采购单 -------------------------

/** GET /purchase-orders */
export function listPurchaseOrders(
  params: Record<string, unknown> = {},
): Promise<PageResult<PurchaseOrderVo>> {
  return http.get<PageResult<PurchaseOrderVo>>('/purchase-orders', params);
}

/** GET /purchase-orders/{id} */
export function getPurchaseOrder(id: number): Promise<PurchaseOrderVo> {
  return http.get<PurchaseOrderVo>(`/purchase-orders/${id}`);
}

/** POST /purchase-orders/{id}/writeback */
export function writebackPurchaseOrder(id: number): Promise<PurchaseWritebackVo> {
  return http.post<PurchaseWritebackVo>(`/purchase-orders/${id}/writeback`);
}

/** POST /purchase-orders/{id}/tracking（本地兜底手工录入） */
export function setPurchaseOrderTracking(id: number, body: TrackingBody): Promise<PurchaseOrderVo> {
  return http.post<PurchaseOrderVo>(`/purchase-orders/${id}/tracking`, body);
}

// ------------------------- 售后 -------------------------

/** GET /after-sales */
export function listAfterSales(
  params: Record<string, unknown> = {},
): Promise<PageResult<AfterSaleVo>> {
  return http.get<PageResult<AfterSaleVo>>('/after-sales', params);
}

/** GET /after-sales/{id} */
export function getAfterSale(id: number): Promise<AfterSaleVo> {
  return http.get<AfterSaleVo>(`/after-sales/${id}`);
}

/** POST /after-sales/{id}/submit-refund */
export function submitRefund(id: number, body: SubmitRefundBody): Promise<SubmitRefundVo> {
  return http.post<SubmitRefundVo>(`/after-sales/${id}/submit-refund`, body);
}

/** POST /after-sales/{id}/return-address */
export function getReturnAddress(id: number): Promise<ReturnAddressVo> {
  return http.post<ReturnAddressVo>(`/after-sales/${id}/return-address`);
}

/** PUT /after-sales/{id}/responsibility */
export function updateResponsibility(id: number, body: ResponsibilityBody): Promise<AfterSaleVo> {
  return http.put<AfterSaleVo>(`/after-sales/${id}/responsibility`, body);
}

// ------------------------- 库存与价格 -------------------------

/** POST /inventory/sync —— 202 */
export function syncInventory(body: {
  source_sku_ids?: number[];
  force?: boolean;
} = {}): Promise<InventorySyncVo> {
  return http.post<InventorySyncVo>('/inventory/sync', body);
}

/** GET /inventory/snapshots */
export function listInventorySnapshots(
  params: Record<string, unknown> = {},
): Promise<PageResult<InventorySnapshotVo>> {
  return http.get<PageResult<InventorySnapshotVo>>('/inventory/snapshots', params);
}

/** GET /inventory/price-snapshots */
export function listPriceSnapshots(
  params: Record<string, unknown> = {},
): Promise<PageResult<PriceSnapshotVo>> {
  return http.get<PageResult<PriceSnapshotVo>>('/inventory/price-snapshots', params);
}

/** GET /inventory/alerts */
export function listInventoryAlerts(
  params: Record<string, unknown> = {},
): Promise<PageResult<InventoryAlertVo>> {
  return http.get<PageResult<InventoryAlertVo>>('/inventory/alerts', params);
}

/** GET /inventory/auto-offline-records */
export function listAutoOfflineRecords(
  params: Record<string, unknown> = {},
): Promise<PageResult<AutoOfflineRecordVo>> {
  return http.get<PageResult<AutoOfflineRecordVo>>('/inventory/auto-offline-records', params);
}

/** GET /inventory/config */
export function getInventoryConfig(): Promise<InventoryConfigVo> {
  return http.get<InventoryConfigVo>('/inventory/config');
}

/** PUT /inventory/config */
export function updateInventoryConfig(
  body: Partial<InventoryConfigVo>,
): Promise<InventoryConfigVo> {
  return http.put<InventoryConfigVo>('/inventory/config', body, undefined, { admin: true });
}
