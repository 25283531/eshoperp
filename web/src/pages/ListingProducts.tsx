import { useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Descriptions,
  Drawer,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  batchOfflineListingProducts,
  fillListingProductPrice,
  getListingProduct,
  listListingProducts,
  offlineListingProduct,
  onlineListingProduct,
} from '@/api/publish';
import type { FillPriceNewConflictVo, ListingProductVo, SkuMappingVo } from '@/api/types';
import PageContainer from '@/components/PageContainer';
import ConflictBadge from '@/components/ConflictBadge';
import MockBadge from '@/components/MockBadge';
import StatusTag from '@/components/StatusTag';
import { formatTime } from '@/components/AuditTimeline';
import { useEnumOptions } from '@/hooks/useEnumOptions';
import { usePagination } from '@/hooks/usePagination';

interface ListingFilterValues {
  platform: string;
  shop_id: string;
  status: string;
  is_mock: string;
  source_product_id: string;
  /** 只看缺售价的商品（GET /listing-products?missing_price=true） */
  missing_price: string;
}

interface OfflineFormValues {
  reason: string;
}

interface BatchOfflineFormValues {
  reason: string;
}

interface FillPriceFormValues {
  sale_price: string;
  reason: string;
}

const DEFAULT_FILTERS: ListingFilterValues = {
  platform: '',
  shop_id: '',
  status: '',
  is_mock: '',
  source_product_id: '',
  missing_price: '',
};

/**
 * P10 平台商品管理：上下架。
 * 下架入口唯一（红线 R2）：第三方工具没有也不得有直接下架店铺商品的路径。
 */
