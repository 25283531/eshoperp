/**
 * SKU 映射域 API（核心模块）。
 * 对应 ARCHITECTURE.md §5.5.6（MAP）：CRUD / 校验 / 冲突 / 待确认 / 导出导入推送 / 变更日志。
 */
import { http } from './client';
import type {
  DetectConflictsVo,
  MappingChangeLogVo,
  MappingConflictVo,
  MappingExportVo,
  MappingImportVo,
  MappingPendingResolveBody,
  MappingPendingVo,
  MappingPushVo,
  MappingStatsVo,
  MappingValidationVo,
  PageResult,
  SkuMappingBatchCreateVo,
  SkuMappingCreateBody,
  SkuMappingVo,
} from './types';

/** GET /sku-mappings */
export function listSkuMappings(
  params: Record<string, unknown> = {},
): Promise<PageResult<SkuMappingVo>> {
  return http.get<PageResult<SkuMappingVo>>('/sku-mappings', params);
}

/** POST /sku-mappings */
export function createSkuMapping(body: SkuMappingCreateBody): Promise<SkuMappingVo> {
  return http.post<SkuMappingVo>('/sku-mappings', body);
}

/** PUT /sku-mappings/{id}（自动写 mapping_change_log） */
export function updateSkuMapping(
  id: number,
  body: Partial<SkuMappingCreateBody> & {
    status?: string;
    source_sku_code_1688?: string;
    purchase_cost?: string;
    remark?: string;
  },
): Promise<SkuMappingVo> {
  return http.put<SkuMappingVo>(`/sku-mappings/${id}`, body);
}

/** DELETE /sku-mappings/{id}（二次确认软删除，保留 180 天） */
export function deleteSkuMapping(id: number, reason: string): Promise<{ id: number }> {
  return http.delete<{ id: number }>(`/sku-mappings/${id}`, { reason, confirm: true });
}

/** POST /sku-mappings/{id}/restore */
export function restoreSkuMapping(id: number): Promise<SkuMappingVo> {
  return http.post<SkuMappingVo>(`/sku-mappings/${id}/restore`);
}

/** POST /sku-mappings/batch（≤200） */
export function batchCreateSkuMappings(
  items: SkuMappingCreateBody[],
): Promise<SkuMappingBatchCreateVo> {
  return http.post<SkuMappingBatchCreateVo>('/sku-mappings/batch', { items });
}

/** POST /sku-mappings/validate —— 不通过返回 422 */
export function validateSkuMappings(body: {
  source_product_id: number;
  platform: string;
  shop_id: string;
  sku_codes: string[];
}): Promise<MappingValidationVo> {
  return http.post<MappingValidationVo>('/sku-mappings/validate', body);
}

/** GET /sku-mappings/conflicts */
export function listMappingConflicts(
  params: Record<string, unknown> = {},
): Promise<PageResult<MappingConflictVo>> {
  return http.get<PageResult<MappingConflictVo>>('/sku-mappings/conflicts', params);
}

/** POST /sku-mappings/detect-conflicts —— 202 */
export function detectMappingConflicts(body: {
  source_product_ids?: number[];
  all?: boolean;
}): Promise<DetectConflictsVo> {
  return http.post<DetectConflictsVo>('/sku-mappings/detect-conflicts', body);
}

/** GET /sku-mappings/pending（待确认工单） */
export function listMappingPending(
  params: Record<string, unknown> = {},
): Promise<PageResult<MappingPendingVo>> {
  return http.get<PageResult<MappingPendingVo>>('/sku-mappings/pending', params);
}

/** POST /sku-mappings/pending/{id}/resolve */
export function resolveMappingPending(
  id: number,
  body: MappingPendingResolveBody,
): Promise<SkuMappingVo> {
  return http.post<SkuMappingVo>(`/sku-mappings/pending/${id}/resolve`, body);
}

/** POST /sku-mappings/export（CSV） */
export function exportSkuMappings(body: {
  adapter_name: 'miaoshou' | 'yitao' | 'generic';
  platform?: string;
  shop_id?: string;
  updated_from?: string;
  updated_to?: string;
}): Promise<MappingExportVo> {
  return http.post<MappingExportVo>('/sku-mappings/export', body);
}

/** POST /sku-mappings/import（multipart: file） */
export function importSkuMappings(file: File): Promise<MappingImportVo> {
  const formData = new FormData();
  formData.append('file', file);
  return http.post<MappingImportVo>('/sku-mappings/import', formData);
}

/** POST /sku-mappings/push —— 202 */
export function pushSkuMappings(body: {
  adapter_name: string;
  ids?: number[];
  all_valid?: boolean;
}): Promise<MappingPushVo> {
  return http.post<MappingPushVo>('/sku-mappings/push', body);
}

/** GET /sku-mappings/{id}/logs（完整变更历史） */
export function listMappingLogs(id: number): Promise<MappingChangeLogVo[]> {
  return http.get<MappingChangeLogVo[]>(`/sku-mappings/${id}/logs`);
}

/** GET /sku-mappings/stats */
export function getMappingStats(): Promise<MappingStatsVo> {
  return http.get<MappingStatsVo>('/sku-mappings/stats');
}
