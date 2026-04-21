# Phase 3 · Stage B · CloudLab 硬件 & 单节点验证笔记

> **时间：** 2026-04-21
> **机器：** CloudLab Wisconsin `d7525-10s10329.wisc.cloudlab.us`（单节点，5h 实验）
> **目的：** 在拿到第二个节点之前，对 Stage B 的真机栈做一次"尽可能深"的单节点 smoke-test，把硬件现状、构建路径、API 可用性钉死。本文件不作 performance claim，只作工程现状记录。

---

## 0. 摘要：和原计划的差异

| 维度 | 原计划 ([design-ddp-integration.md §2.2](./design-ddp-integration.md#22-stage-b--cloudlab-connectx-5-真机--五路-baseline-对比week-78may-25--jun-7)) | 实际（d7525） | 影响 |
|------|-------|-------|------|
| 硬件类型 | Utah `d7615` + ConnectX-5 | Wisconsin `d7525` + **ConnectX-6**（MT28908） | NIC 代差一代，WQE rate 更高 → Phase 2 RQ1 (SoftRoCE 16 KiB 饱和) 不能直接迁移 |
| 固件 | 16.x（CX-5） | **fw 20.38.1002** | 无已知 UC QP bug；预期 Write-with-Imm 正常工作 |
| 链路层 | RoCEv2 / 100 GbE | RoCEv2 / 100 GbE | ✓ 保持一致 |
| GID 索引 | 1（RoCEv2） | **1（RoCEv2）** | ✓ 保持一致 |
| 驱动 | `mlx5_core` | `mlx5_core` | ✓ 保持一致；Phase 2 verbs API 无改动 |
| 节点可用性 | 2 nodes | **1 node（暂）** | 2-node 网络路径测试推迟到用户补约第二节点 |

**结论一句话：** 硬件比计划新一代、驱动相同，**Phase 2 代码不需要改**即可构建并生成 QP；Stage B 的"真机重标定"环节（design §2.2 里的 Phase 2 参数在 CX-5 上重扫）应改成"在 CX-6 上扫"。

---

## 1. 硬件事实（只读盘点）

### 1.1 `ibv_devinfo -d mlx5_0` 关键字段

```
hca_id: mlx5_0
    transport:          InfiniBand (0)
    fw_ver:             20.38.1002
    node_guid:          <redacted>
    sys_image_guid:     <redacted>
    vendor_id:          0x02c9
    vendor_part_id:     4123          # MT28908 family = ConnectX-6
    hw_ver:             0x0
    phys_port_cnt:      1

    port: 1
        state:          PORT_DOWN (1)   # 单节点 → 预期
        max_mtu:        4096 (5)
        active_mtu:     1024 (3)
        sm_lid:         0
        port_lid:       0
        link_layer:     Ethernet
```

**要点：**
1. `link_layer: Ethernet` + `vendor_part_id: 4123` → CX-6 + RoCEv2 部署。
2. `state: PORT_DOWN` 是**单节点的正常状态**：没有 peer 就没有 LAA 上升；这**不**阻止 QP 进入 INIT 状态（INIT 的 `ibv_modify_qp` 只要 PD、port_num、pkey_index 合法就行）。
3. `active_mtu: 1024` 暂时无意义（port down），真正的 MTU 会在 peer 上线后协商，预期 4096。

### 1.2 GID 表

```
sudo ibv_devinfo -d mlx5_0 -v | grep -E "GID|RoCE"
    GID[0]:            fe80::...       # link-local IPv6, RoCEv1
    GID[1]:            0000:...ffff:... # mapped IPv4, RoCEv2
```

Stage B 所有代码用 **`gid_index=1`**（已写入 `experiments/configs/stage_b_cloudlab.yaml` 的 `transport_cfg.gid_index`）。

### 1.3 内核模块

`lsmod | grep -E 'mlx5|ib_'` 打到的关键 module：

```
mlx5_ib      ← RDMA API
mlx5_core    ← NIC driver
ib_core
ib_uverbs
rdma_ucm
rdma_cm
```

Phase 2 C++ 只依赖 `ib_core + ib_uverbs + mlx5_core`，三者齐备。

---

## 2. 单节点可做的验证（done）

### 2.1 Phase 2 C++ 回归（gtest）

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
cd build && ctest --output-on-failure
```

- `test_chunk_roundtrip / test_chunk_sweep / test_ratio_sweep / test_ratio_timeout / test_rms_error` → 全过。
- `test_ghost_mask` → **SKIP**（硬依赖 `rxe0`，CloudLab 节点没有 SoftRoCE，后续 Stage B 不需要在真机跑该 test；原始结果保存在 Phase 2 的 aliyun 数据中，见 [rq2-results-ghost-masking.md](../phase2/rq2-results-ghost-masking.md)）。

工程绿线：Phase 2 C++ 构建路径在 CX-6 + gcc 11.4 + cmake 3.22 组合下**没有任何回归或警告**。

### 2.2 pybind11 扩展构建

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
    -DSEMIRDMA_BUILD_BINDINGS=ON \
    -Dpybind11_DIR=$(python -c "import pybind11; print(pybind11.get_cmake_dir())")
cmake --build build -j $(nproc)
cp build/python/semirdma/_semirdma_ext*.so python/semirdma/
```

产物：`python/semirdma/_semirdma_ext.cpython-310-x86_64-linux-gnu.so`（~1 MB）。

### 2.3 UCQPEngine 实例化 smoke-test

```python
from semirdma._semirdma_ext import UCQPEngine
e = UCQPEngine('mlx5_0', 4*1024*1024, 16, 320)
qp_info = e.local_qp_info()
print(qp_info.qpn)     # → 非零 QP number，实际打印 135
print(qp_info.gid_raw) # → GID idx 0 (link-local IPv6); 后续 bring_up 时会用 idx 1
```

**观察：**
- `UCQPEngine` 构造函数在 `PORT_DOWN` 下**仍然能分配 QP**，状态停在 RESET / INIT，这是 verbs 语义（与 link state 解耦）。
- 这**证明**了 Stage B 单节点阶段可以做：QP 构造 / buffer MR 注册 / `ibv_modify_qp(INIT)` / `ibv_post_recv`。
- 不能做：`modify_qp(RTR)` / `post_send` / `poll_cq` 获得 CQE。需要 peer 上线。

### 2.4 测试环境中转成功：`python/semirdma/hooks.py` 可导入

```bash
PYTHONPATH=python python -c "from semirdma.hooks import semirdma_allreduce_hook; print(semirdma_allreduce_hook.__doc__)"
```

不报错 → Python 层的入口没有硬编码 `rxe0`，Stage A 代码在真机 namespace 下可直接 import。

---

## 3. 单节点**不能**做的（pending 2nd node）

1. **QP RTR/RTS 状态迁移** — `modify_qp(RTR)` 要求 remote QPN + remote GID 是真值；自回环（同节点 2 个 QP）理论可以，但 Stage B 的 bootstrap 代码假定 peer 是不同 IP，自回环测试需要另写路径，投入产出比不对，跳过。
2. **`ib_write_bw` / `ib_write_lat` 基线** — perftest 需要 server + client 两端。[`scripts/cloudlab/run_perftest.sh`](../../scripts/cloudlab/run_perftest.sh) 已写好，等第二节点。
3. **Phase 2 RQ1 在 CX-6 上的重扫** — 这是 Stage B **week 1 的核心交付物**；[`experiments/configs/stage_b_cloudlab.yaml`](../../experiments/configs/stage_b_cloudlab.yaml) 的 `chunk_bytes: 16384` 是占位，需要真机 2-node 扫 {4, 8, 16, 32, 64, 128} KiB 后重设。
4. **RC-Lossy / OptiReduce 5-baseline 对比** — design §2.2 定义的 RQ6 主实验。
5. **Stage A 的数值等价性在真机上复现** — aliyun SoftRoCE 已经 `max_rel_err=0%`（[rq5-results-ddp-baseline.md §1](./rq5-results-ddp-baseline.md)），Stage B 真机重跑一遍是把"数值等价在真 NIC 上也成立"这件事钉死的必要步骤。

---

## 4. 坑与解决方案（留给下次开工）

| # | 问题 | 解决方案 |
|---|------|----------|
| 1 | CloudLab 节点无 GitHub credentials，`git clone` 私仓失败 | 本地 `git archive --format=tar.gz -o /tmp/semirdma.tar.gz HEAD` + `scp` + `tar -xzf` 到目标节点。~136 KB tarball，几秒同步。 |
| 2 | `apt-get install libibverbs-utils` 报包不存在 | Ubuntu 22.04 的正确包名是 **`ibverbs-utils`**（无 `lib` 前缀）。[setup_env.sh](../../scripts/cloudlab/setup_env.sh) 已用正确名。 |
| 3 | 走 `--index-url https://download.pytorch.org/whl/cpu` 装 `hydra-core` 失败 | PyTorch index 不含非 torch 包。拆两步：先 torch/torchvision 用 pytorch index，再 pybind11/numpy/hydra-core/omegaconf 用默认 PyPI。 |
| 4 | Pybind 扩展 build 产物在 `build/python/semirdma/` 而不是 `python/semirdma/`，Python 找不到 | `cp build/python/semirdma/_semirdma_ext*.so python/semirdma/` 一步解决；Stage B 正式部署可以改为在 `setup.py` 里 `cmake --build` 后自动 copy。 |
| 5 | CloudLab SSH 初次失败（publickey），原因是我们的 GitHub key 没上传到 CloudLab 账户 | 中期：把 key 上传到 https://www.cloudlab.us/ssh-keys.php 即可对未来节点生效；当前正在运行的节点走 Web Shell 手动追加到 `~/.ssh/authorized_keys`。 |

---

## 5. 下次开工（2nd node 到位后）的前 30 分钟

```bash
# 0. 在两个节点分别：
bash scripts/cloudlab/day0_check.sh            # 读 fw / GID / link speed，10s 输出
bash scripts/cloudlab/setup_env.sh             # idempotent，已装过的会跳过

# 1. 两节点互 ping（实验 LAN IP）
ping -c 3 <peer-exp-lan-IP>

# 2. Perftest baseline（先 server 后 client）
# Node 0:
bash scripts/cloudlab/run_perftest.sh server
# Node 1:
bash scripts/cloudlab/run_perftest.sh client <node0-IP>
# 期望: ib_write_bw -s 65536 ≈ 92-96 Gbps

# 3. 真机 RQ1 chunk sweep (Stage B week 1 主交付物)
#    可直接复用 tests/phase2/test_chunk_sweep，但换 dev_name=mlx5_0
#    把 {4, 8, 16, 32, 64, 128} KiB 下的 RMS error / throughput 打在
#    docs/phase3/rq6-a-results-real-nic-recalibration.md

# 4. 真机 Stage A 等价性复现
torchrun --nnodes=2 --node_rank=0 --master_addr=<node0-IP> \
    experiments/stage_a/train_cifar10.py \
    --config-name stage_b_cloudlab \
    transport=semirdma loss_rate=0.0 seed=42 steps=100
# 对比 gloo baseline (同 seed, 同 steps) → 期望 max_rel_err < 1e-4
```

---

## 6. 不能声明什么（scope caveats）

遵循 [rq2-results-ghost-masking.md §5.4](../phase2/rq2-results-ghost-masking.md) 的 "不能声明什么" 惯例：

1. **不能说 Stage B 已经跑完** —— 只做了单节点构建/实例化验证，**没有任何网络 I/O、没有 CQE、没有 bandwidth 数据**。
2. **不能把 Phase 2 RQ1 的 16 KiB 饱和点直接搬到 Stage B** —— SoftRoCE 是 CPU-bound、CX-6 是硬件-offload，WQE rate 上限差 1–2 个数量级。Stage B week 1 必须做真机扫描。
3. **不能 extrapolate 到 CX-5** —— 我们拿到的是 CX-6，CX-5 的 WQE rate / atomic ops / doorbell 细节有差，CX-5 原计划节点（d7615）如果将来能约到，需要独立跑 recalibration。
4. **不能把"UC QP 构造成功"等同于"Write-with-Imm 在 CX-6 上正确工作"** —— 前者只要求 verbs API 合法，后者要求两端完成 INIT→RTR→RTS 并真实发包，后者 pending 2nd node。
5. **固件 20.38.1002 不在任何已知 UC QP bug 列表里**，但我们没有独立验证过它 —— Stage B 首次发包时如果看到奇怪行为（例如 imm_data 消失 / silent drop），第一反应查 fw release notes。

---

## 7. 相关文件索引

- [`scripts/cloudlab/day0_check.sh`](../../scripts/cloudlab/day0_check.sh) — 只读硬件/链路核对
- [`scripts/cloudlab/setup_env.sh`](../../scripts/cloudlab/setup_env.sh) — apt + .venv 一键环境
- [`scripts/cloudlab/run_perftest.sh`](../../scripts/cloudlab/run_perftest.sh) — 2-node `ib_write_bw/lat` wrapper
- [`experiments/configs/stage_b_cloudlab.yaml`](../../experiments/configs/stage_b_cloudlab.yaml) — Hydra 真机 config（dev_name=mlx5_0, GID idx 1, chunk_bytes TBD）
- [`docs/phase3/rq5-results-ddp-baseline.md`](./rq5-results-ddp-baseline.md) — Stage A on SoftRoCE（已完成）
- [`docs/phase3/design-ddp-integration.md`](./design-ddp-integration.md) — Stage A/B/C 总设计（本笔记是其 §2.2 的硬件附录）
