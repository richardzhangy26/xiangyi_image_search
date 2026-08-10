# Issue #19 Persistent Image Imports Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development and execute this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a persistent, restartable unassigned-image import path that returns after validated private OSS staging, lets an independent PostgreSQL-backed worker create embeddings, and promotes only validated results into searchable image assets.

**Architecture:** Keep the accepted synchronous product-image path as a compatibility interface and add a separate `/api/image-imports` interface for unassigned imports. The existing ingest module owns deterministic validation, normalization, no-overwrite object staging, and source-identity decisions; a worker module claims persisted items with `FOR UPDATE SKIP LOCKED`, calls the embedding adapter outside a transaction, and atomically promotes an asset plus the completed task behind a claim-token fence.

**Tech Stack:** Python 3.13, Flask, Flask-SQLAlchemy, PostgreSQL 16, pgvector, private Aliyun OSS, DashScope embedding, React 18, TypeScript, Ant Design 5, pytest, Vitest.

## Global Constraints

- Model is exactly `tongyi-embedding-vision-plus-2026-03-06`; dimension is exactly `1024`; vector elements must be finite numbers.
- New imports are unassigned: successful assets have `model_number = NULL` and become visible in the active unassigned list and pgvector search.
- Source identity is exactly `(source_provider, source_bucket, source_relative_path, source_revision)`.
- Same identity and same content is idempotent; same identity and different content is a source conflict; archived asset hits return the recycle-bin outcome and never restore; different paths with equal content remain distinct assets.
- HTTP validates and normalizes images, writes private OSS original/preview objects with no-overwrite semantics, persists queued items, and never calls embedding.
- Reliable work lives only in PostgreSQL. No request thread, background Flask thread, Redis/in-memory queue, or browser state is authoritative.
- Worker claims are short transactions. Embedding is called outside database transactions. Promotion creates/reuses the formal asset and marks the item completed in one transaction.
- Lease reclamation and claim tokens exist only for worker crash recovery and multi-instance fencing. Do not add #20 retry counts, backoff, retry APIs, next-attempt times, or error classification; do not add #21 cancel state, cancel APIs, or UI actions.
- Failed items never produce formal assets, null vectors, placeholder vectors, non-finite vectors, incompatible models, or incompatible dimensions.
- Do not delete or clean staged objects. Do not overwrite, expose public URLs, migrate Kodo, touch the retired compatibility table, or add permanent purge.
- Do not connect PostgreSQL, OSS, Kodo, or DashScope; do not execute migrations, integration tests, deployment, cloud operations, delete, commit, push, or modify GitHub.
- Real PostgreSQL concurrency and end-to-end scenarios are written but explicitly remain unexecuted.

## File Map

| File | Responsibility |
|---|---|
| `backend/models/image_import_item.py` | Persistent item metadata, four states, claim lease, asset link, and constraints. |
| `backend/migrations/issue_19_image_import_items.py` | Explicit, idempotent, expand-only PostgreSQL migration. |
| `backend/services/upload_source.py` | Shared deterministic multipart-to-read-only-source construction. |
| `backend/services/asset_ingest.py` | Add the deep `queue_one` interface while preserving synchronous ingest compatibility. |
| `backend/services/image_import_worker.py` | Claim, embedding validation, failure transition, atomic promotion, processing-to-idle, and structured observations. |
| `backend/services/object_storage.py` | Private preview download adapter used by the worker. |
| `backend/services/embedding.py` | Model-bearing embedding result contract. |
| `backend/blueprints/image_imports.py` | Multipart create plus persisted list/detail transport adapter. |
| `backend/scripts/run_image_import_worker.py` | Independent worker process loop and graceful stop. |
| `postgres/init/01_init.sql` | Fresh-install schema kept equal to ORM/migration. |
| `docker-compose.yml` | Independent restartable worker service using the backend image. |
| `frontend/src/components/ImageImportTaskDrawer.tsx` | Basic persisted task drawer with server statuses and no retry/cancel controls. |
| `frontend/src/components/ProductUpload.tsx` | Unassigned import entry, unresolved badge, polling/display refresh, and completed-asset refresh. |
| `frontend/src/services/productApi.ts` | Import create/list/detail transport. |
| `frontend/src/types/product.ts` | Import item and response wire contracts. |

