#!/usr/bin/env python3
"""Self-contained checks for the memory toolbox.

Run directly (no pytest needed):

    python tests/test_plot_memory.py

or, if pytest is available:

    pytest tests/test_plot_memory.py
"""

from __future__ import annotations

import csv
import datetime as dt
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLOT = HERE.parent / "plot_memory.py"
MEM_RUN = HERE.parent / "mem-run"
MONITOR = HERE.parent / "memory_monitor.sh"


def _fmt(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S") + f".{t.microsecond // 1000:03d}"


def write_unified(logdir: Path, job="12345", nodes=("n1", "n2"), ranks=4,
                  samples=15, interval=5, pss=True, label="good"):
    """Write logs in the unified memory_monitor.sh format."""
    logdir.mkdir(parents=True, exist_ok=True)
    t0 = dt.datetime(2026, 1, 1, 12, 0, 0)
    proc_cols = ["timestamp", "node", "pid", "ppid", "command", "rss_kb", "pss_kb",
                 "vsz_kb", "vmpeak_kb", "data_kb", "threads", "rss_anon_kb",
                 "rss_file_kb", "rss_shmem_kb", "elapsed_s"]
    node_cols = ["timestamp", "node", "mem_total_kb", "mem_available_kb", "mem_used_kb",
                 "mem_used_percent", "cgroup_mem_kb", "swap_used_kb", "shmem_kb",
                 "load1", "load5", "load15", "user_procs", "user_rss_kb",
                 "user_pss_kb", "user_rss_anon_kb", "elapsed_s"]
    for n in nodes:
        with (logdir / f"memory_{job}_{n}.csv").open("w", newline="") as fp:
            w = csv.writer(fp)
            w.writerow(proc_cols)
            for s in range(samples):
                t = t0 + dt.timedelta(seconds=interval * s)
                for r in range(ranks):
                    rss = 200_000 + 1_000 * s + 100 * r
                    pss = rss // 2
                    w.writerow([_fmt(t), n, 1000 + r, 999, "taurex", rss, pss,
                                3_000_000, 3_100_000, 500_000, 1, rss - 1000, 1000, 0,
                                float(interval * s)])
        with (logdir / f"node_memory_{job}_{n}.csv").open("w", newline="") as fp:
            w = csv.writer(fp)
            w.writerow(node_cols)
            for s in range(samples):
                t = t0 + dt.timedelta(seconds=interval * s)
                used = 1_000_000 + 5_000 * s
                w.writerow([_fmt(t), n, 16_000_000, 16_000_000 - used, used,
                            round(used / 160_000, 2), used - 1000, 0, 4000,
                            1.0, 1.0, 1.0, ranks, used, used // 2, used - 1000,
                            float(interval * s)])
    (logdir / f"run_{job}.meta").write_text(f"label={label}\njobid={job}\nnodes=2\n")


def write_colleague(logdir: Path, job="777", node="n1", ranks=4, samples=10, interval=5):
    """Write logs in the colleague's rss-only format (no pss, no ms)."""
    logdir.mkdir(parents=True, exist_ok=True)
    t0 = dt.datetime(2026, 2, 1, 9, 0, 0)
    with (logdir / f"memory_{job}_{node}.csv").open("w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["timestamp", "hostname", "pid", "ppid", "command", "rss_kb",
                    "vsz_kb", "shared_kb", "data_kb", "threads", "rss_anon_kb",
                    "rss_file_kb", "elapsed_s"])
        for s in range(samples):
            t = (t0 + dt.timedelta(seconds=interval * s)).strftime("%Y-%m-%d %H:%M:%S")
            for r in range(ranks):
                rss = 300_000 + 2_000 * s
                w.writerow([t, node, 2000 + r, 1999, "taurex", rss, 4_000_000, 0,
                            700_000, 1, rss, 0, interval * s])
    with (logdir / f"node_memory_{job}_{node}.csv").open("w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["timestamp", "hostname", "mem_total_kb", "mem_available_kb",
                    "mem_used_kb", "mem_used_percent", "load1", "load5", "load15",
                    "shmem_kb", "cgroup_mem_kb", "user_procs", "user_rss_kb",
                    "user_rss_anon_kb", "elapsed_s"])
        for s in range(samples):
            t = (t0 + dt.timedelta(seconds=interval * s)).strftime("%Y-%m-%d %H:%M:%S")
            used = 2_000_000 + 10_000 * s
            w.writerow([t, node, 16_000_000, 16_000_000 - used, used,
                        round(used / 160_000, 2), 1.0, 1.0, 1.0, 4000, used,
                        ranks, used, used, interval * s])


