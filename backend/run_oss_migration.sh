#!/bin/bash
# 运行 OSS 路径迁移脚本

# 设置环境变量（根据实际情况修改）
export FLASK_ENV=development
export DB_HOST=localhost
export DB_USER=root
export DB_PASSWORD=zhang7481592630
export DB_NAME=xiangyipackage

# 切换到 backend 目录
cd "$(dirname "$0")"

echo "开始迁移 product_images OSS 路径..."
echo "=================================================="
echo ""

# 首先运行 dry-run 模式查看前 5 条记录
echo "第一步：预览前 5 条记录的转换结果"
echo "--------------------------------------------------"
python scripts/migrate_oss_path.py --dry-run --limit 5
echo ""

read -p "预览结果是否正确？继续处理所有记录吗? (y/n) " -n 1 -r
echo ""

if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo ""
    echo "第二步：处理所有记录（不包括已有 oss_path 的记录）"
    echo "--------------------------------------------------"
    python scripts/migrate_oss_path.py

    echo ""
    echo "迁移完成！"
else
    echo ""
    echo "已取消迁移。"
fi
