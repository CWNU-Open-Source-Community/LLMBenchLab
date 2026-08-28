# 2026-08-28 — Phase 2 恢复与运维闭环工作日志

## 元信息

- 日期：2026-08-28
- 执行者：Codex
- 分支：`codex/complete-evaluation-workflow`
- 初始 HEAD：`fc7bc9bf406c15920aede0be08bb128c25da277f`
- 关联阶段：[Phase 2 — Reliability](../phases/PHASE-2-RELIABILITY.md)
- 关联计划：[Phase 2 恢复与运维闭环执行计划](../plans/2026-08-28-phase-2-recovery-operations.md)
- 决策基础：[ADR-0005](../decisions/ADR-0005-durable-task-execution.md)、[ADR-0007](../decisions/ADR-0007-web-provider-credentials.md)、[ADR-0009](../decisions/ADR-0009-database-governance-audit-fair-scheduling.md)、[ADR-0015](../decisions/ADR-0015-observability-worker-progress-audit-retention.md)
- 本轮决策：[ADR-0016](../decisions/ADR-0016-postgresql-keyring-recovery-and-redis-rebuild.md)
- 当前状态：P2-07 启动工作包 `completed`；P2-07 功能实现仍为 `not_started`

## 目标与背景

P2-06 已在 implementation SHA `9a20676dcf545040782f04c166205d0043345753`、evidence-doc SHA `ec2959680459a14aa308bd4d9ebcc6bb7bfcf3a6` 和最终状态 SHA `fc7bc9bf406c15920aede0be08bb128c25da277f` 分别完成普通 push 与精确 SHA 4/4 CI。Phase 2 仍缺 P2-07：数据库+keyring 配对恢复、Redis replacement、八规则响应和剩余故障矩阵。

本轮按用户要求只落实最近的启动任务：在不调用真实 Provider、不改变评分/API 协议、不引入危险生产恢复命令的前提下，完成只读勘察，冻结恢复合同，并建立独立计划和工作日志。只读 verifier、隔离恢复、Redis/Worker/alerts/fault matrix 均留给后续独立实施，不在本轮展开。

## 后续计划范围

- ADR-0016、独立计划/日志。
- 13 表 canonical recovery integrity 与 strict manifest/artifact/keyring verifier。
- PostgreSQL 16 custom dump→空目标 restore→read-only exact verify。
- matching/missing/wrong keyring 的 AES-GCM/AAD 本地验证，零 Provider I/O。
- Redis volume/group/PEL/lag 丢失后的数据库扫描恢复和新通知验证。
- Worker expected minimum 与 1↔2 扩缩顺序。
- 八规则 promtool 时序、真实底层 symptom/Runbook 和故障矩阵。
- Make/CI/evidence/security/docs/commit/push/exact-SHA gate。

## 非目标

- 生产 backup scheduler、PITR/WAL、RPO/RTO、HA、Kubernetes、对象锁/KMS/WORM。
- PostgreSQL/Redis destructive API、自制 restore CLI、`--clean/--create`、FLUSH/删 group/DB replay。
- Alertmanager sender/通知/ack/silence 系统。
- 真实 Provider、第三个 Worker、公共多租户、批量 credential 重加密。
- Benchmark、protocol、评分、前端产品或 REST API 行为变化。

## 验收标准

- 正确 dump+keyring 在新空 PG16 目标恢复后 head=`20260828_0005`、13 表 count/PK/content digest 与治理/audit 完整性全 exact。
- Manifest/dump/keyring swap、missing/wrong/权限/类型/size/race/tamper 全部 fail closed；stored fake credential 全部本地解密且 Provider I/O=0。
- Redis container/volume 完全替换后，DB-only scan 恢复 pending/expired、group 从 `0-0` 重建、PEL/lag=0、重复通知不改变 durable snapshot。
- Worker 1↔2 扩缩的 expected/live/stalled/shortfall、graceful stop、lease/fencing/ledger/Response 唯一性收敛。
- 八条规则都有 synthetic before/fire/clear 与真实 symptom/Runbook 处置证据；持久事实不被删除伪造恢复。
- remaining dead-letter、commit unknown、integrity/overdraw、cancel/retry/lease/budget matrix 在 real PG/Redis/Mock-only 环境通过。
- 全量/lint/smoke/build/migration/Compose/secret/diff/staged review、独立 commit/push 和每个 exact SHA CI 全绿；在此之前 P2-07/Phase 2 保持 `in_progress`。

## 假设

- 标准 PostgreSQL 16 工具由部署环境/PG container 提供；backend 不捆绑数据库恢复工具。
- 源数据库在明确维护窗口停止所有 writer，目标数据库新建且为空。
- keyring 与 dump 分离存储，只在受控验证时同时可读。
- 自动化仅使用随机假 keyring、stored credential canary、Mock/Stub/MockTransport。

