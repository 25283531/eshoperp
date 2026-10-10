import { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Checkbox,
  Descriptions,
  Divider,
  Drawer,
  Form,
  Image,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tabs,
  Tag,
  Typography,
  Upload,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { UploadFile } from 'antd';
import {
  CloudDownloadOutlined,
  DownloadOutlined,
  PlusOutlined,
  UploadOutlined,
} from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  assetDownloadUrl,
  batchDownloadAssets,
  collectSourceProducts,
  createSourceProductManual,
  createSupplier,
  deleteSourceProduct,
  deleteSupplier,
  downloadCsvTemplate,
  getSourceProduct,
  importSourceProductsCsv,
  listAssets,
  listSourceProducts,
  listSuppliers,
  rollbackAsset,
  updateSupplier,
} from '@/api/catalog';
import { createAiTasks } from '@/api/ai';
import { triggerDownload } from '@/api/client';
import { getTask } from '@/api/system';
import type {
  AssetVo,
  CollectFailedVo,
  CsvImportFailedRowVo,
  CsvImportResultVo,
  SourceProductVo,
  SourceSkuVo,
  SupplierVo,
  TaskRecordVo,
} from '@/api/types';
import PageContainer from '@/components/PageContainer';
import MoneyText from '@/components/MoneyText';
import StatusTag from '@/components/StatusTag';
import { formatTime } from '@/components/AuditTimeline';
import { useEnumOptions } from '@/hooks/useEnumOptions';
import { usePagination } from '@/hooks/usePagination';

interface SourceFilterValues {
  keyword: string;
  supplier_id: string;
  status: string;
  collected_from: string;
  collected_to: string;
}

interface CollectFormValues {
  identifiers: string;
}

interface AiTaskFormValues {
  target_platform: string;
  rework_items: string[];
}

interface SupplierFormValues {
  supplier_1688_id: string;
  name: string;
  location: string;
  lead_time_hours: number;
  moq: number;
  cooperation_score: number;
}

/** 手工录入弹窗的表单值（SKU 用 Form.List，最少 1 行） */
interface ManualFormValues {
  title: string;
  product_code?: string;
  category_path?: string;
  supplier_id?: number;
  cost_price?: string;
  origin_url?: string;
  main_image_url?: string;
  skus: {
    spec_name?: string;
    spec_value?: string;
    sku_code?: string;
    cost_price?: string;
    sale_price?: string;
    stock_qty?: number;
  }[];
}

const DEFAULT_FILTERS: SourceFilterValues = {
  keyword: '',
  supplier_id: '',
  status: '',
  collected_from: '',
  collected_to: '',
};

const REWORK_ITEM_OPTIONS = [
  { label: '标题重写', value: 'title' },
  { label: '卖点提炼', value: 'selling_points' },
  { label: '主图重绘', value: 'main_image' },
  { label: '详情图重绘', value: 'detail_image' },
  { label: '属性填充', value: 'attributes' },
];

/** CSV 导入失败明细列（★ 逐行展示：行号 + 标识 + 中文原因） */
const CSV_FAILED_COLUMNS: ColumnsType<CsvImportFailedRowVo> = [
  { title: '行号', dataIndex: 'row', width: 80, render: (value: number) => `第 ${value} 行` },
  {
    title: '标识',
    dataIndex: 'identifier',
    width: 160,
    render: (value: string | null) => value ?? '-',
  },
  { title: '失败原因', dataIndex: 'reason' },
];

// ---------------------------------------------------------------------------
// 1688 采集结果：逐条失败的识别与处置建议
// ---------------------------------------------------------------------------

/** 任务进入终态即停止轮询 */
const TERMINAL_TASK_STATUS: string[] = ['success', 'failed', 'cancelled'];

/** 后端判定"这条链接里没有商品 ID"时给的文案（backend/app/utils/kit.py） */
const REASON_NO_PRODUCT_ID = '无法从链接中识别商品 ID';
/** 后端判定"应用没开通这个接口"时给的文案片段（backend/app/adapters/source/alibaba1688.py） */
const REASON_NO_PERMISSION = '未被授予该接口';

/**
 * 按失败原因给出下一步该怎么做的建议。
 *
 * ★ 权限未开通必须单独拎出来：它不是"再试一次"能解决的，
 *   使用者只有去开放平台申请这一条路；而链接解析失败只要重贴一条完整的就行。
 */
function adviceOf(reason: string): string {
  if (reason.includes(REASON_NO_PERMISSION)) {
    return '去 https://open.1688.com 控制台为应用申请「商品详情」接口权限，短期内改用手工录入或 CSV 导入';
  }
  if (reason.includes(REASON_NO_PRODUCT_ID)) {
    return '重新复制完整的商品链接（应形如 https://detail.1688.com/offer/123456.html），或直接填纯数字商品 ID';
  }
  return '按原因修正后重试；若反复失败请改用手工录入或 CSV 导入';
}

const COLLECT_FAILED_COLUMNS: ColumnsType<CollectFailedVo> = [
  {
    title: '商品链接 / ID',
    dataIndex: 'identifier',
    width: 260,
    render: (value: string | null) => (
      <Typography.Text style={{ fontSize: 12 }}>{value ?? '-'}</Typography.Text>
    ),
  },
  { title: '后端返回的原因', dataIndex: 'reason' },
  {
    title: '接下来怎么办',
    key: 'advice',
    width: 320,
    render: (_value: unknown, record) => (
      <Typography.Text type="secondary">{adviceOf(record.reason)}</Typography.Text>
    ),
  },
];

/** 从异步任务记录的 result_json 里安全地取逐条失败明细 */
function collectFailedOf(task: TaskRecordVo | undefined): CollectFailedVo[] {
  const raw = task?.result_json?.failed;
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((item) => {
    if (typeof item !== 'object' || item === null) return [];
    const row = item as Record<string, unknown>;
    return [{ identifier: String(row.identifier ?? '') || null, reason: String(row.reason ?? '') }];
  });
}

/** 从异步任务记录的 result_json 里安全地取计数 */
function countOf(task: TaskRecordVo | undefined, key: string): number {
  const raw = task?.result_json?.[key];
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : 0;
}

