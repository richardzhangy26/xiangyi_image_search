# 受控迁移执行 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Issue #10 提供可复现的 10 张精确选样、只读覆盖验证与安全试迁移，并将 Issue #11 保持在明确批准后才可执行的门槛之后。

**Architecture:** 迁移 CLI 在现有扫描结果之上增加有序 JSON 选样清单。`MigrationOptions` 负责把清单、重试和模式约束变成不可变运行配置；`run_migration` 按清单顺序匹配对象，并在新的只读验证模式中下载、哈希、解码和汇总覆盖证据。写模式继续委托既有 `ImageAssetIngestService`，不复制 OSS、embedding 或数据库逻辑。

**Tech Stack:** Python 3.9+、argparse、Pillow、pytest、Flask-SQLAlchemy/PostgreSQL pgvector、Vite/TypeScript。

## Global Constraints

- Kodo 全程只读；不得调用 Put、Delete 或覆盖操作。
- `--full` 必须拒绝精确选样清单，且不能由 `--pilot` 或验证模式自动进入。
- 清单必须是 UTF-8 JSON 对象，唯一字段为有序 `source_relative_paths` 字符串数组；清单路径、临时绝对路径、凭证和完整签名 URL 均不得进入报告或数据库。
- 试迁移清单必须恰好 10 项；`--pilot N` 与清单长度必须相等。
- 验证必须证明：中文且含空格路径、多层目录、实际 JPEG/PNG/WebP、超过 20 MiB、同哈希不同路径、小图候选；缺项以非零结果明确报告，不能静默降级。
- 测试只通过公开 CLI / `run_migration` seam 断言行为；不测试私有辅助函数。
- 任何真实写入、DashScope 调用或 `--full` 都必须在自动化测试、预检和用户授权之后发生。

---

## 文件结构

- `backend/scripts/migrate_kodo_to_oss.py`：CLI 参数、环境加载和模式路由；不实现选样或图像处理细节。
- `backend/services/kodo_migration.py`：清单解析、不可变运行选项、对象选择、只读选样验证和结构化报告。
- `backend/test/test_kodo_preflight.py`：无数据库的 CLI 安全、参数和只读行为测试。
- `backend/test/integration/test_kodo_oss_migration.py`：真实 Flask/独立 PostgreSQL seam 上的按清单试迁移、批量入库、幂等与冲突测试。
- `backend/scripts/README_OSS_MIGRATION.md`：命令、清单格式、验证顺序和全量门槛的操作文档。

### Task 1: 清单契约与稳定对象选择

**Files:**

- Modify: `backend/services/kodo_migration.py:79-145,503-527`
- Modify: `backend/scripts/migrate_kodo_to_oss.py:25-96,135-194`
- Test: `backend/test/test_kodo_preflight.py`

**Interfaces:**

- Consumes: `SourceObject(key: str, size: int)`，以及现有 `MigrationOptions.build()`。
- Produces: `load_selection_manifest(path: Path) -> tuple[str, ...]`；`MigrationOptions.selection_keys: tuple[str, ...]`；`MigrationOptions.mode` 新增 `verify-selection`。

- [ ] **Step 1: 写入 manifest 的失败测试**

```python
def test_manifest_dry_run_keeps_declared_order_and_never_constructs_writers(
    tmp_path,
):
    manifest = tmp_path / "selection.json"
    manifest.write_text(
        json.dumps({"source_relative_paths": ["二/图.png", "一/图.png"]}),
        encoding="utf-8",
    )
    exit_code = main(
        ["--dry-run", "--selection-manifest", str(manifest)],
        environ=_canonical_env(),
        source_factory=lambda _config: source,
        storage_factory=lambda _environment: pytest.fail("只读模式不得构造 OSS"),
        embedding_factory=lambda _environment: pytest.fail("只读模式不得构造 embedding"),
        stdout=stdout,
        stderr=stderr,
    )
    assert exit_code == 0
    assert [
        item["source_relative_path"]
        for item in json.loads(report_path.read_text())["items"]
    ] == ["二/图.png", "一/图.png"]


@pytest.mark.parametrize("payload", [
    {},
    {"source_relative_paths": ["一/图.png", "一/图.png"]},
    {"source_relative_paths": ["一/图.png", 3]},
])
def test_invalid_selection_manifest_is_rejected_before_source_listing(
    tmp_path,
    payload,
):
    manifest = tmp_path / "selection.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    exit_code = main(
        ["--dry-run", "--selection-manifest", str(manifest)],
        environ=_canonical_env(),
        source_factory=lambda _config: pytest.fail("无效清单不得创建来源"),
        stdout=io.StringIO(),
        stderr=stderr,
    )
    assert exit_code == 2
    assert json.loads(stderr.getvalue())["stage"] == "selection_manifest"
```

