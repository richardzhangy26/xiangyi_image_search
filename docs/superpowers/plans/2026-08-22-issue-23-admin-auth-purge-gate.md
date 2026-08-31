# Issue #23 管理员认证与永久清除安全门 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development for every production change, then superpowers:subagent-driven-development or executing-plans after the main thread authorizes Stage C and creates an isolated worktree via superpowers:using-git-worktrees. Steps use checkbox (`- [ ]`) syntax for tracking. Task checkpoints replace git commit steps.

**Goal:** 为永久清除建立默认关闭的管理员控制面：共享令牌认证、五项安全门只读评估、拒绝写路径，以及回收站只读准备状态；不启用永久清除、不落批次、不碰备份凭证。

**Architecture:** 两个深模块 `AdminAuth` 与 `PurgeSafetyGate`（可注入 `GateEvidenceSource`）隐藏比较、过期与合取；Flask 蓝图 `/api/admin/purge` 只做 HTTP、请求标识和活动记录。写路由判定顺序锁定为认证 → batch_id 语法 → `require_ready()` → `pipeline_available()`（T9 恒 False）。前端只在回收站用 sessionStorage 令牌读 readiness，任何状态都没有执行按钮。

**Tech Stack:** Python 3 / Flask / Flask-SQLAlchemy / pytest；React 18 / TypeScript / Ant Design 5 / Vitest / React Testing Library。

**Design input:** `/tmp/t9-design-rev.md`（architect APPROVE）。本计划吸收其全部不变量及下列五条非阻塞建议。

## Global Constraints

- 基线：`refactor/image-search-pgvector` HEAD `b81dc1c`。Stage C 才用 `.worktrees/` 建隔离 worktree，并符号链接 `backend/.env` 与 `frontend/node_modules`。写本计划时不建 worktree、不改业务代码。
- 未经主线程授权不得 commit / push / 合并 / 改 GitHub 状态。任务结束用验证命令交差，不用 git commit。
- 不碰 Kodo 代码与测试；不运行 `test/test.py`、`test/test_pgvector.py`、`test/benchmark_search.py`；不做真实 OSS / Kodo / DashScope 写入。
- 集成测试只连本机 `image_search_test`；因数据库不可用而 skip 不得写成通过。
- 控制面环境变量名锁定为 `PURGE_ADMIN_TOKEN`、`PURGE_ADMIN_ACTOR_ID`、`PURGE_GATE_EVIDENCE_DIR`。禁止 `PURGE_ENABLED`、`PURGE_GATE_NOW` 及任何运行时时钟环境变量。
- Flask 不加载 `.env.backup`，不 import `postgres_backup` / `purge_object_backup` / `purge_object_storage` / `kodo_*`。
- 无 schema 迁移、无用户表、无清除批次表。活动记录复用 `asset_activity_records`。
- 每块只跑本块列出的定向命令。先红后绿；红必须是断言失败或 ImportError，不是收集期语法错误。
- 普通搜索、导入、归款、回收站恢复、预览、`/api/health` 不加管理员令牌、不评估安全门。

---

## Architect 建议吸收记录

| # | 建议 | 本计划决定 | 落点 |
|---|---|---|---|
| 1 | `GateEvidenceSource.load()` 整体抛异常 → 五项全部 `unknown` | **吸收。** 逐项缺省仍是缺项/坏项 → 该项 `unknown` 或 `failed`；`load()` 抛任意 `Exception` 则五项均为 `unknown` 且 `ready=False`。不把整体异常映射成 `failed`。 | Task 2 测试 `test_load_exception_marks_all_conditions_unknown` 与 `evaluate` 的 `try/except` |
| 2 | `PURGE_CONTROL_AUDIT_FAILED` 与 `PURGE_CONTROL_FAILED` 纳入静态合同 | **吸收。** 与 401/403/400/两种 409 一起锁定。 | Task 4 |
| 3a | `purge.auth.denied` 的 `after_state` 仅含 `error_code` | **吸收。** 认证失败不调用 `evaluate()` / `require_ready()`，不记门状态。 | Task 3 |
| 3b | 敏感键检查递归嵌套对象 | **吸收。** 对 JSON 对象/数组递归；键名大小写不敏感匹配 `password`/`secret`/`token`/`authorization`/`dsn`。 | Task 2 |
| 3c | Bearer scheme 按 RFC 7235 大小写不敏感 | **吸收。** `header.split(None, 1)` 后 `scheme.lower() == "bearer"`。 | Task 1 |
| 4 | #26 前瞻：cancel/retry 也过 `require_ready()` | **吸收并记录给 #26。** T9 判定顺序统一，#26 只许替换第 4 步。批次执行中证据过期会挡住取消（fail-closed，数据保留）。若 #26 要让取消豁免安全门，属边界变更须另行授权，不得在 #26 内悄悄改。 | 本表 + Task 4 合同注释 + Task 7 设计稿 |
| 5 | 文件大小上限 64 KiB；summary 不得含秘密 | **吸收。** `MAX_EVIDENCE_FILE_BYTES = 65536`，超限 `parse_error=True`。键名检查挡不住值泄漏；`summary` 会原样返回管理员，编写指引明确禁止秘密。 | Task 2 测试 + Task 7 `docs/operations/purge-gate-evidence.md` |

---

## File Map

