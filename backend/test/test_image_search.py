#!/usr/bin/env python3
"""
测试以图搜图功能
"""
import sys
from app import create_app
from product_search import VectorProductIndex

def test_search(image_path: str, top_k: int = 5):
    """测试图片搜索功能"""
    print(f"正在测试图片搜索功能...")
    print(f"查询图片: {image_path}")
    print(f"返回Top-{top_k}结果\n")

    # 创建应用上下文
    app = create_app()
    with app.app_context():
        # 创建新的向量索引实例
        print("创建新的向量索引实例...")
        product_index = VectorProductIndex()

        print(f"向量索引中的图片数量: {len(product_index.faiss_id_to_db_id_map)}")
        print(f"FAISS 索引中的向量数量: {product_index.index.ntotal}\n")

        # 执行搜索
        try:
            results = product_index.search(image_path, top_k=top_k)

            if not results:
                print("❌ 没有找到匹配结果！")
                return

            print(f"✅ 找到 {len(results)} 个匹配结果：\n")

            for i, result in enumerate(results, 1):
                print(f"结果 #{i}:")
                print(f"  产品ID: {result['product_id']}")
                print(f"  图片路径: {result['image_path']}")
                print(f"  原始路径: {result.get('original_path', 'N/A')}")
                print(f"  相似度得分: {result['similarity']:.4f}")
                print()

        except Exception as e:
            print(f"❌ 搜索失败: {e}")
            import traceback
            traceback.print_exc()

if __name__ == '__main__':
    # 测试图片路径
    test_image = "/Users/richardzhang/github/xiangyipackage/image-search-engine/backend/data/摄像师拍摄素材/2025.4.18海报照片/加拍照片/DSC00033.jpg"

    if len(sys.argv) > 1:
        test_image = sys.argv[1]

    test_search(test_image, top_k=5)