- [ ] **Step 2: 运行测试，确认新参数尚不存在**

Run: `cd backend && python -m pytest test/test_kodo_preflight.py -k 'manifest' -v`

Expected: FAIL，因为 `--selection-manifest` 尚不存在。

- [ ] **Step 3: 实现最小清单解析、模式约束和选样**

```python
@dataclass(frozen=True)
class MigrationOptions:
    mode: str = "dry-run"
    selection_keys: tuple[str, ...] = ()


def load_selection_manifest(path: Path) -> tuple[str, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        keys = payload["source_relative_paths"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise MigrationError(
            "selection_manifest",
            safe_exception_summary(exc),
        ) from exc
    if (
        not isinstance(keys, list)
        or not keys
        or any(not isinstance(key, str) or not key for key in keys)
        or len(set(keys)) != len(keys)
    ):
        raise MigrationError(
            "selection_manifest",
            "source_relative_paths 必须是非空且唯一的字符串数组",
        )
    return tuple(keys)


def _select_objects(objects, image_objects, options):
    if options.selection_keys:
        all_by_key = {item.key: item for item in objects}
        image_by_key = {item.key: item for item in image_objects}
        missing = [key for key in options.selection_keys if key not in all_by_key]
        non_images = [
            key for key in options.selection_keys
            if key in all_by_key and key not in image_by_key
        ]
        if missing or non_images:
            raise MigrationError(
                "selection_manifest",
                "清单包含不存在或非图片来源路径",
            )
        return [image_by_key[key] for key in options.selection_keys], ()
    # 保持既有 retry、pilot 和 limit 逻辑不变。
```

在 `create_parser()` 的互斥模式组加入 `--verify-selection`，另加 `--selection-manifest`。在 `main()` 载入清单并传给 `MigrationOptions.build()`；拒绝清单与 `--retry-failed` 并用，拒绝 `--full` 携带清单，且在 pilot 模式要求 `args.pilot == len(selection_keys)`。所有这些配置错误都输出 `stage=selection_manifest` 并返回 2。

- [ ] **Step 4: 运行定向测试和静态检查**

Run: `cd backend && python -m pytest test/test_kodo_preflight.py -k 'manifest or pilot_and_full' -v && mypy services/kodo_migration.py scripts/migrate_kodo_to_oss.py`

Expected: pytest 全部通过；mypy 对两文件零错误。

- [ ] **Step 5: 提交清单选择切片**

```bash
git add backend/services/kodo_migration.py backend/scripts/migrate_kodo_to_oss.py backend/test/test_kodo_preflight.py
git commit -m "feat(migration): select pilot images from manifest"
```

### Task 2: 只读验证与覆盖报告

**Files:**

- Modify: `backend/services/kodo_migration.py:205-345,503-610`
- Test: `backend/test/test_kodo_preflight.py`

**Interfaces:**

- Consumes: `ReadOnlyObjectSource`、已验证的 `MigrationOptions(selection_keys=...)` 和 `safe_exception_summary()`。
- Produces: `run_migration(..., options=MigrationOptions(mode="verify-selection", ...)) -> dict[str, Any]`；每项包含 `content_hash`、`image_format`、`source_width`、`source_height`、`coverage_tags`；顶层包含 `verification.covered` 与 `verification.missing`。

- [ ] **Step 1: 写入只读验证失败测试**

