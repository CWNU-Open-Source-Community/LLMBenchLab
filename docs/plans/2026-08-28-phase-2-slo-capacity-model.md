# Phase 2 正式 SLO 与容量模型执行计划

- Owner: Codex
- Status: active
- Created: 2026-08-28
- Updated: 2026-08-28
- Related phase: [Phase 2 — Reliability](../phases/PHASE-2-RELIABILITY.md)
- Worklog: [2026-08-28 工作日志](../worklogs/2026-08-28-phase-2-slo-capacity-model.md)
- ADRs: [ADR-0012](../decisions/ADR-0012-single-host-slo-capacity-qualification.md)、[ADR-0013](../decisions/ADR-0013-stable-image-content-fingerprint.md)、[ADR-0014](../decisions/ADR-0014-dual-backlog-slo-profile.md)；既有基础为 [ADR-0005](../decisions/ADR-0005-durable-task-execution.md)、[ADR-0009](../decisions/ADR-0009-database-governance-audit-fair-scheduling.md)、[ADR-0010](../decisions/ADR-0010-phase-2-governance-delivery-boundaries.md)、[ADR-0011](../decisions/ADR-0011-confirmed-pre-send-release-retry-generation.md)

## Context

Phase 2 已交付数据库权威的可靠 Worker 与治理/审计切片。精确实现 SHA `665244e095905083b606b8e98e946ed1a02dc0fc` 已通过单轮 Mock-only enhanced capacity、9/9 acceptance、真实 PostgreSQL/Redis integration 和远程 4/4 CI，但当前文档正确地把这些称为观测基线而非正式 SLO。P2-01 仍需把支持拓扑、性能/正确性目标、多轮统计、容量公式和参数边界变成可执行合同。

本计划只交付 P2-01。Exporter/告警/retention/Worker progress 与 backup/restore 分别留给后续 P2-06/P2-07 独立切片，因此整个 Phase 2 仍为 `in_progress`。

## Objective

在不调用真实 Provider、不改变协议/API/schema 的前提下，为固定单机 Compose 资格拓扑交付一个可重复的多轮 SLO harness：每轮使用隔离 PostgreSQL 16/Redis 7/Mock Worker，聚合可比证据并对 queue、recovery、throughput、error、backlog、公平与最终对账做明确 pass/fail；最终用精确干净 commit 的真实多轮 evidence 和远程 CI 证明该合同可执行。

## Scope

- 单机/单区域 Compose 支持拓扑与最小资源、连接池、Worker/Run/题/quantum/Mock latency 假设。
- queue、recovery、throughput、error、backlog、公平、ledger/audit/reconciliation/cleanup 的资格目标。
- `phase2_capacity.py` 的恢复耗时与必要 machine-readable facts。
- 新多轮资格脚本、统计/指纹/SLO 判定、gitignored evidence 和 Make target。
- 纯离线单元测试、自检、1 轮 warm-up + 5 轮 measured 的本机资格运行、文档与交付门禁。

## Non-goals

- 真实 Provider 容量、Provider SLA、费用准确性或 exactly-once。
- 生产 HA、多区域、Kubernetes、任意硬件、任意负载或无限 Worker 扩缩。
- P2-06 Exporter/alert/retention/Worker liveness 和 P2-07 backup/restore。
- 生产默认参数的静默修改、数据库 migration、公共 API、Benchmark/评分协议或安全边界变化。

## Assumptions

- 资格实验在同一宿主上串行运行：先完成 1 轮不计样本的 warm-up，再完成恰好 5 轮 measured trial；每轮都有唯一 Compose project 与完整 cleanup。
- Mock delay 是固定的本地可控等待，只用于隔离调度/数据库/Worker 能力，不能代表真实 Provider latency distribution。
- 正确性和完整性目标为每轮硬门禁；性能目标使用跨轮样本和保守阈值，不允许用平均值掩盖单轮正确性失败。
- 只有 clean git commit、相同 capacity/SLO 脚本、相同配置和相同环境指纹的 trial 可以聚合。
- 若真实运行环境不满足 ADR 的最小资源，harness 必须 fail closed 或明确给出不合格，而不是降低阈值。

## Requirements

- `docs/NEXT_TASK.md` P2-01：支持拓扑的排队/恢复/吞吐/错误/backlog SLO、硬件与 Worker/Run/题/quantum/Provider-latency 模型、多轮置信区间/变异和参数校准。
- `AGENTS.md`：测试/CI 仅 Mock；证据必须是实际执行事实；独立 commit/push/精确 SHA CI；Phase 验收未齐不得标记完成。
- ADR-0005/0009/0010/0011：PostgreSQL/DB truth、Redis 通知层、lease/fencing、治理 ledger、CLI 与审计边界不变。

## Implementation steps

1. [completed] 冻结资格合同与统计/安全设计。
   - Files/modules: 本计划、工作日志、`docs/decisions/ADR-0012-*.md`
   - Validation: ADR 明确支持拓扑、阈值、统计、无效 trial、容量/参数公式和非目标；设计评审无未处理高风险。
