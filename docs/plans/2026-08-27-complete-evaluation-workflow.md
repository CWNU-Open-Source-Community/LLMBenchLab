# 正式数据集与真实 API 完整评测流程执行计划

- Owner: Codex
- Status: in_progress（实现、独立终审与完整本地门禁已通过；阶段 commit/push/精确 SHA CI 待完成）
- Created: 2026-08-27
- Updated: 2026-08-27
- Related phase: [Phase 2](../phases/PHASE-2-RELIABILITY.md)、[Phase 3](../phases/PHASE-3-BENCHMARKS.md)
- Worklog: [2026-08-27-complete-evaluation-workflow.md](../worklogs/2026-08-27-complete-evaluation-workflow.md)
- ADRs: [ADR-0004](../decisions/ADR-0004-secret-management.md)、[ADR-0005](../decisions/ADR-0005-durable-task-execution.md)、[ADR-0006](../decisions/ADR-0006-local-real-provider-evaluation.md)

## Context

当前 main `1b0aa2a` 已有 Demo、自定义 ZIP、OpenAI-compatible Adapter 和可靠 Worker 基础，但正式数据仍为 Phase 3 计划，且现有入口要求操作者自行转换 ZIP、注入 Worker 环境变量、逐页收集结果。用户明确要求继续形成可以真实模型测试的完整流程，并允许联网获取数据。

本任务在不掩盖 Phase 2 仍未完成全局限流/预算/审计的前提下，交付可信本地客观评测垂直切片。真实 Provider 只在用户显式运行 CLI 时访问；本次开发与自动验证不使用 Key、不产生费用。

## Objective

用户提供 OpenAI-compatible Base URL、终端安全输入/API Key 环境变量和可选模型名后，可由一个本地命令固定下载 MMLU-Pro 或 GPQA-Diamond、预检接口、创建/恢复 Run，并导出含全量逐题证据和分组指标的可复核报告。

## Scope

- 固定 revision、源 SHA、缓存、转换与 ZIP 生成的数据集插件。
- MMLU-Pro test（direct/official_cot profile）与 GPQA-Diamond。
- dataset-v1 大数据集资源上限扩展，不改变 Hash 或评分语义。
- Provider `/models` 发现、最小 canary、getpass/env 密钥解析和脱敏诊断；远程只允许 HTTPS、HTTP 仅 loopback，发现结果反射当前 Key 或 canary 返回不同模型时硬失败。
- 复用现有数据库/API service、Worker lease、Adapter/Evaluator 的一键本地 CLI；Chat 成功体/错误体分别限制为 4 MiB/64 KiB，只接受 identity 编码，成功内容、raw usage 和相关标识在持久化前精确移除当前 Key。
- 大数据集有界消费者执行、事件循环外快照加载与持续心跳、恢复、JSON/JSONL/CSV 报告和 metadata 分组指标；报告从计划题目与 Responses 派生指标并标记 Run 字段漂移，dead-letter 前先聚合已持久化证据。
- 后端/前端回归、Mock-only Smoke、文档、工作日志、提交/push/CI 证据。

## Non-goals

- IFEval strict/loose 规则评分、代码沙箱、Judge、Arena、Agent。
- Web 明文 Key 输入、应用内密钥存储、OS Keychain 或多用户托管。
- 全局 Provider/Model/Run RPM、TPM、费用硬上限、公平调度或 Provider exactly-once。
- 把 direct profile 与官方 CoT 榜单无提示比较，或把本切片标为完整 Phase 2/3。
- 自动化测试调用真实 Provider 或联网下载数据。

## Assumptions

- 目标 Provider 实现 Chat Completions；远程地址提供 HTTPS（本机 loopback 开发端点可用 HTTP）；`GET /models` 可选，显式 `--model` 可覆盖缺失的模型列表能力。
- MMLU-Pro 固定 revision 的两个 Parquet 和 GPQA 固定 commit archive 可通过 HTTPS 获取；源 SHA 不匹配时硬失败。
- CLI 在可信本地单用户环境执行，运行期间不并行启动第二个 SQLite Worker。
- 当前三种确定性 Evaluator 足以评分 MMLU-Pro/GPQA 的选项字母；IFEval 明确不适用。

## Requirements

- FR-MOD-05–10：Key 不持久化、Chat Completions、有限重试、usage/error 语义。
- FR-BEN-01–08：版本化导入、严格校验、稳定 Hash、安全资源边界。
- FR-EVL-01/03/04/07：选择题解析与失败严格计零。
- FR-RUN-02–10：状态、逐题持久化、取消、聚合和证据。
- FR-REP-01–04：协议/数据/模型/执行快照与可比性隔离。
- NFR-SEC-01–05：密钥、数据导入、可信本地和 SSRF 边界。

