# 多 Worker 并行评测执行计划

- Owner: Codex
- Status: active
- Created: 2026-08-30
- Updated: 2026-08-30
- Related requirements: FR-RUN-04、NFR-PERF-03、Phase 2 P2-03
- Related phase: [Phase 2 — Reliability](../phases/PHASE-2-RELIABILITY.md)
- Worklog: [2026-08-30 multi-worker evaluation](../worklogs/2026-08-30-multi-worker-evaluation.md)
- ADRs: [ADR-0005](../decisions/ADR-0005-durable-task-execution.md)、[ADR-0009](../decisions/ADR-0009-database-governance-audit-fair-scheduling.md)；不新增 ADR，本任务只暴露既有 PostgreSQL 多 Worker 决定，不改变数据库事实、租约或协议语义

## Context

当前 Runner、数据库租约、fencing、Redis consumer group 与 Worker ID 已支持多个独立 Worker 竞争不同 Run，并在真实 PostgreSQL/Redis 双 Worker Mock 验收中通过。日常入口仍把 `make dev` 固定为一个 SQLite Worker，`make docker-up` 也没有传递 Compose `--scale`，所以一个长期 Provider 请求会占住唯一 Worker，使其他 Benchmark Run 只能排队。

## Objective

提供可配置且可观测一致的多 Worker 启动入口，使 PostgreSQL 模式下至少两个不同 Benchmark Run 能由不同 Worker 同时执行；SQLite 继续在启动前拒绝多 Worker，避免产生虚假的并发安全保证。

## Scope

- 扩展本地开发启动器，在 PostgreSQL DSN 下管理 1–32 个独立 Worker 进程、独立私有日志、信号转发与退出清理。
- 增加 Compose 多 Worker 启动包装器，默认两个 Worker，并以同一副本数设置 `worker_expected_processes`、执行 `--scale` 和启动后 gauges 校验。
- 更新 Make 入口、环境变量示例、部署/架构/测试/运维说明和阶段状态文档。
- 增加启动器回归，以及真实 PostgreSQL 下不同 Benchmark Run 的并发领取证据。

## Non-goals

- 不让 SQLite 支持多 Worker；现有 SQLite 数据迁移仍使用 stopped-source、empty-target 的显式 importer。
- 不修改 Run/Response schema、REST API、`llmbenchlab-protocol-v1`、Provider 请求语义或治理限额。
- 不把双 Worker Mock 证据外推为三个以上 Worker、真实 Provider、HA 或生产 SLA。
- 不自动停止当前用户进程、取消 Run、迁移当前 SQLite 数据或调用真实 Provider。

## Assumptions

| 假设 | 依据 | 验证方法 | 不成立时的处理 |
|---|---|---|---|
| PostgreSQL 租约已支持不同 Worker 并发领取不同 Run | ADR-0005、Phase 2 acceptance/capacity | 真实 PostgreSQL integration 回归 | 若失败则停止启动层交付并修复租约竞争 |
| 当前问题来自入口固定单 Worker，而非 API 限制 | `scripts/dev.sh`、Makefile、Compose 默认配置 | 启动脚本测试与 Compose config | 若存在额外串行门禁，补充最小实现和测试 |
| 两个 Worker 是当前唯一经过容量资格的默认值 | P2-local-control-plane-v2 | 保留默认 2 并记录更高规模限制 | 不把 3+ 标记为已资格 |

## Requirements

- [x] PostgreSQL 下 `make dev DEV_WORKERS=2` 启动两个独立 Worker，任一子进程退出时其余服务被清理并传播退出码。
- [x] SQLite 下请求两个以上 Worker 必须在启动任何服务或创建日志前稳定失败，且不输出 DSN。
- [x] `make docker-up` / `make dev-multi` 默认启动两个 Worker，允许显式 `WORKERS=N`，按 ADR-0016 的扩/缩顺序保持 scale 与 API expected 一致，并验证 expected/registered/live/stalled/shortfall=`N/N/N/0/0`。
- [x] 多 Worker 不允许同一 Run 出现两个有效 lease；不同 Benchmark Run 可由不同 owner 并发领取。
- [x] 自动化只使用 Mock/Stub；不读取真实 Key、不调用真实 Provider。
- [x] 文档明确总 Provider 并发约为 Worker 数 × Run 内并发，并仍受 governance policy 与数据库连接容量约束。

