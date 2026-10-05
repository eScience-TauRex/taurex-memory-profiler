#!/usr/bin/env python3
"""Checks for the per-node sampler (``python -m taurex_memory_profiler.monitor``).

    python tests/test_monitor.py
"""

from __future__ import annotations

import csv
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _helpers import MONITOR, env  # noqa: E402  (needs the path tweak above)

NODE_COLUMNS = ["timestamp", "node", "mem_total_kb", "mem_available_kb", "mem_used_kb",
                "mem_used_percent", "cgroup_mem_kb", "swap_used_kb", "shmem_kb", "load1",
                "load5", "load15", "user_procs", "user_rss_kb", "user_pss_kb",
                "user_rss_anon_kb", "elapsed_s"]
PROCESS_COLUMNS = ["timestamp", "node", "pid", "ppid", "command", "rss_kb", "pss_kb",
                   "vsz_kb", "vmpeak_kb", "data_kb", "threads", "rss_anon_kb",
                   "rss_file_kb", "rss_shmem_kb", "elapsed_s"]

LINUX = sys.platform == "linux"  # the sampler reads /proc


def start_monitor(logdir: Path, jobid="7777", interval="0.2", pattern="."):
    return subprocess.Popen(
        [str(part) for part in MONITOR + ["-o", str(logdir), "-i", interval,
                                          "-p", pattern, "-j", jobid]],
        env=env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def wait_for_file(logdir: Path, pattern: str, rows=2, timeout=20) -> Path:
    """Wait until <logdir>/<pattern> holds more than `rows` lines."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for path in sorted(logdir.glob(pattern)):
            if len(path.read_text().splitlines()) > rows:
                return path
        time.sleep(0.1)
    raise AssertionError(f"no {pattern} with more than {rows} lines in {logdir}")


def read_csv(path: Path):
    with path.open(newline="") as fp:
        return list(csv.DictReader(fp))


def test_stop_file_stops_the_sampler():
    """The stop file ends the loop, both CSVs are complete and consistent."""
    if not LINUX:
        return

    with tempfile.TemporaryDirectory() as tmp:
        logdir = Path(tmp) / "memory_logs"
        monitor = start_monitor(logdir)
        node_log = wait_for_file(logdir, "node_memory_7777_*.csv")
        process_log = wait_for_file(logdir, "memory_7777_*.csv")

        (logdir / ".stop_7777").touch()
        out, err = monitor.communicate(timeout=20)
        assert monitor.returncode == 0, out + err

        node_rows = read_csv(node_log)
        process_rows = read_csv(process_log)
        assert list(node_rows[0]) == NODE_COLUMNS
        assert list(process_rows[0]) == PROCESS_COLUMNS

        # per sample: the node row is exactly the sum of the process rows
        for row in node_rows:
            same_sample = [p for p in process_rows if p["timestamp"] == row["timestamp"]]
            assert same_sample, row
            assert int(row["user_procs"]) == len(same_sample)
            assert int(row["user_rss_kb"]) == sum(int(p["rss_kb"]) for p in same_sample)
            assert int(row["user_pss_kb"]) == sum(int(p["pss_kb"]) for p in same_sample)

        assert float(node_rows[-1]["elapsed_s"]) > float(node_rows[0]["elapsed_s"])
        assert all(int(row["mem_total_kb"]) > 0 for row in node_rows)
        assert all(int(row["rss_kb"]) > 0 for row in process_rows)


def test_pattern_filter_and_sigterm():
    """-p keeps only matching processes, and SIGTERM stops the loop cleanly."""
    if not LINUX:
        return

    with tempfile.TemporaryDirectory() as tmp:
        logdir = Path(tmp) / "memory_logs"
        sleeper = subprocess.Popen(["sleep", "60"])
        try:
            monitor = start_monitor(logdir, jobid="8888", pattern="^sleep$")
            process_log = wait_for_file(logdir, "memory_8888_*.csv")

            monitor.send_signal(signal.SIGTERM)
            out, err = monitor.communicate(timeout=20)
            assert monitor.returncode == 0, out + err

            rows = read_csv(process_log)
            assert rows
            assert {row["command"] for row in rows} == {"sleep"}
            assert {int(row["pid"]) for row in rows} == {sleeper.pid}
            assert all(int(row["threads"]) >= 1 for row in rows)
        finally:
            sleeper.terminate()


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"OK  {test.__name__}")
    print(f"\nAll {len(tests)} checks passed.")


if __name__ == "__main__":
    main()
