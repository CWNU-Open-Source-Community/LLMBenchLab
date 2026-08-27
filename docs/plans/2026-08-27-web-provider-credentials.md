# Web Provider 凭据输入执行计划

- Owner: Codex
- Status: active
- Created: 2026-08-27
- Updated: 2026-08-27
- Related requirements: FR-MOD-05–10、NFR-SEC-01–05
- Related phase: [Phase 2](../phases/PHASE-2-RELIABILITY.md)
- Worklog: [Web Provider 凭据输入工作日志](../worklogs/2026-08-27-web-provider-credentials.md)
- ADRs: [ADR-0007](../decisions/ADR-0007-web-provider-credentials.md)、[ADR-0004](../decisions/ADR-0004-secret-management.md)、[ADR-0006](../decisions/ADR-0006-local-real-provider-evaluation.md)

## Context

现有模型 API 和 Web 只接受 `api_key_env` 名称，真实 Key 必须另外注入 API/Worker 环境。用户明确要求最终用户直接在 Web 输入 API Key。该变更跨前后端、数据库、Worker、部署配置和安全边界，必须保持旧 CLI/env 模式、可靠执行及报告不泄漏不变量。

## Objective

用户在 Web 模型表单中粘贴真实 API Key 后，API 只写接收并以独立部署主密钥加密保存，独立 Worker 可在重启/恢复后调用 Provider；系统不把凭据数据流中的 Key 或 Provider 对 Key 的回显复制到读取接口、日志、队列、Run model snapshot 或报告证据，也不暴露加密材料。该保证不覆盖与 Model 无关的 Benchmark/Question 内容发生独立字面巧合。

## Scope

- Model create/PATCH/read API 与 Web 模型表单。
- AES-256-GCM credential keyring、以 `model_id` 为主键的一模型一行凭据表与 Alembic migration；Run 继续只引用 Model，不保存 credential reference。
- OpenAI-compatible Adapter 直接内存凭据、Worker 解密与旧 `api_key_env` 兼容。
- 本地 setup、Compose secret、配置、导入器、文档和安全回归。

## Non-goals

- 公网多用户认证/授权、生产 KMS、租户级 RBAC。
- 在自动化中运行真实或付费 Provider。
- Web 模型发现/canary、全局预算/限流与完整审计 ledger；保留在既有 Phase 2/3 后续项。

## Assumptions

| 假设 | 依据 | 验证方法 | 不成立时的处理 |
|---|---|---|---|
| Web/API/Worker 仅绑定 loopback，操作者可信 | 当前部署与 ADR-0004/0006 | Compose/README/配置检查 | 若需公网，停止并先设计认证、授权、KMS 与 SSRF 防护 |
| API 与 Worker 可读取同一部署 keyring | 本地 `.secrets` 与 Compose secret | local/Compose 配置及 Worker 测试 | 缺失时 fail closed，不回落明文 |
| 旧环境变量模型必须继续工作 | 已有 CLI、Run 和数据库 | Adapter/API/Runner 回归 | 保留 nullable legacy 字段和 snapshot 分支 |

## Requirements

- [x] WEBKEY-01：Web 使用 password input 直接提交 8–8192-byte visible-ASCII write-only `api_key`，编辑留空表示保留且提交后清空内存状态。
- [x] WEBKEY-02：数据库不含明文 Key；AES-GCM 随机 nonce、AAD 模型/origin 绑定、key id 轮换语义有测试。
- [x] WEBKEY-03：API 读取的凭据相关字段仅返回非秘密状态；新/保存旧 Key 都与精确 `ModelRead` 全字段及 Run snapshot `model` 子投影比较，Provider 返回证据递归脱敏，不把凭据流中的 Key/密文/nonce 复制到公开或持久化表面。
- [x] WEBKEY-04：独立 Worker 能解密并调用 OpenAI-compatible Adapter，旧 `api_key_env` 路径不回归。
- [x] WEBKEY-05：origin 变化必须重输 Key；active Run 时端点或凭据修改返回稳定 409；Model 更新与 Run 创建在 PostgreSQL 用 `FOR UPDATE`、SQLite 用 `BEGIN IMMEDIATE` 串行化。
- [x] WEBKEY-06：SQLite/PostgreSQL migration、downgrade guard、SQLite→PostgreSQL import 与部署 keyring 配置完整。

## Implementation steps

1. [completed] **固化安全设计与边界**
   - 修改范围：ADR、计划、工作日志；Model/API/Worker/部署勘察。
   - 完成判据：ADR-0007 Accepted，明确只写、AEAD、origin 绑定、legacy 与 rollback。
