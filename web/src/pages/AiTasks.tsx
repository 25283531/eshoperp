import { useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import {
  Alert,
  Button,
  Col,
  Descriptions,
  Divider,
  Drawer,
  Empty,
  Form,
  Image,
  Input,
  InputNumber,
  Modal,
  Row,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { Link } from 'react-router-dom';
import { SettingOutlined, FullscreenExitOutlined, FullscreenOutlined, ThunderboltOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  cancelAiTask,
  getAiConcurrency,
  getAiTask,
  listAiTasks,
  reviewAiTask,
  retryAiTask,
  updateAiConcurrency,
} from '@/api/ai';
import { getSourceProduct } from '@/api/catalog';
import type { AiTaskVo, BannedWordVo } from '@/api/types';
import PageContainer from '@/components/PageContainer';
import StatusTag from '@/components/StatusTag';
import { TitleCandidatesPanel, VideoScriptPanel } from '@/components/AiResultPanels';
import { formatTime } from '@/components/AuditTimeline';
import { useEnumOptions } from '@/hooks/useEnumOptions';
import { usePagination } from '@/hooks/usePagination';

interface AiTaskFilterValues {
  status: string;
  target_platform: string;
  source_product_id: string;
}

interface ReviewFormValues {
  note: string;
  edited_title: string;
  edited_selling_points: string;
}

interface ConcurrencyFormValues {
  max_concurrency: number;
  max_retry: number;
}

const DEFAULT_FILTERS: AiTaskFilterValues = {
  status: '',
  target_platform: '',
  source_product_id: '',
};

/** 违禁词高亮：命中的词加红色下划线标记 */
function highlightBanned(text: string | null | undefined, words: BannedWordVo[]): ReactNode {
  if (!text) return '-';
  const list = words.map((item) => item.word).filter(Boolean);
  if (list.length === 0) return text;
  const escaped = list.map((word) => word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  const regex = new RegExp(`(${escaped.join('|')})`, 'g');
  return text.split(regex).map((part, index) =>
    list.includes(part) ? (
      <span key={`${part}-${index}`} className="erp-banned-word">
        {part}
      </span>
    ) : (
      <span key={`${part}-${index}`}>{part}</span>
    ),
  );
}

/**
 * P4 AI 重构任务队列。
 * P5 重构审核详情已并入任务 Drawer：
 * 原图 vs 重绘图对比 / 标题原文 vs 新文 / 违禁词高亮 / 属性填充表 / 通过·打回·编辑。
 */
export default function AiTasks(): JSX.Element {
  const queryClient = useQueryClient();
  const { filters, params, setFilters, tablePagination, onTableChange, resetFilters } =
    usePagination<AiTaskFilterValues>(DEFAULT_FILTERS);
  const statusOptions = useEnumOptions('AiTaskStatus');
  const platformOptions = useEnumOptions('Platform');

  const [detailId, setDetailId] = useState<number | null>(null);
  const [editMode, setEditMode] = useState<boolean>(false);
  const [concurrencyOpen, setConcurrencyOpen] = useState<boolean>(false);
  /** 审核抽屉全屏切换：原图 vs 重绘图左右对比需要更宽的视口 */
  const [reviewFullscreen, setReviewFullscreen] = useState<boolean>(false);
  const [reviewForm] = Form.useForm<ReviewFormValues>();
  const [concurrencyForm] = Form.useForm<ConcurrencyFormValues>();

  const listQuery = useQuery({
    queryKey: ['ai-tasks', params],
    queryFn: () => listAiTasks(params),
    refetchInterval: 15_000,
  });

  const detailQuery = useQuery({
    queryKey: ['ai-tasks', detailId],
    queryFn: () => getAiTask(detailId as number),
    enabled: detailId !== null,
  });

  const concurrencyQuery = useQuery({
    queryKey: ['ai-tasks', 'concurrency-config'],
    queryFn: getAiConcurrency,
  });

  const reviewMutation = useMutation({
    mutationFn: (body: {
      id: number;
      action: 'approve' | 'reject' | 'edit';
      note?: string;
      edited?: { title?: string; selling_points?: string };
    }) =>
      reviewAiTask(body.id, {
        action: body.action,
        note: body.note,
        edited: body.edited,
      }),
    onSuccess: (_data, variables) => {
      message.success(variables.action === 'reject' ? '已打回' : '审核已通过');
      setEditMode(false);
      void queryClient.invalidateQueries({ queryKey: ['ai-tasks'] });
    },
  });

  const retryMutation = useMutation({
    mutationFn: (id: number) => retryAiTask(id),
    onSuccess: () => {
      message.success('已重新入队');
      void queryClient.invalidateQueries({ queryKey: ['ai-tasks'] });
    },
  });

  const cancelMutation = useMutation({
    mutationFn: (id: number) => cancelAiTask(id),
    onSuccess: () => {
      message.success('已取消任务');
      void queryClient.invalidateQueries({ queryKey: ['ai-tasks'] });
    },
  });

  const concurrencyMutation = useMutation({
    mutationFn: (body: ConcurrencyFormValues) => updateAiConcurrency(body),
    onSuccess: () => {
      message.success('并发配置已更新');
      setConcurrencyOpen(false);
      void queryClient.invalidateQueries({ queryKey: ['ai-tasks', 'concurrency-config'] });
    },
  });

  const columns = useMemo<ColumnsType<AiTaskVo>>(
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
        title: '任务类型',
        dataIndex: 'task_type',
        width: 130,
        render: (value: string) => <StatusTag enumKey="AiTaskType" value={value} />,
      },
      {
        title: '目标平台',
        dataIndex: 'target_platform',
        width: 100,
        render: (value: string) => <StatusTag enumKey="Platform" value={value} />,
      },
      {
        title: '重构项',
        dataIndex: 'rework_items',
        width: 200,
        render: (items: string[]) =>
          items && items.length > 0 ? items.map((item) => <Tag key={item}>{item}</Tag>) : '-',
      },
      {
        title: '状态',
        dataIndex: 'status',
        width: 110,
        render: (value: string) => <StatusTag enumKey="AiTaskStatus" value={value} />,
      },
      { title: '优先级', dataIndex: 'priority', width: 80 },
      { title: '重试次数', dataIndex: 'retry_count', width: 90 },
      {
        title: '耗时',
        dataIndex: 'duration_ms',
        width: 90,
        render: (value: number | null) => (value === null ? '-' : `${value} ms`),
      },
      { title: '创建人', dataIndex: 'created_by', width: 100, render: (v?: string) => v ?? '-' },
      {
        title: '创建时间',
        dataIndex: 'created_at',
        width: 170,
        render: (value: string) => formatTime(value),
      },
      {
        title: '操作',
        key: 'actions',
        width: 190,
        fixed: 'right',
        render: (_value: unknown, record) => (
          <Space size="small">
            <Typography.Link
              onClick={() => {
                setDetailId(record.id);
                setEditMode(false);
              }}
            >
              审核
            </Typography.Link>
            <Typography.Link onClick={() => retryMutation.mutate(record.id)}>重试</Typography.Link>
            <Typography.Link onClick={() => cancelMutation.mutate(record.id)}>取消</Typography.Link>
          </Space>
        ),
      },
    ],
    [cancelMutation, retryMutation],
  );

  const detail = detailQuery.data;
  const result = detail?.result ?? null;

  /**
   * ★ 原图取自货源商品详情，不取任务详情的 `assets[]`。
   *   后端 `GET /ai-tasks/{id}` 构造 AiTaskDetailVo 时 `assets` 是硬编码的 `[]`
   *   （见 backend/app/api/v1/ai_tasks.py 里 `AiTaskDetailVo(..., assets=[])` 那一行），
   *   照它渲染的话「原图」栏永远是「暂无原图」。
   */
  const sourceProductQuery = useQuery({
    queryKey: ['source-products', detail?.source_product_id],
    queryFn: () => getSourceProduct(detail?.source_product_id as number),
    enabled: typeof detail?.source_product_id === 'number',
  });
  const rawAssets = useMemo(
    () => (sourceProductQuery.data?.assets ?? []).filter((item) => item.origin === 'raw'),
    [sourceProductQuery.data],
  );
  const reworkedAssets = result?.output_assets ?? [];

  const attributeRows = useMemo(() => {
    if (!result?.output_attributes_json) return [];
    return Object.entries(result.output_attributes_json).map(([key, value], index) => ({
      key: `${key}-${index}`,
      attr_key: key,
      attr_value: typeof value === 'object' ? JSON.stringify(value) : String(value),
    }));
  }, [result]);

  return (
    <PageContainer
      title="AI 重构任务"
      subTitle="审核通过（approved）是上架引用素材的前置条件；未审核素材提交上架将被 422 / code 4005 硬拦截。要发起新的 AI 任务（图片重绘 / 标题建议 / 视频脚本）请去「AI 内容工作台」"
      loading={listQuery.isLoading}
      extra={
        <Space>
          <Link to="/ai-studio">
            <Button type="primary" icon={<ThunderboltOutlined />}>
              AI 内容工作台
            </Button>
          </Link>
          <Button
            icon={<SettingOutlined />}
            onClick={() => {
              setConcurrencyOpen(true);
              concurrencyForm.setFieldsValue({
                max_concurrency: concurrencyQuery.data?.max_concurrency ?? 5,
                max_retry: concurrencyQuery.data?.max_retry ?? 3,
              });
            }}
          >
            并发配置
          </Button>
          <Button onClick={resetFilters}>重置筛选</Button>
        </Space>
      }
    >
      <Form
        layout="inline"
        style={{ marginBottom: 12, rowGap: 8 }}
        initialValues={filters}
        onFinish={(values: AiTaskFilterValues) => setFilters(values)}
      >
        <Form.Item name="status" label="状态">
          <Select allowClear placeholder="全部" style={{ width: 150 }} options={statusOptions} />
        </Form.Item>
        <Form.Item name="target_platform" label="平台">
          <Select allowClear placeholder="全部" style={{ width: 130 }} options={platformOptions} />
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

      <Table<AiTaskVo>
        rowKey="id"
        size="small"
        scroll={{ x: 1530 }}
        columns={columns}
        dataSource={listQuery.data?.items ?? []}
        pagination={{ ...tablePagination, total: listQuery.data?.total ?? 0 }}
        onChange={onTableChange}
      />

      {/* 审核详情 Drawer */}
      <Drawer
        width={reviewFullscreen ? '100vw' : '85vw'}
        title={detail ? `重构审核 · #${detail.id} ${detail.source_product_title}` : '重构审核'}
        open={detailId !== null}
        onClose={() => setDetailId(null)}
        extra={
          detail && result ? (
            <Space>
              <Button
                icon={reviewFullscreen ? <FullscreenExitOutlined /> : <FullscreenOutlined />}
                onClick={() => setReviewFullscreen((prev) => !prev)}
              >
                {reviewFullscreen ? '退出全屏' : '全屏对比'}
              </Button>
              <Button
                danger
                onClick={() => reviewMutation.mutate({ id: detail.id, action: 'reject', note: reviewForm.getFieldValue('note') })}
                loading={reviewMutation.isPending}
              >
                打回
              </Button>
              {editMode ? (
                <Button
                  type="primary"
                  onClick={() =>
                    reviewMutation.mutate({
                      id: detail.id,
                      action: 'edit',
                      note: reviewForm.getFieldValue('note'),
                      edited: {
                        title: reviewForm.getFieldValue('edited_title'),
                        selling_points: reviewForm.getFieldValue('edited_selling_points'),
                      },
                    })
                  }
                  loading={reviewMutation.isPending}
                >
                  提交编辑并通过
                </Button>
              ) : (
                <Button
                  type="primary"
                  onClick={() =>
                    reviewMutation.mutate({
                      id: detail.id,
                      action: 'approve',
                      note: reviewForm.getFieldValue('note'),
                    })
                  }
                  loading={reviewMutation.isPending}
                >
                  通过
                </Button>
              )}
              <Button onClick={() => setEditMode((prev) => !prev)}>
                {editMode ? '取消编辑' : '编辑后通过'}
              </Button>
            </Space>
          ) : null
        }
      >
        {detail ? (
          <>
            <Descriptions bordered size="small" column={3} style={{ marginBottom: 16 }}>
              <Descriptions.Item label="任务状态">
                <StatusTag enumKey="AiTaskStatus" value={detail.status} />
              </Descriptions.Item>
              <Descriptions.Item label="审核状态">
                <StatusTag enumKey="ReviewStatus" value={result?.review_status ?? 'pending'} />
              </Descriptions.Item>
              <Descriptions.Item label="任务类型">
                <StatusTag enumKey="AiTaskType" value={detail.task_type} />
              </Descriptions.Item>
              <Descriptions.Item label="AI 客户端">{detail.ai_client || '-'}</Descriptions.Item>
              <Descriptions.Item label="目标平台">
                <StatusTag enumKey="Platform" value={detail.target_platform} />
              </Descriptions.Item>
              <Descriptions.Item label="模板版本">{detail.template_version ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="创建人">{detail.created_by ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="创建时间">{formatTime(detail.created_at)}</Descriptions.Item>
            </Descriptions>

            {detail.input_prompt ? (
              <>
                <Typography.Title level={5}>提交时带的提示词</Typography.Title>
                <Typography.Paragraph style={{ whiteSpace: 'pre-wrap' }}>
                  {JSON.stringify(detail.input_prompt, null, 2)}
                </Typography.Paragraph>
              </>
            ) : null}

            {detail.error_message ? (
              <Alert
                type="error"
                showIcon
                message="任务执行失败"
                description={detail.error_message}
                style={{ marginBottom: 16 }}
              />
            ) : null}

            {!result ? (
              <Empty description="尚无重构产出（任务可能仍在队列或已失败）" />
            ) : (
              <>
                <Alert
                  type={result.banned_words.length > 0 ? 'warning' : 'success'}
                  showIcon
                  style={{ marginBottom: 16 }}
                  message={
                    result.banned_words.length > 0
                      ? `检测到 ${result.banned_words.length} 个违禁词，已在下方高亮`
                      : '未检测到违禁词'
                  }
                  description={
                    result.banned_words.length > 0
                      ? result.banned_words
                          .map((item) => `${item.word}（${item.type}）建议：${item.suggestion ?? '删除或替换'}`)
                          .join('；')
                      : undefined
                  }
                />

                <Row gutter={16}>
                  <Col span={12}>
                    <Typography.Title level={5}>原图</Typography.Title>
                    {rawAssets.length > 0 ? (
                      <div className="erp-compare-block">
                        {rawAssets.map((asset) => (
                          <Image
                            key={asset.id}
                            src={asset.preview_url ?? asset.origin_url ?? undefined}
                            alt={`原图-${asset.id}`}
                          />
                        ))}
                      </div>
                    ) : (
                      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无原图" />
                    )}
                  </Col>
                  <Col span={12}>
                    <Typography.Title level={5}>重绘图</Typography.Title>
                    {reworkedAssets.length > 0 ? (
                      <div className="erp-compare-block">
                        {reworkedAssets.map((asset) => (
                          <Image
                            key={asset.id}
                            src={asset.preview_url ?? undefined}
                            alt={`重绘图-${asset.id}`}
                          />
                        ))}
                      </div>
                    ) : (
                      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无重绘图" />
                    )}
                  </Col>
                </Row>

                <Row gutter={16} style={{ marginTop: 16 }}>
                  <Col span={12}>
                    <Typography.Title level={5}>标题原文</Typography.Title>
                    <Typography.Paragraph>{detail.source_product_title}</Typography.Paragraph>
                  </Col>
                  <Col span={12}>
                    <Typography.Title level={5}>标题新文</Typography.Title>
                    <Typography.Paragraph>
                      {editMode ? null : highlightBanned(result.output_title, result.banned_words)}
                    </Typography.Paragraph>
                  </Col>
                </Row>

                <Typography.Title level={5}>卖点</Typography.Title>
                <Typography.Paragraph>
                  {editMode ? null : highlightBanned(result.output_selling_points, result.banned_words)}
                </Typography.Paragraph>

                <Typography.Title level={5}>属性填充表</Typography.Title>
                <Table
                  rowKey="key"
                  size="small"
                  pagination={false}
                  dataSource={attributeRows}
                  locale={{ emptyText: '暂无属性' }}
                  columns={[
                    { title: '属性名', dataIndex: 'attr_key', width: 200 },
                    { title: '属性值', dataIndex: 'attr_value' },
                  ]}
                />

                {result.output_title_candidates.length > 0 ||
                result.output_video_script !== null ? (
                  <>
                    <Divider orientation="left" plain style={{ margin: '16px 0 12px' }}>
                      新能力产出
                    </Divider>
                    <Tabs
                      items={[
                        {
                          key: 'title',
                          label: `标题候选（${result.output_title_candidates.length}）`,
                          children: (
                            <TitleCandidatesPanel taskId={detail.id} result={result} />
                          ),
                        },
                        {
                          key: 'video',
                          label: '视频脚本',
                          children: <VideoScriptPanel result={result} />,
                        },
                      ]}
                    />
                  </>
                ) : null}

                <Typography.Title level={5} style={{ marginTop: 16 }}>
                  审核操作
                </Typography.Title>
                <Form form={reviewForm} layout="vertical">
                  {editMode ? (
                    <>
                      <Form.Item
                        name="edited_title"
                        label="编辑后的标题"
                        initialValue={result.output_title ?? ''}
                      >
                        <Input.TextArea rows={2} />
                      </Form.Item>
                      <Form.Item
                        name="edited_selling_points"
                        label="编辑后的卖点"
                        initialValue={result.output_selling_points ?? ''}
                      >
                        <Input.TextArea rows={3} />
                      </Form.Item>
                    </>
                  ) : null}
                  <Form.Item name="note" label="审核备注">
                    <Input.TextArea rows={2} placeholder="打回时建议填写原因" />
                  </Form.Item>
                </Form>

                <Descriptions bordered size="small" column={2} title="审核记录">
                  <Descriptions.Item label="审核人">{result.reviewed_by ?? '-'}</Descriptions.Item>
                  <Descriptions.Item label="审核时间">{formatTime(result.reviewed_at)}</Descriptions.Item>
                  <Descriptions.Item label="备注" span={2}>
                    {result.review_note ?? '-'}
                  </Descriptions.Item>
                </Descriptions>
              </>
            )}
          </>
        ) : null}
      </Drawer>

      {/* 并发配置 */}
      <Modal
        title="AI 队列并发配置"
        open={concurrencyOpen}
        onCancel={() => setConcurrencyOpen(false)}
        onOk={() => concurrencyForm.submit()}
        confirmLoading={concurrencyMutation.isPending}
        okText="保存"
        cancelText="取消"
      >
        <Form
          form={concurrencyForm}
          layout="vertical"
          onFinish={(values: ConcurrencyFormValues) => concurrencyMutation.mutate(values)}
        >
          <Form.Item
            name="max_concurrency"
            label="最大并发数"
            rules={[{ required: true, message: '请填写最大并发数' }]}
          >
            <InputNumber min={1} max={50} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="max_retry" label="最大重试次数" rules={[{ required: true }]}>
            <InputNumber min={0} max={10} style={{ width: '100%' }} />
          </Form.Item>
        </Form>
      </Modal>
    </PageContainer>
  );
}