def write_legacy(path: Path, label="64", ranks=4, samples=12, killed_run=False):
    """Write a legacy mem_<config>.csv (epoch timestamps, Pss and RSS)."""
    t0 = 1_700_000_000.0
    with path.open("w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["timestamp", "node", "pid", "rss_kb", "rss_mb", "pss_kb", "pss_mb",
                    "vmsize_kb", "vmsize_mb", "vmpeak_kb", "vmpeak_mb", "comm"])
        for s in range(samples):
            t = t0 + 5 * s
            for r in range(ranks):
                rss = 400_000 + 3_000 * s + 50 * r
                pss = rss // 2
                w.writerow([round(t, 3), "n1", 3000 + r, rss, rss // 1024, pss,
                            pss // 1024, 5_000_000, 4882, 5_100_000, 4980, "taurex"])


def run_plot(*args, cwd=None):
    cmd = [sys.executable, str(PLOT), *[str(a) for a in args]]
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"{' '.join(cmd)} failed:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


def test_unified_overview():
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp) / "memory_logs"
        write_unified(logs)
        out = run_plot("--logdir", logs)
        for name in ("memory_good.png", "memory_good.pdf", "memory_good_total.png"):
            assert (logs / name).is_file(), f"missing {name}\n{out}"
        assert "peak total Pss" in out


def test_per_node_and_top():
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp) / "memory_logs"
        write_unified(logs)
        run_plot("--logdir", logs, "--per-node", "--top", "3")
        assert (logs / "memory_good_per_node.png").is_file()
        assert (logs / "memory_good_top3.png").is_file()


def test_compare_three_way():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        write_legacy(tmp / "mem_64.csv")
        write_legacy(tmp / "mem_64_bad.csv")
        write_legacy(tmp / "mem_64_float32.csv")
        out = run_plot(tmp / "mem_64.csv", "--compare", tmp / "mem_64_bad.csv",
                       "--compare2", tmp / "mem_64_float32.csv",
                       "--label-a", "OOM branch", "--label-b", "original taurex3",
                       "--label-c", "float32", "--output-dir", tmp)
        assert (tmp / "compare_OOM_branch_vs_original_taurex3_vs_float32.png").is_file(), out
        assert "peak:" in out


def test_figures_go_to_cwd_when_runs_span_directories():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_unified(root / "logs_good", job="201", label="good")
        write_unified(root / "logs_bad", job="202", label="bad")
        workdir = root / "work"
        workdir.mkdir()
        run_plot(f"good={root / 'logs_good'}", "--compare", f"bad={root / 'logs_bad'}",
                 cwd=workdir)
        assert (workdir / "compare_good_vs_bad.png").is_file()
        # nothing is dropped inside the log directories
        assert not list((root / "logs_good").glob("compare_*"))

        # a single directory keeps its figures next to the logs
        run_plot("--logdir", root / "logs_good", "--select", "*", cwd=workdir)
        assert list((root / "logs_good").glob("memory_good*.png"))


def test_colleague_format():
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp) / "memory_logs"
        write_colleague(logs)
        out = run_plot("--logdir", logs)
        assert (logs / "memory_777.png").is_file(), out
        assert "no Pss column" in out  # falls back to RSS


def test_labelled_spec_and_command_filter():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        write_unified(tmp / "logs_good")
        write_unified(tmp / "logs_bad", job="999")
        out = run_plot(f"good={tmp / 'logs_good'}", "--compare", f"bad={tmp / 'logs_bad'}",
                       "--command", "taurex", "--output-dir", tmp)
        assert (tmp / "compare_good_vs_bad.png").is_file(), out


def test_unknown_job_fails_cleanly():
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp) / "memory_logs"
        write_unified(logs)
        cmd = [sys.executable, str(PLOT), "--logdir", str(logs), "--job", "nope"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        assert proc.returncode != 0
        assert "No logs for job" in proc.stderr


def test_two_jobs_in_one_directory():
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp) / "memory_logs"
        write_unified(logs, job="111")
        write_unified(logs, job="222")
        out = run_plot(f"a={logs}#111", "--compare", f"b={logs}#222",
                       "--output-dir", logs)
        assert (logs / "compare_a_vs_b.png").is_file(), out
        assert "peak:" in out


