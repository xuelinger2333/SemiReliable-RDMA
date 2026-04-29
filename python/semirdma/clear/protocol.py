"""Sender + receiver protocol helpers for one CLEAR bucket transfer.

This is the orchestration layer that sits between the C++ primitives
(ClearTransport / ControlPlane / Finalizer / RatioController) and the
DDP hook (W2.3e). One bucket goes through this protocol exactly once
per direction per step.

Usage (single-direction A → B, both threads share a sync barrier):

    # Sender (A)
    clear_send_bucket(
        tx_clear, uid=uid, slot=slot, gen=gen,
        base_offset=off, remote_base_offset=off, nbytes=B,
        chunk_bytes=4096, peer_data_mr=b_mr,
        finalize_event=fin_evt,
    )

    # Receiver (B)
    cs, decision, recv_bitmap = clear_recv_bucket(
        rx_clear, uid=uid, n_chunks=N, base_offset=off,
        chunk_bytes=4096, ratio=0.95, timeout_ms=200,
        policy=Policy.MASK_FIRST,
    )
    apply_finalize(decision, mask_bitmap=recv_bitmap, n_chunks=N,
                   chunk_bytes=4096, flat=local_buf[off:off+B])

The DDP hook (W2.3e) will instantiate two ClearTransports per rank
(tx + rx) and run both directions in parallel per bucket per step.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional, Tuple

from semirdma._semirdma_ext import ChunkSet
from semirdma._semirdma_ext.clear import (
    BeginPayload,
    FinalizeDecision,
    Policy,
    RetirePayload,
    encode_imm,
)


def chunkset_to_recv_bitmap(cs: ChunkSet) -> bytes:
    """Pack a ChunkSet's per-chunk has_cqe into a bit-packed LSB-first bitmap.

    Bit i is set iff cs.state(i)["has_cqe"] is true. Length = ceil(N/8) bytes.
    """
    n = cs.size()
    out = bytearray((n + 7) // 8)
    for i in range(n):
        if cs.state(i)["has_cqe"]:
            out[i >> 3] |= 1 << (i & 7)
    return bytes(out)


def drain_send_completions(engine, expected: int, *,
                           timeout_ms: int = 1000) -> int:
    """Poll the SQ for `expected` SEND/WRITE completions.

    Required after post_write loops because UC's send completions still
    queue and must be drained before the SQ refills. Returns the number
    of completions actually drained.
    """
    drained = 0
    while drained < expected:
        cs = engine.poll_cq(max_n=64, timeout_ms=timeout_ms)
        if not cs:
            break
        for c in cs:
            # Either SEND, RDMA_WRITE, or WRITE_WITH_IMM completions go
            # to the SQ side. Just count successful + flush completions.
            if c["opcode_name"] in ("RDMA_WRITE", "SEND"):
                drained += 1
    return drained


# ---------------------------------------------------------------------------
# Sender side
# ---------------------------------------------------------------------------


@dataclass
class SendResult:
    """What the sender saw from one CLEAR transfer."""
    uid: int
    slot: int
    gen: int
    n_chunks: int
    n_posted: int
    finalize_decision: Optional[FinalizeDecision] = None
    finalize_received: bool = False


def clear_send_bucket(
    transport,                          # ClearTransport
    *,
    uid: int,
    bucket_seq: int = 0,
    step_seq: int = 0,
    base_offset: int,
    remote_base_offset: int,
    nbytes: int,
    chunk_bytes: int,
    peer_data_mr,                       # RemoteMR
    policy: Policy = Policy.MASK_FIRST,
    deadline_us: int = 200_000,
    drop_chunks: Optional[set] = None,  # for testing: chunk ids to skip posting
    finalize_event: Optional[threading.Event] = None,
    finalize_holder: Optional[list] = None,  # appended to on FINALIZE rx
    drain_timeout_ms: int = 2000,
) -> SendResult:
    """Send one bucket end-to-end via CLEAR.

    Steps:
      1. Acquire (slot, gen) from the sender lease table.
      2. send_begin over the control plane.
      3. Post UC writes-with-imm for every chunk (skipping ``drop_chunks``).
      4. Drain SQ completions.
      5. (Optional) wait for FINALIZE arrival via ``finalize_event`` so
         the caller can mark the slot as RETIRED.

    The peer's recv side (clear_recv_bucket) drives the FINALIZE / RETIRE
    emission. This function returns once the local SQ is drained; the
    finalize_event is only awaited when provided.
    """
    n_chunks = (nbytes + chunk_bytes - 1) // chunk_bytes
    if drop_chunks is None:
        drop_chunks = set()

    # 1. Acquire slot.
    r = transport.sender_leases.acquire(uid)
    if not r["ok"]:
        raise RuntimeError("sender_leases.acquire failed (table full?)")
    slot, gen = r["slot_id"], r["gen"]

    # 2. send_begin.
    bp = BeginPayload(
        slot_id=slot, gen=gen, phase_id=0, policy=policy,
        peer_edge=0, step_seq=step_seq, bucket_seq=bucket_seq,
        n_chunks=n_chunks, deadline_us=deadline_us,
        chunk_bytes=chunk_bytes, checksum_seed=0,
    )
    if not transport.cp.send_begin(uid=uid, payload=bp):
        raise RuntimeError("control_plane.send_begin failed (ring full?)")

    # 3. Post UC writes-with-imm.
    n_posted = 0
    for i in range(n_chunks):
        if i in drop_chunks:
            continue
        offset = i * chunk_bytes
        length = min(chunk_bytes, nbytes - offset)
        imm = encode_imm(slot_id=slot, chunk_idx=i, gen=gen)
        transport.engine.post_write(
            wr_id=i,
            local_offset=base_offset + offset,
            remote_offset=remote_base_offset + offset,
            length=length,
            remote=peer_data_mr,
            with_imm=True,
            imm_data=imm,
        )
        n_posted += 1

    # 4. Drain SQ.
    drain_send_completions(transport.engine, expected=n_posted,
                           timeout_ms=drain_timeout_ms)

    # 5. Optional: wait for FINALIZE.
    finalize_decision = None
    finalize_received = False
    if finalize_event is not None:
        finalize_received = finalize_event.wait(timeout=drain_timeout_ms / 1000.0)
        if finalize_received and finalize_holder:
            finalize_decision = finalize_holder[0]
        # Caller is responsible for releasing the slot after RETIRE.
    else:
        # No sync requested — release immediately so the lease table doesn't
        # stay full in tests that don't bother with FINALIZE plumbing.
        transport.sender_leases.release(uid)

    return SendResult(
        uid=uid, slot=slot, gen=gen,
        n_chunks=n_chunks, n_posted=n_posted,
        finalize_decision=finalize_decision,
        finalize_received=finalize_received,
    )


# ---------------------------------------------------------------------------
# Receiver side
# ---------------------------------------------------------------------------


@dataclass
class RecvResult:
    """What the receiver saw from one CLEAR transfer."""
    uid: int
    n_chunks: int
    recv_count: int
    decision: FinalizeDecision
    recv_bitmap: bytes
    timed_out: bool


def clear_recv_bucket(
    transport,                       # ClearTransport
    *,
    uid: int,
    slot: int,
    gen: int,
    n_chunks: int,
    base_offset: int,
    chunk_bytes: int,
    ratio: float = 0.95,
    timeout_ms: int = 200,
    policy: Policy = Policy.MASK_FIRST,
) -> RecvResult:
    """Receive one bucket end-to-end via CLEAR.

    Caller must have:
      - pre-posted enough recv WRs for this bucket's chunks,
      - already installed the receiver lease (typically via the
        ControlPlane.on_begin handler the hook installs).

    Steps:
      1. wait_for_ratio_clear on the local RatioController (drives UC
         polling, marks completed chunks on the ChunkSet).
      2. Build recv_bitmap from the ChunkSet's per-chunk has_cqe.
      3. finalizer.on_witness(uid, recv_bitmap) — triggers send_finalize
         + send_retire + apply_mask via the wired callbacks.
      4. Return the decision + bitmap so the caller can apply it locally
         (or trust the apply_mask callback already did).
    """
    cs = ChunkSet(base_offset, n_chunks * chunk_bytes, chunk_bytes)

    # 1. Track in finalizer (caller may have already done this via on_begin
    # but track() is idempotent-friendly: we tolerate False if already set).
    transport.finalizer.track(uid=uid, slot=slot, gen=gen,
                              n_chunks=n_chunks, chunk_bytes=chunk_bytes,
                              policy=policy)

    # 2. Drive UC polling.
    stats = transport.ratio.wait_for_ratio_clear(
        cs, ratio=ratio, timeout_ms=timeout_ms,
        slot_id=slot, gen=gen,
    )

    # 3. Build recv_bitmap from cs.
    recv_bitmap = chunkset_to_recv_bitmap(cs)

    # 4. Hand off to finalizer.
    decision = transport.finalizer.on_witness(uid=uid, recv_bitmap=recv_bitmap)

    return RecvResult(
        uid=uid,
        n_chunks=n_chunks,
        recv_count=stats["completed"],
        decision=decision,
        recv_bitmap=recv_bitmap,
        timed_out=bool(stats["timed_out"]),
    )


__all__ = [
    "RecvResult",
    "SendResult",
    "chunkset_to_recv_bitmap",
    "clear_recv_bucket",
    "clear_send_bucket",
    "drain_send_completions",
]
