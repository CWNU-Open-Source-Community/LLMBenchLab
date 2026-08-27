# 2026-08-27 — Web Provider 凭据输入工作日志

> 本日志记录实际发生的工作，不把本地通过替代为远程 CI 结论。所有命令以仓库根目录为基准。

## 元信息

- 日期：2026-08-27
- 执行者：Codex
- 关联阶段：[Phase 2](../phases/PHASE-2-RELIABILITY.md)
- 关联计划：[Web Provider 凭据输入执行计划](../plans/2026-08-27-web-provider-credentials.md)
- 关联 ADR：[ADR-0007](../decisions/ADR-0007-web-provider-credentials.md)、[ADR-0004](../decisions/ADR-0004-secret-management.md)、[ADR-0006](../decisions/ADR-0006-local-real-provider-evaluation.md)
- 最终状态：`in_progress`（功能与全部本地门禁通过；阶段 commit/push 和精确 SHA CI 尚待收尾）

## 初始仓库状态

- 当前分支：`codex/complete-evaluation-workflow`
- 起点 `git status --short --branch`：分支跟踪 `origin/codex/complete-evaluation-workflow`，工作区干净。
- 前一实现 commit：`0e62a371b9dd7bd819359a4a2b16ff8d5faa3a0d`；前一文档 gate：`7e8f08bdc0530baa100ea2e85e77beaf621713b8`。
- 起点已验证基线：后端 `310 passed, 5 skipped`、真实基础设施 `5 passed`、前端 `13 passed`、Smoke 1、Phase 2 Compose 8/8。
- 环境约束：没有真实 Provider Key；本任务自动化只能使用明显假 Key、固定测试 keyring、Mock/MockTransport，不产生真实调用或费用；PR 创建仍需用户明确授权。

## 目标、范围与非目标

用户明确要求最终用户直接在 Web 端输入 API Key，而不是输入环境变量名称。目标是让 Web 只写提交 Key、API 用数据库外 keyring 加密保存、独立 Worker 在重启/恢复后可调用 Provider，同时不把凭据数据流中的 Key、Provider 对 Key 的回显或加密材料复制到读取面、日志、队列、Run model snapshot、导入输出和报告证据。该保证不扫描无关 Benchmark/Question 内容的独立字面巧合。

范围包括 Models 表单与 API、AES-256-GCM credential crypto、一模型一行的 `model_credentials`、Worker 解密、Adapter direct secret、legacy `api_key_env`、Alembic `0003`、六表 importer、setup/Compose/Nginx、文档和安全回归。

非目标包括公网/多租户认证授权、生产 KMS、Web discovery/canary、全局预算/限流/完整审计，以及任何自动化真实 Provider 调用。

## 验收标准

- [x] Web 直接输入 password 类型 Key；读取/PATCH 不回填，提交开始、关闭、切换 Mock 与 unmount 都清空状态。
- [x] 数据库无明文；API/日志/snapshot/report/queue/import 输出无 Key、ciphertext、nonce、key ID 或 keyring material。
- [x] Worker direct stored Key 与 legacy environment 两条路径都通过 MockTransport/Mock 回归。
- [x] origin 重输、active Run 锁、keyring fail-closed、双方言 migration/downgrade 和六表 importer 回归通过。
- [x] README/API/Architecture/Security/Deployment/Testing/状态/Changelog/Phase/Next Task 已同步。
- [ ] 阶段 commit 已 push，且该精确 SHA 的远程必需 CI 全绿。

## 实际实现

