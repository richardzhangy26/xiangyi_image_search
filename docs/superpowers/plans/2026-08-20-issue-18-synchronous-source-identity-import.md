# Issue #18 Synchronous Source-Identity Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task after the main thread explicitly authorizes execution and creates an isolated worktree with superpowers:using-git-worktrees. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct the existing synchronous `POST /api/image-assets/import` compatibility endpoint so source identity—not global content hash—determines idempotency, conflicts, recycle-bin hits, and distinct assets for identical content at different paths.

**Architecture:** Keep the blueprint thin: it validates the multipart request, builds one `InMemoryObjectSource`, delegates every unique path to `ImageAssetIngestService.ingest_many()`, and maps stable service outcomes to HTTP. Preserve per-item commits and no-overwrite object semantics; prove concurrency through two real HTTP requests on distinct PostgreSQL backend connections before considering any production transaction change.

**Tech Stack:** Python 3, Flask, Flask-SQLAlchemy, PostgreSQL 16, pgvector, pytest, React 18, TypeScript, Ant Design, Vitest, Vite.

## Global Constraints

- The sole design input is `/tmp/issue-18-design-rev2.md`, approved by the architect.
- Execution must start from `refactor/image-search-pgvector` at `b0e505db22e75c646bd40fec86f06b1ec4e51f99`, which contains the `b0e505d` integration-fixture credential fix.
- Do not create the worktree while writing this plan. After main-thread approval, invoke `superpowers:using-git-worktrees`, use the already ignored `.worktrees/` directory, and verify the isolated worktree is based on the exact approved commit before editing.
- Preserve the main checkout's untracked `.claude/agents/`, `frontend/tsconfig.app.tsbuildinfo`, and `frontend/tsconfig.node.tsbuildinfo`; do not copy, remove, or stage them.
- Do not commit, push, merge, open a PR, or modify GitHub state without explicit main-thread authorization. Task checkpoints replace commit steps in this plan.
- Keep `POST /api/image-imports` as the default reliable asynchronous entry point. The synchronous exception is limited to the already existing `POST /api/image-assets/import` URL and is not precedent for another request-time embedding endpoint.
- Source identity is exactly `(source_provider, source_bucket, source_relative_path, source_revision)`.
- Same identity and same content returns `existing`; same identity and changed content returns `source_conflict`; an archived same-identity/same-content hit returns `in_recycle_bin` without restoration; different paths with the same content create distinct asset IDs and search results.
- Content hash may select a compatible preview/vector reuse candidate, but it must never determine asset uniqueness.
- Keep the embedding contract `tongyi-embedding-vision-plus-2026-03-06`, dimension `1024`, and finite-vector validation unchanged.
- Keep Kodo read-only. Do not modify Kodo code or Kodo test expectations.
- Do not add schema changes, migrations, advisory locks, isolation-level changes, object deletion, automatic restoration, cleanup, deployment, or real OSS/Kodo/DashScope calls.
- Legal synchronous batches commit per item and are not atomic. A top-level `500/503` may follow earlier successful item commits; retry by identical source identity is the only supported recovery.
- `skipped_count` remains a deprecated response field fixed at `0`; `skipped_duplicate_content` is removed from item statuses.
- Preserve every existing exact assertion in `backend/test/test_issue_18_static_contract.py::test_repository_guidance_records_source_identity_and_recycle_bin_semantics` unless a separately reviewed plan revision explicitly lists the assertion change and reason.
- A valid concurrency test must prove two distinct `pg_backend_pid()` values and barrier overlap. If it fails after the fixture is proven valid, stop implementation, retain the exact RED evidence, invoke the repository's diagnosis workflow, and revise this plan before editing `backend/services/asset_ingest.py`.

## File Map

| File | Responsibility in this ticket |
| --- | --- |
| `backend/blueprints/image_assets.py` | Remove content-hash prefiltering and map stable service outcomes/counts for the synchronous endpoint. |
| `backend/test/integration/test_image_asset_import.py` | Sequential HTTP contracts, no-overwrite fake storage, orphan-object recovery, and concurrent HTTP acceptance tests. |
| `backend/test/integration/conftest.py` | Reusable temporary-schema Engine helper and a multi-connection `concurrent_app` fixture with symmetric teardown. |
| `backend/test/integration/test_asset_ingest.py` | Remove the invalid single-connection threaded concurrency test after HTTP coverage replaces it. |
| `backend/test/integration/test_vector_search.py` | Existing distinct-search-result regression; verification only. |
| `backend/test/test_issue_18_static_contract.py` | Preserve existing source-identity phrases and add the bounded synchronous-exception documentation contract. |
| `frontend/src/types/product.ts` | Replace the skipped status with recycle-bin status and define the complete response shape. |
| `frontend/src/services/productApi.test.ts` | Lock the synchronous transport contract and deprecated zero count. |
| `frontend/src/components/ImportImagesModal.tsx` | Present five outcomes, retry returned item failures, and expose recycle-bin navigation. |
| `frontend/src/components/ImportImagesModal.test.tsx` | TDD coverage for five outcomes, item/transport retry, and recycle navigation. |
| `frontend/src/components/ProductUpload.tsx` | Wire `ImportImagesModal.onOpenRecycleBin` to the existing top-level navigation function. |
| `frontend/src/components/ProductUpload.test.tsx` | Prove the modal callback switches views and never calls restore. |
| `docs/adr/0006-asynchronous-embedding-before-asset-creation.md` | Append the narrowly scoped synchronous compatibility exception. |
| `AGENTS.md` | Scope worker-only wording to persistent async tasks while preserving exact source-identity phrases. |

`backend/services/asset_ingest.py` is deliberately not in the scheduled modification set. Task 3 is the evidence gate that determines whether a separately reviewed transaction-fix plan is needed.

---

### Task 1: Synchronous HTTP source-identity contract

**Files:**
- Modify: `backend/test/integration/test_image_asset_import.py`
- Modify: `backend/blueprints/image_assets.py:1-541`
- Verify only: `backend/services/asset_ingest.py:421-548`

**Interfaces:**
- Consumes: `ImageAssetIngestService.ingest_many(source_relative_paths, model_number=None, request_id=...) -> list[AssetIngestResult]` in input order.
- Produces: `POST /api/image-assets/import` items with `status` in `created | existing | source_conflict | in_recycle_bin | failed`, fixed nullable `recovery_action`, and counts `created_count`, `existing_count`, `conflict_count`, `recycle_bin_count`, `failed_count`, `skipped_count`.

- [ ] **Step 1: Make the fake object store enforce no-overwrite semantics**

In `backend/test/integration/test_image_asset_import.py`, add `hashlib`, `threading`, and `ObjectStorageConflictError`; replace `FakeImportStorage` with a locked put-if-absent fake that records writes:

