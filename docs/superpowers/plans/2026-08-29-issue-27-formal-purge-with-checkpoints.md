# Issue #27 Formal Purge with Checkpoints Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use `subagent-driven-development` (recommended) or `executing-plans` task-by-task. Steps use checkbox syntax. This plan is high-risk: implementation begins only after its architect review is **APPROVE**; the complete implementation diff then requires a `risk_reviewer` review.

**Goal:** Build the safe, idempotent formal-purge state machine for an already verified `pending_deletion` batch, preserve one-year item-level evidence and partial-failure recovery semantics, without exposing an enabled production deletion path.

**Architecture:** Extend the #26 batch/item state model with a per-item deletion checkpoint machine, append-only item events, append-only deletion-fence epochs, and separate short-lived binding-fence epochs. Every formal object writer takes the same canonical PostgreSQL advisory locks and performs formal OSS Head/Put-if-absent while its durable binding fence lease remains live; existing #19/#25 semantics intentionally keep those objects when embedding fails, then bind only after embedding succeeds and a final ownership check passes. A purge first excludes live binding fences, then commits its held deletion fence and rechecks references, so a concurrent new binding either precedes that recheck and protects the object or is rejected before storage I/O. The T13 production worker composition unconditionally uses an unavailable deletion capability and never reads delete credentials or constructs a deleter; a delete-capable adapter exists only as an isolated unit-test seam for T14 to compose later.

**Tech Stack:** Flask, SQLAlchemy, PostgreSQL 16 + pgvector, Aliyun OSS adapter protocols, React 18 + TypeScript, pytest with real `image_search_test`, Vitest, Docker Compose.

## Global constraints

- No commit, push, PR, GitHub mutation, deployment, Compose startup, shared-database migration, Kodo operation, or real OSS write/delete is authorized.
- The only eligible input is a non-cancelled `PurgeBatch.status == 'pending_deletion'` whose database restore point and object-copy manifest are independently verified and whose `retain_until` is still future-dated at deletion time.
- `PURGE_PIPELINE_EVIDENCE_DIR` policy `backup_only_no_delete` remains insufficient for formal deletion. `PURGE_FORMAL_DELETION_ENABLED` defaults to `0`, an absent/malformed deletion authorization evidence file is false, and T14 alone may define real enablement/credential injection.
- The formal source reader used by #26 remains Head/Get-only. A delete-capable credential and adapter are distinct from application, backup, source-reader, and isolation credentials, but T13's production composition must not read them or construct that adapter under any environment value.
- No public DTO, activity record, capability evidence, exception text, or UI state may contain bucket/key values, signed URLs, manifests, credentials, vector values, request bodies, or headers.
- Existing archived assets remain non-searchable during a purge; a successful final database transaction removes its `image_assets` row (and therefore its pgvector row) only after the checkpointed object consistency condition is met.
- `purge_batch_items.target_asset_id` stays an FK-free tombstone. Every checkpoint, failure, retry, and completion has an append-only event with `audit_retain_until >= created_at + 365 days`; this ticket adds no automatic deletion of that audit evidence, backups, fence epochs, or orphan objects.
- Integration tests use local `image_search_test` and an in-memory fake deletion adapter. They must never instantiate `oss2`, read `.env.backup`, or contact OSS.
- All locks use deterministic ordering: first formal object identity `(bucket, key)`, then target UUID, then batch/item rows. PostgreSQL advisory locks protect only the short fence state transition; the durable held row protects the external OSS/embedding interval. Never acquire asset/item row locks before a lower-sorted object lock.
- Formal object identity always uses an injected `FormalBucketIdentityProvider.bucket_name`, sourced from the private formal-storage configuration (`OSS_BUCKET_NAME` for application/image-worker/cleanup composition and `PURGE_SOURCE_OSS_BUCKET_NAME` only after equality with that configured formal identity is proved in the purge worker). `ImageAsset.source_bucket` is source provenance and must never participate in fence/advisory identities.

---

## State and error-code contract

### Batch states

`queued → database_backup → object_backup → verifying → pending_deletion` remains #26. #27 adds `deleting`, `completed`, and `partial_failure`.

- `pending_deletion → deleting`, `deleting_at`, and the first item's `original_delete_started_at` are committed in the same locked transaction, after formal object identity Head validation plus fence/reference checks and immediately before the first Delete call. It makes cancellation fail with `PURGE_BATCH_NOT_CANCELLABLE`.
- `deleting → completed` occurs only when every item is terminal-success. `deleting` remains visible when any item is retryable failure.
- Retention-expired work that cannot safely continue yields `partial_failure`; it is non-cancellable once `deleting_at` exists, exposes the remaining safe action, and does not keep assets permanently reserved by a stale batch.
- `cancel()` remains available through `pending_deletion` until the atomic first-delete-intent transaction wins; neither failed item retry nor process restart can return a `deleting`/`completed`/`partial_failure` batch to a cancellable state.

### Item checkpoints

Item *status* is one of `pending`, `in_progress`, `failed`, or `completed`. Its monotonic *checkpoint* is one of `pending`, `fenced`, `original_delete_started`, `original_deleted`, `preview_delete_started`, `preview_deleted`, `preview_shared`, or `completed`; failure only changes status/error and never regresses the checkpoint. Every transition has a timestamp. The asset-row removal, `database_deleted_at`, and `completed` checkpoint/status are written in one database transaction.

An item may release its held fence only after: its original object is known absent; its preview is known absent **or** has been rechecked as referenced and deliberately retained; and the asset row/vector has been removed. A worker crash leaves its held fence and preceding checkpoint durable. A first identity Head returning 404 is a safe failure; only a replay after a persisted matching `*_delete_started` intent may translate 404 into the corresponding deleted checkpoint.

### Stable #27 codes

Add and document: `PURGE_FORMAL_DELETION_DISABLED`, `PURGE_BATCH_NOT_READY_FOR_DELETION`, `PURGE_BACKUP_REVALIDATION_FAILED`, `PURGE_OBJECT_FENCE_CONFLICT`, `PURGE_OBJECT_FENCE_HELD`, `PURGE_ORIGINAL_REFERENCE_CONFLICT`, `PURGE_CONCURRENT_REFERENCE_BLOCKED`, `PURGE_ORIGINAL_MISSING_BEFORE_INTENT`, `PURGE_OBJECT_IDENTITY_MISMATCH`, `PURGE_ORIGINAL_DELETE_FAILED`, `PURGE_PREVIEW_DELETE_FAILED`, `PURGE_DATABASE_FINALIZATION_FAILED`, `PURGE_ITEM_NOT_RETRYABLE`, and `PURGE_REPROTECTION_REQUIRED`. `PURGE_PREVIEW_RETAINED_SHARED` and `PURGE_PREVIEW_RETAINED_UNBACKED` are result codes, never errors. Reuse #26 `PURGE_BACKUP_RETENTION_EXPIRED`, `PURGE_REFERENCE_SNAPSHOT_INVALID`, and `PURGE_BATCH_NOT_CANCELLABLE` unchanged. Codes are the only operational outcome exposed publicly.

## File structure

| Path | Responsibility |
| --- | --- |
| `backend/models/purge_batch.py` | #27 batch/item states, per-item checkpoint fields, safe DTO summaries, one-year retention timestamp. |
| `backend/models/purge_object_fence.py` | Append-only fence epochs, with a partial-unique held fence per `(formal_bucket, formal_key)`. |
| `backend/models/object_binding_fence.py` | Short-lived, lease-owned binding fence epoch for an object writer; never a purge audit record. |
| `backend/models/purge_item_event.py` | Append-only one-year checkpoint/retry/failure/completion evidence. |
| `backend/migrations/issue_27_formal_purge.py` | Explicit idempotent PostgreSQL schema extension; never invoked at startup. |
| `backend/services/purge_object_fence.py` | Canonical advisory-lock, fence-epoch acquisition, bind guard, current-reference recheck, and release repository. |
| `backend/services/object_binding_fence.py` | Canonical multi-key lease acquire/renew/release and purge-side live-binding exclusion. |
| `backend/services/formal_bucket_identity.py` | Single injected formal-Bucket identity provider used by binders, cleanup, manifest validation, and fence keys; never derives identity from source metadata. |
| `backend/services/formal_purge.py` | Worker-only per-item checkpoint orchestration; depends on injected repository, reference reader, and deletion protocol. |
| `backend/services/purge_object_storage.py` | Separate worker-only formal-delete config/adapter; existing read/backup/restore roles stay unchanged. |
| `backend/services/purge_formal_deletion_capability.py` | Strict hard-false authorization reader/writer interface; Flask receives only a no-secret reader. |
| `backend/services/purge_batch_control.py`, `backend/services/asset_recycle_bin.py` | One shared item-aware asset-reservation predicate plus pending-deletion claim, irreversible batch transition, item CAS/retry/list/detail and safe audit records. |
| `backend/services/asset_ingest.py`, `backend/services/image_import_worker.py`, `backend/services/import_cleanup.py` | Shared guard before every formal object write, binding promotion, reference check, or deletion. |
| `backend/scripts/run_purge_batch_worker.py`, `backend/services/purge_batch_worker.py` | Keep production #27 formal-purge handler uncomposed/unavailable in T13; permit injected fakes only in tests. |
| `backend/blueprints/admin_purge.py`, frontend batch types/API/grid | Read-only display of deletion state, success/failure counts and next safe action; no new enable/delete HTTP endpoint. |
| `backend/test/test_issue_27_*.py`, `backend/test/integration/test_issue_27_*.py` | Unit/static contracts and real-PostgreSQL plus fake-OSS behavior. |
| `AGENTS.md`, `docs/operations/purge-batch-pipeline-runbook.md`, `docs/operations/purge-gate-evidence.md` | Current operational facts, credential isolation, hard-disabled default, manual T14 authorization prerequisite. |

---

### Task 1: Lock the #27 contract with RED tests

**Files:**
- Create: `backend/test/test_issue_27_schema_static_contract.py`
- Create: `backend/test/test_issue_27_formal_purge_unit.py`
- Create: `backend/test/test_issue_27_fence_unit.py`
- Modify: `backend/test/test_issue_26_worker_static_contract.py`

