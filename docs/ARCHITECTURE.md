# LLMBenchLab 架构

本文描述 LLMBenchLab 当前 Phase 1 产品边界、Phase 2 可靠执行/治理/可观测性工作树，以及可信本地 MMLU-Pro/GPQA-Diamond 真实评测垂直切片。前端仍通过 REST API 操作同一组领域对象，但 API 不执行评测：PostgreSQL/数据库是任务、四层治理、逐 Provider attempt ledger、typed audit、Worker progress 与评分证据的唯一事实来源，Redis Streams 是非权威的 at-least-once 通知层，独立 Worker 或受信本地 CLI 用数据库租约、心跳与 fencing 执行同一 Runner。SQLite 保留为单 Worker 本地兼容路径。Exporter、告警和普通文件 archive 都是数据库事实的受控投影，不是第二状态机。Phase 2/3 总状态没有因此完成，这不是公网、HA、生产架构或 SLA 声明。

## 架构目标与原则

- **离线可验证**：Mock adapter、Demo Benchmark、测试和 CI 均不得访问真实模型 API。
- **真实调用显式 opt-in**：受信本地操作者可以在 Web Model 表单写入 Provider Key 后主动创建 Run，也可使用带发现、canary 和费用确认的正式 CLI；Mock/测试路径始终离线。
- **可复现**：Run 保存模型、数据集、Evaluator、Prompt、生成参数、并发和重试策略的快照。
- **失败隔离**：单题失败被持久化并计 0 分，不中断其余题目。
- **秘密最小化**：Web Key 只以 AES-GCM authenticated ciphertext 持久化，部署 keyring 与数据库分离；兼容模式只保存环境变量名。API/Run/Response 不返回 Key 或 envelope，上游成功内容和相关标识在进入持久化边界前检查当前 Key 的精确反射。
- **事实单一**：Run 状态、取消意图、attempt、租约、Response、聚合、错误、dead-letter、治理 policy/scope/minute bucket、逐调用 reservation/settlement、typed audit 与 Worker progress 只由数据库裁决；Redis、日志、Prometheus、告警、archive manifest 和内存不是第二事实来源。
- **at-least-once 与幂等**：通知可重复，逐题写入以 `(run_id, question_id)` 唯一并由当前 lease token fencing；不声称 Provider 调用或计费 exactly-once。
- **本地 admission，不冒充账单**：managed Web/API Run 在数据库中执行 global/provider/model/run 四层限流、预算与背压；它限制本地开始新 attempt 的许可，不会取消崩溃后仍在 Provider 运行的幽灵请求，也不把本地保守 consumed 伪装成 Provider 账单真值。
- **边界稳定**：Adapter、Evaluator、Dataset Loader 和 Runner 通过明确接口解耦，便于后续替换实现。

## 系统上下文

```mermaid
flowchart LR
    User[个人开发者或研究人员]
    Browser[浏览器]
    API[FastAPI API]
    Worker[独立 Worker]
    DB[(PostgreSQL / SQLite)]
    Queue[(Redis Streams)]
    Dataset[本地 Benchmark 目录]
    Pinned[固定 HTTPS MMLU-Pro / GPQA 源]
    CLI[可信本地评测 CLI]
    Reports[本地完整报告]
    Env[进程环境变量]
    Keyring[部署 AES keyring]
    Upstream[OpenAI-compatible API]
    Git[本地 Git 元数据]

    User --> Browser
    Browser -->|HTTP /api/v1；Model 写入可含 writeOnly api_key| API
    Dataset -->|manifest.json 与 questions.jsonl| API
    Pinned -->|固定 revision + 大小/SHA 校验| CLI
    CLI -->|转换、创建/恢复 Run、fenced 写入| DB
    CLI -->|summary / groups / responses| Reports
    Git -->|创建 Run 时读取 commit SHA| API
    API -->|事务写入| DB
    API -.->|commit 后 best-effort XADD| Queue
    Queue -.->|at-least-once 通知| Worker
    Worker -->|对账、租约、fenced 写入| DB
    API -->|AES-GCM credential envelope| DB
    Keyring -->|加密 Web Key| API
    Keyring -->|解密 stored credential| Worker
    Env -->|兼容 environment 模式运行时读取| Worker
    Env -->|仅显式本地运行时读取密钥| CLI
    Worker -->|远端 HTTPS；HTTP 仅 loopback| Upstream
    CLI -->|发现 + canary + 题目；同一传输限制| Upstream
    API -->|状态、结果与排行榜| Browser
```

信任边界如下：Benchmark 文件、固定上游下载的内容和用户提供的 `base_url` 均视为不可信输入；上游响应也不能直接作为日志或 HTML。固定数据转换器以 revision、大小和 SHA-256 限定供应链，但 Hash 不是签名。Mock adapter 必须完全离线。浏览器只在 Model 表单提交期间持有 Web Key，并经受信 loopback 的 API 请求体发送；API 使用独立部署 keyring 加密后只写 envelope，Worker 使用同一 keyring 在请求前解密。兼容环境变量模式和受信本地 CLI 仍在执行进程中读取 Key。远端 Provider 只允许 HTTPS，明文 HTTP 仅允许字面 loopback/localhost；这仍不阻止 HTTPS 私网、云元数据或 DNS rebinding。任何非 loopback 浏览器/API 链路都必须另行提供 TLS、认证与授权。导出的报告虽经秘密脱敏，仍含题目和原始回答，属于敏感本地 artifact。

## 容器与模块

```mermaid
flowchart TB
    subgraph FE[React 单页应用]
        Pages[Dashboard、Models、Benchmarks、Runs、New Run、Run Detail、Leaderboard]
        Client[集中式 API Client]
        Poller[Run、证据页与 progress block 轮询]
        Pages --> Client
        Pages --> Poller
    end

    subgraph APIProcess[FastAPI API 进程]
        Routes[Routes 与 Pydantic Schemas]
        Services[应用服务]
        Governance[Governance admission / audit]
        Importer[Dataset Loader 与 Validator]
        QueuePublisher[Redis Stream Publisher]
        Routes --> Services
        Services --> Governance
        Services --> Importer
        Services --> APIRepo[SQLAlchemy]
        Services -.->|commit 后通知| QueuePublisher
    end

    subgraph WorkerProcess[独立 Worker 进程]
        Reconcile[数据库 reconciliation]
        QueueConsumer[Redis Consumer Group]
        LeaseRepo[RunLeaseRepository]
        AttemptLedger[Provider attempt controller]
        Runner[EvaluationRunner]
        Adapters[ModelAdapter Registry]
        Evaluators[Evaluator Registry]
        Reconcile --> LeaseRepo
        QueueConsumer --> LeaseRepo
        LeaseRepo --> Runner
        Runner --> Adapters
        Adapters --> AttemptLedger
        Runner --> Evaluators
    end

    DB[(PostgreSQL / SQLite)]
    Redis[(Redis Streams)]
    Files[(Benchmark files)]
    Provider[External provider]
    PinnedSources[固定公共数据源]
    LocalCLI[Trusted-local CLI]
    ReportFiles[summary / groups / responses]

    Client -->|REST JSON| Routes
    Poller -->|GET Run、当前 Responses 页、progress index/变化 blocks| Routes
    Importer -->|受限读取| Files
    APIRepo --> DB
    Governance --> DB
    QueuePublisher -. XADD .-> Redis
    Redis -. XREADGROUP / XAUTOCLAIM .-> QueueConsumer
    Reconcile --> DB
    LeaseRepo --> DB
    AttemptLedger --> DB
    Runner --> DB
    Adapters -->|Mock: 无网络| Runner
    Adapters -->|OpenAI-compatible: HTTPS remote / HTTP loopback| Provider
    PinnedSources -->|固定 revision / SHA| LocalCLI
    LocalCLI -->|复用 Dataset、Run、Lease、Runner| DB
    LocalCLI -->|发现 / canary / generate| Provider
    LocalCLI --> ReportFiles
```