```python
import hashlib
import threading

from services.object_storage import (
    ObjectStorageConflictError,
    SignedDownloadUrl,
    StoredObject,
)


class FakeImportStorage:
    def __init__(self):
        self.objects: dict[str, _FakeStoredObject] = {}
        self.put_calls: list[str] = []
        self._lock = threading.RLock()

    def head_object(self, key):
        with self._lock:
            item = self.objects.get(key)
            if item is None:
                return None
            return StoredObject(
                key=key,
                size=len(item.data),
                content_type=item.content_type,
                metadata=dict(item.metadata),
                etag=item.etag,
            )

    def put_file(self, key, source_path, *, spec):
        with open(source_path, 'rb') as source:
            self._put(key, source.read(), spec)

    def put_bytes(self, key, data, *, spec):
        self._put(key, data, spec)

    def sign_download_url(self, key, expires_seconds, *, cache_control=None):
        return SignedDownloadUrl(
            url=f'https://private.example/{key}?expires={expires_seconds}',
            expires_at=int(time.time()) + expires_seconds,
        )

    def _put(self, key, data, spec):
        with self._lock:
            if key in self.objects:
                raise ObjectStorageConflictError(
                    f'put-if-absent conflict for {key}'
                )
            payload = bytes(data)
            assert hashlib.md5(
                payload,
                usedforsecurity=False,
            ).hexdigest() == spec.md5_hex
            self.objects[key] = _FakeStoredObject(
                data=payload,
                content_type=spec.content_type,
                metadata=dict(spec.metadata),
                etag=spec.md5_hex,
            )
            self.put_calls.append(key)
```

- [ ] **Step 2: Rewrite the old duplicate tests as failing source-identity tests**

Replace the two skipped-content tests with these exact expectations:

```python
def test_import_same_content_at_different_paths_creates_distinct_assets(app):
    storage, embedding = _install_import_dependencies(app)
    client = app.test_client()
    original = _png_bytes('red')

    first = _import_request(client, [(original, 'a.png', 'a.png')])
    second = _import_request(client, [
        (original, 'b.png', 'b.png'),
        (_png_bytes('blue'), 'c.png', 'c.png'),
    ])

    assert first.status_code == 200
    assert second.status_code == 200
    body = second.get_json()
    assert [item['status'] for item in body['items']] == ['created', 'created']
    assert body['created_count'] == 2
    assert body['existing_count'] == 0
    assert body['conflict_count'] == 0
    assert body['recycle_bin_count'] == 0
    assert body['failed_count'] == 0
    assert body['skipped_count'] == 0
    rows = ImageAsset.query.order_by(ImageAsset.source_relative_path).all()
    assert len(rows) == 3
    same_content = [row for row in rows if row.content_hash == rows[0].content_hash]
    assert len(same_content) == 2
    assert same_content[0].id != same_content[1].id
    assert same_content[0].preview_oss_path == same_content[1].preview_oss_path
    assert list(same_content[0].vector) == list(same_content[1].vector)
    assert sum(embedding.batch_calls) == 2
    assert storage.put_calls.count(same_content[0].preview_oss_path) == 1


def test_import_same_content_at_different_paths_in_one_batch_creates_distinct_assets(app):
    storage, embedding = _install_import_dependencies(app)
    client = app.test_client()
    original = _png_bytes('red')

    response = _import_request(client, [
        (original, 'a.png', 'a.png'),
        (original, 'a-copy.png', 'nested/a-copy.png'),
    ])

    assert response.status_code == 200
    body = response.get_json()
    assert [item['relative_path'] for item in body['items']] == [
        '手动导入/a.png',
        '手动导入/nested/a-copy.png',
    ]
    assert [item['status'] for item in body['items']] == ['created', 'created']
    assert body['created_count'] == 2
    assert body['skipped_count'] == 0
    rows = ImageAsset.query.order_by(ImageAsset.source_relative_path).all()
    assert len(rows) == 2
    assert rows[0].content_hash == rows[1].content_hash
    assert rows[0].preview_oss_path == rows[1].preview_oss_path
    assert list(rows[0].vector) == list(rows[1].vector)
    assert sum(embedding.batch_calls) == 1
    assert storage.put_calls.count(rows[0].preview_oss_path) == 1
```

The cross-request assertion expects two batch embedding calls: the first request generates the red vector, while the second request reuses red and generates only blue. The one-batch assertion expects one representative vector.

- [ ] **Step 3: Tighten idempotent, conflict, recycle-bin, failure, and orphan-object tests**

Update the existing same-path tests and add the following cases. Every response item must contain all five fixed fields:

```python
def _assert_count_partition(body):
    assert (
        body['created_count']
        + body['existing_count']
        + body['conflict_count']
        + body['recycle_bin_count']
        + body['failed_count']
    ) == len(body['items'])
    assert body['skipped_count'] == 0


def test_import_same_path_same_content_is_safe_to_retry(app):
    storage, embedding = _install_import_dependencies(app)
    client = app.test_client()
    original = _png_bytes('red')

    first = _import_request(client, [(original, 'a.png', 'a.png')])
    second = _import_request(client, [(original, 'a.png', 'a.png')])

    created = first.get_json()['items'][0]
    body = second.get_json()
    item = body['items'][0]
    assert item == {
        'relative_path': '手动导入/a.png',
        'status': 'existing',
        'asset_id': created['asset_id'],
        'error': None,
        'recovery_action': None,
    }
    assert body['existing_count'] == 1
    _assert_count_partition(body)
    assert ImageAsset.query.count() == 1
    assert sum(embedding.batch_calls) == 1
    assert storage.objects[ImageAsset.query.one().oss_path].data == original


def test_import_same_path_different_content_reports_source_conflict(app):
    storage, _embedding = _install_import_dependencies(app)
    client = app.test_client()
    original = _png_bytes('red')
    created = _import_request(client, [(original, 'a.png', 'a.png')])
    existing_id = created.get_json()['items'][0]['asset_id']
    original_key = ImageAsset.query.one().oss_path
    before = storage.objects[original_key]

    response = _import_request(client, [
        (_png_bytes('blue'), 'a.png', 'a.png'),
    ])

    body = response.get_json()
    assert body['items'][0] == {
        'relative_path': '手动导入/a.png',
        'status': 'source_conflict',
        'asset_id': existing_id,
        'error': '来源冲突：同一路径已存在不同内容的图片',
        'recovery_action': None,
    }
    assert body['conflict_count'] == 1
    assert body['failed_count'] == 0
    _assert_count_partition(body)
    assert ImageAsset.query.count() == 1
    assert storage.objects[original_key] == before


def test_import_archived_same_source_returns_recycle_bin_result(app):
    storage, embedding = _install_import_dependencies(app)
    client = app.test_client()
    original = _png_bytes('red')
    created = _import_request(client, [(original, 'a.png', 'a.png')])
    asset = ImageAsset.query.one()
    asset.status = 'archived'
    asset.archived_at = datetime.now()
    archived_at = asset.archived_at
    db.session.commit()
    put_count = len(storage.put_calls)

    response = _import_request(client, [(original, 'a.png', 'a.png')])

    body = response.get_json()
    asset_id = created.get_json()['items'][0]['asset_id']
    assert body['items'][0] == {
        'relative_path': '手动导入/a.png',
        'status': 'in_recycle_bin',
        'asset_id': asset_id,
        'error': None,
        'recovery_action': {
            'type': 'open_recycle_bin',
            'asset_id': asset_id,
        },
    }
    assert body['recycle_bin_count'] == 1
    _assert_count_partition(body)
    db.session.expire_all()
    unchanged = ImageAsset.query.one()
    assert unchanged.status == 'archived'
    assert unchanged.archived_at == archived_at
    assert len(storage.put_calls) == put_count
    assert sum(embedding.batch_calls) == 1


def test_import_reuses_matching_orphan_objects_without_overwrite(app):
    storage, embedding = _install_import_dependencies(app)
    client = app.test_client()
    original = _png_bytes('orange')
    original_embed = embedding.embed_normalized_images
    embedding.embed_normalized_images = lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError('temporary embedding failure')
    )

    failed = _import_request(client, [(original, 'orphan.png', 'orphan.png')])
    assert failed.get_json()['items'][0]['status'] == 'failed'
    assert ImageAsset.query.count() == 0
    stored_before_retry = dict(storage.objects)
    put_count = len(storage.put_calls)

    embedding.embed_normalized_images = original_embed
    retried = _import_request(client, [(original, 'orphan.png', 'orphan.png')])

    body = retried.get_json()
    assert body['items'][0]['status'] == 'created'
    assert body['created_count'] == 1
    _assert_count_partition(body)
    assert ImageAsset.query.count() == 1
    assert storage.objects == stored_before_retry
    assert len(storage.put_calls) == put_count


def test_import_reports_embedding_failure_as_failed_item(app):
    _storage, embedding = _install_import_dependencies(app)
    client = app.test_client()
    embedding.embed_normalized_images = lambda *args, **kwargs: [None]

    response = _import_request(client, [
        (_png_bytes('red'), 'failed.png', 'failed.png'),
    ])

    body = response.get_json()
    assert body['items'][0]['status'] == 'failed'
    assert body['items'][0]['asset_id'] is None
    assert body['items'][0]['recovery_action'] is None
    assert body['failed_count'] == 1
    assert body['conflict_count'] == 0
    _assert_count_partition(body)
    assert ImageAsset.query.count() == 0
```