| 文件 | 职责 |
|---|---|
| `backend/services/admin_auth.py`（新建） | `AdminAuth.authenticate`：fail-closed、哈希后 `compare_digest`、RFC 7235 Bearer |
| `backend/services/purge_safety_gate.py`（新建） | 五项合取、`pipeline_available()`、File/Dict 探针、64 KiB、递归敏感键 |
| `backend/blueprints/admin_purge.py`（新建） | `/api/admin/purge` GET readiness + 三条写路由；活动记录 |
| `backend/app.py` | 装配 `ADMIN_AUTH` / `PURGE_SAFETY_GATE`，注册蓝图；不评估健康检查 |
| `backend/.env.example` | 注释三个可选变量，不填真令牌 |
| `backend/test/test_issue_23_auth_unit.py`（新建） | 认证矩阵 |
| `backend/test/test_issue_23_gate_unit.py`（新建） | 安全门矩阵 |
| `backend/test/test_issue_23_api_unit.py`（新建） | Flask 测试客户端 HTTP + 活动记录 |
| `backend/test/test_issue_23_static_contract.py`（新建） | #26 边界锁 |
| `backend/test/integration/test_issue_23_purge_gate.py`（新建） | `image_search_test` 活动记录持久化 |
| `frontend/src/types/product.ts` | `PurgeReadiness` 类型 |
| `frontend/src/services/productApi.ts` | 仅 `getPurgeReadiness` 带 Bearer |
| `frontend/src/components/ArchivedAssetGrid.tsx` | 默认可折叠管理员面板；无执行按钮 |
| `frontend/src/components/ArchivedAssetGrid.test.tsx` | 三态文案、默认折叠、清除令牌、无执行按钮 |
| `frontend/src/services/productApi.test.ts` | readiness 传输合同 |
| `frontend/src/components/ProductUpload.test.tsx` | 恢复/导入不被面板破坏 |
| `CONTEXT.md` | 永久清除安全门、安全门证据、管理员令牌 |
| `AGENTS.md` | 控制面事实、关键文件、环境变量 |
| `docs/operations/purge-gate-evidence.md`（新建） | 证据文档编写指引 |
| `docs/superpowers/specs/2026-08-22-issue-23-admin-auth-purge-gate-design.md` | 用批准稿覆盖 Stage A 草稿 |

---

### Task 1: AdminAuth

**Files:**
- Create: `backend/test/test_issue_23_auth_unit.py`
- Create: `backend/services/admin_auth.py`

**Interfaces:**
- Consumes: 无
- Produces:

```python
@dataclass(frozen=True)
class AdminPrincipal:
    actor_id: str
    role: str  # 恒为 "admin"

class AdminAuthError(Exception):
    def __init__(self, error_code: str, message: str):
        self.error_code = error_code  # AUTH_NOT_CONFIGURED | AUTH_REQUIRED | AUTH_FORBIDDEN
        self.message = message

class AdminAuth:
    def __init__(self, expected_token: str | None, actor_id: str = "admin"): ...
    def authenticate(self, authorization_header: str | None) -> AdminPrincipal: ...
```

- [ ] **Step 1: 写失败测试**

创建 `backend/test/test_issue_23_auth_unit.py`：

```python
import hashlib
import hmac
from unittest import mock

import pytest

from services.admin_auth import AdminAuth, AdminAuthError, AdminPrincipal


def test_unconfigured_none_rejects():
    with pytest.raises(AdminAuthError) as exc:
        AdminAuth(None).authenticate("Bearer any")
    assert exc.value.error_code == "AUTH_NOT_CONFIGURED"


def test_unconfigured_empty_rejects():
    with pytest.raises(AdminAuthError) as exc:
        AdminAuth("").authenticate("Bearer any")
    assert exc.value.error_code == "AUTH_NOT_CONFIGURED"


def test_unconfigured_whitespace_rejects():
    with pytest.raises(AdminAuthError) as exc:
        AdminAuth("   ").authenticate("Bearer any")
    assert exc.value.error_code == "AUTH_NOT_CONFIGURED"


def test_unconfigured_and_wrong_token_both_call_compare_digest():
    with mock.patch("services.admin_auth.hmac.compare_digest", wraps=hmac.compare_digest) as compare:
        with pytest.raises(AdminAuthError) as unconfigured:
            AdminAuth(None).authenticate("Bearer secret")
        with pytest.raises(AdminAuthError) as wrong:
            AdminAuth("correct-token").authenticate("Bearer wrong-token")
    assert unconfigured.value.error_code == "AUTH_NOT_CONFIGURED"
    assert wrong.value.error_code == "AUTH_FORBIDDEN"
    assert compare.call_count == 2
    for args, _kwargs in compare.call_args_list:
        assert len(args[0]) == hashlib.sha256().digest_size
        assert len(args[1]) == hashlib.sha256().digest_size


def test_missing_header_required():
    with pytest.raises(AdminAuthError) as exc:
        AdminAuth("correct-token").authenticate(None)
    assert exc.value.error_code == "AUTH_REQUIRED"


def test_malformed_and_basic_scheme_required():
    auth = AdminAuth("correct-token")
    for header in ("Bearer", "Bearer ", "Basic abc", "Token abc"):
        with pytest.raises(AdminAuthError) as exc:
            auth.authenticate(header)
        assert exc.value.error_code == "AUTH_REQUIRED"


def test_bearer_scheme_is_case_insensitive():
    principal = AdminAuth("correct-token", actor_id="ops").authenticate(
        "bEaReR correct-token"
    )
    assert principal == AdminPrincipal(actor_id="ops", role="admin")


def test_wrong_token_forbidden_and_correct_token_allows():
    auth = AdminAuth("  correct-token  ")
    with pytest.raises(AdminAuthError) as exc:
        auth.authenticate("Bearer other")
    assert exc.value.error_code == "AUTH_FORBIDDEN"
    assert auth.authenticate("Bearer correct-token").role == "admin"


def test_error_does_not_contain_token():
    with pytest.raises(AdminAuthError) as exc:
        AdminAuth("super-secret-value").authenticate("Bearer super-secret-valueX")
    dumped = f"{exc.value} {exc.value.message} {exc.value.error_code}"
    assert "super-secret-value" not in dumped
```

- [ ] **Step 2: 运行确认 RED**

Run:

```bash
cd backend
python -m pytest test/test_issue_23_auth_unit.py -v
```

Expected: FAIL（`ModuleNotFoundError: services.admin_auth` 或 `ImportError`）。若是收集期语法错误，先修测试再重新红。

- [ ] **Step 3: 最小实现**

创建 `backend/services/admin_auth.py`：

```python
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass


_UNCONFIGURED_DUMMY = hashlib.sha256(b"purge-admin-unconfigured").digest()


class AdminAuthError(Exception):
    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass(frozen=True)
class AdminPrincipal:
    actor_id: str
    role: str = "admin"


def _sha256(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _presented_token(authorization_header: str | None) -> str | None:
    if not authorization_header:
        return None
    parts = authorization_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        return None
    return parts[1]


class AdminAuth:
    def __init__(self, expected_token: str | None, actor_id: str = "admin"):
        stripped = (expected_token or "").strip()
        self._expected = stripped or None
        self._actor_id = actor_id

    def authenticate(self, authorization_header: str | None) -> AdminPrincipal:
        presented = _presented_token(authorization_header)
        presented_digest = _sha256(presented or "")
        if self._expected is None:
            hmac.compare_digest(presented_digest, _UNCONFIGURED_DUMMY)
            raise AdminAuthError(
                "AUTH_NOT_CONFIGURED",
                "永久清除控制面未配置管理员令牌",
            )
        matched = hmac.compare_digest(presented_digest, _sha256(self._expected))
        if presented is None:
            raise AdminAuthError("AUTH_REQUIRED", "需要管理员认证")
        if not matched:
            raise AdminAuthError(
                "AUTH_FORBIDDEN",
                "当前身份无权执行永久清除操作",
            )
        return AdminPrincipal(actor_id=self._actor_id, role="admin")
```

