# Issue #23 管理员认证与永久清除安全门设计

- 日期：2026-08-22
- 状态：architect APPROVE；五条非阻塞建议已按实施计划吸收（load 整体异常→五项 unknown、两个 500 错误码入静态合同、auth.denied 仅 error_code、敏感键递归、Bearer 大小写不敏感、时钟仅测试注入、64 KiB 上限、#26 cancel/retry 仍过 require_ready）
- 基线：`refactor/image-search-pgvector@b81dc1c`（含 #15–#22、#24、#25）
- 范围：永久清除控制面——管理员令牌认证、五项安全门只读评估、拒绝写路径、回收站只读准备状态
- 非范围：生产账号、云备份配置、部署、启用永久清除、清除批次状态机、正式对象删除、Kodo、完整 RBAC

## 0. 已确认决策与审查补充

1. **认证（问题 1 = A）**：仅保护清除控制面的共享令牌 `PURGE_ADMIN_TOKEN`；`Authorization: Bearer`；未配置 / 空 / 仅空白即失败关闭。无用户表、无登录页。搜索 / 导入 / 归款 / 回收站恢复保持未认证可用。
   - 常量时间比较必须在哈希之后执行。
   - 错误路径不得靠时序区分「令牌未配置」与「令牌错误」。
   - 未配置 / 为空 / 仅空白的失败关闭各有独立测试。
2. **安全门（问题 2 = A）**：可注入证据探针；生产默认 `FileGateEvidenceSource`，目录缺失则五项 `unknown`。Flask 不加载 `BACKUP_*` / `PURGE_SOURCE_OSS_*` / `PURGE_RESTORE_OSS_*`，不跑备份 CLI，不访问 OSS。未知 / 过期 / 失败 → 永久清除不可用。禁止用环境开关打开永久清除。
   - 时钟注入只存在于测试接缝，禁止运行时用环境变量注入「当前时间」。
   - 能写 `PURGE_GATE_EVIDENCE_DIR` 的主体即能打开安全门。这是主机文件系统级信任边界，属于部署策略与 T14 现场验证；本票不解决主机沦陷，但必须显式记录，不得让读者以为门本身抗主机沦陷。
3. **写接口（问题 3 = A）**：本票挂上创建 / 取消 / 重试路由，只做授权与安全门。允许路径终止于 409 `PURGE_PIPELINE_UNAVAILABLE`，不落批次、不调备份、不删对象。
   - 三条 URL、判定顺序、401 / 403 / 400 / 两种 409、以及 `pipeline_available()` 恒 `False`，写成静态合同测试，作为 #26 边界锁：#26 只允许替换第 4 步，不得改前三段或另开入口。
4. **前端（问题 4 = A）**：回收站可折叠管理员区 + 可选令牌（`sessionStorage`）。任何状态都没有「永久清除」执行按钮。
   - 面板默认折叠关闭；清除令牌动作要有测试。
   - 三态文案与「安全门已满足，永久清除流水线尚未开放」进入前端合同测试，防止后续票误加执行按钮。

后续 #26 / #27 复用本票的认证、安全门、`require_ready()` 与写路由 URL。

## 1. 目标与不变量

为永久清除建立最小控制面：未认证或非管理员既看不到也无法调用危险能力；管理员只能看到服务端验证后的只读准备状态。本票结束时生产与默认测试环境的永久清除仍不可用。

必须保持：

- 前端隐藏不是权限控制。创建 / 取消 / 重试必须在服务端拒绝。
- `PURGE_ADMIN_TOKEN` 未设置、为空或仅空白时，清除控制面全部失败关闭。
- 五项条件任一项缺失、无法解析、结果不是 `valid`、已过期或校验过程抛错 → `purge_available=false`，写路径不得进入流水线。
- 不得出现 `PURGE_ENABLED=1` 之类只需翻转即可开放清除的开关。
- 认证失败、准备状态读取、已认证管理员的写尝试都写入 `asset_activity_records`；禁止写入令牌、`Authorization`、密钥、DSN、签名 URL、图片内容或向量。
- `/api/health`、应用启动、普通图片 API 不得评估安全门或加载备份凭证。
- 不新增用户表或 schema 迁移；复用既有活动记录表。
- 不创建生产账号、不配置云备份、不部署、不启用永久清除。

