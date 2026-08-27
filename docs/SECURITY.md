# LLMBenchLab 安全说明

## 1. 适用范围与安全结论

LLMBenchLab 当前开发基线是供个人开发者在受信任机器上使用的本地评测工具，不是可直接暴露公网的服务。Phase 2 已交付 PostgreSQL、Redis 通知和独立 Worker 组成的可靠执行基础，但当前仍没有登录、授权、租户隔离、请求限流、TLS 终止或生产级秘密管理。任何能够访问 API 的人都能读取题目与参考答案、注册模型、导入数据集、启动可能产生费用的 Run，并读取原始模型输出。

安全默认姿势是：只监听本机或可信开发网络、优先使用 Mock、只导入自己审查过的数据集、只配置可信的 Provider 地址，并为数据库、`artifacts/` 与备份应用操作系统权限保护。可信本地正式评测 CLI 会进行模型发现、费用确认和最小 canary，但它不是公网安全层或预算控制器。

本文描述已实现控制、仍存在的风险和公开部署前的硬性改造。它不构成第三方 Provider 或 Benchmark 的安全保证。

## 2. MVP 威胁模型

### 2.1 需要保护的资产

- Provider API Key、访问令牌及其所在环境变量。
- Benchmark 题目、参考答案、metadata、许可证与来源信息；未来可能包含私有数据。
- 原始模型回答、解析结果、错误信息、运行配置快照和排行榜证据。
- PostgreSQL/SQLite 中的数据库事实、SQLite 源库与备份、Redis AOF/队列元数据、宿主机文件和本地网络访问能力。
- 用户的 Provider 配额、预算与账号信誉。
- GitHub 仓库、Actions 权限、发布产物和依赖供应链。

### 2.2 参与者与信任边界

| 参与者/边界 | MVP 假设 | 若假设失效的后果 |
| --- | --- | --- |
| 本机操作者 | 受信任，能管理环境变量和导入文件 | 可读取全部本地数据并发起付费请求 |
| API 客户端/浏览器 | 仅来自显式配置的本地前端 | 无鉴权接口被滥用、数据被读取或篡改 |
| Benchmark 作者 | 文件内容不可信，来源需人工判断 | 资源消耗、提示注入、答案污染、敏感数据外发 |
| OpenAI-compatible Provider | 地址和服务由用户信任 | 收集提示、伪造响应、消耗预算、返回恶意文本 |
| 独立 Worker | 受信任的执行边界，持有数据库/Redis 凭据并按需读取 Provider Key | 凭据泄漏、越权读写全部评测事实、重复外部调用 |
| PostgreSQL / Redis | 只在受控内部网络可达，数据卷由受信操作者保护 | 数据事实被篡改、任务被干扰、运行元数据泄漏或服务不可用 |
| GitHub/CI/依赖源 | 外部供应链 | 依赖投毒、Actions 权限滥用、秘密泄漏 |
| 宿主机上的其他进程/用户 | 不在应用隔离范围内 | 读取进程环境、数据库卷、SQLite、日志或备份 |

主要入口是 Model CRUD 中的 `base_url`、Benchmark ZIP 上传、Run 创建、题目/响应读取、前端渲染、API/Worker 日志、PostgreSQL/Redis、SQLite→PostgreSQL 导入、数据库/备份以及依赖安装与 CI。当前系统不执行 Benchmark 中的代码，这一点显著缩小了攻击面。

### 2.3 重点风险与当前状态

