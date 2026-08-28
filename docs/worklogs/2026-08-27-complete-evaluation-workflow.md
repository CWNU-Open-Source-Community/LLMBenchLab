# 2026-08-27 — 正式数据集与真实 API 完整评测流程工作日志

> 本日志记录实际发生的工作，不是事后美化的总结。所有命令以仓库根目录为基准。

## 元信息

- 日期：2026-08-27
- 执行者：Codex
- 关联阶段：[Phase 2](../phases/PHASE-2-RELIABILITY.md)、[Phase 3](../phases/PHASE-3-BENCHMARKS.md)
- 关联计划：[正式数据集与真实 API 完整评测流程执行计划](../plans/2026-08-27-complete-evaluation-workflow.md)
- 关联 ADR：[ADR-0004](../decisions/ADR-0004-secret-management.md)、[ADR-0005](../decisions/ADR-0005-durable-task-execution.md)、[ADR-0006](../decisions/ADR-0006-local-real-provider-evaluation.md)
- 最终状态：in_progress（实现、独立终审、本地门禁与实现 commit/push 已完成；PR/精确 SHA CI 待用户授权）

## 初始仓库状态

- 当前分支：由 `main` 创建 `codex/complete-evaluation-workflow`
- `git status --short --branch` 摘要：任务开始前 `## main...origin/main`，工作区干净；HEAD/origin/main 均为 `1b0aa2a`
- 已有未提交改动：无
- 相关功能与测试现状：文档记录后端 205 个非集成测试、5 个真实基础设施集成、前端 13 个、Smoke 1 个及 Compose 8/8；本任务开始时尚未重跑
- 环境约束：macOS、Python 3.14 本地 uv 环境、网络可用；无真实 Provider Key，本次开发/自动验证不得产生真实调用或费用

## 本次目标与背景

用户明确要求按计划继续推进到可实际评测的流程，允许在线查找和下载数据，并将提供只需 OpenAI-compatible API URL/Key 即可调用的真实模型。现有仓库只有 Demo 与手工 ZIP，模型配置还需自行注入 Worker 环境变量，且无固定正式数据来源、preflight、一键编排或完整报告。

完成后，可信本地用户可固定下载 MMLU-Pro 或 GPQA-Diamond，安全输入 Key，发现/选择模型，先做最小 canary，再创建或恢复持久化 Run，并获得可复核的完整报告。

## 范围

- 数据插件、固定来源/revision/SHA、缓存、转换、筛选和归档。
- Provider 模型发现、真实 Chat canary、密钥内存边界和一键 CLI。
- 大数据集资源上限、有界执行、恢复、全量逐题与分组报告。
- Mock-only 测试、文档、状态、阶段、Changelog、提交/push/CI。

## 非目标

- IFEval、代码沙箱、Judge/Arena/Agent。
- Web/API 明文密钥、持久化密钥托管。
- 完整 P2-05/P2-06/P2-07 或完整 Phase 3 宣称。
- 自动化真实 Provider 调用或在线数据下载。

## 验收标准

- [x] MMLU-Pro 与 GPQA-Diamond 可从固定 revision 下载，源 SHA 不匹配硬失败，重复转换得到同一 dataset hash。
- [x] MMLU-Pro 全 12,032 题可被校验；GPQA 198 题选项按固定 seed/record 独立打乱且答案映射正确。
- [x] 用户只需 Base URL、安全输入 Key 和必要时模型选择，即可 preflight 并完成/恢复一个持久化 Run；控制流由 MockTransport/Mock 验证，真实兼容性留给持有 Key 的用户。
- [x] CLI 在任何 Provider 请求前显示题数/HTTP attempts 上界并要求确认；认证/模型发现、disabled/conflicting Model、active Run 等失败不会创建正式 Run。远程地址只允许 HTTPS（HTTP 仅 loopback），发现结果反射当前 Key 或 canary 返回不同模型时失败。
- [x] 大数据集不会一次性创建每题 task；同步快照加载移出事件循环且加载期间心跳持续；取消、租约、单题隔离和 protocol-v1 聚合回归保持。
- [x] `summary.json`、`groups.csv`、`responses.jsonl` 行数和聚合一致，不包含 Key；指标从计划题目/Responses 派生，`metrics_provenance` 标记与 Run 聚合字段漂移。
- [x] fail-attempt 与过期租约 reaper 在 dead-letter 前聚合已持久化 Response 证据。
- [x] 当前最终工作树的后端/前端测试、lint/typecheck/build、Smoke、迁移、真实 integration 与 Compose 本地门禁均已通过并冻结计数。
- [x] 实现 commit `0e62a371b9dd7bd819359a4a2b16ff8d5faa3a0d` 已 push 到工作分支且远端一致。
- [ ] 该分支工作流只由 PR/`main` 触发；创建 PR 待用户明确授权，精确 SHA 的必需 GitHub Actions 全绿后才标记完成。

