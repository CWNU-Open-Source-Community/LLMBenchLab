# 项目状态

> 更新时间：2026-08-27（Asia/Shanghai）

## 当前阶段

- Phase 0 — 项目治理和架构：`completed`（2026-08-24）
- Phase 1 — MVP 垂直链路：`completed`（2026-08-25）
- Phase 2 — 可靠性与任务执行：`in_progress`（2026-08-25 开始）
- Phase 3 — 标准 Benchmark 与代码评测：`in_progress`（仅可信本地客观题提前切片）
- 后续阶段：Phase 4–6：`planned`

## 当前版本

`0.1.0` development baseline（尚未发布正式 Release），REST API 为 `/api/v1`，评测协议为 `llmbenchlab-protocol-v1`。

公开仓库：[`CWNU-Open-Source-Community/LLMBenchLab`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab)。`main` 跟踪 `origin/main`；阶段性任务必须独立 commit、push，并由该精确 SHA 的四个 GitHub Actions 必需 job 全部通过后才可声明完成。

## 已完成功能

- 完整的 Charter、Requirements、Architecture、Benchmark Protocol、Dataset Format、Roadmap、Phase 0–6、ADR、治理规则和开源协作文件。
- FastAPI、SQLAlchemy 2.x 与 Alembic 后端；PostgreSQL 是 Compose/共享部署目标和任务事实来源，SQLite 保留单 Worker 本地兼容；六个核心实体、UTC 时间、约束、索引，以及 `0000 -> 0001 -> 0002 -> 0003` 线性迁移链。
- Alembic 是唯一 schema owner；Compose 只允许一次性 `migrate` 服务执行迁移，API/Worker 只检查 head。setup/migrate 仍可安全收养已知未版本化 SQLite，未知漂移在 stamp 前被拒绝。
- Model CRUD 与 Mock/OpenAI-compatible Adapter；Web/API 可通过只写 `api_key` 直接接收 Provider Key，并在一模型一行的 `model_credentials` 中以 AES-256-GCM 保存认证密文。API 不把凭据流中的 Key 复制到公开 `ModelRead`/Run-model snapshot，也不返回 nonce、ciphertext 或 key ID；凭据状态由 `credential_source` 与仅表示 stored ciphertext 的 `has_api_key` 表达，既有 `api_key_env` 名称保持 API 兼容但 Web 不展示。可信本地 CLI 环境/隐藏输入路径继续兼容，API 与 Worker 必须共享独立于数据库的部署 keyring。远程 Provider 只允许 HTTPS，HTTP 只允许 loopback；模型发现与 Chat 都只接受 identity 编码，发现体上限 2 MiB，Chat 成功体上限 4 MiB、错误体上限 64 KiB，并在持久化前递归检查 Provider 返回证据的对象键/JSON 标量，将当前 Key 的精确回显替换为 `[REDACTED]`。该保证不扫描无关 Benchmark/Question 的独立字面巧合。
- 受限 ZIP/目录 Dataset Loader、严格 Schema/JSONL 校验、路径与压缩炸弹防护、稳定 SHA-256，以及 15 道原创 `demo-general`；资源上限现为 20,000 题、128 MiB `questions.jsonl` 和 130 MiB ZIP。
- MMLU-Pro test 与 GPQA-Diamond 的固定 revision/SHA 下载、验证缓存、确定性转换和可复现 ZIP；不在仓库提交第三方题目。MMLU-Pro 支持 `direct` 与 category 5-shot `official_cot`，GPQA 使用固定 seed 逐题重排和 `zero-shot-cot-answer-line-v1`。
- 可信本地 `llmbenchlab-evaluate` CLI：`prepare/run/resume/report`，支持 `/models` 发现、最小 canary、隐藏输入/环境变量 Key、题数与 HTTP attempts 上界确认、缺题恢复和终态报告。发现结果若把当前 Key 反射为模型 ID 会失败；canary 若返回不同于请求目标的模型也会失败；首次 Run 的 discovery/canary 证据会固化进 Run 快照。
- 终态报告以非覆盖原子发布生成 `summary.json`、`groups.csv`、全量分页 `responses.jsonl`；`metrics` 统一从计划题目和持久化 Responses 派生，`metrics_provenance` 标记其来源及与持久化 Run 聚合字段的漂移，并对运行时 Key 值做精确脱敏。
- Exact Match、Multiple Choice、Numeric Evaluator；原始输出、解析结果、评分和错误证据分离持久化。
- Phase 2 可靠执行基础：API 只提交数据库事实并 best-effort 发送 Redis Streams 通知；独立 Worker 以数据库扫描/领取、租约、心跳、单调 fencing token、逐题幂等和有限 attempt 执行 Run。大 Run 的快照加载已移出事件循环，加载期间租约心跳继续运行。
- 数据库裁决取消、重试/退避、租约过期接管、终态聚合和 dead-letter；fail-attempt 与过期租约两条 dead-letter 路径都先从持久化 Response 聚合证据。Redis 是 at-least-once 通知层，不是状态数据库，通知丢失时可由数据库对账恢复。
- 22 个版本化 `/api/v1` 操作：liveness、health、readiness、任务 gauges、服务信息、模型、Benchmark、Run、逐题 Response、Leaderboard 与 Dashboard Metrics；OpenAPI 可用。
- React 中文界面现有 Dashboard、Models、Benchmarks、Evaluation Runs、New Run、Run Detail、Leaderboard 七页。评测记录位于主导航，覆盖全部 Run 状态、筛选、20 条分页、手动刷新和活动页轮询；Run Detail 按 100 条分页读取逐题证据、显示 API 总数并可返回列表。第五个导航项及 Benchmark 详情、配置快照和表单字段已适配桌面/平板/移动裁剪与对齐。Models 对 OpenAI-compatible 提供 password 类型 Key 输入：创建时必填，编辑留空保留同 origin 的 stored/legacy environment 凭据，页面从不回填或展示原文；提交开始、关闭、切换 Mock 与 unmount 都会清空浏览器状态。
- Web/API Run 配置已支持 `max_tokens=1..131072` 或 `null`；`null` 会省略 Provider 请求字段而不是承诺无限输出，未显式给值的通用 API/protocol-v1 默认仍为 256。Web 按 Demo、MMLU-Pro Direct/official CoT、GPQA-Diamond 给出不同输出预算与读取超时起点，`read_timeout_seconds=1..1800` 固化进 Run execution snapshot；Provider 长度截断导致空内容或最终答案解析失败时记录 `output_truncated`。
- LLMBenchLab 应用 JSON 日志、请求/Run/Question correlation ID、`/live`、`/health`、`/ready`、数据库派生任务 gauges，以及数据库/队列依赖能力 Worker probe。
- PostgreSQL/SQLite migration 已扩展到 `0003`；显式 SQLite→PostgreSQL 单向导入器以只读源、空目标、单目标事务和六表 count/PK/content digest 对账（包括 `model_credentials`），并区分提交前回滚、COMMIT 结果未知与提交后验证失败。数据库迁移不会复制部署 keyring，含 stored credential 的目标必须另行获得匹配 keyring 才能解密。
- 统一 Make 命令、setup/dev/smoke/故障验收脚本、锁文件和 GitHub Actions；六服务本地 Compose 由 `postgres`、`redis`、一次性 `migrate`、`api`、`worker`、`frontend` 组成，API/frontend 只绑定 loopback，PostgreSQL/Redis 不发布宿主端口。
- 项目已发布到 CWNU Open Source Community 组织；初始 `main` commit `d2b9bc8` 的远程 CI 四个 job 全部成功，包括真实 PostgreSQL/Redis integration 与 Compose 8/8 故障验收。