---

### Task 1: Persistent schema and explicit migration

**Files:**
- Create: `backend/models/image_import_item.py`
- Modify: `backend/models/__init__.py`
- Create: `backend/migrations/issue_19_image_import_items.py`
- Modify: `postgres/init/01_init.sql`
- Create: `backend/test/test_issue_19_schema_static_contract.py`
- Create: `backend/test/integration/test_issue_19_migration.py`

**Interfaces:**
- Produces: `ImageImportItem` with `queued | embedding | completed | failed`.
- Produces: `apply_migration(connection) -> None`, called only by an explicit `--apply` command.

- [ ] **Step 1: Write the failing static and unexecuted migration contracts**

  Assert the ORM, fresh SQL, and migration all contain the four-state check, four-column source identity unique constraint, exact model/dimension, nullable formal `asset_id`, claim token/generation/lease fields, timestamps, and indexes for claim order/status. Assert `app.py`, health checks, and worker entry do not call migration code. The integration test applies the migration twice in a random PostgreSQL schema and verifies the second run does not alter rows.

- [ ] **Step 2: Verify RED**

  Run:

  ```bash
  cd backend
  python -m pytest test/test_issue_19_schema_static_contract.py -v
  ```

  Expected: FAIL because the model, migration, table, and constraints do not exist. Do not collect the integration test.

- [ ] **Step 3: Implement the minimal expand-only schema**

  Required persistent fields:

  ```text
  id, source_provider, source_bucket, source_relative_path, source_revision,
  display_name, oss_path, preview_oss_path, content_hash, source_size,
  source_mime_type, source_width, source_height, normalization_version,
  expected_embedding_model, expected_embedding_dimension, status, asset_id,
  request_id, claim_token, claim_generation, claimed_by, claimed_at,
  lease_expires_at, embedding_started_at, completed_at, failed_at,
  failure_message, created_at, updated_at
  ```

  `failure_message` is a bounded, sanitized summary and is not an error category. `failed` is never claimable. The migration CLI requires `--apply`; no down migration drops data.

- [ ] **Step 4: Verify GREEN and formatting**

  Re-run the static test and `git diff --check`. Keep the PostgreSQL migration scenario written but unexecuted.

---

### Task 2: Queue validated private uploads without embedding

**Files:**
- Create: `backend/services/upload_source.py`
- Modify: `backend/services/asset_ingest.py`
- Modify: `backend/blueprints/products_v2.py`
- Create: `backend/blueprints/image_imports.py`
- Modify: `backend/app.py`
- Create: `backend/test/test_issue_19_import_queue_unit.py`
- Create: `backend/test/test_issue_19_api_static_contract.py`
- Modify: `backend/test/test_issue_18_static_contract.py`

**Interfaces:**
- Produces: `ImageAssetIngestService.queue_one(source_relative_path, *, request_id, commit) -> ImageImportQueueResult`.
- Produces: `POST /api/image-imports` returning `202` when at least one persistent item is queued.
- Produces: `GET /api/image-imports` and `GET /api/image-imports/<uuid:item_id>` from persisted state.

- [ ] **Step 1: Write a failing queue behavior test**

  Use a real deterministic normalizer with in-memory source bytes plus fake private storage/session. Assert one valid image writes original and preview, creates a queued item, and never calls the embedding fake. Assert no `ImageAsset` is created.

- [ ] **Step 2: Verify queue RED**

  Run:

  ```bash
  cd backend
  python -m pytest test/test_issue_19_import_queue_unit.py -v
  ```

  Expected: FAIL because `queue_one` and `ImageImportItem` are missing.

