import { useMemo } from 'react';
import { Badge, Button, Card, Col, Empty, List, Row, Space, Statistic, Table, Tag, Tooltip, Typography, message } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';

import { cancelTask, getDashboardSummary, getHealth, listTasks, retryTask } from '@/api/system';
import type { TaskRecordVo } from '@/api/types';
import PageContainer from '@/components/PageContainer';
import StatusTag from '@/components/StatusTag';
import { formatTime } from '@/components/AuditTimeline';
import { statusTagColor } from '@/constants/enums';
import { useEnumLabel } from '@/hooks/useEnumOptions';

/**
 * P1 工作台：四指标卡 + 待办清单 + 健康状态灯。
 */
export default function Dashboard(): JSX.Element {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const healthLabel = useEnumLabel('HealthState');

  const tasksQuery = useQuery({
    queryKey: ['tasks', 'recent'],
    queryFn: () => listTasks({ page: 1, page_size: 8 }),
    refetchInterval: 30_000,
  });

  const retryTaskMutation = useMutation({
    mutationFn: (id: number) => retryTask(id),
    onSuccess: () => {
      message.success('任务已重新入队');
      void queryClient.invalidateQueries({ queryKey: ['tasks'] });
    },
  });

  const cancelTaskMutation = useMutation({
    mutationFn: (id: number) => cancelTask(id),
    onSuccess: () => {
      message.success('任务已取消');
      void queryClient.invalidateQueries({ queryKey: ['tasks'] });
    },
  });

  const summaryQuery = useQuery({
    queryKey: ['dashboard', 'summary'],
    queryFn: getDashboardSummary,
    refetchInterval: 60_000,
  });

  const healthQuery = useQuery({
    queryKey: ['health'],
    queryFn: getHealth,
    refetchInterval: 60_000,
  });

  const summary = summaryQuery.data;
  const health = healthQuery.data;

  const metricCards = useMemo(
    () => [
      {
        title: '今日订单',
        value: summary?.today_order_count ?? 0,
        suffix: '单',
        link: '/orders',
      },
      {
        title: '待处理异常',
        value: summary?.pending_exception_count ?? 0,
        suffix: '单',
        link: '/orders',
        danger: true,
      },
      {
        title: '映射告警',
        value: summary?.mapping_alert_count ?? 0,
        suffix: '条',
        link: '/sku-mappings',
        danger: true,
      },
      {
        title: '队列积压',
        value: summary?.queue_backlog_count ?? 0,
        suffix: '个',
        link: '/publish-tasks',
      },
      {
        // ★ 越权告警在工作台的入口：与顶栏同源，点击直达 P17 逐条处置页
        title: '未处置越权告警',
        value: summary?.unhandled_violation_count ?? 0,
        suffix: '条',
        link: '/settings/permissions',
        danger: true,
      },
    ],
    [summary],
  );

  return (
    <PageContainer
      title="工作台"
      subTitle="自研只管上新，第三方只管履约；第三方永远不持有商品编辑权"
      loading={summaryQuery.isLoading}
      extra={
        <Space>
          <Button
            icon={<ReloadOutlined />}
            loading={summaryQuery.isFetching || healthQuery.isFetching}
            onClick={() => {
              void summaryQuery.refetch();
              void healthQuery.refetch();
            }}
          >
            刷新
          </Button>
        </Space>
      }
      alert={
        <Card size="small" styles={{ body: { padding: 12 } }}>
          <Space size="large" wrap>
            <Space size={4}>
              <span>系统状态：</span>
              <Badge
                status={health?.status === 'ok' ? 'success' : 'warning'}
                text={health?.status ?? (healthQuery.isError ? '后端未连接' : '加载中')}
              />
            </Space>
            <span>数据库：{health?.db ?? '-'}</span>
            <span>存储：{health?.storage ?? '-'}</span>
            <span>
              队列：待执行 {health?.queue?.pending ?? 0} / 执行中 {health?.queue?.running ?? 0} / 近 1 小时失败{' '}
              {health?.queue?.failed_1h ?? 0}
            </span>
          </Space>
        </Card>
      }
    >
      <Row gutter={[16, 16]}>
        {metricCards.map((card) => (
          <Col key={card.title} xs={24} sm={12} lg={6}>
            <Card hoverable onClick={() => navigate(card.link)}>
              <Statistic
                title={card.title}
                value={card.value}
                suffix={card.suffix}
                valueStyle={{ color: card.danger && card.value > 0 ? '#cf1322' : undefined }}
              />
            </Card>
          </Col>
        ))}
      </Row>

      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} lg={14}>
          <Card title="待办事项" size="small">
            {summary && summary.todo_list.length > 0 ? (
              <List
                size="small"
                dataSource={summary.todo_list}
                renderItem={(item) => (
                  <List.Item
                    actions={[
                      <Button key="go" type="link" size="small" onClick={() => navigate(item.link)}>
                        去处理
                      </Button>,
                    ]}
                  >
                    <List.Item.Meta
                      title={item.title}
                      description={
                        <Space size={4}>
                          <Tag>{item.type}</Tag>
                          <span>共 {item.count} 项</span>
                        </Space>
                      }
                    />
                  </List.Item>
                )}
              />
            ) : (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无待办" />
            )}
          </Card>
        </Col>

        <Col xs={24} lg={10}>
          <Card
            title="健康状态灯"
            size="small"
            extra={<span style={{ fontSize: 12 }}>每 60 秒自动刷新</span>}
          >
            {summary && summary.health_lights.length > 0 ? (
              <List
                size="small"
                dataSource={summary.health_lights}
                renderItem={(item) => (
                  <List.Item>
                    <Space direction="vertical" size={0} style={{ width: '100%' }}>
                      <Space size={8}>
                        <Badge
                          status={
                            item.status === 'healthy'
                              ? 'success'
                              : item.status === 'degraded'
                                ? 'warning'
                                : 'error'
                          }
                        />
                        <span>{item.name}</span>
                        <Tag color={statusTagColor('HealthState', item.status)}>
                          {healthLabel(item.status)}
                        </Tag>
                      </Space>
                      <Tooltip title={item.message}>
                        <span style={{ fontSize: 12, color: '#8c8c8c' }}>{item.message}</span>
                      </Tooltip>
                    </Space>
                  </List.Item>
                )}
              />
            ) : (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="后端未返回健康数据" />
            )}
          </Card>

          <Card title="适配器连通性" size="small" style={{ marginTop: 16 }}>
            {health && health.adapters.length > 0 ? (
              <List
                size="small"
                dataSource={health.adapters}
                renderItem={(item) => (
                  <List.Item>
                    <Space>
                      <span>{item.name}</span>
                      <Tag color={statusTagColor('HealthState', item.status)}>
                        {healthLabel(item.status)}
                      </Tag>
                      <span style={{ fontSize: 12, color: '#8c8c8c' }}>{item.latency_ms} ms</span>
                    </Space>
                  </List.Item>
                )}
              />
            ) : (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无适配器心跳数据" />
            )}
            <div style={{ fontSize: 12, color: '#8c8c8c', marginTop: 8 }}>
              最近刷新：{formatTime(new Date().toISOString())}
            </div>
          </Card>
        </Col>
      </Row>
      <Card title="最近异步任务" size="small" style={{ marginTop: 16 }} extra={<span style={{ fontSize: 12 }}>采集 / 重构 / 上架 / 同步均为异步任务</span>}>
        <Table<TaskRecordVo>
          rowKey="id"
          size="small"
          pagination={false}
          dataSource={tasksQuery.data?.items ?? []}
          locale={{ emptyText: '暂无任务（后端未连接或无任务）' }}
          columns={[
            { title: 'ID', dataIndex: 'id', width: 70 },
            { title: '任务类型', dataIndex: 'task_type', width: 140 },
            {
              title: '状态',
              dataIndex: 'status',
              width: 110,
              render: (value: string) => <StatusTag enumKey="TaskStatus" value={value} />,
            },
            { title: '重试次数', dataIndex: 'retry_count', width: 90 },
            {
              title: '错误信息',
              dataIndex: 'error_message',
              ellipsis: true,
              render: (value: string | null) => value ?? '-',
            },
            {
              title: '创建时间',
              dataIndex: 'created_at',
              width: 170,
              render: (value: string) => formatTime(value),
            },
            {
              title: '操作',
              key: 'actions',
              width: 130,
              render: (_value: unknown, record) => (
                <Space size="small">
                  <Typography.Link onClick={() => retryTaskMutation.mutate(record.id)}>重试</Typography.Link>
                  <Typography.Link onClick={() => cancelTaskMutation.mutate(record.id)}>取消</Typography.Link>
                </Space>
              ),
            },
          ]}
        />
      </Card>
    </PageContainer>
  );
}
