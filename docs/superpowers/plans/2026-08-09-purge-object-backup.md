# Purge Object Backup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `tdd` and implement each task as a vertical RED→GREEN slice. Formal code review is intentionally omitted by the user for this ticket.

**Goal:** Build a fail-closed, write-once object-backup manifest and isolated-restore workflow for permanent-purge candidates without deleting any formal object.

**Architecture:** `PurgeObjectBackupService` owns reference selection, source-byte verification, immutable plan creation, write-once backup reconciliation, and final-manifest commit. `PurgeObjectRestoreService` separately owns backup-copy verification and restore to program-derived keys in an isolated bucket. Reference, formal OSS, backup OSS, and isolated OSS behavior enter through narrow injected ports; only fake adapters are wired in this ticket.

**Tech Stack:** Python 3.9+, dataclasses, Protocol, canonical JSON, hashlib, tempfile, pytest, Aliyun `oss2` adapters.

## Global Constraints

- Base is detached `088bb9f` plus the exact uncommitted Issue #24 delta imported from its parent worktree.
- Do not commit, push, modify GitHub, deploy, configure credentials, or connect to PostgreSQL/OSS.
- Do not implement or call Delete for formal, backup, or isolated objects.
- Do not expose a standalone create-backup CLI; Issue #26 owns authenticated persistent orchestration.
- A batch contains 1–20 unique archived image assets.
- Reference catalog v1 always covers active/archived `image_assets` and unfinished `image_import_items`, including explicit complete empty slices.
- PostgreSQL restore point and object backup use the same `purge_batch_id`; object retention inherits its exact 30-day `retain_until`.
- Search preview SHA-256 is computed from downloaded preview bytes, never copied from `ImageAsset.content_hash` or OSS ETag.
- Existing destination objects are never overwritten; exact matches may be reconciled only after HEAD and independent download verification.
- `plan.json` precedes payload writes; `manifest.json` is the final commit marker.
- Real IAM, private/SSE state, lifecycle/Object Lock, PostgreSQL snapshot semantics, and real isolated restore remain `not_verified`.

---

### Task 1: Reference snapshot contract and first verified-backup tracer bullet

**Files:**
- Create: `backend/services/purge_object_backup.py`
- Create: `backend/test/test_purge_object_backup.py`

**Interfaces:**
- Consumes: `BackupManifest` returned by `RestorePointGate.require_verified(purge_batch_id)`.
- Produces: `PurgeObjectBackupRequest`, `CompleteReferenceSnapshot`, `PurgeObjectBackupManifest`, `PurgeObjectBackupService.create_verified()`.

- [ ] **Step 1: Write one failing public-seam test**

Create fakes for `RestorePointGate`, `ReferenceSnapshotReader`, formal object reader, and write-once backup store. The worked example contains one archived asset with one source image and one search preview; both fake source payloads have known literal bytes.

```python
def test_create_verified_binds_restore_point_and_commits_source_and_preview(tmp_path):
    service = build_service(
        tmp_path,
        targets=[archived_asset("asset-a", "formal/a.png", "formal/p.jpg")],
        references=[
            asset_reference("asset-a", "source_image", "formal/a.png"),
            asset_reference("asset-a", "search_preview", "formal/p.jpg"),
        ],
        source_objects={
            "formal/a.png": b"source-a",
            "formal/p.jpg": b"preview-a",
        },
    )

    result = service.create_verified(
        PurgeObjectBackupRequest("batch-001", ("asset-a",))
    )

    assert result.status == "complete"
    assert result.manifest.database_restore_point.backup_id == "purge-batch-001"
    assert [(item.kind, item.asset_ids) for item in result.manifest.objects] == [
        ("source_image", ("asset-a",)),
        ("search_preview", ("asset-a",)),
    ]
    assert result.manifest.objects[1].sha256 == hashlib.sha256(b"preview-a").hexdigest()
    assert backup_store.exists(result.manifest_key)
```

- [ ] **Step 2: Run the test and record RED**

Run: `cd backend && python -m pytest test/test_purge_object_backup.py::test_create_verified_binds_restore_point_and_commits_source_and_preview -v`

Expected: FAIL because `services.purge_object_backup` does not exist.

- [ ] **Step 3: Implement the minimal deep Module**

Add strict value models and ports:

