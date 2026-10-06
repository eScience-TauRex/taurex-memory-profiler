"""Per-node memory sampler — the Python replacement for ``memory_monitor.sh``.

Every INTERVAL seconds it samples both the node and every process of the user,
and appends one row per sample to two CSV files:

  <outdir>/node_memory_<jobid>_<node>.csv  one row per node sample
  <outdir>/memory_<jobid>_<node>.csv       one row per process sample

Node file : MemTotal/MemAvailable/MemUsed, the Slurm job cgroup counter (the
            number the OOM killer looks at), swap, load average and the
            per-user RSS/Pss summary.
Process   : RSS, Pss, VSZ, VmPeak, RssAnon/RssFile/RssShmem, VmData and thread
            count for every process of the user.

Why Pss: RSS double-counts pages shared between MPI ranks (shared opacities,
mmap'ed libraries). Pss (Proportional Set Size, from /proc/<pid>/smaps_rollup)
divides every shared page by the number of processes mapping it, so sum(Pss) is
the true physical footprint — use it to size ``--mem``. RSS is still logged for
comparison and for kernels without smaps_rollup.

Pss is the only expensive field: reading /proc/<pid>/smaps_rollup walks the
target's page tables and costs milliseconds for a process with a large address
space, so reading it for every rank in series is what silently stretches a
requested interval (0.1 s became ~0.27 s for 64 ranks). ``-t/--threads`` reads
Pss from parallel threads instead; give the step the matching CPUs
(``taurex-mem-run`` does) so the requested interval is actually met.

One monitor runs per node: :mod:`taurex_memory_profiler.runner` (``taurex-mem-run``)
starts them, but the sampler also works standalone on a login node:

    python -m taurex_memory_profiler.monitor -o memory_logs -i 1

Stop it with SIGTERM/SIGINT, or by creating ``<outdir>/.stop_<jobid>`` (the
latter is what ``taurex-mem-run`` uses to stop every node at once).

The CSVs are appended and flushed after every sample, so they survive even if
the job is OOM-killed.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import signal
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

PROCESS_COLUMNS = [
    "timestamp", "node", "pid", "ppid", "command", "rss_kb", "pss_kb", "vsz_kb",
    "vmpeak_kb", "data_kb", "threads", "rss_anon_kb", "rss_file_kb",
    "rss_shmem_kb", "elapsed_s",
]

NODE_COLUMNS = [
    "timestamp", "node", "mem_total_kb", "mem_available_kb", "mem_used_kb",
    "mem_used_percent", "cgroup_mem_kb", "swap_used_kb", "shmem_kb", "load1",
    "load5", "load15", "user_procs", "user_rss_kb", "user_pss_kb",
    "user_rss_anon_kb", "elapsed_s",
]

# /proc/<pid>/status field -> the column it feeds
STATUS_FIELDS = {
    "Name": "command",
    "PPid": "ppid",
    "VmSize": "vsz_kb",
    "VmPeak": "vmpeak_kb",
    "VmRSS": "rss_kb",
    "RssAnon": "rss_anon_kb",
    "RssFile": "rss_file_kb",
    "RssShmem": "rss_shmem_kb",
    "VmData": "data_kb",
    "Threads": "threads",
}


class _Interrupted(Exception):
    """Raised by the signal handler so the loop stops even mid-sleep."""


def _on_signal(signum, frame):
    raise _Interrupted(f"got signal {signum}")


def _timestamp() -> str:
    """ISO timestamp with milliseconds: readable, sortable and precise enough
    for sampling intervals well below a second."""
    now = datetime.now()
    return f"{now:%Y-%m-%d %H:%M:%S}.{now.microsecond // 1000:03d}"


def _now_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def stop_file_path(outdir: Path, jobid: str) -> Path:
    return Path(outdir) / f".stop_{jobid}"


def find_cgroup_file(jobid: str) -> str:
    """Locate the Slurm job cgroup memory counter (this is what the OOM killer
    looks at). Works for cgroup v2 and v1; empty string if neither is found."""
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        fields = line.split(":")
        if len(fields) < 3:
            continue
        if fields[0] == "0":  # cgroup v2
            directory = _job_cgroup_dir(f"/sys/fs/cgroup{fields[2]}", jobid)
            counter = Path(directory) / "memory.current"
            if os.access(counter, os.R_OK):
                return str(counter)
        elif "memory" in fields[1].split(","):  # cgroup v1
            directory = _job_cgroup_dir(f"/sys/fs/cgroup/memory{fields[2]}", jobid)
            counter = Path(directory) / "memory.usage_in_bytes"
            if os.access(counter, os.R_OK):
                return str(counter)
    return ""


def _job_cgroup_dir(path: str, jobid: str) -> str:
    """Trim a per-task cgroup path back to the Slurm job directory.

    Two layouts are handled: the legacy ``/job_<jobid>`` component, and the
    systemd scope used by the cluster here, ``.../slurmstepd.scope/<job>/step_<n>``
    where the job directory is the one right below ``slurmstepd.scope``. Without
    the second case the sampler reads the counter of its own monitor task (a few
    MB) instead of the whole job.
    """
    marker = f"/job_{jobid}"
    index = path.find(marker)
    if index != -1:
        return path[:index] + marker
    scope = "/slurmstepd.scope/"
    index = path.find(scope)
    if index != -1:
        rest = path[index + len(scope):]
        if rest:
            return path[:index + len(scope)] + rest.split("/", 1)[0]
    return path


def user_pids(uid: int) -> Iterator[int]:
    """Every process owned by ``uid`` (the portable equivalent of ``pgrep -u``)."""
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid == uid:
                yield int(entry.name)
        except OSError:
            continue  # the process vanished between listing and stat


def read_status(pid: int) -> dict:
    """The few /proc/<pid>/status fields the sampler logs, or {} if the process
    is gone (it can exit between the directory listing and this read)."""
    values = {"command": "", "ppid": 0, "rss_kb": 0, "vsz_kb": 0, "vmpeak_kb": 0,
              "data_kb": 0, "threads": 0, "rss_anon_kb": 0, "rss_file_kb": 0,
              "rss_shmem_kb": 0}
    try:
        with open(f"/proc/{pid}/status") as fp:
            for line in fp:
                key, sep, rest = line.partition(":")
                column = STATUS_FIELDS.get(key) if sep else None
                if column is None:
                    continue
                rest = rest.strip()
                # process names may contain commas and spaces; keep the CSV sane
                values[column] = rest.replace(",", "_") if column == "command" \
                    else int(rest.split()[0])
    except (OSError, ValueError):
        return {}
    return values


def read_pss_kb(pid: int) -> int:
    """Pss from smaps_rollup (Linux 4.14+) — a single fast read, 0 if absent."""
    try:
        with open(f"/proc/{pid}/smaps_rollup") as fp:
            for line in fp:
                if line.startswith("Pss:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        return 0
    return 0


def read_meminfo() -> dict:
    wanted = ("MemTotal", "MemAvailable", "Shmem", "SwapTotal", "SwapFree")
    info = dict.fromkeys(wanted, 0)
    with open("/proc/meminfo") as fp:
        for line in fp:
            key, sep, rest = line.partition(":")
            if sep and key in info:
                info[key] = int(rest.split()[0])
    return info


def read_loadavg() -> tuple[str, str, str]:
    with open("/proc/loadavg") as fp:
        fields = fp.read().split()
    return fields[0], fields[1], fields[2]


def read_cgroup_kb(cgroup_file: str) -> str:
    if not cgroup_file:
        return ""
    with open(cgroup_file) as fp:
        return str(int(fp.read().split()[0]) // 1024)


class Monitor:
    """Samples this node into ``memory_<jobid>_<node>.csv`` until told to stop."""

    def __init__(self, outdir: Path, jobid: str, interval: float, pattern: str,
                 threads: int = 1):
        self.outdir = Path(outdir)
        self.jobid = jobid
        self.interval = interval
        self.pattern = re.compile(pattern)
        self.threads = max(1, threads)
        self.node = socket.gethostname().split(".")[0]
        self.uid = os.getuid()
        self.cgroup_file = find_cgroup_file(jobid)
        self.process_log = self.outdir / f"memory_{jobid}_{self.node}.csv"
        self.node_log = self.outdir / f"node_memory_{jobid}_{self.node}.csv"
        self.stop_file = stop_file_path(self.outdir, jobid)
        # reading Pss is the only expensive step; spread it over threads when asked
        self.pool = ThreadPoolExecutor(self.threads) if self.threads > 1 else None

    def banner(self) -> str:
        return "\n".join([
            "============================================================",
            "MEMORY MONITOR",
            "============================================================",
            f"Node       : {self.node}",
            f"Job ID     : {self.jobid}",
            f"Interval   : {self.interval} s",
            f"Pss threads: {self.threads}",
            f"Pattern    : {self.pattern.pattern}",
            f"Process log: {self.process_log}",
            f"Node log   : {self.node_log}",
            f"Cgroup file: {self.cgroup_file or 'not found'}",
            "============================================================",
        ])

    def _sample_processes(self, ts: str, elapsed: float,
                          writer: Any) -> tuple[int, int, int, int]:
        """Append one row per matching process and return
        (n_procs, sum_rss, sum_pss, sum_rss_anon) for the node log.

        The cheap /proc/<pid>/status reads happen first, then the expensive Pss
        reads go through the thread pool (if any) so they overlap instead of
        adding up.
        """
        targets = []
        for pid in user_pids(self.uid):
            status = read_status(pid)
            command = status.get("command", "")
            if not command or not self.pattern.search(command):
                continue
            if status["rss_kb"] <= 0:
                continue
            targets.append((pid, status))

        if self.pool is None:
            pss_values = [read_pss_kb(pid) for pid, _ in targets]
        else:
            pss_values = list(self.pool.map(read_pss_kb, [pid for pid, _ in targets]))

        n_procs = total_rss = total_pss = total_anon = 0
        for (pid, status), pss in zip(targets, pss_values):
            rss = status["rss_kb"]
            writer.writerow([
                ts, self.node, pid, status["ppid"], status["command"], rss, pss,
                status["vsz_kb"], status["vmpeak_kb"], status["data_kb"],
                status["threads"], status["rss_anon_kb"], status["rss_file_kb"],
                status["rss_shmem_kb"], f"{elapsed:.3f}",
            ])
            n_procs += 1
            total_rss += rss
            total_pss += pss
            total_anon += status["rss_anon_kb"]
        return n_procs, total_rss, total_pss, total_anon

    def _sample_node(self, ts: str, elapsed: float, writer: Any,
                     totals: tuple[int, int, int, int]) -> None:
        info = read_meminfo()
        load1, load5, load15 = read_loadavg()
        used = info["MemTotal"] - info["MemAvailable"]
        percent = 100 * used / info["MemTotal"] if info["MemTotal"] > 0 else 0
        writer.writerow([
            ts, self.node, info["MemTotal"], info["MemAvailable"], used,
            f"{percent:.2f}", read_cgroup_kb(self.cgroup_file),
            info["SwapTotal"] - info["SwapFree"], info["Shmem"], load1, load5,
            load15, *totals, f"{elapsed:.3f}",
        ])

    def run(self) -> None:
        self.outdir.mkdir(parents=True, exist_ok=True)
        print(self.banner())

        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT, _on_signal)

        interval_ms = int(self.interval * 1000 + 0.5)
        t0_ms = next_ms = _now_ms()

        with self.process_log.open("w", newline="") as proc_fp, \
                self.node_log.open("w", newline="") as node_fp:
            proc_writer = csv.writer(proc_fp, lineterminator="\n")
            node_writer = csv.writer(node_fp, lineterminator="\n")
            proc_writer.writerow(PROCESS_COLUMNS)
            node_writer.writerow(NODE_COLUMNS)
            proc_fp.flush()
            node_fp.flush()

            try:
                while not self.stop_file.exists():
                    ts = _timestamp()
                    elapsed = (_now_ms() - t0_ms) / 1000
                    totals = self._sample_processes(ts, elapsed, proc_writer)
                    proc_fp.flush()
                    self._sample_node(ts, elapsed, node_writer, totals)
                    node_fp.flush()

                    # sleep until the next sample, without drifting
                    next_ms += interval_ms
                    wait = (next_ms - _now_ms()) / 1000
                    if wait > 0:
                        time.sleep(wait)
                    else:
                        next_ms = _now_ms()
            except _Interrupted:
                pass  # SIGTERM/SIGINT: leave the CSVs as they are
            finally:
                if self.pool is not None:
                    self.pool.shutdown()

        print(f"Memory monitor stopped: {self.process_log}, {self.node_log}")


def parse_args(argv: "list[str] | None" = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m taurex_memory_profiler.monitor",
        description="Sample this node and every process of the user into two CSVs.",
    )
    parser.add_argument("-o", "--outdir", default=os.environ.get("MEM_OUTDIR", "memory_logs"),
                        help="directory for the CSVs [memory_logs]")
    parser.add_argument("-i", "--interval", type=float,
                        default=float(os.environ.get("MEM_INTERVAL", 5)),
                        help="sampling interval in seconds, may be < 1 [5]")
    parser.add_argument("-p", "--pattern", default=os.environ.get("MEM_PATTERN", "."),
                        help="only track processes whose name matches this regex; "
                             "use e.g. 'taurex|prterun' to reduce noise [.]")
    parser.add_argument("-j", "--jobid", default=os.environ.get("SLURM_JOB_ID", "local"),
                        help="job id used in the file names [$SLURM_JOB_ID or local]")
    parser.add_argument("-t", "--threads", type=int,
                        default=int(os.environ.get("MEM_THREADS", 1)),
                        help="threads reading Pss in parallel; >1 needs the matching "
                             "CPUs free on the node [1]")
    return parser.parse_args(argv)


def main(argv: "list[str] | None" = None) -> None:
    args = parse_args(argv)
    Monitor(args.outdir, args.jobid, args.interval, args.pattern,
            args.threads).run()


if __name__ == "__main__":
    sys.exit(main())
