/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 后端 API 统一前缀，默认 /api/v1 */
  readonly VITE_API_BASE?: string;
  /** 默认操作人（审计用 X-Operator 头） */
  readonly VITE_DEFAULT_OPERATOR?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
