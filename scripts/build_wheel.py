"""Build from a clean staging directory so deleted assets never leak into wheels.

The bundle runs this as its artifact build (`python3 scripts/build_wheel.py`); when that
interpreter lacks the `build` package, it hands over to the project's .venv.
"""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


def build():
    # One wheel in dist/, so the bundle's dist/*.whl never resolves to an older one.
    for old in (ROOT / "dist").glob("brickpulse-*.whl"):
        old.unlink()
    with TemporaryDirectory(prefix="brickpulse-wheel-") as scratch:
        stage = Path(scratch)
        shutil.copy2(ROOT / "pyproject.toml", stage / "pyproject.toml")
        shutil.copytree(ROOT / "src" / "brickpulse", stage / "src" / "brickpulse",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.orig", ".DS_Store"))
        subprocess.run([sys.executable, "-m", "build", "--wheel", "--no-isolation",
            "--outdir", str(ROOT / "dist")], cwd=stage, check=True)


if __name__ == "__main__":
    if importlib.util.find_spec("build") is None:
        venv = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if venv.is_file() and Path(sys.executable).resolve() != venv.resolve():
            os.execv(venv, [str(venv), __file__])
        sys.exit("The `build` package is missing: pip install build (or use the project's .venv).")
    build()
