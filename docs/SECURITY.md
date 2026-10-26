# LLMBenchLab 安全说明

## 1. 适用范围与安全结论

LLMBenchLab 当前开发基线是供个人开发者在受信任机器上使用的本地评测工具，不是可直接暴露公网的服务。Phase 2 工作树除 PostgreSQL、Redis 通知和独立 Worker 的可靠执行基础外，已实现 managed Web/API Run 的数据库权威四层 Provider admission、逐 attempt ledger、背压/公平调度和 typed audit/history；但当前仍没有登录、授权、租户隔离、HTTP API 按主体限流、TLS 终止或生产级秘密管理。任何能够访问 API 的人都能读取题目与参考答案、注册模型、导入数据集、改写完整 governance policy、启动可能产生费用的 Run，并读取原始模型输出。

安全默认姿势是：只监听本机或可信开发网络、优先使用 Mock、只导入自己审查过的数据集、只配置可信的 Provider 地址，并分别保护数据库、部署 credential keyring、`artifacts/` 与备份。Web 可以在 Model 表单提交真实 Key，但当前 loopback HTTP、无鉴权边界不适合远程或不受信网络；Provider admission 不能替代入口鉴权，可信本地正式评测 CLI 的发现、费用确认和最小 canary 仍是 `legacy_unmanaged`，也不是公网安全层或预算控制器。

本文描述已实现控制、仍存在的风险和公开部署前的硬性改造。它不构成第三方 Provider 或 Benchmark 的安全保证。

## 2. MVP 威胁模型

### 2.1 需要保护的资产

- Provider API Key、访问令牌、兼容模式环境变量、AES-GCM credential envelope 与部署 keyring。
- Benchmark 题目、参考答案、metadata、许可证与来源信息；未来可能包含私有数据。
- 原始模型回答、解析结果、错误信息、运行配置快照和排行榜证据。
- Governance policy/scope/minute counters、逐 Provider attempt reservation/settlement ledger、typed audit/history/archive、Worker process/progress、credential audit 与安全归一化 Provider metadata。
- PostgreSQL/SQLite 中的数据库事实、SQLite 源库与备份、Redis AOF/队列元数据、宿主机文件和本地网络访问能力。
- 用户的 Provider 配额、预算与账号信誉。
- GitHub 仓库、Actions 权限、发布产物和依赖供应链。

### 2.2 参与者与信任边界

| 参与者/边界 | MVP 假设 | 若假设失效的后果 |
| --- | --- | --- |
| 本机操作者 | 受信任，能管理 keyring、环境变量和导入文件 | 可读取全部本地数据并发起付费请求 |
| API 客户端/浏览器 | 仅来自显式配置的本地前端；Web 表单会短暂持有待提交 Key | 无鉴权接口被滥用、Key/数据被读取或篡改、付费 Run 被启动 |
| FastAPI API | 受信任的写入边界；接收 write-only Key 并持有部署 keyring | 可在加密前读取 Key，或替换 endpoint/envelope、泄漏 keyring |
| Benchmark 作者 | 文件内容不可信，来源需人工判断 | 资源消耗、提示注入、答案污染、敏感数据外发 |
| Chat / Responses / Messages Provider | 地址、协议选择和服务由用户信任 | 收集提示、伪造响应、消耗预算、返回恶意文本 |
| 独立 Worker | 受信任的执行边界，持有数据库/Redis 凭据与部署 keyring，并按 source 解密或读取 Provider Key | 凭据/keyring 泄漏、越权读写全部评测事实、重复外部调用 |
| PostgreSQL / Redis | 只在受控内部网络可达，数据卷由受信操作者保护 | 数据事实被篡改、任务被干扰、运行元数据泄漏或服务不可用 |
| GitHub/CI/依赖源 | 外部供应链 | 依赖投毒、Actions 权限滥用、秘密泄漏 |
| 宿主机上的其他进程/用户 | 不在应用隔离范围内 | 读取进程环境、数据库卷、SQLite、日志或备份 |

主要入口是 Model CRUD 中的 write-only `api_key` 与 `base_url`、Benchmark ZIP 上传、Run 创建、题目/响应读取、前端渲染、API/Worker 日志、部署 keyring、PostgreSQL/Redis、SQLite→PostgreSQL 导入、数据库/备份以及依赖安装与 CI。当前系统不执行 Benchmark 中的代码，这一点显著缩小了攻击面。

### 2.3 重点风险与当前状态

| 风险 | 影响 | 当前控制 | 剩余风险 |
| --- | --- | --- | --- |
| 明文密钥泄漏 | Provider 账号被盗用 | Web Key 只在请求/进程内短暂出现并以 AES-256-GCM envelope 持久化；公开 Schema 不含 Key/envelope；环境兼容模式只存变量名；422/日志/上游反射脱敏 | 浏览器扩展、同机用户、API/Worker 内存、调试器、access proxy、数据库与 keyring 同时失陷仍不在保护范围 |
| 恶意 `base_url` / SSRF | 访问 localhost、内网、云元数据或敏感服务 | 远端只允许 HTTPS、HTTP 仅 loopback；拒绝 URL 内嵌凭据、query 和 fragment；禁用重定向 | **高风险：HTTPS 目标仍没有地址 allowlist、DNS/IP 分类或出站网络隔离** |
| 恶意 Benchmark ZIP | 路径穿越、压缩炸弹、内存/磁盘消耗 | 固定两文件、路径与类型检查、压缩比/大小/条数限制、严格 JSON | 仍可能消耗允许范围内资源或携带敏感/误导内容 |
| Prompt/数据外发 | 私有题目被发送给 Provider | 只有用户创建 Run 时才发送 | 无数据分级、DLP、Provider 策略或告知确认 |
| 固定数据源/缓存漂移 | 下载被替换、缓存投毒或第三方题目误提交 | 固定 HTTPS revision、大小和 SHA-256；缓存拒绝 symlink；`artifacts/` Git 忽略 | Hash 不是发布者签名；HTTPS redirect 只有 scheme 检查，许可与来源仍需人工复核 |
| XSS/内容注入 | 窃取页面数据或误导操作者 | React 默认文本转义；服务端不执行内容 | 未来引入 Markdown/HTML 时可能破坏边界；无 CSP |
| 未授权访问 | 读写全部评测数据、启动付费 Run | 建议仅绑定本机；CORS 显式配置 | CORS 不是鉴权，非浏览器客户端不受其约束 |
| 费用滥用 | 大量、长输出或重复上游请求造成费用 | managed Run 将 policy/override 冻结进快照，在 global/provider/model/run 四层原子裁决 concurrency、fixed-minute RPM/TPM 与累计 request/Token/cost；backlog 有界、逐 HTTP retry 先 reserve/`send_started` 再 actual/conservative settlement；hard Token/cost 缺显式输入/输出上界或价格会在外发前 fail closed；观测估算不冒充 hard input/cost reservation | 默认 policy 可关闭限制；`legacy_unmanaged` CLI 不在该边界；fixed-minute 允许边界突发；崩溃后的 Provider 幽灵请求、重复远端调用与 Provider 账单不能由本地 permit/ledger 消除；没有熔断，Provider 调用不是 exactly-once |
| 数据丢失/篡改 | 证据不可复现或 stored Key 不可用 | 数据库约束、Hash、Run 快照、租约 fencing；credential AAD 绑定 Model/origin；typed audit event key/payload hash 幂等，operational/security 分级保留；PostgreSQL 是任务/治理事实来源 | 数据库/备份整体未加密；keyring 丢失不可恢复；应用 append-only 不是密码学 WORM、不可抵赖或数据库管理员防篡改，访问、归档与恢复仍由用户负责 |
| 后台任务中断 | Run 不完整或重复执行 | 独立 Worker、数据库租约/心跳/fencing、有限重试、dead-letter、数据库对账 | 不构成生产 HA；外部 Provider 副作用不能由本地幂等约束去重 |
| Redis 暴露或篡改 | 伪造/重放任务通知、拒绝服务、元数据泄漏 | Compose 不发布 Redis 宿主端口；消息不是事实来源且不含 Prompt/Key | 本地 Compose 未启用 Redis ACL/TLS；暴露到不可信网络会破坏可用性与保密性 |
| 供应链攻击 | 执行恶意依赖/Action | Python/Node lockfile、CI 检查 | 尚无强制 SBOM、签名验证或持续漏洞门禁 |

