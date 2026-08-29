# 2026-08-30 — 恢复本地数据并静默一键启动工作日志

> 本日志记录实际发生的工作，不是事后美化的总结。所有命令以仓库根目录为基准。

## 元信息

- 日期：2026-08-30
- 执行者：Codex
- 关联阶段：[Phase 2](../phases/PHASE-2-RELIABILITY.md)
- 关联计划：[恢复本地数据并静默一键启动](../plans/2026-08-30-restore-data-quiet-startup.md)
- 关联 ADR：无；不改变 schema、协议或应用日志合同
- 最终状态：completed

## 初始仓库状态

- 当前分支：`codex/complete-evaluation-workflow`，跟踪 `origin/codex/complete-evaluation-workflow`
- `git status --short --branch` 摘要：任务开始时工作树干净
- 已有未提交改动：无；所有 SQLite/keyring/log 文件均为 Git 忽略的本地资产，恢复时不删除既有备份
- 相关功能与测试现状：默认 SQLite 已迁移到 `20260829_0006`，但核心业务表均为 0 行；`scripts/dev.sh` 把 API、Worker、Vite 的 stdout/stderr 直接混合到控制台
- 环境约束：macOS 本地工作区；当前 `make dev` 进程占用默认 SQLite 并监听 `127.0.0.1:8000`、`127.0.0.1:5173`；禁止真实 Provider 调用和秘密输出

## 本次目标与背景

用户重建数据库后可以启动项目，但原有本地数据不可见，同时一键启动在控制台持续输出大量服务日志。本次先恢复最近可验证的原数据，再让 `make dev` 保持简洁控制台输出且仍可从本地文件排障，然后停止，不扩展其他功能。

## 范围

- 核验并恢复默认本地 SQLite 数据，保留当前新库和所有旧备份。
- 调整 `scripts/dev.sh`，把三个服务详细日志分别写入 Git 忽略的本地文件。
- 增加最小自动化回归并同步强制文档。

## 非目标

- 不新增生产备份、日志采集/轮转基础设施或依赖。
- 不修改 API、Benchmark 协议、schema、应用 JSON 日志内容或单服务调试入口。
- 不删除任何数据库、备份、keyring 或用户 artifact。

## 验收标准

- [x] 默认 SQLite 恢复到 Alembic head，`quick_check` 为 `ok`、无外键错误，业务表计数/内容摘要与选定恢复源一致。
- [x] 当前重建空库以明确 UTC 时间戳备份保留，原始候选备份不被修改。
- [x] `make dev` 正常运行时不持续刷 API/Worker/Vite 详细日志，并清楚显示服务地址和日志位置。
- [x] 任一子进程退出仍会停止其余进程；失败时返回非零并指向详细日志。
- [x] 定向/全量测试、lint、build、离线 smoke 和适用部署检查有真实结果，文档与项目状态同步。
- [x] 阶段提交 push 到工作分支，对应精确 SHA 的 GitHub Actions 必需 jobs 全部成功。

## 假设

- “原来的数据”是默认 SQLite 重建前的最近逻辑非空备份；通过时间、revision、计数、完整性和迁移验证确定。
- 详细日志不能丢弃，默认写入仓库内 Git 忽略目录；单独的 Make 服务入口仍用于实时诊断。

## 风险

| 风险 | 影响 | 缓解措施 | 结果 |
|---|---|---|---|
| 恢复错库或空库 | 原数据仍不可见或被错误替换 | 全量枚举候选并比较逻辑计数、revision、时间和完整性 | 已选定最新非空 0002，API/Web 可读恢复数据 |
| 活跃写入导致不一致 | SQLite 损坏或丢写 | 停止占用者、同目录 staging、替换前后完整性检查 | 替换窗口无占用/sidecar，后验通过 |
| 迁移破坏旧数据 | 恢复数据漂移 | 只在副本迁移，保留源和当前库，按表计数/摘要对账 | 共有列 digest 前后一致，源与空库备份均保留 |
| 日志改动隐藏错误 | 启动失败难诊断 | 独立日志文件、退出码传播、简洁失败提示 | fake failure/cleanup 和真实启动探针通过 |

## 实施步骤

1. [completed] 只读核验候选备份、当前进程、keyring 边界和启动脚本。
2. [completed] 停止服务、准备/迁移/验真副本并可回滚替换默认库。
3. [completed] 实现静默启动与回归测试。
4. [completed] 运行完整验证、更新文档、审查、提交、push 并等待精确 SHA CI。

