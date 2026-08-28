# LLMBenchLab MVP 需求规格

- 文档状态：Baseline
- 适用范围：Phase 0–2（含 Web Provider 凭据扩展）
- 协议基线：`llmbenchlab-protocol-v1`
- 关键词：**必须**表示 MVP 验收必需，**应该**表示高优先级但可记录理由后延，**可以**表示兼容性扩展。

## 1. 范围与术语

MVP 必须完成以下链路：注册 Mock 模型，载入内置 Demo Benchmark，创建 Run，在后台逐题调用、解析和评分，持久化结果，并在前端查看进度、逐题结果及排行榜。OpenAI-compatible 接入属于 MVP 配置能力，但自动化验收不得访问真实服务。

- **Model**：一个可调用的模型配置；公开表示不包含真实密钥值，Provider 凭据由独立的加密记录或兼容环境变量提供。
- **Benchmark**：有版本、题目集合、评分器和稳定 Hash 的数据集。
- **Run**：某 Model 在某 Benchmark 与固定协议配置下的一次评测。
- **Response**：Run 中单题生成、解析、评分、用量及错误的持久记录。
- **严格总分**：全部计划题目得分平均值乘以 100，失败或不可解析题计 0。
- **可评答案**：成功获得且被 Evaluator 唯一解析的答案。

## 2. 功能需求

### FR-MOD：模型注册与调用

- **FR-MOD-01** 系统必须支持创建、读取、更新、删除和分页列出 Model。
- **FR-MOD-02** `provider_type` 首期只允许 `mock` 与 `openai_compatible`。
- **FR-MOD-03** Model 必须包含：`id`、`name`、`provider_type`、`base_url`、`remote_model_name`、`credential_source`、兼容字段 `api_key_env`、`enabled`、每百万输入/输出 Token 价格、默认参数、`created_at`、`updated_at`。
- **FR-MOD-04** `openai_compatible` 必须校验并要求 `base_url`、`remote_model_name`，并选择 `stored` 或 `environment` 凭据来源；`mock` 的远端连接字段必须为空且凭据来源必须为 `none`。
- **FR-MOD-05** Web/REST 必须支持只写的 `api_key` 输入：明文只用于当前请求，服务端使用独立 keyring 的 AES-256-GCM 加密后保存到 `model_credentials`，不得把凭据数据流中的明文/Authorization 或 Provider 对 Key 的回显复制到 Model、Run-model snapshot、Response、Leaderboard、报告、日志或错误，也不得公开 nonce、ciphertext、key id 或 keyring material。该控制不要求扫描无关 Benchmark/Question 数据的独立字面巧合；`api_key_env` 仅作为 CLI 与既有部署的兼容路径。
- **FR-MOD-06** 必须定义 `ModelAdapter.generate(messages, generation_config)`，返回文本、Token、延迟、provider request id、原始 usage 和元数据。
- **FR-MOD-07** Mock Adapter 必须完全离线、输出可预测，可完成 Demo 中预定义的部分或全部问题，并可用于单元测试、CI 和 Smoke Test。
- **FR-MOD-08** OpenAI-compatible Adapter 必须使用 Chat Completions 风格请求，支持 system prompt、temperature、top_p、max_tokens、seed、可配置 base URL/模型名及连接/读取超时。
- **FR-MOD-09** 对 429、部分 5xx 和暂时网络错误必须有限次指数退避；明显的 4xx 配置错误不得无限重试。
- **FR-MOD-10** 上游没有 Token Usage 时相关值允许为 `null`；错误应保存稳定类型与经过脱敏的可读信息。
- **FR-MOD-11** Phase 1 的 `default_parameters` 只允许 `temperature`、`top_p`、`max_tokens`、`seed` 及其 Run 级约束；创建 Run 时显式请求值优先，最终有效值必须进入快照。

### FR-BEN：Benchmark 与导入