- [ ] **Step 3: Implement the minimal deep queue interface**

  Reuse `_prepare_one` for source download, safe decoding, normalization, exact OSS metadata, no-overwrite writes, active/archived formal-asset decisions, and compatible preview reuse. Persist the queued item with `model_number` absent. Flush through a savepoint so a database unique race can re-read the canonical item; same task identity/content returns `existing_task`, while different content raises the existing safe source conflict.

- [ ] **Step 4: Add RED→GREEN source-identity cases**

  Add one test at a time for:

  - same task identity/content is stable and does not upload or enqueue twice;
  - same identity/different content conflicts without overwrite;
  - archived formal asset returns recycle-bin navigation and no task;
  - different paths/equal content create distinct queued items;
  - unique insert race converges to the committed same-content item;
  - queue commit/activity failure rolls back the item;
  - response/task representations contain no OSS keys, vector, signature, credentials, or raw image bytes.

- [ ] **Step 5: Add the multipart/list/detail transport**

  The POST accepts 1–20 files under `images`, creates deterministic `imports/<occurrence>/<filename>` source paths, calls only `queue_one`, commits queued items, and maps invalid image/storage/source conflict safely. Listing returns newest-first persisted state plus `unresolved_count` (`queued + embedding + failed`) and `processing_count` (`queued + embedding`). Detail returns 404 for missing items. No endpoint mutates failed/completed tasks.

- [ ] **Step 6: Keep #18 compatibility explicit**

  Extract shared multipart source construction without changing current `prepare_product_uploads` output. Update the superseded #18 static prohibition so it still rejects retry/cancel/delete scope but permits #19 import-item references. Re-run all #18 pure contracts.

---

### Task 3: Private preview read and model-bearing embedding contract

**Files:**
- Modify: `backend/services/object_storage.py`
- Modify: `backend/services/embedding.py`
- Modify: `backend/test/test_object_storage.py`
- Create: `backend/test/test_issue_19_embedding_contract.py`

**Interfaces:**
- Produces: `ObjectReader.download_file(key, target_path) -> None`.
- Produces: `EmbeddingResult(model: str, vector: Sequence[float])` and `EmbeddingClient.embed_normalized_image_result(...)`.

- [ ] **Step 1: Write failing adapter contracts**

  Assert private OSS download writes only the requested target and maps SDK errors to sanitized `ObjectStorageError`. Assert the embedding result carries the exact requested model and vector while preserving existing synchronous methods.

- [ ] **Step 2: Verify RED**

  Run the two focused files; expect missing download/result interfaces.

- [ ] **Step 3: Implement minimal adapters and verify GREEN**

  Do not sign public URLs or persist signed URLs. Do not change DashScope retry behavior in this Ticket; #20 owns persistent retry policy.

---

### Task 4: Transactional multi-worker success and failure chain

**Files:**
- Create: `backend/services/image_import_worker.py`
- Create: `backend/scripts/run_image_import_worker.py`
- Create: `backend/test/test_issue_19_worker_unit.py`
- Create: `backend/test/test_issue_19_worker_static_contract.py`
- Create: `backend/test/integration/test_issue_19_async_import.py`

**Interfaces:**
- Produces: `claim_next_import_item(session, *, worker_id, lease_seconds) -> ClaimedImportItem | None`.
- Produces: `ImageImportWorker.process_one() -> bool`.
- Produces: `ImageImportWorker.process_until_idle(max_items: int | None = None) -> int`.

- [ ] **Step 1: Write a failing claim contract**

  Assert claim ordering is `created_at, id`, SQL uses `FOR UPDATE SKIP LOCKED`, and the claim transaction commits `embedding`, a new token, incremented generation, owner, and lease before returning. Eligible rows are queued or expired embedding rows without assets; failed/completed rows are excluded.