export default function ListingProducts(): JSX.Element {
  const queryClient = useQueryClient();
  const { filters, params, setFilters, tablePagination, onTableChange, resetFilters } =
    usePagination<ListingFilterValues>(DEFAULT_FILTERS);
  const platformOptions = useEnumOptions('Platform');
  const statusOptions = useEnumOptions('ListingStatus');

  const [detailId, setDetailId] = useState<number | null>(null);
  const [offlineId, setOfflineId] = useState<number | null>(null);
  const [onlineId, setOnlineId] = useState<number | null>(null);
  const [batchOpen, setBatchOpen] = useState<boolean>(false);
  const [priceOpen, setPriceOpen] = useState<boolean>(false);
  const [selectedRowKeys, setSelectedRowKeys] = useState<number[]>([]);

  const [offlineForm] = Form.useForm<OfflineFormValues>();
  const [batchForm] = Form.useForm<BatchOfflineFormValues>();
  const [priceForm] = Form.useForm<FillPriceFormValues>();

  const listQuery = useQuery({
    queryKey: ['listing-products', params],
    queryFn: () => listListingProducts(params),
  });

  const detailQuery = useQuery({
    queryKey: ['listing-products', detailId],
    queryFn: () => getListingProduct(detailId as number),
    enabled: detailId !== null,
  });

  const offlineMutation = useMutation({
    mutationFn: (body: { id: number; reason: string }) =>
      offlineListingProduct(body.id, { reason: body.reason }),
    onSuccess: () => {
      message.success('已下架（唯一的下架入口，已落审计）');
      setOfflineId(null);
      offlineForm.resetFields();
      void queryClient.invalidateQueries({ queryKey: ['listing-products'] });
    },
  });

  const onlineMutation = useMutation({
    mutationFn: (id: number) => onlineListingProduct(id, true),
    onSuccess: () => {
      message.success('已重新上架');
      setOnlineId(null);
      void queryClient.invalidateQueries({ queryKey: ['listing-products'] });
    },
  });

  /**
   * 批量补填售价：逐个调用 POST /listing-products/{id}/fill-price 并汇总结果。
   *
   * 后端请求体是 `items[]`（按 SKU 逐条提交 shop_sku_code + sale_price），
   * 因此每个商品先取其明细拿到 SKU 列表，只对 `missing_price=true` 的 SKU 提交，
   * 避免覆盖已经有售价的 SKU。
   */
  const priceMutation = useMutation({
    mutationFn: async (body: { ids: number[]; sale_price: string; reason?: string }) => {
      let success = 0;
      let updated = 0;
      const newConflicts: FillPriceNewConflictVo[] = [];
      const failed: { id: number; reason: string }[] = [];
      for (const id of body.ids) {
        try {
          const detail = await getListingProduct(id);
          const items = (detail.skus ?? [])
            .filter((sku) => sku.missing_price || !sku.sale_price)
            .map((sku) => ({ shop_sku_code: sku.shop_sku_code, sale_price: body.sale_price }));
          if (items.length === 0) {
            failed.push({ id, reason: '该商品无缺售价 SKU，已跳过' });
            continue;
          }
          const result = await fillListingProductPrice(id, { items, reason: body.reason });
          success += 1;
          updated += result.updated ?? 0;
          newConflicts.push(...(result.new_conflicts ?? []));
        } catch (error: unknown) {
          const reason = error instanceof Error ? error.message : '未知错误';
          failed.push({ id, reason });
        }
      }
      return { success, updated, newConflicts, failed };
    },
    onSuccess: (data) => {
      if (data.newConflicts.length > 0) {
        // ★ 补填后重算出的成本倒挂：这是「终于能发现了」，要显式告知而不是静默
        message.warning(
          `补填完成（${data.updated} 个 SKU），但重算出 ${data.newConflicts.length} 条成本倒挂，请到映射冲突页处理`,
        );
      } else {
        message.success(
          `售价补填完成：成功 ${data.success} 个商品 / ${data.updated} 个 SKU，失败 ${data.failed.length} 个`,
        );
      }
      setPriceOpen(false);
      setSelectedRowKeys([]);
      priceForm.resetFields();
      void queryClient.invalidateQueries({ queryKey: ['listing-products'] });
    },
  });

  const batchMutation = useMutation({
    mutationFn: (body: { ids: number[]; reason: string }) =>
      batchOfflineListingProducts(body.ids, body.reason),
    onSuccess: (data) => {
      message.success(
        `批量下架完成：成功 ${data.success.length} 个，失败 ${data.failed.length} 个`,
      );
      setBatchOpen(false);
      setSelectedRowKeys([]);
      batchForm.resetFields();
      void queryClient.invalidateQueries({ queryKey: ['listing-products'] });
    },
  });

  const columns = useMemo<ColumnsType<ListingProductVo>>(
    () => [
      { title: 'ID', dataIndex: 'id', width: 70 },
      {
        title: '标题',
        dataIndex: 'title',
        ellipsis: true,
        render: (value: string, record) => (
          <Space size={4}>
            <Typography.Link onClick={() => setDetailId(record.id)}>{value}</Typography.Link>
            <MockBadge isMock={record.is_mock} />
          </Space>
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
      { title: '店铺商品 ID', dataIndex: 'shop_item_id', width: 150, className: 'erp-mono' },
      {
        title: '状态',
        dataIndex: 'status',
        width: 100,
        render: (value: string) => <StatusTag enumKey="ListingStatus" value={value} />,
      },
      { title: 'SKU 数', dataIndex: 'sku_count', width: 80 },
      {
        title: '售价完整性',
        key: 'missing_price_count',
        width: 130,
        render: (_value: unknown, record) =>
          record.missing_price_count > 0 ? (
            <Tooltip title="存在缺售价 SKU，无法做成本倒挂（cost_underwater）检测，请补填售价">
              <Tag color="red">缺售价 {record.missing_price_count}</Tag>
            </Tooltip>
          ) : (
            <Tag color="green">已齐</Tag>
          ),
      },
      { title: '下架原因', dataIndex: 'offline_reason', width: 160, ellipsis: true },
      {
        title: '上架时间',
        dataIndex: 'published_at',
        width: 170,
        render: (value: string | null) => formatTime(value),
      },
      {
        title: '操作',
        key: 'actions',
        width: 170,
        fixed: 'right',
        render: (_value: unknown, record) => (
          <Space size="small">
            <Typography.Link onClick={() => setDetailId(record.id)}>详情</Typography.Link>
            {record.status === 'on_sale' ? (
              <Typography.Link
                onClick={() => {
                  setOfflineId(record.id);
                  offlineForm.setFieldsValue({ reason: '' });
                }}
              >
                下架
              </Typography.Link>
            ) : (
              <Typography.Link onClick={() => setOnlineId(record.id)}>上架</Typography.Link>
            )}
          </Space>
        ),
      },
    ],
    [offlineForm],
  );

  const mappingColumns = useMemo<ColumnsType<SkuMappingVo>>(
    () => [
      { title: '映射 ID', dataIndex: 'id', width: 90 },
      { title: '店铺 SKU', dataIndex: 'shop_sku_code', width: 160, className: 'erp-mono' },
      { title: '1688 SKU', dataIndex: 'source_sku_code_1688', width: 160, className: 'erp-mono' },
      {
        title: '成本',
        dataIndex: 'purchase_cost',
        width: 100,
        render: (value: string) => `¥${value}`,
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
        width: 160,
        render: (_value: unknown, record) =>
          record.has_conflict ? (
            <ConflictBadge level={record.conflict_level} conflictTypes={record.conflict_types} />
          ) : (
            <Tag>无</Tag>
          ),
      },
    ],
    [],
  );

  const detail = detailQuery.data;

  return (
    <PageContainer
      title="平台商品管理"
      subTitle="下架入口唯一：库存/价格变动只能由自研 ListingAdapter 执行，第三方工具无任何直写店铺商品的路径（红线 R2）"
      loading={listQuery.isLoading}
      extra={
        <Space>
          <Button
            disabled={selectedRowKeys.length === 0}
            onClick={() => setPriceOpen(true)}
          >
            批量补填售价（{selectedRowKeys.length}）
          </Button>
          <Button
            danger
            disabled={selectedRowKeys.length === 0}
            onClick={() => setBatchOpen(true)}
          >
            批量下架（{selectedRowKeys.length}）
          </Button>
          <Button onClick={resetFilters}>重置筛选</Button>
        </Space>
      }
    >
      <Form
        layout="inline"
        style={{ marginBottom: 12, rowGap: 8 }}
        initialValues={filters}
        onFinish={(values: ListingFilterValues) => setFilters(values)}
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
        <Form.Item name="is_mock" label="Mock">
          <Select
            allowClear
            placeholder="全部"
            style={{ width: 100 }}
            options={[
              { label: '仅 Mock', value: 'true' },
              { label: '仅真实', value: 'false' },
            ]}
          />
        </Form.Item>
        <Form.Item name="source_product_id" label="货源商品 ID">
          <Input allowClear placeholder="如 123" style={{ width: 140 }} />
        </Form.Item>
        <Form.Item name="missing_price" label="缺售价">
          <Select
            allowClear
            placeholder="全部"
            style={{ width: 130 }}
            options={[
              { label: '仅缺售价', value: 'true' },
              { label: '已有售价', value: 'false' },
            ]}
          />
        </Form.Item>
        <Form.Item>
          <Button type="primary" htmlType="submit">
            查询
          </Button>
        </Form.Item>
      </Form>

      <Table<ListingProductVo>
        rowKey="id"
        size="small"
        scroll={{ x: 1400 }}
        columns={columns}
        dataSource={listQuery.data?.items ?? []}
        pagination={{ ...tablePagination, total: listQuery.data?.total ?? 0 }}
        onChange={onTableChange}
        rowSelection={{
          selectedRowKeys,
          onChange: (keys) => setSelectedRowKeys(keys as number[]),
        }}
      />

      {/* 详情 */}
      <Drawer
        width={900}
        title={detail ? `平台商品 #${detail.id} ${detail.title}` : '平台商品详情'}
        open={detailId !== null}
        onClose={() => setDetailId(null)}
      >
        {detail ? (
          <>
            <Descriptions bordered size="small" column={2} style={{ marginBottom: 16 }}>
              <Descriptions.Item label="平台">
                <StatusTag enumKey="Platform" value={detail.platform} />
              </Descriptions.Item>
              <Descriptions.Item label="状态">
                <StatusTag enumKey="ListingStatus" value={detail.status} />
              </Descriptions.Item>
              <Descriptions.Item label="店铺 ID">{detail.shop_id}</Descriptions.Item>
              <Descriptions.Item label="店铺商品 ID">
                <span className="erp-mono">{detail.shop_item_id}</span>
              </Descriptions.Item>
              <Descriptions.Item label="Mock">
                <MockBadge isMock={detail.is_mock} />
              </Descriptions.Item>
              <Descriptions.Item label="上架时间">{formatTime(detail.published_at)}</Descriptions.Item>
              <Descriptions.Item label="下架原因">{detail.offline_reason ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="下架时间">{formatTime(detail.offline_at)}</Descriptions.Item>
              <Descriptions.Item label="关联货源商品" span={2}>
                {detail.source_product_title ?? '-'}
              </Descriptions.Item>
            </Descriptions>

            <Typography.Title level={5}>平台 SKU</Typography.Title>
            <Table
              rowKey="id"
              size="small"
              pagination={false}
              dataSource={detail.skus}
              columns={[
                { title: 'SKU 编码', dataIndex: 'shop_sku_code', className: 'erp-mono' },
                { title: '规格名称', dataIndex: 'shop_sku_name', render: (v?: string | null) => v ?? '-' },
              ]}
            />

            <Typography.Title level={5} style={{ marginTop: 16 }}>
              SKU 映射
            </Typography.Title>
            <Table<SkuMappingVo>
              rowKey="id"
              size="small"
              pagination={false}
              dataSource={detail.mappings}
              columns={mappingColumns}
            />
          </>
        ) : null}
      </Drawer>

      {/* 下架 */}
      <Modal
        title={offlineId ? `下架商品 #${offlineId}` : '下架商品'}
        open={offlineId !== null}
        onCancel={() => setOfflineId(null)}
        onOk={() => offlineForm.submit()}
        confirmLoading={offlineMutation.isPending}
        okText="确认下架"
        okButtonProps={{ danger: true }}
        cancelText="取消"
        destroyOnHidden
      >
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="下架动作会调用自研 ListingAdapter 写店铺，并落审计（action_type=offline）"
          description="第三方工具不会也不能执行此操作。"
        />
        <Form
          form={offlineForm}
          layout="vertical"
          onFinish={(values: OfflineFormValues) => {
            if (offlineId === null) return;
            offlineMutation.mutate({ id: offlineId, reason: values.reason });
          }}
        >
          <Form.Item name="reason" label="下架原因" rules={[{ required: true }]}>
            <Input.TextArea rows={3} placeholder="如：货源缺货 / 成本涨幅超阈值 / 人工下架" />
          </Form.Item>
        </Form>
      </Modal>

      {/* 上架 */}
      <Modal
        title={onlineId ? `重新上架商品 #${onlineId}` : '重新上架'}
        open={onlineId !== null}
        onCancel={() => setOnlineId(null)}
        onOk={() => {
          if (onlineId !== null) onlineMutation.mutate(onlineId);
        }}
        confirmLoading={onlineMutation.isPending}
        okText="确认上架"
        cancelText="取消"
      >
        <Alert
          type="info"
          showIcon
          message="上架前会重新校验映射有效性"
          description="若映射无效或处于待确认状态，接口返回 422，系统只通知不自动上架。"
        />
      </Modal>

      {/* 批量补填售价 */}
      <Modal
        title={`批量补填售价（${selectedRowKeys.length} 个商品）`}
        open={priceOpen}
        onCancel={() => setPriceOpen(false)}
        onOk={() => priceForm.submit()}
        confirmLoading={priceMutation.isPending}
        okText="确认补填"
        cancelText="取消"
        destroyOnHidden
      >
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="售价是成本倒挂检测的前置数据"
          description="没有售价就无法判断「售价 < 采购成本」的亏损铺货（冲突类型 cost_underwater，P1 提示）。补填后立即参与倒挂检测。"
        />
        <Form
          form={priceForm}
          layout="vertical"
          onFinish={(values: FillPriceFormValues) =>
            priceMutation.mutate({
              ids: selectedRowKeys,
              sale_price: values.sale_price,
              reason: values.reason || undefined,
            })
          }
        >
          <Form.Item
            name="sale_price"
            label="售价（元）"
            rules={[
              { required: true, message: '请填写售价' },
              { pattern: /^\d+(\.\d{1,2})?$/, message: '格式如 29.90' },
            ]}
          >
            <Input placeholder="29.90" />
          </Form.Item>
          <Form.Item name="reason" label="补填原因">
            <Input.TextArea rows={2} placeholder="如：半自动上架时未填售价，统一按定价表补填" />
          </Form.Item>
        </Form>
      </Modal>

      {/* 批量下架 */}
      <Modal
        title={`批量下架（${selectedRowKeys.length} 个）`}
        open={batchOpen}
        onCancel={() => setBatchOpen(false)}
        onOk={() => batchForm.submit()}
        confirmLoading={batchMutation.isPending}
        okText="确认下架"
        okButtonProps={{ danger: true }}
        cancelText="取消"
        destroyOnHidden
      >
        <Form
          form={batchForm}
          layout="vertical"
          onFinish={(values: BatchOfflineFormValues) =>
            batchMutation.mutate({ ids: selectedRowKeys, reason: values.reason })
          }
        >
          <Form.Item name="reason" label="下架原因" rules={[{ required: true }]}>
            <Input.TextArea rows={3} />
          </Form.Item>
        </Form>
      </Modal>
    </PageContainer>
  );
}