| 模块 | 单一职责 | 不承担的职责 |
| --- | --- | --- |
| API Routes / Schemas | HTTP 校验、分页、状态码和输出脱敏 | 评分和供应商协议细节 |
| Credential Crypto | 严格读取部署 keyring，以 AES-256-GCM 加解密并把 algorithm、Model ID、Provider origin 绑定为 AAD | ORM、HTTP、Provider 调用或秘密展示 |
| Application Services | 编排 CRUD、导入、创建/取消 Run；commit 后 best-effort 通知 | 执行 Adapter 或长时间阻塞请求 |
| Governance Repository / Audit | policy 版本、Run admission、四层 scope/minute bucket、逐 attempt reservation/settlement、typed audit 与历史聚合 | Provider 远端账单、远端 exactly-once 或公网身份认证 |
| Dataset Loader | 限制文件、解析、逐字段校验、稳定 Hash | 下载远程数据或执行数据集代码 |
| Standard Dataset Converters | 固定下载/缓存 MMLU-Pro、GPQA，校验源 Hash 并生成 dataset-v1 ZIP | 接受任意 URL、解释许可或执行题目代码 |
| Trusted-local CLI | prepare、Provider preflight/确认、创建/恢复 `legacy_unmanaged` Run、驱动 Runner、导出报告 | 提供公网 API、保存明文 Key、继承 Web/API governance 或多租户调度 |
| Provider Preflight | 推导 `/models`、拒绝 Key 反射、确定模型、执行最小可解析且返回模型一致的 Chat canary | 猜测多个付费目标或证明供应商完全兼容 |
| Report Exporter | 分页读取终态证据，从计划题与 Responses 派生唯一指标集并原子写出三文件 | 覆盖已有报告、充当访问控制或修改 Run |
| Redis queue | 提供低延迟、at-least-once 通知和 ACK/PEL | 保存权威状态、租约、取消或结果 |
| WorkerService | 数据库对账、消费/确认通知、优雅停机 | 改变评分协议或用 Redis 裁决状态 |
| RunLeaseRepository | 条件领取、心跳、fencing、幂等 Response、retry/cancel/dead-letter | Provider 网络请求 |
| EvaluationRunner | 在有效租约下调度逐题执行、跳过已有 Response、聚合 | 解析特定答案格式 |
| ModelAdapter | 把统一生成请求映射到具体模型 | 评分 |
| Evaluator | 安全解析答案并给出 0/1 分 | 调用模型或数据库 |
| Repository / ORM | 事务与持久化 | API 序列化和业务展示 |
| React UI | 用户操作、write-only Key 表单、独立轮询、虚拟化可访问热力图和可视化 | 持久化/读回 Key、读取 keyring、直接调用模型供应商或从部分 cells 重算权威指标 |

## 关键数据流

### 导入 Benchmark

1. API 接收受限的 Benchmark 目录或上传内容；任何路径都必须解析到允许的导入根目录内。
2. Loader 先检查文件名、大小、UTF-8 和 JSON 语法，再按照 [`DATASET_FORMAT.md`](./DATASET_FORMAT.md) 校验 Schema 与跨字段约束。
3. Loader 验证问题 ID 唯一、`question_count` 一致，以及题型与 Evaluator 映射兼容。
4. Loader 对规范化 manifest 和按原顺序排列的规范化题目计算 SHA-256。
5. Benchmark 与 Questions 在一个数据库事务中写入；任一题失败则整体回滚。
6. 相同 `id + version + hash` 可按幂等导入处理；相同 `id + version` 但 Hash 不同必须报冲突，不能静默覆盖。

标准数据转换是上述导入之前的受限供应链步骤：代码中固定 MMLU-Pro test/validation 或 GPQA archive 的 HTTPS URL、revision、大小和 SHA-256；缓存命中也重新校验。转换 profile、group/limit、GPQA seed 与转换器版本形成稳定版本指纹，生成的 ZIP 再完整经过普通 Loader。manifest 的 `source` 只是已验证事实说明，普通 Importer 仍不会跟随它联网。

### 注册与更新 Provider 凭据

Model 的凭据来源是显式状态，而不是从 nullable 字段猜测：`mock` 使用 `none`；旧客户端/可信 CLI 的 `api_key_env` 使用 `environment`；Web/API write-only `api_key` 使用 `stored`。

1. 浏览器只在 Model create/PATCH 请求体的 `api_key` 字段提交明文；Pydantic 用 `SecretStr` 接收并校验 8–8192 bytes、可见 ASCII 和无首尾空白，422 响应不保留原始 `input`/`ctx`。
2. API 从部署 keyring 选择 active 32-byte key，以 AES-256-GCM 和随机 12-byte nonce 加密。AAD 固定包含算法、Model ID 和规范化 Provider origin（scheme、host、非默认 port；不含路径），因此 envelope 不能跨 Model 或 origin 转发。
3. `model_credentials` 以 `model_id` 同时作为主键和到 `models.id` 的级联外键，保存 `algorithm/key_id/nonce/ciphertext` 与时间戳；没有 plaintext 列。公开 Model 只返回非秘密凭据状态和 legacy environment 变量名称，不会序列化该关系。
4. create/PATCH 在加密前把新 Key 与精确 `ModelRead` 全字段投影及 Run snapshot 的 `model` 子投影比较；PATCH 保留 stored row 时只为同一 fail-closed 比较解密旧 Key。省略 `api_key` 时保留 row；替换 Key 时用新 nonce 重加密。规范化 origin 改变必须重输 Key；切换到 `mock` 或 `environment` 删除 encrypted row。该边界防止凭据流复制，不扫描无关 Benchmark/Question 内容，也不承诺排除独立的字面巧合。
5. stored row 缺失或现有 envelope 因未知/旧 `key_id`、损坏密文而不可读时，可在 active keyring 可用时通过隔离的凭据 PATCH 用显式新 Key 覆盖，或只切换 `mock`/legacy environment 清理；夹带无关公开字段变化返回 422，没有新 Key 却保留 `stored` 返回 503，两者都不修改事务。
6. `pending/running` Run 存在时，Provider 类型、endpoint、远端模型和 credential 的敏感更新返回 409。Run 创建与 Model 更新共用方言锁：PostgreSQL 对 Model 行执行 `SELECT ... FOR UPDATE`，SQLite 在读 Model 前执行 `BEGIN IMMEDIATE`，把 Model snapshot 创建与修改串行化。SQLite 的数据库级竞争可能短暂阻塞请求，因此只适合低并发本地模式；生产或并发评测推荐 PostgreSQL。AES-GCM 的 origin AAD 是竞态或数据库篡改后的最后认证边界。

每次凭据创建、替换、删除/来源切换、拒绝或解密失败还会以数据库 UTC 写入 security-retention typed audit：payload 只包含安全 Model/key 标识、来源、动作和稳定原因，不包含 Key、Authorization、Provider origin 或 envelope。拒绝事件可在失败业务事务回滚后另行持久化；这是可重试、event-key 幂等的应用级审计，不声称跨进程 exactly-once、WORM 或管理员不可篡改。

部署 keyring 是 API 与 Worker 共同读取、但 frontend/migrate 不需要的独立秘密。API 仅用于加密，Worker 仅在 stored Run 执行前解密；keyring 缺失、格式错误、未知 key id 或认证失败均 fail closed，并记录上述最小化 audit。环境变量来源与可信本地 CLI 保持原行为，不会被隐式迁移成 stored credential。

### Policy 激活与 Run admission

Governance policy 是不可变、内容寻址且只有一个 active version 的数据库事实。初始化前 `GET /api/v1/governance/policy` 是纯读取，返回 `404 governance_policy_not_initialized`，不会借查询偷偷 bootstrap。`PUT /api/v1/governance/policy` 是 full-document apply：所有字段都必填，不具备 PATCH 语义；相同内容幂等返回现有版本，重新提交历史内容会重新激活原 ID/version 并追加 activation audit。`policy_hash` 覆盖全部 20 个规范化限制字段，读取和每次 attempt admission 都重算校验。managed Run 创建会在没有 policy 时原子 bootstrap 确定性默认版本，然后在 global admission 锁内检查有限 backlog，把全量 policy、ID/hash、opaque provider scope、`question_quantum` 与恰好四个 Run override（input reservation 及 request/Token/USD lifetime budget）冻结到 execution snapshot；后续 policy 变更不追溯改写已提交 Run。外发前还会比较冻结 override 与 Run 列，防止任一一侧被篡改后绕过硬边界。

