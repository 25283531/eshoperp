import { useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Input,
  Modal,
  Row,
  Segmented,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { SyncOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  getOrder,
  getOrderBoard,
  getOrderTimeline,
  listOrderExceptions,
  listOrders,
  listPurchaseOrders,
  matchOrder,
  setPurchaseOrderTracking,
  submitOrderAction,
  syncOrders,
  writebackPurchaseOrder,
} from '@/api/ops';
import type { OrderExceptionVo, OrderVo, OrderTimelineEventVo, PurchaseOrderVo } from '@/api/types';
import PageContainer from '@/components/PageContainer';
import MoneyText from '@/components/MoneyText';
import MockBadge from '@/components/MockBadge';
import StatusTag from '@/components/StatusTag';
import AuditTimeline, { formatTime } from '@/components/AuditTimeline';
import { useEnumOptions } from '@/hooks/useEnumOptions';
import { usePagination } from '@/hooks/usePagination';

interface OrderFilterValues {
  platform: string;
  shop_id: string;
  fulfillment_status: string;
  adapter_name: string;
  keyword: string;
}

interface ActionFormValues {
  action: 'retry' | 'switch_source' | 'refund' | 'ignore';
  source_sku_code_1688: string;
  reason: string;
}

interface MatchFormValues {
  sku_mapping_id: string;
  source_sku_id: string;
  create_mapping: boolean;
}

interface TrackingFormValues {
  logistics_company: string;
  tracking_no: string;
}

const DEFAULT_FILTERS: OrderFilterValues = {
  platform: '',
  shop_id: '',
  fulfillment_status: '',
  adapter_name: '',
  keyword: '',
};

/**
 * 采购状态标签。
 * ★ 本地兜底（local_csv）通道下状态是 `manual_pending`，语义是「待人工在 1688 下单」，
 *   绝不能美化成「已下单」——这里显式用金色「待人工处理」表达能力边界。
 */
function PurchaseStatusTag(props: { value?: string | null }): JSX.Element {
  const { value } = props;
  if (!value) return <Tag>-</Tag>;
  if (value === 'manual_pending') {
    return (
      <Tooltip title="当前履约渠道不支持自动下单，需人工在 1688 完成下单后再录入物流单号">
        <Tag color="gold">待人工处理</Tag>
      </Tooltip>
    );
  }
  return <StatusTag enumKey="PurchaseStatus" value={value} />;
}

/**
 * P11 订单履约看板。
 * P12 异常订单处理台已并入第二个 Tab：异常分类 + 重试 / 换货源 / 退款 / 忽略 + 处理时间线。
 */
export default function Orders(): JSX.Element {
  const queryClient = useQueryClient();
  const { filters, params, setFilters, tablePagination, onTableChange, resetFilters } =
    usePagination<OrderFilterValues>(DEFAULT_FILTERS);
  const platformOptions = useEnumOptions('Platform');
  const statusOptions = useEnumOptions('OrderStatus');
  const adapterOptions = useEnumOptions('AdapterName');
  const actionOptions = useEnumOptions('OrderAction');

  const [activeTab, setActiveTab] = useState<string>('all');
  const [detailId, setDetailId] = useState<number | null>(null);
  const [actionOrderId, setActionOrderId] = useState<number | null>(null);
  const [matchOrderId, setMatchOrderId] = useState<number | null>(null);
  const [timelineOrderId, setTimelineOrderId] = useState<number | null>(null);
  const [trackingPurchaseId, setTrackingPurchaseId] = useState<number | null>(null);
  const [viewMode, setViewMode] = useState<'list' | 'board'>('list');

  const [actionForm] = Form.useForm<ActionFormValues>();
  const [matchForm] = Form.useForm<MatchFormValues>();
  const [trackingForm] = Form.useForm<TrackingFormValues>();

  const listQuery = useQuery({
    queryKey: ['orders', params],
    queryFn: () => listOrders(params),
    enabled: activeTab === 'all',
    refetchInterval: 30_000,
  });

  const exceptionQuery = useQuery({
    queryKey: ['orders', 'exceptions'],
    queryFn: () => listOrderExceptions({ page: 1, page_size: 100 }),
    enabled: activeTab === 'exceptions',
    refetchInterval: 30_000,
  });

  const detailQuery = useQuery({
    queryKey: ['orders', detailId],
    queryFn: () => getOrder(detailId as number),
    enabled: detailId !== null,
  });

  const timelineQuery = useQuery({
    queryKey: ['orders', timelineOrderId, 'timeline'],
    queryFn: () => getOrderTimeline(timelineOrderId as number),
    enabled: timelineOrderId !== null,
  });

  const boardQuery = useQuery({
    queryKey: ['orders', 'board', params],
    queryFn: () => getOrderBoard(params),
    enabled: activeTab === 'all' && viewMode === 'board',
    refetchInterval: 30_000,
  });

  const purchaseQuery = useQuery({
    queryKey: ['purchase-orders', activeTab],
    queryFn: () => listPurchaseOrders({ page: 1, page_size: 100 }),
    enabled: activeTab === 'purchase',
    refetchInterval: 30_000,
  });

  const syncMutation = useMutation({
    mutationFn: () => syncOrders({ force: false }),
    onSuccess: (data) => {
      message.success(
        `同步已受理：拉取 ${data.fetched} 笔，新增 ${data.new} 笔，重复 ${data.duplicated} 笔`,
      );
      void queryClient.invalidateQueries({ queryKey: ['orders'] });
    },
  });

  const actionMutation = useMutation({
    mutationFn: (body: { id: number; values: ActionFormValues }) =>
      submitOrderAction(body.id, {
        action: body.values.action,
        payload: {
          source_sku_code_1688: body.values.source_sku_code_1688 || undefined,
          reason: body.values.reason || undefined,
        },
      }),
    onSuccess: (data) => {
      message.success(data.message);
      setActionOrderId(null);
      actionForm.resetFields();
      void queryClient.invalidateQueries({ queryKey: ['orders'] });
    },
  });

  const matchMutation = useMutation({
    mutationFn: (body: { id: number; values: MatchFormValues }) =>
      matchOrder(body.id, {
        sku_mapping_id: body.values.sku_mapping_id ? Number(body.values.sku_mapping_id) : undefined,
        source_sku_id: body.values.source_sku_id ? Number(body.values.source_sku_id) : undefined,
        create_mapping: body.values.create_mapping,
      }),
    onSuccess: (data) => {
      message.success(`匹配成功：mapping #${data.mapping_id ?? '-'}`);
      setMatchOrderId(null);
      matchForm.resetFields();
      void queryClient.invalidateQueries({ queryKey: ['orders'] });
    },
  });

  const writebackMutation = useMutation({
    mutationFn: (id: number) => writebackPurchaseOrder(id),
    onSuccess: (data) => {
      if (data.writeback_status === 'success') message.success('物流回填成功');
      else message.warning(`回填未成功（当前状态 ${data.writeback_status}），已重试 ${data.retry} 次`);
      void queryClient.invalidateQueries({ queryKey: ['purchase-orders'] });
    },
  });

  const trackingMutation = useMutation({
    mutationFn: (body: { id: number; values: TrackingFormValues }) =>
      setPurchaseOrderTracking(body.id, {
        logistics_company: body.values.logistics_company,
        tracking_no: body.values.tracking_no,
      }),
    onSuccess: () => {
      message.success('物流单号已录入（本地兜底通道）');
      setTrackingPurchaseId(null);
      trackingForm.resetFields();
      void queryClient.invalidateQueries({ queryKey: ['purchase-orders'] });
    },
  });

  const orderColumns = useMemo<ColumnsType<OrderVo>>(
    () => [
      { title: 'ID', dataIndex: 'id', width: 70 },
      { title: '平台订单号', dataIndex: 'platform_order_no', width: 180, className: 'erp-mono' },
      {
        title: '平台 / 店铺',
        key: 'platform',
        width: 140,
        render: (_value: unknown, record) => (
          <Space size={4}>
            <StatusTag enumKey="Platform" value={record.platform} />
            <span className="erp-mono">{record.shop_id}</span>
          </Space>
        ),
      },
      { title: '买家', dataIndex: 'buyer_masked', width: 140 },
      {
        title: '金额',
        dataIndex: 'total_amount',
        width: 110,
        render: (value: string) => <MoneyText value={value} strong />,
      },
      {
        title: '履约状态',
        dataIndex: 'fulfillment_status',
        width: 150,
        render: (value: string) => <StatusTag enumKey="OrderStatus" value={value} />,
      },
      {
        title: '履约渠道',
        dataIndex: 'adapter_name',
        width: 110,
        render: (value: string) => <StatusTag enumKey="AdapterName" value={value} />,
      },
      {
        title: 'Mock',
        dataIndex: 'is_mock',
        width: 80,
        render: (value: boolean) => <MockBadge isMock={value} />,
      },
      { title: '付款时间', dataIndex: 'paid_at', width: 170, render: (v: string | null) => formatTime(v) },
      {
        title: '操作',
        key: 'actions',
        width: 240,
        fixed: 'right',
        render: (_value: unknown, record) => (
          <Space size="small" wrap>
            <Typography.Link onClick={() => setDetailId(record.id)}>详情</Typography.Link>
            <Typography.Link
              onClick={() => {
                setActionOrderId(record.id);
                actionForm.setFieldsValue({ action: 'retry' });
              }}
            >
              处置
            </Typography.Link>
            <Typography.Link
              onClick={() => {
                setMatchOrderId(record.id);
                matchForm.setFieldsValue({ create_mapping: true });
              }}
            >
              手工匹配
            </Typography.Link>
            <Typography.Link onClick={() => setTimelineOrderId(record.id)}>时间线</Typography.Link>
          </Space>
        ),
      },
    ],
    [actionForm, matchForm],
  );

  const exceptionColumns = useMemo<ColumnsType<OrderExceptionVo>>(
    () => [
      { title: 'ID', dataIndex: 'id', width: 70 },
      { title: '平台订单号', dataIndex: 'platform_order_no', width: 180, className: 'erp-mono' },
      {
        title: '异常类型',
        dataIndex: 'exception_type',
        width: 150,
        render: (value: string | null) => <StatusTag enumKey="ExceptionType" value={value} />,
      },
      {
        title: '履约状态',
        dataIndex: 'fulfillment_status',
        width: 150,
        render: (value: string) => <StatusTag enumKey="OrderStatus" value={value} />,
      },
      {
        title: '履约渠道',
        dataIndex: 'adapter_name',
        width: 110,
        render: (value: string) => <StatusTag enumKey="AdapterName" value={value} />,
      },
      { title: '说明', dataIndex: 'exception_note', ellipsis: true },
      {
        title: '是否已处理',
        dataIndex: 'handled',
        width: 110,
        render: (value: boolean) => (value ? <Tag color="green">已处理</Tag> : <Tag color="red">待处理</Tag>),
      },
      { title: '检测时间', dataIndex: 'detected_at', width: 170, render: (v: string) => formatTime(v) },
      {
        title: '操作',
        key: 'actions',
        width: 180,
        render: (_value: unknown, record) => (
          <Space size="small">
            <Typography.Link
              onClick={() => {
                setActionOrderId(record.order_id);
                actionForm.setFieldsValue({ action: 'retry' });
              }}
            >
              处理
            </Typography.Link>
            <Typography.Link onClick={() => setTimelineOrderId(record.order_id)}>时间线</Typography.Link>
          </Space>
        ),
      },
    ],
    [actionForm],
  );

  const purchaseColumns = useMemo<ColumnsType<PurchaseOrderVo>>(
    () => [
      { title: 'ID', dataIndex: 'id', width: 70 },
      {
        title: '采购单号',
        dataIndex: 'purchase_order_no',
        width: 180,
        render: (v?: string | null) => (v ? <span className="erp-mono">{v}</span> : '-'),
      },
      { title: '关联订单', dataIndex: 'order_id', width: 90, render: (v: number) => `#${v}` },
      { title: '供应商', dataIndex: 'supplier_id', width: 90, render: (v?: number | null) => (v ? `#${v}` : '-') },
      {
        title: '金额',
        dataIndex: 'amount',
        width: 110,
        render: (value: string | null) => <MoneyText value={value} />,
      },
      {
        title: '采购状态',
        dataIndex: 'purchase_status',
        width: 130,
        render: (value: string) => <PurchaseStatusTag value={value} />,
      },
      {
        title: '回填状态',
        dataIndex: 'writeback_status',
        width: 110,
        render: (v?: string | null) => <StatusTag enumKey="WritebackStatus" value={v} />,
      },
      { title: '回填重试', dataIndex: 'writeback_retry', width: 90 },
      { title: '物流', dataIndex: 'logistics_company', width: 110, render: (v?: string | null) => v ?? '-' },
      {
        title: '运单号',
        dataIndex: 'tracking_no',
        width: 160,
        render: (v?: string | null) => (v ? <span className="erp-mono">{v}</span> : '-'),
      },
      { title: '发货时间', dataIndex: 'shipped_at', width: 170, render: (v?: string | null) => formatTime(v) },
    ],
    [],
  );

  /** 采购单 Tab：增加回填重试与手工录入物流单号操作（本地兜底通道） */
  const purchaseTabColumns = useMemo<ColumnsType<PurchaseOrderVo>>(
    () => [
      ...purchaseColumns,
      {
        title: '操作',
        key: 'actions',
        width: 190,
        fixed: 'right',
        render: (_value: unknown, record) => (
          <Space size="small" wrap>
            <Typography.Link onClick={() => writebackMutation.mutate(record.id)}>回填重试</Typography.Link>
            <Typography.Link
              onClick={() => {
                setTrackingPurchaseId(record.id);
                trackingForm.setFieldsValue({
                  logistics_company: record.logistics_company ?? '',
                  tracking_no: record.tracking_no ?? '',
                });
              }}
            >
              录入物流单号
            </Typography.Link>
          </Space>
        ),
      },
    ],
    [purchaseColumns, trackingForm, writebackMutation],
  );

  const detail = detailQuery.data;

  return (
    <PageContainer
      title="订单履约看板"
      subTitle="第三方只执行履约，ERP 监督；映射缺失或待确认时订单挂起，绝不盲发（ORD-P0-03）"
      loading={listQuery.isLoading && activeTab === 'all'}
      extra={
        <Space>
          <Button
            icon={<SyncOutlined />}
            loading={syncMutation.isPending}
            onClick={() => syncMutation.mutate()}
          >
            同步订单
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
            key: 'all',
            label: '全部订单',
            children: (
              <>
                <Space style={{ marginBottom: 12 }}>
                  <Segmented
                    value={viewMode}
                    onChange={(value) => setViewMode(value as 'list' | 'board')}
                    options={[
                      { label: '列表视图', value: 'list' },
                      { label: '看板视图', value: 'board' },
                    ]}
                  />
                </Space>

                <Form
                  layout="inline"
                  style={{ marginBottom: 12, rowGap: 8 }}
                  initialValues={filters}
                  onFinish={(values: OrderFilterValues) => setFilters(values)}
                >
                  <Form.Item name="platform" label="平台">
                    <Select allowClear placeholder="全部" style={{ width: 120 }} options={platformOptions} />
                  </Form.Item>
                  <Form.Item name="shop_id" label="店铺 ID">
                    <Input allowClear placeholder="如 shop_001" style={{ width: 140 }} />
                  </Form.Item>
                  <Form.Item name="fulfillment_status" label="履约状态">
                    <Select allowClear placeholder="全部" style={{ width: 160 }} options={statusOptions} />
                  </Form.Item>
                  <Form.Item name="adapter_name" label="渠道">
                    <Select allowClear placeholder="全部" style={{ width: 130 }} options={adapterOptions} />
                  </Form.Item>
                  <Form.Item name="keyword" label="关键词">
                    <Input allowClear placeholder="订单号 / 买家" style={{ width: 160 }} />
                  </Form.Item>
                  <Form.Item>
                    <Button type="primary" htmlType="submit">
                      查询
                    </Button>
                  </Form.Item>
                </Form>

                {viewMode === 'board' ? (
                  <Row gutter={[12, 12]}>
                    {(boardQuery.data?.columns ?? []).map((column) => (
                      <Col key={column.status} xs={24} sm={12} lg={6} xl={4}>
                        <Card
                          size="small"
                          title={
                            <Space size={4}>
                              <StatusTag enumKey="OrderStatus" value={column.status} />
                              <span>{column.count}</span>
                            </Space>
                          }
                        >
                          {column.orders.slice(0, 5).map((order) => (
                            <div key={order.id} style={{ marginBottom: 6 }}>
                              <Typography.Link onClick={() => setDetailId(order.id)}>
                                <span className="erp-mono">{order.platform_order_no}</span>
                              </Typography.Link>
                            </div>
                          ))}
                          {column.orders.length === 0 ? (
                            <Typography.Text type="secondary">无订单</Typography.Text>
                          ) : null}
                          {column.orders.length > 5 ? (
                            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                              仅显示前 5 条，共 {column.count} 条
                            </Typography.Text>
                          ) : null}
                        </Card>
                      </Col>
                    ))}
                    {boardQuery.data && boardQuery.data.columns.length === 0 ? (
                      <Col span={24}>
                        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="后端未返回看板数据" />
                      </Col>
                    ) : null}
                  </Row>
                ) : (
                <Table<OrderVo>
                  rowKey="id"
                  size="small"
                  scroll={{ x: 1700 }}
                  columns={orderColumns}
                  dataSource={listQuery.data?.items ?? []}
                  pagination={{ ...tablePagination, total: listQuery.data?.total ?? 0 }}
                  onChange={onTableChange}
                />
                )}
              </>
            ),
          },
          {
            key: 'exceptions',
            label: `异常订单（${exceptionQuery.data?.total ?? 0}）`,
            children: (
              <>
                <Alert
                  type="error"
                  showIcon
                  style={{ marginBottom: 12 }}
                  message="异常订单不会自动放行"
                  description="匹配失败 / 下单失败 / 缺货 / 回填失败四类异常需人工介入：重试、换货源、退款或忽略。"
                />
                <Table<OrderExceptionVo>
                  rowKey="id"
                  size="small"
                  scroll={{ x: 1500 }}
                  columns={exceptionColumns}
                  dataSource={exceptionQuery.data?.items ?? []}
                  pagination={false}
                />
              </>
            ),
          },
          {
            key: 'purchase',
            label: '采购单',
            children: (
              <>
                <Alert
                  type="warning"
                  showIcon
                  style={{ marginBottom: 12 }}
                  message="本地兜底通道的能力边界（如实展示，不做美化）"
                  description={
                    <Space direction="vertical" size={2}>
                      <span>
                        ① 采购状态为 <b>manual_pending</b>＝<b>待人工在 1688 下单</b>，不是「已下单」；
                      </span>
                      <span>② 物流单号无自动来源，必须人工录入后再回填店铺；</span>
                      <span>③ 退款提交与退货地址获取在该通道下不支持，会降级为人工待办。</span>
                    </Space>
                  }
                />
                <Table<PurchaseOrderVo>
                  rowKey="id"
                  size="small"
                  scroll={{ x: 1600 }}
                  columns={purchaseTabColumns}
                  dataSource={purchaseQuery.data?.items ?? []}
                  pagination={false}
                />
              </>
            ),
          },
        ]}
      />

      {/* 订单详情 */}
      <Drawer
        width={960}
        title={detail ? `订单 #${detail.id}` : '订单详情'}
        open={detailId !== null}
        onClose={() => setDetailId(null)}
      >
        {detail ? (
          <>
            <Descriptions bordered size="small" column={2} style={{ marginBottom: 16 }}>
              <Descriptions.Item label="平台订单号">
                <span className="erp-mono">{detail.platform_order_no}</span>
              </Descriptions.Item>
              <Descriptions.Item label="履约状态">
                <StatusTag enumKey="OrderStatus" value={detail.fulfillment_status} />
              </Descriptions.Item>
              <Descriptions.Item label="买家（脱敏）">{detail.buyer_masked ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="收货（脱敏）">{detail.receiver_masked ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="订单金额">
                <MoneyText value={detail.total_amount} strong />
              </Descriptions.Item>
              <Descriptions.Item label="付款时间">{formatTime(detail.paid_at)}</Descriptions.Item>
              <Descriptions.Item label="履约渠道">
                <StatusTag enumKey="AdapterName" value={detail.adapter_name} />
              </Descriptions.Item>
              <Descriptions.Item label="匹配状态">{detail.match_status}</Descriptions.Item>
              <Descriptions.Item label="异常类型">
                {detail.exception_type ? (
                  <StatusTag enumKey="ExceptionType" value={detail.exception_type} />
                ) : (
                  '-'
                )}
              </Descriptions.Item>
              <Descriptions.Item label="Mock">
                <MockBadge isMock={detail.is_mock} />
              </Descriptions.Item>
            </Descriptions>

            <Typography.Title level={5}>订单明细</Typography.Title>
            <Table
              rowKey="id"
              size="small"
              pagination={false}
              dataSource={detail.items}
              columns={[
                { title: '店铺商品 ID', dataIndex: 'shop_item_id', width: 150, className: 'erp-mono' },
                { title: '店铺 SKU', dataIndex: 'shop_sku_code', width: 160, className: 'erp-mono' },
                { title: '1688 SKU', dataIndex: 'source_sku_code_1688', width: 160, className: 'erp-mono' },
                { title: '数量', dataIndex: 'quantity', width: 70 },
                {
                  title: '采购成本',
                  dataIndex: 'purchase_cost',
                  width: 110,
                  render: (value: string | null) => <MoneyText value={value} />,
                },
                {
                  title: '售价',
                  dataIndex: 'sale_price',
                  width: 110,
                  render: (value: string | null) => <MoneyText value={value} />,
                },
                { title: '匹配状态', dataIndex: 'match_status', width: 110 },
              ]}
            />

            <Typography.Title level={5} style={{ marginTop: 16 }}>
              采购单
            </Typography.Title>
            <Table<PurchaseOrderVo>
              rowKey="id"
              size="small"
              pagination={false}
              scroll={{ x: 1100 }}
              columns={purchaseColumns}
              dataSource={detail.purchase_orders}
            />

            <Typography.Title level={5} style={{ marginTop: 16 }}>
              售后
            </Typography.Title>
            <Table
              rowKey="id"
              size="small"
              pagination={false}
              dataSource={detail.after_sales}
              locale={{ emptyText: '暂无售后' }}
              columns={[
                { title: 'ID', dataIndex: 'id', width: 70 },
                { title: '平台退款号', dataIndex: 'platform_refund_no', width: 180, className: 'erp-mono' },
                {
                  title: '退款金额',
                  dataIndex: 'refund_amount',
                  width: 110,
                  render: (value: string | null) => <MoneyText value={value} />,
                },
                { title: '处理状态', dataIndex: 'handling_status', width: 110 },
                {
                  title: '1688 退款状态',
                  dataIndex: 'refund_1688_status',
                  width: 140,
                  render: (v?: string | null) =>
                    v === 'manual_pending' ? <Tag color="gold">待人工处理</Tag> : (v ?? '-'),
                },
              ]}
            />
          </>
        ) : null}
      </Drawer>

      {/* 订单处置 */}
      <Modal
        title={actionOrderId ? `订单处置 · #${actionOrderId}` : '订单处置'}
        open={actionOrderId !== null}
        onCancel={() => setActionOrderId(null)}
        onOk={() => actionForm.submit()}
        confirmLoading={actionMutation.isPending}
        okText="提交"
        cancelText="取消"
        destroyOnHidden
      >
        <Form
          form={actionForm}
          layout="vertical"
          onFinish={(values: ActionFormValues) => {
            if (actionOrderId === null) return;
            actionMutation.mutate({ id: actionOrderId, values });
          }}
        >
          <Form.Item name="action" label="处置动作" rules={[{ required: true }]}>
            <Select options={actionOptions} />
          </Form.Item>
          <Form.Item name="source_sku_code_1688" label="新 1688 SKU 编码（换货源时填写）">
            <Input placeholder="如 1688-SKU-002" />
          </Form.Item>
          <Form.Item name="reason" label="原因">
            <Input.TextArea rows={3} />
          </Form.Item>
        </Form>
      </Modal>

      {/* 手工匹配 */}
      <Modal
        title={matchOrderId ? `手工匹配货源 · 订单 #${matchOrderId}` : '手工匹配货源'}
        open={matchOrderId !== null}
        onCancel={() => setMatchOrderId(null)}
        onOk={() => matchForm.submit()}
        confirmLoading={matchMutation.isPending}
        okText="匹配并放行"
        cancelText="取消"
        destroyOnHidden
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="匹配权威源是本系统本地 sku_mapping"
          description="指定货源 SKU 并勾选建映射后，系统补建映射再放行订单；第三方匹配结果仅作交叉校验。"
        />
        <Form
          form={matchForm}
          layout="vertical"
          onFinish={(values: MatchFormValues) => {
            if (matchOrderId === null) return;
            matchMutation.mutate({ id: matchOrderId, values });
          }}
        >
          <Form.Item name="sku_mapping_id" label="映射 ID（已知时填写）">
            <Input placeholder="如 11" />
          </Form.Item>
          <Form.Item name="source_sku_id" label="货源 SKU ID（按 SKU 匹配时填写）">
            <Input placeholder="如 22" />
          </Form.Item>
          <Form.Item name="create_mapping" label="匹配成功时补建映射" valuePropName="checked">
            <Select
              options={[
                { label: '是', value: true },
                { label: '否', value: false },
              ]}
            />
          </Form.Item>
        </Form>
      </Modal>

      {/* 处理时间线 */}
      <Drawer
        width={720}
        title={timelineOrderId ? `处理时间线 · 订单 #${timelineOrderId}` : '处理时间线'}
        open={timelineOrderId !== null}
        onClose={() => setTimelineOrderId(null)}
      >
        <AuditTimeline
          items={(timelineQuery.data?.events ?? []).map((event: OrderTimelineEventVo) => ({
            at: event.at,
            title: '履约状态迁移',
            operator: event.operator,
            note: event.note,
            from_status: event.from_status,
            to_status: event.to_status,
            trace_id: event.trace_id,
          }))}
          renderStatus={(value) => <StatusTag enumKey="OrderStatus" value={value} />}
        />
      </Drawer>

      {/* 手工录入物流单号（本地兜底） */}
      <Modal
        title={trackingPurchaseId ? `录入物流单号 · 采购单 #${trackingPurchaseId}` : '录入物流单号'}
        open={trackingPurchaseId !== null}
        onCancel={() => setTrackingPurchaseId(null)}
        onOk={() => trackingForm.submit()}
        confirmLoading={trackingMutation.isPending}
        okText="保存"
        cancelText="取消"
        destroyOnHidden
      >
        <Form
          form={trackingForm}
          layout="vertical"
          onFinish={(values: TrackingFormValues) => {
            if (trackingPurchaseId === null) return;
            trackingMutation.mutate({ id: trackingPurchaseId, values });
          }}
        >
          <Form.Item name="logistics_company" label="物流公司" rules={[{ required: true }]}>
            <Input placeholder="如 中通快递" />
          </Form.Item>
          <Form.Item
            name="tracking_no"
            label="运单号"
            rules={[{ required: true }, { min: 6, message: '运单号长度不足，请核对' }]}
          >
            <Input placeholder="如 781234567890" />
          </Form.Item>
        </Form>
      </Modal>
    </PageContainer>
  );
}
