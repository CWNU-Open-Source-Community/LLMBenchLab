# LLMBenchLab Roadmap

> 最后更新：2026-08-30
> 当前阶段：Phase 2 — 可靠性与任务执行；Phase 3 可信本地客观数据切片
> 当前状态：Phase 0–1 `completed`；Phase 2–3 `in_progress`；Phase 4–6 `planned`

## 1. Roadmap 使用方式

本 Roadmap 将项目从可离线复现的个人 MVP，逐步演进为可可靠运行、可扩展 Benchmark、可评测 Judge/Arena 与 Agent、并可安全公开部署的平台。阶段必须按依赖顺序推进；只有当前阶段的验收标准全部满足并留下可复核证据，才能把状态改为 `completed`。

状态定义：

- `planned`：范围已定义，尚未开始实施。
- `in_progress`：已有实际改动，但仍有验收项未满足或未验证。
- `blocked`：存在明确外部阻塞，必须同时记录原因和解除条件。
- `completed`：全部验收标准通过，代码、测试、迁移与文档证据齐全。

全阶段共同规则：

- 默认不跨阶段静默扩展范围；新需求先写入对应阶段文档。
- Benchmark、Evaluator 或公平性规则变化必须更新 `protocol_version`。
- 不同 `protocol_version` 的结果不得无提示混合排名。
- CI 和自动测试只使用 Mock Adapter，不调用真实付费模型。
- 每个阶段结束时同步更新 `PROJECT_STATUS.md`、`NEXT_TASK.md`、`CHANGELOG.md` 和工作日志。
- 涉及架构、安全边界或不可逆数据格式的决定必须记录 ADR。

## 2. 阶段总览

| 阶段 | 主题 | 核心结果 | 状态 | 详细文档 |
| --- | --- | --- | --- | --- |
| Phase 0 | 项目治理和架构 | 可执行的需求、架构、协议、ADR 与持续文档流程 | `completed` | [PHASE-0-GOVERNANCE.md](phases/PHASE-0-GOVERNANCE.md) |
| Phase 1 | MVP 垂直链路 | Mock 模型到 Run、逐题结果与排行榜的离线闭环 | `completed` | [PHASE-1-MVP.md](phases/PHASE-1-MVP.md) |
| Phase 2 | 可靠性与任务执行 | 可靠 Worker、治理/审计、P2-01、P2-06 与 observational overdraw 维护已交付；P2-07 工作包已建立但功能尚未实现 | `in_progress` | [PHASE-2-RELIABILITY.md](phases/PHASE-2-RELIABILITY.md) |
| Phase 3 | 标准 Benchmark 与代码评测 | 已有 MMLU-Pro/GPQA 可信本地切片；IFEval、沙箱与完整插件体系待完成 | `in_progress` | [PHASE-3-BENCHMARKS.md](phases/PHASE-3-BENCHMARKS.md) |
| Phase 4 | Judge、Arena 与长上下文 | 可校准 Judge、Pairwise Judge、个人 Arena 和长上下文评测 | `planned` | [PHASE-4-JUDGE-ARENA.md](phases/PHASE-4-JUDGE-ARENA.md) |
| Phase 5 | Agent、私有与 Live Benchmark | 工具调用轨迹、隔离私有集和持续更新的 Live Benchmark | `planned` | [PHASE-5-AGENT-LIVE.md](phases/PHASE-5-AGENT-LIVE.md) |
| Phase 6 | 公共发布 | 多用户、鉴权、运维加固和正式版本发布 | `planned` | [PHASE-6-PUBLIC-RELEASE.md](phases/PHASE-6-PUBLIC-RELEASE.md) |

## 3. Phase 0：项目治理和架构

### 阶段目标

建立能约束后续开发的产品边界、需求基线、架构、评测协议、ADR、任务模板和状态追踪体系，使实现与验收都有唯一、可追溯的依据。

### 功能范围

- Project Charter、功能/非功能需求和 MVP 验收条件。
- 系统上下文、模块边界、数据流、Run 生命周期和扩展接口。
- `llmbenchlab-protocol-v1` 的公平配置、评分与可比性规则。
- Roadmap、阶段文档、ADR、任务计划模板和工作日志模板。
- `AGENTS.md` 的持续开发规则与 Definition of Done。
- 项目状态、下一任务、测试、安全、部署和 GitHub 流程文档。

### 非目标

- 不以治理文档替代 Phase 1 的可运行实现。
- 不承诺生产级可用性、公开托管、多用户或分布式执行。
- 不在此阶段接入真实付费模型或导入大型第三方数据集。

