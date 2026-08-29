# Issue #26 Purge Batch Pipeline Implementation Plan

> **Execution:** The user explicitly selected Matt `$implement`. Execute this ticket in the current protected worktree, task by task, with the pre-agreed TDD seams. Do not commit, push, create a PR, change GitHub state, or create a linked worktree without a separate user authorization.

**Goal:** Build the authenticated, cancellable, persistent backup-and-verification pipeline for archived image assets, stopping safely at `pending_deletion` with no formal-object, asset-row, vector, backup, or orphan deletion.

**Architecture:** Flask owns a no-secret `PurgeBatchControlService` for API/state control and reads a short-lived worker capability evidence file; the dedicated `PurgeBatchWorker` alone loads `.env.backup` and composes PostgreSQL recovery-point, production reference-snapshot, and object-backup adapters. `PurgeBatch`/`PurgeBatchItem` persist complete batch/item outcomes, with tombstone asset IDs rather than an asset FK; the worker commits each claim before an external boundary and uses row locks plus token/generation/status CAS around every result.

**Tech Stack:** Flask, Flask-SQLAlchemy/SQLAlchemy, PostgreSQL 16 + pgvector, React 18 + TypeScript + Ant Design, Docker Compose, pytest, Vitest.

## Global Constraints

- **Workspace:** the current worktree is deliberately dirty with #26 WIP. Preserve all listed and unrelated user changes; work directly in it and never reset, checkout, stash, or create a linked worktree as part of this ticket.
- **No destructive capability:** #26 terminates at `pending_deletion`; do not import, instantiate, call, or add an adapter for formal-object deletion, `session.delete`, Kodo, cleanup, or backup-object deletion.
- **Credential isolation:** only the new `purge-batch-worker` loads `backend/.env.backup` and its `BACKUP_OSS_*`, `PURGE_SOURCE_OSS_*`, and `PURGE_RESTORE_OSS_*` values. Flask/Gunicorn, the existing image worker, and cleanup never receive that file or those variables.
- **Capability evidence:** worker writes `purge_batch_worker.json` into a compose shared evidence volume; worker is the only writer, Flask mounts it read-only. TTL is **120 seconds**, heartbeat is **30 seconds**, and the five safety-gate condition windows remain independent; a valid capability cannot make an expired safety gate ready.
- **Evidence hygiene:** capability payload uses only `schema_version`, `component`, `result`, `verified_at`, `expires_at`, `policy`, and `summary`; reject recursively any key containing `password`, `secret`, `token`, `authorization`, or `dsn`. Never store header values, idempotency keys, request bodies, signed URLs, object keys, absolute paths, bytes, vectors, or credentials in public DTOs/audit records.
- **Snapshot freshness:** use `PURGE_REFERENCE_SNAPSHOT_MAX_AGE_SECONDS=60`; `PurgeObjectBackupConfig.reference_snapshot_max_age_seconds` receives this value in the worker only. A snapshot that is stale, future-dated, changed, or incomplete is a safe failure.
- **Retention:** `purge-<batch_id>` is the only restore-point ID for a batch. On expired/missing restore-point or object-copy retention evidence, write `PURGE_BACKUP_RETENTION_EXPIRED`; that batch cannot resume. The only new attempt is cancel → new batch ID → new Idempotency-Key → new confirmation. #27 must recheck retention before deletion.
- **Database identity:** before the worker claims any batch, its queue database and the `BACKUP_DB` source used by `PostgresBackupService` must prove the same current database/system identifier. A mismatch or unavailable proof writes only a failed capability heartbeat and performs no batch write or claim.
- **Durable worker state:** backup roots, object plans and manifests needed for reconciliation are on a worker-only persistent volume. They are never stored in public DTOs/audit records and are never cleaned by #26.
- **T9 write order:** create is `authenticate → Idempotency-Key header syntax → require_ready → pipeline_available → fourth-step control service`; cancel/retry remain `authenticate → batch_id syntax → require_ready → pipeline_available → fourth-step control service`. The pre-gate create check does not read DB, lock rows, or parse JSON.
- **Stable error codes:** `INVALID_PURGE_IDEMPOTENCY_KEY`, `PURGE_IDEMPOTENCY_CONFLICT`, `PURGE_ASSET_IN_ACTIVE_BATCH`, `PURGE_BATCH_NOT_CANCELLABLE`, `PURGE_BATCH_NOT_RETRYABLE`, `PURGE_PIPELINE_UNAVAILABLE`, `PURGE_ASSET_RESTORE_BLOCKED`, `PURGE_GATE_NOT_READY`, `PURGE_BACKUP_RETENTION_EXPIRED`, plus `PURGE_DATABASE_BACKUP_FAILED`, `PURGE_OBJECT_BACKUP_FAILED`, `PURGE_OBJECT_VERIFICATION_FAILED`, `PURGE_REFERENCE_SNAPSHOT_INVALID`, and existing T9 auth/ID/control codes. Do not rename them during implementation.
- **Verification discipline:** run only scoped tests while implementing, then allowed backend/frontend suites. Real integration tests use local `image_search_test`; a skipped integration test is reported as skipped, never as passed. Do not run `test/test.py`, `test/test_pgvector.py`, or `test/benchmark_search.py`.
- **Authorization boundary:** no commit, push, merge, deployment, migration execution against shared data, real cloud write, or GitHub status change is authorized by this plan.

---

## Execution preflight (after architect-required plan corrections; do not perform during planning)

1. Record `git status --short` and verify the known WIP remains present; do not alter host-owned secret files or print their contents.
2. Confirm the baseline is `2e81595`, then run the already-present WIP contract `cd backend && python -m pytest test/test_issue_26_worker_static_contract.py -v` and record its result. A failure in this pre-existing WIP test stops implementation; Task 1's newly created schema test is deliberately expected to be RED at its own TDD step and is not a preflight gate.
3. Flask commands must never load `backend/.env.backup`; local worker-only tests may use its already-existing host file without printing it, and it must never be staged.

## File structure

