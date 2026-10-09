import { fileURLToPath, URL } from 'node:url';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

/**
 * 后端代理目标。
 *
 * ★ 这里**必须写 127.0.0.1 而不是 localhost**：
 *   Windows 上 Node 解析 `localhost` 会优先走 IPv6 `::1`，而 uvicorn 默认只监听
 *   `127.0.0.1`（IPv4），结果是 dev server 对每个 /api 请求返回 502
 *   （`upstream connect failed ... os error 10061`），页面全白且很难排查。
 *   仍允许用环境变量覆盖，方便后端换端口 / 换机器。
 */
const PROXY_TARGET: string = process.env.VITE_API_PROXY_TARGET ?? 'http://127.0.0.1:8000';

/**
 * Vite 配置。
 * 开发态所有 /api 请求代理到后端 FastAPI，
 * 生产态由部署方（nginx / 静态托管）转发，前端只认相对路径 /api。
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    open: false,
    proxy: {
      '/api': {
        target: PROXY_TARGET,
        changeOrigin: true,
        // 后端路径为 /api/v1/*，前端 baseURL 亦为 /api/v1，此处无需 rewrite
      },
      '/static': {
        target: PROXY_TARGET,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 2000,
    rollupOptions: {
      output: {
        // 拆分 react 与 antd 两个大 vendor，提升缓存命中与首屏加载
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom'],
          antd: ['antd', '@ant-design/icons'],
        },
      },
    },
  },
});