2. [completed] **实现持久凭据与 Worker 使用**
   - 修改范围：ORM、schema、API、credential service、Adapter、Runner、Run service、migration、importer、config。
   - 完成判据：目标后端测试证明加密存储、API 不回显、Worker MockTransport 调用及迁移通过。
3. [completed] **实现 Web 输入与部署体验**
   - 修改范围：ModelsPage、frontend types/tests、setup/Compose/`.env.example`。
   - 完成判据：密码输入、编辑保留、状态显示、提交清空与 keyring 自动生成验证通过。
4. [completed] **安全回归与全量验证**
   - 修改范围：API/日志/report/importer/迁移/前端测试、lint/build/smoke/Compose。
   - 完成判据：所有门禁通过，秘密扫描无 marker/真实 Key，未进行真实 Provider 调用。
5. [in_progress] **文档、状态与交付证据**
   - 修改范围：README、API、Architecture、Security、Deployment、Testing、Changelog、Status、Phase、Next Task、worklog。
   - 完成判据：实现与文档一致，commit/push 完成；PR/精确 SHA CI 按仓库授权规则处理。

## Risks

| 风险 | 可能性 | 影响 | 预防措施 | 触发后的处理 |
|---|---|---|---|---|
| 主密钥丢失导致凭据不可恢复 | 中 | 高 | setup 生成独立文件、文档要求安全备份、keyring 多 key | 稳定失败并让用户重新输入 Provider Key，不尝试绕过认证 |
| 缺失/旧/损坏 envelope 阻止配置恢复 | 中 | 中 | 隔离的 PATCH 替换/来源切换不依赖解密旧值；夹带公开更新 422、保留 stored 才 503 | active keyring 可用时显式输入新 Key 覆盖，或只切 Mock/legacy env 清理 |
| 改 URL 导致旧 Key 外送 | 中 | 高 | origin 变化重输 Key、active Run 更新锁、AAD origin 绑定 | 409/422 阻止变更并保留旧配置 |
| 日志/验证/报告泄漏 | 中 | 高 | SecretStr、hide SQL params、响应白名单、marker 扫描 | 阻断交付并修复所有反射路径 |
| migration/import 漏表或 FK 顺序错误 | 中 | 高 | dependency order、SQLite/PostgreSQL/round-trip 测试 | 保持 migration 可回退，不修改用户数据库外的状态 |
| 浏览器保存 Key | 低 | 高 | password/new-password、只放受控 state、成功/关闭清空、不用 storage | 前端测试失败即阻断 |

## Validation

| 验收项 | 命令/检查 | 预期结果 | 实际结果与证据 |
|---|---|---|---|
| credential/API/Worker | `cd backend && uv run pytest -q tests/test_web_credentials.py` 与完整 `make test` | 全绿、无 secret marker | `54 passed`；全后端 `427 passed, 6 skipped`，skip 仅为未注入 DSN 的 infrastructure |
| migration/import | 临时 PostgreSQL 16/Redis 7 下 `pytest -m integration` 与 `alembic check` | 六表 binary round-trip、并发与 schema 全绿 | `6 passed, 0 skipped`；Alembic 无新操作，临时容器精确清理 |
| frontend | `npm test`、`npm run lint`、`npm run typecheck`、`npm run build` | 全绿 | 5 files / `21 passed`；lint/typecheck/build 通过，仅既有 chunk warning |
| repository gates | `make lint`、`make test`、`make smoke` | 全绿且不联网调用 Provider | 全部通过；Smoke `1 passed, 5 deselected` |
| deployment | PostgreSQL `alembic upgrade/check`、`docker compose config --quiet`、`make phase2-acceptance` | 无 schema drift，配置和故障验收有效 | `0003` upgrade/check、config 与更新后的 Compose `8/8` 通过；清理无残留 |
| 秘密与无关改动检查 | `git diff --check`、`git status --short`、高置信模式扫描 | 无格式错误、无真实 Key、范围正确 | diff check 通过；仅测试文件中的明确假 canary 命中，未发现真实 Key |
| 浏览器/keyring bootstrap | 可信 loopback 浏览器手工检查；`24` 个定向测试；PyPy-first `PATH` 下的全新临时 keyring 创建/二次校验 | password、无 env 字段、保存不回显/日志无测试 Key；入口固定 CPython 且不打印 key material | 均通过；临时 keyring 为 `0600`，使用无效测试 Key且未调用 Provider |