| Path | Responsibility |
| --- | --- |
| `backend/models/purge_batch.py` | `PurgeBatch` and `PurgeBatchItem` ORM schema, status constants, safe row serialization. |
| `backend/migrations/issue_26_purge_batches.py` | Explicit, idempotent PostgreSQL-only creation of batch tables/constraints/indexes. |
| `backend/services/purge_batch_control.py` | Flask-safe create/replay/cancel/retry/list/detail/claim repository and state transitions; imports no ops adapters. |
| `backend/services/purge_pipeline_capability.py` | Bounded, sensitive-key-safe capability evidence reader/writer and unavailable source. |
| `backend/services/postgres_reference_snapshot.py` | Production `ReferenceSnapshotReader` over `image_assets` and `image_import_items`. |
| `backend/services/purge_batch_worker.py` | Worker-only orchestration of backup, object copy, revalidation, state CAS, and audit. |
| `backend/scripts/run_purge_batch_worker.py` | SIGTERM-aware loop, ops-only adapter construction, capability preflight/heartbeat. |
| `backend/blueprints/admin_purge.py` | T9-preserving HTTP ordering and DTO/error mapping. |
| `backend/services/asset_recycle_bin.py` | Restore-side asset lock and active-batch exclusion. |
| `backend/Dockerfile`, `docker-compose.yml` | Separate worker image target and restricted compose service/volume. |
| `backend/test/test_issue_26_*.py`, `backend/test/integration/test_issue_26_*.py` | Unit, static-contract, worker, snapshot, and real-PostgreSQL behavior tests. |
| `frontend/src/types/product.ts`, `frontend/src/services/productApi.ts`, `frontend/src/components/ArchivedAssetGrid.tsx` | Batch DTO/API and recycle-bin administration UI. |
| `AGENTS.md`, `docs/operations/purge-batch-pipeline-runbook.md` | Updated runtime facts and manual-only recovery/retention ledger instructions. |

### Task 1: Persistent schema, explicit migration, and model contract

**Files:**
- Create: `backend/models/purge_batch.py`
- Create: `backend/migrations/issue_26_purge_batches.py`
- Create: `backend/test/test_issue_26_schema_static_contract.py`
- Modify: `backend/models/__init__.py`
- Modify: `backend/init_db.py`
- Modify: `postgres/init/01_init.sql`

**Interfaces:**
- Produces `PurgeBatch`, `PurgeBatchItem`, `PURGE_BATCH_STATUSES`, `CLAIMABLE_BATCH_STATUSES`, and `PurgeBatch.to_public_dict()`.
- Produces migration `apply_migration(connection)` with no application-start invocation.

- [x] **Step 1: Write failing schema tests**

```python
def test_purge_batch_item_schema_has_restrict_asset_fk_and_no_jsonb():
    source = _read('migrations/issue_26_purge_batches.py').lower()
    assert 'target_asset_id uuid not null' in source
    assert 'references image_assets' not in source
    assert 'jsonb' not in source
    assert "status in ('queued', 'database_backup', 'object_backup', 'verifying', 'pending_deletion', 'failed', 'cancelled')" in source

def test_orm_models_create_on_sqlite_and_keep_unique_idempotency_pair():
    engine = create_engine('sqlite://')
    db.metadata.create_all(engine)
    assert _unique_columns(PurgeBatch.__table__) == {'actor_id', 'idempotency_key'}
```

- [x] **Step 2: Run the schema tests and confirm the red state**

Run: `cd backend && python -m pytest test/test_issue_26_schema_static_contract.py -v`

Expected: FAIL because the model and migration do not exist.

- [x] **Step 3: Implement the minimal schema**

```python
class PurgeBatch(db.Model):
    __tablename__ = 'purge_batches'
    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_id = db.Column(db.String(128), nullable=False)
    idempotency_key = db.Column(db.String(128), nullable=False)
    request_fingerprint_sha256 = db.Column(db.String(64), nullable=False)
    confirmation_text = db.Column(db.String(64), nullable=False)
    status = db.Column(db.String(24), nullable=False, default='queued')
    claim_token = db.Column(Uuid(as_uuid=True))
    claim_generation = db.Column(db.BigInteger, nullable=False, default=0)
    claimed_by = db.Column(db.String(128))
    lease_expires_at = db.Column(db.DateTime)
    database_backup_id = db.Column(db.String(160))
    database_manifest_sha256 = db.Column(db.String(64))
    object_manifest_sha256 = db.Column(db.String(64))
    retain_until = db.Column(db.DateTime)
    error_code = db.Column(db.String(80))
    created_at = db.Column(db.DateTime, nullable=False)
    started_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    failed_at = db.Column(db.DateTime)
    cancelled_at = db.Column(db.DateTime)
    __table_args__ = (
        db.UniqueConstraint('actor_id', 'idempotency_key', name='uq_purge_batches_actor_key'),
        db.CheckConstraint("status IN ('queued', 'database_backup', 'object_backup', 'verifying', 'pending_deletion', 'failed', 'cancelled')" , name='ck_purge_batches_status'),
        db.CheckConstraint('claim_generation >= 0', name='ck_purge_batches_claim_generation'),
    )

class PurgeBatchItem(db.Model):
    __tablename__ = 'purge_batch_items'
    batch_id = db.Column(Uuid(as_uuid=True), db.ForeignKey('purge_batches.id', ondelete='CASCADE'), primary_key=True)
    # Deliberate tombstone identity: #27 may delete the asset row without deleting audit history.
    target_asset_id = db.Column(Uuid(as_uuid=True), primary_key=True)
    ordinal = db.Column(db.SmallInteger, nullable=False)
    status = db.Column(db.String(24), nullable=False, default='queued')
    result_code = db.Column(db.String(80))
    error_code = db.Column(db.String(80))
    checkpoint_at = db.Column(db.DateTime)
```

Use scalar `String`, `Text`, `DateTime`, `Integer`, `BigInteger`, and `Uuid` only; store safe evidence identifiers/digests in dedicated scalar columns, not a mutable manifest copy. `PurgeBatchItem.target_asset_id` is a deliberately FK-free immutable tombstone identifier; #27 must not remove batch history to remove an asset. Mirror every field/constraint/index in `MIGRATION_STATEMENTS` and `01_init.sql`; import models in `models/__init__.py` so `db.metadata.create_all()` sees them. Test `to_public_dict()` exposes only status, allowed timestamps, safe codes and item summaries—not keys, paths, manifest payloads or credentials.

- [x] **Step 4: Run the focused schema checks**

Run: `cd backend && python -m pytest test/test_issue_26_schema_static_contract.py -v`

Expected: PASS; no database migration is executed.

### Task 2: Flask-safe control service and deterministic create/replay semantics

**Files:**
- Create: `backend/services/purge_batch_control.py`
- Create: `backend/test/test_issue_26_control_unit.py`
- Modify: `backend/models/purge_batch.py`

**Interfaces:**
- Consumes `PurgeBatch`, `PurgeBatchItem`, `ImageAsset`, `AssetActivityRecord`, and a SQLAlchemy session.
- Produces `IdempotencyKeyError`, `IdempotencyConflictError`, `PurgeBatchStateError`, `PurgeBatchControlService.create_or_replay`, `cancel`, `retry`, `list_batches`, `get_batch`, and `claim_next`.
- Must import no `postgres_backup`, `purge_object_backup`, `purge_object_storage`, `purge_object_restore`, or ops env values.