| 模块 | 实际修改 | 安全/行为结果 |
|---|---|---|
| Credential crypto/ORM | 新增严格 JSON keyring、AES-256-GCM、随机 96-bit nonce、Model ID + Provider origin AAD，以及 `model_credentials(model_id PK/FK)` | 数据库只保存认证密文；跨模型、跨 origin、错误 keyring 和篡改均 fail closed |
| Model API/schema | 新增 8–8192-byte visible-ASCII write-only `api_key`、`none/environment/stored`、`has_api_key`、create/PATCH/来源切换、origin 门禁和 active-Run Model 锁 | 读取/OpenAPI/错误不返回秘密；Mock/env 删除 credential row；legacy env 保持兼容 |
| Worker/Adapter | Worker 通过 Run 的 `model_id` 与 snapshot Base URL 解密；Adapter 接受 `SecretStr` direct key，内部 httpx 禁用代理环境 | Key 只出现在目标 Provider Authorization；成功/失败回显在持久化前脱敏 |
| PATCH/数字边界 | 新 Key/保存旧 Key 都检查精确 `ModelRead` 全字段和 Run snapshot `model` 子投影；Provider JSON 返回递归检查标量、usage 和 HTTP status | 修复凭据流把 Key 复制进生成 ID/时间戳、name/URL/model/defaults/数值，以及 Provider 回显数字 Key 进入 usage/status 的审计 blocker |
| 损坏凭据恢复 | active keyring 可用时，隔离 PATCH 的显式新 Key 可覆盖缺行、未知/旧 `key_id` 或损坏 envelope；只切 Mock/legacy env 可清理它 | 夹带无关公开更新返回 422；保留 stored 且无新 Key 返回 503；失败均保持事务不变 |
| 方言并发锁 | Model PATCH 与 Run create 共用 lock helper | PostgreSQL 用 Model row `FOR UPDATE`；SQLite 在首次读 Model 前用 `BEGIN IMMEDIATE` 串行化，竞争可短暂阻塞且只定位为低并发本地模式 |
| Migration/import | 新增 Alembic `20260827_0003`、credential source backfill、非空凭据 downgrade guard；importer 从五表扩为六表 | SQLite→PostgreSQL 原子复制 nonce/ciphertext/key ID，输出不含明文或 envelope bytes；keyring 不随数据库迁移 |
| Request/log boundary | API 忽略客户端 `X-Request-ID`，每次生成 UUIDv4；CORS 不允许同名请求头；SQLAlchemy 隐藏参数 | 修复把 Key 复制进 request ID 后被响应和日志反射的审计 blocker |
| Frontend | Models 页使用 `type=password`/`autocomplete=new-password`，create 必填、edit 留空保留，状态只显示“已安全保存” | Web 不展示 `api_key_env` 输入，不写 storage/console；请求开始即清空 DOM/state，close/unmount abort |
| Deployment | setup/dev/Makefile 生成或校验 `0600` keyring；bootstrap 保持系统 Python 3.9 兼容；Compose 只给 API/Worker 只读挂载；Nginx 保持 loopback/Host/CSP/request-buffer 边界 | Mock/env 可在无 keyring 时运行；stored 模式缺 keyring 稳定 503；数据库与 keyring 独立备份 |
| 文档 | ADR-0007、README、API、Architecture、Security、Deployment、Testing、Requirements、Charter、Roadmap、状态与下一任务 | Web 主路径统一为直接 Key；ADR-0004/0006 的旧 Web/REST 结论明确被部分取代 |

## 关键决定、偏差与发现

| 类型 | 事实与理由 | 处理结果 |
|---|---|---|
| deviation | 用户明确要求 Web 输入真实 Key，改变 ADR-0004/0006 原 env-only UI 边界 | ADR-0007 接受应用层加密，旧 env/CLI 只作为兼容路径 |
| decision | Run 不新增 credential reference；每个 Model 最多一条凭据，Worker 通过 `run.model_id` 和冻结 origin 读取 | 降低 snapshot/queue 泄漏面，active Run 锁保证执行期间不漂移 |
| discovery | 客户端可把 Key 复制到 `X-Request-ID`，旧实现会响应/日志反射 | 改为服务端 UUID；POST/PATCH canary 与 Phase 2 每请求断言均覆盖 |
| discovery | PATCH 用固定 marker 重建 stored source，看不到旧 Key 是否被写入公开字段 | PATCH 对 preserved Key 做 fail-closed 解密/全公开字段检查，并验证事务不修改原记录 |
| discovery | 纯数字 Key 可伪装成 seed/price/Provider usage/HTTP status | schema 检查数值公开字段；Adapter 对 JSON 标量、token counts 和 status 做最终 secret guard；完整 Worker/DB 路径覆盖 |
| discovery | 旧计划误写 Model/Run credential ID 列 | ADR、计划与迁移统一为 `model_credentials.model_id` 一模型一行，Run 不加列 |
| decision | 不可解密的旧 envelope 若阻塞所有 PATCH，会让用户无法自助恢复 | active keyring 下的新 Key 覆盖与 Mock/env 清理绕过旧值解密；只有保存 stored 时 fail closed 503 |
| decision | SQLite 单 Worker 不等于 API 写事务不会并发 | `BEGIN IMMEDIATE` 与 PostgreSQL `FOR UPDATE` 分别封闭 snapshot/credential 竞态 |

