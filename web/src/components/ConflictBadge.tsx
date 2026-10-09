import { Space, Tag, Tooltip } from 'antd';

import {
  CONFLICT_MANY_TO_ONE_COPY,
  CONFLICT_P0_COPY,
  CONFLICT_P1_COPY,
  resolveConflictBlocking,
  resolveConflictLevel,
} from '@/constants/enums';
import { useEnumMeta } from '@/hooks/useEnumOptions';

export interface ConflictBadgeProps {
  /** 冲突等级（后端返回优先；未给时按冲突类型推导） */
  level?: string | null;
  /** 冲突类型列表（六类） */
  conflictTypes?: string[] | null;
  /** 是否仅渲染一个紧凑标记 */
  compact?: boolean;
  /** 无冲突时的占位文案 */
  emptyText?: string;
}

/**
 * 映射冲突徽标（两类语义，级别与是否拦截一律以 /settings/enums 下发为准）：
 *  - P0 / blocking=true（one_to_many / duplicate / duplicate_item / cost_invalid / spec_mismatch）
 *    → 红色，禁止上架，无绕过路径；
 *  - P1 / blocking=false（many_to_one 跨平台铺货 / cost_underwater 成本倒挂）
 *    → 金色，**仅提示不拦截**，映射照常生效、不影响上架。
 * many_to_one 是「同一货源铺多个平台」的正常主营业务，UI 上禁止表达成错误。
 */
export default function ConflictBadge(props: ConflictBadgeProps): JSX.Element {
  const { level, conflictTypes, compact = false, emptyText = '无冲突' } = props;
  const { levelByValue, blockingByValue, labelByValue } = useEnumMeta('ConflictType');

  const computedLevel = level ?? resolveConflictLevel(conflictTypes, levelByValue);
  const blocking = resolveConflictBlocking(conflictTypes, blockingByValue);
  const hasTypes = Boolean(conflictTypes && conflictTypes.length > 0);

  if (!computedLevel && !hasTypes) {
    return <Tag>{emptyText}</Tag>;
  }

  const types = conflictTypes ?? [];
  const isP0 = computedLevel === 'P0' || blocking;
  const onlyManyToOne = types.length > 0 && types.every((type) => type === 'many_to_one');

  /** Tooltip 文案：跨平台铺货单独说明，避免被误读为错误 */
  const tip = onlyManyToOne
    ? CONFLICT_MANY_TO_ONE_COPY
    : isP0
      ? CONFLICT_P0_COPY
      : CONFLICT_P1_COPY;

  if (compact) {
    return (
      <Tooltip title={tip}>
        <Tag color={isP0 ? 'red' : 'gold'}>
          {computedLevel ?? '冲突'}
          {!isP0 ? '（不拦截）' : ''}
        </Tag>
      </Tooltip>
    );
  }

  return (
    <Space size={4} wrap>
      <Tooltip title={tip}>
        <Tag color={isP0 ? 'red' : 'gold'}>
          {isP0 ? 'P0 · 禁止上架' : 'P1 · 提示（不拦截）'}
        </Tag>
      </Tooltip>
      {types.map((type) => (
        <Tag key={type} color={isP0 ? 'red' : 'gold'} bordered={false}>
          {labelByValue[type] ?? type}
        </Tag>
      ))}
    </Space>
  );
}
