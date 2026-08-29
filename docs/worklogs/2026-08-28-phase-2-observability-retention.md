# 2026-08-28 — Phase 2 可观测性与审计保留工作日志

## 元信息

- 日期：2026-08-28
- 执行者：Codex
- 分支：`codex/complete-evaluation-workflow`
- 初始 HEAD：`7cb44bfda2b0e53f88412a5346a0705799174df6`
- 关联阶段：[Phase 2 — Reliability](../phases/PHASE-2-RELIABILITY.md)
- 关联计划：[Phase 2 可观测性与审计保留执行计划](../plans/2026-08-28-phase-2-observability-retention.md)
- 决策基础：[ADR-0005](../decisions/ADR-0005-durable-task-execution.md)、[ADR-0009](../decisions/ADR-0009-database-governance-audit-fair-scheduling.md)、[ADR-0010](../decisions/ADR-0010-phase-2-governance-delivery-boundaries.md)
- 本轮决策：[ADR-0015](../decisions/ADR-0015-observability-worker-progress-audit-retention.md)
- 当前状态：`completed`；P2-06 implementation、clean-SHA、evidence-doc 与各自精确 SHA CI 均通过；Phase 2 仍为 `in_progress`

## 目标与背景

P2-01 已在精确提交与远程 CI 上闭环，现有系统也已经提供 PostgreSQL 派生的 `/tasks/metrics`、同一读快照内验证审计完整性的 `/tasks/history`、稳定分页的 Run audit、应用 JSON 日志和依赖 capability probe。本任务完成 P2-06：给这些事实增加低基数、fail-closed 的 scrape 出口和告警规则；增加由数据库 UTC 记录的 Worker 主循环进展，使依赖探针不再被误作执行活性；交付显式、可校验、可恢复且默认不删除的 audit retention 维护流程。

本切片完成后仍不宣称生产监控平台、不可篡改审计、HA 或灾难恢复完成。PostgreSQL 与数据库外 keyring 的配对 backup/restore、Redis 重建和完整故障矩阵保留给独立 P2-07。

## 初始状态与已保护边界

- 开始时工作树干净，当前分支与 `origin/codex/complete-evaluation-workflow` 同步。
- 当前公开 API 没有认证，只允许可信 loopback；新增 exporter 与 Worker 状态不得扩大公网支持边界。
- 数据库仍是任务、租约、审计和进展时间的唯一权威；Redis、进程内计数和 exporter 都不是第二事实来源。
- 自动化只可使用 Mock/Stub，不调用真实或付费 Provider。
- 审计 archive 不得包含 Key、Authorization、Cookie、credentialed DSN、ciphertext、nonce、URL、题目、Prompt、Response 正文或 Provider raw body，也不得称为 WORM。
- 不修改 `llmbenchlab-protocol-v1`、Benchmark schema、评分或排行榜隔离。

## 范围

- Prometheus 文本 exposition 的受控 exporter：复用现有 DB gauges、typed counters、Run latency 与 Worker progress，固定指标名、固定 label 集合、采样窗口和错误语义。
- 低基数告警规则与 Runbook：backlog、dead-letter、governance integrity、overdraw、queue degraded、Worker stalled 与恢复时长；记录 severity、`for`、silence 和响应动作。
- Worker 主循环 DB-time progress：进程注册、最近 scan/claim/progress/heartbeat、停止与 stale 判定；依赖 probe 继续只表示 capability。
- Audit retention 维护工具：严格 archive schema、原子写、内容 hash/rollup、独立 verify、幂等 restore、显式 delete-after-verify 与 commit outcome 边界。
- Alembic/SQLite→PostgreSQL importer、API/schema、Compose/config、测试与所有相关文档联动。

## 非目标