## 实际运行命令与结果

| 命令/检查 | 退出码 | 实际结果 |
|---|---:|---|
| `make lint` | 0 | Ruff、109 个 Python 文件 format check、ESLint、TypeScript 全部通过 |
| `make test` | 0 | 后端 `421 passed, 6 skipped`；前端 5 files / `21 passed`；6 个 skip 仅为无 DSN 时的 infrastructure 用例 |
| `cd backend && uv run pytest -q tests/test_web_credentials.py` | 0 | `54 passed`，覆盖 Web/DB/log/request-id/PATCH/恢复隔离/数字 Key/Worker/report 路径 |
| 临时 PostgreSQL 16/Redis 7 + `pytest -m integration` | 0 | `6 passed, 0 skipped`；含租约/取消、Model 行锁、Redis PEL/ACK、六表 credential binary importer |
| PostgreSQL `alembic upgrade head` + `alembic check` | 0 | `0000 -> 0001 -> 0002 -> 0003` 成功；无新 upgrade operation |
| `make smoke` | 0 | `1 passed, 5 deselected`，隔离 SQLite + Mock，全程离线 |
| `npm run build` | 0 | production build 成功；仅保留既有约 649 kB chunk warning |
| `docker compose config --quiet` | 0 | 配置有效 |
| `make phase2-acceptance` | 0 | 更新后的 8/8 场景通过；evidence `llmbenchlab-p2-60f3ccdac113/evidence.json` |
| Phase 2 cleanup 检查 | 0 | `remaining_containers/networks/volumes=[]`；临时 PG/Redis 容器也按精确名称删除 |
| `cd backend && uv lock --check` | 0 | 50 个包解析一致 |
| `git diff --check` | 0 | 无 whitespace error |
| 高置信 secret scan | 0 | 仅 3 个测试文件中的明确假 canary 命中；未发现真实 Key/私钥 |
| 可信 loopback 浏览器手工检查 | — | password input；没有 `api_key_env` 控件；保存后不回显测试 Key；应用日志无该值；未触发 Provider |
| 系统 Python 3.9 keyring bootstrap | 0 | 创建/校验入口兼容运行，不打印 key material；不改变自动化测试计数 |

## 测试与安全结论

- 通过：后端 421、真实基础设施 6、前端 21、Smoke 1、Compose 8/8、lint/type/build、Alembic、lock/config/diff。
- 失败后修复：新增 PostgreSQL 测试最初只有自动格式差异；Ruff format 后通过。安全终审发现 request-id、preserved PATCH 和 numeric evidence 三类泄漏边界，均先补回归再修复并重跑全门禁。
- 未调用真实 Provider；Authorization 仅在 `httpx.MockTransport` 中断言为假 canary。
- 真实浏览器补充验收确认 password/无 env 输入/保存不回显/日志无测试 Key；使用无效测试值且没有 Provider 请求。
- 没有读取、输出或提交 `.secrets` keyring 内容；高置信扫描命中均为测试 marker。
- Phase 2 整体继续 `in_progress`：P2-05 限流/预算/公平、P2-06 完整审计/历史指标、P2-07 性能容量/Runbook 仍未完成。

## 尚未完成与远程边界

- 阶段 commit/push 与精确 SHA GitHub Actions 尚未在本日志快照时完成；完成后补记实际 SHA、push 和 CI 查询结果。
- PR 创建不在既有授权内。若 workflow 只在 PR/main 触发且当前分支没有 PR，必须如实记录“无该 SHA run”，不能自行创建 PR 或把本地门禁冒充 CI。
- 真实 Provider 评测有意未运行；用户后续只需在可信 loopback Web Models 表单提供 Base URL、远端模型名与 Key，再选择固定/自定义 Benchmark 创建 Run。

## 已知限制

- 浏览器扩展、DevTools、password manager、clipboard、JS heap、API/Worker 进程内存和出站 Authorization 仍会短暂接触明文。
- 主机/root 被攻陷，或数据库与 keyring 同时泄漏时，应用层加密不能保护 Provider Key；keyring 丢失则密文不可恢复。
- 当前没有认证、多租户隔离、KMS、批量重加密、Provider allowlist/DNS 重绑定防护或成本硬预算，只允许可信 loopback 使用。

## 最终 Git 状态

```text
in_progress: local gates passed; stage commit/push and exact-SHA CI pending
```