## Rollback

先停止 API/Worker 并备份数据库与独立 keyring。只要 `model_credentials` 存在任意行，migration 就会在执行 DDL 前拒绝 downgrade；用户必须先把对应模型切换为环境变量/Mock 或继续保留新版本，禁止静默删除密文。Run 不含 credential reference，代码回退不删除历史 Run/Response。主密钥文件由用户显式保留或安全删除，任何自动回滚都不得输出其内容。

## Documentation updates

- [x] README / 用户操作说明
- [x] API / Architecture / Security / Deployment / Testing
- [x] ADR-0007 与 migration/rollback 方案
- [x] `CHANGELOG.md`
- [x] `docs/PROJECT_STATUS.md` 与当前 Phase 文档
- [x] `docs/NEXT_TASK.md` 与本次工作日志

## Completion evidence

- 修改文件：后端 credential crypto/ORM/API/Adapter/Worker/migration/importer、安全回归；前端 Models 表单与测试；setup/Compose/Nginx；ADR、用户与状态文档。
- 实际命令：`make lint`、`make test`、`make smoke`、真实 PostgreSQL/Redis `pytest -m integration`、PostgreSQL Alembic upgrade/check、`uv lock --check`、Compose config、`make phase2-acceptance`、diff/secret scan。
- 验收对应：WEBKEY-01–06 的本地证据全部通过；Web 凭据基础实现 commit `b19bdac9236f9b2f927166ebe30578ced3d9f53e` 已正常 push，当前 bootstrap remediation 尚待 commit/push。该分支没有 PR，workflow 仅监听 PR/main，故基础实现 SHA 无远程 run；未获授权创建 PR，远程门禁继续保持未完成。
- 未运行：真实 Provider 调用（无用户 Key，自动化禁止）。
- 已知问题：可信本地边界不等于公网秘密托管；外部 KMS 和完整审计后续处理。

## Decision and discovery log

| 日期时间 | 类型 | 记录 | 影响/后续 |
|---|---|---|---|
| 2026-08-27 CST | deviation | 用户明确要求 Web 直接输入 Key，替代只填环境变量名的原 UI 目标 | 新 ADR supersede ADR-0004 对 Web/REST 的绝对禁令 |
| 2026-08-27 CST | decision | 采用一模型一行独立凭据表、AES-GCM 与部署 keyring；Run 只保留 Model reference | 支持多进程/重启且数据库无明文 |
| 2026-08-27 CST | discovery | 保留 Key 同时改变 Base URL 会形成 credential-forwarding | origin 变更重输、AAD 绑定和 active Run 更新锁纳入验收 |
| 2026-08-27 CST | discovery | 客户端 request ID、保留旧 Key 的 PATCH 和数字 usage/status 都可能形成反射路径 | 服务端强制 UUID、PATCH 解密后公开字段检查及标量脱敏均新增回归并通过 |
| 2026-08-27 CST | decision | SQLite 的“单执行者”说明不足以封闭同进程并发；PostgreSQL 仍需细粒度行锁 | 两种方言共用 Model lock helper：SQLite 先 `BEGIN IMMEDIATE`（竞争可短暂阻塞，仅低并发本地），PostgreSQL `FOR UPDATE`；生产/并发评测推荐后者 |
| 2026-08-27 CST | decision | 不可读的旧 envelope 不能阻止用户修复或退出 stored 模式 | 显式新 Key 可覆盖、Mock/env 可清理；只有保留 stored 且无新 Key时返回 503 |
| 2026-08-27 CST | discovery | macOS 上 `PATH` 优先的 PyPy 3.11 对 no-clobber `os.link` 的 `dir_fd`/`follow_symlinks=False` 组合稳定返回 `EINVAL` | 不移除安全参数；setup/dev/Make 的 keyring 入口统一通过 `uv` 选择 CPython 运行 dependency-free script |
| 2026-08-27 CST | discovery | 瞬时原子安装错误之后若清理也失败，继续重试可能遗留第二份临时 key material；项目感知 wrapper 还会为 Docker-only 路径同步宿主依赖 | 重试前必须确认按 inode 清理成功；清理失败 fail closed；bootstrap 改为不解析后端项目的 `uv run --script` |
| 2026-08-27 CST | verification | 自动化外确认真实浏览器与 PyPy-first 环境的首次 bootstrap | 浏览器确认 password/无 env/无回显/日志无测试 Key；全新临时路径在入口选择 CPython 后创建与二次校验通过，权限 `0600` |
