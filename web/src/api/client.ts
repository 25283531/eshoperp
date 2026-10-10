import axios from 'axios';
import type { AxiosError, AxiosRequestConfig, AxiosResponse } from 'axios';
import { message } from 'antd';

/** 后端统一前缀（.env.development: VITE_API_BASE=/api/v1） */
export const API_BASE: string = import.meta.env.VITE_API_BASE ?? '/api/v1';

/** 统一响应体 {code,message,data,trace_id}，code=0 为成功 */
export interface ApiResponse<T> {
  code: number;
  message: string;
  data: T;
  trace_id: string;
}

/**
 * 业务错误：由 axios 拦截器统一构造，携带 code / data / trace_id / status。
 * 页面据此区分「映射校验拦截 422 / code 4005」等需要特殊 UI 的场景。
 */
export class ApiError extends Error {
  /** 后端业务错误码（0 为成功） */
  public readonly code: number;
  /** 错误响应中的 data（如 MappingValidationVo） */
  public readonly data: unknown;
  /** HTTP 状态码 */
  public readonly status: number | undefined;
  /** 全链路 trace_id */
  public readonly traceId: string | undefined;

  constructor(params: {
    message: string;
    code: number;
    data?: unknown;
    status?: number;
    traceId?: string;
  }) {
    super(params.message);
    this.name = 'ApiError';
    this.code = params.code;
    this.data = params.data ?? null;
    this.status = params.status;
    this.traceId = params.traceId;
  }
}

const DEFAULT_OPERATOR: string = import.meta.env.VITE_DEFAULT_OPERATOR ?? 'owner';

/**
 * 容器形态下的运行时配置。
 * 由 `backend/container_entry.py` 在**容器启动时**写进 `web/dist/runtime-config.js`，
 * 经 `web/index.html` 在业务脚本之前加载。源码开发态该文件不存在（404），取不到即为 undefined。
 */
interface ErpRuntimeConfig {
  adminToken?: string;
}

declare global {
  interface Window {
    __ERP_RUNTIME__?: ErpRuntimeConfig;
  }
}

/**
 * 管理员令牌：凭证 / 适配器配置 / 切换 / 系统设置 / 越权处置 / AI 并发 / 库存配置
 * 这些管理端接口后端要求 X-Operator-Token 头，缺失返回 403（code 1003）。
 *
 * ★★ 这里**没有** fallback 默认值，是有意为之 ★★
 *   原先写的是 `?? 'admin-token'`：即便构建时不注入任何 secret，打包出的 exe 里
 *   也会固化一个公开已知的令牌，任何拿到产物的人都能调用凭证配置、适配器切换、
 *   越权处置这类管理端接口 —— 而使用者很可能以为"我没配密钥，所以是安全的"。
 *
 *   现在未注入即为空：请求头带空值，后端如实返回 403。
 *   「没配就用不了」远好过「没配也能用，只是你以为别人用不了」。
 *
 * ★★ 取值顺序（容器化后新增第一条）★★
 *   1. `window.__ERP_RUNTIME__.adminToken` —— 容器形态，启动时由后端写入，
 *      与后端 `get_settings().admin_token` **同源**，改了 ADMIN_TOKEN 重启即生效。
 *      为什么不走构建期 `VITE_ADMIN_TOKEN`：镜像随公开仓库推到 GHCR，
 *      谁都能拉 —— 把令牌编进产物等于公开发布。
 *   2. `import.meta.env.VITE_ADMIN_TOKEN` —— 源码开发态写 `web/.env.local`（不入库）。
 *
 *   用 `||` 而不是 `??`：容器里若未设 ADMIN_TOKEN，后端会用默认值 `admin-token`，
 *   此时 runtime-config 写出来的也是 `admin-token`（非空）；`||` 额外兜住
 *   "写成了空串"的异常情形，不至于让管理端全 403。
 */
const ADMIN_TOKEN: string =
  window.__ERP_RUNTIME__?.adminToken || import.meta.env.VITE_ADMIN_TOKEN || '';

/**
 * 请求附加选项。
 * admin=true 时自动补上 `op=<operator>` 查询参数与 `X-Operator-Token` 头，
 * 避免各处调用点重复书写、漏写导致 403。
 */
export interface HttpOptions {
  admin?: boolean;
  /**
   * 上传进度回调（0–100）。
   *
   * ★ 手工上传一次最多 20 张 × 10MB，不带进度的话界面上就是一个转圈的按钮，
   *   使用者无法判断"是卡住了还是在传"，会反复点提交。
   */
  onUploadProgress?: (percent: number) => void;
}

export const httpClient = axios.create({
  baseURL: API_BASE,
  timeout: 30_000,
  headers: { 'Content-Type': 'application/json' },
});

httpClient.interceptors.request.use((config) => {
  // MVP 单用户：操作人写入头，后端用于审计埋点
  config.headers['X-Operator'] = DEFAULT_OPERATOR;
  return config;
});

/** 把 axios 错误规整为 ApiError */
function toApiError(error: unknown): ApiError {
  const axiosError = error as AxiosError<ApiResponse<unknown>>;
  const body = axiosError?.response?.data;
  const status = axiosError?.response?.status;
  if (body && typeof body === 'object' && typeof body.code === 'number') {
    return new ApiError({
      message: body.message || `请求失败（code=${body.code}）`,
      code: body.code,
      data: body.data,
      status,
      traceId: body.trace_id,
    });
  }
  return new ApiError({
    message: axiosError?.message || '网络异常，请检查后端服务是否已启动',
    code: -1,
    data: null,
    status,
  });
}

