"""已经标准化的搜索预览图不应被二次有损转码。"""

import base64
import io

from PIL import Image

from services.embedding import _normalized_to_data_uri


def test_compliant_jpeg_bytes_are_sent_to_embedding_unchanged(tmp_path):
    source = tmp_path / 'preview.jpg'
    output = io.BytesIO()
    Image.new('RGB', (32, 20), 'red').save(
        output,
        format='JPEG',
        quality=85,
    )
    original = output.getvalue()
    source.write_bytes(original)

    data_uri = _normalized_to_data_uri(source)
    encoded = data_uri.split(',', 1)[1]

    assert base64.b64decode(encoded) == original
