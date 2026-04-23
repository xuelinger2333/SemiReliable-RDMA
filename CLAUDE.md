# SemiRDMA — Software Semi-Reliable RDMA Transport for AI Training

## Project Overview

SemiRDMA is a **pure-software semi-reliable transport layer** built on RDMA UC QP (Unreliable Connected Queue Pair) for distributed AI training gradient communication. It targets **cloud lossy RoCEv2 networks** (no PFC) where RC QP's strict reliability causes severe tail latency degradation.

**Core insight:** SGD tolerates 1–5% gradient loss with negligible accuracy impact. Instead of paying the cost of full retransmission (RC) or abandoning RDMA entirely (UDP-based MLT/OptiReduce), SemiRDMA uses UC QP's zero-copy RDMA Write with software-layer loss tolerance.

**Target venue:** IEEE INFOCOM 2027 (abstract: 2026-07-17, full paper: 2026-07-24)
**Backup venue:** ACM SoCC 2026 R2 (full paper: 2026-07-14)

## Key Contributions

1. First pure-software semi-reliable RDMA transport for AI training (UC QP + gradient-aware loss tolerance on ConnectX-5 / ConnectX-6)
2. Write granularity formalization (chunk size / WQE rate / loss impact tradeoff)
3. Ghost gradient identification and mitigation (UC-specific stale buffer problem + masked aggregation)
4. Cross-layer adaptive Write granularity (gradient importance-driven per-layer chunk sizing)
5. CQE-driven completion boundary control (ratio-based forward progress using RDMA completion events)

## Architecture

```
PyTorch DDP  -->  gradient hooks  -->  SemiRDMA Python API (pybind11)
                                            |
                              SemiRDMA C++ Transport Library
                    +----------------------------------------------+
                    | Layer Analyzer    | per-layer importance      |
                    | Chunk Manager     | adaptive chunking + bitmap|
                    | Ratio Controller  | CQE counting + timeout   |
                    | UC QP Engine      | Write-with-Imm + ghost   |
                    +----------------------------------------------+
                                            |
                               RDMA NIC (ConnectX-5 / ConnectX-6 / SoftRoCE)
                               UC QP, no retransmission
```

## Directory Structure

```
SemiRDMA/
├── CLAUDE.md                    # This file
├── src/
│   ├── transport/               # Core C++ transport layer
│   │   ├── uc_qp_engine.h/cpp   # UC QP lifecycle, Write-with-Immediate
│   │   ├── chunk_manager.h/cpp  # Buffer chunking, completion bitmap
│   │   ├── ratio_controller.h/cpp # CQE polling, ratio-based progress
│   │   ├── ghost_mask.h/cpp     # Ghost gradient detection + zero-mask
│   │   └── layer_analyzer.h/cpp # Per-layer importance scoring
│   ├── bindings/                # pybind11 Python bindings
│   │   └── py_semirdma.cpp
│   └── utils/                   # Shared utilities (logging, timing)
├── python/
│   └── semirdma/                # Python package
│       ├── __init__.py
│       ├── transport.py         # Python transport wrapper
│       └── hooks.py             # PyTorch DDP gradient hooks
├── tests/
│   ├── unit/                    # C++ unit tests (gtest)
│   ├── integration/             # Multi-process RDMA tests
│   └── training/                # End-to-end training tests
├── benchmarks/
│   ├── wqe_rate/                # WQE throughput micro-benchmarks
│   ├── chunk_sweep/             # Chunk size sweep experiments
│   └── training/                # Full training benchmarks
├── scripts/
│   ├── setup_softroce.sh        # SoftRoCE environment setup
│   ├── run_benchmark.sh         # Benchmark runner
│   └── cloudlab/                # CloudLab deployment scripts
├── experiments/                 # Experiment configs and results
│   ├── configs/                 # Hydra configs for experiments
│   └── results/                 # Experiment output (gitignored)
├── docs/                        # Design docs and notes
├── plan/                        # Planning documents
├── CMakeLists.txt               # C++ build system
├── setup.py                     # Python package build
├── pyproject.toml               # Python project config
└── .gitignore
```

