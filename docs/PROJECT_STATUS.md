# 项目状态

> 更新时间：2026-08-28（Asia/Shanghai）

## 当前阶段

- Phase 0 — 项目治理和架构：`completed`（2026-08-24）
- Phase 1 — MVP 垂直链路：`completed`（2026-08-25）
- Phase 2 — 可靠性与任务执行：`in_progress`（可靠基础、治理/审计与 P2-01 单机资格已完整交付；P2-06/P2-07 运维闭环仍有缺口）
- Phase 3 — 标准 Benchmark 与代码评测：`in_progress`（仅可信本地 MMLU-Pro/GPQA-Diamond 客观题提前切片）
- Phase 4–6：`planned`

## 当前版本与远程边界

`0.1.0` development baseline，REST API 为 `/api/v1`，评测协议为 `llmbenchlab-protocol-v1`；尚未发布正式 Release。

公开仓库：[`CWNU-Open-Source-Community/LLMBenchLab`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab)，当前开发分支为 `codex/complete-evaluation-workflow`，PR [#2](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/pull/2)。P2-01 v2 实现 SHA `b6a35fef1dd069ebb54b69955058915c722aa34d` 已普通 push，GitHub Actions [run `33146681285`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33146681285) 的四个必需 job 均成功；证据文档收尾 commit `875f13a253c40b7573d45c6287385e60f2bb8f04` 也已普通 push，其精确 SHA 的 [run `33150080341`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33150080341) 4/4 成功。后者完成仓库级收尾，不改变性能证据绑定的实现 SHA。

## 已交付基线

- Phase 0/1 的治理、架构、协议、数据格式、ADR、FastAPI/SQLAlchemy/Alembic、React/TypeScript、Mock 垂直链路、三类 Evaluator、Demo 数据、API/UI、离线测试和开源流程。
- PostgreSQL/Redis 可靠执行基础：数据库事实来源、Redis at-least-once 通知、独立 Worker、DB scan、租约/heartbeat/fencing、逐题幂等、有限 retry/backoff、取消、租约接管、dead-letter 和终态 Response 重算。
- OpenAI-compatible SSE、严格 `[DONE]`、JSON fallback、identity-only、wire/event/content/error 上限、idle read timeout、bounded error 与精确当前-Key 脱敏。
- Web write-only `api_key`、AES-256-GCM `model_credentials`、数据库外 API/Worker 共享 keyring、legacy `api_key_env`、origin/active-Run 门禁和 fail-closed repair/remove 路径。
- MMLU-Pro test 与 GPQA-Diamond 固定 revision/SHA 转换、可信本地 `llmbenchlab-evaluate prepare/run/resume/report`、请求上界确认和原子终态报告。该 CLI 仍要求独占数据库，未受 Phase 2 managed budget 保护。
- React 中文界面覆盖 Dashboard、Models、Benchmarks、Evaluation Runs、New Run、Run Detail、Leaderboard；Run 列表全状态筛选/分页/活动轮询，详情逐题分页，关键桌面/平板/移动布局已修复。

## 已通过候选门禁的 Phase 2 切片

- Alembic 链已扩展到 `20260827_0004`。新增六类治理/审计表：`governance_policies`、`governance_scopes`、`governance_minute_buckets`、`question_executions`、`provider_call_reservations`、`audit_events`；加上既有业务/凭据表，SQLite→PostgreSQL importer 现按依赖顺序复制和对账全部 12 表。
- active policy 在 SQLite/PostgreSQL 都由 partial unique index 保证唯一；policy 有 canonical hash。managed API Run 创建时冻结 policy ID/hash 与 input reservation、lifetime request/Token/USD overrides，旧 Run 和可信本地 CLI 保持 `legacy_unmanaged`。
- global/provider/model/run 四层数据库权威治理已实现：concurrency、固定 UTC 分钟 RPM/TPM、global/run lifetime request/Token/cost；Redis 和进程内存不参与裁决。
- Adapter 的每个实际 HTTP retry attempt 都进入 reserve→send-started→actual/conservative settlement 或 confirmed pre-send release ledger。未知 usage、失租或 commit 不确定不按零释放。
- materialized scope/minute counter 只是 ledger 投影；高报/低报、policy/hash 或 Run override 漂移在 admission/mutation/reconcile/import 边界 fail closed，并只尝试记录固定非秘密完整性事件。
- [ADR-0011](decisions/ADR-0011-confirmed-pre-send-release-retry-generation.md) 已修复零 HTTP 的 pre-send release 消耗 retry：旧 ledger row 保持终态，下一 generation 从未发送 ordinal 恢复，包括 `max_retries=0`。
- backlog local admission、typed `429`、database not-before、有限 question quantum、dispatch/failure 分离和跨 Model due ordering已接入；Run 不因 Redis 故障丢失。
- typed append-only 应用 audit、分页 Run audit、task history counters、数据库 Run 时间戳 queue/execution/end-to-end latency、严格规范化 Provider request/model/fingerprint/finish metadata 和固定非秘密 credential audit 已实现。
- 前端 Run Detail 已显示 managed/delayed/exhausted、治理原因和明确 UTC not-before；它不把治理延迟冒充 Worker 正在执行。
- enhanced capacity 脚本已加入有限 policy、显式 Token/费用边界、sub-15 question quantum、并发 backlog `202/429`、跨 Model 公平、双 Worker、Worker/Redis fault 与 ledger/audit 对账；真实 PostgreSQL 测试代码已加入四层 RPM/TPM/lifetime budget、backlog、settlement/reconcile race 和 audit replay。
- acceptance harness 已加入三条确定性数据库 seam injection：`reserved`→send-start、`send_started`→settlement、Response commit→最终恢复。它们明确不是“精确时刻 SIGKILL”声明；精确候选 SHA 的完整 Compose acceptance 已 9/9 通过。
- 精确 SHA `665244e…` 的增强 capacity 使用有限 policy、PostgreSQL 16、Redis 7 与两个 Worker 完成：并发 backlog 精确为 4 个 `202` + 2 个 typed `429`，cooperative yield 与跨 Model 公平顺序均有 durable audit 证据，最终 18 Runs/270 Responses/271 ledger/1229 audit 对账且无 active/reserved/overdrawn 漂移。
- P2-01 的 `P2-local-control-plane-v2` 已在干净 SHA `b6a35fe…` 从零执行 1 次 warm-up + 恰好 5 次 measured trial，本次 invocation 的 `discarded_trials=0`；四个 cell、23/23 SLO 与每轮 hard invariant 全部通过，容量模型为 `qualified`。aggregate SHA-256 为 `a76d167b…d0d9`，六轮均精确完成 22 Runs/330 Responses/330 QuestionExecutions/331 reservations，并清理本项目容器、卷、网络和唯一 build image。历史 v1 aggregate `f993c11f…e3b2` 继续保持 `failed/not_qualified`。

## 仍未完成

- P2-06：受控 metrics exporter、告警规则/响应、audit retention archive/restore、Worker DB-time progress/liveness、全日志源治理；现有 dependency probe 不能证明主循环正在推进。
- P2-07：PostgreSQL backup/restore、数据库与 keyring 配对恢复、audit archive/Redis 重建、剩余故障矩阵和完整运维演练。
- Phase 3：IFEval、通用 Dataset Plugin SDK、代码题 schema/隔离沙箱、完整分组 UI 和安全红队；Phase 4–6 尚未开始。

## 已知边界与风险

- SQLite 只用于个人本地单 Worker；多 Worker 证据必须来自 PostgreSQL。Compose 是本地开发/验收拓扑，不是生产 HA。
- Provider 调用不是 exactly-once。Worker 在 Provider response 后本地 commit 前崩溃可能重复上游计算或费用；本地 ledger/Response 幂等只能保证数据库事实不 double-count。
- fixed UTC minute window 允许边界 burst，不等同平滑 token bucket。Mock capacity 不能推断真实 Provider、生产 SLA 或无限横向扩展。
- trusted-local CLI 按 [ADR-0010](decisions/ADR-0010-phase-2-governance-delivery-boundaries.md) 继续 `legacy_unmanaged`，没有全局 RPM/TPM/USD 硬保证；操作者必须停止常规 API/Worker 并独占数据库。
- audit 是应用 append-only、event-key 幂等且 read 时校验 schema/hash，但数据库管理员仍可修改，不能宣称 WORM。
- Provider metadata 不安全时归一化为 `null`；credential audit 不保存 origin。Key、Authorization、ciphertext、nonce、keyring、Provider URL、题目/prompt/response正文均不得进入 audit。
- Worker probe 只检查数据库/head/Redis 能力，不证明主循环仍在 scan/claim/heartbeat/progress；这是正式 Phase 2 closure 缺口。
- importer 会复制完整敏感评测内容和 credential ciphertext；只支持停写源→空目标单向导入。keyring 不随数据库复制，exit 3/4 禁止盲目重试。
- 远程 Provider 只允许 HTTPS（HTTP 仅 loopback），但仍无 destination allowlist、DNS rebinding 防护、出站隔离、认证、TLS 终止、生产 KMS 或多租户安全；不得直接暴露公网。
- 当前 Python 3.14 本地测试仍可能显示上游弃用 warning；CI 固定 Python 3.12。Vite build 仍有既有 Recharts 大 chunk warning。

## 测试状态

| 验证 | 实际结果 | 当前结论 |
| --- | --- | --- |
| P2-01 实现远程 CI | `b6a35fe…` run `33146681285` 4/4 | 精确实现 SHA 全绿；PR #2 仍 open，未合并 |
| P2-01 证据文档收尾 CI | `875f13a…` run `33150080341` 4/4 | 精确文档 SHA 全绿；P2-01 仓库级收尾完成 |
| 最新本地 `make lint` | Ruff/format、ESLint、TypeScript 通过 | 本地冻结树通过 |
| 最新本地 `make test` | 后端 `829 passed, 29 skipped`；前端 `38 passed` | v2 实现冻结树通过 |
| 最新真实 PostgreSQL/Redis integration | `29/29 passed` | 本地通过；同一实现 SHA 的远程 integration 亦成功 |
| 最新本地 `make smoke` | `1 passed, 7 deselected`，仅 Mock | 本地冻结树通过；没有调用真实 Provider |
| 定向治理/API/Worker | 目标套件零失败；早期独立审计 `218 passed`；完整性边界集合 `18 passed` | 已被最终全量、真实 integration 与精确候选 evidence 补充 |
| SQLite/PostgreSQL migration | 隔离 SQLite 与真实 PostgreSQL prepare/upgrade/downgrade guard/upgrade/check 通过 | 候选与远程 integration 覆盖 |
| 增强 capacity | 精确 `665244e…`，evidence SHA-256 `40deadeb…0588` | passed；Mock-only，cleanup 容器/卷/网络为空，不是生产 SLA |
| 完整 acceptance | 精确 `665244e…`，9/9，evidence SHA-256 `ab311665…ddec` | passed；含三条 deterministic DB seam，cleanup 为空 |
| 正式 v2 单机资格 | 精确 `b6a35fe…`，1+5、23/23，aggregate SHA-256 `a76d167b…d0d9` | passed/qualified；Mock-only 单机控制面，不是生产或真实 Provider SLA |
| 真实 Provider | 未运行（有意） | 所有自动化只使用 Mock/Stub/MockTransport |

详细命令与限制见 [当前工作日志](worklogs/2026-08-28-phase-2-slo-capacity-model.md) 和 [TESTING.md](TESTING.md)。

## 最近工作日志

- [Phase 2 可靠执行基础](worklogs/2026-08-25-phase-2-reliable-execution-foundation.md)
- [完整客观评测流程](worklogs/2026-08-27-complete-evaluation-workflow.md)
- [Web Provider 凭据](worklogs/2026-08-27-web-provider-credentials.md)
- [Web Run UX 与生成预算](worklogs/2026-08-27-web-run-ux-and-generation-budgets.md)
- [OpenAI-compatible SSE](worklogs/2026-08-27-openai-compatible-sse-streaming.md)
- [Phase 2 治理、审计与性能](worklogs/2026-08-27-phase-2-governance-audit-performance.md)
- [Phase 2 正式 SLO 与容量模型](worklogs/2026-08-28-phase-2-slo-capacity-model.md)

## 当前任务入口

[NEXT_TASK.md](NEXT_TASK.md) 是当前合同：治理/审计候选已交付，P2-01 v2 也已完成独立实现提交、正式本机证据、实现 SHA CI 和证据文档 SHA CI；下一工程范围是 P2-06 的 Exporter/告警/retention/Worker progress，以及 P2-07 的 backup/restore 和剩余故障演练；Phase 2 继续保持 `in_progress`。