**Interfaces:** Defines the required post-#27 statuses, codes, forbidden public fields, and the rule that only test fakes may implement formal deletion.

- [ ] **Step 1: Write failing state/contract tests**

```python
def test_issue_27_schema_defines_irreversible_batch_and_item_checkpoints():
    source = _read('models/purge_batch.py')
    assert "'deleting'" in source and "'completed'" in source
    for checkpoint in ('fenced', 'original_delete_started', 'original_deleted', 'preview_delete_started', 'preview_deleted', 'preview_shared', 'completed'):
        assert repr(checkpoint) in source

def test_only_formal_purge_composition_may_reference_the_delete_protocol():
    roots = _worker_composition_roots()
    assert 'delete_if_present' not in roots['app.py']
    assert 'delete_if_present' not in roots['blueprints/admin_purge.py']
    assert 'delete_if_present' in roots['services/formal_purge.py']
    assert 'OssFormalObjectDeleter' not in roots['scripts/run_purge_batch_worker.py']
    assert 'PURGE_DELETE_OSS_' not in roots['scripts/run_purge_batch_worker.py']
```

- [ ] **Step 2: Run RED tests**

Run: `cd backend && python -m pytest test/test_issue_27_schema_static_contract.py test/test_issue_27_formal_purge_unit.py test/test_issue_27_fence_unit.py -v`  
Expected: FAIL because #27 states, fence, and formal-purge protocol do not exist.

- [ ] **Step 3: Record the fixed acceptance matrix in test constants**

```python
TERMINAL_ITEM_SUCCESSES = {'completed'}
PRE_DATABASE_CONSISTENT = {'preview_deleted', 'preview_shared'}
FORBIDDEN_PUBLIC_FIELDS = {'oss_path', 'preview_oss_path', 'formal_key', 'bucket', 'manifest', 'vector'}
```

Assert a database row is never removed from a test item before its original checkpoint plus one `PRE_DATABASE_CONSISTENT` checkpoint; assert a completed fake deletion cannot issue a second delete call on retry.

- [ ] **Step 4: Run focused tests**

Run: `cd backend && python -m pytest test/test_issue_27_schema_static_contract.py test/test_issue_27_formal_purge_unit.py test/test_issue_27_fence_unit.py -v`  
Expected: still RED only for production implementations, with every acceptance edge described by a named test.

### Task 2: Add explicit schema, tombstones, and safe public summaries

**Files:**
- Create: `backend/models/purge_object_fence.py`
- Create: `backend/models/object_binding_fence.py`
- Create: `backend/migrations/issue_27_formal_purge.py`
- Modify: `backend/models/purge_batch.py`
- Modify: `backend/models/__init__.py`
- Modify: `backend/init_db.py`
- Modify: `postgres/init/01_init.sql`
- Modify: `backend/test/test_issue_27_schema_static_contract.py`

**Interfaces:** `PurgeObjectFence`, `FORMAL_PURGE_BATCH_STATUSES`, `FORMAL_PURGE_ITEM_STATUSES`, and `PurgeBatchItem.to_public_dict()`.

- [ ] **Step 1: Extend the red migration test with exact DDL requirements**

```python
assert 'CREATE TABLE IF NOT EXISTS purge_object_fences' in migration
assert 'CREATE UNIQUE INDEX IF NOT EXISTS uq_purge_object_fences_held_identity' in migration
assert "WHERE state = 'held'" in migration
assert 'released_at TIMESTAMP' in migration
assert 'target_asset_id UUID NOT NULL' in migration
assert 'retained_until TIMESTAMP NOT NULL' in migration
assert 'REFERENCES image_assets' not in migration.split('purge_object_fences', 1)[1]
assert 'CREATE TABLE IF NOT EXISTS object_binding_fences' in migration
assert 'uq_object_binding_fences_held_identity' in migration
assert "owner_kind IN ('asset_ingest', 'import_promotion', 'import_cleanup')" in migration
```

- [ ] **Step 2: Run RED migration/model tests**

Run: `cd backend && python -m pytest test/test_issue_27_schema_static_contract.py -v`  
Expected: FAIL because the explicit migration and ORM extensions are absent.

- [ ] **Step 3: Implement additive schema only**

```python
class PurgeObjectFence(db.Model):
    __tablename__ = 'purge_object_fences'
    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    formal_bucket = db.Column(db.String(255), nullable=False)
    formal_key = db.Column(db.Text, nullable=False)
    kind = db.Column(db.String(24), nullable=False)
    batch_id = db.Column(Uuid(as_uuid=True), nullable=False)
    target_asset_id = db.Column(Uuid(as_uuid=True), nullable=False)
    state = db.Column(db.String(24), nullable=False, default='held')
    acquired_at = db.Column(db.DateTime, nullable=False)
    released_at = db.Column(db.DateTime, nullable=True)
    audit_retain_until = db.Column(db.DateTime, nullable=False)
```

Use a `CHECK state IN ('held', 'released')`, indexes on `batch_id,target_asset_id` and `formal_bucket,formal_key`, and a PostgreSQL partial unique index permitting exactly one held epoch for each identity. A release writes `released_at` and never overwrites/deletes the historical row; a later reuse inserts a new epoch. Add item fields for `status`, `checkpoint`, `original_delete_started_at`, `original_deleted_at`, `preview_delete_started_at`, `preview_deleted_at`, `database_deleted_at`, `completed_at`, `failed_at`, `attempt_count`, lease token/generation/expiry, and `audit_retain_until`. Add a `purge_item_events` table with UUID primary key, batch/item tombstone identities, event type, safe result/error code, created timestamp, and `audit_retain_until`. Migrate existing #26 rows to item `pending` status/checkpoint only through this explicit migration. Add batch `deleting_at`, `partial_failure_at`, and status constraints. Keep all asset target identities FK-free.

`to_public_dict()` returns counts plus item `status`, `checkpoint`, safe code, timestamps, and `next_action`; never a formal identity. Tests must prove two released fence epochs for one object key survive a later reuse and second purge. Mirror all fields/constraints/indexes in the migration and initial schema. Do not run the migration.

- [ ] **Step 4: Run schema tests**

Run: `cd backend && python -m pytest test/test_issue_26_schema_static_contract.py test/test_issue_27_schema_static_contract.py -v`  
Expected: PASS without connecting to a shared database.

### Task 3: Implement the durable fence and common binding lock discipline

**Files:**
- Create: `backend/services/purge_object_fence.py`
- Create: `backend/services/formal_bucket_identity.py`
- Create: `backend/models/object_binding_fence.py`
- Create: `backend/services/object_binding_fence.py`
- Modify: `backend/services/asset_ingest.py`
- Modify: `backend/services/image_import_worker.py`
- Modify: `backend/services/import_cleanup.py`
- Modify: `backend/test/test_issue_27_fence_unit.py`
- Create: `backend/test/integration/test_issue_27_fence_race.py`

**Interfaces:**

```python
class ObjectBindingBlocked(RuntimeError): error_code = 'PURGE_OBJECT_FENCE_HELD'
class PurgeObjectFenceService:
    def bind_guard(self, identities: tuple[ObjectIdentity, ...]) -> ContextManager[None]: ...
    def acquire_for_deletion(self, *, batch_id, target_asset_id, identities) -> None: ...
    def recheck_unreferenced(self, *, target_asset_id, identity, kind) -> ReferenceDecision: ...
    def release_completed(self, *, batch_id, identities) -> None: ...

class FormalBucketIdentityProvider:
    def formal_bucket(self) -> str: ...
```

#### Binding-fence lease protocol (approved 2026-08-30; architect review pending)

`object_binding_fences` is intentionally distinct from `purge_object_fences`:

```text
id UUID primary key
formal_bucket VARCHAR(255) not null
formal_key TEXT not null
owner_kind VARCHAR(32) not null
owner_token UUID not null
state VARCHAR(16) not null              -- held | released
acquired_at TIMESTAMP not null
lease_expires_at TIMESTAMP not null
released_at TIMESTAMP null
release_reason VARCHAR(32) null          -- completed | failed | lease_expired
```

- Valid `owner_kind` values are exactly `asset_ingest`, `import_promotion`, and `import_cleanup`.
- ORM/DDL enforce `owner_kind IN ('asset_ingest','import_promotion','import_cleanup')`, `state IN ('held','released')`, `lease_expires_at > acquired_at`, and a held/released consistency CHECK; PostgreSQL partial unique index permits only one `state='held'` epoch per `(formal_bucket, formal_key)`. Add indexes for `(owner_token,state,lease_expires_at)` and `(formal_bucket,formal_key,state,lease_expires_at)`. Released rows remain only through the short binding-fence retention policy and are never `purge_object_fences` audit evidence.
- **All lease decisions use PostgreSQL `clock_timestamp()`**, not Python clocks or transaction-stable `now()`: acquire, renew, final bind, and purge takeover compare against it. A non-expired owner cannot be taken over; an expired owner cannot renew or revive even before a successor appears.
- A caller canonicalizes and de-duplicates its *entire* identity set, takes advisory locks for the entire sorted set before inspecting any fence, and uses one owner token. Acquire either creates/reuses all its epochs or rolls back all of them if one live foreign owner exists. Under the same locks and transaction, an expired epoch changes `held → released/release_reason='lease_expired'`, then the successor inserts its held epoch.
- Renew and final bind take the same complete advisory-lock set and verify every expected `{row_id,key,owner_token}` against `held AND lease_expires_at > clock_timestamp()`. Any missing/expired/mismatched row fails the full operation without partially renewing or binding. Final bind writes the database reference and releases all required binding fences in the one transaction. After that verification succeeds, a deadline crossing before commit is safe: purge takeover must wait on the same locks and sees the committed new reference.
- Success and handled failures release owner-held rows. A crash needs no cleanup process: another complete-set acquire transaction may only replace fully expired epochs. An old `id + owner_token` can never renew, bind, or release a successor epoch. Ownership loss causes safe discard/retry, never deletion or a dangling DB binding.
- Purge acquisition takes the complete same advisory-lock set, rejects any live binding fence before it inserts **any** deletion fence, and rolls back its whole acquire on conflict. `purge_object_fences` remains append-only one-year formal-clearance audit; it is never used to represent an in-flight binding.
- `_ingest_batch` uses a chunk owner token and the complete de-duplicated set for that chunk. It acquires fences and writes all formal objects before existing `embed_normalized_images`; renews the complete current set before/after that call; preserves `embedding.batch_calls == [20, 1]`, one preview Put per content, and per-item invalid-vector isolation. An original fence may release with its final bound asset; a shared preview fence releases only after every chunk consumer has bound or failed. Failed embedding/vector items release only no-longer-needed fences and retain OSS objects.