## 假设

- Provider 支持 Chat Completions；模型列表接口可选，显式模型名作为兼容回退。
- 固定数据源可访问；离线测试用最小 fixture，不依赖网络可用性。
- 真实测试由用户后续显式运行，本任务没有 Key，也不会自行选择付费模型。

## 风险

| 风险 | 影响 | 缓解措施 | 结果 |
|---|---|---|---|
| 12k 题成本和时间 | 可产生高额费用/长运行 | canary、题数确认、limit/group、并发默认 1、resume | 已实现 HTTP attempts 上界与确认；仍无 Token/金额硬预算 |
| 数据许可/漂移 | 不可复现或违规再分发 | 运行时下载、固定 revision/SHA、数据卡、不提交题目 | 完整固定源与缓存 SHA 已验证，仓库无第三方题目 |
| 密钥泄漏 | Provider 凭据泄露 | getpass/env、禁止 argv/DB/报告；成功 content/raw usage/request ID/model/fingerprint/finish reason 持久化前精确移除当前 Key；发现反射 Key 即失败 | 离线回归和 47 个候选文件秘密扫描均通过 |
| 远端正文无界或压缩膨胀 | 内存耗尽/敏感正文扩散 | 发现与 Chat 只接受 identity；发现 2 MiB、Chat 成功 4 MiB/错误 64 KiB 上限 | 超限或压缩响应安全失败，不保存正文 |
| 阶段依赖偏差 | Phase 2 未完成却启动 Phase 3 切片 | ADR-0006 和状态文档明确 partial/非生产边界 | Phase 2/3 均保持 `in_progress` |

## 实施步骤

1. [completed] 记录用户优先级、阶段偏差、来源/许可和安全决定。
2. [completed] 实现与测试标准数据集插件、缓存和归档。
3. [completed] 实现与测试 Provider preflight、一键评测 CLI。
4. [completed] 实现有界执行、恢复和报告。
5. [in_progress] 文档、终审、本地/真实基础设施门禁及实现 commit/push 已完成；PR/精确 SHA CI 待用户授权。

## 实际修改

