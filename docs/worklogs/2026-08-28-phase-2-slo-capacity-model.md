# 2026-08-28 — Phase 2 正式 SLO 与容量模型工作日志

## 元信息

- 日期：2026-08-28
- 执行者：Codex
- 分支：`codex/complete-evaluation-workflow`
- 初始 HEAD：`bed246710054e43aa8604b0fb35564b9ca8f7515`
- 关联阶段：[Phase 2 — 可靠性与任务执行](../phases/PHASE-2-RELIABILITY.md)
- 关联任务：[NEXT_TASK — P2-01](../NEXT_TASK.md)
- 执行计划：[Phase 2 正式 SLO 与容量模型执行计划](../plans/2026-08-28-phase-2-slo-capacity-model.md)
- 架构决定：[ADR-0012](../decisions/ADR-0012-single-host-slo-capacity-qualification.md)、[ADR-0013](../decisions/ADR-0013-stable-image-content-fingerprint.md)、[ADR-0014](../decisions/ADR-0014-dual-backlog-slo-profile.md)
- 当前状态：`in_progress`

## 目标

为现有 PostgreSQL 16、Redis 7、API 与有限 Worker 的受支持单机/单区域 Compose 拓扑建立一个小而完整的 P2-01 交付：固定可审计的 Mock-only 资格配置、正式的排队/恢复/吞吐/错误/backlog 目标、多轮统计方法、参数校准规则和可重复 evidence。该切片完成后，操作者可以对一个精确、干净 commit 运行同一资格测试并得到逐轮原始证据、聚合变异与逐项 pass/fail，而不把结果外推为真实 Provider、生产 HA 或通用 SLA。

## 背景

治理/审计候选实现 SHA `665244e095905083b606b8e98e946ed1a02dc0fc` 已有单轮 enhanced capacity、9/9 acceptance、真实 PostgreSQL/Redis integration 与远程 4/4 CI。现有 capacity evidence 明确只是一次观测，不定义 SLO，也没有跨轮变异。`docs/NEXT_TASK.md` 把 P2-01 列为正式 Phase 2 closure 的首项，要求定义受支持拓扑、容量假设、多轮统计和 lease/heartbeat/scan/backoff/backlog/Worker 边界。

## 范围

- 为单机/单区域 Compose、PostgreSQL 16、Redis 7、1 API、2 Worker 和固定 Demo/Mock 负载定义资格配置与容量模型。
- 新增依赖无关的多轮 harness，串行运行隔离 capacity trials，验证 commit/config/environment 可比性，聚合吞吐与延迟变异，并逐项评估 SLO。
- 补足单轮 capacity evidence 中用于恢复 SLO 的确定性耗时事实；保留每轮独立 cleanup 与脱敏证据。
- 增加 Make target、纯离线单元测试、自检、文档和精确候选 evidence。
- 根据多轮实测记录 lease/heartbeat/scan、backlog、quantum 与 Worker 扩缩的支持边界；不静默修改生产默认值。

## 非目标

- 不调用真实或付费 Provider，不测 Provider 吞吐、费用、限流或 exactly-once。
- 不承诺生产 HA、多区域、Kubernetes、无限扩缩或面向任意硬件的 SLA。
- 不在本切片实现 P2-06 Exporter/告警/retention/Worker progress，也不实现 P2-07 backup/restore。
- 不改变 `llmbenchlab-protocol-v1`、评分、治理 policy、数据库 schema、公共 API 或凭据边界。
- 不把只有五轮的小样本区间描述成普适性能分布；p99 仅作描述。

## 验收标准

- 一个 accepted ADR 明确资格拓扑、硬正确性目标、性能阈值、统计方法、无效样本和外推边界。
- 多轮 harness 固定先运行 1 轮不计样本的 warm-up，再运行恰好 5 轮 measured trial；它拒绝脏工作树、混合 commit/config/environment、失败或 cleanup 不完整的 trial，失败时保留脱敏聚合证据并返回非零。
- evidence 包含精确 commit、脚本 SHA-256、每轮 evidence 路径与 SHA-256、固定配置/环境指纹、逐项样本、均值/中位数/最小/最大/样本标准差/CV/95% 区间和 SLO 判定。
- 资格测试覆盖 queue/recovery/throughput/error/backlog/fairness/reconciliation/cleanup；任何正确性不变量失败都使总体失败，不能由性能均值掩盖。
- 单元测试、Ruff、完整相关测试、Mock smoke、Compose config 与真实多轮资格运行通过；自动化不访问真实 Provider。
- 形成独立 commit，普通 push 到当前 PR 分支，并等待该精确 SHA 的必需 GitHub Actions 全绿；Phase 2 仍保持 `in_progress`。