- **FR-BEN-01** 系统必须分页列出 Benchmark、读取详情、导入自定义数据集并可幂等或明确地重新载入内置 Demo。
- **FR-BEN-02** 每个数据集目录必须包含版本化 `manifest.json` 和逐行 JSON 的 `questions.jsonl`。
- **FR-BEN-03** manifest 必须包含 `schema_version`、`id`、`name`、`version`、`description`、`dimension`、`language`、`license`、`source`、`evaluator`、`prompt_template`、`question_count`。
- **FR-BEN-04** 必须支持 `exact_match`、`multiple_choice` 和 `numeric`；选择题选项使用稳定键，数值题可配置绝对与相对容差。
- **FR-BEN-05** 导入必须验证 manifest、每行 JSON、字段与题型、问题 ID 唯一性、题数声明和 Evaluator 兼容性。
- **FR-BEN-06** 校验错误必须标明文件、字段或 JSONL 行号和可读原因；格式错误时不得部分导入。
- **FR-BEN-07** 必须基于规范化且文档化的输入计算稳定 SHA-256；相同内容重复导入得到相同 Hash。
- **FR-BEN-08** 导入必须限制文件大小、防止路径穿越，manifest 不得指定任意本地文件路径，数据内容不得被执行。
- **FR-BEN-09** Demo 必须含 12–20 道自行编写的低版权风险题，覆盖三种题型，并在所有相关页面标记“Demo 数据，不代表正式模型能力”。

### FR-EVL：解析与评分

- **FR-EVL-01** 必须定义统一 `Evaluator.evaluate(raw_response, reference_answer, config)`，返回 parsed answer、0/1 score、correct、evaluator name、metadata 和 parse error。
- **FR-EVL-02** Exact Match 必须规范化首尾空白、常见换行和多余空格，并支持是否区分大小写；不得进行未声明的模糊匹配。
- **FR-EVL-03** Multiple Choice 必须支持 `A`、`A.`、`(A)`、“答案是 A”、“The answer is A”、“选择 A”、“最终答案：A”等格式，优先明确最终答案模式。
- **FR-EVL-04** Multiple Choice 不得把普通语句中的随机 A/B/C/D 当答案；结果冲突或无法唯一解析时写入 parse error。
- **FR-EVL-05** Numeric 必须安全解析整数、浮点数、科学计数法、`\boxed{}` 和明确最终答案表达，支持绝对/相对误差；禁止 `eval`。
- **FR-EVL-06** Numeric 必须拒绝或明确处理 NaN、Infinity、冲突值和非法输入。
- **FR-EVL-07** 请求错误、空回答和解析失败默认得 0 分，并保留错误类型；单题基础分只允许 0 或 1。

### FR-RUN：运行与持久化

- **FR-RUN-01** 创建 Run 后 API 必须立即返回 Run ID，执行由受控的进程内后台任务完成。
- **FR-RUN-02** 状态至少包括 `pending`、`running`、`completed`、`failed`、`cancelled`，且迁移必须合法、可追踪。
- **FR-RUN-03** 每完成一道题必须持久化 Response 并更新进度；单题异常不得终止其余问题。
- **FR-RUN-04** 同一 Run 不得被重复或并发启动；并发度必须是较小的可配置值。
- **FR-RUN-05** 未捕获的 Run 级错误必须把 Run 标记为 `failed` 并保存原因。
- **FR-RUN-06** 启动时必须把遗留 `running` 状态处理为 failed 或 interrupted 语义并记录“进程重启，MVP 无自动恢复”。
- **FR-RUN-07** 必须提供取消接口；正在执行的任务至少在题目边界检查安全停止标志并进入 `cancelled`。
- **FR-RUN-08** Run 结束必须计算严格总分、`completion_rate`、`answered_accuracy`、平均延迟、Token 与估算成本。
- **FR-RUN-09** `score = sum(question_score) / total_questions * 100`；`completion_rate = 成功获得非空模型响应的问题数 / total_questions * 100`；`answered_accuracy = 正确可评答案数 / 可评答案数 * 100`。`answered_accuracy` 分母为 0 时返回 `null`；总题数为 0 时 `score` 与 `completion_rate` 为 0，且全系统一致。
- **FR-RUN-10** 必须保存每题 raw response、parsed answer、标准答案快照、score、evaluator、latency、Token、成本、错误和 UTC 时间。

