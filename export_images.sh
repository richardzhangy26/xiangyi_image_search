#!/bin/bash
# Docker 镜像导出脚本
# 功能：将所有自定义镜像导出为 tar 文件，方便迁移到其他电脑

set -e

echo "========================================="
echo "Docker 镜像导出工具"
echo "========================================="

# 创建导出目录
EXPORT_DIR="./docker_export"
mkdir -p "$EXPORT_DIR"

echo ""
echo "步骤 1/3: 检查现有镜像..."
echo "----------------------------------------"

# 检查镜像是否存在
IMAGES=(
    "fashion-crm-backend:latest"
    "fashion-crm-frontend:latest"
)

for image in "${IMAGES[@]}"; do
    if docker images --format "{{.Repository}}:{{.Tag}}" | grep -q "^${image}$"; then
        echo "✓ 找到镜像: $image"
    else
        echo "✗ 镜像不存在: $image"
        echo "  请先运行: docker-compose build"
        exit 1
    fi
done

echo ""
echo "步骤 2/3: 导出镜像到 tar 文件..."
echo "----------------------------------------"

# 导出后端镜像
echo "正在导出 fashion-crm-backend:latest..."
docker save -o "$EXPORT_DIR/backend.tar" fashion-crm-backend:latest
echo "✓ 后端镜像已导出到: $EXPORT_DIR/backend.tar"

# 导出前端镜像
echo "正在导出 fashion-crm-frontend:latest..."
docker save -o "$EXPORT_DIR/frontend.tar" fashion-crm-frontend:latest
echo "✓ 前端镜像已导出到: $EXPORT_DIR/frontend.tar"

# 我们不导出 MySQL 镜像，因为官方镜像可以直接拉取
echo ""
echo "注意: MySQL 镜像使用官方镜像 mysql:8.0，无需导出"

echo ""
echo "步骤 3/3: 生成镜像清单..."
echo "----------------------------------------"

# 创建镜像清单文件
cat > "$EXPORT_DIR/image_list.txt" << EOF
Docker 镜像清单
================
导出时间: $(date '+%Y-%m-%d %H:%M:%S')

自定义镜像:
1. fashion-crm-backend:latest  -> backend.tar
2. fashion-crm-frontend:latest -> frontend.tar

官方镜像 (需在新电脑上拉取):
1. mysql:8.0

导出文件大小:
EOF

# 添加文件大小信息
ls -lh "$EXPORT_DIR"/*.tar | awk '{print $9 ": " $5}' >> "$EXPORT_DIR/image_list.txt"

echo ""
echo "========================================="
echo "镜像导出完成!"
echo "========================================="
echo ""
echo "导出目录: $EXPORT_DIR"
echo ""
echo "导出的文件:"
ls -lh "$EXPORT_DIR"
echo ""
echo "下一步: 运行 ./export_database.sh 导出数据库数据"
echo "========================================="