def test_legacy_command_line_with_killed_flags():
    """The exact invocation used by the old plot_all_comparisons.sh script."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        write_legacy(tmp / "mem_64.csv")
        write_legacy(tmp / "mem_64_bad.csv")
        out = run_plot(tmp / "mem_64.csv", "--compare", tmp / "mem_64_bad.csv",
                       "--label-a", "OOM branch", "--label-b", "original taurex3",
                       "--title", "2 nodes 64 tasks",
                       "--killed-b", "--output-dir", tmp)
        assert (tmp / "compare_OOM_branch_vs_original_taurex3.png").is_file(), out
        assert "OOM killed" in out


def write_job_log(root: Path, stem: str, jobid: str = "12345") -> Path:
    """A Slurm log with the markers the job script and TauREx print."""
    path = root / f"{stem}_{jobid}.out"
    path.write_text(
        "================ Job Started: 2026-01-01T12:00:00+00:00 ================\n"
        "starting taurex\n"
        "PROGRAM START AT 2026-01-01 12:00:05\n"
        "Total Retrieval finish in 570.5 seconds\n"
        "Sampling time 500.2 s\n"
        "PROGRAM END AT 2026-01-01 12:09:50\n"
        "py-spy: Samples: 12345 Errors: 0\n"
        "================ Job Finished: 2026-01-01T12:10:00+00:00 ================\n"
    )
    return path


def write_sacct(root: Path, name: str = "sacct_12345.txt", jobid: str = "12345") -> Path:
    path = root / name
    path.write_text("JobID|Elapsed|MaxRSS|MaxVMSize|AveRSS\n"
                    f"{jobid}|00:20:00|2097152K|3145728K|1572864K\n")
    return path


def test_job_log_timing_and_report():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        logs = tmp / "memory_logs"
        write_unified(logs)
        write_job_log(logs, "slurm_good")
        report = tmp / "report.md"
        out = run_plot("--logdir", logs, "--report", report)
        assert "wall clock (job start to end): 10.0 min" in out, out
        assert "TauREx total: 9.8 min" in out, out
        assert "MultiNest sampling: 8.3 min" in out, out
        assert "py-spy CPU samples: 12345" in out, out
        text = report.read_text()
        assert "py-spy CPU samples" in text and "12345" in text
        assert "| 10.0 min |" in text and "peak total (GB)" in text


def test_sacct_fallback_and_compare():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        sacct = write_sacct(tmp)
        out = run_plot(sacct, "--output-dir", tmp)
        assert (tmp / "memory_sacct_12345_peak.png").is_file(), out
        assert "sacct: peak RSS 2.00 GB" in out, out
        assert "elapsed 20.0 min" in out, out

        write_legacy(tmp / "mem_64.csv")
        out = run_plot(tmp / "mem_64.csv", "--compare", sacct, "--output-dir", tmp)
        assert "sacct" in out, out
        assert (tmp / "compare_mem_64_vs_sacct_12345.png").is_file(), out


def test_mem_run_one_liner():
    """mem-run starts the sampler, runs the command and returns its exit code."""
    if not MEM_RUN.exists():
        return  # shell wrapper not shipped
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        env = dict(os.environ, MEM_RUN_WAIT="1", MEM_MONITOR=str(MONITOR))

        proc = subprocess.run(
            ["bash", str(MEM_RUN), "-i", "1", "-j", "4242", "-l", "oneliner",
             "--", "bash", "-c", "sleep 2"],
            cwd=tmp, env=env, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert (tmp / "memory_logs" / "run_4242.meta").is_file()
        assert list((tmp / "memory_logs").glob("memory_4242_*.csv")), proc.stdout
        assert list((tmp / "memory_logs").glob("node_memory_4242_*.csv")), proc.stdout
        assert "Job Started:" in proc.stdout and "Job Finished:" in proc.stdout
        assert "label=oneliner" in (tmp / "memory_logs" / "run_4242.meta").read_text()

        proc = subprocess.run(
            ["bash", str(MEM_RUN), "-i", "1", "-j", "4243",
             "--", "bash", "-c", "exit 3"],
            cwd=tmp, env=env, capture_output=True, text=True)
        assert proc.returncode == 3, proc.stdout + proc.stderr


def test_select_picks_a_whole_grid():
    """--select 'mem_64_*' plots all three variants of one node config."""
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp) / "memory_logs"
        for jobid, label in (("111", "mem_32_good"), ("222", "mem_64_good"),
                             ("333", "mem_64_bad"), ("444", "mem_64_float32"),
                             ("555", "mem_128_good")):
            write_unified(logs, job=jobid, label=label)

        out = run_plot("--logdir", logs, "--select", "mem_64_*", "--output-dir", logs)
        assert (logs / "compare_mem_64_good_vs_mem_64_bad_vs_mem_64_float32.png").is_file(), out
        assert "mem_128_good" not in out, out

        # matching by job id also works, and a bad pattern fails clearly
        out = run_plot("--logdir", logs, "--select", "*", "--report", logs / "grid.md")
        for label in ("mem_32_good", "mem_64_good", "mem_128_good"):
            assert label in out, out
        assert "## Memory" in (logs / "grid.md").read_text()

        cmd = [sys.executable, str(PLOT), "--logdir", str(logs), "--select", "nope*"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        assert proc.returncode != 0
        assert "matches" in proc.stderr


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"OK  {test.__name__}")
    print(f"\nAll {len(tests)} checks passed.")


if __name__ == "__main__":
    main()