### FR-REP：可复现快照

- **FR-REP-01** protocol version 初始为 `llmbenchlab-protocol-v1`。
- **FR-REP-02** Run 必须快照：Benchmark ID/version/SHA-256、Evaluator 名称及版本、Prompt template、system prompt、temperature、top_p、max_tokens、seed、模型名、Adapter 类型、模型参数、Git commit SHA（不可得时为 `null`）、开始/结束时间、并发度和重试策略。
- **FR-REP-03** 默认公平参数为 `temperature=0`、`top_p=1`、固定 max tokens、并发度 1 或较小安全值；上游支持时传 seed。
- **FR-REP-04** 不同 protocol version、Benchmark version 或 dataset hash 的结果不得无提示混合比较。

### FR-API：REST API

- **FR-API-01** 必须提供版本前缀 `/api/v1` 和可用 OpenAPI 文档。
- **FR-API-02** 系统端点：`GET /health`、`GET /info`；健康检查不得依赖真实模型服务。
- **FR-API-03** 模型端点：`GET/POST /models`、`GET/PATCH/DELETE /models/{id}`。
- **FR-API-04** Benchmark 端点：`GET /benchmarks`、`GET /benchmarks/{id}`、`POST /benchmarks/import`、`POST /benchmarks/reload-demo`。
- **FR-API-05** Run 端点：`GET/POST /runs`、`GET /runs/{id}`、`POST /runs/{id}/cancel`、`GET /runs/{id}/responses`。
- **FR-API-06** 汇总端点：`GET /leaderboard`、`GET /metrics/summary`。
- **FR-API-07** 列表必须支持基本分页；Leaderboard 必须支持 Benchmark/Model 筛选和得分排序。
- **FR-API-08** 请求/响应必须使用明确 Schema、合理状态码和可读校验错误，任何响应不得泄漏秘密。
- **FR-API-09** CORS 只能允许配置的前端来源。

### FR-UI：用户界面

- **FR-UI-01** 中文 Dashboard 必须展示模型、Benchmark、Run、成功 Run 数，最近运行及得分、延迟、Token 概览。
- **FR-UI-02** Models 页面必须支持列表、添加、编辑、删除和表单校验；OpenAI-compatible 模型由用户直接在密码输入框粘贴 API Key，提交后立即清空，后续只显示“已安全保存”等状态，绝不回显密钥。环境变量名称不得作为 Web 主流程输入项。
- **FR-UI-03** Benchmarks 页面必须展示名称、版本、维度、语言、题数、Hash、详情、格式说明、Demo 标识及重新载入操作。
- **FR-UI-04** New Run 必须允许选择 Model/Benchmark，设置 temperature、top_p、max_tokens、seed，创建后跳转详情。
- **FR-UI-05** Run Detail 必须轮询状态并展示进度、三类得分/比率、正确/错误数、延迟、Token、成本、配置快照及逐题原始/解析/标准答案、得分与错误。
- **FR-UI-06** Leaderboard 必须展示模型、Benchmark/version、protocol version、严格总分、回答准确率、完成率、延迟、Token、成本和运行时间，并支持筛选与排序。
- **FR-UI-07** 所有页面必须有加载、空数据和错误状态，在常见桌面和移动宽度可用；不能是占位空壳。

## 3. 数据要求

### 3.1 实体最低字段

