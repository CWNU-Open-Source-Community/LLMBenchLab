# 2026-08-30 — 多 Worker 并行评测工作日志

> 本日志记录实际发生的工作，不是事后美化的总结。所有命令以仓库根目录为基准。

## 元信息

- 日期：2026-08-30
- 执行者：Codex
- 关联阶段：[Phase 2 — Reliability](../phases/PHASE-2-RELIABILITY.md)
- 关联计划：[多 Worker 并行评测](../plans/2026-08-30-multi-worker-evaluation.md)
- 关联 ADR：[ADR-0005](../decisions/ADR-0005-durable-task-execution.md)、[ADR-0009](../decisions/ADR-0009-database-governance-audit-fair-scheduling.md)
- 最终状态：in_progress

## 初始仓库状态

- 当前分支：`codex/complete-evaluation-workflow`
- `git status --short --branch` 摘要：`## codex/complete-evaluation-workflow...origin/codex/complete-evaluation-workflow`，工作树干净
- 已有未提交改动：无
- 相关功能与测试现状：PostgreSQL/Redis/租约/fencing 已有双 Worker Mock acceptance/capacity 证据；`scripts/dev.sh` 与普通 `make docker-up` 均只启动一个 Worker
- 环境约束：当前个人 `.env` 使用 SQLite 且未配置 Redis；不得直接多开 SQLite Worker，不得改动当前 Run 或调用真实 Provider

## 本次目标与背景

当前一个长期 Provider SSE 请求会占住唯一 Worker，使其他 Benchmark Run 保持 pending。目标是在既有数据库租约架构上补齐可配置多 Worker启动与验证，让 PostgreSQL 模式下多个数据集 Run 可并行执行，并保持 SQLite 兼容路径安全失败。

## 范围

- 本地 PostgreSQL dev 多 Worker进程管理、独立日志与退出清理
- Compose 默认双 Worker与显式副本配置、expected/live gauges 同步
- 不同 Benchmark Run 的多 Worker领取回归
- README、架构、部署、测试、运维和状态文档同步

## 非目标

- 不支持 SQLite 多 Worker，不自动迁移当前 SQLite 数据
- 不改变数据库 schema、REST API、评分协议、Provider transport 或 governance policy
- 不重启当前用户服务、不取消/重试现有 Run、不访问真实 Provider

## 验收标准

- [x] PostgreSQL 本地 dev 可配置启动至少两个独立 Worker，且正确清理所有进程
- [x] SQLite 多 Worker与非法副本数在任何服务启动前失败
- [x] Compose 默认两个 Worker，按方向同步 API expected/scale，并自动验证 expected/registered/live/stalled/shortfall=`2/2/2/0/0`
- [x] 两个不同 Benchmark Run 可由不同 Worker并发领取，同一 Run 不会重复拥有有效 lease
- [x] 自动化只用 Mock/Stub，相关 lint/test/smoke/config 通过
- [ ] 强制文档、commit/push 与精确 SHA CI 完成

## 假设

- 多 Worker核心正确性已由 ADR-0005 和现有 PostgreSQL tests/acceptance 给出；本任务增加不同 Benchmark 和日常入口覆盖。
- 当前容量资格只覆盖两个 Worker，因此默认 2；更高数量允许显式配置但不宣称已资格。

## 风险

| 风险 | 影响 | 缓解措施 | 结果 |
|---|---|---|---|
| SQLite 被误用为多 Worker共享数据库 | 锁争用、不可支持的恢复语义 | 启动前方言检查并固定错误 | launcher 回归确认在 keyring/log/子进程前失败且不输出 DSN |
| Worker规模与 expected metrics 漂移 | shortfall 误报或漏报 | 同一副本参数驱动两者并做启动后检查 | fake/真实 Compose 均确认五 gauges 必须精确收敛 |
| Provider总并发/费用被放大 | 限流、费用和长尾增加 | 默认 2，记录 Worker×Run并发与治理边界 | 文档明确总并发近似 Worker×Run concurrency；真实 Provider 未运行 |

## 实施步骤

1. [completed] 完成只读勘察、计划、日志与测试合同
2. [completed] 实现本地/Compose 多 Worker启动入口
3. [completed] 增加 launcher、真实 PostgreSQL 与真实 Compose 回归
4. [in_progress] 同步文档、运行完整门禁、commit/push 并等待精确 SHA CI

## 实际修改