## 2. 方案比较（已收敛，详见 /tmp/t9-q1.md … q4.md）

- 认证：共享令牌（采用）优于反向代理身份头、用户名密码会话。
- 安全门：可注入探针 + 本地证据文档（采用）优于 Flask 解析 #24/#25 manifest、五个环境开关。
- 写路径：本票挂路由并拒绝到流水线未交付（采用）优于本票落 queued 批次、本票只做 GET。
- 前端：可选令牌且永不展示执行按钮（采用）优于仅 mock、门就绪就显示按钮。

## 3. 模块与接缝

两个深模块；Flask 蓝图只做 HTTP 适配、请求标识、活动记录提交。

### 3.1 `services.admin_auth`

```python
@dataclass(frozen=True)
class AdminPrincipal:
    actor_id: str
    role: str  # 恒为 "admin"

class AdminAuthError(Exception):
    error_code: str  # AUTH_NOT_CONFIGURED | AUTH_REQUIRED | AUTH_FORBIDDEN

class AdminAuth:
    def __init__(self, expected_token: str | None, actor_id: str = "admin"): ...
    def authenticate(self, authorization_header: str | None) -> AdminPrincipal:
        """成功返回管理员；否则抛 AdminAuthError。"""
```

环境变量：

- `PURGE_ADMIN_TOKEN`：清除控制面共享秘密。名称明确指向永久清除，不得复用为其他功能的通用管理员令牌。
- `PURGE_ADMIN_ACTOR_ID`：活动记录用操作者标识，缺省 `"admin"`。**绝不是令牌或其哈希。**

Fail-closed 与时序：

- `expected_token` 经 `strip` 后为空 → `AUTH_NOT_CONFIGURED`。未设置、空字符串、仅空白三条路径各有独立测试。
- 缺少头、不是恰好一个 `Bearer` 词法单元、Bearer 令牌为空 → `AUTH_REQUIRED`。
- 已配置且 Bearer 存在但 mismatch → `AUTH_FORBIDDEN`。
- 比较协议：对「请求中的令牌」（缺头时用固定空字节）做 SHA-256；对「期望令牌」做 SHA-256；再 `hmac.compare_digest`。期望令牌未配置时，仍对请求摘要与预计算的哑摘要执行同一次 `compare_digest`，使「未配置」与「令牌错误」的哈希 + 比较工作量一致。禁止在未配置分支提前 `return` 以跳过哈希。
- 错误码仍可区分 `AUTH_NOT_CONFIGURED` 与 `AUTH_FORBIDDEN`（运维需要知道控制面是否已配置）；**禁止**用分支耗时、日志或异常文本携带令牌。
- 本模块不读数据库、不写活动记录、不评估安全门。

测试用真模块 + 测试令牌，不另做「永远放行」的 FakeAuth。

### 3.2 `services.purge_safety_gate`

```python
ConditionId = Literal[
    "daily_postgres_backup",
    "instant_restore_point_capability",
    "object_protection",
    "independent_backup_credentials",
    "recovery_drill",
]

CONDITION_IDS: tuple[ConditionId, ...]  # 固定上述顺序

@dataclass(frozen=True)
class ConditionSnapshot:
    id: ConditionId
    status: Literal["valid", "failed", "unknown", "expired"]
    checked_at: datetime | None  # UTC
    expires_at: datetime | None  # UTC
    summary: str | None

@dataclass(frozen=True)
class GateSnapshot:
    ready: bool
    checked_at: datetime  # UTC
    conditions: tuple[ConditionSnapshot, ...]  # 恰好五项，顺序同 CONDITION_IDS

class GateNotReady(Exception):
    error_code = "PURGE_GATE_NOT_READY"
    snapshot: GateSnapshot

@dataclass(frozen=True)
class RawConditionEvidence:
    """探针只负责取证；过期与非法值由门判定。"""
    result: object
    verified_at: object
    expires_at: object
    summary: object
    parse_error: bool = False

class GateEvidenceSource(Protocol):
    def load(self, now: datetime) -> Mapping[ConditionId, RawConditionEvidence]: ...

class PurgeSafetyGate:
    def __init__(self, source: GateEvidenceSource, *, clock=None): ...
    def evaluate(self, now: datetime | None = None) -> GateSnapshot: ...
    def require_ready(self, now: datetime | None = None) -> GateSnapshot: ...

def pipeline_available() -> bool:
    """T9 恒为 False。#26 替换为真实流水线是否已交付，仍必须与 ready 合取。"""
```

