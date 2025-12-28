import psycopg2
import time
import numpy as np
from typing import List, Dict

class IndexBenchmark:
    """pgvector索引性能测试"""
    
    def __init__(self, db_config: Dict):
        self.conn = psycopg2.connect(**db_config)
        
    def create_test_data(self, num_rows: int = 10000, dim: int = 1536):
        """创建测试数据"""
        print(f"创建 {num_rows} 条测试数据...")
        
        with self.conn.cursor() as cur:
            # 创建测试表
            cur.execute("DROP TABLE IF EXISTS vector_test")
            cur.execute(f"""
                CREATE TABLE vector_test (
                    id SERIAL PRIMARY KEY,
                    embedding vector({dim})
                )
            """)
            
            # 批量插入随机向量
            batch_size = 1000
            for i in range(0, num_rows, batch_size):
                vectors = []
                for _ in range(min(batch_size, num_rows - i)):
                    # 生成随机向量并归一化
                    vec = np.random.randn(dim)
                    vec = vec / np.linalg.norm(vec)
                    vectors.append(f"'[{','.join(map(str, vec))}]'")
                
                values = ','.join([f"({v})" for v in vectors])
                cur.execute(f"INSERT INTO vector_test (embedding) VALUES {values}")
                
                if (i + batch_size) % 5000 == 0:
                    print(f"  已插入 {i + batch_size} 条...")
            
            self.conn.commit()
            print("✅ 测试数据创建完成")
    
    def test_no_index(self, query_vector: List[float], limit: int = 10) -> Dict:
        """测试无索引的暴力搜索"""
        print("\n【无索引 - 暴力搜索】")
        
        with self.conn.cursor() as cur:
            start = time.time()
            
            cur.execute(f"""
                SELECT id, embedding <=> %s::vector as distance
                FROM vector_test
                ORDER BY embedding <=> %s::vector
                LIMIT {limit}
            """, (query_vector, query_vector))
            
            results = cur.fetchall()
            elapsed = time.time() - start
            
            print(f"⏱️  查询时间: {elapsed*1000:.2f}ms")
            print(f"📊 返回结果: {len(results)} 条")
            
            return {
                'method': 'no_index',
                'time': elapsed,
                'results': results
            }
    
    def test_ivfflat(self, query_vector: List[float], 
                     lists: int = 100, limit: int = 10) -> Dict:
        """测试IVFFlat索引"""
        print(f"\n【IVFFlat索引 - lists={lists}】")
        
        with self.conn.cursor() as cur:
            # 删除已有索引
            cur.execute("DROP INDEX IF EXISTS vector_test_ivfflat_idx")
            
            # 创建索引
            print("正在创建索引...")
            start = time.time()
            cur.execute(f"""
                CREATE INDEX vector_test_ivfflat_idx 
                ON vector_test 
                USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = {lists})
            """)
            self.conn.commit()
            index_time = time.time() - start
            print(f"✅ 索引创建时间: {index_time:.2f}s")
            
            # 设置搜索参数（probes = 搜索几个桶）
            cur.execute("SET ivfflat.probes = 10")
            
            # 执行搜索
            start = time.time()
            cur.execute(f"""
                SELECT id, embedding <=> %s::vector as distance
                FROM vector_test
                ORDER BY embedding <=> %s::vector
                LIMIT {limit}
            """, (query_vector, query_vector))
            
            results = cur.fetchall()
            query_time = time.time() - start
            
            print(f"⏱️  查询时间: {query_time*1000:.2f}ms")
            print(f"📊 返回结果: {len(results)} 条")
            
            return {
                'method': 'ivfflat',
                'lists': lists,
                'index_time': index_time,
                'query_time': query_time,
                'results': results
            }
    
    def test_hnsw(self, query_vector: List[float], 
                  m: int = 16, ef_construction: int = 64, 
                  limit: int = 10) -> Dict:
        """测试HNSW索引"""
        print(f"\n【HNSW索引 - m={m}, ef_construction={ef_construction}】")
        
        with self.conn.cursor() as cur:
            # 删除已有索引
            cur.execute("DROP INDEX IF EXISTS vector_test_hnsw_idx")
            
            # 创建索引
            print("正在创建索引...")
            start = time.time()
            cur.execute(f"""
                CREATE INDEX vector_test_hnsw_idx 
                ON vector_test 
                USING hnsw (embedding vector_cosine_ops)
                WITH (m = {m}, ef_construction = {ef_construction})
            """)
            self.conn.commit()
            index_time = time.time() - start
            print(f"✅ 索引创建时间: {index_time:.2f}s")
            
            # 设置搜索参数
            cur.execute("SET hnsw.ef_search = 40")
            
            # 执行搜索
            start = time.time()
            cur.execute(f"""
                SELECT id, embedding <=> %s::vector as distance
                FROM vector_test
                ORDER BY embedding <=> %s::vector
                LIMIT {limit}
            """, (query_vector, query_vector))
            
            results = cur.fetchall()
            query_time = time.time() - start
            
            print(f"⏱️  查询时间: {query_time*1000:.2f}ms")
            print(f"📊 返回结果: {len(results)} 条")
            
            return {
                'method': 'hnsw',
                'm': m,
                'ef_construction': ef_construction,
                'index_time': index_time,
                'query_time': query_time,
                'results': results
            }
    
    def compare_accuracy(self, true_results: List, test_results: List) -> float:
        """计算准确率（召回率）"""
        true_ids = set([r[0] for r in true_results])
        test_ids = set([r[0] for r in test_results])
        
        overlap = len(true_ids & test_ids)
        recall = overlap / len(true_ids)
        
        return recall
    
    def get_index_size(self) -> Dict:
        """获取索引大小"""
        with self.conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    indexname,
                    pg_size_pretty(pg_relation_size(indexname::regclass)) as size
                FROM pg_indexes
                WHERE tablename = 'vector_test'
            """)
            
            results = {}
            for row in cur.fetchall():
                results[row[0]] = row[1]
            
            return results
    
    def run_benchmark(self, num_rows: int = 10000):
        """运行完整的性能测试"""
        print("="*60)
        print("pgvector 索引性能对比测试")
        print("="*60)
        
        # 创建测试数据
        self.create_test_data(num_rows)
        
        # 生成查询向量
        query_vector = np.random.randn(1536)
        query_vector = query_vector / np.linalg.norm(query_vector)
        query_vector = query_vector.tolist()
        
        # 测试各种方法
        results = {}
        
        # 1. 无索引（基准）
        results['no_index'] = self.test_no_index(query_vector)
        
        # 2. IVFFlat索引
        lists = max(int(np.sqrt(num_rows)), 10)
        results['ivfflat'] = self.test_ivfflat(query_vector, lists=lists)
        
        # 3. HNSW索引
        results['hnsw'] = self.test_hnsw(query_vector, m=16, ef_construction=64)
        
        # 计算准确率
        print("\n" + "="*60)
        print("准确率对比（与暴力搜索对比）")
        print("="*60)
        
        true_results = results['no_index']['results']
        
        for method in ['ivfflat', 'hnsw']:
            accuracy = self.compare_accuracy(
                true_results, 
                results[method]['results']
            )
            print(f"{method.upper()}: {accuracy*100:.1f}%")
        
        # 索引大小
        print("\n" + "="*60)
        print("索引大小")
        print("="*60)
        index_sizes = self.get_index_size()
        for name, size in index_sizes.items():
            print(f"{name}: {size}")
        
        # 总结
        print("\n" + "="*60)
        print("性能总结")
        print("="*60)
        print(f"数据量: {num_rows:,} 条")
        print(f"\n暴力搜索: {results['no_index']['time']*1000:.2f}ms")
        print(f"\nIVFFlat:")
        print(f"  - 构建时间: {results['ivfflat']['index_time']:.2f}s")
        print(f"  - 查询时间: {results['ivfflat']['query_time']*1000:.2f}ms")
        print(f"  - 加速比: {results['no_index']['time']/results['ivfflat']['query_time']:.1f}x")
        print(f"\nHNSW:")
        print(f"  - 构建时间: {results['hnsw']['index_time']:.2f}s")
        print(f"  - 查询时间: {results['hnsw']['query_time']*1000:.2f}ms")
        print(f"  - 加速比: {results['no_index']['time']/results['hnsw']['query_time']:.1f}x")
        
        return results
    
    def close(self):
        self.conn.close()


# 使用示例
if __name__ == "__main__":
    db_config = {
        'dbname': 'image_search',
        'user': 'zhangyichi',
        'password': '',
        'host': 'localhost'
    }
    
    benchmark = IndexBenchmark(db_config)
    
    # 运行测试（可以调整数据量）
    # 小规模测试：1万条
    # 中等测试：10万条
    # 大规模测试：100万条
    benchmark.run_benchmark(num_rows=10000)
    
    benchmark.close()