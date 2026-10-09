import { Tag, Tooltip } from 'antd';

export interface MockBadgeProps {
  /** 是否 Mock 数据 */
  isMock?: boolean;
  /** 附加说明（默认：Mock 数据不参与真实履约） */
  tooltip?: string;
}

/**
 * Mock 数据显著标记（§10.10）。
 * 凡 is_mock=true 的商品 / 映射 / 订单 / 上架任务，渲染时一律带上此标记。
 */
export default function MockBadge(props: MockBadgeProps): JSX.Element {
  const { isMock, tooltip = 'Mock 数据：由 Mock 适配器产出，不参与真实履约' } = props;
  if (!isMock) return <></>;
  return (
    <Tooltip title={tooltip}>
      <Tag color="orange" style={{ marginInlineEnd: 0 }}>
        MOCK
      </Tag>
    </Tooltip>
  );
}