| 实体 | 必须字段 |
|---|---|
| Model | id, name, provider_type, base_url, remote_model_name, credential_source, api_key_env（兼容）, enabled, input/output price per million, default_parameters, created_at, updated_at |
| ModelCredential | model_id, key_id, nonce, ciphertext, algorithm, created_at, updated_at |
| Benchmark | id, slug, name, version, description, dimension, language, license, source, evaluator_type, dataset_hash, question_count, created_at |
| Question | id, benchmark_id, external_id, question_type, prompt, choices, reference_answer, evaluator_config, metadata |
| EvaluationRun | id, model_id, benchmark_id, status, protocol_version, model_parameters_snapshot, benchmark_hash_snapshot, prompt_template_snapshot, code_commit_sha, totals/counts, score, completion_rate, average_latency_ms, Token/cost, timestamps, error_message |
| EvaluationResponse | id, run_id, question_id, raw_response, parsed_answer, reference_answer_snapshot, score, evaluator_name, latency, Token/cost, error_type/message, created_at |

- **DR-01** 主外键、唯一约束和索引必须保护 Model/ModelCredential/Benchmark/Question/Run/Response 的关联完整性；每个 Model 最多一条凭据记录，删除 Model 时级联删除该记录。
- **DR-02** Benchmark 内 `external_id` 唯一；版本和 Hash 能区分数据修订。
- **DR-03** JSON 配置必须以结构化数据存储和验证，不能依赖不受控字符串解析。
- **DR-04** 所有持久化时间使用 UTC；金额计算和舍入规则必须一致且有测试。
- **DR-05** 删除 Model 或 Benchmark 时不得无提示破坏历史 Run；应拒绝删除或使用能保留历史证据的策略。
- **DR-06** SQLite 数据文件位置必须可配置、可备份，迁移由 Alembic 管理。

## 4. 用户故事

- **US-01** 作为首次使用者，我可以不配置 Key 注册 Mock 模型、载入 Demo 并完成一次 Run，以验证系统可用。
- **US-02** 作为本地用户，我可以在 Web 中直接粘贴 OpenAI-compatible API Key；保存后界面和 API 不再返回明文，后台 Worker 可以直接用于真实模型评测。
- **US-03** 作为研究人员，我可以导入经过校验的版本化小型 Benchmark，并看到题数与稳定 Hash。
- **US-04** 作为评测者，我创建 Run 后立即获得 ID，离开创建页面也能查看进度和终态。
- **US-05** 作为分析者，我能检查每题原始输出、解析答案、标准答案、评分、耗时、Token 和错误。
- **US-06** 作为比较者，我能按模型或 Benchmark 筛选排行榜，并确认结果使用相同协议和数据版本。
- **US-07** 作为维护者，我能用 Mock 自动测试重现完整链路，且 CI 不产生模型费用。
- **US-08** 作为贡献者，我能从文档判断当前阶段、架构决定、已知限制和下一项可执行任务。

## 5. 非功能需求

### 可复现性

- **NFR-REP-01** 相同数据内容的 Hash 必须稳定；数据或协议语义变化必须提升相应版本。
- **NFR-REP-02** 任一排行榜记录都必须可导航到 Run 和逐题证据。
- **NFR-REP-03** 快照不可依赖后续可变的 Model 或 Benchmark 当前配置来解释历史结果。

### 性能与可靠性

- **NFR-PERF-01** 在开发机、默认 SQLite、单个 12–20 题 Demo 上，Mock Run 应能在 30 秒内完成；该指标不适用于真实上游延迟。
- **NFR-PERF-02** 创建 Run 应在 2 秒内返回；列表和详情在本地千级 Response 数据下目标响应时间为 1 秒内（不含首次启动和前端网络开销）。
- **NFR-PERF-03** 列表必须分页，后台并发必须有上限，数据库会话和 HTTP 客户端必须正确释放。
- **NFR-REL-01** 单题失败隔离，汇总指标可由持久化 Response 重算；进程重启遗留状态不得永远显示运行中。
- **NFR-REL-02** MVP 明确不保证后台任务自动恢复、跨进程互斥或高可用；这些属于 Phase 2。

### 安全与隐私