```python
def test_verify_selection_reports_duplicate_hash_and_required_coverage(
    tmp_path,
):
    duplicate = _png_bytes("navy")
    manifest = _write_manifest(tmp_path, [
        "中文 空格/多层/图.png", "格式/图.jpg", "格式/图.webp",
        "超大/图.png", "小图/图.png", "重复/一.png", "重复/二.png",
        "普通/三.png", "普通/四.png", "普通/五.png",
    ])
    exit_code, report = _run_read_only([
        "--verify-selection", "--selection-manifest", str(manifest),
    ])
    assert exit_code == 0
    assert report["read_only"] is True
    assert report["verification"]["missing"] == []
    duplicates = [
        item for item in report["items"]
        if "duplicate_content" in item["coverage_tags"]
    ]
    assert len(duplicates) == 2
    assert all(call[0] in {"resolve", "list", "head", "get"} for call in source.calls)


def test_verify_selection_fails_with_named_missing_coverage(tmp_path):
    manifest = _write_manifest(
        tmp_path,
        [f"平面/图{index}.png" for index in range(10)],
    )
    exit_code, report = _run_read_only([
        "--verify-selection", "--selection-manifest", str(manifest),
    ])
    assert exit_code == 1
    assert set(report["verification"]["missing"]) >= {
        "jpeg", "webp", "duplicate_content", "over_20_mib",
    }
```

- [ ] **Step 2: 运行测试，确认验证模式尚未实现**

Run: `cd backend && python -m pytest test/test_kodo_preflight.py -k 'verify_selection' -v`

Expected: FAIL，因为 `--verify-selection` 和 `verification` 报告尚不存在。

- [ ] **Step 3: 实现临时下载校验与固定覆盖计算**

```python
REQUIRED_SELECTION_COVERAGE = frozenset({
    "chinese_space_path",
    "nested_path",
    "jpeg",
    "png",
    "webp",
    "over_20_mib",
    "duplicate_content",
    "small_source",
})


def _verify_selected_images(source, selected):
    with tempfile.TemporaryDirectory(prefix="kodo-selection-") as directory:
        reports = [
            _verify_one_source_image(source, item, Path(directory), index)
            for index, item in enumerate(selected)
        ]
    hashes = Counter(
        item["content_hash"]
        for item in reports
        if item["status"] == "verified"
    )
    for item in reports:
        if hashes.get(item.get("content_hash"), 0) > 1:
            item["coverage_tags"].append("duplicate_content")
    covered = {
        tag for item in reports for tag in item["coverage_tags"]
    }
    missing = sorted(REQUIRED_SELECTION_COVERAGE - covered)
    return reports, {"covered": sorted(covered), "missing": missing}


def _verify_one_source_image(source, item, temp_root, index):
    path = temp_root / f"source-{index}"
    with path.open("w+b") as target:
        head = source.head_object(item.key)
        downloaded = source.download_object(item.key, target)
        if downloaded != target.tell() or head.size != target.tell():
            raise ValueError("来源 HEAD、下载返回值与实际字节数不一致")
    content_hash = _hash_file(path)
    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        image.load()
        image_format = image.format or "UNKNOWN"
        width, height = image.size
    return _verified_report(item, content_hash, image_format, width, height)
```

`_verified_report()` 根据真实对象生成标签：路径同时含中文与空格为 `chinese_space_path`；`key.count("/") >= 2` 为 `nested_path`；实际格式的 `JPEG`/`PNG`/`WEBP` 分别映射为小写格式标签；`source_size > 20 * 1024 * 1024` 为 `over_20_mib`；`max(width, height) <= ImageNormalizer.from_env().max_edge` 为 `small_source`。下载、解码或哈希异常必须转为单项 `{status: "failed", error_stage: "verification", error: "verification:<ExceptionType>"}`，继续其他项，并且不保留原始异常文本。`run_migration()` 只在全部项 verified 且 `missing == []` 时返回 `status="ok"`；否则返回 `completed_with_issues`，使 CLI 返回 1。

- [ ] **Step 4: 证明验证不构造任何写端**

Run: `cd backend && python -m pytest test/test_kodo_preflight.py -k 'verify_selection or manifest' -v && mypy services/kodo_migration.py scripts/migrate_kodo_to_oss.py`

Expected: pytest 全部通过；断言中的 fake OSS/embedding factory 从未调用；mypy 零错误。

- [ ] **Step 5: 提交只读验证切片**

```bash
git add backend/services/kodo_migration.py backend/scripts/migrate_kodo_to_oss.py backend/test/test_kodo_preflight.py
git commit -m "feat(migration): verify pilot selection coverage"
```

### Task 3: 按清单试迁移、幂等与文档

**Files:**

