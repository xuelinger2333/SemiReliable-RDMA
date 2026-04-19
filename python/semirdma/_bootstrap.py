"""Plain-TCP out-of-band exchange for UC QP bring-up parameters.

During ``UCQPEngine.bring_up`` both sides need the *remote* (qpn, gid) and the
*remote* MR (addr, rkey).  These are tiny fixed-size blobs — 24 bytes for the
QP info and 12 bytes for the MR info — so we skip the RDMA CM / rdma_bind
dance and just hand the bytes over a short-lived TCP socket.

The protocol is symmetric and brutally simple:

  - rank 0 listens on ``port``, rank 1 connects to ``peer_host:port``
  - both sides send their own (info || mr) blob, then read the peer's blob
  - the socket is closed; the caller proceeds to ``bring_up``

We deliberately keep this in pure Python rather than cramming it into the
pybind11 module (design-doc §3.3 decision 2) so the C++ transport stays
free of networking concerns and the bootstrap can be unit-tested without a
real RDMA device.
"""

from __future__ import annotations

import logging
import socket
import struct
from typing import Optional, Tuple

from semirdma._semirdma_ext import RemoteMR, RemoteQpInfo

logger = logging.getLogger(__name__)


# QP info wire format: big-endian qpn (u32) + gid (16 raw bytes) = 20 bytes.
# MR info wire format: big-endian addr (u64) + rkey (u32)        = 12 bytes.
# Total per-side blob                                            = 32 bytes.
_QP_FMT = "!I16s"
_MR_FMT = "!QI"
_QP_LEN = struct.calcsize(_QP_FMT)   # 20
_MR_LEN = struct.calcsize(_MR_FMT)   # 12
_BLOB_LEN = _QP_LEN + _MR_LEN        # 32


def _pack(local_qp: RemoteQpInfo, local_mr: RemoteMR) -> bytes:
    return struct.pack(_QP_FMT, local_qp.qpn, local_qp.gid) + struct.pack(
        _MR_FMT, local_mr.addr, local_mr.rkey
    )


def _unpack(blob: bytes) -> Tuple[RemoteQpInfo, RemoteMR]:
    if len(blob) != _BLOB_LEN:
        raise RuntimeError(
            f"bootstrap: expected {_BLOB_LEN} bytes, got {len(blob)}"
        )
    qpn, gid = struct.unpack(_QP_FMT, blob[:_QP_LEN])
    addr, rkey = struct.unpack(_MR_FMT, blob[_QP_LEN:])
    return RemoteQpInfo(qpn=qpn, gid=gid), RemoteMR(addr=addr, rkey=rkey)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly ``n`` bytes from ``sock`` or raise.  Guards against short
    reads on loopback (rare but legal)."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise RuntimeError(
                f"bootstrap: peer closed after {len(buf)}/{n} bytes"
            )
        buf.extend(chunk)
    return bytes(buf)


def exchange_qp_info(
    is_server: bool,
    host: str,
    port: int,
    local_qp: RemoteQpInfo,
    local_mr: RemoteMR,
    *,
    connect_timeout_s: float = 30.0,
) -> Tuple[RemoteQpInfo, RemoteMR]:
    """Exchange one (QP info, MR info) pair with the peer over TCP.

    Args:
        is_server: True for the listening side (typically rank 0).
        host: server side: bind address; client side: peer address.
        port: TCP port (must match on both sides).
        local_qp: our ``engine.local_qp_info()``.
        local_mr: our ``engine.local_mr_info()``.
        connect_timeout_s: how long the client will retry ECONNREFUSED while
            the server starts up.  Server-side accept() uses this as its
            ``settimeout``.

    Returns:
        ``(remote_qp, remote_mr)`` as freshly constructed pybind11 objects.
    """
    blob_out = _pack(local_qp, local_mr)
    assert len(blob_out) == _BLOB_LEN

    if is_server:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((host, port))
            srv.listen(1)
            srv.settimeout(connect_timeout_s)
            logger.info("bootstrap: listening on %s:%d", host, port)
            conn, peer_addr = srv.accept()
        finally:
            srv.close()
        with conn:
            logger.info("bootstrap: peer %s connected", peer_addr)
            conn.sendall(blob_out)
            blob_in = _recv_exact(conn, _BLOB_LEN)
    else:
        deadline = _clock_monotonic() + connect_timeout_s
        last_err: Optional[BaseException] = None
        while True:
            try:
                cli = socket.create_connection((host, port), timeout=connect_timeout_s)
                break
            except (ConnectionRefusedError, OSError) as e:
                last_err = e
                if _clock_monotonic() >= deadline:
                    raise RuntimeError(
                        f"bootstrap: cannot reach {host}:{port} after "
                        f"{connect_timeout_s}s ({e})"
                    ) from e
                _sleep(0.2)
        with cli:
            logger.info("bootstrap: connected to %s:%d", host, port)
            cli.sendall(blob_out)
            blob_in = _recv_exact(cli, _BLOB_LEN)
        _ = last_err  # silence "assigned-but-unused" when first try succeeds

    return _unpack(blob_in)


# Indirection so tests can monkeypatch time without touching the import.
def _clock_monotonic() -> float:
    import time
    return time.monotonic()


def _sleep(seconds: float) -> None:
    import time
    time.sleep(seconds)


__all__ = ["exchange_qp_info"]
