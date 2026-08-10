"""Issue #19 worker 所需的模型携带型 embedding 合同。"""

from services.embedding import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    EmbeddingClient,
    EmbeddingResult,
)


def test_model_bearing_result_preserves_exact_model_and_vector(monkeypatch):
    client = EmbeddingClient(api_key='fake', max_retries=1, initial_delay=0)
    vector = [0.125] * EMBEDDING_DIMENSION
    monkeypatch.setattr(
        client,
        'embed_normalized_image',
        lambda image_path, request_id=None: vector,
    )

    result = client.embed_normalized_image_result(
        '/temporary/preview.jpg',
        request_id='request-19',
    )

    assert isinstance(result, EmbeddingResult)
    assert result.model == EMBEDDING_MODEL
    assert result.vector is vector

