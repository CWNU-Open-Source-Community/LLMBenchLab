# LLMBenchLab 安全说明

## 1. 适用范围与安全结论

LLMBenchLab MVP 是供个人开发者在受信任机器上使用的本地评测工具，不是可直接暴露公网的服务。当前没有登录、授权、租户隔离、请求限流、TLS 终止或生产级秘密管理。任何能够访问 API 的人都能读取题目与参考答案、注册模型、导入数据集、启动可能产生费用的 Run，并读取原始模型输出。

安全默认姿势是：只监听本机或可信开发网络、优先使用 Mock、只导入自己审查过的数据集、只配置可信的 Provider 地址，并为数据库与备份应用操作系统权限保护。

本文描述已实现控制、仍存在的风险和公开部署前的硬性改造。它不构成第三方 Provider 或 Benchmark 的安全保证。

## 2. MVP 威胁模型

### 2.1 需要保护的资产

- Provider API Key、访问令牌及其所在环境变量。
- Benchmark 题目、参考答案、metadata、许可证与来源信息；未来可能包含私有数据。
- 原始模型回答、解析结果、错误信息、运行配置快照和排行榜证据。
- SQLite 数据库及其备份、宿主机文件和本地网络访问能力。
- 用户的 Provider 配额、预算与账号信誉。
- GitHub 仓库、Actions 权限、发布产物和依赖供应链。

### 2.2 参与者与信任边界

| 参与者/边界 | MVP 假设 | 若假设失效的后果 |
| --- | --- | --- |
| 本机操作者 | 受信任，能管理环境变量和导入文件 | 可读取全部本地数据并发起付费请求 |
| API 客户端/浏览器 | 仅来自显式配置的本地前端 | 无鉴权接口被滥用、数据被读取或篡改 |
| Benchmark 作者 | 文件内容不可信，来源需人工判断 | 资源消耗、提示注入、答案污染、敏感数据外发 |
| OpenAI-compatible Provider | 地址和服务由用户信任 | 收集提示、伪造响应、消耗预算、返回恶意文本 |
| GitHub/CI/依赖源 | 外部供应链 | 依赖投毒、Actions 权限滥用、秘密泄漏 |
| 宿主机上的其他进程/用户 | 不在应用隔离范围内 | 读取进程环境、SQLite、日志或备份 |

主要入口是 Model CRUD 中的 `base_url`、Benchmark ZIP 上传、Run 创建、题目/响应读取、前端渲染、日志、数据库/备份以及依赖安装与 CI。MVP 不执行 Benchmark 中的代码，这一点显著缩小了当前攻击面。

### 2.3 重点风险与当前状态

| 风险 | 影响 | 当前控制 | 剩余风险 |
| --- | --- | --- | --- |
| 明文密钥泄漏 | Provider 账号被盗用 | 只持久化 `api_key_env` 名称；运行时读取环境；错误脱敏 | 同机用户、错误配置、调试器和外部秘密存储不在保护范围 |
| 恶意 `base_url` / SSRF | 访问 localhost、内网、云元数据或敏感服务 | 只允许绝对 HTTP(S)，拒绝 URL 内嵌凭据、query 和 fragment | **高风险：没有地址 allowlist、IP 分类或出站网络隔离** |
| 恶意 Benchmark ZIP | 路径穿越、压缩炸弹、内存/磁盘消耗 | 固定两文件、路径与类型检查、压缩比/大小/条数限制、严格 JSON | 仍可能消耗允许范围内资源或携带敏感/误导内容 |
| Prompt/数据外发 | 私有题目被发送给 Provider | 只有用户创建 Run 时才发送 | 无数据分级、DLP、Provider 策略或告知确认 |
| XSS/内容注入 | 窃取页面数据或误导操作者 | React 默认文本转义；服务端不执行内容 | 未来引入 Markdown/HTML 时可能破坏边界；无 CSP |
| 未授权访问 | 读写全部评测数据、启动付费 Run | 建议仅绑定本机；CORS 显式配置 | CORS 不是鉴权，非浏览器客户端不受其约束 |
| 费用滥用 | 大量上游请求造成费用 | Run 并发较小，重试次数有限 | 无用户预算、速率限制、题量成本预检或熔断 |
| 数据丢失/篡改 | 证据不可复现 | SQLite 外键、Hash 与 Run 快照 | 数据库未加密、无审计日志、备份需用户负责 |
| 后台任务中断 | Run 不完整 | 启动时把遗留 `running` 标记 `failed` | 不会自动恢复；不是生产级可靠队列 |
| 供应链攻击 | 执行恶意依赖/Action | Python/Node lockfile、CI 检查 | 尚无强制 SBOM、签名验证或持续漏洞门禁 |

### 2.4 明确不在 MVP 保证内的事项