- [ ] **Step 1: Write RED lock-order and race tests**

```python
def test_binding_started_before_fence_commits_before_recheck_and_protects_preview(pg_sessions):
    # Writer holds canonical object advisory lock and commits its import binding.
    # Purger then acquires it, sees the binding, and returns PREVIEW_SHARED without delete.

def test_fence_committed_before_binding_rejects_before_storage_put(fake_storage, session):
    with pytest.raises(ObjectBindingBlocked):
        service.queue_one('incoming.png')
    assert fake_storage.put_calls == []

def test_fence_identity_uses_formal_bucket_not_source_provenance(app):
    asset = _asset(source_bucket='kodo-backup-name')
    assert fence_service.identity_for(asset).formal_bucket == app.config['OSS_BUCKET_NAME']
    assert asset.source_bucket not in fence_service.identity_for(asset).lock_material

def test_released_epoch_allows_reuse_but_preserves_both_fence_audit_rows(pg_sessions):
    # Complete delete/release for key, bind a new generation, then release again.
    assert _fence_epochs_for(key) == 2
    assert all(row.audit_retain_until >= row.acquired_at + timedelta(days=365) for row in _fence_epochs_for(key))

def test_live_owner_cannot_be_taken_over_using_a_skewed_application_clock(pg_sessions):
    # SQL clock_timestamp(), not a client-provided datetime, decides liveness.
    first = binding_fences.acquire(all_keys, owner_kind='asset_ingest')
    with pytest.raises(BindingFenceHeld):
        binding_fences.acquire_from_second_session(all_keys, owner_kind='asset_ingest')
    assert first.owner_token != uuid.uuid4()

def test_multikey_acquire_is_atomic_when_one_key_has_live_foreign_owner(pg_sessions):
    _hold_foreign('preview/shared')
    with pytest.raises(BindingFenceHeld):
        binding_fences.acquire((original_key, shared_preview), owner_kind='asset_ingest')
    assert _held_by_current_owner(original_key) is False

def test_old_owner_cannot_bind_after_expiry_takeover(pg_sessions):
    old = binding_fences.acquire(keys, owner_kind='asset_ingest')
    _advance_database_clock_past(old.lease_expires_at)
    new = binding_fences.acquire(keys, owner_kind='asset_ingest')
    assert binding_fences.final_bind(old, bind=lambda: _create_asset()) is False
    assert binding_fences.final_bind(new, bind=lambda: _create_asset()) is True
```

- [ ] **Step 2: Run RED fence tests**

Run: `cd backend && python -m pytest test/test_issue_27_fence_unit.py test/integration/test_issue_27_fence_race.py -v`  
Expected: FAIL because neither common locks nor a durable fence exist.

- [ ] **Step 3: Implement one canonical guard used by every binding writer**

Inject one `FormalBucketIdentityProvider` into application ingest, image worker, import cleanup, and purge composition. It returns the configured private formal OSS Bucket, never `ImageAsset.source_bucket`/`ImageImportItem.source_bucket`. The purge worker compares this identity to the canonical manifest formal bucket and to the read-only purge-source config before every deletion attempt; mismatch fails closed before fence acquisition. Within a database transaction, take `pg_advisory_xact_lock(hashtextextended(formal_bucket || ':' || formal_key, 0))` once per identity in sorted `(bucket, key)` order. The guard reads held fence epochs with `FOR UPDATE`; a held fence raises `ObjectBindingBlocked` before formal storage I/O or binding insertion. A retry owned by the same batch/item may recover its own held epoch even when its worker claim generation changed; other owners fail safely.

Refactor `ImageAssetIngestService` rather than wrapping its current calls: download/hash/normalize identifies keys; a short transaction takes canonical advisory locks and persists a held **binding** fence; OSS Head/Put-if-absent occurs while that durable fence is held; embedding then runs without advisory locks but before formal binding; finally a short transaction verifies the same fence owner, persists `ImageAsset` or `ImageImportItem`, and releases the fence. This deliberately preserves #19/#25 behavior: object metadata conflict fails before embedding, and embedding failure/invalid vector leaves uploaded objects for retry rather than deleting them. `_ingest_batch` atomically acquires its complete de-duplicated chunk identity set, writes the chunk's objects, preserves one `embed_normalized_images` batch call, final-binds items individually, releases exclusive originals per item, and releases a shared preview only after its last chunk consumer completes or fails. A crash leaves only lease-expirable chunk epochs. Apply this to synchronous, batch, and queued import paths.

In `ImageImportWorker`, reacquire the guard for both claimed identities before promoting its completed import to `ImageAsset`; an existing held fence makes the worker safely fail/retry without an asset row. In `import_cleanup`, take the same guard before its reference check, formal deletion, and `objects_purged_at` write, so its existing cleanup path cannot reopen the race. Add a static inventory test enumerating every production `ImageAsset(` / `ImageImportItem(` binding construction and every `delete_object`/object cleanup call, asserting it delegates to the shared guard.

`acquire_for_deletion()` obtains the same locks, inserts/locks held fence epochs in a committed transaction, then performs a fresh locked current-reference scan. Asset references in both lifecycle states always count. An unfinished or recoverable import counts, except `status == 'completed' and asset_id == target_asset_id`, which is target lineage rather than a competing reference; finalization marks this lineage `objects_purged_at` (or a precise equivalent) in the same transaction as removal so it cannot protect the object forever. Any other original reference fails the item; any other preview reference produces the success disposition `preview_shared`. A transaction that observes a held foreign fence fails safely; no operation waits indefinitely—use configured PostgreSQL lock timeout and map timeout to `PURGE_CONCURRENT_REFERENCE_BLOCKED`.

- [ ] **Step 4: Run unit and real-PostgreSQL race tests**

Run: `cd backend && python -m pytest test/test_issue_27_schema_static_contract.py test/test_issue_27_fence_unit.py test/test_issue_18_source_identity_unit.py test/test_issue_20_worker_retry_unit.py test/test_issue_22_cleanup_unit.py test/integration/test_issue_27_fence_race.py test/integration/test_issue_27_binding_fence_leases.py -v`  
Expected: PASS, or the integration module explicitly skips only if localhost PostgreSQL is unavailable.

### Task 4: Create worker-only formal deletion adapter and hard-false authorization seam

**Files:**
- Create: `backend/services/purge_formal_deletion_capability.py`
- Modify: `backend/services/purge_object_storage.py`
- Modify: `backend/scripts/run_purge_batch_worker.py`
- Modify: `backend/services/purge_batch_worker.py`
- Modify: `backend/.env.example`
- Modify: `backend/test/test_issue_27_formal_purge_unit.py`
- Create: `backend/test/test_issue_27_deletion_isolation.py`

**Interfaces:**

```python
class FormalObjectDeleter(Protocol):
    def head_verified(self, key: str, *, expected_size: int, expected_sha256: str | None) -> ObjectObservation: ...
    def delete_after_verified_head(self, observation: ObjectObservation) -> DeleteResult: ...

class FormalDeletionCapabilitySource(Protocol):
    def evaluate(self, now: datetime) -> bool: ...
```

- [ ] **Step 1: Write RED isolation tests**

```python
def test_t13_worker_never_constructs_a_delete_adapter_even_if_enable_vars_exist(monkeypatch, env):
    env.update({'PURGE_FORMAL_DELETION_ENABLED': '1', 'PURGE_DELETE_OSS_ACCESS_KEY_ID': 'fake'})
    worker = _build_worker(env, formal_deleter_factory=_must_not_be_called)
    assert worker.formal_deleter is None

def test_formal_delete_credential_cannot_reuse_any_existing_role(env):
    env['PURGE_DELETE_OSS_ACCESS_KEY_ID'] = env['PURGE_SOURCE_OSS_ACCESS_KEY_ID']
    with pytest.raises(PurgeObjectStorageConfigError):
        PurgeDeleteStorageConfig.from_env(env)
```

- [ ] **Step 2: Run RED isolation tests**

Run: `cd backend && python -m pytest test/test_issue_27_deletion_isolation.py test/test_issue_27_formal_purge_unit.py -v`  
Expected: FAIL because formal-delete configuration/protocol and a separate capability source do not exist.

- [ ] **Step 3: Add isolated protocol without enabling it**

Add `PurgeDeleteStorageConfig` and `OssFormalObjectDeleter` in `purge_object_storage.py`. It validates `PURGE_DELETE_OSS_*` values are non-empty, unique across `OSS_*`, `BACKUP_OSS_*`, `PURGE_SOURCE_OSS_*`, and `PURGE_RESTORE_OSS_*`, and uses the same formal bucket. It exposes a verified Head observation then Delete followed by post-Head; it never claims an unavailable conditional DeleteObject primitive, never supports list, put, overwrite, or batch delete.

The database fence protects only repository writers. Therefore #27 records an explicit T14 enablement stop condition: T14 must demonstrate either an OSS version/conditional-delete token carried from Head through exact-version delete, **or** a deployment-enforced no-overwrite/no-external-writer trust boundary for the formal bucket. Without one, T14 must not compose/enable the adapter. Fake tests simulate a post-Head identity change and require the adapter to report an identity-integrity failure rather than success; the test documents detection, not a claim that T13 can prevent an out-of-band replacement.

The `PurgeDeleteStorageConfig`/`OssFormalObjectDeleter` class exists only for direct unit tests and a future T14 composition change. T13's worker composition unconditionally injects `UnavailableFormalDeletionCapabilitySource`, reads no `PURGE_DELETE_OSS_*`, creates no delete adapter, and sets `worker.formal_deleter = None` even if hostile environment variables claim enablement. `.env.example` may document only the explanatory hard value `PURGE_FORMAL_DELETION_ENABLED=0`; it contains no deletion credentials or evidence path. The Flask app has no import path to this module. T14 must separately change composition, evidence policy, deployment credentials, and human authorization; #27 only supplies test injection seams.