policy 的 `null` 表示关闭该维限制，`0` 表示立即拒绝新 admission。四层 global/provider/model/run 限额全部满足才允许一次 attempt；hard TPM/Token budget 必须同时有显式 `input_token_reservation` 与有限 `max_tokens`，hard cost budget 还必须有冻结 USD 单价，否则在任何 Provider 外发前分别以稳定治理错误 fail closed。未显式提供 input reservation 时，Runner 不把 UTF-8/tokenizer 估算写入 `reserved_input_tokens`，也不据此生成 reserved cost；Provider actual input 仍按原值保存。input/cost overdraw 只由显式 Run input reservation 派生，显式 `max_tokens` 形成的 output reservation 则独立生效。治理 USD 公开上限为 `10000000.00000000`，API 用 JSON string 返回 Decimal；PostgreSQL 使用精确 `NUMERIC(20,8)`，该更低的公开上限使 SQLite IEEE-754 相邻间距低于半个 `1e-8` 存储量化单位，保留兼容路径的 8 位往返。完整字段与状态码见 [`API.md`](./API.md)，操作与恢复见 [`OPERATIONS.md`](./OPERATIONS.md)，修正依据见 [`ADR-0018`](./decisions/ADR-0018-observational-token-estimates-are-not-hard-reservations.md)。

### 创建并执行 Run

```mermaid
sequenceDiagram
    actor U as 用户
    participant W as React UI
    participant A as FastAPI
    participant D as Database
    participant Q as Redis Streams
    participant K as Worker
    participant R as Lease + Runner
    participant C as Deployment Keyring
    participant M as ModelAdapter
    participant E as Evaluator

    U->>W: 选择模型、Benchmark 与生成参数
    W->>A: POST /api/v1/runs
    A->>D: 锁 Model/global admission；校验 backlog，冻结 policy 与完整快照
    A->>D: 写入 managed pending Run + run_admitted audit
    D-->>A: COMMIT
    A-->>Q: best-effort XADD(run_id, correlation_id)
    A-->>W: 202 + Run ID
    K->>D: 扫描 due pending / expired running
    Q-->>K: XREADGROUP / XAUTOCLAIM 可重复通知
    K->>R: 尝试处理 run_id
    R->>D: 最久未获服务优先条件领取；attempt/token +1，写 owner/heartbeat/expiry
    R->>D: 读取 Run snapshot；stored 时按 run.model_id 读取 envelope
    opt credential_source = stored
        R->>C: 以 run.model_id + snapshot origin 认证解密
        C-->>R: 进程内 SecretStr；失败则不构造 Adapter/不联网
    end
    loop 租约有效
        R->>D: 按数据库时间续租
    end
    loop 每道计划题目
        R->>D: 读取 Question/取消意图，跳过已有 Response
        R->>M: generate(messages, config, request-local governance context)
        M->>D: reserve：四层 scope/bucket/ledger 原子 admission
        M->>D: mark send_started（成功后才允许外发）
        M->>M: 打开 Provider SSE，持续消费 token/心跳/usage 至 [DONE]
        alt 生成成功且非空
            M-->>R: ModelGenerationResult
            R->>E: evaluate(raw_response, reference, config)
            E-->>R: EvaluationResult
        else 请求、超时或空响应错误
            M-->>R: 分类后的错误
        end
        M->>D: actual 或 conservative settlement；明确未外发才 release
        R->>D: 校验 owner/token/expiry，幂等写 Response 并同步进度
    end
    alt 本 slice 新增 Response 达到 question quantum
        R->>D: 对账 attempt 后 cooperative yield 为 pending（不计失败）
    else 已完成或治理耗尽
        R->>D: 从持久 Response 聚合并写入终态
    end
    K-->>Q: 数据库处理结果落定后 XACK
    loop Run 未进入终态
        W->>A: GET /api/v1/runs/{id}
        A->>D: 查询最新状态
        A-->>W: 进度与指标
    end
    W->>A: GET /api/v1/runs/{id}/responses
    A-->>W: 分页逐题结果
```

创建接口不等待评测完成，也不在 API 进程内启动 Adapter。固定顺序是“数据库 COMMIT，再 XADD”：COMMIT 失败绝不通知；XADD 失败时保留可恢复的 `pending` Run、尝试写入 `last_error=queue_notification_unavailable`，仍返回 `202`。Worker 的数据库对账修复 commit/XADD 裂缝、Redis 丢消息或暂时不可用。

Run 没有 credential 外键或 envelope 列；`model_parameters_snapshot.model` 只冻结来源、Model ID、远端模型和 endpoint，environment 模式另存环境变量名称。stored 模式由 Worker 按 `run.model_id` 读取当前受保护的 `model_credentials` 行，并用 **Run snapshot 的 Base URL origin** 做 AAD，而不是读取当前 Model endpoint。active-run 更新锁保证正常操作期间 row 不会被替换；即使绕过业务层发生竞态或篡改，错误 Model/origin 的认证也会在任何 Provider 网络调用前失败。

Redis 通知和 ACK 都可重复，所以系统是 at-least-once；数据库中每题只保留一条 Response，并从 Response 事实重算进度和聚合。逐 HTTP attempt ledger 以 `(run, question, execution generation, provider attempt)` 唯一，状态只允许 `reserved → send_started → settled_actual|settled_conservative` 或可证明未外发时 `reserved → released_pre_send`；scope/bucket 聚合与状态转换同事务更新。明确 pre-send release 不删除旧 ledger，而是新建本地 ledger generation 并保留当前未发送 ordinal；已达到 `send_started` 的较小 ordinal 不会被重置。该修正见 [`ADR-0011`](./decisions/ADR-0011-confirmed-pre-send-release-retry-generation.md)。若 Worker 在 `send_started` 后崩溃，对账会保守结算并释放本地 permit，但无法取消仍在 Provider 运行的幽灵请求；若响应已返回而本地 Response 未提交，接管 Worker 仍可能再次调用。本架构只提供本地 admission 与幂等账本，不保证 Provider exactly-once、崩溃后的真实在途并发上界或账单真值。

在 reserve、send-start、settlement、lease renew/reconcile 的锁定边界，scope/minute 物化值都会从 never-delete ledger 重算并比较；高、低任一方向漂移都停止新变更，由 API/Worker 边界以独立短事务尽力记录固定 `governance_integrity_error`，不记录损坏值或异常文本。Run 终态/defer 等已提交转换的 post-commit reconcile 失败会保留已提交状态并向上报错；过期 lease takeover 在新 owner 提交后若旧 ledger 校验失败，会撤销新 lease、聚合已有 Response 并使 Run fail closed，阻止新 Worker 外发。

### 可信本地正式评测

正式 CLI 绕过浏览器/API/Redis 控制面，但不绕过数据、协议或持久化边界；当前创建的 Run 明确标为 `legacy_unmanaged`，不会隐式继承 managed Web/API 的 active governance policy：

1. `prepare` 下载固定源、校验并转换；不读取 Key 或连接 Provider。
2. `run` 强制选择 `--limit` 或 `--full`，从环境变量/隐藏输入取得 Key，使用兼容根路径调用 `GET /models`；远端只允许 HTTPS，HTTP 仅允许 loopback，发现请求只接受 identity 编码且正文上限 2 MiB，多模型时不猜测目标，任何模型 ID 反射当前 Key 都使预检失败。
3. CLI 输出 host、模型、题数、剩余 Run attempts 和最大 Chat HTTP 尝试数并要求确认；上界按 `(缺失题数 × 剩余 Run attempts + 1 个 canary) × 3` 包含 HTTP retries，再执行一个最小可解析、可能计费的 canary。若成功体明确返回其他模型名，canary 失败。
4. 通过 preflight 后才持久化 Benchmark、Model 和带脱敏 preflight/完整配置快照的 pending Run，并在同一进程用独立 lease owner 驱动现有 Runner。
5. Runner 先启动租约心跳，再通过工作线程加载和物化数据库快照，避免大题集同步加载阻塞事件循环；随后以固定 `min(concurrency, question_count)` 个消费者协程从迭代器取题，不为 12,032 题一次性创建 task。Response 仍逐题短事务、fencing 与幂等。
6. `resume` 重新确认/canary；若旧的未完成租约已经过期，本地 Runner 会执行 fenced reclaim，而不是等待已经停止的外部 Worker。恢复时跳过已有 Response，只执行缺失题。`report` 仅读取终态数据库事实，不接触 Provider。
7. Exporter 分页读取全部证据，以不可变计划题数和 Responses 重新派生 summary/groups 的同一指标集；`metrics_provenance` 标出数据库 Run 汇总字段漂移。三文件先写入权限收紧的临时目录并同步，再原子发布到一个不存在的目标目录；失败不留下伪装完成的报告。

