"""semirdma.clear — Python integration for CLEAR (Phase 5).

CLEAR (Completion-Labeled Erasure Attribution for RoCE UC) is the
witnessed erasure semantics protocol on UC + Write-with-Imm + concurrent
DDP buckets. The C++ transport lives in src/transport/clear/; this Python
package owns the application-side glue:

    BucketManifest   — post-warmup bucket → stable bucket_seq id
    uid_hash         — (rank, step, bucket_seq, phase, peer_edge) → uint64
    Policy           — REPAIR_FIRST / MASK_FIRST / STALE_FILL / ESTIMATOR_SCALE
    PolicyRegistry   — bucket_seq → Policy lookup with classification helpers
    apply_finalize   — one-shot mask/stale-fill application onto a flat tensor

W2.3a scope (this module): pure-Python core, no C++ binding dependency,
no torch dependency at the data-layer level (operates on numpy / memoryview).
W2.3b will add pybind11 surfaces for ControlPlane/Finalizer/RQMonitor and
the clear_allreduce_hook that ties everything together.
"""

from .manifest import BucketManifest, uid_hash
from .policy import Policy, PolicyRegistry, FinalizeDecision
from .runtime import apply_finalize

# ClearTransport requires the C++ extension; importing it eagerly fails
# on machines where _semirdma_ext is not built. Tolerate that so the
# pure-Python W2.3a tests can still run on Windows / unbuilt environments.
try:
    from .transport import ClearTransport, ClearTransportConfig
except ImportError:  # pragma: no cover — environment-dependent
    ClearTransport = None  # type: ignore[assignment]
    ClearTransportConfig = None  # type: ignore[assignment]

try:
    from .protocol import (
        RecvResult,
        SendResult,
        chunkset_to_recv_bitmap,
        clear_recv_bucket,
        clear_send_bucket,
    )
except ImportError:  # pragma: no cover — environment-dependent
    RecvResult = SendResult = None  # type: ignore[assignment]
    chunkset_to_recv_bitmap = clear_recv_bucket = clear_send_bucket = None  # type: ignore[assignment]

try:
    from .hook import (
        ClearHookState,
        _run_clear_bucket,
        clear_allreduce_hook,
        step_advance,
    )
except ImportError:  # pragma: no cover — environment-dependent
    ClearHookState = None  # type: ignore[assignment]
    _run_clear_bucket = clear_allreduce_hook = step_advance = None  # type: ignore[assignment]

__all__ = [
    "BucketManifest",
    "ClearTransport",
    "ClearTransportConfig",
    "FinalizeDecision",
    "Policy",
    "PolicyRegistry",
    "apply_finalize",
    "uid_hash",
]
