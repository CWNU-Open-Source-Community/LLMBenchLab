# LLMBenchLab GitHub 协作流程

本文定义 Issue、分支、提交、Pull Request、Review、CI 和 Release 的协作规则。它与根目录
`AGENTS.md`、`CONTRIBUTING.md` 及 PR 模板共同生效。当前是个人项目也应保留可审计流程；“只有一个维护者”不是跳过测试、秘密检查或版本规则的理由。

## 1. 基本原则

- `main` 始终代表已通过必需检查、可供下一位贡献者使用的基线。
- 变更通过短生命周期分支和 PR 合入；不直接向 `main` 推送功能或修复。
- 每个阶段必须形成独立 commit、push 到 `origin`，并让该精确 SHA 的必需 CI 全部成功后才能标记完成。工作分支需有打开的 PR 才会触发当前 CI；用户明确授权的 `main` 直接交付由 `main` push 触发。
- 一个 PR 解决一个清晰问题，包含代码、测试、迁移和相关文档的完整闭环。
- 自动测试只使用 Mock 和临时 SQLite，不调用真实/付费 Provider。
- 不提交 API Key、`.env`、Authorization、Cookie、私有题目/回答、SQLite 或日志。
- 未实际运行的验证必须明确写“未运行”及原因；不得把计划写成结果。
- Benchmark 协议、dataset hash、评分和可比性优先于短期便利，不得静默改变。

## 2. 分支规范

### 2.1 长期分支

- `main`：唯一长期分支。
- 不维护长期 `develop`；Roadmap Phase 不是代码分支。
- Release 从已经验证的 `main` commit 打 tag，不保留长期 release 分支。

建议在 GitHub branch protection/ruleset 中为 `main` 开启：

- Require a pull request before merging。
- Require status checks to pass and branches to be up to date。
- 必需检查：`Backend lint and test`、`Backend PostgreSQL and Redis integration`、`Real Compose reliability acceptance`、`Frontend lint, test, and build`。
- Block force pushes 和 branch deletion。
- Require conversation resolution。
- 有第二位维护者时要求至少一次批准；高风险安全/协议/迁移变更必须由非作者 Review。

这些仓库设置不在 Git 中，维护者必须在 GitHub UI 中核实；本文不能证明规则已经启用。

### 2.2 工作分支

格式：`<type>/<short-kebab-description>`。全部使用小写 ASCII、连字符，避免姓名、秘密、Issue 正文或过长名称。

| 类型 | 用途 | 示例 |
| --- | --- | --- |
| `feat/` | 用户可见能力 | `feat/run-cancellation-ui` |
| `fix/` | 缺陷修复 | `fix/numeric-parser-ambiguity` |
| `docs/` | 纯文档 | `docs/compose-backup-guide` |
| `test/` | 测试与 fixture | `test/adapter-retry-boundaries` |
| `refactor/` | 无行为变化重构 | `refactor/runner-snapshots` |
| `chore/` | 工具、依赖、维护 | `chore/update-python-lock` |
| `security/` | 安全修复；名称不得泄漏利用细节 | `security/harden-import-validation` |
| `release/` | 短期发布准备 | `release/v0.2.0` |
| `codex/` | Codex 创建的工作分支 | `codex/document-mvp-api` |

从最新 `main` 创建，尽早推送到个人 fork/分支并开 Draft PR；合并后删除工作分支。不要在一个分支混入多个 Roadmap Phase。

## 3. Conventional Commits

提交标题使用 Conventional Commits：

```text
<type>(<optional-scope>): <imperative summary>
```

允许的常用 type：

- `feat`：新增用户可见功能。
- `fix`：修复缺陷。
- `docs`：仅文档。
- `test`：测试或 fixture，不改变产品行为。
- `refactor`：无用户可见行为变化。
- `perf`：可测量的性能改进。
- `build`：构建系统、容器或依赖。
- `ci`：GitHub Actions/CI。
- `chore`：其他维护。
- `revert`：显式回退既有提交。

