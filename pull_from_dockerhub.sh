#!/bin/bash
# 从 Docker Hub 拉取镜像并部署
# 功能: 在新电脑上拉取镜像、导入数据、启动服务

set -e

echo "========================================="
echo "Docker Hub 镜像拉取和部署工具"
echo "========================================="

# 配置区域 - 请修改为您的 Docker Hub 用户名
DOCKER_USERNAME=""

# 如果未配置用户名，提示用户输入
if [ -z "$DOCKER_USERNAME" ]; then
    echo ""
    read -p "请输入 Docker Hub 用户名: " DOCKER_USERNAME
    if [ -z "$DOCKER_USERNAME" ]; then
        echo "错误: Docker Hub 用户名不能为空"
        exit 1
    fi
fi

# 镜像配置
REMOTE_BACKEND_IMAGE="${DOCKER_USERNAME}/fashion-crm-backend:latest"
REMOTE_FRONTEND_IMAGE="${DOCKER_USERNAME}/fashion-crm-frontend:latest"
LOCAL_BACKEND_IMAGE="fashion-crm-backend:latest"
LOCAL_FRONTEND_IMAGE="fashion-crm-frontend:latest"

echo ""
echo "步骤 1/7: 检查环境..."
echo "----------------------------------------"

# 检查 Docker
if ! command -v docker &> /dev/null; then
    echo "✗ 未安装 Docker"
    echo "请先安装 Docker Desktop: https://www.docker.com/products/docker-desktop"
    exit 1
fi
echo "✓ Docker 已安装"

# 检查 Docker Compose
if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null; then
    echo "✗ 未安装 Docker Compose"
    exit 1
fi
echo "✓ Docker Compose 已安装"

echo ""
echo "步骤 2/7: 配置环境变量..."
echo "----------------------------------------"

# 检查 .env 文件
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        echo "未找到 .env 文件"
        read -p "是否从 .env.example 创建 .env 文件? (y/n) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            cp .env.example .env
            echo "✓ 已创建 .env 文件"
            echo ""
            echo "请编辑 .env 文件填入正确的配置值，然后重新运行此脚本"
            echo "需要配置的关键变量:"
            echo "  - DB_PASSWORD"
            echo "  - DASHSCOPE_API_KEY"
            echo "  - OSS_ACCESS_KEY_ID"
            echo "  - OSS_ACCESS_KEY_SECRET"
            echo "  - OSS_ENDPOINT"
            echo "  - OSS_BUCKET_NAME"
            exit 0
        else
            echo "✗ 需要 .env 文件才能继续"
            exit 1
        fi
    else
        echo "✗ 找不到 .env.example 文件"
        exit 1
    fi
fi

# 加载环境变量
export $(cat .env | grep -v '^#' | xargs)
echo "✓ 环境变量已加载"

# 验证必需的环境变量
REQUIRED_VARS=("DB_PASSWORD" "DB_NAME" "DASHSCOPE_API_KEY")
MISSING_VARS=()

for var in "${REQUIRED_VARS[@]}"; do
    if [ -z "${!var}" ]; then
        MISSING_VARS+=("$var")
    fi
done

