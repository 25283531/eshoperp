import { useEffect, useMemo, useState } from 'react';
import { Badge, Layout, Menu, Space, Tag, Tooltip, Typography, theme } from 'antd';
import type { MenuProps } from 'antd';
import { MenuFoldOutlined, MenuUnfoldOutlined } from '@ant-design/icons';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';

import { getSystemStatusBar } from '@/api/system';
import { findRouteTitle, menuGroups } from '@/router';

const { Header, Sider, Content } = Layout;

/**
 * 状态栏轮询间隔（毫秒）：60 秒。
 * /system/status-bar 只读 SystemSetting 缓存 + audit_log 计数，不触发任何外部 HTTP，
 * 实测 ~70ms，因此 60s 轮询是安全的。顶栏禁止再拆成 /health + /adapters/* 多个轮询。
 */
const STATUS_BAR_POLL_MS = 60_000;

/** 页面不可见（切到后台标签页）时是否暂停轮询 */
function usePageVisible(): boolean {
  const [visible, setVisible] = useState<boolean>(
    typeof document === 'undefined' ? true : document.visibilityState === 'visible',
  );

  useEffect(() => {
    const onChange = (): void => setVisible(document.visibilityState === 'visible');
    document.addEventListener('visibilitychange', onChange);
    return () => document.removeEventListener('visibilitychange', onChange);
  }, []);

  return visible;
}

/**
 * 主布局：侧边栏（由 router.tsx 同一份配置驱动） + 顶栏状态条。
 *
 * ★ 越权告警的第一层发现入口：顶栏常驻「未处置越权数」红点，
 *   点一下直达 /settings/permissions（第二层：逐条处置页）。
 *   顶栏全部状态统一取自 GET /system/status-bar 这一个接口，页面隐藏时暂停轮询。
 */