建议 scope：`api`、`runner`、`adapter`、`evaluator`、`dataset`、`db`、`web`、`compose`、`docs`。

示例：

```text
feat(api): add cooperative run cancellation
fix(evaluator): reject conflicting final answers
docs(security): document base-url SSRF boundary
test(dataset): cover duplicate ZIP members
```

规则：

- 标题描述结果，简洁、祈使语气，不以句号结尾。
- 正文说明“为什么”和关键取舍，不重复 diff；需要时写验证与迁移影响。
- 用 footer 关联 `Refs:`/`Closes:`；确认全部验收通过前不要过早关闭 Issue。
- 不兼容变化使用 `type(scope)!:` 并在 footer 写 `BREAKING CHANGE:`。MVP 仍是 0.x，也不能隐藏破坏性变化。
- Commit 必须能通过秘密检查；不要先提交秘密再依赖后续 commit 删除。

允许为 Review 保留小步提交；合并时推荐 **Squash and merge**，PR 标题也应符合 Conventional Commits，使 `main` 历史可生成 Changelog。若保留多提交合并，每个提交都必须有意义且可构建。

## 4. Issue 流程

### 4.1 创建前

1. 搜索开放/关闭 Issue、Roadmap、阶段文档和 ADR，避免重复。
2. 确认属于当前 Phase；后续能力记录在对应 Roadmap，不借小修复静默扩范围。
3. 删除密钥、私有 Prompt、真实模型回答、未获授权数据和可利用安全细节。

仓库禁用空白 Issue，使用：

- **Bug report**：现象、最小复现、期望行为、环境和已脱敏日志。
- **Feature request**：用户问题、可观察结果、替代方案、领域和兼容性。

### 4.2 Triage

维护者应确认：

- 是否可复现/有足够上下文，影响与优先级是什么。
- 属于 bug、enhancement、documentation、security 或某一 Phase。
- 验收条件和非目标是否明确。
- 是否影响数据库、API、Benchmark 协议、Hash、安全边界或依赖。
- 是否需要 ADR、migration、协议版本提升或先拆分任务。

建议状态通过 Project/label 管理，例如 `needs-triage`、`accepted`、`blocked`、`needs-info`；没有实际维护流程时不要创建大量空标签。关闭 Issue 时说明结果 commit/PR，或清晰记录“不计划”的原因。

### 4.3 安全问题

可利用漏洞不得开公开 Issue。使用 GitHub **Security → Report a vulnerability**；若未启用，只发不含细节的最小通知，请维护者提供私密渠道。完整流程见 [SECURITY.md](SECURITY.md)。

## 5. 开发流程

开始实现前：

1. 阅读 `README.md`、`AGENTS.md`、`docs/PROJECT_STATUS.md`、`docs/ROADMAP.md` 和当前 Phase。
2. 检查 `git status --short --branch`，保留用户未提交工作。
3. 为任务创建工作日志；复杂任务按 `PLANS.md` 建计划。
4. 架构、公共 API、数据库、协议或安全边界变化先建立/更新 ADR。
5. 从最新 `main` 创建有语义的工作分支。

实施中：

- 先建立可失败的回归测试或明确验收 fixture，再实现最小完整行为。
- 不引入未说明依赖，不把 Roadmap 功能做成 TODO 壳。
- 数据库变化提供 Alembic migration；API 变化更新 `docs/API.md`；协议/数据变化更新相应版本与文档。
- 自动化测试始终使用 Mock；真实 Provider 验证只能由用户显式执行且不进入 CI。
- 定期检查 diff，避免格式化或生成物覆盖无关文件。

提交 PR 前：

```bash
make lint
make test
make smoke
cd frontend && npm run build
```

部署配置变化还需：

```bash
docker compose config
```

更新 `CHANGELOG.md`、`docs/PROJECT_STATUS.md`、当前 Phase、`docs/NEXT_TASK.md` 和工作日志，只记录已验证事实。

## 6. Pull Request 流程

### 6.1 创建 PR