## 假设

- Docker Desktop/Compose、PostgreSQL 16 与 Redis 7 在本机可用；若不可用，只能完成离线实现并如实保留真实多轮未运行。
- 多轮测试串行执行，每轮使用唯一 Compose project 并完整 cleanup；并行运行会造成宿主资源争用，不能作为同一资格样本。
- 正式资格使用部署计时 `lease=30s`、`heartbeat=10s`、`poll=1s`；既有 capacity 默认的 `6/2/0.15s` 只用于加速功能/故障回归。
- Mock generation delay 只模拟固定 Provider 等待，不模拟真实 Provider 网络、排队、token streaming、429 或账单。
- 性能阈值只适用于 ADR 固定的最小主机和资格负载；环境低于边界时可以运行诊断但不能获得资格通过。

## 风险

| 风险 | 影响 | 控制 |
| --- | --- | --- |
| 宿主噪声造成性能波动 | 单轮误判或 CI 偶发失败 | 1 warm-up + 5 measured、串行、记录环境；正确性为硬门禁，性能用保守阈值与跨轮统计 |
| wrapper 接受不可比 trial | 聚合结果失真 | 对 commit、dirty、配置、版本、CPU/内存和脚本 hash 建立严格指纹 |
| evidence 泄漏运行时秘密 | 违反安全底线 | 复用 allowlist/sanitize，保存摘要而非 stdout；增加 canary 与凭据模式扫描测试 |
| 长时间 Compose 运行失败后残留 | 干扰后续测试 | 每轮沿用 capacity finally cleanup，wrapper 校验 cleanup 并记录失败 trial |
| 正式 SLO 被误读为生产 SLA | 错误外推 | 名称与文档限定为 supported single-host qualification；明确 Mock/非 HA/非 Provider 边界 |

## 实施步骤

1. 完成只读设计评审，建立执行计划与 ADR，冻结资格合同。
2. 实现单轮恢复耗时事实与多轮聚合/判定 harness。
3. 增加 Make target、离线测试和安全/失败路径覆盖。
4. 运行定向与全量本地门禁，修复发现的问题。
5. 提交实现、push，并在该精确干净 SHA 上运行 1 轮 warm-up + 5 轮 measured 的真实资格测试。
6. 用真实 evidence 同步 Performance/Operations/Testing/状态文档，再提交、push 并等待精确 SHA CI。

## 初始仓库状态

- `git status --short --branch`：工作树干净；当前分支跟踪同名 origin 分支。
- 初始 HEAD 与 origin：`bed246710054e43aa8604b0fb35564b9ca8f7515`。
- 必须保留：既有 capacity/acceptance evidence 的历史事实、P2-06/P2-07 未完成状态、当前 PR #2 及既有 CI 记录。

## 实际修改

