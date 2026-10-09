/**
 * 占位文件 —— 不是真实配置，不含任何令牌。
 *
 * 真实内容由 `backend/container_entry.py` 在**容器启动时**覆盖写入
 * `web/dist/runtime-config.js`，形如：
 *     window.__ERP_RUNTIME__ = {"adminToken": "..."};
 *
 * 为什么令牌不能在构建期注入：镜像由 GitHub Actions 构建、随公开仓库推到 GHCR，
 * 谁都能拉取 —— 把令牌编进前端产物等于公开发布。详见 `web/src/api/client.ts`。
 *
 * 为什么还要留这个占位：让 `vite build` 不因 index.html 引用缺失文件而告警，
 * 同时开发态浏览器控制台不会每次都刷一条 404。取不到值时前端回退到
 * `web/.env.local` 的 `VITE_ADMIN_TOKEN`，行为不变。
 */
window.__ERP_RUNTIME__ = {};
