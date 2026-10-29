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
import os
import sys
import tempfile
import time
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


def test_labels_option_names_the_compared_runs():
    """--labels gives one label per run, in the order the runs are given."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        write_legacy(tmp / "mem_64.csv")
        write_legacy(tmp / "mem_64_bad.csv")
        write_legacy(tmp / "mem_64_f32.csv")
        out = run_plot(tmp / "mem_64.csv", "--compare", tmp / "mem_64_bad.csv",
                       tmp / "mem_64_f32.csv",
                       "--labels", "OOM branch", "original taurex3", "float32",
                       "--output-dir", tmp)
        assert (tmp / "compare_OOM_branch_vs_original_taurex3_vs_float32.png").is_file(), out
        # the labels reach the figures of the single runs and the peak table
        assert (tmp / "memory_OOM_branch_total.png").is_file(), out
        assert "OOM branch" in out and "float32" in out, out

        # the label written into the run itself wins over --labels
        out = run_plot(f"fixed={tmp / 'mem_64.csv'}", "--compare", tmp / "mem_64_bad.csv",
                       "--labels", "ignored", "OOM branch", "--output-dir", tmp)
        assert (tmp / "compare_fixed_vs_OOM_branch.png").is_file(), out

        # more labels than runs is a mistake, not a silently ignored label
        proc = call([*PLOT, str(tmp / "mem_64.csv"), "--compare", str(tmp / "mem_64_bad.csv"),
                     "--labels", "a", "b", "c", "--output-dir", str(tmp)])
        assert proc.returncode != 0
        assert "3 labels for 2 runs" in proc.stderr


def test_job_is_a_run_of_the_comparison():
    """--job names the first run even when only --compare runs are listed."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        logs = Path(tmp) / "memory_logs"
        for job, label in (("111", "one"), ("222", "two"), ("333", "three")):
            write_unified(logs, job=job, label=label)
        out = run_plot("--logdir", logs, "--job", "111", "--compare", "222", "333",
                       "--labels", "A", "B", "C", "--output-dir", tmp)
        assert (tmp / "compare_A_vs_B_vs_C.png").is_file(), out
        for label in ("A", "B", "C"):
            assert (tmp / f"memory_{label}_total.png").is_file(), (label, out)


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


def test_results_comparison_overlays_the_runs():
    """Every comparison figure holds all the runs on one axes, one curve per run.

    The comparison is not a grid of the plotter's pages: the arrays are read from
    the outputs, so the spectrum figure of two runs holds two model curves on the
    same axis next to the single observation, and each fitted parameter gets one
    histogram per run.
    """
    try:
        import matplotlib
        import numpy as np
    except ImportError:
        print("SKIP test_results_comparison_overlays_the_runs (no deps)")
        return
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    sys.path.insert(0, str(SRC))
    from taurex_memory_profiler import plot as plotmod

    def fake(scale: float, temperature: float) -> dict:
        wl = np.linspace(1.0, 2.0, 12)
        shape = np.linspace(1.0, 1.2, 12)
        pressure = np.logspace(-6.0, 1.0, 20)
        samples = np.linspace(temperature - 5.0, temperature + 5.0, 50)
        return {
            "model": "TransmissionModel",
            "wlgrid": wl,
            "spectrum": scale * shape,
            "sources": {"Absorption": 0.9 * scale * shape,
                        "Rayleigh": 0.1 * scale * shape},
            "pressure": pressure,
            "temperature": np.full(pressure.size, temperature),
            "temperature_std": np.full(pressure.size, 5.0),
            "gases": {"H2O": np.full(pressure.size, 1e-4),
                      "CH4": np.full(pressure.size, 1e-5)},
            "observed": {"wlgrid": wl, "spectrum": 0.02 * shape,
                         "errorbars": np.full(wl.size, 1e-4)},
            "names": ["T"],
            "latex": ["$T$"],
            "tracedata": samples.reshape(-1, 1),
            "weights": np.ones(samples.size),
        }

    saved_fig = plotmod.save_fig
    captured: list = []
    plotmod.save_fig = lambda fig, base: captured.append((fig, base))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            results = {"a": fake(0.02, 350.0), "b": fake(0.021, 360.0)}
            for name, plot in (("spectra", plotmod.plot_spectrum),
                               ("contributions", plotmod.plot_contributions),
                               ("tp_profiles", plotmod.plot_tp_profile),
                               ("mixratios", plotmod.plot_mixratios),
                               ("posteriors", plotmod.plot_posteriors)):
                plot(results, Path(tmp) / f"compare_{name}")
    finally:
        plotmod.save_fig = saved_fig

    assert [base.name for _fig, base in captured] == [
        "compare_spectra", "compare_contributions", "compare_tp_profiles",
        "compare_mixratios", "compare_posteriors"]

    spectrum = captured[0][0].axes[0]
    # one observation + one model curve per run, on the same axis
    assert len(spectrum.lines) == 3, [line.get_label() for line in spectrum.lines]
    assert set(text.get_text() for text in spectrum.get_legend().get_texts()) == \
        {"Observed", "a", "b"}
    assert len(captured[1][0].axes[0].lines) == 4, "2 sources x 2 runs"
    assert len(captured[2][0].axes[0].lines) == 2
    mixing = captured[3][0].axes[0]
    assert len(mixing.lines) == 4, "2 gases x 2 runs"
    # colour = gas (the legend of the axes), line style = run (an extra artist)
    assert [text.get_text() for text in mixing.get_legend().get_texts()] == ["H2O", "CH4"]
    run_legend = [artist for artist in mixing.artists
                  if hasattr(artist, "get_texts")][0]
    assert [text.get_text() for text in run_legend.get_texts()] == ["a", "b"]
    # one histogram per run in the posterior of the one fitted parameter
    posteriors = captured[4][0]
    legend = next(ax.get_legend() for ax in posteriors.axes if ax.get_legend() is not None)
    assert [text.get_text() for text in legend.get_texts()] == ["a", "b"]
    # every results figure names the runs in its title, not only in the legend
    for fig, base in captured:
        texts = " ".join(text.get_text() for text in fig.texts)
        assert "— a, b" in texts, (base.name, texts)
    plt.close("all")


