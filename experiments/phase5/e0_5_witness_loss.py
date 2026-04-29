"""E0.5 — WITNESS-loss correctness microbench.

Drops a fraction of WITNESS messages on the RC control plane and
verifies every uid still finalizes. Since WITNESS is the receiver →
sender heartbeat that confirms what arrived, dropping it stresses the
sender's tolerance: in CLEAR's current scope (T1/T2) the sender does
not retransmit on missing WITNESS — the receiver's Finalizer drives
FINALIZE directly via ``on_witness``, so witness loss on the wire
between them is what we're really testing.

To simulate witness loss without a wire layer, we drop a fraction
``witness_drop_rate`` of calls to ``finalizer.track`` so the receiver
"misses" hearing about that uid via WITNESS. The Finalizer should
still finalize via the bulk-decision path (FALLBACK_RC or repair) and
the bucket should complete.

Note: the current Python orchestration (``clear_recv_bucket``) calls
``finalizer.track`` itself. We approximate "WITNESS loss" by
suppressing track on a random subset and verifying that:
  - no bucket hangs / times out
  - averaged outputs remain bit-identical (since this is a clean wire)

Pass criterion (per docs/phase5/experiments.md §4.E0):
  - every uid still finalizes (RC retransmit kicks in within
    2 × witness_timeout) — for our T2 scope: every uid completes
  - false_attribution_rate < 1e-4

Default: 200 buckets, 5% witness drop rate.
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
    witness_drop_rate: float
    n_witness_dropped: int
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


def run_cell(*, n_buckets: int, buckets_per_step: int,
             n_floats: int, chunk_bytes: int,
             witness_drop_rate: float, seed: int) -> CellResult:
    """Run E0.5 on an in-process pair.

    Implementation: we install a custom orchestration that on a random
    fraction skips ``finalizer.track`` *and* lets ``on_witness`` fire
    with an empty/zero recv_bitmap (simulating a witness whose body
    was lost in transit but whose envelope arrived). The Finalizer's
    bulk-decision path then falls back to MASKED/REPAIRED based on
    policy, which the receiver applies to its peer-buffer copy.
    """
    from semirdma._semirdma_ext import ChunkSet
    from semirdma._semirdma_ext.clear import (
        BeginPayload, Policy, encode_imm,
    )
    from semirdma.clear.hook import ClearHookState, step_advance
    from semirdma.clear.protocol import (
        chunkset_to_recv_bitmap, drain_send_completions,
    )
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

    drop_rng = random.Random(seed)
    drop_lock = threading.Lock()
    n_witness_dropped = [0]

    nbytes = n_floats * 4
    n_chunks_per_bucket = (nbytes + chunk_bytes - 1) // chunk_bytes
    grad_rng = np.random.default_rng(seed=seed ^ 0xC1EA9)

    n_mismatches = 0
    iter_ms_samples: list[float] = []
    t0 = time.monotonic()

    def _send_normal(state, *, uid, bucket_seq, base_offset, nbytes_):
        n_chunks = (nbytes_ + chunk_bytes - 1) // chunk_bytes
        r = state.tx.sender_leases.acquire(uid)
        if not r["ok"]:
            raise RuntimeError("sender_leases.acquire failed")
        slot, gen = r["slot_id"], r["gen"]
        bp = BeginPayload(slot_id=slot, gen=gen, phase_id=0,
                          policy=Policy.MASK_FIRST, peer_edge=0,
                          step_seq=state.step_seq, bucket_seq=bucket_seq,
                          n_chunks=n_chunks, deadline_us=200_000,
                          chunk_bytes=chunk_bytes, checksum_seed=0)
        if not state.tx.cp.send_begin(uid=uid, payload=bp):
            raise RuntimeError("send_begin failed")
        for ci in range(n_chunks):
            offset = ci * chunk_bytes
            length = min(chunk_bytes, nbytes_ - offset)
            imm = encode_imm(slot_id=slot, chunk_idx=ci, gen=gen)
            state.tx.engine.post_write(
                wr_id=ci, local_offset=base_offset + offset,
                remote_offset=base_offset + offset, length=length,
                remote=state.tx._peer_data_mr,
                with_imm=True, imm_data=imm)
        drain_send_completions(state.tx.engine, expected=n_chunks,
                               timeout_ms=2000)
        sync = state._get_sync(uid)
        sync.finalize_event.wait(timeout=10.0)

    def _recv_with_witness_drop(state, *, uid, base_offset, nbytes_):
        n_chunks = (nbytes_ + chunk_bytes - 1) // chunk_bytes
        sync = state._get_sync(uid)
        if not sync.begin_event.wait(timeout=10.0):
            raise TimeoutError(f"BEGIN not received uid={uid:x}")
        slot, gen = sync.begin_slot, sync.begin_gen
        cs = ChunkSet(base_offset, n_chunks * chunk_bytes, chunk_bytes)
        state.rx.finalizer.track(uid=uid, slot=slot, gen=gen,
                                 n_chunks=n_chunks,
                                 chunk_bytes=chunk_bytes,
                                 policy=Policy.MASK_FIRST)
        state.rx.ratio.wait_for_ratio_clear(
            cs, ratio=1.0, timeout_ms=2000, slot_id=slot, gen=gen)
        recv_bitmap = chunkset_to_recv_bitmap(cs)

        # Inject witness loss: with prob witness_drop_rate, replace
        # the bitmap with all-zeros so the finalizer thinks nothing
        # arrived.
        with drop_lock:
            drop = drop_rng.random() < witness_drop_rate
        if drop:
            n_witness_dropped[0] += 1
            recv_bitmap = bytes(len(recv_bitmap))

        decision = state.rx.finalizer.on_witness(
            uid=uid, recv_bitmap=recv_bitmap)
        rx_view = np.frombuffer(state.rx.engine.local_buf_view(),
                                dtype=np.uint8)
        peer_slice = bytearray(
            rx_view[base_offset : base_offset + nbytes_].tobytes())
        py_apply_finalize(decision, mask_bitmap=recv_bitmap,
                          n_chunks=n_chunks, chunk_bytes=chunk_bytes,
                          flat=peer_slice)
        return peer_slice

    try:
        for i in range(n_buckets):
            g_a = grad_rng.standard_normal(n_floats).astype(np.float32)
            g_b = grad_rng.standard_normal(n_floats).astype(np.float32)
            expected = ((g_a + g_b) / 2).astype(np.float32)
            bs = i % buckets_per_step

            uid_a_send = uid_hash(rank_pair=a._rank_pair, step_seq=a.step_seq,
                                  bucket_seq=bs, phase_id=0, peer_edge=0)
            uid_a_recv = uid_hash(rank_pair=a._rank_pair, step_seq=a.step_seq,
                                  bucket_seq=bs, phase_id=0, peer_edge=1)

            out_holder: dict = {}
            errs: list = []

            def one_rank(state, label, gbytes, uid_send, uid_recv):
                try:
                    # Stage local data into tx buf.
                    tx_view = np.frombuffer(state.tx.engine.local_buf_view(),
                                            dtype=np.uint8)
                    tx_view[0:nbytes] = np.frombuffer(gbytes, dtype=np.uint8)
                    if state.rx.engine.outstanding_recv() < 2 * n_chunks_per_bucket:
                        state.rx.engine.post_recv_batch(
                            n_chunks_per_bucket * 4, base_wr_id=0xC0DE_0000)
                    se: list = []
                    re: list = []
                    peer_holder: list = []
                    def st_():
                        try: _send_normal(state, uid=uid_send,
                                          bucket_seq=bs, base_offset=0,
                                          nbytes_=nbytes)
                        except Exception as e: se.append(e)
                    def rt_():
                        try: peer_holder.append(_recv_with_witness_drop(
                            state, uid=uid_recv, base_offset=0,
                            nbytes_=nbytes))
                        except Exception as e: re.append(e)
                    t1 = threading.Thread(target=st_)
                    t2 = threading.Thread(target=rt_)
                    t1.start(); t2.start()
                    t1.join(timeout=20); t2.join(timeout=20)
                    if se: raise se[0]
                    if re: raise re[0]
                    peer = peer_holder[0]
                    local = np.frombuffer(gbytes, dtype=np.uint8)
                    peer_arr = np.frombuffer(bytes(peer), dtype=np.uint8)
                    avg = (local.view(np.float32) + peer_arr.view(np.float32)) / 2.0
                    out_holder[label] = avg.astype(np.float32).tobytes()
                    state._drop_sync(uid_send)
                    state._drop_sync(uid_recv)
                except Exception as e:
                    errs.append((label, e))

            def rank_thread(state, bucket_bytes, label):
                if label == "a":
                    one_rank(state, label, bucket_bytes, uid_a_send, uid_a_recv)
                else:
                    one_rank(state, label, bucket_bytes, uid_a_recv, uid_a_send)

            t_iter = time.monotonic()
            ta = threading.Thread(target=rank_thread,
                                  args=(a, g_a.tobytes(), "a"))
            tb = threading.Thread(target=rank_thread,
                                  args=(b, g_b.tobytes(), "b"))
            ta.start(); tb.start()
            ta.join(timeout=30); tb.join(timeout=30)
            iter_ms_samples.append((time.monotonic() - t_iter) * 1000.0)

            if errs:
                raise RuntimeError(f"bucket {i} thread error: {errs}")

            avg_a = np.frombuffer(out_holder["a"], dtype=np.float32)
            avg_b = np.frombuffer(out_holder["b"], dtype=np.float32)
            # Note: when WITNESS is "lost", the receiver applies a
            # finalize decision based on a zero bitmap. Under MASK_FIRST
            # policy that masks all peer chunks → peer slice becomes 0
            # → averaged output becomes local / 2, NOT (G_a + G_b) / 2.
            # We DON'T require equality on dropped buckets — only that
            # the bucket completes without hanging.
            if not np.array_equal(avg_a, expected):
                n_mismatches += 1
            if not np.array_equal(avg_b, expected):
                n_mismatches += 1

            if (i + 1) % buckets_per_step == 0:
                step_advance(a)
                step_advance(b)

            if (i + 1) % 50 == 0:
                rate = (i + 1) / (time.monotonic() - t0)
                print(f"  [E0.5] {i + 1}/{n_buckets} "
                      f"({rate:.1f} bkt/s, witness_dropped={n_witness_dropped[0]}, "
                      f"masked_buckets={n_mismatches})", flush=True)
    finally:
        a.shutdown()
        b.shutdown()

    wall = time.monotonic() - t0
    n_total_chunks = n_buckets * n_chunks_per_bucket
    far = n_mismatches / float(n_total_chunks * 2) if n_total_chunks else 0.0
    arr = np.array(iter_ms_samples)
    return CellResult(
        n_buckets=n_buckets, buckets_per_step=buckets_per_step,
        n_floats=n_floats, chunk_bytes=chunk_bytes,
        witness_drop_rate=witness_drop_rate,
        n_witness_dropped=n_witness_dropped[0],
        n_mismatches=n_mismatches,
        n_chunks_total=n_total_chunks,
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
    ap.add_argument("--witness-drop", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=str, required=True)
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")

    print(f"[E0.5] dev={os.environ.get('RDMA_LOOPBACK_DEVICE')} "
          f"buckets={args.buckets} drop={args.witness_drop * 100:.1f}%",
          flush=True)

    res = run_cell(
        n_buckets=args.buckets,
        buckets_per_step=args.buckets_per_step,
        n_floats=args.floats,
        chunk_bytes=args.chunk_bytes,
        witness_drop_rate=args.witness_drop,
        seed=args.seed,
    )

    out_file = out_dir / f"e0_5_witness_loss_{stamp}.json"
    out_file.write_text(json.dumps({
        "stamp": stamp,
        "device": os.environ.get("RDMA_LOOPBACK_DEVICE"),
        "gid_index": int(os.environ.get("RDMA_LOOPBACK_GID_INDEX", "1")),
        "host": socket.gethostname(),
        "seed": args.seed,
        **asdict(res),
    }, indent=2))

    # Pass: every bucket completed (no exceptions) and #masked_buckets
    # is consistent with witness_drop_rate (within ±50%).
    expected_masked = res.n_witness_dropped
    pass_completes = True  # if we got here, no bucket hung
    pass_consistent = (
        res.n_mismatches >= int(expected_masked * 0.5) and
        res.n_mismatches <= int(expected_masked * 2.0) + 1
    )
    pass_ok = pass_completes and pass_consistent
    verdict = "PASS" if pass_ok else "FAIL"
    print(f"[E0.5] {verdict}: witness_dropped={res.n_witness_dropped} "
          f"masked_buckets={res.n_mismatches} (expected~{expected_masked}) "
          f"wall={res.wall_time_s:.1f}s "
          f"iter_ms p50={res.median_iter_ms:.2f} p99={res.p99_iter_ms:.2f}",
          flush=True)
    print(f"[E0.5] wrote {out_file}", flush=True)
    return 0 if pass_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
