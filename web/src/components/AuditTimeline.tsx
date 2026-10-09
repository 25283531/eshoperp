import type { ReactNode } from 'react';
import { Empty, Tag, Timeline, Typography } from 'antd';
import dayjs from 'dayjs';

/** 时间线事件（兼容订单 timeline 与审计日志两种结构） */
export interface AuditTimelineItem {
  /** 事件发生时间（ISO8601 UTC） */
  at: string;
  /** 事件标题（二选一：显式 title 或由状态迁移生成） */
  title?: string;
  /** 补充说明 */
  description?: string;
  /** 操作人 */
  operator?: string | null;
  /** 备注 */
  note?: string | null;
  /** 迁移前状态 */
  from_status?: string | null;
  /** 迁移后状态 */
  to_status?: string | null;
  /** 全链路追踪 ID */
  trace_id?: string | null;
}

export interface AuditTimelineProps {
  items: AuditTimelineItem[];
  /** 状态枚举名，用于把 from/to 状态渲染成中文（如 OrderStatus / PublishStatus） */
  enumKey?: string;
  /** 状态标签渲染器（由调用方注入 StatusTag 逻辑，避免组件间循环依赖） */
  renderStatus?: (value?: string | null) => ReactNode;
}

/** 本地时间格式化（后端一律 UTC，前端按 Asia/Shanghai 展示） */
export function formatTime(value?: string | null): string {
  if (!value) return '-';
  const parsed = dayjs(value);
  return parsed.isValid() ? parsed.format('YYYY-MM-DD HH:mm:ss') : value;
}

/**
 * 审计 / 处理记录时间线。
 * 订单处理时间线、映射变更历史、审计日志详情共用。
 */
export default function AuditTimeline(props: AuditTimelineProps): JSX.Element {
  const { items, renderStatus } = props;

  if (!items || items.length === 0) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无记录" />;
  }

  return (
    <Timeline
      items={items.map((item, index) => {
        const statusNode =
          item.from_status || item.to_status ? (
            <span>
              {renderStatus ? renderStatus(item.from_status) : item.from_status}
              {' → '}
              {renderStatus ? renderStatus(item.to_status) : item.to_status}
            </span>
          ) : null;

        return {
          key: `${item.at}-${index}`,
          children: (
            <div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                <Typography.Text strong>{item.title ?? '状态变更'}</Typography.Text>
                {statusNode}
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {formatTime(item.at)}
                </Typography.Text>
              </div>
              {item.description ? (
                <div>
                  <Typography.Text type="secondary">{item.description}</Typography.Text>
                </div>
              ) : null}
              {item.note ? (
                <div>
                  <Typography.Text>备注：{item.note}</Typography.Text>
                </div>
              ) : null}
              <div>
                {item.operator ? <Tag>操作人：{item.operator}</Tag> : null}
                {item.trace_id ? (
                  <Tag className="erp-mono">trace_id：{item.trace_id}</Tag>
                ) : null}
              </div>
            </div>
          ),
        };
      })}
    />
  );
}
