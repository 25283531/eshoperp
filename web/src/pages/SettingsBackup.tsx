import { Alert, Button, Card, Space, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { CloudDownloadOutlined, ReloadOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

import PageContainer from '@/components/PageContainer';
import { createBackup, listBackups } from '@/api/ops';
import type { BackupItemVo } from '@/api/types';

/** 人类可读的文件大小 */
function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

function formatTime(value: string): string {
  // 后端用 datetime.fromtimestamp().isoformat()，本地时间无时区后缀
  return value.replace('T', ' ').slice(0, 19);
}

export default function SettingsBackup() {
  const queryClient = useQueryClient();
  const [lastResult, setLastResult] = useState<string | null>(null);

  const listQuery = useQuery({
    queryKey: ['system', 'backups'],
    queryFn: () => listBackups(50),
  });

  const backupMutation = useMutation({
    mutationFn: () => createBackup(),
    onSuccess: (data) => {
      // 明确告知备份内容范围，避免使用者误以为"备份了数据库就万事大吉"
      setLastResult(
        `已生成 ${data.file_name}（${formatSize(data.size_bytes)}）` +
          (data.pruned > 0 ? `，自动清理了 ${data.pruned} 份旧备份` : ''),
      );
      void queryClient.invalidateQueries({ queryKey: ['system', 'backups'] });
    },
    onError: (err: Error) => {
      setLastResult(`备份失败：${err.message}`);
    },
  });

  const columns: ColumnsType<BackupItemVo> = [
    { title: '文件名', dataIndex: 'file_name', className: 'erp-mono' },
    {
      title: '大小',
      dataIndex: 'size_bytes',
      width: 110,
      render: (v: number) => formatSize(v),
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      width: 180,
      render: (v: string) => formatTime(v),
    },
    {
      title: '宿主机路径',
      dataIndex: 'path',
      width: 260,
      render: (v: string) => (
        <Typography.Text className="erp-mono" copyable>
          {v}
        </Typography.Text>
      ),
    },
  ];

  return (
    <PageContainer
      title="数据备份"
      subTitle="一键把整个 data/ 目录打包，包含数据库、商品素材与 AI 产出"
      loading={listQuery.isLoading}
    >
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Alert
          type="info"
          showIcon
          message="备份的是整个 data/ 目录，不只是数据库"
          description={
            <div>
              <p style={{ marginBottom: 8 }}>
                备份内容包含：<b>erp.db</b>（SQLite 主库）、<b>商品素材图片</b>、
                <b>AI 重构产出</b>。
              </p>
              <p style={{ marginBottom: 8 }}>
                ⚠️ <b>只拷贝 erp.db 是不够的</b>——那样恢复后应用照常启动、
                所有接口正常，但<b>素材全空</b>，且不报错、不容易被发现。
              </p>
              <p style={{ marginBottom: 0 }}>
                备份文件落在 <code>data/backups/</code> 下，因此
                <b>整个 data/ 目录拷走时，备份也一起被带走</b>。
                数据库用 SQLite 在线备份接口导出，WAL 模式下同样自洽。
              </p>
            </div>
          }
        />

        <Card size="small">
          <Space align="center" wrap>
            <Button
              type="primary"
              size="large"
              icon={<CloudDownloadOutlined />}
              loading={backupMutation.isPending}
              onClick={() => backupMutation.mutate()}
            >
              一键备份
            </Button>
            <Button
              icon={<ReloadOutlined />}
              onClick={() => void queryClient.invalidateQueries({ queryKey: ['system', 'backups'] })}
            >
              刷新列表
            </Button>
            {lastResult && <Tag color={lastResult.startsWith('备份失败') ? 'red' : 'green'}>{lastResult}</Tag>}
          </Space>
        </Card>

        <Card size="small" title={`备份历史（保留最近 ${listQuery.data?.max_keep ?? 20} 份）`}>
          <Table<BackupItemVo>
            rowKey="file_name"
            size="small"
            columns={columns}
            dataSource={listQuery.data?.items ?? []}
            pagination={false}
            locale={{ emptyText: '暂无备份，点上方「一键备份」生成第一份' }}
          />
        </Card>
      </Space>
    </PageContainer>
  );
}