- 不交付 Prometheus server、Alertmanager、OTel collector、通知发送器或公网监控。
- 不交付生产 HA、PITR、RPO/RTO、PostgreSQL+keyring 完整 backup/restore 或 Redis 灾备；这些属于 P2-07。
- 不新增真实 Provider 指标、Model/Run/question/provider/worker ID label 或用户控制字符串 label。
- 不自动从 API 请求链路清理 audit，不把 archive 上传到对象存储，也不宣称不可篡改存储。
- 不提前进入 Phase 3。

## 验收标准

1. Exporter 只输出固定低基数指标；数据库或 retained audit 完整性失败时整个 scrape 失败且不回显损坏值，不返回部分成功。
2. 告警规则可由测试解析，全部引用已交付指标，并对持续时间、级别、静默和 Runbook 给出确定合同。
3. Worker 主循环事实全部使用数据库 UTC，scan/claim/progress 与 lease heartbeat 明确分离；stalled 判断能识别“依赖正常但主循环不推进”。
4. Retention archive/verify/restore/delete 在 SQLite 与 PostgreSQL 语义一致，默认只归档不删除；删除前必须验证同一 archive、稳定 snapshot 与 cutoff，commit unknown 不可盲目重试。
5. Archive 与 exporter 的 schema、日志、错误和证据均通过秘密/高基数审查；无真实 Provider 调用。
6. 目标测试、全量测试、lint、smoke、迁移、Compose/规则自检通过；独立 commit push 后该精确 SHA 的四个必需 CI job 全绿。

## 假设

- Prometheus 文本 exposition 可用项目内小型固定 renderer 实现，无需新增生产依赖；若勘察证明不安全，将在 ADR 中记录依赖取舍。
- Worker progress 需要新的持久表和 Alembic revision；旧数据库在 upgrade 后不回填虚构进展。
- Audit archive 是内部敏感运维文件；公开 API 不直接提供下载或删除能力。
- Retention cutoff 由数据库 UTC 产生，archive 以 `expires_at < cutoff` 的稳定有界集合为目标；边界细节由 ADR 冻结。

## 风险

| 风险 | 影响 | 预防与处置 |
| --- | --- | --- |
| Exporter 每次抓取放大 DB 压力 | 高频 scrape 影响 Worker | 固定窗口/样本上限、最小 scrape 间隔文档、单快照查询、失败不重试风暴 |
| label 或 archive 泄露身份/秘密 | 长期外泄 | 指标无对象 ID label；archive 严格 allowlist、权限与原子写；错误固定化 |
| Worker progress 写放大或幽灵在线 | 错告警/数据库压力 | DB UTC、节流 upsert、进程启动/停止事实、stale 阈值与清理策略 |
| archive 后删除竞态或 commit unknown | 证据丢失/重复操作 | 归档、验证、删除分阶段；snapshot/cutoff/hash 绑定；删除事务与不确定结果 fail closed |
| 与 P2-07 交叉扩大范围 | 难审查/虚假完成 | 本切片只验证 audit archive 自身恢复；整库与 keyring 恢复另立任务 |

## 实施步骤

1. 完成现有 API、Worker、审计、migration/importer 与运维文档勘察。
2. 新增 ADR-0015，冻结 exporter、Worker progress、alert 与 retention/archive 合同。
3. 实现 schema/migration、Worker progress repository/接线与读 API/exporter。
4. 实现规则文件、Runbook 与规则校验测试。
5. 实现 audit archive/verify/restore/delete CLI 及双方言/失败路径测试。
6. 更新 importer、Compose/config、API、安全、架构、部署、测试和运维文档。
7. 运行定向、全量、lint、smoke、migration、Compose 和安全门禁；修复终审问题。
8. 更新状态文档，形成独立 commit，push 并等待精确 SHA CI。

## 初始勘察事实

