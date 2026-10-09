import { useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Descriptions,
  Drawer,
  Form,
  Input,
  Modal,
  Result,
  Select,
  Space,
  Steps,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';

import {
  batchCreatePublishTasks,
  cancelPublishTask,
  createPublishTasks,
  fillBackManual,
  getManualFormData,
  getPublishTask,
  listPublishTasks,
  manualPackageUrl,
  precheckPublishTask,
  retryPublishTask,
} from '@/api/publish';
import { asApiError, triggerDownload } from '@/api/client';
import { cancelTask, getTask, retryTask } from '@/api/system';
import type {
  ManualFillBackSkuBody,
  ManualFillBackVo,
  ManualFormDataSkuVo,
  MappingValidationVo,
  PrecheckFailedItemVo,
  PublishTaskVo,
} from '@/api/types';
import PageContainer from '@/components/PageContainer';
import MappingValidationModal from '@/components/MappingValidationModal';
import MockBadge from '@/components/MockBadge';
import MoneyText from '@/components/MoneyText';
import StatusTag from '@/components/StatusTag';
import { formatTime } from '@/components/AuditTimeline';
import { MAPPING_BLOCK_COPY } from '@/constants/enums';
import { useEnumOptions } from '@/hooks/useEnumOptions';
import { usePagination } from '@/hooks/usePagination';

interface PublishFilterValues {
  platform: string;
  shop_id: string;
  status: string;
  mode: string;
  source_product_id: string;
}

interface CreateFormValues {
  source_product_ids: string;
  platform: string;
  shop_id: string;
  mode: 'real' | 'mock' | 'manual';
}

interface FillBackFormValues {
  shop_item_id: string;
  skus_json: string;
}

const DEFAULT_FILTERS: PublishFilterValues = {
  platform: '',
  shop_id: '',
  status: '',
  mode: '',
  source_product_id: '',
};

const FILL_BACK_TEMPLATE = JSON.stringify(
  [
    {
      spec_json: { 颜色: '红色', 尺码: 'XL' },
      shop_sku_code: 'SKU-RED-XL',
      source_sku_code_1688: '1688-A',
      purchase_cost: '12.50',
      sale_price: '29.90',
    },
  ],
  null,
  2,
);

/**
 * P8 上架任务管理。
 * P9 半自动素材包已并入第二个 Tab：素材包下载 / 预填表单一键复制 / 商品 ID 回填 + 校验。
 */
export default function PublishTasks(): JSX.Element {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { filters, params, setFilters, tablePagination, onTableChange, resetFilters } =
    usePagination<PublishFilterValues>(DEFAULT_FILTERS);
  const platformOptions = useEnumOptions('Platform');
  const statusOptions = useEnumOptions('PublishStatus');
  const modeOptions = useEnumOptions('ListingMode');

  const [activeTab, setActiveTab] = useState<string>('tasks');
  const [createOpen, setCreateOpen] = useState<boolean>(false);
  const [validationResult, setValidationResult] = useState<MappingValidationVo | null>(null);
  const [validateOpen, setValidateOpen] = useState<boolean>(false);
  const [detailId, setDetailId] = useState<number | null>(null);
  const [formDataId, setFormDataId] = useState<number | null>(null);
  const [fillBackId, setFillBackId] = useState<number | null>(null);
  /** 回填成功结果：用于展示「查看新映射 / 去填下一批」CTA */
  const [fillBackResult, setFillBackResult] = useState<ManualFillBackVo | null>(null);

  const [createForm] = Form.useForm<CreateFormValues>();
  const [fillBackForm] = Form.useForm<FillBackFormValues>();

  const listQuery = useQuery({
    queryKey: ['publish-tasks', params],
    queryFn: () => listPublishTasks(params),
    enabled: activeTab === 'tasks',
    refetchInterval: 15_000,
  });

  const manualQuery = useQuery({
    queryKey: ['publish-tasks', 'manual'],
    queryFn: () => listPublishTasks({ mode: 'manual', page: 1, page_size: 100 }),
    enabled: activeTab === 'manual',
  });

  const detailQuery = useQuery({
    queryKey: ['publish-tasks', detailId],
    queryFn: () => getPublishTask(detailId as number),
    enabled: detailId !== null,
  });

  const formDataQuery = useQuery({
    queryKey: ['publish-tasks', 'manual', formDataId, 'form-data'],
    queryFn: () => getManualFormData(formDataId as number),
    enabled: formDataId !== null,
  });

  const taskRecordId = detailQuery.data?.task_record_id ?? null;

  /** 异步任务记录（提交长任务返回 202 + task_record_id，前端据此轮询） */
  const taskQuery = useQuery({
    queryKey: ['tasks', taskRecordId],
    queryFn: () => getTask(taskRecordId as number),
    enabled: typeof taskRecordId === 'number',
    refetchInterval: (query) =>
      query.state.data && ['success', 'failed', 'cancelled'].includes(query.state.data.status)
        ? false
        : 5_000,
  });

  const taskRetryMutation = useMutation({
    mutationFn: (id: number) => retryTask(id),
    onSuccess: () => {
      message.success('任务已重新入队');
      void queryClient.invalidateQueries({ queryKey: ['tasks'] });
    },
  });

  const taskCancelMutation = useMutation({
    mutationFn: (id: number) => cancelTask(id),
    onSuccess: () => {
      message.success('任务已取消');
      void queryClient.invalidateQueries({ queryKey: ['tasks'] });
    },
  });

  const createMutation = useMutation({
    mutationFn: (body: {
      source_product_ids: number[];
      platform: string;
      shop_id: string;
      mode: 'real' | 'mock' | 'manual';
    }) => createPublishTasks(body),
    onSuccess: (data) => {
      message.success(`已创建 ${data.task_ids.length} 个上架任务`);
      setCreateOpen(false);
      createForm.resetFields();
      void queryClient.invalidateQueries({ queryKey: ['publish-tasks'] });
    },
  });

  const batchMutation = useMutation({
    mutationFn: (body: {
      source_product_ids: number[];
      platform: string;
      shop_id: string;
      mode: 'real' | 'mock' | 'manual';
    }) => batchCreatePublishTasks(body),
    onSuccess: (data) => {
      message.success(`批量上架已受理：${data.task_ids.length} 个任务`);
      setCreateOpen(false);
      void queryClient.invalidateQueries({ queryKey: ['publish-tasks'] });
    },
  });

  const precheckMutation = useMutation({
    mutationFn: (id: number) => precheckPublishTask(id),
    onSuccess: (data) => {
      if (data.passed) message.success('合规预检通过');
      else message.warning(`预检未通过：${data.failed_items.length} 项`);
      void queryClient.invalidateQueries({ queryKey: ['publish-tasks'] });
    },
  });

  const retryMutation = useMutation({
    mutationFn: (id: number) => retryPublishTask(id),
    onSuccess: () => {
      message.success('已重新入队');
      void queryClient.invalidateQueries({ queryKey: ['publish-tasks'] });
    },
  });

  const cancelMutation = useMutation({
    mutationFn: (id: number) => cancelPublishTask(id),
    onSuccess: () => {
      message.success('已取消');
      void queryClient.invalidateQueries({ queryKey: ['publish-tasks'] });
    },
  });

  const fillBackMutation = useMutation({
    mutationFn: (body: { id: number; shop_item_id: string; skus: ManualFillBackSkuBody[] }) =>
      fillBackManual(body.id, { shop_item_id: body.shop_item_id, skus: body.skus }),
    onSuccess: (data) => {
      message.success(`回填成功，已自动建立 ${data.mapping_ids.length} 条映射`);
      setFillBackId(null);
      setFillBackResult(data);
      fillBackForm.resetFields();
      void queryClient.invalidateQueries({ queryKey: ['publish-tasks'] });
      void queryClient.invalidateQueries({ queryKey: ['sku-mappings'] });
    },
  });

  /** 提交新建：422 / code 4005 时弹出强制校验明细（明确无绕过路径） */
  const submitCreate = async (values: CreateFormValues, batch: boolean): Promise<void> => {
    const sourceProductIds = values.source_product_ids
      .split(/[\n,\s]+/)
      .map((item) => item.trim())
      .filter(Boolean)
      .map((item) => Number(item))
      .filter((item) => Number.isFinite(item));
    if (sourceProductIds.length === 0) {
      message.warning('请填写至少一个货源商品 ID');
      return;
    }
    const body = {
      source_product_ids: sourceProductIds,
      platform: values.platform,
      shop_id: values.shop_id,
      mode: values.mode,
    };
    try {
      if (batch) await batchMutation.mutateAsync(body);
      else await createMutation.mutateAsync(body);
    } catch (error: unknown) {
      const apiError = asApiError(error);
      const data = apiError?.data as MappingValidationVo | null | undefined;
      if (data && typeof data === 'object' && 'blocking' in data) {
        setValidationResult(data);
        setValidateOpen(true);
      }
    }
  };

  const taskColumns = useMemo<ColumnsType<PublishTaskVo>>(
    () => [
      { title: 'ID', dataIndex: 'id', width: 70 },
      {
        title: '货源商品',
        dataIndex: 'source_product_title',
        ellipsis: true,
        render: (value: string, record) => (
          <Typography.Link onClick={() => setDetailId(record.id)}>{value}</Typography.Link>
        ),
      },
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
      {
        title: '模式',
        dataIndex: 'listing_mode',
        width: 90,
        render: (value: string) => <StatusTag enumKey="ListingMode" value={value} />,
      },
      {
        title: '状态',
        dataIndex: 'status',
        width: 120,
        render: (value: string) => <StatusTag enumKey="PublishStatus" value={value} />,
      },
      {
        title: 'Mock',
        dataIndex: 'is_mock',
        width: 80,
        render: (value: boolean) => <MockBadge isMock={value} />,
      },
      { title: '店铺商品 ID', dataIndex: 'shop_item_id', width: 150, className: 'erp-mono' },
      { title: 'SKU 数', dataIndex: 'sku_count', width: 80 },
      {
        title: '预检 / 校验',
        key: 'checks',
        width: 130,
        render: (_value: unknown, record) => (
          <Space size={4}>
            <Tag color={record.precheck_passed ? 'green' : record.precheck_passed === false ? 'red' : 'default'}>
              预检
            </Tag>
            <Tag color={record.validate_passed ? 'green' : record.validate_passed === false ? 'red' : 'default'}>
              校验
            </Tag>
          </Space>
        ),
      },
      { title: '创建时间', dataIndex: 'created_at', width: 170, render: (v: string) => formatTime(v) },
      {
        title: '操作',
        key: 'actions',
        width: 220,
        fixed: 'right',
        render: (_value: unknown, record) => (
          <Space size="small" wrap>
            <Typography.Link onClick={() => setDetailId(record.id)}>详情</Typography.Link>
            <Typography.Link onClick={() => precheckMutation.mutate(record.id)}>预检</Typography.Link>
            <Typography.Link onClick={() => retryMutation.mutate(record.id)}>重试</Typography.Link>
            <Typography.Link onClick={() => cancelMutation.mutate(record.id)}>取消</Typography.Link>
          </Space>
        ),
      },
    ],
    [cancelMutation, precheckMutation, retryMutation],
  );

  const manualColumns = useMemo<ColumnsType<PublishTaskVo>>(
    () => [
      { title: 'ID', dataIndex: 'id', width: 70 },
      { title: '货源商品', dataIndex: 'source_product_title', ellipsis: true },
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
      {
        title: '状态',
        dataIndex: 'status',
        width: 120,
        render: (value: string) => <StatusTag enumKey="PublishStatus" value={value} />,
      },
      { title: 'SKU 数', dataIndex: 'sku_count', width: 80 },
      { title: '素材包路径', dataIndex: 'package_path', ellipsis: true, className: 'erp-mono' },
      {
        title: '操作',
        key: 'actions',
        width: 280,
        render: (_value: unknown, record) => (
          <Space size="small" wrap>
            {/* 素材包是 application/zip 二进制流（不是统一 JSON 信封），用 <a> 直接下载，不走 res.json() */}
            <Typography.Link
              onClick={() =>
                triggerDownload(
                  manualPackageUrl(record.id),
                  `manual-package-task-${record.id}.zip`,
                )
              }
            >
              下载素材包
            </Typography.Link>
            <Typography.Link onClick={() => setFormDataId(record.id)}>预填表单</Typography.Link>
            <Typography.Link
              onClick={() => {
                setFillBackId(record.id);
                fillBackForm.setFieldsValue({ skus_json: FILL_BACK_TEMPLATE });
              }}
            >
              ID 回填
            </Typography.Link>
          </Space>
        ),
      },
    ],
    [fillBackForm],
  );

  const precheckColumns = useMemo<ColumnsType<PrecheckFailedItemVo>>(
    () => [
      { title: '检查项', dataIndex: 'item', width: 160 },
      { title: '失败原因', dataIndex: 'reason' },
      { title: '处理建议', dataIndex: 'suggestion' },
    ],
    [],
  );

  const detail = detailQuery.data;
  const formData = formDataQuery.data;

  return (
    <PageContainer
      title="上架任务管理"
      subTitle="上架必经「合规预检 → 映射强制校验 → 适配器发布」三步；校验不通过无任何绕过路径"
      loading={listQuery.isLoading && activeTab === 'tasks'}
      alert={
        <Alert
          type="error"
          showIcon
          message="红线：无完整映射禁止上架"
          description={
            <>
              提交上架若返回 422 / code 4005（映射缺失、P0 级冲突或 AI 素材未审核通过），系统会弹出{' '}
              <b>MappingValidationVo</b> 明细并硬拦截。
              {MAPPING_BLOCK_COPY.noBypass}
            </>
          }
        />
      }
      extra={
        <Space>
          <Button type="primary" onClick={() => setCreateOpen(true)}>
            新建上架任务
          </Button>
          <Button onClick={resetFilters}>重置筛选</Button>
        </Space>
      }
    >
      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={[
          {
            key: 'tasks',
            label: '上架任务',
            children: (
              <>
                <Form
                  layout="inline"
                  style={{ marginBottom: 12, rowGap: 8 }}
                  initialValues={filters}
                  onFinish={(values: PublishFilterValues) => setFilters(values)}
                >
                  <Form.Item name="platform" label="平台">
                    <Select allowClear placeholder="全部" style={{ width: 120 }} options={platformOptions} />
                  </Form.Item>
                  <Form.Item name="shop_id" label="店铺 ID">
                    <Input allowClear placeholder="如 shop_001" style={{ width: 140 }} />
                  </Form.Item>
                  <Form.Item name="status" label="状态">
                    <Select allowClear placeholder="全部" style={{ width: 150 }} options={statusOptions} />
                  </Form.Item>
                  <Form.Item name="mode" label="模式">
                    <Select allowClear placeholder="全部" style={{ width: 120 }} options={modeOptions} />
                  </Form.Item>
                  <Form.Item name="source_product_id" label="货源商品 ID">
                    <Input allowClear placeholder="如 123" style={{ width: 140 }} />
                  </Form.Item>
                  <Form.Item>
                    <Button type="primary" htmlType="submit">
                      查询
                    </Button>
                  </Form.Item>
                </Form>

                <Table<PublishTaskVo>
                  rowKey="id"
                  size="small"
                  scroll={{ x: 1600 }}
                  columns={taskColumns}
                  dataSource={listQuery.data?.items ?? []}
                  pagination={{ ...tablePagination, total: listQuery.data?.total ?? 0 }}
                  onChange={onTableChange}
                />
              </>
            ),
          },
          {
            key: 'manual',
            label: '半自动素材包',
            children: (
              <>
                <Alert
                  type="warning"
                  showIcon
                  style={{ marginBottom: 12 }}
                  message="★ 半自动上架是主路径（不是兜底）"
                  description="店铺主体为个体户 / 个人身份证店，大概率拿不到三大平台商品发布 API，因此人工发布 + 回填是主要工作方式：系统生成素材包 ZIP + 预填表单 → 人工在平台后台发布 → 回填商品 ID 与售价 → 自动建立映射。售价必填，否则无法做成本倒挂检测。"
                />
                <Table<PublishTaskVo>
                  rowKey="id"
                  size="small"
                  scroll={{ x: 1400 }}
                  columns={manualColumns}
                  dataSource={manualQuery.data?.items ?? []}
                  pagination={false}
                />
              </>
            ),
          },
        ]}
      />

      {/* 新建上架任务 */}
      <Modal
        title="新建上架任务"
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={() => createForm.submit()}
        confirmLoading={createMutation.isPending || batchMutation.isPending}
        okText="提交"
        cancelText="取消"
        destroyOnHidden
      >
        <Form
          form={createForm}
          layout="vertical"
          initialValues={{ mode: 'mock' }}
          onFinish={(values: CreateFormValues) => {
            void submitCreate(values, false);
          }}
        >
          <Form.Item
            name="source_product_ids"
            label="货源商品 ID"
            extra="换行 / 逗号分隔，单次建议 ≤ 50 个"
            rules={[{ required: true, message: '请填写货源商品 ID' }]}
          >
            <Input.TextArea rows={3} placeholder="123,456" />
          </Form.Item>
          <Form.Item name="platform" label="目标平台" rules={[{ required: true }]}>
            <Select options={platformOptions} />
          </Form.Item>
          <Form.Item name="shop_id" label="店铺 ID" rules={[{ required: true }]}>
            <Input placeholder="shop_001" />
          </Form.Item>
          <Form.Item
            name="mode"
            label="上架模式"
            extra="真实 API 需平台资质；MVP 默认 Mock（产出数据带 Mock 标记，不参与真实履约）"
            rules={[{ required: true }]}
          >
            <Select options={modeOptions} />
          </Form.Item>
        </Form>
        <Space>
          <Button
            onClick={() => {
              const values = createForm.getFieldsValue();
              void submitCreate(values, true);
            }}
            loading={batchMutation.isPending}
          >
            批量提交（走 /publish-tasks/batch）
          </Button>
        </Space>
      </Modal>

      {/* 强制校验拦截明细 */}
      <MappingValidationModal
        open={validateOpen}
        validation={validationResult}
        onClose={() => setValidateOpen(false)}
      />

      {/* 任务详情 */}
      <Drawer
        width={860}
        title={detail ? `上架任务 #${detail.id}` : '上架任务详情'}
        open={detailId !== null}
        onClose={() => setDetailId(null)}
      >
        {detail ? (
          <>
            <Descriptions bordered size="small" column={2} style={{ marginBottom: 16 }}>
              <Descriptions.Item label="状态">
                <StatusTag enumKey="PublishStatus" value={detail.status} />
              </Descriptions.Item>
              <Descriptions.Item label="模式">
                <StatusTag enumKey="ListingMode" value={detail.listing_mode} />
              </Descriptions.Item>
              <Descriptions.Item label="平台">
                <StatusTag enumKey="Platform" value={detail.platform} />
              </Descriptions.Item>
              <Descriptions.Item label="店铺 ID">{detail.shop_id}</Descriptions.Item>
              <Descriptions.Item label="AI 产出 ID">{detail.ai_task_result_id ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="店铺商品 ID">
                <span className="erp-mono">{detail.shop_item_id ?? '-'}</span>
              </Descriptions.Item>
              <Descriptions.Item label="平台错误码">{detail.platform_error_code ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="创建人">{detail.created_by ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="处理建议" span={2}>
                {detail.error_advice ?? '-'}
              </Descriptions.Item>
              <Descriptions.Item label="异步任务" span={2}>
                {typeof detail.task_record_id === 'number' ? (
                  <Space wrap>
                    <span className="erp-mono">task_record #{detail.task_record_id}</span>
                    <StatusTag enumKey="TaskStatus" value={taskQuery.data?.status ?? 'pending'} />
                    {taskQuery.data?.error_message ? (
                      <Typography.Text type="danger">{taskQuery.data.error_message}</Typography.Text>
                    ) : null}
                    <Button size="small" onClick={() => taskRetryMutation.mutate(detail.task_record_id as number)}>
                      重试任务
                    </Button>
                    <Button
                      size="small"
                      danger
                      onClick={() => taskCancelMutation.mutate(detail.task_record_id as number)}
                    >
                      取消任务
                    </Button>
                  </Space>
                ) : (
                  '-'
                )}
              </Descriptions.Item>
            </Descriptions>

            {detail.validate_result ? (
              <>
                <Typography.Title level={5}>映射校验结果</Typography.Title>
                <Alert
                  type={detail.validate_result.blocking ? 'error' : 'success'}
                  showIcon
                  style={{ marginBottom: 12 }}
                  message={detail.validate_result.blocked_reason ?? '校验通过'}
                  description={detail.validate_result.blocking ? MAPPING_BLOCK_COPY.noBypass : undefined}
                />
                <Table<MappingValidationVo['missing_mappings'][number]>
                  rowKey="shop_sku_code"
                  size="small"
                  pagination={false}
                  dataSource={detail.validate_result.missing_mappings}
                  columns={[
                    { title: '缺失的店铺 SKU', dataIndex: 'shop_sku_code', className: 'erp-mono' },
                    { title: '原因', dataIndex: 'reason' },
                  ]}
                />
              </>
            ) : null}

            {detail.precheck_result ? (
              <>
                <Typography.Title level={5} style={{ marginTop: 16 }}>
                  合规预检结果
                </Typography.Title>
                <Table<PrecheckFailedItemVo>
                  rowKey="item"
                  size="small"
                  pagination={false}
                  columns={precheckColumns}
                  dataSource={detail.precheck_result.failed_items}
                  locale={{ emptyText: '预检通过' }}
                />
              </>
            ) : null}
          </>
        ) : null}
      </Drawer>

      {/* 半自动预填表单 */}
      <Drawer
        width={760}
        title={formDataId ? `预填表单 · 任务 #${formDataId}` : '预填表单'}
        open={formDataId !== null}
        onClose={() => setFormDataId(null)}
        extra={
          formData ? (
            <Button
              type="primary"
              onClick={() => {
                void navigator.clipboard.writeText(formData.copy_text);
                message.success('已复制预填文本');
              }}
            >
              一键复制
            </Button>
          ) : null
        }
      >
        {formData ? (
          <>
            <Descriptions bordered size="small" column={1}>
              <Descriptions.Item label="平台 / 店铺">
                <Space size={4}>
                  <StatusTag enumKey="Platform" value={formData.platform} />
                  <span className="erp-mono">{formData.shop_id}</span>
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="标题">{formData.title}</Descriptions.Item>
              <Descriptions.Item label="卖点">
                {formData.selling_points.length > 0 ? (
                  <Space size={4} wrap>
                    {formData.selling_points.map((point, index) => (
                      <Tag key={`${point}-${index}`}>{point}</Tag>
                    ))}
                  </Space>
                ) : (
                  '-'
                )}
              </Descriptions.Item>
              <Descriptions.Item label="类目 ID">
                {formData.category_id ? formData.category_id : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="素材">
                主图 {formData.main_image_count} 张 · 详情图 {formData.detail_image_count} 张
              </Descriptions.Item>
              <Descriptions.Item label="属性">
                {formData.attributes ? (
                  <pre style={{ margin: 0, whiteSpace: 'pre-wrap' }}>
                    {JSON.stringify(formData.attributes, null, 2)}
                  </pre>
                ) : (
                  '-'
                )}
              </Descriptions.Item>
            </Descriptions>
            <Typography.Title level={5} style={{ marginTop: 16 }}>
              SKU 预填
            </Typography.Title>
            <Table<ManualFormDataSkuVo>
              rowKey={(record) => `${record.seq}-${record.source_sku_code_1688 ?? ''}`}
              size="small"
              pagination={false}
              dataSource={formData.sku_list}
              columns={[
                { title: '#', dataIndex: 'seq', width: 50 },
                {
                  title: '规格',
                  dataIndex: 'spec_text',
                  render: (value: string) => value || '-',
                },
                {
                  title: '1688 SKU',
                  dataIndex: 'source_sku_code_1688',
                  width: 160,
                  className: 'erp-mono',
                  render: (value: string | null) => value ?? '-',
                },
                {
                  title: '建议售价',
                  dataIndex: 'sale_price',
                  width: 120,
                  render: (value: string) => <MoneyText value={value} />,
                },
                {
                  title: '采购成本',
                  dataIndex: 'purchase_cost',
                  width: 120,
                  render: (value: string) => <MoneyText value={value} />,
                },
                { title: '库存', dataIndex: 'stock_qty', width: 90 },
              ]}
            />
          </>
        ) : null}
      </Drawer>

      {/* 商品 ID 回填 */}
      <Modal
        title={fillBackId ? `商品 ID 回填 · 任务 #${fillBackId}` : '商品 ID 回填'}
        open={fillBackId !== null}
        onCancel={() => setFillBackId(null)}
        onOk={() => fillBackForm.submit()}
        confirmLoading={fillBackMutation.isPending}
        okText="校验并回填"
        cancelText="取消"
        width={860}
        destroyOnHidden
      >
        {/* 半自动为主路径：给出清晰的人工操作引导 */}
        <Steps
          size="small"
          direction="horizontal"
          style={{ marginBottom: 16 }}
          current={2}
          items={[
            { title: '下载素材包', description: 'ZIP（图片 + 预填表单）' },
            { title: '平台后台发布', description: '人工在店铺后台上新' },
            { title: '粘贴商品 ID', description: '逐 SKU 填编码与售价' },
            { title: '提交回填', description: '自动建立映射' },
          ]}
        />
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="售价必填"
          description="sale_price 是成本倒挂（cost_underwater）检测的前置数据；缺失会导致无法判断亏损铺货。前端已做校验，后端也会返回 422。"
        />
        <Form
          form={fillBackForm}
          layout="vertical"
          onFinish={(values: FillBackFormValues) => {
            if (fillBackId === null) return;
            let parsed: ManualFillBackSkuBody[] = [];
            try {
              parsed = JSON.parse(values.skus_json) as ManualFillBackSkuBody[];
            } catch {
              message.error('SKU JSON 格式错误');
              return;
            }
            if (!Array.isArray(parsed) || parsed.length === 0) {
              message.error('请至少填写 1 个 SKU');
              return;
            }
            const invalid = parsed.find(
              (item) =>
                !item.shop_sku_code ||
                !item.source_sku_code_1688 ||
                !item.purchase_cost ||
                !item.sale_price,
            );
            if (invalid) {
              message.error(
                '每条 SKU 必须包含 shop_sku_code / source_sku_code_1688 / purchase_cost / sale_price（售价必填）',
              );
              return;
            }
            const badPrice = parsed.find(
              (item) => !/^\d+(\.\d{1,2})?$/.test(String(item.sale_price ?? '')),
            );
            if (badPrice) {
              message.error('售价格式不正确（如 29.90），且必须大于 0');
              return;
            }
            const underwater = parsed.find(
              (item) => Number(item.sale_price) <= Number(item.purchase_cost),
            );
            if (underwater) {
              message.warning('存在售价 ≤ 采购成本的 SKU（成本倒挂），提交后会被标记为 P1 提示');
            }
            fillBackMutation.mutate({
              id: fillBackId,
              shop_item_id: values.shop_item_id,
              skus: parsed,
            });
          }}
        >
          <Form.Item name="shop_item_id" label="平台商品 ID" rules={[{ required: true }]}>
            <Input placeholder="如 6789012345" />
          </Form.Item>
          <Form.Item
            name="skus_json"
            label="SKU 列表（JSON，含必填售价 sale_price）"
            extra="每条 SKU：spec_json / shop_sku_code / source_sku_code_1688 / purchase_cost / sale_price；回填成功后系统自动建立 SKU 映射；ID 重复返回 409"
            rules={[{ required: true }]}
          >
            <Input.TextArea rows={12} />
          </Form.Item>
        </Form>
      </Modal>

      {/* 回填成功 CTA */}
      <Modal
        title="回填成功"
        open={fillBackResult !== null}
        onCancel={() => setFillBackResult(null)}
        footer={null}
        destroyOnHidden
      >
        <Result
          status="success"
          title={`已建立 ${fillBackResult?.mapping_ids.length ?? 0} 条 SKU 映射`}
          subTitle={`上架任务 #${fillBackResult?.publish_task_id ?? '-'} 的商品 ID 与售价已回填，映射立即可用于订单匹配。`}
          extra={[
            <Button
              key="mapping"
              type="primary"
              onClick={() => {
                setFillBackResult(null);
                navigate('/sku-mappings');
              }}
            >
              查看新映射
            </Button>,
            <Button
              key="next"
              onClick={() => {
                const next = (manualQuery.data?.items ?? []).find(
                  (item) => !item.shop_item_id && item.id !== fillBackResult?.publish_task_id,
                );
                setFillBackResult(null);
                if (next) {
                  setFillBackId(next.id);
                  fillBackForm.setFieldsValue({ skus_json: FILL_BACK_TEMPLATE });
                } else {
                  message.info('没有更多待回填的半自动任务');
                }
              }}
            >
              去填下一批
            </Button>,
          ]}
        />
      </Modal>
    </PageContainer>
  );
}