Add `from datetime import datetime` to the test imports. In `test_import_creates_unassigned_asset_without_product`, add these exact response assertions after `body = response.get_json()`:

```python
assert body['created_count'] == 1
assert body['existing_count'] == 0
assert body['conflict_count'] == 0
assert body['recycle_bin_count'] == 0
assert body['failed_count'] == 0
assert body['skipped_count'] == 0
item = body['items'][0]
assert item == {
    'relative_path': '手动导入/手机挂绳/A47/修改后/2.png',
    'status': 'created',
    'asset_id': item['asset_id'],
    'error': None,
    'recovery_action': None,
}
```

Keep both request-validation tests and add the request-body limit case:

```python
def test_import_returns_json_413_before_any_write_when_body_is_too_large(app):
    storage, _embedding = _install_import_dependencies(app)
    client = app.test_client()
    app.config['MAX_CONTENT_LENGTH'] = 128

    response = _import_request(client, [
        (_png_bytes('red'), 'large.png', 'large.png'),
    ])

    assert response.status_code == 413
    assert response.get_json() == {
        'error': '上传图片过大',
        'error_code': 'IMAGE_TOO_LARGE',
    }
    assert storage.objects == {}
    assert ImageAsset.query.count() == 0
```

- [ ] **Step 4: Run the focused backend contract tests and verify RED**

Run from `backend/`:

```bash
python -m pytest \
  test/integration/test_image_asset_import.py::test_import_same_content_at_different_paths_creates_distinct_assets \
  test/integration/test_image_asset_import.py::test_import_same_content_at_different_paths_in_one_batch_creates_distinct_assets \
  test/integration/test_image_asset_import.py::test_import_same_path_same_content_is_safe_to_retry \
  test/integration/test_image_asset_import.py::test_import_same_path_different_content_reports_source_conflict \
  test/integration/test_image_asset_import.py::test_import_archived_same_source_returns_recycle_bin_result \
  test/integration/test_image_asset_import.py::test_import_reuses_matching_orphan_objects_without_overwrite \
  test/integration/test_image_asset_import.py::test_import_reports_embedding_failure_as_failed_item -v
```

Expected: failures show `skipped_duplicate_content`, missing `conflict_count`/`recycle_bin_count`/`recovery_action`, and the archived outcome being mapped as `failed`.

- [ ] **Step 5: Replace blueprint prefiltering with one stable result mapper**

In `backend/blueprints/image_assets.py`, remove `hashlib` and `AssetIngestConflictError` imports, delete the global/batch hash prefilter, and add these helpers next to `_import_item_error`:

```python
_IMPORT_ITEM_STATUSES = frozenset({
    'created',
    'existing',
    'source_conflict',
    'in_recycle_bin',
    'failed',
})


def _import_result_item(result):
    status = (
        result.status
        if result.status in _IMPORT_ITEM_STATUSES
        else 'failed'
    )
    return {
        'relative_path': result.source_relative_path,
        'status': status,
        'asset_id': result.asset_id,
        'error': (
            _import_item_error(result)
            if status in {'source_conflict', 'failed'}
            else None
        ),
        'recovery_action': (
            result.recovery_action
            if status == 'in_recycle_bin'
            else None
        ),
    }


def _import_result_counts(items):
    counts = {
        'created_count': 0,
        'existing_count': 0,
        'conflict_count': 0,
        'recycle_bin_count': 0,
        'failed_count': 0,
        'skipped_count': 0,
    }
    count_key_by_status = {
        'created': 'created_count',
        'existing': 'existing_count',
        'source_conflict': 'conflict_count',
        'in_recycle_bin': 'recycle_bin_count',
        'failed': 'failed_count',
    }
    for item in items:
        counts[count_key_by_status[item['status']]] += 1
    return counts
```

Change the source-conflict message in `_import_item_error` to:

```python
if result.status == 'source_conflict':
    return '来源冲突：同一路径已存在不同内容的图片'
```

Replace the body-processing block with:

```python
objects = {}
content_types = {}
for image_file, final_path in zip(files, cleaned_paths):
    objects[final_path] = image_file.read()
    content_types[final_path] = (
        image_file.mimetype or 'application/octet-stream'
    )

source = InMemoryObjectSource(
    source_bucket=IMPORT_SOURCE_BUCKET,
    objects=objects,
    content_types=content_types,
)
results = _import_ingest_service(source).ingest_many(
    cleaned_paths,
    model_number=None,
    request_id=uuid.uuid4().hex,
)
if len(results) != len(cleaned_paths):
    raise AssetIngestError(
        '批量导入结果数量与请求不一致',
        stage='ingest',
    )
items = [_import_result_item(result) for result in results]
return jsonify({'items': items, **_import_result_counts(items)})
```

