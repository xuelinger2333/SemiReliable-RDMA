"""E0.2 — Pre-BEGIN race correctness microbench.

Forces UC Write-with-Imm chunks to arrive at the receiver **before**
the BEGIN control message that announces the slot lease. The hook's
recv_thread must still finalize every uid correctly: UC completions
sit in the rx CQ until ``wait_for_ratio_clear`` is called, which only
happens after BEGIN installs the lease.

Mechanism: monkey-patch each rank's ``tx.cp.send_begin`` so it sleeps
``begin_delay_ms`` (uniformly sampled 1–10 ms) before the actual
RC SEND goes out. The sender's UC writes proceed immediately after
``send_begin`` returns, so they hit the wire while the peer's BEGIN
is still in the artificial delay.

Pass criterion (per docs/phase5/experiments.md §4.E0):
  - every uid finalizes correctly (no timeout, no crash)
  - PREBEGIN_PENDING drains: every bucket's averaged output equals
    ``(G_a + G_b) / 2`` bit-for-bit

Default: 200 buckets at chunk_bytes=4096, BEGIN delay 1–10 ms uniform.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import socket
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np


@dataclass
class CellResult:
    n_buckets: int
    buckets_per_step: int
    n_floats: int
    chunk_bytes: int
    delay_ms_min: float
    delay_ms_max: float
    n_mismatches: int
    n_chunks_total: int
    false_attribution_rate: float
    wall_time_s: float
    median_iter_ms: float
    p99_iter_ms: float


def _env() -> tuple[str, int]:
    dev = os.environ.get("RDMA_LOOPBACK_DEVICE")
    if not dev:
        raise SystemExit("RDMA_LOOPBACK_DEVICE unset")
    gid = int(os.environ.get("RDMA_LOOPBACK_GID_INDEX", "1"))
    return dev, gid


def _send_uc_first_then_begin(state, *, uid: int, bucket_seq: int,
                                base_offset: int, nbytes: int,
                                chunk_bytes: int, delay_s: float,
                                drain_timeout_ms: int = 5000):
    """Custom send orchestration that inverts the BEGIN/UC order.

    Standard ``clear_send_bucket`` does: acquire → BEGIN → UC writes →
    drain SQ → wait FINALIZE. Here we acquire → UC writes → drain SQ →
    sleep → BEGIN → wait FINALIZE, so UC chunks land in the peer's rx
    buffer BEFORE the peer's lease is installed via on_begin_rx.

    Returns (slot, gen, finalize_decision_or_None, finalize_received).
    """
    from semirdma._semirdma_ext.clear import BeginPayload, encode_imm
    from semirdma.clear.protocol import drain_send_completions
    from semirdma._semirdma_ext.clear import Policy

    transport = state.tx
    n_chunks = (nbytes + chunk_bytes - 1) // chunk_bytes

    r = transport.sender_leases.acquire(uid)
    if not r["ok"]:
        raise RuntimeError("sender_leases.acquire failed")
    slot, gen = r["slot_id"], r["gen"]

    # 1. Post UC writes FIRST.
    for i in range(n_chunks):
        offset = i * chunk_bytes
        length = min(chunk_bytes, nbytes - offset)
        imm = encode_imm(slot_id=slot, chunk_idx=i, gen=gen)
        transport.engine.post_write(
            wr_id=i,
            local_offset=base_offset + offset,
            remote_offset=base_offset + offset,
            length=length,
            remote=transport._peer_data_mr,
            with_imm=True,
            imm_data=imm,
        )
    drain_send_completions(transport.engine, expected=n_chunks,
                           timeout_ms=drain_timeout_ms)

    # 2. Sleep — UC writes are now sitting in peer's rx CQ unprocessed.
    time.sleep(delay_s)

    # 3. Send BEGIN late.
    bp = BeginPayload(
        slot_id=slot, gen=gen, phase_id=0, policy=Policy.MASK_FIRST,
        peer_edge=0, step_seq=state.step_seq, bucket_seq=bucket_seq,
        n_chunks=n_chunks, deadline_us=200_000,
        chunk_bytes=chunk_bytes, checksum_seed=0,
    )
    if not transport.cp.send_begin(uid=uid, payload=bp):
        raise RuntimeError("control_plane.send_begin failed")

    # 4. Wait for FINALIZE via the existing sync object.
    sync = state._get_sync(uid)
    received = sync.finalize_event.wait(timeout=drain_timeout_ms / 1000.0)
    return slot, gen, sync.finalize_decision, received


def run_cell(*, n_buckets: int, buckets_per_step: int,
             n_floats: int, chunk_bytes: int,
             delay_ms_min: float, delay_ms_max: float,
             seed: int) -> CellResult:
    from semirdma.clear.hook import ClearHookState, step_advance
    from semirdma.clear.protocol import clear_recv_bucket
    from semirdma.clear.runtime import apply_finalize as py_apply_finalize
    from semirdma.clear.manifest import uid_hash
    from semirdma.clear.transport import ClearTransportConfig

    dev, gid = _env()
    cfg = ClearTransportConfig(
        dev_name=dev, gid_index=gid,
        buffer_bytes=8 * 1024 * 1024,
        sq_depth=256, rq_depth=2048,
        chunk_bytes=chunk_bytes,
        cp_recv_slots=64, cp_send_slots=16,
    )
    a, b = ClearHookState.for_in_process_pair(cfg)

    delay_rng = random.Random(seed)
    delay_lock = threading.Lock()
    nbytes = n_floats * 4
    n_chunks_per_bucket = (nbytes + chunk_bytes - 1) // chunk_bytes
    grad_rng = np.random.default_rng(seed=seed ^ 0xC1EA9)

    n_mismatches = 0
    iter_ms_samples: list[float] = []
    t0 = time.monotonic()

    def one_rank(state: "ClearHookState", *, bucket_bytes: bytes,
                 uid_send: int, uid_recv: int, bucket_seq: int,
                 delay_s: float, base_offset: int, nbytes: int,
                 holder: dict, label: str, errs: list):
        try:
            # Top up rx recv WRs.
            if state.rx.engine.outstanding_recv() < 2 * n_chunks_per_bucket:
                state.rx.engine.post_recv_batch(
                    n_chunks_per_bucket * 4, base_wr_id=0xC0DE_0000)

            # Stage local bytes.
            tx_buf = np.frombuffer(state.tx.engine.local_buf_view(), dtype=np.uint8)
            tx_buf[base_offset : base_offset + nbytes] = np.frombuffer(
                bucket_bytes, dtype=np.uint8)

            send_err: list = []
            recv_err: list = []
            recv_holder: list = []
            peer_slice_holder: list = []

            def send_thread():
                try:
                    _send_uc_first_then_begin(
                        state, uid=uid_send, bucket_seq=bucket_seq,
                        base_offset=base_offset, nbytes=nbytes,
                        chunk_bytes=chunk_bytes, delay_s=delay_s,
                    )
                except Exception as e:
                    send_err.append(e)

            def recv_thread():
                try:
                    sync = state._get_sync(uid_recv)
                    if not sync.begin_event.wait(timeout=15.0):
                        raise TimeoutError(f"BEGIN not received uid={uid_recv:x}")
                    rr = clear_recv_bucket(
                        state.rx, uid=uid_recv,
                        slot=sync.begin_slot, gen=sync.begin_gen,
                        n_chunks=n_chunks_per_bucket,
                        base_offset=base_offset, chunk_bytes=chunk_bytes,
                        ratio=1.0, timeout_ms=2000,
                    )
                    rx_view = np.frombuffer(state.rx.engine.local_buf_view(),
                                            dtype=np.uint8)
                    peer_slice_holder.append(bytearray(
                        rx_view[base_offset : base_offset + nbytes].tobytes()))
                    recv_holder.append(rr)
                except Exception as e:
                    recv_err.append(e)

            st = threading.Thread(target=send_thread)
            rt = threading.Thread(target=recv_thread)
            st.start(); rt.start()
            st.join(timeout=20); rt.join(timeout=20)
            if send_err: raise send_err[0]
            if recv_err: raise recv_err[0]

            rr = recv_holder[0]
            peer_slice = peer_slice_holder[0]
            py_apply_finalize(rr.decision, mask_bitmap=rr.recv_bitmap,
                              n_chunks=n_chunks_per_bucket,
                              chunk_bytes=chunk_bytes, flat=peer_slice)
            local_arr = np.frombuffer(bucket_bytes, dtype=np.uint8)
            peer_arr = np.frombuffer(bytes(peer_slice), dtype=np.uint8)
            a32 = local_arr.view(np.float32)
            b32 = peer_arr.view(np.float32)
            avg = (a32 + b32) / float(state.world_size)
            holder[label] = avg.astype(np.float32).tobytes()
            state._drop_sync(uid_send)
            state._drop_sync(uid_recv)
        except Exception as e:
            errs.append((label, e))

    try:
        for i in range(n_buckets):
            g_a = grad_rng.standard_normal(n_floats).astype(np.float32)
            g_b = grad_rng.standard_normal(n_floats).astype(np.float32)
            expected = ((g_a + g_b) / 2).astype(np.float32)

            with delay_lock:
                d_a = delay_rng.uniform(delay_ms_min, delay_ms_max) / 1000.0
                d_b = delay_rng.uniform(delay_ms_min, delay_ms_max) / 1000.0

            # Compute uids for this bucket — match _run_clear_bucket's scheme.
            bs = i % buckets_per_step
            uid_a_send = uid_hash(rank_pair=a._rank_pair, step_seq=a.step_seq,
                                  bucket_seq=bs, phase_id=0, peer_edge=0)
            uid_a_recv = uid_hash(rank_pair=a._rank_pair, step_seq=a.step_seq,
                                  bucket_seq=bs, phase_id=0, peer_edge=1)
            uid_b_send = uid_a_recv  # rank 1 sends edge=1
            uid_b_recv = uid_a_send  # rank 1 receives edge=0

            holder: dict = {}
            errs: list = []
            t_iter = time.monotonic()
            ta = threading.Thread(target=one_rank, kwargs=dict(
                state=a, bucket_bytes=g_a.tobytes(),
                uid_send=uid_a_send, uid_recv=uid_a_recv,
                bucket_seq=bs, delay_s=d_a, base_offset=0, nbytes=nbytes,
                holder=holder, label="a", errs=errs))
            tb = threading.Thread(target=one_rank, kwargs=dict(
                state=b, bucket_bytes=g_b.tobytes(),
                uid_send=uid_b_send, uid_recv=uid_b_recv,
                bucket_seq=bs, delay_s=d_b, base_offset=0, nbytes=nbytes,
                holder=holder, label="b", errs=errs))
            ta.start(); tb.start()
            ta.join(timeout=30); tb.join(timeout=30)
            iter_ms_samples.append((time.monotonic() - t_iter) * 1000.0)

            if errs:
                raise RuntimeError(f"bucket {i} thread error: {errs}")

            avg_a = np.frombuffer(holder["a"], dtype=np.float32)
            avg_b = np.frombuffer(holder["b"], dtype=np.float32)
            if not np.array_equal(avg_a, expected):
                n_mismatches += 1
            if not np.array_equal(avg_b, expected):
                n_mismatches += 1
            if not np.array_equal(avg_a, avg_b):
                n_mismatches += 1

            if (i + 1) % buckets_per_step == 0:
                step_advance(a)
                step_advance(b)

            if (i + 1) % 50 == 0:
                rate = (i + 1) / (time.monotonic() - t0)
                print(f"  [E0.2] {i + 1}/{n_buckets} "
                      f"({rate:.1f} bkt/s, mismatches={n_mismatches})",
                      flush=True)
    finally:
        a.shutdown()
        b.shutdown()

    wall = time.monotonic() - t0
    n_total_chunks = n_buckets * n_chunks_per_bucket
    far = n_mismatches / float(n_total_chunks) if n_total_chunks else 0.0
    arr = np.array(iter_ms_samples)
    return CellResult(
        n_buckets=n_buckets, buckets_per_step=buckets_per_step,
        n_floats=n_floats, chunk_bytes=chunk_bytes,
        delay_ms_min=delay_ms_min, delay_ms_max=delay_ms_max,
        n_mismatches=n_mismatches, n_chunks_total=n_total_chunks,
        false_attribution_rate=far, wall_time_s=wall,
        median_iter_ms=float(np.median(arr)) if arr.size else 0.0,
        p99_iter_ms=float(np.percentile(arr, 99)) if arr.size else 0.0,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--buckets", type=int, default=200)
    ap.add_argument("--buckets-per-step", type=int, default=20)
    ap.add_argument("--floats", type=int, default=1024)
    ap.add_argument("--chunk-bytes", type=int, default=4096)
    ap.add_argument("--delay-ms-min", type=float, default=1.0)
    ap.add_argument("--delay-ms-max", type=float, default=10.0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=str, required=True)
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")

    print(f"[E0.2] dev={os.environ.get('RDMA_LOOPBACK_DEVICE')} "
          f"buckets={args.buckets} delay={args.delay_ms_min}-{args.delay_ms_max}ms",
          flush=True)

    res = run_cell(
        n_buckets=args.buckets,
        buckets_per_step=args.buckets_per_step,
        n_floats=args.floats,
        chunk_bytes=args.chunk_bytes,
        delay_ms_min=args.delay_ms_min,
        delay_ms_max=args.delay_ms_max,
        seed=args.seed,
    )

    out_file = out_dir / f"e0_2_prebegin_race_{stamp}.json"
    out_file.write_text(json.dumps({
        "stamp": stamp,
        "device": os.environ.get("RDMA_LOOPBACK_DEVICE"),
        "gid_index": int(os.environ.get("RDMA_LOOPBACK_GID_INDEX", "1")),
        "host": socket.gethostname(),
        "seed": args.seed,
        **asdict(res),
    }, indent=2))

    pass_far = res.false_attribution_rate < 1e-4 and res.n_mismatches == 0
    verdict = "PASS" if pass_far else "FAIL"
    print(f"[E0.2] {verdict}: false_attribution_rate={res.false_attribution_rate:.3e} "
          f"mismatches={res.n_mismatches}/{res.n_chunks_total} "
          f"wall={res.wall_time_s:.1f}s "
          f"iter_ms p50={res.median_iter_ms:.2f} p99={res.p99_iter_ms:.2f}",
          flush=True)
    print(f"[E0.2] wrote {out_file}", flush=True)
    return 0 if pass_far else 1


if __name__ == "__main__":
    raise SystemExit(main())
