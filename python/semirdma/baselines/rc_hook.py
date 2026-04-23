"""RC-Baseline DDP comm hook.

Represents the "reliable transport, no wire loss" reference in the RQ6
5-baseline comparison (design-ddp-integration.md §2.2).  Implementation
is a thin wrapper over ``torch.distributed.all_reduce`` — since our
rendezvous backend is gloo (TCP), the reduce itself is fully reliable
and maps semantically to "what would happen if the network and transport
both worked perfectly."

This is functionally equivalent to ``transport=gloo`` in
train_cifar10.py, but exposed separately so the RQ6 output tree has a
named row for RC-Baseline (aligned with the paper's baseline table).
"""

from dataclasses import dataclass

import torch
import torch.distributed as dist
import torch.futures as futures
from torch.distributed import GradBucket


@dataclass
class RCBaselineState:
    """Lives for the lifetime of DDP training; exposed for symmetry with
    SemiRDMAHookState even though RC-Baseline is stateless.

    Future extensions (e.g., per-step counters) can attach here without
    breaking the hook signature.
    """

    bucket_idx: int = 0


def rc_baseline_hook(
    state: RCBaselineState, bucket: GradBucket
) -> torch.futures.Future[torch.Tensor]:
    """Reliable AllReduce over the rendezvous backend (gloo TCP).

    Returns a future yielding the averaged-in-place bucket tensor, matching
    the contract of ``ddp_default_hooks.allreduce_hook``.
    """
    state.bucket_idx += 1
    tensor = bucket.buffer()

    # world_size is the correct divisor for converting SUM to AVG — matches
    # the default DDP allreduce semantics so training math is identical to
    # the transport=gloo control.
    world_size = dist.get_world_size()
    work = dist.all_reduce(tensor, op=dist.ReduceOp.SUM, async_op=True)
    fut = work.get_future()

    def _finish(fut: futures.Future) -> torch.Tensor:
        out = fut.value()[0]
        out.div_(world_size)
        return out

    return fut.then(_finish)