- [x] **Step 1: Write failing control-service tests**

```python
def test_same_actor_key_and_fingerprint_replays_current_cancelled_batch(session):
    first = service.create_or_replay(actor_id='admin', key='key.1234567', asset_ids=(asset.id,), confirmation='永久删除 1 张')
    service.cancel(first.batch_id, actor_id='admin')
    replay = service.create_or_replay(actor_id='admin', key='key.1234567', asset_ids=(asset.id,), confirmation='永久删除 1 张')
    assert replay.replayed is True
    assert replay.batch.status == 'cancelled'

def test_same_key_different_fingerprint_raises_stable_conflict(session):
    service.create_or_replay(actor_id='admin', key='key.1234567', asset_ids=(a.id,), confirmation='永久删除 1 张')
    with pytest.raises(IdempotencyConflictError) as caught:
        service.create_or_replay(actor_id='admin', key='key.1234567', asset_ids=(b.id,), confirmation='永久删除 1 张')
    assert caught.value.error_code == 'PURGE_IDEMPOTENCY_CONFLICT'

def test_concurrent_different_keys_cannot_claim_the_same_asset(two_sessions):
    first = service_for(two_sessions.first).create_or_replay(
        actor_id='admin-a', idempotency_key='key.first.1', asset_ids=(asset.id,), confirmation='永久删除 1 张')
    with pytest.raises(PurgeBatchStateError, match='PURGE_ASSET_IN_ACTIVE_BATCH'):
        service_for(two_sessions.second).create_or_replay(
            actor_id='admin-b', idempotency_key='key.second.1', asset_ids=(asset.id,), confirmation='永久删除 1 张')
    assert first.batch.id == only_batch_item_for(asset.id).batch_id

def test_same_actor_key_unique_race_reloads_and_replays(two_sessions):
    first = service_for(two_sessions.first).create_or_replay(
        actor_id='admin', idempotency_key='key.race.01', asset_ids=(asset.id,), confirmation='永久删除 1 张')
    replay = service_for(two_sessions.second).create_or_replay(
        actor_id='admin', idempotency_key='key.race.01', asset_ids=(asset.id,), confirmation='永久删除 1 张')
    assert replay.replayed is True
    assert replay.batch.id == first.batch.id
```

- [x] **Step 2: Run the red control tests**

Run: `cd backend && python -m pytest test/test_issue_26_control_unit.py -v`

Expected: FAIL with missing module/classes.

- [x] **Step 3: Implement validation and row-locked state transitions**

```python
IDEMPOTENCY_KEY_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$')

def canonical_fingerprint(asset_ids: Sequence[uuid.UUID], confirmation: str) -> str:
    if len(asset_ids) != len(set(asset_ids)):
        raise PurgeBatchValidationError('DUPLICATE_PURGE_ASSET_ID')
    payload = {'asset_ids': sorted(str(value) for value in asset_ids), 'confirmation': confirmation}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

def create_or_replay(self, *, actor_id: str, idempotency_key: str, asset_ids: Sequence[uuid.UUID], confirmation: str) -> CreateResult:
    # Caller has already passed gate/pipeline; this method validates body, locks assets in stable UUID order,
    # rejects non-archived/referenced choices, inserts all batch items atomically, and returns 201-or-replay metadata.
```

Reject non-1–20 selections, duplicate selection payloads, non-exact `永久删除 N 张`, and non-archived assets with stable safe errors. First validate duplicates, then calculate the fingerprint. Lock all selected `ImageAsset` rows in canonical UUID order; inside those locks reject an item held by any non-`cancelled` batch with `PURGE_ASSET_IN_ACTIVE_BATCH`. For an existing actor/key, compare fingerprints before any new rows; same fingerprint returns the existing row at any state, different fingerprint raises `PURGE_IDEMPOTENCY_CONFLICT`. On a concurrent unique insert, roll back only to a savepoint, reread the locked actor/key row, then replay or conflict deterministically. `retry` only changes retryable `failed -> queued`; cancel is permitted from every non-`pending_deletion` status, including failed retention expiry, increments generation and clears the claim. `PURGE_BACKUP_RETENTION_EXPIRED` cannot retry. Record only fixed event names and safe reason/state fields in activity records, in the same transaction as the successful state change.

- [x] **Step 4: Run focused unit tests**

Run: `cd backend && python -m pytest test/test_issue_26_control_unit.py -v`

Expected: PASS.

### Task 3: Capability evidence source and Flask app wiring without ops adapters

**Files:**
- Create: `backend/services/purge_pipeline_capability.py`
- Create: `backend/test/test_issue_26_capability_unit.py`
- Modify: `backend/services/purge_safety_gate.py`
- Modify: `backend/app.py`
- Modify: `backend/test/test_issue_23_gate_unit.py`

**Interfaces:**
- Produces `PurgePipelineCapabilitySource.evaluate(now) -> bool`, `FilePurgePipelineCapabilitySource`, `UnavailablePurgePipelineCapabilitySource`, and worker-only `write_capability_evidence(path, now, result)`.
- `pipeline_available()` remains the static name and delegates only to `current_app.config['PURGE_PIPELINE_CAPABILITY_SOURCE']` with unavailable fallback.

- [x] **Step 1: Write failing capability and static-isolation tests**

```python
def test_valid_evidence_is_true_only_until_its_120_second_expiry(tmp_path):
    source = FilePurgePipelineCapabilitySource(tmp_path / 'purge_batch_worker.json')
    write_capability_evidence(source.path, now=NOW, result='valid', ttl_seconds=120)
    assert source.evaluate(NOW + timedelta(seconds=119)) is True
    assert source.evaluate(NOW + timedelta(seconds=120)) is False

def test_flask_app_path_has_no_ops_adapter_import_or_ops_env_name():
    combined = _read('app.py') + _read('services/purge_safety_gate.py')
    assert 'PURGE_SOURCE_OSS_' not in combined
    assert 'PurgeObjectBackupService' not in combined
```

- [x] **Step 2: Run the red tests**

Run: `cd backend && python -m pytest test/test_issue_26_capability_unit.py test/test_issue_23_gate_unit.py -v`

Expected: FAIL because the source/delegation does not exist.

- [x] **Step 3: Implement strict file parsing and app fallback**

