import { useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Drawer,
  Form,
  Input,
  Modal,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tabs,
  Tag,
  Typography,
  Upload,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { UploadOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  deleteSkuMapping,
  detectMappingConflicts,
  exportSkuMappings,
  getMappingStats,
  importSkuMappings,
  listMappingConflicts,
  listMappingLogs,
  listMappingPending,
  listSkuMappings,
  pushSkuMappings,
  resolveMappingPending,
  restoreSkuMapping,
  updateSkuMapping,
  validateSkuMappings,
} from '@/api/mapping';
import { asApiError, triggerDownload } from '@/api/client';
import type {
  MappingChangeLogVo,
  MappingConflictVo,
  MappingPendingVo,
  MappingValidationVo,
  SkuMappingVo,
} from '@/api/types';
import PageContainer from '@/components/PageContainer';
import ConflictBadge from '@/components/ConflictBadge';
import MappingValidationModal from '@/components/MappingValidationModal';
import MockBadge from '@/components/MockBadge';
import MoneyText from '@/components/MoneyText';
import StatusTag from '@/components/StatusTag';
import AuditTimeline, { formatTime } from '@/components/AuditTimeline';
import { useEnumOptions } from '@/hooks/useEnumOptions';
import { usePagination } from '@/hooks/usePagination';

interface MappingFilterValues {
  platform: string;
  shop_id: string;
  status: string;
  has_conflict: string;
  keyword: string;
}

interface MappingEditFormValues {
  source_sku_code_1688: string;
  purchase_cost: string;
  status: string;
  remark: string;
}

interface PendingResolveFormValues {
  action: 'confirm' | 'reject' | 'manual_assign';
  new_source_sku_code_1688: string;
  note: string;
}

interface ExportFormValues {
  adapter_name: 'miaoshou' | 'yitao' | 'generic';
  platform: string;
}

interface ValidateFormValues {
  source_product_id: string;
  platform: string;
  shop_id: string;
  sku_codes: string;
}

const DEFAULT_FILTERS: MappingFilterValues = {
  platform: '',
  shop_id: '',
  status: '',
  has_conflict: '',
  keyword: '',
};

/**
 * P6 SKU 映射管理。
 * P7「变更待确认」已并入第二个 Tab；另设「冲突记录」Tab 展示四类冲突检测结果。
 */
export default function SkuMappings(): JSX.Element {
  const queryClient = useQueryClient();
  const { filters, params, setFilters, tablePagination, onTableChange, resetFilters } =
    usePagination<MappingFilterValues>(DEFAULT_FILTERS);
  const platformOptions = useEnumOptions('Platform');
  const statusOptions = useEnumOptions('MappingStatus');
  const conflictTypeOptions = useEnumOptions('ConflictType');
  const exportTargetOptions = useEnumOptions('MappingExportTarget');
  const pendingActionOptions = useEnumOptions('MappingPendingAction');

  const [activeTab, setActiveTab] = useState<string>('mappings');
  const [editRecord, setEditRecord] = useState<SkuMappingVo | null>(null);
  const [logsMappingId, setLogsMappingId] = useState<number | null>(null);
  const [exportOpen, setExportOpen] = useState<boolean>(false);
  const [pushOpen, setPushOpen] = useState<boolean>(false);
  const [validateOpen, setValidateOpen] = useState<boolean>(false);
  const [validateFormOpen, setValidateFormOpen] = useState<boolean>(false);
  const [validationResult, setValidationResult] = useState<MappingValidationVo | null>(null);
  const [pendingRecord, setPendingRecord] = useState<MappingPendingVo | null>(null);

  const [editForm] = Form.useForm<MappingEditFormValues>();
  const [exportForm] = Form.useForm<ExportFormValues>();
  const [validateForm] = Form.useForm<ValidateFormValues>();
  const [pendingForm] = Form.useForm<PendingResolveFormValues>();

  const listQuery = useQuery({
    queryKey: ['sku-mappings', params],
    queryFn: () => listSkuMappings(params),
    enabled: activeTab === 'mappings',
  });

  const pendingQuery = useQuery({
    queryKey: ['sku-mappings', 'pending'],
    queryFn: () => listMappingPending({ page: 1, page_size: 100 }),
    enabled: activeTab === 'pending',
  });

  const conflictQuery = useQuery({
    queryKey: ['sku-mappings', 'conflicts'],
    queryFn: () => listMappingConflicts({ page: 1, page_size: 100 }),
    enabled: activeTab === 'conflicts',
  });

  const statsQuery = useQuery({
    queryKey: ['sku-mappings', 'stats'],
    queryFn: getMappingStats,
  });

  const logsQuery = useQuery({
    queryKey: ['sku-mappings', logsMappingId, 'logs'],
    queryFn: () => listMappingLogs(logsMappingId as number),
    enabled: logsMappingId !== null,
  });

  const updateMutation = useMutation({
    mutationFn: (body: { id: number; values: Partial<MappingEditFormValues> }) =>
      updateSkuMapping(body.id, {
        source_sku_code_1688: body.values.source_sku_code_1688,
        purchase_cost: body.values.purchase_cost,
        status: body.values.status,
        remark: body.values.remark,
      }),
    onSuccess: () => {
      message.success('映射已更新，变更已写入 mapping_change_log');
      setEditRecord(null);
      void queryClient.invalidateQueries({ queryKey: ['sku-mappings'] });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (body: { id: number; reason: string }) => deleteSkuMapping(body.id, body.reason),
    onSuccess: () => {
      message.success('已软删除（保留 180 天，可回滚）');
      void queryClient.invalidateQueries({ queryKey: ['sku-mappings'] });
    },
  });

  const restoreMutation = useMutation({
    mutationFn: (id: number) => restoreSkuMapping(id),
    onSuccess: () => {
      message.success('已恢复');
      void queryClient.invalidateQueries({ queryKey: ['sku-mappings'] });
    },
  });

  const detectMutation = useMutation({
    mutationFn: () => detectMappingConflicts({ all: true }),
    onSuccess: (data) => {
      message.success(`冲突检测已受理，命中 ${data.detected} 项`);
      void queryClient.invalidateQueries({ queryKey: ['sku-mappings'] });
    },
  });

  const exportMutation = useMutation({
    mutationFn: (body: ExportFormValues) => exportSkuMappings(body),
    onSuccess: (data) => {
      triggerDownload(data.download_url);
      message.success(`已导出 ${data.row_count} 行`);
      setExportOpen(false);
    },
  });

  const pushMutation = useMutation({
    mutationFn: (body: { adapter_name: string; all_valid: boolean }) => pushSkuMappings(body),
    onSuccess: (data) => {
      message.success(
        `推送已受理：总计 ${data.pushed}，成功 ${data.success}，失败 ${data.failed}${
          data.degraded ? '（能力不支持，已降级为 CSV 导出）' : ''
        }`,
      );
      if (data.download_url) triggerDownload(data.download_url);
      setPushOpen(false);
    },
  });

  const importMutation = useMutation({
    mutationFn: (file: File) => importSkuMappings(file),
    onSuccess: (data) => {
      message.success(
        `导入完成：新增 ${data.added}，修改 ${data.modified}，冲突 ${data.conflict}，孤儿 ${data.orphan}`,
      );
      void queryClient.invalidateQueries({ queryKey: ['sku-mappings'] });
    },
  });

  const validateMutation = useMutation({
    mutationFn: (body: {
      source_product_id: number;
      platform: string;
      shop_id: string;
      sku_codes: string[];
    }) => validateSkuMappings(body),
    onSuccess: (data) => {
      setValidationResult(data);
      setValidateOpen(true);
    },
    onError: (error: unknown) => {
      const apiError = asApiError(error);
      if (apiError?.data) {
        setValidationResult(apiError.data as MappingValidationVo);
        setValidateOpen(true);
      }
    },
  });

  const resolveMutation = useMutation({
    mutationFn: (body: { id: number; values: PendingResolveFormValues }) =>
      resolveMappingPending(body.id, {
        action: body.values.action,
        new_source_sku_code_1688: body.values.new_source_sku_code_1688 || undefined,
        note: body.values.note || undefined,
      }),
    onSuccess: () => {
      message.success('工单已处理');
      setPendingRecord(null);
      pendingForm.resetFields();
      void queryClient.invalidateQueries({ queryKey: ['sku-mappings'] });
    },
  });

  const mappingColumns = useMemo<ColumnsType<SkuMappingVo>>(
    () => [
      { title: 'ID', dataIndex: 'id', width: 70 },
      {
        title: '平台 / 店铺',
        key: 'platform',
        width: 150,
        render: (_value: unknown, record) => (
          <Space size={4}>
            <StatusTag enumKey="Platform" value={record.platform} />
            <span className="erp-mono">{record.shop_id}</span>
          </Space>
        ),
      },
      { title: '店铺商品 ID', dataIndex: 'shop_item_id', width: 150, className: 'erp-mono' },
      { title: '店铺 SKU 编码', dataIndex: 'shop_sku_code', width: 160, className: 'erp-mono' },
      {
        title: '1688 SKU',
        key: 'source',
        width: 200,
        render: (_value: unknown, record) => (
          <Space direction="vertical" size={0}>
            <span className="erp-mono">{record.source_sku_code_1688 ?? '-'}</span>
            <span style={{ fontSize: 12, color: '#8c8c8c' }}>{record.source_sku_name ?? ''}</span>
          </Space>
        ),
      },
      {
        title: '采购成本',
        dataIndex: 'purchase_cost',
        width: 110,
        render: (value: string) => <MoneyText value={value} />,
      },
      {
        title: '状态',
        dataIndex: 'status',
        width: 100,
        render: (value: string) => <StatusTag enumKey="MappingStatus" value={value} />,
      },
      {
        title: '冲突',
        key: 'conflict',
        width: 150,
        render: (_value: unknown, record) =>
          record.has_conflict ? (
            <ConflictBadge level={record.conflict_level} conflictTypes={record.conflict_types} />
          ) : (
            <Tag>无</Tag>
          ),
      },
      {
        title: 'Mock',
        dataIndex: 'is_mock',
        width: 80,
        render: (value: boolean) => <MockBadge isMock={value} />,
      },
      { title: '来源', dataIndex: 'source', width: 90 },
      {
        title: '最近推送',
        key: 'push',
        width: 150,
        render: (_value: unknown, record) => (
          <Space direction="vertical" size={0}>
            <span>{record.last_push_status ?? '-'}</span>
            <span style={{ fontSize: 12, color: '#8c8c8c' }}>{formatTime(record.last_pushed_at)}</span>
          </Space>
        ),
      },
      {
        title: '操作',
        key: 'actions',
        width: 230,
        fixed: 'right',
        render: (_value: unknown, record) => (
          <Space size="small" wrap>
            <Typography.Link
              onClick={() => {
                setEditRecord(record);
                editForm.setFieldsValue({
                  source_sku_code_1688: record.source_sku_code_1688 ?? '',
                  purchase_cost: record.purchase_cost,
                  status: record.status,
                  remark: record.remark ?? '',
                });
              }}
            >
              编辑
            </Typography.Link>
            <Typography.Link onClick={() => setLogsMappingId(record.id)}>变更日志</Typography.Link>
            {record.is_deleted ? (
              <Typography.Link onClick={() => restoreMutation.mutate(record.id)}>恢复</Typography.Link>
            ) : (
              <Typography.Link
                onClick={() =>
                  Modal.confirm({
                    title: '确认删除该映射？',
                    content:
                      'SKU 映射是最高等级资产：删除后订单将无法匹配，订单会被挂起。软删除保留 180 天可回滚。',
                    okText: '确认删除',
                    okButtonProps: { danger: true },
                    cancelText: '取消',
                    onOk: () => deleteMutation.mutate({ id: record.id, reason: '人工删除' }),
                  })
                }
              >
                删除
              </Typography.Link>
            )}
          </Space>
        ),
      },
    ],
    [deleteMutation, editForm, restoreMutation],
  );

  const pendingColumns = useMemo<ColumnsType<MappingPendingVo>>(
    () => [
      { title: 'ID', dataIndex: 'id', width: 70 },
      { title: '店铺 SKU 编码', dataIndex: 'shop_sku_code', width: 160, className: 'erp-mono' },
      { title: '货源商品', dataIndex: 'source_product_title', ellipsis: true },
      {
        title: '变更类型',
        dataIndex: 'change_type',
        width: 120,
        render: (value: string) => <StatusTag enumKey="MappingChangeType" value={value} />,
      },
      {
        title: '原值 → 新值',
        key: 'change',
        width: 220,
        render: (_value: unknown, record) => (
          <span className="erp-mono">
            {record.old_value ?? '-'} → {record.new_value ?? '-'}
          </span>
        ),
      },
      {
        title: '影响订单数',
        dataIndex: 'affected_order_count',
        width: 110,
        render: (value: number) => (
          <Typography.Text type={value > 0 ? 'danger' : undefined} strong={value > 0}>
            {value}
          </Typography.Text>
        ),
      },
      { title: '来源', dataIndex: 'source', width: 90 },
      { title: '检测时间', dataIndex: 'detected_at', width: 170, render: (v: string) => formatTime(v) },
      {
        title: '操作',
        key: 'actions',
        width: 100,
        render: (_value: unknown, record) => (
          <Typography.Link
            onClick={() => {
              setPendingRecord(record);
              pendingForm.setFieldsValue({ action: 'confirm' });
            }}
          >
            处理
          </Typography.Link>
        ),
      },
    ],
    [pendingForm],
  );

  const conflictColumns = useMemo<ColumnsType<MappingConflictVo>>(
    () => [
      { title: 'ID', dataIndex: 'id', width: 70 },
      {
        title: '等级',
        dataIndex: 'level',
        width: 150,
        render: (_value: unknown, record) => (
          <ConflictBadge level={record.level} conflictTypes={[record.conflict_type]} compact />
        ),
      },
      {
        title: '冲突类型',
        dataIndex: 'conflict_type',
        width: 180,
        render: (value: string) =>
          conflictTypeOptions.find((item) => item.value === value)?.label ?? value,
      },
      { title: '店铺 SKU 编码', dataIndex: 'shop_sku_code', width: 160, className: 'erp-mono' },
      { title: '说明', dataIndex: 'description', ellipsis: true },
      {
        title: '是否已解决',
        dataIndex: 'is_resolved',
        width: 110,
        render: (value: boolean) => (value ? <Tag color="green">已解决</Tag> : <Tag color="red">未解决</Tag>),
      },
      { title: '检测时间', dataIndex: 'detected_at', width: 170, render: (v: string) => formatTime(v) },
    ],
    [conflictTypeOptions],
  );

  const logColumns = useMemo<ColumnsType<MappingChangeLogVo>>(
    () => [
      { title: '时间', dataIndex: 'created_at', width: 170, render: (v: string) => formatTime(v) },
      { title: '动作', dataIndex: 'change_action', width: 120 },
      { title: '字段', dataIndex: 'field_name', width: 160 },
      {
        title: '前值 → 后值',
        key: 'value',
        render: (_value: unknown, record) => (
          <span className="erp-mono">
            {record.old_value ?? '-'} → {record.new_value ?? '-'}
          </span>
        ),
      },
      { title: '来源', dataIndex: 'change_source', width: 90 },
      { title: '操作人', dataIndex: 'operator', width: 100, render: (v?: string) => v ?? '-' },
      { title: 'trace_id', dataIndex: 'trace_id', width: 200, className: 'erp-mono' },
    ],
    [],
  );

  const stats = statsQuery.data;

  return (
    <PageContainer
      title="SKU 映射管理"
      subTitle="映射是最高等级资产：变更全量留痕、软删除保留 180 天可回滚；P0 冲突与缺失映射会硬拦截上架"
      loading={listQuery.isLoading && activeTab === 'mappings'}
      alert={
        stats ? (
          <Card size="small" styles={{ body: { padding: 12 } }}>
            <Row gutter={16}>
              <Col span={4}>
                <Statistic title="映射总数" value={stats.total} />
              </Col>
              <Col span={4}>
                <Statistic title="有效" value={stats.valid} valueStyle={{ color: '#3f8600' }} />
              </Col>
              <Col span={4}>
                <Statistic title="待确认" value={stats.pending_confirm} valueStyle={{ color: '#d46b08' }} />
              </Col>
              <Col span={4}>
                <Statistic title="失效" value={stats.invalid} valueStyle={{ color: '#cf1322' }} />
              </Col>
              <Col span={4}>
                <Statistic title="P0 冲突" value={stats.conflict_p0} valueStyle={{ color: '#cf1322' }} />
              </Col>
              <Col span={4}>
                <Statistic title="P1 冲突" value={stats.conflict_p1} valueStyle={{ color: '#d46b08' }} />
              </Col>
            </Row>
          </Card>
        ) : null
      }
      extra={
        <Space wrap>
          <Button
            onClick={() =>
              detectMutation.mutate()
            }
            loading={detectMutation.isPending}
          >
            检测冲突
          </Button>
          <Button onClick={() => setExportOpen(true)}>导出 CSV</Button>
          <Upload
            showUploadList={false}
            accept=".csv,.txt"
            beforeUpload={(file) => {
              importMutation.mutate(file);
              return false;
            }}
          >
            <Button icon={<UploadOutlined />}>导入</Button>
          </Upload>
          <Button onClick={() => setPushOpen(true)}>推送第三方</Button>
          <Button onClick={() => setValidateFormOpen(true)}>上架前校验</Button>
          <Button onClick={resetFilters}>重置筛选</Button>
        </Space>
      }
    >
      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={[
          {
            key: 'mappings',
            label: '映射列表',
            children: (
              <>
                <Form
                  layout="inline"
                  style={{ marginBottom: 12, rowGap: 8 }}
                  initialValues={filters}
                  onFinish={(values: MappingFilterValues) => setFilters(values)}
                >
                  <Form.Item name="platform" label="平台">
                    <Select allowClear placeholder="全部" style={{ width: 120 }} options={platformOptions} />
                  </Form.Item>
                  <Form.Item name="shop_id" label="店铺 ID">
                    <Input allowClear placeholder="如 shop_001" style={{ width: 140 }} />
                  </Form.Item>
                  <Form.Item name="status" label="状态">
                    <Select allowClear placeholder="全部" style={{ width: 120 }} options={statusOptions} />
                  </Form.Item>
                  <Form.Item name="has_conflict" label="有冲突">
                    <Select
                      allowClear
                      placeholder="全部"
                      style={{ width: 100 }}
                      options={[
                        { label: '是', value: 'true' },
                        { label: '否', value: 'false' },
                      ]}
                    />
                  </Form.Item>
                  <Form.Item name="keyword" label="关键词">
                    <Input allowClear placeholder="SKU 编码 / 商品 ID" style={{ width: 180 }} />
                  </Form.Item>
                  <Form.Item>
                    <Button type="primary" htmlType="submit">
                      查询
                    </Button>
                  </Form.Item>
                </Form>

                <Table<SkuMappingVo>
                  rowKey="id"
                  size="small"
                  scroll={{ x: 1800 }}
                  columns={mappingColumns}
                  dataSource={listQuery.data?.items ?? []}
                  pagination={{ ...tablePagination, total: listQuery.data?.total ?? 0 }}
                  onChange={onTableChange}
                />
              </>
            ),
          },
          {
            key: 'pending',
            label: `变更待确认（${pendingQuery.data?.total ?? 0}）`,
            children: (
              <>
                <Alert
                  type="warning"
                  showIcon
                  style={{ marginBottom: 12 }}
                  message="货源端发生变更时，映射转入待确认状态，相关订单会被挂起（不放行、不盲发）"
                  description="请逐条确认：确认变更 / 驳回（置失效）/ 手工指定新货源 SKU。"
                />
                <Table<MappingPendingVo>
                  rowKey="id"
                  size="small"
                  scroll={{ x: 1200 }}
                  columns={pendingColumns}
                  dataSource={pendingQuery.data?.items ?? []}
                  pagination={false}
                />
              </>
            ),
          },
          {
            key: 'conflicts',
            label: `冲突记录（${conflictQuery.data?.total ?? 0}）`,
            children: (
              <Table<MappingConflictVo>
                rowKey="id"
                size="small"
                scroll={{ x: 1200 }}
                columns={conflictColumns}
                dataSource={conflictQuery.data?.items ?? []}
                pagination={false}
              />
            ),
          },
        ]}
      />

      {/* 编辑映射 */}
      <Modal
        title={editRecord ? `编辑映射 #${editRecord.id}` : '编辑映射'}
        open={editRecord !== null}
        onCancel={() => setEditRecord(null)}
        onOk={() => editForm.submit()}
        confirmLoading={updateMutation.isPending}
        okText="保存"
        cancelText="取消"
        destroyOnHidden
      >
        {editRecord ? (
          <>
            <Descriptions size="small" column={2} style={{ marginBottom: 12 }}>
              <Descriptions.Item label="平台">
                <StatusTag enumKey="Platform" value={editRecord.platform} />
              </Descriptions.Item>
              <Descriptions.Item label="店铺 SKU">{editRecord.shop_sku_code}</Descriptions.Item>
              <Descriptions.Item label="1688 商品 ID">{editRecord.source_product_1688_id ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="规格指纹" span={2}>
                <span className="erp-mono">{editRecord.spec_signature ?? '-'}</span>
              </Descriptions.Item>
            </Descriptions>
            <Form
              form={editForm}
              layout="vertical"
              onFinish={(values: MappingEditFormValues) => {
                if (!editRecord) return;
                updateMutation.mutate({ id: editRecord.id, values });
              }}
            >
              <Form.Item name="source_sku_code_1688" label="1688 SKU 编码" rules={[{ required: true }]}>
                <Input />
              </Form.Item>
              <Form.Item
                name="purchase_cost"
                label="采购成本（元）"
                rules={[{ required: true }, { pattern: /^\d+(\.\d{1,2})?$/, message: '格式如 12.50' }]}
              >
                <Input />
              </Form.Item>
              <Form.Item name="status" label="状态" rules={[{ required: true }]}>
                <Select options={statusOptions} />
              </Form.Item>
              <Form.Item name="remark" label="备注">
                <Input.TextArea rows={2} />
              </Form.Item>
            </Form>
          </>
        ) : null}
      </Modal>

      {/* 变更日志 */}
      <Drawer
        width={900}
        title={logsMappingId ? `映射变更日志 · #${logsMappingId}` : '映射变更日志'}
        open={logsMappingId !== null}
        onClose={() => setLogsMappingId(null)}
      >
        <AuditTimeline
          items={(logsQuery.data ?? []).map((log) => ({
            at: log.created_at,
            title: log.change_action,
            description: `${log.field_name}：${log.old_value ?? '-'} → ${log.new_value ?? '-'}`,
            operator: log.operator,
            note: log.reason,
            trace_id: log.trace_id,
          }))}
        />
        {logsQuery.data && logsQuery.data.length > 0 ? (
          <Table<MappingChangeLogVo>
            rowKey="id"
            size="small"
            style={{ marginTop: 16 }}
            columns={logColumns}
            dataSource={logsQuery.data}
            pagination={false}
          />
        ) : null}
      </Drawer>

      {/* 待确认工单处理 */}
      <Modal
        title={pendingRecord ? `处理待确认工单 #${pendingRecord.id}` : '处理待确认工单'}
        open={pendingRecord !== null}
        onCancel={() => setPendingRecord(null)}
        onOk={() => pendingForm.submit()}
        confirmLoading={resolveMutation.isPending}
        okText="提交"
        cancelText="取消"
        destroyOnHidden
      >
        {pendingRecord ? (
          <>
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 12 }}
              message={`该变更影响 ${pendingRecord.affected_order_count} 笔订单`}
              description="确认后映射恢复有效，挂起订单继续履约；驳回则映射置失效，订单需人工处理。"
            />
            <Descriptions size="small" column={1} bordered style={{ marginBottom: 12 }}>
              <Descriptions.Item label="店铺 SKU">{pendingRecord.shop_sku_code}</Descriptions.Item>
              <Descriptions.Item label="货源商品">{pendingRecord.source_product_title}</Descriptions.Item>
              <Descriptions.Item label="变更类型">
                <StatusTag enumKey="MappingChangeType" value={pendingRecord.change_type} />
              </Descriptions.Item>
              <Descriptions.Item label="值变化">
                <span className="erp-mono">
                  {pendingRecord.old_value ?? '-'} → {pendingRecord.new_value ?? '-'}
                </span>
              </Descriptions.Item>
            </Descriptions>
            <Form
              form={pendingForm}
              layout="vertical"
              onFinish={(values: PendingResolveFormValues) => {
                if (!pendingRecord) return;
                resolveMutation.mutate({ id: pendingRecord.id, values });
              }}
            >
              <Form.Item name="action" label="处理动作" rules={[{ required: true }]}>
                <Select options={pendingActionOptions} />
              </Form.Item>
              <Form.Item name="new_source_sku_code_1688" label="新 1688 SKU 编码（手工指定时必填）">
                <Input placeholder="如 1688-SKU-002" />
              </Form.Item>
              <Form.Item name="note" label="备注">
                <Input.TextArea rows={2} />
              </Form.Item>
            </Form>
          </>
        ) : null}
      </Modal>

      {/* 导出 */}
      <Modal
        title="导出映射 CSV"
        open={exportOpen}
        onCancel={() => setExportOpen(false)}
        onOk={() => exportForm.submit()}
        confirmLoading={exportMutation.isPending}
        okText="导出"
        cancelText="取消"
      >
        <Form form={exportForm} layout="vertical" onFinish={(values: ExportFormValues) => exportMutation.mutate(values)}>
          <Form.Item name="adapter_name" label="目标格式" rules={[{ required: true }]}>
            <Select options={exportTargetOptions} />
          </Form.Item>
          <Form.Item name="platform" label="平台（可选）">
            <Select allowClear options={platformOptions} />
          </Form.Item>
        </Form>
      </Modal>

      {/* 推送第三方 */}
      <Modal
        title="推送映射到第三方履约工具"
        open={pushOpen}
        onCancel={() => setPushOpen(false)}
        onOk={() => {
          pushMutation.mutate({ adapter_name: 'miaoshou', all_valid: true });
        }}
        confirmLoading={pushMutation.isPending}
        okText="推送全部有效映射"
        cancelText="取消"
      >
        <Alert
          type="info"
          showIcon
          message="推送为单向导出，第三方不回写本系统映射"
          description="若第三方未开放写入 API，能力降级为 CSV 导出（unverified 能力），需人工导入第三方工具。本系统映射始终是唯一权威源。"
        />
      </Modal>

      {/* 上架前强制校验（与上架提交共用同一套拦截 UI） */}
      <Modal
        title="上架前映射校验"
        open={validateFormOpen}
        onCancel={() => setValidateFormOpen(false)}
        onOk={() => validateForm.submit()}
        confirmLoading={validateMutation.isPending}
        okText="校验"
        cancelText="取消"
      >
        <Form
          form={validateForm}
          layout="vertical"
          onFinish={(values: ValidateFormValues) =>
            validateMutation.mutate({
              source_product_id: Number(values.source_product_id),
              platform: values.platform,
              shop_id: values.shop_id,
              sku_codes: values.sku_codes
                .split(/[\n,\s]+/)
                .map((item) => item.trim())
                .filter(Boolean),
            })
          }
        >
          <Form.Item name="source_product_id" label="货源商品 ID" rules={[{ required: true }]}>
            <Input placeholder="如 123" />
          </Form.Item>
          <Form.Item name="platform" label="平台" rules={[{ required: true }]}>
            <Select options={platformOptions} />
          </Form.Item>
          <Form.Item name="shop_id" label="店铺 ID" rules={[{ required: true }]}>
            <Input placeholder="如 shop_001" />
          </Form.Item>
          <Form.Item
            name="sku_codes"
            label="店铺 SKU 编码列表"
            extra="换行 / 逗号分隔"
            rules={[{ required: true }]}
          >
            <Input.TextArea rows={3} placeholder="SKU-RED-XL" />
          </Form.Item>
        </Form>
      </Modal>

      {/* 校验结果（含无绕过路径提示） */}
      <MappingValidationModal
        open={validateOpen}
        validation={validationResult}
        onClose={() => setValidateOpen(false)}
      />
    </PageContainer>
  );
}
