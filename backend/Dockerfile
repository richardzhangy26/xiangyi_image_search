# syntax=docker/dockerfile:1

# 多阶段构建 - 构建阶段
FROM python:3.11-slim AS builder

WORKDIR /build

# 安装构建依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    pkg-config \
    default-libmysqlclient-dev \
    && rm -rf /var/lib/apt/lists/*

# 创建虚拟环境
RUN python -m venv /opt/venv

# 复制依赖文件
COPY requirements.txt .

# 使用 venv 中的 pip 安装依赖
# 这能确保所有包和可执行文件都在 /opt/venv 中
RUN /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# -------------------------------------------------------------------

# 生产阶段 - 精简镜像
FROM python:3.11-slim

WORKDIR /app

# 安装运行时依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    default-libmysqlclient-dev \
    libgomp1 \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# 创建非 root 用户运行应用
RUN useradd -m -s /bin/bash -u 1000 appuser && \
    mkdir -p /app/data/product_search/images \
             /app/data/product_search/db \
             /app/data/product_search/index \
             /app/uploads

# 复制应用代码并直接赋予正确的所有权，避免额外 chown
COPY --chown=appuser:appuser . /app

# 从构建阶段复制虚拟环境，同样保留权限
COPY --from=builder --chown=appuser:appuser /opt/venv /opt/venv

# 切换到非 root 用户
USER appuser

# 将 venv 的 bin 目录添加到 PATH
# 这样 CMD 才能找到 gunicorn
ENV PATH="/opt/venv/bin:$PATH"

# 暴露端口
EXPOSE 5000

# 健康检查
# 注意：现在它會使用 venv 中的 python 和 requests
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:5000/api/health', timeout=5)" || exit 1

# 使用 Gunicorn 运行生产环境应用
# 由於 PATH 已經設定，系統會自動找到 /opt/venv/bin/gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "4", "--threads", "2", "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