```python
CAPABILITY_FILENAME = 'purge_batch_worker.json'
CAPABILITY_TTL_SECONDS = 120
CAPABILITY_HEARTBEAT_SECONDS = 30

def pipeline_available() -> bool:
    source = current_app.config.get('PURGE_PIPELINE_CAPABILITY_SOURCE', UnavailablePurgePipelineCapabilitySource())
    return source.evaluate(datetime.now(timezone.utc))
```

Reuse T9 size (`65536`), ISO-8601, recursive forbidden-key, schema-version, and fail-closed rules. Require exact non-secret keys `schema_version`, `component`, `result`, `verified_at`, `expires_at`, `policy`, `summary`; require component `purge_batch_worker` and policy `backup_only_no_delete`. `app.py` creates a read-only source from `PURGE_PIPELINE_EVIDENCE_DIR`; missing/invalid config supplies unavailable source. No Flask module imports any ops adapter.

- [x] **Step 4: Run capability tests**

Run: `cd backend && python -m pytest test/test_issue_26_capability_unit.py test/test_issue_23_gate_unit.py -v`

Expected: PASS.

### Task 4: Production PostgreSQL reference snapshot reader

**Files:**
- Create: `backend/services/postgres_reference_snapshot.py`
- Create: `backend/test/test_issue_26_reference_snapshot_unit.py`
- Create: `backend/test/integration/test_issue_26_reference_snapshot.py`
- Modify: `backend/services/purge_object_backup.py`

**Interfaces:**
- Produces `PostgresReferenceSnapshotReader(session, *, clock, max_age_seconds).capture_for_purge(asset_ids) -> CompleteReferenceSnapshot`.
- Consumes existing `PurgeAssetSnapshot`, `ObjectReference`, `ReferenceSourceSlice`, and required source constants in `purge_object_backup.py`.

- [x] **Step 1: Write failing snapshot tests**

```python
def test_snapshot_enumerates_assets_and_persisted_import_items_and_protects_shared_preview(session):
    snapshot = reader.capture_for_purge((archived_asset.id,))
    assert {slice.source for slice in snapshot.source_slices} == {'image_assets', 'image_import_items'}
    assert any(ref.source == 'image_import_items' and ref.formal_key == archived_asset.preview_oss_path for ref in snapshot.references)
    planned, protected, _ = _plan_snapshot(snapshot, (str(archived_asset.id),), formal_bucket='formal')
    assert planned == ()
    assert protected[0].formal_key == archived_asset.preview_oss_path

def test_snapshot_is_rejected_after_60_seconds():
    snapshot = reader.capture_for_purge((asset.id,))
    with pytest.raises(PurgeObjectReferenceError, match='实时引用快照'):
        backup_service._require_fresh_snapshot(replace(snapshot, captured_at=NOW - timedelta(seconds=61)))
```

- [x] **Step 2: Run the red tests**

Run: `cd backend && python -m pytest test/test_issue_26_reference_snapshot_unit.py -v`

Expected: FAIL because no production reader exists.

- [x] **Step 3: Implement a complete, deterministic reader**

```python
class PostgresReferenceSnapshotReader:
    def capture_for_purge(self, asset_ids: tuple[str, ...]) -> CompleteReferenceSnapshot:
        with self.session.begin():
            self.session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
            asset_rows = self._read_all_image_assets(asset_ids)
            import_rows = self._read_unpurged_import_bindings()
            return self._complete_snapshot(asset_rows, import_rows)
        # Return source slices for both required sources; no source may be truncated.
```

The adapter, not its caller, owns one `READ ONLY REPEATABLE READ` transaction and sets isolation in its first SQL statement. Include `image_import_items` only when `objects_purged_at IS NULL` and its original/preview binding remains semantically present; unknown lifecycle states fail closed. Build owner state `unfinished` for the stable object-backup catalog semantics. Include selected asset original/preview metadata and calculate deterministic references sorted by source/owner/kind/key. Both complete source slices, their counts and canonical rows form the one consistency token. Test concurrent insert/update after the transaction starts cannot produce mixed-source tokens. Preserve existing `_require_fresh_snapshot` behavior but pass the fixed 60-second config from worker construction; call existing `revalidate_current_candidates()` before final batch promotion.

- [ ] **Step 4: Run unit and real-PostgreSQL snapshot tests**

Run: `cd backend && python -m pytest test/test_issue_26_reference_snapshot_unit.py test/integration/test_issue_26_reference_snapshot.py -v`

Expected: PASS, or integration explicitly reports `SKIPPED` only when local PostgreSQL is unreachable.

### Task 5: Worker-only orchestration, leasing, cancellation CAS, and retention failure

**Files:**
- Create: `backend/services/purge_batch_worker.py`
- Create: `backend/test/test_issue_26_worker_unit.py`
- Create: `backend/test/integration/test_issue_26_worker.py`
- Modify: `backend/services/purge_batch_control.py`

**Interfaces:**
- Produces `PurgeBatchWorker.process_one() -> bool`, `ClaimedPurgeBatch`, and `PurgeBatchRepository.claim_next/advance_if_current/fail_if_current`.
- Consumes worker-only `PostgresBackupService`, a production `RestorePointGate` adapter, `PurgeObjectBackupService`, `PurgeObjectRestoreService`, and `PostgresReferenceSnapshotReader` through injected protocols.

- [ ] **Step 1: Write failing worker-state tests**

```python
def test_claimable_statuses_are_queued_or_expired_in_progress_only(session):
    assert repo.claim_next(worker_id='w1', now=NOW).batch_id == queued.id
    assert repo.claim_next(worker_id='w2', now=NOW).batch_id == expired_verifying.id
    assert repo.claim_next(worker_id='w3', now=NOW) is None  # failed, cancelled, pending, and live lease remain excluded

def test_cancel_generation_wins_over_late_object_backup_result(session):
    claim = repo.claim_next(worker_id='w1', now=NOW)
    control.cancel(claim.batch_id, actor_id='admin', now=NOW)
    assert repo.advance_if_current(claim, status='verifying', now=NOW) is False
    assert batch.status == 'cancelled'

def test_expired_retention_is_non_resumable_until_new_batch(session):
    worker.process_one()
    assert batch.status == 'failed'
    assert batch.error_code == 'PURGE_BACKUP_RETENTION_EXPIRED'
    with pytest.raises(PurgeBatchStateError):
        control.retry(batch.id, actor_id='admin')

def test_retention_expired_batch_can_cancel_to_release_its_assets(session):
    failed = make_batch(status='failed', error_code='PURGE_BACKUP_RETENTION_EXPIRED')
    assert control.cancel(failed.id, actor_id='admin').status == 'cancelled'

def test_worker_refuses_to_claim_when_queue_and_backup_database_identities_differ():
    assert worker.process_one() is False
    assert capability.last_result == 'failed'
```

