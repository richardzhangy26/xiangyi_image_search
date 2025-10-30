#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试以图搜款功能
"""
import sys
import os

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(__file__))

from product_search import VectorProductIndex

def main():
    # 测试图片路径
    test_image_path = "/Users/richardzhang/github/xiangyipackage/image-search-engine/backend/data/摄像师拍摄素材/2025.5手机挂绳C1-C11+4K产品视频小样/c2/DSC02690.jpg"

    print("="*80)
    print("测试以图搜款功能")
    print("="*80)

    # 检查图片是否存在
    if not os.path.exists(test_image_path):
        print(f"❌ 错误：图片不存在 - {test_image_path}")
        return

    print(f"✓ 测试图片: {test_image_path}")
    print(f"✓ 图片大小: {os.path.getsize(test_image_path) / 1024:.2f} KB")

    # 初始化搜索引擎
    print("\n初始化搜索引擎...")
    search_engine = VectorProductIndex()

    # 检查索引状态
    print(f"\nFAISS 索引状态:")
    print(f"  - 索引向量数量: {search_engine.index.ntotal}")
    print(f"  - ID映射数量: {len(search_engine.faiss_id_to_db_id_map)}")

    if search_engine.index.ntotal == 0:
        print("\n⚠️  警告：FAISS 索引为空！")
        print("可能原因：")
        print("  1. 数据库中没有产品图片")
        print("  2. 产品图片没有生成向量embeddings")
        print("  3. 索引文件损坏或未正确加载")

        # 检查数据库
        print("\n检查数据库...")
        from models import db
        from models.product import Product, ProductImage
        from app import create_app

        app = create_app()
        with app.app_context():
            product_count = Product.query.count()
            image_count = ProductImage.query.count()
            images_with_embedding = ProductImage.query.filter(
                ProductImage.embedding.isnot(None)
            ).count()

            print(f"  - 产品数量: {product_count}")
            print(f"  - 图片数量: {image_count}")
            print(f"  - 有embedding的图片: {images_with_embedding}")

            if images_with_embedding == 0:
                print("\n❌ 数据库中没有图片embeddings！需要重新生成。")
                return

    # 执行搜索
    print(f"\n开始搜索相似产品...")
    try:
        results = search_engine.search_similar_images(test_image_path, top_k=5)

        print(f"\n搜索结果: 找到 {len(results)} 个相似产品")
        print("="*80)

        for i, result in enumerate(results, 1):
            print(f"\n{i}. 产品 ID: {result['product_id']}")
            print(f"   相似度: {result['similarity']:.4f}")
            if result.get('image_path'):
                print(f"   图片: {result['image_path']}")
            if result.get('original_path'):
                print(f"   原始路径: {result['original_path']}")

        print("="*80)
        print("✓ 测试完成")

    except Exception as e:
        print(f"\n❌ 搜索失败: {str(e)}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