## 实际修改

| 文件/模块 | 修改内容 | 对应需求/原因 |
|---|---|---|
| `docs/plans/2026-08-30-restore-data-quiet-startup.md` | 建立可持续执行计划、恢复回滚和验收门禁 | 数据恢复跨持久层与启动器，需要明确保护边界 |
| `docs/worklogs/2026-08-30-restore-data-quiet-startup.md` | 记录初始状态、范围、风险与真实执行证据 | 仓库强制任务追踪 |
| Git 忽略的 `backend/data/llmbenchlab.db` | 从最新非空 0002 一致性备份副本迁移到 0006 后替换默认空库 | 恢复用户原有本地数据 |
| `scripts/dev.sh` | 三服务 stdout/stderr 分流到私有本地日志，控制台保留简洁摘要，失败状态继续传播 | 启动时不再持续刷屏且仍可排障 |
| `backend/tests/test_dev_script.py` | 用假进程覆盖日志 append/权限/分流、失败码和清理 | 新启动行为的离线回归 |
| `README.md`、`docs/DEPLOYMENT.md` | 说明安静组合启动、日志路径、跟踪方式和 override | 用户操作方式变化 |
| `docs/SECURITY.md`、`docs/OPERATIONS.md`、`docs/TESTING.md` | 记录私有日志安全边界、本地 SQLite 恢复纪律和测试覆盖 | 安全/恢复/验证联动 |
| `CHANGELOG.md`、`docs/PROJECT_STATUS.md`、`docs/phases/PHASE-2-RELIABILITY.md`、`docs/NEXT_TASK.md` | 同步已验证事实并明确不改变 P2-07/Phase 2 状态 | 仓库强制收尾 |

## 决定、偏差与发现

| 时间 | 类型 | 事实与理由 | 后续影响 |
|---|---|---|---|
| 00:xx CST | discovery | 默认库、两份 2026-08-28 备份及 2026-08-29 空库备份的业务表均为 0 行，不能按 96 MiB 文件大小认定含原数据。 | 继续核验更早且逻辑非空的候选。 |
| 00:xx CST | discovery | 2026-08-25/27 的三个迁移前备份均含 1 Model、1 Benchmark、15 Questions、1 Run、15 Responses；最新一份 revision 为 `20260825_0002`。 | 将对最新非空候选做内容摘要、状态与迁移演练。 |
| 00:xx CST | discovery | API/Worker/Vite 当前仍运行并占用默认库。 | 数据替换前停止整个本地开发进程组。 |
| 01:01 CST | decision | 选定最新非空且 revision 最高的 `backend/data/llmbenchlab.db.pre-alembic-20260827T073137431634Z.bak`；其 SHA-256 为 `7e046c1e7cd4ec39c5fe6f57b34f130670e0d249a70bf052a84a23e085a59a53`，与更早两份备份的共有列内容摘要一致。 | 原文件不修改，只复制到 staging 并向前迁移。 |
| 01:01 CST | action | 向已确认的开发进程组发送 SIGINT，父启动器按既有 cleanup 停止 API、Worker、Vite；随后文件占用、8000/5173 listener 与 WAL/SHM/journal 复核均为空。 | 默认 SQLite 已进入可安全离线替换窗口。 |
| 01:02 CST | validation | 首次 staging 后验 SQL 误把 reservation 列写成 `status`，SQLite 以 `no such column: status` 安全失败；检查 schema 后改用真实列 `state` 重跑。 | 该失败只发生在只读核验语句，未修改 staging；已在下条验证中纠正。 |
| 01:02 CST | validation | staging 完成 `0002→0003→0004→0005→0006`；五张旧业务表共有列摘要保持 `d1b3b74b7726f9e7903fbd3f445ad258d5f5aa4b885c976582f2d53e1d30302f`，quick/FK/head/三个修复索引通过，active Run/reservation 为 0。 | 证明迁移未改变原有业务内容且可离线替换。 |
| 01:03 CST | action | 当前空库通过 SQLite backup API 保存为 `backend/data/llmbenchlab.db.pre-original-data-restore-20260829T170121Z.bak`，SHA-256 `ec2ef8b2d5c9a338ce3e5f94c68a3c5742d288a798df2b7a6096960a48610c90`，quick/FK/head 均通过；已验证 staging 随后同目录替换默认库。 | 默认库恢复原数据；空库仍可恢复，源备份未改。 |
| 01:04 CST | implementation | 组合启动器保留应用 INFO 日志合同，把 API/Worker/Vite stdout/stderr 分别 append 到本地日志；控制台只显示地址/路径，子进程退出仍传播状态并清理其余服务。 | 不通过提高全局日志级别或丢弃 stderr 隐藏故障；单服务入口不变。 |
| 01:05 CST | validation | 首次从 `backend/` 运行定向组合命令时误写 `scripts/dev.sh`，`bash -n`/ShellCheck 因路径不存在失败；该命令未启用 `set -e`，后续 Ruff/pytest 仍通过。随后用 `set -euo pipefail` 和 `../scripts/dev.sh` 完整重跑，全项通过。 | 最终证据只采用纠正后的完整命令，同时保留首次操作偏差。 |
| 01:06 CST | validation | 真实 `make dev` 控制台只出现两行摘要；live/health/ready、Web 及 Model/Benchmark/Run 读取通过，重复请求后控制台无新增输出。发送 Ctrl-C 后 Make 报预期中断 130，所有服务/端口退出。 | 用户可正常启动且不再刷屏；详细日志仍可追查。 |
| 01:11 CST | review | 独立终审为 0 Blocker/High/Medium；指出 PROJECT_STATUS 仍写“等待完整测试”的 Low 文档残留，已改为只等待提交/push/CI。可信本地日志目录的同用户 symlink 边界为非阻断 Low，默认目录 `0700` 且威胁模型不承诺防同权限攻击者，本次不扩成安全文件安装器。 | 关闭文档不一致，保持最小范围。 |
| 01:19 CST | validation | 实现 commit `5075bdb5e9b53f527a43e5aff7b7d2c7b48c5c9b` 已普通 push；GitHub Actions run `33265171953` 的 frontend、backend、真实 PostgreSQL/Redis integration 和完整 Compose reliability 四个必需 job 全部成功。 | 精确实现 SHA 远程门禁完成。 |