export default function MainLayout(): JSX.Element {
  const navigate = useNavigate();
  const location = useLocation();
  const [collapsed, setCollapsed] = useState<boolean>(false);
  const { token } = theme.useToken();
  const pageVisible = usePageVisible();

  const statusBarQuery = useQuery({
    queryKey: ['system', 'status-bar'],
    queryFn: getSystemStatusBar,
    refetchInterval: pageVisible ? STATUS_BAR_POLL_MS : false,
    retry: false,
  });

  const statusBar = statusBarQuery.data;
  const violationCount = statusBar?.unhandled_violation_count ?? 0;
  /**
   * ★ 上架模式标签以后端下发为准（`listing.mode` 默认已从 mock 改为 manual，
   * 前端不得写死 mock）。后端不可达时显示「未知」，不臆断。
   */
  const listingModeLabel = statusBar?.listing_mode_label ?? statusBar?.listing_mode ?? '未知';
  const activeAdapterLabel =
    statusBar?.active_fulfillment_adapter_label ?? statusBar?.active_fulfillment_adapter ?? '-';
  /**
   * ★ 是否 Mock 一律以后端 `is_mock_active` 为准（不再按 listing_mode === 'mock' 推断）。
   * 仅在后端完全不可达时才兜底 true —— 宁可多警告，也不能漏掉 Mock 提示。
   */
  const isMockMode = statusBar?.is_mock_active ?? true;
  const healthStatus = statusBar?.health_status ?? 'unknown';
  const unhealthyItems = statusBar?.unhealthy_items ?? [];
  const latestViolation = statusBar?.latest_violation ?? null;

  const healthColor =
    healthStatus === 'healthy'
      ? 'green'
      : healthStatus === 'degraded'
        ? 'orange'
        : healthStatus === 'down'
          ? 'red'
          : 'default';

  const healthLabelText =
    healthStatus === 'healthy'
      ? '正常'
      : healthStatus === 'degraded'
        ? '降级'
        : healthStatus === 'down'
          ? '不可用'
          : '未知';

  /** 不健康项明细：逐项列出「名称 — 说明」 */
  const healthTip =
    unhealthyItems.length > 0
      ? unhealthyItems.map((item) => `${item.name}：${item.message}`).join('；')
      : '系统健康状态（来自 GET /system/status-bar，60 秒轮询）';

  /** 越权告警 Tooltip：展示最近一条被拒绝的 scope */
  const violationTip = latestViolation
    ? `最近一条：${latestViolation.adapter_name ?? '-'} 申请了 ${latestViolation.denied_scopes.join('、')}，已被系统拒绝并落审计`
    : '第三方越权申请记录；未处置项需逐条确认（处置 ≠ 放行）';

  const menuItems = useMemo<MenuProps['items']>(
    () =>
      menuGroups.map((group) => ({
        key: group.key,
        type: 'group' as const,
        label: group.label,
        children: group.items.map((item) => ({
          key: item.path,
          icon: item.icon,
          label: item.label,
        })),
      })),
    [],
  );

  return (
    <Layout style={{ minHeight: '100vh' }}>
      {isMockMode ? (
        <div className="erp-mock-banner">
          <Tag color="orange" style={{ marginInlineEnd: 0 }}>
            MOCK
          </Tag>
          <span>
            当前上架模式为「{listingModeLabel}」：上架产出的商品 / 映射 / 订单均为模拟数据，
            <b>不参与真实履约</b>，仅用于链路演示与联调。
          </span>
        </div>
      ) : null}

      <Layout style={{ minHeight: isMockMode ? 'calc(100vh - 33px)' : '100vh' }}>
        <Sider collapsible collapsed={collapsed} trigger={null} width={208}>
          <div
            style={{
              height: 56,
              display: 'flex',
              alignItems: 'center',
              justifyContent: collapsed ? 'center' : 'flex-start',
              paddingInline: collapsed ? 0 : 16,
              color: '#fff',
              fontWeight: 600,
              letterSpacing: 1,
              borderBottom: '1px solid rgba(255,255,255,0.12)',
            }}
          >
            {collapsed ? 'ERP' : '自用电商 ERP'}
          </div>
          <Menu
            theme="dark"
            mode="inline"
            selectedKeys={[location.pathname]}
            items={menuItems}
            onClick={({ key }) => navigate(key)}
          />
        </Sider>

        <Layout>
          <Header
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              paddingInline: 16,
              borderBottom: `1px solid ${token.colorBorderSecondary}`,
            }}
          >
            <Space size="middle">
              <Typography.Text
                type="secondary"
                onClick={() => setCollapsed((prev) => !prev)}
                style={{ cursor: 'pointer', fontSize: 16 }}
              >
                {collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
              </Typography.Text>
              <Typography.Text strong>{findRouteTitle(location.pathname)}</Typography.Text>
              <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                自研只管上新 · 第三方只管履约 · 第三方永不持有商品编辑权
              </Typography.Text>
            </Space>

            <Space size="small">
              {/* ★ 越权告警第一层：未处置数量红点，任何页面都能看见 */}
              <Tooltip title={violationTip}>
                <Badge count={violationCount} dot={violationCount > 0} offset={[-2, 2]}>
                  <Tag
                    color={violationCount > 0 ? 'red' : 'default'}
                    style={{ cursor: 'pointer', marginInlineEnd: 0 }}
                    onClick={() => navigate('/settings/permissions')}
                  >
                    越权告警 {violationCount}
                  </Tag>
                </Badge>
              </Tooltip>

              <Tooltip title="当前生效的履约渠道（切换只影响新订单，在途订单按原渠道跑完）">
                <Tag
                  color={
                    statusBar?.active_fulfillment_adapter === 'local_csv' ? 'default' : 'blue'
                  }
                  style={{ cursor: 'pointer', marginInlineEnd: 0 }}
                  onClick={() => navigate('/settings/adapters')}
                >
                  履约渠道：{activeAdapterLabel}
                </Tag>
              </Tooltip>

              <Tooltip title="当前上架适配器模式（Mock 数据不参与真实履约）">
                <Tag
                  color={isMockMode ? 'orange' : 'green'}
                  style={{ cursor: 'pointer', marginInlineEnd: 0 }}
                  onClick={() => navigate('/settings/adapters')}
                >
                  上架模式：{listingModeLabel}
                </Tag>
              </Tooltip>

              <Tooltip title={healthTip}>
                <Tag color={healthColor} style={{ marginInlineEnd: 0 }}>
                  系统：{healthLabelText}
                </Tag>
              </Tooltip>
            </Space>
          </Header>

          <Content style={{ background: token.colorBgLayout }}>
            <Outlet />
          </Content>
        </Layout>
      </Layout>
    </Layout>
  );
}
