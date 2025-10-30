#!/bin/bash
# 推送 Docker 镜像到 Docker Hub Private Registry
# 功能: 将本地镜像打标签并推送到私有仓库

set -e

echo "========================================="
echo "Docker Hub 镜像推送工具"
echo "========================================="

# 配置区域 - 请修改为您的 Docker Hub 用户名
DOCKER_USERNAME=""

# 如果未配置用户名，提示用户输入
if [ -z "$DOCKER_USERNAME" ]; then
    echo ""
    read -p "请输入您的 Docker Hub 用户名: " DOCKER_USERNAME
    if [ -z "$DOCKER_USERNAME" ]; then
        echo "错误: Docker Hub 用户名不能为空"
        exit 1
    fi
fi

# 镜像配置
LOCAL_BACKEND_IMAGE="fashion-crm-backend:latest"
LOCAL_FRONTEND_IMAGE="fashion-crm-frontend:latest"

REMOTE_BACKEND_IMAGE="${DOCKER_USERNAME}/fashion-crm-backend:latest"
REMOTE_FRONTEND_IMAGE="${DOCKER_USERNAME}/fashion-crm-frontend:latest"

echo ""
echo "步骤 1/5: 检查本地镜像..."
echo "----------------------------------------"

# 检查镜像是否存在
if ! docker images --format "{{.Repository}}:{{.Tag}}" | grep -q "^${LOCAL_BACKEND_IMAGE}$"; then
    echo "✗ 后端镜像不存在: $LOCAL_BACKEND_IMAGE"
    echo "  请先运行: docker-compose build backend"
    exit 1
fi
echo "✓ 找到后端镜像: $LOCAL_BACKEND_IMAGE"

if ! docker images --format "{{.Repository}}:{{.Tag}}" | grep -q "^${LOCAL_FRONTEND_IMAGE}$"; then
    echo "✗ 前端镜像不存在: $LOCAL_FRONTEND_IMAGE"
    echo "  请先运行: docker-compose build frontend"
    exit 1
fi
echo "✓ 找到前端镜像: $LOCAL_FRONTEND_IMAGE"

echo ""
echo "步骤 2/5: 登录 Docker Hub..."
echo "----------------------------------------"

# 检查是否已登录
if docker info 2>/dev/null | grep -q "Username: $DOCKER_USERNAME"; then
    echo "✓ 已登录 Docker Hub (用户: $DOCKER_USERNAME)"
else
    echo "请登录 Docker Hub..."
    if docker login; then
        echo "✓ Docker Hub 登录成功"
    else
        echo "✗ Docker Hub 登录失败"
        exit 1
    fi
fi

echo ""
echo "步骤 3/5: 为镜像打标签..."
echo "----------------------------------------"

# 给镜像打标签
echo "标记后端镜像: $LOCAL_BACKEND_IMAGE -> $REMOTE_BACKEND_IMAGE"
docker tag "$LOCAL_BACKEND_IMAGE" "$REMOTE_BACKEND_IMAGE"

echo "标记前端镜像: $LOCAL_FRONTEND_IMAGE -> $REMOTE_FRONTEND_IMAGE"
docker tag "$LOCAL_FRONTEND_IMAGE" "$REMOTE_FRONTEND_IMAGE"

# 如果需要版本标签，可以添加
VERSION_TAG=$(date '+%Y%m%d-%H%M%S')
REMOTE_BACKEND_VERSION="${DOCKER_USERNAME}/fashion-crm-backend:${VERSION_TAG}"
REMOTE_FRONTEND_VERSION="${DOCKER_USERNAME}/fashion-crm-frontend:${VERSION_TAG}"

echo ""
read -p "是否同时创建带版本号的标签? (${VERSION_TAG}) (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    docker tag "$LOCAL_BACKEND_IMAGE" "$REMOTE_BACKEND_VERSION"
    docker tag "$LOCAL_FRONTEND_IMAGE" "$REMOTE_FRONTEND_VERSION"
    echo "✓ 已创建版本标签: $VERSION_TAG"
    USE_VERSION_TAG=true
else
    USE_VERSION_TAG=false
fi

echo ""
echo "步骤 4/5: 推送镜像到 Docker Hub..."
echo "----------------------------------------"

# 推送后端镜像
echo "正在推送后端镜像..."
echo "这可能需要几分钟，取决于网络速度..."
if docker push "$REMOTE_BACKEND_IMAGE"; then
    echo "✓ 后端镜像推送成功: $REMOTE_BACKEND_IMAGE"
