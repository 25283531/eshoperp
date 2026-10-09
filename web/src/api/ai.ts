/**
 * AI 重构域 API。
 * 对应 ARCHITECTURE.md §5.5.5（AIR）。
 * 硬约束：review_status != 'approved' 的产出不允许被上架引用（422 / code 4005）。
 */
import { http } from './client';
import type {
  AiConcurrencyVo,
  AiReviewBody,
  AiTaskCreateBody,
  AiTaskCreateVo,
  AiTaskDetailVo,
  AiTaskResultVo,
  AiTaskVo,
  PageResult,
} from './types';

/** POST /ai-tasks —— 202 */
export function createAiTasks(body: AiTaskCreateBody): Promise<AiTaskCreateVo> {
  return http.post<AiTaskCreateVo>('/ai-tasks', body);
}

/** GET /ai-tasks */
export function listAiTasks(params: Record<string, unknown> = {}): Promise<PageResult<AiTaskVo>> {
  return http.get<PageResult<AiTaskVo>>('/ai-tasks', params);
}

/** GET /ai-tasks/{id} */
export function getAiTask(id: number): Promise<AiTaskDetailVo> {
  return http.get<AiTaskDetailVo>(`/ai-tasks/${id}`);
}

/** POST /ai-tasks/{id}/retry */
export function retryAiTask(id: number): Promise<{ id: number; status: string }> {
  return http.post<{ id: number; status: string }>(`/ai-tasks/${id}/retry`);
}

/** POST /ai-tasks/{id}/cancel */
export function cancelAiTask(id: number): Promise<{ id: number; status: string }> {
  return http.post<{ id: number; status: string }>(`/ai-tasks/${id}/cancel`);
}

/** POST /ai-tasks/{id}/review */
export function reviewAiTask(id: number, body: AiReviewBody): Promise<AiTaskResultVo> {
  return http.post<AiTaskResultVo>(`/ai-tasks/${id}/review`, body);
}

/** GET /ai-tasks/concurrency-config */
export function getAiConcurrency(): Promise<AiConcurrencyVo> {
  return http.get<AiConcurrencyVo>('/ai-tasks/concurrency-config');
}

/** PUT /ai-tasks/concurrency-config */
export function updateAiConcurrency(body: Partial<AiConcurrencyVo>): Promise<AiConcurrencyVo> {
  return http.put<AiConcurrencyVo>('/ai-tasks/concurrency-config', body, undefined, {
    admin: true,
  });
}