时钟：

- 生产装配不传 `clock`，门使用 `datetime.now(timezone.utc)`。
- `clock` / `now=` 只出现在单元测试与集成测试构造处。
- 禁止 `PURGE_GATE_NOW` 或任何环境变量覆盖当前时间。静态合同测试扫描新模块不得出现这类环境变量名。

`evaluate` 聚合规则（fail-closed，唯一判定点）：

- 对每个 `CONDITION_IDS` 向 source 取 `RawConditionEvidence`。
- source 缺该项、返回未知 id、抛错 → 该项 `unknown`。
- `parse_error=True` 或 `result` 不是 `valid`/`failed` → `failed`。
- `result=failed` → `failed`（不再看时间）。
- `verified_at` / `expires_at` 不是可解析的 UTC datetime → `failed`。
- `verified_at > now + 60s` → `unknown`。
- `expires_at <= now` → `expired`。
- 仅当 `result=valid` 且时间合法且未过期 → `valid`。
- 五项都是 `valid` → `ready=True`；否则 `False`。
- `summary` 截断到 200 字符。
- 不缓存；每次请求重新 `load`。
- 不读取 `.env.backup`，不 import `postgres_backup` / `purge_object_backup` / `purge_object_storage` / `kodo_*`。

`FileGateEvidenceSource(evidence_dir: Path | None)`：

- `evidence_dir` 为 `None`、不存在或不是目录 → 不返回任何项（门视为五项 `unknown`）。
- 只读取固定文件名 `{condition_id}.json`，禁止拼接调用方路径。
- 读文件失败、非 UTF-8、非 JSON 对象、`schema_version !== 1`、`condition` 与文件名不一致、出现 `password` / `secret` / `token` / `authorization` / `dsn` 键（大小写不敏感）→ `parse_error=True`。
- 未知字段忽略。不在 Adapter 里判定过期。

`DictGateEvidenceSource`：测试用内存探针。缺项由门视为 `unknown`。

证据文档合同：

```json
{
  "schema_version": 1,
  "condition": "daily_postgres_backup",
  "result": "valid",
  "verified_at": "2026-08-22T04:00:00Z",
  "expires_at": "2026-08-23T05:00:00Z",
  "summary": "daily-2026-08-22 complete, copies verified"
}
```

`result` 只允许 `valid` | `failed`。文档若写 `expired` 视为非法 → `failed`。过期只由门根据 `expires_at` 与时钟判定。

| id | 只读展示名 |
|---|---|
| `daily_postgres_backup` | 数据库定期备份 |
| `instant_restore_point_capability` | 即时备份能力 |
| `object_protection` | 对象保护 |
| `independent_backup_credentials` | 独立备份凭证 |
| `recovery_drill` | 恢复演练 |

本票不验证这些证据在云上是否真实。没有新鲜合法文档就不能开放。现场真实性属于 ops / #24 / #25 / T14。

#### 信任边界（必须写入实施计划）

能在应用进程可读位置写入 `PURGE_GATE_EVIDENCE_DIR` 下五个 JSON 的主体，就能把五项打成 `valid` 从而让 `require_ready()` 通过。这与能改应用环境、能写容器文件系统的权限等价，**不是**门模块能防御的威胁。本票把该边界记录为部署策略：证据目录权限、主机准入、以及 T14 对证据来源的现场验证。设计与测试证明的是：缺文档 / 过期 / 非法文档时 fail-closed，以及应用不持有备份凭证。它们不证明「门抗主机沦陷」。

### 3.3 Flask 装配

`create_app`：

