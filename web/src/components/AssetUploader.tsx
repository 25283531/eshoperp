/**
 * 手工上传素材图片（POST /assets/upload）。
 *
 * ★★ 为什么这个组件是「救命」而不是「锦上添花」★★
 *   使用者的 1688 应用**尚未开通商品详情接口权限**（调用返回 `gw.APIACLDecline`），
 *   采集链路进不来任何一张真实图片 ⇒ AI 逐图重绘这条主链路既没法验证也没法用。
 *   手工上传是当前**唯一**能把图片送进系统的入口，本组件就是它的门面。
 *
 * ★ 三条硬约束（与后端 `services/asset_service.py` 顶部常量逐条对齐）：
 *   单张 ≤10MB / 单次 ≤20 张 / 仅 jpg·jpeg·png·webp·bmp·gif。
 *   这里在**提交前**就校验并讲明原因 —— 让使用者在按下按钮之前就知道哪张过不了，
 *   而不是等 10MB 传完才收到一句「文件过大」。
 *
 * ★ 结果必须逐条如实呈现：新增 N / 重复 M / 失败 K，失败的把后端给的
 *   `reason` 原样显示出来（"文件内容不是有效的图片"这类信息对使用者有用，不要吞掉）。
 *
 * ★ 不硬编码任何目录名或文件名：角色只输出 `main_image` / `detail_image` 两个枚举值，
 *   落盘路径与序号全由后端决定，前端只负责渲染。
 */
import { useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Progress,
  Radio,
  Space,
  Table,
  Tag,
  Typography,
  Upload,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { UploadFile } from 'antd';
import { InboxOutlined } from '@ant-design/icons';
import { useMutation, useQueryClient } from '@tanstack/react-query';

import {
  UPLOAD_ALLOWED_EXTS,
  UPLOAD_MAX_BYTES,
  UPLOAD_MAX_FILES,
  uploadAssets,
} from '@/api/catalog';
import type { AssetUploadFailedItem, AssetUploadVo } from '@/api/types';
import { useEnumOptions } from '@/hooks/useEnumOptions';

/** 两个图片角色（后端 AssetType 的图片取值，别的取值不属于图片） */
const IMAGE_ROLES = ['main_image', 'detail_image'];

/** 被拒条目在界面上的形态：后端返回的 `reason` 与前端本地校验的原因共用一套渲染 */
interface RejectedItem extends AssetUploadFailedItem {
  key: string;
}

function formatBytes(size: number): string {
  if (size >= 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(2)} MB`;
  if (size >= 1024) return `${(size / 1024).toFixed(0)} KB`;
  return `${size} B`;
}

/**
 * 提交前的本地校验。
 *
 * ★ 注意：这里**只能**校验大小与扩展名。文件头是否真的是图片由后端按内容嗅探，
 *   前端读字节流做校验既慢又容易被绕过；所以「不是有效图片」这类拒绝会由后端在
 *   `failed[].reason` 里回传，本组件照样逐条显示。
 */
function validateFile(file: File): string | null {
  const name = file.name || '（无文件名）';
  const dot = name.lastIndexOf('.');
  const suffix = dot >= 0 ? name.slice(dot).toLowerCase() : '';
  if (!UPLOAD_ALLOWED_EXTS.includes(suffix)) {
    return `不支持的文件类型 ${suffix || '（无扩展名）'}，仅支持 ${UPLOAD_ALLOWED_EXTS.join(' / ')}`;
  }
  if (file.size === 0) return '文件内容为空';
  if (file.size > UPLOAD_MAX_BYTES) {
    return `文件超过 ${UPLOAD_MAX_BYTES / (1024 * 1024)}MB 上限（当前 ${formatBytes(file.size)}），请压缩后再传`;
  }
  return null;
}

const REJECTED_COLUMNS: ColumnsType<RejectedItem> = [
  {
    title: '文件',
    dataIndex: 'filename',
    ellipsis: true,
    render: (value: string) => <Typography.Text code>{value || '（无文件名）'}</Typography.Text>,
  },
  { title: '原因', dataIndex: 'reason' },
];

export interface AssetUploaderProps {
  /** 归属的货源商品（必填，后端按它落目录） */
  sourceProductId: number;
  /** 默认角色 */
  defaultRole?: string;
  /** 上传完成后回调（父组件据此刷新自己的视图） */
  onUploaded?: (result: AssetUploadVo) => void;
}

export default function AssetUploader(props: AssetUploaderProps): JSX.Element {
  const { sourceProductId, defaultRole = 'main_image', onUploaded } = props;
  const queryClient = useQueryClient();
  const assetTypeOptions = useEnumOptions('AssetType');

  const [role, setRole] = useState<string>(defaultRole);
  const [pending, setPending] = useState<File[]>([]);
  const [rejected, setRejected] = useState<RejectedItem[]>([]);
  const [result, setResult] = useState<AssetUploadVo | null>(null);
  const [percent, setPercent] = useState<number>(0);

  const roleOptions = useMemo(
    () =>
      assetTypeOptions
        .filter((item) => IMAGE_ROLES.includes(item.value))
        .map((item) => ({ label: item.label, value: item.value })),
    [assetTypeOptions],
  );

  /** 超出单次数量上限的那部分：单独列出来，不静默截断 */
  const overflow = useMemo(() => pending.slice(UPLOAD_MAX_FILES), [pending]);
  const accepted = useMemo(() => pending.slice(0, UPLOAD_MAX_FILES), [pending]);

  const fileList: UploadFile[] = pending.map((file, index) => ({
    uid: `${file.name}-${file.size}-${index}`,
    name: file.name,
    size: file.size,
    status: 'done',
    originFileObj: file as unknown as UploadFile['originFileObj'],
  }));

  const uploadMutation = useMutation({
    mutationFn: () =>
      uploadAssets({
        files: accepted,
        source_product_id: sourceProductId,
        role,
        onUploadProgress: setPercent,
      }),
    onSuccess: (data) => {
      setResult(data);
      setPending([]);
      setRejected([]);
      setPercent(100);
      if (data.failed_count > 0) {
        message.warning(
          `上传完成：新增 ${data.created_count} 张 / 重复 ${data.duplicated_count} 张 / 被拒 ${data.failed_count} 个`,
        );
      } else {
        message.success(
          `上传完成：新增 ${data.created_count} 张 / 重复 ${data.duplicated_count} 张`,
        );
      }
      // ★ 让新传的图立刻出现在逐图提示词界面里
      void queryClient.invalidateQueries({ queryKey: ['assets'] });
      void queryClient.invalidateQueries({ queryKey: ['source-products'] });
      onUploaded?.(data);
    },
    onError: () => {
      setPercent(0);
    },
  });

  const handleSubmit = (): void => {
    setResult(null);
    setPercent(0);
    uploadMutation.mutate();
  };

  const uploading = uploadMutation.isPending;

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Space size={12} wrap align="center">
        <Typography.Text strong>素材角色</Typography.Text>
        <Radio.Group
          optionType="button"
          buttonStyle="solid"
          options={roleOptions}
          value={role}
          onChange={(event) => setRole(event.target.value)}
        />
        <Typography.Text type="secondary">
          一次最多 {UPLOAD_MAX_FILES} 张，单张 ≤{UPLOAD_MAX_BYTES / (1024 * 1024)}MB
        </Typography.Text>
      </Space>

      <Upload
        multiple
        accept={UPLOAD_ALLOWED_EXTS.join(',')}
        fileList={fileList}
        listType="picture"
        disabled={uploading}
        beforeUpload={(file) => {
          const reason = validateFile(file);
          if (reason) {
            setRejected((prev) => [
              ...prev,
              { key: `${file.name}-${prev.length}`, filename: file.name, reason },
            ]);
          } else {
            setPending((prev) => [...prev, file]);
          }
          // 返回 false：不自动上传，由「开始上传」统一提交（要能先看见校验结论）
          return false;
        }}
        onRemove={(file) => {
          setPending((prev) => prev.filter((item) => item.name !== file.name));
        }}
      >
        <Button icon={<InboxOutlined />} disabled={uploading}>
          选择图片（可多选）
        </Button>
      </Upload>

      {rejected.length > 0 ? (
        <Alert
          type="error"
          showIcon
          message={`${rejected.length} 个文件未通过本地校验，不会提交`}
          description={
            <Table<RejectedItem>
              size="small"
              rowKey="key"
              pagination={false}
              columns={REJECTED_COLUMNS}
              dataSource={rejected}
            />
          }
        />
      ) : null}

      {overflow.length > 0 ? (
        <Alert
          type="error"
          showIcon
          message={`本次选了 ${pending.length} 张，超出单次 ${UPLOAD_MAX_FILES} 张上限`}
          description={
            <span>
              请先移除 {overflow.length} 张再上传（超出的：
              {overflow.map((file) => file.name).join('、')}），或分次上传。
            </span>
          }
        />
      ) : null}

      <Space size={12} wrap>
        <Button
          type="primary"
          onClick={handleSubmit}
          loading={uploading}
          disabled={accepted.length === 0 || overflow.length > 0}
        >
          {accepted.length > 0 ? `开始上传（${accepted.length} 张）` : '开始上传'}
        </Button>
        {pending.length > 0 ? (
          <Typography.Link
            onClick={() => {
              setPending([]);
              setRejected([]);
            }}
          >
            清空
          </Typography.Link>
        ) : null}
      </Space>

      {uploading ? (
        <Progress
          percent={percent}
          status={percent >= 100 ? 'success' : 'active'}
          format={(value) => `已发送 ${value ?? 0}%`}
        />
      ) : null}

      {result ? (
        <Alert
          type={result.failed_count > 0 ? 'warning' : 'success'}
          showIcon
          message={
            <Space size={8} wrap>
              <Tag color="success">新增 {result.created_count} 张</Tag>
              <Tag color="blue">重复 {result.duplicated_count} 张</Tag>
              <Tag color={result.failed_count > 0 ? 'red' : 'default'}>
                失败 {result.failed_count} 张
              </Tag>
            </Space>
          }
          description={
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              {result.duplicated_count > 0 ? (
                <Typography.Text type="secondary">
                  重复的图按内容去重，未重复落盘，仍指向原有的素材
                  {result.duplicated[0] ? `（如 #${result.duplicated[0].id}）` : ''}。
                </Typography.Text>
              ) : null}
              {result.failed_count > 0 ? (
                <Table<RejectedItem>
                  size="small"
                  rowKey="key"
                  pagination={false}
                  columns={REJECTED_COLUMNS}
                  dataSource={result.failed.map((item, index) => ({
                    key: `server-${index}`,
                    filename: item.filename,
                    reason: item.reason,
                  }))}
                />
              ) : null}
            </Space>
          }
        />
      ) : null}
    </Space>
  );
}
