"""Isolated baselines for RQ6 main-experiment comparison.

Hook roster (post-2026-04-25 XDP middlebox era):

  rc_baseline_hook  — Gloo-TCP ``dist.all_reduce``.  "Baseline of
                      baselines": pure-reliable, software-only reference
                      curve.  Unaffected by middlebox drop (TCP:29500,
                      not UDP:4791) so it isolates the pure-SGD
                      reliability effect without wire-level drop noise.

  rc_lossy_hook     — Gloo-TCP ``dist.all_reduce`` + shared-seed per-
                      chunk Bernoulli zeroing applied identically on
                      both ranks (drift-free).  Isolates "accuracy
                      degradation from lossy information" from "UC-
                      specific per-rank drift".  Answers: "is
                      semirdma's accuracy parity the credit of UC, or
                      just of lossy-info tolerance?".

  rc_rdma_allreduce_hook — HW-reliable RC QP over the same UCQPEngine
                      as SemiRDMA (qp_type="rc"), 2-worker tx/rx swap
                      via Write-with-Imm.  Exposes real RC behavior
                      under XDP middlebox drop: retry chain tail
                      latency and IBV_WC_RETRY_EXC_ERR abort.  This is
                      the training-layer pair to the ``ib_write_bw`` RC
                      transport-layer sweep.

  semirdma_allreduce_hook (imported from ``semirdma.hooks``, NOT this
                      package) — your method: UC QP + ghost mask +
                      ratio-based progress.

Rationale for isolation:
  - Baselines are for comparison only; they must not pollute the
    SemiRDMA critical path (``semirdma/{transport,hooks,config}.py``
    and ``src/transport/``).
  - Deleting this entire directory drops all baselines cleanly if the
    paper's baseline table changes.
"""

from semirdma.baselines.rc_hook import RCBaselineState, rc_baseline_hook
from semirdma.baselines.rc_lossy_hook import (
    RCLossyConfig,
    RCLossyState,
    rc_lossy_hook,
)
from semirdma.baselines.rc_rdma_hook import (
    RCRDMAHookState,
    rc_rdma_allreduce_hook,
)
from semirdma.baselines.rc_rdma_transport import ReliableRDMATransport

__all__ = [
    "RCBaselineState",
    "RCLossyConfig",
    "RCLossyState",
    "RCRDMAHookState",
    "ReliableRDMATransport",
    "rc_baseline_hook",
    "rc_lossy_hook",
    "rc_rdma_allreduce_hook",
]