| 文件/模块 | 修改内容 | 对应需求/原因 |
|---|---|---|
| `docs/decisions/ADR-0006-local-real-provider-evaluation.md` | 固定可信本地正式数据/真实 Provider 入口与边界 | 用户目标、ADR-0004/0005 补充 |
| `docs/plans/2026-08-27-complete-evaluation-workflow.md` | 建立跨模块可持续执行计划 | `PLANS.md` 强制要求 |
| 本工作日志 | 记录初始事实、范围、风险与证据 | `AGENTS.md` 强制要求 |
| `backend/app/standard_datasets/` | 固定 MMLU-Pro/GPQA 来源、SHA、缓存、转换、筛选和可复现 ZIP | 正式数据供应链与可复现性 |
| `backend/app/providers/` | `/models` 发现、URL 推导、响应上限、认证分类与最小 Chat canary；远程 HTTPS/loopback HTTP 策略、当前 Key 反射拒绝和返回模型一致性检查 | 正式 Run 前验证配置并限制秘密泄漏/目标替换 |
| `backend/app/cli/` | `prepare/run/resume/report`、getpass/env Key、确认、直接 lease/Runner 编排 | 用户只提供 URL/Key 即可运行的可信本地入口 |
| `backend/app/reports/` | 终态 Run 全量分页证据、证据派生指标、`metrics_provenance` 漂移标记、分组指标、原子非覆盖私有文件发布 | Failed/Cancelled 部分证据与 summary/groups/responses 使用同一口径 |
| Adapter、Runner、Run service / lease | Chat 成功/错误体 4 MiB/64 KiB、identity-only、成功证据当前 Key 精确脱敏、连接池回收、固定消费者 task、快照加载移出事件循环并保持心跳、dead-letter 前聚合证据、空 system 省略、API/CLI 共用不可变快照 | 大集资源、安全和终态一致性边界 |
| Dataset Loader / Schema / `pyproject.toml` / lock | 上限 20k/128/130 MiB，引入固定范围 `pyarrow` 与 CLI console script | MMLU 12,032 题和 Parquet 解析 |
| 后端测试 | 新增标准数据、preflight、CLI、报告测试并扩展 Adapter/Loader/Runner/Smoke | 全部自动化保持离线/Mock-only |
| README 与 `docs/` | 操作、API、数据、协议、安全、架构、部署、测试、Roadmap、阶段、状态、下一任务和 Changelog 联动 | 实现/边界/证据一致 |

## 决定、偏差与发现

| 时间 | 类型 | 事实与理由 | 后续影响 |
|---|---|---|---|
| 10:54 CST | deviation | 用户明确要求优先形成真实模型完整评测，超过现有 Phase 2 Next Task 的非目标 | 新增 ADR-0006；Phase 2 仍不标完成 |
| 10:54 CST | discovery | MMLU-Pro 最新固定 revision test 12,032、validation 70；现有上限 10,000 | 提升安全上限并验证有界执行 |
| 10:54 CST | discovery | GPQA archive 数据许可为 CC BY 4.0，Diamond 198；根代码许可 MIT | 插件数据卡分离记录许可，不提交题目 |
| 实施中 | decision | IFEval 需要官方 strict/loose evaluator，不能映射为现有选择题评分 | 本轮明确排除 IFEval，不生成不可比的伪分数 |
| 实施中 | decision | GPQA 选项按 `sha256-sort-v1(seed\0record_id\0option_index)` 逐题重排 | 固定位置不泄漏答案；转换配置进入身份和来源证据 |
| 实施中 | decision | 正式 CLI 复用现有数据库 lease/Runner，并在 canary 前完成本地 Model/Run 冲突检查 | 不新增 secret API；需停止常规 Worker 并独占数据库 |
| 验收中 | discovery | OpenAI-compatible 服务可能没有 `/models` | 仅在用户显式给出 model 时允许 404/405 回退，不猜测多个模型 |
| 收尾中 | decision | 远程 Provider 只允许 HTTPS，HTTP 仅 loopback；发现与 Chat 只接受 identity，发现 2 MiB、Chat 成功/错误响应限制为 4 MiB/64 KiB | 降低明文传输、压缩膨胀和无界正文风险，但不替代地址 allowlist/出站隔离 |
| 收尾中 | decision | 模型发现反射当前 Key 或 canary 返回不同模型时失败；成功 content/raw usage/request ID/model/fingerprint/finish reason 持久化前按当前 Key 精确脱敏 | 不让恶意/异常 Provider 把凭据写入成功证据或静默替换目标 |
| 收尾中 | fix | `resume` 对过期但证据不全的 `running` lease 调用本地 Runner 做 fenced reclaim | 避免常规 Worker 已停止时无限等待；有效未过期 owner 仍不被覆盖 |
| 收尾中 | decision | Runner 同步快照加载移出事件循环；fail-attempt/reaper dead-letter 前聚合证据；报告从计划题与 Responses 派生并标记 Run 字段漂移 | 心跳不被大快照阻塞，终态和导出对部分证据保持一致 |
| 收尾中 | discovery | 初次 canary 证据会固化进 Run 快照；resume canary 不追加独立事件，每题 transport request ID/model/fingerprint 未持久化 | 记录为 P2-06 剩余审计缺口，不宣称逐请求完整审计 |