## 实际运行命令

| 命令 | 目的 | 退出码 | 结果摘要 |
|---|---|---:|---|
| `git status --short --branch` | 确认分支和用户改动 | 0 | 工作树初始干净 |
| `sqlite3 -readonly <候选库> ...` | 核验 revision、quick check、外键和业务表计数 | 0 | 96 MiB 候选逻辑为空；三个早期候选逻辑非空且完整 |
| `lsof -nP backend/data/llmbenchlab.db` 及端口检查 | 识别数据库占用者 | 0 | API/Worker 持有 DB；API/Vite 分别监听 8000/5173 |
| `kill -INT -- -67299` 后复核文件/端口占用 | 优雅停止当前 `make dev` 进程组 | 0 | API、Worker、Vite 全部停止；默认库无占用、无 sidecar |
| staging SQLite backup + `prepare_migrations` + `alembic upgrade head/check/current` | 在副本上迁移原数据 | 0 | `0002→0006` 成功，Alembic 无待生成操作，current 为 head |
| staging 首次 active reservation 核验 | 检查迁移后无活跃外发状态 | 1 | 只读 SQL 使用了不存在的 `status` 列；未修改 DB，按 schema 改为 `state` |
| staging/common-column digest、quick/FK/count/index 核验 | 证明数据和 schema 可替换 | 0 | 摘要一致；`1/1/15/1/15`，active 为 0，三个修复索引存在 |
| SQLite backup API + 默认库替换 + 后验 digest/quick/FK/count/head | 可回滚恢复原数据 | 0 | 默认库已为 0006；核心计数 `1/1/15/1/15`，1 个 completed Run、15 Responses |
| `make migrate` | 用仓库受支持入口复核恢复后的默认库 | 0 | no-op 成功 |
| `bash -n scripts/dev.sh` 等（cwd=`backend`） | 首次定向检查 | 部分失败 | 路径写错导致 shell syntax/ShellCheck 未运行成功；Ruff 与 `3 passed` 继续执行，随后整体纠正重跑 |
| `set -euo pipefail; bash -n ../scripts/dev.sh; shellcheck ../scripts/dev.sh; uv run ruff ...; uv run pytest -q tests/test_dev_script.py` | 静默启动定向回归 | 0 | shell/Ruff/format 通过；`3 passed`，仅既有依赖 warning |
| 有界 `make dev` + curl/live/API/Web + Ctrl-C | 真实本地启动与恢复数据读取 | 预期中断 | 运行期间控制台仅两行；live/health/ready 正常，Web 200，Models/Benchmarks/Runs=`1/1/1`，日志 `0600`；退出后无占用 |
| `make test` | 完整离线回归 | 0 | backend `930 passed, 33 skipped`；frontend 9 files / `38 passed`；warning 均为既有依赖/迁移负向用例 |
| `make lint` | Ruff/format/ESLint/TypeScript | 0 | Ruff 154 files、ESLint、typecheck 全部通过 |
| `cd frontend && npm run build` | production build | 0 | 2192 modules，构建成功；保留既有主 chunk 662.39 kB warning |
| `make smoke` | 完全离线 Mock 垂直链路 | 0 | `1 passed, 7 deselected` |
| `docker compose config --quiet` | 部署配置语法 | 0 | 通过，未启动容器 |
| `alembic current && alembic check` + 默认库 quick/FK/count | 恢复后最终 schema/data 复核 | 0 | `0006 (head)`、无新操作、quick ok、FK 0、`1/1/15/1/15` |
| 真实启动后的共有列 digest/Worker process 核验 | 确认启动探针未改历史业务数据 | 0 | digest 仍为 `d1b3b74b…0302f`；新增 1 条正常 stopped Worker process，active 为 0 |
| `git commit` + `git push origin codex/complete-evaluation-workflow` | 提交并推送实现阶段 | 0 | commit `5075bdb5e9b53f527a43e5aff7b7d2c7b48c5c9b` 已推送 |
| `gh run watch 33265171953 --exit-status` | 等待精确实现 SHA 远程门禁 | 0 | 四个必需 job 全部 success；仅现有 GitHub Actions Node runtime deprecation 注解 |

