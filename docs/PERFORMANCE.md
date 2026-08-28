# LLMBenchLab 性能与容量基线

## 1. 结论与边界

Phase 2 的 `P2-local-control-plane-v2` 已在 clean commit `b6a35fef1dd069ebb54b69955058915c722aa34d` 完成正式 Mock-only 单机资格：从零执行 1 次 warm-up + 恰好 5 次 measured trial，四个 measurement cell、23/23 SLO、逐轮硬门禁和容量模型全部通过。v2 由 [ADR-0014](decisions/ADR-0014-dual-backlog-slo-profile.md) 接替 [ADR-0012](decisions/ADR-0012-single-host-slo-capacity-qualification.md) 的 v1；v1 已完成同样的 1+5，但只有 15/18 SLO，永久保持 `unqualified`。第 3、4 节的 dirty-worktree 与 clean `665244e…` 单轮容量基线继续作为历史记录保留，不能与正式 v1/v2 多轮证据混用。

这不是生产 SLO/SLA，也不能证明：

- 真实 Provider 的吞吐、延迟、限额或账单准确性；
- 不同硬件、容器配额、数据集、模型参数或 commit 上的容量；
- 无限横向扩展、生产 HA、灾难恢复时间或 Provider exactly-once；
- 单个 cell 仅有 4 个 Run 时，p95/p99 具有可外推的统计代表性。

基线只描述本页记录的工作树、硬件、配置和 Demo 数据。部署或治理策略有实质变化时必须重跑，不能把本页数值当作固定容量承诺。具体故障处理见 [OPERATIONS.md](OPERATIONS.md)，架构语义见 [ADR-0009](decisions/ADR-0009-database-governance-audit-fair-scheduling.md)。

## 2. 可重复入口

从仓库根目录运行：

```bash
make phase2-capacity
make phase2-slo
```

`make phase2-capacity` 执行 [`scripts/phase2_capacity.py`](../scripts/phase2_capacity.py) 的日常单轮三-cell 基线；`make phase2-slo` 则由 [`scripts/phase2_slo.py`](../scripts/phase2_slo.py) 固定调用 v2 四-cell child，并完成多轮统计。两者都使用随机 Compose project 和随机 loopback API 端口，移除已知真实 Provider Key 环境变量，只注册 Mock Model，把证据写入 Git 忽略目录，并在成功或失败后执行 scoped cleanup。日常 capacity 默认负载为：

```text
workers=2
runs_per_phase=4
backlog_limit=4
burst_runs=6
submit_concurrency=6
run_concurrency=1
question_quantum=5
questions_per_run=15
mock_generation_delay_seconds=0.08
lease_seconds=6
heartbeat_seconds=2
worker_poll_seconds=0.15
```

可用 `python3 scripts/phase2_capacity.py --help` 查看有界参数。`--workers` 最小为 2；脚本会先保留双 Worker 拓扑证据，再临时缩到 1 个 Worker 做参考测量，之后恢复到指定数量。证据包含：

- commit、dirty 状态、脚本/Compose SHA-256；
- OS、CPU、内存、Docker/PostgreSQL/Redis 版本与容器配额；
- Benchmark version/hash、题数、Worker/Run/提交并发，以及经 API apply/read-back 的 20 字段全有限治理 policy；
- Run/题吞吐，queue/execution/end-to-end 和题级 latency 的 p50/p95/p99；
- 停 Worker 后并发 burst 的逐请求状态码；默认必须精确得到 4 个 `202` 和 2 个带 `run_backlog_full`/`limit=4` 的 `429`，随后排空 4 个已接纳 Run；
- 终态、题错误、失败 attempt、lease acquisition、dispatch 与 cooperative yield；默认 `question_quantum=5 < 15`，每个测量 Run 必须至少经历两次 dispatch；
- 单 Worker 下高流量 Model 的 3 个 Run 与低流量 Model 的最后一个 backlog slot，并由 durable audit 顺序证明低流量 Run 在高流量 backlog 全部结束前获得 claim/slice；
- PostgreSQL stats 差值、数据库大小、任务 gauges、Redis Stream/group 压力；
- lease/Redis/duplicate fault 结果，以及 ledger/audit/reconciliation 最终不变量；
- 脱敏命令尾部、诊断与 cleanup 结果。

证据文件含 Run ID、容器 ID、数据 hash 和运维计数，仍应按内部运维数据保护；不得公开 Provider 数据、数据库内容或凭据。脚本不会调用真实 Provider。