| 风险 | 影响 | 当前控制 | 剩余风险 |
| --- | --- | --- | --- |
| 明文密钥泄漏 | Provider 账号被盗用 | 只持久化 `api_key_env` 名称；运行时读取；错误与成功体当前 Key 反射脱敏 | 同机用户、错误配置、调试器、其他秘密和外部秘密存储不在保护范围 |
| 恶意 `base_url` / SSRF | 访问 localhost、内网、云元数据或敏感服务 | 远端只允许 HTTPS、HTTP 仅 loopback；拒绝 URL 内嵌凭据、query 和 fragment；禁用重定向 | **高风险：HTTPS 目标仍没有地址 allowlist、DNS/IP 分类或出站网络隔离** |
| 恶意 Benchmark ZIP | 路径穿越、压缩炸弹、内存/磁盘消耗 | 固定两文件、路径与类型检查、压缩比/大小/条数限制、严格 JSON | 仍可能消耗允许范围内资源或携带敏感/误导内容 |
| Prompt/数据外发 | 私有题目被发送给 Provider | 只有用户创建 Run 时才发送 | 无数据分级、DLP、Provider 策略或告知确认 |
| 固定数据源/缓存漂移 | 下载被替换、缓存投毒或第三方题目误提交 | 固定 HTTPS revision、大小和 SHA-256；缓存拒绝 symlink；`artifacts/` Git 忽略 | Hash 不是发布者签名；HTTPS redirect 只有 scheme 检查，许可与来源仍需人工复核 |
| XSS/内容注入 | 窃取页面数据或误导操作者 | React 默认文本转义；服务端不执行内容 | 未来引入 Markdown/HTML 时可能破坏边界；无 CSP |
| 未授权访问 | 读写全部评测数据、启动付费 Run | 建议仅绑定本机；CORS 显式配置 | CORS 不是鉴权，非浏览器客户端不受其约束 |
| 费用滥用 | 大量或重复上游请求造成费用 | Run 题内并发为 1–4、Run attempt 有限、本地 Response 幂等；真实评测 CLI 显示尝试上界、要求确认并先做小 canary | canary 可能计费；无 Provider 速率限制、预算、完整背压或熔断；Provider 调用不是 exactly-once |
| 数据丢失/篡改 | 证据不可复现 | 数据库约束、Hash、Run 快照、租约 fencing；PostgreSQL 是任务事实来源 | 数据库/备份未加密、无完整审计日志，访问与恢复仍由用户负责 |
| 后台任务中断 | Run 不完整或重复执行 | 独立 Worker、数据库租约/心跳/fencing、有限重试、dead-letter、数据库对账 | 不构成生产 HA；外部 Provider 副作用不能由本地幂等约束去重 |
| Redis 暴露或篡改 | 伪造/重放任务通知、拒绝服务、元数据泄漏 | Compose 不发布 Redis 宿主端口；消息不是事实来源且不含 Prompt/Key | 本地 Compose 未启用 Redis ACL/TLS；暴露到不可信网络会破坏可用性与保密性 |
| 供应链攻击 | 执行恶意依赖/Action | Python/Node lockfile、CI 检查 | 尚无强制 SBOM、签名验证或持续漏洞门禁 |

### 2.4 明确不在 MVP 保证内的事项

- 对拥有宿主机账号、容器宿主权限或数据库文件权限的攻击者提供隔离。
- 保证第三方 Provider 不保存、训练或泄漏收到的 Prompt，或对 Provider 调用/计费提供 exactly-once。
- 对结果真实性做密码学证明；Dataset SHA-256 用于一致性，不是发布者签名。
- 公网抗攻击、多租户隔离、合规认证、灾难恢复 SLA。
- 安全执行任意代码。当前格式没有代码题，任何 Benchmark 内容都不会被执行。

## 3. API Key 与秘密管理

### 3.1 数据流

1. Model 记录保存 `api_key_env`，例如 `LOCAL_COMPAT_API_KEY`，而不是密钥值。
2. 创建 Run 时快照保存的仍是环境变量名称。
3. 服务路径中的独立 Worker，或可信本地 CLI 启动的同进程 Runner，只在实际请求时从执行进程环境读取对应值。
4. Key 只在内存中用于构造 `Authorization: Bearer ...`，不进入 Model/Run/Response Schema。成功内容、raw usage 的对象键/字符串值、request ID、返回模型名、system fingerprint 和 finish reason 若精确包含当前 Key，会在离开 Adapter 前替换为 `[REDACTED]`。
5. 环境变量不存在时，该题记录安全的 `missing_api_key` 错误，不会尝试无凭据联网。

### 3.2 操作规则

