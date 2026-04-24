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

H3 fix variant (``semirdma_hybrid_allreduce_hook``): splits the bucket into
owned halves and runs a 2-phase Ring AllReduce:

  Phase 1 (UC reduce-scatter): each rank UC-writes the peer-owned half to
    the peer; ghost-masks missed chunks on its own-owned half.
  Phase 2 (gloo all-gather): reliable TCP AllGather of each rank's partial
    sum for its owned half.  Both ranks end with a byte-identical averaged
    gradient — no rank-asymmetric ghost drift (H3 root cause).
"""

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
    # DDP typically fires 2–3 buckets per step for ResNet-18 (~47 MiB of
    # gradient bytes at bucket_cap_mb=25).  If two buckets shared the same
    # MR offset range [0, nbytes), the peer's second-bucket Writes would
    # overwrite the first bucket's still-in-flight data.  Partition the MR
    # into ``n_slots`` disjoint chunks so bucket_idx%n_slots owns a unique
    # slot.  Stage A's default buffer_bytes=64 MiB with n_slots=2 gives
    # 32 MiB/slot, enough for any ResNet-18 bucket.
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

        # Port P is the rank0-writes-to-rank1 direction.  On this channel
        # rank 0 is the writer (advertises its *tx* QP) and rank 1 is the
        # target (advertises its *rx* QP).  After the exchange rank 0.tx
        # has rank1.rx as its remote, and rank 1.rx knows which peer QPN
        # is permitted to Write into it.  The earlier version had both
        # sides advertise their tx info, which made rank 0.tx point at
        # rank1.tx — so Writes landed on the peer's *tx* QP and were
        # invisible to rank 1.rx's CQ (observed as grad_l2 ~= local/√2).
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

        # Port P+1 is the reverse: rank 1 writes, rank 0 receives.
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
    bucket: dist.GradBucket,
) -> futures.Future[torch.Tensor]:
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

        # Partition MR into disjoint slots so back-to-back buckets in the
        # same step don't overwrite each other's still-in-flight data.
        slot_bytes = state.cfg.buffer_bytes // state.n_slots
        slot = bucket_id % state.n_slots
        base = slot * slot_bytes
        if nbytes > slot_bytes:
            raise RuntimeError(
                f"semirdma_allreduce_hook: bucket {nbytes} B exceeds "
                f"slot_bytes={slot_bytes} (buffer_bytes={state.cfg.buffer_bytes}, "
                f"n_slots={state.n_slots}).  Shrink bucket_cap_mb or bump "
                f"TransportConfig.buffer_bytes."
            )

        # ------------------ send local, receive remote ----------------
        cs_send = state.tx.post_gradient(
            byte_view, base_offset=base, remote_base_offset=base
        )
        cs_recv = ChunkSet(base, nbytes, state.cfg.chunk_bytes)
        stats = state.rx.await_gradient(cs_recv)

        # Peer's bytes are now in state.rx.buffer_view()[base:base+nbytes].
        # Build a torch view that shares memory with the MR so averaging is
        # a single vectorized add.
        import numpy as np

        remote_np = np.frombuffer(state.rx.buffer_view(), dtype=np.uint8)[base : base + nbytes]
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


def semirdma_hybrid_allreduce_hook(
    state: SemiRDMAHookState,
    bucket: dist.GradBucket,
) -> futures.Future[torch.Tensor]:
    """Hybrid Ring AllReduce: UC reduce-scatter + gloo all-gather.

    Stage 1 de-risk of the hybrid design (2-rank only).  Compared to
    ``semirdma_allreduce_hook``, the two ranks do not symmetrically swap
    the full bucket via UC; instead each rank *owns* half of the bucket
    and only the peer-owned half is shipped over UC.  Phase 2 then uses
    reliable gloo AllGather to broadcast each half's partial sum back to
    both ranks, producing a byte-identical averaged tensor.

    Semantics per bucket index i:

        own_slice  = flat[rank*K : (rank+1)*K]      # K = numel // 2
        peer_slice = flat[(1-rank)*K : (2-rank)*K]

      Phase 1 (UC reduce-scatter):
          - post_gradient(peer_slice_bytes)  # UC Write to peer
          - await_gradient for peer's own_slice bytes (ghost-masked)
          - own_partial = own_slice + ghost(peer_bytes_for_own_slice)

      Phase 2 (gloo all-gather):
          - all_gather_into_tensor(gathered, own_partial)    # TCP, reliable
          - flat.copy_(gathered / world_size)

    Why this fixes H3: the original ``semirdma_allreduce_hook`` has each
    rank compute  ``(own + ghost(peer)) / 2`` on the *full* bucket, with
    the ghost mask applied only to the peer-received half — so rank 0
    keeps g_0[i]/2 while rank 1 keeps g_1[i]/2 at chunks that dropped
    asymmetrically on the two rx paths, producing model drift.  In the
    hybrid path both ranks learn the *same* masked averaged value for
    each element (courtesy of gloo AllGather's reliability), so the two
    model replicas stay bit-identical.

    Magnitude compensation: when a chunk c in our owned half gets
    ghost-masked (peer's bytes for c didn't arrive), the naive partial
    sum is ``g_own[c] + 0`` and after ``/world_size`` we'd apply
    ``g_own[c]/2`` — a systematic gradient magnitude shrinkage.  To
    preserve the expected gradient magnitude we scale the partial sum at
    missed chunks by ``world_size`` so the final post-divide update is
    ``g_own[c]`` (unbiased estimator under the assumption that
    E[g_own[c]] == E[g_peer[c]] — i.i.d. mini-batches).  Variance at
    missed chunks rises (fewer samples averaged), but the first moment
    matches.  Both ranks apply the same scaling through ``all_gather``,
    so no drift is reintroduced.

    Limitations: 2-rank only.  N>2 needs a ring-topology version with
    rank-dependent owned chunks — Stage 2 of the plan.  Bucket must have
    even element count (we split the element dimension, not bytes).
    """
    if state.world_size != 2:
        raise NotImplementedError(
            "semirdma_hybrid_allreduce_hook Stage 1 supports world_size=2 "
            "only; Stage 2 will add ring topology for N>2"
        )

    flat = bucket.buffer()
    if flat.device.type != "cpu":
        raise RuntimeError(
            f"semirdma_hybrid_allreduce_hook: bucket must be on CPU, got {flat.device}"
        )
    if not flat.is_contiguous():
        flat = flat.contiguous()

    numel = flat.numel()
    if numel % 2 != 0:
        raise RuntimeError(
            f"semirdma_hybrid_allreduce_hook: bucket numel={numel} is odd; "
            "hybrid hook requires even element count for 2-rank half-split"
        )
    elem_size = flat.element_size()
    half_numel = numel // 2
    half_nbytes = half_numel * elem_size

    if half_nbytes > state.cfg.buffer_bytes // state.n_slots:
        raise RuntimeError(
            f"semirdma_hybrid_allreduce_hook: half bucket {half_nbytes} B "
            f"exceeds slot_bytes={state.cfg.buffer_bytes // state.n_slots}."
        )

    own_start = state.rank * half_numel
    own_end = own_start + half_numel
    peer_start = (1 - state.rank) * half_numel
    peer_end = peer_start + half_numel

    own_slice = flat[own_start:own_end]
    peer_slice = flat[peer_start:peer_end]

    fut: "futures.Future[torch.Tensor]" = futures.Future()

    import numpy as np

    with _HOOK_LOCK:
        bucket_id = state.bucket_idx
        state.bucket_idx += 1

        slot_bytes = state.cfg.buffer_bytes // state.n_slots
        slot = bucket_id % state.n_slots
        base = slot * slot_bytes

        # ---------- Phase 1: UC reduce-scatter ----------
        peer_byte_view = memoryview(peer_slice.numpy()).cast("B")
        assert len(peer_byte_view) == half_nbytes
        cs_send = state.tx.post_gradient(
            peer_byte_view, base_offset=base, remote_base_offset=base
        )
        cs_recv = ChunkSet(base, half_nbytes, state.cfg.chunk_bytes)
        stats = state.rx.await_gradient(cs_recv)

        # Peer's bytes for our owned half (ghost-masked where chunks missed).
        remote_np = np.frombuffer(state.rx.buffer_view(), dtype=np.uint8)[
            base : base + half_nbytes
        ]
        remote_typed = remote_np.view(flat.numpy().dtype).reshape(own_slice.shape)
        remote_t = torch.from_numpy(remote_typed)

        # Partial sum for own-owned region: own + ghost(peer).  Materialize
        # as new tensor so gloo AllGather doesn't alias rx MR or flat.
        own_partial = own_slice + remote_t

        # Magnitude compensation at ghost-masked chunks.  If every chunk
        # received a CQE, skip the loop entirely (the common fast path).
        n_chunks = cs_recv.size()
        n_missing = n_chunks - cs_recv.num_completed()
        if n_missing > 0 and elem_size > 0:
            # chunk_bytes divides evenly into elements under
            # post_gradient's layout (same invariant as ghost_mask).
            chunk_bytes = state.cfg.chunk_bytes
            elem_per_chunk = chunk_bytes // elem_size
            if elem_per_chunk * elem_size != chunk_bytes:
                raise RuntimeError(
                    f"chunk_bytes={chunk_bytes} not a multiple of "
                    f"elem_size={elem_size}; hybrid magnitude compensation "
                    "requires elem-aligned chunks"
                )
            for i in range(n_chunks):
                if not cs_recv.state(i)["has_cqe"]:
                    el_start = i * elem_per_chunk
                    el_end = min(el_start + elem_per_chunk, half_numel)
                    # own_partial[el] = g_own (peer was zeroed); scale by
                    # world_size so gathered/world_size = g_own keeps the
                    # gradient magnitude instead of shrinking to g_own/2.
                    own_partial[el_start:el_end].mul_(state.world_size)

        state.tx.drain_send_completions()

    # ---------- Phase 2: gloo all-gather (reliable) ----------
    # Default process group is gloo (train_cifar10 initializes dist with
    # backend=gloo even when transport=semirdma_hybrid).  all_gather_into_tensor
    # concatenates rank 0's input at [0:K] and rank 1's at [K:2K].
    gathered = torch.empty_like(flat)
    dist.all_gather_into_tensor(gathered, own_partial.contiguous())

    # Write the averaged result back into flat so DDP sees the aggregated
    # gradient.  Each own_partial[i] = g_own[i] + ghost(g_peer[i]) ≈ g0[i]+g1[i]
    # (sum, not mean), so dividing by world_size gives the true average.
    # (Without this copy, DDP would see the un-modified local gradient —
    # training still converges but each rank steps only on its own data.)
    flat.copy_(gathered.div_(state.world_size))

    logger.debug(
        "semirdma_hybrid_allreduce_hook: bucket=%d numel=%d half_nbytes=%d "
        "n_missing=%d stats=%s",
        bucket_id, numel, half_nbytes, n_missing, stats,
    )
    fut.set_result(flat)
    return fut


__all__ = [
    "SemiRDMAHookState",
    "semirdma_allreduce_hook",
    "semirdma_hybrid_allreduce_hook",
]