## Development Environment

### Target Platform: Linux

All code is written for Linux. The development workflow is:
- **Code editing:** Windows (this machine) — write code, commit, push
- **Building & running:** Remote Linux machine (cs528 or CloudLab) — pull, build, test

### Dependencies

**C++ (transport layer):**
- libibverbs (RDMA verbs API)
- librdmacm (RDMA connection manager)
- CMake >= 3.16
- GCC >= 9 or Clang >= 10
- pybind11 (Python bindings)
- Google Test (unit tests)

**Python:**
- Python >= 3.9
- PyTorch >= 2.0
- pybind11

**RDMA environment:**
- SoftRoCE (rxe) for prototyping (Phase 1, Phase 2, Phase 3 Stage A on aliyun)
- Mellanox ConnectX-5 / ConnectX-6 for real evaluation (Phase 3 Stage B, CloudLab)
  - d7525 nodes ship ConnectX-6 (MT28908, fw 20.38.1002, RoCEv2 GID idx 1, 100 GbE)
  - c240g5 Wisconsin nodes (deprecated 2026-04-23) shipped ConnectX-6 Lx (MT2894, fw 20.38.1002, 25 GbE) — data archived to `docs/phase3/results-cx6lx25g-c240g5_archive/`
  - **Current polishing platform (2026-04-23+)**: `amd203.utah.cloudlab.us` + `amd196.utah.cloudlab.us` with ConnectX-5 (fw 16.28.4512, 25 GbE, RoCEv2 GID idx 1). Multi-ACTIVE-port nodes (mlx5_0 on 128.110.x management + mlx5_2 on 10.10.1.x experiment LAN); `scripts/cloudlab/detect_rdma_dev.sh` + `day0_check.sh` prefer `enp<bus>s<slot>f<func>np<port>` naming to pick the experiment port. CPU-only torch (no GPU). Post-fix Stage B matrices land under `docs/phase3/results-cx5-amd203-amd196/`.
  - see `docs/phase3/stage-b-hardware-notes.md` §8 (c240g5 archive) / §9 (amd203/amd196 CX-5)

### Build Commands (Linux remote)

```bash
# C++ library
mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
make -j$(nproc)

# Python bindings
pip install -e .

# Unit tests
cd build && ctest --output-on-failure

# Single test compile (early prototyping)
gcc -o test_uc test_uc.c -libverbs -lpthread
```

### SoftRoCE Setup (Linux remote)

```bash
# Check device
ibv_devinfo -d rxe0
rdma link show

# If not configured
sudo rdma link add rxe0 type rxe netdev eno1
```

## Research Questions (RQ)

### RQ1: Write Granularity Optimization
Optimal chunk size `C*` that minimizes expected convergence error given loss rate `p` and NIC WQE rate limit `W_max`.

### RQ2: Ghost Gradient Problem (UC-specific)
Detect and compensate stale buffer data after silent UC Write failure. Solution: masked aggregation based on per-chunk completion bitmap.

### RQ3: Cross-Layer Adaptive Write Granularity
Dynamic per-layer chunk sizing driven by gradient sensitivity and real-time loss feedback.

### RQ4: CQE-Driven Completion Boundary Control
Use Write-with-Immediate receiver-side CQE as precise delivery signal for ratio-based forward progress.

## Experiment Configurations

### Five Comparison Baselines

| Config | Transport | Reliability | Purpose |
|--------|-----------|-------------|---------|
| RC-Baseline | RC QP | Full (HW retx) | Gold standard |
| RC-Lossy | RC QP + netem loss | Full but degraded | Show RC tail latency issue |
| OptiReduce | Gloo/UDP | UBT + Hadamard | Best non-RDMA semi-reliable |
| UD-Naive | UD QP | None | Lower bound |
| **SemiRDMA** | **UC QP** | **Semi-reliable (SW)** | **Our system** |