```python
class RestorePointGate(Protocol):
    def require_verified(self, purge_batch_id: str) -> BackupManifest: ...

class ReferenceSnapshotReader(Protocol):
    def capture_for_purge(self, asset_ids: tuple[str, ...]) -> CompleteReferenceSnapshot: ...

class ReadableObjectStorage(Protocol):
    def head(self, key: str) -> Optional[BackupObject]: ...
    def download_to(self, key: str, target: BinaryIO) -> None: ...

class WriteOnceObjectStorage(ReadableObjectStorage, Protocol):
    def put_file_if_absent(self, key: str, path: Path, *, metadata: Mapping[str, str]) -> None: ...
    def put_bytes_if_absent(self, key: str, data: bytes, *, metadata: Mapping[str, str]) -> None: ...
```

Implement `create_verified()` as one non-skippable flow: validate request and restore point, validate complete snapshot, select objects, HEAD/download/hash source bytes, write/readback plan, write/reconcile/readback payloads, capture the snapshot again, then write/readback final manifest.

- [ ] **Step 4: Run the tracer test and record GREEN**

Run the exact Step 2 command.

Expected: `1 passed`.

### Task 2: Complete reference catalog, shared previews, and current revalidation

**Files:**
- Modify: `backend/services/purge_object_backup.py`
- Modify: `backend/test/test_purge_object_backup.py`

**Interfaces:**
- Consumes: reference catalog v1 and `CompleteReferenceSnapshot`.
- Produces: `PurgeObjectBackupService.revalidate_current_candidates()` and `CurrentDeletionCandidates`.

- [ ] **Step 1: Add the batch-shared last-reference RED test**

```python
def test_shared_preview_is_backed_up_once_for_all_selected_assets(tmp_path):
    result = create_for_two_assets_sharing_preview(tmp_path)

    previews = [item for item in result.manifest.objects if item.kind == "search_preview"]
    assert len(previews) == 1
    assert previews[0].asset_ids == ("asset-a", "asset-b")
```

Run only that test and confirm it fails before changing implementation, then group references by `(bucket, key)` and emit one canonical item.

- [ ] **Step 2: Add external-reference and unfinished-import RED tests one at a time**

```python
@pytest.mark.parametrize("external_reference", [
    asset_reference("asset-c", "search_preview", "formal/shared.jpg", state="active"),
    import_reference("import-1", "search_preview", "formal/shared.jpg", state="processing"),
])
def test_external_reference_protects_shared_preview(tmp_path, external_reference):
    result = create_with_external_reference(tmp_path, external_reference)
    assert not any(item.kind == "search_preview" for item in result.manifest.objects)
    assert result.manifest.reference_protected[0].formal_key == "formal/shared.jpg"
```

For each parameterized behavior, run RED first, implement only the reference-protected decision, then rerun GREEN.

- [ ] **Step 3: Add incomplete-snapshot RED tests**

Reject a missing import slice, mismatched consistency token, `truncated=true`, incorrect enumerated count, non-archived target, missing target edge, mixed object role, and shared source image. Use stable `PurgeObjectReferenceError(stage="reference_snapshot", error_code=...)` results and do not touch either object store.

- [ ] **Step 4: Add TOCTOU RED tests**

Provide one snapshot before copies and a semantically different snapshot before final commit. Assert no complete manifest exists. Implement a canonical semantic digest over sorted targets, source slices, references, and decisions; exclude volatile capture timestamps from equality.

- [ ] **Step 5: Add current-revalidation RED tests**

`revalidate_current_candidates()` may remove a previously backed-up preview when a new reference appears. It must fail if a previously protected preview has become a last reference, because that object was not backed up.

- [ ] **Step 6: Run Task 2 tests**

Run: `cd backend && python -m pytest test/test_purge_object_backup.py -v`

Expected: all object-planning and reference-contract tests PASS with no DB/OSS access.

### Task 3: Immutable plan, write-once reconcile, and strict manifest

**Files:**
- Modify: `backend/services/purge_object_backup.py`
- Modify: `backend/test/test_purge_object_backup.py`

**Interfaces:**
- Consumes: `WriteOnceObjectStorage`.
- Produces: strict `PurgeObjectBackupManifest.from_dict()` and canonical `to_dict()`.

- [ ] **Step 1: Add plan-before-payload RED test**

Use a fake store that records writes and fails the second payload. Assert the first write is deterministic `objects/plan.json`, no `objects/manifest.json` exists, and every partial payload carries batch/object identity metadata that the plan references.

- [ ] **Step 2: Implement deterministic Key and immutable plan**

