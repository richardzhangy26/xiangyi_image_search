#!/bin/bash

echo "======================================"
echo "图片导入进度监控"
echo "======================================"
echo ""

# 检查进程是否在运行
if ps aux | grep "python.*ingest_dataset" | grep -v grep > /dev/null; then
    echo "✅ 导入进程正在运行中..."
    echo ""
else
    echo "❌ 导入进程未运行"
    echo ""
    exit 1
fi

# 显示数据库统计
echo "📊 数据库统计:"
mysql -h localhost -u root -pzhang7481592630 -D xiangyipackage -e "
SELECT
    COUNT(*) as '已导入图片数',
    COUNT(DISTINCT product_id) as '产品数'
FROM product_images;
" 2>&1 | grep -v Warning
echo ""

# 显示最近的日志（最后10行）
echo "📝 最近日志:"
tail -10 /tmp/import_progress.log 2>/dev/null || echo "暂无日志"
echo ""

# 估算进度
total_images=2418
current_count=$(mysql -h localhost -u root -pzhang7481592630 -D xiangyipackage -e "SELECT COUNT(*) FROM product_images;" 2>&1 | grep -v Warning | tail -1)
if [ "$current_count" != "COUNT(*)" ]; then
    percentage=$((current_count * 100 / total_images))
    echo "📈 总体进度: $current_count / $total_images ($percentage%)"
fi

echo ""
echo "======================================"