### 依赖

- 无产品阶段依赖。
- 以初始仓库勘察、用户规格和开源仓库约束为输入。

### 任务拆分

1. 记录仓库初始状态、范围、假设和风险。
2. 完成 Charter、Requirements、Architecture 与 Benchmark Protocol。
3. 确定技术栈、SQLite-first、协议和密钥管理 ADR。
4. 建立 Roadmap、Phase 0–6 文档和计划/工作日志模板。
5. 固化贡献、测试、安全、部署、API 与 GitHub 工作流约定。
6. 建立 Project Status、Next Task 和 Changelog 的持续更新流程。

### 验收标准

- 项目目标、用户、边界和非目标明确。
- Requirements、Architecture、Benchmark Protocol 与 Roadmap 内容完整。
- 每个阶段均有目标、范围、非目标、依赖、任务、验收、风险、交付物和状态。
- `AGENTS.md` 固化任务前、中、后的文档与验证流程，并含 Definition of Done。
- `PLANS.md` 提供复杂任务执行计划模板。
- 主要技术与安全决定已有 ADR。
- `PROJECT_STATUS.md` 能反映真实进度并指向最近工作日志和下一任务。

### 风险

- 文档先于实现而发生漂移；通过 Phase 1 验收清单、工作日志和结束前同步规则降低风险。
- 过早冻结架构；通过 ADR 可修订机制和稳定接口边界保留演进空间。
- 误把计划当作已交付能力；所有状态只按可验证证据更新。

### 交付物

- `docs/PROJECT_CHARTER.md`、`docs/REQUIREMENTS.md`、`docs/ARCHITECTURE.md`。
- `docs/BENCHMARK_PROTOCOL.md`、本 Roadmap 与 `docs/phases/`。
- `AGENTS.md`、`PLANS.md`、`docs/decisions/`、`docs/templates/`。
- `docs/PROJECT_STATUS.md`、`docs/NEXT_TASK.md` 与工作日志。

### 状态

`completed`（2026-08-24）。治理与架构文档体系已建立；任何后续实现差异都必须在对应阶段修正文档，不回溯虚构完成证据。

## 4. Phase 1：MVP 垂直链路

### 阶段目标

交付一个默认离线、可复现、可本地运行的端到端 MVP：注册 Mock 模型，载入 Demo Benchmark，后台评测并保存逐题结果，最后在前端展示进度、详情和排行榜。

### 功能范围

- FastAPI、SQLAlchemy、Alembic 与 SQLite 本地后端。
- `Model`、`Benchmark`、`Question`、`EvaluationRun`、`EvaluationResponse` 核心模型。
- Mock 与 OpenAI-compatible Adapter；自动测试只运行 Mock。
- exact match、multiple choice、numeric Evaluator。
- 版本化数据集导入、稳定 SHA-256 和 12–20 道原创 Demo 题。
- 进程内受控后台 Runner、取消标志、逐题隔离、重启遗留状态处理。
- `/api/v1` 模型、Benchmark、Run、响应、排行榜和汇总指标 API。
- Dashboard、Models、Benchmarks、New Run、Run Detail、Leaderboard 页面。
- 后端/前端测试、离线 Smoke Test、CI、Makefile、Compose 和运行文档。

### 非目标

- 不引入 PostgreSQL、Redis、独立 Worker 或复杂分布式调度。
- 不下载完整 MMLU/GPQA/HumanEval 等大型数据集。
- 不执行不可信代码，不实现 LLM Judge、Arena、Agent 或多用户系统。
- 不把 MVP 直接暴露到公网，也不提供商业计费能力。

### 依赖

- Phase 0 `completed`。
- Python/Node 本地运行环境；OpenAI-compatible 实际调用是可选能力，不是离线验收依赖。

### 任务拆分

1. 建立后端、数据库、迁移、Schema、错误处理与安全配置。
2. 实现数据集 Loader/Validator/Hash 和内置 Demo Benchmark。
3. 实现 Adapter、三类 Evaluator、Runner、汇总指标与排行榜。
4. 实现版本化 REST API 和 OpenAPI 文档。
5. 实现六个前端页面、轮询、错误/空/加载状态和响应式布局。
6. 完成后端单元/集成测试、前端测试和纯离线 Smoke Test。
7. 完成开发脚本、Compose、CI、安全/部署/测试/API 文档。
8. 执行最终验证并如实更新项目状态和下一任务。

### 验收标准

