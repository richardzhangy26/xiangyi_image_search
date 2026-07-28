"""向量检索的语义正确性。此前这条路径零测试覆盖——SQLite 测试用 FakeSearchService
把整条链路 mock 掉了，而 SQLite 只是把 VECTOR(1024) 当未知类型名接受。
"""
import numpy as np
import pytest
from sqlalchemy import text

from models import Product, ProductImage, db
from services.vector_search import VectorSearchService


def _unit_vector(seed):
    """构造归一化向量：第 seed 维为 1，其余为 0。彼此正交，距离可预期。"""
    v = np.zeros(1024, dtype=np.float32)
    v[seed] = 1.0
    return v


def _tilted_vector(seed, tilt):
    """在 _unit_vector(seed) 基础上掺入一点第 1023 维，制造可控的距离差。"""
    v = _unit_vector(seed)
    v[1023] = tilt
    return v / np.linalg.norm(v)


def _seed(model_numbers_to_vectors):
    for model_number, vectors in model_numbers_to_vectors.items():
        db.session.add(Product(
            model_number=model_number,
            photographer_file='p',
            alibaba_product_url='https://example.com/x',
            category='相机肩带',
        ))
        for i, vec in enumerate(vectors):
            db.session.add(ProductImage(
                model_number=model_number,
                image_path=f'/uploads/product_images/{model_number}/{i}.jpg',
                vector=vec.tolist(),
                content_hash=f'{model_number}-{i}'.ljust(64, '0'),
                image_order=i,
                is_primary=(i == 0),
            ))
    db.session.commit()


def test_returns_distinct_products_not_distinct_images(app):
    """核心修复：一个产品有 5 张图时，top_k=3 必须返回 3 个不同产品。

    旧实现取 top_k 张图再在 Python 里折叠，这里只会返回 1 个产品。
    """
    _seed({
        'M-001': [_tilted_vector(0, t) for t in (0.01, 0.02, 0.03, 0.04, 0.05)],
        'M-002': [_tilted_vector(1, 0.01)],
        'M-003': [_tilted_vector(2, 0.01)],
    })

    results = VectorSearchService().search_by_vector(_unit_vector(0), top_k=3)

    assert len(results) == 3
    assert len({r['model_number'] for r in results}) == 3
    assert results[0]['model_number'] == 'M-001'   # 与查询向量同轴，距离最小


def test_returns_best_matching_image_per_product(app):
    """折叠时保留该产品下距离最小的那张图。"""
    _seed({'M-001': [_tilted_vector(0, 0.5), _tilted_vector(0, 0.01)]})

    results = VectorSearchService().search_by_vector(_unit_vector(0), top_k=1)

    assert len(results) == 1
    # tilt=0.01 的是第 1 张（索引 1），与查询向量更接近
    assert results[0]['image_path'] == '/uploads/product_images/M-001/1.jpg'


def test_results_ordered_by_descending_similarity(app):
    _seed({
        'M-001': [_tilted_vector(0, 0.01)],
        'M-002': [_tilted_vector(1, 0.01)],
    })

    results = VectorSearchService().search_by_vector(_unit_vector(0), top_k=2)

    assert results[0]['model_number'] == 'M-001'
    assert results[0]['similarity'] > results[1]['similarity']


def test_similarity_clamped_to_unit_interval(app):
    """实测向量 L2 范数 1.000282，同图余弦相似度可达 1.00056，必须夹上界。"""
    vec = _tilted_vector(0, 0.01)
    _seed({'M-001': [vec]})

    results = VectorSearchService().search_by_vector(vec, top_k=1)

    assert 0.0 <= results[0]['similarity'] <= 1.0
    assert results[0]['similarity'] == pytest.approx(1.0, abs=1e-3)


def test_top_k_larger_than_default_ef_search_still_returns_all(app):
    """旧实现的阻断级缺陷：hnsw.ef_search 默认 40，top_k=50 拿不满。

    强制 enable_seqscan=off：60 行的测试表在 Postgres 规划器眼里体积太小——
    顺序扫描+排序比遍历 HNSW 索引更便宜，规划器天然会选 Seq Scan，导致
    hnsw.ef_search 根本不参与运算，测不出「SET LOCAL 未生效」这类回归
    （T3 fix round 1 的 mutation test 证实：不加这行时，即使 SET LOCAL 被
    整行删掉，本测试仍然全绿）。生产环境表更大时规划器会自然选择索引路径，
    这里用规划器提示复现同样的执行路径，让测试真正覆盖 ef_search 的行为。
    """
    _seed({f'M-{i:03d}': [_tilted_vector(i, 0.01)] for i in range(60)})
    db.session.execute(text('SET enable_seqscan = off'))
    db.session.commit()

    results = VectorSearchService().search_by_vector(_unit_vector(0), top_k=50)

    assert len(results) == 50
    assert len({r['model_number'] for r in results}) == 50


def test_empty_database_returns_empty_list(app):
    assert VectorSearchService().search_by_vector(_unit_vector(0), top_k=10) == []


def test_result_dict_shape(app):
    _seed({'M-001': [_tilted_vector(0, 0.01)]})

    result = VectorSearchService().search_by_vector(_unit_vector(0), top_k=1)[0]

    assert set(result) == {'model_number', 'image_path', 'original_path', 'oss_path', 'similarity'}


def test_invalid_top_k_raises_vector_search_error(app):
    """T3 fix round 1 Minor：top_k 转 int 失败时必须包装成 VectorSearchError，
    而不是让原始 ValueError 击穿"只捕获 VectorSearchError"的调用约定。
    """
    from services.vector_search import VectorSearchError

    with pytest.raises(VectorSearchError):
        VectorSearchService().search_by_vector(_unit_vector(0), top_k='not-a-number')