- 新增 accepted ADR-0012，冻结 `P2-local-control-plane-v1`、预登记阈值、1+5 实验纪律、配对 Worker scaling、扫描/backoff/连接与安全容量公式。
- 新增 `scripts/phase2_slo.py` 与 `make phase2-slo`：固定 seed/profile、独立进程组、420 秒 scoped cleanup 窗口、最小 child 环境、clean/exact Git 根校验、严格 JSON/path/fingerprint、1 warm-up + 恰好 5 measured、Student-t/CV/零事件与逐项 SLO 判定。
- 扩充 `phase2_capacity.py`：显式 timing/backoff/pool 参数与容器内 Settings 回读、稳定 image/resource 指纹、匿名延迟样本、吞吐 wall time、每 cell ledger delta、恢复 UTC facts、pause→fence→SIGKILL、重复 delivery 前后 hash 与完整 ledger projection 对账。
- Compose 显式映射 pool、Worker retry/backoff、Redis block 等资格参数；`db_run_snapshot` 增加 active/send-started attempt 计数以确定性命中故障 seam。
- 单元测试覆盖统计、CLI、strict JSON/path、吞吐/恢复重算、资源/配置漂移、账本投影、child/validator 失败留证、核心 run-suite 编排、Git override、进程组信号、环境 allowlist 和秘密 canary。
- 提交前对抗性终审额外修正真实 producer/consumer 合同：capacity evidence 现在保留 Demo `slug/schema_version`；非默认合法 workload 的最终 Run/Response/reservation/audit 计数由 `runs_per_phase/backlog_limit` 推导，正式 wrapper 仍锁定 18/270/271。
- 容量公式不再误用 Mock 固定为 1ms 的兼容 `Response.question_latency_ms`，而是显式使用 `0.08s × quantum 5 = 0.4s` 的 Mock slice 服务预算；所有扫描/接管公式统一并记录 `delta_db=1s`。lease fault 在 pause 后任一路径异常都会 best-effort unpause，避免普通失败留下冻结 Worker。
- 第一次 clean-SHA warm-up 暴露 producer 先用未舍入 wall time 算吞吐、再分别把两者舍入到 6 位，validator 无法从序列化事实精确复算。producer 现先冻结 6 位 wall time，再由它计算 6 位吞吐；validator 要求与 `round(completed_questions / wall_duration, 6)` 精确一致，并增加真实边界回归。
- 第二次 clean-SHA suite 暴露 Docker Compose v5 会把随机 project/service labels 写入 image config，导致相同 RootFS 的完整 image ID 跨 trial 变化。新增 accepted ADR-0013：child 保留 raw ID，aggregate 改锁定 RootFS layers 与只过滤这两个动态 labels 后 Config 的 content SHA；Compose version、代码、依赖、执行配置或其他 label 漂移仍会 fail closed。
- 第三次资格在精确 clean SHA `dfa67abb1a9a0418a7e3337c179f816e3c69f121` 完整执行 1 个 warm-up 和 5 个 measured。六个 child 的 hard correctness/cleanup 均通过，但 measured-02 的 cold burst queue/execution/E2E p95 为 `3.277036/5.936064/8.997677s`，超过 v1 `3/5/8s`；aggregate 仅 15/18，因此 v1 永久保持 `unqualified`，不删除该轮也不重复运行碰绿。
- 新增 accepted ADR-0014：v2 把 warmed pause 与 cold stop/start backlog 拆成两个正式 AND measurement；warmed 保持 `3/5/8s`，cold 使用在新样本前冻结、由既有模型导出的 `6/8/10s`，两者都要求恰好两个 distinct validated Worker claim。formal child 固定变为 22 Run/330 Response/331 reservation；capacity cleanup 还将安全删除本项目唯一、无 alias/引用的 backend build tag，任何残留 fail closed。
- 实现 `P2-local-control-plane-v2` producer/consumer：默认 `capacity-v1` 仍为原三个 measurement，formal profile 固定四个 cell、完整 18 项运行参数、1 warm-up + 恰好 5 measured、22/330/331 对账和每轮唯一项目镜像清理。Run admission、measurement、cooperative scheduling、Worker claim、分段 timing 和 fairness 的 Run/Model/Event 身份都做交叉校验；fairness 每 Run 固定 `3 claim / 2 yield / 1 terminal`，使用数据库 UTC 严格 `<` 重算先后关系，aggregate 只保留匿名结论。
- 镜像 cleanup 只在通用 `down -v` 和本项目容器/volume/network 清空后执行；候选必须绑定 down 前实际 backend image ID、exact project/service label、唯一内部 tag、零容器引用，且只执行不带 force 的 exact-tag 删除。reference/rm 超时及所有失败路径使用固定错误，不把 raw image ID、tag、inspect Config 或 stderr 写入 failure。

## 实际验证

