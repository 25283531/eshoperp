/**
 * 三种新 AI 能力的**产出展示组件**（标题候选 / 短视频脚本）。
 *
 * 抽成公共组件的原因：同一个产出要在两个地方看 ——
 *  ① `AiTasks.tsx` 的既有审核 Drawer（从任务队列进）；
 *  ② `AiStudio.tsx` 的「查看产出」Drawer（从创建工作台里看刚提交的任务）。
 * 两份各自实现＝字段口径迟早漂移（比如一边显示违禁词一边不显示）。
 */
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Empty,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { CheckOutlined } from '@ant-design/icons';
import { useMutation, useQueryClient } from '@tanstack/react-query';

import { selectAiTitleCandidate } from '@/api/ai';
import type { AiTaskResultVo, AiVideoScriptSceneVo } from '@/api/types';
import { formatTime } from '@/components/AuditTimeline';

/** 多数平台商品标题的字数上限（30 个汉字 = 60 字节），选中前就要能看出超没超 */
const TITLE_CHAR_LIMIT = 30;

// ---------------------------------------------------------------------------
// P1 标题候选
// ---------------------------------------------------------------------------

export interface TitleCandidatesPanelProps {
  taskId: number;
  result: AiTaskResultVo;
}

/**
 * 标题候选列表。
 *
 * ★ 每候选必须展示 style / reason / score / char_count / platform_fit / banned_words ——
 *   他要能看懂"这三条有什么区别"才能挑。只打一串标题等于没做。
 */
export function TitleCandidatesPanel(props: TitleCandidatesPanelProps): JSX.Element {
  const { taskId, result } = props;
  const queryClient = useQueryClient();

  const selectMutation = useMutation({
    mutationFn: (index: number) => selectAiTitleCandidate(taskId, { index }),
    onSuccess: (_data, index) => {
      message.success(`已选定第 ${index + 1} 条标题`);
      void queryClient.invalidateQueries({ queryKey: ['ai-tasks'] });
    },
  });

  const candidates = result.output_title_candidates ?? [];
  if (candidates.length === 0) {
    return (
      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="本次任务没有产出标题候选" />
    );
  }

  const selectedIndex = result.selected_title_index;
  const hasSelection = selectedIndex !== null && selectedIndex !== undefined;

  return (
    <>
      {hasSelection ? (
        <Alert
          type="success"
          showIcon
          style={{ marginBottom: 12 }}
          message={`已选第 ${Number(selectedIndex) + 1} 条`}
          description={
            <Space size={16} wrap>
              <span>选择人：{result.selected_title_by ?? '-'}</span>
              <span>选择时间：{formatTime(result.selected_title_at)}</span>
              <span>
                当前生效标题：
                {candidates[Number(selectedIndex)]?.title ?? result.output_title ?? '-'}
              </span>
            </Space>
          }
        />
      ) : (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="尚未选定标题"
          description="选定后 `output_title` 会变成这一条，上架链路才会用到它；不选则该字段不会更新。"
        />
      )}

      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        {candidates.map((item, index) => (
          <Card
            key={`${index}-${item.title}`}
            size="small"
            title={
              <Space size={8} wrap>
                <Typography.Text strong>候选 {index + 1}</Typography.Text>
                <Tag color="blue">{item.style || '未标风格'}</Tag>
                <Tag color="gold">评分 {item.score}</Tag>
                <Typography.Text
                  type={item.char_count > TITLE_CHAR_LIMIT ? 'danger' : 'secondary'}
                >
                  {item.char_count} 字
                  {item.char_count > TITLE_CHAR_LIMIT
                    ? `（超出 ${TITLE_CHAR_LIMIT} 字上限，上架可能被截断）`
                    : ''}
                </Typography.Text>
                {index === selectedIndex ? <Tag color="success">已选定</Tag> : null}
              </Space>
            }
            extra={
              <Button
                type={index === selectedIndex ? 'default' : 'primary'}
                icon={<CheckOutlined />}
                disabled={index === selectedIndex}
                loading={selectMutation.isPending}
                onClick={() => selectMutation.mutate(index)}
              >
                {index === selectedIndex ? '当前生效' : '选定这条'}
              </Button>
            }
          >
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              <Typography.Paragraph style={{ marginBottom: 0 }}>{item.title}</Typography.Paragraph>

              <Descriptions size="small" column={1} colon={false}>
                <Descriptions.Item label="为什么这么写">{item.reason || '-'}</Descriptions.Item>
                <Descriptions.Item label="平台适配">{item.platform_fit || '-'}</Descriptions.Item>
              </Descriptions>

              {item.selling_points && item.selling_points.length > 0 ? (
                <Space size={4} wrap>
                  <Typography.Text type="secondary">卖点：</Typography.Text>
                  {item.selling_points.map((point) => (
                    <Tag key={point}>{point}</Tag>
                  ))}
                </Space>
              ) : null}

              {item.banned_words && item.banned_words.length > 0 ? (
                <Space size={4} wrap>
                  <Typography.Text type="secondary">命中违禁词：</Typography.Text>
                  {item.banned_words.map((word) => (
                    <Tag key={word} color="red">
                      {word}
                    </Tag>
                  ))}
                </Space>
              ) : (
                <Typography.Text type="secondary">未命中违禁词</Typography.Text>
              )}
            </Space>
          </Card>
        ))}
      </Space>
    </>
  );
}