### 2.1 `P2-local-control-plane-v2` 固定资格合同

`make phase2-slo` 不是可任意调参的容量搜索，而是在精确 clean commit 上串行执行 1 次 warm-up 和 5 次 measured trial；默认 seed 为 `20260828`，单/双 Worker cell 使用预先平衡的固定顺序，随后固定运行 warmed pause 和 cold start。warm-up 不进入统计，measured trial 不删除异常轮，也不允许只重跑失败 cell 后拼接证据。每个 trial 都调用隔离的 `phase2-capacity` child，并固定以下支持 profile：

| 维度 | `P2-local-control-plane-v2` 固定值 |
| --- | --- |
| 故障域 | 单台可信主机、一个 API、PostgreSQL 16、Redis 7、两个独立 Worker；不宣称 HA |
| 最低资源 | Host 至少 8 logical CPU / 8,000,000,000 bytes RAM；Docker 至少 8 CPU / 4,000,000,000 bytes memory |
| 数据库 | `max_connections >= 100`；API 与每个 Worker `pool_size=5`、`max_overflow=5`、pool timeout 2 秒 |
| 执行 | 每 Worker 同时 1 个 Run；Run concurrency 1；四个 cell；每 cell 4 个 Run；每 Run 为 Demo 15 题 |
| Mock 与预算 | Mock generation delay 80 ms；input reservation 256 Token；`max_tokens=64`；全有限 policy |
| 调度与背压 | backlog 4、并发提交 6、`question_quantum=5`；warmed pause 与 cold start 两个 burst 都必须精确 `4×202 + 2×429`，并由 durable audit 证明两个已验证 Worker 参与 |
| 恢复参数 | `lease/heartbeat/poll=30/10/1s`；`max_attempts=3`；retry backoff `base/cap=1/30s`；shutdown grace 30 秒 |
| Redis/连接参数 | block 1000 ms、operation timeout 1 秒；readiness DB timeout 2 秒 |

Compose 通过 `LLMBENCHLAB_COMPOSE_WORKER_MAX_ATTEMPTS`、`LLMBENCHLAB_COMPOSE_WORKER_RETRY_BACKOFF_BASE_SECONDS`、`LLMBENCHLAB_COMPOSE_WORKER_RETRY_BACKOFF_CAP_SECONDS`、`LLMBENCHLAB_COMPOSE_DATABASE_POOL_SIZE` 和 `LLMBENCHLAB_COMPOSE_DATABASE_MAX_OVERFLOW` 显式映射这些资格敏感值。每个 child 还从容器内应用 Settings 回读 lease、heartbeat、poll、retry、pool 和 Redis 参数，并把 PostgreSQL `max_connections`、只过滤 Compose project/service labels 后的稳定 image content SHA 与主机/容器资源纳入环境指纹；raw image ID 只作逐轮审计，Compose version、真正内容或资源漂移仍会使整个 suite 失败。

### 2.2 统计、硬门禁与 evidence

主 SLI 不信任 child 提供的汇总吞吐：wrapper 以每个 cell 的 `60 completed questions / wall_duration_seconds` 独立重算题吞吐，再按 trial 为单位计算样本均值、标准差、CV、双侧 95% 均值区间和单侧 95% Student-t lower confidence bound（LCB）。双/单 Worker scale 使用同一个 measured trial 内的配对吞吐比，避免把不同轮的主机噪声错误配对。资格阈值为：

| SLI | 预登记门槛 |
| --- | --- |
| 单 Worker 吞吐 | one-sided 95% LCB `>= 5 q/s`，CV `<= 15%` |
| 双 Worker 吞吐 | one-sided 95% LCB `>= 10 q/s`，CV `<= 15%` |
| warmed pause burst 吞吐 | one-sided 95% LCB `>= 6 q/s`，CV `<= 20%` |
| cold start burst 吞吐 | one-sided 95% LCB `>= 6 q/s`，CV `<= 20%` |
| 同 trial 双/单 Worker scale | 配对 ratio 的 one-sided 95% LCB `>= 1.50`，CV `<= 15%` |
| 各 trial p95 | 单 Worker queue/execution/e2e `<= 3/8/10s`；双 Worker `<= 2/5/7s`；warmed `<= 3/5/8s`；cold `<= 6/8/10s` |
| backlog 与恢复 | warmed/cold 每 trial drain 均 `<= 10s`；kill fence→reclaim `<= 38s`；lease expiry→reclaim `<= 6s`；Redis Run durable `created_at`→claim `<= 3s` |

