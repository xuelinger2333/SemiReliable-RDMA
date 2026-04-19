"""Shared pytest fixtures for Phase 3 Stage A tests.

RDMA device probing: every test that touches the pybind11 bindings requires
a SoftRoCE / ConnectX device.  We detect once per session and ``pytest.skip``
the whole module when absent, so running ``pytest tests/phase3`` on a
laptop without ibverbs doesn't fail noisily.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest


def _has_rxe_device(dev: str = "rxe0") -> bool:
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
    """Name of the RDMA device to use, or skip the test if none is available."""
    dev = os.environ.get("SEMIRDMA_TEST_DEV", "rxe0")
    if not _has_rxe_device(dev):
        pytest.skip(f"RDMA device {dev!r} not active (SoftRoCE or HCA required)")
    return dev
