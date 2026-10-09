import { Tag } from 'antd';

import { statusTagColor } from '@/constants/enums';
import { useEnumOptions } from '@/hooks/useEnumOptions';

export interface StatusTagProps {
  /** 枚举名（与 GET /settings/enums 的 key 一致，如 PublishStatus / OrderStatus） */
  enumKey: string;
  /** 枚举值 */
  value?: string | null;
  /** 无值时的占位文案，默认 '-' */
  emptyText?: string;
}

/**
 * 状态徽标：按枚举字典取中文标签 + 按 STATUS_TAG_COLORS 取颜色。
 * 页面禁止自行硬编码中文标签或颜色。
 */
export default function StatusTag(props: StatusTagProps): JSX.Element {
  const { enumKey, value, emptyText = '-' } = props;
  const options = useEnumOptions(enumKey);

  const label = options.find((item) => item.value === value)?.label ?? value ?? emptyText;
  const color = statusTagColor(enumKey, value);

  return <Tag color={color}>{label}</Tag>;
}
