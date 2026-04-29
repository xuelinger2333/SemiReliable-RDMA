"""Phase 5 Python tests — gate on semirdma package importability.

Same pattern as tests/phase4/conftest.py: ``import semirdma`` triggers
loading of the C++ extension via ``semirdma.hooks``. On developer
machines without the built extension (e.g. Windows), the whole package
fails to import — skip gracefully.

The W2.3 ``semirdma.clear`` modules are themselves pure Python and have
no _semirdma_ext dependency, but importing them goes through the
package's __init__.py.
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