- 后端可启动且前端可 production build。
- Mock 模型可注册，Demo Benchmark 可载入并明确标记为非正式数据。
- Run 可创建、后台完成、逐题持久化，单题失败不终止整个 Run。
- 三种 Evaluator、严格总分、`completion_rate` 和 `answered_accuracy` 工作正确。
- 前端能显示运行进度、配置快照、逐题结果和可筛选排行榜。
- 离线 Smoke Test 和关键单元测试通过，CI 配置完整。
- API/运行/测试/安全文档与实际行为一致，不含真实密钥。
- 规格列出的 20 项 Phase 1 验收条件全部有可复核证据。

### 风险

- 进程内任务在崩溃后无法自动续跑；MVP 将遗留 `running` 标记为失败/中断，Phase 2 再实现恢复。
- 任意 `base_url` 带来 SSRF 风险；MVP 明示信任边界，公开部署前必须实施 allowlist/网络隔离。
- 上游输出不稳定导致解析失败；保留 raw response、解析错误与严格计零规则。
- SQLite 写并发有限；保持低并发并把高并发迁移留给 Phase 2。

### 交付物

- `backend/`、`frontend/`、`benchmarks/demo-general/`。
- `scripts/`、`Makefile`、`compose.yaml`、`.env.example`。
- `.github/workflows/ci.yml` 与开源协作文件。
- API、数据格式、测试、部署、安全、状态与工作日志文档。

### 状态

`completed`（2026-08-25）。全部 20 项 MVP 验收条件已由离线端到端、自动测试、静态门禁、迁移、构建及 Compose 运行证据确认；详细结果见 Phase 1 文档和 Bootstrap 工作日志。

## 5. Phase 2：可靠性与任务执行

### 阶段目标

将单进程 SQLite MVP 演进为可恢复、可由受限数量独立 Worker 并发执行、可观测的任务架构，同时保持 Phase 1 协议与 API 的兼容迁移路径。本阶段不承诺未经容量验证的无限横向扩展、生产高可用或灾难恢复 SLA。

### 功能范围

- PostgreSQL 作为共享部署数据库和任务唯一事实来源；SQLite 保留单 Worker 本地兼容，并提供显式、单向、可对账的 SQLite→PostgreSQL 导入工具。
- Redis Streams 只提供 at-least-once 低延迟通知；任务状态、取消、租约、重试和 dead-letter 均由数据库裁决，Redis 故障由数据库扫描恢复。
- 独立 Worker、原子领取、数据库时间租约、心跳、单调 fencing token、逐题幂等、有限重试、取消、过期接管和 dead-letter；大 Run 快照加载移出事件循环并保持租约心跳，dead-letter 前从持久化 Response 重聚合证据。
- Alembic `0004` 的 policy/scope/minute bucket/question execution/Provider reservation/audit event 六类治理表及 `0005` 的 Worker process/progress 表与 bounded audit indexes；Run/Response 证据字段和 13 表 SQLite→PostgreSQL importer。
- managed Run 冻结 policy/hash 与显式 overrides；global/provider/model/run 四层 concurrency、固定窗口 RPM/TPM、global/run lifetime request/Token/USD budget 和逐 Provider HTTP attempt ledger。
- 当前 data-only head `20260830_0007` 按 ADR-0018 将观测 input 估算与 hard reservation 分离：无显式 input bound 时不生成 input reservation/reserved cost，actual usage 仍保存；显式 input/output 上界及由完整上界和价格派生的 reserved cost 超额继续 fail closed。本地完整门禁、当前 SQLite 迁移、修正 SHA `cb00924…` 的 real-Compose 9/9 与远程 CI 4/4 均通过。
- materialized counter 只作 ledger 投影；counter、policy/hash 或 Run override 漂移 fail closed。confirmed pre-send release 按 ADR-0011 不消耗未发送 HTTP retry。
- 有限 backlog、typed `429`、database not-before、question quantum、dispatch/failure 分离和跨 Model 公平排序。
- typed audit、Run audit、task history/latency、Provider metadata、credential 非秘密事件和前端治理状态；P2-06 实现 SHA `9a20676…` 另交付固定低基数 Prometheus exporter/八条规则、canonical retention archive/verify/reconcile/restore/delete、Worker DB-time progress/liveness 聚合与全日志源治理。
- enhanced Mock-only capacity/acceptance、真实 PostgreSQL 竞争测试，以及绑定 clean SHA 的正式多轮单机控制面资格；backup/restore 仍待完成。

### 非目标

- 不新增大型标准 Benchmark、代码执行、Judge、Arena 或 Agent 能力。
- 不实现 Kubernetes、多区域容灾或无限水平扩展。
- 不以提高吞吐为由改变评分协议或混合不可比结果。

