"""Phase 4 tests gate on the SemiRDMA package being importable.

Most of the layer-aware code is pure Python, but the package's
``__init__.py`` re-exports symbols from ``semirdma.hooks``, which loads
the C++ extension ``_semirdma_ext`` at import time. On developer
machines without the built extension (e.g. Windows), ``import semirdma``
fails — so we skip the whole module gracefully there.

The Linux remote (amd203 / amd196) builds the extension, so these tests
run there.
"""

from __future__ import annotations

import pytest

try:
    import semirdma  # noqa: F401
except ImportError as exc:  # pragma: no cover — environment-dependent
    pytest.skip(
        f"semirdma package not importable: {exc}",
        allow_module_level=True,
    )
