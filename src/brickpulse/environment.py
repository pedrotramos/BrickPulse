"""Configuration names: BRICKPULSE wins, FINOPS remains a compatibility alias."""

import os


def get_env(name: str, default=None):
    canonical = name.replace("FINOPS_", "BRICKPULSE_", 1) if name.startswith("FINOPS_") else name
    return os.environ.get(canonical, os.environ.get(name, default))
