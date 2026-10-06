#!/usr/bin/env python3
"""Checks for the runner (``python -m taurex_memory_profiler.runner``, the
``taurex-mem-run`` command): it samples every node, keeps the command's exit
code and writes the metadata the plots read back.

    python tests/test_runner.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _helpers import RUNNER, call  # noqa: E402  (needs the path tweak above)

LINUX = sys.platform == "linux"  # the sampler reads /proc


def samples(path: Path) -> int:
    return max(len(path.read_text().splitlines()) - 1, 0)


def test_runner_monitors_the_command():
    """A monitored run leaves the CSVs, the meta file and no stop file."""
    if not LINUX:
        return

    with tempfile.TemporaryDirectory() as tmp:
        proc = call([*RUNNER, "-i", "0.2", "-j", "4242", "-l", "oneliner",
                     "--", "bash", "-c", "sleep 2"],
                    cwd=tmp, MEM_RUN_WAIT="1")
        assert proc.returncode == 0, proc.stdout + proc.stderr

        logs = Path(tmp) / "memory_logs"
        meta = (logs / "run_4242.meta").read_text()
        assert "label=oneliner" in meta
        assert "jobid=4242" in meta
        assert not (logs / ".stop_4242").exists()

        node_logs = list(logs.glob("node_memory_4242_*.csv"))
        process_logs = list(logs.glob("memory_4242_*.csv"))
        assert node_logs and process_logs, proc.stdout
        assert samples(node_logs[0]) >= 1
        assert samples(process_logs[0]) >= 1

        assert "Job Started:" in proc.stdout and "Job Finished:" in proc.stdout
        assert "taurex-mem-plot --logdir" in proc.stdout


def test_runner_returns_the_command_exit_code():
    if not LINUX:
        return

    with tempfile.TemporaryDirectory() as tmp:
        proc = call([*RUNNER, "-i", "0.2", "-j", "4243", "--", "bash", "-c", "exit 3"],
                    cwd=tmp, MEM_RUN_WAIT="0")
        assert proc.returncode == 3, proc.stdout + proc.stderr
        assert "Exit code: 3" in proc.stdout


def test_runner_passes_threads_to_the_sampler():
    """-t reaches the sampler and is recorded in the meta file."""
    if not LINUX:
        return

    with tempfile.TemporaryDirectory() as tmp:
        proc = call([*RUNNER, "-i", "0.2", "-t", "3", "-j", "4244",
                     "--", "bash", "-c", "sleep 1"],
                    cwd=tmp, MEM_RUN_WAIT="0")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "threads 3" in proc.stdout

        logs = Path(tmp) / "memory_logs"
        assert "threads=3" in (logs / "run_4244.meta").read_text()
        assert list(logs.glob("node_memory_4244_*.csv"))


def test_runner_without_a_command_fails():
    proc = call([*RUNNER, "-i", "1"])
    assert proc.returncode != 0
    assert "no command given" in proc.stderr


def test_runner_rejects_unknown_options():
    proc = call([*RUNNER, "--nope", "--", "true"])
    assert proc.returncode != 0
    assert "--nope" in proc.stderr


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"OK  {test.__name__}")
    print(f"\nAll {len(tests)} checks passed.")


if __name__ == "__main__":
    main()