| 文件/模块 | 修改内容 | 对应需求/原因 |
|---|---|---|
| `docs/plans/2026-08-30-multi-worker-evaluation.md` | 新建复杂任务执行计划 | AGENTS/PLANS 强制流程 |
| `docs/worklogs/2026-08-30-multi-worker-evaluation.md` | 新建事实工作日志 | AGENTS 强制流程 |
| `Makefile`、`.env.example` | 暴露 `DEV_WORKERS` / `WORKERS` 入口与安全默认 | 日常可操作入口 |
| `scripts/dev.sh` | PostgreSQL 1–32 Worker进程、独立日志、expected、SQLite fail-fast、TERM cleanup | 本地开发并行执行与无遗留进程 |
| `scripts/compose_up.sh`、`compose.yaml` | 默认 2、方向化扩缩、active scan、API-only expected、五 gauges 有界门禁 | 落实 ADR-0016 且 fail closed |
| `backend/tests/test_dev_script.py`、`test_compose_up_script.py` | 假子进程/Docker、输入/环境/Make、SIGINT/TERM、扩缩顺序和失败路径 | 启动器离线回归 |
| `backend/tests/integration/test_postgres_leases.py` | 两个 Benchmark Run 的不同 owner 并发领取和同 Run lease 唯一 | 真实 PostgreSQL 正确性 |
| `README.md`、`docs/ARCHITECTURE.md`、`docs/DEPLOYMENT.md`、`docs/OPERATIONS.md`、`docs/TESTING.md` | 使用方法、执行槽、扩缩顺序、容量/迁移/Provider并发边界 | 用户与运维合同同步 |
| `CHANGELOG.md`、`docs/PROJECT_STATUS.md`、Phase 2、`docs/NEXT_TASK.md` | 进行中事实、证据和下一任务保持 | 仓库状态闭环 |

## 决定、偏差与发现

| 时间 | 类型 | 事实与理由 | 后续影响 |
|---|---|---|---|
| 15:00 CST | discovery | 当前个人模式为 SQLite + 单 Worker；底层 PostgreSQL/Compose 已有双 Worker资格 | 不重写调度核心，补齐入口与跨 Benchmark证据 |
| 15:00 CST | decision | 不直接 fork 多个 SQLite Worker | 多 Worker模式必须验证 PostgreSQL DSN |
| 15:00 CST | decision | 不新增 ADR | 既有 ADR-0005/0009 已明确批准受限 PostgreSQL 多 Worker，本任务不改变其不变量 |
| 15:05 CST | diagnosis | API gauges 为 pending/due/running=`3/3/1`；唯一 Worker仍 live/续租但最近题进度停滞，当前 Run 为 concurrency 4、read timeout 300 秒、最多 3 次 HTTP attempt | 单个上游长请求批次最坏约占用一个 Worker 15 分钟；多 Worker隔离执行槽，但不把总超时冒充已解决 |
| 15:25 CST | review | 初版 Compose只校验三 gauges、未按 ADR-0016 排序，超长十进制还可在 Bash 3.2 回绕 | 改为字符串 `1..32`、扩容 scan→API、缩容 API→Worker、最终 `N/N/N/0/0`；stale generation 不能通过 |
| 15:35 CST | review | 子进程可能继承忽略 SIGINT | 主 launcher 保留 130/143，cleanup 固定 TERM 并回归所有子进程终止 |
| 16:20 CST | review | 终审发现旧 stale generation 可误满足 scan count，且仅统计 running container 会误判含 exited replica 的缩容方向 | 改为应用 DB 时钟 watermark + fresh live scan 双门禁，并分别统计 all/running replica；新增两项回归，复审确认 0 Blocker/High/Medium |

## 实际运行命令

