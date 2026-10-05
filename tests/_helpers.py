"""Shared plumbing for the checks: run the package the way a user would.

The tests deliberately go through the command line (``python -m
taurex_memory_profiler``, ``python -m taurex_memory_profiler.runner``) instead
of importing the modules, so they exercise the real entry points. ``SRC`` is put
on ``PYTHONPATH`` so a plain checkout works without installing the package.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"

PLOT = [sys.executable, "-m", "taurex_memory_profiler"]
RUNNER = [sys.executable, "-m", "taurex_memory_profiler.runner"]
MONITOR = [sys.executable, "-m", "taurex_memory_profiler.monitor"]


def env(**overrides) -> dict:
    """Environment with the checkout's ``src`` importable, plus overrides."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), environment["PYTHONPATH"]] if environment.get("PYTHONPATH") else [str(SRC)])
    environment.update(overrides)
    return environment


def call(command: list, cwd=None, **overrides) -> subprocess.CompletedProcess:
    return subprocess.run([str(part) for part in command],
                          cwd=None if cwd is None else str(cwd),
                          env=env(**overrides), capture_output=True, text=True)