## Implementation steps

1. [completed] **冻结启动合同与失败边界**
   - 修改范围：计划、工作日志、启动器测试设计。
   - 操作：复用 ADR-0005 的 PostgreSQL 多 Worker边界；确定本地/Compose 参数、上限、日志与 gauges 合同。
   - 完成判据：计划和工作日志记录明确，测试先覆盖非法输入与进程管理。
2. [completed] **实现本地与 Compose 多 Worker 启动**
   - 修改范围：`scripts/dev.sh`、Compose 包装脚本、`Makefile`、`compose.yaml`、`.env.example`。
   - 操作：管理多个独立 Worker，校验数据库方言/副本数，同步 expected，启动后验证 live/shortfall。
   - 完成判据：目标脚本测试、`bash -n`、Compose config 通过。
3. [completed] **补充并发正确性回归**
   - 修改范围：后端 Worker/租约或 PostgreSQL integration 测试。
   - 操作：验证两个不同 Benchmark Run 被不同 Worker领取且无同 Run重复 owner。
   - 完成判据：目标后端测试通过；真实基础设施测试在可用环境执行并记录。
4. [in_progress] **同步文档并完成门禁**
   - 修改范围：README、Architecture、Deployment、Operations、Testing、Changelog、Project Status、Phase 2、Next Task、本计划与工作日志。
   - 操作：记录命令、支持边界、回滚与实际验证；运行相关完整门禁。
   - 完成判据：lint/test/smoke/build/config 与精确提交远程 CI 符合仓库 DoD。

## Risks

| 风险 | 可能性 | 影响 | 预防措施 | 触发后的处理 |
|---|---|---|---|---|
| 多开 SQLite Worker造成锁竞争或重复副作用 | 中 | 高 | 方言检查在子进程/日志创建前 fail-fast | 保持单 Worker并提示使用 PostgreSQL |
| scale 与 expected 不一致造成监控误报 | 中 | 中 | 单一副本参数同时驱动环境与 `--scale`，启动后校验 gauges | 启动命令失败并保留栈供诊断 |
| 任一 Worker退出后遗留同组进程 | 中 | 高 | 数组化 PID 管理、统一 TERM/wait、信号回归 | 失败即终止整个本地 dev 会话 |
| Worker 数 × Run 并发放大 Provider成本 | 中 | 高 | 默认只用已资格的 2 Worker，文档强调治理与成本上界 | 要求操作者设置 policy/Run concurrency |
| 当前 SQLite 数据与新 PostgreSQL 栈分叉 | 高 | 中 | 不自动迁移，文档指向显式 stopped-source importer | 用户决定维护窗口后另行迁移 |

## Validation

| 验收项 | 命令/检查 | 预期结果 | 实际结果与证据 |
|---|---|---|---|
| 本地多进程启动器 | `cd backend && uv run pytest tests/test_dev_script.py` | 多 Worker/失败/清理回归全部通过 | 与 Compose 包装器合并目标套件 `42 passed`；含 SIGINT→TERM 全清理、显式空 Make 参数、stale generation 与 exited replica |
| Compose 启动包装器 | 目标脚本测试、`docker compose config --quiet` | 默认 2、显式 N、非法输入和 gauges 同步通过 | fake Docker目标套件包含扩/缩顺序、scan、五 gauges、stale/超时/上游失败；隔离真实 Compose 冷启动 `2/2/2/0/0`，随后 `2→1→2` 得到 `1/1/1/0/0` 与 `2/2/2/0/0`，cleanup C/V/N=`0/0/0`；config 通过 |
| 租约并发 | PostgreSQL integration 目标测试 | 不同 Benchmark Run 不同 owner；同 Run 唯一 lease | 首次空库未迁移在 fixture setup 报两项 `UndefinedTable`；迁移到 `20260830_0007` 后相同两项 `2 passed` |
| 质量门禁 | `make lint && make test && make smoke` | 全部通过且无真实 Provider | 终审后重跑：lint/typecheck 通过；backend `1003 passed, 35 skipped`、frontend `64 passed`；offline Mock smoke `1 passed, 7 deselected`；frontend build 与 Compose config 通过 |
| 秘密与无关改动检查 | `git diff --check`、`git status --short` 及敏感词检查 | 无格式错误、无密钥、范围正确 | 19-file staged diff/范围已复核；secret/key、敏感路径、debug marker 三类扫描均为 0；`git diff --cached --check` 通过 |

