"""Cancel and first intent compete on the same pending_deletion batch row."""

import uuid
import threading
from datetime import datetime, timedelta

from models import ImageAsset, PurgeBatch, PurgeBatchItem, db
from services.formal_purge import FormalPurgeRepository
from services.purge_batch_control import PurgeBatchControlService, PurgeBatchStateError


def _seed():
    nonce = uuid.uuid4().hex
    asset = ImageAsset(source_provider='t', source_bucket='s', source_relative_path=f'{nonce}.png', source_revision=1, display_name='x', oss_path=f'o/{nonce}', preview_oss_path=f'p/{nonce}', content_hash=nonce, source_size=1, source_mime_type='image/png', source_width=1, source_height=1, vector=[0.0]*1024, embedding_model='tongyi-embedding-vision-plus-2026-03-06', embedding_dimension=1024, normalization_version='preview-v1', status='archived')
    db.session.add(asset); db.session.flush()
    batch = PurgeBatch(actor_id='a', idempotency_key=f'key.{nonce}', request_fingerprint_sha256='a'*64, confirmation_text='永久删除 1 张', status='pending_deletion', retain_until=datetime.now()+timedelta(days=1))
    item = PurgeBatchItem(batch=batch, target_asset_id=asset.id, ordinal=0, status='pending', original_formal_key=asset.oss_path, original_backup_object_id='o', original_backup_sha256='a'*64, preview_formal_key=asset.preview_oss_path, preview_delete_authorized=False, authorization_retain_until=datetime.now()+timedelta(days=1), formal_bucket='b')
    db.session.add_all([batch,item]); db.session.commit(); return batch, asset


def test_cancel_wins_before_first_intent(app):
    batch, asset = _seed()
    assert PurgeBatchControlService(db.session).cancel(batch.id, actor_id='a', request_id='r').status == 'cancelled'
    repo = FormalPurgeRepository(db.session, manifest_validator=lambda _b,_i: True)
    assert repo.claim_next_item() is None


def test_first_intent_wins_then_cancel_is_rejected(app):
    batch, asset = _seed()
    repo = FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True)
    claim = repo.claim_next_item()
    assert repo.checkpoint(claim, 'original_delete_started') is True
    with __import__('pytest').raises(PurgeBatchStateError):
        PurgeBatchControlService(db.session).cancel(batch.id, actor_id='a', request_id='r')
    assert db.session.get(PurgeBatch, batch.id).status == 'deleting'


def test_two_sessions_cancel_first_rejects_intent(pg_session_factory):
    from models import ImageAsset, PurgeBatch, PurgeBatchItem
    sessions = (pg_session_factory(), pg_session_factory(), pg_session_factory())
    setup, cancel_session, intent_session = sessions
    try:
        nonce = uuid.uuid4().hex
        asset = ImageAsset(source_provider='t', source_bucket='s', source_relative_path=f'{nonce}.png', source_revision=1, display_name='x', oss_path=f'o/{nonce}', preview_oss_path=f'p/{nonce}', content_hash=nonce, source_size=1, source_mime_type='image/png', source_width=1, source_height=1, vector=[0.0]*1024, embedding_model='tongyi-embedding-vision-plus-2026-03-06', embedding_dimension=1024, normalization_version='preview-v1', status='archived')
        setup.add(asset); setup.flush()
        batch = PurgeBatch(actor_id='a', idempotency_key=f'key.{nonce}', request_fingerprint_sha256='a'*64, confirmation_text='永久删除 1 张', status='pending_deletion', retain_until=datetime.now()+timedelta(days=1))
        item = PurgeBatchItem(batch=batch, target_asset_id=asset.id, ordinal=0, status='pending', original_formal_key=asset.oss_path, original_backup_object_id='o', original_backup_sha256='a'*64, preview_formal_key=asset.preview_oss_path, preview_delete_authorized=False, authorization_retain_until=datetime.now()+timedelta(days=1), formal_bucket='b')
        setup.add_all([batch,item]); setup.commit()
        claim = FormalPurgeRepository(intent_session, manifest_validator=lambda _b,_i: True).claim_next_item()
        go = threading.Event(); cancelled = threading.Event(); outcome = {}
        def do_cancel():
            go.wait(); outcome['cancel'] = PurgeBatchControlService(cancel_session).cancel(batch.id, actor_id='a', request_id='race').status; cancelled.set()
        def do_intent():
            go.wait(); cancelled.wait(); outcome['intent'] = FormalPurgeRepository(intent_session, manifest_validator=lambda _b,_i: True).checkpoint(claim, 'original_delete_started')
        a=threading.Thread(target=do_cancel); b=threading.Thread(target=do_intent); a.start(); b.start(); go.set(); a.join(); b.join()
        assert outcome == {'cancel': 'cancelled', 'intent': False}
    finally:
        [session.close() for session in sessions]


def test_two_sessions_intent_first_rejects_cancel(pg_session_factory):
    from models import ImageAsset, PurgeBatch, PurgeBatchItem
    setup, intent_session, cancel_session = (pg_session_factory(), pg_session_factory(), pg_session_factory())
    try:
        nonce = uuid.uuid4().hex
        asset = ImageAsset(source_provider='t', source_bucket='s', source_relative_path=f'{nonce}.png', source_revision=1, display_name='x', oss_path=f'o/{nonce}', preview_oss_path=f'p/{nonce}', content_hash=nonce, source_size=1, source_mime_type='image/png', source_width=1, source_height=1, vector=[0.0]*1024, embedding_model='tongyi-embedding-vision-plus-2026-03-06', embedding_dimension=1024, normalization_version='preview-v1', status='archived')
        setup.add(asset); setup.flush()
        batch = PurgeBatch(actor_id='a', idempotency_key=f'key.{nonce}', request_fingerprint_sha256='a'*64, confirmation_text='永久删除 1 张', status='pending_deletion', retain_until=datetime.now()+timedelta(days=1))
        item = PurgeBatchItem(batch=batch, target_asset_id=asset.id, ordinal=0, status='pending', original_formal_key=asset.oss_path, original_backup_object_id='o', original_backup_sha256='a'*64, preview_formal_key=asset.preview_oss_path, preview_delete_authorized=False, authorization_retain_until=datetime.now()+timedelta(days=1), formal_bucket='b')
        setup.add_all([batch,item]); setup.commit()
        repo = FormalPurgeRepository(intent_session, manifest_validator=lambda _b,_i: True)
        claim = repo.claim_next_item()
        go = threading.Event(); intended = threading.Event(); outcome = {}
        def do_intent():
            go.wait(); outcome['intent'] = repo.checkpoint(claim, 'original_delete_started'); intended.set()
        def do_cancel():
            go.wait(); intended.wait()
            try:
                PurgeBatchControlService(cancel_session).cancel(batch.id, actor_id='a', request_id='race')
            except PurgeBatchStateError as exc:
                outcome['cancel'] = exc.error_code
        a=threading.Thread(target=do_intent); b=threading.Thread(target=do_cancel); a.start(); b.start(); go.set(); a.join(); b.join()
        assert outcome == {'intent': True, 'cancel': 'PURGE_BATCH_NOT_CANCELLABLE'}
    finally:
        [session.close() for session in (setup, intent_session, cancel_session)]