## Implementation steps

1. [completed] 固定设计、来源与安全边界
   - Files/modules: ADR-0006、本计划、工作日志
   - Validation: 文档明确阶段偏差、许可、source revision/hash、密钥和真实调用边界
2. [completed] 数据集插件与可复现归档
   - Files/modules: `backend/app/standard_datasets/`、Dataset Loader、CLI/测试 fixture
   - Validation: 两次转换 hash 相同；源 hash/坏字段/筛选/GPQA shuffle/MMLU profile 测试通过
3. [completed] Provider preflight 与真实评测编排
   - Files/modules: `backend/app/providers/`、`backend/app/cli/`、project scripts/Make
   - Validation: MockTransport 覆盖发现/canary/认证/多模型、远程明文 HTTP 拒绝、当前 Key 反射拒绝、canary 返回模型不一致；Mock CLI 完成持久化 Run，并把首次 preflight 证据固化进快照
4. [completed] 有界执行、恢复与报告
   - Files/modules: Runner、`backend/app/reports/`、Run/response 读取
   - Validation: 大 fixture 不一次性建全量 task，快照加载移出事件循环且心跳持续；恢复只补缺失题；报告从计划题目/Responses 派生一致指标并通过 `metrics_provenance` 标漂移；两条 dead-letter 路径先聚合部分证据
5. [in_progress] 文档、全量验证与远程门禁
   - Files/modules: README、API/Data/Protocol/Security/Testing/Deployment、状态/阶段/Next/Changelog/工作日志
   - Validation: 统一命令、本地 diff/秘密扫描、阶段 commit/push、精确 SHA CI 全绿

## Risks

| 风险 | 可能性/影响 | 预防措施 | 触发后的处理 |
|---|---|---|---|
| 全量 12k 请求造成高费用 | 高/高 | 显示题数、canary、显式确认、支持 filter/limit、默认并发 1 | 取消 Run，保留已完成证据后按需 resume |
| Provider 配置错误被放大 | 中/高 | `/models` auth 检查和最小 Chat canary 在创建 Run 前完成 | preflight 失败，不创建正式 Run |
| 固定数据源漂移/下线 | 中/中 | revision URL + SHA-256 + 本地缓存 | 拒绝未知内容；新增版本/ADR 更新，不静默换源 |
| GPQA 选项位置泄题 | 高/高 | 每题基于 record id + 固定 seed 独立重排 | hash/映射测试失败即禁止生成 |
| MMLU official_cot 上下文成本大 | 高/中 | 提供 direct profile并明确不可比；报告 profile | 用户按用途选择，未知时不冒充官方分数 |
| Key 泄漏 | 低/高 | getpass/env、随机 env name、禁止 argv/DB/报告；成功 content/raw usage/request ID/model/fingerprint 持久化前按当前 Key 精确脱敏；发现反射 Key 即失败 | 立即停止传播、轮换 Key、清理进程环境 |
| 远端响应压缩/过大 | 中/高 | 只请求并接受 identity；成功体 4 MiB、错误体 64 KiB 上限 | 安全失败并记录有界、已脱敏错误，不持久化超限正文 |
| Runner 为 12k 题建 12k tasks | 高/中 | 固定消费者协程/队列 | 内存/取消回归失败则不开放全量入口 |

## Validation

| 验收项 | 命令或检查 | 预期结果 | 实际结果 |
|---|---|---|---|
| 数据插件 | `cd backend && uv run pytest tests/test_standard_datasets.py` | 全部通过、无网络 | 通过；固定真实源另验证 MMLU 12,032×2、GPQA 198 |
| Provider/CLI | `cd backend && uv run pytest tests/test_provider_preflight.py tests/test_evaluation_cli.py` | 全部通过、Mock-only | 通过；identity/体积上限、Key 反射/脱敏、模型错配和过期租约恢复均有回归，无真实调用 |
| 报告/Runner/租约 | `cd backend && uv run pytest tests/test_run_report.py tests/test_evaluation_runner_reliability.py tests/test_run_leases.py` | 聚合/恢复/心跳/有界执行通过 | 通过；独立终审未发现剩余可复现高/中阻断项 |
| 后端回归 | `make test` | 零失败 | `310 passed, 5 skipped`；5 个 skip 为未注入 DSN 的 infrastructure marker |
| 质量门禁 | `make lint && make test && make smoke` | 零失败 | 通过；Smoke `1 passed, 5 deselected` |
| 前端构建 | `cd frontend && npm run build` | production build 成功 | 通过；保留既有 647.22 kB chunk warning |
| 迁移/Compose | `make phase2-acceptance`、`docker compose config --quiet` | 既有可靠性回归成功 | 真实 PG/Redis 5/5；Compose 8/8；Alembic/lock/config 通过且资源清理为空 |
| 安全 | `git diff --check` + secret pattern scan | 无密钥/调试残留 | 通过；47 个候选文件无高置信 Key/Bearer 模式匹配 |
| 远程门禁 | 工作分支 commit/push + 精确 SHA GitHub Actions | 四个 required job 全绿 | 待完成；本地结果不替代远程 CI |