### 2.4 明确不在 MVP 保证内的事项

- 对拥有宿主机账号、容器宿主权限或数据库文件权限的攻击者提供隔离。
- 保证第三方 Provider 不保存、训练或泄漏收到的 Prompt，或对 Provider 调用/计费提供 exactly-once。
- 对结果真实性做密码学证明；Dataset SHA-256 用于一致性，不是发布者签名。
- 公网抗攻击、多租户隔离、合规认证、灾难恢复 SLA。
- 安全执行任意代码。当前格式没有代码题，任何 Benchmark 内容都不会被执行。

### 2.5 数据库治理与审计安全边界

- 初始化前 `GET /api/v1/governance/policy` 是无副作用读取并返回 `404 governance_policy_not_initialized`；它不会把无鉴权的查询变成 policy 写入。`PUT` 必须提交全部 policy 字段，是不可变、内容寻址版本的 full-document apply，不是局部 PATCH。当前两个端点只适合可信 loopback；能够访问 `PUT` 的调用方可把限制设为 `null`（关闭）或 `0`（紧急拒绝），因此公开部署必须先加管理员授权。
- managed Run 创建在 global admission 锁内检查 backlog，并冻结全部 20 个 policy 字段、ID/hash、opaque provider scope、question quantum 与恰好四个 Run override（input reservation、request/Token/USD lifetime budget）。policy hash 与冻结 override 在 attempt 外发前重新校验，任何一侧漂移均 fail closed。四层锁序固定为 global→provider→model→run，网络期间不持有数据库锁。hard Token/TPM 需要显式 input reservation 与有限 `max_tokens`，hard cost 还需要 USD input/output price；缺失时在外发前 fail closed。治理 USD 公开上限为 `10000000.00000000`，Decimal 响应使用 JSON string；该上限使 SQLite IEEE-754 间距低于半个 `1e-8` 量化单位，PostgreSQL 则使用精确 `NUMERIC(20,8)`。
- 每个 HTTP attempt 的 ledger 状态只允许 `reserved → send_started → settled_actual|settled_conservative`，或可证明未开始外发时 `reserved → released_pre_send`。明确 pre-send release 保留旧 ledger 并开启新 generation，只重试当前未发送 ordinal，不重置已经 `send_started` 的较小 ordinal。commit/usage/transport 不确定一律保守结算；这防止本地预算按零释放，却可能低估剩余额度。没有显式 `input_token_reservation` 时，输入估算不写入新的 attempt hard reservation，也不参与 reserved cost 或 input/cost overdraw；Provider actual input 仍完整保存。显式 `max_tokens` 的 output reservation 独立生效。
- scope 和 UTC-minute 物化 counter 会在治理变更前从 never-delete ledger 重算；高、低漂移都停止新 admission/结算/对账，并由 API/Worker 边界用独立短事务尽力记录固定、不含损坏值的 `governance_integrity_error`。过期 lease takeover 在旧 ledger 校验失败时撤销新 owner 并使 Run fail closed，防止新 Worker 外发。
- 租约失效后 reconciler 会释放/结算本地 reservation，避免永久占用 admission。该动作无法停止已从 `send_started` 外发、仍在 Provider 运行的幽灵请求，也不证明崩溃后的真实远端并发仍受本地上限约束。本地 consumed 只用于 admission 与审计，必须另行核对 Provider 账单。
- typed audit payload 只接受 allowlist 短枚举/数值和 opaque ID，不含 Key、Authorization、URL、Prompt、原始回答或异常文本。事件重放以唯一 key 和 payload hash 检查一致性；默认 operational/security 至少保留 90/365 天。Retention CLI 会把过期完整事实写入权限收紧、canonical、内容/文件 hash 绑定的内部 JSONL，并支持离线 verify、精确 restore/delete/reconcile；hash 不是签名或 WORM，archive 仍含内部身份与时间线，必须按敏感运维文件保护。

## 3. API Key 与秘密管理

### 3.1 凭据来源与数据流

Model 用 `credential_source` 明确区分三种状态：

| source | 写入方式 | 数据库存储 | 执行时取值 |
| --- | --- | --- | --- |
| `none` | `mock` 不接受凭据 | 无 `api_key_env`、无 credential row | 不读取秘密、不联网 |
| `environment` | API/可信 CLI 传环境变量名称 `api_key_env` | Model 和 Run snapshot 只保存变量名 | Worker/本地 Runner 在调用前读取执行进程环境 |
| `stored` | Web/API Model 写请求传 `api_key` | `model_credentials` 保存 AES-GCM envelope | Worker 在构造 Adapter 前解密为进程内 `SecretStr` |

Web stored 流程如下：

1. `api_key` 只定义在 Model create/PATCH schema，OpenAPI 标记 `writeOnly`。它必须为 8–8192 bytes、没有首尾空白且只含可见 ASCII；API 不会把该凭据数据流中的值复制进公开 Model 字段。
2. API 使用部署 keyring 的 active 32-byte key、AES-256-GCM 与随机 12-byte nonce 加密；AAD 包含算法、Model ID 和规范化 Provider origin（scheme、host、非默认 port），不包含可变路径。
3. `model_credentials` 以 `model_id` 为主键/外键，仅保存 `algorithm`、`key_id`、`nonce`、`ciphertext` 和时间戳；plaintext 从不映射到 ORM 或数据库列。
4. Model 读响应只返回凭据状态 `credential_source`/`has_api_key`，以及 environment 兼容模式所需的变量名称；`has_api_key=true` 仅表示 stored row 存在，environment 模式不会因进程里恰好有变量值而返回 true。API/Run/Response/OpenAPI 读 schema 都不含 `api_key`、ciphertext、nonce 或 keyring 数据。
5. Run 没有 credential reference/envelope 列。快照只保存 source、Model ID、远端模型和 endpoint；environment 模式另保存变量名。stored 模式的 Worker 按 `run.model_id` 读取 row，并用 `run.model_id + Run snapshot base_url origin` 做 AAD 解密，绝不以当前 Model Base URL 替代快照目标。
6. keyring 缺失/无效、未知 key id、row 缺失、密文篡改、跨 Model 或错误 origin 都会 fail closed，并在 Adapter 构造和任何 Provider 网络请求之前结束该 Run attempt。environment 变量缺失则产生安全的 `missing_api_key`，同样不会无凭据联网。
7. 解密值只在内存中用于与显式协议匹配的秘密 header：Chat/Responses 使用 `Authorization: Bearer ...`，Messages 使用 `x-api-key`。Provider 响应在离开 Adapter 前递归脱敏：成功内容、raw usage 的对象键和所有 JSON 标量（包括数值/布尔/null）、token/status 数值、request ID、返回模型名、system fingerprint 和 finish reason 若精确包含当前 Key，都会替换为 `[REDACTED]`。
8. credential create/replace/delete/source switch 记录 `credential_changed`，origin/active-Run/恢复边界拒绝记录 `credential_rejected`，key ID/认证解密失败记录 `credential_decrypt_failed`。security audit 以数据库 UTC 记时，只保存 Model、source/action/reason 和安全 key ID，不保存 Key、origin 或 envelope；被回滚的拒绝以 server request ID 在独立短事务中幂等写入，但进程在响应前崩溃仍可能缺事件，因此不声称跨进程 exactly-once。

