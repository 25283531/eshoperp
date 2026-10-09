import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';

import MainLayout from './layouts/MainLayout';
import { buildRouteElements } from './router';

/**
 * 应用根组件：
 * BrowserRouter + 单一布局路由（MainLayout）+ 由 router.tsx 配置生成的页面路由。
 */
export default function App(): JSX.Element {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<MainLayout />}>
          {buildRouteElements()}
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