- `PURGE_ADMIN_TOKEN = os.getenv("PURGE_ADMIN_TOKEN")`
- `PURGE_ADMIN_ACTOR_ID = os.getenv("PURGE_ADMIN_ACTOR_ID", "admin")`
- `PURGE_GATE_EVIDENCE_DIR = os.getenv("PURGE_GATE_EVIDENCE_DIR")`（可空 → File source 的 dir=None）
- `AdminAuth` 与 `PurgeSafetyGate` 放入 `app.config`，测试可替换 `PURGE_SAFETY_GATE` / `ADMIN_AUTH`。测试替换时钟只通过构造 `PurgeSafetyGate(..., clock=...)`，不设环境变量。
- 注册蓝图 `admin_purge_bp`，url prefix `/api/admin/purge`。
- **不** `load_dotenv(.env.backup)`，**不**把备份凭证键拷进 Flask config。
- `/api/health` 不调用认证或安全门。

`.env.example` 只增加注释说明可选变量：默认不设置令牌、不设置证据目录，控制面关闭。不填写示例真令牌。

既有 CORS 已允许 `Authorization` 请求头，本票不改 CORS 策略。

## 4. HTTP 合同

统一请求头：`X-Request-ID` 可选，缺省 UUID hex，截断 64。活动记录 `source='api'`。

错误体固定 `{ "error": "<中文>", "error_code": "<CODE>" }`。门未就绪或流水线未交付时额外带 `readiness`（与 GET 体同形）。未认证 / 非管理员的响应**不得**带 `readiness`。

### 4.1 `GET /api/admin/purge/readiness`

| 认证 | HTTP | error_code | 体 |
|---|---|---|---|
| 未配置令牌 | 401 | `AUTH_NOT_CONFIGURED` | 仅 error |
| 缺 / 畸形 Bearer | 401 | `AUTH_REQUIRED` | 仅 error |
| Bearer 不匹配 | 403 | `AUTH_FORBIDDEN` | 仅 error |
| 管理员 | 200 | — | readiness；`purge_available` 随门；`pipeline_available` 恒 false |

200 体：

```json
{
  "purge_available": false,
  "pipeline_available": false,
  "checked_at": "2026-08-22T12:00:00Z",
  "conditions": [
    {
      "id": "daily_postgres_backup",
      "label": "数据库定期备份",
      "status": "unknown",
      "checked_at": null,
      "expires_at": null,
      "summary": null
    }
  ]
}
```

`conditions` 恰好五项，顺序同表。即使全部 `valid`，T9 的 `pipeline_available` 仍为 `false`。

### 4.2 写路由（本票全部不落库）

- `POST /api/admin/purge/batches`
- `POST /api/admin/purge/batches/<batch_id>/cancel`
- `POST /api/admin/purge/batches/<batch_id>/retry`

`<batch_id>` 必须匹配 `^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$`。不匹配：认证通过后 400 `INVALID_PURGE_BATCH_ID`，并写对应 rejected 活动记录。创建路由本票不解析 JSON 体。

判定顺序（#26 只替换第 4 步）：

1. `authenticate` → 401 / 403，写 `purge.auth.denied`，结束。
2. 路径 `batch_id` 语法（仅 cancel / retry）。
3. `require_ready` → 409 `PURGE_GATE_NOT_READY` + `readiness`，写 `purge.batch.*.rejected`。
4. T9：409 `PURGE_PIPELINE_UNAVAILABLE` + `readiness`（`purge_available=true` 且 `pipeline_available=false`），写 rejected。无 INSERT。

允许路径 = 步骤 1 与步骤 3 通过，停在步骤 4。测试断言 409 `PURGE_PIPELINE_UNAVAILABLE`，不是 401 / 403。

两种 409 的区分：`PURGE_GATE_NOT_READY` 表示认证已过但五项未全部 valid；`PURGE_PIPELINE_UNAVAILABLE` 表示认证与安全门都过，流水线未交付。静态合同必须锁住这两个 `error_code` 字符串、三条 URL、以及源码中步骤 1–3 的存在。

## 5. 活动记录

复用 `asset_activity_records`，不建新表。认证失败时 `actor_id` 为 `null`，成功时为 principal。`batch_id` 仅 cancel / retry 有合法路径 id 时填写。