这里的保证限定于**不把 Key 或 Provider 对 Key 的回显从凭据数据流复制进公开字段或持久化证据**。create/PATCH 精确扫描 `ModelRead` 的所有字段和 Run snapshot 的 `model` 子投影；Adapter 递归扫描 Provider 返回证据。它不扫描整个 Run 中与 Model 无关的 Benchmark/Question 固定内容，也不声称用户数据与 Key 永远不会发生独立的字面巧合。

### 3.2 Keyring、更新与恢复边界

- `LLMBENCHLAB_CREDENTIAL_KEYS_FILE` 指向部署 keyring；严格格式为 `{"active_key_id": ..., "keys": {"id": "base64url-encoded-32-bytes"}}`。active key 用于新加密，其他已登记 key 只用于解密旧 row，以支持显式轮换。
- `make setup`/启动辅助脚本通过 `uv` 显式选择 CPython，只在文件不存在时原子生成 Git 忽略的本地 keyring，不打印 key material；既有普通文件会严格校验并收紧为 `0600`，symlink、目录、超大或非法 JSON 会被拒绝。原子安装只在确认临时文件清理成功后重试明确的瞬时 errno；清理或安装持续失败都只显示符号错误码，不输出操作系统原文、路径或密钥。Compose 把同一只读 secret 挂载到 API 与 Worker，不挂载给 frontend 或 migrate。
- keyring 本身不是加密的，也不是 KMS/HSM。它必须与数据库分开备份、限制权限并参与恢复演练；丢失 keyring 后 stored rows 不可恢复，数据库与 keyring 同时泄漏后攻击者可离线解密。
- API 需要 keyring 是为了接收/加密 Web Key；Worker 需要它是为了解密 stored Run。API 不应读取 legacy Provider 环境变量值，Worker 不应提供读回 stored Key 的端点。
- PATCH 省略 `api_key` 表示保留现有 row，显式 `api_key:null` 被拒绝；替换时重新加密并使用新 nonce。规范化 Provider origin 改变必须同时重输新 Key，同一 origin 的路径变化可保留现有 row。create/PATCH 会把新 Key 与精确 `ModelRead` 全字段及 Run snapshot 的 `model` 子投影比较；保留 stored 时只为同一比较解密旧值，任何匹配都在持久化前拒绝。
- 若 stored row 缺失，或现有 envelope 因旧/未知 `key_id`、损坏密文而无法解密，操作者仍可在可用 active keyring 下通过**只修改凭据**的 PATCH 显式提供一个有效新 Key，或通过只切换为 `mock`/legacy `environment` 清理该状态；同一请求夹带无关公开字段更新会返回 `422 credential_recovery_requires_isolated_update`。若仍要保留 `stored` 且没有新 Key，则稳定返回 `503 credential_store_unavailable`，事务不修改 Model 或 credential。
- Model 有 `pending`/`running` Run 时，Provider 类型、endpoint、远端模型或 credential 的敏感更新返回 `409 model_has_active_runs`。Run 创建与更新共用方言锁：PostgreSQL 对 Model 行执行 `SELECT ... FOR UPDATE`，SQLite 在读取 Model 前执行数据库级 `BEGIN IMMEDIATE`；AAD 绑定仍是绕过业务层、竞态或数据库篡改后的最后防转发边界。SQLite 因此仍是低并发本地模式，竞争写事务可能短暂阻塞请求；生产或并发评测应使用 PostgreSQL。
- 切换到 `mock` 或 `api_key_env` environment 模式会删除 `model_credentials` row。active-run 锁保证仍可能执行的 Run 不会失去或换绑凭据；终态历史 Run 只保留非秘密 snapshot，不获得可重放的 credential 副本。

### 3.3 操作与进程规则

- Web/API 真实 Key 只能放在 Model JSON 请求体的 `api_key` 字段。不得放进 URL、query、path、header 名、模型名、Base URL、默认参数、环境变量名称、日志、Issue、PR 或截图。当前本地 Web→API 使用受信 loopback；任何跨主机或不受信网络传输都必须先增加 TLS、认证与授权。
- `.env`、shell history、终端录屏、Issue、PR、截图和 Benchmark metadata 都不是秘密存储。environment 兼容模式可使用未提交的 `.env`、操作系统 Keychain/密码管理器注入或临时 shell 环境；`.env.example` 只能包含变量名和无效占位符。
- `llmbenchlab-evaluate` 没有 `--api-key` 参数。它保留 `--api-key-env`/`getpass` 路径；非交互且环境变量为空时停止，不把 Key 放进 argv，也不会把 CLI Model 隐式改成 stored source。
- 数据库和 API 会公开 environment 模式的变量**名称**。名称本身不应编码账号、项目机密或密钥片段。
- 给每个环境和用途使用独立、最小权限 Key；能设置预算、允许模型或来源 IP 时应开启。怀疑泄漏时先在 Provider 侧吊销/轮换，再清理仓库历史、数据库副本、日志和 keyring 备份。
- 本地 `make dev` 的 API/Worker 可能继承同一 `.env`，但只有 environment 执行路径解析已登记变量；Vite 不会自动把非 `VITE_*` 变量打入客户端。更严格部署应分别注入：API/Worker 共享 keyring，legacy Provider env 只给 Worker，frontend/migrate 两者都不给。
- Worker 同时需要数据库与 Redis 连接能力。生产设计应拆分 API/Worker/迁移数据库角色，限制 Worker 只能访问所需表和操作，并用独立 Redis ACL、短期凭据及轮换流程代替共享本地凭据。
- API/Worker 日志、崩溃转储、进程列表和诊断端点都属于秘密边界。不得记录环境、DSN、`Authorization`/`x-api-key` header、请求体、解密值或原始 Provider 请求。
- at-least-once 只保证任务最终可再次处理；逐 attempt ledger 防止同一本地逻辑 attempt 重复结算，但 `send_started` 后崩溃仍可能留下远端幽灵请求。若 Provider 已响应而本地 Response 提交前崩溃，接管 Worker 可能重复上游调用和计费；本地 `(run_id, question_id)` 唯一约束、保守 consumed 或释放 permit 都不能消除这一外部副作用或替代真实账单。

### 3.4 可信本地 CLI 秘密边界