- [ ] **Step 4: Run isolation/static tests**

Run: `cd backend && python -m pytest test/test_issue_27_deletion_isolation.py test/test_issue_26_worker_static_contract.py -v`  
Expected: PASS; existing #26 static assertions are revised only to permit the dedicated delete adapter in its narrow worker composition root.

### Task 5: Implement checkpointed partial-success worker execution

**Files:**
- Create: `backend/services/formal_purge.py`
- Modify: `backend/services/purge_batch_control.py`
- Modify: `backend/services/asset_recycle_bin.py`
- Modify: `backend/services/purge_batch_worker.py`
- Modify: `backend/scripts/run_purge_batch_worker.py`
- Modify: `backend/test/test_issue_27_formal_purge_unit.py`
- Create: `backend/test/integration/test_issue_27_formal_purge.py`

**Interfaces:**

```python
class FormalPurgeRepository(Protocol):
    def claim_pending_deletion(self, *, worker_id, lease_seconds) -> ClaimedFormalPurgeItem | None: ...
    def checkpoint_if_current(self, claim, *, status, checkpoint, result_code=None, error_code=None) -> bool: ...
    def finalize_asset_if_current(self, claim, *, fence_ids) -> bool: ...

class FormalPurgeWorker:
    def process_one_item(self) -> bool: ...
```

- [ ] **Step 1: Write RED checkpoint tests**

```python
def test_preview_delete_failure_keeps_original_checkpoint_and_retries_only_preview(fake_deleter):
    fake_deleter.fail_on('preview/key')
    worker.process_one_item()
    assert item.checkpoint == 'original_deleted'
    worker.retry_item(item.id)
    worker.process_one_item()
    assert fake_deleter.calls_for('original/key') == 1

def test_first_missing_original_fails_but_missing_after_persisted_intent_recovers(fake_deleter):
    fake_deleter.remove_before_first_head('original/key')
    worker.process_one_item()
    assert item.error_code == 'PURGE_ORIGINAL_MISSING_BEFORE_INTENT'
    _persist_original_delete_intent(item)
    worker.process_one_item()
    assert item.checkpoint == 'original_deleted'

def test_shared_preview_is_retained_but_asset_and_vector_are_removed(session, fake_deleter):
    worker.process_one_item()
    assert fake_deleter.calls_for(shared_preview) == 0
    assert session.get(ImageAsset, target.id) is None
    assert item.checkpoint == 'completed'

def test_unbacked_manifest_protected_preview_is_retained_when_later_unreferenced(fake_deleter):
    worker.process_one_item()
    assert item.result_code == 'PURGE_PREVIEW_RETAINED_UNBACKED'
    assert fake_deleter.calls_for(protected_preview) == 0
```

- [ ] **Step 2: Run RED unit/integration tests**

Run: `cd backend && python -m pytest test/test_issue_27_formal_purge_unit.py test/integration/test_issue_27_formal_purge.py -v`  
Expected: FAIL because pending-deletion batches cannot be claimed item-by-item.

- [ ] **Step 3: Implement exact deletion sequence**

Add item claim token/generation/lease fields and use `FOR UPDATE SKIP LOCKED` item claims, so items can make independent progress while batch state remains CAS-protected. Tests cover two workers, expired item lease, late current-token result, and own-fence recovery. A retry changes only `failed → pending` for that item, preserves checkpoint, appends an audit event, and cannot reclaim a completed item.

Before the first delete intent in a batch, run one #26-compatible complete-batch revalidation: batch IDs/digests must equal canonical database/object manifests; manifest batch ID, selected asset IDs, formal bucket, object kind/key/asset/size/hash and verified backup copy must all match; both retention deadlines are future; and the complete reference snapshot is fresh. A preview marked `reference_protected` by that canonical manifest remains permanently ineligible for deletion even if later references vanish. `backup_only_no_delete` is evidence only, never authorization.

After any item succeeds, never call the #26 whole-batch snapshot/revalidation again because completed tombstones legitimately lack `image_assets` rows. Each later/retried item instead verifies immutable manifest/copy/retention evidence and its own still-archived target, then uses Task 3's locked current-reference query. A completed item is a legal manifest member missing from `image_assets`, never a deletion candidate.

Then, in order:

1. Derive identities only from the canonical manifest and current archived target; acquire Task 3 fences; Head each candidate and validate expected identity. Original must be exclusively referenced. A live other preview reference yields terminal success disposition `preview_shared`; a manifest `reference_protected` preview yields `preview_retained_unbacked`; neither calls Delete.
2. For the first item only, in this same locked transaction after successful Head/fence/recheck, CAS-write batch `pending_deletion → deleting`, `deleting_at`, item `status=in_progress`, checkpoint `original_delete_started`, and `original_delete_started_at`. `cancel()` locks the same batch row, so cancel versus start has exactly one winner. Subsequent original/preview attempts first persist their respective `*_delete_started` checkpoint/intent.
3. Call the injected fake/authorized deleter only after its delete intent commits. A first Head 404 is `PURGE_ORIGINAL_MISSING_BEFORE_INTENT`; only a replay with the matching durable intent may turn a 404 into `original_deleted` or `preview_deleted`. Identity mismatch is `PURGE_OBJECT_IDENTITY_MISMATCH` and never calls Delete.
4. CAS-write `original_deleted`; if the preview is deletable, persist `preview_delete_started`, then delete and CAS-write `preview_deleted`; otherwise CAS-write the appropriate retained-preview result.
5. In one locked transaction verify current item claim, all required checkpoints, fence ownership, and target completed-import lineage; run `session.execute(delete(ImageAsset).where(ImageAsset.id == claim.target_asset_id))`, mark target completed import bindings `objects_purged_at`, update `database_deleted_at`, checkpoint/status `completed`, release fence epochs, and append completion evidence. This is the only permitted `ImageAsset` deletion in the repository. The vector disappears with the row.

Every external call occurs after its preceding checkpoint commits. Current item claim token/generation/status is required for every checkpoint and finalization; stale results append only `purge.item.stale_result`. A delete failure leaves the last successful checkpoint and fence held, marks the item failed/retryable, and rolls back no completed peer. The batch becomes `completed` only when a locked count proves all items `completed`; otherwise its item-level status drives retry.