这条路径要求先停止常规 API/Worker 并由 CLI 独占同一数据库，尤其 SQLite 只能有一个执行者。代码只能拒绝已有 `running` Run，不能探测空闲 Worker 并阻止它抢 `pending`。preflight、确认和限题降低误配置成本，但 `legacy_unmanaged` 不等于 Provider RPM/TPM、金额预算硬边界、远端 exactly-once 或真实账单核对。

## Run 生命周期与并发控制

```mermaid
stateDiagram-v2
    [*] --> pending: 创建并持久化
    pending --> running: Worker 取得有效租约
    pending --> cancelled: 取消请求
    pending --> failed: attempt 耗尽 / dead-letter
    running --> completed: 所有题已处理并完成聚合
    running --> cancelled: 有效 owner 或过期对账收敛取消
    running --> pending: 可重试 Run 级失败
    running --> running: 租约过期后新 token 接管
    running --> failed: attempt 耗尽 / dead-letter
    completed --> [*]
    failed --> [*]
    cancelled --> [*]
```

- 状态只允许沿上图迁移；终态不可重新领取且不保留 owner/expiry/heartbeat。
- 数据库当前时间是租约裁决标准。领取在同一条件更新/事务内令 `attempt_count` 和单调 `lease_token` 加 1，写入 owner/heartbeat/expiry；影响 0 行的 Worker 不得执行。
- 心跳只能续期尚未过期且 owner/token 匹配的租约。Response、进度、retry、取消、完成和失败写入都必须在事务内再次校验；旧 token 永久失效。
- 每个 Worker/本地 CLI 同时执行一个 Run；每个 Runner 用固定数量消费者将 Run 内题目并发限制为 1–4，默认为 1。managed Run 的每个 lease 最多新增 frozen `question_quantum` 条 Response，随后 fenced cooperative yield 为 `pending`；yield 延续同一 execution generation/Provider ordinal，不增加 `failed_attempt_count`。数据库按 `coalesce(last_scheduled_at, created_at)` 最老优先稳定领取，Redis 通知不能插队。
- global/provider/model/run 四层 scope 按固定顺序锁定并共同裁决 active permits、fixed-minute requests/tokens 与累计 request/Token/cost；backlog 满在 Run commit 前返回稳定 429，瞬时 rate/concurrency 饱和使 Run 延迟后再调度，确定性 lifetime budget/pricing 耗尽则聚合已有证据并进入治理失败。直连 CLI 的 `legacy_unmanaged` Run 不应用这些规则。
- 取消是协作式：pending 直接收敛；running 先持久化意图，有效 owner 在题目/心跳边界处理；Worker 已死则由过期对账收敛。已发出且无法撤销的上游请求可能继续至返回或超时。
- Run 级可重试失败以有限指数退避回到 pending；attempt 耗尽时先从持久化 Responses 聚合部分证据，再进入 `failed`，`dead_lettered_at` 是权威 dead-letter 证据。Redis dead-letter 通知即使存在也只能是派生信号。
- 如已持久 Response 数等于计划题数，Worker/对账直接从事实聚合 `completed`，不为补终态而多耗 attempt 或再调 Provider。
- 单题异常生成带 `error_type`/`error_message` 的 EvaluationResponse 并计 0 分，不改变 `llmbenchlab-protocol-v1` 的分母、完成率或已回答准确率含义。

## 模型适配器架构

```mermaid
classDiagram
    class ModelAdapter {
        <<interface>>
        +generate(messages, generation_config, attempt_context) ModelGenerationResult
        +aclose()
    }
    class MockModelAdapter {
        +generate(messages, generation_config) ModelGenerationResult
    }
    class OpenAICompatibleAdapter {
        +generate(messages, generation_config) ModelGenerationResult
    }
    class ModelGenerationResult {
        +text: string
        +input_tokens: integer or null
        +output_tokens: integer or null
        +latency_ms: number
        +provider_request_id: string or null
        +raw_usage: object or null
        +metadata: object
    }

    ModelAdapter <|.. MockModelAdapter
    ModelAdapter <|.. OpenAICompatibleAdapter
    ModelAdapter --> ModelGenerationResult
```

统一语义为：

```text
generate(messages, generation_config, *, attempt_context=None) -> ModelGenerationResult
```

`messages` 是已经应用 Run 快照 Prompt 的消息数组；`generation_config` 至少承载 `temperature`、`top_p`、`max_tokens` 和 `seed`。`max_tokens` 为 `1..131072` 的数字时由 Adapter 原样发送，为 `null` 时不发送该字段并由 Provider 选择默认预算；这不等于无限输出。Prompt 渲染只在 manifest `system` 含非空白内容时插入 system message；空 system 被省略。当前 GPQA-Diamond 固定 `zero-shot-cot-answer-line-v1`，要求模型推理后在末行输出 `Answer: X`；MMLU-Pro 与 GPQA 的标准 system 都为空。上述 profile/省略规则属于模型输入身份，必须随模板与 Dataset Hash 一起比较。

`attempt_context` 是 request-local、只含 run/question/model/provider opaque scope、lease token、execution generation、attempt ordinal 与数值预留的非秘密对象；可选 controller 在 Adapter 内围住每一个真实 HTTP retry，保证一次 `generate()` 的多个 attempt 不会被漏记。controller 不接触 Key、header、Prompt 或响应正文。

Adapter Registry 根据 `provider_type` 选择实现：

- `mock`：使用输入中稳定标识或 Demo 题目映射产生可预测回答，不读取密钥且不得发起网络请求；latency、Token、usage shape 与模拟错误等全部确定性本地配置先验证，非法配置不会调用 reserve/mark/finish hook，成功与模拟 Provider 错误仍走同一三阶段治理语义。
- `openai_compatible`：校验 `base_url` 和 `remote_model_name`，并要求恰好一种凭据输入：environment Run 在调用前按 `api_key_env` 读取，stored Run 由 Worker 传入已解密的 `SecretStr`。Adapter 不读取数据库或 keyring。远端 URL 必须为 HTTPS，HTTP 仅允许 loopback；请求声明 `Accept-Encoding: identity` 并拒绝压缩响应。Chat payload 显式发送 `stream:true` 与 `stream_options.include_usage:true`，request-local parser 支持任意字节/UTF-8 分块、SSE comment 心跳、多 `data` 行、role/null delta、usage-only 尾块和 `[DONE]`，并只聚合 `delta.content`；推理扩展字段不混入评测答案。看到 finish reason 不会提前返回，HTTP 干净 EOF 却缺少 `[DONE]` 不作成功；忽略 stream 请求而返回普通 JSON 的 Provider 继续兼容。普通 JSON 成功体上限 4 MiB，SSE wire/单事件/聚合 content 上限分别为 64 MiB/1 MiB/4 MiB，非 2xx 错误体上限 64 KiB。对 429、部分 5xx 和暂时性 transport 错误仍执行快照内有上限的指数退避；明显的 4xx 配置错误不重试。