Move the initial `request.files.getlist('images')` access into a small `try/except RequestEntityTooLarge` so body-limit errors return `_import_error('上传图片过大', 'IMAGE_TOO_LARGE', 413)`. Remove the now-unreachable top-level `except AssetIngestConflictError -> 409`; all source conflicts are per-item results, while infrastructure errors retain their existing `500/503` mappings.

- [ ] **Step 6: Run the entire synchronous endpoint file and verify GREEN**

Run from `backend/`:

```bash
python -m pytest test/integration/test_image_asset_import.py -v
```

Expected: all tests in the file pass; every successful response has a five-way count partition and `skipped_count == 0`.

- [ ] **Step 7: Review checkpoint without committing**

Inspect only the Task 1 diff:

```bash
git diff -- backend/blueprints/image_assets.py backend/test/integration/test_image_asset_import.py
git diff --check -- backend/blueprints/image_assets.py backend/test/integration/test_image_asset_import.py
```

Confirm there is no global `ImageAsset.content_hash` prefilter, no `skipped_duplicate_content`, no object overwrite, and no schema or Kodo change. Do not commit.

---

### Task 2: Frontend five-outcome presentation, item retry, and recycle navigation

**Files:**
- Modify: `frontend/src/types/product.ts:130-151`
- Modify: `frontend/src/services/productApi.test.ts`
- Modify: `frontend/src/components/ImportImagesModal.tsx`
- Modify: `frontend/src/components/ImportImagesModal.test.tsx`
- Modify: `frontend/src/components/ProductUpload.tsx:1626-1634`
- Modify: `frontend/src/components/ProductUpload.test.tsx`

**Interfaces:**
- Consumes: Task 1's complete ordered `ImageAssetImportResponse`.
- Produces: `ImportImagesModalProps.onOpenRecycleBin: () => void`, five distinct result labels/counts, item-level retry chunks, and ProductUpload-owned recycle-bin navigation.

- [ ] **Step 1: Write failing TypeScript transport and modal result tests**

Import `importImageAssets` in `frontend/src/services/productApi.test.ts` and add:

```typescript
it('preserves the complete synchronous source-identity import response', async () => {
  const response = {
    items: [{
      relative_path: '手动导入/a.png',
      status: 'in_recycle_bin' as const,
      asset_id: 'archived-18',
      error: null,
      recovery_action: {
        type: 'open_recycle_bin' as const,
        asset_id: 'archived-18',
      },
    }],
    created_count: 0,
    existing_count: 0,
    conflict_count: 0,
    recycle_bin_count: 1,
    failed_count: 0,
    skipped_count: 0,
  };
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => response,
  });
  vi.stubGlobal('fetch', fetchMock);
  const file = new File(['image'], 'a.png', { type: 'image/png' });

  await expect(importImageAssets([file], ['a.png'], '手动导入'))
    .resolves.toEqual(response);

  const body = fetchMock.mock.calls[0][1].body as FormData;
  expect(body.getAll('images')).toEqual([file]);
  expect(body.get('relative_paths')).toBe(JSON.stringify(['a.png']));
  expect(body.get('prefix')).toBe('手动导入');
  expect(response.skipped_count).toBe(0);
});
```

Replace the old skipped-summary modal test with this complete five-outcome fixture and assertions:

```typescript
it('shows idempotent, conflict, recycle-bin, and failed outcomes separately', async () => {
  vi.mocked(api.importImageAssets).mockResolvedValue({
    items: [
      {
        relative_path: '手动导入/a.png', status: 'existing',
        asset_id: 'asset-a', error: null, recovery_action: null,
      },
      {
        relative_path: '手动导入/b.png', status: 'source_conflict',
        asset_id: 'asset-b', error: '来源冲突：同一路径已存在不同内容的图片',
        recovery_action: null,
      },
      {
        relative_path: '手动导入/c.png', status: 'in_recycle_bin',
        asset_id: 'asset-c', error: null,
        recovery_action: { type: 'open_recycle_bin', asset_id: 'asset-c' },
      },
      {
        relative_path: '手动导入/d.png', status: 'failed',
        asset_id: null, error: '图片识别服务暂不可用', recovery_action: null,
      },
    ],
    created_count: 0, existing_count: 1, conflict_count: 1,
    recycle_bin_count: 1, failed_count: 1, skipped_count: 0,
  });
  renderModal();
  selectFiles([
    makeFile('a.png'), makeFile('b.png'),
    makeFile('c.png'), makeFile('d.png'),
  ]);
  fireEvent.click(screen.getByRole('button', { name: '开始导入（4 张）' }));

expect(await screen.findByText('已存在（幂等） 1')).toBeInTheDocument();
expect(screen.getByText('来源冲突 1')).toBeInTheDocument();
expect(screen.getByText('在回收站 1')).toBeInTheDocument();
expect(screen.getByText('失败 1')).toBeInTheDocument();
expect(screen.queryByText(/内容重复/)).not.toBeInTheDocument();
});
```

- [ ] **Step 2: Write failing item-retry and recycle callback tests**

Add these behaviors to `ImportImagesModal.test.tsx`:

```typescript
it('retries only response items whose status is failed', async () => {
  vi.mocked(api.importImageAssets)
    .mockResolvedValueOnce({
      items: [
        {
          relative_path: '手动导入/a.png', status: 'created',
          asset_id: 'asset-a', error: null, recovery_action: null,
        },
        {
          relative_path: '手动导入/b.png', status: 'failed',
          asset_id: null, error: '图片识别服务暂不可用', recovery_action: null,
        },
      ],
      created_count: 1, existing_count: 0, conflict_count: 0,
      recycle_bin_count: 0, failed_count: 1, skipped_count: 0,
    })
    .mockResolvedValueOnce({
      items: [{
        relative_path: '手动导入/b.png', status: 'created',
        asset_id: 'asset-b', error: null, recovery_action: null,
      }],
      created_count: 1, existing_count: 0, conflict_count: 0,
      recycle_bin_count: 0, failed_count: 0, skipped_count: 0,
    });
  renderModal();
  selectFiles([makeFile('a.png'), makeFile('b.png')]);
  fireEvent.click(screen.getByRole('button', { name: '开始导入（2 张）' }));
  fireEvent.click(await screen.findByRole('button', { name: '重试失败项' }));

  await waitFor(() => expect(api.importImageAssets).toHaveBeenCalledTimes(2));
  expect(vi.mocked(api.importImageAssets).mock.calls[1][0]
    .map((file) => file.name)).toEqual(['b.png']);
  expect(vi.mocked(api.importImageAssets).mock.calls[1][1]).toEqual(['b.png']);
});


it('closes and delegates recycle-bin navigation without restoring', async () => {
  const onClose = vi.fn();
  const onOpenRecycleBin = vi.fn();
  vi.mocked(api.importImageAssets).mockResolvedValue({
    items: [{
      relative_path: '手动导入/a.png', status: 'in_recycle_bin',
      asset_id: 'archived-a', error: null,
      recovery_action: { type: 'open_recycle_bin', asset_id: 'archived-a' },
    }],
    created_count: 0, existing_count: 0, conflict_count: 0,
    recycle_bin_count: 1, failed_count: 0, skipped_count: 0,
  });
  renderModal({ onClose, onOpenRecycleBin });
  selectFiles([makeFile('a.png')]);
  fireEvent.click(screen.getByRole('button', { name: '开始导入（1 张）' }));
  fireEvent.click(await screen.findByRole('button', { name: '前往回收站' }));

  expect(onClose).toHaveBeenCalledTimes(1);
  expect(onOpenRecycleBin).toHaveBeenCalledTimes(1);
});
```

