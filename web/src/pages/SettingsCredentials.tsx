import { useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Descriptions,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  authorizePlatformAccount,
  createCredential,
  createPlatformAccount,
  deleteCredential,
  listCredentials,
  listPlatformAccounts,
  revealCredential,
  testCredential,
} from '@/api/system';
import type { CredentialVo, PlatformAccountVo } from '@/api/types';
import PageContainer from '@/components/PageContainer';
import StatusTag from '@/components/StatusTag';
import { formatTime } from '@/components/AuditTimeline';
import { ALLOWED_SCOPES, FORBIDDEN_SCOPES } from '@/constants/enums';
import { useEnumOptions } from '@/hooks/useEnumOptions';

interface CredentialFormValues {
  owner_type: string;
  owner_key: string;
  credential_key: string;
  value: string;
}

interface RevealFormValues {
  verify_code: string;
}

interface AccountFormValues {
  platform: string;
  shop_id: string;
  shop_name: string;
  granted_scopes: string[];
  app_key: string;
  app_secret: string;
  access_token: string;
}

const OWNER_TYPE_OPTIONS = [
  { label: '平台账号', value: 'platform' },
  { label: '适配器', value: 'adapter' },
  { label: '货源（1688）', value: 'source' },
];

const CREDENTIAL_KEY_OPTIONS = [
  { label: 'app_key', value: 'app_key' },
  { label: 'app_secret', value: 'app_secret' },
  { label: 'access_token', value: 'access_token' },
  { label: '自定义', value: 'custom' },
];

/**
 * P15 平台凭证设置：加密存储 + 表单掩码 + 连通性测试 + 二次验证查看明文。
 * 凭证明文永不落库、永不入日志、永不出现在 API 响应（仅 reveal 二次验证后临时返回）。
 */