每轮还必须满足零题错误、唯一 Response/operation/audit key、无 active/reserved/overdrawn 残留、Redis PEL/lag 清零、backpressure/fairness/fault/cleanup 全通过。最终 ledger 对账不是读取物化 counter 自证：capacity child 从不可删除的 Provider attempt ledger 独立重算 scope 与 UTC-minute projection，检查缺行、多行及每个 consumed/reserved 字段漂移，wrapper 再要求全部 projection drift 字段为 0。只有所有硬门禁和 SLO 同时成功，容量模型才输出 `qualified`；否则吞吐 LCB 派生的安全到达率保持 `not_qualified`。

v2 aggregate schema 为 `llmbenchlab-phase2-slo-evidence-v2`，默认写入 Git 忽略的 `.pytest_cache/artifacts/phase2-slo/<suite>/evidence.json`；各 raw child evidence 位于同一 suite 的 trial 子目录，也不提交或自动上传。aggregate 只复制 allowlist：精确 commit、脚本/Compose hash、稳定配置/环境指纹、child hash chain、脱敏 SLI/统计/判定、容量模型与 cleanup 摘要；不复制 child stdout/log、DSN/URL、环境变量、题目、Prompt/Response、keyring/envelope 或 Provider 数据。公开文档再采用更窄投影，不发布 raw child 路径、内部 Run/Worker/容器/模型/事件/镜像/lease 标识、宿主指纹或原始命令。child 运行在独立进程组；超时或中断后先给 scoped cleanup 最多 420 秒，再只针对该进程组升级终止。即使如此，artifact 仍是内部运维证据而非访问控制、WORM 或“操作者从未删除更早 suite”的证明。

GitHub-hosted CI 的共享 CPU、内存和 Docker 调度不稳定，因此 required jobs 只验证统计函数、validator、失败路径与既有可靠性，不执行上述绝对吞吐/延迟资格。正式数值只能来自目标 clean commit 的受控本机，并必须与该精确 SHA 的公开 CI 正确性门禁共同记录。`make phase2-slo --self-check-only` 不是 Make target 支持的参数形式；如需纯合同自检应直接运行 `python3 -I scripts/phase2_slo.py --self-check-only`，它不执行 Docker trial，不能替代正式资格。

### 2.3 正式 v2 结果与 v1 历史

v2 的公开追溯只引用 aggregate；raw child 路径、内部对象标识和宿主指纹不进入本文。

| 项目 | 正式 v2 记录 |
| --- | --- |
| Profile / schema | `P2-local-control-plane-v2` / `llmbenchlab-phase2-slo-evidence-v2` |
| 实现与远程门禁 | clean commit `b6a35fef1dd069ebb54b69955058915c722aa34d`；[GitHub Actions run 33146681285](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33146681285) 4/4 必需 job 成功 |
| Aggregate | `.pytest_cache/artifacts/phase2-slo/llmbenchlab-p2-slo-20260828T060722Z-87d7a8af7f91/evidence.json`；SHA-256 `a76d167bb664e2ee3ee7514c39ac738b76cef37776d7b66e1175a8596329d0d9` |
| 执行与判定 | 1 次 warm-up + 恰好 5 次 measured，本次 invocation 的 `discarded_trials=0`；四个 cell、23/23 SLO、全部逐轮硬门禁通过；容量模型 `qualified` |

五轮 measured 的匿名统计如下；p95 是五轮中最差的一轮值，而非跨轮合并样本：

| Cell / ratio | one-sided 95% LCB | CV | 最大 queue / execution / E2E p95（秒） |
| --- | ---: | ---: | ---: |
| 单 Worker | 6.800966 q/s | 4.3298% | 2.775371 / 6.126339 / 8.786802 |
| 双 Worker | 11.603003 q/s | 5.2926% | 1.309017 / 3.798312 / 5.052355 |
| warmed pause burst | 9.486195 q/s | 0.9531% | 1.101793 / 4.094106 / 5.114906 |
| cold start burst | 9.324905 q/s | 1.0258% | 1.994580 / 4.019623 / 5.952585 |
| 同 trial 双/单 Worker ratio | 1.628400 | 5.8351% | 不适用 |

