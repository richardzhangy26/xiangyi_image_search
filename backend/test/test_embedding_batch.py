"""EmbeddingClient 的分批、降级与重试行为。全程 mock DashScope，不产生真实调用。"""
import sys
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from services.embedding import (  # noqa: E402
    MAX_BATCH_SIZE,
    EmbeddingClient,
    EmbeddingServiceError,
)


class FakeResponse:
    def __init__(self, count, status_code=HTTPStatus.OK, message=''):
        self.status_code = status_code
        self.message = message
        self.output = {
            'embeddings': [
                {'index': i, 'embedding': [0.1] * 1024} for i in range(count)
            ]
        }


@pytest.fixture(autouse=True)
def _stub_data_uri():
    """跳过真实图片读取，测试只关心调用编排。"""
    with patch('services.embedding._to_data_uri', side_effect=lambda p, **kw: f'data:image/jpeg;base64,{p}'):
        yield


def test_embed_images_splits_into_chunks_of_max_batch_size():
    paths = [f'/img/{i}.jpg' for i in range(45)]
    calls = []

    def fake_call(**kwargs):
        calls.append(len(kwargs['input']))
        return FakeResponse(len(kwargs['input']))

    with patch('dashscope.MultiModalEmbedding.call', side_effect=fake_call):
        vectors = EmbeddingClient(api_key='k').embed_images(paths)

    assert calls == [MAX_BATCH_SIZE, MAX_BATCH_SIZE, 5]
    assert len(vectors) == 45
    assert all(isinstance(v, np.ndarray) for v in vectors)


def test_batch_failure_falls_back_to_single_calls():
    """一批里有坏图会让整批 400；降级后只有坏图被标记为 None。"""
    paths = [f'/img/{i}.jpg' for i in range(3)]

    def fake_call(**kwargs):
        inputs = kwargs['input']
        if len(inputs) > 1:
            return FakeResponse(0, status_code=400, message='invalid image')
        if inputs[0]['image'].endswith('1.jpg'):
            return FakeResponse(0, status_code=400, message='invalid image')
        return FakeResponse(1)

    with patch('dashscope.MultiModalEmbedding.call', side_effect=fake_call):
        vectors = EmbeddingClient(api_key='k').embed_images(paths)

    assert len(vectors) == 3
    assert isinstance(vectors[0], np.ndarray)
    assert vectors[1] is None
    assert isinstance(vectors[2], np.ndarray)


def test_retries_on_429_then_succeeds():
    attempts = {'n': 0}

    def fake_call(**kwargs):
        attempts['n'] += 1
        if attempts['n'] == 1:
            return FakeResponse(0, status_code=429, message='Throttling.RateQuota')
        return FakeResponse(1)

    with patch('dashscope.MultiModalEmbedding.call', side_effect=fake_call), \
         patch('services.embedding.time.sleep') as sleep:
        vector = EmbeddingClient(api_key='k').embed_image('/img/a.jpg')

    assert attempts['n'] == 2
    assert vector.shape == (1024,)
    sleep.assert_called_once_with(5.0)


def test_does_not_retry_on_non_429_error():
    attempts = {'n': 0}

    def fake_call(**kwargs):
        attempts['n'] += 1
        return FakeResponse(0, status_code=400, message='invalid image')

    with patch('dashscope.MultiModalEmbedding.call', side_effect=fake_call):
        with pytest.raises(EmbeddingServiceError):
            EmbeddingClient(api_key='k').embed_image('/img/a.jpg')

    assert attempts['n'] == 1


def test_gives_up_after_max_retries_on_persistent_429():
    attempts = {'n': 0}

    def fake_call(**kwargs):
        attempts['n'] += 1
        return FakeResponse(0, status_code=429, message='Throttling.RateQuota')

    with patch('dashscope.MultiModalEmbedding.call', side_effect=fake_call), \
         patch('services.embedding.time.sleep') as sleep:
        with pytest.raises(EmbeddingServiceError):
            EmbeddingClient(api_key='k', max_retries=3).embed_image('/img/a.jpg')

    assert attempts['n'] == 3
    # 指数退避：5s, 10s（最后一次失败不再 sleep）
    assert [c.args[0] for c in sleep.call_args_list] == [5.0, 10.0]


def test_embed_images_empty_list_makes_no_call():
    with patch('dashscope.MultiModalEmbedding.call') as call:
        assert EmbeddingClient(api_key='k').embed_images([]) == []
    call.assert_not_called()
