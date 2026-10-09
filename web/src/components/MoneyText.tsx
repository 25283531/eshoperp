import { Typography } from 'antd';

export interface MoneyTextProps {
  /** 金额：字符串「元」（API 层约定）或数字 */
  value?: string | number | null;
  /** 货币符号，默认 ¥ */
  symbol?: string;
  /** 空值占位，默认 '-' */
  emptyText?: string;
  /** 是否加粗强调（如合计） */
  strong?: boolean;
  /** 是否显示为删除线（如原价） */
  deleted?: boolean;
}

/** 解析为「元」的数字，非法值返回 null */
function parseYuan(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === '') return null;
  const num = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(num) ? num : null;
}

/**
 * 金额统一渲染组件（§10.5：禁止各页面自行 toFixed）。
 * API 层金额为字符串「元」，此处按 2 位小数格式化并带 ¥ 前缀。
 */
export default function MoneyText(props: MoneyTextProps): JSX.Element {
  const { value, symbol = '¥', emptyText = '-', strong = false, deleted = false } = props;
  const num = parseYuan(value);

  if (num === null) {
    return <Typography.Text type="secondary">{emptyText}</Typography.Text>;
  }

  const text = `${symbol}${num.toFixed(2)}`;
  return (
    <Typography.Text strong={strong} delete={deleted} style={{ fontVariantNumeric: 'tabular-nums' }}>
      {text}
    </Typography.Text>
  );
}