## 进行中功能

- Phase 1 已固定为基线 commit `3db1e29`。
- Phase 2 可靠任务执行基础已按 [ADR-0005](decisions/ADR-0005-durable-task-execution.md) 交付并经过真实 PostgreSQL/Redis 与进程故障验证；实现、验证和阶段边界记录在 [当前工作日志](worklogs/2026-08-25-phase-2-reliable-execution-foundation.md)。
- Phase 2 总状态仍为 `in_progress`：P2-05 尚未实施；P2-06 和 P2-07 只有部分交付，不能称为完整可观测、生产 HA 或容量已验证。
- [ADR-0006](decisions/ADR-0006-local-real-provider-evaluation.md) 按用户优先级批准可信本地正式数据/真实 Provider 提前切片；本地代码、固定数据源下载和 Mock-only 回归已通过，真实 Provider 调用留给持有 Key 的用户显式执行。该切片没有补齐 P2-05，也不代表 Phase 3 完成。
- [ADR-0007](decisions/ADR-0007-web-provider-credentials.md) 已按用户明确要求接受 Web 直接输入 Key：write-only API/UI、AES-GCM `model_credentials`、API/Worker 共享 keyring、legacy environment 兼容、origin 变更重输 Key 和 active-Run 变更禁令均已通过完整本地门禁。用户随后暴露的 PyPy keyring 首次初始化问题也已修复、通过本地回归并正常 push；没有调用真实 Provider。它不改变 Phase 2 的 `in_progress` 状态。
- Web 长推理预算/读取超时、`output_truncated` 诊断、全状态评测记录、逐题证据分页和响应式修复已在功能提交 `467d0243b4fb081c2d637b20ee0958c3bd6ee6d1` 中正常 push；完整本地门禁通过。该切片不改变 Phase 2/3 状态，自动化没有调用真实 Provider。
- 当前分支 `codex/complete-evaluation-workflow` 的 Web 凭据基础实现 `b19bdac9236f9b2f927166ebe30578ced3d9f53e`、前一文档证据 `d41517a0cc385da6931f83de672f24f841192a31`、bootstrap remediation `d26cdbe4f3f97057ce09d5d7a539ddbfe605d967` 与 Web Run UX 功能提交 `467d0243b4fb081c2d637b20ee0958c3bd6ee6d1` 均已推送。工作流只由 PR 或 `main` push 触发；当前分支没有 PR，最新功能 SHA 的 Actions 查询与 PR 查询均为空。创建 PR 需用户明确授权，远程绿色前保持任务 `in_progress`。