规则：未配置分支不得在 `compare_digest` 之前 `return`/`raise`。`expected_token` 只 strip 一次。`actor_id` 不得来自令牌或其哈希。

- [ ] **Step 4: 运行确认 GREEN**

Run:

```bash
cd backend
python -m pytest test/test_issue_23_auth_unit.py -v
```

Expected: PASS（全部用例）。

- [ ] **Step 5: 检查点** — 不 commit。记录本块命令与实际结果后进入 Task 2。

---

### Task 2: PurgeSafetyGate

**Files:**
- Create: `backend/test/test_issue_23_gate_unit.py`
- Create: `backend/services/purge_safety_gate.py`

**Interfaces:**
- Consumes: 无
- Produces: 设计稿中的 `ConditionId`、`CONDITION_IDS`、`RawConditionEvidence`、`ConditionSnapshot`、`GateSnapshot`、`GateNotReady`、`GateEvidenceSource`、`FileGateEvidenceSource`、`DictGateEvidenceSource`、`PurgeSafetyGate.evaluate` / `require_ready`、`pipeline_available() -> bool`（恒 `False`）、`MAX_EVIDENCE_FILE_BYTES = 65536`、`CONDITION_LABELS`。

`evaluate` 协议：

- 调用 `source.load(now)`；**整体抛 `Exception` → 五项全部 `unknown`，`ready=False`**（architect #1）。
- 缺项 → 该项 `unknown`。
- `parse_error` 或非法 `result` → `failed`。
- `result == "failed"` → `failed`。
- 时间字段无法解析为带时区 UTC datetime → `failed`。
- `verified_at > now + 60s` → `unknown`。
- `expires_at <= now` → `expired`。
- 仅 `result == "valid"` 且时间合法且未过期 → `valid`。
- 五项都 `valid` 才 `ready=True`。
- `clock` 仅构造注入；模块内不得读取任何 `NOW`/`CLOCK` 环境变量。

- [ ] **Step 1: 写失败测试**

创建 `backend/test/test_issue_23_gate_unit.py`，至少包含：

```python
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.purge_safety_gate import (
    CONDITION_IDS,
    MAX_EVIDENCE_FILE_BYTES,
    DictGateEvidenceSource,
    FileGateEvidenceSource,
    GateNotReady,
    PurgeSafetyGate,
    RawConditionEvidence,
    pipeline_available,
)

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


def _valid_raw(**overrides):
    payload = dict(
        result="valid",
        verified_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=12),
        summary="ok",
        parse_error=False,
    )
    payload.update(overrides)
    return RawConditionEvidence(**payload)


def _all_valid():
    return {cid: _valid_raw() for cid in CONDITION_IDS}


def test_pipeline_available_is_false():
    assert pipeline_available() is False


def test_missing_dir_is_all_unknown():
    snap = PurgeSafetyGate(FileGateEvidenceSource(None), clock=lambda: NOW).evaluate()
    assert snap.ready is False
    assert [c.status for c in snap.conditions] == ["unknown"] * 5
    assert [c.id for c in snap.conditions] == list(CONDITION_IDS)


def test_load_exception_marks_all_conditions_unknown():
    class Boom:
        def load(self, now):
            raise RuntimeError("disk failed")

    snap = PurgeSafetyGate(Boom(), clock=lambda: NOW).evaluate()
    assert snap.ready is False
    assert [c.status for c in snap.conditions] == ["unknown"] * 5


def test_expired_and_future_verified_at():
    source = DictGateEvidenceSource({
        **_all_valid(),
        "recovery_drill": _valid_raw(expires_at=NOW - timedelta(seconds=1)),
    })
    snap = PurgeSafetyGate(source, clock=lambda: NOW).evaluate()
    assert snap.ready is False
    assert {c.id: c.status for c in snap.conditions}["recovery_drill"] == "expired"

    source = DictGateEvidenceSource({
        **_all_valid(),
        "object_protection": _valid_raw(
            verified_at=NOW + timedelta(seconds=120),
        ),
    })
    snap = PurgeSafetyGate(source, clock=lambda: NOW).evaluate()
    assert {c.id: c.status for c in snap.conditions}["object_protection"] == "unknown"


def test_nested_secret_key_is_parse_error(tmp_path: Path):
    (tmp_path / "daily_postgres_backup.json").write_text(
        '{"schema_version":1,"condition":"daily_postgres_backup",'
        '"result":"valid","verified_at":"2026-08-22T04:00:00Z",'
        '"expires_at":"2026-08-23T05:00:00Z","meta":{"token":"leak"}}',
        encoding="utf-8",
    )
    snap = PurgeSafetyGate(
        FileGateEvidenceSource(tmp_path), clock=lambda: NOW
    ).evaluate()
    assert snap.conditions[0].status == "failed"


def test_oversized_file_is_parse_error(tmp_path: Path):
    payload = (
        '{"schema_version":1,"condition":"daily_postgres_backup",'
        '"result":"valid","verified_at":"2026-08-22T04:00:00Z",'
        '"expires_at":"2026-08-23T05:00:00Z","summary":"'
        + ("x" * (MAX_EVIDENCE_FILE_BYTES))
        + '"}'
    )
    (tmp_path / "daily_postgres_backup.json").write_text(payload, encoding="utf-8")
    snap = PurgeSafetyGate(
        FileGateEvidenceSource(tmp_path), clock=lambda: NOW
    ).evaluate()
    assert snap.conditions[0].status == "failed"


def test_five_valid_ready_and_require_ready():
    gate = PurgeSafetyGate(DictGateEvidenceSource(_all_valid()), clock=lambda: NOW)
    snap = gate.require_ready()
    assert snap.ready is True


def test_require_ready_raises_when_not_ready():
    gate = PurgeSafetyGate(DictGateEvidenceSource({}), clock=lambda: NOW)
    with pytest.raises(GateNotReady) as exc:
        gate.require_ready()
    assert exc.value.error_code == "PURGE_GATE_NOT_READY"
    assert exc.value.snapshot.ready is False
```