else
    echo "✗ 后端镜像推送失败"
    exit 1
fi

if [ "$USE_VERSION_TAG" = true ]; then
    docker push "$REMOTE_BACKEND_VERSION"
    echo "✓ 后端版本镜像推送成功: $REMOTE_BACKEND_VERSION"
fi

# 推送前端镜像
echo ""
echo "正在推送前端镜像..."
if docker push "$REMOTE_FRONTEND_IMAGE"; then
    echo "✓ 前端镜像推送成功: $REMOTE_FRONTEND_IMAGE"
else
    echo "✗ 前端镜像推送失败"
    exit 1
fi

if [ "$USE_VERSION_TAG" = true ]; then
    docker push "$REMOTE_FRONTEND_VERSION"
    echo "✓ 前端版本镜像推送成功: $REMOTE_FRONTEND_VERSION"
fi

echo ""
echo "步骤 5/5: 生成镜像信息文件..."
echo "----------------------------------------"

# 创建导出目录
EXPORT_DIR="./docker_export"
mkdir -p "$EXPORT_DIR"

# 生成镜像信息文件
cat > "$EXPORT_DIR/dockerhub_images.txt" << EOF
Docker Hub 镜像信息
====================
推送时间: $(date '+%Y-%m-%d %H:%M:%S')
Docker Hub 用户名: $DOCKER_USERNAME

镜像列表:
----------------------------------------
后端镜像:
  - $REMOTE_BACKEND_IMAGE
EOF

if [ "$USE_VERSION_TAG" = true ]; then
    echo "  - $REMOTE_BACKEND_VERSION" >> "$EXPORT_DIR/dockerhub_images.txt"
fi

cat >> "$EXPORT_DIR/dockerhub_images.txt" << EOF

前端镜像:
  - $REMOTE_FRONTEND_IMAGE
EOF

if [ "$USE_VERSION_TAG" = true ]; then
    echo "  - $REMOTE_FRONTEND_VERSION" >> "$EXPORT_DIR/dockerhub_images.txt"
fi

cat >> "$EXPORT_DIR/dockerhub_images.txt" << EOF

MySQL 镜像 (官方):
  - mysql:8.0

在新电脑上拉取镜像的命令:
----------------------------------------
# 登录 Docker Hub (如果是私有镜像)
docker login

# 拉取后端镜像
docker pull $REMOTE_BACKEND_IMAGE

# 拉取前端镜像
docker pull $REMOTE_FRONTEND_IMAGE

# 拉取 MySQL 镜像
docker pull mysql:8.0

设置镜像可见性:
----------------------------------------
如果需要设置为私有镜像，请访问:
https://hub.docker.com/repository/docker/$DOCKER_USERNAME/fashion-crm-backend/general
https://hub.docker.com/repository/docker/$DOCKER_USERNAME/fashion-crm-frontend/general

在 Settings 中可以设置为 Private
EOF

echo "✓ 镜像信息已保存到: $EXPORT_DIR/dockerhub_images.txt"

echo ""
echo "========================================="
echo "镜像推送完成!"
echo "========================================="
echo ""
echo "推送的镜像:"
echo "  后端: $REMOTE_BACKEND_IMAGE"
echo "  前端: $REMOTE_FRONTEND_IMAGE"

if [ "$USE_VERSION_TAG" = true ]; then
    echo ""
    echo "版本镜像:"
    echo "  后端: $REMOTE_BACKEND_VERSION"
    echo "  前端: $REMOTE_FRONTEND_VERSION"
fi

echo ""
echo "查看镜像:"
echo "  https://hub.docker.com/u/$DOCKER_USERNAME"
echo ""
echo "下一步:"
echo "  1. 访问 Docker Hub 设置镜像为私有 (如需要)"
echo "  2. 在新电脑上运行 ./pull_from_dockerhub.sh 拉取镜像"
echo "  3. 或者运行 ./export_database.sh 导出数据库"
echo "========================================="

# 清理本地标签 (可选)
echo ""
read -p "是否清理本地的远程标签? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    docker rmi "$REMOTE_BACKEND_IMAGE" 2>/dev/null || true
    docker rmi "$REMOTE_FRONTEND_IMAGE" 2>/dev/null || true
    if [ "$USE_VERSION_TAG" = true ]; then
        docker rmi "$REMOTE_BACKEND_VERSION" 2>/dev/null || true
        docker rmi "$REMOTE_FRONTEND_VERSION" 2>/dev/null || true
    fi
    echo "✓ 本地远程标签已清理"
fi