- CLI 只适合受信任的交互式机器。它将读取到的 Key 临时放入 Run 快照所引用的进程环境变量，使现有 Adapter 能复用同一秘密接口；上下文结束时恢复原值或删除临时值。拥有同一用户权限、调试/进程转储能力的程序仍可能读取内存或环境。
- 模型发现 `GET /models`、按显式 Chat Completions / Responses / Messages 协议构造的最小 canary 和正式题目请求都使用同一个 Key。discovery 同样按显式协议鉴权：Chat/Responses 使用 `Authorization: Bearer`，Messages 使用 `x-api-key` 与 `anthropic-version`；Messages 的 `has_more/last_id` 只通过 `after_id` 跟进，并受累计 100 页、60 秒 wall-clock、2 MiB、10,000 个模型 ID 与重复 cursor 门禁保护。任何发现到的模型 ID 若反射该 Key，预检立即失败；canary 若明确返回不同于请求目标的模型名也失败。CLI 只保存脱敏 preflight 元数据，不保存请求 header 或 Key；完整 Provider access log 不受本应用控制。
- 在 canary 前，CLI 打印 Provider host、显式协议、目标模型、题数、剩余 failed-attempt 预算和最大 Provider HTTP 尝试数，并要求输入 `RUN`；剩余预算严格为 `max_attempts - failed_attempt_count`，不把 cooperative yield 算作失败，上界按 `(缺失题数 × 剩余 failed-attempt 预算 + 1 个 canary) × 3` 包含 HTTP retries，但仍不是 Token/金额预算。`--yes` 只用于操作者明确授权的非交互运行，不是预算、速率限制或安全审批。该路径创建 `legacy_unmanaged` Run，不继承 managed Web/API policy。
- `resume` 会再次读取 Key、确认并发送 canary，然后只处理本地缺失 Response。初次 canary 会固化到新 Run 快照，但 resume canary 当前不会追加为独立 audit event；逐题 request ID、返回模型名、system fingerprint、finish reason 与 HTTP attempt count 已安全归一化持久化。远端调用不是 exactly-once，恢复前应同时检查 Provider 账单与是否仍允许继续。
- `report` 和 `prepare` 不需要 Provider Key；不得为了方便给这两条命令或 CI 注入真实凭据。
- 正式 CLI 必须在常规 API/Worker 停止后独占数据库。代码只能拒绝已有 `running` Run，不能识别空闲 Worker；若空闲 Worker 抢到新 `pending` Run，可能在错误的 Key/进程边界发起调用。

## 4. 日志与错误脱敏

当前应用日志和三类远程 Adapter 的错误处理会：

- 只有显式登记的 LLMBenchLab 应用 logger 可输出 literal message 与 structured extra；字段除了 allowlist，还分别受固定 event/result/error/component 枚举、canonical UUID、Redis stream ID、HTTP method/route template 和有限数值合同约束。非法身份字段被省略，未知 method/path/code 只映射到固定 `unsupported`，异常类名只保留固定错误族，不反射原值。API 不记录原始查询串或请求体。
- `configure_logging` 把已知进程内 `Uvicorn`、`httpx`/`httpcore`、SQLAlchemy 与 Redis client logger 统一路由到同一 sanitizer：第三方动态 logger 名、message、exception 与伪造 structured extra 不进入 JSON；原始 `uvicorn.access` handler 被禁用，由应用 middleware 的 route-template 事件替代。
- 请求校验响应只保留安全的 `type`、白名单化 `loc` 和 `msg`，省略 Pydantic `input`/`ctx`；keyring/加解密失败只暴露稳定错误码。Model create/PATCH 成功响应带 `Cache-Control: no-store`。
- SQLAlchemy engine 固定 `hide_parameters=true`，因此 SQL echo/异常不会打印 bound Key/envelope 参数；这不替代关闭生产 SQL debug、保护数据库和限制第三方 telemetry。
- 用 `[REDACTED]` 替换当前 API Key 的精确值。
- 识别常见 Bearer、Authorization、API key、token 和 secret 表达形式。
- 把上游错误折叠为单行并截断到 500 字符。
- 不保存请求头、完整上游请求或响应对象；最终 `httpx.TransportError`，以及三协议 malformed JSON/SSE 或 oversized 响应，都在捕获原始异常的 `except` 边界外转成安全 `AdapterError`，其 `__cause__`、`__context__` 和格式化 traceback 不保留可达 request、秘密 header 或原始 Provider bytes；Run 只持久化分类后的错误类型与可读消息。
- 不记录原始 SSE 行、事件或单个 delta；只在收到完整终止信号后使用聚合结果。
- 对 Chat/Responses/Messages 成功内容、raw usage 的对象键和全部 JSON 标量，以及 provider request ID、返回模型名、system fingerprint、finish reason 递归执行当前 Key 的精确替换，再允许其进入后续 Runner/preflight 边界。
- EvaluationResponse/API/report 只保存经过长度、字符和 Key 反射检查的 provider request ID、returned model、system fingerprint、finish reason 与整数 HTTP attempt count；不保存任意 raw usage object，被拒字符串归一化为 `null`。
- 三类 SSE 的文本 delta 先按各自 typed event 完整聚合、再执行当前 Key 的精确替换，覆盖 Key 被分到多个 delta 的情况；这仍不是通用 DLP。
- 把 `finish_reason="length"` 的空输出或无法解析最终答案的输出归类为稳定的 `output_truncated`；这只是安全的诊断分类，不回显完整 Provider 响应对象或请求头。

仍需遵守以下规则：

- 不记录 `os.environ`、HTTP headers、完整异常 locals、请求体或 `.env` 内容。
- 应用外的反向代理 access log、PostgreSQL/Redis server log、Docker/runtime log，以及 formatter 配置前或进程崩溃时的 stderr 不受上述 sanitizer 控制；未知且安装自有 handler 的第三方库也必须在部署侧审计、过滤并设置保留策略。
- 本地 `make dev` 不再把三个服务的详细输出持续写到控制台，而是分别追加到 Git 忽略的 `artifacts/dev-logs/*.log`；启动器把目录/文件权限收紧为 `0700`/`0600`。Git ignore 和文件权限都不是加密、自动保留或安全删除，日志仍不得包含 Key、请求体或完整 Provider 内容，也不得作为公开附件上传。
- 即使进程内原始 Uvicorn access handler 已禁用，秘密仍不得出现在 URL、查询参数或路径；反向代理和基础设施日志必须应用相同规则。
- `DEBUG` 仅用于无秘密的本地排障。HTTP wire logging 和第三方 SDK debug logging 默认关闭；即使 SQL 参数已隐藏，也不应在含敏感数据的部署中采集冗长 SQL trace。
- 原始模型回答可能含供应商回显的其他敏感内容。当前只保证精确替换正在使用的 Key，并识别部分常见秘密形态；回答仍会作为评测证据持久化并通过 Responses API 返回，不能把“没有当前 Key”误当成“可以公开”。
- 异常报告、CI artifact 和截图发出前再次人工检查，不把数据库或 `.env` 整体上传。

脱敏降低偶发泄漏，不是数据访问控制；已经被未授权方读取的秘密必须轮换。

### 4.1 Phase 2 SLO evidence 安全边界