2. [completed] 扩充单轮 capacity 的恢复计时并实现多轮资格 harness。
   - Files/modules: `scripts/phase2_capacity.py`、新增 `scripts/phase2_slo.py`
   - Validation: dependency-free self-check；能串行启动独立 trials、严格验证可比性、聚合统计、评估所有 SLO 并在失败时保留 evidence/返回非零。
3. [completed] 增加离线测试和统一命令入口。
   - Files/modules: `backend/tests/test_phase2_capacity_script.py`、新增 `backend/tests/test_phase2_slo_script.py`、`Makefile`
   - Validation: 正常/边界/混合 trial/失败/cleanup/secret canary/统计精度/CLI 测试通过，Ruff 与格式检查通过。
4. [completed] 完成本地实现门禁并冻结 v1 实现 commit。
   - Files/modules: 所有本切片实现、ADR、计划与初始日志
   - Validation: 定向测试、`make lint`、`make test`、`make smoke`、Compose config、自检、diff/secret/staged review 全通过；独立 commit 普通 push。
5. [completed] 在精确干净实现 SHA 上运行 v1 真实多轮资格实验。
   - Files/modules: gitignored `.pytest_cache/artifacts/phase2-slo/` 与逐轮 capacity artifacts
   - Validation: 1 轮 warm-up + 5 轮 measured 全部保留；六个 child 硬门禁通过，但 aggregate 15/18 SLO，通过合同正确判为 `unqualified`。不得选择或重跑单轮改写结论。
6. [in_progress] 冻结并实现 v2 双 backlog 资格合同。
   - Files/modules: ADR-0014、`phase2_capacity.py`、`phase2_slo.py` 及两套脚本测试
   - Validation: 默认 capacity 保持兼容；formal v2 固定 warmed `3/5/8` 与 cold `6/8/10` 两个 AND 门禁、两个 distinct claim Worker、22/330/331 对账、capacity-only 安全镜像 cleanup。
7. [pending] 提交/push v2 实现，等待精确 SHA CI，并从零执行 v2 1+5。
   - Validation: 新 clean SHA 的所有必需 CI job 成功；全新 warm-up + 5 measured 不复用 v1 样本；所有 SLO/invariant/cleanup 通过并复核 evidence SHA-256。
8. [pending] 收敛文档、提交并等待最终精确 SHA CI。
   - Files/modules: README、Performance、Testing、Operations、CHANGELOG、PROJECT_STATUS、Roadmap、Phase 2、NEXT_TASK、本计划与工作日志
   - Validation: 文档只引用实际 evidence；Phase 2 仍 `in_progress`；最终 commit push 后 GitHub Actions 必需 job 对该精确 SHA 全绿。

## Risks

| 风险 | 可能性/影响 | 预防措施 | 触发后的处理 |
| --- | --- | --- | --- |
| 性能阈值因主机抖动偶发失败 | 中/中 | 多轮串行、保守阈值、记录最小资源和 CV/区间 | 保留失败 evidence，诊断资源争用；不得重跑到“挑中”成功样本 |
| 多轮 evidence 聚合不可比样本 | 中/高 | strict commit/config/environment/script fingerprint | 整体 fail closed，逐项报告不一致字段 |
| 每轮约 100 秒导致反馈慢 | 高/低 | 单元测试模拟 trial；真实资格仅候选冻结后运行 | 不减少真实轮数或跳过 fault/reconciliation，记录时长 |
| 正确性失败被统计平均掩盖 | 低/高 | 每轮 invariants all-pass 为先决条件 | 立即标记 aggregate failed，不计算“通过”结论 |
| 日志/evidence 泄漏秘密 | 低/高 | 复用 redaction、只存 allowlist 摘要、canary 扫描 | 中止交付，删除未提交 artifact 并修复测试 |
| 宿主不满足最小资格资源 | 中/中 | environment preflight fail closed | 保留失败 evidence；不能宣称 SLO 通过，另行记录诊断缺口 |

## Validation

