/**
 * 系统域 API：工作台 / 平台账号与凭证 / 适配器与权限 / 系统设置 / 审计 / 异步任务。
 * 对应 ARCHITECTURE.md §5.5.1、§5.5.2、§5.5.12、§5.5.13。
 */
import { http } from './client';
import type {
  AdapterCapabilitiesVo,
  AdapterConfigUpdateBody,
  AdapterSwitchBody,
  AdapterSwitchVo,
  AdapterTestVo,
  AuditExportVo,
  AuditLogVo,
  AuthorizeResultVo,
  CredentialCreateBody,
  CredentialRevealVo,
  CredentialTestVo,
  CredentialVo,
  DashboardSummaryVo,
  EnumsMapLike,
  FulfillmentAdapterVo,
  FulfillmentAdaptersVo,
  HealthVo,
  ListingAdaptersVo,
  ListingModeUpdateVo,
  PageResult,
  PlatformAccountCreateBody,
  PlatformAccountVo,
  ScopePoliciesVo,
  ScopeViolationVo,
  SettingUpdateVo,
  SettingsVo,
  SystemStatusBarVo,
  TaskRecordVo,
  ViolationHandleBody,
  ViolationHandleVo,
} from './types';

// ------------------------- 工作台 / 健康检查 -------------------------

/** GET /dashboard/summary */
export function getDashboardSummary(): Promise<DashboardSummaryVo> {
  return http.get<DashboardSummaryVo>('/dashboard/summary');
}

/** GET /health */
export function getHealth(): Promise<HealthVo> {
  return http.get<HealthVo>('/health');
}

// ------------------------- 平台账号与凭证 -------------------------

/** GET /platform-accounts */
export function listPlatformAccounts(
  params: Record<string, unknown> = {},
): Promise<PlatformAccountVo[]> {
  return http.get<PlatformAccountVo[]>('/platform-accounts', params);
}

/** POST /platform-accounts —— 201 / 403（scope 越权） */
export function createPlatformAccount(
  body: PlatformAccountCreateBody,
): Promise<PlatformAccountVo> {
  return http.post<PlatformAccountVo>('/platform-accounts', body, undefined, { admin: true });
}

/** PUT /platform-accounts/{id} */
export function updatePlatformAccount(
  id: number,
  body: Partial<PlatformAccountCreateBody>,
): Promise<PlatformAccountVo> {
  return http.put<PlatformAccountVo>(`/platform-accounts/${id}`, body, undefined, {
    admin: true,
  });
}

/** POST /platform-accounts/{id}/authorize —— 200 / 403 */
export function authorizePlatformAccount(
  id: number,
  grantedScopes: string[],
): Promise<AuthorizeResultVo> {
  return http.post<AuthorizeResultVo>(
    `/platform-accounts/${id}/authorize`,
    { granted_scopes: grantedScopes },
    undefined,
    { admin: true },
  );
}

/** GET /credentials（仅 value_masked） */
export function listCredentials(
  params: Record<string, unknown> = {},
): Promise<CredentialVo[]> {
  return http.get<CredentialVo[]>('/credentials', params);
}

/** POST /credentials */
export function createCredential(body: CredentialCreateBody): Promise<CredentialVo> {
  return http.post<CredentialVo>('/credentials', body, undefined, { admin: true });
}

/** PUT /credentials/{id} */
export function updateCredential(
  id: number,
  body: { value?: string; expires_at?: string | null },
): Promise<CredentialVo> {
  return http.put<CredentialVo>(`/credentials/${id}`, body, undefined, { admin: true });
}

/** DELETE /credentials/{id} */
export function deleteCredential(id: number): Promise<{ id: number }> {
  return http.delete<{ id: number }>(`/credentials/${id}`, undefined, { admin: true });
}

/** POST /credentials/{id}/test（连通性自检） */
export function testCredential(id: number): Promise<CredentialTestVo> {
  return http.post<CredentialTestVo>(`/credentials/${id}/test`);
}

/** POST /credentials/{id}/reveal（二次验证 + 留审计） */
export function revealCredential(id: number, verifyCode: string): Promise<CredentialRevealVo> {
  return http.post<CredentialRevealVo>(
    `/credentials/${id}/reveal`,
    { verify_code: verifyCode },
    undefined,
    { admin: true },
  );
}

// ------------------------- 适配器与权限 -------------------------

/** GET /adapters/listing */
export function getListingAdapters(): Promise<ListingAdaptersVo> {
  return http.get<ListingAdaptersVo>('/adapters/listing');
}

/** PUT /adapters/listing/mode（切换留痕） */
export function updateListingMode(mode: string, reason: string): Promise<ListingModeUpdateVo> {
  return http.put<ListingModeUpdateVo>('/adapters/listing/mode', { mode, reason }, undefined, {
    admin: true,
  });
}