## 实际运行命令

| 命令 | 目的 | 退出码 | 结果摘要 |
|---|---|---:|---|
| `git status --short --branch` | 检查任务开始状态 | 0 | `main` 与 `origin/main` 同步，工作区干净 |
| `git ls-remote https://huggingface.co/datasets/TIGER-Lab/MMLU-Pro.git` | 核实官方数据 revision | 0 | main/HEAD `b189ec765aa7ed75c8acfea42df31fdae71f97be` |
| `git show ...:data/*.parquet`（LFS pointer） | 核实 MMLU-Pro 固定源 SHA/大小 | 0 | test SHA `0e24a191...`、validation SHA `139423c2...` |
| `git clone --no-checkout https://github.com/idavidrein/gpqa.git` | 核实 GPQA 官方来源 | 0 | HEAD `56686c06f5e19865c153de0fdb11be3890014df7` |
| `shasum -a 256` 与 archive header inspection | 核实 GPQA archive 与字段 | 0 | archive SHA `461ae732...`；Diamond 198；数据 license CC BY 4.0 |
| `git switch -c codex/complete-evaluation-workflow` | 建立独立工作分支 | 0 | 已切换 |
| 目标化 pytest（标准数据、Provider、CLI、报告、Adapter、Runner、租约、API/Smoke） | 快速迭代与边界回归 | 0 | 多轮通过；终审后 Provider/Adapter/CLI 聚焦回归 `102 passed`，核心六模块聚焦回归 `125 passed` |
| 完整固定源转换 | 核实联网来源与全量转换 | 0 | MMLU `direct/official_cot` 各 12,032；GPQA-Diamond 198；round-trip hash 一致 |
| `llmbenchlab-evaluate prepare ... --limit 2` | 验证公开 CLI 的真实下载/转换入口 | 0 | MMLU archive `bf2479159ecb34a1034b81335dca1e46e9d7b01355d45d18da347501490fab52` / dataset `436f589e4f8ccb8c69724cbea40e4a778a99ce4252aac00a77e7b25d473aa1b9`；GPQA archive `db5413b8e1090cc792382e0500a069ce461ef76f795af71983f85ab7089f44fa` / dataset `570f5f254f3d1648c3ab4122c1cbaaa352f2bcc13a5a123c0f21841e3d7aadc3` |
| `make lint` | 后端 Ruff/format、前端 ESLint/typecheck | 0 | 全部通过；后端 100 files formatted |
| `make test` | 后端普通全量与前端 Vitest | 0 | 后端 `310 passed, 5 skipped`；前端 4 files / `13 passed` |
| `make smoke` | 隔离 SQLite + Mock 垂直链路 | 0 | `1 passed, 5 deselected` |
| `npm run build` | 前端 production build | 0 | 成功；保留既有 647.22 kB chunk warning |
| `uv run alembic check` / `uv lock --check` / `docker compose config --quiet` | migration、依赖锁和部署静态门禁 | 0 | 均通过；无新 migration，锁文件可解析，Compose 配置有效 |
| 临时 PostgreSQL 16/Redis 7 + `pytest -m integration` | 独立真实基础设施 CI 等价回归 | 0 | `5 passed, 0 skipped`；精确测试容器由 trap 清理 |
| `make phase2-acceptance` | 隔离 Compose 八场景故障验收 | 0 | 8/8 passed；evidence `llmbenchlab-p2-7cf8ce9e4428/evidence.json`；容器/卷/网络残留均为空 |
| `git diff --check` + 47 个候选文件高置信 secret pattern scan | 空白、patch 完整性与秘密检查 | 0 | 无 diff 错误、无 Key/Bearer 模式匹配 |
| `git commit` / `git push -u origin codex/complete-evaluation-workflow` | 形成并发布实现阶段 | 0 | commit `0e62a371b9dd7bd819359a4a2b16ff8d5faa3a0d`；远端 ref 与本地一致 |
| `gh run list --commit 0e62a371...` | 核对精确 SHA 远程门禁 | 0 | `[]`；workflow 只对 PR/`main` push 触发，PR 创建待用户明确授权 |