- Modify: `backend/test/integration/test_kodo_oss_migration.py`
- Modify: `backend/services/kodo_migration.py:205-345`
- Modify: `backend/scripts/README_OSS_MIGRATION.md:15-71`

**Interfaces:**

- Consumes: `main(argv, app, source_factory, storage_factory, embedding_factory)` 和 JSON `source_relative_paths` 清单。
- Produces: `--pilot 10 --selection-manifest PATH` 按清单顺序调用 `ImageAssetIngestService.ingest_many()`，报告选择数、阶段计数和逐项路径。

- [ ] **Step 1: 写入数据库 seam 的失败测试**

```python
def test_pilot_manifest_writes_selected_ten_and_reruns_idempotently(
    app,
    tmp_path,
):
    keys = [
        "中文 空格/多层/0.png", "格式/1.jpg", "格式/2.webp", "超大/3.png",
        "小图/4.png", "重复/5.png", "重复/6.png", "普通/7.png",
        "普通/8.png", "普通/9.png",
    ]
    manifest = _write_manifest(tmp_path, keys)
    first_code, first, _ = _run(
        ["--pilot", "10", "--selection-manifest", str(manifest)],
        app=app, source=source, storage=storage, embedding=embedding,
    )
    second_code, second, _ = _run(
        ["--pilot", "10", "--selection-manifest", str(manifest)],
        app=app, source=source, storage=storage, embedding=embedding,
    )
    assert first_code == second_code == 0
    assert ImageAsset.query.count() == 10
    assert first["summary"]["outcomes"] == {"created": 10}
    assert second["summary"]["outcomes"] == {"existing": 10}


def test_full_rejects_selection_manifest_before_oss_or_embedding(tmp_path):
    manifest = _write_manifest(tmp_path, ["图.png"])
    exit_code, report, error = _run(
        ["--full", "--selection-manifest", str(manifest)],
        app=app, source=source, storage=pytest.fail, embedding=pytest.fail,
    )
    assert exit_code == 2
    assert report is None
    assert error["stage"] == "selection_manifest"
```

- [ ] **Step 2: 运行测试，确认写模式契约失败**

Run: `cd backend && python -m pytest test/integration/test_kodo_oss_migration.py -k 'manifest' -v`

Expected: FAIL，直到 `--pilot` 把清单传入 `MigrationOptions`。

- [ ] **Step 3: 用最小改动让写模式复用既有入库服务**

```python
options = MigrationOptions.build(
    mode=mode,
    prefix=args.prefix,
    pilot_count=args.pilot,
    limit=args.limit,
    batch_size=args.batch_size,
    selection_keys=selection_keys,
    retry_enabled=args.retry_failed is not None,
    retry_failed_keys=retry_report.failed_keys if retry_report else (),
    retry_binding=retry_report,
)
```

不要向 `ImageAssetIngestService` 增加参数，也不要新建 OSS/embedding client。`run_migration()` 已经以选出的 `selected_keys` 批量调用 `service.ingest_many()`；只需让清单分支成为该列表的唯一来源。报告的 `options` 新增 `selection_manifest: bool` 与 `selection_count: int`，不能记录本地清单文件名。

- [ ] **Step 4: 添加操作文档和与之对应的计数约束测试**

```python
def test_selection_manifest_requires_pilot_count_match(tmp_path):
    manifest = _write_manifest(
        tmp_path,
        [f"图/{index}.png" for index in range(10)],
    )
    exit_code = main(
        ["--pilot", "9", "--selection-manifest", str(manifest)],
        environ=_canonical_env(),
        source_factory=lambda _config: source,
        stdout=io.StringIO(),
        stderr=stderr,
    )
    assert exit_code == 2
    assert json.loads(stderr.getvalue())["stage"] == "selection_manifest"
```

在 `README_OSS_MIGRATION.md` 加入准确的操作顺序：

```bash
cd backend
python -m scripts.migrate_kodo_to_oss --preflight --report-path reports/issue-10/preflight.json
python -m scripts.migrate_kodo_to_oss --dry-run --report-path reports/issue-10/inventory.json
python -m scripts.migrate_kodo_to_oss --verify-selection --selection-manifest reports/issue-10/selection.json --report-path reports/issue-10/selection-verification.json
python -m scripts.migrate_kodo_to_oss --pilot 10 --selection-manifest reports/issue-10/selection.json --report-path reports/issue-10/pilot.json
python -m scripts.migrate_kodo_to_oss --pilot 10 --selection-manifest reports/issue-10/selection.json --report-path reports/issue-10/pilot-rerun.json
```

