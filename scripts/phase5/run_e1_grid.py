"""Phase 5 E1 grid runner — 2-node parallel execution.

Cell space: 4 transports × 3 drops × 3 seeds = 36 cells. Distribution:
hash-based assignment to amd247 / amd245 (18 each), sequential within node.

Port discipline (per node, per local cell index ``i`` ∈ 0..17):
    master_port    = port_base_master + i
    semirdma_port  = port_base_semi + 4 * i   (4 consecutive: tx.data, tx.cp, rx.data, rx.cp)

Sequential cells on the same node are spaced ~23 min apart (200 steps ×
~7 s/step), well above the 60 s TIME_WAIT, so port reuse is safe.

Retry policy: a failed cell retries with the SAME local index ``i`` (so
the cell↔port mapping is stable for log grep), with a 60 s sleep before
retry to flush any TIME_WAIT.

Run:
    python scripts/phase5/run_e1_grid.py --print     # show distribution, no exec
    python scripts/phase5/run_e1_grid.py --launch    # SSH-launch both nodes

Per-node side-runner (invoked by --launch over SSH):
    python scripts/phase5/run_e1_grid.py --node-runner \\
        --node amd247 --cells <comma-sep-cell-ids>
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List

TRANSPORTS = ["rc_baseline", "phase4_flat", "phase4_prc", "clear_t1"]
DROPS      = [0.0, 0.01, 0.05]
SEEDS      = [41, 42, 43]
STEPS      = 200
NODES      = ["amd247", "amd245"]
SSH_USER   = "chen123"

# Per-node port bases — chosen to keep the two nodes' ranges fully disjoint
# even though only TIME_WAIT collisions on the SAME node actually matter.
PORTS = {
    "amd247": {"master": 29500, "semi": 29700},
    "amd245": {"master": 29600, "semi": 29800},
}


@dataclass(frozen=True)
class Cell:
    cell_id: int
    transport: str
    drop: float
    seed: int

    def tag(self) -> str:
        d = f"{self.drop:.3f}".rstrip("0").rstrip(".") or "0"
        return f"c{self.cell_id:02d}_{self.transport}_loss{d}_seed{self.seed}"


def enumerate_cells() -> List[Cell]:
    cells = []
    cid = 0
    for t in TRANSPORTS:
        for d in DROPS:
            for s in SEEDS:
                cells.append(Cell(cid, t, d, s))
                cid += 1
    return cells


def assign_node(c: Cell) -> str:
    """Cell-id round-robin assignment.

    Cells are enumerated in (transport, drop, seed) lex order. cell_id % 2
    splits each per-transport block of 9 cells into 5/4 between the two
    nodes — guarantees 18/18 total AND that no single transport lands
    entirely on one node (which a coarser hash on small N can produce).
    SHA256 hash on 36 cells gave 25/11; round-robin gives the balanced
    split the user asked for.
    """
    return NODES[c.cell_id % len(NODES)]


def per_node_index(c: Cell, all_cells: List[Cell]) -> int:
    """Stable local-index of cell ``c`` within its assigned node."""
    node = assign_node(c)
    siblings = [x for x in all_cells if assign_node(x) == node]
    return siblings.index(c)


def cell_distribution(cells: List[Cell]) -> dict:
    """Group cells by node + by transport for sanity inspection."""
    out = {n: {"total": 0, "by_transport": {}, "by_drop": {}, "by_seed": {}}
           for n in NODES}
    for c in cells:
        node = assign_node(c)
        out[node]["total"] += 1
        out[node]["by_transport"].setdefault(c.transport, 0)
        out[node]["by_transport"][c.transport] += 1
        out[node]["by_drop"].setdefault(c.drop, 0)
        out[node]["by_drop"][c.drop] += 1
        out[node]["by_seed"].setdefault(c.seed, 0)
        out[node]["by_seed"][c.seed] += 1
    return out


def print_distribution(cells: List[Cell]) -> None:
    dist = cell_distribution(cells)
    print(f"Total cells: {len(cells)} (expected 36)")
    for node in NODES:
        d = dist[node]
        print(f"\n  {node}: {d['total']} cells")
        print(f"    by transport: {sorted(d['by_transport'].items())}")
        print(f"    by drop:      {sorted(d['by_drop'].items())}")
        print(f"    by seed:      {sorted(d['by_seed'].items())}")
    print("\nCell list:")
    for c in cells:
        node = assign_node(c)
        i = per_node_index(c, cells)
        ports = PORTS[node]
        master = ports["master"] + i
        semi = ports["semi"] + 4 * i
        print(f"  c{c.cell_id:02d}  node={node:<7s} i={i:2d}  "
              f"transport={c.transport:<12s} drop={c.drop:.3f} seed={c.seed}  "
              f"master={master} semi={semi}")


# --------------------------------------------------------------------------
# Per-node side runner: executes a list of cell ids sequentially
# --------------------------------------------------------------------------

def _torchrun_cmd(c: Cell, all_cells: List[Cell],
                  node: str, dev: str, gid: int,
                  data_root: str, repo: str,
                  steps: int = STEPS) -> str:
    """Build the torchrun shell command for one cell. Returns a single
    space-joined string ready to embed in the SSH wrapper."""
    i = per_node_index(c, all_cells)
    ports = PORTS[node]
    master = ports["master"] + i
    semi = ports["semi"] + 4 * i

    common_overrides = [
        f"transport={c.transport}",
        f"loss_rate={c.drop}",
        f"seed={c.seed}",
        f"steps={steps}",
        f"data.root={data_root}",
        "data.download=false",
        f"dist.semirdma_port={semi}",
    ]

    if c.transport == "clear_t1":
        config_args = ["--config-name", "phase5_e1"]
        # phase5_e1.yaml already pins transport_cfg fields; nothing extra.
        extras: List[str] = []
    else:
        config_args = []  # use default stage_a_baseline.yaml
        extras = [
            f"transport_cfg.dev_name={dev}",
            f"+transport_cfg.gid_index={gid}",
            "transport_cfg.chunk_bytes=16384",
            "transport_cfg.sq_depth=4096",
            "transport_cfg.rq_depth=8192",
            "+bucket_cap_mb=512",
        ]

    cmd = (
        f"PYTHONPATH={repo}/python torchrun "
        f"--nproc_per_node=2 --master_port={master} "
        f"{repo}/experiments/stage_a/train_cifar10.py "
        + " ".join(config_args + common_overrides + extras)
    )
    return cmd


SSH_OPTS = ["-o", "ServerAliveInterval=60",
            "-o", "ServerAliveCountMax=10",
            "-o", "ConnectTimeout=15"]


def _ssh(node: str, cmd: str, capture: bool = False, timeout: int = 60):
    """Short SSH call — returns CompletedProcess. Use for poll/probe only."""
    full = ["ssh"] + SSH_OPTS + [f"{SSH_USER}@{node}.utah.cloudlab.us", cmd]
    return subprocess.run(full, capture_output=capture, text=True, timeout=timeout)


def _cell_state(node: str, log_path: str) -> str:
    """Single-SSH probe. Returns one of: DONE, RUNNING, CRASHED, NOT_STARTED.

    RUNNING is true iff EITHER:
      (a) the log file was modified within the last 600 s (10 min) — the
          trainer logs every 50 steps so a 5-min gap is normal; or
      (b) a torchrun process is currently active on the node.

    Either signal is sufficient — both being absent means CRASHED.
    """
    probe = (
        f"if [ ! -f {log_path} ]; then echo NOT_STARTED; "
        f"elif grep -q 'training done' {log_path}; then echo DONE; "
        f"elif [ $(($(date +%s) - $(stat -c %Y {log_path}))) -lt 600 ] "
        f"     || pgrep -f 'master_port=' >/dev/null; then echo RUNNING; "
        f"else echo CRASHED; fi"
    )
    r = _ssh(node, probe, capture=True)
    return r.stdout.strip()


def _kill_zombies_on_node(node: str, log_path: str) -> None:
    """Best-effort kill of any torchrun tied to log_path."""
    _ssh(node, f"pkill -f '{log_path}' 2>/dev/null; sleep 2; "
               f"pkill -9 -f '{log_path}' 2>/dev/null; true")


def run_one_cell_remote(c: Cell, all_cells: List[Cell],
                        node: str, dev: str, gid: int,
                        data_root: str, repo: str,
                        log_dir: str, steps: int = STEPS) -> int:
    """Execute one cell on `node` via detached nohup + poll loop.

    SSH connections are short-lived (probe + launch). The cell runs in a
    detached shell on the node so a connection drop doesn't kill it.
    """
    inner_cmd = _torchrun_cmd(c, all_cells, node, dev, gid, data_root, repo, steps)
    log_path = f"{log_dir}/{c.tag()}.log"
    pid_path = f"{log_dir}/{c.tag()}.pid"

    print(f"[{time.strftime('%H:%M:%S')}] >>> {c.tag()} on {node}", flush=True)

    for attempt in range(2):
        state = _cell_state(node, log_path)
        if state == "DONE":
            print(f"[{time.strftime('%H:%M:%S')}] === {c.tag()} already DONE",
                  flush=True)
            return 0
        if state == "RUNNING":
            print(f"  {c.tag()} already running on node, waiting...", flush=True)
        else:
            # NOT_STARTED or CRASHED → launch fresh.
            if state == "CRASHED":
                _ssh(node, f"mv {log_path} {log_path}.attempt{attempt} 2>/dev/null; true")
            launch = (
                f"mkdir -p {log_dir} && cd {repo} && source .venv/bin/activate && "
                f"export RDMA_LOOPBACK_DEVICE={dev} RDMA_LOOPBACK_GID_INDEX={gid} "
                f"SEMIRDMA_PEER_HOST=127.0.0.1 HYDRA_FULL_ERROR=1 && "
                f"nohup bash -c \"{inner_cmd}\" </dev/null "
                f">{log_path} 2>&1 & disown"
            )
            try:
                _ssh(node, launch, timeout=30)
            except subprocess.TimeoutExpired:
                print(f"  launch SSH timeout", flush=True)
                continue

        # Poll for completion (max 45 min per cell).
        t0 = time.monotonic()
        deadline = t0 + 45 * 60
        last_print = 0.0
        while time.monotonic() < deadline:
            time.sleep(30)
            try:
                state = _cell_state(node, log_path)
            except subprocess.TimeoutExpired:
                continue  # transient
            if state == "DONE":
                elapsed = time.monotonic() - t0
                print(f"[{time.strftime('%H:%M:%S')}] <<< {c.tag()} "
                      f"DONE attempt={attempt} elapsed={elapsed:.0f}s",
                      flush=True)
                return 0
            if state == "CRASHED":
                print(f"  {c.tag()} CRASHED (log idle, no 'training done')",
                      flush=True)
                break
            now = time.monotonic()
            if now - last_print > 300:
                print(f"  [{time.strftime('%H:%M:%S')}] {c.tag()} still RUNNING "
                      f"({(now - t0):.0f}s)", flush=True)
                last_print = now

        print(f"  {c.tag()} attempt={attempt} not done; cleaning up", flush=True)
        _kill_zombies_on_node(node, log_path)
        if attempt < 1:
            print(f"  retry in 60s...", flush=True)
            time.sleep(60)

    return 1


def node_runner(node: str, cell_ids: List[int],
                dev: str, gid: int, data_root: str,
                repo: str, log_dir: str, steps: int = STEPS) -> int:
    cells = enumerate_cells()
    failed = []
    for cid in cell_ids:
        c = cells[cid]
        rc = run_one_cell_remote(c, cells, node, dev, gid,
                                  data_root, repo, log_dir, steps=steps)
        if rc != 0:
            failed.append(cid)
    if failed:
        print(f"FAILED CELLS on {node}: {failed}", flush=True)
        return 1
    return 0


# --------------------------------------------------------------------------
# Top-level launcher
# --------------------------------------------------------------------------

def launch(args) -> int:
    cells = enumerate_cells()
    dist = {n: [c.cell_id for c in cells if assign_node(c) == n] for n in NODES}

    print(f"Launching E1 grid: {len(cells)} cells across {NODES}")
    for n in NODES:
        print(f"  {n}: {len(dist[n])} cells, ids={dist[n]}")

    procs = {}
    for node in NODES:
        ids_csv = ",".join(str(i) for i in dist[node])
        cmd = [
            sys.executable, __file__, "--node-runner",
            "--node", node,
            "--cells", ids_csv,
            "--dev", args.dev,
            "--gid", str(args.gid),
            "--data-root", args.data_root,
            "--repo", args.repo,
            "--log-dir", args.log_dir,
            "--steps", str(args.steps),
        ]
        log = open(f"{args.local_log_dir}/{node}.runner.log", "w")
        procs[node] = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        print(f"  started {node} runner pid={procs[node].pid} → {log.name}")

    rcs = {}
    for node, p in procs.items():
        rc = p.wait()
        rcs[node] = rc
        print(f"  {node} runner exited rc={rc}")
    return 0 if all(v == 0 for v in rcs.values()) else 1


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode")
    ap.add_argument("--print", dest="print_dist", action="store_true")
    ap.add_argument("--launch", action="store_true")
    ap.add_argument("--node-runner", action="store_true")
    ap.add_argument("--node", type=str)
    ap.add_argument("--cells", type=str, help="comma-sep cell ids")
    ap.add_argument("--dev", type=str, default="mlx5_2")
    ap.add_argument("--gid", type=int, default=1)
    ap.add_argument("--data-root", type=str, default="/users/chen123/data/cifar10")
    ap.add_argument("--repo", type=str, default="/users/chen123/SemiRDMA")
    ap.add_argument("--log-dir", type=str,
                    default="/users/chen123/SemiRDMA/experiments/results/phase5/e1/logs")
    ap.add_argument("--local-log-dir", type=str,
                    default="experiments/results/phase5/e1/logs")
    ap.add_argument("--steps", type=int, default=STEPS,
                    help="Override step count (smoke=5, grid=200)")
    ap.add_argument("--smoke-cell", type=int, default=None,
                    help="Run a single cell id with --steps; for runner sanity")
    args = ap.parse_args()

    if args.print_dist:
        print_distribution(enumerate_cells())
        return 0
    if args.smoke_cell is not None:
        cells = enumerate_cells()
        c = cells[args.smoke_cell]
        node = assign_node(c)
        print(f"SMOKE: cell c{args.smoke_cell:02d} on {node} steps={args.steps}")
        return run_one_cell_remote(c, cells, node, args.dev, args.gid,
                                    args.data_root, args.repo,
                                    args.log_dir, steps=args.steps)
    if args.node_runner:
        cell_ids = [int(x) for x in args.cells.split(",")]
        return node_runner(args.node, cell_ids, args.dev, args.gid,
                           args.data_root, args.repo, args.log_dir,
                           steps=args.steps)
    if args.launch:
        Path(args.local_log_dir).mkdir(parents=True, exist_ok=True)
        return launch(args)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
