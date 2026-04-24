"""RC-RDMA DDP comm hook — HW-reliable RC QP baseline.

The reviewer-facing "RC-Baseline" for RQ6 training-layer evaluation:
same end-to-end pipeline as ``semirdma_allreduce_hook`` (2-worker tx/rx
swap over a registered MR, Write-with-Immediate per chunk), but the QP
type is RC instead of UC so the NIC performs ACK + retransmit + retry-
exhausted error handling in hardware.

This hook answers the reviewer question "why not RC?":
  - on a clean wire, RC matches SemiRDMA-UC in accuracy and throughput
    (both go zero-copy through the same MR, same chunk granularity).
  - on a lossy wire (XDP middlebox drop > 0), RC either
      (a) pays an iteration-time tail as retries pile up — making the
          40× throughput gap visible at training layer, not just in
          ``ib_write_bw``; or
      (b) exceeds retry_cnt and aborts with IBV_WC_RETRY_EXC_ERR —
          documenting that HW-RC is catastrophic, not graceful, under
          loss.
  - SemiRDMA holds throughput AND converges.

Implementation: mirrors ``SemiRDMAHookState.for_rank`` so the training
driver can substitute ``transport=rc_rdma`` without touching rendezvous
/ bootstrap code.  Two ``ReliableRDMATransport`` instances (tx/rx), both
brought up via the same TCP exchange pattern used by the UC hook.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Optional

import torch
import torch.distributed as dist
from torch import futures

from semirdma._semirdma_ext import ChunkSet
from semirdma._bootstrap import exchange_qp_info
from semirdma.config import TransportConfig
from semirdma.baselines.rc_rdma_transport import ReliableRDMATransport

logger = logging.getLogger(__name__)


@dataclass
class RCRDMAHookState:
    """State carried across DDP hook invocations for the RC-RDMA baseline.

    Mirrors ``SemiRDMAHookState`` so the training driver's plumbing
    (bucket_cap_mb, single-bucket constraint, MR-slot partitioning) is
    structurally identical — only the transport underneath changes.
    """

    rank: int
    world_size: int
    cfg: TransportConfig
    tx: ReliableRDMATransport
    rx: ReliableRDMATransport
    bucket_idx: int = 0
    n_slots: int = 2

    @classmethod
    def for_rank(
        cls,
        *,
        rank: int,
        world_size: int,
        peer_host: str,
        port: int,
        cfg: Optional[TransportConfig] = None,
    ) -> "RCRDMAHookState":
        if world_size != 2:
            raise NotImplementedError(
                "RC-RDMA baseline is 2-worker for now; multi-worker "
                "would need a ring-reduce on top of pairwise RC links."
            )
        if cfg is None:
            cfg = TransportConfig(qp_type="rc")
        elif cfg.qp_type != "rc":
            raise ValueError(
                f"RCRDMAHookState requires qp_type='rc' in config, "
                f"got {cfg.qp_type!r}"
            )

        tx = ReliableRDMATransport(cfg)
        rx = ReliableRDMATransport(cfg)

        # Port P: rank0-writes-to-rank1 direction.
        if rank == 0:
            remote_qp, remote_mr = exchange_qp_info(
                is_server=True, host="0.0.0.0", port=port,
                local_qp=tx.local_qp_info, local_mr=tx.local_mr_info,
            )
            tx.bring_up(remote_qp, remote_mr)
        else:
            remote_qp, remote_mr = exchange_qp_info(
                is_server=False, host=peer_host, port=port,
                local_qp=rx.local_qp_info, local_mr=rx.local_mr_info,
            )
            rx.bring_up(remote_qp, remote_mr)

        # Port P+1: reverse direction.
        if rank == 0:
            remote_qp, remote_mr = exchange_qp_info(
                is_server=False, host=peer_host, port=port + 1,
                local_qp=rx.local_qp_info, local_mr=rx.local_mr_info,
            )
            rx.bring_up(remote_qp, remote_mr)
        else:
            remote_qp, remote_mr = exchange_qp_info(
                is_server=True, host="0.0.0.0", port=port + 1,
                local_qp=tx.local_qp_info, local_mr=tx.local_mr_info,
            )
            tx.bring_up(remote_qp, remote_mr)

        logger.info(
            "RCRDMAHookState up: rank=%d tx qpn=%d rx qpn=%d "
            "(rc_timeout=%d, rc_retry_cnt=%d)",
            rank, tx.qpn, rx.qpn, cfg.rc_timeout, cfg.rc_retry_cnt,
        )
        return cls(rank=rank, world_size=world_size, cfg=cfg, tx=tx, rx=rx)


# One lock across tx/rx: DDP drives the hook strictly serially within a
# step, matching the SemiRDMA hook's guard.
_HOOK_LOCK = threading.Lock()


def rc_rdma_allreduce_hook(
    state: RCRDMAHookState,
    bucket: dist.GradBucket,
) -> futures.Future[torch.Tensor]:
    """DDP hook: HW-reliable RC-RDMA 2-worker all-reduce.

    Semantics: returns ``(local + remote) / world_size`` exactly (no
    biased-shrinkage, no ghost mask — HW-RC is either-all-or-abort).
    On a lossy wire this hook's iteration time scales with the per-
    chunk retry chain, which is the "RC崩" signal we want to record.
    """
    flat = bucket.buffer()
    if flat.device.type != "cpu":
        raise RuntimeError(
            f"rc_rdma_allreduce_hook: bucket must be on CPU, got {flat.device}"
        )
    if not flat.is_contiguous():
        flat = flat.contiguous()

    nbytes = flat.numel() * flat.element_size()
    if nbytes > state.cfg.buffer_bytes:
        raise RuntimeError(
            f"rc_rdma_allreduce_hook: bucket {nbytes} B exceeds "
            f"buffer_bytes={state.cfg.buffer_bytes}"
        )

    byte_view = memoryview(flat.numpy()).cast("B")
    assert len(byte_view) == nbytes

    fut: "futures.Future[torch.Tensor]" = futures.Future()

    with _HOOK_LOCK:
        bucket_id = state.bucket_idx
        state.bucket_idx += 1

        slot_bytes = state.cfg.buffer_bytes // state.n_slots
        slot = bucket_id % state.n_slots
        base = slot * slot_bytes
        if nbytes > slot_bytes:
            raise RuntimeError(
                f"rc_rdma_allreduce_hook: bucket {nbytes} B exceeds "
                f"slot_bytes={slot_bytes} (buffer_bytes="
                f"{state.cfg.buffer_bytes}, n_slots={state.n_slots})"
            )

        cs_send = state.tx.post_bucket(
            byte_view, base_offset=base, remote_base_offset=base
        )
        cs_recv = ChunkSet(base, nbytes, state.cfg.chunk_bytes)
        stats = state.rx.await_bucket(cs_recv)

        # Average peer's gradient into ours.  For RC every byte arrived
        # intact (or an exception would have fired above), so the view
        # is the authentic peer gradient — no ghost-mask zeroing.
        import numpy as np
        remote_np = np.frombuffer(state.rx.buffer_view(), dtype=np.uint8)[
            base : base + nbytes
        ]
        remote_typed = remote_np.view(flat.numpy().dtype).reshape(flat.shape)
        remote_t = torch.from_numpy(remote_typed)

        flat.add_(remote_t)
        flat.div_(state.world_size)

        state.tx.drain_send_completions()

    logger.debug(
        "rc_rdma_allreduce_hook: bucket=%d nbytes=%d recv_latency_ms=%.2f",
        bucket_id, nbytes, stats["latency_ms"],
    )
    fut.set_result(flat)
    return fut


__all__ = [
    "RCRDMAHookState",
    "rc_rdma_allreduce_hook",
]
