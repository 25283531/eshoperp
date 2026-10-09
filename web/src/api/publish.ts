/**
 * 上架域 API：上架任务 / 半自动素材包 / 平台商品。
 * 对应 ARCHITECTURE.md §5.5.7（LST）与 §5.5.8（LST-P0-03）。
 * 注意：POST /publish-tasks 可能返回 422（映射校验不通过或素材未审核），
 *       页面需 catch ApiError 并展示 MappingValidationVo 明细（无绕过路径）。
 */
import { fileUrl, http } from './client';
import type {
  BatchOfflineVo,
  FillPriceBody,
  FillPriceVo,
  ListingOfflineBody,
  ListingProductDetailVo,
  ListingProductVo,
  ManualFillBackBody,
  ManualFillBackVo,
  ManualFormDataVo,
  PageResult,
  PrecheckVo,
  PublishTaskBatchCreateVo,
  PublishTaskCreateBody,
  PublishTaskCreateVo,
  PublishTaskDetailVo,
  PublishTaskVo,
} from './types';

// ------------------------- 上架任务 -------------------------

/** POST /publish-tasks —— 202 / 422 */
export function createPublishTasks(body: PublishTaskCreateBody): Promise<PublishTaskCreateVo> {
  return http.post<PublishTaskCreateVo>('/publish-tasks', body);
}

/** POST /publish-tasks/batch（≤50） */
export function batchCreatePublishTasks(body: {
  source_product_ids: number[];
  platform: string;
  shop_id: string;
  mode: 'real' | 'mock' | 'manual';
}): Promise<PublishTaskBatchCreateVo> {
  return http.post<PublishTaskBatchCreateVo>('/publish-tasks/batch', body);
}

/** GET /publish-tasks */
export function listPublishTasks(
  params: Record<string, unknown> = {},
): Promise<PageResult<PublishTaskVo>> {
  return http.get<PageResult<PublishTaskVo>>('/publish-tasks', params);
}

/** GET /publish-tasks/{id} */
export function getPublishTask(id: number): Promise<PublishTaskDetailVo> {
  return http.get<PublishTaskDetailVo>(`/publish-tasks/${id}`);
}

/** POST /publish-tasks/{id}/precheck */
export function precheckPublishTask(id: number): Promise<PrecheckVo> {
  return http.post<PrecheckVo>(`/publish-tasks/${id}/precheck`);
}

/** POST /publish-tasks/{id}/retry */
export function retryPublishTask(id: number): Promise<{ id: number; status: string }> {
  return http.post<{ id: number; status: string }>(`/publish-tasks/${id}/retry`);
}

/** POST /publish-tasks/{id}/cancel */
export function cancelPublishTask(id: number): Promise<{ id: number; status: string }> {
  return http.post<{ id: number; status: string }>(`/publish-tasks/${id}/cancel`);
}

// ------------------------- 半自动模式（素材包） -------------------------

/** GET /publish-tasks/manual/{id}/package —— ZIP 文件流地址 */
export function manualPackageUrl(id: number): string {
  return fileUrl(`/publish-tasks/manual/${id}/package`);
}

/** GET /publish-tasks/manual/{id}/form-data */
export function getManualFormData(id: number): Promise<ManualFormDataVo> {
  return http.get<ManualFormDataVo>(`/publish-tasks/manual/${id}/form-data`);
}

/** POST /publish-tasks/manual/{id}/fill-back（回填后自动建映射） */
export function fillBackManual(id: number, body: ManualFillBackBody): Promise<ManualFillBackVo> {
  return http.post<ManualFillBackVo>(`/publish-tasks/manual/${id}/fill-back`, body);
}

// ------------------------- 平台商品 -------------------------

/** GET /listing-products */
export function listListingProducts(
  params: Record<string, unknown> = {},
): Promise<PageResult<ListingProductVo>> {
  return http.get<PageResult<ListingProductVo>>('/listing-products', params);
}

/** GET /listing-products/{id} */
export function getListingProduct(id: number): Promise<ListingProductDetailVo> {
  return http.get<ListingProductDetailVo>(`/listing-products/${id}`);
}

/** POST /listing-products/{id}/offline —— 唯一的下架入口（红线 R2） */
export function offlineListingProduct(id: number, body: ListingOfflineBody): Promise<{
  id: number;
  status: string;
}> {
  return http.post<{ id: number; status: string }>(`/listing-products/${id}/offline`, body);
}

/** POST /listing-products/{id}/online */
export function onlineListingProduct(
  id: number,
  revalidateMapping = true,
): Promise<{ id: number; status: string }> {
  return http.post<{ id: number; status: string }>(`/listing-products/${id}/online`, {
    revalidate_mapping: revalidateMapping,
  });
}

/**
 * POST /listing-products/{id}/fill-price —— 存量商品补填售价。
 * 售价按 SKU 逐条提交（items[].shop_sku_code + items[].sale_price），
 * 是成本倒挂（cost_underwater）检测的前置数据；缺失售将无法判断亏损铺货。
 */
export function fillListingProductPrice(id: number, body: FillPriceBody): Promise<FillPriceVo> {
  return http.post<FillPriceVo>(`/listing-products/${id}/fill-price`, body);
}

/** POST /listing-products/batch-offline */
export function batchOfflineListingProducts(
  ids: number[],
  reason: string,
): Promise<BatchOfflineVo> {
  return http.post<BatchOfflineVo>('/listing-products/batch-offline', { ids, reason });
}
