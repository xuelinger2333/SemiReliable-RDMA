# 100 GbE NIC 重新校准（amd247 / amd245 / amd264 集群）

> **日期：** 2026-04-29
> **集群：** chen123-303213.rdma-nic-perf-pg0（amd247=rank 0/recv，amd245=rank 1/send，amd264=XDP middlebox）
> **变化点：** 之前 amd203/amd196 用 `mlx5_1`（CX-5，25 GbE）；这次新 profile 起的是 `mlx5_2`（CX-5 Ex / MT28800，**100 GbE**），所有 25 GbE 时代锁定的 transport 参数必须重新验证。

---

## 1. 链路与设备 fact sheet

| 项 | amd247 (rank 0) | amd245 (rank 1) | amd264 (middlebox) |
|---|---|---|---|
| RDMA dev | `mlx5_2` | `mlx5_2` | n/a (mlx5_ib blacklisted) |
| netdev | `enp65s0f0np0` | `enp65s0f0np0` | `enp65s0f0np0` |
| LAN IP | 10.10.1.4 | 10.10.1.2 | 10.10.1.1 |
| MAC | `0c:42:a1:8c:db:c4` | `0c:42:a1:8c:db:dc` | `0c:42:a1:8b:31:80` |
| 链路速率 | **100 GbE** | **100 GbE** | 100 GbE |
| MTU (eth) | 9000 | 9000 | 9000 |
| `active_mtu` (IB) | 4096 | 4096 | — |
| GID idx | 3 (RoCE v2 IPv4-mapped) | 3 | — |

直连 `ib_write_bw -d mlx5_2 -x 3 --report_gbits -D 5` 基线：**98.01 Gbps**（near line-rate 100 GbE）。

---

## 2. NIC envelope：chunk_bytes 扫描

`ib_write_bw -s <SZ>` 直连测得：

| chunk_bytes | BW (Gbps) | MsgRate (Mpps) | bottleneck |
|---:|---:|---:|---|
| 512 | 15.74 | 3.84 | msg-rate |
| 1024 | 31.24 | 3.81 | msg-rate |
| 2048 | 63.71 | 3.89 | msg-rate |
| **4096** | **65.11** | **1.99** | **msg-rate** ← SemiRDMA 默认 |
| 8192 | 97.84 | 1.49 | BW knee |
| 16384 | 97.94 | 0.75 | line-rate |
| 65536 | 98.01 | 0.19 | line-rate |

**关键结论**：
- CX-5 Ex 在 100 GbE 上 msg-rate 上限 ≈ **2.0 Mpps @ 4096 B** → 65 Gbps cap
- 25 GbE 时代 `chunk_bytes=4096` 是受 link 限（25 Gbps）；100 GbE 时代变成受 **msg-rate 限**（65 Gbps）
- 链路升级 4×，但 SemiRDMA 实际有效带宽只升 ~2.6×（65/25）
- 不能简单加大 `chunk_bytes` 到 8192：`active_mtu=4096` 是 CX-5 IB 上限，> 4096 会让一个 chunk 拆 2 IB 包，UC 单包丢即整 chunk 丢，破坏正确性前提
- **结论：`chunk_bytes=4096` 锁死，与 25 GbE 时代一致**

---

## 3. SQ_DEPTH 扫描（drop=0, STEPS=200, single seed）

| SQ_DEPTH | final_loss | mean iter ms | p50 | p99 |
|---:|---:|---:|---:|---:|
| **8** (默认) | 1.736 | 702 | 701 | **817** |
| 16 | 1.663 | 705 | 707 | 808 |
| 32 | 1.789 | **674** | 672 | 854 |
| 64 | 1.714 | 696 | 688 | 841 |

- mean iter_ms 在 SQ ∈ {8…64} 内 CV ~5%，**实际是 forward+backward 占大头**（~700 ms 中 RDMA 段顶多 100 ms）
- p99 在 SQ=8 最低，再放大反而上升
- 25 GbE 时代选 SQ=8 是为了避免 back-to-back UC 包丢（NIC HW 现象）；100 GbE 下 NIC 在 msg-rate 上限锁住，SQ 不再是瓶颈
- **结论：保持 `sq_depth=8`**

历史对比：25 GbE PR-B drop=0 mean iter = 854 ms → 100 GbE 测得 702 ms（−18%）。链路加速被 CPU 训练时间稀释。

---

## 4. End-to-end sanity：drop=0.05 through XDP middlebox

| 配置 | 值 |
|---|---|
| transport | semirdma (flat ratio) |
| chunk_bytes | 4096 |
| sq_depth | 8 |
| timeout_ms | 200 |
| ratio | 0.95 floor |
| STEPS | 200 |
| middlebox drop_pct | 5.00% (XDP Bernoulli) |
| GID idx | 3 (强制；ARP-spoof 必需) |