## Rollback

本任务不迁移 schema 或用户数据。回退启动器/Make/Compose/文档即可恢复单 Worker默认；停止多 Worker时使用现有 SIGTERM grace，未完成 Run 保留数据库 lease 并由剩余 Worker或自然过期恢复。不得用删除 PostgreSQL volume 回退。

## Documentation updates

- [x] README / 用户操作说明
- [x] Architecture / Deployment / Operations / Testing
- [x] CHANGELOG、PROJECT_STATUS、Phase 2、NEXT_TASK、工作日志（远程证据将在 closeout commit 补齐）
- [x] ADR：不新增；复用 ADR-0005/0009，原因见上文
- [x] API / Benchmark protocol / Security：接口、评分和秘密边界不变

## Completion evidence

- 修改文件：Make/环境示例/Compose、本地与 Compose launcher、launcher/真实 PG tests、README 与 Phase 2 运维/部署/测试/状态文档
- 实际命令：目标 launcher `42 passed`；迁移后真实 PG `2 passed`；终审修复后的真实 Compose 冷启动与 `2→1→2` 五 gauges 通过且隔离 cleanup 为零；终审后完整 lint/test/smoke/build/config/diff check 通过
- 验收对应：本地/Compose入口、SQLite fail-fast、按方向 scale/expected、active scan、五 gauges 与跨 Benchmark lease 已有本地证据
- 未运行：精确 implementation SHA 的远程 CI
- 已知问题：SQLite 继续单 Worker；3+ Worker和真实 Provider尚未资格

## Decision and discovery log

| 日期时间 | 类型 | 记录 | 影响/后续 |
|---|---|---|---|
| 2026-08-30 15:00 CST | discovery | 核心租约与 Compose 已支持多 Worker；日常启动入口固定为一个 Worker | 实现聚焦启动/配置/验证，不重写 Runner |
| 2026-08-30 15:00 CST | decision | SQLite 保持单 Worker；多 Worker只在 PostgreSQL 下启用 | 本地启动器对 SQLite fail-fast，Compose 作为默认多 Worker入口 |
| 2026-08-30 15:00 CST | decision | 默认副本数为已通过资格的 2，而非未测量的更高数量 | 允许显式 N，但文档不外推容量结论 |
| 2026-08-30 15:25 CST | review | 初版包装器只检查三项 gauges、单次同时 scale/API，且超长数字可触发 Bash 回绕 | 按 ADR-0016 改为扩容 Worker scan→API、缩容 API→Worker，并验证 `N/N/N/0/0`；字符串校验先于算术 |
| 2026-08-30 15:35 CST | review | 后台子进程可能忽略 SIGINT，转发 INT 后无界 wait | launcher 对主进程保留 130/143，但一律用 TERM 清理全部子进程，并新增忽略 INT 回归 |
| 2026-08-30 16:20 CST | review | scan gate 可计入 stale generation，且只看 running container 会把含 exited replica 的缩容误判为非缩容 | 使用应用 DB 时钟 watermark 与 fresh scan 双门禁；方向同时统计 all/running replica，增加两个回归并由独立复审确认 0 Blocker/High/Medium |