再补：缺文件（目录存在但空）、坏 JSON、`schema_version !== 1`、condition 与文件名不一致、`result: "expired"` 非法、文档自称过期不得绕过时钟。`FileGateEvidenceSource` 不得读取调用方任意相对路径（只拼 `CONDITION_IDS` 文件名）。

- [ ] **Step 2: 运行确认 RED**

Run:

```bash
cd backend
python -m pytest test/test_issue_23_gate_unit.py -v
```

Expected: FAIL（`ImportError: services.purge_safety_gate`）。

- [ ] **Step 3: 最小实现**

创建 `backend/services/purge_safety_gate.py`，按 Interfaces 实现。要点：

- `CONDITION_IDS` 顺序固定为 `daily_postgres_backup`、`instant_restore_point_capability`、`object_protection`、`independent_backup_credentials`、`recovery_drill`。
- `CONDITION_LABELS` 对应「数据库定期备份」「即时备份能力」「对象保护」「独立备份凭证」「恢复演练」。
- `FORBIDDEN_EVIDENCE_KEYS = frozenset({"password", "secret", "token", "authorization", "dsn"})`；`_contains_forbidden(value)` 对 `dict`/`list` **递归**。
- `FileGateEvidenceSource.load`：dir 为空/非目录 → `{}`；对每个 id 只读 `{id}.json`；`stat().st_size > 65536` 或读失败或非对象或敏感键或 schema/condition 不符 → 该项 `RawConditionEvidence(parse_error=True, result=None, verified_at=None, expires_at=None, summary=None)`。
- UTC 解析：`"Z"` 替换为 `"+00:00"` 后 `fromisoformat`；无 tzinfo 视为非法。
- `PurgeSafetyGate.__init__(self, source, *, clock=None)`；`clock` 缺省 `lambda: datetime.now(timezone.utc)`。
- `evaluate`：`try: raw = self.source.load(now) except Exception: 五项 unknown`。
- `def pipeline_available() -> bool: return False`
- `summary` 转 str 后切到 200 字符；`None` 保持 `None`。
- 本文件不得出现 `os.getenv` 读取时钟或备份凭证。

- [ ] **Step 4: 运行确认 GREEN**

Run:

```bash
cd backend
python -m pytest test/test_issue_23_gate_unit.py test/test_issue_23_auth_unit.py -v
```

Expected: PASS。

- [ ] **Step 5: 检查点** — 不 commit。

---

### Task 3: HTTP 蓝图、活动记录与装配

**Files:**
- Create: `backend/test/test_issue_23_api_unit.py`
- Create: `backend/blueprints/admin_purge.py`
- Modify: `backend/app.py`

**Interfaces:**
- Consumes: `AdminAuth.authenticate`、`PurgeSafetyGate.evaluate` / `require_ready`、`pipeline_available`、`CONDITION_LABELS`、`AssetActivityRecord`
- Produces:

| 方法 | 路径 |
|---|---|
| GET | `/api/admin/purge/readiness` |
| POST | `/api/admin/purge/batches` |
| POST | `/api/admin/purge/batches/<batch_id>/cancel` |
| POST | `/api/admin/purge/batches/<batch_id>/retry` |

`batch_id` 正则：`^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$`。

蓝图从 `current_app.config["ADMIN_AUTH"]` 与 `current_app.config["PURGE_SAFETY_GATE"]` 取值，便于测试替换。

错误码与中文（锁定）：

| error_code | HTTP | error |
|---|---|---|
| `AUTH_NOT_CONFIGURED` | 401 | 永久清除控制面未配置管理员令牌 |
| `AUTH_REQUIRED` | 401 | 需要管理员认证 |
| `AUTH_FORBIDDEN` | 403 | 当前身份无权执行永久清除操作 |
| `INVALID_PURGE_BATCH_ID` | 400 | 清除批次标识无效 |
| `PURGE_GATE_NOT_READY` | 409 | 永久清除安全门未满足 |
| `PURGE_PIPELINE_UNAVAILABLE` | 409 | 永久清除流水线尚未开放 |
| `PURGE_CONTROL_AUDIT_FAILED` | 500 | 操作记录写入失败 |
| `PURGE_CONTROL_FAILED` | 500 | 永久清除控制面暂时不可用 |

401/403 体只有 `error` + `error_code`，**无** `readiness`。409 带与 GET 同形的 `readiness`。

活动记录：

- 认证失败：`event_type='purge.auth.denied'`，`target_type='purge_gate'`，`target_id='purge-gate'`，`result='denied'`，`actor_id=None`，`after_state={"error_code": "<AUTH_*>"}` **仅此键**（architect #3a）。此路径不得调用 `evaluate`/`require_ready`。
- GET 成功：`purge.readiness.read` / `succeeded` / `after_state` 含 `purge_available`、`pipeline_available`、五项 `{id, status}`。
- 写拒绝：`purge.batch.{create,cancel,retry}.rejected`；create 的 `target_id='unspecified'`；非法 batch_id 的 `target_id='invalid'`。

判定顺序（写路由，#26 只替换第 4 步）：

1. `authenticate`
2. cancel/retry：batch_id 语法
3. `require_ready`
4. `pipeline_available()` 为 False → 409 `PURGE_PIPELINE_UNAVAILABLE`

#26 输入（architect #4，写入合同注释）：cancel/retry 同样执行步骤 3。批次执行中若证据过期，取消会被 409 `PURGE_GATE_NOT_READY` 挡住（fail-closed，数据保留）。豁免须另行授权。

- [ ] **Step 1: 写失败测试**

`backend/test/test_issue_23_api_unit.py` 使用 `create_app('testing')` + `db.create_all()`。每个测试在 `app.config` 注入 `ADMIN_AUTH` 与 `PURGE_SAFETY_GATE`。

至少包含：