- `GET /api/v1/tasks/metrics` 已有当前 DB gauges；`GET /api/v1/tasks/history` 已在一致读快照中验证 retained audit 后输出 typed counters 与三类 Run latency。
- 当前文档明确写明“没有 Prometheus exporter、告警发送器或 Worker 主循环 liveness”；`worker_probe` 只检查 DB/head/queue capability。
- `audit_events` 已有 `retention_class`、`occurred_at` 与 `expires_at` 及 expiry 索引，但没有 archive/verify/delete/restore 维护命令。
- 本任务开始尚未修改生产代码、schema 或公共 API。

## 已实现结果（尚待仓库级门禁）

### Worker progress、migration 与 importer

- 新增 Alembic `20260828_0005` 与 `worker_processes` generation 表，字段/约束覆盖 DB UTC start/seen/scan/claim/progress/lease-heartbeat/stop；辅助索引为 `(stopped_at,last_seen_at,generation_id)`，audit 另增加 `(expires_at,id)` 和 `(occurred_at,id)` 有界扫描索引。
- 长运行 `WorkerService.run()` 在执行前注册 generation，按真实 event bit 最多每五秒合并一笔短事务；timer 无 event 时零写入，提交失败保留 bit，graceful stop 原子写 pending event 与 stop，stopped generation 的 late flush 被 CAS 拒绝。
- Runner/Worker 已在 claim、scan、Response insert、durable transition、reaper progress 与 lease renewal 的提交后边界接线；`run_once()`、probe 和其他短命 CLI 使用 null observer，不虚构长运行 generation。
- `/tasks/metrics` 增加 expected/registered/live/stalled/shortfall/stale-after 与五个最近 DB-time 聚合；边界 `last_seen_at == cutoff` 为 live，响应不含 generation/worker ID。probe 固定增加 `probe_scope=dependencies_only`、`main_loop_progress=not_checked`。
- SQLite→PostgreSQL importer 从历史 12 表扩展为当前 13 表；live generation 在打开目标前 fail closed，stopped/stale rows 可精确复制。`0005 -> 0004` 在 `worker_processes` 非空时于 DDL 前拒绝，空表才允许进入原 `0004` guard。
- Importer 完整性终审把 committed target 的 postverify 收敛到同一 canonical count/PK/content contract，避免仅凭行数或事务前快照接受目标事实漂移。

### Exporter、规则与日志

- 新增 `GET /api/v1/metrics/prometheus`，固定 Prometheus text `0.0.4`、全部 gauge、固定 enum label 与顺序；current/governance/Worker、15 分钟 typed-audit、1 小时 Run latency 共用一个 DB-time 读快照，Redis 只作快照外非权威 observation。
- audit 最多读取 `50,001` 行并在超限时整次 `503`；每类 latency 最多 `10,001` 样本并显式报告 truncated；数据库、retained audit 或 renderer 损坏均整次 fail closed，不返回 last-good cache 或部分 exposition。
- collection owner 持有进程内 single-flight gate，request cancellation 不能取消同步 DB thread 或提前释放 gate；重叠 scrape 立即 `429`。
- `deploy/observability/` 已加入固定八条 Prometheus rules 与 `60s` scrape 示例；规则只有固定 `severity`/`component` labels、Runbook/silence annotations，不加入 Prometheus/Alertmanager 服务或 sender。
- 生产 logger 调用由 AST 回归限制为无格式参数字面量，并静态核对登记 logger 与 literal event/result/error code 全集；JSON formatter 对每个 allowlisted extra 执行固定 enum、canonical UUID/Redis stream ID、HTTP method/route 与有限数值规范化，非法 ID/数值省略，未知字符串固定为 `unsupported`。Redis publish/delivery 同时拒绝非 UUID Run/correlation identity；外部 logger 的动态消息/identity 固定化且不能通过伪造 extra 注入，raw Uvicorn access handler 关闭。

### Audit retention