`make phase2-slo` 的当前正式 profile 是 `P2-local-control-plane-v2`，aggregate schema 为 `llmbenchlab-phase2-slo-evidence-v2`。它是完全 Mock-only、单主机/单故障域的本地控制面资格，不是读取现有模型配置的通用压测。四个 measurement 固定为 seed-balanced 单/双 Worker baseline，随后是不可拆分的 warmed pause 与 cold stop/start backlog；两个 burst 的 raw child 都先以 durable Run/Worker/claim 身份、分段时间和容器事实完成闭环验证，aggregate 才投影匿名计数与时长。wrapper 只给 child 继承运行本地 Git/Docker 所需的环境 allowlist，移除已知 Provider credential 变量；child validator 还要求 Model 为 Mock、没有 `api_key_env` 或自定义 `base_url`，并拒绝真实 Provider、非有限 policy、脏工作树和跨轮配置/环境漂移。不能通过把真实 Key 改成其他变量名来绕开此边界；资格进程本来就不应在含 Provider credential 的 shell 中运行。

raw child 与 aggregate 都写入 Git 忽略的 `.pytest_cache/artifacts/phase2-slo/`，不提交、不自动上传。v2 aggregate 使用严格 allowlist，只保存精确 commit、脚本/Compose SHA-256、稳定配置/环境指纹、child 相对路径/hash、匿名 Worker 参与计数、脱敏 timing/统计/判定、独立 ledger→scope/minute projection 结果和 cleanup 摘要；不复制 raw Run/Model/Worker/container/audit ID、child stdout/log、DSN、URL、环境变量、题目、Prompt/Response、keyring/envelope 或 Provider 数据。读取 child evidence 时还限制文件大小、UTF-8/strict JSON、重复键、NaN/Infinity、路径逃逸、symlink 和读取期置换。

allowlist 只减少聚合面，不等于 artifact 无敏感性。raw child 仍含 Run/Model/Worker/container ID、durable event 时间、数据 hash、资源与运维计数，禁止作为公开附件；aggregate 的主机/环境/配置指纹、内嵌 trial child 路径、SLI 和故障结果也可能暴露内部拓扑或性能，公开状态不得原样复制这些字段。公开记录可以给出仓库内 Git 忽略的 aggregate 相对路径和内容 SHA-256，便于本地操作者定位同一证据，但只摘录经人工复核的精确实现 commit、匿名统计/判定和明确支持边界；不得复制 aggregate 内的 child 路径或环境明细。两类 artifact 都应按内部运维证据保护，不把整个 artifact 根、数据库、Compose 展开配置或 `.env` 上传为 CI artifact。Git 忽略不是加密、访问控制、保留策略或安全删除。

每个 child 位于独立进程组和唯一、正则约束的 Compose project。超时或中断时 wrapper 先发送终止信号，并给 child 最多 420 秒执行 scoped `down -v --remove-orphans`、零容器/volume/network 复核和 v2 的唯一项目 backend image 安全清理，之后才只针对该进程组升级终止；该窗口不是允许清理其他项目、共享 tag 或其他 Docker cache 的授权。失败 evidence 会保留。它能证明一次 invocation 内记录了所有计划轮，不能证明操作者未删除更早的 suite，也不是 WORM、签名 provenance 或生产 SLA 证据。

历史 `P2-local-control-plane-v1` 在 clean `dfa67abb1a9a0418a7e3337c179f816e3c69f121` 上只通过 15/18 项 SLO，保持 `unqualified`，不得用 v2 结果追认或覆盖。当前 v2 在 clean `b6a35fef1dd069ebb54b69955058915c722aa34d` 上完成一个 warm-up 和恰好五个 measured trial，23/23 项 SLO 与每轮 hard invariant/cleanup 均通过；可公开的 aggregate 内容 SHA-256 为 `a76d167bb664e2ee3ee7514c39ac738b76cef37776d7b66e1175a8596329d0d9`。该事实仍只证明固定 Mock 单机 profile，不是 Provider 性能、生产 SLA、HA、安全认证或 Phase 2 整体完成证明。

## 5. `base_url` 与 SSRF

三个远程 Provider 类型都允许用户配置 `base_url`，Adapter 会根据显式类型调用 Chat Completions、OpenAI Responses 或 Anthropic Messages 路径。这是当前最高优先级的公开部署阻断项。

### 5.1 已实现的有限校验

- URL 必须是含 hostname 的绝对地址；远端 Provider 必须使用 `https://`，只有 `localhost` 或字面量 loopback IP 可使用 `http://`。
- 禁止内嵌 username/password、query 与 fragment，并去除末尾 `/`。
- Adapter 使用有限连接/读取超时及有限重试；普通配置型 4xx 不会无限重试。
- 除既有 retryable HTTP/transport 分类外，只重试显式白名单的 typed transient error：Responses 的 rate-limit/server error，以及 Messages 的 `rate_limit_error`、`api_error`、`overloaded_error`、`timeout_error`；Messages 的 HTTP `529` 也进入冻结的 retryable status 集。未知流内错误 fail closed，每个重试 HTTP attempt 都独立结算 ledger。
- Provider 请求禁用 HTTP redirect。CLI 从根地址或三个已知完整 endpoint 推导同级 `/models`；discovery 与生成都使用显式 Adapter 对应的认证 header，生成 endpoint 也由该类型决定。匹配的完整 endpoint 原样使用，其他已知协议后缀在发送 Key 前拒绝。系统不按模型名/URL猜测协议，也不在失败后跨协议 fallback。
- 模型发现与三类远程请求都发送 `Accept-Encoding: identity`，并在读取正文前拒绝压缩响应；discovery 聚合限制为 2 MiB/10,000 个模型 ID，Messages 分页另限制累计 100 页/60 秒 wall-clock，并拒绝重复或 `has_more=true` 时缺失 `last_id` 的继续游标。普通 JSON 成功体限制为 4 MiB，SSE 累计 wire/单事件/最终 content 分别限制为 64 MiB/1 MiB/4 MiB，非 2xx 错误体限制为 64 KiB，超限即中止而不保留整段正文。Chat、Responses、Messages 分别要求 `[DONE]`、`response.completed`、`message_stop` 终止事件，截断流不会保存部分答案。
- stored credential 的 AES-GCM AAD 绑定规范化 origin；改变 scheme/host/非默认 port 时必须重输 Key，active Run 期间禁止 endpoint/credential 更新，Worker 解密只接受 Run snapshot origin。这阻止 Key 被静默换绑到另一 origin，但不判断首次配置的 HTTPS 目标是否可信。

这些校验降低凭据经远端明文 HTTP 外泄和大/压缩响应耗尽内存的风险，但不保证目标安全。loopback HTTP 是明确支持的本地推理路径；RFC 1918 私网、IPv6 link-local、云元数据和其他敏感目标仍可能通过 HTTPS 被访问，DNS rebinding 也未防御。CORS 对服务端 SSRF 没有帮助。模型发现另有 10,000 个模型 ID 上限和 2 MiB 正文上限；远程协议普通 JSON 成功体为 4 MiB，SSE wire/单事件/聚合 content 为 64 MiB/1 MiB/4 MiB，错误体为 64 KiB。Anthropic Messages 的 `x-api-key` 与 Chat/Responses 的 `Authorization` 同属秘密 header，不得进入日志、异常或证据。

### 5.2 MVP 使用要求