### 依赖

- Phase 1 `completed`，并有稳定的核心实体、Runner 接口和基线 Smoke Test。
- 可用于集成测试的 PostgreSQL 与 Redis 环境。

### 任务拆分

| ID | 状态 | 已交付 / 剩余范围 |
| --- | --- | --- |
| P2-01 一致性与容量设计 | 已交付 | ADR-0012～0014、DB truth/lease/fencing/治理、v2 四 cell 多轮统计、恢复与连接模型已实现；clean SHA `b6a35fe…` 的 1+5 资格为 23/23、`qualified`；证据文档 `875f13a…` 的精确 SHA CI 4/4 成功 |
| P2-02 PostgreSQL 迁移 | 切片已实现 | `0002`/`0003` 可靠性与凭据基础、`0004` 六类治理/审计表已通过既有门禁；P2-06 实现 SHA `9a20676…` 增加 `0005`、13 表 importer、live-Worker preflight 与 downgrade guard，实现精确 SHA CI 已通过 |
| P2-03 Queue/Worker | 可靠基础已交付 | Redis Streams 通知、独立 Worker、数据库扫描、租约、心跳、fencing 和重复消息 no-op 已交付；P2-06 实现 SHA `9a20676…` 增加 generation 级 DB-time progress，dependency probe 仍只检查 capability |
| P2-04 生命周期可靠性 | 可靠基础已交付 | retry/取消/恢复/dead-letter/终态重算及三个确定性 DB crash-seam 场景已通过完整 Compose acceptance；外部调用仍不保证 exactly-once |
| P2-05 并发治理 | 切片已实现 | 四层 concurrency/RPM/TPM/lifetime budget、per-attempt ledger、backpressure、finite quantum、公平排序和完整性 fail-closed 已实现；真实 PG/capacity/acceptance/精确 SHA CI 候选门禁已通过 |
| P2-05 observational overdraw 维护 | `completed` | ADR-0018 与 data-only `0007` 只重算 overdrawn 并保留 ledger/actual，active reservation 时拒绝；当前库迁移和本地门禁通过，最终 SHA `cb00924…` 的 run `33271095910` 4/4 成功 |
| P2-06 可观测性 | `completed` | exporter/八规则、retention CLI、Worker DB-time progress、`0005`/13 表 importer 与全日志源治理已实现；`9a20676…` 已 push、PR #3、实现 run `33164609388` 4/4，clean capacity/9/9 acceptance 全绿；evidence-doc commit `ec29596…` 已 push且精确 SHA run `33165775037` 4/4 |
| P2-07 验证与运维 | `planned` | ADR-0016、独立计划与工作日志已建立；功能尚未实现，后续从最小只读 verifier 开始，再开展数据库+keyring restore、Redis 重建、告警响应与完整失败矩阵 |

### 验收标准