紧接命令给出 `{"source_relative_paths":["来自 inventory.json 的精确 Kodo Key"]}` 的 schema，并明确示例文字必须替换为刚刚验证通过的十项真实 Key。文档还必须说明 `--full` 的前置条件：#10 Ticket 已附证据、用户在 #10 或父 PRD 明确批准、重新 preflight/dry-run 和数据库恢复点均已完成。

- [ ] **Step 5: 运行后端回归、typecheck 与前端生产构建**

Run: `cd backend && python -m pytest test/integration/test_kodo_oss_migration.py -v && python -m pytest test/ -v && mypy services/kodo_migration.py scripts/migrate_kodo_to_oss.py && cd ../frontend && npm run build`

Expected: 所有可运行测试通过（仅 PostgreSQL 物理不可达时允许既有集成 skip）；mypy 无错误；`tsc && vite build` 退出 0。

- [ ] **Step 6: 按 code-review 流程审查并提交**

以提交 `f6773f3` 为固定点运行 Standards 与 #10/#11 Spec 两轴 code review。先修复所有 P0/P1 或明确规格缺项、重跑受影响测试和上一步完整验证，然后提交：

```bash
git add backend/services/kodo_migration.py backend/scripts/migrate_kodo_to_oss.py backend/test/test_kodo_preflight.py backend/test/integration/test_kodo_oss_migration.py backend/scripts/README_OSS_MIGRATION.md
git commit -m "feat(migration): add controlled pilot selection"
```

### Task 4: Issue #10 真实试迁移与验收证据

**Files:**

- Create locally, untracked: `backend/reports/issue-10/preflight.json`
- Create locally, untracked: `backend/reports/issue-10/inventory.json`
- Create locally, untracked: `backend/reports/issue-10/selection.json`
- Create locally, untracked: `backend/reports/issue-10/selection-verification.json`
- Create locally, untracked: `backend/reports/issue-10/pilot.json`
- Create locally, untracked: `backend/reports/issue-10/pilot-rerun.json`

**Interfaces:**

- Consumes: 已通过的 CLI、`backend/.env` 本地凭证、Docker PostgreSQL、私有 OSS 和 Issue #10 接受标准。
- Produces: 脱敏 JSON 报告、Kodo/OSS/PostgreSQL 对账、搜索界面截图和 Issue #10 评论；不会产出 `--full` 调用。

- [ ] **Step 1: 记录自动化验证证据并进行只读环境预检**

Run: `cd backend && python -m pytest test/ -v && python -m scripts.migrate_kodo_to_oss --preflight --report-path reports/issue-10/preflight.json && python -m scripts.migrate_kodo_to_oss --dry-run --report-path reports/issue-10/inventory.json`

Expected: 三命令退出 0；报告显示 Kodo 可读、目标不写入、实时对象/图片/非图片/字节数基线。检查报告和终端输出不含凭证或完整签名 URL。

- [ ] **Step 2: 从实时盘点固定十项清单**

从 `inventory.json` 的 `items` 选择恰好十个真实 Key，至少包含：一个中文且含空格并有两层以上目录的 Key；真实 JPEG、PNG、WebP 各一个；一个 `source_size > 20971520` 的 Key；一个最长边不超过当前 `IMAGE_PREVIEW_MAX_EDGE` 的小图；以及两个候选重复内容的不同 Key。写入 `selection.json` 的 `source_relative_paths`。

先按相同源大小和相近文件名选择重复候选；运行下一步，若 `duplicate_content` 缺失，则在完全只读的状态下替换候选并重复验证。保存每次候选验证报告；最终 `selection.json` 是写入前固定样本。

- [ ] **Step 3: 验证清单，确认无写端活动**

Run: `cd backend && python -m scripts.migrate_kodo_to_oss --verify-selection --selection-manifest reports/issue-10/selection.json --report-path reports/issue-10/selection-verification.json`

Expected: 退出 0、`read_only: true`、`verification.missing: []`，十项均为 `verified`；检查报告确认重复组为两个不同路径同一 SHA-256。此命令不得构造 OSS、embedding 或数据库写端。

