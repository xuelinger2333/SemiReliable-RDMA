"""PyTorch DDP communication hook backed by SemiRDMA transport.

Wiring:

    state = SemiRDMAHookState.for_rank(rank=0, world_size=2,
                                       peer_host="10.0.0.2", port=29700,
                                       cfg=TransportConfig(...))
    model = DDP(model)
    model.register_comm_hook(state, semirdma_allreduce_hook)

On each backward pass DDP calls ``semirdma_allreduce_hook(state, bucket)``
with a ``dist.GradBucket`` containing a flat tensor of gradient bytes.  The
hook must return a ``torch.futures.Future[torch.Tensor]`` that eventually
resolves to the *averaged* gradient tensor.

For Stage A's 2-worker setup an all-reduce reduces to a plain swap:

    rank 0 sends its bucket to rank 1 (via tx), receives rank 1's bucket
    (via rx), averages (bucket + remote) / 2, returns.

We ping-pong the exchange so both workers can drive the hook synchronously
without deadlocking.  Per-bucket work happens on the DDP thread — torch
futures resolve immediately here; there is no worker thread pool.  This
keeps Stage A simple; Stage B can add a background dispatcher for
bucket-overlap if profiling demands it.
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
from semirdma.transport import SemiRDMATransport

logger = logging.getLogger(__name__)


@dataclass
class SemiRDMAHookState:
    """State carried across DDP hook invocations for one worker.

    Owns two transports (``tx`` outbound, ``rx`` inbound), both already
    brought up against the peer.  Stage A uses a single peer (world_size=2).

    Construction is async-unsafe: use ``SemiRDMAHookState.for_rank`` to build.
    """

    rank: int
    world_size: int
    cfg: TransportConfig
    tx: SemiRDMATransport
    rx: SemiRDMATransport
    bucket_idx: int = 0

    @classmethod
    def for_rank(
        cls,
        *,
        rank: int,
        world_size: int,
        peer_host: str,
        port: int,
        cfg: Optional[TransportConfig] = None,
    ) -> "SemiRDMAHookState":
        """Build a fully-bootstrapped state for the given rank.

        The two-QP bring-up uses two different TCP ports
        (``port``, ``port + 1``) so the send-direction and recv-direction
        exchanges don't collide.
        """
        if world_size != 2:
            raise NotImplementedError(
                "Stage A only supports world_size=2; Stage B tackles N > 2"
            )
        cfg = cfg or TransportConfig()

        tx = SemiRDMATransport(cfg)
        rx = SemiRDMATransport(cfg)

        # TX direction: rank 0 server, rank 1 client.  We advertise tx's
        # local info and expect the peer's rx info back (because from the
        # peer's perspective its rx is what our tx writes into).
        tx_is_server = (rank == 0)
        tx_port = port
        tx_remote_qp, tx_remote_mr = exchange_qp_info(
            is_server=tx_is_server,
            host=peer_host if not tx_is_server else "0.0.0.0",
            port=tx_port,
            local_qp=tx.local_qp_info,
            local_mr=tx.local_mr_info,
        )
        tx.bring_up(tx_remote_qp, tx_remote_mr)

        # RX direction: flip server/client so both directions bring up
        # concurrently without nested sockets.  rank 1 now listens.
        rx_is_server = (rank == 1)
        rx_port = port + 1
        rx_remote_qp, rx_remote_mr = exchange_qp_info(
            is_server=rx_is_server,
            host=peer_host if not rx_is_server else "0.0.0.0",
            port=rx_port,
            local_qp=rx.local_qp_info,
            local_mr=rx.local_mr_info,
        )
        rx.bring_up(rx_remote_qp, rx_remote_mr)

        logger.info(
            "SemiRDMAHookState up: rank=%d, tx qpn=%d, rx qpn=%d",
            rank, tx.qpn, rx.qpn,
        )
        return cls(rank=rank, world_size=world_size, cfg=cfg, tx=tx, rx=rx)


# A single lock guards both transports because the DDP thread drives them
# strictly serially; the lock is mostly defensive against future multi-bucket
# parallelism.
_HOOK_LOCK = threading.Lock()


def semirdma_allreduce_hook(
    state: SemiRDMAHookState,
    bucket: "dist.GradBucket",
) -> "futures.Future[torch.Tensor]":
    """DDP communication hook: SemiRDMA-backed 2-worker all-reduce.

    Semantics: produces (local + remote) / world_size, matching what
    ``allreduce_hook`` would return.  If the receiver times out before all
    chunks arrive, the missing chunks have been zeroed by ``GhostMask``;
    the averaged result is therefore biased slightly toward the sender for
    missed regions — this is the Phase 2 semi-reliability trade-off,
    deliberately exposed to the training loop.
    """
    flat = bucket.buffer()  # torch.Tensor, 1-D, usually float32
    if flat.device.type != "cpu":
        # Stage A is CPU-only; Stage B will add a staging tensor on CPU.
        raise RuntimeError(
            f"semirdma_allreduce_hook: bucket must be on CPU, got {flat.device}"
        )
    if not flat.is_contiguous():
        flat = flat.contiguous()

    # Interpret the flat float tensor as raw bytes for the MR copy.
    # flat.numel() * element_size() == total byte count.
    nbytes = flat.numel() * flat.element_size()
    if nbytes > state.cfg.buffer_bytes:
        raise RuntimeError(
            f"semirdma_allreduce_hook: bucket {nbytes} B exceeds "
            f"buffer_bytes={state.cfg.buffer_bytes}.  Increase "
            f"TransportConfig.buffer_bytes or shrink DDP bucket_cap_mb."
        )

    byte_view = memoryview(flat.numpy()).cast("B")  # zero-copy uint8 view
    assert len(byte_view) == nbytes

    fut: "futures.Future[torch.Tensor]" = futures.Future()

    with _HOOK_LOCK:
        bucket_id = state.bucket_idx
        state.bucket_idx += 1

        # ------------------ send local, receive remote ----------------
        cs_send = state.tx.post_gradient(byte_view)
        cs_recv = ChunkSet(0, nbytes, state.cfg.chunk_bytes)
        stats = state.rx.await_gradient(cs_recv)

        # Peer's bytes are now in state.rx.buffer_view()[0:nbytes].  Build a
        # torch view that shares memory with the MR so averaging is a single
        # vectorized add.
        import numpy as np

        remote_np = np.frombuffer(state.rx.buffer_view(), dtype=np.uint8)[:nbytes]
        # View as the same dtype/shape as the bucket tensor.  np.frombuffer
        # gives us read-only semantics; .view() reinterprets, .reshape(-1)
        # matches flat's shape.
        remote_typed = remote_np.view(flat.numpy().dtype).reshape(flat.shape)
        remote_t = torch.from_numpy(remote_typed)

        # Average in-place into flat to avoid a second allocation.
        flat.add_(remote_t)
        flat.div_(state.world_size)

        # Also drain any sender-side CQEs that piled up; ignore count.
        state.tx.drain_send_completions()

    logger.debug(
        "semirdma_allreduce_hook: bucket=%d nbytes=%d stats=%s",
        bucket_id, nbytes, stats,
    )
    fut.set_result(flat)
    return fut


__all__ = [
    "SemiRDMAHookState",
    "semirdma_allreduce_hook",
]
