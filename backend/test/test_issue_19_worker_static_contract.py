"""Issue #19 多实例 worker 与事务边界的静态合同。"""

from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = BACKEND_DIR.parent


def _read(path: Path) -> str:
    return path.read_text(encoding='utf-8') if path.exists() else ''


def test_claim_is_ordered_skip_locked_committed_and_lease_fenced():
    source = _read(BACKEND_DIR / 'services' / 'image_import_worker.py')

    assert 'def claim_next_import_item(' in source
    assert '.with_for_update(skip_locked=True)' in source
    assert 'ImageImportItem.created_at' in source
    assert 'ImageImportItem.id' in source
    assert "ImageImportItem.status == 'queued'" in source
    assert "ImageImportItem.status == 'embedding'" in source
    assert 'ImageImportItem.lease_expires_at < now' in source
    assert 'ImageImportItem.asset_id.is_(None)' in source
    assert "item.status = 'embedding'" in source
    assert 'item.claim_generation += 1' in source
    assert 'item.claim_token = uuid.uuid4()' in source
    assert 'session.commit()' in source
    assert 'session.rollback()' in source


def test_embedding_is_outside_claim_and_promotion_is_atomic_and_fenced():
    source = _read(BACKEND_DIR / 'services' / 'image_import_worker.py')

    assert 'class ImageImportWorker:' in source
    assert 'self._repository.claim_next(' in source
    assert 'self._storage.download_file(' in source
    assert 'self._embedding.embed_normalized_image_result(' in source
    assert 'validate_embedding_result(' in source
    assert 'self._repository.complete(' in source
    assert 'def complete_import_item(' in source
    assert '.with_for_update()' in source
    assert 'item.claim_token != claim.claim_token' in source
    assert "item.status = 'completed'" in source
    assert 'session.add(candidate)' in source
    assert 'session.flush()' in source
    assert 'session.commit()' in source


def test_failure_has_persistent_retry_and_cancel_but_no_cleanup_or_placeholder_vector_scope():
    source = _read(BACKEND_DIR / 'services' / 'image_import_worker.py')
    entry = _read(BACKEND_DIR / 'scripts' / 'run_image_import_worker.py')
    combined = (source + entry).lower()

    assert "item.status = 'failed'" in source
    assert 'session.rollback()' in source
    # Issue #20 取代原「无重试」禁令：重试必须以持久调度字段表达
    assert 'import_retry' in source
    assert 'next_retry_at' in source
    assert 'attempt_count' in source
    assert "'awaiting_retry'" in source
    # Issue #21 取代原「无取消」禁令：取消以持久意图 + 终态表达
    assert 'cancel_requested_at' in source
    assert "'cancelled'" in source
    # 等待必须表达为持久 next_retry_at，worker 绝不进程内 sleep 退避
    assert 'time.sleep' not in source
    # 仍禁止对象清理与占位向量范围
    assert 'delete_object' not in combined
    assert 'session.delete' not in combined
    assert 'placeholder' not in combined


def test_worker_is_an_independent_restartable_compose_service():
    compose = _read(ROOT_DIR / 'docker-compose.yml')
    entry = _read(BACKEND_DIR / 'scripts' / 'run_image_import_worker.py')
    source = _read(BACKEND_DIR / 'services' / 'image_import_worker.py')

    assert '  worker:' in compose
    assert 'command: ["python", "-m", "scripts.run_image_import_worker"]' in compose
    assert 'restart: unless-stopped' in compose
    assert 'condition: service_healthy' in compose
    assert 'gunicorn' not in entry.lower()
    assert 'signal.SIGTERM' in entry
    assert 'signal.SIGINT' in entry
    for field in (
        'task_id',
        'worker_id',
        'claim_generation',
        'queue_depth',
        'queue_latency_ms',
        'embedding_duration_ms',
        'total_duration_ms',
        'error_type',
    ):
        assert field in source + entry