Define one shared query predicate `item_holds_asset_for_purge(item, batch)` used by `PurgeBatchControlService.create_or_replay()` and `asset_recycle_bin.restore_image_assets()`: it holds every item whose target asset remains present and is pending/in-progress/**any retryable failure**, regardless of whether a fence was reached. It releases only `completed`, or an explicit non-retryable `PURGE_REPROTECTION_REQUIRED` item whose fence is released **and** whose original is still present. Any item whose original is confirmed absent always holds its fence and asset reservation until database/vector tombstone finalization succeeds; it is never eligible for restore or a replacement batch. This replaces each current service's broad “any non-cancelled batch” query and is tested once through both callers.

If retention expires before any delete intent, preserve #26 behavior: mark the batch failed with `PURGE_BACKUP_RETENTION_EXPIRED`; it remains cancellable, and only cancel plus a new batch/key/confirmation releases its asset. If retention expires after the non-cancellable first intent: (a) a still-present, not-yet-deleted original becomes non-retryable `PURGE_REPROTECTION_REQUIRED`, releases its fence, and no longer holds the asset; (b) an original already confirmed absent never proceeds to preview deletion, records a retained-preview degraded result, holds its fence/reservation, and retries **only** target completed-import lineage and database/vector tombstone finalization. A failed finalization neither repeats any object deletion nor releases the fence, restore block, or new-batch exclusion. An irreconcilable target identity mismatch is a non-released manual-safety error with the same block. Only successful tombstone finalization releases the fence and completes the item. In one lock-held transaction after every item state/event/fence write, derive the batch: all item statuses completed → `completed`; any retryable failure → `deleting`; no retryable failure plus one or more released/reprotection-required still-present item → `partial_failure`. `partial_failure` is never claimable; successful peers remain completed, retryable peers remain retryable, and only released still-present targets can appear in a fresh protected batch. Test a mixed three-item batch plus retention-expired/original-absent/finalization-failed replay.

- [ ] **Step 4: Run focused behavior suite**

Run: `cd backend && python -m pytest test/test_issue_27_formal_purge_unit.py test/test_issue_26_control_unit.py test/test_issue_26_worker_unit.py test/integration/test_issue_27_formal_purge.py -v`  
Expected: PASS, or explicit PostgreSQL skip only when unavailable.

### Task 6: Preserve API/UI observability without a deletion control plane

**Files:**
- Modify: `backend/blueprints/admin_purge.py`
- Modify: `frontend/src/types/product.ts`
- Modify: `frontend/src/services/productApi.ts`
- Modify: `frontend/src/services/productApi.test.ts`
- Modify: `frontend/src/components/ArchivedAssetGrid.tsx`
- Modify: `frontend/src/components/ArchivedAssetGrid.test.tsx`
- Create: `backend/test/test_issue_27_admin_api_unit.py`

**Interfaces:** Existing GET batch DTOs gain safe counts `completed_count`, `failed_count`, `pending_count`, `cancellable`, and item `next_action`; no new HTTP endpoint turns on deletion.

- [ ] **Step 1: Write RED DTO/UI tests**

```tsx
it('renders completed and failed counts plus the server-provided item next action', async () => {
  render(<ArchivedAssetGrid {...props} purgeBatch={deletingWithPartialFailure} />);
  expect(screen.getByText('已完成 1')).toBeVisible();
  expect(screen.getByText('失败 1')).toBeVisible();
  expect(screen.getByText('可安全重试')).toBeVisible();
});

it('never displays an OSS path or enables cancel after deleting begins', () => {
  render(<ArchivedAssetGrid {...props} purgeBatch={deleting} />);
  expect(screen.queryByRole('button', {name: '取消批次'})).toBeNull();
  expect(screen.queryByText(/original\//)).toBeNull();
});
```

- [ ] **Step 2: Run RED API/UI tests**

Run: `cd backend && python -m pytest test/test_issue_27_admin_api_unit.py -v && cd ../frontend && npm test --run ArchivedAssetGrid.test.tsx productApi.test.ts`  
Expected: FAIL because #27 DTO fields and rendering are absent.

- [ ] **Step 3: Implement display-only changes**

Map only safe code/status/count fields from `PurgeBatch.to_public_dict()`. Preserve #26 authentication and gate ordering; `GET` remains observable even with a closed gate. `cancel` returns `PURGE_BATCH_NOT_CANCELLABLE` after `deleting_at` is set. Existing retry endpoint may enqueue only a failed item/batch according to server rules; it cannot reset a completed item or create a new confirmation. The UI calls no delete endpoint, does not synthesize status, and shows “删除功能尚未启用” when `PURGE_FORMAL_DELETION_DISABLED` is returned.

- [ ] **Step 4: Run focused API/UI verification**

Run: `cd backend && python -m pytest test/test_issue_26_admin_api_unit.py test/test_issue_27_admin_api_unit.py -v && cd ../frontend && npm test --run ArchivedAssetGrid.test.tsx productApi.test.ts && npm run build`  
Expected: PASS.

### Task 7: Prove real-PostgreSQL behavior and permanent search invisibility

**Files:**
- Create: `backend/test/integration/test_issue_27_shared_preview.py`
- Create: `backend/test/integration/test_issue_27_partial_retry.py`
- Create: `backend/test/integration/test_issue_27_vector_invisibility.py`
- Create: `backend/test/integration/test_issue_27_binding_fence_leases.py`
- Modify: `backend/test/integration/conftest.py` only if a reusable multi-session fixture is missing

**Interfaces:** All formal object operations use `FakeFormalObjectDeleter` in-process; no cloud adapter construction.

- [ ] **Step 1: Write integration tests against distinct PostgreSQL sessions**

```python
def test_purger_and_concurrent_import_binding_cannot_delete_the_new_reference(pg_session_factory):
    # Synchronize writer/purger at canonical advisory lock; assert either the
    # binding commits before recheck and preview is retained, or binding raises
    # PURGE_OBJECT_FENCE_HELD before fake-storage put. Neither branch deletes it.

def test_completed_purge_is_absent_from_pgvector_search(app, fake_deleter):
    _complete_one_pending_deletion_item(app, fake_deleter)
    assert VectorSearchService().search_by_vector([0.0] * 1024) == []
```

- [ ] **Step 2: Run RED integration suite**

Run: `cd backend && python -m pytest test/integration/test_issue_27_shared_preview.py test/integration/test_issue_27_partial_retry.py test/integration/test_issue_27_vector_invisibility.py -v`  
Expected: FAIL before implementation, or explicit PostgreSQL skip only when the server is unreachable.

- [ ] **Step 3: Complete fixtures and assertions**

Cover exactly: exclusive original deletion; shared active and archived preview retention; unfinished/cancelled/failed recoverable import reference retention; completed target-import lineage release; concurrent writer/purger/`import_cleanup` three-way competition; same key delete/rebind/delete fence epochs; binding-fence complete-set renew atomicity; live binding lease blocks purge before any deletion fence; purge deletion fence blocks binding before OSS I/O; expired takeover rejects old renew/final bind; OSS Put then crash/lease takeover/late embedding result never creates a DB reference; final binding wins then purge recheck sees the reference; chunk shared preview releases only after its last consumer; delete failure after original; crash after fence, after Delete, and before checkpoint; retry/restart idempotency; duplicate item worker claim/CAS stale result; cancel versus first intent race; manifest-protected preview later becoming unreferenced; a pre-fence transient manifest failure that still blocks create/restore; retention expiry before intent and after original deletion; a three-item completed/retryable/reprotection-required batch reduction; retention-expired original-absent database-finalization failure replay that holds fence/reservation and retains preview; database-finalization rollback; retry endpoint preserving completed peers; create and recycle-bin shared reservation predicate behavior; append-only one-year audit retention; asset/vector removal only after the object condition; and first-404 versus replay-404 behavior. Assert fake calls never receive real credentials and every successful target is absent from direct SQL and `VectorSearchService` results.

- [ ] **Step 4: Run the complete #27 integration set**

Run: `cd backend && python -m pytest test/integration/test_issue_27_fence_race.py test/integration/test_issue_27_binding_fence_leases.py test/integration/test_issue_27_formal_purge.py test/integration/test_issue_27_shared_preview.py test/integration/test_issue_27_partial_retry.py test/integration/test_issue_27_vector_invisibility.py -v`  
Expected: PASS, or an explicitly reported PostgreSQL skip only when unreachable.

### Task 8: Update static safety contracts and operational facts

**Files:**
- Modify: `backend/test/test_issue_26_worker_static_contract.py`
- Create: `backend/test/test_issue_27_static_contract.py`
- Modify: `AGENTS.md`
- Modify: `docs/operations/purge-batch-pipeline-runbook.md`
- Modify: `docs/operations/purge-gate-evidence.md`
- Modify: `docs/operations/purge-object-backup-restore-runbook.md`
- Modify: `backend/.env.example`

**Interfaces:** Documents the T13 code boundary accurately while preserving an operationally hard-disabled delete path until T14.

- [ ] **Step 1: Write RED documentation/static tests**

```python
def test_t13_documents_two_independent_capabilities_and_default_disable():
    agents = _root('AGENTS.md')
    runbook = _root('docs/operations/purge-batch-pipeline-runbook.md')
    assert 'pending_deletion' in agents
    assert 'T14' in agents
    assert 'PURGE_FORMAL_DELETION_ENABLED=0' in runbook
    assert '正式删除前仍须重新校验保留期与引用' in agents
```

- [ ] **Step 2: Run RED static tests**

Run: `cd backend && python -m pytest test/test_issue_27_static_contract.py -v`  
Expected: FAIL until operational docs match the #27 runtime facts.

- [ ] **Step 3: Update facts, not implementation history**

State: T13's worker composition never reads deletion credentials, evidence, or enable values and never constructs a deleter; the adapter class is test-only until T14 changes composition under human authorization. `pending_deletion` revalidates retention/references before first intent; the atomic first intent makes a batch non-cancellable; each item is recoverable from its checkpoint; partial retention failure has a documented re-protection state; fence epochs and append-only events retain one-year evidence; no automatic purge of backups/audit/fences/orphans exists. Restrict static scans by role: formal asset-deletion orchestration is allowed only in `services/formal_purge.py`; concrete OSS Delete is allowed only in `services/purge_object_storage.py`; `services/import_cleanup.py` retains only its existing guarded temporary-import cleanup delete path. Even `run_purge_batch_worker.py` must not import/configure the concrete deleter in T13. Everywhere else, including Flask/API/frontend, delete primitives remain forbidden. Still prohibit Kodo, list, put, overwrite, batch-delete, public URL, and credential leakage.

- [ ] **Step 4: Run final allowed verification**

Run:

```bash
cd backend && python -m pytest \
  test/test_issue_26_schema_static_contract.py \
  test/test_issue_26_control_unit.py \
  test/test_issue_26_worker_unit.py \
  test/test_issue_26_worker_static_contract.py \
  test/test_issue_27_schema_static_contract.py \
  test/test_issue_27_fence_unit.py \
  test/test_issue_27_deletion_isolation.py \
  test/test_issue_27_formal_purge_unit.py \
  test/test_issue_27_admin_api_unit.py \
  test/test_issue_27_static_contract.py -v
cd backend && python -m pytest \
  test/integration/test_issue_27_fence_race.py \
  test/integration/test_issue_27_binding_fence_leases.py \
  test/integration/test_issue_27_formal_purge.py \
  test/integration/test_issue_27_shared_preview.py \
  test/integration/test_issue_27_partial_retry.py \
  test/integration/test_issue_27_vector_invisibility.py -v
cd frontend && npm test --run ArchivedAssetGrid.test.tsx productApi.test.ts && npm run build
git diff --check
git diff --stat
```

Expected: all executed tests pass; PostgreSQL unavailability is reported as skipped/not run, never passed. Do not run real OSS tests, manual pgvector benchmarks, migrations, Compose, or external services.

## Plan self-review record

- **Spec coverage:** pending-deletion admission, retention/reference revalidation, irreversible cancellation boundary, item checkpoints, original exclusivity, shared previews, concurrent writers, partial retries, audit retention, DTO/UI observability, real PostgreSQL + fake OSS, and hard-disabled production capability each have a task.
- **Lock correctness:** storage writes and binding persistence share the same canonical advisory-lock guard; a deletion fence is committed before object deletion and checked by all binders, closing both sides of the reference race.
- **Authorization check:** no step enables a real delete adapter, reads deletion credentials/evidence in worker composition, uses `.env.backup` in tests, contacts OSS, starts services, or executes an explicit migration. T14 remains a separate human-authorization gate.
- **Architect review (2026-08-29, round 1):** APPROVE WITH REQUIRED PLAN CHANGES. Incorporated: append-only multi-epoch fence history; per-item guarded batch refactor plus import-cleanup inventory; exact manifest membership; first-delete full batch revalidation versus later item revalidation; completed-import lineage; delete intent semantics; explicit item claims; atomic cancel/start race; partial-retention policy; shared-preview result codes; T13 unconditional no-deleter composition; and append-only one-year events.
- **Architect review (2026-08-29, round 2):** APPROVE WITH REQUIRED PLAN CHANGES. Incorporated: corrected status/checkpoint/static-contract split; injected formal-Bucket identity separated from source provenance; shared item-aware create/restore reservation predicate; explicit T14 conditional-delete/no-overwrite stop condition; and lock-transaction batch reduction for mixed terminal/retryable/reprotection outcomes. Resubmit before implementation.
- **Architect review (2026-08-29, round 3):** APPROVE WITH REQUIRED PLAN CHANGES. Incorporated: every retryable item retains its asset reservation even before fence acquisition; an original-confirmed-absent item remains fenced/reserved and may retry database tombstone finalization only, never preview deletion, restore, or replacement-batch creation. Resubmit before implementation.
- **User decision (2026-08-30):** Preserve #19/#25 object-persistence semantics. Formal OSS Head/Put-if-absent occurs during a durable held binding-fence period *before* embedding; embedding failure/invalid vectors leave those objects intact for retry, and object metadata conflicts fail before embedding. Advisory locks protect only short fence-state transactions and never span embedding. This replaces the earlier plan wording requiring every OSS Put after embedding. Before implementation, define a recoverable binding-fence owner/lease: `PurgeObjectFence` currently requires a purge `batch_id` and target `asset_id`, neither exists before new-asset binding; it cannot safely be overloaded with random IDs without an owner type and expired-lease recovery rule.
- **Architect review (2026-08-30, binding fence round 1):** APPROVE WITH REQUIRED PLAN CHANGES. Incorporated: all decisions use `clock_timestamp()`; multi-key acquire/renew/final-bind/purge exclusion is all-or-nothing under the full advisory-lock set; stale token cannot revive/touch successor epochs; chunk-owner protocol preserves existing batched embedding and shared-preview semantics; binding-fence ORM/migration/bootstrap/index contracts and corresponding PostgreSQL competition/crash tests are required. Resubmit before schema or production-path implementation.
- **Progress update (2026-08-30):** Binding-fence schema and `ObjectBindingFenceService` have RED/GREEN coverage for DB-clock complete-set acquire, live-owner exclusion, expired takeover, old-token renew rejection, atomic multikey conflict rollback, and final-bind release (real PostgreSQL lease suite: 3 passed). Synchronous `ingest_one` now supports **explicit** binding-service injection: acquire after keys, preserve OSS Head/Put before embedding, settle read-only transaction/renew before embedding, final-bind wraps `_persist(commit=False)`, and handled failure releases the lease. Asset-ingest + #19 regression: 40 passed. Remaining Task 3 work is purge/binding complete-set mutual exclusion, then batch/queue, import promotion, and cleanup integration; deletion remains hard-disabled.
- **Progress update (2026-08-30, continued):** Purge/binding mutual exclusion is now covered by real PostgreSQL multi-session tests: a live complete binding lease prevents purge fence acquisition, and a live purge fence prevents binding acquire before any OSS I/O. Binding lease suite: **5 passed**. The next unimplemented work remains chunk-owner batch/queue integration, import-promotion and import-cleanup integration; no deletion adapter is enabled.
- **Progress update (2026-08-30, batch boundary):** Before enabling binding-fence injection for `_ingest_batch`/`queue_one`, move lease acquisition out of `_prepare_one` into an explicit orchestration policy. Current synchronous injection is correct, but direct batch reuse would acquire per prepared item while batch still calls `_persist` directly, leaking held epochs and violating the architect-approved chunk-owner all-or-nothing protocol. Required next refactor: make `_prepare_one` accept an explicit binding-acquire policy; synchronous requests select single-owner, batch selects one complete de-duplicated chunk owner then writes/renews/final-binds/release per chunk, queue selects one owner; add the RED tests before enabling these paths. Promotion/cleanup remain unmodified. No deletion adapter is enabled.
- **Progress update (2026-08-30, pure prepare + write-path rollout):** `_prepare_one` is now pure for new/cached/reusable candidates: it downloads/hashes/decides source identity/normalizes and returns source path, location, keys and payload without formal PUT. Sync uses single-owner acquire → formal write → renew → embedding → final bind; batch acquires a complete de-duplicated chunk identity set before any PUT, preserves existing `[20,1]` batch embedding/shared-preview/invalid-vector behavior, binds items under the retained chunk lease, then releases it; queue uses single-owner write/final-bind. Asset-ingest + #19 queue regressions: **27 passed**; explicit chunk-owner test is green. Image-import promotion has an optional `import_promotion` complete-set lease wrapper in `complete_import_item` / `SqlAlchemyImageImportRepository` and worker composition; #19 worker units: **14 passed**. Import cleanup still needs the analogous lease lifecycle; no deletion adapter is enabled.
- **Progress update (2026-08-30, all write paths):** Import cleanup now has an optional `import_cleanup` complete-set lease wrapper and cleanup process composition injects the formal bucket / binding and purge fence services. Real PostgreSQL integration tests prove promotion creates/completes the asset under final-bind and cleanup writes `objects_purged_at` under final-bind; both release their lease. Focused write-path regression is **58 passed** across asset ingest, #19 queue/worker, #22 cleanup, binding/purge mutual exclusion, and promotion/cleanup contracts. Task 4 hard-disable first RED/GREEN is complete: `UnavailableFormalDeletionCapabilitySource` is always false and static tests verify the purge-batch worker has no deletion credentials/adapter/delete call; T13 formal worker returns before even claiming work when unavailable. Task 5 currently has only this hard-gated worker seam; persistent per-item checkpoints, partial-success reduction, retries, irreversible transition and one-year events remain unimplemented.
- **Progress update (2026-08-30, verification checkpoint):** All formal-object write paths are now lease-aware: sync, chunk batch, queue, import promotion, and import cleanup each use their approved owner semantics; PostgreSQL contracts cover binding/purge mutual exclusion and promotion/cleanup final checkpoints. Consolidated scoped run: **67 passed**. Task 4 remains deliberately hard-disabled in all production composition roots. Task 5 is not yet implemented beyond the unavailable-capability worker seam: before any fake-deleter state-machine code, add the missing persisted item-operation repository whose claim must lock `pending_deletion` batch/item plus current asset/manifest identity, and extend tests for manifest membership/retention and item-specific audit events. Do not treat the current tombstone-only `PurgeBatchItem` as sufficient object deletion authority.
- **Progress update (2026-08-30, Task 5 start):** Added persistent per-item authorization snapshot fields (formal original/preview key, backup object IDs/digests, preview deletion authorization, authorization retain-until) with RED/GREEN schema contract. The injected-only fake `FormalPurgeWorker` now has RED/GREEN checkpoint order coverage for original intent/delete, shared preview retention, database finalization, and completion; unavailable capability still prevents any claim or delete call. This is intentionally not production execution: the remaining required work is a PostgreSQL `FormalPurgeRepository` that writes/claims/checkpoints these fields under batch/item/asset locks, manifest and retention revalidation, one-year `PurgeItemEvent` append-only records, item retry/partial failure reduction, plus fake-deleter PostgreSQL integration. Production composition remains hard-disabled.
- **Progress update (2026-08-30, Task 5 vertical slice):** `FormalPurgeRepository` now claims only `pending_deletion`/`deleting` items with complete unexpired per-item authorization snapshots and current archived asset; it writes claim/checkpoint/failure/retry/completion events with one-year retention, does first `original_delete_started` as the atomic `pending_deletion → deleting` boundary, preserves retry checkpoint so a preview failure does not repeat original deletion, transactionally retains shared previews, deletes the test `ImageAsset` row/vector only after an authorized terminal checkpoint, and reduces all-complete to `completed` or nonretryable failures to `partial_failure`. PostgreSQL fake-deleter tests cover successful finalization/vector-row absence, shared preview retention, partial preview failure/retry idempotency, manifest-validator rejection, and partial-failure reduction. Consolidated scoped regression: **98 passed**. Important hard boundary: T13 production `purge-batch-worker` still does not import this repository/deleter, does not read delete credentials, and cannot delete an OSS object; manifest membership/revalidation is an injected validator seam pending a future T14-authorized composition of canonical local manifest/copy verification. Risk reviewer must inspect the complete diff before any claim of ticket completion.
- **Progress update (2026-08-30, Tasks 6–8):** Task 6 DTO/UI is complete: batch DTO provides safe completed/failed/pending counts, cancellable state and item next action; UI renders them without adding a deletion control plane. Task 7 PostgreSQL integration contracts now cover item claim/first intent, authorized fake-deleter finalization/vector row absence, shared previews, partial failure/retry idempotency, manifest-validator refusal, binding/purge concurrency and write-path final checkpoints. Task 8 static/operations facts are complete: AGENTS.md and runbook distinguish the test seam from the production hard-disable/T14 boundary. Final allowed regression: backend **106 passed**; frontend targeted tests **43 passed** and production build passed. `git diff --check` passed. Complete diff is awaiting required risk review; do not claim ticket complete before that review.
- **Risk review (2026-08-30): REJECT / blocking repairs required.** The production hard-disable passed independent review, but the fake formal-purge seam is not a safe formal-deletion implementation. Required redesign/repairs before further Task 5 work: (1) acquire/hold `PurgeObjectFence` across current-reference recheck and each delete; original requires exclusive-reference recheck; (2) inject binding/purge fencing into every production ingest factory/migration without breaking caller-owned `commit=False` product transactions; (3) replace manifest validator default-allow with mandatory canonical manifest/copy/retention verifier and lock-compare all asset keys/hashes to persisted authorization snapshot; (4) claim CAS/lease result must gate every external call, reclaim expired `in_progress`, and reject stale workers; (5) migrate #26 `queued` item state plus populate per-item authorization snapshots at verifying→pending_deletion; (6) make cancel vs first intent one locked atomic race and align DTO/API; (7) add formal-purge multi-session tests for these cases and actual `VectorSearchService` invisibility. Do not claim Task 5/6/7 complete; T13 production remains hard-disabled while this repair design is re-reviewed.
- **Architect repair review (2026-08-30): APPROVE WITH REQUIRED PLAN CHANGES.** Before any Task 5 repair code, replace the fake seam with: mandatory fail-closed canonical manifest/copy/retention verifier; `verifying → pending_deletion` single-transaction item authorization snapshot promotion; DB `clock_timestamp()` item leases with expired `in_progress` takeover/generation CAS; one `authorize_delete_call()` gate whose successful transaction owns complete original/preview advisory locks and held deletion fences through object/database completion; original exclusive and preview shared/protected rechecks under those fences; cancel/first-intent competition on the same locked batch row; and caller-owned transaction compatible binding-fence APIs for all HTTP/Kodo factories. Existing snapshotless `pending_deletion` batches fail closed. T13 worker composition must remain hard-disabled. Required multi-session PostgreSQL tests are listed in the architect review message for this turn; re-submit full diff to risk review only after those repairs. Current fake repository/worker is test-only scaffolding and must not be treated as formal deletion authority.
- **Repair progress (2026-08-30):** Repair step 1 RED/GREEN complete: `FormalPurgeRepository` now requires an explicit canonical manifest verifier and has no default-allow behavior; missing verifier construction fails closed. Existing fake-deleter integration tests now supply explicit test verifiers (9 related unit/integration tests passed). Remaining architect repairs are not started: #26 verified-to-pending snapshot promotion, complete-set deletion fences and `authorize_delete_call`, DB-clock item lease takeover/CAS, cancel-intent race, caller-owned transaction binding APIs, and required multi-session formal-purge tests.
- **Repair progress (2026-08-30, step 2):** Added RED/GREEN `advance_verified_to_pending_if_current()`: it locks a `verifying` batch/items/assets, rejects mismatched manifest digest, incomplete authorization map, absent/non-archived target, or current asset key mismatch; only then atomically writes per-item snapshot scalars, `queued → pending`, and batch `pending_deletion`. Missing legacy snapshots therefore fail closed. Focused promotion contract passed. Remaining repair steps: canonical verifier implementation, complete-set deletion fences/authorize-delete-call, DB-clock item lease takeover/CAS, cancel race, caller-owned transaction factory seam, and multi-session formal-purge tests.
- **Repair progress (2026-08-30, step 3):** Added RED/GREEN `CanonicalFormalPurgeAuthorizationVerifier`: missing/expired retention, manifest digest/batch mismatch, copy verification failure, and original/preview snapshot key/backup/SHA mismatch all fail closed. T13 remains uncomposed. Remaining repair steps: bind this verifier to the future T14-only canonical local manifest reader, complete-set deletion fences/authorize-delete-call, DB-clock item lease takeover/CAS, cancel race, caller-owned transaction factory seam, and multi-session formal-purge tests.
- **Repair progress (2026-08-30, step 4 start):** Added a fail-closed `authorize_delete_call()` RED/GREEN contract: without a claim and verified complete-set authorization it returns no call token. It is deliberately not connected to a deleter and does not yet acquire fences/leases; the next repair must replace the placeholder with the architect-required single transaction holding complete original/preview locks, purge fences, verifier observation, item lease/CAS, and current reference recheck through intent persistence.
- **Repair progress (2026-08-30, step 4 contract):** Extended authorization RED/GREEN contracts to reject a partial original-only fence set. The production implementation remains fail-closed placeholder. Resume by implementing the complete-set transaction in `FormalPurgeRepository` with real PostgreSQL tests before changing worker deleter calls; then add expired in-progress takeover/stale-call tests, cancel/intent race, caller-owned factory interface, and formal-purge multi-session/vector tests.
- **Repair progress (2026-08-30, step 4 prerequisite):** Added per-item `formal_bucket` authorization snapshot with RED/GREEN schema contract; complete-set purge fences can no longer infer bucket from source provenance. `advance_verified_to_pending_if_current()` must populate this field from canonical manifest before it can safely promote a batch. Complete authorize-delete transaction remains unimplemented/fail-closed.
- **Repair progress (2026-08-30, step 4 authorization):** `authorize_delete_call()` now has PostgreSQL RED/GREEN coverage for a valid first original intent: in one transaction it validates item token/generation/checkpoint/DB-clock lease, deleting batch, archived current asset identity, verifier, canonical complete original/preview advisory locks, and creates/reuses the two held `PurgeObjectFence` rows before renewing the item lease and returning call authorization. Every mismatch returns no authorization. T13 worker still does not call it. Remaining: hold/recheck fences through every object/database transition, original/preview reference rechecks, expired `in_progress` takeover, cancel race, caller-owned factory seam and multi-session tests.
- **Repair progress (2026-08-30, deleter gate):** `FormalPurgeWorker` now requires both checkpoint CAS success and `authorize_delete_call()` success before each injected deleter call; authorization rejection contract proves zero calls. T13 remains uncomposed. Remaining architect repairs unchanged: fence/recheck lifetime, DB-clock takeover, cancel race, caller-owned factory interface and multi-session coverage.
- **Repair progress (2026-08-30, DB-clock claim):** Claim selection now uses PostgreSQL `clock_timestamp()` and includes expired `in_progress` items; a reclaim increments generation and replaces token (real PostgreSQL RED/GREEN contract passed). Remaining: enforce DB-clock lease in every checkpoint/fail/finalize path, complete fence/recheck lifetime, cancel race, caller-owned factory interface and multi-session coverage.
- **Repair progress (2026-08-31, caller-owned CAS):** `ObjectBindingFenceService.finalize_in_transaction()` now performs caller-session complete-set `FOR UPDATE` CAS on id/token/generation/held/DB-clock lease without beginning, committing or rolling back. Real PostgreSQL four-branch integration passed: generation mismatch, released, and successor owner reject with zero callback; successful outer commit releases fences; outer rollback preserves held fences. Factory integration remains pending until a control-session factory is threaded through caller-owned `commit=False` requests.
- **Repair progress (2026-08-31, ingest caller-owned branch):** 双路径改造第二步完成（条件分支，非重构）。`ingest_one` 在 `control_session_factory` 为 None 时现有路径逐行不变；factory 已注入且 `commit=False` 时走新增 `_ingest_one_caller_owned`：pure prepare → `acquire_prewrite`（独立 control session）→ 正式对象写入 → embedding → `renew_prewrite`（control session）→ `finalize_in_transaction(db.session, bind)` 把绑定写入调用方事务；全程不调用 `_settle_binding_session()`、不提交也不回滚调用方 session，围栏自带 session 零触碰（毒化 session 测试证明）。lease 经新增默认字段 `AssetIngestResult.binding_lease`（旧路径恒为 None）交还调用方；`abort_after_outer_rollback` 改为经 factory 新建独立 control session 驱动（`ObjectBindingFenceService.abort_after_rollback` 新增可选 `control_session_factory` kwarg）。失败语义：finalize 之前的失败由服务经 control session 释放围栏、对象为重试保留；finalize 一旦开始（调用方事务可能已持围栏行锁），服务不越权释放，由调用方回滚后 abort 或租约到期接管。真实 PostgreSQL + 伪 OSS 集成 4 用例转绿（`test_issue_27_ingest_outer_rollback.py` 重写，含：外层提交才释放；回滚后围栏保留至 control abort；embedding 失败无 held 残留且不动调用方事务；None factory 不产生 lease）。回归：ingest 集成基准 24 全绿、#27 fence/lease/promotion/cleanup 全绿、单元 **506 passed**、集成 **184 passed**；余留失败：6 个 `test_kodo_oss_migration.py` 与 2 个 `test_kodo_preflight.py` 用例因测试写死 `2026-08-02` 报告时间戳超出 24 小时新鲜度窗口（日历炸弹，另行处理）；另 3 个 `formal_purge` 失败初判为在途 WIP，经用户指正复核为真实缺陷，由下一条修复记录取代。多图共享 preview 的 chunk 语义（`ingest_many`/`queue_one`/Product 循环）按裁决本轮不处理；删除能力仍硬性关闭。
- **Repair progress (2026-08-31, authorize fixes):** 修复上条误判为 WIP 的 3 个真实 `formal_purge.py` 缺陷（红→绿，测试断言零改动）：① `authorize_delete_call` 现于任何 session 事务操作之前拒绝声明的不完整围栏集（`verified_authorization.fence_ids` 去重后必须恰为两把有效 id；缺省声明仍由事务内派生），部分集不再落入 `with session.begin()`；② 原图独占复核的 import 引用谓词改为 NULL 安全：`asset_id` 为 NULL 的未清除引用（如 queued 未提升项）过去被 `!=` 三值逻辑漏放，现与预览复核同构（排除 completed 且 asset_id 为目标的自身提升项）；③ `preview_is_shared` 的返回值改为在 `rollback()` 前完全求值——rollback expire ORM 实例后再触碰属性会 autobegin 新事务并泄漏给下一调用，导致“重试自 preview_delete_started 恢复、中间无 checkpoint 提交”路径上 `authorize` 的 `session.begin()` 抛 `InvalidRequestError` 并被 except 静默成拒绝、重试永远无法完成资产行删除。验证：目标 3 用例与 `test_issue_27_formal_purge_repository.py`/`test_issue_27_delete_authorization_unit.py`/`test_issue_27_formal_purge_unit.py` 合计 **20 passed**；ingest 系列 + #27 fence 系列定向 **48 passed**；全量单元 **507 passed**（余 2 失败仍为上条所述 kodo 日历炸弹）；全量集成 **186 passed**（余 6 失败同上）。本条修复未触碰任何测试文件；删除能力仍硬性关闭，未 commit、未 push、未动 Issues。
- **Repair progress (2026-08-31, kodo report freshness):** 经用户授权修复 kodo 8 个日历炸弹失败（2 单元 + 6 集成）。根因：`test_kodo_preflight.py`/`test_kodo_oss_migration.py` 两处 `_write_full_authorization` helper 把 preflight/dry-run 报告的 `generated_at` 写死为 `2026-08-02`，而 `services/kodo_migration.py` 的全量授权门要求两份报告在过去 24 小时内按序生成（preflight 不晚于 dry-run）。修复只在测试侧：helper 改为相对当前时间生成时间戳（preflight/dry-run 各 now−30/−15 分钟），满足新鲜度与顺序校验；`MAX_FULL_AUTHORIZATION_REPORT_AGE` 生产门禁与任何断言语义未改动，两文件不存在故意使用过期时间戳的拒绝用例。`test_kodo_preflight.py` **40 passed**、`test_kodo_oss_migration.py` **8 passed**。全套首次全绿：单元 **509 passed**、集成 **192 passed / 1 skipped**。
- **Repair progress (2026-08-31, chunk-owner control factory):** 双路径第三步——`ingest_many`/`queue_one` 接入 control-factory chunk-owner 语义（RED/GREEN）。`_ingest_batch`/`queue_one` 顶部条件分支：`control_session_factory` 为 None 时现有实现一行不动；非 None 时进入新增 `_ingest_batch_with_control_lease`/`_queue_one_with_control_lease`——整批一次 `acquire_prewrite` 完整去重 identity 集（独立 control session）、写对象与单次 `embed_normalized_images` 保持既有 `[20,1]`/代表去重/无效向量隔离语义、embedding 后 `renew_prewrite`、逐 item `finalize_in_transaction(db.session, bind=_persist(commit=False)) + 提交`（`ObjectBindingFenceService.sublease` 子租约视图共享父 token/generation）：独占 original 随 item 提交原子释放，共享 preview 只在最后一个 consumer 的绑定中释放；批末剩余围栏经 control session 以 `failed` 清扫，失败 item 的 OSS 对象为重试保留不删除；围栏自带 session 在 control 模式全程零触碰（毒化 session 集成测试证明）。新增 `test_issue_27_ingest_batch_queue_control.py` 3 用例：同内容双路径共享 preview 单 PUT + 5 围栏全 released/completed、坏向量 item 隔离 + 4 围栏 2/2 completed/failed 无 held、queue_one 绑定 item 行且 2 围栏 completed。定向回归 **84 passed**（ingest/#19/#21/import 资产/全部 #27 集成），单元 **509 passed**。Product 多图循环与生产工厂注入见下一条；删除能力仍硬性关闭。
- **Repair progress (2026-08-31, production factory injection + HTTP boundary):** 双路径第四、五步（用户授权范围）。新增 `services/fence_composition.py`：`binding_fence_kwargs(values)` 按显式 `INGEST_BINDING_FENCE_ENABLED` 开关返回 `formal_bucket`（取 `OSS_BUCKET_NAME`，缺失即抛错不静默降级）+ `ObjectBindingFenceService(db.session, purge_fence_service=PurgeObjectFenceService(db.session))`（组合与 import worker/cleanup 同构）+ `control_session_factory`（每次调用新建独立 session）；未启用返回空 kwargs，行为与今日一致。`caller_owned_ingest_boundary(ingest_service)` contextmanager 统一“先 rollback 调用方事务、再经 control session 逐个 abort leases”的全有或全无边界，legacy 配置下 leases 恒空、边界无副作用。注入点：`products_v2.get_asset_ingest_service`（POST/PUT 两个多图循环改走边界、收集 `result.binding_lease`）、`image_imports._get_ingest_service`（队列端点本就是 commit=False 循环——`ImageImportQueueResult` 新增尾部默认字段 `binding_lease`，控制队列变体经 `replace()` 携带）、`image_assets._import_ingest_service`（`ingest_many` 直接命中步骤1控制 chunk-owner 变体）、`migrate_kodo_to_oss.ingest_service_factory`（`binding_fence_kwargs(environment)`，operator env 显式启用）。新集成 `test_issue_27_product_ingest_boundary.py` 5 用例（concurrent_app + test_client + 伪 OSS）：Product 双图成功 → 4 围栏随外层 commit 原子 completed；第二图 embedding 失败 → 503、Product/资产不提交、第一图 rollback 后回 held 再由边界 abort 为 failed、第二图 finalize 前服务自 abort、零 held、对象保留不删；队列双图第二图损坏 → 400、任务行不提交、第一图 2 围栏 failed；队列成功 2 任务 4 completed；显式关闭开关 → 零围栏行（旧行为）。全量回归：单元 **509 passed**、集成 **200 passed / 1 skipped**（含步骤1的 3 用例与本步 5 用例）。启用开关属部署决策，未改 docker-compose/.env；删除能力仍硬性关闭。
- **Repair progress (2026-08-31, formal-purge multisession coverage):** 双路径第六步。新增 `test_issue_27_formal_purge_multisession.py` 4 用例（真实 PostgreSQL 多会话 + 伪 deleter，concurrent_app 临时 schema）：① 活绑定租约阻塞 `authorize_delete_call`、绑定释放后授权成功——暴露并修复真实缺口：purge 授权事务此前从不复核绑定围栏；② 过期 in_progress 被第二会话接管（gen+1/换 token），旧 claim 的 checkpoint/authorize 全拒绝且 deleter 零调用，接管 worker 恰一次合法完成删除，接管后旧 claim 复授权仍 None；③ finalize 提交失败模拟——事务回滚后资产仍 archived、claim 未变、两把删除围栏仍 held，同 claim 重试 finalize 成功并释放；④ 删除完成后 `VectorSearchService.search_by_vector` 双次检索均永久无该资产行，观察会话确认行不存在。生产修复（`formal_purge.py`）：`authorize_delete_call` 拆为结构前置校验 + `_authorize_delete_call_locked` 事务体 + 外层按结果显式 commit/rollback——不再用 `with session.begin()`（worker 序列在两次仓库调用间可能因过期属性刷新 autobegin，begin 抛 InvalidRequestError 被吞成授权拒绝）；咨询锁获取后、purge fence 创建前调用 `ObjectBindingFenceService.assert_purge_available` 完成绑定↔删除双向互斥闭环（活租约抛 `BindingFenceHeld`→fail-closed None；过期 epoch 由 assert 内回收）。拒绝路径显式 rollback 及时归还行锁。定向回归 **29 passed**（formal_purge 仓储/多会话/cancel-race/#26 worker/授权单元/删除隔离），单元全量 **509 passed**。
- **Repair progress (2026-08-31, F1 request-level chunk fix):** risk_reviewer 首轮复审 REJECT，唯一阻塞项 F1：启用围栏后同请求同内容多图走逐图 caller-owned `ingest_one` 时，图 1 finalize 在调用方事务内持共享 preview 围栏行锁未提交，图 2 的 control `acquire_prewrite` 等该行锁 → 跨连接自死锁（fail-closed、无数据损坏，但确定性挂死且重试不自愈）。按 architect chunk-owner 协议修正（本应如此解读“Product 多图循环按 chunk-owner 协议接入”）：新增服务入口 `ingest_many_caller_owned`/`queue_many_caller_owned`——整请求一次去重 identity `acquire_prewrite`、批量 embed（代表去重单次 `embed_normalized_images`）、`renew_prewrite`、逐 item `sublease` finalize 绑定进调用方事务（行锁同事务可重入，无自锁），preview 最后 consumer 释放，不 commit，返回 `(results, lease)`；未注入 control factory 时逐图委托旧 `ingest_one/queue_one(commit=False)`（行为与旧一致）。products_v2 POST/PUT 与 image_imports 队列循环切换到新入口（边界收集单 lease）。F2（finalize 已开始后失败的 300s 挂窗）同修：服务把租约挂 `exc.binding_fence_lease`，`caller_owned_ingest_boundary` 回滚后统一回收；新增服务级测试证明回收后零 held。F1 回归测试：服务级双同内容图（commit 前 observer 3 held、commit 后 3 completed、preview 单 PUT）+ HTTP 层 Product/队列同内容双图 201/成功。#19 静态合同按批准架构演进更新（持久队列路由不变量同时接受 `queue_one` 与 `queue_many_caller_owned` 入口，意图注释保留，非弱化）。F3（purge 围栏在 partial_failure/deleting-过期终态永久 held）、F4（声明 fence_ids 不与事务内派生核对）、F5（T14 启用前须确保全部 HTTP 入口开启 INGEST_BINDING_FENCE_ENABLED）为 T14 启用前置条件，本轮不改代码、记录于此。回归：单元 **509 passed**、围栏相关集成 **44 passed**、全量集成 **209 passed / 1 skipped**。删除能力仍硬性关闭。
- **Risk review (2026-08-31, F1 修复轮): APPROVE WITH FINDINGS。** risk_reviewer 聚焦复核确认：F1 在新协议下**结构性根除**（每请求仅 finalize 之前一次 control acquire，自锁必要条件不存在；同内容双图/早退混合/三图两两同内容/跨请求咨询锁交错逐一核验无死锁）；F2 修复有效（挂异常租约 → 边界回滚后回收，fence 全 failed 零 held）；工厂 None 委托与旧行为严格一致；#19 合同 OR 改动保持持久队列路由意图（硬断言未动）。新发现三条均 INFO 不阻塞：N1 输入路径须互异（已在 `ingest_many_caller_owned` docstring 明示；queue 版天然对重复安全）；N2 缺 HTTP 层“已提交 existing + 新图混合 + 围栏开启”专项用例（代码层已证安全，后续可补）；N3 pre-finalize abort 自身抛错时退化为租约到期自愈（有界设计，记录备查）。F3/F4/F5 维持 T14 启用前置跟踪。
- **Progress:** Architect 已于 2026-08-29 第四轮批准。Task 1 完成首轮 RED/GREEN：`backend/test/test_issue_27_schema_static_contract.py -v` 首次 RED 为 1 failed / 1 passed（缺少 `deleting`），随后为 2 passed；与 #26 schema 合同合跑为 6 passed。模型已声明 #27 批次状态和单调检查点常量。**Task 2 完成**：迁移、首次初始化 SQL、ORM 均补齐 batch/item 删除检查点、item lease、fence epoch 与一年事件字段；ORM 含 held-fence partial unique index。新增真实 PostgreSQL 临时 schema 测试，确认同键两条 released epoch 可保留、第二条 held epoch 被唯一约束拒绝。最终 schema 回归为 #26/#27 合同加集成测试 **10 passed**（全局 pytest-asyncio 配置仍输出既有弃用警告）。2026-08-30 用户裁决保留 #19/#25 对象先写/embedding 后绑定语义；architect 已批准独立 `object_binding_fences` lease 协议。Task 2 binding-fence schema 完成 RED/GREEN：新增 ORM、显式迁移、首次初始化 SQL 的 owner kind/state/release/lease CHECK、held partial unique 和 owner/identity-expiry 索引，`test_binding_fence_schema_has_leased_owner_contract` 通过；未执行迁移。Task 3 已完成 `FormalBucketIdentityProvider`、`PurgeObjectFenceService` 的 canonical advisory lock/clearance fence/reference scan 与 Put 前 held deletion-fence seam。`ObjectBindingFenceService` 第一轮 RED/GREEN 已完成：PostgreSQL `clock_timestamp()` complete-set acquire、live-owner rejection、expired takeover、old-token renew rejection、atomic multikey conflict rollback、final-bind ownership verification and release。真实多 session lease suite **3 passed**。**下一步**：用该 service 驱动同步入口的“acquire → OSS write → renew → embedding → final-bind”时序，再扩展 batch/queue/promotion/cleanup；删除能力仍硬性关闭，未执行真实 OSS/数据库写入、服务启动或 Git/GitHub 操作。