## 测试结果

- 通过：启动器 `3 passed`；完整后端 `930 passed`，前端 `38 passed`
- 失败：最终验证 0；曾有一次只读 reservation 列名错误和一次检查路径错误，均已记录并完整纠正重跑
- Lint/typecheck/build：Ruff/format 154 files、ESLint、TypeScript、Vite build 全部通过；保留既有 662.39 kB chunk warning
- Smoke/Docker：离线 Mock smoke `1 passed, 7 deselected`；Compose config 通过且未启动容器
- 远程 CI：frontend、backend、真实 PostgreSQL/Redis integration、完整 Compose reliability 4/4 success

## 未运行验证

- 本机 capacity、SLO 和完整 Phase 2 acceptance：未运行；本次不改变其语义。远程 CI 已执行真实 PostgreSQL/Redis integration 与完整 Compose reliability acceptance 并成功。

## 未完成项

- 无；约定范围、实现/文档、本地验证、实现提交/push 与精确 SHA CI 均已完成。

## 已知问题与限制

- 2026-08-28 的两份 96 MiB 备份逻辑为空且主要为空闲页，保留但未作未授权取证；本次恢复的是最新可验证的逻辑非空备份。

## 安全检查

- 真实密钥扫描：未暂存与 staged diff 的高置信模式扫描、`git diff --check` 均通过；核验未读取或输出 keyring 内容、credential envelope 或 Provider Key
- 真实 API 调用：否
- 日志/API 脱敏：应用日志合同不变；组合启动详细输出保留到 Git 忽略目录，目录/文件收紧为 `0700/0600`
- 危险 Git 操作（force push/reset 等）：无
- 阶段 push：`origin/codex/complete-evaluation-workflow`，实现 commit `5075bdb5e9b53f527a43e5aff7b7d2c7b48c5c9b`，成功
- 远程 CI：精确实现 SHA 的 [run 33265171953](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33265171953) 4/4 必需 job 成功
- 遗留安全风险：数据库与 keyring 必须配对恢复；若存在 stored credential 且 keyring 不匹配，应保持 fail closed

## 结果与下一步

任务完成：默认本地 SQLite 已恢复原 Demo 数据，组合启动控制台保持简洁且详细日志可追查，全部本地和远程门禁通过。[NEXT_TASK.md](../NEXT_TASK.md) 仍指向原定 P2-07 最小 verifier，本维护不改变路线图。

## 最终 Git 状态

```text
## codex/complete-evaluation-workflow...origin/codex/complete-evaluation-workflow
```
