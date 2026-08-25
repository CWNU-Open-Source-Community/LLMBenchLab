# 2026-08-25 — GitHub 发布与阶段 CI 门禁

## 元信息

- 日期：2026-08-25（Asia/Shanghai）
- 执行者：Codex
- 远程仓库：[`CWNU-Open-Source-Community/LLMBenchLab`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab)
- 分支：`main`
- 任务状态：首次发布与首次 CI 已完成；治理更新 commit 的最终完成状态由 push 后该精确 SHA 的 GitHub Actions check 决定，不能在 commit 内自引用其尚未产生的 run。

## 目标

将已有 LLMBenchLab Git 历史发布到 CWNU Open Source Community 组织，并把“每个阶段独立 commit、push、精确 SHA 远程 CI 全绿”固定为长期 Definition of Done。

## 范围与非目标

- 创建公开组织仓库并配置 `origin`，首次 push 当前 `main` 全部 7 个提交。
- 在公开前扫描全部提交中的常见密钥模式、误跟踪的 `.env`/数据库/构建产物和大文件。
- 等待首次 push 的四个 GitHub Actions job 全部成功。
- 更新协作规则、GitHub 工作流、Next Task、计划/工作日志模板、README、Project Status 和 Changelog。
- 本任务不创建 Release/tag，不修改 branch protection/ruleset，不创建或合并 PR，不调用真实 Provider，也不改变 Phase 2 产品状态。

## 初始状态与安全检查

- 本地 `main` 工作树干净，HEAD 为 `d2b9bc8`，没有 remote。
- GitHub CLI 登录账号具备 `repo`、`workflow`、`read:org` scope；组织允许成员创建公开/私有仓库，目标仓库尚不存在。
- 项目已有 MIT License 和开源协作文件；结合用户“上传到开源组织”的明确要求，创建为 `PUBLIC`。
- 扫描 7 个提交、177 个 tracked files：没有 `.env`、数据库、backup、密钥文件、`node_modules`、`dist` 或大于 25 MiB 的 tracked 文件。
- 唯一 Provider-Key 形态命中是 `backend/tests/test_api.py` 的固定假路径，用于证明未知用户路径不会进入应用日志；其 SHA-256 在三个包含该测试的提交中一致，没有真实凭据命中。
- 未执行 force push、历史改写、tag、Release、Issue、PR 或仓库设置变更。

## 首次发布与 CI 证据

- 仓库创建：`gh repo create CWNU-Open-Source-Community/LLMBenchLab --public --source=. --remote=origin --push ...`，退出 0。
- `main` 已跟踪 `origin/main`，首次远程 HEAD 为 `d2b9bc8f9d6319b6d626a61f6a936bce4e97026b`。
- GitHub Actions run：[`32822777304`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/32822777304)，结论 `success`。
- `Backend lint and test`：success。
- `Backend PostgreSQL and Redis integration`：success。
- `Real Compose reliability acceptance`：success，八场景与脱敏 evidence 上传完成。
- `Frontend lint, test, and build`：success。
- Actions 对旧版 JavaScript action runtime 发出 Node 20 弃用 annotation，但没有 job 失败；升级 Action major 需要独立依赖/供应链审查，未在本任务中静默扩展。

## 新的长期门禁

1. 每个阶段使用独立、可审查的 commit。
2. 提交前完成 staged diff、秘密和相关本地验证审查。
3. 将 commit push 到 `origin`；常规工作分支通过打开/更新 PR 触发 CI，用户明确授权的 `main` 直接交付由 push 触发。
4. 核对 GitHub Actions run 的 `headSha` 等于该阶段 commit；四个必需 job 必须全部为 `success`。
5. 失败时保留 run、修复后创建新 commit 并重新 push；绿色前保持 `in_progress`，不得用旧 SHA 或仅本地通过替代。
6. force push、Issue/PR 合并、Release/tag 和仓库设置不因阶段 push 授权而自动获准。

## 验证与交付边界

- 首次发布 commit 的远程 CI 已实际通过，不调用真实模型。
- 本治理更新在本地完成文档链接、格式与 diff 检查后提交；push 后必须等待该新 SHA 的四个远程 job。该 run URL 和最终结论由 GitHub commit checks 与任务最终回复记录，因为 commit 无法包含其自身 push 后才生成的 run ID。
- Phase 2 仍为 `in_progress`；本任务只改变交付治理，不补齐 P2-05/P2-06/P2-07。

## 下一步

按 [`docs/NEXT_TASK.md`](../NEXT_TASK.md) 继续 Phase 2。每个实施阶段遵循本日志与 [`AGENTS.md`](../../AGENTS.md) 的 commit/push/CI 门禁。