## 尚未完成的功能

- Phase 2 / P2-01：正式 SLO、容量模型和容量基线。
- Phase 2 / P2-05：Provider 速率限制、预算硬上限、完整背压、公平调度和全局并发治理。
- Phase 2 / P2-06：历史 counters、延迟/恢复时长、完整任务审计、全日志源脱敏治理、Worker 主事件循环 liveness 和告警；当前 `/tasks/metrics` 只是数据库 gauges，应用 JSON logger 不覆盖全部 Uvicorn/第三方日志。首次 canary 证据虽会固化进 Run 快照，但 `resume` 的 canary 不会独立追加审计事件，且每题 transport request ID、Provider 返回 model 与 system fingerprint 尚未持久化。
- Phase 2 / P2-07：性能/容量测试、完整操作 Runbook、告警响应和更完整的备份/恢复演练。现有故障证据证明可靠基础行为，不证明生产高可用或无限横向扩展。
- Phase 3：IFEval 官方 strict/loose 评分、通用 Dataset Plugin SDK、代码题 Schema/隔离沙箱、完整标准 Benchmark 分组/子集 UI 和安全红队；当前只有 MMLU-Pro/GPQA-Diamond 可信本地客观题切片。
- Phase 4：LLM/Pairwise Judge、个人 Arena 与长上下文评测。
- Phase 5：Agent/Tool Use、私有 Benchmark 与 Live Benchmark。
- Phase 6：多用户、鉴权、公共部署安全和正式版本发布。

## 已知问题

- SQLite 只面向个人本地、单 Worker 路径；多 Worker 竞争证据仅针对 PostgreSQL。当前 Compose 是可靠性开发/验收拓扑，不是生产 HA。
- 每个 Run 内并发上限为 4、每个 Worker 同时执行一个 Run，但不同 Run/Provider 之间没有完整全局限流、预算、背压或公平调度。
- Web 的每题 `max_tokens` 与读取超时是请求配置和诊断能力，不是 P2-05 的全局 Token/费用预算。选择 Provider 默认也可能被上游自身限制或收费，不能解读为无限输出或成本可控。
- 任务投递为 at-least-once，本地 Response 和聚合幂等，但 Provider 调用不是 exactly-once；Worker 在上游响应后、本地提交前崩溃可能造成重复请求或费用。
- 取消和失租会阻止后续题目与陈旧 Worker 写入，但已经发出的 Provider 请求可能继续至响应或超时。
- Redis 故障会让 API readiness 降级并增加调度延迟；数据库可继续提交/对账，但这不是 Redis 高可用保证。当前本地 Redis 无 ACL/TLS，只能位于隔离网络。
- Worker probe 只检查数据库/head/队列能力，不证明 Worker 主循环仍在领取、心跳或推进任务。API readiness 的 `asyncio.to_thread` 超时也不会取消底层同步数据库调用，最终上界取决于驱动/连接池 timeout。
- SQLite→PostgreSQL importer 会复制完整敏感评测内容和 `model_credentials` 认证密文，且只支持空目标的单向导入；退出码 3/4 禁止盲目重试，工具不提供 PostgreSQL→SQLite 自动回迁。keyring 不随数据库导入，必须作为独立部署秘密安全转移。
- OpenAI-compatible `base_url` 已强制远程 HTTPS、仅允许 loopback 使用 HTTP，但仍没有目的地址 allowlist、DNS 重绑定防护或出站网络隔离，题目外发与 SSRF 风险未消除；MVP 不得直接暴露公网。
- 没有认证、授权、TLS、限流、预算上限或生产 KMS；Web 凭据仅限可信 loopback 使用，Compose 仍只用于本地验证。部署 keyring 是新的高价值秘密；数据库与 keyring 同时泄漏时 stored Provider Key 可被解密。
- 标准数据当前只有 MMLU-Pro 与 GPQA-Diamond。固定源下载/转换，以及带 discovery、canary、请求上界确认和完整终态报告的可信流程仅由本地 CLI 提供；已载入的标准集可从 Web 运行，但不包含 CLI 的预检、确认与报告护栏。当前没有 IFEval、代码沙箱、Judge、Arena 或 Agent 能力。
- 可信本地 CLI 没有全局 RPM/TPM/金额硬上限，也无法阻止连接同一数据库的空闲常规 Worker 抢走新 `pending` Run；操作者必须先停止 API/Worker 并独占数据库。Provider 调用/计费仍不保证 exactly-once。
- 当前本地 `uv` 环境选择 Python 3.14，测试出现 `pytest-asyncio` 与 FastAPI TestClient 的上游弃用警告，但无失败；CI 固定 Python 3.12。
- 前端 production build 成功，但 Recharts 主包触发大于 500 kB 的 Vite chunk 警告；不影响 MVP 功能，后续可按页面懒加载。