| 场景 | event_type | target_type | target_id | result | error_code |
|---|---|---|---|---|---|
| 认证失败（含 GET / 写） | `purge.auth.denied` | `purge_gate` | `purge-gate` | `denied` | `AUTH_*` |
| GET readiness 成功 | `purge.readiness.read` | `purge_gate` | `purge-gate` | `succeeded` | null |
| POST create 门未就绪或流水线关闭 | `purge.batch.create.rejected` | `purge_batch` | `unspecified` | `rejected` | `PURGE_GATE_NOT_READY` 或 `PURGE_PIPELINE_UNAVAILABLE` |
| POST cancel 门未就绪、流水线关闭或 batch_id 非法 | `purge.batch.cancel.rejected` | `purge_batch` | `<batch_id>` 或 `invalid` | `rejected` | 同上或 `INVALID_PURGE_BATCH_ID` |
| POST retry 同上 | `purge.batch.retry.rejected` | `purge_batch` | `<batch_id>` 或 `invalid` | `rejected` | 同上 |

`after_state` 只允许：`purge_available`、`pipeline_available`、五项 `{id, status}`、`error_code`。禁止令牌、头、文件绝对路径、密钥类键、证据原文。

同一请求认证失败只写 `purge.auth.denied`。活动记录与响应同一请求提交：插入失败则 rollback，返回 500 `PURGE_CONTROL_AUDIT_FAILED`，写路径仍无批次。审计失败不得改成放行。

## 6. 前端

范围限于产品管理页回收站标签。

- `sessionStorage` 键：`xiangyi.adminPurgeToken`。不用 `localStorage`。
- 仅 `productApi.getPurgeReadiness()` 附加 `Authorization: Bearer`。
- 回收站工具区可折叠「管理员」面板：**默认折叠关闭**。令牌输入 `type="password"`，可保存、可清除。无令牌不请求 readiness。只在打开面板、保存令牌、清除令牌时请求一次，不轮询。
- 三态：
  1. 未授权（无令牌或 401/403）：不展示五项条件，不展示任何清除按钮；令牌输入保留。401/403 不升级为整页「回收站加载失败」。
  2. 已授权未就绪：只读五项，说明未满足安全门、永久清除不可用。无执行按钮。
  3. 已授权已就绪：只读五项，固定文案 **「安全门已满足，永久清除流水线尚未开放」**。无执行按钮。
- 本票不封装 create / cancel / retry。前端测试不调用写接口，并断言页面没有「永久清除」按钮 / `aria-label`。
- 恢复选中图片、搜索、分页与 #17 保持不变。

## 7. 测试接缝

禁止：`test/test.py`、`test/test_pgvector.py`、`test/benchmark_search.py`、真实 OSS / Kodo / DashScope、把定向测试扩成全量。集成只连本机 `image_search_test`。

### 7.1 后端单元

- `AdminAuth`：未设置、空字符串、仅空白（三条独立 fail-closed）；缺头；畸形头；错误令牌；正确令牌；strip；「未配置」与「错误令牌」都执行哈希 + `compare_digest`；异常 / 日志不含令牌。
- `PurgeSafetyGate` + File source：缺目录、缺文件、坏 JSON、错误 schema、condition 不匹配、非法 result、敏感键、未来 `verified_at`、过期、五项 valid、source 抛错。时钟只通过构造注入。
- 蓝图：
  - 未认证 / 非管理员：GET 与三条写路由 401 或 403，无 `readiness`。
  - 管理员 + 未就绪：GET 200 `purge_available=false`；写 409 `PURGE_GATE_NOT_READY`。
  - 管理员 + 五项 valid：GET 200 `purge_available=true` 且 `pipeline_available=false`；写 409 `PURGE_PIPELINE_UNAVAILABLE`；无新批次表、无 `image_assets` 变化。
  - 活动记录不含令牌。
  - 无令牌时既有 image-assets / archived / restore / imports / search 对照仍通过。

### 7.2 静态合同（#26 边界锁）

