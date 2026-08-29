# 恢复本地数据并静默一键启动执行计划

- Owner: Codex
- Status: completed
- Created: 2026-08-30
- Updated: 2026-08-30
- Related phase: [Phase 2](../phases/PHASE-2-RELIABILITY.md)
- Worklog: [2026-08-30-restore-data-quiet-startup](../worklogs/2026-08-30-restore-data-quiet-startup.md)
- ADRs: 无；本次不改变数据库协议或应用日志安全合同，只恢复既有本地数据并调整开发启动器的输出去向。

## Context

当前默认 SQLite `backend/data/llmbenchlab.db` 已在上次兼容修复中重建到 Alembic head，但业务表为空。开发启动器 `scripts/dev.sh` 正在同时运行 API、Worker 与 Vite，并把三个子进程的全部 stdout/stderr 混合输出到当前控制台。仓库中存在多份迁移前 SQLite 一致性备份；必须从逻辑行数、完整性和 revision 识别真正含原数据的来源，不能仅按文件大小猜测。

## Objective

在保留当前新库和所有既有备份的前提下，把最近一份可验证的原始业务数据恢复到默认 SQLite 并迁移到当前 head；随后让 `make dev` 只显示简洁启动/退出状态，把三个服务的详细日志分别写入 Git 忽略的本地日志文件。

## Scope

- 只读核验候选 SQLite 备份、keyring 存在性和当前数据库占用者。
- 停止当前本地开发进程，制作可恢复的当前库快照，迁移旧库副本并原子替换默认 SQLite。
- 修改 `scripts/dev.sh` 的日志重定向、生命周期监管和失败提示；单独的 `make backend`、`make worker`、`make frontend` 保持流式诊断行为。
- 增加启动器回归测试并同步本地运行、恢复、安全和项目状态文档。

## Non-goals

- 不新增通用备份产品、日志轮转守护进程、日志聚合系统或生产依赖。
- 不改变应用 JSON 日志格式、级别、API、Benchmark 协议、数据库 schema 或迁移历史。
- 不恢复 `backend/data` 以外的数据库，不删除任何候选备份，不调用真实 Provider。

## Assumptions

- “原来的数据”指仓库默认本地 SQLite 在重建为空库前保存的业务数据；以候选备份的逻辑行数、最新时间、完整性和迁移可达性验证。
- 当前 `make dev` 的 API/Worker/Vite 是唯一默认库写入者；通过文件占用和端口检查确认并在替换前停止。
- 详细日志仍需可追查，因此写入本地 Git 忽略文件，而不是丢弃到 `/dev/null`。

## Requirements

- 遵守 `AGENTS.md` 的数据保护、迁移前备份、离线 Mock 测试、文档联动、阶段提交/push 和精确 SHA CI 门禁。
- 恢复后必须满足 SQLite `quick_check=ok`、零外键错误、Alembic current=head，且业务表计数与恢复源一致。
- `make dev` 正常运行期间控制台不得持续刷出 API/Worker/Vite 详细日志；子进程失败时必须返回非零并指出对应日志文件。

## Implementation steps

1. [completed] 核验备份、占用进程和启动器现状，确定唯一恢复源与最小输出行为。
   - Files/modules: `backend/data/*.bak`、`.secrets/credential-keys.json`（仅元数据）、`scripts/dev.sh`、相关文档/测试
   - Validation: 候选 revision、完整性、业务计数和文件占用有可复核结果，恢复源选择有明确理由。
2. [completed] 停止本地服务，在同目录准备并迁移恢复副本，验证后以可回滚的 rename 替换默认库。
   - Files/modules: Git 忽略的 `backend/data/llmbenchlab.db*`
   - Validation: 当前新库保留为明确命名备份；恢复后 head、完整性、外键与逻辑计数全部通过。
3. [completed] 实现静默 `make dev` 并增加回归测试。
   - Files/modules: `scripts/dev.sh`、`backend/tests/test_dev_script.py`
   - Validation: 正常启动只输出简洁摘要，三个日志文件接收详细输出，失败状态和清理行为有自动化覆盖。
4. [completed] 运行定向与全量验证，更新强制文档并完成审查、提交、push 与精确 SHA CI。
   - Files/modules: README、部署/运维/安全/测试、CHANGELOG、PROJECT_STATUS、PHASE-2、NEXT_TASK、本计划与工作日志
   - Validation: 本地验证通过、diff/秘密扫描通过、远程必需 jobs 对精确 SHA 全绿。

## Risks

| 风险 | 可能性/影响 | 预防措施 | 触发后的处理 |
|---|---|---|---|
| 选择到逻辑空备份 | 中/高 | 比较所有候选的表计数、revision、完整性和时间，不按体积选择 | 不替换当前库，继续保留并核对候选 |
| 活跃进程在替换时写库 | 已发生/高 | 替换前识别并停止占用默认库的 API/Worker，复核无 WAL/SHM 写入者 | 放弃替换并从保留快照回滚 |
| 旧数据迁移到 head 失败或漂移 | 低/高 | 只迁移同目录 staging 副本，运行受支持 preflight、upgrade、check 和内容对账 | 保持当前库不变，保存失败副本供诊断 |
| stored credential 与 keyring 不匹配 | 低/高 | 只检查 keyring 元数据/权限和 credential 行数，不输出任何密钥或 envelope | 数据可恢复但 stored 模式保持 fail closed，并明确记录需重新输入 Key |
| 日志静默掩盖启动失败 | 中/中 | 保留每服务独立日志，父进程返回子进程状态并显示日志路径 | 用户按提示查看对应日志；单服务 Make 入口仍流式输出 |

## Validation