httpClient.interceptors.response.use(
  (response: AxiosResponse) => response,
  (error: unknown) => {
    const apiError = toApiError(error);
    // 非 0 统一弹错误提示（页面需要更重的 Modal 展示时自行 catch，此处不阻止）
    message.error(
      apiError.traceId
        ? `${apiError.message}（trace_id: ${apiError.traceId}）`
        : apiError.message,
    );
    return Promise.reject(apiError);
  },
);

/**
 * 统一请求入口：拆包 {code,message,data,trace_id}，非 0 抛 ApiError。
 * @param config axios 请求配置
 * @returns 业务数据 T
 */
export async function request<T>(config: AxiosRequestConfig): Promise<T> {
  const response = await httpClient.request<ApiResponse<T>>(config);
  const body = response.data;
  if (!body || typeof body !== 'object' || typeof (body as ApiResponse<T>).code !== 'number') {
    // 文件流等非统一响应体，原样返回
    return body as T;
  }
  const envelope = body as ApiResponse<T>;
  if (envelope.code !== 0) {
    const apiError = new ApiError({
      message: envelope.message || `请求失败（code=${envelope.code}）`,
      code: envelope.code,
      data: envelope.data,
      status: response.status,
      traceId: envelope.trace_id,
    });
    message.error(apiError.message);
    throw apiError;
  }
  return envelope.data;
}

/** FormData 场景（如映射 CSV 导入）让浏览器自动带 boundary */
function multipartHeaders(data: unknown): { 'Content-Type': string } | undefined {
  return data instanceof FormData ? { 'Content-Type': 'multipart/form-data' } : undefined;
}

/**
 * 合并管理端鉴权信息：请求头 `X-Operator-Token`。
 * 凭证 / 适配器配置与切换 / 系统设置 / 越权处置 / AI 并发 / 库存配置这些管理端接口
 * 后端要求该头，缺失返回 403（code 1003）。
 *
 * 历史说明：后端曾有一个 `op` 必填 query 参数，那是 `Depends(lambda op: ...)` 无类型注解
 * 导致 FastAPI 误把依赖参数解析成 query 字符串的 bug；后端修复后该参数已消失，
 * 这里不再注入 `op`。
 */
function withAdminAuth(config: AxiosRequestConfig, options?: HttpOptions): AxiosRequestConfig {
  if (!options?.admin) return config;
  return {
    ...config,
    headers: {
      ...(config.headers ?? {}),
      'X-Operator-Token': ADMIN_TOKEN,
    },
  };
}

/**
 * 把 `HttpOptions.onUploadProgress` 适配成 axios 的进度回调。
 * `total` 不可知时（部分代理不回 Content-Length）axios 会传 0，此时不回报进度
 * —— 与其显示一个假的 50%，不如什么都不显示。
 */
function uploadProgressHandler(
  options?: HttpOptions,
): ((event: { loaded: number; total?: number }) => void) | undefined {
  const onProgress = options?.onUploadProgress;
  if (!onProgress) return undefined;
  return (event) => {
    const total = event.total ?? 0;
    if (total <= 0) return;
    onProgress(Math.min(100, Math.round((event.loaded / total) * 100)));
  };
}

/** 简易 HTTP 动词封装，供各 api 模块使用 */
export const http = {
  get<T>(url: string, params?: Record<string, unknown>, options?: HttpOptions): Promise<T> {
    return request<T>(withAdminAuth({ method: 'GET', url, params }, options));
  },
  post<T>(
    url: string,
    data?: unknown,
    params?: Record<string, unknown>,
    options?: HttpOptions,
  ): Promise<T> {
    return request<T>(
      withAdminAuth(
        {
          method: 'POST',
          url,
          data,
          params,
          headers: multipartHeaders(data),
          onUploadProgress: uploadProgressHandler(options),
        },
        options,
      ),
    );
  },
  put<T>(
    url: string,
    data?: unknown,
    params?: Record<string, unknown>,
    options?: HttpOptions,
  ): Promise<T> {
    return request<T>(
      withAdminAuth(
        {
          method: 'PUT',
          url,
          data,
          params,
          headers: multipartHeaders(data),
          onUploadProgress: uploadProgressHandler(options),
        },
        options,
      ),
    );
  },
  delete<T>(url: string, data?: unknown, options?: HttpOptions): Promise<T> {
    return request<T>(withAdminAuth({ method: 'DELETE', url, data }, options));
  },
};

/** 从任意异常中提取 ApiError（用于 catch 块） */
export function asApiError(error: unknown): ApiError | null {
  if (error instanceof ApiError) return error;
  const maybe = error as { code?: number; message?: string; data?: unknown } | null;
  if (maybe && typeof maybe.code === 'number' && typeof maybe.message === 'string') {
    return new ApiError({ message: maybe.message, code: maybe.code, data: maybe.data });
  }
  return null;
}

/**
 * 拼接后端文件下载/预览地址。
 * - 素材 preview_url 后端已下发完整 `/api/v1/assets/{id}/download`，原样返回不再重复拼前缀；
 * - 其他相对路径（如 `files/exports/xxx.csv`）拼上 API_BASE。
 */
export function fileUrl(path: string): string {
  if (path.startsWith('http://') || path.startsWith('https://')) return path;
  if (path.startsWith(API_BASE)) return path;
  return `${API_BASE}${path.startsWith('/') ? path : `/${path}`}`;
}

/** 导出文件下载地址：GET /api/v1/files/exports/{filename} */
export function exportFileUrl(filename: string): string {
  return fileUrl(`/files/exports/${filename}`);
}

/** 触发浏览器下载 */
export function triggerDownload(url: string, filename?: string): void {
  const link = document.createElement('a');
  link.href = url;
  link.target = '_blank';
  link.rel = 'noopener';
  if (filename) link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
}