```python
from app import create_app
from models import AssetActivityRecord, ImageAsset, db
from services.admin_auth import AdminAuth
from services.purge_safety_gate import (
    CONDITION_IDS,
    DictGateEvidenceSource,
    PurgeSafetyGate,
    RawConditionEvidence,
)

NOW_HEADERS = {"Authorization": "Bearer test-token", "X-Request-ID": "issue-23"}


def _app(source=None, token="test-token"):
    app = create_app("testing")
    with app.app_context():
        db.create_all()
    app.config["ADMIN_AUTH"] = AdminAuth(token, actor_id="admin")
    raw = source if source is not None else {}
    app.config["PURGE_SAFETY_GATE"] = PurgeSafetyGate(DictGateEvidenceSource(raw))
    return app


def test_unauthenticated_write_and_get_have_no_readiness():
    app = _app()
    client = app.test_client()
    for method, url in (
        ("get", "/api/admin/purge/readiness"),
        ("post", "/api/admin/purge/batches"),
        ("post", "/api/admin/purge/batches/batch-1/cancel"),
        ("post", "/api/admin/purge/batches/batch-1/retry"),
    ):
        response = getattr(client, method)(url)
        assert response.status_code in (401, 403)
        body = response.get_json()
        assert "readiness" not in body
        assert body["error_code"].startswith("AUTH_")


def test_admin_not_ready_get_and_create():
    app = _app({})
    client = app.test_client()
    ready = client.get("/api/admin/purge/readiness", headers=NOW_HEADERS)
    assert ready.status_code == 200
    assert ready.get_json()["purge_available"] is False
    assert ready.get_json()["pipeline_available"] is False
    assert len(ready.get_json()["conditions"]) == 5
    created = client.post("/api/admin/purge/batches", headers=NOW_HEADERS)
    assert created.status_code == 409
    assert created.get_json()["error_code"] == "PURGE_GATE_NOT_READY"
    assert "readiness" in created.get_json()


def _valid_source():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    evidence = RawConditionEvidence(
        result="valid",
        verified_at=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=12),
        summary="ok",
    )
    return {cid: evidence for cid in CONDITION_IDS}


def test_admin_ready_allow_path_is_pipeline_unavailable():
    app = _app(_valid_source())
    client = app.test_client()
    ready = client.get("/api/admin/purge/readiness", headers=NOW_HEADERS).get_json()
    assert ready["purge_available"] is True
    assert ready["pipeline_available"] is False
    for url in (
        "/api/admin/purge/batches",
        "/api/admin/purge/batches/batch-1/cancel",
        "/api/admin/purge/batches/batch-1/retry",
    ):
        response = client.post(url, headers=NOW_HEADERS)
        assert response.status_code == 409
        assert response.get_json()["error_code"] == "PURGE_PIPELINE_UNAVAILABLE"
        with app.app_context():
            assert ImageAsset.query.count() == 0


def test_auth_denied_after_state_only_error_code():
    app = _app()
    app.test_client().post("/api/admin/purge/batches")
    with app.app_context():
        record = AssetActivityRecord.query.filter_by(
            event_type="purge.auth.denied"
        ).one()
        assert record.after_state == {"error_code": record.error_code}
        assert "purge_available" not in record.after_state
        assert record.actor_id is None
        dumped = str(record.after_state)
        assert "Bearer" not in dumped
        assert "test-token" not in dumped


def test_invalid_batch_id_after_auth():
    app = _app()
    response = app.test_client().post(
        "/api/admin/purge/batches/batch@id/cancel",
        headers=NOW_HEADERS,
    )
    assert response.status_code == 400
    assert response.get_json()["error_code"] == "INVALID_PURGE_BATCH_ID"


def test_ordinary_asset_list_does_not_need_token():
    app = _app()
    response = app.test_client().get("/api/image-assets?assignment=unassigned")
    assert response.status_code == 200


def test_health_does_not_require_admin():
    app = _app(token=None)
    # sqlite health 可能 200 或 503，但不得 401
    assert app.test_client().get("/api/health").status_code != 401
```

非法 `batch_id` 使用能进入 Flask 路由、但通不过 `BATCH_ID_RE` 的值：`batch@id`（`@` 不在允许字符集）。路径：`/api/admin/purge/batches/batch@id/cancel`。

审计失败与未分类异常：

```python
from unittest import mock


def test_audit_failure_returns_500_and_does_not_open_pipeline():
    app = _app(_valid_source())
    with app.app_context():
        with mock.patch.object(
            db.session, "commit", side_effect=RuntimeError("audit boom")
        ):
            response = app.test_client().get(
                "/api/admin/purge/readiness", headers=NOW_HEADERS
            )
    assert response.status_code == 500
    assert response.get_json()["error_code"] == "PURGE_CONTROL_AUDIT_FAILED"


def test_unclassified_failure_returns_500_without_internal_text():
    app = _app({})
    app.config["PURGE_SAFETY_GATE"].evaluate = mock.Mock(
        side_effect=RuntimeError("secret-token-value exploded")
    )
    response = app.test_client().get(
        "/api/admin/purge/readiness", headers=NOW_HEADERS
    )
    assert response.status_code == 500
    body = response.get_json()
    assert body["error_code"] == "PURGE_CONTROL_FAILED"
    assert "secret-token-value" not in body["error"]
```

- [ ] **Step 2: 运行确认 RED**

Run:

```bash
cd backend
python -m pytest test/test_issue_23_api_unit.py -v
```

Expected: FAIL（404 或无法注册 `/api/admin/purge`）。

- [ ] **Step 3: 实现蓝图并装配**

`backend/blueprints/admin_purge.py`：`Blueprint("admin_purge", __name__, url_prefix="/api/admin/purge")`。

辅助：

```python
def _request_id():
    return (request.headers.get("X-Request-ID") or uuid.uuid4().hex)[:64]


def _readiness_body(snapshot) -> dict:
    return {
        "purge_available": snapshot.ready,
        "pipeline_available": pipeline_available(),
        "checked_at": snapshot.checked_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "conditions": [
            {
                "id": item.id,
                "label": CONDITION_LABELS[item.id],
                "status": item.status,
                "checked_at": None if item.checked_at is None else item.checked_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "expires_at": None if item.expires_at is None else item.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "summary": item.summary,
            }
            for item in snapshot.conditions
        ],
    }


def _record(**kwargs):
    db.session.add(AssetActivityRecord(source="api", **kwargs))
```

`authenticate` 失败：写 `purge.auth.denied`（`after_state={"error_code": error_code}`），commit，返回 401/403。commit 失败 → rollback，500 `PURGE_CONTROL_AUDIT_FAILED`。

GET：认证后 `evaluate()`，写 `purge.readiness.read`，返回 200。