Add this transport-failure test; it proves a top-level error retries the entire chunk:

```typescript
it('retries the entire chunk after a transport-level failure', async () => {
  vi.mocked(api.importImageAssets)
    .mockRejectedValueOnce(new Error('图片导入服务暂不可用'))
    .mockResolvedValueOnce({
      items: [
        {
          relative_path: '手动导入/a.png', status: 'existing',
          asset_id: 'asset-a', error: null, recovery_action: null,
        },
        {
          relative_path: '手动导入/b.png', status: 'created',
          asset_id: 'asset-b', error: null, recovery_action: null,
        },
      ],
      created_count: 1, existing_count: 1, conflict_count: 0,
      recycle_bin_count: 0, failed_count: 0, skipped_count: 0,
    });
  renderModal();
  selectFiles([makeFile('a.png'), makeFile('b.png')]);
  fireEvent.click(screen.getByRole('button', { name: '开始导入（2 张）' }));
  fireEvent.click(await screen.findByRole('button', { name: '重试失败项' }));

  await waitFor(() => expect(api.importImageAssets).toHaveBeenCalledTimes(2));
  expect(vi.mocked(api.importImageAssets).mock.calls[1][0]
    .map((file) => file.name)).toEqual(['a.png', 'b.png']);
  expect(vi.mocked(api.importImageAssets).mock.calls[1][1])
    .toEqual(['a.png', 'b.png']);
});
```

Update `renderModal` to pass `onOpenRecycleBin={props.onOpenRecycleBin ?? vi.fn()}`.

- [ ] **Step 3: Write a failing ProductUpload wiring test**

In `ProductUpload.test.tsx`, add this full wiring test:

```typescript
it('wires local-import recycle hits to the archived view without restore', async () => {
  vi.mocked(api.importImageAssets).mockResolvedValue({
    items: [{
      relative_path: '手动导入/a.png', status: 'in_recycle_bin',
      asset_id: 'archived-local-a', error: null,
      recovery_action: {
        type: 'open_recycle_bin', asset_id: 'archived-local-a',
      },
    }],
    created_count: 0, existing_count: 0, conflict_count: 0,
    recycle_bin_count: 1, failed_count: 0, skipped_count: 0,
  });
  render(<ProductUpload />);
  fireEvent.click(await screen.findByRole('button', { name: '本地导入' }));
  const dialog = await screen.findByRole('dialog', {
    name: '导入图片到待归款',
  });
  const fileInput = dialog.querySelector('input[type="file"]');
  if (!(fileInput instanceof HTMLInputElement)) {
    throw new Error('local import file input not found');
  }
  fireEvent.change(fileInput, {
    target: { files: [new File(['x'], 'a.png', { type: 'image/png' })] },
  });
  fireEvent.click(within(dialog).getByRole('button', {
    name: '开始导入（1 张）',
  }));
  fireEvent.click(await within(dialog).findByRole('button', {
    name: '前往回收站',
  }));

expect(await screen.findByRole('region', { name: '回收站' }))
  .toBeInTheDocument();
expect(recycleBinApi.restoreImageAssets).not.toHaveBeenCalled();
expect(recycleBinApi.getArchivedImageAssets).toHaveBeenLastCalledWith({
  page: 1,
  perPage: 24,
  search: '',
});
});
```

- [ ] **Step 4: Run the focused frontend tests and verify RED**

Run from `frontend/`:

```bash
npm test -- --run \
  src/services/productApi.test.ts \
  src/components/ImportImagesModal.test.tsx \
  src/components/ProductUpload.test.tsx
```

Expected: failures show the missing `in_recycle_bin` type/label/count, missing `onOpenRecycleBin` prop, and response-level failed items not producing retry chunks.

- [ ] **Step 5: Implement the complete transport types**

Replace the synchronous import types in `frontend/src/types/product.ts` with:

```typescript
export type ImageAssetImportItemStatus =
  | 'created'
  | 'existing'
  | 'source_conflict'
  | 'in_recycle_bin'
  | 'failed';

export interface ImageAssetImportRecoveryAction {
  type: 'open_recycle_bin';
  asset_id: string;
}

export interface ImageAssetImportItem {
  relative_path: string;
  status: ImageAssetImportItemStatus;
  asset_id: string | null;
  error: string | null;
  recovery_action: ImageAssetImportRecoveryAction | null;
}

export interface ImageAssetImportResponse {
  items: ImageAssetImportItem[];
  created_count: number;
  existing_count: number;
  conflict_count: number;
  recycle_bin_count: number;
  failed_count: number;
  /** @deprecated Kept for response compatibility; always zero. */
  skipped_count: number;
}
```

The `importImageAssets` transport remains on `/api/image-assets/import`; do not route it to `/api/image-imports`.

- [ ] **Step 6: Implement five-way display and exact failed-item retry reconstruction**

In `ImportImagesModal.tsx`, add `onOpenRecycleBin` to `ImportImagesModalProps`, destructure it, and replace `STATUS_LABELS` with:

```typescript
const STATUS_LABELS: Record<ImageAssetImportItem['status'], {
  text: string;
  color: string;
}> = {
  created: { text: '导入成功', color: 'green' },
  existing: { text: '已存在（幂等）', color: 'blue' },
  source_conflict: { text: '来源冲突', color: 'red' },
  in_recycle_bin: { text: '在回收站', color: 'orange' },
  failed: { text: '失败', color: 'red' },
};
```

After each successful response, verify cardinality and collect only returned failed indexes:

```typescript
if (response.items.length !== chunk.paths.length) {
  throw new Error('导入结果数量与请求不一致');
}
aggregated.push(...response.items);
const failedItemChunk: ImportChunk = { files: [], paths: [] };
response.items.forEach((item, itemIndex) => {
  if (item.status === 'failed') {
    failedItemChunk.files.push(chunk.files[itemIndex]);
    failedItemChunk.paths.push(chunk.paths[itemIndex]);
  }
});
if (failedItemChunk.files.length > 0) {
  failed.push(failedItemChunk);
}
```

Transport exceptions continue to add the entire `chunk`; synthetic failed results must include `recovery_action: null`. Replace summary calculation with:

```typescript
const summary = useMemo(() => {
  const counts = {
    created: 0,
    existing: 0,
    conflict: 0,
    recycleBin: 0,
    failed: 0,
  };
  results.forEach((item) => {
    if (item.status === 'created') counts.created += 1;
    else if (item.status === 'existing') counts.existing += 1;
    else if (item.status === 'source_conflict') counts.conflict += 1;
    else if (item.status === 'in_recycle_bin') counts.recycleBin += 1;
    else counts.failed += 1;
  });
  return counts;
}, [results]);
```

Render separate tags named `成功`, `已存在（幂等）`, `来源冲突`, `在回收站`, and `失败`. Rename the retry control to `重试失败项`. When `summary.recycleBin > 0`, render:

```tsx
<Button
  onClick={() => {
    onClose();
    onOpenRecycleBin();
  }}
>
  前往回收站
</Button>
```

- [ ] **Step 7: Wire ProductUpload ownership of navigation**

At the existing `ImportImagesModal` render in `ProductUpload.tsx`, add exactly:

```tsx
onOpenRecycleBin={openRecycleBinFromImport}
```

Do not import or call `restoreImageAssets` from `ImportImagesModal`; restoration remains an explicit action in the archived grid.

- [ ] **Step 8: Run focused tests and the TypeScript build and verify GREEN**

Run from `frontend/`:

```bash
npm test -- --run \
  src/services/productApi.test.ts \
  src/components/ImportImagesModal.test.tsx \
  src/components/ProductUpload.test.tsx
npm run build
```

Expected: focused tests pass and the production build exits `0`; there is no `skipped_duplicate_content` TypeScript union member or UI text.

- [ ] **Step 9: Review checkpoint without committing**

```bash
git diff -- frontend/src/types/product.ts frontend/src/services/productApi.test.ts frontend/src/components/ImportImagesModal.tsx frontend/src/components/ImportImagesModal.test.tsx frontend/src/components/ProductUpload.tsx frontend/src/components/ProductUpload.test.tsx
git diff --check -- frontend/src
```

Confirm only `failed` items and transport-failed chunks are retried, recycle navigation closes the modal, and no restore call was added. Do not commit.

---

### Task 3: Valid multi-connection concurrent HTTP gate

**Files:**
- Modify: `backend/test/integration/conftest.py`
- Modify: `backend/test/integration/test_image_asset_import.py`
- Modify: `backend/test/integration/test_asset_ingest.py:1-33,369-415`
- Do not modify unless this plan is revised after valid RED evidence: `backend/services/asset_ingest.py`

**Interfaces:**
- Consumes: Task 1's synchronous HTTP contract and test fakes.
- Produces: `concurrent_app` backed by a pooled Engine, two distinct PostgreSQL backend PIDs, barrier-overlapped HTTP requests, and deterministic concurrency acceptance evidence.

- [ ] **Step 1: Add three failing HTTP concurrency tests before the fixture exists**

In `test_image_asset_import.py`, add `ThreadPoolExecutor`, SQLAlchemy `text`, and this request runner:

```python
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text


def _parallel_import_requests(app, entries):
    start = threading.Barrier(2)
    pids = []
    pid_lock = threading.Lock()

    def post_one(entry):
        with app.app_context():
            try:
                pid = db.session.execute(
                    text('SELECT pg_backend_pid()')
                ).scalar_one()
                with pid_lock:
                    pids.append(pid)
                start.wait(timeout=10)
                return _import_request(app.test_client(), [entry])
            finally:
                db.session.remove()

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(post_one, entries))
    assert len(set(pids)) == 2
    return responses
```

Add a barrier embedding used only by the empty-database race:

```python
class BarrierImportEmbedding(FakeImportEmbedding):
    def __init__(self):
        super().__init__()
        self.barrier = threading.Barrier(2)
        self.pids = []
        self._lock = threading.Lock()

    def embed_normalized_images(self, image_paths, request_id=None):
        pid = db.session.execute(text('SELECT pg_backend_pid()')).scalar_one()
        with self._lock:
            self.pids.append(pid)
        vectors = super().embed_normalized_images(
            image_paths,
            request_id=request_id,
        )
        self.barrier.wait(timeout=10)
        return vectors
```

Then add:

```python
def test_concurrent_http_same_source_same_content_converges(concurrent_app):
    storage = FakeImportStorage()
    embedding = BarrierImportEmbedding()
    concurrent_app.config['IMAGE_ASSET_STORAGE'] = storage
    concurrent_app.config['IMAGE_INGEST_EMBEDDING'] = embedding
    original = _png_bytes('purple')
    entry = (original, 'same.png', 'same.png')

    responses = _parallel_import_requests(concurrent_app, [entry, entry])

    bodies = [response.get_json() for response in responses]
    assert sorted(body['items'][0]['status'] for body in bodies) == [
        'created',
        'existing',
    ]
    assert len({body['items'][0]['asset_id'] for body in bodies}) == 1
    assert len(set(embedding.pids)) == 2
    assert sum(embedding.batch_calls) == 2
    with concurrent_app.app_context():
        assert ImageAsset.query.count() == 1


def test_concurrent_http_existing_and_changed_content_are_deterministic(concurrent_app):
    storage, embedding = _install_import_dependencies(concurrent_app)
    original = _png_bytes('red')
    with concurrent_app.app_context():
        created = _import_request(
            concurrent_app.test_client(),
            [(original, 'same.png', 'same.png')],
        ).get_json()['items'][0]
        asset = ImageAsset.query.one()
        original_key = asset.oss_path
        before = storage.objects[original_key]
        db.session.remove()

    responses = _parallel_import_requests(concurrent_app, [
        (original, 'same.png', 'same.png'),
        (_png_bytes('blue'), 'same.png', 'same.png'),
    ])

    items = [response.get_json()['items'][0] for response in responses]
    assert sorted(item['status'] for item in items) == [
        'existing',
        'source_conflict',
    ]
    assert {item['asset_id'] for item in items} == {created['asset_id']}
    assert storage.objects[original_key] == before
    assert sum(embedding.batch_calls) == 1


def test_concurrent_http_archived_hits_never_restore(concurrent_app):
    storage, embedding = _install_import_dependencies(concurrent_app)
    original = _png_bytes('red')
    with concurrent_app.app_context():
        created = _import_request(
            concurrent_app.test_client(),
            [(original, 'same.png', 'same.png')],
        ).get_json()['items'][0]
        asset = ImageAsset.query.one()
        asset.status = 'archived'
        asset.archived_at = datetime.now()
        archived_at = asset.archived_at
        db.session.commit()
        put_count = len(storage.put_calls)
        db.session.remove()

    responses = _parallel_import_requests(concurrent_app, [
        (original, 'same.png', 'same.png'),
        (original, 'same.png', 'same.png'),
    ])

    items = [response.get_json()['items'][0] for response in responses]
    assert [item['status'] for item in items] == [
        'in_recycle_bin',
        'in_recycle_bin',
    ]
    assert {item['asset_id'] for item in items} == {created['asset_id']}
    with concurrent_app.app_context():
        unchanged = ImageAsset.query.one()
        assert unchanged.status == 'archived'
        assert unchanged.archived_at == archived_at
    assert len(storage.put_calls) == put_count
    assert sum(embedding.batch_calls) == 1
```

