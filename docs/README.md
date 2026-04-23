# SemiRDMA 文档索引

> **当前状态快照 + 下一步：** [PLAN.md](PLAN.md)

## 分阶段最终文档（读这两份就够了）

| Phase | 最终文档 | 状态 |
|---|---|---|
| Phase 1 · UC QP 语义验证 | [phase1/p0-walkthrough.md](phase1/p0-walkthrough.md) | ✅ 完成 |
| Phase 2 · Core Transport Layer (C++) | [phase2/phase2-final.md](phase2/phase2-final.md) | ✅ 完成 |
| Phase 3 · DDP Integration + Real-NIC Validation | [phase3/phase3-final.md](phase3/phase3-final.md) | ✅ 完成（CX-5 benign wire） |
| Phase 4 · Lossy Wire Validation + Paper Writing | 启动中 | ⏳ 见 [PLAN.md](PLAN.md) |

## 历史文档

每个 phase 的原始设计、日志、各 RQ 独立分析、各平台原始数据都保留在 `history/` 子目录：

- [phase2/history/](phase2/history/) — `design-core-transport.md` / `rq1-log-implementation.md` / `rq2-results-ghost-masking.md` / `rq4-results-ratio-timeout.md`
- [phase3/history/](phase3/history/) — 10 个 top-level 文档 + `results-cx5-amd203-amd196/` + `results-cx6lx25g-c240g5_archive/` 两个结果树

最终文档 `phaseN-final.md` 已经把这些 history 材料里真正 load-bearing 的结论摘出来了；history 主要用于回溯细节（具体数据表、原始 diagnostic、bug 诊断链）。

## 写作约定

- 正文：简体中文；技术术语 / venue / 工具名 / 命名空间 保留英文
- 代码引用：`file:line` 格式
- 日期：`YYYY-MM-DD`
- Phase 2-3 的细节分析在实验进行中会陆续落盘到 history/，最终文档由完成时 review 过后的摘要版组成
