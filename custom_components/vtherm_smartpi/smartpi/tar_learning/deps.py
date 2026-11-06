"""Numerical dependencies of the T_ar learning (numpy, scipy).

They are requirements of the integration but are imported lazily: only the
executor jobs of the learning import them, so the event loop never loads scipy.
When they cannot be found, the learning stays inactive and the regulation keeps
the bootstrap dead time.
"""
from __future__ import annotations

import importlib.util


def dependencies_importable() -> bool:
    """True when numpy and scipy can be found (they are not imported here)."""
    try:
        return all(importlib.util.find_spec(name) is not None for name in ("numpy", "scipy"))
    except (ImportError, ValueError):
        return False