- [ ] **Step 2: Run the new node IDs and verify RED because `concurrent_app` is absent**

Run from `backend/`:

```bash
python -m pytest \
  test/integration/test_image_asset_import.py::test_concurrent_http_same_source_same_content_converges \
  test/integration/test_image_asset_import.py::test_concurrent_http_existing_and_changed_content_are_deterministic \
  test/integration/test_image_asset_import.py::test_concurrent_http_archived_hits_never_restore -v
```

Expected: collection/setup errors say fixture `concurrent_app` is not found. This is the RED for the real concurrency harness, not evidence of a production transaction bug.

- [ ] **Step 3: Extract a symmetric temporary-schema Engine helper**

In `backend/test/integration/conftest.py`, import `contextmanager`, `event`, and `sessionmaker` at module level, then add:

```python
from contextlib import contextmanager

from sqlalchemy import event
from sqlalchemy.orm import sessionmaker


@contextmanager
def _temporary_schema_engine(database_url):
    from models import db

    schema_name = _temporary_schema_name()
    quoted_schema = f'"{schema_name}"'
    engine = sqlalchemy.create_engine(database_url, pool_pre_ping=True)

    @event.listens_for(engine, 'connect')
    def _set_search_path(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f'SET search_path TO {quoted_schema}, public')
        cursor.close()

    with engine.connect() as setup_connection:
        setup_connection.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
        setup_connection.execute(text(f'CREATE SCHEMA {quoted_schema}'))
        setup_connection.commit()
        db.metadata.create_all(
            bind=setup_connection.execution_options(
                schema_translate_map={None: schema_name}
            )
        )
        setup_connection.commit()

    try:
        yield engine
    finally:
        with engine.connect() as cleanup_connection:
            cleanup_connection.execute(
                text(f'DROP SCHEMA {quoted_schema} CASCADE')
            )
            cleanup_connection.commit()
        engine.dispose()
```

Refactor `pg_session_factory` to:

```python
@pytest.fixture()
def pg_session_factory(_test_database):
    with _temporary_schema_engine(_test_database) as engine:
        yield sessionmaker(bind=engine)
```

- [ ] **Step 4: Add `concurrent_app` with symmetric engine restoration**

Add this fixture after `app`:

```python
@pytest.fixture()
def concurrent_app(_test_database, tmp_path):
    from app import create_app
    from models import db

    application = create_app()
    application.config['TESTING'] = True
    application.config['UPLOAD_FOLDER'] = str(tmp_path / 'uploads-concurrent')
    os.makedirs(application.config['UPLOAD_FOLDER'], exist_ok=True)

    with _temporary_schema_engine(_test_database) as engine:
        with application.app_context():
            db.session.remove()
            original_engine = db.engines[None]
            db.engines[None] = engine
        try:
            yield application
        finally:
            with application.app_context():
                db.session.remove()
                db.engines[None] = original_engine
```

The context manager owns schema deletion and `engine.dispose()` after `db.engines[None]` is restored. Do not modify the ordinary single-connection `app` fixture; this limits regression risk for the existing integration suite.

- [ ] **Step 5: Run the valid concurrency gate**

Run the three exact node IDs from Step 2 again.

Expected if the production transaction logic is correct: all three pass, the same-content test records two different backend PIDs and two embedding calls, and the database contains one source-identity row.

If any test fails after `len(set(pids)) == 2` and the embedding barrier has released both requests, stop this plan immediately. Save the full traceback and PostgreSQL exception class, do not edit `backend/services/asset_ingest.py`, and request a diagnosis/plan revision. Schema, isolation, advisory-lock, Kodo, model, or dimension changes are outside this plan.

- [ ] **Step 6: Remove the invalid single-connection threaded test only after the HTTP gate is GREEN**

Delete `ConcurrentFakeOss` and `test_concurrent_same_source_retries_converge_to_one_asset` from `backend/test/integration/test_asset_ingest.py`. Remove its now-unused `threading`, `ThreadPoolExecutor`, and `ObjectStorageConflictError` imports. Keep the sequential service-layer source-identity, recycle-bin, and content-reuse tests unchanged.

- [ ] **Step 7: Run concurrency and inherited service regressions together**

Run from `backend/`:

```bash
python -m pytest \
  test/integration/test_image_asset_import.py \
  test/integration/test_asset_ingest.py \
  test/integration/test_vector_search.py::test_same_hash_at_different_paths_occupies_two_result_positions -v
```

Expected: all selected tests pass with real PostgreSQL; no single-connection concurrency warning appears.

- [ ] **Step 8: Review checkpoint without committing**

```bash
git diff -- backend/test/integration/conftest.py backend/test/integration/test_image_asset_import.py backend/test/integration/test_asset_ingest.py
git diff --check -- backend/test/integration
```

Confirm teardown restores `db.engines[None]` before schema cleanup/disposal, every worker removes its scoped session, and tests prove distinct PIDs. Do not commit.

---

### Task 4: Append-only ADR exception and repository contract protection

**Files:**
- Modify: `backend/test/test_issue_18_static_contract.py`
- Modify: `docs/adr/0006-asynchronous-embedding-before-asset-creation.md`
- Modify: `AGENTS.md:243-255`

**Interfaces:**
- Consumes: Rev 2's bounded compatibility exception and the existing four exact source-identity phrases.
- Produces: additive documentation that scopes worker-only behavior to persistent async tasks without permitting any new synchronous embedding endpoint.

- [ ] **Step 1: Add a failing additive-documentation contract**

Append to `backend/test/test_issue_18_static_contract.py`:

```python
def test_synchronous_import_is_one_bounded_append_only_exception():
    guidance = _source('../AGENTS.md')
    adr = _source('../docs/adr/0006-asynchronous-embedding-before-asset-creation.md')

    assert '持久异步图片导入任务只由独立 worker 处理' in guidance
    assert 'POST /api/image-assets/import' in guidance
    assert '同步兼容入口' in guidance
    assert 'POST /api/image-assets/import' in adr
    assert '追加式例外' in adr
    assert '不得作为新增同步 embedding 入口的先例' in adr
    assert '向量成功后才创建图片资产' in adr
    assert '现有手动导入按内容哈希直接跳过不同路径图片的行为必须移除' in adr
```

- [ ] **Step 2: Run the static contract and verify RED without changing existing assertions**

Run from `backend/`:

```bash
python -m pytest test/test_issue_18_static_contract.py -v
```

Expected: the new test fails on the missing bounded-exception phrases; the existing four exact guidance assertions still pass.

- [ ] **Step 3: Append the narrow ADR-0006 exception**

Append this section to `docs/adr/0006-asynchronous-embedding-before-asset-creation.md`; do not edit or reorder the existing accepted paragraphs:

```markdown
## 现存同步兼容入口的追加式例外

`POST /api/image-assets/import` 保留为现存的同步兼容入口，每个 HTTP 请求最多接收二十张图片，并在请求内完成标准化、embedding 与资产创建。它没有持久导入项、自动重试、取消或恢复导入项能力；`POST /api/image-imports` 仍是默认的可靠异步入口。

本段是追加式例外，只记录该既有 URL 的受限行为。它不改变向量成功后才创建图片资产、按来源身份去重、不同路径同内容分别形成资产、异步重试与取消等既有决策，也不得作为新增同步 embedding 入口的先例。
```

- [ ] **Step 4: Scope AGENTS.md worker-only wording without deleting it**

Keep the complete source-identity bullet at `AGENTS.md:248` byte-for-byte unchanged. Replace only the next worker bullet with:

```markdown
- 持久异步图片导入任务只由独立 worker 处理；HTTP 请求内不启动线程或可靠内存队列。瞬时失败按错误分类指数退避自动重试并受尝试预算约束，手工重试、取消与放弃项恢复只改持久状态；当前没有暂存对象清理或永久删除能力。现存 POST /api/image-assets/import 是每请求最多 20 张、无持久任务/自动重试/取消语义的同步兼容入口；该受限例外不得扩展为新的请求内 embedding 入口。
```

- [ ] **Step 5: Run static and source-identity contracts and verify GREEN**

Run from `backend/`:

```bash
python -m pytest \
  test/test_issue_18_static_contract.py \
  test/test_issue_18_source_identity_unit.py -v
```

Expected: all tests pass, including the original exact phrases at lines 66-69 of the static contract.

- [ ] **Step 6: Review checkpoint without committing**

```bash
git diff -- AGENTS.md docs/adr/0006-asynchronous-embedding-before-asset-creation.md backend/test/test_issue_18_static_contract.py
git diff --check -- AGENTS.md docs/adr/0006-asynchronous-embedding-before-asset-creation.md backend/test/test_issue_18_static_contract.py
```

Confirm ADR changes are append-only, the worker-only bullet remains present and scoped, and the four existing source-identity sentences are unchanged. Do not commit.

---

### Task 5: Fresh verification and safety audit

**Files:**
- Verify: all files changed by Tasks 1-4
- Do not modify: Kodo code/tests, schema/migrations, `backend/services/asset_ingest.py` unless a separately approved revised plan exists

**Interfaces:**
- Consumes: completed Task 1-4 diffs.
- Produces: fresh, scoped evidence for TDD outcomes, concurrency validity, frontend build, documentation contracts, and prohibited-operation boundaries.

- [ ] **Step 1: Run the focused Issue #18 backend gate**

Run from `backend/`:

```bash
python -m pytest \
  test/test_issue_18_source_identity_unit.py \
  test/test_issue_18_product_upload_unit.py \
  test/test_issue_18_static_contract.py \
  test/integration/test_image_asset_import.py \
  test/integration/test_asset_ingest.py \
  test/integration/test_vector_search.py::test_same_hash_at_different_paths_occupies_two_result_positions -v
```

Expected: all selected tests pass, concurrency tests prove distinct backend PIDs, and no scenario is skipped for an available local PostgreSQL server.

- [ ] **Step 2: Run non-integration backend regression tests**

Run from `backend/`:

```bash
python -m pytest test/ \
  --ignore=test/integration \
  --ignore=test/test.py \
  --ignore=test/test_pgvector.py \
  --ignore=test/benchmark_search.py -v
```

Expected: exit `0`. This command must not connect to real OSS, Kodo, or DashScope.

- [ ] **Step 3: Run the full integration suite and report inherited Kodo failures honestly**

Run from `backend/`:

```bash
python -m pytest test/integration/ -v
```

Expected ticket result: all Issue #18 and non-Kodo integration tests pass. If the six known Kodo migration tests still fail, record their exact node IDs and tracebacks as pre-existing; do not edit Kodo code and do not describe the complete integration suite as green. Any new non-Kodo failure blocks completion.

- [ ] **Step 4: Compile backend Python sources**

Run from `backend/`:

```bash
python -m compileall -q app.py blueprints models services scripts test
```

Expected: exit `0` with no syntax errors.

- [ ] **Step 5: Run focused and full frontend verification**

Run from `frontend/`:

```bash
npm test -- --run \
  src/services/productApi.test.ts \
  src/components/ImportImagesModal.test.tsx \
  src/components/ProductUpload.test.tsx \
  src/components/ArchivedAssetGrid.test.tsx \
  src/components/UnassignedAssetGrid.test.tsx \
  src/components/ProductSearch.test.tsx
npm test -- --run
npm run build
```

Expected: both test commands and the build exit `0`. Existing Browserslist or chunk-size warnings may be reported as warnings, not failures.

- [ ] **Step 6: Run diff integrity and prohibited-change scans**

Run from the isolated worktree root:

```bash
git diff --check
git status --short
git diff --name-only
rg -n "skipped_duplicate_content|跳过（内容重复）" backend/blueprints/image_assets.py backend/test/integration/test_image_asset_import.py frontend/src/types/product.ts frontend/src/components/ImportImagesModal.tsx frontend/src/components/ImportImagesModal.test.tsx
rg -n "delete_object|batch_delete_objects|DROP TABLE|DELETE FROM|advisory_lock|SET TRANSACTION ISOLATION|qiniu|kodo" backend/blueprints/image_assets.py backend/test/integration/conftest.py backend/test/integration/test_image_asset_import.py frontend/src docs/adr/0006-asynchronous-embedding-before-asset-creation.md AGENTS.md
```

Expected:

- `git diff --check` exits `0`.
- The skipped-status search returns no matches in the listed active contract files.
- The prohibited-change scan returns no newly added destructive, isolation, advisory-lock, or Kodo behavior; pre-existing textual matches must be identified by file and excluded only after checking the actual diff.
- `git diff --name-only` contains only the files in this plan's File Map, with `backend/services/asset_ingest.py` absent.
- Main-checkout untracked files are absent from the isolated worktree diff.

- [ ] **Step 7: Requirements trace and completion report without committing**

Re-read `/tmp/issue-18-design-rev2.md` and record evidence for each requirement:

```text
source identity over global hash
distinct assets and search positions for same content at different paths
same-source idempotency
source conflict with safe asset_id and no overwrite
recycle-bin result and no restore
five-way ordered HTTP contract and count partition
item-level and transport-level retry
two backend PIDs plus embedding barrier
orphan-object HEAD reuse
append-only ADR exception and preserved static phrases
```

Report raw commands, exit codes, pass/fail/skip counts, the exact status of the six inherited Kodo tests, and any unverified real external system. Do not claim completion if a fresh required command is missing or a new non-Kodo failure remains. Do not commit, push, merge, or create a PR.

## Execution Stop

This plan is the final artifact for the current session. Stop after writing and self-reviewing it. The next allowed action is main-thread confirmation, followed by `superpowers:using-git-worktrees` based on `b0e505db22e75c646bd40fec86f06b1ec4e51f99`. No implementation action is authorized by this plan-writing request.