warmed/cold 最大 drain 分别为 6.253076/6.350613 秒；kill fence→reclaim、lease expiry→reclaim、Redis durable create→claim 的最大观测值分别为 33.752306/4.031426/1.052824 秒。每个 warm-up/measured child 都精确对账 22 Runs、330 Responses、330 QuestionExecutions 和 331 reservations（330 actual + 1 conservative），零题错误、重复 key、active/reserved 残留、Redis PEL/lag。每轮 scoped cleanup 都移除本项目唯一 build image，并令本项目容器、volume、network、image 为零；suite 后的 exact-project live 复核也为零。

五个 measured trial 中，每个 cell 共观察 300 道题且错误数为 0；在独立同分布 Bernoulli 近似下，零事件的单侧 95% 描述性上界为 `0.009936081944`。这不是错误率为零、99.9% 可用性或生产可靠性证明，也不替代逐轮 hard correctness 门禁。

容量模型以双 Worker LCB 11.603003 q/s 乘 0.70 安全系数，得到 8.122102 q/s，再按 15 题/Run 得到 0.541473 Run/s；估计无新流量 backlog drain 为 5.171075 秒。冻结的首次 slice、lease expiry→claim、kill fence→claim 工程上界分别为 5.8/6.0/36.0 秒。一个 API 加两个 Worker 的应用连接上界为 30，低于 PostgreSQL 运维预留后的 80；这些数字只能用于同一 Mock 单机 profile。

v1 历史不能被 v2 覆盖：clean commit `dfa67abb1a9a0418a7e3337c179f816e3c69f121` 的 [GitHub Actions run 33141140969](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33141140969) 4/4 成功，并完整执行 1+5；aggregate `.pytest_cache/artifacts/phase2-slo/llmbenchlab-p2-slo-20260828T041254Z-5fde74882caf/evidence.json` 的 SHA-256 为 `f993c11ff1a9f55921b5d7ea14974b0e3ca280f75427095c771ef3f5964ae3b2`。六轮硬正确性门禁通过，但 aggregate 只有 15/18 SLO，因此结论永久为 `failed/not_qualified`；当时本项目容器、volume、network 已清理，六个 build image 作为 Docker cache 留存，不能追写为全资源零残留，也不能删除失败轮或复用其样本。

## 3. 2026-08-27 增强前的历史 dirty-worktree 基线

### 3.1 可追溯性

| 项目 | 记录值 |
| --- | --- |
| 运行时间 | 2026-08-27 15:19:37–15:21:45 UTC（Asia/Shanghai 23:19:37–23:21:45） |
| Git HEAD | `1cd19c51ed309316047a18ed3b2a308647af495d` |
| 工作树 | `dirty=true`；包含本轮尚未提交的 Phase 2 治理实现 |
| evidence 本地路径 | `.pytest_cache/artifacts/phase2-capacity/llmbenchlab-p2-bb49c6069785/evidence.json` |
| evidence SHA-256 | `f78886422eeb1d6b54c3fe1da401fd411042a6b4421aeae3f4f5e7ef43444340` |
| capacity script SHA-256 | `83c13eabad622019a5f294b82c42b21eee88d48f89e3e404c0447e7f8c4260b8` |
| acceptance script SHA-256 | `0d201b58e88200e23a8965fc471c31e36c7489560e719cd037fd064549521b11` |
| Compose SHA-256 | `53d77e21720eecdcb255399def4b38840a0615a302cb501c70c09d6692c80b6d` |

因为该次运行明确记录了 `dirty=true`，它只是当时工作树的历史基线，不是 HEAD commit 单独可复现的精确 SHA 绿色证明，也不是当前候选或增强 capacity harness 的通过证据。表中的 script SHA 对应增强前脚本；当前 clean 候选的独立证据见第 4 节。新的容量运行仍应新增记录，而不是覆盖这里的事实。

### 3.2 环境与数据

| 项目 | 记录值 |
| --- | --- |
| Host | Darwin 25.5.0, arm64, 8 logical CPU, 8 GiB RAM |
| Docker Desktop | Server 29.7.2, aarch64, 8 logical CPU, 4,108,632,064 bytes memory |
| PostgreSQL | 16.14 |
| Redis | 7.4.10 |
| 容器配额 | API/PostgreSQL/Redis/Worker 的 CPU、memory、PID 显式 limit 均未设置；共享 Docker Desktop 资源 |
| Benchmark | Demo `1.0.0`, 15 题，hash `5c51bb4fa42fc6aa2e8b0b95bb7e37ef8bdff8b6fa4eecfb66da5d4faf755afe` |
| Model | `mock`，无 Key、无网络、无费用 |
| 协议 | `llmbenchlab-protocol-v1` |

