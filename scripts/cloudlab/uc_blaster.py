#!/usr/bin/env python3
"""uc_blaster.py — UC RDMA Write-with-immediate blaster for Phase 4 lossy
calibration.

Purpose
-------
Real training on CPU + ResNet-18 only offers ~0.3 Gbps of AllReduce traffic,
so even at 1 G hammer the switch egress queue never overflows and the P1
matrix sees 0 real wire drops.  This blaster generates **line-rate UC
Write-with-immediate** traffic so that hammer + blaster >> 25 Gbps egress,
forcing real switch drops.  Receiver counts delivered chunks (one per
successful RECV_RDMA_WITH_IMM completion); client reports posted count.
The difference is the true UC drop rate on the wire.

Design
------
Two-node, client/server, over the cluster SSH mesh established in
scripts/cloudlab/hammer_validate.sh.  TCP control socket exchanges
(qpn, gid, remote-buf addr, rkey) then signals START/STOP; data plane
is UC QP Write-with-immediate on the registered MR.

Usage
-----
Server (on amd203, the experiment receiver):

    python scripts/cloudlab/uc_blaster.py server \\
        --dev mlx5_2 --port 31111 --duration 30

Client (on amd196, the experiment sender):

    python scripts/cloudlab/uc_blaster.py client --peer 10.10.1.1 \\
        --dev mlx5_2 --port 31111 --duration 30 --chunk-bytes 16384

Both sides print a one-line summary at exit.
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time

from semirdma._semirdma_ext import UCQPEngine, RemoteQpInfo, RemoteMR

# TCP exchange wire format (fixed 32 B):
#   qpn   uint32    4 B
#   gid   16B       16 B
#   addr  uint64    8 B
#   rkey  uint32    4 B
EXCHANGE_FMT = "<I16sQI"
EXCHANGE_LEN = struct.calcsize(EXCHANGE_FMT)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError(f"peer closed mid-read ({len(buf)}/{n} B)")
        buf += chunk
    return buf


def exchange(sock: socket.socket, qpn: int, gid: bytes, addr: int, rkey: int):
    sock.sendall(struct.pack(EXCHANGE_FMT, qpn, gid, addr, rkey))
    return struct.unpack(EXCHANGE_FMT, _recv_exact(sock, EXCHANGE_LEN))


# ---------------------------------------------------------------------------


def run_server(args) -> int:
    # MR need only hold a single chunk — every incoming Write targets
    # remote_offset=0, so the buffer is overwritten continuously.  But RQ
    # depth must be *large* enough to absorb bursts at line rate.
    buf_bytes = max(args.chunk_bytes * 64, 4 * 1024 * 1024)
    rq_depth = args.rq_depth

    engine = UCQPEngine(args.dev, buf_bytes, 16, rq_depth)
    print(f"[server] dev={args.dev} rq_depth={rq_depth} buf={buf_bytes}B "
          f"qpn={engine.qpn}", flush=True)

    # Pre-post the full RQ.  We'll re-post in the poll loop to keep it full.
    engine.post_recv_batch(rq_depth, 0)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", args.port))
    srv.listen(1)
    print(f"[server] tcp listen :{args.port}", flush=True)

    conn, addr = srv.accept()
    print(f"[server] peer={addr}", flush=True)

    local_qp = engine.local_qp_info()
    local_mr = engine.local_mr_info()
    peer_qpn, peer_gid, _peer_addr, _peer_rkey = exchange(
        conn, local_qp.qpn, local_qp.gid, local_mr.addr, local_mr.rkey
    )
    engine.bring_up(RemoteQpInfo(peer_qpn, peer_gid))
    print(f"[server] QP up  peer_qpn={peer_qpn}", flush=True)

    conn.sendall(b"READY")
    if _recv_exact(conn, 5) != b"START":
        print("[server] expected START", file=sys.stderr)
        return 2

    t0 = time.time()
    # +2 s gives the last client-inflight chunks time to land on the wire
    # after the client's sending loop exits.
    deadline = t0 + args.duration + 2

    n_recv = 0
    n_error = 0
    err_samples = []
    refill_threshold = rq_depth // 2

    while time.time() < deadline:
        cqes = engine.poll_cq(256, 10)  # 10 ms block; wakes up on activity
        for c in cqes:
            # Successful incoming Write-with-immediate → count.  pybind
            # opcode_name returns the IBV_WC_ *tail* (e.g. "RECV_RDMA_WITH_IMM",
            # not the full symbol) — see src/bindings/py_semirdma.cpp opcode_name().
            if c["opcode_name"] == "RECV_RDMA_WITH_IMM" and c["status"] == 0:
                n_recv += 1
            else:
                n_error += 1
                if len(err_samples) < 5:
                    err_samples.append(
                        f"op={c['opcode_name']}({c['opcode']}) "
                        f"status={c['status_name']}({c['status']})"
                    )
        # Top up RQ — each consumed WR must be refunded or the NIC will
        # drop future Writes (UC still needs a posted Recv WR for the
        # with-imm variant because the immediate is consumed like a RECV).
        while engine.outstanding_recv() < refill_threshold:
            engine.post_recv(0)

    # Grab client's sent total.
    raw = _recv_exact(conn, 8)
    n_sent = struct.unpack("<Q", raw)[0]

    elapsed = time.time() - t0
    n_missing = max(0, n_sent - n_recv)
    drop_pct = 100.0 * n_missing / n_sent if n_sent > 0 else 0.0
    gbps = n_recv * args.chunk_bytes * 8 / elapsed / 1e9

    # Echo result back to client so its log shows the same numbers.
    summary = (f"elapsed={elapsed:.2f}s sent={n_sent} recv={n_recv} "
               f"missing={n_missing} drop={drop_pct:.4f}% gbps={gbps:.2f} "
               f"err={n_error}")
    conn.sendall(summary.encode() + b"\n")
    print(f"[server] {summary}", flush=True)
    if err_samples:
        print(f"[server] first {len(err_samples)} error CQEs:", flush=True)
        for s in err_samples:
            print(f"[server]   {s}", flush=True)
    conn.close()
    srv.close()
    return 0


# ---------------------------------------------------------------------------


def run_client(args) -> int:
    buf_bytes = max(args.chunk_bytes, 64 * 1024)
    sq_depth = args.sq_depth

    engine = UCQPEngine(args.dev, buf_bytes, sq_depth, 16)
    print(f"[client] dev={args.dev} sq_depth={sq_depth} chunk={args.chunk_bytes}B "
          f"qpn={engine.qpn}", flush=True)

    # Fill the first chunk of the MR with a test pattern (content is
    # irrelevant for drop counting; non-zero makes tcpdump easier).
    mv = engine.local_buf_view()
    pattern = bytes([0x42] * args.chunk_bytes)
    mv[: args.chunk_bytes] = pattern

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((args.peer, args.port))

    local_qp = engine.local_qp_info()
    local_mr = engine.local_mr_info()
    peer_qpn, peer_gid, peer_addr, peer_rkey = exchange(
        sock, local_qp.qpn, local_qp.gid, local_mr.addr, local_mr.rkey
    )
    remote = RemoteMR(peer_addr, peer_rkey)

    engine.bring_up(RemoteQpInfo(peer_qpn, peer_gid))
    print(f"[client] QP up  peer_qpn={peer_qpn} peer_addr=0x{peer_addr:x} "
          f"rkey=0x{peer_rkey:x}", flush=True)

    if _recv_exact(sock, 5) != b"READY":
        print("[client] expected READY", file=sys.stderr)
        return 2
    sock.sendall(b"START")

    t0 = time.time()
    t_stop = t0 + args.duration

    n_sent = 0
    n_sq = 0
    n_post_fail = 0
    imm = 0

    # Rate-cap pacing: every BATCH posts, sleep to hold offered rate at
    # --rate-cap-gbps.  With 0 cap, the check is a no-op.  Purpose: diagnose
    # whether blaster-side UC "loss" is a NIC/switch property or a Python
    # RQ-refill bottleneck on the receiver (Phase 4 causality split).
    BATCH = 1024
    bytes_per_batch = BATCH * args.chunk_bytes
    target_batch_sec = (bytes_per_batch * 8 / (args.rate_cap_gbps * 1e9)
                        if args.rate_cap_gbps > 0 else 0.0)
    t_batch_start = time.perf_counter()
    posts_this_batch = 0

    # Main blast loop.  We keep SQ full; when post_write fails due to queue
    # overrun, drain CQEs for send completions and retry.
    while time.time() < t_stop:
        # Fill SQ.
        while n_sq < sq_depth:
            try:
                engine.post_write(
                    wr_id=imm,
                    local_offset=0,
                    remote_offset=0,
                    length=args.chunk_bytes,
                    remote=remote,
                    with_imm=True,
                    imm_data=imm & 0xFFFFFFFF,
                )
            except Exception:
                n_post_fail += 1
                break
            imm += 1
            n_sq += 1
            n_sent += 1
            posts_this_batch += 1
        # Free SQ slots by polling send completions.
        cqes = engine.poll_cq(256, 0)
        n_sq -= len(cqes)
        if n_sq < 0:
            n_sq = 0
        # Pace for --rate-cap-gbps: every BATCH posts, sleep to the target
        # batch duration.  Coarse but adequate — 1024 × 16 KB = 16 MB per
        # batch, at 1 Gbps that's 128 ms, plenty of sleep granularity.
        if target_batch_sec > 0 and posts_this_batch >= BATCH:
            now = time.perf_counter()
            elapsed = now - t_batch_start
            if elapsed < target_batch_sec:
                time.sleep(target_batch_sec - elapsed)
            t_batch_start = time.perf_counter()
            posts_this_batch = 0

    # Drain remaining inflight sends before reporting totals.
    drain_deadline = time.time() + 2.0
    while n_sq > 0 and time.time() < drain_deadline:
        cqes = engine.poll_cq(256, 100)
        n_sq -= len(cqes)

    elapsed = time.time() - t0
    gbps = n_sent * args.chunk_bytes * 8 / elapsed / 1e9
    local_rate_msg = (f"[client] local: elapsed={elapsed:.2f}s sent={n_sent} "
                      f"offered={gbps:.2f}Gbps post_fail={n_post_fail}")
    print(local_rate_msg, flush=True)

    sock.sendall(struct.pack("<Q", n_sent))
    summary = sock.recv(1024).decode().rstrip()
    print(f"[client] server: {summary}", flush=True)
    sock.close()
    return 0


# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="role", required=True)

    s = sub.add_parser("server")
    s.add_argument("--dev", required=True)
    s.add_argument("--port", type=int, default=31111)
    s.add_argument("--duration", type=float, default=30.0)
    s.add_argument("--chunk-bytes", type=int, default=16384)
    s.add_argument("--rq-depth", type=int, default=4096)

    c = sub.add_parser("client")
    c.add_argument("--peer", required=True)
    c.add_argument("--dev", required=True)
    c.add_argument("--port", type=int, default=31111)
    c.add_argument("--duration", type=float, default=30.0)
    c.add_argument("--chunk-bytes", type=int, default=16384)
    c.add_argument("--sq-depth", type=int, default=512)
    c.add_argument("--rate-cap-gbps", type=float, default=0.0,
                   help="Cap offered rate (Gbps). 0 = uncapped (blast as fast as SQ allows). "
                        "Used by Phase 4 calibration to separate NIC hardware loss from "
                        "Python RQ-refill bottleneck.")

    args = p.parse_args()
    if args.role == "server":
        return run_server(args)
    else:
        return run_client(args)


if __name__ == "__main__":
    sys.exit(main())