- [ ] **Step 2: Verify claim RED, then implement and verify GREEN**

  Lease reclamation is infrastructure crash recovery only. Do not add attempts, backoff, retry scheduling, error classes, cancel intent, or user operations.

- [ ] **Step 3: Write failing success-chain tests**

  Through the public worker interface and stateful fake repository/storage/embedding adapters, assert:

  - the database claim is committed before preview download/embedding;
  - embedding runs with no open database transaction;
  - exact model, 1024 finite floats are required;
  - promotion creates one active unassigned formal asset and completes the task in one commit;
  - task activity stores safe state only;
  - a stale claim token cannot promote.

- [ ] **Step 4: Implement minimal worker and atomic promotion**

  Promotion locks the item and verifies `status='embedding'` plus token. It inserts the full non-null `ImageAsset`, flushes it, assigns `asset_id`, sets completed fields, clears active lease fields, adds activity, and commits once. An `image_assets` source-identity uniqueness race uses a savepoint and canonical winner re-read; same-content active winners complete the task, archived winners bind without restoration and remain discoverable only through task/recycle-bin status, different-content winners fail safely.

- [ ] **Step 5: Add RED→GREEN failure tests**

  Cover embedding exception, wrong model, wrong dimension, NaN/Infinity, preview download error, asset flush error, task update/activity error, and commit error. Every case must observe zero formal asset after rollback. After a processing failure, rollback first and mark the still-owned item failed in a new short transaction; if that transaction fails, leave it embedding for lease recovery.

- [ ] **Step 6: Write but do not execute real PostgreSQL scenarios**

  Integration tests cover two workers claiming different items, one item never being claimed twice before lease expiry, expired-lease takeover with stale-token fencing, HTTP queue → process-until-idle → completed query → active unassigned/vector result, unique source race, and promotion rollback. Use fake OSS/DashScope. Mark the file PostgreSQL-only and do not collect it in current verification.

---

### Task 5: Independent worker topology and observations

**Files:**
- Modify: `docker-compose.yml`
- Modify: `backend/scripts/run_image_import_worker.py`
- Modify: `backend/test/test_issue_19_worker_static_contract.py`

**Interfaces:**
- Produces: an independent `worker` Compose service using the backend image and database/environment configuration.

- [ ] **Step 1: Write failing topology/log contracts**

  Assert the worker is not a Gunicorn thread, has a dedicated command, depends on healthy PostgreSQL, restarts safely, and logs task ID, worker ID, claim generation, queue depth, queue latency, embedding duration, total duration, and failure exception type without credentials/object signatures/raw provider bodies.

- [ ] **Step 2: Implement the worker loop and Compose service**

  The loop stops claiming on SIGTERM/SIGINT, lets the current call finish when possible, polls PostgreSQL, and derives queue depth from persisted counts rather than process memory. Do not run Compose or deploy.

- [ ] **Step 3: Verify static contracts and Python compilation**

  Compile changed Python files without importing production app configuration, then run the focused static tests.

---

### Task 6: Persisted task UI and refresh behavior

**Files:**
- Modify: `frontend/src/types/product.ts`
- Modify: `frontend/src/services/productApi.ts`
- Modify: `frontend/src/services/productApi.test.ts`
- Create: `frontend/src/components/ImageImportTaskDrawer.tsx`
- Create: `frontend/src/components/ImageImportTaskDrawer.test.tsx`
- Modify: `frontend/src/components/ProductUpload.tsx`
- Modify: `frontend/src/components/ProductUpload.test.tsx`
- Modify if needed: `frontend/src/index.css`

**Interfaces:**
- Produces: `createImageImports(files)`, `getImageImportItems(params)`, and `getImageImportItem(id)`.
- Produces: a product-management import modal, unresolved badge, and persisted task drawer.

