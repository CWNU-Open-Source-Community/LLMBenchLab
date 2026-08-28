# ADR-0014：双 backlog 场景的单机控制面 SLO v2

- **Status**: Accepted
- **Date**: 2026-08-28
- **Deciders**: LLMBenchLab maintainers
- **Scope**: Phase 2 P2-01 backlog 性能语义、Worker 参与证据与资格清理
- **Supersedes**: [ADR-0012](ADR-0012-single-host-slo-capacity-qualification.md) 的 `P2-local-control-plane-v1` 作为后续正式资格 profile；v1 合同与失败 evidence 保持不可改写
- **Preserves**: ADR-0012 的受支持拓扑、统计纪律、故障恢复、容量公式和外推边界，以及 [ADR-0013](ADR-0013-stable-image-content-fingerprint.md) 的镜像内容身份

## Context

精确 clean commit `dfa67abb1a9a0418a7e3337c179f816e3c69f121` 已从零完成 ADR-0012 规定的 1 个 warm-up 和 5 个 measured trial。六个 child 都通过 Mock-only、有限 policy、故障、公平、ledger 投影、唯一性和容器/volume/network cleanup 硬门禁，但 aggregate 只通过 18 项 SLO 中的 15 项，因此 `P2-local-control-plane-v1` 必须永久保持 `unqualified`：

- aggregate：`.pytest_cache/artifacts/phase2-slo/llmbenchlab-p2-slo-20260828T041254Z-5fde74882caf/evidence.json`
- SHA-256：`f993c11ff1a9f55921b5d7ea14974b0e3ca280f75427095c771ef3f5964ae3b2`
- measured-02 child SHA-256：`437492fe7e0d54d797f599410d475d0ef994ad28cb3f9a4f36010168da8e782f`
- measured-02 的 cold bounded burst queue/execution/E2E p95 分别为 `3.277036/5.936064/8.997677s`，超过 v1 的 `3/5/8s`；其余 15 项通过。

这不是可以删除的异常轮。该轮四个 Run 全部完成，零题错误、零 retry、零 ledger/counter 漂移，两个 Worker 容器最终 healthy，且同轮双 Worker baseline 已比其他轮慢。证据支持一次真实的本地 Docker/Worker 暂态退化，但不足以归因于数据库、Redis 或某个 Worker。

同时，v1 的 `bounded_queue_burst_and_drain` 把两种不同语义混在一个 cell 中：先完全停止 Worker，再创建 backlog，把 `docker start`、进程/consumer readiness、queue 和 steady drain 一起计时。dependency health 只证明 DB/Redis 可达，并不证明 Worker 主循环已开始 claim；child 又丢弃 audit 中的 `worker_id`，所以声明 `workers=2` 不能证明两个 Worker 都参与了该 cell。直接删除失败轮、反复重跑或只把 `3/5/8` 事后放宽都会破坏预登记纪律。

## Decision

### 1. 新 profile 与历史边界

- 后续正式资格使用 `P2-local-control-plane-v2` 和独立 aggregate schema；v1 的合同、三个资格尝试及上述失败结论永久保留，不追认通过。
- v2 仍使用 ADR-0012 的单主机、PostgreSQL 16、Redis 7、1 API、2 Worker、15 题 Demo、Mock 80ms、Run concurrency 1、quantum 5、backlog 4、`lease/heartbeat/poll=30/10/1s` 和 1 warm-up + 5 measured 纪律。
- v2 必须在新的 clean 精确 SHA 上从 warm-up 开始执行；v1 的任何 child 或 measured sample 都不得复用。
- 默认 `make phase2-capacity` 继续保留原来的单个 stop/start burst 工作负载；只有 `phase2-slo` child 可通过一个固定、不可拆分组合的 profile 开关启用 v2 双 burst，避免操作者挑选较有利的 cell。

### 2. 四个正式 measurement cell

每个 child 固定包含四个名称唯一的 measurement；single/multi 顺序继续按 seed 平衡，随后固定运行 warmed、再运行 cold：

1. `single_worker_reference`
2. `configured_multi_worker_baseline`
3. `warmed_pause_burst_and_drain`
4. `cold_start_burst_and_drain`

两个 burst 都是最终资格的 AND 门禁，不得把任一个降为描述性结果：

| Cell | 建立 backlog 前的 Worker 状态 | queue / execution / E2E p95 | 吞吐 | drain |
| --- | --- | ---: | ---: | ---: |
| warmed pause | 两个已初始化、healthy、idle Worker 同时 pause；验证 paused 后提交 | 每轮 `<= 3/5/8s` | one-sided 95% LCB `>= 6 q/s`，CV `<= 20%` | 每轮 `<= 10s` |
| cold start | 两个 Worker 完全 stop 后提交，再启动并等待 healthy | 每轮 `<= 6/8/10s` | one-sided 95% LCB `>= 6 q/s`，CV `<= 20%` | 每轮 `<= 10s` |

warmed 阈值原样保留 v1 对 DB 调度、backpressure 和 steady drain 的要求。cold queue `6s` 来自 ADR-0012 的首次 slice 上界 `5.8s` 向上取整；execution `8s` 与单 Worker execution 工程预算一致；E2E `10s` 与既有无新流量 backlog drain 预算一致。这些值在 v2 新样本运行前冻结，只能用于包含 cold startup/readiness 的独立 cell，不能回写 v1。

