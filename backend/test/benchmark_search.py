#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
对比 FAISS vs MySQL 的向量搜索性能
"""
import sys
import os
import time
import numpy as np
import pymysql
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
from product_search import VectorProductIndex

load_dotenv()

# 数据库配置
DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'localhost'),
    'port': int(os.getenv('DB_PORT', 3306)),
    'user': os.getenv('DB_USER', 'root'),
    'password': os.getenv('DB_PASSWORD', 'zhang7481592630'),
    'database': os.getenv('DB_NAME', 'xiangyipackage'),
    'charset': 'utf8mb4'
}

def mysql_vector_search(query_vector, top_k=10):
    """使用纯 MySQL 进行向量搜索（模拟）"""
    conn = pymysql.connect(**DB_CONFIG)
    cursor = conn.cursor()

    # 获取所有向量
    cursor.execute("SELECT id, product_id, vector FROM product_images")
    rows = cursor.fetchall()

    results = []
    for db_id, product_id, vector_blob in rows:
        # 反序列化向量
        db_vector = np.frombuffer(vector_blob, dtype=np.float32)

        # 计算 L2 距离
        distance = np.linalg.norm(query_vector - db_vector)

        results.append({
            'id': db_id,
            'product_id': product_id,
            'distance': float(distance)
        })

    # 排序并返回 top-k
    results.sort(key=lambda x: x['distance'])

    cursor.close()
    conn.close()

    return results[:top_k]

def faiss_vector_search(product_index, query_vector, top_k=10):
    """使用 FAISS 进行向量搜索"""
    query_vector = query_vector.reshape(1, -1).astype('float32')
    distances, indices = product_index.index.search(query_vector, top_k)

    results = []
    for i, (distance, faiss_idx) in enumerate(zip(distances[0], indices[0])):
        if faiss_idx >= 0 and faiss_idx < len(product_index.faiss_id_to_db_id_map):
            db_id = product_index.faiss_id_to_db_id_map[faiss_idx]
            results.append({
                'faiss_idx': int(faiss_idx),
                'db_id': db_id,
                'distance': float(distance)
            })

    return results

def benchmark():
    print("="*80)
    print("向量搜索性能对比：FAISS vs MySQL")
    print("="*80)

    # 初始化 FAISS 索引
    print("\n1. 初始化 FAISS 索引...")
    product_index = VectorProductIndex()
    total_vectors = product_index.index.ntotal
    print(f"   ✓ 加载了 {total_vectors} 个向量")

    # 生成随机查询向量（模拟图片特征）
    print("\n2. 生成随机查询向量...")
    query_vector = np.random.randn(1024).astype('float32')
    query_vector = query_vector / np.linalg.norm(query_vector)  # 归一化
    print(f"   ✓ 查询向量维度: {query_vector.shape}")

    # 测试 FAISS
    print("\n3. 测试 FAISS 搜索...")
    faiss_times = []
    for i in range(10):  # 运行 10 次取平均
        start_time = time.time()
        faiss_results = faiss_vector_search(product_index, query_vector, top_k=10)
        end_time = time.time()
        faiss_times.append((end_time - start_time) * 1000)  # 转换为毫秒

    faiss_avg_time = np.mean(faiss_times)
    faiss_std_time = np.std(faiss_times)
    print(f"   ✓ 平均耗时: {faiss_avg_time:.2f} ms")
    print(f"   ✓ 标准差: {faiss_std_time:.2f} ms")
    print(f"   ✓ 最快: {min(faiss_times):.2f} ms")
    print(f"   ✓ 最慢: {max(faiss_times):.2f} ms")

    # 测试 MySQL
    print("\n4. 测试纯 MySQL 搜索...")
    mysql_times = []
    for i in range(10):  # 运行 10 次取平均
        start_time = time.time()
        mysql_results = mysql_vector_search(query_vector, top_k=10)
        end_time = time.time()
        mysql_times.append((end_time - start_time) * 1000)  # 转换为毫秒

    mysql_avg_time = np.mean(mysql_times)
    mysql_std_time = np.std(mysql_times)
    print(f"   ✓ 平均耗时: {mysql_avg_time:.2f} ms")
    print(f"   ✓ 标准差: {mysql_std_time:.2f} ms")
    print(f"   ✓ 最快: {min(mysql_times):.2f} ms")
    print(f"   ✓ 最慢: {max(mysql_times):.2f} ms")

    # 对比结果
    print("\n" + "="*80)
    print("性能对比总结")
    print("="*80)
    print(f"数据规模: {total_vectors} 个向量 × 1024 维")
    print(f"\nFAISS:  {faiss_avg_time:.2f} ms ± {faiss_std_time:.2f} ms")
    print(f"MySQL:  {mysql_avg_time:.2f} ms ± {mysql_std_time:.2f} ms")
    print(f"\n速度提升: {mysql_avg_time / faiss_avg_time:.1f}x 倍")
    print(f"时间节省: {mysql_avg_time - faiss_avg_time:.2f} ms")

    # 验证结果一致性
    print("\n5. 验证结果一致性...")
    faiss_top3 = faiss_results[:3]
    mysql_top3 = mysql_results[:3]

    print("\nFAISS Top-3:")
    for i, r in enumerate(faiss_top3, 1):
        print(f"  {i}. DB_ID={r['db_id']}, Distance={r['distance']:.4f}")

    print("\nMySQL Top-3:")
    for i, r in enumerate(mysql_top3, 1):
        print(f"  {i}. ID={r['id']}, Distance={r['distance']:.4f}")

    print("\n" + "="*80)

    # 扩展性分析
    print("\n6. 扩展性分析（理论预测）")
    print("="*80)
    scales = [10000, 100000, 1000000]
    for n in scales:
        scale_factor = n / total_vectors
        faiss_predicted = faiss_avg_time * scale_factor
        mysql_predicted = mysql_avg_time * scale_factor
        print(f"\n{n:,} 个向量:")
        print(f"  FAISS 预测: {faiss_predicted:.0f} ms ({faiss_predicted/1000:.1f} 秒)")
        print(f"  MySQL 预测: {mysql_predicted:.0f} ms ({mysql_predicted/1000:.1f} 秒)")
        print(f"  差距: {mysql_predicted - faiss_predicted:.0f} ms")

if __name__ == "__main__":
    benchmark()