| 验收项 | 命令或检查 | 预期结果 | 实际结果 |
| --- | --- | --- | --- |
| SLO/capacity 单元测试 | `cd backend && uv run pytest -q tests/test_phase2_capacity_script.py tests/test_phase2_slo_script.py` | 全部通过 | 237 passed |
| 静态门禁 | `make lint` | 后端/前端 lint、format、typecheck 全通过 | 已通过 |
| 全量测试 | `make test` | 后端与前端零失败 | 后端 829 passed、29 skipped；前端 38/38 passed |
| 离线 smoke | `make smoke` | Mock-only smoke 通过 | 1 passed、7 deselected |
| 既有 capacity 回归 | `make phase2-capacity` | v1 单轮合同与 cleanup 通过 | 已通过；当前脏树兼容性证据，不能用于正式资格 |
| 完整 acceptance 回归 | `make phase2-acceptance` | 既有 9 类场景与 cleanup 通过 | 9/9 通过；当前脏树兼容性证据，不能用于正式资格 |
| 前端 production build | `cd frontend && npm run build` | build 通过 | 已通过；仅既有 chunk size warning |
| Alembic metadata | 独立临时 SQLite `alembic upgrade head && alembic check` | 无未迁移 schema diff | 已通过；未改本地默认数据库 |
| Compose 配置 | `docker compose config --quiet` | exit 0 | 已通过 |
| harness 自检 | `python3 -I scripts/phase2_slo.py --self-check-only` | 固定合同与安全边界通过 | 已通过 |
| 真实多轮资格 | `make phase2-slo` | 1 warm-up + 5 measured，所有 SLO/invariant/cleanup 通过 | v1 第三次从零完成 1+5；六个 child 硬门禁通过，但 measured-02 cold burst 使 aggregate 仅 15/18，最终 `unqualified`；v2 尚未运行 |
| diff/secret | `git diff --check`、staged secret scan | 无无关改动、秘密或 artifact | `git diff --check` 与 staged scan 已通过；仅命中明确的测试 canary/假 DSN |
| 远程门禁 | `gh run view <run-id>` | 精确最终 SHA 的必需 job 全成功 | `d5a1bd3` run `33139542534`、`c909f24` run `33139960008`、`dfa67ab` run `33141140969` 均为 4/4 success；v2 实现 SHA 尚未形成 |

## Rollback

本切片不迁移数据库、不改变 API 或运行时默认。回滚时可移除新的 wrapper、Make target、测试和文档，并保留既有 `phase2_capacity.py`；gitignored evidence 可按项目目录单独清理。若只回滚恢复计时字段，旧 capacity evidence schema 的既有字段和执行语义仍须保持兼容。任何失败/历史 evidence 不作为生产数据导入数据库。

## Documentation updates

- [ ] README / 用户操作说明
- [ ] Performance / Testing / Operations / Deployment（按实际影响）
- [ ] Architecture / Security / ADR（安全边界不变时只交叉引用）
- [ ] CHANGELOG、PROJECT_STATUS、Roadmap、Phase 2、NEXT_TASK、工作日志

## Completion evidence

- Changed files: 待完成后填写。
- Commands run: 待完成后填写实际命令、退出码和测试数。
- Acceptance evidence: 待记录精确 SHA 的多轮 aggregate path/SHA-256 与逐轮 capacity evidence。
- Not run: 待完成后填写。
- Known issues: P2-06/P2-07 仍未完成，Phase 2 保持 `in_progress`。

## Decision and discovery log

| 日期 | 类型 | 记录 | 影响/后续 |
| --- | --- | --- | --- |
| 2026-08-28 | discovery | 既有 enhanced capacity 只有单轮观测，明确 `production_slo=false`，但已具备完整隔离 Compose、fault、fairness 和 reconciliation 基础。 | 复用单轮 harness，通过外层串行多轮资格工具保持每轮故障覆盖与 cleanup。 |
| 2026-08-28 | decision | 本切片只做 P2-01，不把 Exporter/retention/backup 混入同一 commit。 | Phase 2 继续 `in_progress`，后续按 P2-06/P2-07 独立交付。 |
| 2026-08-28 | decision | 正式资格预登记 1 warm-up + 5 measured，并使用部署计时 `30/10/1s`；加速 `6/2/0.15s` 不作为正式证据。 | 真实候选运行约十余分钟；Hosted CI 不门禁绝对数值，只验证脚本和正确性。 |
| 2026-08-28 | discovery | 首次 clean-SHA warm-up 发现 qps 与 wall time 独立舍入后无法由序列化 evidence 精确复算，suite 在 0 measured 时 fail closed。 | 保留失败 aggregate；producer 改为先冻结 wall time、再计算 qps，并新增精确边界测试后重新提交与整套重跑。 |
| 2026-08-28 | discovery | 第二次 suite 的 warm-up 通过，但 Compose v5 为每个隔离 project 写入 project/service image labels，使完整 image ID 跨轮变化；RootFS layers 与只过滤这两个动态 labels 后的 config SHA 实际一致。 | 新增 ADR-0013；raw image ID 仅审计，跨轮锁定 RootFS+稳定 Config（含 Compose version）的 content SHA，保留第二份失败 evidence并再次整套重跑。 |
| 2026-08-28 | discovery | `dfa67ab` 的 v1 从零完成 1+5，六个 child 硬门禁均通过，但 measured-02 cold burst queue/execution/E2E p95 超过 `3/5/8s`，aggregate 仅 15/18。 | v1 保持 `unqualified`；保留 aggregate SHA `f993c11f…e3b2`，不删样本、不只重跑失败轮。 |
| 2026-08-28 | decision | v1 把 cold stop/start 与 steady backlog 混在一个 cell，且未证明两个 Worker 都 claim。 | 新增 ADR-0014/v2：warmed `3/5/8` 与 cold `6/8/10` 都是 AND 门禁，要求两个 distinct Worker、22/330/331 对账和项目镜像安全清理；新 SHA 全新 1+5。 |