- `delivered`：API 执行中重启和实际租约 owner Worker `SIGKILL` 后，未完成 Mock Run 可恢复，已有 Response ID 保持唯一。
- `delivered`：真实 PostgreSQL 并发领取只有一个有效 lease；自然过期后由递增 fencing token 接管，陈旧 owner 写入被拒绝。
- `delivered/partial`：pending/running 取消有真实 Compose 证据；有限重试、超时和 dead-letter 有自动化状态机/Runner 证据，但尚未把所有失败组合都纳入完整生产式故障演练。
- `delivered`：历史 `0004` / 12 表 importer、四层治理、逐 attempt ledger、counter 重算 fail-closed、policy/override freeze、typed backpressure 和 finite fairness 已实现，并在精确候选 SHA 的真实 PG/capacity/acceptance 与远程 CI 通过。
- `completed`：ADR-0018 修复不改变 protocol-v1 或历史事实；只有显式 input/output reservation 或由完整显式上界和价格派生的 reserved cost 才触发对应 overdraw，`0007` 只重算 materialized flag 且在 active reservation 时拒绝。本地完整验证与当前库迁移通过，最终 SHA `cb00924…` 的远程门禁 4/4 成功。
- `completed`：P2-06 的 `0005` / 13 表 importer、Worker DB-time progress、固定低基数 exporter、八条规则、canonical audit archive/verify/reconcile/restore/delete 与全日志源治理已在 SHA `9a20676dcf545040782f04c166205d0043345753` 实现；本地 lint/test/integration/rules 与修复后 76-file 技术/安全终审全绿，实现已 push 到 PR #3，精确 SHA run `33164609388` 4/4 成功。Clean acceptance `.pytest_cache/artifacts/phase2-acceptance/llmbenchlab-p2-92e173eeee28/evidence.json`（SHA-256 `e4ffb8668fd3fa62d59b5d83f5c29eede35b327d88e6099345acd5950670fc47`）9/9、Worker `2/2/2/0/0`、cleanup C/V/N empty；clean capacity `.pytest_cache/artifacts/phase2-capacity/llmbenchlab-p2-ca5673061b0f/evidence.json`（SHA-256 `2382f9138f09028f269d76c341b236dd4089d678c8a2323582045fac2b4f5039`）记录 QPS `7.267474/12.962228/9.333604`、wall `8.255963/4.628834/6.428385s`、18/270/270/271/1230、0 question error/drift/duplicate/PEL/lag、expected=2/shortfall=0 与 cleanup C/V/N/image=0、image `1/1/0/0`。两者均为 clean-SHA Mock-only evidence，不是 SLO；此前 dirty evidence 保留为历史。Evidence-doc commit [`ec2959680459a14aa308bd4d9ebcc6bb7bfcf3a6`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/commit/ec2959680459a14aa308bd4d9ebcc6bb7bfcf3a6) 已 push，其精确 SHA [run `33165775037`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33165775037) 4/4 成功，P2-06 仓库级收尾完成。
- `delivered`：`P2-local-control-plane-v2` 在 clean SHA `b6a35fe…` 完成 1 warm-up + 5 measured、23/23 SLO、逐轮 hard invariant/cleanup 和 `qualified` 容量模型；只限定记录的 Mock-only 单机拓扑。
- `planned`：P2-07 工作包和恢复不变量已冻结，但功能尚未实现；后续开展 backup/restore、Redis 重建、告警响应与完整恢复矩阵。三个确定性 DB crash-seam 场景与正式恢复时长目标不能替代生产恢复认证。
- `delivered`：治理实现 SHA `665244e…` 已 push；本地 enhanced capacity、9/9 acceptance 与远程 run `33099260233` 4/4 均通过。
- `delivered`：P2-01 v2 实现 SHA `b6a35fe…` 已 push；本地 aggregate SHA-256 `a76d167b…d0d9` 为 23/23，实现 run `33146681285` 4/4 成功；证据文档 commit `875f13a…` 已 push，收尾 run `33150080341` 4/4 成功。
- `delivered`：P2-06 实现 SHA `9a20676…` 已 push 至 PR #3，实现 run `33164609388` 4/4 成功，绑定同一 SHA 的 clean capacity 与 9/9 acceptance 已通过；evidence closeout 文档 SHA `ec29596…` 的 run `33165775037` 也已 4/4 成功，仓库级收尾完成。
- `delivered`：既有 API、离线 Mock Smoke 和 `llmbenchlab-protocol-v1` 评分/聚合回归继续通过，没有调用真实 Provider。

### 风险

- 队列的 at-least-once 语义造成重复写；使用幂等键、唯一约束和事务状态转换。
- 数据库与队列状态分裂；明确数据库为事实来源，并以可重放调度事件修复。
- Worker 在 Provider 响应后、本地提交前崩溃仍可能重复上游调用或计费；不得把本地幂等描述为 Provider exactly-once。
- materialized counter、policy 或 override 漂移可能绕过治理；当前实现从 ledger/冻结事实重算并 fail closed，精确候选真实 PostgreSQL gate 已通过，后续仍需持续回归和生产规模校准。
- Mock 单机资格不能外推真实 Provider、其他硬件、多主机 HA 或生产 SLA；任何 profile/环境/Worker/连接参数变化都必须重新设计或测量。

### 交付物

- 已交付基础：PostgreSQL/SQLite migration、Redis/Worker/lease/fencing/recovery、六服务 Compose 与历史故障证据。
- 已交付候选切片：`0004`、历史 12 表 importer、四层治理/ledger/backpressure/fairness、typed audit/history/Provider/credential evidence、UI 状态、enhanced capacity/PG tests 与运维文档。
- 已完成 P2-06：`0005` / 13 表 importer、Worker generation/progress、Prometheus exporter 与八条规则、canonical audit retention CLI、公共 retained-row validator、全日志源治理，以及实现/evidence-doc 两个精确 SHA 的远程 CI。
- 治理候选门禁已完成；P2-01 的 v2 多轮资格、实现 commit/push、实现 SHA 4/4 CI 与证据文档精确 SHA 4/4 CI 也已完成，P2-01 仓库级收尾完成。
- 阶段正式剩余：实施状态为 `planned` 的 P2-07 PostgreSQL+keyring backup/restore、Redis 重建、告警响应与完整故障矩阵。