/**
 * 采集任务的结果面板（`POST /source-products/collect` 返回的 `task_record_id` 指向的异步任务）。
 *
 * ★ 关键：**不能只看 `status`**。
 *   后端 handler 只有在"一条都没采到且 failed 非空"时才把任务标成 `failed`；
 *   部分成功时 status 是 `success`，失败明细藏在 `result_json.failed[]` 里。
 *   只看 status 就会把"10 条里挂了 8 条"显示成采集成功。
 */
function CollectTaskResult(props: {
  task: TaskRecordVo | undefined;
  onAgain: () => void;
}): JSX.Element {
  const { task, onAgain } = props;

  if (!task) return <Spin tip="加载任务状态…" />;

  const finished = TERMINAL_TASK_STATUS.includes(String(task.status ?? ''));
  const failed = collectFailedOf(task);
  const permissionBlocked = failed.filter((item) => item.reason.includes(REASON_NO_PERMISSION));
  const created = countOf(task, 'created');
  const updated = countOf(task, 'updated');

  return (
    <>
      <Space size={24} style={{ marginBottom: 12 }} wrap>
        <span>
          任务状态：<StatusTag enumKey="TaskStatus" value={task.status} />
        </span>
        <Statistic title="受理条数" value={countOf(task, 'accepted')} />
        <Statistic title="新建商品" value={created} valueStyle={{ color: created > 0 ? '#3f8600' : undefined }} />
        <Statistic title="更新商品" value={updated} valueStyle={{ color: updated > 0 ? '#1677ff' : undefined }} />
        <Statistic
          title="失败条数"
          value={failed.length}
          valueStyle={{ color: failed.length > 0 ? '#cf1322' : undefined }}
        />
      </Space>

      {finished ? null : (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="正在后台采集"
          description="1688 接口较慢时可能需要一两分钟。本页每 3 秒自动刷新一次，不用手动点。"
        />
      )}

      {task.error_message ? (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 12 }}
          message="任务报错"
          description={task.error_message}
        />
      ) : null}

      {permissionBlocked.length > 0 ? (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 12 }}
          message="1688 应用未开通「商品详情」接口权限"
          description={
            <Space direction="vertical" size={4}>
              <span>
                {permissionBlocked.length} 条因为这个原因采集失败。这不是链接写错，
                <strong>重试多少次都一样</strong>。
              </span>
              <span>
                要去开放平台控制台为这个应用申请「商品详情」接口权限：
                <Typography.Link href="https://open.1688.com" target="_blank">
                  https://open.1688.com
                </Typography.Link>
              </span>
              <span>
                权限下来之前，请用「批量导入 CSV」或「手工录入」准备货源商品，两者不依赖外部平台。
              </span>
            </Space>
          }
        />
      ) : null}

      {failed.length > 0 ? (
        <Table<CollectFailedVo>
          rowKey={(record, index) => `${record.identifier}-${record.reason}-${index ?? 0}`}
          size="small"
          pagination={{ pageSize: 10 }}
          columns={COLLECT_FAILED_COLUMNS}
          dataSource={failed}
        />
      ) : finished ? (
        <Alert type="success" showIcon message="全部采集成功，没有失败项" />
      ) : null}

      <Divider style={{ margin: '16px 0 12px' }} />
      <Button onClick={onAgain}>继续粘贴下一条</Button>
    </>
  );
}

/**
 * P2 货源商品库。
 * 素材库（P3）已并入商品详情 Drawer：原始 / 重构 Tab + 版本切换 + 批量下载。
 */