- `.env`、shell history、终端录屏、Issue、PR、截图和 Benchmark metadata 都不是秘密存储。
- 本地开发可把值放在未提交的 `.env`；更稳妥的方式是使用操作系统 Keychain、密码管理器或临时 shell 环境。生产环境必须使用部署平台的 secret store。
- `.env.example` 只能包含变量名和无效占位符；不得复制后直接填值并提交。
- 给每个环境和用途使用独立、最小权限 Key；能设置预算、允许模型或来源 IP 时应开启。
- 怀疑泄漏时先在 Provider 侧吊销/轮换，再清理仓库历史和日志。仅删除当前文件不足以让已提交密钥失效。
- API 请求只传 `api_key_env`。Model Schema 将 `default_parameters` 限定为四个已知生成参数并校验类型/范围，同时拒绝 `base_url` query；任何真实 Secret 都不得进入请求 JSON。
- `llmbenchlab-evaluate` 没有 `--api-key` 参数。它只从 `--api-key-env` 指定的环境变量读取，或在交互终端用 `getpass` 隐藏输入；非交互且环境变量为空时必须停止，不能把 Key 改放进 argv。

数据库和 API 会公开环境变量**名称**。名称本身不应编码账号、项目机密或密钥片段。

### 3.3 Worker 秘密边界

- Provider Key 只应注入需要发起上游请求的 Worker，不应注入 frontend、浏览器、migrate 容器；API 只需处理 `api_key_env` 名称。当前本地 Compose 没有替用户注入任何真实 Provider Key，也不是 secrets manager。
- 本地 `make dev` 会让 API、Worker 和 frontend 开发进程继承同一份 `.env`；只有 Worker 代码会读取已登记的 Provider Key，且 Vite 不会自动把非 `VITE_*` 变量打入客户端，但这仍不构成进程级秘密隔离。需要更严格边界时应分别启动进程，并只向 `make worker`/Worker 容器注入 Key。
- Worker 同时需要数据库与 Redis 连接能力。生产设计应拆分 API/Worker/迁移数据库角色，限制 Worker 只能访问所需表和操作，并用独立的 Redis ACL、短期凭据及轮换流程代替共享本地凭据。
- Worker 日志、崩溃转储、进程列表和诊断端点都属于秘密边界。不得把环境、DSN、Authorization header、Prompt 或原始 Provider 请求写入日志或 artifact。
- at-least-once 只保证任务最终可再次处理；若 Worker 在 Provider 已响应而本地 Response 提交前崩溃，接管 Worker 可能重复上游调用和计费。本地 `(run_id, question_id)` 唯一约束不能消除这一外部副作用。

### 3.4 可信本地 CLI 秘密边界

- CLI 只适合受信任的交互式机器。它将读取到的 Key 临时放入 Run 快照所引用的进程环境变量，使现有 Adapter 能复用同一秘密接口；上下文结束时恢复原值或删除临时值。拥有同一用户权限、调试/进程转储能力的程序仍可能读取内存或环境。
- 模型发现 `GET /models`、最小 Chat canary 和正式题目请求都使用同一个 Key。任何发现到的模型 ID 若反射该 Key，预检立即失败；canary 若明确返回不同于请求目标的模型名也失败。CLI 只保存脱敏 preflight 元数据，不保存请求 header 或 Key；完整 Provider access log 不受本应用控制。
- 在 canary 前，CLI 打印 Provider host、目标模型、题数、剩余 Run attempts 和最大 Chat HTTP 尝试数，并要求输入 `RUN`；上界按 `(缺失题数 × 剩余 Run attempts + 1 个 canary) × 3` 包含 HTTP retries，但仍不是 Token/金额预算。`--yes` 只用于操作者明确授权的非交互运行，不是预算、速率限制或安全审批。
- `resume` 会再次读取 Key、确认并发送 canary，然后只处理本地缺失 Response。初次 canary 会固化到新 Run 快照，但 resume canary 当前不会追加为独立审计事件；逐题 request ID、返回模型名和 system fingerprint 也未持久化。远端调用不是 exactly-once，恢复前应同时检查 Provider 账单与是否仍允许继续。
- `report` 和 `prepare` 不需要 Provider Key；不得为了方便给这两条命令或 CI 注入真实凭据。
- 正式 CLI 必须在常规 API/Worker 停止后独占数据库。代码只能拒绝已有 `running` Run，不能识别空闲 Worker；若空闲 Worker 抢到新 `pending` Run，可能在错误的 Key/进程边界发起调用。