- 对拥有宿主机账号、容器宿主权限或数据库文件权限的攻击者提供隔离。
- 保证第三方 Provider 不保存、训练或泄漏收到的 Prompt。
- 对结果真实性做密码学证明；Dataset SHA-256 用于一致性，不是发布者签名。
- 公网抗攻击、多租户隔离、合规认证、灾难恢复 SLA。
- 安全执行任意代码。当前格式没有代码题，任何 Benchmark 内容都不会被执行。

## 3. API Key 与秘密管理

### 3.1 数据流

1. Model 记录保存 `api_key_env`，例如 `LOCAL_COMPAT_API_KEY`，而不是密钥值。
2. 创建 Run 时快照保存的仍是环境变量名称。
3. `OpenAICompatibleAdapter` 在请求发生前从当前进程环境读取对应值。
4. Key 只在内存中用于构造 `Authorization: Bearer ...`，不进入 Model/Run/Response Schema。
5. 环境变量不存在时，该题记录安全的 `missing_api_key` 错误，不会尝试无凭据联网。

### 3.2 操作规则

- `.env`、shell history、终端录屏、Issue、PR、截图和 Benchmark metadata 都不是秘密存储。
- 本地开发可把值放在未提交的 `.env`；更稳妥的方式是使用操作系统 Keychain、密码管理器或临时 shell 环境。生产环境必须使用部署平台的 secret store。
- `.env.example` 只能包含变量名和无效占位符；不得复制后直接填值并提交。
- 给每个环境和用途使用独立、最小权限 Key；能设置预算、允许模型或来源 IP 时应开启。
- 怀疑泄漏时先在 Provider 侧吊销/轮换，再清理仓库历史和日志。仅删除当前文件不足以让已提交密钥失效。
- API 请求只传 `api_key_env`。Model Schema 将 `default_parameters` 限定为四个已知生成参数并校验类型/范围，同时拒绝 `base_url` query；任何真实 Secret 都不得进入请求 JSON。

数据库和 API 会公开环境变量**名称**。名称本身不应编码账号、项目机密或密钥片段。

## 4. 日志与错误脱敏

当前 OpenAI-compatible Adapter 的错误处理会：

- 用 `[REDACTED]` 替换当前 API Key 的精确值。
- 识别常见 Bearer、Authorization、API key、token 和 secret 表达形式。
- 把上游错误折叠为单行并截断到 500 字符。
- 不保存请求头、完整上游请求或响应对象；Run 只持久化分类后的错误类型与可读消息。

仍需遵守以下规则：

- 不记录 `os.environ`、HTTP headers、完整异常 locals、请求体或 `.env` 内容。
- 生产前增加统一的结构化日志脱敏 filter，而不能只依赖 Adapter 局部清洗。
- Uvicorn access log 会记录方法、路径和状态码；因此秘密不得出现在 URL、查询参数或路径。
- `DEBUG` 仅用于无秘密的本地排障。SQL echo、HTTP wire logging 和第三方 SDK debug logging 默认关闭。
- 原始模型回答可能含供应商回显的敏感内容。它会作为评测证据持久化并通过 Responses API 返回，不能把“不是 API Key”误当成“可以公开”。
- 异常报告、CI artifact 和截图发出前再次人工检查，不把数据库或 `.env` 整体上传。

脱敏降低偶发泄漏，不是数据访问控制；已经被未授权方读取的秘密必须轮换。

## 5. `base_url` 与 SSRF

`openai_compatible` 允许用户配置 `base_url`，Adapter 会调用其 Chat Completions 路径。这是当前最高优先级的公开部署阻断项。

### 5.1 已实现的有限校验

- URL 必须是含 hostname 的绝对 `http://` 或 `https://` 地址。
- 禁止内嵌 username/password、query 与 fragment，并去除末尾 `/`。
- Adapter 使用有限连接/读取超时及有限重试；普通配置型 4xx 不会无限重试。

这些校验只保证语法，不保证目标安全。`localhost`、RFC 1918 私网、IPv6 loopback/link-local、Unix 主机上可达的管理接口、云元数据服务和 DNS rebinding 目标目前都可能被访问。CORS 对服务端 SSRF 没有帮助。

### 5.2 MVP 使用要求

- 仅管理员/仓库所有者配置经过人工核验的 Provider。
- 服务只监听 loopback 或可信网络；不要向不受信任用户开放 Model 写接口。
- 优先 HTTPS，验证证书，不使用把凭据写进 URL 的反向代理。
- 在主机防火墙、容器网络或出站代理层拒绝云元数据、loopback、link-local 与内网网段；若必须访问本地推理服务，应为它建立精确的目标例外。
- 创建 Run 前确认 Benchmark 内容允许发送给目标 Provider。

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