- 三条写 URL 与 GET readiness 字面量存在于蓝图。
- 判定顺序在源码中按 认证 → batch_id 语法 → `require_ready` → `pipeline_available` 出现；#26 只应替换最后一步的返回，不得删除前三段。
- `error_code` 字面量锁定：`AUTH_NOT_CONFIGURED`、`AUTH_REQUIRED`、`AUTH_FORBIDDEN`、`INVALID_PURGE_BATCH_ID`、`PURGE_GATE_NOT_READY`、`PURGE_PIPELINE_UNAVAILABLE`。
- `pipeline_available()` 源码恒返回 `False`。
- 不存在 `PURGE_ENABLED`、`PURGE_GATE_NOW` 或等价时钟环境变量。
- `app.py` / 新蓝图 / 新服务不出现 `BACKUP_OSS_`、`PURGE_SOURCE_OSS_`、`PURGE_RESTORE_OSS_`、`.env.backup`、`kodo`。
- 写路由函数体不含 INSERT 清除批次、不含对象 Delete。

### 7.3 集成（`image_search_test`）

- 真实 PostgreSQL 写入 `purge.readiness.read`、`purge.auth.denied`、`purge.batch.create.rejected`。
- 无资产时控制面事件也可插入。
- 不得把 skip 说成通过。

### 7.4 前端（RTL + mock API）

- 面板默认折叠；展开后可见令牌输入。
- 未授权：无五项、无「永久清除」按钮；恢复按钮在选择后仍可用。
- 已授权未就绪：五项只读，无执行按钮。
- 已授权已就绪：可见固定文案「安全门已满足，永久清除流水线尚未开放」，无执行按钮。
- 清除令牌后回到未授权态（不展示五项）。
- 页面查询不到永久清除执行按钮 / 对应 aria-label。
- 对照：移入回收站 / 恢复 / 导入入口不被管理员面板破坏。

## 8. 给 #26 / #27 的接口

```python
current_app.config["ADMIN_AUTH"].authenticate(header) -> AdminPrincipal
current_app.config["PURGE_SAFETY_GATE"].require_ready() -> GateSnapshot
pipeline_available() -> bool  # T9 False
```

HTTP 稳定 URL 见 §4.2。#26 不得再开第二条不经安全门的创建入口。#27 正式删除仍受本安全门约束；默认证据缺失即不可用。真实启用等待 T14 人工授权与现场证据，不是环境开关。

## 9. 失败模式与回滚

| 失败 | 行为 |
|---|---|
| 令牌未配置 | 控制面全关，普通图库不受影响 |
| 证据目录空 | GET 管理员可见五项 unknown；写 409 门未就绪 |
| 时钟歪斜（verified_at 在未来） | 该项 unknown |
| 活动记录写入失败 | 500，写路径仍无批次 |
| 主机可写证据目录 | 可伪造 valid 文档（信任边界，见 §3.2）；本票不防御 |
| 模块抛未分类异常 | 500 `PURGE_CONTROL_FAILED`，不泄露内部异常文本 |

回滚：还原应用代码即可。无 schema 迁移、无对象变更。留下的活动记录与证据 JSON 不是清除授权。

## 10. 领域语言（实施时写入 CONTEXT.md，本阶段不改仓库文件）

- **永久清除安全门**：服务端对五项备份与恢复条件的合取判定。任一项未知、过期或失败则永久清除不可用。它不是前端按钮、不是环境开关。
- **安全门证据**：ops 写入的、带过期时间的条件证明文档。应用只读文档，不在请求内执行备份或演练。能写证据目录即能开门，属主机信任边界。
- **管理员令牌**：仅用于永久清除控制面的共享秘密（`PURGE_ADMIN_TOKEN`）。它不是用户账号，也不保护搜索、导入、归款或回收站恢复。

不新增 ADR：ADR-0005 已固定「无认证则关闭永久清除」。令牌 vs RBAC 是本规格的实施选择。

## 11. 明确不做

- 用户表、密码、Cookie 会话、OAuth、多角色 RBAC
- 把认证加到搜索 / 导入 / 归款 / 恢复 / 预览
- 在健康检查或启动时评估安全门
- 创建或调用每日备份、即时恢复点、对象备份、隔离恢复
- 加载 `.env.backup` 或任何备份 / 清除专用 OSS 凭证
- 持久清除批次表、worker、确认文案「永久删除 N 张」、20 张上限校验（#26）
- 正式 OSS 删除、向量删除、共享预览判定（#27）
- 前端永久清除执行按钮与二次确认框
- 运行时可配置的安全门时钟
- Kodo 代码与测试
- commit / push / 改 GitHub 状态 / 部署 / 进 worktree