| 命令 | 目的 | 退出码 | 结果摘要 |
|---|---|---:|---|
| `git status --short --branch` | 初始状态 | 0 | 当前分支跟踪 origin，工作树干净 |
| `docker compose config --quiet`（只读勘察代理） | 现有 Compose 配置校验 | 0 | 配置可解析；默认仍为一个 Worker |
| 现有 acceptance/capacity 目标测试（只读勘察代理） | 核对双 Worker现有合同 | 0 | 127 项通过；未修改文件 |
| `cd backend && uv run pytest tests/test_dev_script.py tests/test_compose_up_script.py -q`（最终目标版本） | launcher/Make/fake Docker完整边界 | 0 | `42 passed`；仅既有 deprecation warnings |
| `bash -n`、ShellCheck、Ruff check/format | shell/Python静态检查 | 0 | 两 launcher 与两个目标测试文件通过 |
| 隔离 PostgreSQL 16 目标 lease tests（空库首次） | 真实方言并发 | 1 | fixture setup 两项 `UndefinedTable`；原因是临时库未先迁移，未进入测试断言 |
| 隔离 PostgreSQL 16 `alembic upgrade head` 后重跑相同 tests | 真实方言并发 | 0 | `2 passed`；临时容器按精确名称清理 |
| 隔离 Compose 冷启动（第一次） | 默认双 Worker真实入口 | 2 | 一个 Worker container exit 1，wrapper正确失败；trap cleanup C/V/N 全空，但容器日志随首次清理丢失，未把它写成通过 |
| 隔离 Compose 冷启动（第二次） | 复现并在失败时保留日志 | 0 | `expected/registered/live/stalled/shortfall=2/2/2/0/0`，随后精确清理 |
| 隔离 Compose `cold 2 → scale 1 → scale 2` | ADR-0016 方向化扩缩 | 0 | gauges 依次 `2/2/2/0/0`、`1/1/1/0/0`、`2/2/2/0/0`；cleanup C/V/N=`0/0/0` |
| 终审修复后隔离 Compose `cold 2 → scale 1 → scale 2` | fresh/watermark 与 all/running replica 真实验证 | 0 | gauges 再次为 `2/2/2/0/0`、`1/1/1/0/0`、`2/2/2/0/0`；唯一 project `llmbenchlab-mwfix-7f3a21` cleanup C/V/N/image tags=`0/0/0/0`；image tag 可由构建恢复 |
| `make lint` | Ruff/format、ESLint、TypeScript | 0 | 160 个 Python 文件、前端 lint/typecheck 全部通过 |
| `make test`（终审修复后重跑） | 完整后端/前端回归 | 0 | backend `1003 passed, 35 skipped`；frontend 10 files / `64 passed` |
| `make smoke` | 离线端到端链路 | 0 | 明确使用 Mock + 临时 SQLite；`1 passed, 7 deselected` |
| `cd frontend && npm run build` | production build | 0 | 2194 modules；构建通过，仅既有 >500 kB chunk warning |
| `docker compose config --quiet`、`git diff --check` | 部署/格式门禁 | 0 | 无输出、无 warning/whitespace error |
| `git diff --cached --check`、staged name/stat/diff 与三类秘密/debug 扫描 | 提交前范围与安全复核 | 0 | 19 files；secret/key patterns、敏感路径、debug marker 均为 0；无 unstaged diff |

## 测试结果

- 通过：launcher `42`、迁移后真实 PostgreSQL `2`、终审修复前后真实 Compose 冷启动与 `2→1→2` 扩缩；隔离资源均清理
- 失败：首次空 PostgreSQL 因未迁移 setup error，按部署顺序修正后通过；第一次真实 Compose冷启动有一个 Worker exit 1，wrapper安全失败，随后两次冷启动和完整扩缩均通过
- Lint/typecheck/build：完整门禁全部通过；build 仅有既有 chunk-size warning
- Smoke/Docker：真实 Compose只使用空 PostgreSQL/Redis且没有 Model/Run；未调用真实 Provider

## 未运行验证

- 精确 implementation/evidence commit 的远程 CI 尚未运行。

## 未完成项

- implementation commit/push 与精确 SHA远程门禁待完成。

## 已知问题与限制

- 当前个人 SQLite 中的 Model/Benchmark/Run 不会自动出现在新的 PostgreSQL Compose volume；迁移必须另选停写维护窗口执行现有 importer。
- 三个以上 Worker和真实 Provider容量未资格。

## 安全检查

- 真实密钥扫描：初始与 staged diff 均未发现；未读取 `.env` 内容或 keyring
- 真实 API 调用：否
- 日志/API 脱敏：未改变
- 危险 Git 操作（force push/reset 等）：无
- 阶段 push：待完成
- 远程 CI：待完成
- 遗留安全风险：现有可信 loopback与 Provider SSRF边界不变

## 结果与下一步

任务进行中；实现、完整本地门禁、staged diff 与秘密复核已完成，下一步提交、普通 push 并等待精确 SHA远程门禁。

## 最终 Git 状态

```text
任务进行中
```