- 收敛公共 retained-row read validator，API/history/exporter/archive/restore 对 event contract、原始 canonical payload、hash、identity、retention interval、timestamp 与 finite numeric 使用同一严格合同。
- 新增 `llmbenchlab-audit-retention archive|verify|reconcile|restore|delete`。archive 在一个 DB snapshot 取 cutoff，只归档 `expires_at < cutoff` 且最多 10,000 条，默认不删除。
- `llmbenchlab-audit-archive-v1` 使用 canonical UTF-8 JSONL、完整行事实、rollup、domain-separated content hash 与整文件 SHA-256；在逐行 decode 前执行全局行数上限，并拒绝超限、duplicate key、非 canonical JSON、重复/乱序事实、未知 head、FIFO/非普通文件、symlink/非 owner/过宽权限或可由 group/other 写入的父目录。
- 输出以同目录 `0600` 临时文件、fsync、no-replace install 和父目录 fsync 完成；verify/delete/restore 从同一个 descriptor 解析。hash 只表示误改检测/精确绑定，不是签名或 WORM。
- delete/restore 要求用户确认同一 archive digest，只处理 archive 中的精确事实；冲突、缺引用或 mixed state fail closed。PostgreSQL mutation 保持 advisory/row lock，零行 no-op 也执行 postverify；exit `3` 表示提交后验证失败，exit `4` 表示 commit outcome unknown，二者都要求先 `reconcile` 而非盲重试。
- `app.db` 与 `app.governance` package runtime exports 改为 lazy；fresh process `verify` 在无效数据库配置下仍不导入/构造 engine，也不创建数据库父目录，保持真正离线。

### 状态边界

- 以上功能已进入 clean implementation commit `9a20676dcf545040782f04c166205d0043345753`。lint/test/smoke/integration/migration/build/config/rules、clean-SHA capacity/9/9 acceptance、push 与该实现 SHA 的 GitHub Actions 4/4 均已通过。staged security review 发现的 structured logging High 与 `python -m app.worker` logger Medium 已修复，最终技术/安全终审为 0 Blocker/High/Medium。Evidence-doc commit `ec2959680459a14aa308bd4d9ebcc6bb7bfcf3a6` 及其自身精确 SHA CI 也已通过，P2-06 仓库级状态为 `completed`。
- P2-06 archive restore 只恢复 typed audit archive 自身。PostgreSQL+keyring 配对 backup/restore、Redis 重建、Worker 扩缩/告警处置与剩余故障矩阵仍属于 P2-07；Phase 2 继续 `in_progress`。

## 实际命令与结果

