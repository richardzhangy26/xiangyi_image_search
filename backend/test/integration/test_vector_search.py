"""图片资产级 pgvector 检索的语义正确性。"""

import numpy as np
import pytest
from sqlalchemy import text

from models import ImageAsset, Product, db
from services.vector_search import VectorSearchError, VectorSearchService


def _unit_vector(seed):
    """构造归一化向量：第 seed 维为 1，其余为 0。"""
    vector = np.zeros(1024, dtype=np.float32)
    vector[seed] = 1.0
    return vector


def _tilted_vector(seed, tilt):
    """在指定轴向量中掺入第 1023 维，制造可控距离差。"""
    vector = _unit_vector(seed)
    vector[1023] = tilt
    return vector / np.linalg.norm(vector)


def _add_product(model_number):
    db.session.add(Product(
        model_number=model_number,
        photographer_file='p',
        alibaba_product_url='https://example.com/x',
        category='相机肩带',
    ))


def _asset(
    source_relative_path,
    vector,
    *,
    model_number=None,
    content_hash=None,
    status='active',
):
    digest = content_hash or (
        source_relative_path.encode('utf-8').hex()[:64].ljust(64, '0')
    )
    return ImageAsset(
        model_number=model_number,
        source_provider='qiniu-kodo',
        source_bucket='xiangxipackage',
        source_relative_path=source_relative_path,
        source_revision=1,
        oss_path=f'image-search/xiangxipackage/{source_relative_path}',
        preview_oss_path=(
            f'image-search/previews/preview-v1/{digest[:2]}/{digest}.jpg'
        ),
        content_hash=digest,
        source_size=123,
        source_mime_type='image/png',
        source_width=16,
        source_height=12,
        vector=vector.tolist(),
        embedding_model='tongyi-embedding-vision-plus-2026-03-06',
        embedding_dimension=1024,
        normalization_version='preview-v1',
        status=status,
    )


def test_returns_unassigned_and_same_model_assets_without_folding(app):
    _add_product('M-001')
    db.session.add_all([
        _asset('同型号/第一张.png', _tilted_vector(0, 0.01), model_number='M-001'),
        _asset('同型号/第二张.png', _tilted_vector(0, 0.02), model_number='M-001'),
        _asset('未归款/第三张.png', _tilted_vector(0, 0.03)),
    ])
    db.session.commit()

    results = VectorSearchService().search_by_vector(_unit_vector(0), top_k=3)

    assert [item['model_number'] for item in results] == [
        'M-001',
        'M-001',
        None,
    ]
    assert [item['relative_path'] for item in results] == [
        '同型号/第一张.png',
        '同型号/第二张.png',
        '未归款/第三张.png',
    ]


def test_same_hash_at_different_paths_occupies_two_result_positions(app):
    digest = 'a' * 64
    db.session.add_all([
        _asset('目录一/同图.png', _tilted_vector(0, 0.01), content_hash=digest),
        _asset('目录二/同图副本.png', _tilted_vector(0, 0.02), content_hash=digest),
    ])
    db.session.commit()

    results = VectorSearchService().search_by_vector(_unit_vector(0), top_k=2)

    assert [item['relative_path'] for item in results] == [
        '目录一/同图.png',
        '目录二/同图副本.png',
    ]


def test_archived_assets_are_not_searchable(app):
    db.session.add_all([
        _asset('活跃.png', _tilted_vector(0, 0.02)),
        _asset('已归档.png', _tilted_vector(0, 0.01), status='archived'),
    ])
    db.session.commit()

    results = VectorSearchService().search_by_vector(_unit_vector(0), top_k=10)

    assert [item['relative_path'] for item in results] == ['活跃.png']


def test_results_are_ordered_by_descending_similarity(app):
    db.session.add_all([
        _asset('较近.png', _tilted_vector(0, 0.01)),
        _asset('较远.png', _tilted_vector(1, 0.01)),
    ])
    db.session.commit()

    results = VectorSearchService().search_by_vector(_unit_vector(0), top_k=2)

    assert [item['relative_path'] for item in results] == ['较近.png', '较远.png']
    assert results[0]['similarity'] > results[1]['similarity']


def test_similarity_is_clamped_to_unit_interval(app):
    vector = _tilted_vector(0, 0.01)
    db.session.add(_asset('同图.png', vector))
    db.session.commit()

    result = VectorSearchService().search_by_vector(vector, top_k=1)[0]

    assert 0.0 <= result['similarity'] <= 1.0
    assert result['similarity'] == pytest.approx(1.0, abs=1e-3)


def test_top_k_larger_than_default_ef_search_still_returns_all(app):
    db.session.add_all([
        _asset(f'批量/{index:03d}.png', _tilted_vector(index, 0.01))
        for index in range(60)
    ])
    db.session.commit()
    db.session.execute(text('SET enable_seqscan = off'))
    db.session.commit()

    results = VectorSearchService().search_by_vector(_unit_vector(0), top_k=50)

    assert len(results) == 50
    assert len({item['asset_id'] for item in results}) == 50


def test_ef_search_is_transaction_local(app):
    db.session.execute(text('SET hnsw.ef_search = 77'))
    db.session.commit()
    try:
        VectorSearchService().search_by_vector(_unit_vector(0), top_k=5)

        current = db.session.execute(text('SHOW hnsw.ef_search')).scalar_one()
        assert int(current) == 77
    finally:
        db.session.execute(text('RESET hnsw.ef_search'))
        db.session.commit()


def test_empty_database_returns_empty_list(app):
    assert VectorSearchService().search_by_vector(_unit_vector(0), top_k=10) == []


def test_result_dict_shape_is_image_asset_contract(app):
    db.session.add(_asset('中文 目录/图片.png', _tilted_vector(0, 0.01)))
    db.session.commit()

    result = VectorSearchService().search_by_vector(_unit_vector(0), top_k=1)[0]

    assert set(result) == {
        'asset_id',
        'model_number',
        'display_name',
        'source_relative_path',
        'relative_path',
        'version',
        'preview_url',
        'similarity',
    }
    assert result['display_name'] == '图片.png'
    assert result['source_relative_path'] == '中文 目录/图片.png'
    assert result['preview_url'] == (
        f"/api/image-assets/{result['asset_id']}/preview"
    )


def test_invalid_top_k_raises_vector_search_error(app):
    with pytest.raises(VectorSearchError):
        VectorSearchService().search_by_vector(
            _unit_vector(0),
            top_k='not-a-number',
        )
