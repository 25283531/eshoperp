import type { ReactNode } from 'react';
import { Alert, Card, Col, Row, Skeleton, Space, Typography } from 'antd';

export interface PageContainerProps {
  /** 页面标题 */
  title: string;
  /** 标题右侧操作区 */
  extra?: ReactNode;
  /** 标题下方描述 */
  subTitle?: ReactNode;
  /** 顶部提示区（如红线说明） */
  alert?: ReactNode;
  /** 内容区（可为 Tabs / Table 等） */
  children: ReactNode;
  /** 加载态 */
  loading?: boolean;
}

/**
 * 页面骨架：标题 + 描述 + 操作区 + 可选提示条 + 内容。
 * 全站 12 个页面统一使用，保证视觉一致。
 */
export default function PageContainer(props: PageContainerProps): JSX.Element {
  const { title, extra, subTitle, alert, children, loading = false } = props;

  return (
    <div className="erp-page-content">
      <Row align="middle" justify="space-between" style={{ marginBottom: 12 }}>
        <Col>
          <Space direction="vertical" size={0}>
            <Typography.Title level={4} style={{ margin: 0 }}>
              {title}
            </Typography.Title>
            {subTitle ? (
              <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                {subTitle}
              </Typography.Text>
            ) : null}
          </Space>
        </Col>
        {extra ? <Col>{extra}</Col> : null}
      </Row>

      {alert ? <div style={{ marginBottom: 12 }}>{alert}</div> : null}

      <Card styles={{ body: { padding: 16 } }}>
        {loading ? <Skeleton active paragraph={{ rows: 6 }} /> : children}
      </Card>
    </div>
  );
}

/** 便捷：生成一条提示 Alert（type 与文案由调用方决定） */
export function PageAlert(props: {
  type: 'info' | 'warning' | 'error' | 'success';
  message: ReactNode;
  description?: ReactNode;
  showIcon?: boolean;
}): JSX.Element {
  return (
    <Alert
      type={props.type}
      message={props.message}
      description={props.description}
      showIcon={props.showIcon ?? true}
    />
  );
}