## 测试结果

- 固定真实数据源全量转换、目标回归和统一收尾门禁均已完成；独立终审未发现剩余可复现高/中风险阻断项。
- 后端 `310 passed, 5 skipped`；真实 PostgreSQL/Redis 单独运行 `5 passed, 0 skipped`；前端 `13 passed`；Smoke 1 项；Compose 8/8。
- Ruff/format、ESLint、TypeScript、Vite build、Alembic check、lock check、Compose config、diff 和秘密扫描均通过。
- 本机 Python 3.14 仍有 pytest-asyncio、Starlette TestClient 和既有 migration fixture 的上游警告，无测试失败；CI 固定 Python 3.12。

## 未运行验证

- 真实 OpenAI-compatible Provider 调用未运行：本任务没有 Key，且自动化/CI 禁止真实或付费调用。真实兼容性、模型质量、吞吐和实际金额只能在用户显式提供 URL/Key 并确认后验证。
- 精确阶段 SHA 的 GitHub Actions 尚未运行：实现 commit 已 push，但 workflow 只由 PR/`main` push 触发；创建 PR 需用户明确授权，本地通过不替代远程门禁。

## 未完成项

- PR 创建与精确 SHA 四个 GitHub Actions required job 仍待完成；完成前本任务保持 `in_progress`。
- Phase 2 全局配额/预算/审计/性能基线与 Phase 3 IFEval/通用插件 SDK/代码沙箱/UI 继续留在后续任务。

## 已知问题与限制

- 真实 Provider 调用不是 exactly-once；全局 RPM/TPM/费用硬上限尚未交付。
- 任意 Provider Base URL 仍有已记录 SSRF/DNS 重绑定风险，仅可信本地使用。
- 首次 Run 的 discovery/canary 证据会固化进 Run 快照，但 resume canary 未追加独立审计事件；每题 transport request ID、Provider 返回 model 与 system fingerprint 未持久化，P2-06 完整逐请求审计仍未完成。
- CLI 只能拒绝已经存在的 `running` Run，无法检测一个尚空闲、但会抢领新 `pending` Run 的常规 Worker；正式运行前必须停止 API/Worker 并独占数据库。
- MMLU-Pro `direct` 与 `official_cot`、筛选/limit 子集和 GPQA Prompt profile 必须按报告身份隔离，不能无提示与其他榜单比较。

## 安全检查

- 真实密钥扫描：47 个 modified/untracked 候选文件高置信 Key/Bearer 模式无匹配
- 真实 API 调用：否；本任务无 Key
- 传输/脱敏边界：远程 HTTPS、HTTP 仅 loopback；发现/Chat identity-only，发现 2 MiB、Chat 成功/错误体 4 MiB/64 KiB；成功 content/raw usage/request ID/model/fingerprint/finish reason 持久化前精确移除当前 Key。目标与完整门禁均通过
- 危险 Git 操作（force push/reset 等）：无
- 阶段 push：实现 commit `0e62a371...` 已成功推送，远端一致
- 远程 CI：该 SHA run 列表为空；PR 创建待用户明确授权
- 遗留安全风险：见 [SECURITY.md](../SECURITY.md) 与 ADR-0006

## 结果与下一步

实现已形成固定正式数据、真实 API 预检、持久 Run、恢复与完整报告闭环，且没有使用真实 Key；本地与真实基础设施门禁及实现 commit/push 已完成。下一步需用户授权创建 PR，再等待精确 SHA 远程 CI；远程门禁完成前不把本任务或 Phase 标记为完成。用户后续可先用 `--limit 20` 做显式付费兼容性测试，再决定是否全量。

## 最终 Git 状态

```text
implementation commit: 0e62a371b9dd7bd819359a4a2b16ff8d5faa3a0d（已 push，远端一致）
branch: codex/complete-evaluation-workflow
remote gate: pending PR authorization; no Actions run for implementation SHA
```