## 4. 日志与错误脱敏

当前应用日志和 OpenAI-compatible Adapter 的错误处理会：

- 对 LLMBenchLab 应用 logger 使用结构化 JSON、请求/Run correlation ID 和字段 allowlist；API 只记录代码定义的 route template，不记录原始查询串或请求体。
- 用 `[REDACTED]` 替换当前 API Key 的精确值。
- 识别常见 Bearer、Authorization、API key、token 和 secret 表达形式。
- 把上游错误折叠为单行并截断到 500 字符。
- 不保存请求头、完整上游请求或响应对象；Run 只持久化分类后的错误类型与可读消息。
- 对 Chat 成功内容、raw usage 的字符串值与对象键，以及 provider request ID、返回模型名、system fingerprint、finish reason 执行当前 Key 的精确替换，再允许其进入后续 Runner/preflight 边界。

仍需遵守以下规则：

- 不记录 `os.environ`、HTTP headers、完整异常 locals、请求体或 `.env` 内容。
- 结构化 JSON/字段 allowlist 只覆盖 LLMBenchLab 应用 logger，不覆盖全部 Uvicorn、access log、SQLAlchemy、Redis client 或其他第三方 logger；部署侧必须另行统一采集、过滤和保留策略。
- Uvicorn access log 仍可能记录方法、原始路径和状态码；因此秘密不得出现在 URL、查询参数或路径，反向代理 access log 也必须应用同一规则。
- `DEBUG` 仅用于无秘密的本地排障。SQL echo、HTTP wire logging 和第三方 SDK debug logging 默认关闭。
- 原始模型回答可能含供应商回显的其他敏感内容。当前只保证精确替换正在使用的 Key，并识别部分常见秘密形态；回答仍会作为评测证据持久化并通过 Responses API 返回，不能把“没有当前 Key”误当成“可以公开”。
- 异常报告、CI artifact 和截图发出前再次人工检查，不把数据库或 `.env` 整体上传。

脱敏降低偶发泄漏，不是数据访问控制；已经被未授权方读取的秘密必须轮换。

## 5. `base_url` 与 SSRF

`openai_compatible` 允许用户配置 `base_url`，Adapter 会调用其 Chat Completions 路径。这是当前最高优先级的公开部署阻断项。

### 5.1 已实现的有限校验

- URL 必须是含 hostname 的绝对地址；远端 Provider 必须使用 `https://`，只有 `localhost` 或字面量 loopback IP 可使用 `http://`。
- 禁止内嵌 username/password、query 与 fragment，并去除末尾 `/`。
- Adapter 使用有限连接/读取超时及有限重试；普通配置型 4xx 不会无限重试。
- Provider 请求禁用 HTTP redirect。CLI 从兼容根地址推导同路径的 `/models` 和 `/chat/completions`；若传入完整 `/chat/completions`，模型发现回到其同级 `/models`。
- 模型发现与 Chat 请求都发送 `Accept-Encoding: identity`，并在读取正文前拒绝压缩响应；发现体流式限制为 2 MiB，Chat 2xx 成功体限制为 4 MiB，非 2xx 错误体限制为 64 KiB，超限即中止而不保留整段正文。

这些校验降低凭据经远端明文 HTTP 外泄和大/压缩响应耗尽内存的风险，但不保证目标安全。loopback HTTP 是明确支持的本地推理路径；RFC 1918 私网、IPv6 link-local、云元数据和其他敏感目标仍可能通过 HTTPS 被访问，DNS rebinding 也未防御。CORS 对服务端 SSRF 没有帮助。模型发现另有 10,000 个模型 ID 上限；2 MiB 与 4 MiB/64 KiB 分别适用于发现和 Chat 响应。

### 5.2 MVP 使用要求