export default function SettingsCredentials(): JSX.Element {
  const queryClient = useQueryClient();
  const platformOptions = useEnumOptions('Platform');

  const [activeTab, setActiveTab] = useState<string>('credentials');
  const [createOpen, setCreateOpen] = useState<boolean>(false);
  const [revealId, setRevealId] = useState<number | null>(null);
  const [revealedValue, setRevealedValue] = useState<string>('');
  const [accountOpen, setAccountOpen] = useState<boolean>(false);

  const [credentialForm] = Form.useForm<CredentialFormValues>();
  const [revealForm] = Form.useForm<RevealFormValues>();
  const [accountForm] = Form.useForm<AccountFormValues>();

  const credentialsQuery = useQuery({
    queryKey: ['credentials'],
    queryFn: () => listCredentials(),
    enabled: activeTab === 'credentials',
  });

  const accountsQuery = useQuery({
    queryKey: ['platform-accounts'],
    queryFn: () => listPlatformAccounts(),
    enabled: activeTab === 'accounts',
  });

  const createMutation = useMutation({
    mutationFn: (body: CredentialFormValues) => createCredential(body),
    onSuccess: () => {
      message.success('凭证已加密保存（明文永不落库）');
      setCreateOpen(false);
      credentialForm.resetFields();
      void queryClient.invalidateQueries({ queryKey: ['credentials'] });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => deleteCredential(id),
    onSuccess: () => {
      message.success('凭证已删除（已落审计）');
      void queryClient.invalidateQueries({ queryKey: ['credentials'] });
    },
  });

  const testMutation = useMutation({
    mutationFn: (id: number) => testCredential(id),
    onSuccess: (data) => {
      if (data.ok) message.success(`连通性正常（${data.latency_ms} ms）：${data.message}`);
      else message.error(`连通性失败：${data.message}`);
    },
  });

  const revealMutation = useMutation({
    mutationFn: (body: { id: number; verify_code: string }) =>
      revealCredential(body.id, body.verify_code),
    onSuccess: (data) => {
      setRevealedValue(data.value_plain);
      message.success(`明文仅在 ${data.expires_in_sec} 秒内有效，查看行为已落审计`);
    },
  });

  const accountMutation = useMutation({
    mutationFn: (body: AccountFormValues) =>
      createPlatformAccount({
        platform: body.platform,
        shop_id: body.shop_id,
        shop_name: body.shop_name,
        granted_scopes: body.granted_scopes,
        credential: {
          app_key: body.app_key,
          app_secret: body.app_secret,
          access_token: body.access_token,
        },
      }),
    onSuccess: () => {
      message.success('平台账号已创建');
      setAccountOpen(false);
      accountForm.resetFields();
      void queryClient.invalidateQueries({ queryKey: ['platform-accounts'] });
    },
  });

  const authorizeMutation = useMutation({
    mutationFn: (body: { id: number; granted_scopes: string[] }) =>
      authorizePlatformAccount(body.id, body.granted_scopes),
    onSuccess: (data) => {
      if (data.accepted) message.success(`授权成功，生效 scope：${data.effective_scopes.join('、')}`);
      else message.error(`授权被拒，越权 scope：${data.denied.join('、')}（已落审计）`);
      void queryClient.invalidateQueries({ queryKey: ['platform-accounts'] });
    },
  });

  const credentialColumns = useMemo<ColumnsType<CredentialVo>>(
    () => [
      { title: 'ID', dataIndex: 'id', width: 70 },
      {
        title: '归属类型',
        dataIndex: 'owner_type',
        width: 110,
        render: (value: string) =>
          OWNER_TYPE_OPTIONS.find((item) => item.value === value)?.label ?? value,
      },
      { title: '归属标识', dataIndex: 'owner_key', width: 160 },
      { title: '凭证项', dataIndex: 'credential_key', width: 140, className: 'erp-mono' },
      {
        title: '值（掩码）',
        dataIndex: 'value_masked',
        render: (value: string) => <span className="erp-mono">{value}</span>,
      },
      { title: '过期时间', dataIndex: 'expires_at', width: 170, render: (v: string | null) => formatTime(v) },
      { title: '更新时间', dataIndex: 'updated_at', width: 170, render: (v: string) => formatTime(v) },
      {
        title: '操作',
        key: 'actions',
        width: 230,
        render: (_value: unknown, record) => (
          <Space size="small" wrap>
            <Typography.Link onClick={() => testMutation.mutate(record.id)}>连通性测试</Typography.Link>
            <Typography.Link
              onClick={() => {
                setRevealId(record.id);
                setRevealedValue('');
                revealForm.resetFields();
              }}
            >
              查看明文
            </Typography.Link>
            <Typography.Link
              onClick={() => {
                Modal.confirm({
                  title: '确认删除该凭证？',
                  content: '删除后相关适配器/平台账号将无法调用，操作会落审计。',
                  okText: '确认删除',
                  okButtonProps: { danger: true },
                  cancelText: '取消',
                  onOk: () => deleteMutation.mutate(record.id),
                });
              }}
            >
              删除
            </Typography.Link>
          </Space>
        ),
      },
    ],
    [deleteMutation, revealForm, testMutation],
  );

  const accountColumns = useMemo<ColumnsType<PlatformAccountVo>>(
    () => [
      { title: 'ID', dataIndex: 'id', width: 70 },
      {
        title: '平台',
        dataIndex: 'platform',
        width: 100,
        render: (value: string) => <StatusTag enumKey="Platform" value={value} />,
      },
      { title: '店铺 ID', dataIndex: 'shop_id', width: 150, className: 'erp-mono' },
      { title: '店铺名称', dataIndex: 'shop_name', width: 180 },
      {
        title: '已授权 scope',
        dataIndex: 'granted_scopes',
        render: (scopes: string[]) =>
          scopes.map((scope) => (
            <Tag key={scope} color={FORBIDDEN_SCOPES.includes(scope) ? 'red' : 'green'}>
              {scope}
              {FORBIDDEN_SCOPES.includes(scope) ? '（越权）' : ''}
            </Tag>
          )),
      },
      { title: 'Token（掩码）', dataIndex: 'token_masked', width: 180, className: 'erp-mono' },
      { title: '状态', dataIndex: 'status', width: 90 },
      {
        title: '操作',
        key: 'actions',
        width: 160,
        render: (_value: unknown, record) => (
          <Typography.Link onClick={() => authorizeMutation.mutate({ id: record.id, granted_scopes: ALLOWED_SCOPES })}>
            按最小权限重新授权
          </Typography.Link>
        ),
      },
    ],
    [authorizeMutation],
  );

  return (
    <PageContainer
      title="平台凭证设置"
      subTitle="凭证明文永不落库、永不入日志、永不出现在 API 响应；查看明文需二次验证并留审计"
      extra={
        activeTab === 'credentials' ? (
          <Button type="primary" onClick={() => setCreateOpen(true)}>
            新增凭证
          </Button>
        ) : (
          <Button type="primary" onClick={() => setAccountOpen(true)}>
            新增平台账号
          </Button>
        )
      }
    >
      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={[
          {
            key: 'credentials',
            label: '凭证',
            children: (
              <>
                <Alert
                  type="warning"
                  showIcon
                  style={{ marginBottom: 12 }}
                  message="最小权限提示"
                  description={`履约类凭证只允许 ${ALLOWED_SCOPES.join(' / ')}；${FORBIDDEN_SCOPES.slice(0, 5).join(' / ')} 等一律拒绝启用并落审计。`}
                />
                <Table<CredentialVo>
                  rowKey="id"
                  size="small"
                  scroll={{ x: 1400 }}
                  columns={credentialColumns}
                  dataSource={credentialsQuery.data ?? []}
                  pagination={false}
                />
              </>
            ),
          },
          {
            key: 'accounts',
            label: '平台账号授权',
            children: (
              <Table<PlatformAccountVo>
                rowKey="id"
                size="small"
                scroll={{ x: 1400 }}
                columns={accountColumns}
                dataSource={accountsQuery.data ?? []}
                pagination={false}
              />
            ),
          },
        ]}
      />

      {/* 新增凭证 */}
      <Modal
        title="新增凭证"
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={() => credentialForm.submit()}
        confirmLoading={createMutation.isPending}
        okText="保存"
        cancelText="取消"
        destroyOnHidden
      >
        <Form
          form={credentialForm}
          layout="vertical"
          onFinish={(values: CredentialFormValues) => createMutation.mutate(values)}
        >
          <Form.Item name="owner_type" label="归属类型" rules={[{ required: true }]}>
            <Select options={OWNER_TYPE_OPTIONS} />
          </Form.Item>
          <Form.Item name="owner_key" label="归属标识" rules={[{ required: true }]}>
            <Input placeholder="如 taobao_shop_001 / miaoshou" />
          </Form.Item>
          <Form.Item name="credential_key" label="凭证项" rules={[{ required: true }]}>
            <Select options={CREDENTIAL_KEY_OPTIONS} />
          </Form.Item>
          <Form.Item
            name="value"
            label="明文值"
            extra="提交后立即加密存储，明文不再出现在任何响应中"
            rules={[{ required: true }]}
          >
            <Input.Password autoComplete="new-password" />
          </Form.Item>
        </Form>
      </Modal>

      {/* 二次验证查看明文 */}
      <Modal
        title={revealId ? `查看凭证明文 · #${revealId}` : '查看凭证明文'}
        open={revealId !== null}
        onCancel={() => {
          setRevealId(null);
          setRevealedValue('');
        }}
        footer={null}
        destroyOnHidden
      >
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 12 }}
          message="敏感操作"
          description="查看明文的动作会写入审计日志（action_type=credential_change），且明文仅在 60 秒内有效。"
        />
        {revealedValue ? (
          <Descriptions bordered size="small" column={1}>
            <Descriptions.Item label="明文">
              <Typography.Text copyable className="erp-mono">
                {revealedValue}
              </Typography.Text>
            </Descriptions.Item>
          </Descriptions>
        ) : (
          <Form
            form={revealForm}
            layout="vertical"
            onFinish={(values: RevealFormValues) => {
              if (revealId === null) return;
              revealMutation.mutate({ id: revealId, verify_code: values.verify_code });
            }}
          >
            <Form.Item name="verify_code" label="二次验证码" rules={[{ required: true }]}>
              <Input.Password placeholder="请输入验证码" />
            </Form.Item>
            <Button type="primary" htmlType="submit" loading={revealMutation.isPending}>
              验证并查看
            </Button>
          </Form>
        )}
      </Modal>

      {/* 新增平台账号 */}
      <Modal
        title="新增平台账号"
        open={accountOpen}
        onCancel={() => setAccountOpen(false)}
        onOk={() => accountForm.submit()}
        confirmLoading={accountMutation.isPending}
        okText="保存"
        cancelText="取消"
        destroyOnHidden
      >
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={`只允许申请 ${ALLOWED_SCOPES.join(' / ')}`}
          description="申请商品编辑 / 上新 / 改价等 scope 会被系统拒绝（403），并落审计与告警。"
        />
        <Form
          form={accountForm}
          layout="vertical"
          onFinish={(values: AccountFormValues) => accountMutation.mutate(values)}
        >
          <Form.Item name="platform" label="平台" rules={[{ required: true }]}>
            <Select options={platformOptions} />
          </Form.Item>
          <Form.Item name="shop_id" label="店铺 ID" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="shop_name" label="店铺名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="granted_scopes" label="申请 scope" rules={[{ required: true }]}>
            <Select
              mode="multiple"
              options={[...ALLOWED_SCOPES, ...FORBIDDEN_SCOPES].map((scope) => ({
                label: FORBIDDEN_SCOPES.includes(scope) ? `${scope}（越权，将被拒绝）` : scope,
                value: scope,
              }))}
            />
          </Form.Item>
          <Form.Item name="app_key" label="app_key" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="app_secret" label="app_secret" rules={[{ required: true }]}>
            <Input.Password />
          </Form.Item>
          <Form.Item name="access_token" label="access_token">
            <Input.Password />
          </Form.Item>
        </Form>
      </Modal>
    </PageContainer>
  );
}