### 状态

`in_progress`。可靠执行基础、P2-05 治理和 P2-01 v2 单机控制面资格已有可复核证据与精确 SHA 远程门禁；P2-06 与 observational overdraw 维护均为 `completed`。P2-07 工作包已建立，状态为 `planned`，功能尚未实现。不得把 Phase 2、生产 HA、灾难恢复 SLA、无限横向扩展或 Provider exactly-once 标记为完成。

## 6. Phase 3：标准 Benchmark 与代码评测

### 阶段目标

在合法授权、可追溯版本和安全隔离的前提下，引入代表性标准 Benchmark、受限代码能力评测和数据集插件机制。

### 功能范围

- MMLU-Pro、GPQA、IFEval 的独立插件、获取说明、许可元数据和固定版本。
- 通用数据集插件接口、缓存、校验、来源证明、去重与 Hash 清单。
- 代码题 Schema、编译/运行结果模型和资源受限沙箱执行器。
- 语言/任务分组指标、子集筛选和可比性提示。
- 污染风险声明、数据卡、导入合规检查和可复现实验清单。

### 非目标

- 不把受许可证限制的数据打包进仓库。
- 不在 API/Worker 主机上直接执行不可信代码。
- 不实现 Judge、Arena、Agent 或公共多用户托管。

### 依赖

- Phase 2 `completed`，特别是可靠 Worker、超时、取消、资源预算和审计能力。
- 明确的数据集许可审查流程和可用的容器/沙箱运行环境。

用户在 2026-08-27 明确要求先形成可真实模型测试的完整客观评测流程；
[ADR-0006](decisions/ADR-0006-local-real-provider-evaluation.md) 因此批准一个仅限可信本地、
无全局预算承诺的提前切片。该偏差不把 Phase 2 或 Phase 3 标为完成，也不放宽沙箱前置依赖。
该切片只允许远程 HTTPS（HTTP 仅 loopback），只接受 identity 编码；按
[ADR-0008](decisions/ADR-0008-openai-compatible-sse-transport.md)，Chat JSON 成功体、SSE wire/单事件/聚合 content、错误体分别限制为 4 MiB、64 MiB/1 MiB/4 MiB、64 KiB。模型发现拒绝反射当前 Key 的模型 ID，canary 拒绝不同返回模型，成功证据在持久化前
精确移除当前 Key。报告指标从计划题目与 Responses 派生，并用 `metrics_provenance` 标出 Run 字段漂移。

### 任务拆分

1. 定义 Dataset Plugin 与代码评测协议扩展并记录 ADR。
2. 为各数据集实现许可安全的下载/导入、版本固定与数据卡。
3. 实现离线缓存、Hash 清单、分片和验证失败诊断。
4. 构建无网络、最小权限、CPU/内存/时间/输出受限的代码沙箱。
5. 实现编译、运行、测试用例隔离和结构化错误分类。
6. 增加分组指标、UI 筛选、测试夹具与端到端验证。
7. 完成威胁建模、渗透测试范围和操作手册。

### 验收标准

- 每个标准 Benchmark 均可由固定来源和版本重复导入并得到相同 Hash。
- 许可证、引用、数据卡、污染风险和分发限制可见且可审计。
- 插件故障不会破坏核心服务，格式错误能定位到数据集/记录。
- 不可信代码在隔离环境运行，默认无网络，并强制资源与输出上限。
- 沙箱逃逸防线、超时、fork bomb、磁盘耗尽等关键威胁有自动化验证。
- 指标按任务/子集正确聚合，协议不兼容时不混排。

### 风险

- 数据集许可或访问条款变化；只保存来源和校验信息，下载前再次确认许可。
- Benchmark 污染使结果失真；披露训练污染不确定性并支持私有/Live 路线。
- 沙箱逃逸造成主机危害；采用专用隔离边界、默认拒绝网络和最小权限。

### 交付物

- 数据集插件 SDK、MMLU-Pro/GPQA/IFEval 插件与数据卡。
- 代码评测 Schema、沙箱执行器和安全测试套件。
- 数据版本/Hash 清单、分组指标 UI 与操作文档。

### 状态

`in_progress`（2026-08-27 开始）。MMLU-Pro 与 GPQA-Diamond 已有固定 revision/SHA、
可复现转换、受限 real-Provider CLI 和证据派生分组报告切片；IFEval、正式 Plugin SDK、代码题模型、
安全沙箱、分组 UI、完整数据卡/红队及全部阶段验收仍未完成。

## 7. Phase 4：Judge、Arena 与长上下文

### 阶段目标

