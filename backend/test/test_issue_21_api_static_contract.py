"""Issue #21 取消 HTTP 边界与 worker 清扫的静态安全合同。"""

from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]


def _read(path: Path) -> str:
    return path.read_text(encoding='utf-8') if path.exists() else ''


def test_cancel_routes_are_registered_with_batch_limit():
    source = _read(BACKEND_DIR / 'blueprints' / 'image_imports.py')

    assert "@image_imports_bp.post('/<uuid:item_id>/cancel')" in source
    assert "@image_imports_bp.post('/cancel')" in source
    assert 'MAX_CANCEL_BATCH = 100' in source
    assert 'IMAGE_IMPORT_CANCEL_COMPLETED' in source
    assert 'IMAGE_IMPORT_CANCEL_TOO_MANY' in source
    assert '.to_public_dict()' in source


def test_cancel_http_path_never_exposes_private_state_or_calls_embedding():
    source = _read(BACKEND_DIR / 'blueprints' / 'image_imports.py')
    lowered = source.lower()

    assert 'embed_normalized' not in source
    assert 'threading' not in lowered
    assert 'threadpool' not in lowered
    assert 'preview_oss_path' not in source
    assert 'signed_url' not in lowered
    assert "'vector'" not in source
    # 取消不删除暂存对象
    assert 'delete_object' not in lowered
    assert 'batch_delete' not in lowered
    # Issue #20+#21 汇合：手工重试范围已合法并存
    assert "@image_imports_bp.post('/<uuid:item_id>/retry')" in source


def test_worker_cancel_checkpoints_and_sweep_are_present():
    source = _read(BACKEND_DIR / 'services' / 'image_import_worker.py')

    assert 'def cancel_import_item_if_requested(' in source
    assert 'def sweep_cancelled_imports(' in source
    assert 'def _transition_item_to_cancelled(' in source
    # 领取必须排除已提交取消意图的行
    assert 'ImageImportItem.cancel_requested_at.is_(None)' in source
    # 三个检查点：调用前、结果返回后/提交前、失败路径意图优先
    assert 'self._repository.cancel_if_requested(claim)' in source
    assert "event_type='image_import.late_result_discarded'" in source
    assert 'image_import.cancelled_before_embedding' in source


def test_cancel_transition_never_deletes_objects_or_leaks_secrets():
    source = _read(BACKEND_DIR / 'services' / 'image_import_worker.py')
    lowered = source.lower()

    assert 'delete_object' not in lowered
    assert 'session.delete' not in lowered
    assert 'signed_url' not in lowered
    assert 'placeholder' not in lowered
    # Issue #20+#21 汇合：退避/重试调度范围已合法并存；仍禁止进程内退避 sleep 字面
    assert 'backoff' not in lowered