写：认证 →（cancel/retry 校验 `BATCH_ID_RE`）→ `require_ready` → 若 `not pipeline_available()` 则 409 `PURGE_PIPELINE_UNAVAILABLE`。无任何批次 INSERT。

未分类异常：rollback，500 `PURGE_CONTROL_FAILED`，日志只记 `error_type` 与 `request_id`。

`backend/app.py` 的 `create_app`：

```python
from pathlib import Path
from services.admin_auth import AdminAuth
from services.purge_safety_gate import FileGateEvidenceSource, PurgeSafetyGate
from blueprints.admin_purge import admin_purge_bp

evidence_dir = os.getenv("PURGE_GATE_EVIDENCE_DIR")
app.config["ADMIN_AUTH"] = AdminAuth(
    os.getenv("PURGE_ADMIN_TOKEN"),
    actor_id=os.getenv("PURGE_ADMIN_ACTOR_ID", "admin"),
)
app.config["PURGE_SAFETY_GATE"] = PurgeSafetyGate(
    FileGateEvidenceSource(Path(evidence_dir) if evidence_dir else None)
)
app.register_blueprint(admin_purge_bp)
```

健康检查函数体不得引用 `ADMIN_AUTH` 或 `PURGE_SAFETY_GATE`。

- [ ] **Step 4: 运行确认 GREEN**

Run:

```bash
cd backend
python -m pytest test/test_issue_23_api_unit.py test/test_issue_23_auth_unit.py test/test_issue_23_gate_unit.py -v
```

Expected: PASS。

- [ ] **Step 5: 检查点** — 不 commit。

---

### Task 4: 静态合同（#26 边界锁）

**Files:**
- Create: `backend/test/test_issue_23_static_contract.py`

**Interfaces:**
- Consumes: Task 1–3 源文件字面量
- Produces: #26 不得破坏的扫描断言

- [ ] **Step 1: 写失败测试（若 Task 3 已满足则会直接绿；仍先写全量断言再跑）**

```python
from pathlib import Path
import re

BACKEND = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (BACKEND / rel).read_text(encoding="utf-8")


def test_urls_and_error_codes_locked():
    source = _read("blueprints/admin_purge.py")
    for needle in (
        "@admin_purge_bp.get('/readiness')",
        "@admin_purge_bp.post('/batches')",
        "@admin_purge_bp.post('/batches/<batch_id>/cancel')",
        "@admin_purge_bp.post('/batches/<batch_id>/retry')",
        "AUTH_NOT_CONFIGURED",
        "AUTH_REQUIRED",
        "AUTH_FORBIDDEN",
        "INVALID_PURGE_BATCH_ID",
        "PURGE_GATE_NOT_READY",
        "PURGE_PIPELINE_UNAVAILABLE",
        "PURGE_CONTROL_AUDIT_FAILED",
        "PURGE_CONTROL_FAILED",
    ):
        assert needle in source


def test_write_handlers_keep_decision_order():
    source = _read("blueprints/admin_purge.py")
    for marker in ("def create_purge_batch", "def cancel_purge_batch", "def retry_purge_batch"):
        start = source.index(marker)
        chunk = source[start:start + 2500]
        i_auth = chunk.index(".authenticate(")
        i_gate = chunk.index("require_ready(")
        i_pipe = chunk.index("pipeline_available(")
        assert i_auth < i_gate < i_pipe
    cancel = source[source.index("def cancel_purge_batch"):]
    retry = source[source.index("def retry_purge_batch"):]
    for chunk in (cancel, retry):
        i_auth = chunk.index(".authenticate(")
        i_syntax = chunk.index("INVALID_PURGE_BATCH_ID")
        i_gate = chunk.index("require_ready(")
        assert i_auth < i_syntax < i_gate
    # #26 只许替换第 4 步；cancel/retry 不得绕过 require_ready。


def test_pipeline_available_constant_false():
    gate = _read("services/purge_safety_gate.py")
    assert re.search(
        r"def pipeline_available\(\) -> bool:\s+return False\b",
        gate,
    )


def test_control_plane_does_not_touch_backup_or_kodo_or_enable_switches():
    combined = "\n".join(
        _read(path)
        for path in (
            "app.py",
            "blueprints/admin_purge.py",
            "services/admin_auth.py",
            "services/purge_safety_gate.py",
        )
    )
    lowered = combined.lower()
    for forbidden in (
        "BACKUP_OSS_",
        "PURGE_SOURCE_OSS_",
        "PURGE_RESTORE_OSS_",
        ".env.backup",
        "PURGE_ENABLED",
        "PURGE_GATE_NOW",
        "kodo",
    ):
        assert forbidden.lower() not in lowered
    assert "INSERT" not in combined.upper() or "asset_activity_records" in lowered
    assert "delete_object" not in lowered
    assert "session.delete" not in lowered
```

函数名必须与蓝图实现一致：`create_purge_batch` / `cancel_purge_batch` / `retry_purge_batch`。Step 3 若用了别的名字，以本测试为准回改蓝图。

- [ ] **Step 2: 运行**

Run:

```bash
cd backend
python -m pytest test/test_issue_23_static_contract.py -v
```

Expected: 若蓝图函数名或顺序不符则 FAIL；按断言修到 PASS。不要放宽合同。

- [ ] **Step 3: 对照既有静态合同未被破坏**

Run:

```bash
cd backend
python -m pytest test/test_issue_22_static_contract.py test/test_issue_21_api_static_contract.py test/test_purge_object_backup_contract.py -v
```

Expected: PASS。`test_purge_object_modules_are_not_wired_into_app_or_database_models` 仍应成立（本票不把对象备份模块接入 Flask）。

- [ ] **Step 4: 检查点** — 不 commit。

---

### Task 5: PostgreSQL 集成

**Files:**
- Create: `backend/test/integration/test_issue_23_purge_gate.py`

**Interfaces:**
- Consumes: 既有 `app` fixture（`image_search_test` 临时 schema）
- Produces: 活动记录在真实 PostgreSQL 可插入；无资产时亦可

- [ ] **Step 1: 写测试**

