"""Numerical dependencies of the room model (numpy, scipy).

They are requirements of the integration but are imported lazily: only the
executor jobs import them, so the event loop never loads scipy. When they
cannot be found, the room history records nothing and the room model stays
silent; the regulation is unchanged.
"""
from __future__ import annotations

import importlib.util


def dependencies_importable() -> bool:
    """True when numpy and scipy can be found (they are not imported here)."""
    try:
        return all(importlib.util.find_spec(name) is not None for name in ("numpy", "scipy"))
    except (ImportError, ValueError):
        return False
