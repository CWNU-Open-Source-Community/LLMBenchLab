# 2026-08-30 — 修复观测 Token 估算误触发全局 overdraw 工作日志

> 本日志记录实际发生的工作；所有命令以仓库根目录为基准。

## 元信息

- 日期：2026-08-30
- 执行者：Codex
- 关联阶段：[Phase 2](../phases/PHASE-2-RELIABILITY.md)
- 关联计划：[执行计划](../plans/2026-08-30-fix-observational-token-overdraw.md)
- 关联 ADR：[ADR-0018](../decisions/ADR-0018-observational-token-estimates-are-not-hard-reservations.md)
- 当前状态：in_progress

## 初始仓库状态

- 分支：`codex/complete-evaluation-workflow`，跟踪同名 origin 分支。
- `git status --short --branch`：任务开始时工作树干净。
- 已有用户改动：无 Git tracked 改动；本地 SQLite、keyring、日志与 dataset artifact 均保持为 Git 忽略资产。
- 当前数据库 head：`20260829_0006`；开发 API/Worker/Web 正在运行。

## 目标与背景

修复 OpenCode-Hy3 Run 被本地治理误判为 global overdraw 的缺陷。冻结 policy 和 Run override 没有 hard 限制；第七次成功 Provider attempt 的观测 input estimate/reservation 为 59、actual 为 75，导致四层 scope 被永久标记。任务必须恢复后续 managed admission，同时保留历史实际用量、七条 Response 和失败 Run 终态。

## 范围

- ADR、runtime、数据迁移、历史 materialization、UI 文案、测试和强制文档。
- 在无 active reservation 的维护窗口迁移当前个人 SQLite并只读验真。

## 非目标

- 不继续/改写旧失败 Run，不发送真实 Provider 请求。
- 不实现其他 OpenCode 协议或扩大 Provider 功能。
- 不清空数据库、ledger、audit 或治理计数。

## 验收标准

- [x] 无显式 input hard bound 时，Provider actual 大于观测 estimate 不触发 overdraw。
- [x] 显式 input/output hard reservation 及由完整上界和价格派生的 reserved cost 超额仍 fail closed。
- [x] 历史 0006 数据迁移保留 ledger/actual/Response，并按新语义重算 scope。
- [x] 当前数据库四层误判解除，旧 Run 仍 failed/exhausted且 7 条 Response/407 input/599 output 保留。
- [x] 前端不再把所有 overdrawn 描述为 conservative settlement。
- [x] 目标/完整测试、`make lint`、frontend build、smoke、部署配置和双方言迁移验证通过。
- [ ] 独立 commit 已 push，精确 SHA CI 必需 jobs 全部成功。

## 假设与风险

- 历史显式 input bound 可由冻结的 `evaluation_runs.input_token_reservation` 无歧义判断。
- 迁移时若存在 active reservation 必须停止，不猜测结算结果。
- 新 head 必须同步 setup/adoption、archive allowlist、acceptance 常量与 P2-07 exact-head 文档。

## 实施步骤

1. [completed] 读取仓库规则、Phase 2/治理文档，核对工作树和真实 Run/ledger。
2. [completed] 建立 ADR-0018、执行计划与本日志。
3. [completed] 实现代码、迁移与回归测试，并完成本地完整与真实基础设施验证。
4. [completed] 执行当前数据库维护迁移和只读事实验证。
5. [in_progress] 文档收尾、审查、修正 commit/push/exact-SHA CI。

## 已确认事实

- Run `2181503c-eab2-4699-bede-db48bd078f95`：15 题完成 7 题，终态 `failed/exhausted`，reason=`governance_global_overdrawn`。
- 7 个 attempt 全部 `settled_actual/succeeded`，无 conservative settlement、429 或 HTTP retry。
- 第七次 input reservation/actual=`59/75`；其余六次 actual 未超过 reservation。
- global/provider/model/run 四层 scope 均 active/reserved=0、consumed requests=7、input=407、output=599、overdrawn=true。
- policy v1 和 Run overrides 的所有 hard request/Token/cost 限制均为 `null`。

## 实际修改、命令、结果与收尾

本次实际修改：