Adapter 将供应商错误映射为稳定的内部分类，例如 `authentication_error`、`rate_limited`、`provider_4xx`、`provider_5xx`、`connect_timeout`、`read_timeout`、`network_error`、`invalid_provider_stream`、`provider_stream_error`、`incomplete_provider_stream`、`empty_response` 和 `output_truncated`。Provider 以 `finish_reason="length"` 返回空内容时，Adapter 直接产生 `output_truncated`；返回非空内容但 Evaluator 无法解析有效最终答案时，Runner 根据同一 finish reason 把普通 parse error 提升为 `output_truncated`，并保留已有 raw response、usage、延迟与成本证据。最终 `httpx.TransportError` 转成安全 `AdapterError` 时不保留可达 request/Authorization 的 `__cause__` 或 `__context__`。日志和持久化错误不得包含 Authorization、密钥值、原始 SSE 行/事件或完整敏感响应头。SSE content 先聚合再执行当前 Key 的精确替换，避免 Key 横跨 delta 时泄漏。Provider 返回证据会递归检查成功内容、raw usage 的对象键和 JSON 标量，以及 provider request ID、返回模型名、system fingerprint 和 finish reason；其中出现的当前 Key 会先做精确替换。这不是对任意敏感内容的通用 DLP，也不扫描与 Provider 响应无关的固定数据。

## Evaluator 架构

```mermaid
classDiagram
    class Evaluator {
        <<interface>>
        +evaluate(raw_response, reference_answer, config) EvaluationResult
    }
    class ExactMatchEvaluator
    class MultipleChoiceEvaluator
    class NumericEvaluator
    class EvaluationResult {
        +parsed_answer: string or number or null
        +score: 0 or 1
        +correct: boolean
        +evaluator_name: string
        +metadata: object
        +parse_error: string or null
    }

    Evaluator <|.. ExactMatchEvaluator
    Evaluator <|.. MultipleChoiceEvaluator
    Evaluator <|.. NumericEvaluator
    Evaluator --> EvaluationResult
```

Evaluator Registry 由题型和 manifest 中的版本化映射选择实现。Evaluator 必须是确定性的纯业务逻辑，不访问网络和数据库：

- Exact Match 只做已声明的空白、换行和大小写规范化，不做语义模糊匹配。
- Multiple Choice 优先解析明确的最终答案表达；多个冲突候选必须返回 `parse_error`，不得猜测。
- Numeric 使用安全十进制/浮点解析和绝对、相对误差，不使用 `eval`，拒绝 NaN 与 Infinity。

原始回答、解析答案和标准答案快照分别保存。解析失败的 `score` 为 0，并记录 `parse_error`；详细规则见 [`BENCHMARK_PROTOCOL.md`](./BENCHMARK_PROTOCOL.md)。

## 数据持久化

```mermaid
erDiagram
    MODEL ||--o{ EVALUATION_RUN : evaluates
    MODEL ||--o| MODEL_CREDENTIAL : owns
    BENCHMARK ||--|{ QUESTION : contains
    BENCHMARK ||--o{ EVALUATION_RUN : selected_for
    GOVERNANCE_POLICY ||--o{ EVALUATION_RUN : freezes
    GOVERNANCE_POLICY ||--o{ GOVERNANCE_MINUTE_BUCKET : configures
    GOVERNANCE_SCOPE ||--o{ GOVERNANCE_MINUTE_BUCKET : materializes
    EVALUATION_RUN ||--o{ EVALUATION_RESPONSE : produces
    EVALUATION_RUN ||--o{ QUESTION_EXECUTION : schedules
    QUESTION ||--o{ QUESTION_EXECUTION : executes
    QUESTION_EXECUTION ||--o{ PROVIDER_CALL_RESERVATION : attempts
    PROVIDER_CALL_RESERVATION ||--o{ AUDIT_EVENT : records
    QUESTION ||--o{ EVALUATION_RESPONSE : answered_by

    MODEL {
        string id PK
        string provider_type
        string credential_source
        string api_key_env
        json default_parameters
    }
    MODEL_CREDENTIAL {
        string model_id PK, FK
        string algorithm
        string key_id
        bytes nonce
        bytes ciphertext
    }
    BENCHMARK {
        string id PK
        string version
        string dataset_hash
        integer question_count
    }
    QUESTION {
        string id PK
        string benchmark_id FK
        string external_id
        string question_type
        json evaluator_config
    }
    GOVERNANCE_POLICY {
        string id PK
        integer version
        string policy_hash
        boolean is_active
        integer backlog_limit
        integer question_quantum
    }
    GOVERNANCE_SCOPE {
        string id PK
        string scope_type
        string scope_key
        integer active_reservations
        integer consumed_requests
        decimal consumed_cost_usd
    }
    GOVERNANCE_MINUTE_BUCKET {
        string id PK
        string scope_id FK
        string policy_id FK
        datetime window_start
        integer consumed_requests
        integer consumed_input_tokens
        integer consumed_output_tokens
    }
    EVALUATION_RUN {
        string id PK
        string model_id FK
        string benchmark_id FK
        string status
        string protocol_version
        string benchmark_hash_snapshot
        json model_parameters_snapshot
        integer attempt_count
        integer max_attempts
        integer failed_attempt_count
        integer dispatch_count
        string governance_policy_id FK
        string governance_status
        string governance_reason
        string lease_owner
        integer lease_token
        datetime lease_expires_at
        datetime heartbeat_at
        datetime next_attempt_at
        datetime last_enqueued_at
        string last_error
        datetime dead_lettered_at
    }
    EVALUATION_RESPONSE {
        string id PK
        string run_id FK
        string question_id FK
        text raw_response
        string parsed_answer
        number score
        string provider_request_id
        string returned_model
        string system_fingerprint
        string finish_reason
        integer http_attempt_count
        string error_type
    }
    QUESTION_EXECUTION {
        string id PK
        string run_id FK
        string question_id FK
        integer execution_generation
        integer next_provider_attempt
    }
    PROVIDER_CALL_RESERVATION {
        string id PK
        string policy_id FK
        string question_execution_id FK
        integer provider_attempt
        string state
        integer reserved_input_tokens
        integer reserved_output_tokens
        integer actual_input_tokens
        integer actual_output_tokens
        decimal reserved_cost_usd
        decimal actual_cost_usd
    }
    AUDIT_EVENT {
        string id PK
        string event_key
        string event_type
        string payload_hash
        string retention_class
        datetime occurred_at
        datetime expires_at
    }
```

SQLAlchemy ORM 模型与 Pydantic API Schema 分离。Alembic 是唯一受支持的 Schema 演进入口；应用启动只校验数据库已到达 Alembic head，不会隐式建表。Compose 只允许一次性 `migrate` service 执行 migration，API 和 Worker 不并发抢占 schema owner。隔离测试可用 metadata 创建临时表，但必须显式标记对应 revision，不能成为运行路径。

PostgreSQL 是共享部署目标，并提供真实多 Worker 条件领取、行锁和数据库时间语义。SQLite 继续使用同一 ORM/Alembic head，仅支持单 Worker 本地开发、离线 Smoke 和兼容测试，不声称多 Worker 安全或生产能力。

每个 Run 至少快照：

- Benchmark ID、版本、Dataset SHA-256；
- Evaluator 名称、版本和数据集级配置；逐题配置属于按 Hash 锁定且无更新 API 的不可变 Question 记录；
- Prompt template、system prompt；
- temperature、top_p、max_tokens（数字或 `null`）、seed；
- 展示模型名、远端模型名、adapter 类型、Base URL、`credential_source`、价格和有效模型参数；environment 模式另含密钥环境变量名，stored 模式不含 Key/envelope/reference；
- Git commit SHA（无法读取时为 `null`）；
- 并发度、连接/读取/写入/连接池超时和重试策略；其中读取超时由 Run 请求在 `1..1800` 秒内选择，省略时为 `60` 秒；
- managed Run 的完整 20-field governance policy、ID/hash、opaque provider scope、question quantum，以及恰好四个 Run override：显式 input Token reservation 与 lifetime request/Token/USD budget；`legacy_unmanaged` 不伪造这些字段；
- `protocol_version`、创建时间、开始时间和结束时间；
- 可信本地正式 Run 的初次模型发现/canary 脱敏结果：是否发现、候选数/request ID，以及返回模型、system fingerprint、finish reason、usage、延迟和尝试次数；不含 Key/header。`resume` 的新 canary 当前不会追加为独立审计事件。

Runner 从 Run 快照读取模型连接配置、凭据来源、价格、生成参数、Prompt、并发、超时和重试策略，不回读可编辑 Model 的这些值。environment 模式按快照变量名取值；stored 模式仅按 `run.model_id` 读取 envelope，并用快照 Base URL origin 认证解密。题目内容与逐题 Evaluator 配置通过不可变 Benchmark 记录和 `benchmark_hash_snapshot` 绑定；Phase 1 不提供 Benchmark/Question 更新或删除 API。

