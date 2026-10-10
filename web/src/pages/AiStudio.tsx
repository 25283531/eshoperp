/**
 * AI 内容工作台（路由 `/ai-studio`）：三种新 AI 能力的**操作界面**。
 *
 * ★ 为什么单独开一页而不是塞进 AiTasks：
 *   `AiTasks.tsx` 是任务**队列与审核**视角（先看有没有跑完、再决定放不放行）；
 *   本页是**发起与取用**视角（挑商品 → 给提示词 → 提交 → 看产出 → 选定标题）。
 *   两个视角混在一页里，操作者会先陷进一堆历史任务，找不到"从哪开始"。
 *
 * ★★ 诚实边界（重要，不要删）★★
 *   使用者的 1688 应用**尚未开通商品详情接口权限**，现在采不到真实商品数据。
 *   本页能验证的是：素材分组渲染正确 / 逐图提示词能正确组装成
 *   `image_prompts[{index,prompt,asset_id,source_path,tag}]` 提交给后端 /
 *   三种产出能正确取回并与 select-title 闭环。
 *   **端到端的真实 1688 数据链路未跑通**，原因就是上面这条权限限制。
 */
import { useMemo, useState } from 'react';
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
import { ThunderboltOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { createAiTasks, getAiTask, listAiTasks } from '@/api/ai';
import { listAssets, listSourceProducts } from '@/api/catalog';
import type { AiTaskCreateBody, AiTaskVo, AssetVo } from '@/api/types';
import PageContainer from '@/components/PageContainer';
import StatusTag from '@/components/StatusTag';
import { TitleCandidatesPanel, VideoScriptPanel } from '@/components/AiResultPanels';
import { formatTime } from '@/components/AuditTimeline';
import { useEnumLabel, useEnumOptions } from '@/hooks/useEnumOptions';

/** 三种新能力的任务类型（与后端 AiTaskType 逐字对齐） */
const TASK_TYPE_IMAGE_REDRAW = 'image_redraw';
const TASK_TYPE_TITLE_SUGGEST = 'title_suggest';
const TASK_TYPE_VIDEO_SCRIPT = 'video_script';

/** 素材角色（后端 AssetType）：区分「主图」与「详情图」 */
const ASSET_MAIN = 'main_image';
const ASSET_DETAIL = 'detail_image';

interface ImageFormValues {
  target_platform: string;
  global_prompt?: string;
}

interface TitleFormValues {
  target_platform: string;
  title_prompt?: string;
}

interface VideoFormValues {
  target_platform: string;
  video_script_prompt?: string;
}

/** 一张图在界面上的位置信息 */
interface ImageSlot {
  asset: AssetVo;
  /** 在该商品全部图片素材中的下标（0 起），提交给后端充当 image_prompts[].index */
  index: number;
  /** 在本组（主图 / 详情图）内的展示序号（1 起） */
  seq: number;
}

function isImageAsset(asset: AssetVo): boolean {
  return asset.asset_type === ASSET_MAIN || asset.asset_type === ASSET_DETAIL;
}

/** 从排序后的全部图片素材里抽出某一角色的那些，并编出组内序号（1 起） */
function buildSlots(assets: AssetVo[], role: string): ImageSlot[] {
  return assets
    .map((asset, index) => ({ asset, index }))
    .filter((slot) => slot.asset.asset_type === role)
    .map((slot, position) => ({ asset: slot.asset, index: slot.index, seq: position + 1 }));
}

/**
 * 单张图的「缩略图 + 独立提示词输入框」卡片。
 *
 * ★ 这是本页的核心交互：每张图旁边一个自己的输入框，不是共用一个全局框。
 *   空着不填 = 回落用全局提示词（后端 AiInputPrompt 就是这么设计的）。
 */
function ImagePromptCard(props: {
  slot: ImageSlot;
  value: string;
  onChange: (value: string) => void;
}): JSX.Element {
  const { slot, value, onChange } = props;
  const assetTypeLabel = useEnumLabel('AssetType');
  const role = assetTypeLabel(slot.asset.asset_type);
  const { asset } = slot;

  return (
    <Row gutter={12} align="middle" style={{ marginBottom: 12 }}>
      <Col flex="132px">
        <Space direction="vertical" size={4}>
          {asset.preview_url ? (
            <Image
              src={asset.preview_url}
              alt={`${role}-${slot.seq}`}
              width={120}
              height={120}
              style={{ objectFit: 'cover', borderRadius: 6 }}
            />
          ) : (
            <div
              style={{
                width: 120,
                height: 120,
                background: '#fafafa',
                border: '1px dashed #d9d9d9',
                borderRadius: 6,
              }}
            />
          )}
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {role} {slot.seq}
            {asset.width && asset.height ? ` · ${asset.width}×${asset.height}` : ''}
          </Typography.Text>
        </Space>
      </Col>
      <Col flex="auto">
        <Input.TextArea
          rows={3}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder={`这张${role}要怎么改；留空则使用下方「全局提示词」`}
          maxLength={500}
          showCount
        />
      </Col>
    </Row>
  );
}

export default function AiStudio(): JSX.Element {
  const queryClient = useQueryClient();
  const platformOptions = useEnumOptions('Platform');
  const assetTypeLabel = useEnumLabel('AssetType');

  const [sourceProductId, setSourceProductId] = useState<number | null>(null);
  const [keyword, setKeyword] = useState<string>('');
  /** asset_id → 这张图单独填的提示词（没填的图不会出现在 key 里，提交时补空串） */
  const [promptMap, setPromptMap] = useState<Record<number, string>>({});
  const [previewTaskId, setPreviewTaskId] = useState<number | null>(null);

  const [imageForm] = Form.useForm<ImageFormValues>();
  const [titleForm] = Form.useForm<TitleFormValues>();
  const [videoForm] = Form.useForm<VideoFormValues>();

  /** 货源商品搜索（最多 20 条，够挑） */
  const productsQuery = useQuery({
    queryKey: ['source-products', 'studio-picker', keyword],
    queryFn: () => listSourceProducts({ keyword: keyword || undefined, page: 1, page_size: 20 }),
  });

  /**
   * 该商品的原始素材。
   *
   * ★ 用 `GET /assets?source_product_id=&origin=raw` 而不是任务详情的 `assets[]`：
 *   后端 `GET /ai-tasks/{id}` 构造详情 VO 时 `assets` 恒为 `[]`
 *   （见 backend/app/api/v1/ai_tasks.py 里 `AiTaskDetailVo(..., assets=[])` 那一行），
 *   读它只能拿到空列表。
   */
  const assetsQuery = useQuery({
    queryKey: ['assets', 'studio-raw', sourceProductId],
    queryFn: () =>
      listAssets({
        source_product_id: sourceProductId as number,
        origin: 'raw',
        page: 1,
        page_size: 100,
      }),
    enabled: sourceProductId !== null,
  });

  /**
   * 待提交的图片素材顺序：**主图在前、详情图在后**。
   *
   * ★ 顺序即契约：`image_prompts[].index` 是后端据以定位"改哪张图"的唯一依据，
   *   必须与界面展示的顺序同源，否则第 3 张的提示词会被安到第 2 张上
   *   —— 这种错不报错，图出来才发现。
   */
  const orderedImages = useMemo<AssetVo[]>(() => {
    const assets = (assetsQuery.data?.items ?? []).filter(isImageAsset);
    const mains = assets.filter((item) => item.asset_type === ASSET_MAIN);
    const details = assets.filter((item) => item.asset_type === ASSET_DETAIL);
    return [...mains, ...details];
  }, [assetsQuery.data]);

  const mainSlots = useMemo(() => buildSlots(orderedImages, ASSET_MAIN), [orderedImages]);
  const detailSlots = useMemo(() => buildSlots(orderedImages, ASSET_DETAIL), [orderedImages]);

  const productOptions = (productsQuery.data?.items ?? []).map((item) => ({
    label: `#${item.id} ${item.title}`,
    value: item.id,
  }));
  const pickedProduct = (productsQuery.data?.items ?? []).find(
    (item) => item.id === sourceProductId,
  );

  /** 该商品最近的 AI 任务（轮询 15s，与 AiTasks 队列页一致） */
  const tasksQuery = useQuery({
    queryKey: ['ai-tasks', 'studio', sourceProductId],
    queryFn: () =>
      listAiTasks({ source_product_id: sourceProductId as number, page: 1, page_size: 20 }),
    enabled: sourceProductId !== null,
    refetchInterval: 15_000,
  });

  const previewQuery = useQuery({
    queryKey: ['ai-tasks', previewTaskId],
    queryFn: () => getAiTask(previewTaskId as number),
    enabled: previewTaskId !== null,
    refetchInterval: 15_000,
  });

  const createMutation = useMutation({
    mutationFn: (body: AiTaskCreateBody) => createAiTasks(body),
    onSuccess: (data) => {
      message.success(
        `已创建 ${data.task_ids.length} 个 AI 任务${data.task_type ? `（${data.task_type}）` : ''}`,
      );
      void queryClient.invalidateQueries({ queryKey: ['ai-tasks'] });
      const first = data.task_ids[0];
      if (first !== undefined) setPreviewTaskId(first);
    },
  });

  const submitImageRedraw = (values: ImageFormValues): void => {
    if (sourceProductId === null) return;
    /**
     * ★ 每张图都发一条（包括没填字的），否则下标错位。
     */
    const imagePromptsBody = orderedImages.map((asset, index) => ({
      index,
      prompt: (promptMap[asset.id] ?? '').trim(),
      asset_id: asset.id,
      source_path: asset.storage_path,
      tag: asset.asset_type,
    }));
    const globalPrompt = (values.global_prompt ?? '').trim();
    if (globalPrompt === '' && imagePromptsBody.every((item) => item.prompt === '')) {
      message.warning('至少填一个「全局提示词」或某张图的单独提示词，否则 AI 没有依据');
      return;
    }
    createMutation.mutate({
      source_product_ids: [sourceProductId],
      target_platform: values.target_platform,
      rework_items: [],
      task_type: TASK_TYPE_IMAGE_REDRAW,
      global_prompt: globalPrompt,
      image_prompts: imagePromptsBody,
    });
  };

  const submitTitleSuggest = (values: TitleFormValues): void => {
    if (sourceProductId === null) return;
    createMutation.mutate({
      source_product_ids: [sourceProductId],
      target_platform: values.target_platform,
      rework_items: [],
      task_type: TASK_TYPE_TITLE_SUGGEST,
      title_prompt: (values.title_prompt ?? '').trim(),
    });
  };

  const submitVideoScript = (values: VideoFormValues): void => {
    if (sourceProductId === null) return;
    createMutation.mutate({
      source_product_ids: [sourceProductId],
      target_platform: values.target_platform,
      rework_items: [],
      task_type: TASK_TYPE_VIDEO_SCRIPT,
      video_script_prompt: (values.video_script_prompt ?? '').trim(),
    });
  };

  const taskColumns = useMemo<ColumnsType<AiTaskVo>>(
    () => [
      { title: 'ID', dataIndex: 'id', width: 70 },
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
        title: '状态',
        dataIndex: 'status',
        width: 110,
        render: (value: string) => <StatusTag enumKey="AiTaskStatus" value={value} />,
      },
      {
        title: 'AI 客户端',
        dataIndex: 'ai_client',
        width: 110,
        render: (value?: string) => value || '-',
      },
      {
        title: '创建时间',
        dataIndex: 'created_at',
        width: 170,
        render: (value: string) => formatTime(value),
      },
      {
        title: '操作',
        key: 'actions',
        width: 110,
        render: (_value: unknown, record) => (
          <Typography.Link onClick={() => setPreviewTaskId(record.id)}>查看产出</Typography.Link>
        ),
      },
    ],
    [],
  );

  const previewTask = previewQuery.data;
  const previewResult = previewTask?.result ?? null;

  /** 一组素材的分区标题：数量 + 角色标签（标签文案来自 enum 字典，不硬编码） */
  const renderGroupDivider = (role: string, count: number): JSX.Element => (
    <Divider orientation="left" plain style={{ margin: '8px 0' }}>
      <Space size={8}>
        <span>{assetTypeLabel(role)}</span>
        <Tag>{`${count} 张`}</Tag>
      </Space>
    </Divider>
  );

  const renderImageCards = (slots: ImageSlot[]): JSX.Element[] =>
    slots.map((slot) => (
      <ImagePromptCard
        key={slot.asset.id}
        slot={slot}
        value={promptMap[slot.asset.id] ?? ''}
        onChange={(value) => setPromptMap((prev) => ({ ...prev, [slot.asset.id]: value }))}
      />
    ));

  return (
    <PageContainer
      title="AI 内容工作台"
      subTitle="挑一个货源商品 → 填提示词 → 提交 AI 任务 → 在这里直接看产出、选定标题"
      alert={
        <Alert
          type="warning"
          showIcon
          message="1688 商品详情接口权限尚未开通，现在采不到真实商品数据"
          description="当前可用「手工录入货源商品」或「批量导入 CSV」准备数据来验证本页（两者都不依赖 1688 开放平台）。这条限制解除前，不要宣告本页已用真实 1688 数据验证过。"
        />
      }
    >
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        <Space size={12} wrap align="center">
          <Typography.Text strong>货源商品</Typography.Text>
          <Select
            showSearch
            allowClear
            filterOption={false}
            placeholder="输入商品标题关键词搜索"
            style={{ width: 420 }}
            onSearch={setKeyword}
            onSelect={(value: number) => {
              setSourceProductId(value);
              setPromptMap({});
            }}
            onClear={() => {
              setSourceProductId(null);
              setPromptMap({});
            }}
            value={sourceProductId}
            options={productOptions}
            notFoundContent={keyword ? '没有匹配的货源商品' : '先输入关键词'}
          />
          {pickedProduct ? (
            <Typography.Text type="secondary">
              #{pickedProduct.id} · {pickedProduct.product_1688_id}
            </Typography.Text>
          ) : null}
        </Space>

        {sourceProductId === null ? (
          <Empty description="先选一个货源商品。1688 采集暂时不可用，请先在「货源商品库」用手工录入或 CSV 导入准备数据" />
        ) : (
          <>
            <Tabs
              defaultActiveKey={TASK_TYPE_IMAGE_REDRAW}
              items={[
                {
                  key: TASK_TYPE_IMAGE_REDRAW,
                  label: '图片重绘',
                  children: (
                    <Form form={imageForm} layout="vertical" onFinish={submitImageRedraw}>
                      {assetsQuery.isLoading ? (
                        <Typography.Text type="secondary">素材加载中…</Typography.Text>
                      ) : orderedImages.length === 0 ? (
                        <Empty
                          description={
                            <span>
                              这个商品还没有原始图片素材。
                              <br />
                              后端目前没有图片上传端点，素材来源只有「1688 采集时下载」一条，
                              无法手工补传。
                            </span>
                          }
                        />
                      ) : (
                        <>
                          {mainSlots.length > 0 ? renderGroupDivider(ASSET_MAIN, mainSlots.length) : null}
                          {renderImageCards(mainSlots)}
                          {detailSlots.length > 0
                            ? renderGroupDivider(ASSET_DETAIL, detailSlots.length)
                            : null}
                          {renderImageCards(detailSlots)}

                          <Form.Item
                            name="global_prompt"
                            label="全局提示词"
                            extra="上面某张图没单独填时，就回落用这一条"
                          >
                            <Input.TextArea
                              rows={3}
                              placeholder="如：整体改成暖色 ins 风，去掉原图上的中文促销贴字"
                              maxLength={1000}
                              showCount
                            />
                          </Form.Item>
                        </>
                      )}

                      <Form.Item
                        name="target_platform"
                        label="目标平台"
                        rules={[{ required: true, message: '请选择目标平台' }]}
                      >
                        <Select options={platformOptions} placeholder="请选择目标平台" />
                      </Form.Item>
                      <Button
                        type="primary"
                        htmlType="submit"
                        icon={<ThunderboltOutlined />}
                        loading={createMutation.isPending}
                        disabled={orderedImages.length === 0}
                      >
                        提交图片重绘任务
                      </Button>
                    </Form>
                  ),
                },
                {
                  key: TASK_TYPE_TITLE_SUGGEST,
                  label: '标题建议',
                  children: (
                    <Form form={titleForm} layout="vertical" onFinish={submitTitleSuggest}>
                      <Form.Item
                        name="title_prompt"
                        label="提问要求"
                        extra="写清想要的风格与热词倾向；AI 会给出 3 条以上候选，之后在产出里挑一条并选定"
                      >
                        <Input.TextArea
                          rows={4}
                          placeholder="如：走性价比路线，突出夏季薄款透气，带上通勤、学生党这类热词"
                          maxLength={1000}
                          showCount
                        />
                      </Form.Item>
                      <Form.Item
                        name="target_platform"
                        label="目标平台"
                        rules={[{ required: true, message: '请选择目标平台' }]}
                      >
                        <Select options={platformOptions} placeholder="请选择目标平台" />
                      </Form.Item>
                      <Button
                        type="primary"
                        htmlType="submit"
                        icon={<ThunderboltOutlined />}
                        loading={createMutation.isPending}
                      >
                        提交标题建议任务
                      </Button>
                    </Form>
                  ),
                },
                {
                  key: TASK_TYPE_VIDEO_SCRIPT,
                  label: '视频脚本',
                  children: (
                    <Form form={videoForm} layout="vertical" onFinish={submitVideoScript}>
                      <Alert
                        type="info"
                        showIcon
                        style={{ marginBottom: 12 }}
                        message="本能力只产出拍摄脚本文案，不生成视频"
                        description="视频由你自己按脚本拍。"
                      />
                      <Form.Item
                        name="video_script_prompt"
                        label="拍摄风格 / 内容倾向"
                        extra="会回填到产出里的 style 字段，方便回看是按哪句话写的"
                      >
                        <Input.TextArea
                          rows={4}
                          placeholder="如：竖屏快节奏，强调使用前后对比，30 秒内讲清三个卖点"
                          maxLength={1000}
                          showCount
                        />
                      </Form.Item>
                      <Form.Item
                        name="target_platform"
                        label="目标平台"
                        rules={[{ required: true, message: '请选择目标平台' }]}
                      >
                        <Select options={platformOptions} placeholder="请选择目标平台" />
                      </Form.Item>
                      <Button
                        type="primary"
                        htmlType="submit"
                        icon={<ThunderboltOutlined />}
                        loading={createMutation.isPending}
                      >
                        提交视频脚本任务
                      </Button>
                    </Form>
                  ),
                },
              ]}
            />

            <Divider orientation="left" plain style={{ margin: '8px 0' }}>
              这个商品的 AI 任务（15 秒自动刷新）
            </Divider>
            <Table<AiTaskVo>
              rowKey="id"
              size="small"
              scroll={{ x: 900 }}
              columns={taskColumns}
              dataSource={tasksQuery.data?.items ?? []}
              pagination={false}
              locale={{ emptyText: '还没有提交过 AI 任务' }}
            />
          </>
        )}
      </Space>

      {/* 产出预览 */}
      <Drawer
        width={900}
        title={previewTask ? `AI 产出 · 任务 #${previewTask.id}` : 'AI 产出'}
        open={previewTaskId !== null}
        onClose={() => setPreviewTaskId(null)}
      >
        {previewTask ? (
          <>
            <Descriptions bordered size="small" column={2} style={{ marginBottom: 16 }}>
              <Descriptions.Item label="任务类型">
                <StatusTag enumKey="AiTaskType" value={previewTask.task_type} />
              </Descriptions.Item>
              <Descriptions.Item label="任务状态">
                <StatusTag enumKey="AiTaskStatus" value={previewTask.status} />
              </Descriptions.Item>
              <Descriptions.Item label="AI 客户端">{previewTask.ai_client || '-'}</Descriptions.Item>
              <Descriptions.Item label="目标平台">
                <StatusTag enumKey="Platform" value={previewTask.target_platform} />
              </Descriptions.Item>
              <Descriptions.Item label="创建时间">
                {formatTime(previewTask.created_at)}
              </Descriptions.Item>
              <Descriptions.Item label="审核状态">
                <StatusTag
                  enumKey="ReviewStatus"
                  value={previewResult?.review_status ?? 'pending'}
                />
              </Descriptions.Item>
            </Descriptions>

            {previewTask.input_prompt ? (
              <>
                <Typography.Title level={5}>提交时带的提示词</Typography.Title>
                <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 16 }}>
                  {JSON.stringify(previewTask.input_prompt, null, 2)}
                </Typography.Paragraph>
              </>
            ) : null}

            {previewTask.error_message ? (
              <Alert
                type="error"
                showIcon
                style={{ marginBottom: 16 }}
                message="任务执行失败"
                description={previewTask.error_message}
              />
            ) : null}

            {!previewResult ? (
              <Empty description="尚无产出（任务可能还在队列里，或已失败）" />
            ) : (
              <Tabs
                items={[
                  {
                    key: 'title',
                    label: '标题候选',
                    children: <TitleCandidatesPanel taskId={previewTask.id} result={previewResult} />,
                  },
                  {
                    key: 'video',
                    label: '视频脚本',
                    children: <VideoScriptPanel result={previewResult} />,
                  },
                ]}
              />
            )}
          </>
        ) : null}
      </Drawer>
    </PageContainer>
  );
}
