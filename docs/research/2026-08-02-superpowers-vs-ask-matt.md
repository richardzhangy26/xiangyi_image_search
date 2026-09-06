# Superpowers 与 ask-matt 工作流选型研究

> 核对日期：2026-08-02（Asia/Shanghai）
>
> 研究范围：官方 GitHub 仓库、README、技能源码、发布页、许可证；OpenAI、Anthropic、DeepSeek 与 Qoder 官方模型/价格/集成文档；项目本地的 `CONTEXT.md`、ADR、Agent 配置与现有工作流产物。
>
> 源码快照：Superpowers [`44c9b2d`](https://github.com/obra/superpowers/tree/44c9b2d6e889982ac18c27d05a19fefe335194e1)，mattpocock/skills [`2ab9580`](https://github.com/mattpocock/skills/tree/2ab958093e83e0ec752e6c1c5932da465bf23e0c)。下文对行为的描述以这两个固定快照为准，不把可能继续变化的 `main` 当成永恒事实。

## 结论先行

这不是“二选一且全局只能装一个”的问题，而是两种不同的治理强度：

- **本项目默认用 mattpocock/skills 做工作流控制面**：`/ask-matt` 路由，`/grill-with-docs` 沉淀领域语言和 ADR，`/to-spec`、`/to-tickets` 对接 GitHub Issues，普通票据用 `/implement`。本仓库已经具备它依赖的 [`CONTEXT.md`](../../CONTEXT.md)、[`docs/agents/`](../agents/) 和 ADR，并且仓库内 `.agents/skills` 与本次核对的官方快照一致，迁移成本最低。
- **把 Superpowers 作为高风险执行通道**：数据库或对象存储迁移、权限/安全、向量模型或 schema 变更、跨前后端且需长时间无人值守的改动。它的隔离工作区、逐任务审查、修复熔断、持久化进度账本、最终整枝审查和“新鲜证据后才能宣称完成”更适合这些失败代价高的工作。[Superpowers 的完整主流程](https://github.com/obra/superpowers/blob/44c9b2d6e889982ac18c27d05a19fefe335194e1/README.md#L196-L245)明确把这些门禁串在一起。
- **不要在同一个 ticket 上叠两套完整流程**。每一阶段只能有一个所有者：选一个访谈/设计流程、一个拆分产物、一个实现与审查循环。否则会重复访谈、重复计划、重复 TDD 和重复 review，省下的模型单价会被流程调用次数吃掉。
- **模型分层的经济学正好相反**：Superpowers 用昂贵的设计与详细计划，把多数实现降成便宜模型可执行的“抄写 + 测试”；mattpocock/skills 故意让 spec/ticket 保持耐久、少写文件路径和代码，因此计划开销更小，但实现者通常需要更强的代码库理解能力。若任务量足够多、可机械执行，前者容易摊薄规划成本；若只是一个普通小功能，后者通常更省。

## 先纠正名称

`ask-matt` **不是与 Superpowers 等量齐观的一整个库**；它是 mattpocock/skills 中一个只负责选路、不亲自实现工作的 router，并且设置了 `disable-model-invocation: true`，必须由用户显式调用。[`ask-matt` 源码](https://github.com/mattpocock/skills/blob/2ab958093e83e0ec752e6c1c5932da465bf23e0c/skills/engineering/ask-matt/SKILL.md#L1-L15)与[官方技能目录的调用语义](https://github.com/mattpocock/skills/blob/2ab958093e83e0ec752e6c1c5932da465bf23e0c/.agents/invocation.md#L1-L10)都明确了这一点。准确的比较对象应是：

- `obra/superpowers` 整套方法论；
- `mattpocock/skills` 整套工程技能，其中 `/ask-matt` 是入口路由器。

截至上述快照，官方工程技能目录中没有 `/to-ticket`（单数）、`/to-spike` 或 `/renew-stock`。现有准确名称及最接近的功能是：

| 用户表述 | 当前官方名称或近似功能 | 说明 |
| --- | --- | --- |
| to ticket | `/to-tickets` | 把已理解的 spec/对话切成带阻塞边的 tracer-bullet tickets。[源码](https://github.com/mattpocock/skills/blob/2ab958093e83e0ec752e6c1c5932da465bf23e0c/skills/engineering/to-tickets/SKILL.md#L1-L38) |
| to spike | `/prototype`（若指技术/UI spike）或 `/research`（若指资料调查） | `/prototype` 明确是回答一个设计问题的可丢弃代码；逻辑分支做交互式终端程序，UI 分支做多个可切换方案。[源码](https://github.com/mattpocock/skills/blob/2ab958093e83e0ec752e6c1c5932da465bf23e0c/skills/engineering/prototype/SKILL.md#L6-L26) |
| renew stock | **无法从当前官方目录确认** | 若原意是“review work”，最接近 `/code-review`；若是整理外部 backlog，最接近 `/triage`。正式自动化前应先确认原词，不能按猜测绑定命令。 |

## 两套方法论的本质差异

| 维度 | Superpowers | mattpocock/skills（含 ask-matt） |
| --- | --- | --- |
| 产品形态 | 从会话启动即介入的完整开发方法论；相关技能是强制流程，不只是建议。[README](https://github.com/obra/superpowers/blob/44c9b2d6e889982ac18c27d05a19fefe335194e1/README.md#L1-L20) | 小而可组合的技能集合，强调用户控制和可改造；`ask-matt` 只负责把场景路由到合适流程。[README](https://github.com/mattpocock/skills/blob/2ab958093e83e0ec752e6c1c5932da465bf23e0c/README.md#L11-L20) |
| 触发方式 | `using-superpowers` 要求任何响应或动作前先检查技能，设计、调试等流程会自动介入。[源码](https://github.com/obra/superpowers/blob/44c9b2d6e889982ac18c27d05a19fefe335194e1/skills/using-superpowers/SKILL.md#L10-L31) | 编排技能通常由人显式调用；TDD、诊断、prototype、review 等可由模型自动调用。用户调用与模型调用被明确分层。[调用规则](https://github.com/mattpocock/skills/blob/2ab958093e83e0ec752e6c1c5932da465bf23e0c/.agents/invocation.md#L3-L16) |
| 设计入口 | 每项创作型工作先 brainstorming；即使很小也要呈现设计并获批，再写设计文档与计划。[源码](https://github.com/obra/superpowers/blob/44c9b2d6e889982ac18c27d05a19fefe335194e1/skills/brainstorming/SKILL.md#L8-L32) | 有代码库时由 `/grill-with-docs` 访谈并更新领域文档；无代码库用 `/grill-me`；不确定时才问 `/ask-matt`。[路由图](https://github.com/mattpocock/skills/blob/2ab958093e83e0ec752e6c1c5932da465bf23e0c/skills/engineering/ask-matt/SKILL.md#L13-L32) |
| 规划产物 | implementation plan 包含精确文件、完整代码、命令、预期输出，步骤通常 2–5 分钟；禁止占位符。[源码](https://github.com/obra/superpowers/blob/44c9b2d6e889982ac18c27d05a19fefe335194e1/skills/writing-plans/SKILL.md#L36-L52) [任务模板](https://github.com/obra/superpowers/blob/44c9b2d6e889982ac18c27d05a19fefe335194e1/skills/writing-plans/SKILL.md#L79-L148) | spec/ticket 记录问题、行为、接口和决策，刻意避免易过期的文件路径与代码；ticket 是可独立演示的跨层垂直切片，带阻塞边。[to-spec](https://github.com/mattpocock/skills/blob/2ab958093e83e0ec752e6c1c5932da465bf23e0c/skills/engineering/to-spec/SKILL.md#L7-L19) [to-tickets](https://github.com/mattpocock/skills/blob/2ab958093e83e0ec752e6c1c5932da465bf23e0c/skills/engineering/to-tickets/SKILL.md#L25-L65) |
| 上下文策略 | 每 task 新 implementer；brief、report、diff package 和 plan-scoped ledger 落盘，避免 controller context 与压缩后重复执行。[源码](https://github.com/obra/superpowers/blob/44c9b2d6e889982ac18c27d05a19fefe335194e1/skills/subagent-driven-development/SKILL.md#L110-L143) | grill → spec → tickets 尽量保留在一个连续 context；之后每个 `/implement` 清空 context 单独做。超过 smart zone 时用 `/handoff` 跨会话。[源码](https://github.com/mattpocock/skills/blob/2ab958093e83e0ec752e6c1c5932da465bf23e0c/skills/engineering/ask-matt/SKILL.md#L28-L32) |
| 实现编排 | 逐 task 单个 implementer，随后 task review；禁止同一工作树并行多个实现者，失败到第 4–5 轮升级模型，最多 5 轮后熔断与人工裁决。[源码](https://github.com/obra/superpowers/blob/44c9b2d6e889982ac18c27d05a19fefe335194e1/skills/subagent-driven-development/SKILL.md#L194-L250) [修复循环](https://github.com/obra/superpowers/blob/44c9b2d6e889982ac18c27d05a19fefe335194e1/skills/subagent-driven-development/SKILL.md#L302-L375) | `/implement` 本身很薄：按约定 seam 做 TDD、持续 typecheck/定向测试、末尾完整测试、再 `/code-review`、最后 commit。[源码](https://github.com/mattpocock/skills/blob/2ab958093e83e0ec752e6c1c5932da465bf23e0c/skills/engineering/implement/SKILL.md#L1-L15) |
| Review | 每个 task 都要同时得到 spec compliance 与 task quality 两个 verdict，整枝结束再用最高能力模型做一次 broad review。[源码](https://github.com/obra/superpowers/blob/44c9b2d6e889982ac18c27d05a19fefe335194e1/skills/subagent-driven-development/SKILL.md#L256-L300) [最终 review](https://github.com/obra/superpowers/blob/44c9b2d6e889982ac18c27d05a19fefe335194e1/skills/subagent-driven-development/SKILL.md#L391-L414) | `/code-review` 把 Standards 与 Spec 分给两个并行 sub-agent，最后并列汇总且不把两轴混成一个评分。[源码](https://github.com/mattpocock/skills/blob/2ab958093e83e0ec752e6c1c5932da465bf23e0c/skills/engineering/code-review/SKILL.md#L6-L23) [汇总规则](https://github.com/mattpocock/skills/blob/2ab958093e83e0ec752e6c1c5932da465bf23e0c/skills/engineering/code-review/SKILL.md#L58-L89) |
| TDD | 极严格的 RED–GREEN–REFACTOR；生产代码先于失败测试就要求删除重来，只有 prototype、生成代码、配置等可经用户同意例外。[源码](https://github.com/obra/superpowers/blob/44c9b2d6e889982ac18c27d05a19fefe335194e1/skills/test-driven-development/SKILL.md#L16-L47) | 强调只在预先约定的 public seam 测外部行为，一次一个垂直 slice；当前版本把 refactor 放到 review 阶段而非红绿循环中。[源码](https://github.com/mattpocock/skills/blob/2ab958093e83e0ec752e6c1c5932da465bf23e0c/skills/engineering/tdd/SKILL.md#L6-L36) |
| Issue tracker / 领域知识 | 核心流程以 repo 中的 design/plan 和 git 分支为主，不原生建立统一 issue-tracker/domain glossary 配置。 | `/setup-matt-pocock-skills` 一次性配置 tracker、triage labels、`CONTEXT.md` 与 ADR 消费规则，正好对应本仓库现状。[源码](https://github.com/mattpocock/skills/blob/2ab958093e83e0ec752e6c1c5932da465bf23e0c/skills/engineering/setup-matt-pocock-skills/SKILL.md#L7-L15) |

## 优缺点

### Superpowers

优势：

1. **最强的执行可预测性。** 详细计划把“做什么、改哪里、怎么验”固化下来，fresh worker 只拿自己的 brief；适合让便宜模型处理机械任务。
2. **高风险变更有多层防线。** worktree 基线、每 task review、修复循环、最终 review、完成前新鲜验证证据，不依赖实现者自报成功。[验证门禁](https://github.com/obra/superpowers/blob/44c9b2d6e889982ac18c27d05a19fefe335194e1/skills/verification-before-completion/SKILL.md#L14-L48)
3. **长会话恢复能力强。** plan-scoped workspace 与 ledger 针对压缩、断点续作和多计划污染做了明确防护；v6.2.0 的发布说明也专门记录了这项改进。[v6.2.0](https://github.com/obra/superpowers/releases/tag/v6.2.0)
4. **模型路由直接内置。** 官方明确要求：1–2 文件且 spec 完整的机械实现用便宜模型，多文件集成用标准模型，架构/设计和最终整枝 review 用最高能力模型；不要省略 model 参数导致继承昂贵主模型。[源码](https://github.com/obra/superpowers/blob/44c9b2d6e889982ac18c27d05a19fefe335194e1/skills/subagent-driven-development/SKILL.md#L157-L192)
5. **跨 harness 支持更完整。** 官方 README 同时列出 Claude Code、Codex App/CLI、Cursor、Gemini CLI、OpenCode、Pi 等安装路径，并有 harness 与 plugin 测试。[安装与平台](https://github.com/obra/superpowers/blob/44c9b2d6e889982ac18c27d05a19fefe335194e1/README.md#L26-L194)

代价：

1. **固定开销高。** brainstorm、spec、plan、每 task worker、每 task reviewer、fix re-review、final reviewer 都会产生调用。一个本可一次完成的小改动，可能因流程本身变贵。
2. **详细计划有双刃剑。** 精确路径与完整代码能喂给便宜模型，但也会复制源码事实、增加计划陈旧风险；需求仍在变化时，过早写这种 plan 会浪费最多。
3. **强制触发降低灵活性。** “任何任务先检查技能”和“每个创作任务都先设计获批”很安全，但对文档、更名、简单配置可能过重，也更容易与另一套自动工作流争夺控制权。
4. **严格 TDD 不是所有前端探索的最佳工具。** UI 视觉方向、动效和交互手感在尚未决定时，先写 production test 往往是在测试猜测；应先明确走 prototype 例外，再把定稿行为纳入测试。
5. **高频 review 不等于所有 reviewer 都该用最高模型。** 官方最新版已按 diff 复杂度与风险分级；如果无脑给每个 reviewer 配顶级模型，等于绕过它自己的成本设计。

### mattpocock/skills / ask-matt

优势：

1. **更贴合 GitHub Issue 驱动与领域建模。** 它把 tracker、triage、领域词汇和 ADR 作为一等输入；本项目已经按此配置，不必再造一套事实源。
2. **产物更耐久。** spec 记录用户问题、implementation/testing decisions、out-of-scope，ticket 记录端到端行为与阻塞，不塞易过期路径；实施者基于当时源码再探索。
3. **垂直切片与依赖图更适合全栈。** `/to-tickets` 要求每个 slice 穿过 schema/API/UI/tests 且独立可演示，GitHub 上可用阻塞关系形成 frontier；对 React + Flask 的功能开发比按“先后端、再前端”横切更稳。
4. **对模糊和超大工作有更细的前置工具。** `/prototype` 回答需要跑起来或看见才知道的问题；`/wayfinder` 用 decision tickets 处理超过一个会话、路线仍有雾的项目，只产出决策、不提前造交付物。[wayfinder](https://github.com/mattpocock/skills/blob/2ab958093e83e0ec752e6c1c5932da465bf23e0c/skills/engineering/wayfinder/SKILL.md#L7-L25) [ticket 类型](https://github.com/mattpocock/skills/blob/2ab958093e83e0ec752e6c1c5932da465bf23e0c/skills/engineering/wayfinder/SKILL.md#L73-L80)
5. **用户控制强、容易裁剪。** Claude plugin 是托管只读 bundle；skills.sh 则把普通文件复制进 repo，可自行修改，官方明确要求二选一以免重复安装。[安装说明](https://github.com/mattpocock/skills/blob/2ab958093e83e0ec752e6c1c5932da465bf23e0c/README.md#L25-L80)

代价：

1. **`/implement` 的执行治理很薄。** 没有 Superpowers 那样的 worktree 所有权、plan ledger、per-task review breaker、final top-model review 和完成声明铁律；这些要靠 harness、仓库规则或人工补足。
2. **耐久 ticket 会把更多推理留给实现者。** 因为 ticket 不给路径和完整代码，最便宜的小模型可能反复探索、走更多轮；对“prose ticket → 实现/审查”，标准模型往往比 Flash 级模型更省总成本。
3. **`ask-matt` 不会自动替用户选路。** 这是控制力也是记忆负担；忘记显式调用时，不会像 Superpowers 那样强制把开发拉回流程。
4. **issue tracker 是能力也是外部副作用。** `/to-spec`、`/to-tickets`、`/triage` 会发布或修改 tracker 内容；必须先跑 setup，并在执行前确认当前 ticket 拆分，而不是把探索草稿直接发布。
5. **版本仍在快速演进。** 截至核对日，最新稳定 release 是 [v1.1.0（2026-07-08 发布）](https://github.com/mattpocock/skills/releases/tag/v1.1.0)，但 `main` 的 [Claude plugin manifest](https://github.com/mattpocock/skills/blob/2ab958093e83e0ec752e6c1c5932da465bf23e0c/.claude-plugin/plugin.json) 已写 `1.2.0`，说明 `main` 含稳定 tag 之后的内容。追求复现应 pin commit/tag，不要把滚动 `main` 当版本号。

## 对本项目的适配

### 项目现状决定了“上游 Matt、风险执行 Superpowers”最划算

本仓库已经有两套资产：

- Matt 侧：领域词汇 [`CONTEXT.md`](../../CONTEXT.md)、[`docs/agents/issue-tracker.md`](../agents/issue-tracker.md)、[`triage-labels.md`](../agents/triage-labels.md)、[`domain.md`](../agents/domain.md) 和四个 ADR；
- Superpowers 侧：已有 [`docs/superpowers/specs/`](../superpowers/specs/) 与 [`docs/superpowers/plans/`](../superpowers/plans/) 的真实设计/执行文档。

因此不应重新“统一成一个大框架”，而应规定清晰边界：**GitHub Issue + CONTEXT/ADR 是长期事实源；Superpowers plan 是某个高风险 ticket 的短期执行配方。** 若 plan 中出现新的不可逆架构决定，应回写 ADR，而不是让决定只留在 plan。

### 前端：优先 Matt 的 prototype 与垂直 tickets

React/TypeScript/Vite 前端已有 Vitest 与 Testing Library，但 UI 形态和交互手感经常需要先看再决定：

- 页面布局、未归款图片卡片、批量关联交互、响应式方案：先 `/prototype` 生成几个明显不同的 UI 变体，保留结论、丢弃 prototype code；
- 方案定稿后，用 `/to-tickets` 切一个能从 UI 到 API 再到测试独立演示的 vertical slice；
- 普通组件或 API client 改动用 Matt `/implement` 足够；涉及权限、上传一致性、复杂并发状态或跨多页面的高风险改动，再升级 Superpowers；
- 不要让顶级模型做 CSS 搬运、类型补全、测试命令运行；这些在设计已定时适合便宜 worker。视觉方向和交互架构仍应由强模型或人做判断。

### 后端：按失败半径分流

Flask + PostgreSQL/pgvector + 私有 OSS 的普通 CRUD 可以走 Matt 主流程；以下情况建议进入 Superpowers 高风险通道：

- `image_assets` schema、HNSW/vector(1024)、embedding 模型或事务语义变更；
- 私有 OSS 签名预览、无覆盖上传、Kodo 只读边界；
- 任何迁移、批量写入、恢复点、幂等与冲突处理；
- 跨 `asset_ingest`、对象存储、embedding、数据库和 API 的多文件一致性改动。

特别是迁移：[`ADR-0003`](../adr/0003-oss-as-authoritative-image-store.md) 与仓库规则规定 Kodo 只读、OSS 为正式来源、删除或清理需另行授权。应把这些逐字放进 Superpowers plan 的 `Global Constraints` 和 reviewer brief，并且：

1. `/to-tickets` 先建立 `preflight/audit → selection verification → pilot → full approval` 的阻塞边；
2. 每张高风险 ticket 单独进入 Superpowers，做一次简短但真实的 design 确认，再写执行 plan；
3. 只读检查、fixture 构造、测试运行可交给便宜 worker；真实写模式、授权门、事务/幂等设计和最终 review 由顶级模型 controller 把关；
4. 无论模型多强，都不能代替人为授权、数据库恢复点、私有凭证隔离和“不得 Put/Delete Kodo”的操作权限。

### 验证命令必须有 allowlist，不能把“跑全量测试”机械下放

Superpowers 的通用完成流程倾向于运行 full suite，但项目指令优先于 skill；其自身也明确写明用户/仓库指令优先。[优先级规则](https://github.com/obra/superpowers/blob/44c9b2d6e889982ac18c27d05a19fefe335194e1/skills/using-superpowers/SKILL.md#L60-L62) 本仓库存在两个不能交给弱模型无差别执行的脚本：

- [`backend/test/test.py`](../../backend/test/test.py) 会读取真实 `OSS_*` 环境变量，列举 bucket 对象，并对固定键 `test_oss_connection.txt` 执行上传、下载、删除（源码第 36–64 行）。文件名与函数名都符合 pytest 默认收集规则，因此无差别 `pytest test/` 会执行这些外部副作用；它还可能覆盖并删除同名真实对象，绝不能当成普通测试。
- [`backend/test/test_pgvector.py`](../../backend/test/test_pgvector.py) 是手工 benchmark；创建数据前执行 `DROP TABLE IF EXISTS vector_test` / `CREATE TABLE`（源码第 12–24 行），直接运行 main 还默认连接 `image_search`（第 266–281 行）。它不是安全的日常验证命令。

因此 controller 必须先审计并批准验证命令，再把“只执行这条命令、不得扩大全套”交给便宜 worker。后端日常 pytest 至少要显式排除上述外部/手工脚本，并继续确保 integration tests 指向独立 `image_search_test`；高风险 ticket 最稳妥的是维护经过审计的 test-file allowlist。任何 worker 若认为需要真实 OSS、真实迁移或 benchmark DDL，应返回 `NEEDS_CONTEXT/BLOCKED`，而不是自行运行。

## 能力边界与模型选择

下表是角色分配，不是某一厂商型号的能力保证。可以把 5.6-sol / Opus 5 归入“最高层”，强通用模型归入“标准层”，Luna Worker / Flash 类模型归入“便宜层”；Qoder CLI、Claude Code、Codex 是 harness，不应被当成模型能力等级。

| 阶段 | 推荐模型层级 | 原因与边界 |
| --- | --- | --- |
| 需求澄清、领域命名、架构、不可逆数据/存储决策 | **最高层** | 错一次会让整个执行链便宜而错误；需要跨 CONTEXT、ADR、代码和操作约束做取舍。 |
| `/wayfinder`、高风险 `/to-spec`、迁移 ticket 的 blocking edges 与测试 seam | **最高层** | 这些产物决定后续并行性与安全边界；便宜模型不适合独立拍板。 |
| Superpowers 的完整 implementation plan | **最高层或强标准层** | 计划越准确，后续越能降级；迁移/安全必须最高层，普通清晰功能可强标准层。 |
| 1–2 文件、完整 brief、预期输出明确的机械实现 | **便宜层** | 正是 Superpowers 官方建议的 cheapest-tier 场景；Luna Worker/Flash 可做，但只给窄文件所有权并要求运行测试。 |
| 从 Matt 的 prose ticket 探索并实现、多文件常规集成 | **标准层** | 需要代码库导航与局部判断。Superpowers 官方也提醒，便宜模型在多步任务可能多花 2–3 倍轮次，不能只看单 token 价格。[源码](https://github.com/obra/superpowers/blob/44c9b2d6e889982ac18c27d05a19fefe335194e1/skills/subagent-driven-development/SKILL.md#L181-L192) |
| lint/typecheck/已审计的定向 test、只读检索、报告整理、批量机械迁移代码改写 | **便宜层** | 判定标准是预批准命令和 diff，而不是模型审美；不能让弱模型自行把定向测试扩成全套，更不能触发真实 OSS、benchmark DDL 或云端迁移。 |
| 小而机械的 task review / 修复后 scoped re-review | **标准层下限，可降至便宜-中档** | review 需要独立判断，不能默认用最弱模型；仅当 diff 极窄且 spec 完整时下调。 |
| 安全、并发、事务、迁移、最终整枝 review | **最高层** | Superpowers 也明确把 final whole-branch review 列为最高能力任务；本项目错误成本高。 |
| hard bug 的反馈回路构造、假设排序、根因确认 | **标准层起步，复杂时最高层** | 复现与命令运行可便宜；从证据构造可证伪假设需要判断。不要让小模型无限重试，失败应升级或缩小任务。 |

一个实惠的默认预算形态是：**1 次顶级规划 + N 个便宜机械 worker + 风险分级 reviewer + 1 次顶级最终 review**。但只有 Superpowers 的 plan 足够完整时，`N` 个 worker 才真的是机械任务；若输入只是 Matt 风格的 durable ticket，应把 worker 提升到标准层，避免低价模型靠多轮探索把总成本反超。

## 当前价格与可执行的模型路由

### 先区分 API 美元价与 Agent 套餐 credits

以下是 2026-08-02 核对的官方标价，单位为每 100 万 token；它们会变，真实采购时应以当日账户页面为准。

| 模型 / 计费面 | 输入 | 缓存命中 | 输出 | 与本工作流的关系 |
| --- | ---: | ---: | ---: | --- |
| GPT-5.6 Sol API | $5 | $0.50 | $30 | 顶级规划、风险裁决、R4 最终 review。[OpenAI 模型页](https://developers.openai.com/api/docs/models/gpt-5.6-sol) |
| GPT-5.6 Terra API | $2 | $0.20 | $12 | 多文件常规实现、普通 review。[OpenAI 模型页](https://developers.openai.com/api/docs/models/gpt-5.6-terra) |
| GPT-5.6 Luna API | $0.20 | $0.02 | $1.20 | 完整 brief 下的封闭机械任务。[OpenAI 模型页](https://developers.openai.com/api/docs/models/gpt-5.6-luna) |
| Claude Opus 5 API | $5 | $0.50 | $25 | 复杂 agentic coding、架构和难调试；并非 Anthropic 绝对最高档，但对本项目通常已足够。[Anthropic 选型](https://platform.claude.com/docs/en/about-claude/models/choosing-a-model) |
| Claude Sonnet 5 API | $2 | $0.20 | $10 | 当前促销价至 2026-08-31；之后为 $3 / $0.30 / $15。适合日常实现。[Anthropic 价格](https://platform.claude.com/docs/en/about-claude/pricing) |
| Claude Haiku 4.5 API | $1 | $0.10 | $5 | 快速查询、简单编辑、高吞吐 sub-agent。[Anthropic 价格](https://platform.claude.com/docs/en/about-claude/pricing) |
| DeepSeek V4 Flash API | $0.14 缓存未命中 | $0.0028 | $0.28 | 现金单价最低，但只应承担可回滚、可机器验收的工作。[DeepSeek 价格](https://api-docs.deepseek.com/quick_start/pricing/) |

Codex 订阅/credits 不直接按上表的美元 API 价扣除。当前 token-based rate card 中，Sol / Terra / Luna 的“输入–缓存–输出”分别为 `125–12.5–750`、`50–5–300`、`5–0.5–30` credits；绝大多数账户已转到这张表。因此，使用 Codex 时要用 [Codex 官方 rate card](https://help.openai.com/en/articles/20001106-codex-rate-card) 算，不要把 API 美元价直接换算成订阅额度。`luna_worker` 是 Agent 角色配置，不是独立计费 SKU；若它固定为 `max` reasoning，会比 Luna low/medium 产生更多推理 token，不能只看模型名。

若 token 数与输入/输出结构不变，`10% Sol + 30% Terra + 60% Luna` 的理论成本是全部使用 Sol 的 **24.4%**，即节省 75.6%；对当前 Codex credits 比例也恰好相同。这只是预算上界估算：便宜模型反复失败、sub-agent 重复读取上下文、并行工作量增加都会吃掉部分节省。

Anthropic 官方也给出几乎同样的路由：Sonnet 处理大多数编码，Opus 处理跨切重构、难调试与架构，Haiku 处理机械高吞吐任务；Claude Code 的 `/model opusplan` 会用 Opus 规划、Sonnet 执行。[官方 Claude Code 指引](https://support.claude.com/en/articles/14552983-models-usage-and-limits-in-claude-code) 由于 Claude 4.7 及以后的新 tokenizer 对同样文本可能生成约 30% 更多 token（依内容而定），不应仅因 Opus 5 输出标价低于 Sol 就断言它的真实任务成本一定更低。

### 三种实际配置

1. **最佳综合性价比（推荐）：单一 Codex harness。** Sol 只做计划/R4 裁决/最终审查，Terra 做常规多文件实现，Luna low/medium 或 Luna Worker 做窄任务。好处是 skills、权限、上下文和账单只有一套，少了 harness 切换和重复探索。
2. **已经主用 Claude Code：Opus plan + Sonnet execute + Haiku mechanical。** 直接使用 `/model opusplan`，而不是全程开 Opus。若你已为某一家的顶级套餐付费，先用掉已包含额度；除非 R4 变更需要异构复核，不必同时为 Sol 和 Opus 的日常执行付费。
3. **最低现金支出：Qoder Free/BYOK + DeepSeek V4 Flash，顶级模型只收头尾。** Qoder Free 包含 BYOK，自定义模型费用由 provider 直接收取、不消耗 Qoder credits，但 Code Review Agent/Repo Wiki 等固定功能例外。[Qoder 价格](https://docs.qoder.com/account/pricing) [Qoder 自定义模型](https://docs.qoder.com/user-guide/chat/custom-models) Flash 做 R0/R1，Terra/Sonnet 处理 R2/R3，Sol/Opus 做 R4 计划与审查；不要为了“只用 Flash”让它在多文件集成上反复试错。

DeepSeek 官方确实给出了通过 Anthropic-compatible endpoint 把 Claude Code 的主模型映射到 V4 Pro、sub-agent 映射到 V4 Flash 的[Claude Code 集成方法](https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code/)。但 Anthropic 同时明确说，它不支持通过 gateway 把 Claude Code 路由到非 Claude 模型；并且 gateway credential 激活时不会使用 claude.ai 订阅额度。[Anthropic gateway 说明](https://code.claude.com/docs/en/llm-gateway) 所以这是“借 Claude Code shell 跑 DeepSeek”的实验配置，不是受 Anthropic 保证的混合模式。若真要用，应放在独立 shell profile，每次 Claude Code 升级后重跑工具调用与 skill 兼容性 eval；若只是想要便宜 worker，Qoder BYOK 的责任边界更清楚。

### 自动升级规则

不要让 controller 凭感觉选模型。下列任一条发生就升一档，数据损失/安全/不可逆操作直接到顶级模型 + 人工授权：

- 便宜 worker 对同一验收目标连续失败两次；
- 验收条件无法改写为可执行测试，或反馈回路慢/不稳定；
- 跨越三个以上边界（例如 schema + API + UI + 存储）且契约不清；
- 两个独立 Agent 结论冲突，或 review 新发现未覆盖的不变量；
- 涉及凭证、签名 URL、事务、并发、schema、embedding 模型/维度、OSS/Kodo 或数据迁移。

## 什么时候用哪一套

| 场景 | 首选 | 具体路径 |
| --- | --- | --- |
| 不知道该用哪个技能 | Matt | `/ask-matt`，它只路由，不做工作。 |
| 有一个模糊但一会话可讲清的产品想法 | Matt | `/grill-with-docs → /to-spec → /to-tickets`；小改可直接 `/implement`。 |
| 绿地项目或巨大功能，连路线都看不清 | Matt | `/wayfinder`；每 session 只解决一个 decision ticket，清雾后回 `/to-spec`，不要直接开工。[源码](https://github.com/mattpocock/skills/blob/2ab958093e83e0ec752e6c1c5932da465bf23e0c/skills/engineering/ask-matt/SKILL.md#L42-L46) |
| UI 必须“看见才知道”、状态机必须跑起来才知道 | Matt | `/prototype`，然后 `/handoff` 结论回原线程。 |
| 外部用户报来的 issue/PR 堆积 | Matt | `/triage`；自己由 `/to-tickets` 生成的 ticket 已是 agent-ready，不要再 triage。[路由规则](https://github.com/mattpocock/skills/blob/2ab958093e83e0ec752e6c1c5932da465bf23e0c/skills/engineering/ask-matt/SKILL.md#L34-L42) |
| 普通前端组件、常规 CRUD、文档或小修 | Matt 或直接窄实现 | 不值得启动整套 Superpowers；用 pre-agreed seam 的 TDD 与一次 code-review 即可。 |
| 明确、重复、可测试且有很多机械 slices | Superpowers | 顶级/强模型写一次详细 plan，便宜模型逐 task 实现，按风险配置 reviewer。 |
| DB/OSS/Kodo/embedding/schema/权限等高风险变更 | Superpowers | worktree + baseline + plan Global Constraints + SDD + final top-model review + fresh verification。上游仍可由 Matt 产出 issue/spec。 |
| hard bug / 性能回归 | 保持当前 lane | 若尚未进入任何流程，本项目更适合 Matt `/diagnosing-bugs` 的“先建立一个已跑红的紧反馈命令”；若已经在 Superpowers 执行链中，留在其 `/systematic-debugging`，避免切换造成重复调查。 |
| 已完成、准备合并 | 跟随该 ticket 的执行框架 | Matt lane 用 `/code-review` 两轴并行；Superpowers lane 用 per-task + final review。除非变更极高风险，不重复跑两套全量 review。 |

## 混合使用的操作规则

推荐的最小混合架构如下：

```text
长期事实源：CONTEXT.md + ADR + GitHub Issue
                       │
                       ▼
Matt：grill / spec / tickets / blockers
                       │
              按 ticket 风险分流
                 ┌─────┴─────┐
                 ▼           ▼
       普通：Matt implement   高风险：Superpowers design → plan → SDD
                 │           │
                 └─────┬─────┘
                       ▼
                单一 review/merge 门
```

1. **一个阶段只选一个 owner。** 不同时跑 Superpowers brainstorming 与 Matt grilling；不同时把同一工作拆成 Superpowers micro-plan 和 Matt 全量 tickets。
2. **在 ticket 边界换框架。** Matt 可以只做到 `/to-tickets`；某张高风险票另开干净会话交给 Superpowers。不要执行到一半切换。
3. **Superpowers 不得悄悄跳过自己的设计门。** 已有 Matt spec 可缩短 brainstorming，但仍需确认它是否足以作为该 ticket 的 approved design；否则后续详细 plan 只是把歧义放大。
4. **避免双份 TDD 与双份 review。** 两套都能提供测试与审查，重复不会线性增加质量，却会近似线性增加调用成本。
5. **给小模型的不是“项目”，而是封闭任务。** 明确文件所有权、不可触碰区域、输入输出、测试命令和停止条件；真实迁移/发布/外部写入仍由 controller 或人执行。
6. **按风险升级，不按工具品牌升级。** 同一个 CLI 可以挂不同模型；真正需要顶级模型的是决策与审查角色，而不是“所有 Claude Code 步骤”或“所有 Codex 步骤”。
7. **验证命令也属于权限边界。** worker 只运行 controller 明示的 audited command；禁止自行使用“full suite”替换定向命令，尤其不得运行 `backend/test/test.py` 或手工 pgvector benchmark。

## 维护、安装与许可证

| 项目 | 当前稳定发布（核对日） | 安装/更新 | 许可证与维护判断 |
| --- | --- | --- | --- |
| Superpowers | [v6.2.0，2026-07-24 发布](https://github.com/obra/superpowers/releases/tag/v6.2.0)；本报告 `main` 快照在 2026-07-28 | Claude Code 与 Codex App/CLI 都有官方 marketplace 路径；不同 harness 需分别安装。[README](https://github.com/obra/superpowers/blob/44c9b2d6e889982ac18c27d05a19fefe335194e1/README.md#L26-L194) | [MIT](https://github.com/obra/superpowers/blob/44c9b2d6e889982ac18c27d05a19fefe335194e1/LICENSE)。v6.2.0 与之后 main 均有活动，且仓库包含 plugin/harness tests 与独立 eval 说明，维护成熟度较高。 |
| mattpocock/skills | [v1.1.0，2026-07-08 发布](https://github.com/mattpocock/skills/releases/tag/v1.1.0)；`main` manifest 已到 1.2.0，属 release 之后快照 | Claude Code 可装托管 plugin；Codex/其他 Agent 用 `npx skills@latest add mattpocock/skills` 复制可编辑文件，随后 `npx skills update`。不要 plugin 与 skills.sh 双装。[README](https://github.com/mattpocock/skills/blob/2ab958093e83e0ec752e6c1c5932da465bf23e0c/README.md#L25-L80) | [MIT](https://github.com/mattpocock/skills/blob/2ab958093e83e0ec752e6c1c5932da465bf23e0c/LICENSE)。发布较新、main 演进快；适合 pin commit 并在 repo 内定期审阅更新。 |

本机环境核对还发现：仓库内 Matt 工程技能与 `2ab9580` 快照逐文件一致；全局可见的 Superpowers 技能文件与 `44c9b2d` 快照不完全一致。因此若要做真实的成本/质量 A/B 测试，应先记录各 harness 实际加载的 skill 版本，不能只用“我装了 Superpowers”作为实验条件。

## 最终建议

对当前项目，最佳实践不是让一个库吞掉另一个，而是：

1. **默认主线用 Matt**：利用已经存在的领域词汇、ADR、GitHub Issues 和 vertical tickets，保持人对需求与优先级的控制；
2. **高风险 ticket 用 Superpowers**：用顶级模型锁定设计与详细 plan，便宜 worker 做机械实现，风险匹配 reviewer，顶级模型做最终整枝审查；
3. **前端先 prototype，后端按失败半径升级**；
4. **在 ticket 边界换框架，不在同一 diff 上叠流程**；
5. **模型成本按角色而非品牌分配**：最贵算力只放在决定错误方向会连锁放大的节点，便宜算力放在有可执行规格和机器验证的节点。

这能同时保留 mattpocock/skills 的领域/issue 优势与 Superpowers 的执行安全网，并把最高等级模型的使用集中到真正不可替代的判断工作上。