- [ ] **Step 1: Write failing transport tests**

  Assert multipart POST uses `/api/image-imports`, list/detail encode pagination, and response parsing preserves all documented fields. Non-2xx errors expose only the safe server message.

- [ ] **Step 2: Verify transport RED, implement, and verify GREEN**

  Add exact status unions for queued, embedding, completed, failed. No retry/cancel types or methods.

- [ ] **Step 3: Write failing drawer tests**

  Render the real drawer and assert status labels, source display name, timestamps, safe failure text, completed asset navigation information, loading/error/empty states, and absence of retry/cancel/abandon controls.

- [ ] **Step 4: Implement the basic drawer and verify GREEN**

  Keep it presentation-only; `ProductUpload` owns fetching and refresh orchestration.

- [ ] **Step 5: Write failing top-level user flows**

  Assert initial mount requests persisted task state; unresolved badge survives a remount; importing files returns queued tasks and opens the drawer; queued/embedding items cause display-only polling; polling stops when no processing items remain; a newly completed item refreshes the unassigned assets list; failed items remain visible but do not poll forever; refresh button reloads real server state.

- [ ] **Step 6: Implement the ProductUpload orchestration and verify GREEN**

  The browser never advances task state. Polling only reads the API. Submitting a new import does not call product create/update and does not wait for embedding.

---

### Task 7: Documentation, regression verification, and safety audit

**Files:**
- Modify: `AGENTS.md`
- Verify: all #15–#19 changed files.

**Interfaces:**
- Produces: current architecture facts, fresh test/build evidence, and an explicit unverified-real-scenarios list.

- [ ] **Step 1: Update current architecture facts**

  Record the persistent import table, independent worker, upload/list endpoints, claim/promotion boundaries, and the fact that the old product synchronous upload remains a compatibility interface. Do not record implementation history.

- [ ] **Step 2: Run focused backend tests**

  Run all new #19 pure unit/mock/static files plus the inherited ten-file parent suite. Do not include `test/integration`.

- [ ] **Step 3: Run focused frontend tests and production build**

  Run `productApi`, `ProductUpload`, `ImageImportTaskDrawer`, `ArchivedAssetGrid`, `UnassignedAssetGrid`, `AssetDisplayNameEditor`, and `ProductSearch`, then `npm run build`.

- [ ] **Step 4: Run compile and diff checks**

  Run Python compile checks for changed files and `git diff --check`.

- [ ] **Step 5: Perform the strict safety scan**

  Inspect the Issue #19 delta for database/cloud calls in tests, migration invocation, request threads/in-memory queues, delete/overwrite/public URL, Kodo write, placeholder/null/wrong vectors, retry/backoff/error classification, cancel/abandon state, object cleanup, permanent purge, secrets/signatures/raw image/vector activity fields, and changes to the parent/main worktrees.

- [ ] **Step 6: Report without formal code review or Git actions**

  List parent-manifest proof, files changed relative to the parent delta, exact fresh test/build output, written-but-unexecuted PostgreSQL/migration/OSS/DashScope scenarios, safety results, and conflict status. Do not commit, push, open PRs, or modify Issues.

## Plan Self-Review

- **Spec coverage:** Persistent states, fast no-embedding HTTP, PostgreSQL multi-worker claims, restart leases, exact model/dimension validation, atomic promotion, failure zero-assets, query APIs, badge/drawer, worker topology, observations, source identity, and all explicit exclusions map to tasks above.
- **Placeholder scan:** No TBD/TODO/unspecified error-handling steps remain; each behavior has a named interface, exact state, transaction boundary, and verification command.
- **Type consistency:** Backend and frontend use `queued | embedding | completed | failed`; `asset_id` is nullable until completed; `unresolved_count` and `processing_count` have distinct meanings; claim token/generation never appear as user actions.
- **Architect decision:** A separate unassigned import interface avoids corrupting the accepted product-upload compatibility contract while meeting the explicit “successful assets enter unassigned search” requirement.
