/**
 * 货源域 API：供应商 / 货源商品 / 采集 / 素材库。
 * 对应 ARCHITECTURE.md §5.5.3（SRC）与 §5.5.4（AST）。
 */
import { fileUrl, http } from './client';
import type {
  AcceptedTaskVo,
  AssetVo,
  BatchDownloadVo,
  CollectBody,
  CsvImportResultVo,
  ManualSourceProductBody,
  ManualSourceProductVo,
  PageResult,
  SourceProductDetailVo,
  SourceProductVo,
  SourceSkuVo,
  SupplierCreateBody,
  SupplierVo,
} from './types';

// ------------------------- 供应商 -------------------------

/** GET /suppliers */
export function listSuppliers(params: Record<string, unknown> = {}): Promise<PageResult<SupplierVo>> {
  return http.get<PageResult<SupplierVo>>('/suppliers', params);
}

/** POST /suppliers */
export function createSupplier(body: SupplierCreateBody): Promise<SupplierVo> {
  return http.post<SupplierVo>('/suppliers', body);
}

/** PUT /suppliers/{id} */
export function updateSupplier(id: number, body: Partial<SupplierCreateBody>): Promise<SupplierVo> {
  return http.put<SupplierVo>(`/suppliers/${id}`, body);
}

/** DELETE /suppliers/{id}（软删除） */
export function deleteSupplier(id: number): Promise<{ id: number }> {
  return http.delete<{ id: number }>(`/suppliers/${id}`);
}

// ------------------------- 货源商品 -------------------------

/** POST /source-products/collect —— 202 */
export function collectSourceProducts(body: CollectBody): Promise<AcceptedTaskVo> {
  return http.post<AcceptedTaskVo>('/source-products/collect', body);
}

/** POST /source-products/batch-import —— 202（★ 走 1688 适配器，未配置 AppKey 时拿不到数据） */
export function batchImportSourceProducts(body: {
  identifiers: string[];
  supplier_id?: number;
}): Promise<AcceptedTaskVo> {
  return http.post<AcceptedTaskVo>('/source-products/batch-import', body);
}

/**
 * POST /source-products/import-csv —— CSV 批量导入（**multipart/form-data**，同步返回结果）。
 *
 * ★ 为什么它是「批量导入」的主入口：`batch-import` 走 1688 开放接口，
 *   在 AppKey/AccessToken 未配置时必然 0 条且不报错，使用者会一脸茫然。
 *   `import-csv` 不依赖外部平台，逐行解析并**逐行回执**（哪一行错、为什么错）。
 *
 * 注意：不要手动设置 Content-Type boundary，`http.post` 检测到 FormData 会自动处理。
 */
export function importSourceProductsCsv(file: File): Promise<CsvImportResultVo> {
  const form = new FormData();
  form.append('file', file);
  return http.post<CsvImportResultVo>('/source-products/import-csv', form);
}

/** POST /source-products/manual —— 手工录入（同步返回，201） */
export function createSourceProductManual(
  body: ManualSourceProductBody,
): Promise<ManualSourceProductVo> {
  return http.post<ManualSourceProductVo>('/source-products/manual', body);
}

/**
 * CSV 导入模板（UTF-8 带 BOM）。
 *
 * 列顺序与后端 `backend/scripts/source_products_template.csv` 逐字一致。
 * 后端没有暴露模板下载接口（未挂静态目录），这里在前端生成，
 * 保证离线可用且与解析器同版本；权威文件仍以后端 scripts 目录下那份为准。
 */
export const CSV_TEMPLATE: string = [
  '商品编码,商品标题,类目,供应商ID,商品成本价,原链接,主图URL,商品状态,SKU编码,规格名,规格值,SKU成本价,售价,库存,备注',
  'DEMO-1001,纯棉圆领短袖T恤 夏季薄款,女装/上装/T恤,1,18.00,https://example.com/item/DEMO-1001,https://example.com/img/DEMO-1001-1.jpg,on_sale,DEMO-1001-RED-XL,颜色;尺码,红色;XL,18.00,59.90,200,示例行：多规格用英文分号分隔',
  'DEMO-1001,,,,,,,,DEMO-1001-BLUE-L,颜色;尺码,蓝色;L,17.50,55.90,150,同一商品的第二行：商品编码相同即可',
  'DEMO-1002,304不锈钢保温杯 500ml,家居/水具/保温杯,1,22.00,https://example.com/item/DEMO-1002,https://example.com/img/DEMO-1002-1.jpg,on_sale,,容量,500ml,,49.00,80,SKU编码留空 ⇒ 由「商品编码 + 规格指纹」自动生成',
].join('\r\n');

/** 下载 CSV 模板（带 BOM，避免 Excel 打开乱码） */
export function downloadCsvTemplate(): void {
  // \uFEFF 即 UTF-8 BOM，Excel 靠它识别编码
  const blob = new Blob([`\uFEFF${CSV_TEMPLATE}`], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'source_products_template.csv';
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

/** GET /source-products */
export function listSourceProducts(
  params: Record<string, unknown> = {},
): Promise<PageResult<SourceProductVo>> {
  return http.get<PageResult<SourceProductVo>>('/source-products', params);
}

/** GET /source-products/{id} */
export function getSourceProduct(id: number): Promise<SourceProductDetailVo> {
  return http.get<SourceProductDetailVo>(`/source-products/${id}`);
}

/** GET /source-products/{id}/skus */
export function listSourceSkus(id: number): Promise<SourceSkuVo[]> {
  return http.get<SourceSkuVo[]>(`/source-products/${id}/skus`);
}

/** DELETE /source-products/{id}（软删除） */
export function deleteSourceProduct(id: number): Promise<{ id: number }> {
  return http.delete<{ id: number }>(`/source-products/${id}`);
}

// ------------------------- 素材库 -------------------------

/** GET /assets */
export function listAssets(params: Record<string, unknown> = {}): Promise<PageResult<AssetVo>> {
  return http.get<PageResult<AssetVo>>('/assets', params);
}

/** GET /assets/{id} */
export function getAsset(id: number): Promise<AssetVo> {
  return http.get<AssetVo>(`/assets/${id}`);
}

/** GET /assets/{id}/download —— 文件流地址 */
export function assetDownloadUrl(id: number): string {
  return fileUrl(`/assets/${id}/download`);
}

/** POST /assets/batch-download */
export function batchDownloadAssets(ids: number[]): Promise<BatchDownloadVo> {
  return http.post<BatchDownloadVo>('/assets/batch-download', { ids });
}

/** POST /assets/{id}/rollback */
export function rollbackAsset(id: number, reason: string): Promise<AssetVo> {
  return http.post<AssetVo>(`/assets/${id}/rollback`, { reason });
}

/** PUT /assets/{id}/tags */
export function updateAssetTags(id: number, tags: string[]): Promise<AssetVo> {
  return http.put<AssetVo>(`/assets/${id}/tags`, { tags });
}