export default function SourceProducts(): JSX.Element {
  const queryClient = useQueryClient();
  const { filters, params, setFilters, tablePagination, onTableChange, resetFilters } =
    usePagination<SourceFilterValues>(DEFAULT_FILTERS);
  const platformOptions = useEnumOptions('Platform');
  const supplierStatusOptions = useEnumOptions('SourceProductStatus');

  const [detailId, setDetailId] = useState<number | null>(null);
  const [collectOpen, setCollectOpen] = useState<boolean>(false);
  const [aiOpen, setAiOpen] = useState<boolean>(false);
  const [importOpen, setImportOpen] = useState<boolean>(false);
  const [manualOpen, setManualOpen] = useState<boolean>(false);
  /** 待上传的 CSV 文件（不走 Upload 自动上传，点「开始导入」才提交） */
  const [csvFile, setCsvFile] = useState<File | null>(null);
  /**
   * 本次采集对应的异步任务记录 ID。
   *
   * ★ `POST /source-products/collect` 是**无条件受理**的 202：链接能不能解析、
   *   1688 给不给权限，都得等异步任务跑完才知道。以前 UI 拿到 202 就弹「已受理」然后关窗口，
   *   结果是一条都搜不出来还不知道为啥 —— 静默失效。现在把任务号留下，弹窗里轮询到终态。
   */
  const [collectTaskId, setCollectTaskId] = useState<number | null>(null);
  /** CSV 导入逐行结果（保留在弹窗内供查看，不只用 toast 一闪而过） */
  const [importResult, setImportResult] = useState<CsvImportResultVo | null>(null);
  const [activeTab, setActiveTab] = useState<string>('products');
  const [supplierEdit, setSupplierEdit] = useState<SupplierVo | null>(null);
  const [supplierCreateOpen, setSupplierCreateOpen] = useState<boolean>(false);
  const [assetOrigin, setAssetOrigin] = useState<'raw' | 'ai_rework'>('raw');
  const [assetVersion, setAssetVersion] = useState<string>('');
  const [selectedAssetIds, setSelectedAssetIds] = useState<number[]>([]);

  /** 素材库 Tab（跨商品浏览）的独立翻页与筛选 */
  const [assetPage, setAssetPage] = useState<number>(1);
  const [assetPageSize, setAssetPageSize] = useState<number>(20);
  const [assetOriginFilter, setAssetOriginFilter] = useState<string>('');
  const [assetProductFilter, setAssetProductFilter] = useState<string>('');

  const [collectForm] = Form.useForm<CollectFormValues>();
  const [aiForm] = Form.useForm<AiTaskFormValues>();
  const [supplierForm] = Form.useForm<SupplierFormValues>();
  const [manualForm] = Form.useForm<ManualFormValues>();

  const listQuery = useQuery({
    queryKey: ['source-products', params],
    queryFn: () => listSourceProducts(params),
  });

  const supplierQuery = useQuery({
    queryKey: ['suppliers', 'options'],
    queryFn: () => listSuppliers({ page: 1, page_size: 200 }),
    staleTime: 300_000,
  });

  const detailQuery = useQuery({
    queryKey: ['source-products', detailId],
    queryFn: () => getSourceProduct(detailId as number),
    enabled: detailId !== null,
  });

  /** 素材库 Tab：跨商品浏览全部素材（AST-P0-03 版本回退的操作场所） */
  const assetsTabQuery = useQuery({
    queryKey: [
      'assets',
      'library',
      assetPage,
      assetPageSize,
      assetOriginFilter,
      assetProductFilter,
    ],
    queryFn: () =>
      listAssets({
        page: assetPage,
        page_size: assetPageSize,
        origin: assetOriginFilter || undefined,
        source_product_id: assetProductFilter || undefined,
      }),
    enabled: activeTab === 'assets',
  });

  const assetsQuery = useQuery({
    queryKey: ['assets', detailId, assetOrigin, assetVersion],
    queryFn: () =>
      listAssets({
        source_product_id: detailId as number,
        origin: assetOrigin,
        version: assetVersion || undefined,
        page: 1,
        page_size: 100,
      }),
    enabled: detailId !== null,
  });

  const collectMutation = useMutation({
    mutationFn: (identifiers: string[]) =>
      collectSourceProducts({ source: '1688', identifiers }),
    onSuccess: (data) => {
      message.success(`已受理 ${data.accepted} 个，正在后台采集（任务 #${data.task_record_id}）`);
      collectForm.resetFields();
      setCollectTaskId(data.task_record_id);
    },
  });

  /**
   * ★ 轮询采集进度与逐条结果（`GET /tasks/{id}`）。
   *   弹窗不关、结果不只用一句 toast 带走 —— 他要知道"哪条成了、哪条为什么没成"。
   */
  const collectTaskQuery = useQuery({
    queryKey: ['tasks', collectTaskId],
    queryFn: () => getTask(collectTaskId as number),
    enabled: collectTaskId !== null,
    refetchInterval: (query) => {
      const status = String(query.state.data?.status ?? '');
      return TERMINAL_TASK_STATUS.includes(status) ? false : 3_000;
    },
  });

  /** 采集任务跑到终态时刷新商品列表（进行中也刷意义不大，徒增请求） */
  useEffect(() => {
    const status = String(collectTaskQuery.data?.status ?? '');
    if (collectTaskId !== null && TERMINAL_TASK_STATUS.includes(status)) {
      void queryClient.invalidateQueries({ queryKey: ['source-products'] });
    }
  }, [collectTaskId, collectTaskQuery.data?.status, queryClient]);

  /**
   * CSV 批量导入。
   *
   * ★ 结果必须逐行展示：后端是「合法行入库、非法行跳过」，
   *   全批次不回滚，只回一句"导入完成"会让使用者以为都成功了。
   *   失败行带行号 + 中文原因（如"第4行：商品成本价「十五块」不是合法金额"）。
   */
  const importCsvMutation = useMutation({
    mutationFn: (file: File) => importSourceProductsCsv(file),
    onSuccess: (data) => {
      setImportResult(data);
      setCsvFile(null);
      void queryClient.invalidateQueries({ queryKey: ['source-products'] });
      if (data.failed.length === 0) {
        message.success(`导入完成：新增 ${data.created} / 更新 ${data.updated} 个商品，无失败行`);
      } else {
        message.warning(`导入完成：新增 ${data.created} / 更新 ${data.updated}，${data.failed.length} 行失败（见下方明细）`);
      }
    },
  });

  /** 手工录入（同步返回，201） */
  const manualMutation = useMutation({
    mutationFn: (body: ManualFormValues) =>
      createSourceProductManual({
        title: body.title,
        product_code: body.product_code || undefined,
        category_path: body.category_path || undefined,
        supplier_id: body.supplier_id ?? undefined,
        cost_price: body.cost_price || undefined,
        origin_url: body.origin_url || undefined,
        main_image_url: body.main_image_url || undefined,
        skus: (body.skus ?? []).map((sku) => ({
          spec_name: sku.spec_name || undefined,
          spec_value: sku.spec_value || undefined,
          sku_code: sku.sku_code || undefined,
          cost_price: sku.cost_price || undefined,
          sale_price: sku.sale_price || undefined,
          stock_qty: sku.stock_qty ?? 0,
        })),
      }),
    onSuccess: (data) => {
      message.success(
        `已录入「${data.title}」（${data.created ? '新建' : '已存在'}，SKU ${data.created_skus} 个）`,
      );
      setManualOpen(false);
      manualForm.resetFields();
      void queryClient.invalidateQueries({ queryKey: ['source-products'] });
    },
  });

  const aiMutation = useMutation({
    mutationFn: (body: { source_product_ids: number[]; target_platform: string; rework_items: string[] }) =>
      createAiTasks(body),
    onSuccess: (data) => {
      message.success(`已创建 ${data.task_ids.length} 个 AI 重构任务`);
      setAiOpen(false);
      aiForm.resetFields();
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => deleteSourceProduct(id),
    onSuccess: () => {
      message.success('已软删除');
      void queryClient.invalidateQueries({ queryKey: ['source-products'] });
    },
  });

  const batchDownloadMutation = useMutation({
    mutationFn: (ids: number[]) => batchDownloadAssets(ids),
    onSuccess: (data) => {
      triggerDownload(data.download_url);
      message.success('素材包已开始下载');
    },
  });

  const supplierCreateMutation = useMutation({
    mutationFn: (body: SupplierFormValues) =>
      createSupplier({
        supplier_1688_id: body.supplier_1688_id,
        name: body.name,
        location: body.location || undefined,
        lead_time_hours: body.lead_time_hours,
        moq: body.moq,
        cooperation_score: body.cooperation_score,
      }),
    onSuccess: () => {
      message.success('供应商已创建');
      setSupplierCreateOpen(false);
      supplierForm.resetFields();
      void queryClient.invalidateQueries({ queryKey: ['suppliers'] });
    },
  });

  const supplierUpdateMutation = useMutation({
    mutationFn: (body: { id: number; values: SupplierFormValues }) =>
      updateSupplier(body.id, {
        supplier_1688_id: body.values.supplier_1688_id,
        name: body.values.name,
        location: body.values.location || undefined,
        lead_time_hours: body.values.lead_time_hours,
        moq: body.values.moq,
        cooperation_score: body.values.cooperation_score,
      }),
    onSuccess: () => {
      message.success('供应商已更新');
      setSupplierEdit(null);
      supplierForm.resetFields();
      void queryClient.invalidateQueries({ queryKey: ['suppliers'] });
    },
  });

  const supplierDeleteMutation = useMutation({
    mutationFn: (id: number) => deleteSupplier(id),
    onSuccess: () => {
      message.success('供应商已软删除');
      void queryClient.invalidateQueries({ queryKey: ['suppliers'] });
    },
  });

  const rollbackMutation = useMutation({
    mutationFn: ({ id, reason }: { id: number; reason: string }) => rollbackAsset(id, reason),
    onSuccess: () => {
      message.success('已回滚到该版本');
      void queryClient.invalidateQueries({ queryKey: ['assets'] });
    },
  });

  const versionOptions = useMemo(() => {
    const versions = new Set<number>();
    (assetsQuery.data?.items ?? []).forEach((item) => versions.add(item.version));
    return Array.from(versions)
      .sort((a, b) => b - a)
      .map((version) => ({ label: `v${version}`, value: String(version) }));
  }, [assetsQuery.data]);

  const columns = useMemo<ColumnsType<SourceProductVo>>(
    () => [
      { title: 'ID', dataIndex: 'id', width: 70 },
      { title: '1688 商品 ID', dataIndex: 'product_1688_id', width: 140, className: 'erp-mono' },
      {
        title: '标题',
        dataIndex: 'title',
        ellipsis: true,
        render: (value: string, record) => (
          <Space size={4}>
            <Typography.Link onClick={() => setDetailId(record.id)}>{value}</Typography.Link>
          </Space>
        ),
      },
      { title: '供应商', dataIndex: 'supplier_name', width: 140, render: (v?: string) => v ?? '-' },
      {
        title: '成本价',
        dataIndex: 'cost_price',
        width: 100,
        render: (value: string) => <MoneyText value={value} />,
      },
      {
        title: '库存状态',
        dataIndex: 'stock_status',
        width: 100,
        render: (value: string) => <StatusTag enumKey="StockStatus" value={value} />,
      },
      {
        title: '状态',
        dataIndex: 'status',
        width: 100,
        render: (value: string) => <StatusTag enumKey="SourceProductStatus" value={value} />,
      },
      { title: 'SKU 数', dataIndex: 'sku_count', width: 80 },
      {
        title: '采集时间',
        dataIndex: 'collected_at',
        width: 170,
        render: (value: string | null) => formatTime(value),
      },
      {
        title: '操作',
        key: 'actions',
        width: 190,
        fixed: 'right',
        render: (_value: unknown, record) => (
          <Space size="small">
            <Typography.Link onClick={() => setDetailId(record.id)}>详情/素材</Typography.Link>
            <Typography.Link
              onClick={() => {
                setDetailId(record.id);
                setAiOpen(true);
                aiForm.setFieldsValue({ target_platform: platformOptions[0]?.value });
              }}
            >
              AI 重构
            </Typography.Link>
            <Typography.Link
              onClick={() => {
                Modal.confirm({
                  title: '确认软删除该货源商品？',
                  content: '软删除后可在保留期内回滚，关联的映射需人工确认。',
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
    [aiForm, deleteMutation, platformOptions],
  );

  const assetColumns = useMemo<ColumnsType<AssetVo>>(
    () => [
      {
        title: '选择',
        key: 'check',
        width: 48,
        render: (_value: unknown, record) => (
          <Checkbox
            checked={selectedAssetIds.includes(record.id)}
            onChange={(event) => {
              const checked = event.target.checked;
              setSelectedAssetIds((prev) =>
                checked ? [...prev, record.id] : prev.filter((id) => id !== record.id),
              );
            }}
          />
        ),
      },
      {
        title: '预览',
        dataIndex: 'preview_url',
        width: 90,
        render: (value: string | null) =>
          value ? <Image src={value} width={56} height={56} style={{ objectFit: 'cover' }} /> : '-',
      },
      { title: '类型', dataIndex: 'asset_type', width: 100 },
      { title: '版本', dataIndex: 'version', width: 70, render: (v: number) => `v${v}` },
      {
        title: '当前版本',
        dataIndex: 'is_current',
        width: 90,
        render: (value: boolean) => (value ? <Tag color="green">当前</Tag> : <Tag>历史</Tag>),
      },
      {
        title: '尺寸',
        key: 'size',
        width: 120,
        render: (_value: unknown, record) =>
          record.width && record.height ? `${record.width}×${record.height}` : '-',
      },
      {
        title: '标签',
        dataIndex: 'tags',
        render: (tags: string[]) =>
          tags && tags.length > 0 ? tags.map((tag) => <Tag key={tag}>{tag}</Tag>) : '-',
      },
      {
        title: '操作',
        key: 'actions',
        width: 150,
        render: (_value: unknown, record) => (
          <Space size="small">
            <Typography.Link onClick={() => triggerDownload(assetDownloadUrl(record.id))}>
              下载
            </Typography.Link>
            {record.is_current ? null : (
              <Typography.Link onClick={() => rollbackMutation.mutate({ id: record.id, reason: '人工回滚' })}>
                回滚
              </Typography.Link>
            )}
          </Space>
        ),
      },
    ],
    [rollbackMutation, selectedAssetIds],
  );

  const skuColumns = useMemo<ColumnsType<SourceSkuVo>>(
    () => [
      { title: 'SKU ID', dataIndex: 'id', width: 80 },
      { title: '1688 SKU 编码', dataIndex: 'sku_code_1688', width: 160, className: 'erp-mono' },
      { title: '规格', dataIndex: 'sku_name', render: (v?: string) => v ?? '-' },
      {
        title: '成本',
        dataIndex: 'cost_price',
        width: 100,
        render: (value: string) => <MoneyText value={value} />,
      },
      { title: '库存', dataIndex: 'stock_qty', width: 80, render: (v?: number | null) => v ?? '-' },
      {
        title: '规格指纹',
        dataIndex: 'spec_signature',
        width: 160,
        className: 'erp-mono',
        render: (v?: string | null) => v ?? '-',
      },
      {
        title: '状态',
        dataIndex: 'status',
        width: 90,
        render: (value: string) => <StatusTag enumKey="SourceProductStatus" value={value} />,
      },
    ],
    [],
  );

  const supplierColumns = useMemo<ColumnsType<SupplierVo>>(
    () => [
      { title: 'ID', dataIndex: 'id', width: 70 },
      { title: '1688 供应商 ID', dataIndex: 'supplier_1688_id', width: 160, className: 'erp-mono' },
      { title: '名称', dataIndex: 'name', ellipsis: true },
      { title: '所在地', dataIndex: 'location', width: 140, render: (v?: string | null) => v ?? '-' },
      {
        title: '发货时效（小时）',
        dataIndex: 'lead_time_hours',
        width: 140,
        render: (v?: number | null) => v ?? '-',
      },
      { title: '起订量', dataIndex: 'moq', width: 90, render: (v?: number | null) => v ?? '-' },
      {
        title: '合作评分',
        dataIndex: 'cooperation_score',
        width: 100,
        render: (v?: number | null) => v ?? '-',
      },
      { title: '状态', dataIndex: 'status', width: 90 },
      {
        title: '操作',
        key: 'actions',
        width: 140,
        render: (_value: unknown, record) => (
          <Space size="small">
            <Typography.Link
              onClick={() => {
                setSupplierEdit(record);
                setSupplierCreateOpen(true);
                supplierForm.setFieldsValue({
                  supplier_1688_id: record.supplier_1688_id,
                  name: record.name,
                  location: record.location ?? '',
                  lead_time_hours: record.lead_time_hours ?? 0,
                  moq: record.moq ?? 0,
                  cooperation_score: record.cooperation_score ?? 0,
                });
              }}
            >
              编辑
            </Typography.Link>
            <Typography.Link
              onClick={() =>
                Modal.confirm({
                  title: '确认删除该供应商？',
                  content: '软删除后其历史商品与映射仍保留，仅不再用于新采集。',
                  okText: '确认删除',
                  okButtonProps: { danger: true },
                  cancelText: '取消',
                  onOk: () => supplierDeleteMutation.mutate(record.id),
                })
              }
            >
              删除
            </Typography.Link>
          </Space>
        ),
      },
    ],
    [supplierDeleteMutation, supplierForm],
  );

  /** 素材库 Tab 的列：在详情抽屉的列基础上补「货源商品」列，便于跨商品浏览 */
  const libraryAssetColumns = useMemo<ColumnsType<AssetVo>>(() => {
    const productColumn: ColumnsType<AssetVo> = [
      {
        title: '货源商品 ID',
        dataIndex: 'source_product_id',
        width: 120,
        render: (value: number) => (
          <Typography.Link onClick={() => setDetailId(value)}>#{value}</Typography.Link>
        ),
      },
    ];
    return [...productColumn, ...assetColumns];
  }, [assetColumns]);

  const detail = detailQuery.data;

  return (
    <PageContainer
      title="货源商品库"
      subTitle="1688 采集入库的商品与素材；素材库为独立 Tab（跨商品浏览 / 版本回滚 / 批量下载），商品详情抽屉内只展示该商品素材子集"
      loading={listQuery.isLoading}
      extra={
        <Space wrap>
          <Button
            type="primary"
            icon={<UploadOutlined />}
            onClick={() => {
              setImportResult(null);
              setCsvFile(null);
              setImportOpen(true);
            }}
          >
            批量导入 CSV
          </Button>
          <Button
            icon={<PlusOutlined />}
            onClick={() => {
              manualForm.resetFields();
              manualForm.setFieldsValue({
                skus: [{ spec_name: '', spec_value: '', sku_code: '', stock_qty: 0 }],
              });
              setManualOpen(true);
            }}
          >
            手工录入
          </Button>
          <Button onClick={() => setCollectOpen(true)}>采集商品（1688）</Button>
          <Button onClick={resetFilters}>重置筛选</Button>
        </Space>
      }
    >
      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={[
          {
            key: 'products',
            label: '货源商品',
            children: (
              <>
      <Form
        layout="inline"
        style={{ marginBottom: 12, rowGap: 8 }}
        initialValues={filters}
        onFinish={(values: SourceFilterValues) => {
          setFilters({
            keyword: values.keyword,
            supplier_id: values.supplier_id,
            status: values.status,
          });
        }}
      >
        <Form.Item name="keyword" label="关键词">
          <Input allowClear placeholder="标题 / 1688 商品 ID" style={{ width: 200 }} />
        </Form.Item>
        <Form.Item name="supplier_id" label="供应商">
          <Select
            allowClear
            placeholder="全部"
            style={{ width: 180 }}
            options={(supplierQuery.data?.items ?? []).map((item) => ({
              label: item.name,
              value: String(item.id),
            }))}
          />
        </Form.Item>
        <Form.Item name="status" label="状态">
          <Select
            allowClear
            placeholder="全部"
            style={{ width: 140 }}
            options={supplierStatusOptions}
          />
        </Form.Item>
        <Form.Item>
          <Space>
            <Button type="primary" htmlType="submit">
              查询
            </Button>
          </Space>
        </Form.Item>
      </Form>

      <Table<SourceProductVo>
        rowKey="id"
        size="small"
        scroll={{ x: 1200 }}
        columns={columns}
        dataSource={listQuery.data?.items ?? []}
        pagination={{ ...tablePagination, total: listQuery.data?.total ?? 0 }}
        onChange={onTableChange}
      />
              </>
            ),
          },
          {
            key: 'assets',
            label: '素材库',
            children: (
              <>
                <Space style={{ marginBottom: 12 }} wrap>
                  <Select
                    allowClear
                    value={assetOriginFilter || undefined}
                    placeholder="来源：全部"
                    style={{ width: 160 }}
                    onChange={(value?: string) => {
                      setAssetOriginFilter(value ?? '');
                      setAssetPage(1);
                    }}
                    options={[
                      { label: '原始素材', value: 'raw' },
                      { label: 'AI 重构素材', value: 'ai_rework' },
                    ]}
                  />
                  <Input
                    allowClear
                    value={assetProductFilter}
                    placeholder="按货源商品 ID 过滤"
                    style={{ width: 180 }}
                    onChange={(event) => {
                      setAssetProductFilter(event.target.value);
                      setAssetPage(1);
                    }}
                  />
                  <Button
                    icon={<CloudDownloadOutlined />}
                    disabled={selectedAssetIds.length === 0}
                    loading={batchDownloadMutation.isPending}
                    onClick={() => batchDownloadMutation.mutate(selectedAssetIds)}
                  >
                    批量下载（{selectedAssetIds.length}）
                  </Button>
                </Space>
                <Table<AssetVo>
                  rowKey="id"
                  size="small"
                  scroll={{ x: 1300 }}
                  columns={libraryAssetColumns}
                  dataSource={assetsTabQuery.data?.items ?? []}
                  pagination={{
                    current: assetPage,
                    pageSize: assetPageSize,
                    total: assetsTabQuery.data?.total ?? 0,
                    showSizeChanger: true,
                    pageSizeOptions: ['20', '50', '100'],
                    onChange: (nextPage, nextSize) => {
                      setAssetPage(nextPage);
                      setAssetPageSize(nextSize);
                    },
                  }}
                />
                <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8 }}>
                  素材按内容哈希去重归档，支持版本回滚（AST-P0-03）：这里可跨商品浏览与批量下载，
                  单个商品的素材子集在「货源商品 → 详情/素材」抽屉内查看。
                </Typography.Paragraph>
              </>
            ),
          },
          {
            key: 'suppliers',
            label: `供应商（${supplierQuery.data?.total ?? 0}）`,
            children: (
              <>
                <Space style={{ marginBottom: 12 }}>
                  <Button
                    type="primary"
                    onClick={() => {
                      setSupplierEdit(null);
                      supplierForm.resetFields();
                      setSupplierCreateOpen(true);
                    }}
                  >
                    新增供应商
                  </Button>
                </Space>
                <Table<SupplierVo>
                  rowKey="id"
                  size="small"
                  scroll={{ x: 1100 }}
                  columns={supplierColumns}
                  dataSource={supplierQuery.data?.items ?? []}
                  pagination={false}
                />
              </>
            ),
          },
        ]}
      />

      {/* 采集弹窗（★ 走 1688 开放接口，未配置 AppKey / AccessToken 时必然 0 条） */}
      <Modal
        title={collectTaskId === null ? '采集 1688 商品（粘贴链接或商品 ID）' : '采集任务进展'}
        open={collectOpen}
        width={860}
        onCancel={() => {
          setCollectOpen(false);
          setCollectTaskId(null);
        }}
        onOk={() => {
          if (collectTaskId === null) collectForm.submit();
          else {
            setCollectOpen(false);
            setCollectTaskId(null);
          }
        }}
        confirmLoading={collectMutation.isPending}
        okText={collectTaskId === null ? '提交采集' : '关闭'}
        cancelText="取消"
        destroyOnHidden
      >
        {collectTaskId === null ? (
          <>
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 12 }}
              message="采集依赖 1688 开放接口资质"
              description="提交后能否真的拿到数据，要看 1688 是否给这个应用开通了「商品详情」接口权限。没开通时，「采集任务进展」会逐条列出失败原因并说明该去哪里申请；短期内请改用「批量导入 CSV」或「手工录入」，两者不依赖外部平台。"
            />
            <Form form={collectForm} layout="vertical" onFinish={(values: CollectFormValues) => {
              const identifiers = values.identifiers
                .split(/[\n,\s]+/)
                .map((item) => item.trim())
                .filter(Boolean);
              if (identifiers.length === 0) {
                message.warning('请至少填写 1 个商品 ID 或链接');
                return;
              }
              collectMutation.mutate(identifiers.slice(0, 50));
            }}>
              <Form.Item
                name="identifiers"
                label="1688 商品链接或商品 ID"
                extra={
                  <Space direction="vertical" size={2}>
                    <span>直接粘贴 1688 商品详情页链接即可，例如 https://detail.1688.com/offer/123456.html</span>
                    <span>支持换行 / 逗号分隔，单次最多 50 个；也可以只填纯数字商品 ID</span>
                  </Space>
                }
                rules={[{ required: true, message: '请填写商品链接或商品 ID' }]}
              >
                <Input.TextArea rows={6} placeholder="https://detail.1688.com/offer/123456.html" />
              </Form.Item>
            </Form>
          </>
        ) : (
          <CollectTaskResult task={collectTaskQuery.data} onAgain={() => setCollectTaskId(null)} />
        )}
      </Modal>

      {/* CSV 批量导入（★ 主入口；逐行回执） */}
      <Modal
        title="批量导入货源商品（CSV）"
        open={importOpen}
        onCancel={() => setImportOpen(false)}
        onOk={() => {
          if (!csvFile) {
            message.warning('请先选择 CSV 文件');
            return;
          }
          importCsvMutation.mutate(csvFile);
        }}
        confirmLoading={importCsvMutation.isPending}
        okText="开始导入"
        cancelText="关闭"
        okButtonProps={{ disabled: csvFile === null }}
        width={760}
        destroyOnHidden
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="先下载模板，按模板填写"
          description={
            <Space direction="vertical" size={4}>
              <span>
                列顺序：商品编码 / 商品标题 / 类目 / 供应商ID / 商品成本价 / 原链接 / 主图URL /
                商品状态 / SKU编码 / 规格名 / 规格值 / SKU成本价 / 售价 / 库存 / 备注
              </span>
              <span>
                同一商品的多个 SKU 写多行，<strong>商品编码相同即可</strong>；多规格用英文分号分隔
                （规格名「颜色;尺码」对应规格值「红色;XL」）；金额填数字，如 29.90。
              </span>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                权威模板：backend/scripts/source_products_template.csv（UTF-8 带 BOM）
              </Typography.Text>
            </Space>
          }
          action={
            <Button size="small" icon={<DownloadOutlined />} onClick={downloadCsvTemplate}>
              下载模板
            </Button>
          }
        />

        <Upload
          accept=".csv,text/csv"
          maxCount={1}
          beforeUpload={(file: File) => {
            setCsvFile(file);
            setImportResult(null);
            return false; // 阻止自动上传，点「开始导入」才提交
          }}
          onRemove={() => {
            setCsvFile(null);
            setImportResult(null);
          }}
          fileList={
            csvFile
              ? ([{ uid: '-1', name: csvFile.name, status: 'done' }] as UploadFile[])
              : []
          }
        >
          <Button icon={<UploadOutlined />}>选择 CSV 文件</Button>
        </Upload>

        {/* ★ 导入结果：汇总 + 逐行失败明细 */}
        {importResult ? (
          <>
            <Divider style={{ margin: '16px 0 12px' }} />
            <Space size={32} style={{ marginBottom: 12 }} wrap>
              <Statistic title="解析行数" value={importResult.total} />
              <Statistic
                title="新增商品"
                value={importResult.created}
                valueStyle={{ color: '#3f8600' }}
              />
              <Statistic
                title="更新商品"
                value={importResult.updated}
                valueStyle={{ color: '#1677ff' }}
              />
              <Statistic title="新增 SKU" value={importResult.created_skus} />
              <Statistic
                title="失败行"
                value={importResult.failed.length}
                valueStyle={{
                  color: importResult.failed.length > 0 ? '#cf1322' : undefined,
                }}
              />
            </Space>
            {importResult.failed.length > 0 ? (
              <>
                <Alert
                  type="error"
                  showIcon
                  style={{ marginBottom: 8 }}
                  message={`${importResult.failed.length} 行未能导入（合法行已入库，失败行已跳过）`}
                  description="请按下表行号回到 CSV 修正后重新导入；已成功的行不会重复创建（同商品编码会更新）。"
                />
                <Table<CsvImportFailedRowVo>
                  rowKey={(record) => `${record.row}-${record.identifier}-${record.reason}`}
                  size="small"
                  pagination={{ pageSize: 10 }}
                  columns={CSV_FAILED_COLUMNS}
                  dataSource={importResult.failed}
                />
              </>
            ) : (
              <Alert type="success" showIcon message="全部行导入成功，无失败行" />
            )}
          </>
        ) : null}
      </Modal>

      {/* 手工录入（同步返回，201） */}
      <Modal
        title="手工录入货源商品"
        open={manualOpen}
        onCancel={() => setManualOpen(false)}
        onOk={() => manualForm.submit()}
        confirmLoading={manualMutation.isPending}
        okText="保存"
        cancelText="取消"
        width={860}
        destroyOnHidden
      >
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="SKU 必填"
          description="货源商品没有 SKU 就建不了映射、上不了架，所以至少要填一个 SKU：可直接填「SKU编码」，或填「规格名 / 规格值」由系统自动生成编码。"
        />
        <Form
          form={manualForm}
          layout="vertical"
          onFinish={(values: ManualFormValues) => manualMutation.mutate(values)}
        >
          <Form.Item name="title" label="商品标题" rules={[{ required: true }]}>
            <Input placeholder="如：纯棉圆领短袖T恤 夏季薄款" />
          </Form.Item>
          <Space size={12} wrap style={{ display: 'flex' }}>
            <Form.Item
              name="product_code"
              label="商品编码"
              style={{ minWidth: 220 }}
              extra="留空自动生成；存入前加 MANUAL- 前缀"
            >
              <Input placeholder="如：DEMO-1001" />
            </Form.Item>
            <Form.Item name="category_path" label="类目" style={{ minWidth: 220 }}>
              <Input placeholder="如：女装/上装/T恤" />
            </Form.Item>
            <Form.Item name="supplier_id" label="供应商" style={{ minWidth: 200 }}>
              <Select
                allowClear
                placeholder="全部"
                options={(supplierQuery.data?.items ?? []).map((item) => ({
                  label: item.name,
                  value: item.id,
                }))}
              />
            </Form.Item>
            <Form.Item name="cost_price" label="商品成本价（元）" style={{ minWidth: 160 }}>
              <Input placeholder="如：18.00" />
            </Form.Item>
          </Space>
          <Space size={12} wrap style={{ display: 'flex' }}>
            <Form.Item name="origin_url" label="原链接" style={{ minWidth: 320 }}>
              <Input placeholder="https://..." />
            </Form.Item>
            <Form.Item name="main_image_url" label="主图 URL" style={{ minWidth: 320 }}>
              <Input placeholder="https://..." />
            </Form.Item>
          </Space>

          <Divider orientation="left" plain style={{ margin: '8px 0' }}>
            SKU 列表
          </Divider>
          <Form.List
            name="skus"
            rules={[
              {
                validator: async (_rule, skus: unknown[]) => {
                  if (!Array.isArray(skus) || skus.length === 0) {
                    throw new Error('请至少填写 1 个 SKU');
                  }
                },
              },
            ]}
          >
            {(fields, { add, remove }, { errors }) => (
              <>
                {fields.map((field) => (
                  <Space
                    key={field.key}
                    size={8}
                    wrap
                    align="baseline"
                    style={{ display: 'flex', marginBottom: 8 }}
                  >
                    <Form.Item name={[field.name, 'spec_name']} style={{ marginBottom: 0 }}>
                      <Input placeholder="规格名（颜色;尺码）" style={{ width: 150 }} />
                    </Form.Item>
                    <Form.Item name={[field.name, 'spec_value']} style={{ marginBottom: 0 }}>
                      <Input placeholder="规格值（红色;XL）" style={{ width: 150 }} />
                    </Form.Item>
                    <Form.Item name={[field.name, 'sku_code']} style={{ marginBottom: 0 }}>
                      <Input placeholder="SKU编码（可留空）" style={{ width: 160 }} />
                    </Form.Item>
                    <Form.Item name={[field.name, 'cost_price']} style={{ marginBottom: 0 }}>
                      <Input placeholder="成本价" style={{ width: 100 }} />
                    </Form.Item>
                    <Form.Item name={[field.name, 'sale_price']} style={{ marginBottom: 0 }}>
                      <Input placeholder="售价" style={{ width: 100 }} />
                    </Form.Item>
                    <Form.Item name={[field.name, 'stock_qty']} style={{ marginBottom: 0 }}>
                      <InputNumber placeholder="库存" style={{ width: 90 }} min={0} />
                    </Form.Item>
                    {fields.length > 1 ? (
                      <Typography.Link onClick={() => remove(field.name)}>删除</Typography.Link>
                    ) : null}
                  </Space>
                ))}
                <Button
                  type="dashed"
                  block
                  icon={<PlusOutlined />}
                  onClick={() =>
                    add({ spec_name: '', spec_value: '', sku_code: '', stock_qty: 0 })
                  }
                >
                  添加 SKU
                </Button>
                <Form.ErrorList errors={errors} />
              </>
            )}
          </Form.List>
        </Form>
      </Modal>

      {/* AI 重构弹窗 */}
      <Modal
        title="发起 AI 重构任务"
        open={aiOpen}
        onCancel={() => setAiOpen(false)}
        onOk={() => aiForm.submit()}
        confirmLoading={aiMutation.isPending}
        okText="提交"
        cancelText="取消"
      >
        <Form form={aiForm} layout="vertical" onFinish={(values: AiTaskFormValues) => {
          if (detailId === null) return;
          aiMutation.mutate({
            source_product_ids: [detailId],
            target_platform: values.target_platform,
            rework_items: values.rework_items,
          });
        }}>
          <Form.Item name="target_platform" label="目标平台" rules={[{ required: true }]}>
            <Select options={platformOptions} placeholder="请选择目标平台" />
          </Form.Item>
          <Form.Item name="rework_items" label="重构项" rules={[{ required: true }]}>
            <Select mode="multiple" options={REWORK_ITEM_OPTIONS} placeholder="请选择重构项" />
          </Form.Item>
        </Form>
      </Modal>

      {/* 商品详情（含素材库） */}
      <Drawer
        width={1000}
        title={detail ? `商品详情 · ${detail.title}` : '商品详情'}
        open={detailId !== null}
        onClose={() => setDetailId(null)}
      >
        {detail ? (
          <>
            <Descriptions bordered size="small" column={2} style={{ marginBottom: 16 }}>
              <Descriptions.Item label="1688 商品 ID">{detail.product_1688_id}</Descriptions.Item>
              <Descriptions.Item label="供应商">{detail.supplier_name ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="类目">{detail.category_path ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="成本价">
                <MoneyText value={detail.cost_price} />
              </Descriptions.Item>
              <Descriptions.Item label="库存状态">
                <StatusTag enumKey="StockStatus" value={detail.stock_status} />
              </Descriptions.Item>
              <Descriptions.Item label="状态">
                <StatusTag enumKey="SourceProductStatus" value={detail.status} />
              </Descriptions.Item>
              <Descriptions.Item label="采集时间">{formatTime(detail.collected_at)}</Descriptions.Item>
              <Descriptions.Item label="原始链接">
                {detail.origin_url ? (
                  <Typography.Link href={detail.origin_url} target="_blank">
                    打开 1688 页面
                  </Typography.Link>
                ) : (
                  '-'
                )}
              </Descriptions.Item>
            </Descriptions>

            <Tabs
              defaultActiveKey="skus"
              items={[
                {
                  key: 'skus',
                  label: 'SKU 列表',
                  children: (
                    <Table<SourceSkuVo>
                      rowKey="id"
                      size="small"
                      columns={skuColumns}
                      dataSource={detail.skus}
                      pagination={false}
                    />
                  ),
                },
                {
                  key: 'assets',
                  label: '素材库',
                  children: (
                    <>
                      <Space style={{ marginBottom: 12 }} wrap>
                        <Select
                          value={assetOrigin}
                          style={{ width: 160 }}
                          onChange={(value: 'raw' | 'ai_rework') => setAssetOrigin(value)}
                          options={[
                            { label: '原始素材', value: 'raw' },
                            { label: 'AI 重构素材', value: 'ai_rework' },
                          ]}
                        />
                        <Select
                          allowClear
                          value={assetVersion || undefined}
                          placeholder="全部版本"
                          style={{ width: 140 }}
                          onChange={(value?: string) => setAssetVersion(value ?? '')}
                          options={versionOptions}
                        />
                        <Button
                          icon={<CloudDownloadOutlined />}
                          disabled={selectedAssetIds.length === 0}
                          loading={batchDownloadMutation.isPending}
                          onClick={() => batchDownloadMutation.mutate(selectedAssetIds)}
                        >
                          批量下载（{selectedAssetIds.length}）
                        </Button>
                      </Space>
                      <Table<AssetVo>
                        rowKey="id"
                        size="small"
                        columns={assetColumns}
                        dataSource={assetsQuery.data?.items ?? []}
                        pagination={false}
                      />
                    </>
                  ),
                },
                {
                  key: 'params',
                  label: '商品参数',
                  children: detail.params_json ? (
                    <Descriptions bordered size="small" column={1}>
                      {Object.entries(detail.params_json).map(([key, value]) => (
                        <Descriptions.Item key={key} label={key}>
                          {String(value)}
                        </Descriptions.Item>
                      ))}
                    </Descriptions>
                  ) : (
                    <Typography.Text type="secondary">暂无参数</Typography.Text>
                  ),
                },
              ]}
            />
          </>
        ) : null}
      </Drawer>

      {/* 新增 / 编辑供应商 */}
      <Modal
        title={supplierEdit ? `编辑供应商 #${supplierEdit.id}` : '新增供应商'}
        open={supplierCreateOpen}
        onCancel={() => {
          setSupplierCreateOpen(false);
          setSupplierEdit(null);
        }}
        onOk={() => supplierForm.submit()}
        confirmLoading={supplierCreateMutation.isPending || supplierUpdateMutation.isPending}
        okText="保存"
        cancelText="取消"
        destroyOnHidden
      >
        <Form
          form={supplierForm}
          layout="vertical"
          onFinish={(values: SupplierFormValues) => {
            if (supplierEdit) {
              supplierUpdateMutation.mutate({ id: supplierEdit.id, values });
            } else {
              supplierCreateMutation.mutate(values);
            }
          }}
        >
          <Form.Item name="supplier_1688_id" label="1688 供应商 ID" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="name" label="供应商名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="location" label="所在地">
            <Input placeholder="如 浙江 杭州" />
          </Form.Item>
          <Form.Item name="lead_time_hours" label="发货时效（小时）">
            <InputNumber min={0} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="moq" label="起订量">
            <InputNumber min={0} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="cooperation_score" label="合作评分（0-100）">
            <InputNumber min={0} max={100} style={{ width: '100%' }} />
          </Form.Item>
        </Form>
      </Modal>
    </PageContainer>
  );
}