每条 EvaluationResponse 除 raw/parsed/reference/score/error/usage 外，可保存经过长度、字符与当前 Key 精确反射检查的 `provider_request_id`、`returned_model`、`system_fingerprint`、`finish_reason` 和 `http_attempt_count`；任一不安全字符串归一化为 `null`，报告只导出这些 typed 字段，不持久化任意 raw usage object。

Model 的 `default_parameters` 在 Phase 1 只接受 Adapter 实际转发的 `temperature`、`top_p`、`max_tokens`、`seed`。创建 Run 时显式字段覆盖 Model 默认，省略字段才使用 Model 默认；显式 `max_tokens:null` 也是覆盖值，不会回退。`generation` 块因此只包含实际执行值，不把未转发的 Provider 扩展伪装成有效参数。通用 API 保留 protocol-v1 的 `max_tokens=256` 和读取超时 `60s` 默认；没有对应 Model 默认且用户尚未手动修改时，Web 根据已知 Benchmark 提交可编辑的显式建议：Demo `256/60s`、MMLU-Pro direct `1024/180s`、official CoT `4000/300s`、GPQA-Diamond `8192/600s`。

React 主导航包含独立的 Runs 列表页。该页通过 `GET /runs` 以 20 条为一页显示所有状态，支持状态筛选，并在当前页存在 active Run 时轮询；列表和 Run Detail 都以持久化 Run ID 建立链接。Run Detail 对 `GET /runs/{id}/responses` 使用每页 100 条的 offset 分页，而不是只加载大型正式 Benchmark 的前 100 条；详情页页码与全 Run 进度读取相互独立。Run Detail 还显式展示 `managed/delayed/exhausted/legacy_unmanaged` 治理状态，只将封闭的稳定 reason 映射为人类可读文案，`governance_not_before` 以 UTC 显示，未知值不原样反射。

全 Run 热力图采用固定 512 题 absolute-position blocks，而不是 Response offset 页或时间 cursor。`GET /runs/{id}/progress` 在一个数据库读取快照中返回全部计划 block 的 `response_count` 与 evidence-derived live metrics；`GET /runs/{id}/progress/blocks/{block_index}` 只返回该范围内按 position 排序的已持久化 cell 白名单。Response 对 Run/Question 唯一且追加，因而 block count 单调：客户端每秒只比较 index，hydrate 非空或 count 变化的 block；index→block 之间的新提交可使 payload 比旧 index 更新，但不会永久漏失，下一 index 会收敛。没有 Response 的计划 position 才是 `not_run`，非空 block 初始 hydrate 完成前为“同步中”。页面切 Run、旧请求返回、hidden/visible 和终态先到都由独立 progress reducer/poller 处理；终态需等目标 counts 追齐后才停止该 poller。

四态 outcome 的后端优先级为 `error_type != null -> error`、否则 `score == 1 -> passed`、否则 `wrong`；未持久化为 `not_run`。index 的 score/completion/answered accuracy/error/latency 与 known Token/cost coverage 复用 protocol-v1 聚合定义，前端不从部分 block Map 重算。known subtotal 只是覆盖证据，不能写回或替代 Run `input_tokens/output_tokens/estimated_cost` 的 all-or-nothing nullable 真值。固定白名单排除 Question/Response ID、正文/答案、error message 与 Provider transport metadata；两个 progress 响应均为 `no-store`。该设计复用现有唯一约束和 `Question.position`，不增加表、revision 或 migration。

写入约束：

- 时间以 UTC 存储，API 使用带 `Z` 或显式偏移的 ISO 8601；跨进程排序相关的 Run `created_at`/`finished_at`、governance window/lease 与 credential/lifecycle audit 都取数据库 UTC，不依赖 Worker/API 主机墙钟。
- JSON 字段只保存可序列化值；plaintext Key、Authorization 和未脱敏上游头禁止写入。`model_credentials` 只允许受约束的 AES-GCM envelope，SQL engine 隐藏 bound parameters。
- `(benchmark_id, external_id)` 唯一；一个 Run 对同一 Question 最多一条 EvaluationResponse。
- 导入 Benchmark 使用整体事务；逐题结果和进度使用短事务，避免把网络请求包在数据库事务中。
- 只有 owner/token 匹配、租约未过期的 Worker 能写 Response、进度、费用、retry 或终态；唯一约束是最后的竞态防线，不替代 fencing。
- attempt ledger never-delete 且状态单向；四层 scope/minute materialization 必须能由 ledger 重算，检测到高或低漂移时 fail closed 并由对外边界尽力写固定、无损坏值的完整性 audit，不静默修补后继续外发。历史 managed ledger 的 input/cost overdraw 重算通过 `evaluation_runs.input_token_reservation` 判断预留是否显式；没有关联 Run 的内部 synthetic reservation 仍把调用者提供的值视为显式。
- audit event 的 `event_key` 唯一，同 key 重放必须匹配 event type 与 payload hash；operational/security 默认分别至少保留 90/365 天。append-only 是应用约束，不是密码学 WORM 或数据库管理员防篡改。
- 聚合从已持久化的 Responses 计算，并在 completed、cancelled 或 dead-letter 终态更新前写回 Run。报告仍防御性地从计划题与 Responses 重算唯一主指标，并只用 `metrics_provenance` 标注 Run 字段漂移，保证 summary/groups/responses 不互相矛盾。

### SQLite 到 PostgreSQL 的显式导入

数据库平台迁移是 stopped-source/offline-empty-target 操作，不是双写或在线复制：

1. 停止 SQLite 源的 API/Worker 和新 Run 创建，确认无 `pending/running` Run。导入器使用 read-only URI，校验 integrity、foreign keys 和 Alembic head，不修改源文件。
2. PostgreSQL 目标必须已在 head，13 张核心/治理表必须为空且不可对外服务；源库不得含 active reservation 或按同一 DB UTC cutoff 仍 live 的 Worker generation，并必须在连接目标前用全部 reservation ledger 重算每个 scope/minute 物化值，任何高、低漂移或缺 bucket 都拒绝导入。事务级 advisory lock 在空库检查之前串行化竞争导入，随后对 `alembic_version` 和核心表取 `ACCESS EXCLUSIVE` 锁。
3. `governance_policies → models → model_credentials → benchmarks → questions → governance_scopes → evaluation_runs → evaluation_responses → governance_minute_buckets → question_executions → provider_call_reservations → audit_events → worker_processes` 在一个目标事务中按依赖顺序复制，包括 encrypted credential envelope、ledger/audit、stopped/stale Worker facts 与 typed Provider metadata，但不解密或输出秘密列值。源、precommit target 都只输出 row count、主键集 digest 和 canonical row digest；任一 precommit 失配/复制失败都整体 rollback，CLI 退出 `2`。
4. COMMIT 确认后，工具在独立的只读 `REPEATABLE READ` 事务中取稳定 postcommit snapshot，再对账并输出第三组摘要。全部完成才退出 `0`。

带凭据的目标 DSN 必须通过 `--target-env` 从受控环境读取；`--target` 拒绝 URL password 和 password query。COMMIT 未获得 PostgreSQL 确认时退出 `4`/`commit_outcome_unknown`；由于事务原子性，目标可能为空，也可能是完整的 precommit 快照。COMMIT 已确认但连接收尾、postcommit 快照/对账或报告失败时退出 `3`/`committed_but_verification_failed`；这时目标已提交完整 precommit 快照，不会自动回滚。两种结果都禁止盲目重试，必须保持目标离线，按已输出摘要独立检查目标是空还是完整提交。非空目标会拒绝再次导入，工具也不提供 PostgreSQL 到 SQLite 的反向同步。