- [ ] **Step 2: Run the red worker tests**

Run: `cd backend && python -m pytest test/test_issue_26_worker_unit.py -v`

Expected: FAIL because worker/repository protocols do not exist.

- [ ] **Step 3: Implement phase progression with no delete calls**

```python
CLAIMABLE = ('queued', 'database_backup', 'object_backup', 'verifying')

def process_one(self) -> bool:
    claim = self.repository.claim_next(worker_id=self.worker_id, lease_seconds=self.lease_seconds)
    if claim is None:
        return False
    # Require current safety gate and capability; then invoke only the phase's backup/revalidation protocol.
    # After every invocation, advance/fail only through token + generation + SELECT FOR UPDATE CAS.
```

`claim_next()` locks its row, turns `queued` into `database_backup` in the same committed transaction, and only reclaims an in-progress row after lease expiry. Every external backup/object/verification call occurs after the claim transaction commits. Every result mutation uses `id + claim_token + claim_generation + expected_status` in a `SELECT FOR UPDATE` CAS. Cancel/retry increment generation and clear every claim field. Before each durable advance re-evaluate the safety gate and capability. Map backup exceptions to the fixed phase error codes; map stale/changed snapshots to `PURGE_REFERENCE_SNAPSHOT_INVALID`. Revalidate restore-point/object `retain_until` before resume and before `pending_deletion`; retention error marks failed non-retryable but cancellable for that batch. The production restore-point gate calls `PostgresBackupService.create_backup()`, `verify_copies()`, then returns or reloads the strict verified manifest needed by `PurgeObjectBackupService.require_verified()`. Before any claim, compare queue `current_database()`/`system_identifier` with the backup source equivalents; mismatch/unavailable proof writes failed capability evidence and touches no batch. A late result may write only a fixed, sanitized `purge.batch.stale_result` audit event and never change a cancelled/new generation. Never import any delete adapter and never clean late/orphan artifacts.

- [ ] **Step 4: Run worker unit and integration tests**

Run: `cd backend && python -m pytest test/test_issue_26_worker_unit.py test/integration/test_issue_26_worker.py -v`

Expected: PASS, or an explicitly reported local-PostgreSQL skip only.

### Task 6: Dedicated worker process, compose topology, and evidence heartbeat

**Files:**
- Create: `backend/scripts/run_purge_batch_worker.py`
- Create: `backend/test/test_issue_26_worker_static_contract.py`
- Modify: `backend/Dockerfile`
- Modify: `docker-compose.yml`
- Modify: `backend/.env.example`
- Modify: `docs/operations/purge-batch-pipeline-runbook.md`

**Interfaces:**
- Produces SIGTERM-aware `main()` using 30-second evidence heartbeat and 2-second poll default.
- Produces a `purge-batch-worker` compose service that is the only `.env.backup` consumer.

- [ ] **Step 1: Write failing topology tests**

```python
def test_compose_isolates_backup_env_and_mounts_evidence_readonly_for_backend():
    compose = _read_root('docker-compose.yml')
    assert 'purge-batch-worker:' in compose
    assert './backend/.env.backup' in _service_block(compose, 'purge-batch-worker')
    assert './backend/.env.backup' not in _service_block(compose, 'backend')
    assert 'purge_pipeline_evidence:/app/purge-evidence:ro' in _service_block(compose, 'backend')

def test_worker_entry_imports_ops_adapters_but_flask_path_does_not():
    assert 'PurgeObjectBackupService' in _read('scripts/run_purge_batch_worker.py')
    assert 'PurgeObjectBackupService' not in _read('app.py')
```

- [ ] **Step 2: Run the red topology tests**

Run: `cd backend && python -m pytest test/test_issue_26_worker_static_contract.py -v`

Expected: FAIL because the service/entrypoint does not exist.

- [ ] **Step 3: Implement the separate runtime target and service**

```yaml
purge-batch-worker:
  build:
    context: ./backend
    target: purge-batch-worker-runtime
  command: ["python", "-m", "scripts.run_purge_batch_worker"]
  env_file: [./backend/.env.backup]
  image: fashion-crm-purge-batch-worker:latest
  environment:
    - BACKUP_ROOT=/var/lib/purge-batch-worker/postgres
    - PURGE_OBJECT_BACKUP_LOCAL_ROOT=/var/lib/purge-batch-worker/object-manifests
  volumes:
    - purge_pipeline_evidence:/app/purge-evidence
    - purge_batch_worker_state:/var/lib/purge-batch-worker
```

Refactor `backend/Dockerfile` into a normal backend runtime target and a `purge-batch-worker-runtime` target that installs PostgreSQL 16 client commands (`pg_dump`, `pg_restore`, `psql`, `createdb`); keep those commands out of the Flask target. Compose must build/tag the targets distinctly (`fashion-crm-backend:latest` versus `fashion-crm-purge-batch-worker:latest`) so a build cannot overwrite the other runtime. Backend mounts `purge_pipeline_evidence:/app/purge-evidence:ro`; worker mounts it read-write. Add `purge_batch_worker_state:/var/lib/purge-batch-worker` as the worker-only RW persistent root. In the worker image, a root-only entrypoint may do exactly `chown -R <fixed-worker-uid>:<fixed-worker-gid> /app/purge-evidence /var/lib/purge-batch-worker`, then must immediately `exec` the worker under that fixed unprivileged UID; it must not load `.env.backup`, generate capability payloads, or perform backup work while root. The worker process is the only capability-payload writer. Compose overrides `BACKUP_ROOT=/var/lib/purge-batch-worker/postgres` and `PURGE_OBJECT_BACKUP_LOCAL_ROOT=/var/lib/purge-batch-worker/object-manifests`; Postgres backup output and every object `plan.json`/`manifest.json` local root must derive only from these paths. No other service mounts this volume and #26 never removes its contents. Test the exact entrypoint privilege-drop commands, environment overrides and volume mounts as a static contract. The 30-second heartbeat must run independently of any synchronous backup operation (up to 3600 seconds), write with atomic replace, and on process preflight failure write `failed` evidence or naturally expire. `.env.example` documents only non-secret paths/timings (`PURGE_PIPELINE_EVIDENCE_DIR`, TTL 120, heartbeat 30, snapshot max age 60); it does not add ops credentials.

- [ ] **Step 4: Run topology tests**

Run: `cd backend && python -m pytest test/test_issue_26_worker_static_contract.py -v`

Expected: PASS; do not build or start compose services in this task without separate authorization.

### Task 7: Admin routes, public DTO, audit mapping, and T9 static replacement