两个 burst 均必须：

- 六路并发提交精确得到 `4×202 + 2×typed 429 run_backlog_full`；四个接受 Run 全部完成且 backlog 回到 0；
- 从 durable `run_claimed` audit 证明恰有两个不同且映射到本轮已验证 Worker 容器的 claim owner；`peak_active_attempts=2` 或容器 healthy 不能替代该证据；
- 零题错误、零基础设施失败、完整 ledger/audit/projection 对账，并保留每个 Run 的多 dispatch/yield 证据。

### 3. 分段计时与安全 evidence

child raw evidence 使用数据库 UTC 和 monotonic wall clock 记录：suspend/stop 完成、backlog ready、restore command 完成、首个及两个 Worker 的首次 durable claim、相邻 claim/yield gap、每个 Run first-claim→finish 和总 drain。两个 burst 的吞吐 wall 固定从并发 submission 开始计到全部终态，drain 固定从调用 unpause/start **之前**计到全部终态，不能把 healthy 或首次 claim 重新选作起点。aggregate 只保留经过验证的匿名时长、distinct worker count/boolean 和 SLO 判定，不复制 worker/container/Run/audit ID。

`/ready` 的 dependency probe 仍不冒充 Worker progress；Worker 参与只由 typed audit 的 durable claim 事实证明。UTC 时间用于跨事件事实，monotonic 时间用于本进程 wall duration，两者不得混算为同一差值。

v2 每个 child 固定为 22 个 completed Run、330 个唯一 Response/QuestionExecution、331 个 reservation（330 `settled_actual`、1 个 lease fault 的 `settled_conservative`），Provider-attempt reserved/send-started/settled audit 各 331。公式为 `2×runs_per_phase + 3×backlog_limit + 2`，其中三个 backlog 组分别是 warmed burst、cold burst和公平性场景；正式 wrapper 仍锁定默认 `4/4` 配置及精确计数。

### 4. 项目镜像 cleanup

capacity trial 的 cleanup 在通用 Compose `down -v` 成功且本项目容器、volume、network 都为空后，才处理该 trial 构建的唯一 backend tag：

- 只按完整、正则验证过的随机 project label 枚举，并对每个完整 lowercase SHA-256 image ID 再次 inspect exact project label；该 ID 还必须等于 `down` 前从本项目已验证 api/migrate/worker 容器冻结的 backend image ID；
- 候选必须至多一个，且唯一 tag 必须等于内部推导的 `llmbenchlab-backend:p2-<project suffix>`，service label 只允许 `api/migrate/worker`，没有额外 alias，也没有任何容器引用；
- 只执行不带 `--force` 的 `docker image rm <exact tag>`；禁止按 ID 删除、通配、`image prune`、`compose down --rmi all` 或扩大到 acceptance 通用 cleanup；
- 所有 image list/inspect/reference/rm 调用不进入命令记录；inspect/删除/复核任一异常、共享 alias、引用或残留都 fail closed，但不升级为强制删除。child 只保存候选/删除/共享保留/残留计数，不复制 image Config、Env、labels、layer、raw ID、tag、命令或 stderr。

正式 v2 child 要求本项目容器、volume、network、唯一 backend build image 全部清理成功。该结论只覆盖本 trial 的 exact project，不宣称清空宿主全部 Docker cache 或历史 artifact。

## Consequences

### Positive

- 热态调度与冷启动恢复各自有明确 SLI；v1 的真实失败路径没有被删除或静默改名。
- 两个 Worker 的实际参与成为数据库可审计硬事实，dependency health 不再被当作主循环 progress。
- 每个 trial 不再稳定累积项目镜像，失败/共享边界保持保守且可审计。

### Negative

- 每个 child 增加 4 个 Run/60 题，完整 1+5 增加 24 个 Run/360 题，并增加一个 pause/unpause 失败面。
- cold 阈值只适用于固定 stop/start 语义；不能外推任意宿主冷启动、生产 autoscaling 或 HA 恢复。
- v1 和 v2 aggregate 不能合并；运维文档必须同时保留失败 v1 与后续 v2 结果。

## Validation

- 单元测试覆盖默认 capacity 兼容、固定 v2 四 cell 顺序、warmed pause 全路径 unpause、cold start 分段计时、两个 distinct validated Worker、22/330/331 对账及全部 SLO 方向/边界。
- cleanup 测试覆盖 exact 成功、label mismatch、额外 alias、容器引用、多候选、删除失败与删除后残留，并证明没有 force/prune/`--rmi`。
- 新实现 commit 先通过定向/全量/Mock smoke/Compose/acceptance/capacity 回归、staged secret 审查和精确 SHA CI；随后只在该 clean SHA 上执行全新 v2 1+5。
- v2 任一 cell、硬不变量、cleanup 或环境指纹失败，都保留整个 aggregate 并停止资格；不得只重跑失败轮或连续运行到通过。

## Rollback

回滚 v2 profile、双 burst 和 capacity-only 镜像 cleanup 不改变数据库、公共 API、协议或默认部署。默认 capacity 的原单 burst 工作负载必须保持可运行；若 v2 被回滚，就只能陈述 v1 已执行但 `unqualified`，不得恢复旧的完成声明。