| 验收项 | 命令或检查 | 预期结果 | 实际结果 |
|---|---|---|---|
| 恢复源可信 | SQLite 只读 revision/count/quick/FK/digest 核验 | 最新含数据候选完整且可迁移 | 通过：选定最新非空 `0002`，quick/FK 正常且与更早非空备份共有列摘要一致 |
| 恢复后数据一致 | SQLite count/digest、`alembic current/check` | 业务数据与源一致，revision 为 head | 通过：`1/1/15/1/15`，共有列 digest 一致，quick/FK/head/check 全绿 |
| 静默启动行为 | 定向 pytest + 有界 `make dev` 探针 | 控制台简洁、详细日志分流、服务可用 | 通过：`3 passed`；真实启动期间仅两行摘要，API/Web/恢复数据可读，详细日志 `0600` |
| 全量质量门禁 | `make test`、`make lint`、frontend build、`make smoke`、Compose config | 全部适用项通过 | 通过：backend `930 passed, 33 skipped`、frontend `38 passed`；lint/build/smoke/config 全绿，build 保留既有 chunk warning |
| 远程门禁 | push 后 GitHub Actions | 精确提交 4/4 必需 job 成功 | 通过：`5075bdb5e9b53f527a43e5aff7b7d2c7b48c5c9b` 的 [run 33265171953](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33265171953) 4/4 成功 |

## Rollback

数据库替换前已通过 SQLite backup API 把当前默认库保存为带 UTC 时间戳的同目录 `.bak`，且没有覆盖任何既有文件；若后验验证失败，停止服务后将恢复库另存，再由该备份恢复默认路径。代码回滚只需恢复 `scripts/dev.sh`；已有日志文件为 Git 忽略的本地诊断资产，不参与数据库事实或协议。

## Documentation updates

- [x] README / 用户操作说明
- [x] API / 数据格式 / Benchmark 协议（不适用：接口与协议不变）
- [x] Deployment / Operations / Security / Testing（恢复与日志边界）
- [x] CHANGELOG、PROJECT_STATUS、阶段文档、NEXT_TASK、工作日志

## Completion evidence

- Changed files: `scripts/dev.sh`、`backend/tests/test_dev_script.py`、README、Deployment/Operations/Security/Testing、CHANGELOG、PROJECT_STATUS、Phase 2、NEXT_TASK、本计划与工作日志；另有 Git 忽略的默认 SQLite 恢复和安全备份。
- Commands run: SQLite 只读审计/backup/staging migration/digest/head 检查；启动器定向 `3 passed`；完整 backend `930 passed, 33 skipped`、frontend `38 passed`；lint/build/smoke/Compose config；真实有界 `make dev` 与 API/Web 探针；commit/push 和精确 SHA CI 4/4。
- Acceptance evidence: 默认库 `1/1/15/1/15` 且共有列 digest 与源一致；quick/FK/head/check 通过；真实运行控制台仅两行摘要，日志分流为 `0600`，API/Web 可读恢复数据。
- Not run: 本机 capacity/SLO/完整 Phase 2 acceptance；本任务不改变其语义。远程 CI 已运行真实 PostgreSQL/Redis integration 和完整 Compose reliability acceptance 并成功。
- Known issues: 既有 Python 3.14 依赖 warning 和 Vite 662.39 kB chunk warning；2026-08-28 大备份逻辑为空但继续保留。

## Decision and discovery log

| 日期 | 类型 | 记录 | 影响/后续 |
|---|---|---|---|
| 2026-08-30 | discovery | 当前默认库和三份 96 MiB 迁移前备份业务表均为 0 行；2026-08-25/27 的较小备份仍含 1 Model、1 Benchmark、15 Questions、1 Run 和 15 Responses。 | 恢复源必须从较小含数据备份中按最新 revision/时间进一步核验。 |
| 2026-08-30 | discovery | 当前 API、Worker 和 Vite 仍在运行，API/Worker 持有默认 SQLite 文件。 | 恢复步骤开始前必须停止该开发进程组并再次复核占用。 |
| 2026-08-30 | decision | 选择 `backend/data/llmbenchlab.db.pre-alembic-20260827T073137431634Z.bak`：它是最新、revision 最高且逻辑非空的候选，SHA-256 为 `7e046c1e7cd4ec39c5fe6f57b34f130670e0d249a70bf052a84a23e085a59a53`；与两个更早非空备份在所有共有列上的内容摘要一致。 | 只在该文件的一致性副本上迁移；源文件保持只读不变。 |
| 2026-08-30 | discovery | staging 从 `0002` 顺序升级到 `0006` 后，五张原有业务表共有列摘要保持 `d1b3b74b7726f9e7903fbd3f445ad258d5f5aa4b885c976582f2d53e1d30302f`；完整性、外键、head、索引和 active-state 检查通过。 | 允许替换默认库。 |
| 2026-08-30 | action | 当前空库先通过 SQLite backup API 保存为 `backend/data/llmbenchlab.db.pre-original-data-restore-20260829T170121Z.bak`，SHA-256 `ec2ef8b2d5c9a338ce3e5f94c68a3c5742d288a798df2b7a6096960a48610c90`；随后用已验证 staging 同目录替换默认库。 | 空库及其 3 条 Worker process facts 可回滚；原恢复源未修改。 |
| 2026-08-30 | validation | 实现 commit `5075bdb5e9b53f527a43e5aff7b7d2c7b48c5c9b` 已 push；GitHub Actions run `33265171953` 的 frontend、backend、真实 PostgreSQL/Redis integration 和完整 Compose reliability 四个必需 job 全部成功。 | 远程门禁完成，任务可收尾。 |