/** GET /adapters/fulfillment */
export function getFulfillmentAdapters(): Promise<FulfillmentAdaptersVo> {
  return http.get<FulfillmentAdaptersVo>('/adapters/fulfillment');
}

/** GET /adapters/fulfillment/{name}/capabilities */
export function getAdapterCapabilities(name: string): Promise<AdapterCapabilitiesVo> {
  return http.get<AdapterCapabilitiesVo>(`/adapters/fulfillment/${name}/capabilities`);
}

/** PUT /adapters/fulfillment/{name}/config —— 403（scope 越权，code 5003） */
export function updateAdapterConfig(
  name: string,
  body: AdapterConfigUpdateBody,
): Promise<FulfillmentAdapterVo> {
  return http.put<FulfillmentAdapterVo>(`/adapters/fulfillment/${name}/config`, body, undefined, {
    admin: true,
  });
}

/** POST /adapters/fulfillment/{name}/test（连通性自检） */
export function testAdapter(name: string): Promise<AdapterTestVo> {
  return http.post<AdapterTestVo>(`/adapters/fulfillment/${name}/test`);
}

/** POST /adapters/fulfillment/switch（在途订单按原渠道跑完） */
export function switchFulfillmentAdapter(body: AdapterSwitchBody): Promise<AdapterSwitchVo> {
  return http.post<AdapterSwitchVo>('/adapters/fulfillment/switch', body, undefined, {
    admin: true,
  });
}

/** GET /adapters/scope-policies */
export function getScopePolicies(): Promise<ScopePoliciesVo> {
  return http.get<ScopePoliciesVo>('/adapters/scope-policies');
}

/** GET /adapters/violations（越权告警记录；is_unhandled=true 只看未处置） */
export function listScopeViolations(
  params: Record<string, unknown> = {},
): Promise<PageResult<ScopeViolationVo>> {
  return http.get<PageResult<ScopeViolationVo>>('/adapters/violations', params);
}

/** POST /adapters/violations/{id}/handle —— 逐条处置越权告警 */
export function handleScopeViolation(
  id: number,
  body: ViolationHandleBody,
): Promise<ViolationHandleVo> {
  return http.post<ViolationHandleVo>(`/adapters/violations/${id}/handle`, body, undefined, {
    admin: true,
  });
}

/**
 * GET /system/status-bar —— 顶栏聚合数据（只读缓存与计数，不触发外部 HTTP）。
 * 前端每 60 秒轮询：未处置越权数 + 当前模式/渠道/Mock 态 + 健康状态。
 */
export function getSystemStatusBar(): Promise<SystemStatusBarVo> {
  return http.get<SystemStatusBarVo>('/system/status-bar');
}

// ------------------------- 系统设置 -------------------------

/** GET /settings */
export function getSettings(): Promise<SettingsVo> {
  return http.get<SettingsVo>('/settings');
}

/** PUT /settings/{key} */
export function updateSetting(key: string, value: string, reason?: string): Promise<SettingUpdateVo> {
  return http.put<SettingUpdateVo>(`/settings/${key}`, { value, reason }, undefined, {
    admin: true,
  });
}

/** GET /settings/enums（枚举字典唯一来源） */
export function getEnums(): Promise<EnumsMapLike> {
  return http.get<EnumsMapLike>('/settings/enums');
}

// ------------------------- 审计日志 -------------------------

/** GET /audit-logs */
export function listAuditLogs(
  params: Record<string, unknown> = {},
): Promise<PageResult<AuditLogVo>> {
  return http.get<PageResult<AuditLogVo>>('/audit-logs', params);
}

/** GET /audit-logs/export */
export function exportAuditLogs(params: Record<string, unknown> = {}): Promise<AuditExportVo> {
  return http.get<AuditExportVo>('/audit-logs/export', params);
}

// ------------------------- 异步任务 -------------------------

/** GET /tasks */
export function listTasks(params: Record<string, unknown> = {}): Promise<PageResult<TaskRecordVo>> {
  return http.get<PageResult<TaskRecordVo>>('/tasks', params);
}

/** GET /tasks/{id}（含 error_message） */
export function getTask(id: number): Promise<TaskRecordVo> {
  return http.get<TaskRecordVo>(`/tasks/${id}`);
}

/** POST /tasks/{id}/retry */
export function retryTask(id: number): Promise<{ id: number; status: string }> {
  return http.post<{ id: number; status: string }>(`/tasks/${id}/retry`);
}

/** POST /tasks/{id}/cancel */
export function cancelTask(id: number): Promise<{ id: number; status: string }> {
  return http.post<{ id: number; status: string }>(`/tasks/${id}/cancel`);
}
