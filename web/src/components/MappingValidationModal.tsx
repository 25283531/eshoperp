import { Alert, Modal, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';

import type { MappingConflictDetailVo, MappingValidationVo, MissingMappingVo } from '@/api/types';
import ConflictBadge from '@/components/ConflictBadge';
import { MAPPING_BLOCK_COPY } from '@/constants/enums';
import { useEnumOptions } from '@/hooks/useEnumOptions';

export interface MappingValidationModalProps {
  open: boolean;
  /** 校验返回体（来自 422 响应的 data 或 /sku-mappings/validate） */
  validation: MappingValidationVo | null;
  onClose: () => void;
}

const missingColumns: ColumnsType<MissingMappingVo> = [
  { title: '店铺 SKU 编码', dataIndex: 'shop_sku_code', className: 'erp-mono' },
  { title: '原因', dataIndex: 'reason' },
];

/**
 * ★ 业务红线 UI：上架前的映射强制校验结果。
 * 只要 blocking=true 就必须明确告知「无绕过路径」，并列出缺失映射与冲突明细。
 */
export default function MappingValidationModal(props: MappingValidationModalProps): JSX.Element {
  const { open, validation, onClose } = props;
  const conflictTypeOptions = useEnumOptions('ConflictType');

  const conflictColumns: ColumnsType<MappingConflictDetailVo> = [
    {
      title: '等级',
      dataIndex: 'level',
      width: 160,
      render: (_value: unknown, record) => (
        <ConflictBadge level={record.level} conflictTypes={[record.conflict_type]} compact />
      ),
    },
    {
      title: '冲突类型',
      dataIndex: 'conflict_type',
      width: 160,
      render: (value: string) =>
        conflictTypeOptions.find((item) => item.value === value)?.label ?? value,
    },
    { title: '店铺 SKU 编码', dataIndex: 'shop_sku_code', width: 180, className: 'erp-mono' },
    { title: '说明', dataIndex: 'description' },
  ];

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width={860}
      title={MAPPING_BLOCK_COPY.title}
      destroyOnHidden
    >
      {validation ? (
        <>
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 12 }}
            message={validation.blocked_reason ?? '存在阻断项，已禁止上架'}
            description={MAPPING_BLOCK_COPY.noBypass}
          />
          <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
            校验范围：货源商品 #{validation.source_product_id} · 平台 {validation.platform} · 店铺{' '}
            {validation.shop_id} · 已检查 {validation.checked_sku_count} 个 SKU
          </Typography.Paragraph>
          <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
            冲突分两档：<b>P0（红）</b>＝拦截，必须解决后才能上架；
            <b>P1（黄）</b>＝仅提示不拦截（多平台铺同一货源是正常主营业务，售价倒挂只做亏损提醒），
            不影响上架。
          </Typography.Paragraph>

          <Typography.Title level={5}>{MAPPING_BLOCK_COPY.missingTitle}</Typography.Title>
          <Table<MissingMappingVo>
            rowKey="shop_sku_code"
            size="small"
            pagination={false}
            columns={missingColumns}
            dataSource={validation.missing_mappings ?? []}
            locale={{ emptyText: '无缺失映射' }}
          />

          <Typography.Title level={5} style={{ marginTop: 16 }}>
            {MAPPING_BLOCK_COPY.conflictTitle}
          </Typography.Title>
          <Table<MappingConflictDetailVo>
            rowKey={(record) => `${record.conflict_type}-${record.shop_sku_code}`}
            size="small"
            pagination={false}
            columns={conflictColumns}
            dataSource={validation.conflicts ?? []}
            locale={{ emptyText: '无冲突' }}
          />
        </>
      ) : null}
    </Modal>
  );
}
