import { useCallback, useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';

import { getEnums } from '@/api/system';
import { ENUM_FALLBACK } from '@/constants/enums';
import type { EnumOption } from '@/constants/enums';

/**
 * 读取某个枚举的中文选项。
 * 权威来源是后端 GET /settings/enums，后端不可达时回落 constants/enums.ts 本地常量。
 */
export function useEnumOptions(enumKey: string): EnumOption[] {
  const { data } = useQuery({
    queryKey: ['settings', 'enums'],
    queryFn: getEnums,
    staleTime: Number.POSITIVE_INFINITY,
    retry: false,
  });

  return useMemo(() => {
    const fromServer = data?.[enumKey];
    if (Array.isArray(fromServer) && fromServer.length > 0) return fromServer;
    return ENUM_FALLBACK[enumKey] ?? [];
  }, [data, enumKey]);
}

/**
 * 取枚举携带的附加元数据（后端 ConflictType 每项下发 level 与 blocking）。
 * 前端一律以此为准判定冲突级别，不再自行硬编码推断，避免前后端语义漂移。
 */
export function useEnumMeta(enumKey: string): {
  levelByValue: Record<string, string>;
  blockingByValue: Record<string, boolean>;
  labelByValue: Record<string, string>;
} {
  const options = useEnumOptions(enumKey);
  return useMemo(() => {
    const levelByValue: Record<string, string> = {};
    const blockingByValue: Record<string, boolean> = {};
    const labelByValue: Record<string, string> = {};
    options.forEach((item) => {
      labelByValue[item.value] = item.label;
      const level = (item as { level?: string }).level;
      const blocking = (item as { blocking?: boolean }).blocking;
      if (level) levelByValue[item.value] = level;
      if (typeof blocking === 'boolean') blockingByValue[item.value] = blocking;
    });
    return { levelByValue, blockingByValue, labelByValue };
  }, [options]);
}

/** 取单个枚举值的中文标签（未命中返回原值） */
export function useEnumLabel(enumKey: string): (value?: string | null) => string {
  const { labelByValue } = useEnumMeta(enumKey);

  return useCallback(
    (value?: string | null) => {
      if (!value) return '-';
      return labelByValue[value] ?? value;
    },
    [labelByValue],
  );
}