- 非就绪工作使用 Draft，尽早暴露架构/兼容性方向。
- 标题符合 Conventional Commits；描述链接 Issue/ADR。
- 填完 `.github/PULL_REQUEST_TEMPLATE.md`，不能删除不适用检查项，应写明“不适用”及理由。
- 描述必须包含 Objective、Scope/Non-goals、用户影响、风险、迁移/回滚、精确测试命令与结果、未运行验证。
- UI 变更可附真实截图；不得伪造，也不得包含 Key、私有题目或模型输出。
- 控制规模。若代码、协议重构和无关依赖升级混在一起，应拆分为有依赖顺序的 PR。

### 6.2 PR 必查清单

仓库模板要求核对：

- 目标与范围清晰。
- 自动测试、失败路径和空状态完成。
- 文档与用户操作说明更新。
- migration 已提供或确认不需要。
- API 兼容性已保持，或契约/测试已更新。
- Benchmark protocol、dataset schema、Hash 与评分兼容，或已版本化。
- 没有 Key、`.env`、Authorization、Cookie 或私有模型数据。
- CI/Smoke 只用 Mock。
- Project Status、工作日志和 Changelog 已同步。
- 最终 diff 无无关改动和生成物。

### 6.3 更新与合并

- Review 中的新提交应聚焦反馈，不顺手扩大需求。
- 分支落后造成实际冲突或基线变化时，rebase/merge 最新 `main` 并重新跑门禁；不要为追求“绿色”无意义重写已审查历史。
- 所有必需 CI 通过、Review conversation resolved、文档/迁移齐全后才可转 Ready/合并。
- 推荐 Squash and merge，并再次校对最终 commit 标题。
- 合并后删除分支，确认 Issue、Project Status 和下一任务链接正确。

## 7. Code Review

Reviewer 先验证行为和风险，再讨论样式。至少检查：

### 7.1 通用

- 需求、范围和非目标一致，无隐藏的后续 Phase 实现。
- 错误路径不会吞异常或伪造成功；类型、边界和并发行为清楚。
- 测试能在修复前失败、断言有意义、没有真实网络、sleep 或共享开发数据库。
- 没有秘密、私有数据、调试日志、缓存、数据库、build artifact。

### 7.2 API/数据库

- 状态码、请求/响应 Schema、分页和错误 code 与 `docs/API.md` 一致。
- Response 不解析/返回 Key 值；上游错误已脱敏。
- migration 可前进、约束/索引/外键正确，升级和回滚风险已说明。
- UTC、null/zero 语义和历史 Run 复现证据未被破坏。

### 7.3 Benchmark/评分

- 输入限制、路径穿越/压缩炸弹、JSON 错误定位和许可证字段仍有效。
- Hash 规范、题目顺序、Prompt、Evaluator 或分母语义变化是否需要新版本。
- `score`、`completion_rate`、`answered_accuracy` 不被混用；错误题严格计 0。
- 不同 protocol/version/hash 不会无提示混排，Demo 标识不会丢失。

### 7.4 Runner/Adapter/安全

- 单题失败隔离、状态机、取消、重复领取和进程重启行为有测试。
- 重试有上限，普通配置型 4xx 不重试；Token 缺失保持 `null`。
- `base_url`、日志、CORS、依赖或权限变化已更新威胁模型。
- 新代码不执行 Benchmark 内容；未来代码题不得在 API 主机直接运行。

### 7.5 前端

- loading/empty/error/terminal 状态可操作；轮询会停止和清理。
- 不使用 `dangerouslySetInnerHTML` 渲染不可信 Prompt/回答；敏感字段不进入浏览器配置。
- 移动宽度、键盘/label/role、错误反馈和 Demo 警示可用。
- TypeScript、Vitest、ESLint、production build 均通过。

作者对 Review 意见应给出证据或取舍，不仅回复“已修”。Reviewer 若批准有剩余风险，应把它记录为 Issue/Next Task，不能隐含遗忘。

## 8. CI 规则

当前 `.github/workflows/ci.yml`：

