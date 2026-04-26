"""End-to-end DDP hook test for the layer-aware dispatcher.

Mirrors ``tests/phase3/test_ddp_hook.py`` but installs
``layer_aware_dispatcher_hook`` and a ``LossToleranceRegistry``. Validates
that the dispatcher fires on a real (RDMA + DDP) loop and training
converges. Skipped when no RDMA device is available.
"""

from __future__ import annotations

import os
import socket

import pytest

torch = pytest.importorskip("torch")
dist = pytest.importorskip("torch.distributed")
mp = pytest.importorskip("torch.multiprocessing")


# Reuse the rxe_device fixture from phase3 (same conftest in phase3/).
# We mirror its detection logic here so phase4 conftest doesn't depend on
# phase3 layout.
import shutil
import subprocess


def _has_rxe_device(dev: str) -> bool:
    if shutil.which("ibv_devinfo") is None:
        return False
    try:
        r = subprocess.run(
            ["ibv_devinfo", "-d", dev],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and "PORT_ACTIVE" in r.stdout


@pytest.fixture(scope="session")
def rxe_device() -> str:
    dev = os.environ.get("SEMIRDMA_TEST_DEV", "rxe0")
    if not _has_rxe_device(dev):
        pytest.skip(f"RDMA device {dev!r} not active (SoftRoCE or HCA required)")
    return dev


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _worker(rank: int, world_size: int, port: int, semirdma_port_base: int, out_q) -> None:
    os.environ["MASTER_ADDR"] = "127.0.0.1"
    os.environ["MASTER_PORT"] = str(port)
    dist.init_process_group(backend="gloo", rank=rank, world_size=world_size)

    torch.manual_seed(42 + rank)
    # Small model with mixed layer types, so the registry exercises both paths
    # if the user-set p_L assigns different values per layer. With the default
    # bucket_cap_mb=25 the entire model fits in one bucket → routing is binary
    # (whichever min(p_L) lands the bucket).
    model = torch.nn.Sequential(
        torch.nn.Linear(32, 64),     # name "0"
        torch.nn.BatchNorm1d(64),    # name "1"
        torch.nn.ReLU(),             # name "2" — no params
        torch.nn.Linear(64, 10),     # name "3"
    )
    ddp = torch.nn.parallel.DistributedDataParallel(model)

    from semirdma import (
        LayerAwareHookState,
        LossToleranceRegistry,
        TransportConfig,
        layer_aware_dispatcher_hook,
    )

    # Register ALL parameter-bearing modules with non-zero p so the dispatcher
    # routes through the SemiRDMA path. (If we leave any unregistered, the
    # bucket's min(p_L) collapses to 0 and forces RC every step, never
    # exercising the calibrator.)
    registry = LossToleranceRegistry()
    registry.register("0", 0.05)
    registry.register("1", 0.05)
    registry.register("3", 0.05)

    cfg = TransportConfig(
        dev_name=os.environ.get("SEMIRDMA_TEST_DEV", "rxe0"),
        buffer_bytes=4 * 1024 * 1024,
        chunk_bytes=16 * 1024,
        sq_depth=16,
        rq_depth=64,
        layer_aware=True,
        # Fast bootstrap so we exit the legacy-fallback window inside 5 steps.
        calibration_alpha=0.3,
        calibration_window=10,
        calibration_bootstrap_buckets=2,
        loss_safety_margin=0.005,
    )
    state = LayerAwareHookState.for_rank_layer_aware(
        rank=rank, world_size=world_size,
        peer_host="127.0.0.1", port=semirdma_port_base,
        cfg=cfg,
        model=ddp.module,
        registry=registry,
    )
    ddp.register_comm_hook(state, layer_aware_dispatcher_hook)

    opt = torch.optim.SGD(ddp.parameters(), lr=0.05)
    loss_fn = torch.nn.CrossEntropyLoss()

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

    out_q.put((rank, losses, state.n_buckets, state.n_routed_rc, state.n_routed_semi))
    dist.destroy_process_group()


@pytest.mark.timeout(120)
def test_layer_aware_dispatcher_runs_and_converges(rxe_device) -> None:
    os.environ.setdefault("SEMIRDMA_TEST_DEV", rxe_device)

    world_size = 2
    port = _free_port()
    # 4-port range for layer_aware: P, P+1 = UC tx/rx; P+2, P+3 = RC tx/rx.
    # We rely on the OS not handing out P+1..P+3 to other listeners between
    # this allocation and the workers' bind. Same approach as phase3 test.
    semirdma_port_base = _free_port()

    ctx = mp.get_context("spawn")
    out_q = ctx.Queue()
    procs = [
        ctx.Process(target=_worker, args=(r, world_size, port, semirdma_port_base, out_q))
        for r in range(world_size)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=100)
        assert p.exitcode == 0, f"worker {p.pid} exitcode={p.exitcode}"

    results = {}
    while not out_q.empty():
        r, losses, n_buckets, n_rc, n_semi = out_q.get()
        results[r] = (losses, n_buckets, n_rc, n_semi)
    assert set(results.keys()) == {0, 1}

    # Both ranks should see byte-identical losses (deterministic CPU torch).
    assert results[0][0] == pytest.approx(results[1][0], abs=1e-4)

    losses_r0, n_buckets, n_rc, n_semi = results[0]
    assert losses_r0[-1] < losses_r0[0], f"loss did not decrease: {losses_r0}"

    # Dispatcher should have fired at least once per step.
    assert n_buckets >= 5, f"n_buckets={n_buckets} (expected >=5 for 5 steps)"
    # With all layers registered at p=0.05 > eps + margin, post-bootstrap
    # buckets should route through SemiRDMA, not RC.
    assert n_semi > 0, f"no SemiRDMA dispatch: rc={n_rc} semi={n_semi}"