if [ ${#MISSING_VARS[@]} -gt 0 ]; then
    echo "✗ 缺少必需的环境变量:"
    printf '  - %s\n' "${MISSING_VARS[@]}"
    echo "请检查 .env 文件"
    exit 1
fi

echo ""
echo "步骤 3/7: 登录 Docker Hub..."
echo "----------------------------------------"

# 检查是否已登录
if docker info 2>/dev/null | grep -q "Username: $DOCKER_USERNAME"; then
    echo "✓ 已登录 Docker Hub (用户: $DOCKER_USERNAME)"
else
    echo "请登录 Docker Hub 以拉取私有镜像..."
    if docker login; then
        echo "✓ Docker Hub 登录成功"
    else
        echo "✗ Docker Hub 登录失败"
        exit 1
    fi
fi

echo ""
echo "步骤 4/7: 拉取 Docker 镜像..."
echo "----------------------------------------"

# 拉取后端镜像
echo "正在拉取后端镜像: $REMOTE_BACKEND_IMAGE"
if docker pull "$REMOTE_BACKEND_IMAGE"; then
    # 重新标记为本地镜像名
    docker tag "$REMOTE_BACKEND_IMAGE" "$LOCAL_BACKEND_IMAGE"
    echo "✓ 后端镜像拉取成功"
else
    echo "✗ 后端镜像拉取失败"
    echo "  请检查:"
    echo "  1. Docker Hub 用户名是否正确"
    echo "  2. 镜像是否存在"
    echo "  3. 是否有权限访问该镜像"
    exit 1
fi

# 拉取前端镜像
echo ""
echo "正在拉取前端镜像: $REMOTE_FRONTEND_IMAGE"
if docker pull "$REMOTE_FRONTEND_IMAGE"; then
    # 重新标记为本地镜像名
    docker tag "$REMOTE_FRONTEND_IMAGE" "$LOCAL_FRONTEND_IMAGE"
    echo "✓ 前端镜像拉取成功"
else
    echo "✗ 前端镜像拉取失败"
    exit 1
fi

# 拉取 MySQL 镜像
echo ""
echo "正在拉取 MySQL 镜像: mysql:8.0"
docker pull mysql:8.0
echo "✓ MySQL 镜像拉取成功"

# 验证镜像
echo ""
echo "已拉取的镜像:"
docker images | grep -E "(fashion-crm|mysql)" | grep -E "(latest|8.0)"

echo ""
echo "步骤 5/7: 启动 MySQL 容器..."
echo "----------------------------------------"

# 启动 MySQL
echo "正在启动 MySQL 容器..."
docker-compose up -d db

echo "等待 MySQL 启动..."
sleep 20

# 检查 MySQL 是否健康
MAX_RETRIES=30
RETRY_COUNT=0

while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if docker-compose exec -T db mysqladmin ping -h localhost -u root -p"$DB_PASSWORD" &> /dev/null; then
        echo "✓ MySQL 已就绪"
        break
    fi
    RETRY_COUNT=$((RETRY_COUNT + 1))
    echo "等待 MySQL 启动... ($RETRY_COUNT/$MAX_RETRIES)"
    sleep 2
done

if [ $RETRY_COUNT -eq $MAX_RETRIES ]; then
    echo "✗ MySQL 启动超时"
    echo "请查看日志: docker-compose logs db"
    exit 1
fi

echo ""
echo "步骤 6/7: 导入数据库..."
echo "----------------------------------------"

# 检查是否有数据库备份文件
if [ -f database_backup.sql ]; then
    echo "找到数据库备份文件，正在导入..."
    if docker-compose exec -T db mysql -u root -p"$DB_PASSWORD" "$DB_NAME" < database_backup.sql; then
        echo "✓ 数据库导入成功"
    else
        echo "⚠ 数据库导入失败，但会继续使用初始化脚本"
    fi
elif [ -f mysql/init.sql ]; then
    echo "未找到数据库备份，使用初始化脚本..."
    docker-compose exec -T db mysql -u root -p"$DB_PASSWORD" "$DB_NAME" < mysql/init.sql
    echo "✓ 初始化脚本执行成功"

    # 如果有初始数据
    if [ -f mysql/initial.sql ]; then
        echo "正在导入初始数据..."
        docker-compose exec -T db mysql -u root -p"$DB_PASSWORD" "$DB_NAME" < mysql/initial.sql
        echo "✓ 初始数据导入成功"
    fi
else
    echo "⚠ 未找到数据库文件，将创建空数据库"
    echo "  如果需要导入数据，请将以下文件之一放在当前目录:"
    echo "  - database_backup.sql (完整备份)"
    echo "  - mysql/init.sql (初始化脚本)"
fi

# 验证数据库
echo ""
echo "验证数据库表..."
TABLES=$(docker-compose exec -T db mysql -u root -p"$DB_PASSWORD" "$DB_NAME" -e "SHOW TABLES;" 2>/dev/null | tail -n +2)
if [ -n "$TABLES" ]; then
    echo "✓ 数据库表:"
    echo "$TABLES" | sed 's/^/    /'
else
    echo "⚠ 数据库中没有表"
fi

echo ""
echo "步骤 7/7: 启动所有服务..."
echo "----------------------------------------"

# 启动所有服务
echo "正在启动所有服务..."
docker-compose up -d

echo ""
echo "等待服务启动..."
sleep 15

# 检查服务状态
echo ""
echo "服务状态:"
docker-compose ps

echo ""
echo "========================================="
echo "部署完成!"
echo "========================================="
echo ""
echo "服务访问地址:"
echo "  前端: http://localhost"
echo "  后端: http://localhost:5000"
echo "  数据库: localhost:3307"
echo ""
echo "常用命令:"
echo "  查看日志: docker-compose logs -f"
echo "  重启服务: docker-compose restart"
echo "  停止服务: docker-compose down"
echo ""
echo "健康检查:"
echo "  后端健康: curl http://localhost:5000/api/health"
echo "  前端访问: curl http://localhost/"
echo ""
echo "如果遇到问题，请查看日志:"
echo "  docker-compose logs backend"
echo "  docker-compose logs frontend"
echo "  docker-compose logs db"
echo "========================================="

# 可选: 自动打开浏览器
echo ""
read -p "是否在浏览器中打开前端页面? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    # 等待服务完全启动
    sleep 5

    # 根据操作系统打开浏览器
    if [[ "$OSTYPE" == "darwin"* ]]; then
        # macOS
        open http://localhost
    elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
        # Linux
        xdg-open http://localhost 2>/dev/null || echo "请手动打开 http://localhost"
    elif [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" ]]; then
        # Windows
        start http://localhost
    fi
fi