- 仅管理员/仓库所有者配置经过人工核验的 Provider。
- 服务只监听 loopback 或可信网络；不要向不受信任用户开放 Model 写接口。
- 远端只使用 HTTPS 并验证证书；HTTP 只用于确认由本机操作者控制的 loopback 推理服务，不使用把凭据写进 URL 的反向代理。
- 在主机防火墙、容器网络或出站代理层拒绝云元数据、loopback、link-local 与内网网段；若必须访问本地推理服务，应为它建立精确的目标例外。
- 创建 Run 前确认 Benchmark 内容允许发送给目标 Provider。
- 把 Web 的 Demo `256/60s`、MMLU-Pro direct `1024/180s`、official CoT `4000/300s`、GPQA-Diamond `8192/600s` 只当作可编辑建议。更高输出预算和更长读取超时可能增加费用；Chat/Responses 的“由 Provider 决定”提交 `max_tokens:null`，只表示请求中省略对应输出字段，并不取消 Provider 自身限制或平台费用风险。Messages 不支持该选择，必须提交有限正整数 `max_tokens`。
- 把 `read_timeout_seconds` 理解为 LLMBenchLab 等待下一批 Provider 字节的空闲窗口，不是模型生成总时限；它不会配置 Worker→Provider 链路上的 Cloudflare、Caddy 或其他 Gateway。真实评测前必须另行核对代理的 SSE flush、buffering、空闲与绝对总时长。
- 真实 CLI 先使用小额 `--limit` 验证；只有确认模型发现/canary、响应解析、失败率和账单后才考虑 `--full`。不要把 CLI 的请求尝试上界误当作 Token 或金额硬上限。

### 5.3 公开部署前必须实现

1. 将 Model 管理限制为受授权管理员。
2. 建立 scheme/hostname/port allowlist；解析 DNS 后拒绝 loopback、private、link-local、multicast、unspecified、保留地址及云元数据目标，IPv4/IPv6 都要覆盖。
3. 在连接时重新验证解析结果，处理 DNS rebinding；重定向必须禁用或对每一跳重复校验。
4. 通过独立 egress proxy 或网络策略只允许获批 Provider，应用层校验不能替代网络层隔离。
5. 为本地 Provider 设计显式、审计可见的 opt-in，而不是放宽所有私网访问。
6. 在已有 Provider attempt governance 之外增加管理员/租户对象授权、入口按主体限流、目标 allowlist audit、失败熔断和 SSRF/DNS rebinding 回归；不能把共享本地 policy 当公网身份边界。

## 6. 不可信 Benchmark

上传文件、所有 JSON 字符串、metadata、Prompt、选项和答案一律视为不可信输入。

### 6.1 当前导入防护

- API 只接受 ZIP，压缩包最大 130 MiB。
- ZIP 必须恰好含根目录 `manifest.json` 和 `questions.jsonl`；拒绝重复、额外、嵌套、绝对路径、驱动器路径、反斜杠路径、目录、symlink/非普通文件、加密成员和不支持的压缩算法。
- 展开后 manifest 最大 1 MiB、questions 最大 128 MiB、单行最大 256 KiB、最多 20,000 题；单成员压缩比不得超过 100:1。这些放宽只为容纳固定的 12,032 题 MMLU-Pro，全量解析仍会消耗显著内存和磁盘。
- 严格使用 UTF-8 JSON；拒绝重复键、空 JSONL 行、非法 Unicode、NaN/Infinity、未知字段、未知题型、重复题目 ID、题数不符和不兼容 Evaluator。
- 本地目录 Loader 只读取两个固定名称的普通非 symlink 文件，验证解析后路径仍在数据集根目录。
- `source` 只是文本元数据；Importer 不下载它，不允许 manifest 指定任意本地文件。
- 不使用 `eval`，不加载插件，不执行题目、metadata 或归档中的任何代码。

标准数据转换器不是通用 URL importer：MMLU-Pro 与 GPQA-Diamond 的 URL、revision、预期大小和 SHA-256 固定在代码中；下载完成后先验证，缓存命中也重新验证，输出再经过相同 dataset-v1 Loader。下载只允许 HTTPS，若发生重定向最终 URL 仍必须是 HTTPS；当前没有 redirect host allowlist 或上游签名，所以网络层可信度仍依赖 TLS、固定 Hash 和代码 Review。GPQA 的公开 archive 密码不是 Provider/用户秘密，内层 Diamond CSV 还会单独校验 SHA-256。

详细格式见 [DATASET_FORMAT.md](DATASET_FORMAT.md)。

### 6.2 仍需人工负责的风险

- 合法格式仍可携带提示注入、冒犯内容、个人信息、商业秘密、错误答案、恶意许可证声明或训练数据污染内容。
- Run 会把 Prompt 发往所选 Provider；导入成功不代表允许外发。
- 允许范围内的大数据集仍会占用 CPU、内存、数据库空间和 Provider 预算。
- API 会返回题目、metadata 和参考答案。没有鉴权时，私有 Benchmark 不应导入本 MVP。
- Hash 只能证明规范化内容一致，不能证明来源、作者、许可证、未污染或未被篡改者签名。
- 标准数据题目和答案会落入 Git 忽略的 `artifacts/dataset-cache/`、`artifacts/benchmarks/`，并在执行后进入数据库。Git 忽略不等于加密或访问控制；不要同步到公共云盘或作为 CI artifact 上传。
- 前端必须把内容当纯文本渲染。未来若支持 Markdown，必须禁用原始 HTML、危险 URL 和脚本，并配置严格 CSP。

公开或多用户部署前还需要每用户配额、总存储限制、内容安全策略、数据分级/DLP、来源审批、删除/保留策略、审计事件和按 Benchmark 的访问控制。

## 7. 未来代码评测沙箱

MVP **没有代码执行能力**。在 Phase 3 之前不得通过 `subprocess`、`eval`、`exec`、动态导入、模板表达式或宿主 Docker socket 执行 Benchmark 提供的任何内容。

未来代码能力必须先形成独立威胁模型和 ADR，并至少满足：

- 在与 API、数据库、密钥和开发机隔离的专用 Worker/宿主上执行。
- 每题一次性、非 root、最小文件系统；只读基镜像和临时可丢弃工作目录。
- 默认无网络、无云元数据、无 Docker socket、无宿主路径挂载、无 Provider Key。
- 强制 CPU、内存、进程数、线程、文件描述符、磁盘、wall-clock 和输出字节上限。
- 使用 seccomp、AppArmor/SELinux、capability drop、no-new-privileges；高风险场景优先 microVM 或经过审计的隔离运行时。
- 防御 fork bomb、压缩炸弹、无限输出、编译器漏洞、缓存污染、侧信道和跨任务残留。
- 沙箱镜像固定 digest、持续补丁、生成审计证据，并对逃逸和资源耗尽做专门测试。

仅“在 Docker 中运行”不等于安全沙箱。

## 8. CORS、健康探针与网络暴露

- CORS Origin 来自配置；通配符 `*` 会在启动配置校验时被拒绝。
- 默认只允许本地 Vite Origin；允许的方法为 `GET/POST/PUT/PATCH/DELETE/OPTIONS`，请求头 allowlist 只含 `Accept` 和 `Content-Type`，但会向浏览器暴露响应中的 `X-Request-ID`；`allow_credentials=false`。API 忽略任何客户端 `X-Request-ID`，每次都生成新的 server-side UUID，防止攻击者把 write-only Key 复制到该头并迫使服务反射或记录。
- `TrustedHostMiddleware` 使用非空显式 Host allowlist并拒绝 `*`；不匹配的 Host 在 Model 写入/credential 持久化前被拒绝。这降低 Host-header rebinding 风险，但不是客户端认证。
- CORS 只约束浏览器，不阻止 curl、脚本、服务端请求或被攻陷的同源页面，不能替代认证、授权或防火墙。
- 生产前需要反向代理 TLS、认证/RBAC、请求体上限、速率限制、可信代理配置、安全响应头与审计日志。
- 不要在未理解代理头行为时信任 `X-Forwarded-*`；限制可信代理并验证 Host。

