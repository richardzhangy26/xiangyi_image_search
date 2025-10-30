#!/bin/bash

echo "=========================================="
echo "开始导入所有图片到向量数据库"
echo "预计时间: 20-30 分钟"
echo "=========================================="
echo ""

cd /Users/richardzhang/github/xiangyipackage/image-search-engine/backend

# 运行导入脚本，每 50 张显示一次进度
python -m scripts.ingest_dataset --batch-size 50 2>&1 | tee /tmp/import_progress.log

echo ""
echo "=========================================="
echo "导入完成！"
echo "=========================================="

# 显示统计信息
mysql -h localhost -u root -pzhang7481592630 -D xiangyipackage -e "
SELECT
  COUNT(*) as total_products,
  COUNT(DISTINCT id) as unique_products
FROM products;

SELECT
  COUNT(*) as total_images,
  COUNT(DISTINCT product_id) as products_with_images
FROM product_images;
" 2>&1 | grep -v Warning
