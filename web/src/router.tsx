import type { ReactNode } from 'react';
import { Route } from 'react-router-dom';
import {
  ApiOutlined,
  AppstoreOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  FileSearchOutlined,
  ExperimentOutlined,
  LockOutlined,
  NodeIndexOutlined,
  CloudUploadOutlined,
  CloudDownloadOutlined,
  RobotOutlined,
  RollbackOutlined,
  SafetyCertificateOutlined,
  ShopOutlined,
  ShoppingCartOutlined,
} from '@ant-design/icons';

import AiTasks from './pages/AiTasks';
import AiStudio from './pages/AiStudio';
import AfterSales from './pages/AfterSales';
import AuditLogs from './pages/AuditLogs';
import Dashboard from './pages/Dashboard';
import Inventory from './pages/Inventory';
import ListingProducts from './pages/ListingProducts';
import Orders from './pages/Orders';
import PublishTasks from './pages/PublishTasks';
import SettingsAdapters from './pages/SettingsAdapters';
import SettingsBackup from './pages/SettingsBackup';
import SettingsCredentials from './pages/SettingsCredentials';
import SettingsPermissions from './pages/SettingsPermissions';
import SkuMappings from './pages/SkuMappings';
import SourceProducts from './pages/SourceProducts';

/** 单个路由项：同时用于生成 <Route> 与侧边栏菜单（同一份配置，禁止两处维护） */
export interface RouteItem {
  /** 路由绝对路径 */
  path: string;
  /** 菜单与页面标题 */
  label: string;
  /** 侧边栏图标 */
  icon?: ReactNode;
  /** 页面元素 */
  element: ReactNode;
}

/** 侧边栏分组 */
export interface MenuGroup {
  key: string;
  label: string;
  items: RouteItem[];
}

/**
 * 路由 + 菜单的唯一配置源。
 * 共 14 个页面：原 18 页中 5 个合并为 Tab / Drawer（素材库、AI 审核、
 * 半自动素材包、售后），P17 权限与越权告警按 PM 要求**保留独立页面**，
 * 另新增 `/ai-studio`（AI 内容工作台：三种新能力的发起与取用界面）。
 */
export const menuGroups: MenuGroup[] = [
  {
    key: 'grp-listing',
    label: '上新链路',
    items: [
      { path: '/', label: '工作台', icon: <DashboardOutlined />, element: <Dashboard /> },
      {
        path: '/source-products',
        label: '货源商品库',
        icon: <AppstoreOutlined />,
        element: <SourceProducts />,
      },
      { path: '/ai-tasks', label: 'AI 重构任务', icon: <RobotOutlined />, element: <AiTasks /> },
      {
        path: '/ai-studio',
        label: 'AI 内容工作台',
        icon: <ExperimentOutlined />,
        element: <AiStudio />,
      },
    ],
  },
  {
    key: 'grp-mapping-publish',
    label: '映射与上架',
    items: [
      {
        path: '/sku-mappings',
        label: 'SKU 映射管理',
        icon: <NodeIndexOutlined />,
        element: <SkuMappings />,
      },
      {
        path: '/publish-tasks',
        label: '上架任务管理',
        icon: <CloudUploadOutlined />,
        element: <PublishTasks />,
      },
      {
        path: '/listing-products',
        label: '平台商品管理',
        icon: <ShopOutlined />,
        element: <ListingProducts />,
      },
    ],
  },
  {
    key: 'grp-fulfillment',
    label: '履约与售后',
    items: [
      { path: '/orders', label: '订单履约看板', icon: <ShoppingCartOutlined />, element: <Orders /> },
      { path: '/after-sales', label: '售后管理', icon: <RollbackOutlined />, element: <AfterSales /> },
      {
        path: '/inventory',
        label: '库存与价格监控',
        icon: <DatabaseOutlined />,
        element: <Inventory />,
      },
    ],
  },
  {
    key: 'grp-system',
    label: '系统',
    items: [
      {
        path: '/settings/credentials',
        label: '平台凭证设置',
        icon: <SafetyCertificateOutlined />,
        element: <SettingsCredentials />,
      },
      {
        path: '/settings/adapters',
        label: '适配器设置',
        icon: <ApiOutlined />,
        element: <SettingsAdapters />,
      },
      {
        path: '/settings/permissions',
        label: '权限与越权告警',
        icon: <LockOutlined />,
        element: <SettingsPermissions />,
      },
      {
        path: '/settings/audit-logs',
        label: '审计日志',
        icon: <FileSearchOutlined />,
        element: <AuditLogs />,
      },
      {
        path: '/settings/backup',
        label: '数据备份',
        icon: <CloudDownloadOutlined />,
        element: <SettingsBackup />,
      },
    ],
  },
];

/** 扁平化后的全部路由项（菜单与路由共用） */
export const routeItems: RouteItem[] = menuGroups.flatMap((group) => group.items);

/** 由配置生成 <Route> 元素集合（首页用 index route） */
export function buildRouteElements(): ReactNode[] {
  return routeItems.map((item) =>
    item.path === '/' ? (
      <Route key={item.path} index element={item.element} />
    ) : (
      <Route key={item.path} path={item.path} element={item.element} />
    ),
  );
}

/** 按 pathname 反查页面标题（用于 Content 头部面包屑/标题） */
export function findRouteTitle(pathname: string): string {
  const hit = routeItems.find((item) => item.path === pathname);
  return hit ? hit.label : '自用电商 ERP';
}
