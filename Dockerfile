# 自用电商 ERP —— 容器镜像（目标：OpenWrt 路由器上的 Docker，linux/amd64）
#
# ★ 为什么必须多阶段构建：
#   前端 `vite build`（含 `tsc --noEmit`）内存开销较大，而路由器内存常见 256MB–1GB。
#   若把 Node 打进运行时镜像，既浪费 Flash 空间，又让运行时白白多一个不需要的运行时。
#   这里让构建阶段完成前端编译，运行时镜像只含：Python 运行时 + 后端代码 + 前端静态文件。
#
# ★ 目录结构必须保留 `backend/` 这一层：
#   `app/core/config.py` 用 `PROJECT_ROOT = Path(__file__).resolve().parents[3]`
#   推导项目根。若把 `backend/` 内容直接摊平到 /app，
#   PROJECT_ROOT 会变成 `/`，数据就落到 `/data` 而不是 `/app/data`。
#   保持 `/app/backend/app/core/config.py` ⇒ PROJECT_ROOT 正好是 `/app`。

# =====================================================================
# Stage 1：构建前端
# =====================================================================
FROM node:22-alpine AS frontend-builder

WORKDIR /build/web

# 先只拷依赖清单，利用 Docker 层缓存：源码变了但依赖没变时不重新 npm ci
COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web/ ./
RUN npm run build

# =====================================================================
# Stage 2：Python 运行时
# =====================================================================
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # 数据目录：唯一需要持久化的位置，整个挂到宿主机
    ERP_DATA_DIR=/app/data

WORKDIR /app

# 只装运行时依赖（dev 依赖 pytest/ruff/mypy **不进镜像**）
COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# 后端代码（保持 backend/ 层级，见顶部说明）
COPY backend/ /app/backend/

# 前端构建产物（后端用 StaticFiles 挂载它，运行时是一个进程一个端口）
COPY --from=frontend-builder /build/web/dist /app/web/dist

# alembic.ini 与迁移脚本需要在 backend 目录下被找到
# （container_entry.py 会按 backend/alembic.ini 定位）

# 数据目录先建立，确保挂载点存在且属主正确
RUN mkdir -p /app/data

EXPOSE 8000

# ★ 健康检查：后端提供 /api/v1/health
#   注意用 127.0.0.1 而非 0.0.0.0 —— 容器内自检，避免依赖外部网络
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4).status==200 else 1)"

# ★ 入口：自建库 → 起服务
#   迁移逻辑（alembic upgrade head，以及"有业务表无版本号"时 stamp head）
#   从桌面 exe 的 desktop_entry.py 平移而来，容器首次启动同样需要它。
CMD ["python", "/app/backend/container_entry.py"]