- 对所有 Pull Request 和 `main` push 触发。
- 普通工作分支仅 push 不会触发当前 workflow；应创建或更新指向 `main` 的 PR，再等待该 SHA 的 PR CI。用户明确授权直接交付 `main` 时由 push 事件触发。
- 顶层 `GITHUB_TOKEN` 权限仅 `contents: read`。
- 同一 workflow/ref 的旧运行会被取消，避免浪费资源。
- backend 与 backend-integration job 超时 20 分钟，full-stack reliability 超时 35 分钟，frontend 超时 15 分钟。

### 8.1 Backend job

`Backend lint and test` 使用 Ubuntu、Python 3.12 和固定版本 uv：

1. `uv sync --frozen --extra dev`
2. `uv run ruff check .`
3. `uv run ruff format --check .`
4. 临时 SQLite `upgrade -> 0001 -> head` 与 `alembic check`
5. `uv run pytest -m "not integration"`

数据库位于 `${{ runner.temp }}` 的临时 SQLite；只设置一个未使用的环境变量**名称**，没有 Key。pytest 包含 Mock Smoke，真实基础设施用例在独立 job 执行。

### 8.2 Backend infrastructure integration job

`Backend PostgreSQL and Redis integration` 使用 PostgreSQL 16、Redis 7、Python 3.12 和专用测试数据库：

1. PostgreSQL `head -> 0001 -> head` 与 `alembic check`。
2. 收集全部 `integration` marker 用例，覆盖真实 PostgreSQL lease/竞态、Redis Streams 和 SQLite→PostgreSQL importer。
3. 生成 JUnit；用例数必须大于 0 且不得有 skip，否则 job 失败。

它只连接 CI service containers，不配置 Provider Key，也不调用真实模型。

### 8.3 Full-stack reliability job

`Real Compose reliability acceptance` 运行 `scripts/phase2_acceptance.py`，构建隔离的 PostgreSQL、Redis、migrate、API、双 Worker 和 frontend，执行八个真实故障场景。脚本只使用 Mock，使用唯一 Compose project 与随机 loopback 端口，成功或失败都精确清理；脱敏 evidence 作为短期 artifact 上传。

### 8.4 Frontend job

`Frontend lint, test, and build` 使用 Ubuntu、Node 22 和 npm lockfile cache：

1. `npm ci`
2. `npm run lint`
3. `npm test`
4. `npm run build`

`npm run build` 先执行 `tsc -b` 再执行 Vite build，因此也承担生产类型检查；构建期 API base 为同源 `/api/v1`。

### 8.5 CI 安全与维护

- CI 禁止加入真实 Provider Secret；增加 Key 来“修复”测试是流程违规。
- 不使用 `pull_request_target` 执行 fork 代码，不给 PR job 写权限。
- 失败检查不能通过 `continue-on-error`、跳过 marker 或降低断言绕过。
- 当前 Actions 使用受信任 major tag（如 `actions/checkout@v4`）；供应链加固应迁移到完整 commit SHA 并由依赖机器人维护。
- Workflow/Action/lockfile 变更必须经过同代码一样的 Review；脚本输出不得回显环境。
- 必需检查名称变化时同步更新 branch protection，否则可能意外失去门禁或永久阻塞。

CI 已覆盖本地可靠性 Compose，但成功仍不代表真实 Provider、生产编排、公网安全、HA、容量或灾难恢复已验证。PR 必须列出超出 CI 的实际/未运行验证。

### 8.6 阶段完成门禁

- push 后用 GitHub Actions run 的 `headSha` 核对它对应本阶段 commit，不能拿旧 commit 的绿色结果替代。
- 四个必需 job 必须全部为 `success`；`skipped`、`cancelled`、`neutral` 或仅部分 job 通过都不算阶段完成。
- CI 失败时保留失败 run，读取首个有因果信息的日志，修复后创建新 commit 并再次 push；禁止 rerun 偶然变绿来掩盖确定性缺陷。
- 若失败只由 GitHub/组织权限/runner 服务等外部条件造成，记录 run URL、阻塞事实和恢复条件，阶段保持 `in_progress`；本地通过不能替代远程门禁。
- 最终工作日志与交付回复必须记录 remote、branch、commit SHA、run URL 和四个 job 结论。