**Files:**
- Modify: `backend/blueprints/admin_purge.py`
- Modify: `backend/app.py`
- Modify: `backend/test/test_issue_23_static_contract.py`
- Modify: `backend/test/test_issue_23_api_unit.py`
- Create: `backend/test/test_issue_26_admin_api_unit.py`
- Create: `backend/test/integration/test_issue_26_admin_api.py`

**Interfaces:**
- Adds `GET /api/admin/purge/batches` and `GET /api/admin/purge/batches/<batch_id>`.
- Changes existing create/cancel/retry fourth step to `PurgeBatchControlService`; maps safe `PurgeBatchError.error_code` to 400/404/409/201/200.

- [ ] **Step 1: Write failing route-order and behavior tests**

```python
def test_create_invalid_key_precedes_gate_but_does_not_parse_body():
    response = client.post('/api/admin/purge/batches', headers={**AUTH, 'Idempotency-Key': '@bad'}, json={'asset_ids': 'not-a-list'})
    assert response.status_code == 400
    assert response.get_json()['error_code'] == 'INVALID_PURGE_IDEMPOTENCY_KEY'

def test_gate_closed_invalid_key_returns_gate_409_and_audits_key_reason_only():
    response = closed_gate_client.post('/api/admin/purge/batches', headers={**AUTH, 'Idempotency-Key': '@bad'})
    assert response.status_code == 409
    assert response.get_json()['error_code'] == 'PURGE_GATE_NOT_READY'
    assert _last_audit().error_code == 'INVALID_PURGE_IDEMPOTENCY_KEY'

def test_detail_authenticates_before_not_found():
    assert client.get('/api/admin/purge/batches/missing').status_code in (401, 403)
```

- [ ] **Step 2: Run the red route tests**

Run: `cd backend && python -m pytest test/test_issue_23_static_contract.py test/test_issue_26_admin_api_unit.py -v`

Expected: FAIL because route/DTO semantics are absent.

- [ ] **Step 3: Implement route adapter only**

```python
@admin_purge_bp.post('/batches')
def create_purge_batch():
    principal = _authenticate_or_denied()
    key_error = _idempotency_key_syntax_error(request.headers.get('Idempotency-Key'))
    snapshot = current_app.config['PURGE_SAFETY_GATE'].require_ready()
    _require_pipeline(snapshot)
    if key_error:
        return _rejected('create', 'unspecified', error_code='INVALID_PURGE_IDEMPOTENCY_KEY', status=400, snapshot=snapshot)
    payload = request.get_json(silent=False)
    result = _control().create_or_replay(
        actor_id=principal.actor_id,
        idempotency_key=request.headers['Idempotency-Key'],
        asset_ids=payload['asset_ids'],
        confirmation=payload['confirmation'],
    )
```

When gate is closed, audit the key syntax reason separately without placing it in the response; do not call `request.get_json()`. Add `Idempotency-Key` to the Flask CORS `allow_headers` and assert it in API/static tests. Add GET list/detail routes with authentication before ID syntax/look-up, DTO-only responses, and bounded `limit`/cursor pagination; GET must not require safety-gate readiness so a stalled batch remains observable. Preserve cancel/retry `require_ready()` behavior; report `PURGE_BATCH_NOT_CANCELLABLE`/`PURGE_BATCH_NOT_RETRYABLE` as 409. Define and test fixed safe audit event names: `purge.batch.created`, `purge.batch.claimed`, `purge.batch.database_backup.succeeded`, `purge.batch.object_backup.succeeded`, `purge.batch.verifying.succeeded`, `purge.batch.failed`, `purge.batch.cancelled`, `purge.batch.retried`, and `purge.batch.stale_result`; each may carry only batch/item ID, status and error/result code. Replace the T9 constant-false test with delegation/fallback tests and document both authorized reasons in comments: pipeline delegation is the #26 fourth-step replacement, and Q9 allows only create header syntax before the gate. Add literal `url_prefix` assertion.

- [ ] **Step 4: Run route, static, and local integration tests**

Run: `cd backend && python -m pytest test/test_issue_23_static_contract.py test/test_issue_23_api_unit.py test/test_issue_26_admin_api_unit.py test/integration/test_issue_26_admin_api.py -v`

Expected: PASS, or explicit integration skip only when local PostgreSQL is unreachable.

### Task 8: Restore-side exclusion and batch/asset locking integration

**Files:**
- Modify: `backend/services/asset_recycle_bin.py`
- Modify: `backend/blueprints/image_assets.py`
- Create: `backend/test/test_issue_26_restore_unit.py`
- Create: `backend/test/integration/test_issue_26_restore_locking.py`

**Interfaces:**
- Restore checks `PurgeBatchItem` after locking every requested `ImageAsset`; blocking code is `PURGE_ASSET_RESTORE_BLOCKED`.

- [ ] **Step 1: Write failing restore tests**

```python
def test_restore_rejects_archived_asset_held_by_failed_batch_and_records_reference(session):
    result = restore_image_assets(session, [asset.id], actor_id='admin', request_id='r1')
    assert result.error_code == 'PURGE_ASSET_RESTORE_BLOCKED'
    assert _activity().after_state['batch_id'] == str(batch.id)

def test_cancelled_batch_allows_existing_restore_behavior(session):
    batch.status = 'cancelled'
    restored = restore_image_assets(session, [asset.id], actor_id='admin', request_id='r2')
    assert restored.restored_ids == [asset.id]
```

- [ ] **Step 2: Run red restore tests**

Run: `cd backend && python -m pytest test/test_issue_26_restore_unit.py -v`

Expected: FAIL because purge references are not checked.

- [ ] **Step 3: Add lock-first exclusion without changing normal restore**

```python
assets = session.execute(select(ImageAsset).where(ImageAsset.id.in_(asset_ids)).order_by(ImageAsset.id).with_for_update()).scalars().all()
blocking = session.execute(
    select(PurgeBatchItem, PurgeBatch)
    .join(PurgeBatch, PurgeBatch.id == PurgeBatchItem.batch_id)
    .where(PurgeBatchItem.target_asset_id.in_([asset.id for asset in assets]))
    .where(PurgeBatch.status != 'cancelled')
    .order_by(PurgeBatchItem.target_asset_id, PurgeBatch.id)
).first()
if blocking:
    raise RestoreBlockedByPurgeBatch(batch_id=blocking.PurgeBatch.id)
```

Use the same stable ordering as create; no row may become active while a non-cancelled pre-deletion batch holds it. Convert the service error to a safe API message and activity record. Do not alter success output for archived assets with no batch item.

- [ ] **Step 4: Run focused restore tests**

Run: `cd backend && python -m pytest test/test_asset_recycle_bin_unit.py test/test_issue_26_restore_unit.py test/integration/test_issue_26_restore_locking.py -v`