Derive object IDs from `sha256(formal_bucket + "\\0" + formal_key)` and keys from the configured exact prefix plus database `backup_id`. Persist a local 0600 candidate, Put-if-absent the remote plan, HEAD it, then download and compare canonical bytes before payload writes.

- [ ] **Step 3: Add exact-reconcile and conflict RED tests**

One test reruns an identical batch and asserts the final manifest identity is unchanged. A second preloads a destination with the same size but different downloaded bytes and asserts `backup_object_conflict`; no overwrite occurs.

- [ ] **Step 4: Add independent verification RED tests**

Use a fake whose post-Put HEAD lies about metadata and a fake whose download corrupts bytes. Both must fail before the final manifest. Implement validation of HEAD size/metadata and independent downloaded size/SHA-256.

- [ ] **Step 5: Add strict-manifest RED tests**

Reject unknown fields, unknown schema/status/kind, mismatched batch/DB backup ID, unsafe keys, duplicate source or backup identities, wrong 30-day retention, unsupported verification values, and any self-asserted production gate other than `not_verified`.

- [ ] **Step 6: Run Task 3 tests**

Run: `cd backend && python -m pytest test/test_purge_object_backup.py -v`

Expected: all tests PASS; plan is first, final manifest is last, and no fake exposes Delete.

### Task 4: Isolated restore Module

**Files:**
- Create: `backend/services/purge_object_restore.py`
- Create: `backend/test/test_purge_object_restore.py`

**Interfaces:**
- Consumes: strict `PurgeObjectBackupManifest`, backup `ReadableObjectStorage`, isolated `WriteOnceObjectStorage`.
- Produces: `verify_copies()` and `restore_to_isolation()`.

- [ ] **Step 1: Write the restore tracer RED test**

```python
def test_restore_uses_program_derived_isolated_keys_and_rechecks_bytes(tmp_path):
    result = restore_service(tmp_path).restore_to_isolation(
        complete_manifest(),
        restore_run_id="drill-001",
        acknowledge_isolated=True,
    )

    assert result.status == "verified"
    assert all(item.isolated_key.startswith("isolated/drill-001/purge-batch-001/objects/") for item in result.objects)
    assert all(item.isolated_key != item.formal_key for item in result.objects)
```

Run RED, then implement download-and-verify backup payload, deterministic target Key, write-once target Put, target HEAD, and independent target download verification.

- [ ] **Step 2: Add isolation-gate RED tests**

Require explicit acknowledgement, `PURGE_RESTORE_ISOLATED=1`, and a target Bucket distinct from every formal and backup Bucket. Do not accept a caller-supplied target Key.

- [ ] **Step 3: Add restore conflict and corruption RED tests**

Existing identical isolated objects reconcile. Existing mismatched objects, corrupt backup download, corrupt isolated readback, or remote final-manifest mismatch return stable failures without overwrite.

- [ ] **Step 4: Run restore tests**

Run: `cd backend && python -m pytest test/test_purge_object_restore.py -v`

Expected: all tests PASS with only in-memory stores and temporary local files.

### Task 5: Dedicated OSS adapters, ops CLI, and static safety contracts

**Files:**
- Create: `backend/services/purge_object_storage.py`
- Create: `backend/scripts/manage_purge_object_backups.py`
- Create: `backend/test/test_purge_object_storage.py`
- Create: `backend/test/test_manage_purge_object_backups.py`
- Create: `backend/test/test_purge_object_backup_contract.py`
- Modify: `backend/services/backup_storage.py`
- Modify: `backend/test/test_backup_storage.py`
- Modify: `backend/.env.backup.example`

**Interfaces:**
- Produces: Head/Get-only formal reader; private/SSE/forbid-overwrite isolated writer; CLI `verify-copies` and `restore-isolated` only.

- [ ] **Step 1: Add credential-isolation RED tests**

Require `PURGE_SOURCE_OSS_*` without fallback to `OSS_*`; reject source access-key reuse with the app/backup/restore credential. Require `PURGE_RESTORE_OSS_*`; reject restore Bucket or access-key reuse with formal/backup identities. Extend `BackupStorageConfig` to reject reuse of the restore credential when both are present.

- [ ] **Step 2: Implement narrow adapters**

The source Adapter exposes only `head()` and `download_to()`. The isolation Adapter exposes only `head()`, `put_file_if_absent()`, and `download_to()`, with `x-oss-forbid-overwrite=true`, private ACL, SSE, and caller metadata. Neither exposes Delete, ACL mutation, lifecycle mutation, signing, or list operations.