Mock Response 的 `latency_ms=1` 是确定性测试证据，不等于 80 ms 人工 generation delay 或真实 Provider latency；容量比较使用数据库时间和墙钟完成时间。

### 3.3 结果

每个测量阶段为 4 个 Run、60 道题。分位数采用排序样本上的线性插值；每阶段只有 4 个 end-to-end 样本，必须把 p95/p99 视为烟雾基线，不作统计外推。

| 场景 | Worker | 墙钟秒 | Run/s | 题/s | E2E p50 / p95 / p99 秒 | queue p95 秒 | execution p95 秒 | 题错误 | failed attempt |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 单 Worker 参考 | 1 | 7.422401 | 0.538909 | 8.083637 | 4.578112 / 6.953858 / 7.167727 | 5.179710 | 1.912167 | 0 | 0 |
| 双 Worker 基线 | 2 | 4.085257 | 0.979131 | 14.686958 | 2.951079 / 3.899837 / 3.907275 | 2.015659 | 2.010238 | 0 | 0 |
| 停 Worker 后 4 Run 受控积压并排空 | 2 | 6.423057 | 0.622756 | 9.341347 | 3.943747 / 4.943670 / 4.945666 | 2.951674 | 1.992746 | 0 | 0 |

观测到双 Worker 的题吞吐为单 Worker 的约 1.82 倍，但脚本不对此设置 pass/fail 比率；样本、锁竞争、Docker 调度和 Mock delay 都不足以支持线性扩展结论。当时的受控积压场景在 Worker 停止时记录 `pending=due_pending=managed_backlog=4`，4 次 admission 全部成功；它只验证当时 backlog 的排空，不等于触达当时默认 `backlog_limit=1000` 的拒绝阈值，更不能证明当前脚本要求的 4 个 `202`、2 个 `429`、cooperative yield 或跨 Model 公平性。

数据库压力差值如下；这些是 PostgreSQL 累计 stats 的窗口差值，不是应用错误计数：

| 场景 | xact commit / rollback | block hit / read | tuple insert / update | deadlock / conflict / temp file |
| --- | ---: | ---: | ---: | ---: |
| 单 Worker | 1045 / 293 | 42454 / 71 | 646 / 1864 | 0 / 0 / 0 |
| 双 Worker | 919 / 206 | 36343 / 0 | 447 / 1721 | 0 / 0 / 0 |
| 受控积压 | 863 / 125 | 40709 / 0 | 452 / 1752 | 0 / 0 / 0 |

基线终点数据库大小约 11.1 MB。Redis 最终 `entries_added=14`、consumer group `pending=0`、`lag=0`；consumer 数包含扩缩和重启期间留下的 consumer identity，不能直接当作当前 Worker 数。

### 3.4 故障和最终对账

- 实际 lease owner `SIGKILL` 后，Run 在 6 秒数据库租约自然过期后由 peer 以新 token 恢复；15 条 Response 唯一且完成，`failed_attempt_count=1`。裂缝中的一个 `send_started` attempt 被保守结算。
- Redis 与 Worker 先停止后，API 在 0.033 秒内提交 Run 并记录 `queue_notification_unavailable`；Worker 在 Redis 仍停止时仅靠数据库扫描完成 Run，Redis 恢复后 `/ready` 回到 `ready`。
- 对已完成 Run 重复 `XADD` 后，Redis ACK 完成且 Run/Response canonical hash 不变。
- 最终共有 14 个 completed Run、210 条唯一 Response、210 条 `settled_actual` 和 1 条预期 `settled_conservative` reservation、900 条 typed audit event。
- 最终 active Run/reservation、scope/minute reserved 数、overdrawn scope、重复 operation key、重复 audit event key、题错误、Redis PEL/lag 全为 0。
- 隔离 Compose cleanup 的容器、volume、network 均为 0；evidence 对示例数据库密码、Authorization/Bearer 和 Key marker 的扫描无命中。

## 4. `665244e` 增强候选基线

### 4.1 可追溯性与门禁