Alembic `20260827_0004` 引入治理/审计表与 Run/Response 字段；`20260828_0005` 增加 `worker_processes` 与 audit retention/exporter 扫描索引；schema-equivalent `20260829_0006` 只条件补齐早期 `0004` 变体缺少的三个 canonical 索引；当前 data-only head `20260830_0007` 不改 schema、ledger 或 actual usage，只按显式 hard reservation 语义重算 `governance_scopes.overdrawn`。`0007` upgrade/downgrade 均在任何更新前拒绝 `reserved/send_started` active reservation；downgrade 只恢复旧派生谓词。兼容 preflight 仍以精确 fingerprint、integrity/FK、索引定义和 single-active 数据约束 fail closed。`0006 → 0005` 不删除 canonical 对象；后续两个 downgrade guard 都在第一条有损 DDL 前拒绝可能丢失的事实：0005 拒绝任意 Worker generation，0004 拒绝 policy/scope/bucket/question-execution/ledger/audit 或新 Run/Response 证据。隔离空数据库分别验证 `0005 ↔ 0004` 与 `0004 ↔ 0003`；已使用环境应优先向前修复，或恢复经核验的旧备份并单独保留新 schema 证据。完整流程见 [`OPERATIONS.md`](./OPERATIONS.md)。

## 错误处理与可观察性

| 层级 | 示例 | 行为 |
| --- | --- | --- |
| 请求校验 | 非法 provider 参数、未知 ID | 返回 4xx 与字段级可读错误，不创建副作用 |
| 数据集校验 | JSONL 第 8 行无效、重复题号 | 返回文件、行号、JSON Pointer、错误码与原因；事务回滚 |
| 单题生成 | 超时、429、空回答、非法/不完整 SSE | 暂时性 transport 有限重试；最终失败或协议错误保存错误 Response，计 0 分，继续下一题 |
| 治理瞬时背压 | 四层 concurrency/RPM/TPM 暂满 | 不生成 0 分 Response；drain 已启动题，fenced defer/yield，按 DB 时间到期后公平再调度 |
| 治理确定性耗尽 | lifetime budget、价格或 Token 上界缺失 | 在 Provider 外发前 fail closed，聚合已有证据并以稳定治理原因终止 Run |
| settlement unknown | 数据库 commit acknowledgement 不确定 | 停止当前 HTTP retry，不猜测释放或再次外发；由唯一 ledger key 与 reconciler actual/conservative CAS 裁决 |
| 单题解析 | 多选冲突、非法数值 | 保存原始回答和 `parse_error`，计 0 分，继续下一题 |
| Run 级故障 | Runner 未捕获异常 | 有效 owner 以有限退避重新 pending；attempt 耗尽则 failed/dead-letter |
| 通知故障 | XADD/read/ACK 超时或 Redis 停机 | 保留 DB 事实，记录脱敏状态，数据库 reconciliation 恢复 |
| 租约丢失 | heartbeat 失败、过期或 token 被接管 | 取消并等待本 Worker 在途题，拒绝所有旧 token 写入，不 ACK 不确定结果 |
| 进程重启 | API 或 Worker 中断 | API 不改写 Run；Worker 租约自然过期后由 peer/新 Worker 跳过已有 Response 接管 |

可观测基础包含：

- API 忽略客户端传入的 `X-Request-ID`，为每个请求始终生成全新的 server-side UUID 并在响应中回传。CORS 请求 header allowlist 不含 `X-Request-ID`，但会向浏览器暴露响应中的该 header，避免客户端把 write-only Key 复制到 correlation ID 后迫使服务反射或记录。新 Run 使用 run ID 作为稳定 correlation ID，通知、Worker、Runner 和 Question 事件继承该链路。
- LLMBenchLab 配置的已登记应用 logger 与进程内 Uvicorn/SQLAlchemy client logger 统一通过 JSON sanitizer；消息必须是 literal，structured extra 先按字段白名单，再按固定 enum、canonical UUID/Redis stream ID 和有限数值合同规范化，非法 ID 省略，未知字符串只投影固定 `unsupported`。Redis Run 通知在进入 Worker 前同样要求 Run/correlation canonical UUID；Uvicorn access log 默认关闭。Alembic migration CLI 使用独立 `fileConfig`/console 输出，不经过应用 sanitizer；该边界也不治理反向代理、PostgreSQL、Redis server、Docker daemon 或崩溃转储，所以秘密仍不得出现在 URL、query、path、argv 或迁移诊断。持久 typed audit 与 logger 分离，前者是应用 append-only、event-key 幂等且受 retention 管理的历史事实，但仍不是不可篡改/WORM 日志。
- `/live` 是纯进程存活检查，不访问 DB/Redis/Provider；`/health` 保持 DB-only 兼容语义；`/ready` 并行检查 DB 连接/Alembic head 与 Redis，不探测 Provider。Redis 失败时返回 `503/degraded`，但 DB/head 可用时 `accepting_runs=true` 且对账可用；DB/head 失败时 `not_ready` 且不接收任务。
- `/ready` 将同步 DB 探测放入 `asyncio.to_thread` 并限制 HTTP 等待时间。async timeout 不会取消已进入线程的驱动调用；后台资源的真正上界仍依赖数据库 driver/connect/pool timeout。
- `/tasks/metrics` 派生任务/governance 与匿名 Worker expected/registered/live/stalled/shortfall、最近聚合进展；`/tasks/history?window_hours=1..2160` 在一个 PostgreSQL `REPEATABLE READ READ ONLY`/显式 SQLite 读快照内逐条验证 retained audit 后聚合任务 counters 与 Run p50/p95/p99。`/metrics/prometheus` 复用同一 collector，在固定 15 分钟 audit/1 小时 latency window、hard row cap 和 per-process single-flight 下输出全部 gauge 的固定低基数 exposition。Redis availability 是快照外观察，不改变 DB 事实。损坏 audit 使 history/exporter 整体 fail closed，不返回部分结果；`/runs/{id}/audit` 仍按 `(occurred_at,id)` 稳定分页。
- 每个长运行 Worker 注册唯一 generation；scan/claim/progress/lease-heartbeat 只置固定 bit，由 event-loop recorder 每 5 秒最多一笔短事务用同一 DB UTC 合并。无事件零写，故 timer 不能在主循环卡死后制造 keepalive；crash generation 保持 active 后自然派生为 stale。JSON 公开 active generation 时间的 `MAX`，Prometheus oldest age 使用 `MIN`，两者都不输出 Worker/generation ID。dependency probe 明确 `main_loop_progress=not_checked`。
- 每题 Provider metadata 已安全归一化写入 Response/API/报告；凭据变更/拒绝/解密失败另写 security-retention audit。`llmbenchlab-audit-retention` 提供权限收紧的 canonical JSONL、完全离线 verify、精确 reconcile/restore/delete；archive 保存完整内部事实，hash 不是签名/WORM。仓库交付八条 Prometheus 规则与 Runbook，但不部署告警发送器、Dashboard、tracing 或管理员不可篡改存储。

## 部署拓扑与安全边界

本地 Make 模式启动 API、独立 Worker 和 Vite，默认 SQLite 且 Redis 可选；SQLite 只支持一个 Worker。`make setup` 与其他相关启动入口通过 `uv run --script` 显式选择满足 `>=3.11` 的独立 CPython，再由 bootstrap 为 Web credential 生成 Git 忽略、权限为 `0600` 的 `.secrets/credential-keys.json`，API 与 Worker 读取同一文件；这避免 `PATH` 中其他 Python 实现破坏安全原子安装语义，也不会为 Docker-only 入口同步宿主后端依赖。可信本地 CLI 是第三条运维入口：它直接复用当前数据库与 Runner，因此运行时必须停止连接同库的常规 API/Worker 并独占数据库，默认把下载、转换 ZIP 与报告放入 Git 忽略的 `artifacts/`。Compose 包含六个 service：长运行的 PostgreSQL、Redis、API、Worker、frontend，以及一次性 migrate；同一只读 Compose secret 只挂载到 API/Worker。PostgreSQL/Redis 各自使用 named volume，Redis 启用 AOF；API/frontend host port 明确绑定 loopback，DB/Redis 无 host port。CORS 只允许配置的前端 Origin。