- `cd backend && uv run pytest -q tests/test_phase2_capacity_script.py tests/test_phase2_slo_script.py`：237 passed。
- `cd backend && uv run ruff check ...` 与 `ruff format --check ...`：通过。
- `python3 -I scripts/phase2_capacity.py --self-check-only`、`python3 -I scripts/phase2_slo.py --self-check-only`：通过。
- `python3 -m py_compile scripts/phase2_capacity.py scripts/phase2_slo.py scripts/phase2_acceptance.py`：通过。
- `make lint`：通过；Ruff、Ruff format check、ESLint 与 TypeScript typecheck 均为零失败。
- `make test`：通过；后端 829 passed、29 skipped，前端 38/38 passed。
- `make smoke`：通过；1 passed、7 deselected，完全使用 Mock。
- `cd frontend && npm run build`：通过；保留既有单个大于 500 kB 的 chunk warning。
- `docker compose config --quiet`：通过。
- 在独立临时 SQLite 上执行 `alembic upgrade head && alembic check`：通过；没有修改本地默认数据库。
- 当前候选执行 `make phase2-capacity`：通过；脏树回归 evidence 为 `.pytest_cache/artifacts/phase2-capacity/llmbenchlab-p2-fc7ba5acea65/evidence.json`，SHA-256 `611f70cdec36c9bf5a4aa744e0689ac1e934382a42b161f77e734937a828d4bf`，cleanup 无残留。它记录 `dirty=true`，只证明旧单轮合同兼容，不是正式资格证据。
- 当前候选执行 `make phase2-acceptance`：9/9 通过；脏树回归 evidence 为 `.pytest_cache/artifacts/phase2-acceptance/llmbenchlab-p2-1255ad054266/evidence.json`，SHA-256 `f81a0f84f8279c3e876f7955250861219ab9524cd2feea72656b351ef7283c74`，cleanup 无残留。它同样不替代 clean-SHA 资格 evidence。
- 两次 Compose 回归后按 project label 复核容器、网络与 volume：均无残留；evidence 凭据模式扫描无命中。
- 实现 commit `d5a1bd3aa556e84ed88bb17c028b847c8d51c129` 的第一次正式 suite 在 warm-up 后按合同失败，未产生 measured sample：aggregate `.pytest_cache/artifacts/phase2-slo/llmbenchlab-p2-slo-20260828T034014Z-2df252f4bbdb/evidence.json`，SHA-256 `b5d7314eb72ba6bcf6f41cfa042eec46f1123dfbeda76b6c53c7f99e9f4af7ab`；失败原因为序列化 wall time 与旧吞吐舍入顺序不一致。child 自身功能/cleanup 为 passed，project 容器、volume、network 现场复核均为空，artifact 凭据模式扫描无命中；该失败证据永久披露，不计入资格样本。
- 同一 `d5a1bd3` 的 GitHub Actions run `33139542534` 已完成且 4/4 job success；它证明远程正确性门禁，不会把上述本地正式资格失败变成通过。吞吐合同修复 commit `c909f241ada1fbcc19d6ef7795ad30104cd151e6` 的 run `33139960008` 也已对精确 SHA 4/4 success；镜像指纹修复随后形成 `dfa67ab` 并重新走完 CI。
- 修复 commit `c909f241ada1fbcc19d6ef7795ad30104cd151e6` 的第二次 suite 中，warm-up 全部通过，measured 1 child 也完成并 cleanup，但 validator 在把该轮纳入样本前因完整 image ID 漂移 fail closed：aggregate `.pytest_cache/artifacts/phase2-slo/llmbenchlab-p2-slo-20260828T034832Z-2e045233c5f0/evidence.json`，SHA-256 `984d7dd7badcb247212faae470f00230d848f12dd403054f02e9e5f348eace92`；warm-up/候选 measured child SHA 分别为 `f8de666d70c5f3c0a118343af9df17a5871dcd2c5105200c47ded6b8bf30f798` 与 `0f429165e96cd81f3234a1f35174b9618f9296aa93a85af6eb38b9c16afafe2e`。两 project 的容器、volume、network 现场复核均为空，artifact 凭据模式扫描无命中；没有 measured sample 进入统计。
- 两轮 backend raw image ID 不同，但 RootFS layer 列表相同；用当前 producer 的完整 canonical 输入（Architecture、OS、Variant、RootFS layers，以及只过滤 project/service labels、保留 Compose version 的完整 Config）复算后，两张镜像的 content SHA 均为 `4fc3a760c4cdb5c392cea5203f53cbab167c1e1e134522180758b7fa4629c9bb`。这项实际证据驱动 ADR-0013，而不是放宽真实镜像内容漂移门禁。
- 镜像指纹修复 commit `dfa67abb1a9a0418a7e3337c179f816e3c69f121` 的 GitHub Actions run `33141140969` 已对精确 SHA 4/4 success。
- 同一 `dfa67ab` 的完整 v1 aggregate 为 `.pytest_cache/artifacts/phase2-slo/llmbenchlab-p2-slo-20260828T041254Z-5fde74882caf/evidence.json`，SHA-256 `f993c11ff1a9f55921b5d7ea14974b0e3ca280f75427095c771ef3f5964ae3b2`，状态 `failed/not_qualified`；measured-02 child SHA-256 `437492fe7e0d54d797f599410d475d0ef994ad28cb3f9a4f36010168da8e782f`。所有 child 为 clean/Mock-only/18 Run/270 Response/271 reservation/零题错误，容器、volume、network 清理为空，凭据扫描无命中；六个 project backend build image 仍留作 Docker cache，因此不得写成所有 Docker 资源零残留。
- v2 脏树预演第一次在 warmed pause read-back 处按合同失败：Docker Engine 对 paused 容器返回 `State.Status=paused`，旧测试夹具错误要求 `running`。失败 evidence 为 `.pytest_cache/artifacts/phase2-capacity-v2-precommit/llmbenchlab-p2-5a676f38712b/evidence.json`，SHA-256 `534e45d1434a084a91db3b4740b6643f5dc071c15b747c0d7f59ee71448a029f`；失败轮的容器、volume、network 与唯一项目镜像仍全部清理成功。实现已改为严格匹配 `paused/true` 与 `running/false`，并增加两态回归。
- 从零重跑的第二次 v2 脏树预演通过：`.pytest_cache/artifacts/phase2-capacity-v2-precommit-r2/llmbenchlab-p2-387adb957c6c/evidence.json`，SHA-256 `0e504c2531b857f0b97d5f3a6374bf19b251db206dce100318247543a296e453`。它实际完成四个 measurement、22 Run/330 Response/330 QuestionExecution/331 reservation，fairness 有 24 个 typed 事件，cleanup 为 image `1/1/0/0` 且本项目容器/volume/network/镜像均无残留。该 evidence 明确 `dirty=true`，只用于真实 producer/consumer 形状预演，不是正式资格样本。
- 用当前 consumer 重放第二次 raw evidence 时又发现真实 duration JSON 是 `180.0/30.0/10.0/1.0`，而人工 fixture 曾使用整数。双方合同已统一为有限 float 并锁定类型；仅在内存把预演的 dirty/status 标志改为 clean 后，真实 evidence 的完整 consumer validation 与所有 hard invariant 均通过，artifact 本身未修改。
- 最新树再次执行默认 `make phase2-capacity` 通过，保持 v1 三个 measurement/18 Run 合同：`.pytest_cache/artifacts/phase2-capacity/llmbenchlab-p2-b2795bc22e2b/evidence.json`，SHA-256 `323c81dd86a408039a17034ab4166cd9ff2db83ba2e93efbc7ee00dc51454c7b`；本项目容器/volume/network/image 现场均为 0。该轮仍为 `dirty=true` 兼容性回归，不是资格样本。
- 最新树再次执行 `make phase2-acceptance` 为 9/9 通过：`.pytest_cache/artifacts/phase2-acceptance/llmbenchlab-p2-81de75fbbcf8/evidence.json`，SHA-256 `86f5126addfa553af016353ced33495e2502384f55afd743ba6fa1b296ff7bd8`；本项目容器/volume/network 现场为 0。通用 acceptance cleanup 按既有边界保留 2 个本地 build-cache image；ADR-0014 的 exact image 删除只属于 capacity trial，不据此把 acceptance 扩大为镜像删除工具。
- `git diff --check`：通过。
- 过程中的两条非产品失败已如实保留：曾在 `frontend/` 误执行根目录 `make smoke`；默认本地 SQLite 的 Alembic marker/schema 与 head 漂移，因此未修改用户 DB，改用临时空 SQLite 验证通过。
- 提交前曾把整个历史 `phase2_acceptance.py` 额外纳入 backend 的现代化 Ruff 规则，得到 98 个既存风格告警；该脚本不在项目 `make lint` 的 Ruff 文件集内，本切片也不机械重写其无关代码。其窄范围 SQL 增量已由 `py_compile`、脚本单测和真实 9/9 acceptance 验证；新增/主要改动的 capacity/SLO 脚本及测试单独 Ruff/format check 通过。

## 已知问题与下一步

- 按 ADR-0014 实现 v2 双 backlog、distinct Worker claim 与 capacity-only 镜像 cleanup；形成新 commit/push/精确 SHA CI 后，必须从新的 warm-up 开始完整执行 v2 1+5，不能复用 v1 trial。当前不得声称 P2-01 完成。
- 本地 evidence 能证明单次 invocation 未丢轮，不能证明不存在已删除的更早 suite；正式记录必须披露本次所有资格尝试。
- P2-06/P2-07 等正式 closure 仍未完成，Phase 2 保持 `in_progress`。