- **NFR-SEC-01** 不得明文保存或返回真实 Key，不得记录 Authorization；Web 提交的 Key 只能以 AES-256-GCM 密文保存，错误信息、请求标识和日志必须经过 canary 测试证明不会反射密钥。
- **NFR-SEC-02** 导入限制大小、拒绝路径穿越和任意文件引用，不执行数据集代码；数值解析禁止 `eval`。
- **NFR-SEC-03** CORS 来源显式配置，API 校验输入；恶意 `base_url`/SSRF 是已记录风险。
- **NFR-SEC-04** `.env` 被 Git 忽略，CI secret 非必需；依赖应固定或有 lockfile 并接受自动化审计。
- **NFR-SEC-05** 当前系统仅面向可信 loopback 用户，不应直接暴露局域网或公网；生产前需鉴权、请求限制、KMS/Secret Manager、租户隔离、SSRF 防护和更严格上传隔离。

### 可维护性与可访问性

- **NFR-MNT-01** 后端使用 Python/FastAPI/SQLAlchemy 2.x/Pydantic/Alembic/httpx，前端使用 React/TypeScript/Vite；默认 SQLite，不引入 Redis。
- **NFR-MNT-02** API、Adapter、Evaluator、Runner 和持久层职责分离，公共函数有必要类型与文档。
- **NFR-MNT-03** 行为、API、迁移或协议变化必须同步文档、测试、状态、Changelog 和工作日志。
- **NFR-UX-01** UI 默认中文，状态不只依赖颜色表达，错误信息提供可操作建议，数值和时间显示单位/时区。

## 6. 测试与质量要求

- **TST-01** 后端覆盖三类 Evaluator 的正常、边界、冲突、容差、boxed 与非法输入。
- **TST-02** 覆盖 Mock Adapter、manifest/JSONL 校验与行号、Hash 稳定性、Model API 脱敏、CRUD 和 Health。
- **TST-03** 覆盖创建与完成 Mock Run、单题故障隔离、汇总分数和 Leaderboard 聚合。
- **TST-04** 前端覆盖格式化、Run 状态、得分/完成率、API 错误和至少一个主要页面，并通过 typecheck 与 production build。
- **TST-05** Smoke Test 使用临时 SQLite，完成注册 Mock、导入 Demo、Run、Response、Score 和 Leaderboard 断言，全程禁止网络。
- **TST-06** CI 在 PR 和 main push 上运行后端 lint/test、前端 lint/test/build，不要求 API Key。

## 7. MVP 验收条件

只有以下项目均有实际证据时 Phase 1 才能标为 `completed`：

1. 后端可启动，OpenAPI 与 Health 可访问；前端 production build 成功。
2. Mock Model 可注册，Demo Benchmark 可载入且题数、三类题型和 Hash 正确。
3. 创建 Run 后后台完成，逐题 Response 数与计划题目一致或每个缺失项有失败记录。
4. Exact Match、Multiple Choice、Numeric Evaluator 的关键边界测试通过。
5. 严格总分、`completion_rate`、`answered_accuracy` 计算正确并在 Run 与排行榜展示。
6. 前端可查看轮询进度、配置快照、逐题证据和可筛选排行榜；Demo 标识明显。
7. 离线 Smoke Test、关键单元/集成测试、lint、typecheck 和 build 实际通过。
8. CI、Makefile、环境变量示例、可选 Compose 和启动脚本完整；README 可从全新环境复现。
9. 仓库不含真实密钥，自动验证未访问真实 API，接口和日志不泄密。
10. 架构、协议、API、数据格式、安全、测试、部署、状态、阶段、ADR、Changelog、Next Task 与工作日志真实一致。

任何关键项未满足时，Phase 1 状态保持 `in_progress`，并在 `docs/PROJECT_STATUS.md`、阶段文档和工作日志列出缺口；不得用计划或文件存在替代验证结果。

## 8. 明确排除的 MVP 需求

完整大型数据集、代码执行沙箱、LLM Judge、Arena、Agent/Tool Use、长上下文、Redis/分布式 Worker、PostgreSQL 强制依赖、多用户鉴权、支付、Kubernetes 和公网生产部署均不属于本次验收，按 `docs/ROADMAP.md` 后续推进。