- 仅管理员/仓库所有者配置经过人工核验的 Provider。
- 服务只监听 loopback 或可信网络；不要向不受信任用户开放 Model 写接口。
- 远端只使用 HTTPS 并验证证书；HTTP 只用于确认由本机操作者控制的 loopback 推理服务，不使用把凭据写进 URL 的反向代理。
- 在主机防火墙、容器网络或出站代理层拒绝云元数据、loopback、link-local 与内网网段；若必须访问本地推理服务，应为它建立精确的目标例外。
- 创建 Run 前确认 Benchmark 内容允许发送给目标 Provider。
- 真实 CLI 先使用小额 `--limit` 验证；只有确认模型发现/canary、响应解析、失败率和账单后才考虑 `--full`。不要把 CLI 的请求尝试上界误当作 Token 或金额硬上限。

### 5.3 公开部署前必须实现

1. 将 Model 管理限制为受授权管理员。
2. 建立 scheme/hostname/port allowlist；解析 DNS 后拒绝 loopback、private、link-local、multicast、unspecified、保留地址及云元数据目标，IPv4/IPv6 都要覆盖。
3. 在连接时重新验证解析结果，处理 DNS rebinding；重定向必须禁用或对每一跳重复校验。
4. 通过独立 egress proxy 或网络策略只允许获批 Provider，应用层校验不能替代网络层隔离。
5. 为本地 Provider 设计显式、审计可见的 opt-in，而不是放宽所有私网访问。
6. 增加目标审计、速率/预算限制、失败熔断和 SSRF 回归测试。

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
- 默认只允许本地 Vite Origin；允许的方法为 `GET/POST/PATCH/DELETE/OPTIONS`，允许头为 `Accept`、`Content-Type` 和 `X-Request-ID`，并向浏览器暴露 `X-Request-ID`；`allow_credentials=false`。
- CORS 只约束浏览器，不阻止 curl、脚本、服务端请求或被攻陷的同源页面，不能替代认证、授权或防火墙。
- 生产前需要反向代理 TLS、认证/RBAC、请求体上限、速率限制、可信代理配置、安全响应头与审计日志。
- 不要在未理解代理头行为时信任 `X-Forwarded-*`；限制可信代理并验证 Host。

Compose 默认只把 API 和 frontend 端口发布到宿主 `127.0.0.1`，PostgreSQL 与 Redis 不发布宿主端口；容器内 API 的 `0.0.0.0` 监听只在 Compose 网络中使用。这些是本地暴露面缩减，不是认证、TLS、防火墙或生产网络策略。

健康端点也不是访问控制或完整监控：

- `/live` 只证明 API 进程能响应；`/health` 只检查数据库连接；`/ready` 检查数据库、Alembic head 与 Redis，并可能在 Redis 故障时返回 `503/degraded`，同时保留数据库提交和对账能力。
- `/tasks/metrics` 只公开数据库当前状态派生的 gauges。它没有历史 counters、延迟、完整审计或租户隔离；在无鉴权部署中，这些计数本身也是可被读取的运行元数据。
- Worker 容器 probe 只检查数据库/head 和队列依赖能力。Redis 不可用时它会报告 degraded 但以成功退出保留数据库对账路径；它**不证明 Worker 主事件循环仍在领取、心跳或推进任务**，不能替代 watchdog、进度告警或进程级 liveness。
- API readiness 把同步数据库检查放入 `asyncio.to_thread` 并限制等待时间；asyncio 超时不会取消已进入线程的数据库驱动调用，实际资源占用仍由数据库连接/驱动/池 timeout 约束。不得把 HTTP 探针 timeout 当作数据库查询硬中止。

Redis 仅可置于受控内部网络。当前本地 Compose 使用 AOF、无 ACL/TLS，不能暴露公网或共享开发网；更高信任级别部署必须启用 Redis ACL/认证、TLS、最小网络可达、磁盘权限与备份策略，并限制危险管理命令。Redis 消息只含内部 ID、版本与 correlation ID，不含 Prompt、答案、Provider Key 或权威 Run 状态；清空、丢失或重复消息应只影响延迟/可用性，数据库事实不能被 Redis 覆盖。

