"""Databricks Apps entrypoint.

Compatibility ASGI import for existing launchers. New deployments install the
src-layout package and run `python -m brickpulse`, respecting the platform port.
"""

from __future__ import annotations

import os
import sys

_BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_BASE, "src"))

from brickpulse.server.main import app  # noqa: E402

__all__ = ["app"]