### Training Workloads

- ResNet-50 / ImageNet (subset) / 2–8 workers
- GPT-2 (small) / OpenWebText (subset) / 2–8 workers
- BERT-base / WikiText / 2–4 workers

### Key Metrics

- Time-to-accuracy (TTA)
- Iteration time
- Tail latency (P99)
- Gradient loss rate
- Final accuracy
- WQE throughput
- CQE polling overhead

## Timeline

| Phase | Dates | Focus |
|-------|-------|-------|
| Week 1–2 | Now → Apr 26 | UC QP validation on SoftRoCE |
| Week 3–4 | Apr 27 → May 10 | Core transport (ChunkManager, RatioController, GhostMask) |
| Week 5–6 | May 11 → May 24 | pybind11 + PyTorch hook integration |
| Week 7–8 | May 25 → Jun 7 | CloudLab deployment + comparison experiments |
| Week 9–10 | Jun 8 → Jun 21 | Layer Analyzer + ablation studies |
| Week 11–13 | Jun 22 → Jul 7 | Paper writing + submission |

## Coding Standards

### C++ (transport layer)

- C++17 standard
- File size: 200–400 lines per file
- Use `spdlog` or stderr logging (no printf debugging)
- All RDMA verb calls must check return codes
- Error handling: log + throw for unrecoverable, log + return error code for recoverable
- Naming: `snake_case` for functions/variables, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants
- Header guards: `#pragma once`
- Memory: RAII for all RDMA resources (PD, CQ, QP, MR)

### Python (bindings + hooks)

- Type hints on all functions
- Follow coding-style.md conventions
- Config-driven via Hydra/OmegaConf for experiments

### RDMA-Specific Conventions

- Always verify device existence before operations
- UC QP state transitions: RESET → INIT → RTR → RTS
- UC QP does NOT use: `max_rd_atomic`, `max_dest_rd_atomic`, `min_rnr_timer`, `timeout`, `retry_cnt` (those are RC-only)
- Write-with-Immediate requires receiver to post Receive WR
- Always use GID (not LID) for RoCE/SoftRoCE addressing

## Known Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| SoftRoCE UC QP support incomplete | High | Validate early; fall back to CloudLab |
| UC Write-with-Imm no receiver CQE on SoftRoCE | High | Test immediately; critical path |
| ConnectX-5/6 WQE rate too low for fine-grained chunks | Medium | Micro-benchmark first; determine chunk size floor |
| CQE polling overhead at high rates | Medium | Batch polling; event-driven fallback |
| CloudLab ConnectX-5/6 node availability | Medium | Reserve early; SoftRoCE as functional backup |
| Phase 2 RQ1 chunk-size tuned on SoftRoCE doesn't transfer to CX-6 100 GbE | Medium | Stage B week-1 recalibration sweep on real NIC |

## Git Workflow

- Branch: `main` for stable, feature branches for development
- Commit style: Conventional Commits (`feat:`, `fix:`, `test:`, `docs:`, `refactor:`, `perf:`, `chore:`)
- Push after each work session (user pulls on remote Linux machine)
- Keep commits small and reviewable

## Key References

- **MLT** (NSDI'24): Loss-tolerant training, UDP/TCP, layer-aware priority
- **OptiReduce** (NSDI'25): Resilient AllReduce, Gloo/UDP, Hadamard Transform
- **UCCL** (arXiv 2025): GPU networking software transport, UC/RC/UD, multi-path
- **Flor** (OSDI'23): Heterogeneous RNIC RDMA framework, UC QP + selective retransmission
- **SDR-RDMA** (SC'25): Software-defined reliability, bitmap-based partial completion
- **Celeris/OptiNIC** (arXiv 2025): Domain-specific RDMA NIC for ML (FPGA)
- **SHIFT** (arXiv 2025): Cross-NIC RDMA failover, idempotent batch transfer