提供可校准、可审计的主观与成对比较能力，以及不会掩盖成本和截断影响的长上下文评测。

### 功能范围

- LLM-as-a-Judge rubric、结构化输出、重试、校准和版本化 Judge 配置。
- Pairwise Judge，含顺序交换、平局、位置偏差检测与成对聚合。
- 个人 Arena 盲评、随机顺序、投票撤销/审计和基础排名。
- 长上下文数据生成/导入、needle/retrieval/综合任务和长度分桶指标。
- Judge 模型/提示/采样快照、费用追踪与客观指标并列展示。

### 非目标

- 不宣称 Judge 分数等同于人类偏好或绝对质量。
- 不建立公开众包 Arena、商业榜单或未经校准的单一总排名。
- 不实现 Agent 工具调用或多用户权限。

### 依赖

- Phase 2 `completed` 的可靠调度、预算和审计能力。
- Phase 3 的插件/数据版本机制可复用；长上下文不强依赖代码沙箱。
- 已批准的 Judge 数据、rubric 与人工校准样本。

### 任务拆分

1. 定义 Judge/Pairwise/Arena/Long-context 协议与数据模型。
2. 实现 rubric、结构化解析、Judge 配置快照和失败处理。
3. 实现顺序交换、平局、重复评审和一致性/偏差分析。
4. 实现匿名 Arena 会话、随机呈现、投票审计和排名算法。
5. 实现上下文长度分桶、截断检测、成本限制和任务插件。
6. 建立人工金标校准集、稳定性测试和跨 Judge 对照。
7. 更新 UI、报告、风险说明和费用治理。

### 验收标准

- Judge 结果可追溯到模型、rubric、prompt、参数、协议和原始输出。
- Pairwise 顺序互换测试可量化位置偏差；冲突与平局不被静默丢弃。
- Judge 在校准集上达到预先登记的一致性门槛，并展示置信信息。
- Arena 默认隐藏模型身份并随机左右顺序，投票有审计记录。
- 长上下文报告按实际输入长度展示准确率、失败、延迟、Token 和成本。
- Judge/客观得分明确分栏，不同协议与 Judge 版本不混排。

### 风险

- Judge 偏差、自偏好和提示敏感性；使用多次/多 Judge 校准、顺序交换和公开局限。
- Arena 样本少导致排名不稳定；显示样本量和不确定性，不输出过度精确名次。
- 长上下文费用激增或上游截断；实行预算上限、预估成本和截断检测。

### 交付物

- Judge/Pairwise 引擎、版本化 rubric 和校准报告。
- 个人 Arena 界面、投票审计与排名模块。
- 长上下文插件、分桶报告和成本/截断保护。

### 状态

`planned`。

## 8. Phase 5：Agent、私有与 Live Benchmark

### 阶段目标

评测模型在受控工具环境中的多步任务能力，并用隔离私有集和持续轮换的 Live Benchmark 降低数据污染与过拟合。

### 功能范围

- Agent/Tool Use 任务、工具契约、轨迹、环境状态和成功判定协议。
- 受控工具沙箱、权限清单、网络策略、预算和副作用隔离。
- 私有 Benchmark 加密存储、访问审计、泄漏防护和结果脱敏。
- Live Benchmark 发布、冻结、轮换、退役、Hash 与时间窗口管理。
- 轨迹级指标：任务成功、步骤、无效调用、恢复能力、延迟和成本。
- 可重复环境快照、回放和人工复核工作流。

### 非目标

- 不允许 Agent 默认访问宿主机、真实账户或无限制公网。
- 不把私有题目、参考答案或完整轨迹公开到榜单。
- 不在本阶段提供公共注册、组织级权限或商业化服务。

### 依赖

- Phase 2 的可靠任务调度、预算、取消和审计。
- Phase 3 的插件与安全沙箱基础。
- Phase 4 的主观评审能力可用于开放式轨迹，但不得替代客观成功条件。

### 任务拆分

1. 定义 Agent Episode、Tool Call、环境状态和评分协议。
2. 实现工具注册表、Schema 校验、权限策略、隔离和可回放轨迹。
3. 建立无真实副作用的参考工具环境与基准任务。
4. 实现私有数据加密、密钥轮换、最小权限和审计导出。
5. 建立 Live Benchmark 生命周期、密封发布和防泄漏流程。
6. 实现轨迹查看器、复核、分层指标和不确定结果处理。
7. 完成红队测试、泄漏演练、恢复演练和文档。

### 验收标准