- API 只接受 ZIP，压缩包最大 18 MiB。
- ZIP 必须恰好含根目录 `manifest.json` 和 `questions.jsonl`；拒绝重复、额外、嵌套、绝对路径、驱动器路径、反斜杠路径、目录、symlink/非普通文件、加密成员和不支持的压缩算法。
- 展开后 manifest 最大 1 MiB、questions 最大 16 MiB、单行最大 256 KiB、最多 10,000 题；单成员压缩比不得超过 100:1。
- 严格使用 UTF-8 JSON；拒绝重复键、空 JSONL 行、非法 Unicode、NaN/Infinity、未知字段、未知题型、重复题目 ID、题数不符和不兼容 Evaluator。
- 本地目录 Loader 只读取两个固定名称的普通非 symlink 文件，验证解析后路径仍在数据集根目录。
- `source` 只是文本元数据；Importer 不下载它，不允许 manifest 指定任意本地文件。
- 不使用 `eval`，不加载插件，不执行题目、metadata 或归档中的任何代码。

详细格式见 [DATASET_FORMAT.md](DATASET_FORMAT.md)。

### 6.2 仍需人工负责的风险

- 合法格式仍可携带提示注入、冒犯内容、个人信息、商业秘密、错误答案、恶意许可证声明或训练数据污染内容。
- Run 会把 Prompt 发往所选 Provider；导入成功不代表允许外发。
- 允许范围内的大数据集仍会占用 CPU、内存、数据库空间和 Provider 预算。
- API 会返回题目、metadata 和参考答案。没有鉴权时，私有 Benchmark 不应导入本 MVP。
- Hash 只能证明规范化内容一致，不能证明来源、作者、许可证、未污染或未被篡改者签名。
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

## 8. CORS 与网络暴露

- CORS Origin 来自配置；通配符 `*` 会在启动配置校验时被拒绝。
- 默认只允许本地 Vite Origin；允许的方法为 `GET/POST/PATCH/DELETE/OPTIONS`，允许头为 `Accept` 和 `Content-Type`，`allow_credentials=false`。
- CORS 只约束浏览器，不阻止 curl、脚本、服务端请求或被攻陷的同源页面，不能替代认证、授权或防火墙。
- 生产前需要反向代理 TLS、认证/RBAC、请求体上限、速率限制、可信代理配置、安全响应头与审计日志。
- 不要在未理解代理头行为时信任 `X-Forwarded-*`；限制可信代理并验证 Host。

本地建议让 API 绑定 `127.0.0.1`。绑定 `0.0.0.0` 是容器/局域网可达配置，不代表安全发布。

## 9. 数据库、响应与备份

- SQLite 数据库、备份和容器 volume 都是明文静态数据；用专用系统账号和最小文件权限保护，不放在云盘公共共享目录。
- 备份可能比在线数据库保留更久，应应用同等或更严格的访问、加密、保留和销毁策略。
- Responses API 包含 raw response、Prompt、标准答案和错误；Questions API 包含参考答案和 metadata。不要把它们接入公开 Dashboard。
- Run 快照用于复现，会保存模型/Benchmark 标识和环境变量名称。设计环境变量名时避免包含机密业务信息。
- 删除 Model 会在存在历史 Run 时被拒绝，以保护证据；MVP 尚无合规删除、匿名化或数据生命周期功能。
- 备份与恢复步骤见 [DEPLOYMENT.md](DEPLOYMENT.md)。

## 10. 依赖与构建供应链

- Python 使用 `uv.lock`，Node 使用 lockfile；CI/可复现安装应采用 frozen/clean install，提交依赖变化时同时提交 lockfile。
- `pyproject.toml` 的兼容范围不是完整性保证，实际构建应以审查过的 lockfile 为准。
- 新依赖必须说明用途、维护状态和许可证；避免执行来源不明的安装脚本。
- 定期运行 Python/Node 漏洞审计，审查高危传递依赖，并在修复后运行完整离线测试。审计需要网络时应记录数据来源与日期。
- GitHub Actions 及容器基础镜像也属于依赖。公开发布前应将 Action 固定到完整 commit SHA、镜像固定 digest、生成 SBOM，并启用 Dependabot/Renovate 或等价流程。
- 自动升级不得绕过测试、协议兼容性和安全 Review。

当前 MVP 的 lockfile 与 CI 只能降低漂移，尚未提供签名制品、SLSA provenance 或强制漏洞阻断门禁。

## 11. GitHub Secrets 与仓库卫生

Mock CI 不需要任何 Provider Key，也禁止配置真实模型凭据。未来确需部署秘密时：

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
- PostgreSQL、可靠 Worker、幂等/恢复、全局并发、速率与预算上限。
- TLS、反向代理请求体限制、安全响应头、CSP、Host/代理信任配置。
- 私有题目、参考答案、raw response 的访问控制、静态加密、保留与删除策略。
- 集中 secrets manager、Key 轮换、备份加密与恢复演练。
- 依赖/容器扫描、SBOM、Action SHA 固定、制品来源证明。
- 安全测试：鉴权绕过、IDOR、SSRF/DNS rebinding、恶意 ZIP、资源耗尽、XSS、限流、备份恢复和故障注入。
- 如引入代码题，完成第 7 节的专用隔离与逃逸测试。

部署限制与生产改造总表见 [DEPLOYMENT.md](DEPLOYMENT.md)。
