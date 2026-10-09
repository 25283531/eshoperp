import { useCallback, useMemo } from 'react';
import { useSearchParams } from 'react-router-dom';
import type { TablePaginationConfig } from 'antd';

/** 分页参数（后端约定 page 从 1 开始，page_size 默认 20） */
export interface PaginationParams {
  page: number;
  page_size: number;
}

export interface UsePaginationResult<F> {
  /** 当前页码（从 1 开始） */
  page: number;
  /** 每页条数 */
  pageSize: number;
  /** 当前筛选条件（与 URL query 同步） */
  filters: F;
  /** 合并了分页与筛选的请求参数（已剔除空值） */
  params: Record<string, unknown>;
  /** 传给 antd Table 的 pagination 配置 */
  tablePagination: TablePaginationConfig;
  /** Table onChange 处理器 */
  onTableChange: (pagination: TablePaginationConfig) => void;
  /** 更新筛选条件并回到第 1 页 */
  setFilters: (patch: Partial<F>) => void;
  /** 直接跳页 */
  setPage: (page: number) => void;
  /** 重置筛选条件 */
  resetFilters: () => void;
}

/**
 * 分页 + 筛选参数管理，并与 URL query 同步（刷新/分享链接可复原）。
 * @param initialFilters 筛选字段初始值（key 即 URL query 名，也直接作为 API query 名）
 * @param defaultPageSize 默认每页条数
 */
export function usePagination<F extends object>(
  initialFilters: F,
  defaultPageSize = 20,
): UsePaginationResult<F> {
  const [searchParams, setSearchParams] = useSearchParams();
  const initialKeys = useMemo(
    () => Object.keys(initialFilters as Record<string, unknown>),
    [initialFilters],
  );

  const page = Math.max(1, Number(searchParams.get('page') ?? 1) || 1);
  const pageSize = Math.max(1, Number(searchParams.get('page_size') ?? defaultPageSize) || defaultPageSize);

  const filters = useMemo(() => {
    const next: Record<string, unknown> = { ...(initialFilters as Record<string, unknown>) };
    initialKeys.forEach((key) => {
      const raw = searchParams.get(key);
      if (raw !== null && raw !== '') next[key] = raw;
    });
    return next as F;
  }, [searchParams, initialFilters, initialKeys]);

  /** 剔除空值，避免把 undefined 传给后端 */
  const params = useMemo(() => {
    const merged: Record<string, unknown> = { page, page_size: pageSize };
    Object.entries(filters as Record<string, unknown>).forEach(([key, value]) => {
      if (value === undefined || value === null || value === '') return;
      merged[key] = value;
    });
    return merged;
  }, [filters, page, pageSize]);

  const patchSearch = useCallback(
    (patch: Record<string, string | null>) => {
      const next = new URLSearchParams(searchParams);
      Object.entries(patch).forEach(([key, value]) => {
        if (value === null || value === '') next.delete(key);
        else next.set(key, value);
      });
      setSearchParams(next, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  const setFilters = useCallback(
    (patchValue: Partial<F>) => {
      const patch: Record<string, string | null> = { page: null };
      Object.entries(patchValue as Record<string, unknown>).forEach(([key, value]) => {
        patch[key] = value === undefined || value === null ? null : String(value);
      });
      patchSearch(patch);
    },
    [patchSearch],
  );

  const setPage = useCallback(
    (nextPage: number) => {
      patchSearch({ page: String(Math.max(1, nextPage)) });
    },
    [patchSearch],
  );

  const resetFilters = useCallback(() => {
    const patch: Record<string, string | null> = { page: null };
    initialKeys.forEach((key) => {
      patch[key] = null;
    });
    patchSearch(patch);
  }, [initialKeys, patchSearch]);

  const onTableChange = useCallback(
    (pagination: TablePaginationConfig) => {
      const nextPage = pagination.current ?? 1;
      const nextSize = pagination.pageSize ?? pageSize;
      patchSearch({ page: String(nextPage), page_size: String(nextSize) });
    },
    [pageSize, patchSearch],
  );

  return {
    page,
    pageSize,
    filters,
    params,
    tablePagination: {
      current: page,
      pageSize,
      showSizeChanger: true,
      showQuickJumper: true,
      pageSizeOptions: ['20', '50', '100'],
    },
    onTableChange,
    setFilters,
    setPage,
    resetFilters,
  };
}