## 风险

- 误删默认/共享数据库或 volume：全部 destructive 操作只允许随机隔离 project/database、exact label/identity，禁止 glob/prune/force。
- dump/keyring/manifest 泄漏：0600/0700、nofollow/no-replace、分离 ACL、严格 evidence allowlist，不记录路径/命令/内容/ID。
- restore commit outcome unknown：不盲重试或 clean；保留隔离目标并只读分类。
- 错误 keyring 空集通过：artifact digest 必须先精确匹配，再做 envelope decrypt。
- Redis 被误当任务备份：只允许空 replacement + DB reconciliation，不恢复 AOF/PEL 权威状态。
- 告警/CI 为快速过绿而降低语义：规则时序与真实 symptom 双层 AND，失败 evidence 保留并从零重跑。

## 实施步骤

1. 阅读强制文档、检查 clean 工作树并勘察 keyring/importer/queue/Worker/alerts/Compose/CI。
2. 新增 ADR-0016、执行计划和本日志，冻结工具、安全、结果和 evidence 边界。
3. 抽取 recovery integrity，实现 manifest/只读 verifier 与单元/PG integration。
4. 补 Redis NOGROUP/rebuild、Worker expected/scale 与 real Redis/PG 测试。
5. 实现 promtool fixture、独立 Compose recovery harness、八响应/fault matrix 和 scoped cleanup。
6. 同步文档，运行完整门禁，完成 staged 技术/安全审查。
7. 独立 commit/push，在 clean SHA 重跑正式 evidence，完成 evidence/status commit 与各自 exact-SHA CI。

## 初始勘察事实

- 初始工作树 clean，HEAD 与 origin 均为 `fc7bc9bf406c15920aede0be08bb128c25da277f`。
- 当前 Alembic head 为 `20260828_0005`，importer 固定 13 表并已有 typed canonical digest、policy/Run/ledger/audit preflight。
- 本机没有 host `pg_dump/pg_restore`，Docker/Compose 可用；隔离 harness 应使用 PostgreSQL 16 container 内标准工具。
- `CredentialKeyring` 已提供严格 JSON、最多 32 个 32-byte key、AES-GCM 与 Model/origin AAD；当前运行时读取不等同备份 artifact 的 owner/mode/race 检查。
- 现有 setup/dev/docker-up 会在 keyring 缺失时自动生成新文件；恢复路径必须绕过 bootstrap，只验证既有配对 artifact，并在验证成功前禁止启动 API/Worker。
- Worker 每轮先 reaper/due DB scan，再读 Redis；queue failure 已把 initialized flag 与 autoclaim cursor 重置，但 Redis replacement/group-loss 缺少真实回归。
- P2-06 八条 rule/Runbook 已交付；真实 15 分钟等待不适合作为 CI，需 promtool synthetic time + Compose symptom 双层验收。
- `worker_expected_processes` 由 API Settings 输出；只 scale Worker 不会更新运行中 API 的 expected。
- P2-07 不需要 REST API；`docs/API.md` 预计不改，最终日志仍须记录此适用性判断。

## 实际修改

- 新增 ADR-0016、P2-07 独立计划和本工作日志。
- 最小同步 README、Changelog、项目状态、Roadmap、Phase 2 和 NEXT_TASK，使仓库明确 P2-07 已有工作包但功能尚未开始。
- 未修改生产代码、测试、Compose、CI、API、数据库 schema 或依赖。

## 已运行命令与结果

- `git status --short --branch`：初始工作树 clean，分支与 origin 同步。
- 强制文档/ADR/API/Operations/Deployment/Testing 与 keyring/importer/queue/Worker/Compose/CI 只读勘察：完成。
- `git diff --check`、三个新增文件的 no-index whitespace 检查与 9 份变更文档的 97 个相对链接检查：通过，0 缺失。
- staged 范围/模式/秘密/禁止 artifact 路径检查：仅 9 份预期 Markdown，新增文件均为普通 `100644` 文本，未发现真实凭据、绝对本机路径或 evidence/artifact。
- 代码测试/lint/Compose：未运行；本轮只有文档启动包，不得写成通过。

## 已知问题与未完成项

- P2-07 所有功能实现/验证仍未开始；当前仅合同、执行记录和状态入口落盘。
- Phase 2 必须保持 `in_progress`；不得宣称 DR SLA、PITR/RPO/RTO、HA、Alertmanager 或 Provider exactly-once。
- 默认用户 SQLite 不在本任务范围，不迁移、不删除、不作为恢复目标。

## 下一步

本轮在启动工作包提交、push 和精确 SHA CI 后停止。后续恢复本任务时，从计划步骤 2 的最小只读 verifier 切片开始；不得把本轮文档提交解释为 P2-07 功能已实现。