当前 Compose 只是本地开发/故障验收拓扑，示例数据库密码不是生产秘密管理。前端 Nginx 位于浏览器→API 路径，不代表 Worker→Provider 路径上的 Cloudflare、Caddy 或其他 Gateway；真 SSE 必须在这条上游链路上保留正确 Content-Type 并持续 flush，且仍受每层独立的缓冲/超时限制。虽然远端 Provider 已强制 HTTPS、明文 HTTP 只允许 loopback，`base_url` 的允许范围仍未达到公网多租户要求；有效的 HTTPS URL 仍可能指向私网/云元数据或发生 DNS rebinding。本版本仅供受信任的本地操作者使用，不应直接暴露公网。后续公开部署必须增加鉴权、TLS、URL allowlist、DNS/IP 重绑定防护、出站网络策略、上传隔离、权限拆分、备份/PITR 和资源配额。当前不声称生产、HA 或灾备 SLA。

隔离容量入口 `make phase2-capacity` 以真实 PostgreSQL 16、Redis 7、1/2 Worker 和纯 Mock workload 比较吞吐/延迟，显式 PUT/读回全有限 policy，并要求并发 backlog 准确分裂为 `202/429`、`question_quantum<15` 产生 cooperative yield，以及低流量 Mock Model 在高流量 backlog 排空前获得 slice；另注入 lease、Redis、重复通知和 ledger/audit 对账场景。脱敏 JSON 记录环境、数据、并发、p50/p95/p99、错误/重试及 DB/queue 压力。capacity child 会从 never-delete Provider attempt ledger 独立重算 scope 与 UTC-minute projection，不能用物化 counter 自证无漂移。

正式 wrapper `make phase2-slo` 在 capacity child 之上实现 [ADR-0012](decisions/ADR-0012-single-host-slo-capacity-qualification.md) 并按 [ADR-0014](decisions/ADR-0014-dual-backlog-slo-profile.md) 固定为 `P2-local-control-plane-v2`。它只接受 exact clean commit，固定单主机的 PostgreSQL 16、Redis 7、一个 API、两个 Worker、pool/overflow `5/5`、retry `max/base/cap=3/1/30s` 和 `lease/heartbeat/poll=30/10/1s`，串行保留 1 次 warm-up 与恰好 5 次 measured trial。每个 child 固定四个 measurement：seed-balanced 的单/双 Worker baseline，随后是 warmed pause backlog 和 cold stop/start backlog；两个 burst 都是 AND 门禁，均要求 `4×202 + 2×typed 429`、恰好两个经容器事实验证的 distinct claim Worker、submission→terminal 吞吐 wall 和 restore 调用前→terminal drain。wrapper 再次验证每轮拓扑、Settings read-back、环境/数据/配置指纹、22 Run/330 Response/330 QuestionExecution/331 reservation 对账、fault/fairness、durable identity/timing、ledger projection 和容器/volume/network/唯一项目镜像 cleanup；吞吐按 `completed_questions / wall_duration_seconds` 重算，双/单 Worker scale 以同 trial 配对，避免信任 child 汇总或跨轮挑样。

v2 aggregate schema 为 `llmbenchlab-phase2-slo-evidence-v2`。aggregate 与 raw child evidence 都位于 Git 忽略目录；aggregate 只含 commit/source hash、稳定指纹、child 相对路径/hash、匿名参与计数、脱敏统计/判定、容量模型和 cleanup 摘要的 allowlist，不复制 raw identity、stdout/log、DSN/URL、环境变量、题目、Prompt/Response 或秘密材料。公开状态可以给出 Git 忽略的外层 aggregate 相对路径和内容 SHA；raw child、aggregate 内嵌 child 路径与环境/配置明细仍是内部证据，只摘录人工复核的 commit 与匿名结果。child 使用独立进程组，超时/中断后给 scoped cleanup 420 秒。

历史 v1 在 clean `dfa67abb1a9a0418a7e3337c179f816e3c69f121` 上为 15/18，保持 `unqualified`。v2 已在 clean `b6a35fef1dd069ebb54b69955058915c722aa34d` 上从零完成 1+5，23/23 项 SLO、每轮 hard invariant 和 cleanup 全部通过，aggregate 内容 SHA-256 为 `a76d167bb664e2ee3ee7514c39ac738b76cef37776d7b66e1175a8596329d0d9`；同一实现 SHA 的 [GitHub Actions run 33146681285](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33146681285) 4/4 必需 job 成功。该架构把正确性硬门禁、统计资格和证据最小化分层；结果只描述被记录的固定单机 Mock 硬件、配置和 commit，不是 Provider 性能、生产 SLO/SLA、HA、无限扩展或 Phase 2 整体完成证明。GitHub-hosted CI 只验证 validator、统计和失败路径，不承担绝对性能门禁。基线解释见 [`PERFORMANCE.md`](./PERFORMANCE.md)，扩缩容、预算/背压、settlement unknown、DB/Redis/lease 恢复与 0005/0004 安全回滚见 [`OPERATIONS.md`](./OPERATIONS.md)。

## 当前限制

- PostgreSQL 租约已支持受限多 Worker 协调；SQLite 仍只适合单 Worker、单机低并发。这不是无限水平扩展或 HA 保证。
- managed Web/API Run 已实现数据库权威四层限流/预算/背压与公平调度；默认 policy 可关闭限制，`legacy_unmanaged` CLI 不覆盖，当前 global scope 锁会串行化新 admission，Mock 容量基线也不是生产调优结论。
- typed audit/history/archive、低基数 exporter、八条规则、Worker DB-time progress 与逐题 Provider metadata 已实现，但 append-only/普通文件 hash 不是 WORM；尚无告警发送器、tracing/认证监控面板、`resume` canary 独立事件或公网对象级访问控制。
- at-least-once 不能防止 `send_started` 后崩溃留下 Provider 幽灵请求，或 Provider 响应到本地 COMMIT 之间的重复远程调用/费用；本地 ledger 幂等和保守结算都不是远端 exactly-once/账单真值。
- 上游是否真正遵守 `seed`、temperature 等参数由供应商决定；同配置不保证逐 token 完全确定。
- 仅支持客观的 exact match、multiple choice、numeric；不执行代码，不提供 LLM Judge、Arena、Agent 或长上下文专用协议。
- Benchmark 可来自本地受信任操作者，或两个固定公共数据转换器；尚无通用远程注册表、发布者签名、隔离解压或恶意内容扫描。
- MMLU-Pro `direct`/`official_cot`、group/limit、GPQA shuffle seed 或 Dataset Hash 不同的结果不可直接比较；公共题污染与 Provider 同名模型滚动更新仍无法由平台消除。
- 单用户、无鉴权、无配额；只能在可信本地环境或受保护网络使用。
- Web credential 使用本地文件 keyring，而不是集中 secrets manager/HSM；keyring 丢失会使 stored rows 不可恢复，数据库与 keyring 同时泄漏则可解密，因此必须分开备份和限制访问。
- 成本为基于配置单价和供应商 usage 的估算；usage 缺失时不能视为真实的 0 成本。

## 后续扩展方式

1. **生产治理与观测收口**：在已有 DB governance、attempt ledger、typed audit/archive、Worker progress、exporter/规则与 Mock 容量基线上增加认证/租户 policy、告警发送/认证 Dashboard/tracing、签名/WORM 审计选项，并在目标硬件与精确提交上重新校准容量。
2. **数据集插件**：在已有固定 MMLU-Pro/GPQA 转换器上增加通用注册、签名、分片和更多 dataset plugin；继续把原始来源版本与转换器版本纳入 Hash 元数据。
3. **Evaluator 插件**：以版本化 Registry 增加 IFEval、代码沙箱、LLM Judge 和 Pairwise Judge；任何评分语义变化必须升级 protocol version。
4. **Adapter 扩展**：实现新的供应商 Adapter，而不是在 Runner 中添加条件分支；能力声明用于标识 seed、usage、工具调用和上下文窗口支持。
5. **Arena 与 Agent**：新增独立的 Match、Vote、Trajectory、ToolCall 等领域实体，不把交互式评测硬塞进单轮 EvaluationResponse。
6. **公共部署**：增加用户、项目、权限、审计、租户级秘密存储、速率限制和网络隔离后，才考虑公网服务。

这些扩展必须保持旧 Run 的快照可读，不得在无提示情况下把不同 `protocol_version` 或不同数据集 Hash 的结果合并比较。
