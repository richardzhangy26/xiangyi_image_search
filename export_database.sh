#!/bin/bash
# 数据库导出脚本
# 功能：从运行中的 Docker MySQL 容器导出完整数据库

set -e

echo "========================================="
echo "数据库导出工具"
echo "========================================="

# 从 .env 文件读取配置
if [ -f .env ]; then
    export $(cat .env | grep -v '^#' | xargs)
else
    echo "错误: 找不到 .env 文件"
    exit 1
fi

# 创建导出目录
EXPORT_DIR="./docker_export"
mkdir -p "$EXPORT_DIR"

# 容器名称
CONTAINER_NAME="fashion-crm-db"

echo ""
echo "步骤 1/4: 检查 MySQL 容器状态..."
echo "----------------------------------------"

# 检查容器是否运行
if ! docker ps --format "{{.Names}}" | grep -q "^${CONTAINER_NAME}$"; then
    echo "✗ MySQL 容器未运行"
    echo "  请先运行: docker-compose up -d db"
    exit 1
fi

echo "✓ MySQL 容器正在运行"

echo ""
echo "步骤 2/4: 导出数据库结构和数据..."
echo "----------------------------------------"

# 导出数据库
DB_FILE="$EXPORT_DIR/database_backup.sql"
echo "正在导出数据库: $DB_NAME"

docker exec "$CONTAINER_NAME" mysqldump \
    -u root \
    -p"$DB_PASSWORD" \
    --single-transaction \
    --routines \
    --triggers \
    --events \
    --hex-blob \
    "$DB_NAME" > "$DB_FILE"

echo "✓ 数据库已导出到: $DB_FILE"

echo ""
echo "步骤 3/4: 复制初始化脚本..."
echo "----------------------------------------"

# 复制 mysql 目录下的初始化脚本
cp -r ./mysql "$EXPORT_DIR/"
echo "✓ 初始化脚本已复制"

echo ""
echo "步骤 4/4: 生成数据库信息文件..."
echo "----------------------------------------"

# 生成数据库信息文件
cat > "$EXPORT_DIR/database_info.txt" << EOF
数据库备份信息
================
导出时间: $(date '+%Y-%m-%d %H:%M:%S')
数据库名: $DB_NAME
字符集: utf8mb4

备份文件:
1. database_backup.sql - 完整数据库备份 (结构+数据)
2. mysql/ - 初始化脚本目录
   - init.sql - 表结构定义
   - initial.sql - 初始数据 (如果存在)

文件大小:
EOF

# 添加文件大小信息
ls -lh "$DB_FILE" | awk '{print $9 ": " $5}' >> "$EXPORT_DIR/database_info.txt"
du -sh "$EXPORT_DIR/mysql" | awk '{print "mysql/: " $1}' >> "$EXPORT_DIR/database_info.txt"

# 统计数据库表信息
echo "" >> "$EXPORT_DIR/database_info.txt"
echo "数据库表统计:" >> "$EXPORT_DIR/database_info.txt"
echo "----------------------------------------" >> "$EXPORT_DIR/database_info.txt"

docker exec "$CONTAINER_NAME" mysql \
    -u root \
    -p"$DB_PASSWORD" \
    -D "$DB_NAME" \
    -e "SELECT
        TABLE_NAME as '表名',
        TABLE_ROWS as '行数',
        ROUND(DATA_LENGTH/1024/1024, 2) as '数据大小(MB)',
        ROUND(INDEX_LENGTH/1024/1024, 2) as '索引大小(MB)'
    FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = '$DB_NAME'
    ORDER BY DATA_LENGTH DESC;" \
    >> "$EXPORT_DIR/database_info.txt" 2>/dev/null || echo "无法获取表统计信息" >> "$EXPORT_DIR/database_info.txt"

echo ""
echo "========================================="
echo "数据库导出完成!"
echo "========================================="
echo ""
echo "导出目录: $EXPORT_DIR"
echo ""
echo "导出的文件:"
ls -lh "$EXPORT_DIR" | grep -v "^d" | tail -n +2
echo ""
echo "下一步: 运行 ./create_deployment_package.sh 创建完整部署包"
echo "========================================="
