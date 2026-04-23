# Phase 3 · Stage B · SemiRDMA Effective-Loss 设计 Bug 分析 + 修复

> **时间：** 2026-04-23
> **触发：** B.5 RC-Lossy 1% seed=42 final loss = 1.04，**A2 SemiRDMA 1% seed=42 final loss = 1.39**（+34%）。头对头对比意外发现 SemiRDMA 在同名义 loss 下显著更差。

---

## 0. 一句话结论

SemiRDMA 的 DDP hook 里 `await_gradient` 用 `cfg.ratio = 0.95` **硬编码**作为 receive 目标，导致 **effective loss = max(cfg.loss_rate, 1 − ratio) = 始终 ≥ 5%**，不论 `cfg.loss_rate` 设成多少。这让 A2 矩阵里 4 档 loss 本质上测的都是 "5% effective loss"，跟 RC-Lossy 在同 label 下不是一个量级的实验。修复：[transport.py:258-267](../../python/semirdma/transport.py#L258-L267) 改为动态 `r = max(cfg.ratio, 1 − loss_rate − 0.005)`。

---

## 1. 现象

### 1.1 数据

A2 SemiRDMA 12 cells（`docs/phase3/results-cx6lx25g-c240g5_archive/rq6-prep-a2-real-nic/`）:

| cfg.loss_rate | seed=42 final | seed=123 final | seed=7 final | mean |
|---|---:|---:|---:|---:|
| 0.00 | 1.320 | 1.675 | 1.573 | 1.52 |
| 0.01 | 1.393 | 1.451 | 1.556 | 1.47 |
| 0.03 | 1.273 | ~1.30 | ~1.40 | ~1.32 |
| 0.05 | ? | ? | ? | ? |

**观察：** cfg.loss_rate 从 0 到 3% 变化，final loss 基本在 1.27-1.68 区间，**看不到明显的 loss_rate 影响**。

B.5 RC-Baseline / RC-Lossy（`docs/phase3/results-cx6lx25g-c240g5_archive/rq6-b5-rc-baselines/`）:

| Baseline | seed=42 final | seed=123 final | seed=7 final | mean |
|---|---:|---:|---:|---:|
| RC-Baseline (0% loss) | 1.010 | 1.065 | 1.108 | **1.06** |
| RC-Lossy 1% | 1.041 | — | — | — |

### 1.2 头对头（seed=42）

| step | RC-Baseline | RC-Lossy 1% | **SemiRDMA 1%** | **SemiRDMA 0%** |
|---:|---:|---:|---:|---:|
| 0 | 2.4233 | 2.4233 | 2.4233 | 2.4233 |
| 100 | 1.7364 | 1.7551 | 1.9262 | 1.991 |
| 250 | 1.3654 | 1.3958 | 1.7273 | 1.759 |
| **499** | **1.010** | **1.041** | **1.393** | **1.320** |

SemiRDMA 0% 和 SemiRDMA 1% 的最终 loss 几乎一样（1.32 vs 1.39）— 确认"cfg.loss_rate 不怎么影响 SemiRDMA 的训练结果"。

---

## 2. 根因分析

### 2.1 代码路径

[python/semirdma/hooks.py:198-218](../../python/semirdma/hooks.py#L198-L218)：
```python
cs_send = state.tx.post_gradient(byte_view, ...)  # 按 cfg.loss_rate Bernoulli drop
cs_recv = ChunkSet(base, nbytes, state.cfg.chunk_bytes)
stats = state.rx.await_gradient(cs_recv)          # <-- 这里用 cfg.ratio
...
flat.add_(remote_t)
flat.div_(state.world_size)
```

[python/semirdma/transport.py:257 (pre-fix)](../../python/semirdma/transport.py#L257)：
```python
r = self._cfg.ratio if ratio is None else ratio   # cfg.ratio = 0.95 from yaml
stats = self._ratio.wait_for_ratio(cs, r, t)      # 等 95% chunk 到齐就返回
buf = np.frombuffer(self._engine.local_buf_view(), ...)
apply_ghost_mask(buf, cs)                         # 剩下 5% 被 zero 掉
```

### 2.2 数学

设 N = 总 chunk 数，p = cfg.loss_rate，R = cfg.ratio。

| Chunk 来源 | 数量 | receiver 看到 |
|---|---|---|
| sender 主动 drop | p · N | 永远不到（wait_for_ratio 不等这些） |
| sender 发出 → 及时到达 receiver | ≥ R · N | ✓ 正常使用 |
| sender 发出 → 但 wait_for_ratio 在 R · N 命中后返回 | (1 − p) · N − R · N | ✗ 被 apply_ghost_mask zero 掉 |

**Effective receive loss** = (p · N + `max(0, (1-p)·N - R·N)`) / N
                       = p + `max(0, 1 - p - R)`
                       = `max(p, 1 - R)`

代入 R = 0.95：

| cfg.loss_rate (p) | 1 − R = 0.05 | **effective loss** |
|---|---:|---:|
| 0.00 | 0.05 | **0.05** |
| 0.01 | 0.05 | **0.05** |
| 0.03 | 0.05 | **0.05** |
| 0.05 | 0.05 | 0.05 |
| 0.10 | 0.05 | 0.10 |

**4 档 A2 cell 实际都测的是 ~5% effective loss**。名义 label 对受端没意义。

### 2.3 为什么 A1 bit-for-bit 没触发这个 bug

A1 用 `transport_cfg.ratio=1.0` 显式覆盖 + `timeout_ms=60000`（见 [run_a1_real_nic.sh](../../scripts/cloudlab/run_a1_real_nic.sh)）→ effective loss = max(0, 1-1.0) = 0% → 跟 gloo bit-for-bit。

A2 改用 [stage_b_cloudlab.yaml](../../experiments/configs/stage_b_cloudlab.yaml) 默认的 `ratio=0.95 timeout_ms=5`（Phase 2 RQ4 的 sweet spot），从此 effective loss 永远 ≥ 5%。

### 2.4 为什么 Phase 2 RQ4 没暴露这个

RQ4 是 C++ `test_ratio_sweep.cpp` 测试，**sender 和 receiver 在同一进程**，receiver 知道 sender 发了多少，target 动态算：
```cpp
double target = static_cast<double>(sent_count) / static_cast<double>(num_chunks);
rc.wait_for_ratio(cs, target, WAIT_TIMEOUT);
```

在 RQ4 里 ratio=0.95 的含义是"接受 95% 的 sent chunks" — sender 1% drop 时这相当于等 0.95 × 0.99 ≈ 94% 的 total chunks。

**但 DDP hook 里是跨进程 AllReduce**，receiver 不知道 sender 实际发了多少，默认就用 cfg.ratio 当作 "fraction of total chunks"。两处用的是同一个 `cfg.ratio` 字段但语义不同 — 这是典型的**接口 hidden assumption mismatch**。

---

## 3. 修复

### 3.1 代码改动（已提交）

[python/semirdma/transport.py](../../python/semirdma/transport.py#L258-L267)：
```python
# Pre-fix:
r = self._cfg.ratio if ratio is None else ratio

# Post-fix:
if ratio is None:
    dyn_target = 1.0 - self._cfg.loss_rate - 0.005   # 0.5% jitter slack
    r = max(self._cfg.ratio, dyn_target)
else:
    r = ratio
```

**语义变化：**
- `cfg.ratio = 0.95` 从"固定 receive cutoff"变成"最低安全 floor"
- 动态 target = 1 − loss_rate − 0.5%(slack) 作为主要目标
- `max(...)` 保证就算 loss_rate 很小，也不会等到死（保留 cfg.ratio 作为上限浪费时间的下限）

### 3.2 Predicted 行为（各档 loss）

| cfg.loss_rate | dyn_target | cfg.ratio | 最终 r = max | Effective loss |
|---|---:|---:|---:|---:|
| 0.00 | 0.995 | 0.95 | **0.995** | 0.5% (slack) |
| 0.01 | 0.985 | 0.95 | **0.985** | 1.5% |
| 0.03 | 0.965 | 0.95 | **0.965** | 3.5% |
| 0.05 | 0.945 | 0.95 | **0.95** | 5% (floor kick in) |
| 0.10 | 0.895 | 0.95 | **0.95** | 10% (sender drop dominates) |

目标：各档 effective loss 跟名义 loss_rate 差不多，训练效果也该更接近 RC-Lossy 同档。

### 3.3 Fix 不影响的代码路径

- ✅ **Phase 2 C++ 测试**：chunk_sweep / ratio_sweep 不走 Python transport，用自己的动态 target 逻辑 → 无变化
- ✅ **Phase 2 RQ2/RQ4 已发表结论**：基于 C++ 路径，无影响
- ✅ **Stage B M1-M5**：微基准不涉及 await_gradient loss 路径 → 无变化
- ✅ **A1 bit-for-bit**：显式传 `ratio=1.0` → `ratio is None` 条件不触发，走 else 分支 → 无变化
- ⚠️ **aliyun Stage A A2 (rq5-results)**：之前也有这个 bug（ratio=0.95），数据本身仍然有效但需补注释说明"effective loss ≠ cfg.loss_rate"

---

## 4. 需要重跑的实验

按 ROI 排序：

### 🔴 P0（必须重跑，论文主数据）

1. **A2 SemiRDMA 12 cells**（loss ∈ {0, 0.01, 0.03, 0.05} × 3 seed × 500 step）
   - 现有数据：all effective 5% loss 的 "伪" A2
   - 重跑后：各档 effective loss 接近名义值，可以跟 B.5 RC-Lossy 同档直接对比
   - **估时**：~90 min（12 cell × 7.5 min）

### 🟡 P1（应该重跑，验证 fix 有效）

2. **A2-fix sanity (3 cells, 100 steps)** 先跑，确认 fix 如预期：
   - SemiRDMA L=0 + fix → 应该 match RC-Baseline final loss ~1.01
   - SemiRDMA L=0.01 + fix → 应该接近 RC-Lossy 1% final loss ~1.04
   - SemiRDMA L=0.05 + fix → 应该明显差（因为真 5% loss）
   - **估时**：~8 min (3 cell × 2.5 min for 100 step)
   - 如果 sanity 不符合预期，可能需要调 slack 或看 H3（rank-asymmetric ghost）

### 🟢 P2（不需重跑，数据仍有效）

3. **B.5 RC-Baseline / RC-Lossy 4 cells**：dist.all_reduce + 软件 mask，不走 SemiRDMA 路径 → **数据保留**
4. **B.5 剩余 8 cells (rc_lossy 1/3/5% × 2 seeds + rc_lossy 5% × 3 seeds)**：同样不受影响 → 正常续跑
5. **RQ6-prep A1 bit-for-bit 6 cells**：explicit ratio=1.0 → 无影响
6. **Phase 2 真机重扫 (RQ1/RQ2/RQ4)**：C++ 路径 → 无影响
7. **Stage B M1-M5 microbench**：无影响

### 总 P0+P1 重跑时间预算

~100 min cluster time（全新 session 可以塞进去）。

---

## 5. 次步骤：H3 rank-asymmetric ghost 是否也要处理？

修 H2 后，SemiRDMA 还有一个二阶问题：**两 rank 看到的 "peer's bytes" 不同**（rank 0 看 rank 1 发的，rank 1 看 rank 0 发的），ghost 模式也不同，averaged gradient 两端字面不等 → 模型参数 drift。

影响：
- 每步 drift 期望值 0（drops uncorrelated），方差 O(loss × bucket_size / N)
- 500 step 后 drift 可能累积到 non-trivial 值
- RC-Lossy 没有这个问题（真 AllReduce + post mask）

怎么验证：修 H2 后再对比 SemiRDMA vs RC-Lossy。如果 SemiRDMA 仍然显著更差，是 H3 主导；如果追平 RC-Lossy，说明 H3 影响可忽略（drift 自我抵消）。

**策略**：先修 H2，等 P1 sanity 结果，再决定是否需要 H3 修复。

---

## 6. 重跑流程（next session）

```bash
# 1. 拉最新代码到两节点（含 transport.py fix）
ssh chen123@c240g5-110231.wisc.cloudlab.us
cd ~/SemiRDMA
git pull        # 或 scp transport.py（local 是 authoritative）
# 注意：pip install -e . 是 editable，.py 改动自动生效，无需重装

# 2. link / GPU sanity
bash ~/SemiRDMA/scripts/cloudlab/link_setup.sh
nvidia-smi --query-gpu=name,memory.used --format=csv,noheader

# 3. P1 sanity (100 step × 3 cell × 2 seed)，确认 fix 按预期
# TODO: 需要额外写 run_a2_fix_sanity.sh 或者手动起 3 torchrun

# 4. P0 full rerun: 先把旧 A2 run dir 归档 or 删除（让 cell-skip 不跳），再跑
ssh chen123@c240g5-110231.wisc.cloudlab.us
mv ~/SemiRDMA/experiments/results/stage_b/2026-04-23 \
   ~/SemiRDMA/experiments/results/stage_b/2026-04-23_pre-ratio-fix
nohup bash -c 'NODE_PEER_HOST=chen123@10.10.1.2 bash ~/SemiRDMA/scripts/cloudlab/run_a2_real_nic.sh' \
    > /tmp/a2_v2_matrix.log 2>&1 &

# 5. 同时 / 之后：resume B.5（cell-skip 会认 4 个已完成，从 cell 4 续）
# 注意：先在 node0 上把 B.5 已完成的 4 cell 从旧目录挪回来（或保留原样，cell-skip 跨 date 能识别）
```

---

## 7. 论文叙事影响

### 修复前（错误数据）
- "SemiRDMA 在 1-5% 真硬件丢包下仍收敛" — 实际测的是 5% effective loss 下收敛，跟 cfg.loss_rate 无关
- "SemiRDMA vs RC-Lossy TTA 对比" — 不可直接比，loss 档不等价

### 修复后（正确预期）
- "SemiRDMA 在 cfg.loss_rate ∈ {0, 1, 3, 5}% 下 effective loss 对应 {0.5, 1.5, 3.5, 5}%，训练 loss 曲线与 RC-Lossy 同档接近"
- 可以直接 head-to-head 对比各档 final loss / TTA / p99 step latency
- 如果 SemiRDMA p99 < RC-Lossy p99（predicted）→ SemiRDMA 核心卖点成立

---

## 8. Commit 记录（计划）

- (本 commit) `fix(transport): dynamic receive target = max(cfg.ratio, 1 - loss_rate - slack)` 
- (next session) `bench(stage-b): A2 v2 re-run on fixed ratio` (12 cells post-fix)
- (next session) `docs(phase3): rq6-b5 complete analysis (A2 v2 + B.5 12 cells)`
