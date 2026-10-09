import { useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  Form,
  Input,
  Modal,
  Radio,
  Row,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';

import {
  getAdapterCapabilities,
  getFulfillmentAdapters,
  getListingAdapters,
  getScopePolicies,
  switchFulfillmentAdapter,
  testAdapter,
  updateAdapterConfig,
  updateListingMode,
} from '@/api/system';
import type { CapabilitySpecVo, FulfillmentAdapterVo } from '@/api/types';
import PageContainer from '@/components/PageContainer';
import StatusTag from '@/components/StatusTag';
import { formatTime } from '@/components/AuditTimeline';
import { ALLOWED_SCOPES, FORBIDDEN_SCOPES } from '@/constants/enums';
import { useEnumOptions } from '@/hooks/useEnumOptions';

interface SwitchFormValues {
  adapter_name: string;
  reason: string;
  drain_inflight: boolean;
}

interface ModeFormValues {
  mode: string;
  reason: string;
}

interface AdapterConfigFormValues {
  declared_scopes: string[];
  is_enabled: boolean;
}

/**
 * 一次「重新启用」意图。
 *
 * ★ 为什么必须把 `declared_scopes` 一并带上：后端 `PUT /adapters/fulfillment/{name}/config`
 *   只在**请求体带 `declared_scopes`** 时才做 scope 白名单校验。若只发 `{"is_enabled": true}`，
 *   适配器会被直接启用且 `scope_check_status` 仍停留在 `rejected`（实测 200）。
 *   所以前端**任何启用路径都必须随请求提交当前声明 scope**，把校验权交回后端，
 *   禁止出现「前端点了启用就绕过校验」的通道。
 */
interface EnableIntent {
  adapter: FulfillmentAdapterVo;
  /** 本次启用将随请求提交的 scope 列表 */
  declared_scopes: string[];
  /** 来源：列表直接启用 / 配置弹窗内保存 */
  source: 'table' | 'form';
  /** 来源为 form 时的完整表单值（确认后原样提交） */
  formValues: AdapterConfigFormValues | null;
}

/**
 * P16 适配器配置与切换（上架模式 + 履约适配器 + 能力矩阵 + 自检 + 配置）。
 *
 * ★ 权限与越权告警（P17）**已拆为独立页面** `/settings/permissions`：
 *   告警面不是配置项，必须能在任何页面通过顶栏红点发现，再进独立页逐条处置。
 */
export default function SettingsAdapters(): JSX.Element {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const modeOptions = useEnumOptions('ListingMode');
  const adapterOptions = useEnumOptions('AdapterName');
  const capabilityOptions = useEnumOptions('Capability');
  const levelOptions = useEnumOptions('CapabilityLevel');

  const [switchOpen, setSwitchOpen] = useState<boolean>(false);
  const [modeOpen, setModeOpen] = useState<boolean>(false);
  const [capabilityAdapter, setCapabilityAdapter] = useState<string>('');
  const [configAdapter, setConfigAdapter] = useState<FulfillmentAdapterVo | null>(null);
  /** 待二次确认的「重新启用」意图（停用 → 启用必须走这里） */
  const [enableIntent, setEnableIntent] = useState<EnableIntent | null>(null);

  const [switchForm] = Form.useForm<SwitchFormValues>();
  const [modeForm] = Form.useForm<ModeFormValues>();
  const [configForm] = Form.useForm<AdapterConfigFormValues>();

  const listingQuery = useQuery({
    queryKey: ['adapters', 'listing'],
    queryFn: getListingAdapters,
  });

  const fulfillmentQuery = useQuery({
    queryKey: ['adapters', 'fulfillment'],
    queryFn: getFulfillmentAdapters,
  });

  const capabilityQuery = useQuery({
    queryKey: ['adapters', 'fulfillment', capabilityAdapter, 'capabilities'],
    queryFn: () => getAdapterCapabilities(capabilityAdapter),
    enabled: capabilityAdapter !== '',
  });

  /**
   * scope 白名单 / 黑名单以**后端 `GET /adapters/scope-policies` 为准**，
   * 本地常量只作兜底（后端不可达时不至于把越权 scope 判成合法）。
   */
  const policiesQuery = useQuery({
    queryKey: ['adapters', 'scope-policies'],
    queryFn: getScopePolicies,
    staleTime: 300_000,
  });

  const forbiddenScopes = useMemo<string[]>(
    () => policiesQuery.data?.forbidden ?? FORBIDDEN_SCOPES,
    [policiesQuery.data],
  );

  /** 当前声明里命中黑名单的 scope（即"仍然越权"的部分） */
  const deniedScopesOf = (scopes: string[] | null | undefined): string[] =>
    (scopes ?? []).filter((scope) => forbiddenScopes.includes(scope));

  /**
   * 是否"存在未清除的越权拒绝记录"。
   *
   * 判定依据（后端均已暴露，无需后端改动）：
   *   1. `scope_check_status === 'rejected'` —— 最近一次 scope 校验被拒；
   *   2. 或当前声明里仍命中黑名单。
   *
   * ★ 实测澄清：后端**不会**在拒绝时把 `is_enabled` 自动置 false
   *   （启用态适配器声明 `item.write` → 403，`is_enabled` 仍为 true，
   *   只是 `scope_check_status` 变 `rejected`，且被拒的越权声明**不会**写进 `declared_scopes`，
   *   适配器继续按被拒前的合规旧声明运行）。
   *   所以这里不能假设"rejected ⇒ 已停用"，两种状态都要如实呈现。
   */
  const hasScopeRejection = (adapter: FulfillmentAdapterVo): boolean =>
    adapter.scope_check_status === 'rejected' || deniedScopesOf(adapter.declared_scopes).length > 0;

  /** 有越权拒绝记录且当前处于停用态 */
  const disabledByScope = useMemo<FulfillmentAdapterVo[]>(
    () => (fulfillmentQuery.data?.adapters ?? []).filter((a) => hasScopeRejection(a) && !a.is_enabled),
    [fulfillmentQuery.data, forbiddenScopes],
  );

  /** 有越权拒绝记录但仍处于启用态（系统未自动停用，生效的是被拒前的旧声明） */
  const rejectedButEnabled = useMemo<FulfillmentAdapterVo[]>(
    () => (fulfillmentQuery.data?.adapters ?? []).filter((a) => hasScopeRejection(a) && a.is_enabled),
    [fulfillmentQuery.data, forbiddenScopes],
  );

  /**
   * ★ F10 危险组合：`is_enabled = true`（适配器在跑）+ `scope_check_status = rejected`（权限声明被拒）。
   *
   * 后端越权时**不会**自动停用适配器，所以这个组合会合法存在。
   * 启用态与校验态**必须分列**展示，否则"在跑但越权被拒"会被合并后的单一状态藏起来；
   * 命中时整行红色高亮（`.erp-row-danger`）并在操作列给处置入口。
   */
  const isDangerousCombo = (adapter: FulfillmentAdapterVo): boolean =>
    adapter.is_enabled && adapter.scope_check_status === 'rejected';

  const modeMutation = useMutation({
    mutationFn: (body: ModeFormValues) => updateListingMode(body.mode, body.reason),
    onSuccess: (data) => {
      message.success(`上架模式已切换为 ${data.mode}（切换留痕，审计 #${data.audit_id ?? '-'}）`);
      setModeOpen(false);
      modeForm.resetFields();
      void queryClient.invalidateQueries({ queryKey: ['adapters'] });
    },
  });

  const switchMutation = useMutation({
    mutationFn: (body: SwitchFormValues) =>
      switchFulfillmentAdapter({
        adapter_name: body.adapter_name,
        reason: body.reason,
        drain_inflight: body.drain_inflight,
      }),
    onSuccess: (data) => {
      message.success(
        `已切换到 ${data.active_adapter}；${data.inflight_order_count} 笔在途订单仍由原渠道履约完成`,
      );
      setSwitchOpen(false);
      switchForm.resetFields();
      void queryClient.invalidateQueries({ queryKey: ['adapters'] });
    },
  });

  const configMutation = useMutation({
    mutationFn: (body: { name: string; values: AdapterConfigFormValues }) =>
      updateAdapterConfig(body.name, {
        declared_scopes: body.values.declared_scopes,
        is_enabled: body.values.is_enabled,
      }),
    onSuccess: () => {
      message.success('适配器配置已保存，保存即生效（不重启）');
      setConfigAdapter(null);
      void queryClient.invalidateQueries({ queryKey: ['adapters'] });
    },
    onError: (error: unknown) => {
      const apiError = error as { code?: number; message?: string };
      if (apiError?.code === 5003) {
        message.error(`保存被拒：${apiError.message ?? '申请了越权 scope'}（已落审计）`);
      }
    },
  });

  /**
   * 列表直接「启用」。
   *
   * ★ 必带 `declared_scopes`：让后端重新跑一次 scope 校验，
   *   越权未清掉时后端仍会 403（code 5003），前端不做任何本地放行。
   */
  const enableMutation = useMutation({
    mutationFn: (body: { name: string; declared_scopes: string[] }) =>
      updateAdapterConfig(body.name, {
        declared_scopes: body.declared_scopes,
        is_enabled: true,
      }),
    onSuccess: () => {
      message.success('适配器已重新启用（scope 校验由后端重新执行并通过）');
      setEnableIntent(null);
      void queryClient.invalidateQueries({ queryKey: ['adapters'] });
    },
    onError: (error: unknown) => {
      const apiError = error as { code?: number; message?: string };
      if (apiError?.code === 5003) {
        message.error(`重新启用被拒：${apiError.message ?? '声明了越权 scope'}（已落审计）`);
      }
    },
  });

  /** 提交二次确认后的启用动作 */
  const confirmEnable = (): void => {
    if (!enableIntent) return;
    if (enableIntent.source === 'form' && enableIntent.formValues) {
      configMutation.mutate({
        name: enableIntent.adapter.adapter_name,
        values: { ...enableIntent.formValues, is_enabled: true },
      });
      return;
    }
    enableMutation.mutate({
      name: enableIntent.adapter.adapter_name,
      declared_scopes: enableIntent.declared_scopes,
    });
  };

  const testMutation = useMutation({
    mutationFn: (name: string) => testAdapter(name),
    onSuccess: (data) => {
      if (data.ok) message.success(`自检通过（${data.latency_ms} ms）：${data.message}`);
      else message.error(`自检失败：${data.message}`);
    },
  });

  const adapterColumns = useMemo<ColumnsType<FulfillmentAdapterVo>>(
    () => [
      { title: 'ID', dataIndex: 'id', width: 70 },
      {
        title: '适配器',
        dataIndex: 'display_name',
        width: 140,
        render: (value: string, record) => (
          <Space size={4}>
            <span>{value}</span>
            <span className="erp-mono" style={{ fontSize: 12 }}>
              {record.adapter_name}
            </span>
          </Space>
        ),
      },
      {
        title: '是否生效',
        dataIndex: 'is_active',
        width: 100,
        render: (value: boolean) =>
          value ? <Tag color="blue">当前生效</Tag> : <Tag>未启用</Tag>,
      },
      /**
       * ★ F10：启用状态列 —— **只表达启用/停用**，绝不掺入 scope 语义。
       * 掺了就会把"启用 + 校验被拒"这个危险组合折叠成一个看不懂的状态。
       */
      {
        title: '启用状态',
        dataIndex: 'is_enabled',
        width: 100,
        render: (value: boolean) => (value ? <Tag color="blue">启用</Tag> : <Tag>停用</Tag>),
      },
      {
        title: '健康',
        dataIndex: 'health_status',
        width: 100,
        render: (value: string) => <StatusTag enumKey="HealthState" value={value} />,
      },
      /**
       * ★ F10：scope 校验状态列 —— 与「启用状态」严格分列，
       * 只表达校验结果，不表达启用与否。两者由 `.erp-row-danger` 交叉高亮。
       */
      {
        title: 'scope 校验状态',
        key: 'scope',
        width: 220,
        render: (_value: unknown, record) => (
          <Space direction="vertical" size={0}>
            <StatusTag enumKey="ScopeCheckStatus" value={record.scope_check_status} />
            <span style={{ fontSize: 12, color: '#8c8c8c' }}>{record.scope_check_message ?? ''}</span>
            {isDangerousCombo(record) ? (
              <Tag color="red" style={{ marginTop: 2 }}>
                启用中但权限被拒
              </Tag>
            ) : null}
          </Space>
        ),
      },
      {
        title: '声明 scope',
        dataIndex: 'declared_scopes',
        render: (scopes: string[]) =>
          scopes.map((scope) => (
            <Tag key={scope} color={scope.startsWith('item.') || scope === 'price.update' ? 'red' : 'green'}>
              {scope}
            </Tag>
          )),
      },
      { title: '心跳失败次数', dataIndex: 'heartbeat_fail_count', width: 120 },
      {
        title: '最近心跳',
        dataIndex: 'last_heartbeat_at',
        width: 170,
        render: (value: string | null) => formatTime(value),
      },
      {
        title: '操作',
        key: 'actions',
        width: 340,
        fixed: 'right',
        render: (_value: unknown, record) => (
          <Space size="small" wrap>
            {/** ★ F10 危险组合的处置入口：直接跳告警面逐条处置 */}
            {isDangerousCombo(record) ? (
              <Typography.Link
                style={{ color: '#cf1322' }}
                onClick={() => navigate('/settings/permissions')}
              >
                处置越权
              </Typography.Link>
            ) : null}
            <Typography.Link onClick={() => testMutation.mutate(record.adapter_name)}>自检</Typography.Link>
            <Typography.Link onClick={() => setCapabilityAdapter(record.adapter_name)}>
              能力矩阵
            </Typography.Link>
            <Typography.Link
              onClick={() => {
                setConfigAdapter(record);
                configForm.setFieldsValue({
                  declared_scopes: record.declared_scopes,
                  is_enabled: record.is_enabled,
                });
              }}
            >
              配置
            </Typography.Link>
            {record.is_enabled ? null : (
              <Typography.Link
                onClick={() =>
                  setEnableIntent({
                    adapter: record,
                    declared_scopes: record.declared_scopes,
                    source: 'table',
                    formValues: null,
                  })
                }
              >
                启用
              </Typography.Link>
            )}
            <Typography.Link
              onClick={() => {
                setSwitchOpen(true);
                switchForm.setFieldsValue({
                  adapter_name: record.adapter_name,
                  drain_inflight: true,
                });
              }}
            >
              切换为生效
            </Typography.Link>
          </Space>
        ),
      },
    ],
    [switchForm, testMutation, forbiddenScopes],
  );

  const capabilityColumns = useMemo<ColumnsType<CapabilitySpecVo>>(
    () => [
      {
        title: '能力',
        dataIndex: 'name',
        width: 200,
        render: (value: string) =>
          capabilityOptions.find((item) => item.value === value)?.label ?? value,
      },
      {
        title: '支持级别',
        dataIndex: 'level',
        width: 120,
        render: (value: string) => <StatusTag enumKey="CapabilityLevel" value={value} />,
      },
      { title: '降级通道', dataIndex: 'fallback', width: 140, render: (v?: string | null) => v ?? '-' },
      { title: '说明', dataIndex: 'note' },
      {
        title: '实测状态',
        dataIndex: 'unverified',
        width: 120,
        render: (value: boolean) =>
          value ? <Tag color="orange">待实测</Tag> : <Tag color="green">已实测</Tag>,
      },
    ],
    [capabilityOptions, levelOptions],
  );


  return (
    <PageContainer
      title="适配器设置"
      subTitle="上架适配器模式切换 + 履约适配器（妙手 / 逸淘 / 本地）切换；能力声明驱动降级，越权 scope 一律拒绝"
      extra={
        <Space>
          <Button onClick={() => setModeOpen(true)}>切换上架模式</Button>
        </Space>
      }
    >
      <>
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="越权告警已独立成页"
          description="权限与越权告警是告警面而不是配置项，请前往「权限与越权告警」页查看并逐条处置（顶栏红点也可直达）。"
          action={
            <Button size="small" onClick={() => navigate('/settings/permissions')}>
              前往处置
            </Button>
          }
        />

        {/* ★ 有越权拒绝记录且处于停用态：必须显式告知运营"为什么不能用了 / 怎么才能开" */}
        {disabledByScope.length > 0 ? (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 12 }}
            message={`${disabledByScope.length} 个适配器因越权处于停用状态`}
            description={
              <Space direction="vertical" size={4}>
                {disabledByScope.map((adapter) => (
                  <span key={adapter.adapter_name}>
                    <Typography.Text strong>{adapter.display_name}</Typography.Text>
                    <Typography.Text type="secondary">（{adapter.adapter_name}）：</Typography.Text>
                    该适配器因声明了超出授权范围的权限已被停用
                    {adapter.scope_check_message ? ` —— ${adapter.scope_check_message}` : ''}
                    。<Typography.Text type="danger">重新启用前必须先去掉这些越权权限</Typography.Text>
                    ，否则会被再次拒绝。
                  </span>
                ))}
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  系统不会因为你点了「启用」就放行：每次启用都会重新跑一次权限校验，仍越权则再次被拒并记录审计。
                </Typography.Text>
              </Space>
            }
            action={
              <Button size="small" onClick={() => navigate('/settings/permissions')}>
                查看越权记录
              </Button>
            }
          />
        ) : null}

        {/* ★ 有越权拒绝记录但仍启用：状态不一致，必须如实暴露，不能让运营以为"已经处理好了" */}
        {rejectedButEnabled.length > 0 ? (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message={`★ 危险组合：${rejectedButEnabled.length} 个适配器「启用中 + 权限声明被拒」`}
            description={
              <Space direction="vertical" size={4}>
                {rejectedButEnabled.map((adapter) => (
                  <span key={adapter.adapter_name}>
                    <Typography.Text strong>{adapter.display_name}</Typography.Text>
                    <Typography.Text type="secondary">（{adapter.adapter_name}）：</Typography.Text>
                    {adapter.scope_check_message ?? '最近一次 scope 校验被拒'}。
                    被拒的越权声明<strong>不会</strong>写入生效配置，适配器仍在按被拒前的旧声明运行：
                    <span className="erp-mono">
                      {(adapter.declared_scopes ?? []).join('、') || '（无）'}
                    </span>
                    。
                  </span>
                ))}
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  系统不会自动停用适配器。请确认第三方已撤回越权授权后，重新保存一次合规配置以清除 rejected 标记。
                </Typography.Text>
              </Space>
            }
            action={
              <Button size="small" onClick={() => navigate('/settings/permissions')}>
                查看越权记录
              </Button>
            }
          />
        ) : null}

        <Card size="small" style={{ marginBottom: 12 }} title="上架适配器（自研独占写店铺权限）">
          <Row gutter={16} align="middle">
            <Col span={8}>
              <Stat modeLabel="当前模式" value={listingQuery.data?.current_mode ?? '-'} />
            </Col>
            <Col span={16}>
              <Space wrap>
                {(listingQuery.data?.adapters ?? []).map((item) => (
                  <Tag
                    key={`${item.platform}-${item.mode}`}
                    color={item.available ? 'green' : 'default'}
                  >
                    {item.platform} / {item.mode}
                    {item.available ? ' · 可用' : ' · 不可用'}
                  </Tag>
                ))}
              </Space>
            </Col>
          </Row>
        </Card>

        <Typography.Title level={5}>履约适配器</Typography.Title>
        <Table<FulfillmentAdapterVo>
          rowKey="id"
          size="small"
          scroll={{ x: 1600 }}
          /** ★ F10：启用中 + 权限被拒 的危险组合整行红色高亮，避免被淹没在列表里 */
          rowClassName={(record: FulfillmentAdapterVo) =>
            isDangerousCombo(record) ? 'erp-row-danger' : ''
          }
          columns={adapterColumns}
          dataSource={fulfillmentQuery.data?.adapters ?? []}
          pagination={false}
        />

        {capabilityAdapter ? (
          <>
            <Typography.Title level={5} style={{ marginTop: 16 }}>
              能力矩阵 · {capabilityAdapter}
            </Typography.Title>
            <Table<CapabilitySpecVo>
              rowKey="name"
              size="small"
              columns={capabilityColumns}
              dataSource={capabilityQuery.data?.manifest?.capabilities ?? []}
              pagination={false}
            />
            <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8 }}>
              UNSUPPORTED 能力不会抛异常，由调度层按 fallback 降级（如导出 CSV / 人工处理）。
            </Typography.Paragraph>
          </>
        ) : null}
      </>

      {/* 切换履约适配器 */}
      <Modal
        title="切换履约适配器"
        open={switchOpen}
        onCancel={() => setSwitchOpen(false)}
        onOk={() => switchForm.submit()}
        confirmLoading={switchMutation.isPending}
        okText="确认切换"
        cancelText="取消"
        destroyOnHidden
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="切换只影响新订单"
          description="已落库订单的 adapter_name 不变，在途订单按原渠道跑完；切换动作落审计（action_type=adapter_switch）。"
        />
        <Form
          form={switchForm}
          layout="vertical"
          onFinish={(values: SwitchFormValues) => switchMutation.mutate(values)}
        >
          <Form.Item name="adapter_name" label="目标适配器" rules={[{ required: true }]}>
            <Select options={adapterOptions} />
          </Form.Item>
          <Form.Item name="reason" label="切换原因" rules={[{ required: true }]}>
            <Input.TextArea rows={3} placeholder="如：妙手涨价，切换逸淘" />
          </Form.Item>
          <Form.Item name="drain_inflight" label="在途订单按原渠道跑完">
            <Radio.Group
              options={[
                { label: '是（推荐）', value: true },
                { label: '否', value: false },
              ]}
            />
          </Form.Item>
        </Form>
      </Modal>

      {/* 适配器配置（越权 scope 会被 403 / code 5003 拒绝） */}
      <Modal
        title={configAdapter ? `配置适配器 · ${configAdapter.display_name}` : '配置适配器'}
        open={configAdapter !== null}
        onCancel={() => setConfigAdapter(null)}
        onOk={() => configForm.submit()}
        confirmLoading={configMutation.isPending}
        okText="保存"
        cancelText="取消"
        destroyOnHidden
      >
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 12 }}
          message="只允许声明 order.read / logistics.write"
          description="若声明商品编辑 / 上新 / 改价等 scope，保存接口会返回 403（code 5003），系统拒绝启用并落审计。"
        />
          {configAdapter && hasScopeRejection(configAdapter) ? (
            <Alert
              type={configAdapter.is_enabled ? 'warning' : 'error'}
              showIcon
              style={{ marginBottom: 12 }}
              message={
                configAdapter.is_enabled
                  ? `${configAdapter.display_name} 最近一次 scope 校验被拒（当前仍启用）`
                  : `${configAdapter.display_name} 因越权处于停用状态`
              }
              description={
                configAdapter.is_enabled
                  ? `该适配器因声明了超出授权范围的权限被系统拒绝${
                      configAdapter.scope_check_message ? ` —— ${configAdapter.scope_check_message}` : ''
                    }。越权声明不会写入生效配置，适配器仍按旧声明运行；保存一次合规配置即可清除该标记。`
                  : `该适配器因声明了超出授权范围的权限已被停用${
                      configAdapter.scope_check_message ? ` —— ${configAdapter.scope_check_message}` : ''
                    }。重新启用前必须先去掉这些越权权限，否则保存会被再次拒绝（403 / code 5003）。`
              }
            />
          ) : null}
          <Form
            form={configForm}
            layout="vertical"
            onFinish={(values: AdapterConfigFormValues) => {
              if (!configAdapter) return;
              // ★ 停用 → 启用 必须二次确认；其余（停用、只改 scope）直接提交
              if (values.is_enabled && !configAdapter.is_enabled) {
                setEnableIntent({
                  adapter: configAdapter,
                  declared_scopes: values.declared_scopes,
                  source: 'form',
                  formValues: values,
                });
                return;
              }
              configMutation.mutate({ name: configAdapter.adapter_name, values });
            }}
          >
          <Form.Item name="declared_scopes" label="声明 scope" rules={[{ required: true }]}>
            <Select
              mode="multiple"
              options={[...ALLOWED_SCOPES, ...FORBIDDEN_SCOPES].map((scope) => ({
                label: FORBIDDEN_SCOPES.includes(scope) ? `${scope}（越权，将被拒绝）` : scope,
                value: scope,
              }))}
            />
          </Form.Item>
          <Form.Item name="is_enabled" label="是否启用" valuePropName="checked">
            <Switch checkedChildren="启用" unCheckedChildren="停用" />
          </Form.Item>
        </Form>
      </Modal>

      {/* ★ 重新启用二次确认：告知后果，但绝不"确认即放行" */}
      <Modal
        title={enableIntent ? `重新启用适配器 · ${enableIntent.adapter.display_name}` : '重新启用适配器'}
        open={enableIntent !== null}
        onCancel={() => setEnableIntent(null)}
        onOk={confirmEnable}
        confirmLoading={configMutation.isPending || enableMutation.isPending}
        okText="确认启用"
        cancelText="取消"
        okButtonProps={{ disabled: deniedScopesOf(enableIntent?.declared_scopes).length > 0 }}
        destroyOnHidden
      >
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="重新启用会立即触发一次权限校验"
          description="确认后系统会重新校验该适配器的声明 scope。若仍包含越权权限，将再次被拒绝（403 / code 5003）并写入一条审计（action_type=permission_change），顶栏红点也会再次亮起。"
        />
        {deniedScopesOf(enableIntent?.declared_scopes).length > 0 ? (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 12 }}
            message="当前声明仍包含越权 scope，无法启用"
            description={
              <Space direction="vertical" size={4}>
                <Space size={4} wrap>
                  {deniedScopesOf(enableIntent?.declared_scopes).map((scope) => (
                    <Tag key={scope} color="red">
                      {scope}
                    </Tag>
                  ))}
                </Space>
                <span>
                  正确路径：<strong>先去掉上述越权 scope → 保存 → 再回来启用</strong>
                  。这里不会提供"确认即强开"的通道，越权权限校验只能由后端判定。
                </span>
              </Space>
            }
          />
        ) : (
          <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}>
            本次将随请求提交声明 scope：
            <span className="erp-mono">
              {(enableIntent?.declared_scopes ?? []).join('、') || '（无）'}
            </span>
            ，由后端重新校验。
          </Typography.Paragraph>
        )}
      </Modal>

      {/* 切换上架模式 */}
      <Modal
        title="切换上架适配器模式"
        open={modeOpen}
        onCancel={() => setModeOpen(false)}
        onOk={() => modeForm.submit()}
        confirmLoading={modeMutation.isPending}
        okText="确认切换"
        cancelText="取消"
        destroyOnHidden
      >
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="Mock 模式产出的数据不参与真实履约"
          description="真实 API 需要平台资质；半自动生成素材包由人工发布。三种模式可随时切换，切换留痕。"
        />
        <Form
          form={modeForm}
          layout="vertical"
          onFinish={(values: ModeFormValues) => modeMutation.mutate(values)}
        >
          <Form.Item name="mode" label="模式" rules={[{ required: true }]}>
            <Select options={modeOptions} />
          </Form.Item>
          <Form.Item name="reason" label="切换原因" rules={[{ required: true }]}>
            <Input.TextArea rows={3} />
          </Form.Item>
        </Form>
      </Modal>
    </PageContainer>
  );
}

/** 顶部指标展示（上架模式 / 当前生效渠道） */
function Stat(props: { modeLabel: string; value: string }): JSX.Element {
  return (
    <Space direction="vertical" size={0}>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {props.modeLabel}
      </Typography.Text>
      <Typography.Text strong style={{ fontSize: 18 }}>
        {props.value}
      </Typography.Text>
    </Space>
  );
}