// ---------------------------------------------------------------------------
// P2 短视频脚本
// ---------------------------------------------------------------------------

export interface VideoScriptPanelProps {
  result: AiTaskResultVo;
}

const SCENE_COLUMNS: ColumnsType<AiVideoScriptSceneVo> = [
  {
    title: '序号',
    dataIndex: 'index',
    width: 70,
    render: (value: number) => `第 ${value} 镜`,
  },
  {
    title: '时长',
    dataIndex: 'duration_sec',
    width: 80,
    render: (value: number) => `${value} 秒`,
  },
  { title: '画面', dataIndex: 'shot' },
  { title: '机位', dataIndex: 'camera', width: 140 },
  { title: '拍摄要点', dataIndex: 'shooting_tips' },
  { title: '口播台词', dataIndex: 'narration' },
];

/**
 * 短视频拍摄脚本文案。
 *
 * ★ 只出文案，不生成视频 —— 这句话必须出现在界面上，
 *   否则使用者会以为系统应该吐出一个 mp4（“脚本都给了，视频呢”）。
 */
export function VideoScriptPanel(props: VideoScriptPanelProps): JSX.Element {
  const script = props.result.output_video_script;
  if (!script) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="本次任务没有产出拍摄脚本" />;
  }

  return (
    <>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="本能力只产出拍摄脚本文案，不生成视频"
        description="视频由你自己按脚本拍。文案可直接复制到拍摄提纲里。"
      />

      <Descriptions bordered size="small" column={4} style={{ marginBottom: 16 }}>
        <Descriptions.Item label="脚本标题" span={2}>
          {script.title || '-'}
        </Descriptions.Item>
        <Descriptions.Item label="拍摄风格">{script.style || '-'}</Descriptions.Item>
        <Descriptions.Item label="分镜数">{script.scene_count}</Descriptions.Item>
        <Descriptions.Item label="总时长" span={4}>
          {script.total_duration_sec} 秒
        </Descriptions.Item>
      </Descriptions>

      <Typography.Title level={5}>分镜表</Typography.Title>
      <Table<AiVideoScriptSceneVo>
        rowKey={(record, index) => `${record.index}-${index ?? 0}`}
        size="small"
        pagination={false}
        scroll={{ x: 1100 }}
        columns={SCENE_COLUMNS}
        dataSource={script.scenes ?? []}
        locale={{ emptyText: '没有分镜' }}
      />

      <Typography.Title level={5} style={{ marginTop: 16 }}>
        照着拍（纯文本）
      </Typography.Title>
      <Typography.Paragraph copyable={{ text: script.text }} style={{ whiteSpace: 'pre-wrap' }}>
        {script.text || '-'}
      </Typography.Paragraph>
    </>
  );
}
