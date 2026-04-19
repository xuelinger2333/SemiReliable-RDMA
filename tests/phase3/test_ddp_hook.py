"""End-to-end DDP hook test: 2 workers on the same host, 5 training steps
of a tiny MLP on synthetic data, with the SemiRDMA hook installed.

Goal: verify the hook doesn't crash and loss moves in the expected
direction.  Real convergence / numerical-equivalence evaluation is the
experiments/ job (RQ5-A1 / A2), not pytest's job — this test gates the
*integration* not the *research claim*.

Marked slow: takes ~10s per run because torch.multiprocessing startup
dominates.
"""

from __future__ import annotations

import os
import socket

import pytest

torch = pytest.importorskip("torch")
dist = pytest.importorskip("torch.distributed")
mp = pytest.importorskip("torch.multiprocessing")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _worker(rank: int, world_size: int, port: int, semirdma_port: int, out_q) -> None:
    """Body of each spawned worker process."""
    os.environ["MASTER_ADDR"] = "127.0.0.1"
    os.environ["MASTER_PORT"] = str(port)
    dist.init_process_group(backend="gloo", rank=rank, world_size=world_size)

    torch.manual_seed(42 + rank)
    model = torch.nn.Sequential(
        torch.nn.Linear(32, 64),
        torch.nn.ReLU(),
        torch.nn.Linear(64, 10),
    )
    ddp = torch.nn.parallel.DistributedDataParallel(model)

    from semirdma import (
        SemiRDMAHookState,
        TransportConfig,
        semirdma_allreduce_hook,
    )

    cfg = TransportConfig(
        dev_name=os.environ.get("SEMIRDMA_TEST_DEV", "rxe0"),
        buffer_bytes=4 * 1024 * 1024,
        chunk_bytes=16 * 1024,
        sq_depth=16,
        rq_depth=64,
    )
    state = SemiRDMAHookState.for_rank(
        rank=rank, world_size=world_size,
        peer_host="127.0.0.1", port=semirdma_port, cfg=cfg,
    )
    ddp.register_comm_hook(state, semirdma_allreduce_hook)

    opt = torch.optim.SGD(ddp.parameters(), lr=0.05)
    loss_fn = torch.nn.CrossEntropyLoss()

    # Fixed synthetic batch — both ranks see the same inputs so loss should
    # only depend on the initial weights (which are broadcast identically
    # by DDP at construction).
    torch.manual_seed(0)
    x = torch.randn(64, 32)
    y = torch.randint(0, 10, (64,))

    losses = []
    for _ in range(5):
        opt.zero_grad()
        out = ddp(x)
        loss = loss_fn(out, y)
        loss.backward()
        opt.step()
        losses.append(float(loss.item()))

    out_q.put((rank, losses))
    dist.destroy_process_group()


@pytest.mark.timeout(90)
def test_ddp_hook_runs_and_converges(rxe_device) -> None:
    os.environ.setdefault("SEMIRDMA_TEST_DEV", rxe_device)

    world_size = 2
    port = _free_port()
    semirdma_port = _free_port()

    ctx = mp.get_context("spawn")
    out_q = ctx.Queue()
    procs = [
        ctx.Process(target=_worker, args=(r, world_size, port, semirdma_port, out_q))
        for r in range(world_size)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=75)
        assert p.exitcode == 0, f"worker {p.pid} exitcode={p.exitcode}"

    results = {}
    while not out_q.empty():
        r, losses = out_q.get()
        results[r] = losses
    assert set(results.keys()) == {0, 1}

    # With gradient averaging both ranks should see identical losses.
    assert results[0] == pytest.approx(results[1], abs=1e-4), (
        f"ranks diverged: {results}"
    )

    losses = results[0]
    assert losses[-1] < losses[0], f"loss did not decrease: {losses}"