Expected: existing #17 tests and new lock tests PASS, or explicit local-PostgreSQL skip only.

### Task 9: Frontend DTO/API, strong confirmation, bounded polling, and messages

**Files:**
- Modify: `frontend/src/types/product.ts`
- Modify: `frontend/src/services/productApi.ts`
- Modify: `frontend/src/services/productApi.test.ts`
- Modify: `frontend/src/components/ArchivedAssetGrid.tsx`
- Modify: `frontend/src/components/ArchivedAssetGrid.test.tsx`

**Interfaces:**
- Produces `PurgeBatchDto`, `PurgeBatchItemDto`, `createPurgeBatch`, `getPurgeBatches`, `getPurgeBatch`, `cancelPurgeBatch`, and `retryPurgeBatch`.
- `createPurgeBatch(assetIds, confirmation, idempotencyKey, token)` sends both required headers; no batch function runs without a session token.

- [ ] **Step 1: Write failing UI/API tests**

```tsx
it('does not request batch APIs without a session token', () => {
  render(<ArchivedAssetGrid {...baseProps} />);
  expect(api.getPurgeBatches).not.toHaveBeenCalled();
});

it('polls every 5000ms only while queued and stops at pending_deletion', async () => {
  vi.useFakeTimers();
  vi.mocked(api.getPurgeBatch).mockResolvedValueOnce(queued).mockResolvedValueOnce(pendingDeletion);
  renderWithToken();
  await vi.advanceTimersByTimeAsync(5000);
  await vi.advanceTimersByTimeAsync(10000);
  expect(api.getPurgeBatch).toHaveBeenCalledTimes(2);
});

it('shows the safe-gate cancellation explanation on gate 409', async () => {
  vi.mocked(api.cancelPurgeBatch).mockRejectedValue(new PurgeBatchRequestError('安全门关闭时无法取消', 409, 'PURGE_GATE_NOT_READY'));
  renderWithToken();
  await userEvent.click(screen.getByRole('button', { name: '取消批次' }));
  expect(await screen.findByText('安全门关闭时无法取消')).toBeVisible();
});
```

- [ ] **Step 2: Run red frontend tests**

Run: `cd frontend && npm test -- ArchivedAssetGrid.test.tsx productApi.test.ts`

Expected: FAIL because batch types/functions/UI do not exist.

- [ ] **Step 3: Implement API and UI without local status synthesis**

```ts
export type PurgeBatchStatus = 'queued' | 'database_backup' | 'object_backup' | 'verifying' | 'pending_deletion' | 'failed' | 'cancelled';
export const PURGE_BATCH_POLL_MS = 5_000;
export const POLLABLE_PURGE_BATCH_STATUSES: PurgeBatchStatus[] = ['queued', 'database_backup', 'object_backup', 'verifying'];
```

Use `crypto.randomUUID()` once per explicit confirmation attempt; retain it only for a transport replay of that same attempt, and create a new one after cancelled/new confirmation. Require exact client confirmation text but let server remain authoritative. Stop polling on failed/cancelled/pending/unmount; render item-level codes/reasons and clear, safe restore-blocked/cancel-gate text. Do not add tokens, URLs, object keys, or manifest details to TypeScript DTOs.

- [ ] **Step 4: Run frontend focused tests and type/build checks**

Run: `cd frontend && npm test -- ArchivedAssetGrid.test.tsx productApi.test.ts && npm run build`

Expected: PASS.

### Task 10: Operations facts, AGENTS synchronization, and final allowed verification

**Files:**
- Modify: `AGENTS.md`
- Create: `docs/operations/purge-batch-pipeline-runbook.md`
- Modify: `docs/operations/purge-gate-evidence.md`
- Modify: `docs/operations/postgresql-backup-restore-runbook.md`
- Modify: `docs/operations/purge-object-backup-restore-runbook.md`
- Modify: `backend/test/test_issue_26_worker_static_contract.py`

**Interfaces:**
- Documents the new worker, capability volume permissions, production snapshot adapter, deterministic retention behavior, manual orphan/recovery-point ledger, and no-delete boundary.

- [ ] **Step 1: Write failing documentation/static assertions**

```python
def test_agents_and_runbook_state_the_current_purge_worker_facts():
    agents = _read_root('AGENTS.md')
    runbook = _read_root('docs/operations/purge-batch-pipeline-runbook.md')
    assert 'purge-batch-worker' in agents
    assert 'PostgresReferenceSnapshotReader' in agents
    assert '不自动清理' in runbook
    assert 'PURGE_BACKUP_RETENTION_EXPIRED' in runbook
```

- [ ] **Step 2: Run the red documentation test**

Run: `cd backend && python -m pytest test/test_issue_26_worker_static_contract.py -v`

Expected: FAIL until the documentation assertions and documents exist.

- [ ] **Step 3: Update only changed operational facts**

Document the worker-only `.env.backup` contract, compose shared-volume worker-write/backend-read-only permissions, separate worker-state persistence root, unprivileged worker UID initialization, distinct runtime images, 120/30/60 timing values, capability key restrictions, all stable error codes, retention expiration/new-batch rule, #27 revalidation requirement, manual-only handling for residual restore points/copies/orphans, and prohibition on deletion. In `AGENTS.md`, replace the now-stale statements “no production PostgreSQL reference snapshot adapter”, “pipeline_available always false”, and the compose table lacking this worker. Do not document implementation history as operational fact. Extend the static contract across every new worker/composition root to reject `session.delete`, SQL `DELETE`, `delete_object`, any Kodo import/use, and every formal/backup Delete adapter.

- [ ] **Step 4: Run all permitted verification**

Run:

```bash
cd backend && python -m pytest \
  test/test_issue_23_static_contract.py \
  test/test_issue_23_auth_unit.py \
  test/test_issue_23_gate_unit.py \
  test/test_issue_23_api_unit.py \
  test/test_issue_26_schema_static_contract.py \
  test/test_issue_26_control_unit.py \
  test/test_issue_26_capability_unit.py \
  test/test_issue_26_reference_snapshot_unit.py \
  test/test_issue_26_worker_unit.py \
  test/test_issue_26_worker_static_contract.py \
  test/test_issue_26_admin_api_unit.py \
  test/test_issue_26_restore_unit.py \
  test/test_purge_object_backup.py \
  test/test_postgres_backup.py -v
cd backend && python -m pytest test/integration/test_issue_26_reference_snapshot.py test/integration/test_issue_26_worker.py test/integration/test_issue_26_admin_api.py test/integration/test_issue_26_restore_locking.py -v
cd frontend && npm test -- ArchivedAssetGrid.test.tsx productApi.test.ts && npm run build
```