- Runner 不再把非显式输入估算传给治理 repository；Run 未冻结 `input_token_reservation` 时，新 attempt 的 input reservation 与 reserved cost 为空，actual usage 仍原样保存。
- Runtime、SQLite→PostgreSQL importer 和 Phase 2 capacity reconciliation 使用同一显式性规则：历史 managed reservation 关联 Run 冻结字段判断 input/cost，output reservation 独立，没有 Run 的 synthetic reservation 仍视调用者值为显式。
- 新增 data-only `20260830_0007`；不改 schema/ledger/actual/Response/audit/Run，只重算 scope `overdrawn`，两个方向都在更新前拒绝 active reservation，downgrade 恢复旧谓词。
- Migration adoption、archive-v1 compatible-head、P2-07 exact recovery head 和 acceptance/capacity 常量已同步到 `0007`；历史 `0006` 索引修复事实保留。
- Run Detail 对 overdrawn 显示“实际用量曾被判定超过预留”，不再声称一定发生保守结算，也不会误述升级前保留的历史终态；对应目标回归已加入。
- README、Changelog、Architecture、API、Operations、Security、Testing、Deployment、Project Status、Roadmap、Phase 2、Next Task、ADR/计划/日志已同步。

已实际完成的本地验证与数据维护：

- Backend 全量：`946 passed, 33 skipped`；自动化仅使用 Mock/fixture。
- `make lint` exit 0：backend Ruff check/format、frontend ESLint/TypeScript typecheck 全部通过。
- 真实 PostgreSQL 16 + Redis 7 integration：`33 passed`；PostgreSQL migration upgrade/downgrade/upgrade/check 通过。
- Frontend：`39 passed`；production build 通过；仅保留既有 662.40 kB chunk warning。
- Mock smoke：`1 passed`；`docker compose config` 通过。
- 首个实现 commit `c6212db2376ffc6b5f32c46b000aad8e7faf9b1f` 已普通 push。其精确 SHA GitHub Actions [run `33270167616`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33270167616) 中 backend、真实 PostgreSQL/Redis integration、frontend 三个 job 成功；real-Compose job 在 `deterministic_database_seam_provider_and_response_commit_recovery` 失败。下载的 evidence 精确定位为 acceptance harness 对合法的 `actual_cost_usd=null` 执行 `float(None)`；生产治理和所有 Run 均正常完成。
- 已将 conservative settlement 验收改为逐维精确比较 nullable/numeric reserved→actual，并加入 null、numeric、mismatch 回归。目标脚本测试 `16 passed`；完整 `make phase2-acceptance` 为 `9/9 passed`，此前失败场景通过，最终 cleanup 的 container/volume/network 均为空。第一次本地尝试在业务场景前被 Docker Hub 镜像代理 `docker.1panel.live` 的 403 阻断；随后仅使用临时匿名 Docker 配置与公共缓存补齐本机基础镜像，不修改全局 Docker 配置，实际验收成功。
- 当前个人 SQLite 已由一致性备份 `backend/data/llmbenchlab-pre-0007-20260830.db`（SHA-256 `3fcf7f4980e935a78a3d7b66351cb3556f6cbba5e37d348a82d9252d4d962792`）和 migration 入口自动备份共同保护后迁移到 `20260830_0007`；两份备份均收紧为 `0600`。四层 `overdrawn` scope 从 4 降为 0，旧 Run 仍为 `failed/exhausted`，7 Responses、7 ledger、407 input/599 output 全部保留，13 张业务表行数迁移前后一致，`quick_check=ok`、foreign-key violation=0。
- 开发 API/Worker/Web 已重新启动；`/api/v1/health` 为 `ok`，任务 metrics 为 active Provider attempt `0`、overdrawn scope `0`、live Worker `1`。真实 PostgreSQL/Redis 回归使用的两个精确临时容器已停止并由 `--rm` 清除。
- 未调用真实 Provider。首个实现 SHA 的远程门禁如实保留为 3/4；nullable acceptance 修正尚待形成新 commit/push 并取得新的 exact-SHA 4/4 证据，因此任务仍为 `in_progress`，最后一项验收保持未勾选。

最终独立审查未发现 Blocker/High/Medium。剩余 Low 覆盖缺口是：seeded historical overdraw 与 active-reservation rollback 的长期自动回归目前在 SQLite，真实 PostgreSQL 已执行空库双方言 SQL 往返但未持久化同构数据 fixture；importer/capacity 的 provenance wiring 也分别由 helper/SQL 文本目标测试覆盖，尚无另一个端到端 historical row 场景。这些不改变当前修复结论，后续若扩充集成门禁可合并为一个串行 PostgreSQL fixture。
