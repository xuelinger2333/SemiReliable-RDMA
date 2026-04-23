"""Stage A · RQ5-A1 / A2 training driver.

Invoked via ``torchrun --nproc_per_node=2`` (see scripts/aliyun/run_stage_a.sh).
Each worker reads the same Hydra config, installs either Gloo or SemiRDMA
as the DDP comm hook, then runs ``steps`` SGD updates on ResNet-18 /
CIFAR-10 and writes four CSVs to the Hydra run dir.

Design notes:
  - Data loader uses DistributedSampler with a fixed seed so both
    workers walk CIFAR-10 in the same order across transport variants —
    required by A1's "loss curve equivalence" claim.
  - For transport=semirdma we keep gradient tensors on CPU; GPU staging
    is a Stage B concern.  CIFAR-10 + ResNet-18 on 8 vCPUs is tight but
    tractable.
  - Iteration-time metrics (forward/backward/comm/optim) are recorded
    only on rank 0 to avoid per-rank noise confusing downstream plots.
"""

from __future__ import annotations

import csv
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Tuple, IO

import hydra
import numpy as np
import torch
import torch.distributed as dist
import torch.nn as nn
import torch.utils.data as tud
from omegaconf import DictConfig, OmegaConf

import torchvision
import torchvision.transforms as T
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.distributed.algorithms.ddp_comm_hooks import default_hooks as ddp_default_hooks

logger = logging.getLogger(__name__)


def _set_seed(seed: int) -> None:
    import random
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.use_deterministic_algorithms(False)  # CIFAR-10 kernels miss some deterministic impls
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    os.environ["PYTHONHASHSEED"] = str(seed)


def _build_loaders(cfg: DictConfig, rank: int, world_size: int) -> tud.DataLoader:
    tfm = T.Compose([
        T.ToTensor(),
        T.Normalize((0.4914, 0.4822, 0.4465), (0.2470, 0.2435, 0.2616)),
    ])
    ds = torchvision.datasets.CIFAR10(
        root=cfg.data.root, train=True, download=cfg.data.download, transform=tfm,
    )
    sampler = tud.distributed.DistributedSampler(
        ds, num_replicas=world_size, rank=rank, shuffle=True, seed=cfg.seed, drop_last=True,
    )
    return tud.DataLoader(
        ds,
        batch_size=cfg.data.batch_size,
        sampler=sampler,
        num_workers=cfg.data.num_workers,
        pin_memory=cfg.data.pin_memory,
        drop_last=True,
    )


def _build_model(cfg: DictConfig) -> nn.Module:
    if cfg.model.name != "resnet18":
        raise NotImplementedError(f"model.name={cfg.model.name}")
    net = torchvision.models.resnet18(weights=None, num_classes=cfg.model.num_classes)
    # CIFAR-10 is 32x32; ResNet-18's first 7x7/stride2 conv + maxpool discard
    # too much spatial information, so tune the stem.
    net.conv1 = nn.Conv2d(3, 64, kernel_size=3, stride=1, padding=1, bias=False)
    net.maxpool = nn.Identity()
    return net


def _install_hook(ddp_model: DDP, cfg: DictConfig, rank: int) -> object:
    """Returns any per-rank hook state that must outlive this function
    (e.g. SemiRDMAHookState)."""
    if cfg.transport == "gloo":
        ddp_model.register_comm_hook(None, ddp_default_hooks.allreduce_hook)
        return None

    if cfg.transport == "semirdma":
        from semirdma import (
            SemiRDMAHookState, TransportConfig, semirdma_allreduce_hook,
        )
        tcfg = TransportConfig(
            dev_name=cfg.transport_cfg.dev_name,
            buffer_bytes=cfg.transport_cfg.buffer_bytes,
            chunk_bytes=cfg.transport_cfg.chunk_bytes,
            sq_depth=cfg.transport_cfg.sq_depth,
            rq_depth=cfg.transport_cfg.rq_depth,
            ratio=cfg.transport_cfg.ratio,
            timeout_ms=cfg.transport_cfg.timeout_ms,
            loss_rate=cfg.loss_rate,
            loss_seed=cfg.seed * 31 + 7,   # different drop pattern per seed
        )
        # Per-rank peer host: rank 0 connects to rank 1's IP, and vice versa.
        # On same-host runs (Stage A SoftRoCE), set SEMIRDMA_PEER_HOST=127.0.0.1
        # on both ranks; on multi-host (Stage B real NIC), set it to the
        # OTHER node's experiment-LAN IP per rank.
        peer_host = os.environ.get("SEMIRDMA_PEER_HOST", cfg.dist.master_addr)
        state = SemiRDMAHookState.for_rank(
            rank=rank,
            world_size=cfg.dist.world_size,
            peer_host=peer_host,
            port=cfg.dist.semirdma_port,
            cfg=tcfg,
        )
        ddp_model.register_comm_hook(state, semirdma_allreduce_hook)
        return state

    raise ValueError(f"transport={cfg.transport!r}")


