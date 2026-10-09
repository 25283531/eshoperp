import { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  Divider,
  Form,
  InputNumber,
  Progress,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { SyncOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  getInventoryConfig,
  listAutoOfflineRecords,
  listInventoryAlerts,
  listInventorySnapshots,
  listPriceSnapshots,
  syncInventory,
  updateInventoryConfig,
} from '@/api/ops';
import type {
  AutoOfflineRecordVo,
  InventoryAlertVo,
  InventorySnapshotVo,
  PriceSnapshotVo,
} from '@/api/types';
import PageContainer from '@/components/PageContainer';
import MoneyText from '@/components/MoneyText';
import StatusTag from '@/components/StatusTag';
import { formatTime } from '@/components/AuditTimeline';
import { useEnumOptions } from '@/hooks/useEnumOptions';
import { usePagination } from '@/hooks/usePagination';

interface InventoryFilterValues {
  source_sku_id: string;
  source_product_id: string;
  type: string;
}

interface ConfigFormValues {
  poll_interval_min: number;
  price_increase_threshold: string;
  out_of_stock_action: string;
  price_increase_action: string;
}

const DEFAULT_FILTERS: InventoryFilterValues = {
  source_sku_id: '',
  source_product_id: '',
  type: '',
};

/** 涨幅百分比渲染（change_rate 为字符串比例，如 "0.1234"） */
/**
 * 告警的库存数据源展示文案（F11）。
 *
 * ⚠️ 硬约束：`unknown` 必须映射为「未知」并如实显示，
 *    **严禁回落成「自动同步」** —— 回落会把"没有数据"伪装成"有可信数据"，
 *    直接绕过 §4.8 的自动下架门槛（L5 / 第 15 条反复提防的失效形态）。
 */
const DATA_SOURCE_LABEL: Record<string, string> = {
  erp_poll: '自动同步',
  third_party_push: '第三方推送',
  manual_import: '手工维护',
  manual_edit: '手工编辑',
  unknown: '未知',
};

function renderRate(value: string | null | undefined): JSX.Element {
  const num = value === null || value === undefined ? Number.NaN : Number(value);
  if (!Number.isFinite(num)) return <span>-</span>;
  const percent = Number((num * 100).toFixed(2));
  return (
    <Progress
      percent={Math.min(Math.abs(percent), 100)}
      size="small"
      status={percent > 0 ? 'exception' : 'normal'}
      format={() => `${percent > 0 ? '+' : ''}${percent}%`}
    />
  );
}

/**
 * ★ F11（架构 v1.14 / PRD §4.8）库存数据源标签。
 *
 * 取值来自后端 `InventorySnapshotVo.source`，当前后端枚举只有两种：
 *   - `erp_poll`        自研轮询采集 → 自动同步
 *   - `third_party_push` 第三方推送   → 第三方推送
 * 空值 / 无快照 → 暂无数据。
 *
 * ⚠ 「手工维护」这一档后端**尚不存在**（`InventorySource` 枚举无 manual 值，
 *   也没有任何手工改库存的接口），所以这里**不能**凭空造出"手工维护"状态。
 */
function renderStockSource(source: string | null | undefined): JSX.Element {
  switch (source) {
    case 'erp_poll':
      return <Tag color="blue">自动同步（ERP 轮询）</Tag>;
    case 'third_party_push':
      return <Tag color="purple">第三方推送</Tag>;
    case null:
    case undefined:
    case '':
      return <Tag>暂无数据</Tag>;
    default:
      return <Tag color="default">{source}</Tag>;
  }
}

/**
 * 快照龄（距最后更新的时长）。
 *
 * ★ 只陈述"多久没更新"这个**事实**，不输出"库存已过期"或"同步已中断"这类**根因结论**。
 * 根因不同处置方式正好相反（架构 §5.8 边界）：手工维护超期要人工核对；
 * 而同步中断时若提示成"过期"，运营一改库存，最近快照会变成 manual_edit，
 * 该 SKU 直接退出自动下架范围 —— 一句提示写错会把可信数据源污染成不可信数据源。
 */
function renderSnapshotAge(collectedAt: string | null | undefined): JSX.Element {
  if (!collectedAt) return <span>-</span>;
  const collected = new Date(collectedAt).getTime();
  if (!Number.isFinite(collected)) return <span>-</span>;
  const hours = Math.max(0, (Date.now() - collected) / 3_600_000);
  if (hours < 1) return <span>{Math.round(hours * 60)} 分钟前</span>;
  if (hours < 24) return <span>{hours.toFixed(1)} 小时前</span>;
  const days = Math.floor(hours / 24);
  return (
    <Tag color="orange">
      {days} 天 {Math.round(hours % 24)} 小时未更新
    </Tag>
  );
}

/**
 * P14 库存与价格监控：快照表 + 涨幅榜 + 阈值配置 + 自动下架记录。
 */
export default function Inventory(): JSX.Element {
  const queryClient = useQueryClient();
  const { params, tablePagination, onTableChange } = usePagination<InventoryFilterValues>(
    DEFAULT_FILTERS,
  );
  const actionOptions = useEnumOptions('InventoryAction');
  const [activeTab, setActiveTab] = useState<string>('alerts');
  const [configForm] = Form.useForm<ConfigFormValues>();

  const configQuery = useQuery({
    queryKey: ['inventory', 'config'],
    queryFn: getInventoryConfig,
  });

  useEffect(() => {
    if (configQuery.data) {
      configForm.setFieldsValue({
        poll_interval_min: configQuery.data.poll_interval_min,
        price_increase_threshold: configQuery.data.price_increase_threshold,
        out_of_stock_action: configQuery.data.out_of_stock_action,
        price_increase_action: configQuery.data.price_increase_action,
      });
    }
  }, [configForm, configQuery.data]);

  const alertsQuery = useQuery({
    queryKey: ['inventory', 'alerts', params],
    queryFn: () => listInventoryAlerts({ ...params, type: params.type || undefined }),
    enabled: activeTab === 'alerts',
  });

  const snapshotsQuery = useQuery({
    queryKey: ['inventory', 'snapshots', params],
    queryFn: () => listInventorySnapshots(params),
    enabled: activeTab === 'snapshots',
  });

  const priceQuery = useQuery({
    queryKey: ['inventory', 'price-snapshots', params],
    queryFn: () => listPriceSnapshots(params),
    enabled: activeTab === 'price',
  });

  const offlineQuery = useQuery({
    queryKey: ['inventory', 'auto-offline-records', params],
    queryFn: () => listAutoOfflineRecords(params),
    enabled: activeTab === 'offline',
  });

  const syncMutation = useMutation({
    mutationFn: () => syncInventory({ force: false }),
    onSuccess: (data) => {
      message.success(`库存同步已受理：本次写入 ${data.snapshot_count} 条快照`);
      void queryClient.invalidateQueries({ queryKey: ['inventory'] });
    },
  });

  const configMutation = useMutation({
    mutationFn: (body: ConfigFormValues) =>
      updateInventoryConfig({
        poll_interval_min: body.poll_interval_min,
        price_increase_threshold: body.price_increase_threshold,
        out_of_stock_action: body.out_of_stock_action,
        price_increase_action: body.price_increase_action,
      }),
    onSuccess: () => {
      message.success('阈值配置已保存');
      void queryClient.invalidateQueries({ queryKey: ['inventory', 'config'] });
    },
  });

  const alertColumns = useMemo<ColumnsType<InventoryAlertVo>>(
    () => [
      { title: 'ID', dataIndex: 'id', width: 70 },
      { title: '货源商品', dataIndex: 'source_product_title', ellipsis: true },
      { title: '1688 SKU', dataIndex: 'source_sku_name', width: 180 },
      {
        title: '类型',
        dataIndex: 'type',
        width: 120,
        render: (value: string) => (
          <Tag color={value === 'out_of_stock' ? 'volcano' : 'gold'}>
            {value === 'out_of_stock' ? '缺货' : '涨价'}
          </Tag>
        ),
      },
      { title: '当前库存', dataIndex: 'current_stock', width: 100, render: (v?: number | null) => v ?? '-' },
      {
        title: '当前成本',
        dataIndex: 'current_cost',
        width: 110,
        render: (value: string | null) => <MoneyText value={value} />,
      },
      {
        title: '原价',
        dataIndex: 'prev_cost',
        width: 110,
        render: (value: string | null) => <MoneyText value={value} deleted />,
      },
      { title: '涨幅', dataIndex: 'change_rate', width: 160, render: (v: string) => renderRate(v) },
      { title: '阈值', dataIndex: 'threshold', width: 90, render: (v: string) => renderRate(v) },
      {
        title: '建议动作',
        dataIndex: 'suggested_action',
        width: 110,
        // ★ 文案必须带「建议」前缀：这一列是**尚未发生**的建议动作，
        //   红色 + 光秃秃的"下架"会被误读成"已下架"。配色同步改橙。
        render: (value: string) =>
          value === 'offline' ? <Tag color="orange">建议下架</Tag> : <Tag>建议通知</Tag>,
      },
      {
        // ★ F11：库存数据源。后端字段 `data_source`
        //   （erp_poll / manual_import / third_party_push / manual_edit / unknown）
        //   ⚠️ 硬约束：`unknown` 必须显式显示「未知」，**严禁回落成「自动同步」** ——
        //      回落会把"没有数据"伪装成"有可信数据"，直接绕过 §4.8 自动下架门槛。
        title: '库存数据源',
        dataIndex: 'data_source',
        width: 110,
        render: (value: string | null) => <Tag>{DATA_SOURCE_LABEL[value ?? 'unknown'] ?? '未知'}</Tag>,
      },
      {
        // ★ F11：可否自动下架。false ⇒ 只告警、不自动执行，需人工确认后一键下架。
        title: '可否自动下架',
        dataIndex: 'auto_offline_allowed',
        width: 120,
        render: (value: boolean | null) =>
          value ? (
            <Tag color="orange">可自动下架</Tag>
          ) : (
            // 措辞必须能表达「系统不会替你执行」，避免使用者误以为已下架
            <Tag>仅告警·需人工确认</Tag>
          ),
      },
      { title: '检测时间', dataIndex: 'detected_at', width: 170, render: (v: string) => formatTime(v) },
    ],
    [],
  );

  /**
   * 库存快照列。
   *
   * ★ F11：补「库存数据源」+「最后更新时间」+「快照龄」三列。
   * 库存数据源 / 最后更新时间直接取后端字段（source / collected_at），如实渲染。
   */
  const snapshotColumns = useMemo<ColumnsType<InventorySnapshotVo>>(
    () => [
      { title: 'ID', dataIndex: 'id', width: 70 },
      { title: '货源商品', dataIndex: 'source_product_title', ellipsis: true },
      { title: '1688 SKU', dataIndex: 'source_sku_name', width: 200 },
      {
        title: '库存',
        dataIndex: 'stock_qty',
        width: 90,
        render: (value: number | null) =>
          value === null ? '-' : value === 0 ? <Tag color="red">0</Tag> : value,
      },
      {
        title: '库存状态',
        dataIndex: 'stock_status',
        width: 110,
        render: (value: string | null) => <StatusTag enumKey="StockStatus" value={value} />,
      },
      {
        title: '库存数据源',
        dataIndex: 'source',
        width: 170,
        render: (value: string | null) => renderStockSource(value),
      },
      {
        title: '最后更新时间',
        dataIndex: 'collected_at',
        width: 170,
        render: (v: string) => formatTime(v),
      },
      {
        title: '快照龄',
        dataIndex: 'collected_at',
        width: 170,
        render: (v: string) => renderSnapshotAge(v),
      },
    ],
    [],
  );

  const priceColumns = useMemo<ColumnsType<PriceSnapshotVo>>(
    () => [
      { title: 'ID', dataIndex: 'id', width: 70 },
      { title: '1688 SKU', dataIndex: 'source_sku_name', width: 220 },
      {
        title: '当前成本',
        dataIndex: 'cost',
        width: 110,
        render: (value: string) => <MoneyText value={value} />,
      },
      {
        title: '上次成本',
        dataIndex: 'prev_cost',
        width: 110,
        render: (value: string | null) => <MoneyText value={value} />,
      },
      { title: '涨幅', dataIndex: 'change_rate', width: 160, render: (v: string) => renderRate(v) },
      {
        title: '是否超阈值',
        dataIndex: 'over_threshold',
        width: 110,
        render: (value: boolean) => (value ? <Tag color="red">超阈值</Tag> : <Tag>正常</Tag>),
      },
      { title: '采集时间', dataIndex: 'collected_at', width: 170, render: (v: string) => formatTime(v) },
    ],
    [],
  );

  const offlineColumns = useMemo<ColumnsType<AutoOfflineRecordVo>>(
    () => [
      { title: 'ID', dataIndex: 'id', width: 70 },
      { title: '商品', dataIndex: 'title', ellipsis: true },
      {
        title: '平台',
        dataIndex: 'platform',
        width: 100,
        render: (value: string | null) => <StatusTag enumKey="Platform" value={value} />,
      },
      { title: '店铺商品 ID', dataIndex: 'shop_item_id', width: 160, className: 'erp-mono' },
      { title: '触发类型', dataIndex: 'trigger_type', width: 120, render: (v?: string | null) => v ?? '-' },
      { title: '原因', dataIndex: 'reason', ellipsis: true },
      {
        title: '结果',
        dataIndex: 'success',
        width: 90,
        render: (value: boolean) => (value ? <Tag color="green">成功</Tag> : <Tag color="red">失败</Tag>),
      },
      { title: '时间', dataIndex: 'created_at', width: 170, render: (v: string) => formatTime(v) },
    ],
    [],
  );

  return (
    <PageContainer
      title="库存与价格监控"
      subTitle="库存/价格变动只能由自研 ListingAdapter 执行下架，第三方无直写店铺商品的路径（红线 R2）"
      extra={
        <Space>
          <Button
            icon={<SyncOutlined />}
            loading={syncMutation.isPending}
            onClick={() => syncMutation.mutate()}
          >
            同步库存
          </Button>
        </Space>
      }
      alert={
        configQuery.data ? (
          <Card size="small" styles={{ body: { padding: 12 } }}>
            <Row gutter={16}>
              <Col span={6}>
                <Statistic title="轮询间隔（分钟）" value={configQuery.data.poll_interval_min} />
              </Col>
              <Col span={6}>
                <Statistic
                  title="涨价阈值"
                  value={Number(configQuery.data.price_increase_threshold ?? 0) * 100}
                  precision={2}
                  suffix="%"
                />
              </Col>
              <Col span={6}>
                <Statistic title="缺货动作" value={configQuery.data.out_of_stock_action} />
              </Col>
              <Col span={6}>
                <Statistic title="涨价动作" value={configQuery.data.price_increase_action} />
              </Col>
            </Row>
          </Card>
        ) : null
      }
    >
      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={[
          {
            key: 'alerts',
            label: `涨幅榜 / 告警（${alertsQuery.data?.total ?? 0}）`,
            children: (
              <Table<InventoryAlertVo>
                rowKey="id"
                size="small"
                scroll={{ x: 1500 }}
                columns={alertColumns}
                dataSource={alertsQuery.data?.items ?? []}
                pagination={{ ...tablePagination, total: alertsQuery.data?.total ?? 0 }}
                onChange={onTableChange}
              />
            ),
          },
          {
            key: 'snapshots',
            label: '库存快照',
            children: (
              <>
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 12 }}
                  message="「库存可能已过期」/「自动同步已中断」两种结论暂未开启"
                  description="这两种陈旧的根因完全不同、处置方式正好相反：手工维护超期 → 请人工核对库存；自动同步中断 → **不要动库存**（一改就把最近快照变成 manual_edit，该 SKU 会退出自动下架范围）。后端目前未提供「手工维护」标记、`inventory.manual_stock_max_age_days` 配置与同步中断信号，在判据齐备前本页只呈现「数据源 / 最后更新时间 / 快照龄」这些事实，不替运营下根因结论。"
                />
                <Table<InventorySnapshotVo>
                  rowKey="id"
                  size="small"
                  scroll={{ x: 1600 }}
                  columns={snapshotColumns}
                  dataSource={snapshotsQuery.data?.items ?? []}
                  pagination={{ ...tablePagination, total: snapshotsQuery.data?.total ?? 0 }}
                  onChange={onTableChange}
                />
              </>
            ),
          },
          {
            key: 'price',
            label: '价格快照',
            children: (
              <Table<PriceSnapshotVo>
                rowKey="id"
                size="small"
                scroll={{ x: 1200 }}
                columns={priceColumns}
                dataSource={priceQuery.data?.items ?? []}
                pagination={{ ...tablePagination, total: priceQuery.data?.total ?? 0 }}
                onChange={onTableChange}
              />
            ),
          },
          {
            key: 'offline',
            label: '自动下架记录',
            children: (
              <Table<AutoOfflineRecordVo>
                rowKey="id"
                size="small"
                scroll={{ x: 1400 }}
                columns={offlineColumns}
                dataSource={offlineQuery.data?.items ?? []}
                pagination={{ ...tablePagination, total: offlineQuery.data?.total ?? 0 }}
                onChange={onTableChange}
              />
            ),
          },
          {
            key: 'config',
            label: '阈值配置',
            children: (
              <>
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 16 }}
                  message="自动下架由 InventoryService 决策"
                  description="缺货或涨幅超阈值时按此处动作执行；库存恢复后重新上架仍需映射校验通过，否则只通知不自动上架。"
                />
                <Form
                  form={configForm}
                  layout="vertical"
                  style={{ maxWidth: 520 }}
                  onFinish={(values: ConfigFormValues) => configMutation.mutate(values)}
                >
                  <Form.Item
                    name="poll_interval_min"
                    label="轮询间隔（分钟）"
                    rules={[{ required: true }]}
                  >
                    <InputNumber min={1} max={1440} style={{ width: '100%' }} />
                  </Form.Item>
                  <Form.Item
                    name="price_increase_threshold"
                    label="涨价阈值（比例，如 0.10 表示 10%）"
                    rules={[{ required: true }]}
                  >
                    <Select
                      options={[
                        { label: '5%', value: '0.05' },
                        { label: '10%', value: '0.10' },
                        { label: '15%', value: '0.15' },
                        { label: '20%', value: '0.20' },
                      ]}
                    />
                  </Form.Item>
                  <Form.Item name="out_of_stock_action" label="缺货动作" rules={[{ required: true }]}>
                    <Select options={actionOptions} />
                  </Form.Item>
                  <Form.Item name="price_increase_action" label="涨价动作" rules={[{ required: true }]}>
                    <Select options={actionOptions} />
                  </Form.Item>
                  <Form.Item>
                    <Button type="primary" htmlType="submit" loading={configMutation.isPending}>
                      保存配置
                    </Button>
                  </Form.Item>
                </Form>
                <Divider />
                <Typography.Text type="secondary">
                  库存恢复后重新上架由 InventoryService 触发，映射无效时仅通知不自动上架（INV-P1-01）。
                </Typography.Text>
              </>
            ),
          },
        ]}
      />
    </PageContainer>
  );
}