- [ ] **Step 4: 执行试迁移并立刻幂等重跑**

Run: `cd backend && python -m scripts.migrate_kodo_to_oss --pilot 10 --batch-size 10 --selection-manifest reports/issue-10/selection.json --report-path reports/issue-10/pilot.json && python -m scripts.migrate_kodo_to_oss --pilot 10 --batch-size 10 --selection-manifest reports/issue-10/selection.json --report-path reports/issue-10/pilot-rerun.json`

Expected: 首次报告满足 `created + existing + failed + *_conflict = 10`；若失败或冲突，停止并报告路径/阶段，不自动覆盖。第二次不新增 OSS 上传、embedding 或 `image_assets`，报告为 `existing`。绝不传递 `--full`。

- [ ] **Step 5: 三方对账、浏览器验收与 Ticket 暂停**

以 `pilot.json` 的十项为准：比较 Kodo/OSS 原图 SHA-256，查询 `image_assets` 验证十条来源路径与 `model_number IS NULL`，确认重复哈希两行共享 `preview_oss_path` 和向量，确认预览最长边≤2048、字节≤2.5 MiB、小图预览未被放大。

启动现有后端/前端，以一张试迁移源图通过搜索页面检索；保存结果卡、完整中文路径、未补充型号、复制路径和私有预览 302 的截图。向 Issue #10 评论附上命令、脱敏报告摘要、对账表、截图、失败项（如有）和“未运行 `--full`”；不能附凭证、签名 URL 或临时路径。此处停止并等待全量批准。

### Task 5: Issue #11 全量迁移（仅批准后）

**Files:**

- Create locally, untracked: `backend/reports/issue-11/preflight.json`
- Create locally, untracked: `backend/reports/issue-11/inventory.json`
- Create locally, untracked: `backend/reports/issue-11/full.json`
- Create locally, untracked: `backend/reports/issue-11/reconciliation.md`

**Interfaces:**

- Consumes: #10 Ticket 完整证据、#9 完成状态、用户在 #10 或父 PRD 的明确文字批准、数据库恢复点和实时 Kodo 盘点。
- Produces: 全量迁移报告与三方对账；Kodo 保持只读。

- [ ] **Step 1: 验证全量门槛**

读取 Issue #10 或父 PRD 中的明确批准评论永久链接；若不存在，停止本任务，不运行 `--full`。保存带时间戳的 `pg_dump` 或已验证恢复点，并记录脱敏环境配置摘要。

- [ ] **Step 2: 重新只读盘点并固定实时基线**

Run: `cd backend && python -m scripts.migrate_kodo_to_oss --preflight --report-path reports/issue-11/preflight.json && python -m scripts.migrate_kodo_to_oss --dry-run --report-path reports/issue-11/inventory.json`

Expected: 两命令退出 0；把当次 `objects`、`images`、`non_images`、`bytes` 和时间戳写入对账表，不能使用设计阶段的 2419/5.967 GiB 快照。

- [ ] **Step 3: 仅在门槛满足后执行全量与验收**

Run: `cd backend && python -m scripts.migrate_kodo_to_oss --full --batch-size 20 --report-path reports/issue-11/full.json`

Expected: `created + existing + failed + *_conflict = inventory.images`；每个失败项含来源相对路径、阶段和脱敏错误。随后进行 OSS 原图对象/字节数、活跃 `image_assets`、分层 SHA-256 与普通/超大/中文/多层/重复搜索抽样对账。用同命令重跑一次，确认不重复上传、embedding 或插入；将材料附到 Issue #11，永不删除或改写 Kodo。

## 自审

- **规格覆盖：** Task 1–3 实现 #10 所需的精确清单和只读验证；Task 4 覆盖 preflight、dry-run、试迁移、重跑、对账、页面验收与 Ticket 暂停；Task 5 将 #11 严格置于批准门槛之后。
- **占位检查：** 每个代码任务都有行为测试、失败命令、最小接口和通过命令。唯一运行时数据是 Kodo 的真实 Key，按 Task 4 的明确规则从当次只读盘点生成，不能在计划阶段伪造。
- **类型一致性：** CLI 传入 `MigrationOptions.selection_keys`，`run_migration` 选择 `SourceObject` 并返回报告，既有 `ImageAssetIngestService.ingest_many()` 的签名保持不变。
