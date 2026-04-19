"""SemiRDMA — semi-reliable RDMA transport for AI training gradients.

Phase 3 Stage A public API grows incrementally — this commit adds
``TransportConfig`` and the TCP bootstrap helper; ``SemiRDMATransport`` and
the DDP hook (``semirdma_allreduce_hook`` / ``SemiRDMAHookState``) land in
later commits of this stage.
"""

from semirdma.config import TransportConfig

__version__ = "0.3.0a0"

__all__ = [
    "TransportConfig",
    "__version__",
]