## 9. Release 规则

### 9.1 版本

项目版本遵循 SemVer：

- PATCH：向后兼容的修复/文档/安全补丁。
- MINOR：向后兼容的新能力；在 `0.x` 阶段也应谨慎说明实验性契约。
- MAJOR：不兼容 API、存储或用户工作流变化。

`protocol_version` 和 dataset `schema_version` 是独立兼容轴。应用 SemVer 提升不能替代协议升级；反之亦然。不兼容评分/Hash/Prompt/Evaluator 变化必须按协议文档处理。

### 9.2 发布步骤

1. 建立明确 milestone/Issue 范围，确认没有未披露的关键阻断。
2. 从 `main` 创建短期 `release/vX.Y.Z`，更新版本、`CHANGELOG.md`、状态/阶段和迁移说明。
3. 运行完整 lint、test、Mock smoke、前端 build、Compose config；需要发布镜像时还要实际构建和扫描。
4. 通过 Release PR 合入 `main`，所有必需检查绿色。
5. 维护者在该 `main` commit 创建 `vX.Y.Z` tag；推荐签名或受保护 tag。
6. GitHub Release notes 以 Changelog 为准，列出兼容性、migration、已知限制、安全注意和校验信息。
7. 发布后从干净环境验证安装/启动/迁移/Mock Smoke，并推进 Project Status。

不得创建“占位”或虚假 Release，不给未验证 commit 打正式 tag，不覆盖/重用已发布 tag。当前 CI 没有自动发布 job；新增发布自动化必须单独 Review、使用环境保护和最小权限。

### 9.3 Hotfix 与撤回

安全或严重回归从最新受支持 tag/main 创建最小 `fix/` 或 `security/` 分支，补回归测试，按正常 PR/CI 合入并发布 PATCH。若发布有害，应先在 Release 中明确标记、停止分发并给出升级/回滚建议；不要静默移动 tag。涉及泄漏的 Key 必须先吊销，不等待代码发布。

## 10. 秘密与敏感数据禁止项

禁止提交或粘贴到 GitHub：

- `.env`、API Key、Bearer、Cookie、私钥、云凭据、数据库密码。
- 含秘密的命令输出、截图、录像、trace、HTTP dump 或 Actions artifact。
- 本地 SQLite、备份、私有 Benchmark、未获授权数据、真实敏感模型回答。
- 把 Key 编进 `VITE_*`、Docker build arg、URL、Issue 表单、PR comment 或 test fixture。

提交前检查 staged diff 与文件列表；仓库支持时启用 secret scanning 和 push protection。GitHub Secrets 仅供确有需要的部署环境，Mock CI 不需要 Provider Key。Secrets 应最小权限、环境保护、可轮换，且不得在不可信 fork PR 中可用。

若意外提交秘密：立即在签发方吊销/轮换，通知维护者，检查 Git 历史、fork、Actions log、cache 和 release artifact。删除当前行不能撤回已传播数据；历史改写/强推必须由维护者协调，任何贡献者或自动化都不得擅自执行。

## 11. 合并完成定义

PR 只有在以下事实成立时才可合并：

- 范围内行为完成，无关键 TODO/占位或隐藏失败。
- 相关测试、lint、typecheck/build 已实际运行且通过；未运行项明确且风险可接受。
- API、migration、协议、数据、安全、部署文档已同步。
- CI/Smoke 完全离线，无真实 Key/Provider 请求。
- Demo、版本、Hash、评分和 UTC 语义保持清晰可复现。
- Changelog、Project Status、当前 Phase、Next Task 和工作日志反映真实状态。
- Review conversation 已解决，最终 diff 无秘密、无关改动或生成物。

完整 Definition of Done 以 `AGENTS.md` 为准；本流程不能用来降低其中的要求。
