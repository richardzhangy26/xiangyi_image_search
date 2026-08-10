# Issue #18 Source-Identity Ingest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development and execute this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing synchronous image ingest path return stable source-identity outcomes for created, idempotent, source-conflict, and recycle-bin cases without overwriting objects or restoring archived assets.

**Architecture:** Deepen the existing `ImageAssetIngestService` interface instead of adding a pass-through module. It owns source-identity lookup, content comparison, OSS race reconciliation, and database uniqueness-race reconciliation; the product blueprint maps results to HTTP, while `ProductUpload` renders those stable results and owns navigation to the existing recycle-bin view.

**Tech Stack:** Flask, SQLAlchemy/PostgreSQL contracts, private OSS adapter, React 18, TypeScript, Ant Design, pytest, Vitest.

## Global Constraints

- Source identity is exactly `(source_provider, source_bucket, source_relative_path, source_revision)`; `source_revision` remains `1` in the current source adapters.
- Same identity and same content is idempotent; same identity and different content is a source conflict.
- Archived identity hits return `in_recycle_bin` and never change lifecycle, model assignment, OSS keys, preview, vector, or timestamps.
- Different source paths with identical content create independent asset IDs; compatible preview and vector data may be reused by content hash.
- Object writes remain private and no-overwrite; a concurrent forbid-overwrite response is reusable only after a fresh HEAD matches the complete expected object contract.
- The database unique constraint on source identity is the final concurrency guard; a uniqueness race must converge to an existing/recycle-bin result or a source conflict.
- Do not infer `model_number` from a path.
- Do not implement persistent import items/workers (#19), retry policy (#20), or cancellation (#21).
- Do not connect PostgreSQL, OSS, Kodo, or DashScope; only unit/mock/static/frontend tests may run.
- Do not commit, push, deploy, or modify GitHub state.

---

### Task 1: Stable ingest domain outcomes

**Files:**
- Modify: `backend/services/asset_ingest.py`
- Test: `backend/test/test_issue_18_source_identity_unit.py`
- Test-only contract: `backend/test/integration/test_asset_ingest.py`

**Interfaces:**
- Consumes: `ImageAssetIngestService.ingest_one(...) -> AssetIngestResult`.
- Produces: `AssetIngestResult.status` in `created | existing | in_recycle_bin`; source conflicts remain `AssetIngestConflictError(kind='source_conflict')` with safe source-result metadata.

- [ ] **Step 1: Write failing unit tests for source decisions**

  Cover active same-content, archived same-content, and different-content decisions. Assert archived results contain the stable asset ID and `recycle_bin` action, and assert no lifecycle or assignment field changes.

- [ ] **Step 2: Run the focused test and verify RED**

  Run: `python -m pytest test/test_issue_18_source_identity_unit.py -q`

  Expected: failures because archived assets currently return `existing`, conflict metadata is absent, and product attachment reactivates archived assets.

- [ ] **Step 3: Implement the minimal source-result mapping**

  Extend the result/error value objects with safe recovery metadata, centralize the status mapping inside `ImageAssetIngestService`, and keep source lookup keyed by all four identity columns.

- [ ] **Step 4: Add unexecuted PostgreSQL scenarios**

  Update integration expectations so archived re-upload stays archived, and add coverage for distinct paths with identical bytes, same-identity conflicts, and concurrent uniqueness convergence. Mark no scenario as executed in the final report.

- [ ] **Step 5: Run focused unit/static tests and verify GREEN**

  Run: `python -m pytest test/test_issue_18_source_identity_unit.py test/test_issue_18_static_contract.py -q`

---

### Task 2: Concurrency and no-overwrite reconciliation

**Files:**
- Modify: `backend/services/asset_ingest.py`
- Test: `backend/test/test_issue_18_source_identity_unit.py`

**Interfaces:**
- Consumes: the existing `ObjectWriter` HEAD and forbid-overwrite write operations, plus `uq_image_assets_source_identity`.
- Produces: a stable idempotent/recycle-bin result after an OSS or database race only when the winning object/row matches the requested source identity and content.

- [ ] **Step 1: Write failing OSS-race test**

  Use an in-memory writer whose first write raises `ObjectStorageConflictError` after installing a matching object. Assert the ingest helper performs a fresh HEAD and returns reuse; install a mismatching object and assert a conflict without another PUT.

- [ ] **Step 2: Verify OSS-race RED**

  Run the single race test and confirm the current implementation raises immediately.

- [ ] **Step 3: Implement post-conflict HEAD reconciliation**

  On forbid-overwrite conflict, perform one fresh HEAD through the existing full metadata/size/type/ETag matcher. Return `reused` only on an exact match; otherwise preserve the conflict.

- [ ] **Step 4: Write failing database-race test**

  Use a fake session/savepoint and source-identity lookup to simulate a unique violation followed by a committed winner. Assert one stable result, no duplicate asset result, and a conflict when the winner has different content.

- [ ] **Step 5: Implement uniqueness-race convergence**

  Isolate the candidate asset insert in a nested transaction, catch only `IntegrityError`, then re-read by all four source-identity fields and reapply the same content/lifecycle decision. Other database failures stay failures and roll back according to the existing `commit` contract.

- [ ] **Step 6: Run focused tests and refactor**

  Keep the public ingest interface unchanged and remove duplicated decision logic after all focused tests are green.

---

### Task 3: Product HTTP result contract

**Files:**
- Modify: `backend/blueprints/products_v2.py`
- Test: `backend/test/test_issue_18_product_upload_unit.py`
- Test-only contract: `backend/test/integration/test_write_paths.py`

**Interfaces:**
- Consumes: `AssetIngestResult` and `AssetIngestConflictError`.
- Produces: per-image `created | existing | in_recycle_bin` results, counts for created/reused/recycle-bin outcomes, and `409 IMAGE_ASSET_SOURCE_CONFLICT` with a safe per-image `source_conflict` result.

- [ ] **Step 1: Write failing adapter tests**

  Assert active existing results may be explicitly associated, archived results are returned but never attached/reactivated, summaries count each stable status separately, and source conflicts map to the dedicated error code.

- [ ] **Step 2: Verify RED**

  Run: `python -m pytest test/test_issue_18_product_upload_unit.py -q`

- [ ] **Step 3: Implement minimal HTTP mapping**

  Preserve legacy count fields, add `recycle_bin_images`, include `recovery_action: {type: 'open_recycle_bin', asset_id}` for archived hits, and never write `status='active'` or clear `archived_at` for those hits.

- [ ] **Step 4: Update unexecuted integration expectations**

  Replace the old automatic-reactivation test with a recycle-bin result test and add safe conflict response assertions.

- [ ] **Step 5: Run backend focused suite**

  Run all new Issue #18 unit/static files plus the six inherited backend files; expect no PostgreSQL connection attempt.

---

### Task 4: Product-upload result presentation and recycle-bin navigation

**Files:**
- Modify: `frontend/src/types/product.ts`
- Modify: `frontend/src/services/productApi.ts`
- Modify: `frontend/src/components/ProductUpload.tsx`
- Test: `frontend/src/services/productApi.test.ts`
- Test: `frontend/src/components/ProductUpload.test.tsx`

**Interfaces:**
- Consumes: the product write summary and dedicated source-conflict response.
- Produces: readable created/idempotent/conflict/recycle-bin feedback and a `前往回收站` action that switches to the existing archived view without restoring anything.

- [ ] **Step 1: Write failing transport tests**

  Assert `createProduct`/`updateProduct` preserve the complete write summary and throw a typed error retaining `error_code` and safe `image_results` for a source conflict.

- [ ] **Step 2: Verify transport RED**

  Run the focused product API test file and confirm the typed error/result fields are missing.

- [ ] **Step 3: Implement transport types**

  Expand `ProductImageWriteResult` to the four client-visible statuses, add recovery action and recycle-bin count fields, and introduce a typed product-write error without changing endpoints.

- [ ] **Step 4: Write failing top-level UI tests**

  Through the real `ProductUpload` component with fake API responses, assert distinct feedback for created, existing, source conflict, and recycle-bin outcomes. Click `前往回收站` and assert the archived view becomes active and no restore API is called.

- [ ] **Step 5: Implement UI feedback and navigation**

  Retain a recycle-bin notice after the form closes, show the dedicated action, clear/select existing view state safely, and route source-conflict errors to readable copy while leaving the edit form open.

- [ ] **Step 6: Run focused Vitest and refactor**

  Run the API and ProductUpload test files; keep navigation owned by `ProductUpload`, not the leaf archived grid.

---

### Task 5: Verification and safety audit

**Files:**
- Modify only if architecture facts changed: `AGENTS.md`

**Interfaces:**
- Consumes: all implementation and test results.
- Produces: fresh evidence for behavior, regressions, build, formatting, and prohibited-operation boundaries.

- [ ] **Step 1: Run new backend tests**

  Run the Issue #18 pure unit/mock/static contract files only.

- [ ] **Step 2: Run inherited backend regression tests**

  Run the six inherited #15–#17 files and any existing pure ingest/object-storage tests that do not load PostgreSQL credentials.

- [ ] **Step 3: Run frontend tests**

  Run product API, ProductUpload, ArchivedAssetGrid, UnassignedAssetGrid, AssetDisplayNameEditor, and ProductSearch test files.

- [ ] **Step 4: Build and whitespace-check**

  Run `npm run build` and `git diff --check`.

- [ ] **Step 5: Audit safety boundaries**

  Inspect the diff for `DELETE`, overwrite, automatic restore, cloud clients, credential reads, worker/import-item/retry/cancel scope, path-based model inference, and public URL construction. Report real PostgreSQL concurrency/transaction behavior and real OSS races as written-but-unexecuted.

- [ ] **Step 6: Report without committing**

  List parent-delta proof, changed files, fresh raw test/build results, unverified integration scenarios, and conflict status. Do not commit or modify GitHub.