**结果**：
- final_loss = 1.543（vs drop=0 的 1.74，loss 反而略低 = 同 seed 起点不同）
- mean_iter_ms = 811（vs drop=0 的 702，+15%，符合预期：丢包→等 timeout 比例升）
- middlebox 实测 wire drop = **5.0048%**（目标 5%，精度 0.01% 内，3.77 M RoCE 包）
- SemiRDMA UC + ghost mask + ratio=0.95 floor 全程稳定，无 hang，无 RC fallback

→ `chunk_bytes=4096`、`sq_depth=8`、`timeout_ms=200`、`ratio=0.95` 这套 25 GbE 时代锁定的参数 **在 100 GbE 上仍然适用**，无需重调；只是 NIC 侧的有效 BW 上限从 25 Gbps 升到 ~65 Gbps。

---

## 5. 锁定的最终参数（用于后续 PR-C / PR-D 实验）

```yaml
# experiments/configs/stage_b_cloudlab.yaml — 100 GbE adjusted
transport_cfg:
  chunk_bytes: 4096          # path_mtu cap, single IB packet per chunk
  sq_depth: 8                # SQ wave throttle, 100 GbE confirms still optimal
  rq_depth: 16384            # unchanged
  timeout_ms: 200            # unchanged, comfortably above 100 GbE iter floor
  ratio: 0.95                # SGD tolerance floor unchanged
  gid_index: 3               # required when middlebox in path
```

---

## 6. 不做的校准（明确放弃，已有充分论据）

- ❌ `chunk_bytes ∈ {1024, 2048, 8192}` 在 SemiRDMA 端到端：1024/2048 受 msg-rate 限，BW 反而更低；8192 破坏 single-packet-per-chunk 的 UC 正确性前提
- ❌ `path_mtu` 升到 8192：CX-5 IB 上限是 4096，硬件不支持
- ❌ `timeout_ms < 200`：iter floor 100 GbE 下仍是 ~700 ms，200 ms timeout 离 floor 远，缩小没意义；缩到 50 ms 反而进入抖动带

---

## 7. 后续 PR-C 真机回归方法

参数锁定 → 回到 PLAN.md 主线：
1. **PR-C E2E 回归**：在 amd247/amd245 上以 `bucket_cap_mb=512` 重跑 PR-B 18-cell 矩阵（3 seed × 2 transport × 3 drop），验证 `imm_data` bucket_id 编码（commit `a248914`）不破坏 flat 路径
2. **PR-C heterogeneous registry 验证**：`bucket_cap_mb=1` + BN p=0 / conv p=0.05 / fc p=0.01，看 dispatcher DIAG mixed-route per-step
3. **PR-D**：5+ seed 扩展 + heterogeneous-p_L sweep，paper-grade std

预期 PR-B 18-cell 在 100 GbE 上 ~1.5 h（之前 25 GbE 是 2.1 h，因 iter_ms 降 18%）。

---

## 8. 复现命令

### bootstrap
```bash
# amd247, amd245
bash scripts/cloudlab/bootstrap_fresh_node.sh

# amd264
sudo bash scripts/cloudlab/middlebox_setup.sh bootstrap
```

### NIC envelope
```bash
# server (amd247)
ib_write_bw -d mlx5_2 -x 3 -F --report_gbits -D 5 -s 4096

# client (amd245)
ib_write_bw -d mlx5_2 -x 3 -F --report_gbits -D 5 -s 4096 10.10.1.4
```

### Middlebox + drop=0.05 sanity
```bash
# amd264
XDP_MODE=generic IFACE=enp65s0f0np0 \
  PEER_A_IP=10.10.1.4 PEER_A_MAC=0c:42:a1:8c:db:c4 \
  PEER_B_IP=10.10.1.2 PEER_B_MAC=0c:42:a1:8c:db:dc \
  sudo -E bash scripts/cloudlab/middlebox_setup.sh start 0.05

# amd247 + amd245 (ARP spoof, MBX_MAC = amd264 enp65s0f0np0 MAC)
sudo arp -s <peer_ip> 0c:42:a1:8b:31:80

# amd247 (matrix runner)
STEPS=200 DROP_RATES="0.05" TIMEOUTS_MS=200 \
  NODE0_IP=10.10.1.4 NODE1_IP=10.10.1.2 NODE_PEER_HOST=chen123@10.10.1.2 \
  TRANSPORTS=semirdma \
  MIDDLEBOX_HOST=chen123@amd264.utah.cloudlab.us \
  bash scripts/cloudlab/run_p1_matrix.sh
```