## 9. 数据库、响应、迁移与备份

- PostgreSQL/SQLite 数据库、Redis AOF、备份和容器 volume 都是明文静态数据；用专用系统账号和最小文件权限保护，不放在云盘公共共享目录。Compose 中的 `llmbenchlab-local-only` PostgreSQL 密码只是隔离本地开发占位，不是生产秘密。
- 备份可能比在线数据库保留更久，应应用同等或更严格的访问、加密、保留和销毁策略。
- Responses API 包含 raw response、Prompt、标准答案和错误；Questions API 包含参考答案和 metadata。不要把它们接入公开 Dashboard。
- 正式评测报告同样包含题目、参考答案、raw response、解析和错误证据。Exporter 创建权限收紧的目录/文件、拒绝覆盖已有目标并脱敏常见秘密形态；报告指标从计划题与 Responses 重算，`metrics_provenance` 会显式标注 Run 汇总字段漂移。操作者仍须像保护数据库一样保护 `summary.json`、`groups.csv` 与 `responses.jsonl`；脱敏不是内容访问控制。
- Run 快照用于复现，会保存模型/Benchmark 标识和环境变量名称。设计环境变量名时避免包含机密业务信息。
- 删除 Model 会在存在历史 Run 时被拒绝，以保护证据；MVP 尚无合规删除、匿名化或数据生命周期功能。
- 备份与恢复步骤见 [DEPLOYMENT.md](DEPLOYMENT.md)。

显式 SQLite→PostgreSQL importer 会复制五张核心表的**完整内容**，包括题目、参考答案、Prompt/模型快照、原始回答、错误和 `api_key_env` 名称。它只能在受信环境中针对停止写入、已在 Alembic head 的源库和空目标执行；源库、目标库、对账输出与中间备份必须按最高敏感数据等级保护。工具输出只包含行数和内容无关的 SHA-256 摘要，但摘要并不等于加密或访问控制。

- 含凭据的目标 DSN 必须通过 `--target-env ENV_VAR`（默认 `LLMBENCHLAB_DATABASE_URL`）读取；`--target` 只接受无密码 URL。仍需防止环境、进程转储和 CI 配置泄露 DSN。
- 退出码 `0` 表示提交后对账成功；退出码 `2` 表示提交前失败并回滚目标事务。
- 退出码 `4` 表示 PostgreSQL 未确认 `COMMIT` 结果：原子事务保证目标应为“空”或“完整”，但客户端不知道是哪一种。**禁止盲目重试**，必须先检查目标五表、Alembic head 和已有对账证据。
- 退出码 `3` 表示 `COMMIT` 已确认、但提交后验证或报告失败；导入已经提交。**禁止盲目重试或把它描述为回滚**，应先只读核验目标，必要时从已验证备份执行人工恢复。
- 工具是单向导入，不提供 PostgreSQL→SQLite 自动回迁。schema downgrade 也不等于数据平台回滚。

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

- 身份认证、对象级授权、管理员权限、审计日志和安全会话/API Token 设计。
- SSRF 应用层校验与网络层出站 allowlist。
- Provider 级限流、全局预算、完整背压、公平调度、容量上限与生产级 Worker 隔离；当前可靠执行基础不等于生产 HA。
- 完整历史 counters/延迟指标、不可抵赖审计事件、Worker 主循环 liveness、告警与经过演练的运行手册。
- TLS、反向代理请求体限制、安全响应头、CSP、Host/代理信任配置。
- 私有题目、参考答案、raw response 的访问控制、静态加密、保留与删除策略。
- 集中 secrets manager、Key 轮换、备份加密与恢复演练。
- 依赖/容器扫描、SBOM、Action SHA 固定、制品来源证明。
- 安全测试：鉴权绕过、IDOR、SSRF/DNS rebinding、恶意 ZIP、资源耗尽、XSS、限流、备份恢复和故障注入。
- 如引入代码题，完成第 7 节的专用隔离与逃逸测试。

部署限制与生产改造总表见 [DEPLOYMENT.md](DEPLOYMENT.md)。