## Rollback

无 schema migration。停止使用新 CLI 即可回到既有 Demo/ZIP 路径；代码回滚不删除已经导入的 Benchmark、Model、Run 或 Response。新增正式数据缓存与报告位于 Git 忽略的 `artifacts/`，只有用户明确清理时才删除。Dataset Loader 上限可在确认数据库中没有依赖超旧上限的导入行为后恢复，但已有大 Benchmark 仍应保留可读。

## Documentation updates

- [x] README / 用户操作说明
- [x] API / 数据格式 / Benchmark 协议
- [x] Architecture / Security / ADR
- [x] Testing / Deployment
- [x] CHANGELOG、PROJECT_STATUS、Phase 2/3、NEXT_TASK、工作日志

## Completion evidence

- Changed files: 47 个 tracked/untracked 候选文件；无 schema migration、无前端产品行为变更
- Commands run: 完整本地门禁已执行；后端 310/5、真实基础设施 5/5、前端 13、Smoke 1、Compose 8/8，静态/构建/迁移/锁文件均通过
- Acceptance evidence: 固定来源全量转换、Provider 安全边界、心跳、报告漂移、dead-letter 和过期租约恢复均有回归；Compose evidence 为 `llmbenchlab-p2-7cf8ce9e4428/evidence.json`
- Not run: 真实 Provider 调用（无 Key 且自动化禁止）；远程精确 SHA CI（commit/push 前）
- Known issues: Phase 2 全局治理与 IFEval/代码沙箱继续未完成；CLI 需独占数据库且没有金额硬预算；首次 canary 会固化，但 resume canary 无独立审计事件，每题 transport request ID/model/fingerprint 未持久化

## Decision and discovery log

| 日期 | 类型 | 记录 | 影响/后续 |
|---|---|---|---|
| 2026-08-27 | discovery | MMLU-Pro 当前固定版本 test 为 12,032 题，超过现有 10,000 上限 | 提升资源上限并增加大集有界执行 |
| 2026-08-27 | discovery | GPQA 数据许可为 archive 内 CC BY 4.0，代码仓库根 LICENSE 为 MIT | 数据卡分别记录数据/代码许可，不混写 |
| 2026-08-27 | decision | 用户新指令优先形成真实客观评测切片，Phase 2 仍保持 in_progress | ADR-0006 明确可信本地和非生产边界 |
| 2026-08-27 | decision | IFEval 不适配现有三种确定性 Evaluator | 本切片只接选择题 MMLU-Pro/GPQA，不伪造 IFEval 分数 |
| 2026-08-27 | decision | CLI 直接复用数据库 lease/Runner，而不托管 Key 或新增 Web secret API | Key 只存在于可信本地进程；运行前必须停常规 Worker |
| 2026-08-27 | decision | 真实 Provider 远程只允许 HTTPS、HTTP 仅 loopback；发现与 Chat 只接受 identity，发现 2 MiB、Chat 成功/错误体 4 MiB/64 KiB | 缩小明文传输、压缩炸弹和无界正文风险；仍不替代 SSRF allowlist/出站隔离 |
| 2026-08-27 | decision | 发现结果反射当前 Key、canary 返回不同模型均失败；成功 content/raw usage/request ID/model/fingerprint/finish reason 在持久化前精确移除当前 Key | 防止不可信 Provider 把当前凭据写入证据或把目标模型静默替换 |
| 2026-08-27 | decision | 报告指标从计划题目与 Responses 派生并标记 Run 字段漂移；fail-attempt/reaper dead-letter 前聚合证据 | Failed/Cancelled 部分结果在 summary/groups/responses 与 Run 终态之间保持可解释一致性 |
| 2026-08-27 | discovery | 初次 canary 证据会固化进 Run 快照，但 resume canary 无独立追加事件；每题 transport request ID/model/fingerprint 不落库 | 明确保留为 P2-06 完整审计缺口，不把当前快照描述为逐请求审计 |
| 2026-08-27 | verification | 完整固定源转换与全部本地门禁已完成；独立终审无剩余高/中阻断项 | 只剩阶段 commit/push 与远程精确 SHA CI |