Compose 默认只把 API 和 frontend 端口发布到宿主 `127.0.0.1`，PostgreSQL 与 Redis 不发布宿主端口；容器内 API 的 `0.0.0.0` 监听只在 Compose 网络中使用。Web `api_key` 因此只允许在操作者信任的 loopback 链路提交；loopback HTTP 没有提供 TLS 保密性，任何远程访问都必须先建立 TLS 与访问控制。这些是本地暴露面缩减，不是认证、TLS、防火墙或生产网络策略。

健康端点也不是访问控制或完整监控：

- `/live` 只证明 API 进程能响应；`/health` 只检查数据库连接；`/ready` 检查数据库、Alembic head 与 Redis，并可能在 Redis 故障时返回 `503/degraded`，同时保留数据库提交和对账能力。
- `/tasks/metrics` 公开数据库当前任务/governance gauges 与匿名 Worker expected/registered/live/stalled/shortfall、最近聚合进展时间；`/tasks/history` 在同一数据库读取快照中逐条验证 retained audit 的 contract/hash/identity/retention，再公开 typed event counters 与 Run queue/execution/end-to-end p50/p95/p99。`/metrics/prometheus` 以固定 labels 输出同类 current/window/latency/Worker gauges，并用硬行数上限与 per-process single-flight 约束抓取压力。任一损坏 retained event 使整个 history/exporter fail closed 且不反射损坏值。pending/running cancel 都写 `run_cancel_requested`，dead-letter 单列 `run_dead_lettered`；`/runs/{id}/audit` 公开该 Run 的稳定分页 typed event。它们没有认证或租户隔离；无鉴权时这些计数、时序、ledger 关联和 Provider metadata 都是可读取的敏感运维元数据。
- Worker 主进程只把固定 scan/claim/progress/lease-heartbeat bit 合并写入 DB UTC `worker_processes`，API/exporter不返回 generation/worker ID。无真实 event 时 recorder 不刷新 `last_seen_at`，避免 timer 制造幽灵健康；异常退出的 generation 会保守变 stale。Worker 容器 probe 仍只检查数据库/head 和队列依赖能力并明确 `main_loop_progress=not_checked`；它**不证明 Worker 主事件循环正在推进**，不能替代 DB-time progress 告警。
- API readiness 把同步数据库检查放入 `asyncio.to_thread` 并限制等待时间；asyncio 超时不会取消已进入线程的数据库驱动调用，实际资源占用仍由数据库连接/驱动/池 timeout 约束。不得把 HTTP 探针 timeout 当作数据库查询硬中止。

Redis 仅可置于受控内部网络。当前本地 Compose 使用 AOF、无 ACL/TLS，不能暴露公网或共享开发网；更高信任级别部署必须启用 Redis ACL/认证、TLS、最小网络可达、磁盘权限与备份策略，并限制危险管理命令。Redis 消息只含 canonical UUID Run/correlation ID 与固定版本，不含 Prompt、答案、Provider Key 或权威 Run 状态；publish 和 delivery 两侧都拒绝非 UUID identity，防止受污染通知在数据库校验前进入 Worker structured log。清空、丢失或重复消息应只影响延迟/可用性，数据库事实不能被 Redis 覆盖。

## 9. 数据库、响应、迁移与备份

- PostgreSQL/SQLite、Redis AOF、备份和容器 volume 整体都没有存储层加密；只有 `model_credentials` 的 Provider Key 字段在应用层形成 AES-GCM envelope。题目、回答、endpoint、环境变量名和其他元数据仍是明文，应使用专用系统账号和最小文件权限保护，不放在云盘公共共享目录。Compose 中的 `llmbenchlab-local-only` PostgreSQL 密码只是隔离本地开发占位，不是生产秘密。
- 数据库备份可能比在线数据库保留更久，应应用同等或更严格的访问、加密、保留和销毁策略。部署 keyring 必须单独备份：不备份会失去恢复能力，与数据库备份放在一起则失去 envelope 的隔离价值。
- Responses API 包含 raw response、Prompt、标准答案和错误；Questions API 包含参考答案和 metadata。Web Run Detail 每次分页读取 100 条只限制单次展示量，不是授权、脱敏或数据隔离；Runs 列表中的详情链接也不会改变任何人的读取权限。不要把这些接口接入公开 Dashboard。
- Governance policy、四层 opaque scope/Token/费用聚合、逐 attempt ledger、typed audit/history/archive、Worker progress 和 Provider request/model/fingerprint/finish metadata 虽不含凭据正文，仍可暴露调用规模、费用趋势、内部身份关联和故障时间线，必须与 Run 证据同级保护。应用 append-only 与 payload/file hash 不阻止数据库管理员或本地文件所有者修改、替换或删除事实。
- 正式评测报告同样包含题目、参考答案、raw response、解析和错误证据。Exporter 创建权限收紧的目录/文件、拒绝覆盖已有目标并脱敏常见秘密形态；报告指标从计划题与 Responses 重算，`metrics_provenance` 会显式标注 Run 汇总字段漂移。操作者仍须像保护数据库一样保护 `summary.json`、`groups.csv` 与 `responses.jsonl`；脱敏不是内容访问控制。
- Run 快照用于复现，会保存模型/Benchmark 标识、`credential_source`、Model ID、endpoint，以及 environment 模式的变量名称；不会保存 credential row ID、ciphertext、nonce、key id 或 plaintext。设计环境变量名时避免包含机密业务信息。
- 删除 Model 会在存在历史 Run 时被拒绝，以保护证据；MVP 尚无合规删除、匿名化或数据生命周期功能。
- 备份与恢复步骤见 [DEPLOYMENT.md](DEPLOYMENT.md)。

显式 SQLite→PostgreSQL importer 会按依赖顺序复制 13 张核心/治理表的**完整内容**：`governance_policies`、`models`、`model_credentials`、`benchmarks`、`questions`、`governance_scopes`、`evaluation_runs`、`evaluation_responses`、`governance_minute_buckets`、`question_executions`、`provider_call_reservations`、`audit_events`、`worker_processes`。因此它会复制 encrypted credential envelope、题目/答案/快照/原始回答、typed Provider metadata、policy、Token/费用 ledger、audit 与 stopped/stale Worker facts，但不会解密或打印 row 内容。它只能在受信环境中针对停止写入、已在 Alembic head 且无 active reservation/live Worker generation 的源库和空目标执行；在打开目标前还会从全部 ledger 重算 scope/minute 计数，任何高/低漂移或缺 bucket 都拒绝导入。源库、目标库、对账输出与中间备份必须按最高敏感数据等级保护。工具输出只包含每表行数、主键集 SHA-256 digest 和 canonical row SHA-256 digest；摘要仍可能用于关联同一快照，不等于加密或访问控制。

