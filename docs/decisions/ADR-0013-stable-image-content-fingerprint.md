# ADR-0013：跨隔离 Compose trial 的稳定镜像内容指纹

- **Status**: Accepted
- **Date**: 2026-08-28
- **Deciders**: LLMBenchLab maintainers
- **Scope**: `P2-local-control-plane-v1` 跨 trial 环境身份
- **Amends**: [ADR-0012](ADR-0012-single-host-slo-capacity-qualification.md) 第 1 节的稳定 image ID 要求
- **Preserves**: ADR-0012 的固定 commit、配置、资源、统计、SLO 与 fail-closed 边界

## Context

实现 commit `c909f241ada1fbcc19d6ef7795ad30104cd151e6` 的第二次正式 qualification 中，warm-up 完整通过，随后 measured trial 在聚合前因 image ID 不同而 fail closed。只读比较证明两轮 backend 镜像的 RootFS layer 列表完全相同；差异只来自 Docker Compose v5 自动写入 image config 的随机 `com.docker.compose.project` 与 `com.docker.compose.service` labels。完整 image ID 因此同时编码了隔离 project 身份，不能充当跨 project 的稳定构建内容身份。

qualification 必须继续检测代码、依赖、基础镜像或可执行配置漂移，但不能因为每轮按合同创建不同 Compose project 而必然失败。直接忽略所有镜像事实又会削弱环境可比性。

## Decision

- child evidence 继续记录 Docker 返回的 raw `image_id`，并验证它是 `sha256:` 身份；该值只供逐轮本地审计，不参与跨轮相等判定。
- 另计算 `image_content_sha256`。其 canonical 输入为 image 的 `Architecture`、`Os`、`Variant`、有序 RootFS layer 列表，以及完整 image `Config`。
- 在 canonical 化 `Config.Labels` 时只移除已证实随隔离 project 改变的 `com.docker.compose.project` 与 `com.docker.compose.service`。Compose version、OCI、供应链和所有其他 labels 仍参与内容指纹；任一 RootFS layer、命令、入口、环境、用户、工作目录、健康检查或其他 label 变化都会改变 SHA。
- aggregate 的稳定环境指纹包含 `image_content_sha256` 与容器资源限制，不包含 raw image ID、container ID、hostname、PID 或随机 project label。
- 缺失/畸形 RootFS、Config、raw image ID 或 content SHA 都使 trial fail closed。content SHA 只输出摘要，不复制 image config 或其中的环境值。
- 本决定只修正本地隔离 Compose qualification 的身份语义；不定义 registry 签名、SBOM、远程镜像 provenance 或生产供应链认证。

## Consequences

### Positive

- 每轮独立 project 可以比较同一可执行镜像内容，不再被 Compose 运行身份误判为构建漂移。
- 真实代码、依赖、基础层或镜像配置变化仍会在 measured trial 聚合前被拒绝。
- aggregate 继续是严格 allowlist，不新增原始镜像配置或潜在环境内容。

### Negative

- raw image ID 不再是跨轮相等条件；审计者需同时查看 child raw ID 与 aggregate content SHA。
- 该摘要不是镜像签名，也不能证明 registry 来源或构建系统可信。

## Alternatives considered

- **继续要求完整 image ID 相同**：拒绝。隔离 project labels 会使正确的 1+5 suite 必然漂移。
- **完全忽略镜像身份**：拒绝。会允许实际 RootFS/Config 变化混入同一资格样本。
- **只比较 RootFS layers**：拒绝。相同文件系统但不同 `Cmd`、`Entrypoint`、`User` 或其他执行配置仍不可视为同一环境。
- **固定所有 trial 共用一个 Compose project/image label**：拒绝。会削弱 trial 隔离与失败 cleanup 边界。

## Validation

- 单元测试证明只改变 Compose project/service labels 时 content SHA 不变，而改变 Compose version、RootFS layer、`Config.Cmd` 或任一其他 label 时 SHA 改变；`Labels=null` 与空字典保持可区分，畸形 labels 或非小写十六进制的 raw/layer digest 会被拒绝。
- SLO 环境测试证明 raw image ID/container identity 变化不改变 normalized fingerprint，content SHA 变化会被拒绝。
- 第二次失败的两张 backend 镜像实际 rootfs/config content SHA 相同；失败 aggregate、两轮 child SHA 与 cleanup 事实保留在工作日志中。
- 修复 commit 后必须从全新 warm-up 开始执行完整 1+5；旧 warm-up 或 measured child 都不复用。

## Rollback

若实现回滚到只记录 raw image ID 的版本，就不能继续宣称跨独立 Compose project 的 `P2-local-control-plane-v1` qualification 可执行。回滚不影响数据库、API、Benchmark 协议或历史 child evidence；历史 raw image ID 仍可逐轮审计。
