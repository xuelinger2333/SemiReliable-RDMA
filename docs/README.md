# SemiRDMA 文档索引

文档按 **Phase** 组织，每个 Phase 独立目录。文件名前缀表明文档类型：

| 前缀 | 含义 |
|------|------|
| `pN-` | Phase 1 的 PN 预研实验产物（如 `p0-walkthrough.md`） |
| `design-` | Phase 开工前的设计锁定文档 |
| `log-` | Phase 实施日志（工程流水账） |
| `rqN-results-` | 某个 Research Question 的独立结果文档 |

## Phase 1 · UC QP 验证（2026-04-12 → 2026-04-14，已完成）

目录：[`phase1/`](phase1/)

| 文件 | 内容 |
|------|------|
| [p0-walkthrough.md](phase1/p0-walkthrough.md) | P0 实验（500 轮 × 6 档丢包率扫描）的完整走读：动机、方法、四条核心结论、UC 两层语义 |
| [p0-sweep-500rounds.csv](phase1/p0-sweep-500rounds.csv) | P0 扫描原始数据 |
| `figures/` | 走读文档引用的图表 |

**四条核心结论**（对 Phase 2 的硬约束）：

1. 单 WR 内部丢包 → 整个 WR 作废，概率 `1-(1-p)^c`
2. `IBV_WC_RECV_RDMA_WITH_IMM` 是全送达的充要条件 — 不能扫 buffer
3. 污染模式是**纯前缀截断** — `{has_cqe, valid_len}` 够用，不需要 bitmap
4. 几何丢包模型成立 — 软件丢包注入在审稿上可辩护

## Phase 2 · Core Transport Layer（2026-04-16 → 进行中）

目录：[`phase2/`](phase2/)

| 文件 | 内容 |
|------|------|
| [design-core-transport.md](phase2/design-core-transport.md) | 开工前的设计锁定：接口草案、架构决策、实验方法、时间线 |
| [log-implementation.md](phase2/log-implementation.md) | 实施日志：代码落地、碰到的问题、修复过程、阶段性结论 |
| [rq2-results-ghost-masking.md](phase2/rq2-results-ghost-masking.md) | RQ2 结果：GhostMask 的梯度 RMS 误差量化（实测 ratio 0.707，与理论 `1/√2` 吻合） |

## 之后的 Phase（预留）

- **Phase 3** · PyTorch 集成 + Layer Analyzer（2026-05-11 → 2026-06-07）
- **Phase 4** · CloudLab ConnectX-5 + 对比实验（2026-06-08 → 2026-07-07）
- **Phase 5** · 论文写作与投稿（2026-07-08 → 2026-07-24）

## 写作约定

- 文档正文：简体中文
- 技术术语、venue 名、工具名、命名空间：保留英文原词
- 代码引用格式：`file:line`
- 日期格式：`YYYY-MM-DD`