def _open_csv(path: Path, header: List[str]) -> Tuple["csv._writer", IO]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w", newline="")
    w = csv.writer(fh)
    w.writerow(header)
    return w, fh


def _train(cfg: DictConfig, rank: int, world_size: int) -> None:
    _set_seed(cfg.seed + rank)

    loader = _build_loaders(cfg, rank, world_size)
    model = _build_model(cfg)
    # Force a single DDP bucket per step.  SemiRDMA uses Write-with-Imm's
    # imm_data (just the chunk index within a ChunkSet) as the only chunk
    # identifier.  Two concurrent buckets would share imm=0..min(N0,N1),
    # causing bucket 1's CQEs to be consumed by bucket 0's await (and
    # vice-versa) and bucket 1 then times out to all-zero gradient.
    # ResNet-18 fp32 is ~47 MiB, so 512 MB cap comfortably fits the whole
    # model in one bucket.  Gloo sees the same setting so A1 comparison is
    # apples-to-apples.
    ddp_model = DDP(model, bucket_cap_mb=512)
    _hook_state = _install_hook(ddp_model, cfg, rank)  # keep-alive

    opt = torch.optim.SGD(
        ddp_model.parameters(),
        lr=cfg.optim.lr,
        momentum=cfg.optim.momentum,
        weight_decay=cfg.optim.weight_decay,
    )
    loss_fn = nn.CrossEntropyLoss()

    out_dir = Path.cwd()   # hydra.job.chdir=true places us in the run dir
    if rank == 0:
        loss_w, loss_fh = _open_csv(out_dir / "loss_per_step.csv", ["step", "loss"])
        iter_w, iter_fh = _open_csv(out_dir / "iter_time.csv",     ["step", "fwd_ms", "bwd_ms", "opt_ms", "total_ms"])
        grad_w, grad_fh = _open_csv(out_dir / "grad_norm.csv",     ["step", "grad_l2"])
    else:
        loss_w = iter_w = grad_w = None
        loss_fh = iter_fh = grad_fh = None

    step = 0
    ddp_model.train()
    t_run_start = time.perf_counter()

    while step < cfg.steps:
        # Reshuffle each epoch so we don't replay the same 391 batches if
        # steps > len(loader).
        loader.sampler.set_epoch(step // max(1, len(loader)))
        for x, y in loader:
            if step >= cfg.steps:
                break

            t0 = time.perf_counter()
            opt.zero_grad(set_to_none=True)
            out = ddp_model(x)
            loss = loss_fn(out, y)
            t1 = time.perf_counter()
            loss.backward()
            t2 = time.perf_counter()
            opt.step()
            t3 = time.perf_counter()

            if rank == 0:
                l = float(loss.item())
                loss_w.writerow([step, f"{l:.6f}"])       # type: ignore[union-attr]
                iter_w.writerow([                           # type: ignore[union-attr]
                    step,
                    f"{(t1 - t0) * 1000:.3f}",
                    f"{(t2 - t1) * 1000:.3f}",
                    f"{(t3 - t2) * 1000:.3f}",
                    f"{(t3 - t0) * 1000:.3f}",
                ])
                # grad_l2 is a quick convergence sanity signal.
                grad_sq = 0.0
                for p in ddp_model.parameters():
                    if p.grad is not None:
                        grad_sq += float(p.grad.detach().pow(2).sum().item())
                grad_w.writerow([step, f"{grad_sq ** 0.5:.6f}"])   # type: ignore[union-attr]

                if step % 50 == 0:
                    logger.info(
                        "step=%d loss=%.4f iter_ms=%.1f",
                        step, l, (t3 - t0) * 1000,
                    )

            step += 1

    elapsed = time.perf_counter() - t_run_start
    if rank == 0:
        logger.info("training done: %d steps in %.1fs", cfg.steps, elapsed)
        loss_fh.close()
        iter_fh.close()
        grad_fh.close()


@hydra.main(version_base=None, config_path="../configs", config_name="stage_a_baseline")
def main(cfg: DictConfig) -> None:
    rank = int(os.environ.get("RANK", "0"))
    world_size = int(os.environ.get("WORLD_SIZE", str(cfg.dist.world_size)))
    if rank == 0:
        logger.info("Stage A config:\n%s", OmegaConf.to_yaml(cfg))

    os.environ.setdefault("MASTER_ADDR", cfg.dist.master_addr)
    os.environ.setdefault("MASTER_PORT", str(cfg.dist.master_port))
    # Use gloo as the rendezvous backend even for transport=semirdma; DDP
    # internals still need a process group for parameter broadcast etc., and
    # our SemiRDMA hook replaces only the per-bucket allreduce.
    dist.init_process_group(backend="gloo", rank=rank, world_size=world_size)

    try:
        _train(cfg, rank, world_size)
    finally:
        dist.destroy_process_group()


if __name__ == "__main__":
    main()