## 测试状态

| 验证 | 结果 | 证据 |
| --- | --- | --- |
| Web 凭据后端全量 | 通过 | `make test`：`427 passed, 6 skipped`；6 个 skip 仅为未注入 DSN 的 PostgreSQL/Redis/importer integration；keyring bootstrap 定向 `24 passed` |
| Web 凭据真实基础设施 | 通过 | 临时 PostgreSQL 16/Redis 7：`6 passed, 0 skipped`，含 Model 行锁、Redis 重投递和六表 credential binary 导入；精确容器已清理 |
| Web 凭据前端 | 通过 | ESLint/typecheck 通过；Vitest 5 files / `21 passed`；Vite production build 成功（保留既有约 649 kB chunk warning） |
| Web 凭据离线 Smoke | 通过 | `1 passed, 5 deselected`，全程 Mock 与隔离 SQLite |
| Web 凭据静态/迁移/Compose | 通过 | Ruff/format、PostgreSQL Alembic upgrade/check、`uv lock --check`、Compose config、更新后的 8/8 故障验收、diff check 与高置信 secret scan 均通过；evidence `llmbenchlab-p2-60f3ccdac113` 已确认无残留容器/卷/网络 |
| 标准数据真实源验证 | 通过 | 固定源下载并转换完整 MMLU-Pro 两个 profile（各 12,032 题）与 GPQA-Diamond（198 题）；另以 CLI `prepare --limit 2` 验证普通入口和可复现归档 |
| Web Run UX / 长推理配置切片 | 已 push，远程未触发 | 功能提交 `467d0243b4fb081c2d637b20ee0958c3bd6ee6d1` 已 push；后端 `442 passed, 6 skipped`、前端 9 files / `36 passed`、lint/typecheck/build、Smoke `1 passed, 6 deselected`、lock/Compose config/diff 与 390–1280px 关键断点通过；精确 SHA 无 Actions run |
| 真实 Provider | 未运行（有意） | 本任务没有 API Key；自动化只用 Mock/MockTransport，真实调用及费用必须由用户显式确认后发生 |
| 远程精确 SHA CI | 未触发 | 最新功能 SHA `467d0243b4fb081c2d637b20ee0958c3bd6ee6d1` 已正常 push；Actions 与 PR 查询均为空，workflow 仅监听 PR/main；未获授权创建 PR，本地通过不替代 CI |

所有模型相关自动化路径均使用 Mock、MockTransport 或 stub fetch；基础设施用例只连接隔离的 PostgreSQL/Redis，没有调用真实 Provider，也不要求 Provider API Key。详细命令和结果见工作日志与 [TESTING.md](TESTING.md)。

## 最近工作日志

[2026-08-25-github-publication-and-ci-policy.md](worklogs/2026-08-25-github-publication-and-ci-policy.md)（公开组织仓库、首次远程 CI 与阶段 commit/push/CI 门禁）

[2026-08-25-phase-2-reliable-execution-foundation.md](worklogs/2026-08-25-phase-2-reliable-execution-foundation.md)（可靠执行基础实现、真实故障验证与剩余阶段边界）

[2026-08-27-complete-evaluation-workflow.md](worklogs/2026-08-27-complete-evaluation-workflow.md)（固定正式数据、真实 API 本地 CLI、恢复、完整报告与本地验收）

[2026-08-27-web-provider-credentials.md](worklogs/2026-08-27-web-provider-credentials.md)（Web 只写 Key、AES-GCM 凭据、共享 keyring、兼容迁移与安全门禁）

[2026-08-27-web-run-ux-and-generation-budgets.md](worklogs/2026-08-27-web-run-ux-and-generation-budgets.md)（Web 长推理配置、评测记录/证据分页与响应式修复；功能提交已 push，精确 SHA 未触发 workflow）

## 当前任务入口

[NEXT_TASK.md](NEXT_TASK.md) 作为后续任务的唯一入口；在 Phase 2 保持 `in_progress` 的前提下，下一切片应优先收敛 P2-05，并为 P2-06/P2-07 的剩余验收留下可复核证据。