def test_finished_output_falls_back_for_a_light_run():
    """A --light -o is replaced by the finished chain that has the same name."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        logs = tmp / "memory_logs"
        logs.mkdir()
        (tmp / "output_run.hdf5").write_bytes(b"light")
        (tmp / "tmp_postprocess").mkdir()
        finished = tmp / "tmp_postprocess" / "output_run.hdf5"
        finished.write_bytes(b"finished")

        sys.path.insert(0, str(SRC))
        from taurex_memory_profiler import plot as plotmod

        run = plotmod.Run(label="run", source=str(logs))
        assert plotmod.finished_output(run, tmp / "output_run.hdf5") == finished
        # no finished counterpart: nothing is substituted
        (tmp / "other.hdf5").write_bytes(b"light")
        assert plotmod.finished_output(run, tmp / "other.hdf5") is None


def test_read_result_of_a_light_output():
    """An output without the forward 'Output' group has no results to draw."""
    try:
        import h5py
        import numpy as np
    except ImportError:
        print("SKIP test_read_result_of_a_light_output (no h5py)")
        return

    sys.path.insert(0, str(SRC))
    from taurex_memory_profiler import plot as plotmod

    with tempfile.TemporaryDirectory() as tmp:
        light = Path(tmp) / "light.hdf5"
        with h5py.File(light, "w") as handle:
            handle["ModelParameters/model_type"] = np.bytes_(b"TransmissionModel")
        assert plotmod.read_result(light) is None

        # the groups the comparison reads, one solution deep
        done = Path(tmp) / "done.hdf5"
        with h5py.File(done, "w") as handle:
            handle["ModelParameters/model_type"] = np.bytes_(b"TransmissionModel")
            handle["ModelParameters/Chemistry/active_gases"] = np.array(
                [b"H2O"], dtype="S3")
            handle["Observed/wlgrid"] = np.linspace(1.0, 2.0, 4)
            handle["Observed/spectrum"] = np.full(4, 0.02)
            handle["Observed/errorbars"] = np.full(4, 1e-4)
            solution = handle.create_group("Output/Solutions/solution0")
            spectra = solution.create_group("Spectra")
            spectra["binned_wlgrid"] = np.linspace(1.0, 2.0, 4)
            spectra["binned_spectrum"] = np.full(4, 0.02)
            spectra["Contributions/Rayleigh/binned_spectrum"] = np.full(4, 0.01)
            profiles = solution.create_group("Profiles")
            profiles["pressure_profile"] = np.logspace(1.0, 5.0, 3)
            profiles["temp_profile"] = np.full(3, 350.0)
            profiles["temp_profile_std"] = np.full(3, 5.0)
            profiles["active_mix_profile"] = np.full((1, 3), 1e-4)

        result = plotmod.read_result(done)
        assert result["model"] == "TransmissionModel"
        assert list(result["sources"]) == ["Rayleigh"]
        assert list(result["gases"]) == ["H2O"]
        assert result["pressure"][-1] == 1.0, "Pa -> bar"
        assert result["observed"]["spectrum"].size == 4


def test_duplicate_run_names_ask_before_deleting():
    """Two runs with one name: the plotter keeps both with a note when nobody
    can be asked, and drops the older one on --delete-old."""
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp) / "memory_logs"
        write_unified(logs, job="111", label="twice")
        write_unified(logs, job="222", label="twice")

        # no terminal: both runs stay
        out = run_plot("--logdir", logs, "--select", "twice", "--output-dir", logs)
        assert "2 runs are labelled 'twice'" in out, out
        assert (logs / "run_111.meta").exists() and (logs / "run_222.meta").exists()

        # the older run is the one written first, and only it goes
        out = run_plot("--logdir", logs, "--select", "twice", "--output-dir", logs,
                       "--delete-old")
        assert "Deleted the old run 111" in out, out
        assert not (logs / "run_111.meta").exists()
        assert not list(logs.glob("*_111_*.csv"))
        assert (logs / "run_222.meta").exists()
        assert (logs / "memory_twice.png").is_file()


def test_duplicate_names_are_found_when_one_run_is_picked():
    """Plotting only the newest run of a name still finds the older namesake:
    its figures are the ones that would be overwritten."""
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp) / "memory_logs"
        write_unified(logs, job="111", label="twice")
        write_unified(logs, job="222", label="twice")
        old = time.time() - 600
        for path in logs.glob("*_111_*"):
            os.utime(path, (old, old))

        # no --select and no --job: the newest job alone, whose name is shared
        out = run_plot("--logdir", logs, "--output-dir", logs, "--delete-old")
        assert "Deleted the old run 111" in out, out
        assert not (logs / "run_111.meta").exists()
        assert not list(logs.glob("*_111_*.csv"))
        assert (logs / "run_222.meta").exists()
        assert (logs / "memory_twice.png").is_file()


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"OK  {test.__name__}")
    print(f"\nAll {len(tests)} checks passed.")


if __name__ == "__main__":
    main()