| 命令/检查 | 结果 |
| --- | --- |
| `git status --short --branch` | 工作树干净；分支与 origin 同步 |
| 阅读 README、AGENTS、PROJECT_STATUS、ROADMAP、Phase 2、NEXT_TASK、PLANS、ADR-0005/0009/0010 与相关 API/Security/Testing/Operations/Deployment/Architecture | 已完成初始只读勘察；实现前继续核对具体模块 |
| 三路只读设计审查：exporter/alerts、Worker progress、audit retention | 已完成；未修改生产代码；结论已冻结进 ADR-0015 |
| 全生产 logger source 静态勘察 | 应用 logger 共 47 个调用；现有消息为固定模板，但第三方 logger/Uvicorn access 可绕过应用字段 allowlist；已纳入 formatter 与 AST 回归范围 |
| Worker progress/API/runner/migration/importer 目标 pytest 与 Ruff 批次 | 实现过程中已执行并完成；随后由合并定向、全量与真实 integration 门禁覆盖 |
| Prometheus exporter/rules、audit archive/retention/CLI、logging 目标 pytest 与 Ruff 批次 | 实现过程中已执行并完成；取消竞态、离线 verify 和 audit scan index 审查修正后已加入回归，随后由合并定向/全量门禁覆盖 |
| Structured logging High / Worker entrypoint Medium 修复门禁 | 应用 logger/message/event/result/error code 全集静态登记，`python -m app.worker` 真实入口固定使用已登记 `app.worker` logger；HTTP method、UUID/Redis stream ID、level/timestamp/duration、queue delivery canary 与无 I/O Worker 入口回归共 `83 passed`；凭据日志回归 `55 passed`；随后启动独立 Redis 7 容器并以 `LLMBENCHLAB_TEST_REDIS_URL=redis://127.0.0.1:16379/15` 实跑 `tests/integration/test_redis_streams.py`，结果 `2 passed` 且容器由 trap 停止；Ruff check、Ruff format check 与 `git diff --check` 全绿。独立 reviewer 再跑 logging/source/queue `19 passed`，最新 staged 技术/安全终审 0 Blocker/High/Medium |
| 合并 P2-06 定向 pytest 套件 | 全绿；随后 `make test` 也已完成，不单独虚构定向测试数 |
| 真实 PostgreSQL/Redis migration/check + `integration` marker | 临时 PostgreSQL 16/Redis 7 全绿，`33 passed, 0 skipped`；覆盖 audit retention advisory/row-lock 与既有 lease/governance/importer 路径。首次 cleanup 命令被本地安全策略拒绝，容器尚未启动；改用明确目标后通过 |
| `make lint` | 全绿：Ruff 检查 152 files、Ruff format check、ESLint、TypeScript typecheck 均通过 |
| `make test` | 后端 `916 passed, 33 skipped`；前端 `38 passed`；自动化只使用 Mock/MockTransport/stub |
| `make smoke` | `1 passed, 7 deselected`；完全离线 Mock |
| 从仓库根运行 `npm run build` | 失败：根目录没有 `package.json`；这是命令目录错误，不是 frontend build 失败 |
| `cd frontend && npm run build` | 成功：2192 modules；主 chunk `662.39 kB` 产生既有非阻断 warning |
| 默认用户 SQLite 上 `cd backend && uv run alembic check` | 失败：该用户数据库尚未在 `20260828_0005` head；按保护用户数据原则未擅自运行 upgrade |
| 临时 SQLite head→`0001`→head + `alembic check`，以及隔离真实 PostgreSQL migration round-trip/check | 全绿；当前 head 为 `20260828_0005`，包含 populated downgrade guard/空库往返 |
| `docker compose config --quiet` | exit 0 |
| 临时 `prom/prometheus:v3.5.0` 容器中 `promtool check rules` | SUCCESS；八条规则全部通过 |
| 过宽 Ruff scripts 命令 | 报告 93 条既有 modernization 告警；随后按本任务静态合同运行 `--select E,F,I` 并通过，未修改范围外历史现代化问题 |
| `make phase2-acceptance`（dirty 工作树） | 9/9；evidence `.pytest_cache/artifacts/phase2-acceptance/llmbenchlab-p2-11554c25ec2d/evidence.json`，SHA-256 `d5f058457dbc29875cbac4bc38345b810b5ed556ea538862d309116ceb629fde`，`dirty=true`；Worker expected/registered/live/stalled/shortfall=`2/2/2/0/0`；application populated `0005` refusal、isolated populated `0004` refusal、两层空库往返、cleanup containers/volumes/networks empty |
| `make phase2-capacity`（最新 dirty 工作树） | artifact `.pytest_cache/artifacts/phase2-capacity/llmbenchlab-p2-c6de062ab77e/evidence.json`，SHA-256 `4aeb8271dd81e8671fc287942839f8d06862140ea9a6bf1d7ee5660265aa8453`，`dirty=true`；1W/2W/burst wall `8.257520/4.640051/7.161722s`、`7.266104/12.930892/8.377873 q/s`；18 Runs/270 Responses/270 QuestionExecutions/271 reservations（270 actual + 1 conservative）/1229 audit，0 error/drift/duplicate/PEL/lag，Worker expected 2，cleanup C/V/N/image 全零且 image `1/1/0/0`；offline Mock、非 SLO |
| `make phase2-acceptance`（clean SHA） | implementation commit `9a20676dcf545040782f04c166205d0043345753`，artifact `.pytest_cache/artifacts/phase2-acceptance/llmbenchlab-p2-92e173eeee28/evidence.json`，SHA-256 `e4ffb8668fd3fa62d59b5d83f5c29eede35b327d88e6099345acd5950670fc47`，`dirty=false`、9/9；Worker `2/2/2/0/0`，两级 populated refusal、两层空库往返、queue 0/0 与 cleanup C/V/N empty；脚本不承诺 build image cleanup |
| `make phase2-capacity`（clean SHA） | 同一 implementation commit，artifact `.pytest_cache/artifacts/phase2-capacity/llmbenchlab-p2-ca5673061b0f/evidence.json`，SHA-256 `2382f9138f09028f269d76c341b236dd4089d678c8a2323582045fac2b4f5039`，`dirty=false`；1W/2W/burst wall `8.255963/4.628834/6.428385s`、`7.267474/12.962228/9.333604 q/s`；18/270/270/271/1230，0 question error/drift/duplicate/PEL/lag，expected Worker=2、stalled/shortfall=0，故障恢复后的瞬时 registered/live=3；cleanup C/V/N/image=0 且 image `1/1/0/0`；offline Mock、非 SLO |
| implementation commit / push / exact-SHA CI | commit `9a20676dcf545040782f04c166205d0043345753` 已普通 push 至 `origin/codex/complete-evaluation-workflow`；PR [#3](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/pull/3)；[run `33164609388`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33164609388) 精确绑定该 SHA，Frontend、Compose reliability、Backend、PG/Redis integration 四个 job 全 success |
| evidence-doc commit / push / exact-SHA CI | commit `ec2959680459a14aa308bd4d9ebcc6bb7bfcf3a6` 已普通 push；[run `33165775037`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33165775037) 精确绑定该 SHA，四个必需 job 全 success |
| 先前 code review 与 hydration/import integrity 回归 | 当时结论为 0 Blocker/High/Medium、目标集 `67 passed`；其后 staged security review 发现 structured logging High 与 Worker entrypoint Medium，均已修复并重新复核 |
| Implementation staged 技术/安全终审 | 76-file implementation index 为 0 Blocker/High/Medium；独立 logging/source/queue `19 passed`，archive/retention/CLI/exporter/Docker-script `167 passed`；secret/path/blob/category 扫描无真实凭据或禁止产物 |
| README、TESTING、CHANGELOG、PROJECT_STATUS、ROADMAP、Phase 2、NEXT_TASK、计划/工作日志状态同步 | 已回填 implementation 与 evidence-doc 精确 SHA 门禁，并把 P2-06 completed / P2-07 ready-next 边界写入状态文档；本轮链接/diff 检查通过 |
| 九份状态文档相对链接检查 | 全部目标存在 |
| 七份 tracked 状态文档 `git diff --check` + 新 plan/worklog 的 `git diff --no-index --check` | 无 whitespace error；no-index 命令因文件整体为新增返回差异状态 1，且没有 `--check` 诊断 |

## 已知问题与下一步

- P2-06 三条实现线、structured logging High 与 Worker entrypoint Medium 修复已落地；lint、全量 test、smoke、frontend build、临时 SQLite/真实 PG migration、真实 PG/Redis integration、Compose config、八规则 promtool、clean-SHA capacity/9/9 acceptance、implementation push 与精确 SHA 4/4 CI 均已通过。
- P2-06 implementation、clean-SHA evidence、证据文档提交及对应精确 SHA CI 均已完成；本工作日志状态为 `completed`。
- P2-07 尚未开始，仍缺数据库+keyring 配对 backup/restore、Redis 重建、告警响应与完整恢复矩阵；下一步须新建独立计划/日志/必要 ADR，Phase 2 继续 `in_progress`。