- [ ] **Step 3: Add CLI RED tests**

Verify `verify-copies --manifest PATH` and `restore-isolated --manifest PATH --restore-run-id ID --acknowledge-isolated` return stable JSON and fixed exit codes. Confirm there is no create or delete subcommand, direct script execution works, and SDK error text is redacted.

- [ ] **Step 4: Add static contract RED tests**

Use AST/text inspection to assert the new production modules do not call `delete_object`, do not import Flask/app models, do not read `DATABASE_URL`/`DB_*`, do not fall back from ops OSS variables to app secrets, and do not construct a create-backup CLI.

- [ ] **Step 5: Run adapter/CLI/static tests**

Run: `cd backend && python -m pytest test/test_purge_object_storage.py test/test_manage_purge_object_backups.py test/test_purge_object_backup_contract.py test/test_backup_storage.py -v`

Expected: all tests PASS without network access.

### Task 6: Operations docs and final offline verification

**Files:**
- Create: `docs/operations/purge-object-backup-restore-runbook.md`
- Create: `docs/operations/templates/purge-object-restore-drill-record.md`
- Modify: `AGENTS.md`

**Interfaces:**
- Documents the Module/CLI entry points, credential split, evidence, and production gates.

- [ ] **Step 1: Write the runbook and evidence template**

Document that object-backup creation has no standalone CLI and is only composed by future #26. Document fake commands separately from real T14 steps. The template records purge/database/object manifest IDs, object counts, actual size/hash verification, isolation target identity, IAM/SSE/lifecycle evidence, authorization reference, and `not_verified|verified` conclusion without secrets or signed URLs.

- [ ] **Step 2: Update the nearest architecture facts**

Add the new Module/CLI files and credential boundaries to root `AGENTS.md`. Do not describe implementation history or claim production enablement.

- [ ] **Step 3: Run all Issue #25 fake tests**

Run: `cd backend && python -m pytest test/test_purge_object_backup.py test/test_purge_object_restore.py test/test_purge_object_storage.py test/test_manage_purge_object_backups.py test/test_purge_object_backup_contract.py -v`

Expected: all PASS; no skip and no PostgreSQL/OSS connection.

- [ ] **Step 4: Re-run inherited Issue #24 tests and regressions**

Run: `cd backend && python -m pytest test/test_backup_storage.py test/test_postgres_backup.py test/test_postgres_restore_verification.py test/test_manage_postgres_backups.py -v`

Expected: `42 passed` or a higher intentional count if Task 5 extends `test_backup_storage.py`.

Run: `cd backend && env -u RUN_DISPOSABLE_BACKUP_RESTORE_TEST python -m pytest test/integration/test_postgres_backup_restore.py -v`

Expected: `1 skipped` at the missing explicit gate, with no connection attempt.

Run: `cd backend && python -m pytest test/test_object_storage.py test/test_contract_configuration.py -v`

Expected: `10 passed`.

- [ ] **Step 5: Compile and run static safety scans**

Run: `cd backend && python -m compileall services/backup_storage.py services/postgres_backup.py services/purge_object_backup.py services/purge_object_restore.py services/purge_object_storage.py scripts/manage_postgres_backups.py scripts/manage_purge_object_backups.py`

Run: `rg -n "delete_object|delete_objects|batch_delete|DROP |DELETE FROM" backend/services/purge_object_* backend/scripts/manage_purge_object_backups.py`

Expected: no destructive production call; test/docs references may describe prohibition only.

Run: `rg -n "(postgresql|postgres)://|AKIA|LTAI|BEGIN (RSA|OPENSSH) PRIVATE KEY" backend/.env.backup.example docs/operations/purge-object-backup-restore-runbook.md docs/operations/templates/purge-object-restore-drill-record.md`

Expected: no secret or DSN match.

- [ ] **Step 6: Diff and parent-worktree safety checks**

Run: `git diff --check && git status --short`

Expected: only inherited #24 files plus Issue #25 files are modified/untracked. Recompute the parent #24 file hashes and confirm `/Users/zhangyichi/.codex/worktrees/b986/xiangyi_image_search` remains unchanged.

- [ ] **Step 7: Record unverified gates**

Final report must keep real PostgreSQL reference transactions, real OSS copies, Bucket ACL/SSE, IAM no-Delete, 30-day lifecycle/Object Lock, real isolated restore, deployment, worker composition, authentication, permanent deletion, and merge behavior explicitly unverified. No commit step is present because the user prohibited commit/push.