| 项目 | 记录值 |
| --- | --- |
| Git commit / 工作树 | `665244e095905083b606b8e98e946ed1a02dc0fc`；`dirty=false` |
| capacity 时间 | 2026-08-27 17:36:38–17:38:16 UTC（Asia/Shanghai 2026-08-28 01:36:38–01:38:16） |
| capacity evidence | `.pytest_cache/artifacts/phase2-capacity/llmbenchlab-p2-51cfadee04f5/evidence.json` |
| capacity evidence SHA-256 | `40deadebc357bbb24a07c91b05eb39f3d2fb7de11a28da9a7f95871c7acd0588` |
| acceptance 时间 | 2026-08-27 17:38:39–17:40:51 UTC（Asia/Shanghai 2026-08-28 01:38:39–01:40:51） |
| acceptance evidence | `.pytest_cache/artifacts/phase2-acceptance/llmbenchlab-p2-afe52c2d54cb/evidence.json` |
| acceptance evidence SHA-256 | `ab311665ff0cb834efdd648cd634f943a4cbc5b8b00728ac8597a288a877ddec` |
| 精确 SHA CI | [GitHub Actions run 33099260233](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33099260233)：4/4 必需 job 成功 |

两份 evidence 都记录 `offline_only=true`、Mock-only、clean commit，秘密自检与 cleanup 为 `passed`；最终项目容器、volume、network 均为空。CI 的 backend、backend-integration、full-stack reliability 和 frontend 四个 job 均成功。artifact 位于 Git 忽略目录，路径用于本机复核，SHA-256 用于确认内容身份。

### 4.2 容量、背压与公平性

| 场景 | Worker | Run / 题 | 墙钟秒 | Run/s | 题/s | admission |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 单 Worker 参考 | 1 | 4 / 60 | 8.211325 | 0.487132 | 7.306981 | 4×`202` |
| 双 Worker 基线 | 2 | 4 / 60 | 4.478702 | 0.893116 | 13.396740 | 4×`202` |
| 停 Worker 后 bounded burst 并排空 | 2 | 4 / 60 | 6.988683 | 0.572354 | 8.585309 | 4×`202` + 2×`429` |

policy API apply/read-back 的 20 个字段全部有限；默认 `backlog_limit=4`、`question_quantum=5`。三个测量场景的每个 Run 都有 3 次 dispatch 和 2 次 cooperative yield。burst 的两个拒绝均为稳定 `run_backlog_full` 且报告 `limit=4`，四个已接纳 Run 随后全部排空。

跨 Model 场景以一个 Worker、三个高流量 Run 和最后一个低流量 Run 填满 4 个 backlog slot。typed audit 顺序记录低流量 Run 的 claim 和 slice；该时点三个高流量 Run 都尚未终态，因此 `low_claim_before_high_backlog_drained=true`。这只证明本地数据库调度在该 Mock 场景中的顺序，不是 Provider 侧公平性或容量承诺。

### 4.3 故障、对账与 acceptance

- capacity 的 lease-owner `SIGKILL`/自然过期接管、Redis stop/start 加数据库 reconciliation、终态重复投递 no-op 均通过。
- 最终对账为 18 个 completed Run、270 条唯一 Response、271 条 ledger（270 `settled_actual`、1 `settled_conservative`）和 1,229 条 typed audit event；active Run/reservation、scope/minute reserved、overdrawn、重复 operation/audit key、题错误、Redis PEL/lag 均为 0。
- acceptance 的 9/9 场景全部通过，其中确定性数据库 seam 覆盖 reservation→send-start、send-started→settlement、Provider response→本地 Response commit；另覆盖拓扑/健康、protocol-v1、API restart、实际 lease owner `SIGKILL`、Redis stop/start、pending/running cancel、duplicate delivery 和 populated 0004 downgrade refusal/空库往返。
- 两个 harness 的 cleanup 均确认容器、volume 和 network 零残留；这些结果仍不证明 Provider exactly-once、生产 HA、恢复时间目标或真实费用准确性。

## 5. 如何使用基线做容量决策

1. 在目标 commit、目标硬件和目标容器配额上重跑；保留 evidence SHA，不覆盖旧结果。
2. 先使用 Mock 分离数据库、调度和 Worker 开销，再在受信任的非自动化环境单独测量真实 Provider；两类结果不得混写。
3. 逐步增加 `runs-per-phase`、Worker 数和 Run concurrency；每次核对数据库连接、deadlock/conflict、queue lag、active reservation、错误和保守结算。
4. 任一错误、重复 key、reserved 漂移、overdrawn scope 或 cleanup 失败都使该次基线无效，不能通过只看吞吐忽略。
5. 只有连续多次、样本充分、监控完整且故障演练仍通过后，才能制定环境自己的告警阈值；本页不定义生产阈值。

日常指标、告警和故障处置步骤见 [OPERATIONS.md](OPERATIONS.md)。