Expected: every executed test passes; if the integration fixture explicitly skips because local PostgreSQL is unreachable, report that as **not run**, not passing. Also include focused tests for same-key insert races, competing asset acquisition, two expired leases, cancel-vs-external-call late results, create-vs-restore lock order, snapshot concurrent writes, queue/backup identity mismatch, long-call heartbeat, state-volume absence/corruption and CORS headers. Then inspect `git diff --check`, `git diff --stat`, and the exact scoped diff. Do not commit, push, start compose, run migrations against shared data, or call any real external service.

## Plan self-review record

- **Spec coverage:** schema/state/claiming, T9 compatibility, capability isolation, production reference snapshots, #24/#25 orchestration, retention, cancellation, restore locking, DTO/UI, compose, operations docs, and zero deletion each have a task.
- **Architect review (2026-08-29):** APPROVE WITH REQUIRED PLAN CHANGES. The plan now records current-worktree Matt execution, tombstone item IDs, create/replay races, durable CAS/cancellation, isolated repeatable-read snapshots, production restore-point/database-identity seams, persistent worker state and independent heartbeat, CORS/pagination/audit contracts, and whole-surface zero-delete assertions. Implementation may start only after this revised plan is checked for internal consistency.
- **T9 replacement rationale:** Task 7 records both authorized changes: `pipeline_available()` delegates to a no-secret capability source instead of a constant, and create adds only header syntax before gate per Q9. No other first-three-step behavior changes.
- **Scope check:** no task creates formal deletion or changes #27; all backup residue has a manual operational ledger only.
- **Risk-review repair verification (2026-08-29):** B1/B2/M1 regression suite is green (23 passed across worker/control/static contracts), and `git diff --check` is green. The permitted non-integration backend suite (`test/`, excluding integration, real OSS and manual pgvector/benchmark scripts) reports **479 passed, 2 failed**. Both failures are pre-existing Kodo full-migration authorization-age/list-failure expectations in `test_kodo_preflight.py`, outside #26. No `localhost:5433` integration test was run because Docker is unavailable.
- **Risk-review follow-up repair (2026-08-29):** follow-up M1 now preflights database identity before the first capability publication and on every heartbeat, so `valid` cannot precede an identity failure and a recovered identity can become healthy again. Docker context explicitly excludes `.env.backup`, and cleanup joins backend/image worker in pinning `target: backend-runtime`. New red/green contracts plus the worker/static suite are green (18 passed); `git diff --check` remains green.
- **Progress:** Task 1 completed: its first RED run confirmed the absent migration/model, and `backend/test/test_issue_26_schema_static_contract.py -v` is green (4 passed). Task 2 completed: `backend/test/test_issue_26_control_unit.py -v` is green (7 passed), covering deterministic create/replay, active-batch exclusion, cancellation/retry retention semantics, atomic claiming, idempotency savepoint handling and late-result CAS rejection. Task 3 completed: capability evidence is strict, TTL-bound and fail-closed, while Flask has only a no-secret reader/fallback; the Task 3/T9 targeted suite is green (19 passed). Task 4 unit contracts are green (3 passed); its isolated local PostgreSQL test was **not run** because the sandbox denied `localhost:5433` access before the `image_search_test` fixture could connect. Task 5 completed: worker CAS, identity fail-closed, RestorePointGate create-then-verify, phase evidence mapping and snapshot-error codes are unit-green (`test_issue_26_worker_unit.py`, 9 passed). Task 6 completed: `_build_worker()` composes PostgresBackupService / OssBackupStorage / OssPurgeSourceReader / PostgresRestorePointGate / PurgeObjectBackupService / PurgeObjectRestoreService (verify-copies only, no isolation writer); missing BACKUP_ROOT fails closed without constructing storage; heartbeat continues during a long `process_one`; root entrypoint only `chown`s then `setpriv`s to UID 1000; `postgresql-client-16` is the worker runtime client. No adapter constructor was executed against live OSS/PostgreSQL. Worker/control/static suite is green (19 passed). Task 5/6 local PostgreSQL integration (`test/integration/test_issue_26_worker.py`) was **not run**. Task 7 completed: create/cancel/retry 第四步接入 `PurgeBatchControlService`；create 在门前只做 Idempotency-Key 语法检查（门关闭时响应仍为 `PURGE_GATE_NOT_READY`，审计记录键语法）；GET list/detail 先认证且不要求安全门；CORS 允许 `Idempotency-Key`。Task 8 completed: 恢复在锁资产后排除未取消批次，失败批次阻断、取消批次可恢复。Task 9 completed: 前端 DTO/API 发送 Bearer 与 Idempotency-Key；无令牌不请求批次 API；可轮询状态每 5s，止于 `pending_deletion`；门 409 取消展示「安全门关闭时无法取消」。`npm test --run ArchivedAssetGrid.test.tsx productApi.test.ts` 43 passed；`npm run build` 通过。Task 10 completed: AGENTS.md 与运维手册已改为当前事实（purge-batch-worker、PostgresReferenceSnapshotReader、能力 TTL/heartbeat、保留期到期、不自动清理）；静态零删除合同覆盖 worker 组合根。最终允许的后端定向套件 144 passed。Issue #26 四份集成测试本次实际执行并通过（4 passed）；未启动 Compose、未做真实云调用、未 commit。Follow-up: `test_issue_17_static_contract.py` 的裸子串 `'purge' not in lowered` 在 #26 恢复锁引入 `RestoreBlockedByPurgeBatch` / `PURGE_ASSET_RESTORE_BLOCKED` / ORM `PurgeBatch` 后变红。合同原意是回收站不得持有删除通道，不是禁止观察批次占用。已将断言收窄为 AST 导入禁令（`purge_batch_control`、`purge_batch_worker`、`purge_object_backup`、`purge_object_restore`、`admin_purge`、`run_purge_batch_worker`），保留 delete/存储/embedding 原语禁令，并锁定允许的异常名与错误码；不以改名规避。收窄后该合同 5 passed。**Risk-review repair round (2026-08-29):** B1：`item_asset_ids()` 在任何返回路径 rollback，真实仓库/快照 reader 共享 session 回归测试通过；B2：backend 与普通 image worker Compose build 均显式 `target: backend-runtime`；M1：worker heartbeat 从 `capability_healthy()` 派生，身份失败后持续发布 failed，不再覆盖为 valid。能力临时文件名加入 pid/UUID，避免心跳与主线程竞争。审查修复定向套件 16 passed；不启动 Compose、不执行迁移或真实外部调用。