```python
import pytest
from models import AssetActivityRecord, db
from services.admin_auth import AdminAuth
from services.purge_safety_gate import DictGateEvidenceSource, PurgeSafetyGate

pytestmark = pytest.mark.postgresql


def test_readiness_and_denied_and_rejected_persist(app):
    app.config["ADMIN_AUTH"] = AdminAuth("pg-token", actor_id="admin")
    app.config["PURGE_SAFETY_GATE"] = PurgeSafetyGate(DictGateEvidenceSource({}))
    client = app.test_client()
    denied = client.get("/api/admin/purge/readiness")
    assert denied.status_code == 401
    ok = client.get(
        "/api/admin/purge/readiness",
        headers={"Authorization": "Bearer pg-token"},
    )
    assert ok.status_code == 200
    rejected = client.post(
        "/api/admin/purge/batches",
        headers={"Authorization": "Bearer pg-token"},
    )
    assert rejected.status_code == 409
    assert rejected.get_json()["error_code"] == "PURGE_GATE_NOT_READY"
    records = {row.event_type: row for row in AssetActivityRecord.query.all()}
    assert "purge.auth.denied" in records
    assert records["purge.auth.denied"].after_state == {
        "error_code": records["purge.auth.denied"].error_code
    }
    assert "purge.readiness.read" in records
    assert "purge.batch.create.rejected" in records


def test_unassigned_list_still_works_without_admin(app):
    response = app.test_client().get("/api/image-assets?assignment=unassigned")
    assert response.status_code == 200
```

- [ ] **Step 2: 运行**

Run:

```bash
cd backend
python -m pytest test/integration/test_issue_23_purge_gate.py -v
```

Expected: PASS。若输出 `SKIPPED` 因 PostgreSQL 不可达，报告必须写「未执行，不得当作通过」，停止本块，不得改断言绕过。

- [ ] **Step 3: 检查点** — 不 commit。

---

### Task 6: 前端只读准备状态

**Files:**
- Modify: `frontend/src/types/product.ts`
- Modify: `frontend/src/services/productApi.ts`
- Modify: `frontend/src/services/productApi.test.ts`
- Modify: `frontend/src/components/ArchivedAssetGrid.tsx`
- Modify: `frontend/src/components/ArchivedAssetGrid.test.tsx`
- Modify: `frontend/src/components/ProductUpload.test.tsx`（mock `getPurgeReadiness`，对照恢复）

**Interfaces:**
- Consumes: `GET /api/admin/purge/readiness`
- Produces: `getPurgeReadiness(token: string)`；回收站默认可折叠「管理员」面板；三态文案；无执行按钮

```typescript
export type PurgeConditionStatus = 'valid' | 'failed' | 'unknown' | 'expired';

export interface PurgeCondition {
  id: string;
  label: string;
  status: PurgeConditionStatus;
  checked_at: string | null;
  expires_at: string | null;
  summary: string | null;
}

export interface PurgeReadiness {
  purge_available: boolean;
  pipeline_available: boolean;
  checked_at: string;
  conditions: PurgeCondition[];
}
```

`getPurgeReadiness`：`fetch(..., { headers: { Authorization: \`Bearer ${token}\` } })`。401/403 抛带 `status` 的错误，供面板隐藏条件而不是整页失败。**不**封装 create/cancel/retry。

`sessionStorage` 键：`xiangyi.adminPurgeToken`。打开面板 / 保存 / 清除时各请求一次，不轮询。

固定文案（合同）：`安全门已满足，永久清除流水线尚未开放`

- [ ] **Step 1: 写失败测试**

在 `ArchivedAssetGrid.test.tsx` mock `../services/productApi` 的 `getPurgeReadiness` 与既有 `getImageUrl`。`beforeEach`：`sessionStorage.clear()`。

```tsx
it('keeps the admin panel collapsed and hides purge actions by default', () => {
  render(<ArchivedAssetGrid {...baseProps} />);
  expect(screen.queryByLabelText('管理员令牌')).not.toBeInTheDocument();
  expect(screen.queryByText('数据库定期备份')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', {
    name: /永久清除|彻底删除|清空回收站/,
  })).not.toBeInTheDocument();
  expect(api.getPurgeReadiness).not.toHaveBeenCalled();
});

it('does not show conditions when unauthorized after saving a token', async () => {
  vi.mocked(api.getPurgeReadiness).mockRejectedValue(
    Object.assign(new Error('需要管理员认证'), { status: 401 })
  );
  render(<ArchivedAssetGrid {...baseProps} />);
  fireEvent.click(screen.getByRole('button', { name: '管理员' }));
  fireEvent.change(screen.getByLabelText('管理员令牌'), {
    target: { value: 'bad-token' },
  });
  fireEvent.click(screen.getByRole('button', { name: '保存令牌' }));
  await waitFor(() => expect(api.getPurgeReadiness).toHaveBeenCalledWith('bad-token'));
  expect(screen.queryByText('数据库定期备份')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /永久清除/ })).not.toBeInTheDocument();
  expect(screen.queryByText('回收站加载失败')).not.toBeInTheDocument();
});

it('shows read-only conditions when authorized but not ready', async () => {
  vi.mocked(api.getPurgeReadiness).mockResolvedValue(notReadyPayload);
  // 展开、保存令牌后
  expect(await screen.findByText('数据库定期备份')).toBeInTheDocument();
  expect(screen.getByText(/未满足安全门/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /永久清除/ })).not.toBeInTheDocument();
});

it('shows pipeline-closed copy when the gate is ready', async () => {
  vi.mocked(api.getPurgeReadiness).mockResolvedValue(readyPayload);
  expect(await screen.findByText(
    '安全门已满足，永久清除流水线尚未开放'
  )).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /永久清除/ })).not.toBeInTheDocument();
});

it('clears the token and hides conditions', async () => {
  sessionStorage.setItem('xiangyi.adminPurgeToken', 'old');
  vi.mocked(api.getPurgeReadiness).mockResolvedValue(notReadyPayload);
  render(<ArchivedAssetGrid {...baseProps} />);
  fireEvent.click(screen.getByRole('button', { name: '管理员' }));
  await screen.findByText('数据库定期备份');
  fireEvent.click(screen.getByRole('button', { name: '清除令牌' }));
  expect(sessionStorage.getItem('xiangyi.adminPurgeToken')).toBeNull();
  expect(screen.queryByText('数据库定期备份')).not.toBeInTheDocument();
});
```

`productApi.test.ts`：断言 `getPurgeReadiness('abc')` 的 fetch URL 为 `/api/admin/purge/readiness` 且 header 含 `Bearer abc`；**不**导出 create/cancel/retry。

