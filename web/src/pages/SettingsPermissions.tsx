import { useMemo, useState } from 'react';
import {
  Alert,
  Badge,
  Button,
  Card,
  Descriptions,
  Form,
  Input,
  Modal,
  Segmented,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { CheckCircleTwoTone, CloseCircleTwoTone } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { getScopePolicies, handleScopeViolation, listScopeViolations } from '@/api/system';
import type { ScopeViolationVo } from '@/api/types';
import PageContainer from '@/components/PageContainer';
import { formatTime } from '@/components/AuditTimeline';
import { SCOPE_MATRIX } from '@/constants/enums';

interface HandleFormValues {
  handle_note: string;
}

/**
 * P17 权限与越权告警（★ 独立页面，不是适配器设置的 Tab）。
 *
 * 为什么必须独立：越权告警是**告警面**而不是配置项。
 * 若降级为设置页的二级 Tab，第三方申请了 item.write 这类越权 scope 时，
 * 运营必须主动点进设置页二级 Tab 才能发现，可能几天都看不到 —— 这是安全降级。
 * 本页 + 顶栏 /system/status-bar 红点构成两层告警：
 *   第一层：任何页面顶部都能看到未处置数量（发现）
 *   第二层：本页逐条处置（处置）
 */
export default function SettingsPermissions(): JSX.Element {
  const queryClient = useQueryClient();
  const [scope, setScope] = useState<'unhandled' | 'all'>('unhandled');
  const [handleId, setHandleId] = useState<number | null>(null);
  const [handleForm] = Form.useForm<HandleFormValues>();

  const violationsQuery = useQuery({
    queryKey: ['adapters', 'violations', scope],
    queryFn: () =>
      listScopeViolations({
        page: 1,
        page_size: 100,
        is_unhandled: scope === 'unhandled' ? 'true' : undefined,
      }),
    refetchInterval: 60_000,
  });

  const policiesQuery = useQuery({
    queryKey: ['adapters', 'scope-policies'],
    queryFn: getScopePolicies,
    staleTime: 300_000,
  });

  const handleMutation = useMutation({
    mutationFn: (body: { id: number; handle_note: string }) =>
      handleScopeViolation(body.id, { handle_note: body.handle_note }),
    onSuccess: (data) => {
      message.success(`告警 #${data.id} 已处置（处置记录入审计）`);
      setHandleId(null);
      handleForm.resetFields();
      void queryClient.invalidateQueries({ queryKey: ['adapters', 'violations'] });
      // 顶栏红点来自 GET /system/status-bar，处置后必须立刻重算，否则红点要等 60s 才熄灭
      void queryClient.invalidateQueries({ queryKey: ['system', 'status-bar'] });
    },
  });

  /**
   * 未处置计数。
   * 判定一律用 `is_handled` 字段，不只看 `action_type`：
   * 后端对「scope 校验通过」的记录也会入库一条并置 `is_handled=true`，
   * 那不是告警，不能计入红点。
   */
  const unhandledCount = (violationsQuery.data?.items ?? []).filter(
    (item) => !item.is_handled && (item.denied_scopes?.length ?? 0) > 0,
  ).length;

  /** 按「是否真有被拒 scope」区分：通过记录 ≠ 待处置告警 */
  const isActualViolation = (item: ScopeViolationVo): boolean =>
    (item.denied_scopes?.length ?? 0) > 0;

  const columns = useMemo<ColumnsType<ScopeViolationVo>>(
    () => [
      { title: 'ID', dataIndex: 'id', width: 70 },
      {
        title: '适配器',
        dataIndex: 'adapter_name',
        width: 130,
        render: (value: string | null) => value ?? '-',
      },
      {
        title: '平台',
        dataIndex: 'platform',
        width: 100,
        render: (value: string | null) => value ?? '-',
      },
      {
        title: '声明 scope',
        dataIndex: 'declared_scopes',
        width: 220,
        render: (scopes: string[]) => (
          <Space size={4} wrap>
            {(scopes ?? []).length === 0 ? (
              <Typography.Text type="secondary">-</Typography.Text>
            ) : (
              (scopes ?? []).map((item) => <Tag key={item}>{item}</Tag>)
            )}
          </Space>
        ),
      },
      {
        title: '被拒 scope',
        dataIndex: 'denied_scopes',
        width: 220,
        render: (scopes: string[]) => (
          <Space size={4} wrap>
            {(scopes ?? []).map((item) => (
              <Tag key={item} color="red">
                {item}
              </Tag>
            ))}
          </Space>
        ),
      },
      { title: '说明', dataIndex: 'message', ellipsis: true },
      {
        title: '状态',
        key: 'status',
        width: 120,
        render: (_value: unknown, record) => {
          if (!isActualViolation(record)) {
            // 「scope 校验通过」的记录：不是告警，绝不能渲染成待处置
            return <Tag color="blue">校验通过</Tag>;
          }
          return record.is_handled ? (
            <Tag color="green">已处置</Tag>
          ) : (
            <Tag color="red">未处置</Tag>
          );
        },
      },
      { title: '操作人', dataIndex: 'operator', width: 110, render: (v?: string | null) => v ?? '-' },
      { title: '时间', dataIndex: 'created_at', width: 170, render: (v: string) => formatTime(v) },
      {
        title: '处置备注',
        dataIndex: 'handle_note',
        width: 180,
        ellipsis: true,
        render: (value: string | null) => value ?? '-',
      },
      {
        title: '操作',
        key: 'actions',
        width: 100,
        fixed: 'right',
        render: (_value: unknown, record) => {
          // 「校验通过」的记录没有处置动作，避免运营对着一条通过记录点处置
          if (!isActualViolation(record)) {
            return <Typography.Text type="secondary">无需处置</Typography.Text>;
          }
          return record.is_handled ? (
            <Typography.Text type="secondary">已处置</Typography.Text>
          ) : (
            <Typography.Link
              onClick={() => {
                setHandleId(record.id);
                handleForm.resetFields();
              }}
            >
              处置
            </Typography.Link>
          );
        },
      },
    ],
    [handleForm],
  );

  return (
    <PageContainer
      title="权限与越权告警"
      subTitle="★ 告警面（不是配置项）：第三方若申请商品编辑 / 上新 / 改价 scope，系统一律拒绝并落审计，此处逐条处置"
      extra={
        <Space>
          <Badge count={unhandledCount} overflowCount={99} offset={[-4, 4]}>
            <Button>未处置</Button>
          </Badge>
          <Button onClick={() => void violationsQuery.refetch()} loading={violationsQuery.isFetching}>
            刷新
          </Button>
        </Space>
      }
      alert={
        <Alert
          type={unhandledCount > 0 ? 'error' : 'success'}
          showIcon
          message={
            unhandledCount > 0
              ? `存在 ${unhandledCount} 条未处置的越权告警，请逐条确认并处置`
              : '暂无未处置的越权告警'
          }
          description="红线 R1：第三方永远不持有商品编辑 / 上新 / 下架 / 改价权限。任何越权申请都会被 scope 白名单硬校验拒绝，并写入 action_type=permission_change 的审计日志。"
        />
      }
    >
      <Card size="small" title="授权清单（权限最小化）" style={{ marginBottom: 16 }}>
        <Table
          rowKey="scope"
          size="small"
          pagination={false}
          dataSource={SCOPE_MATRIX}
          columns={[
            { title: '权限项', dataIndex: 'label', width: 220 },
            { title: 'scope', dataIndex: 'scope', width: 200, className: 'erp-mono' },
            {
              title: '是否授予',
              dataIndex: 'allowed',
              width: 140,
              render: (value: boolean) =>
                value ? (
                  <Space size={4}>
                    <CheckCircleTwoTone twoToneColor="#52c41a" />
                    <span>允许</span>
                  </Space>
                ) : (
                  <Space size={4}>
                    <CloseCircleTwoTone twoToneColor="#ff4d4f" />
                    <Tag color="red">拒绝</Tag>
                  </Space>
                ),
            },
            {
              title: '说明',
              dataIndex: 'allowed',
              render: (value: boolean) =>
                value
                  ? '履约必需能力：第三方仅可读取订单、写物流'
                  : '系统已拒绝并落审计（action_type=permission_change）',
            },
          ]}
        />
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8, marginBottom: 0 }}>
          订单读取 ✓ / 发货（物流回填）✓ / 商品编辑 ✗ / 上新 ✗ / 改价 ✗ / 下架 ✗
        </Typography.Paragraph>
      </Card>

      <Card size="small" title="Scope 白名单 / 黑名单" style={{ marginBottom: 16 }}>
        {policiesQuery.data ? (
          <Descriptions bordered size="small" column={1}>
            <Descriptions.Item label="允许（白名单）">
              <Space wrap>
                {policiesQuery.data.allowed.map((item) => (
                  <Tag key={item} color="green">
                    {item}
                  </Tag>
                ))}
              </Space>
            </Descriptions.Item>
            <Descriptions.Item label="禁止（黑名单）">
              <Space wrap>
                {policiesQuery.data.forbidden.map((item) => (
                  <Tag key={item} color="red">
                    {item}
                  </Tag>
                ))}
              </Space>
            </Descriptions.Item>
            <Descriptions.Item label="说明">{policiesQuery.data.description}</Descriptions.Item>
          </Descriptions>
        ) : (
          <Typography.Text type="secondary">后端未返回策略（GET /adapters/scope-policies）</Typography.Text>
        )}
      </Card>

      <Card
        size="small"
        title={
          <Space>
            <span>越权告警记录</span>
            <Statistic
              value={violationsQuery.data?.total ?? 0}
              suffix="条"
              valueStyle={{ fontSize: 14 }}
            />
          </Space>
        }
        extra={
          <Segmented
            value={scope}
            onChange={(value) => setScope(value as 'unhandled' | 'all')}
            options={[
              { label: '未处置', value: 'unhandled' },
              { label: '全部', value: 'all' },
            ]}
          />
        }
      >
        <Table<ScopeViolationVo>
          rowKey="id"
          size="small"
          scroll={{ x: 1750 }}
          columns={columns}
          dataSource={violationsQuery.data?.items ?? []}
          pagination={false}
          locale={{ emptyText: '暂无越权记录（这是好事）' }}
        />
      </Card>

      {/* 逐条处置 */}
      <Modal
        title={handleId ? `处置越权告警 #${handleId}` : '处置越权告警'}
        open={handleId !== null}
        onCancel={() => setHandleId(null)}
        onOk={() => handleForm.submit()}
        confirmLoading={handleMutation.isPending}
        okText="确认处置"
        cancelText="取消"
        destroyOnHidden
      >
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="处置不代表放行"
          description="越权 scope 已经被系统拒绝，处置只是确认「已知悉并已处理」（例如已撤回第三方授权、已更换适配器）。处置记录写入审计。"
        />
        <Form
          form={handleForm}
          layout="vertical"
          onFinish={(values: HandleFormValues) => {
            if (handleId === null) return;
            handleMutation.mutate({ id: handleId, handle_note: values.handle_note });
          }}
        >
          <Form.Item name="handle_note" label="处置备注" rules={[{ required: true }]}>
            <Input.TextArea rows={3} placeholder="如：已联系妙手撤回 item.write 授权申请" />
          </Form.Item>
        </Form>
      </Modal>
    </PageContainer>
  );
}