- 相同环境快照与固定输入可重放 Agent 轨迹，并区分模型与环境非确定性。
- 未授权工具、参数和网络访问被默认拒绝并留下审计事件。
- 任务成功规则与 Judge 辅助规则分离，工具错误不被误判为模型成功。
- 私有题目和答案不会出现在普通 API、日志、前端或公开导出中。
- Live 集每个版本均有生效/冻结/退役时间、不可变 Hash 和泄漏处置记录。
- 预算、步数、时间和副作用限制均有端到端测试。

### 风险

- 工具调用产生真实副作用；使用仿真/临时环境、显式 allowlist 和默认拒绝策略。
- Prompt injection 窃取私有答案；分离模型可见数据与评分数据，并做泄漏检测。
- Live 题维护成本与可比性冲突；冻结评测窗口并保留版本化历史结果。

### 交付物

- Agent/Tool Use 协议、Runner、轨迹模型和回放工具。
- 受控工具环境、权限策略和安全测试。
- 私有 Benchmark 保险库、Live 生命周期服务和审计文档。

### 状态

`planned`。

## 9. Phase 6：多用户、公共部署与正式发布

### 阶段目标

在保留个人本地模式的同时，完成多用户隔离、鉴权、滥用防护、生产运维和开源发布治理，形成可安全公开部署的正式版本。

### 功能范围

- 用户、组织/项目、角色和资源级授权。
- 登录、会话/Token 生命周期、可选外部身份提供商和账户恢复。
- 租户数据隔离、密钥保险库、审计日志、配额、限流和费用归属。
- 生产数据库/队列、TLS、备份恢复、监控告警、SLO 与事件响应。
- 上传/URL/工具调用防护、SSRF 控制、恶意文件处理和滥用响应。
- API 版本/弃用策略、升级指南、SBOM、签名构建和正式版本发布。

### 非目标

- 不承诺未经容量验证的超大规模托管或多区域强一致。
- 不默认启用付费计费、模型转售或公开无限制注册。
- 不牺牲离线个人部署与 Mock 验证路径。

### 依赖

- Phase 2 的生产级执行和运维基础。
- Phase 3–5 中计划进入正式版的能力已通过各自验收；可按发布范围选择性启用。
- 完成隐私、安全、许可证和第三方服务条款审查。

### 任务拆分

1. 建立公共部署威胁模型、数据分类、隐私和发布准入标准。
2. 实现身份、会话、RBAC/项目隔离和授权测试矩阵。
3. 迁移密钥至保险库，实施租户隔离、配额、限流和审计。
4. 加固网络、上传、`base_url`、CORS、Headers 与供应链。
5. 建立 SLO、容量测试、备份恢复、灾难演练和事件响应。
6. 完成 API 稳定性、数据迁移、兼容/回滚和升级文档。
7. 执行安全评审、发布候选验证、版本标记和发布说明。

### 验收标准

- 未授权访问和跨租户读写在完整授权矩阵中被拒绝。
- 密钥不进入数据库明文字段、日志、错误响应或客户端包。
- SSRF、恶意上传、滥用、限流和高费用路径有防护与验证证据。
- 备份恢复、数据库迁移回滚和队列灾难恢复按目标 RPO/RTO 演练通过。
- 监控、告警、SLO、值守与事件响应流程可执行。
- 发布候选通过功能、性能、安全、可访问性、许可证和升级检查。
- 版本说明、兼容矩阵、SBOM、校验/签名产物和支持政策齐全。

### 风险

- 鉴权或租户隔离缺陷造成数据泄漏；采用默认拒绝、集中授权和系统化安全测试。
- 公网入口放大 SSRF、上传和成本风险；分层网络策略、配额和自动熔断。
- 正式发布破坏本地用户升级；提供版本化迁移、备份前置检查和回滚路径。

### 交付物

- 多用户身份与授权系统、租户隔离和密钥管理。
- 生产部署清单、监控/SLO、备份恢复与安全运行手册。
- Release Candidate 报告、SBOM、签名产物、升级指南和正式 Release Notes。

### 状态

`planned`。

## 10. 阶段转换门槛

每次阶段转换必须同时满足：

1. 阶段文档中的验收项全部有测试、命令输出、截图或审计记录等证据。
2. 新增数据结构有迁移与回滚说明，新增 API 有版本与兼容性说明。
3. 安全风险已处置或明确记录为可接受的已知限制。
4. Roadmap、当前阶段、Project Status、Next Task、Changelog 和工作日志状态一致。
5. 未运行的验证被明确列出，不以“配置完成”替代“实际通过”。
6. 下一阶段可以在不猜测前置背景的情况下从对应 Phase 文档启动。