`ProductUpload.test.tsx`：mock `getPurgeReadiness`；既有「恢复选中图片」用例仍通过。

- [ ] **Step 2: 运行确认 RED**

Run:

```bash
cd frontend
npx vitest run src/components/ArchivedAssetGrid.test.tsx src/services/productApi.test.ts src/components/ProductUpload.test.tsx
```

Expected: FAIL（缺少 `getPurgeReadiness` 或「管理员」按钮）。

- [ ] **Step 3: 最小实现**

- types + `getPurgeReadiness`
- `ArchivedAssetGrid` 顶部 Ant Design `Collapse`，`defaultActiveKey={[]}`，标题「管理员」。`Input.Password` 的 `aria-label="管理员令牌"`。按钮「保存令牌」「清除令牌」。
- 未授权：不渲染五项。已授权未就绪：五项 + 「未满足安全门，永久清除不可用」。已授权已就绪：五项 + 固定流水线文案。永不渲染「永久清除」按钮。
- 401/403 只影响面板，不调用 `onRetry`、不改 `error`。

- [ ] **Step 4: 运行确认 GREEN**

Run:

```bash
cd frontend
npx vitest run src/components/ArchivedAssetGrid.test.tsx src/services/productApi.test.ts src/components/ProductUpload.test.tsx
```

Expected: PASS。

- [ ] **Step 5: 检查点** — 不 commit。

---

### Task 7: 文档与证据编写指引

**Files:**
- Modify: `CONTEXT.md`
- Modify: `AGENTS.md`
- Modify: `backend/.env.example`
- Create: `docs/operations/purge-gate-evidence.md`
- Modify: `docs/superpowers/specs/2026-08-22-issue-23-admin-auth-purge-gate-design.md`（用批准稿覆盖，并注明 architect APPROVE 与五条已吸收）

**Interfaces:**
- Consumes: 本计划吸收记录与 `/tmp/t9-design-rev.md`
- Produces: 仓库内可检索的领域词、控制面事实、证据编写约束

- [ ] **Step 1: CONTEXT.md 在「永久清除」之后插入**

```markdown
**永久清除安全门（Permanent Purge Safety Gate）**:
服务端对五项备份与恢复条件的合取判定。任一项未知、过期或失败则永久清除不可用。它不是前端按钮、不是环境开关。
_Avoid_: 环境开关、前端隐藏、回收站恢复

**安全门证据（Purge Gate Evidence）**:
ops 写入的、带过期时间的条件证明文档。应用只读文档，不在请求内执行备份或演练。能写证据目录的主体即能打开安全门，这是主机文件系统级信任边界，不属于本控制面的抗沦陷能力。
_Avoid_: 备份清单、环境开关、现场演练本身

**管理员令牌（Purge Admin Token）**:
仅用于永久清除控制面的共享秘密。它不是用户账号，也不保护搜索、导入、归款或回收站恢复。
_Avoid_: 登录会话、通用管理员账号
```

- [ ] **Step 2: AGENTS.md 只补当前事实**

- 关键文件增加：`backend/services/admin_auth.py`、`backend/services/purge_safety_gate.py`、`backend/blueprints/admin_purge.py`。
- 环境变量可选：`PURGE_ADMIN_TOKEN`、`PURGE_ADMIN_ACTOR_ID`、`PURGE_GATE_EVIDENCE_DIR`；未设置则控制面关闭。
- 正式图片工作流 / 备份段补一句：永久清除 HTTP 控制面已存在但默认关闭；`pipeline_available()` 为 False；真实启用仍待现场证据与后续票授权。能写证据目录即能让安全门报就绪，属主机信任边界。
- 前端段：回收站可折叠管理员面板只读准备状态，无执行按钮。

- [ ] **Step 3: `.env.example` 追加注释块**

```
# Permanent purge control plane (optional; unset keeps the gate closed)
# PURGE_ADMIN_TOKEN=
# PURGE_ADMIN_ACTOR_ID=admin
# PURGE_GATE_EVIDENCE_DIR=
```

不填示例真令牌。

- [ ] **Step 4: `docs/operations/purge-gate-evidence.md`**

写明五个文件名、JSON 合同、`expires_at` 由门判定、敏感键递归拒绝、64 KiB 上限、**`summary` 会原样返回管理员因此不得含秘密/路径凭证/DSN**（键名检查挡不住值泄漏）、目录权限是主机信任边界、T14 才做现场验证、本文件不是启用授权。

- [ ] **Step 5: 覆盖设计稿**

将批准内容写入 `docs/superpowers/specs/2026-08-22-issue-23-admin-auth-purge-gate-design.md`，文首增加：architect APPROVE；五条建议已按计划吸收。含 #26 取消仍过 `require_ready()` 的前瞻段落。

- [ ] **Step 6: 定向回归**

Run:

```bash
cd backend
python -m pytest \
  test/test_issue_23_auth_unit.py \
  test/test_issue_23_gate_unit.py \
  test/test_issue_23_api_unit.py \
  test/test_issue_23_static_contract.py \
  test/integration/test_issue_23_purge_gate.py \
  test/test_issue_22_static_contract.py \
  test/test_purge_object_backup_contract.py -v
```

```bash
cd frontend
npx vitest run src/components/ArchivedAssetGrid.test.tsx src/services/productApi.test.ts src/components/ProductUpload.test.tsx
```

Expected: 后端所列 PASS（集成若 skip 须在回报中标明未执行）；前端所列 PASS。

- [ ] **Step 7: 检查点** — 不 commit。停下等 risk_reviewer（主线程协调）。

---

## 给 #26 的输入（不得在本票实现）

- 稳定 URL 与判定顺序见 Task 4。#26 只替换 `pipeline_available()` 为真之后的第 4 步（创建/推进批次）。
- `require_ready()` 对 create / cancel / retry 一视同仁。执行中证据过期 → 取消被拒、批次数据保留。若需「取消豁免安全门」，必须作为边界变更另行授权，不能在 #26 内删掉 cancel/retry 的 `require_ready()`。
- 不要新增第二条不经本蓝图的创建 CLI/HTTP 入口。
- 确认文案「永久删除 N 张」、20 张上限、批次表、worker、备份调用、执行按钮均属 #26/#27。

## 明确不在本计划内

生产账号、云备份配置、部署、启用永久清除、Kodo、真实 OSS 写入、commit/push、建 worktree（Stage C 才做）。
