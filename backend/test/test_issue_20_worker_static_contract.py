"""Issue #20 worker 重试路径的静态安全合同。"""

from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]


def _read(path: Path) -> str:
    return path.read_text(encoding='utf-8') if path.exists() else ''


def test_claim_includes_due_awaiting_retry_and_counts_attempts():
    source = _read(BACKEND_DIR / 'services' / 'image_import_worker.py')

    assert "ImageImportItem.status == 'awaiting_retry'" in source
    assert 'ImageImportItem.next_retry_at <= now' in source
    assert 'item.attempt_count += 1' in source
    assert 'item.last_attempt_at = now' in source
    assert 'item.next_retry_at = None' in source


def test_failure_path_classifies_and_never_sleeps_for_retry():
    source = _read(BACKEND_DIR / 'services' / 'image_import_worker.py')
    entry = _read(BACKEND_DIR / 'scripts' / 'run_image_import_worker.py')
    combined = (source + entry).lower()

    assert 'import_retry.classify_import_failure(exc)' in source
    assert 'import_retry.should_auto_retry(' in source
    assert 'def schedule_import_retry(' in source
    assert "item.status = 'awaiting_retry'" in source
    assert 'self._repository.schedule_retry(' in source
    # 等待必须由持久 next_retry_at 表达，worker 绝不进程内 sleep
    assert 'time.sleep' not in source
    # Issue #20+#21 汇合：取消范围已合法并存；仍禁止对象清理
    assert 'delete_object' not in combined


def test_retry_transition_is_claim_token_fenced_and_audited():
    source = _read(BACKEND_DIR / 'services' / 'image_import_worker.py')
    retry_scope = source.split('def schedule_import_retry(')[1]

    assert 'item.claim_token != claim.claim_token' in retry_scope
    assert '.with_for_update()' in retry_scope
    assert "event_type='image_import.awaiting_retry'" in source
    assert 'import_retry.next_retry_delay_seconds(' in retry_scope
    # 手工重试是 API 层动作，worker 不得代办
    assert 'manual_retry' not in source


def test_embedding_module_keeps_structured_transient_error_types():
    source = _read(BACKEND_DIR / 'services' / 'embedding.py')

    assert 'class EmbeddingNetworkError(EmbeddingServiceError)' in source
    assert 'class EmbeddingServerError(EmbeddingServiceError)' in source
    assert 'self.status_code = status_code' in source
    assert 'raise EmbeddingNetworkError(' in source


def test_object_storage_download_carries_structured_stage_and_status():
    source = _read(BACKEND_DIR / 'services' / 'object_storage.py')
    # 取最后一个 download_file（OssObjectStorage 实现，而非 Protocol 存根）
    download_scope = source.split('def download_file(')[-1].split('def ')[0]

    assert "error.stage = 'download'" in download_scope
    assert 'error.status_code' in download_scope
