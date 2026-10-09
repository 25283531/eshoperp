import { useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Descriptions,
  Drawer,
  Form,
  Input,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { DownloadOutlined } from '@ant-design/icons';
import { useQuery } from '@tanstack/react-query';

import { exportAuditLogs, listAuditLogs } from '@/api/system';
import { triggerDownload } from '@/api/client';
import type { AuditLogVo } from '@/api/types';
import PageContainer from '@/components/PageContainer';
import StatusTag from '@/components/StatusTag';
import { formatTime } from '@/components/AuditTimeline';
import { useEnumOptions } from '@/hooks/useEnumOptions';
import { usePagination } from '@/hooks/usePagination';

interface AuditFilterValues {
  action_type: string;
  object_type: string;
  object_id: string;
  operator: string;
  created_from: string;
  created_to: string;
}

const DEFAULT_FILTERS: AuditFilterValues = {
  action_type: '',
  object_type: '',
  object_id: '',
  operator: '',
  created_from: '',
  created_to: '',
};

/**
 * P18 审计日志：筛选 + 导出 + 详情（变更前后值）。
 * 审计保留 ≥180 天，不软删除；越权拦截（permission_change）是红线 R1 的审计证据。
 */
export default function AuditLogs(): JSX.Element {
  const { filters, params, setFilters, tablePagination, onTableChange, resetFilters } =
    usePagination<AuditFilterValues>(DEFAULT_FILTERS);
  const actionOptions = useEnumOptions('AuditActionType');
  const objectOptions = useEnumOptions('AuditObjectType');

  const [detail, setDetail] = useState<AuditLogVo | null>(null);

  const listQuery = useQuery({
    queryKey: ['audit-logs', params],
    queryFn: () => listAuditLogs(params),
  });

  const exportQuery = useQuery({
    queryKey: ['audit-logs', 'export', params],
    queryFn: () => exportAuditLogs(params),
    enabled: false,
  });

  const columns = useMemo<ColumnsType<AuditLogVo>>(
    () => [
      { title: 'ID', dataIndex: 'id', width: 70 },
      {
        title: '动作类型',
        dataIndex: 'action_type',
        width: 140,
        render: (value: string) => (
          <StatusTag
            enumKey="AuditActionType"
            value={value}
          />
        ),
      },
      {
        title: '对象',
        key: 'object',
        width: 200,
        render: (_value: unknown, record) => (
          <Space size={4}>
            <StatusTag enumKey="AuditObjectType" value={record.object_type} />
            <span className="erp-mono">{record.object_id ?? '-'}</span>
          </Space>
        ),
      },
      { title: '操作人', dataIndex: 'operator', width: 110, render: (v?: string | null) => v ?? '-' },
      { title: '角色', dataIndex: 'operator_role', width: 100, render: (v?: string | null) => v ?? '-' },
      { title: '原因', dataIndex: 'reason', ellipsis: true },
      { title: 'IP', dataIndex: 'ip', width: 130, render: (v?: string | null) => v ?? '-' },
      {
        title: 'trace_id',
        dataIndex: 'trace_id',
        width: 200,
        className: 'erp-mono',
        render: (v?: string | null) => v ?? '-',
      },
      { title: '时间', dataIndex: 'created_at', width: 170, render: (v: string) => formatTime(v) },
      {
        title: '操作',
        key: 'actions',
        width: 90,
        fixed: 'right',
        render: (_value: unknown, record) => (
          <Typography.Link onClick={() => setDetail(record)}>详情</Typography.Link>
        ),
      },
    ],
    [],
  );

  return (
    <PageContainer
      title="审计日志"
      subTitle="映射 / 上架 / 下架 / 切换 / 凭证 / 权限 / 订单处置全部埋点；保留 ≥180 天，不软删除"
      loading={listQuery.isLoading}
      alert={
        <Alert
          type="info"
          showIcon
          message="越权拦截必留审计"
          description="任何被 scope 白名单拒绝的调用都会写入 action_type=permission_change 的审计记录，这是红线 R1 的可核查证据。"
        />
      }
      extra={
        <Space>
          <Button
            icon={<DownloadOutlined />}
            loading={exportQuery.isFetching}
            onClick={async () => {
              const result = await exportQuery.refetch();
              if (result.data) {
                triggerDownload(result.data.download_url);
                message.success(`已导出 ${result.data.row_count} 条`);
              }
            }}
          >
            导出
          </Button>
          <Button onClick={resetFilters}>重置筛选</Button>
        </Space>
      }
    >
      <Form
        layout="inline"
        style={{ marginBottom: 12, rowGap: 8 }}
        initialValues={filters}
        onFinish={(values: AuditFilterValues) => setFilters(values)}
      >
        <Form.Item name="action_type" label="动作">
          <Select allowClear placeholder="全部" style={{ width: 160 }} options={actionOptions} />
        </Form.Item>
        <Form.Item name="object_type" label="对象">
          <Select allowClear placeholder="全部" style={{ width: 150 }} options={objectOptions} />
        </Form.Item>
        <Form.Item name="object_id" label="对象 ID">
          <Input allowClear placeholder="如 11" style={{ width: 120 }} />
        </Form.Item>
        <Form.Item name="operator" label="操作人">
          <Input allowClear placeholder="如 owner" style={{ width: 130 }} />
        </Form.Item>
        <Form.Item name="created_from" label="起始时间">
          <Input allowClear placeholder="2026-10-01" style={{ width: 140 }} />
        </Form.Item>
        <Form.Item name="created_to" label="结束时间">
          <Input allowClear placeholder="2026-10-08" style={{ width: 140 }} />
        </Form.Item>
        <Form.Item>
          <Button type="primary" htmlType="submit">
            查询
          </Button>
        </Form.Item>
      </Form>

      <Table<AuditLogVo>
        rowKey="id"
        size="small"
        scroll={{ x: 1600 }}
        columns={columns}
        dataSource={listQuery.data?.items ?? []}
        pagination={{ ...tablePagination, total: listQuery.data?.total ?? 0 }}
        onChange={onTableChange}
      />

      <Drawer
        width={720}
        title={detail ? `审计详情 #${detail.id}` : '审计详情'}
        open={detail !== null}
        onClose={() => setDetail(null)}
      >
        {detail ? (
          <>
            <Descriptions bordered size="small" column={2} style={{ marginBottom: 16 }}>
              <Descriptions.Item label="动作类型">
                <StatusTag enumKey="AuditActionType" value={detail.action_type} />
              </Descriptions.Item>
              <Descriptions.Item label="对象类型">
                <StatusTag enumKey="AuditObjectType" value={detail.object_type} />
              </Descriptions.Item>
              <Descriptions.Item label="对象 ID">{detail.object_id ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="操作人">{detail.operator ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="角色">{detail.operator_role ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="IP">{detail.ip ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="时间">{formatTime(detail.created_at)}</Descriptions.Item>
              <Descriptions.Item label="trace_id">
                <span className="erp-mono">{detail.trace_id ?? '-'}</span>
              </Descriptions.Item>
              <Descriptions.Item label="原因" span={2}>
                {detail.reason ?? '-'}
              </Descriptions.Item>
            </Descriptions>

            <Typography.Title level={5}>变更前</Typography.Title>
            <pre className="erp-mono" style={{ whiteSpace: 'pre-wrap', background: '#fafafa', padding: 12 }}>
              {detail.old_value ?? '-'}
            </pre>

            <Typography.Title level={5}>变更后</Typography.Title>
            <pre className="erp-mono" style={{ whiteSpace: 'pre-wrap', background: '#fafafa', padding: 12 }}>
              {detail.new_value ?? '-'}
            </pre>

            {detail.action_type === 'permission_change' ? (
              <Alert
                type="error"
                showIcon
                style={{ marginTop: 12 }}
                message="该记录为越权拦截证据（红线 R1）"
                description="第三方申请了商品编辑 / 上新 / 改价等越权 scope，系统已拒绝并落审计。"
              />
            ) : null}

            <Space style={{ marginTop: 12 }}>
              <Tag>保留 ≥180 天</Tag>
              <Tag>不软删除</Tag>
            </Space>
          </>
        ) : null}
      </Drawer>
    </PageContainer>
  );
}
