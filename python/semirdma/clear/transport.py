"""ClearTransport — Python skeleton composing the W1.x C++ pieces.

Phase 5 W2.3c skeleton: provides a single class that owns one peer's
worth of CLEAR machinery (UC data plane + RC control plane + lease
tables + finalizer + RQ monitor) and exposes the high-level methods
``clear_allreduce_hook`` will call. The hook itself lands in W2.3d
since its DDP integration involves bidirectional state-machine
orchestration (warm-up RC pass, manifest build, per-bucket BEGIN→post
UC→ratio_clear→on_witness→FINALIZE→mask cycle).

Threading: not thread-safe by itself. Hook layer serializes via a lock.

Construction does not yet wire two transports against each other —
that's a `bring_up()` step the caller drives after exchanging QP info
via the existing TCP bootstrap (or a programmatic exchange in tests).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple

from semirdma._semirdma_ext import (
    RatioController,
    RemoteMR,
    RemoteQpInfo,
    UCQPEngine,
)
from semirdma._semirdma_ext.clear import (
    ControlPlane,
    ControlPlaneConfig,
    Finalizer,
    FinalizerConfig,
    Policy,
    ReceiverLeaseTable,
    RQMonitor,
    RQMonitorConfig,
    SenderLeaseTable,
)


@dataclass
class ClearTransportConfig:
    """All knobs needed to bring up one ClearTransport instance."""

    dev_name: str
    gid_index: int = 1               # 1 = RoCEv2 link-local (CX-5 default)
    buffer_bytes: int = 64 * 1024 * 1024
    sq_depth: int = 256
    rq_depth: int = 4096
    chunk_bytes: int = 4096
    # Control plane
    cp_recv_slots: int = 64
    cp_send_slots: int = 16
    # Repair budget per training step (T2 must-ship default).
    repair_budget_bytes_per_step: int = 16 * 1024 * 1024
    max_repair_bytes_per_uid: int = 0  # 0 = no per-uid cap
    # Quarantine (logical ticks) on slot recycle in the sender lease table.
    quarantine_ticks: int = 1
    # RQ monitor knobs
    rq_low_watermark: int = 64
    rq_refill_target: int = 256
    rq_initial_credits: int = 256


class ClearTransport:
    """Owns one peer pair's worth of CLEAR machinery.

    The data plane (UC QP via UCQPEngine) and control plane (RC QP via
    ControlPlane) are independent — both must be brought up before
    transfers can flow. After construction the caller publishes
    ``local_qp_info()`` / ``local_mr_info()`` for both planes via TCP
    bootstrap, receives the peer's, then calls ``bring_up_data`` and
    ``bring_up_control`` exactly once each.

    The hook layer (W2.3d) wires the Finalizer's send_* callbacks to
    ControlPlane methods, registers ControlPlane recv handlers that
    drive Finalizer.on_witness / on_repair_complete, and serializes
    bucket transfers per step.
    """

    def __init__(self, cfg: ClearTransportConfig):
        self.cfg = cfg

        # ---- Data plane: UC QP + RatioController -----------------------
        self.engine = UCQPEngine(
            dev_name=cfg.dev_name,
            buffer_bytes=cfg.buffer_bytes,
            sq_depth=cfg.sq_depth,
            rq_depth=cfg.rq_depth,
            gid_index=cfg.gid_index,
            qp_type="uc",
        )
        self.ratio = RatioController(self.engine)

        # ---- Control plane: RC QP -------------------------------------
        self.cp = ControlPlane(
            ControlPlaneConfig(
                dev_name=cfg.dev_name,
                gid_index=cfg.gid_index,
                recv_slots=cfg.cp_recv_slots,
                send_slots=cfg.cp_send_slots,
            )
        )

        # ---- Lease tables (one half each) -----------------------------
        self.sender_leases   = SenderLeaseTable(
            quarantine_ticks=cfg.quarantine_ticks)
        self.receiver_leases = ReceiverLeaseTable()

        # ---- Finalizer + RQ monitor -----------------------------------
        self.finalizer = Finalizer(FinalizerConfig(
            repair_budget_bytes_per_step=cfg.repair_budget_bytes_per_step,
            max_repair_bytes_per_uid=cfg.max_repair_bytes_per_uid,
        ))
        self.rq_monitor = RQMonitor(RQMonitorConfig(
            low_watermark   = cfg.rq_low_watermark,
            refill_target   = cfg.rq_refill_target,
            initial_credits = cfg.rq_initial_credits,
        ))

        self._data_up    = False
        self._control_up = False

    # ----- bring-up ----------------------------------------------------

    def bring_up_data(self, peer_qp: RemoteQpInfo, peer_mr: RemoteMR) -> None:
        """Bring up the UC data plane against the peer's data-plane QP+MR."""
        if self._data_up:
            raise RuntimeError("ClearTransport.bring_up_data called twice")
        self.engine.bring_up(peer_qp)
        self._peer_data_mr = peer_mr
        self._data_up = True

    def bring_up_control(self, peer_qp: RemoteQpInfo) -> None:
        """Bring up the RC control plane against the peer's control QP."""
        if self._control_up:
            raise RuntimeError("ClearTransport.bring_up_control called twice")
        self.cp.bring_up(peer_qp)
        self._control_up = True

    # ----- accessors ---------------------------------------------------

    @property
    def data_qp_info(self) -> RemoteQpInfo:
        return self.engine.local_qp_info()

    @property
    def data_mr_info(self) -> RemoteMR:
        return self.engine.local_mr_info()

    @property
    def control_qp_info(self) -> RemoteQpInfo:
        return self.cp.local_qp_info()

    @property
    def control_mr_info(self) -> RemoteMR:
        return self.cp.local_mr_info()

    @property
    def is_up(self) -> bool:
        return self._data_up and self._control_up

    # ----- callback wiring --------------------------------------------

    def wire_default_callbacks(
        self,
        *,
        apply_mask_cb: Callable[[int, int, bytes, int], None],
    ) -> None:
        """Wire the Finalizer's send_* callbacks to send via ControlPlane.

        ``apply_mask_cb(uid, decision, mask_bytes, n_chunks)`` is the
        application's mask-application hook (e.g. invokes
        ``apply_finalize`` from runtime.py against its own buffer copy).

        After this call, the only piece left for the hook layer is to
        register ControlPlane.on_begin / on_witness / on_finalize / etc.
        handlers that drive the local lease tables + finalizer state.
        """
        from semirdma._semirdma_ext.clear import (
            FinalizeDecision, RetirePayload, WitnessEncoding,
        )

        def send_repair_req(uid, ranges):
            self.cp.send_repair_req(uid, list(ranges))

        def send_finalize(uid, decision, mask_encoding, body):
            self.cp.send_finalize(uid, decision, mask_encoding, body)

        def send_retire(uid, slot, gen):
            self.cp.send_retire(uid, RetirePayload(slot_id=slot, gen=gen))

        self.finalizer.on_send_repair_req(send_repair_req)
        self.finalizer.on_send_finalize(send_finalize)
        self.finalizer.on_send_retire(send_retire)
        self.finalizer.on_apply_mask(apply_mask_cb)


__all__ = [
    "ClearTransport",
    "ClearTransportConfig",
]