- 含凭据的目标 DSN 必须通过 `--target-env ENV_VAR`（默认 `LLMBENCHLAB_DATABASE_URL`）读取；`--target` 只接受无密码 URL。仍需防止环境、进程转储和 CI 配置泄露 DSN。
- 退出码 `0` 表示提交后对账成功；退出码 `2` 表示提交前失败并回滚目标事务。
- 退出码 `4` 表示 PostgreSQL 未确认 `COMMIT` 结果：原子事务保证目标应为“空”或“完整”，但客户端不知道是哪一种。**禁止盲目重试**，必须先检查目标 13 表、Alembic head 和已有对账证据。
- 退出码 `3` 表示 `COMMIT` 已确认、但提交后验证或报告失败；导入已经提交。**禁止盲目重试或把它描述为回滚**，应先只读核验目标，必要时从已验证备份执行人工恢复。
- 工具是单向导入，不提供 PostgreSQL→SQLite 自动回迁。schema downgrade 也不等于数据平台回滚。

当前 head `20260830_0008` 将 `models.provider_type` 从 `VARCHAR(17)` 扩为 `VARCHAR(18)`，并同时替换 Provider 类型 check 与远程配置 check；它不改写旧 `mock`/`openai_compatible` 行。数据库中存在 `openai_responses` 或 `anthropic_messages` Model 时，`0008 → 0007` 在第一条 DDL 前拒绝，避免静默丢失新配置。其上游 `20260830_0007` 是 data-only 修复：它不改 schema、never-delete ledger、Provider actual usage、Response、audit 或 Run 终态，只依据冻结的 `evaluation_runs.input_token_reservation` 重算 `governance_scopes.overdrawn`。`0007` upgrade/downgrade 都会在任何更新前拒绝 `reserved/send_started` active reservation；downgrade 仅恢复旧派生谓词。`20260829_0006` 仍只补齐 canonical `0004` 的三个索引；兼容入口只接受 canonical schema，或缺失项为这三个索引的非空子集，以便 SQLite 非事务 DDL 中断后安全重入。新近成为 historical 的 PostgreSQL `0005/0006/0007` 必须按各自规则通过 metadata drift 校验；任何额外差异仍 fail closed。SQLite preflight 与 repair migration 都会在恢复 single-active 唯一索引前拒绝多条 active policy。`0006 → 0005` 不删除对象；`20260828_0005 → 0004` 会移除 Worker progress，guard 在第一条 DDL 前拒绝任何 generation 行；`20260827_0004 → 0003` 会移除治理/ledger/audit/Provider metadata并拒绝任何相关事实。正常运行后的数据库不可原地 downgrade；应优先向前修复，或恢复经核验的旧备份并保留只读证据。只有隔离空数据库用于双方向往返验证；详见 [OPERATIONS.md](OPERATIONS.md)。

## 10. 依赖与构建供应链

- Python 使用 `uv.lock`，Node 使用 lockfile；CI/可复现安装应采用 frozen/clean install，提交依赖变化时同时提交 lockfile。
- `pyproject.toml` 的兼容范围不是完整性保证，实际构建应以审查过的 lockfile 为准。
- 新依赖必须说明用途、维护状态和许可证；避免执行来源不明的安装脚本。
- 定期运行 Python/Node 漏洞审计，审查高危传递依赖，并在修复后运行完整离线测试。审计需要网络时应记录数据来源与日期。
- GitHub Actions 及容器基础镜像也属于依赖。公开发布前应将 Action 固定到完整 commit SHA、镜像固定 digest、生成 SBOM，并启用 Dependabot/Renovate 或等价流程。
- 自动升级不得绕过测试、协议兼容性和安全 Review。

当前开发基线的 lockfile 与 CI 只能降低漂移，尚未提供签名制品、SLSA provenance 或强制漏洞阻断门禁。

## 11. GitHub Secrets 与仓库卫生

Mock CI 不需要任何 Provider Key，也禁止配置真实模型凭据。标准数据测试使用内存 fixture/fetcher，Provider 预检测试使用 MockTransport，报告测试使用临时数据库；CI 不运行在线 `prepare` 或真实 `llmbenchlab-evaluate run`。未来确需部署秘密时：

- 在 Repository/Environment Secrets 中保存，不写 workflow YAML、Issue、PR、Actions input 默认值或 artifact。
- 优先使用环境级保护、最小权限、短期 OIDC 凭据；避免长期云访问密钥。
- 明确设置最小 `GITHUB_TOKEN` permissions；来自 fork 的 PR 不应获得写权限或部署秘密。
- 不在 `pull_request_target` 上检出并运行不可信 PR 代码。
- 不 `echo`、拼接到命令行、URL、矩阵、输出或缓存 key；mask 不是绝对防线。
- 启用 GitHub secret scanning/push protection（仓库套餐支持时），并在 Review 中检查 `.env`、数据库、日志、录屏和测试 fixture。
- 轮换策略和所有者应记录在私有运维系统，不能把真实值写进本文档。

如果秘密意外提交：立即吊销并创建新 Key，停止传播；再评估 Git 历史、fork、缓存、Actions log 和发布产物的清理。历史改写属于协作性破坏操作，必须由仓库维护者明确协调，不能私自强推。

## 12. 漏洞报告

请不要在公开 Issue、Discussion、PR 或日志中披露可利用细节、真实数据或凭据。

1. 优先使用 GitHub 仓库 **Security → Report a vulnerability** 的私密报告功能。
2. 若私密报告未启用，只创建不含漏洞细节的最小 Issue，请维护者提供私密联络方式。
3. 报告应包含受影响版本/commit、环境、影响、最小复现、是否已被利用，以及建议缓解；所有 Token、个人信息和真实 Benchmark 内容必须替换为安全占位符。
4. 在维护者确认修复并协调披露前，不公开 PoC。若凭据已经泄漏，不要等待软件修复，先在签发方吊销。

本个人项目目前不承诺固定响应 SLA。维护者应确认收到、分级、复现、修复、回归测试、轮换受影响秘密，并在适当时发布安全说明。

## 13. 公网/生产前安全门槛

下列事项未完成前，不得把实例称为生产就绪：

- 身份认证、对象级授权、管理员权限、安全会话/API Token，以及把现有 typed audit 送入受控归档/WORM 或等价独立安全日志。
- SSRF 应用层校验与网络层出站 allowlist。
- 将现有共享 DB governance 扩展为认证主体/租户 policy、Provider 熔断和生产级 Worker 隔离；在目标硬件与精确提交重跑真实工作负载容量/HA 验证，不能把当前 Mock dirty-worktree 基线当 SLA。
- 将现有 exporter/固定告警接入受控 Prometheus/Alertmanager、认证 Dashboard 与组织值班流程，并另行设计密码学签名/WORM 或等价不可抵赖审计；当前 DB-time Worker progress、普通文件 archive 和 Mock 故障演练不等于生产 HA、不可篡改存储或多主机恢复。
- TLS、反向代理请求体限制、安全响应头、CSP、Host/代理信任配置。
- 私有题目、参考答案、raw response 的访问控制、静态加密、保留与删除策略。
- 集中 secrets manager、Key 轮换、备份加密与恢复演练。
- 依赖/容器扫描、SBOM、Action SHA 固定、制品来源证明。
- 安全测试：鉴权绕过、IDOR、SSRF/DNS rebinding、恶意 ZIP、资源耗尽、XSS、限流、备份恢复和故障注入。
- 如引入代码题，完成第 7 节的专用隔离与逃逸测试。

部署限制与生产改造总表见 [DEPLOYMENT.md](DEPLOYMENT.md)；治理/settlement/恢复 Runbook 见 [OPERATIONS.md](OPERATIONS.md)，当前 Mock-only 容量证据与支持边界见 [PERFORMANCE.md](PERFORMANCE.md)。
