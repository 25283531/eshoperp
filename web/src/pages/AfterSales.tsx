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
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  getAfterSale,
  getReturnAddress,
  listAfterSales,
  submitRefund,
  updateResponsibility,
} from '@/api/ops';
import type { AfterSaleVo } from '@/api/types';
import PageContainer from '@/components/PageContainer';
import MoneyText from '@/components/MoneyText';
import StatusTag from '@/components/StatusTag';
import { formatTime } from '@/components/AuditTimeline';
import { useEnumOptions } from '@/hooks/useEnumOptions';
import { usePagination } from '@/hooks/usePagination';

interface AfterSaleFilterValues {
  order_id: string;
  handling_status: string;
  responsibility: string;
}

interface RefundFormValues {
  refund_amount: string;
  reason: string;
}

interface ResponsibilityFormValues {
  responsibility: 'our_shop' | 'supplier' | 'buyer' | 'platform';
  note: string;
}

const DEFAULT_FILTERS: AfterSaleFilterValues = {
  order_id: '',
  handling_status: '',
  responsibility: '',
};

/**
 * P13 售后管理：退款状态 / 退货地址回传 / 责任归属。
 */
export default function AfterSales(): JSX.Element {
  const queryClient = useQueryClient();
  const { filters, params, setFilters, tablePagination, onTableChange, resetFilters } =
    usePagination<AfterSaleFilterValues>(DEFAULT_FILTERS);
  const statusOptions = useEnumOptions('AfterSaleStatus');
  const responsibilityOptions = useEnumOptions('Responsibility');

  const [detailId, setDetailId] = useState<number | null>(null);
  const [refundId, setRefundId] = useState<number | null>(null);
  const [responsibilityId, setResponsibilityId] = useState<number | null>(null);

  const [refundForm] = Form.useForm<RefundFormValues>();
  const [responsibilityForm] = Form.useForm<ResponsibilityFormValues>();

  const listQuery = useQuery({
    queryKey: ['after-sales', params],
    queryFn: () => listAfterSales(params),
  });

  const detailQuery = useQuery({
    queryKey: ['after-sales', detailId],
    queryFn: () => getAfterSale(detailId as number),
    enabled: detailId !== null,
  });

  const refundMutation = useMutation({
    mutationFn: (body: { id: number; values: RefundFormValues }) =>
      submitRefund(body.id, { refund_amount: body.values.refund_amount, reason: body.values.reason }),
    onSuccess: (data) => {
      // ★ 本地兜底（local_csv）不支持 submit_refund，后端返回 503 并降级；
      // 此处严禁把「已生成待办」说成「已提交 / 成功」。
      if (data.refund_1688_status === 'manual_pending') {
        message.warning(
          `当前履约渠道不支持自动退款，已生成人工待办（${data.refund_1688_no ?? '待人工在 1688 提交'}）`,
        );
      } else {
        message.success(
          `退款已提交：1688 退款单 ${data.refund_1688_no ?? '-'}（${data.refund_1688_status}）`,
        );
      }
      setRefundId(null);
      refundForm.resetFields();
      void queryClient.invalidateQueries({ queryKey: ['after-sales'] });
    },
  });

  const addressMutation = useMutation({
    mutationFn: (id: number) => getReturnAddress(id),
    onSuccess: (data) => {
      // ★ 本地兜底（local_csv）不支持 get_return_address，后端返回 503。
      // 这里是「能力边界」，必须如实展示为待人工处理，不能写成「已获取 / 成功」。
      if (data.return_address_push_status === 'success') {
        message.success('退货地址已获取并回传');
      } else if (data.return_address_push_status === 'manual_pending') {
        message.warning('当前履约渠道不支持自动获取退货地址，已生成人工待办');
      } else {
        message.warning('退货地址获取失败，请人工维护地址库');
      }
      void queryClient.invalidateQueries({ queryKey: ['after-sales'] });
    },
    onError: () => {
      message.warning('当前履约渠道不支持自动获取退货地址（能力边界），请人工维护地址库');
    },
  });

  const responsibilityMutation = useMutation({
    mutationFn: (body: { id: number; values: ResponsibilityFormValues }) =>
      updateResponsibility(body.id, {
        responsibility: body.values.responsibility,
        note: body.values.note || undefined,
      }),
    onSuccess: () => {
      message.success('责任归属已更新');
      setResponsibilityId(null);
      responsibilityForm.resetFields();
      void queryClient.invalidateQueries({ queryKey: ['after-sales'] });
    },
  });

  const columns = useMemo<ColumnsType<AfterSaleVo>>(
    () => [
      { title: 'ID', dataIndex: 'id', width: 70 },
      {
        title: '订单',
        dataIndex: 'order_id',
        width: 90,
        render: (value: number, record) => (
          <Typography.Link onClick={() => setDetailId(record.id)}>#{value}</Typography.Link>
        ),
      },
      {
        title: '平台退款号',
        dataIndex: 'platform_refund_no',
        width: 180,
        render: (v?: string | null) => (v ? <span className="erp-mono">{v}</span> : '-'),
      },
      {
        title: '退款金额',
        dataIndex: 'refund_amount',
        width: 110,
        render: (value: string | null) => <MoneyText value={value} />,
      },
      {
        title: '处理状态',
        dataIndex: 'handling_status',
        width: 110,
        render: (value: string) => <StatusTag enumKey="AfterSaleStatus" value={value} />,
      },
      {
        title: '责任归属',
        dataIndex: 'responsibility',
        width: 110,
        render: (value: string | null) => <StatusTag enumKey="Responsibility" value={value} />,
      },
      {
        title: '1688 退款',
        key: 'refund1688',
        width: 160,
        render: (_value: unknown, record) => (
          <Space direction="vertical" size={0}>
            <span>{record.refund_1688_status ?? '-'}</span>
            <span className="erp-mono" style={{ fontSize: 12 }}>
              {record.refund_1688_no ?? ''}
            </span>
          </Space>
        ),
      },
      {
        title: '地址回传',
        dataIndex: 'return_address_push_status',
        width: 120,
        render: (value: string | null) =>
          value === 'success' ? (
            <Tag color="green">已回传</Tag>
          ) : value === 'manual_pending' ? (
            <Tag color="gold">待人工处理</Tag>
          ) : value === 'failed' ? (
            <Tag color="red">失败</Tag>
          ) : (
            '-'
          ),
      },
      { title: '创建时间', dataIndex: 'created_at', width: 170, render: (v: string) => formatTime(v) },
      {
        title: '操作',
        key: 'actions',
        width: 250,
        fixed: 'right',
        render: (_value: unknown, record) => (
          <Space size="small" wrap>
            <Typography.Link onClick={() => setDetailId(record.id)}>详情</Typography.Link>
            <Typography.Link
              onClick={() => {
                setRefundId(record.id);
                refundForm.setFieldsValue({ refund_amount: record.refund_amount ?? '' });
              }}
            >
              提交退款
            </Typography.Link>
            <Typography.Link onClick={() => addressMutation.mutate(record.id)}>退货地址</Typography.Link>
            <Typography.Link
              onClick={() => {
                setResponsibilityId(record.id);
                responsibilityForm.setFieldsValue({ responsibility: 'supplier' });
              }}
            >
              责任归属
            </Typography.Link>
          </Space>
        ),
      },
    ],
    [addressMutation, refundForm, responsibilityForm],
  );

  const detail = detailQuery.data;

  return (
    <PageContainer
      title="售后管理"
      subTitle="退款提交 / 退货地址获取与回传 / 责任归属标记；第三方能力不支持时降级为人工待办"
      loading={listQuery.isLoading}
      extra={<Button onClick={resetFilters}>重置筛选</Button>}
    >
      <Form
        layout="inline"
        style={{ marginBottom: 12, rowGap: 8 }}
        initialValues={filters}
        onFinish={(values: AfterSaleFilterValues) => setFilters(values)}
      >
        <Form.Item name="order_id" label="订单 ID">
          <Input allowClear placeholder="如 1001" style={{ width: 140 }} />
        </Form.Item>
        <Form.Item name="handling_status" label="处理状态">
          <Select allowClear placeholder="全部" style={{ width: 130 }} options={statusOptions} />
        </Form.Item>
        <Form.Item name="responsibility" label="责任归属">
          <Select allowClear placeholder="全部" style={{ width: 130 }} options={responsibilityOptions} />
        </Form.Item>
        <Form.Item>
          <Button type="primary" htmlType="submit">
            查询
          </Button>
        </Form.Item>
      </Form>

      <Table<AfterSaleVo>
        rowKey="id"
        size="small"
        scroll={{ x: 1500 }}
        columns={columns}
        dataSource={listQuery.data?.items ?? []}
        pagination={{ ...tablePagination, total: listQuery.data?.total ?? 0 }}
        onChange={onTableChange}
      />

      {/* 详情 */}
      <Drawer
        width={760}
        title={detail ? `售后单 #${detail.id}` : '售后详情'}
        open={detailId !== null}
        onClose={() => setDetailId(null)}
      >
        {detail ? (
          <>
            <Descriptions bordered size="small" column={2}>
              <Descriptions.Item label="关联订单">#{detail.order_id}</Descriptions.Item>
              <Descriptions.Item label="平台退款号">{detail.platform_refund_no ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="退款金额">
                <MoneyText value={detail.refund_amount} />
              </Descriptions.Item>
              <Descriptions.Item label="处理状态">
                <StatusTag enumKey="AfterSaleStatus" value={detail.handling_status} />
              </Descriptions.Item>
              <Descriptions.Item label="责任归属">
                <StatusTag enumKey="Responsibility" value={detail.responsibility} />
              </Descriptions.Item>
              <Descriptions.Item label="1688 退款状态">{detail.refund_1688_status ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="1688 退款单号">{detail.refund_1688_no ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="地址回传">
                {detail.return_address_push_status === 'manual_pending' ? (
                  <Tag color="gold">待人工处理</Tag>
                ) : (
                  (detail.return_address_push_status ?? '-')
                )}
              </Descriptions.Item>
              <Descriptions.Item label="退款原因" span={2}>
                {detail.refund_reason ?? '-'}
              </Descriptions.Item>
              <Descriptions.Item label="创建时间">{formatTime(detail.created_at)}</Descriptions.Item>
            </Descriptions>

            {detail.return_address_json ? (
              <>
                <Typography.Title level={5} style={{ marginTop: 16 }}>
                  退货地址
                </Typography.Title>
                <Descriptions bordered size="small" column={1}>
                  {Object.entries(detail.return_address_json).map(([key, value]) => (
                    <Descriptions.Item key={key} label={key}>
                      {value}
                    </Descriptions.Item>
                  ))}
                </Descriptions>
              </>
            ) : null}
          </>
        ) : null}
      </Drawer>

      {/* 提交退款 */}
      <Modal
        title={refundId ? `提交 1688 退款 · 售后 #${refundId}` : '提交退款'}
        open={refundId !== null}
        onCancel={() => setRefundId(null)}
        onOk={() => refundForm.submit()}
        confirmLoading={refundMutation.isPending}
        okText="提交"
        cancelText="取消"
        destroyOnHidden
      >
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="退款动作由履约适配器执行"
          description="当前本地兜底（local_csv）通道不支持 submit_refund，提交后会生成「ERP 退款待办」状态为待人工处理，需人工到 1688 完成退款；界面不会显示「已退款 / 成功」。"
        />
        <Form
          form={refundForm}
          layout="vertical"
          onFinish={(values: RefundFormValues) => {
            if (refundId === null) return;
            refundMutation.mutate({ id: refundId, values });
          }}
        >
          <Form.Item
            name="refund_amount"
            label="退款金额（元）"
            rules={[{ required: true }, { pattern: /^\d+(\.\d{1,2})?$/, message: '格式如 29.90' }]}
          >
            <Input />
          </Form.Item>
          <Form.Item name="reason" label="退款原因" rules={[{ required: true }]}>
            <Input.TextArea rows={3} />
          </Form.Item>
        </Form>
      </Modal>

      {/* 责任归属 */}
      <Modal
        title={responsibilityId ? `责任归属 · 售后 #${responsibilityId}` : '责任归属'}
        open={responsibilityId !== null}
        onCancel={() => setResponsibilityId(null)}
        onOk={() => responsibilityForm.submit()}
        confirmLoading={responsibilityMutation.isPending}
        okText="保存"
        cancelText="取消"
        destroyOnHidden
      >
        <Form
          form={responsibilityForm}
          layout="vertical"
          onFinish={(values: ResponsibilityFormValues) => {
            if (responsibilityId === null) return;
            responsibilityMutation.mutate({ id: responsibilityId, values });
          }}
        >
          <Form.Item name="responsibility" label="责任归属" rules={[{ required: true }]}>
            <Select options={responsibilityOptions} />
          </Form.Item>
          <Form.Item name="note" label="说明">
            <Input.TextArea rows={3} placeholder="如：供应商发错货，已与供应商协商赔付" />
          </Form.Item>
        </Form>
      </Modal>
    </PageContainer>
  );
}
