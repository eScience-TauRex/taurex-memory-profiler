#!/usr/bin/env python3
"""Self-contained checks for the plots of the memory profiler.

Run directly (no pytest needed):

    python tests/test_plot.py

or, if pytest is available:

    pytest tests/test_plot.py
"""

from __future__ import annotations

import csv
import datetime as dt
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _helpers import PLOT, SRC, call  # noqa: E402  (needs the path tweak above)


def _fmt(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S") + f".{t.microsecond // 1000:03d}"


def write_unified(logdir: Path, job="12345", nodes=("n1", "n2"), ranks=4,
                  samples=15, interval=5, pss=True, label="good"):
    """Write logs in the unified format of taurex_memory_profiler.monitor."""
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
    cmd = [*PLOT, *[str(a) for a in args]]
    proc = call(cmd, cwd=cwd)
    if proc.returncode != 0:
        raise AssertionError(f"{' '.join(cmd)} failed:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


def test_unified_overview():
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp) / "memory_logs"
        write_unified(logs)
        out = run_plot("--logdir", logs)
        for name in ("memory_good.png", "memory_good_total.png"):
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
        cmd = [*PLOT, "--logdir", str(logs), "--job", "nope"]
        proc = call(cmd)
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


def test_oom_from_exit_code():
    """A SIGKILLed run is marked OOM even without a Slurm 'oom_kill' line."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        logs = tmp / "memory_logs"
        write_unified(logs, job="12345", label="mem_64_oom")
        (tmp / "slurm-12345.out").write_text(
            "Job Finished: 2026-01-01T12:10:00+00:00\n"
            "Exit code: 137   Runtime: 59 s\n")
        out = run_plot("--logdir", logs, "--no-overview", "--no-total",
                       "--output-dir", logs)
        assert "OOM killed: yes" in out, out

        # a clean exit is not marked
        (tmp / "slurm-12345.out").write_text("Exit code: 0   Runtime: 59 s\n")
        out = run_plot("--logdir", logs, "--no-overview", "--no-total",
                       "--output-dir", logs)
        assert "OOM killed: no" in out, out


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

        cmd = [*PLOT, "--logdir", str(logs), "--select", "nope*"]
        proc = call(cmd)
        assert proc.returncode != 0
        assert "matches" in proc.stderr


def test_compare_by_run_name():
    """Two runs in one directory are compared by name, no job ids needed."""
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp) / "memory_logs"
        write_unified(logs, job="111", label="mem_64_good")
        write_unified(logs, job="222", label="mem_64_bad")

        # the two names at once, and as the first / second run
        out = run_plot("--logdir", logs, "--compare", "mem_64_good", "mem_64_bad")
        assert (logs / "compare_mem_64_good_vs_mem_64_bad.png").is_file(), out

        out = run_plot("mem_64_good", "--compare", "mem_64_bad", "--logdir", logs)
        assert (logs / "compare_mem_64_good_vs_mem_64_bad.png").is_file(), out

        # a name that is neither a file nor a known run fails clearly
        proc = call([*PLOT, "--logdir", str(logs), "--compare", "mem_64_nope"])
        assert proc.returncode != 0
        assert "mem_64_nope" in proc.stderr and "available" in proc.stderr


def test_output_check_same_and_different():
    """--compare checks that the compared runs wrote the same output."""
    try:
        import h5py
        import numpy as np
    except ImportError:
        print("SKIP test_output_check_same_and_different (no h5py)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        logs = tmp / "memory_logs"
        write_unified(logs, job="111", label="mem_64_good")
        write_unified(logs, job="222", label="mem_64_bad")
        for job, scale in (("111", 1.0), ("222", 1.0000001)):
            with h5py.File(tmp / f"out_{job}.hdf5", "w") as handle:
                handle["profile"] = np.linspace(0.0, scale, 50)
                handle["model"] = "tau"
        # the -o of the recorded command is found one directory above the logs
        (logs / "run_111.meta").write_text(
            "label=mem_64_good\njobid=111\ncommand=taurex -o out_111.hdf5\n")
        (logs / "run_222.meta").write_text(
            "label=mem_64_bad\njobid=222\ncommand=taurex -o out_222.hdf5\n")

        out = run_plot("--logdir", logs, "--compare", "mem_64_good", "mem_64_bad",
                       "--report", logs / "report.md",
                       "--output-dir", logs)
        assert "Output check (baseline mem_64_good)" in out, out
        assert "same (2 datasets" in out, out
        assert (logs / "outputs_mem_64_good_vs_mem_64_bad.png").is_file()
        assert "## Output check" in (logs / "report.md").read_text()

        # one dataset outside the tolerance is reported with its values
        with h5py.File(tmp / "out_222.hdf5", "w") as handle:
            handle["profile"] = np.linspace(0.0, 2.0, 50)
            handle["model"] = "tau"
        proc = call([*PLOT, "--logdir", str(logs), "--compare",
                     "mem_64_good", "mem_64_bad", "--no-overview", "--no-total",
                     "--output-dir", str(logs)])
        assert proc.returncode == 0, proc.stderr
        assert "DIFFERENT" in proc.stdout and "profile" in proc.stdout, proc.stdout

        # a plain --check-output applies to every run, a LABEL= one to that run
        out = run_plot("--logdir", logs, "--compare", "mem_64_good", "mem_64_bad",
                       "--check-output", "out_111.hdf5",
                       "--check-output", "mem_64_bad=out_111.hdf5",
                       "--no-overview", "--no-total", "--output-dir", logs)
        assert "same (2 datasets" in out, out

        # --no-check-output leaves the comparison alone
        out = run_plot("--logdir", logs, "--compare", "mem_64_good", "mem_64_bad",
                       "--no-check-output", "--no-overview", "--no-total",
                       "--output-dir", logs)
        assert "Output check" not in out, out

        # a run killed before it finished writing leaves a truncated HDF5 file:
        # that is reported as a note, the comparison does not die on it
        with h5py.File(tmp / "out_222.hdf5", "w") as handle:
            handle["profile"] = np.linspace(0.0, 1.0, 50)
        with (tmp / "out_222.hdf5").open("r+b") as handle:
            handle.truncate(96)
        proc = call([*PLOT, "--logdir", str(logs), "--compare",
                     "mem_64_good", "mem_64_bad", "--no-overview", "--no-total",
                     "--report", str(logs / "report.md"), "--output-dir", str(logs)])
        assert proc.returncode == 0, proc.stderr
        assert "unreadable output" in proc.stdout, proc.stdout
        assert "out_111.hdf5 vs out_222.hdf5" in proc.stdout, proc.stdout
        assert "unreadable output" in (logs / "report.md").read_text()


def test_output_check_figure_shows_the_values():
    """The output-check figure overlays the quantities, it is not a text panel.

    An array dataset (a profile, an SED) must be drawn once per run so the
    values can be read against each other, and the scalar parameters must get a
    parity panel instead of a paragraph.
    """
    try:
        import h5py
        import matplotlib
        import numpy as np
    except ImportError:
        print("SKIP test_output_check_figure_shows_the_values (no deps)")
        return
    matplotlib.use("Agg")

    sys.path.insert(0, str(SRC))
    from taurex_memory_profiler import plot as plotmod
    from taurex_memory_profiler.outputs import compare_runs

    class FakeRun:  # compare_runs only reads these three attributes
        def __init__(self, source, meta, label):
            self.source, self.meta, self.label = source, meta, label

    def check_of(tmp: Path, name: str, write_a, write_b, rtol: float):
        for stem, writer in (("a", write_a), ("b", write_b)):
            with h5py.File(tmp / f"{name}_{stem}.hdf5", "w") as handle:
                writer(handle, np)
        runs = [FakeRun(str(tmp), {"label": "good",
                                   "command": f"taurex -o {name}_a.hdf5"}, "good"),
                FakeRun(str(tmp), {"label": "bad",
                                   "command": f"taurex -o {name}_b.hdf5"}, "bad")]
        checks = compare_runs(runs, {}, rtol=rtol, atol=0.0)
        assert checks and checks[0].kind == "hdf5", checks
        return checks[0]

    captured: list = []
    plotmod.save_fig = lambda fig, base: captured.append(fig)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # a) a profile that moved a little and one scalar we can point at
        def small_a(handle, np):
            handle["profile"] = np.linspace(1.0, 2.0, 40)
            handle["temperature"] = np.float64(1500.0)

        def small_b(handle, np):
            handle["profile"] = np.linspace(1.0, 2.0, 40) * (1 + 1e-5)
            handle["temperature"] = np.float64(1500.5)

        check = check_of(tmp, "small", small_a, small_b, rtol=1e-2)
        plotmod.plot_output_check(check, tmp / "fig_small", 1e-2, "good")
        fig = captured[-1]
        overlay = [ax for ax in fig.axes if len(ax.lines) >= 2]
        parity = [ax for ax in fig.axes if ax.collections]
        assert overlay, "the profile must be overlaid from both runs"
        assert parity, "the scalars must get a parity panel"
        assert len(overlay[0].lines) == 2, "baseline and the other run"

        # b) the profile changed length: the values are still overlaid and the
        #    mismatch is stated, it is not a text-only figure
        def shape_a(handle, np):
            handle["profile"] = np.zeros(50)
            handle["temperature"] = np.float64(1500.0)

        def shape_b(handle, np):
            handle["profile"] = np.zeros(12)
            handle["temperature"] = np.float64(1500.0)

        check = check_of(tmp, "shape", shape_a, shape_b, rtol=1e-6)
        assert not check.same, check
        plotmod.plot_output_check(check, tmp / "fig_shape", 1e-6, "good")
        fig = captured[-1]
        assert [ax for ax in fig.axes if len(ax.lines) >= 2], "still overlaid"
        blob = " ".join(t.get_text() for ax in fig.axes for t in ax.texts)
        assert "different lengths" in blob, blob
        assert "structural" in blob, blob

    import matplotlib.pyplot as plt
    for fig in captured:
        plt.close(fig)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"OK  {test.__name__}")
    print(f"\nAll {len(tests)} checks passed.")


if __name__ == "__main__":
    main()
