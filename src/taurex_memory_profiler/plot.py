#!/usr/bin/env python3
"""Unified TauREx memory profiler — plot and compare memory profiles.

Reads every memory-log format used in this project, so the old and the
new tools can be retired:

  * unified      memory_<job>_<node>.csv  +  node_memory_<job>_<node>.csv
                 (written by taurex_memory_profiler.monitor, Pss and RSS)
  * colleague's  memory_monitor.sh logs (the old shell sampler: rss only,
                 ISO timestamps)
  * legacy       mem_<config>.csv single-file Pss/RSS profiles

Run it without arguments to plot the latest job found in ./memory_logs.
Everything is auto-detected from the CSV headers.

Examples
--------
# latest job in ./memory_logs: overview + total
taurex-mem-plot

# a specific job, with the per-node and top-process breakdowns
taurex-mem-plot --job 1234567 --per-node --top 10

# one legacy CSV
taurex-mem-plot mem_64.csv

# compare runs (the legacy command line keeps working)
taurex-mem-plot mem_64.csv --compare mem_64_bad.csv \
    --label-a "OOM branch" --label-b "original taurex3" --title "2 nodes 64 tasks"

# compare two runs by name, straight out of the log directory
taurex-mem-plot --compare mem_64_good mem_64_bad

# several unified log directories, labelled
taurex-mem-plot good=logs_good --compare bad=logs_bad --compare2 f32=logs_f32

# two jobs that live in the same log directory, picked by job id
taurex-mem-plot mem_64_good=memory_logs#12345 \
    --compare mem_64_bad=memory_logs#12346
"""

from __future__ import annotations

import argparse
from fnmatch import fnmatch
import re
import sys
import warnings
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless — works over SSH
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .outputs import (checked_any, compare_runs, format_checks, report_lines,
                      structural_messages)

KB_PER_GB = 1024.0 ** 2
DEFAULT_LIMIT_GB = 224.0
DEFAULT_PROJECT_RANKS = 128

# Palette (kept identical to the previous plotter for consistent figures)
BLUE, ORANGE, AQUA, BAND = "#2a78d6", "#eb6834", "#1baf7a", "#cde2fb"
INK, INK2, MUTED, GRID, AXIS = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
OOM_RED = "#d62728"
CYCLE = ("#2a78d6", "#eb6834", "#1baf7a", "#8c564b", "#9467bd", "#e377c2")

# Column names used by the different generations of CSVs
RENAME = {
    "hostname": "node",
    "comm": "command",
    "name": "command",
    "shared_kb": "rss_shmem_kb",
    "vmsize_kb": "vsz_kb",
}
NUMERIC_COLS = (
    "pid", "ppid", "threads", "elapsed_s",
    "mem_used_percent", "load1", "load5", "load15",
)


# ----------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------

@dataclass
class Run:
    label: str
    source: str
    mem: str = "pss_kb"           # memory column selected for this run
    proc: pd.DataFrame | None = None   # one row per process per sample
    node: pd.DataFrame | None = None   # one row per node per sample
    meta: dict = field(default_factory=dict)
    t0: pd.Timestamp | None = None
    killed: bool = False
    timing: dict = field(default_factory=dict)   # wall clock / runtime / py-spy
    sacct: dict | None = None                    # peak memory from an sacct dump


def mem_label(col: str) -> str:
    return "Pss" if col.startswith("pss") else "RSS"


def gb(run: Run, values):
    """Convert memory values of `run` from their CSV unit to GB."""
    factor = 1024.0 if run.mem.endswith("_mb") else KB_PER_GB
    return np.asarray(values, dtype=float) / factor


# ----------------------------------------------------------------------
# Reading and normalising
# ----------------------------------------------------------------------

def parse_time(series: pd.Series) -> pd.Series:
    """Timestamps may be epoch seconds (legacy) or ISO strings (new)."""
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_datetime(series, unit="s")
    s = series.astype(str)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        t = pd.to_datetime(s, errors="coerce")
    if t.isna().all():
        t = pd.to_datetime(pd.to_numeric(s, errors="coerce"), unit="s")
    return t


def normalize(df: pd.DataFrame, node: str | None = None) -> pd.DataFrame:
    """Bring the different generations of CSVs onto a common column naming."""
    df = df.rename(columns={k: v for k, v in RENAME.items() if k in df.columns})
    if node is not None and "node" not in df.columns:
        df = df.assign(node=node)
    if "node" not in df.columns:
        df = df.assign(node="node")
    for col in df.columns:
        if col.endswith(("_kb", "_mb")) or col in NUMERIC_COLS:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def is_node_frame(df: pd.DataFrame) -> bool:
    cols = df.columns
    return ("mem_total_kb" in cols or "mem_used_kb" in cols) and not is_proc_frame(df)


def is_proc_frame(df: pd.DataFrame) -> bool:
    cols = df.columns
    return any(c in cols for c in ("rss_kb", "rss_mb", "pss_kb", "pss_mb"))


def pick_mem_col(df: pd.DataFrame, want_rss: bool) -> str:
    order = ["rss_kb", "rss_mb"] if want_rss else ["pss_kb", "pss_mb", "rss_kb", "rss_mb"]
    for col in order:
        if col in df.columns:
            return col
    raise SystemExit("No rss/pss column found in the process log.")


def load_csv(path: Path, node: str | None = None):
    """Return (proc_frame | None, node_frame | None) for one CSV."""
    df = pd.read_csv(path)
    if df.empty or "timestamp" not in df.columns:
        return None, None
    df = normalize(df, node=node)
    df["_t"] = parse_time(df["timestamp"])
    df = df[df["_t"].notna()]
    if df.empty:
        return None, None
    if is_node_frame(df):
        return None, df
    if is_proc_frame(df):
        return df, None
    return None, None


def read_meta(base: Path, jobid: str) -> dict:
    """Read the optional run_<jobid>.meta written by taurex-mem-run."""
    for name in (f"run_{jobid}.meta", "run.meta"):
        path = base / name
        if path.is_file():
            meta = {}
            for line in path.read_text(errors="ignore").splitlines():
                key, sep, value = line.partition("=")
                if sep:
                    meta[key.strip()] = value.strip()
            meta.setdefault("jobid", jobid)
            return meta
    return {"jobid": jobid}


# ----------------------------------------------------------------------
# Discovery: build Run objects from files or directories
# ----------------------------------------------------------------------

NODE_RE = re.compile(r"^node_memory_(?P<job>.+?)_(?P<node>.+)\.csv$")
PROC_RE = re.compile(r"^memory_(?P<job>.+?)_(?P<node>.+)\.csv$")


def _log_root(path: Path) -> Path:
    return path / "memory_logs" if (path / "memory_logs").is_dir() else path


def discover_dir(path: Path, job: str | None = None,
                 select: str | None = None) -> list[Run]:
    """Build the Run objects of a log directory.

    Without `job`/`select` only the most recently modified job is used. `select`
    is an fnmatch pattern matched against the run labels (from
    ``run_<jobid>.meta``), which is how a whole grid is picked up at once.
    """
    base = _log_root(path)
    node_files: dict[str, dict[str, Path]] = {}
    proc_files: dict[str, dict[str, Path]] = {}
    for f in sorted(base.glob("node_memory_*.csv")):
        m = NODE_RE.match(f.name)
        if m:
            node_files.setdefault(m["job"], {})[m["node"]] = f
    for f in sorted(base.glob("memory_*.csv")):
        m = PROC_RE.match(f.name)
        if m:
            proc_files.setdefault(m["job"], {})[m["node"]] = f

    jobs = set(node_files) | set(proc_files)
    if not jobs:
        return []

    def _mtime(j):
        files = list(node_files.get(j, {}).values()) + list(proc_files.get(j, {}).values())
        return max(f.stat().st_mtime for f in files)

    # job id -> label, so the selection can match either one
    labels = {j: read_meta(base, j).get("label", j) for j in jobs}

    if select is not None and job is None:
        chosen = sorted((j for j in jobs
                         if fnmatch(labels[j], select) or fnmatch(j, select)),
                        key=_mtime)
        if not chosen:
            available = ", ".join(sorted(labels.values()))
            sys.exit(f"No run in {base} matches '{select}' (available: {available})")
    elif job:
        if job not in jobs:
            sys.exit(f"No logs for job '{job}' in {base} (found: {', '.join(sorted(jobs))})")
        chosen = [job]
    else:
        chosen = [max(jobs, key=_mtime)]

    runs = []
    for j in chosen:
        frames = []
        for node, f in sorted(proc_files.get(j, {}).items()):
            df, _ = load_csv(f, node=node)
            if df is not None:
                frames.append(df)
        proc = pd.concat(frames, ignore_index=True) if frames else None

        frames = []
        for node, f in sorted(node_files.get(j, {}).items()):
            _, df = load_csv(f, node=node)
            if df is not None:
                frames.append(df)
        node_df = pd.concat(frames, ignore_index=True) if frames else None

        meta = read_meta(base, j)
        runs.append(Run(label=meta.get("label", j), source=str(base), proc=proc, node=node_df, meta=meta))
    return runs


def discover_file(path: Path, label: str | None = None) -> list[Run]:
    run_label = label or path.stem
    if looks_like_sacct(path):
        stats = parse_sacct(path)
        if stats is None:
            sys.exit(f"{path}: looks like an sacct dump but has no MaxRSS/Elapsed values")
        return [Run(label=run_label, source=str(path), mem="rss_kb", sacct=stats)]
    proc, node_df = load_csv(path)
    if proc is None and node_df is None:
        sys.exit(f"{path}: not a memory log (no rss/pss or node memory columns)")
    return [Run(label=run_label, source=str(path), proc=proc, node=node_df)]


def build_runs(spec_path: Path, job: str | None, select: str | None = None) -> list[Run]:
    if spec_path.is_dir():
        return discover_dir(spec_path, job=job, select=select)
    return discover_file(spec_path)


# ----------------------------------------------------------------------
# Finalising a run: pick the memory column, filter, align time
# ----------------------------------------------------------------------

def finalize(run: Run, want_rss: bool, command: str | None):
    if run.proc is not None:
        if command is not None:
            available = sorted(run.proc["command"].dropna().unique())
            if command not in available:
                sys.exit(f"Process '{command}' not in {run.source}. Available: {', '.join(available)}")
            run.proc = run.proc[run.proc["command"] == command].copy()
            if run.proc.empty:
                sys.exit(f"Process '{command}' has no samples in {run.source}")
        run.mem = pick_mem_col(run.proc, want_rss)
        if run.mem.startswith("rss") and not want_rss:
            print(f"NOTE: {run.label}: no Pss column, plotting RSS instead")
        if run.proc[run.mem].max() <= 0:
            fallback = pick_mem_col(run.proc, want_rss=not run.mem.startswith("rss"))
            print(f"NOTE: {run.label}: '{run.mem}' is all zero, plotting '{fallback}' instead")
            run.mem = fallback

    times = []
    if run.proc is not None:
        times.append(run.proc["_t"].min())
    if run.node is not None:
        times.append(run.node["_t"].min())
    run.t0 = min(times) if times else None


def minutes_since(run: Run, times):
    """Minutes between `times` (Series or DatetimeIndex) and the run start."""
    return (pd.DatetimeIndex(times) - run.t0).total_seconds() / 60.0


def time_units(run: Run):
    """Return (unit label, divisor) for the time axis."""
    end = 0.0
    if run.proc is not None:
        end = max(end, (run.proc["_t"].max() - run.t0).total_seconds())
    if run.node is not None:
        end = max(end, (run.node["_t"].max() - run.t0).total_seconds())
    if end < 300:
        return "s", 1.0
    if end < 3 * 3600:
        return "min", 60.0
    return "h", 3600.0


# ----------------------------------------------------------------------
# Aggregations
# ----------------------------------------------------------------------

def per_node_time_sum(run: Run) -> pd.DataFrame:
    """Memory summed over the ranks of each node: index = time, columns = nodes."""
    df = run.proc
    table = df.groupby(["node", "_t"], observed=True)[run.mem].sum().unstack("node")
    return table.sort_index()


def total_series(run: Run) -> pd.Series:
    """Total memory over time (all ranks of all nodes)."""
    table = per_node_time_sum(run).ffill().bfill()
    return table.sum(axis=1)


def rank_stats(run: Run) -> pd.DataFrame:
    """Per node and sample: min / mean / max / count over the ranks."""
    return run.proc.groupby(["node", "_t"], observed=True)[run.mem].agg(["min", "mean", "max", "count"])


def run_total(run: Run):
    """Total memory over time in GB: (values, minutes, kind)."""
    if run.proc is not None:
        series = total_series(run)
        return gb(run, series), minutes_since(run, series.index), "ranks"
    if run.node is not None:
        series = run.node.groupby("_t", observed=True)["mem_used_kb"].sum().sort_index()
        return np.asarray(series, dtype=float) / KB_PER_GB, minutes_since(run, series.index), "node"
    return None, None, "sacct" if run.sacct else None


def has_series(run: Run) -> bool:
    return run.proc is not None or run.node is not None


def run_peak_gb(run: Run) -> float | None:
    """Peak memory of a run in GB, from the samples or from an sacct dump."""
    if run.proc is not None:
        return float(np.max(gb(run, total_series(run))))
    if run.node is not None:
        return float(run.node["mem_used_kb"].max() / KB_PER_GB)
    if run.sacct:
        peak = run.sacct.get("peak_rss_kb")
        return peak / KB_PER_GB if peak else None
    return None


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    if seconds < 120:
        return f"{seconds:.1f} s"
    return f"{seconds / 60.0:.1f} min"


# ----------------------------------------------------------------------
# OOM detection
# ----------------------------------------------------------------------

OOM_KEYWORDS = ("oom_kill", "oom-kill", "oom_reaper", "out of memory", "out-of-memory")

# SIGKILL, which is what the OOM killer sends. Slurm's own "oom_kill events"
# line is not always written, but the run logs its exit code either way
# (137 = 128 + 9, and subprocess reports a signalled child as -9).
OOM_EXIT_RE = re.compile(r"exit code:\s*(?:137|-9)\b")


def candidate_logs(run: Run) -> list[Path]:
    """Slurm/job logs that belong to a run, newest last.

    When the run is a log directory (``memory_logs``) the batch script's own
    output sits one level up, so the parent is searched as well.
    """
    source = Path(run.source)
    is_dir = source.is_dir()
    root = source if is_dir else source.parent

    files: list[Path] = []
    for hint in (run.meta.get("log"), run.meta.get("err")):
        if hint:
            hinted = Path(hint)
            if hinted.is_file():
                files.append(hinted)

    roots = [root, root / "slurm_logs"]
    if is_dir:
        roots.append(root.parent)

    needles = {str(run.meta.get("jobid", "")), run.label, run.label.replace("mem_", "")}
    needles = {n for n in needles if n}
    for base in roots:
        if not base.is_dir():
            continue
        for pattern in ("*.out", "*.err", "*.log"):
            for f in base.glob(pattern):
                if not needles or any(n in f.name for n in needles):
                    files.append(f)
    return sorted(set(files), key=lambda p: p.stat().st_mtime)


def detect_oom(run: Run) -> bool:
    """Look at the Slurm .out logs next to the run for an OOM kill."""
    for out in reversed(candidate_logs(run)):
        try:
            text = out.read_text(errors="ignore").lower()
        except OSError:
            continue
        if any(k in text for k in OOM_KEYWORDS) or OOM_EXIT_RE.search(text):
            return True
    return False


# ----------------------------------------------------------------------
# Timing and CPU samples from the job logs (optional, best effort)
# ----------------------------------------------------------------------

_SPY_RE = re.compile(r"Samples:\s*(?P<samples>[0-9]+)\s*Errors:")
_TIMING_RES = {
    "job_start": re.compile(r"Job Started:?\s*(?P<v>[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9:.]+(?:\s*[+-][0-9:]+)?)"),
    "job_end": re.compile(r"Job Finished:?\s*(?P<v>[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9:.]+(?:\s*[+-][0-9:]+)?)"),
    "program_start": re.compile(r"PROGRAM START AT\s*(?P<v>[0-9-]+ [0-9:.]+)"),
    "program_end": re.compile(r"PROGRAM END AT\s*(?P<v>[0-9-]+ [0-9:.]+)"),
    "retrieval_s": re.compile(r"Total Retrieval finish in\s*(?P<v>[0-9.eE+-]+)\s*seconds"),
    "sampling_s": re.compile(r"Sampling time\s*(?P<v>[0-9.eE+-]+)\s*s\b"),
}


def _parse_stamp(text: str) -> datetime | None:
    """Parse a log timestamp, always returning a naive local wall clock.

    `date -Is` prints an offset (``...+02:00``) while TauREx's PROGRAM START
    lines are local and naive; dropping the offset keeps them comparable.
    """
    text = text.strip()
    for candidate in (text, text.replace(" ", "T", 1)):
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed.replace(tzinfo=None)
        except ValueError:
            continue
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def read_timing(run: Run) -> dict:
    """Wall clock, TauREx runtime and py-spy samples, when the logs have them.

    The fields are searched newest log first, and each field is taken from the
    first log that contains it, so a run's numbers stay consistent.
    """
    logs = candidate_logs(run)
    if not logs:
        return {}

    found: dict = {}
    for log in reversed(logs):
        try:
            text = log.read_text(errors="ignore")
        except OSError:
            continue
        for key, pattern in _TIMING_RES.items():
            if key in found:
                continue
            match = pattern.search(text)
            if match:
                found[key] = match.group("v")
        if "spy_samples" not in found:
            match = _SPY_RE.search(text)
            if match:
                found["spy_samples"] = int(match.group("samples"))
        if len(found) >= len(_TIMING_RES) + 1:
            break

    timing: dict = {}
    for key in ("job_start", "job_end", "program_start", "program_end"):
        if key in found:
            timing[key] = _parse_stamp(found[key])
    for key in ("retrieval_s", "sampling_s"):
        if key in found:
            timing[key] = float(found[key])
    if "spy_samples" in found:
        timing["spy_samples"] = found["spy_samples"]

    start = timing.get("job_start")
    end = timing.get("job_end") or timing.get("program_end")
    if start and end:
        timing["wall_s"] = (end - start).total_seconds()
    prog_start, prog_end = timing.get("program_start"), timing.get("program_end")
    if prog_start and prog_end:
        timing["taurex_s"] = (prog_end - prog_start).total_seconds()
    if start and prog_start:
        timing["startup_s"] = (prog_start - start).total_seconds()
    return timing


# ----------------------------------------------------------------------
# sacct fallback: peak memory without a monitor
# ----------------------------------------------------------------------

SACCT_FIELDS = {"MaxRSS": "peak_rss_kb", "AveRSS": "average_rss_kb",
                "MaxVMSize": "max_vmsize_kb"}


def parse_sacct_size(text: str) -> float | None:
    """SLURM size string ('1234K', '1.5G', '1234') -> KiB.

    A bare number is KiB: that is the unit sacct reports RSS in.
    """
    text = (text or "").strip()
    if not text or text == ".":
        return None
    match = re.match(r"^([0-9.]+)\s*([KMGTkmgt]?)B?$", text)
    if match is None:
        return None
    factors = {"": 1.0, "K": 1.0, "M": 1024.0, "G": 1024.0 ** 2, "T": 1024.0 ** 3}
    return float(match.group(1)) * factors[match.group(2).upper()]


def looks_like_sacct(path: Path) -> bool:
    if "sacct" in path.name.lower():
        return True
    try:
        with path.open(errors="ignore") as handle:
            head = handle.read(4096)
    except OSError:
        return False
    return "MaxRSS" in head and ("JobID" in head or "MaxVMSize" in head)


def parse_sacct(path: Path, job_id: str | None = None) -> dict | None:
    """Read `sacct -j ID -P --format=JobID,Elapsed,MaxRSS,MaxVMSize,AveRSS`."""
    lines = [line for line in path.read_text(errors="ignore").splitlines() if line.strip()]
    if len(lines) < 2:
        return None

    separator = "|" if "|" in lines[0] else None
    header = [name.strip() for name in (lines[0].split(separator) if separator
                                        else lines[0].split())]
    rows = []
    for line in lines[1:]:
        if set(line.strip()) <= set("-|"):
            continue
        values = [v.strip() for v in (line.split(separator) if separator else line.split())]
        if len(values) < len(header):
            continue
        rows.append(dict(zip(header, values)))

    stats: dict = {"source": str(path)}
    for row in rows:
        if job_id and row.get("JobID", "").split(".")[0] != job_id:
            continue
        for name, key in SACCT_FIELDS.items():
            value = parse_sacct_size(row.get(name, ""))
            if value is not None:
                stats[key] = max(stats.get(key, 0.0), value)
        if "Elapsed" in row and row["Elapsed"] not in (".", ""):
            stats.setdefault("elapsed_s", _parse_elapsed(row["Elapsed"]))
    if not any(k in stats for k in SACCT_FIELDS.values()):
        return None
    return stats


def _parse_elapsed(text: str) -> float | None:
    """'HH:MM:SS' or 'D-HH:MM:SS' -> seconds."""
    days = 0.0
    if "-" in text:
        day_part, _, text = text.partition("-")
        days = float(day_part)
    fields = [float(x) for x in text.split(":")] if text else []
    while len(fields) < 3:
        fields.insert(0, 0.0)
    return days * 86400.0 + fields[0] * 3600.0 + fields[1] * 60.0 + fields[2]


# ----------------------------------------------------------------------
# Plot helpers
# ----------------------------------------------------------------------

def style(ax, title: str, xlabel: str | None = None, ylabel: str | None = None):
    ax.set_facecolor("#fcfcfb")
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.set_title(title, color=INK, fontsize=11, loc="left")
    if xlabel:
        ax.set_xlabel(xlabel, color=INK2, fontsize=10)
    if ylabel:
        ax.set_ylabel(ylabel, color=INK2, fontsize=10)


def reference_line(ax, y, text, color, dashes):
    ax.axhline(y, color=color, lw=1.2, ls=(0, dashes))
    ax.annotate(text, (1, y), xycoords=("axes fraction", "data"), xytext=(-4, 3),
                textcoords="offset points", ha="right", fontsize=8.5, color=color)


def save_fig(fig, base: Path):
    fig.savefig(f"{base}.png", dpi=150)
    plt.close(fig)
    print(f"Saved {base}.png")


def safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "run"


# ----------------------------------------------------------------------
# Figures
# ----------------------------------------------------------------------

def plot_overview(run: Run, base: Path, limit_gb: float, project_ranks: int,
                  title: str | None = None):
    """Node memory and per-rank memory, one column per node (colleague's layout)."""
    nodes = set()
    if run.node is not None:
        nodes |= set(run.node["node"].unique())
    if run.proc is not None:
        nodes |= set(run.proc["node"].unique())
    nodes = sorted(nodes)
    ncols = len(nodes)
    rows = []
    if run.node is not None:
        rows.append("node")
    if run.proc is not None:
        rows.append("ranks")
    if not rows:
        return

    unit, div = time_units(run)
    fig, axes = plt.subplots(len(rows), ncols, figsize=(6.5 * ncols, 4.0 * len(rows)),
                             sharex=True, sharey="row", squeeze=False)
    fig.patch.set_facecolor("#fcfcfb")

    top_node, top_rank = limit_gb, 0.0

    for j, node in enumerate(nodes):
        # --- node panel -------------------------------------------------
        if "node" in rows:
            ax = axes[rows.index("node"), j]
            sub = run.node[run.node["node"] == node]
            if sub.empty:
                style(ax, f"{node} — no node samples")
            else:
                t = minutes_since(run, sub["_t"]) / div
                used = sub["mem_used_kb"].to_numpy(dtype=float) / KB_PER_GB
                physical = sub["mem_total_kb"].iloc[0] / KB_PER_GB
                top_node = max(top_node, physical, used.max())
                ax.plot(t, used, color=BLUE, lw=2)
                if "cgroup_mem_kb" in sub.columns and sub["cgroup_mem_kb"].notna().any():
                    cg = sub["cgroup_mem_kb"].to_numpy(dtype=float) / KB_PER_GB
                    ax.plot(t, cg, color=AQUA, lw=1.4, ls=":", label="Slurm cgroup")
                reference_line(ax, physical, f"physical memory: {physical:.0f} GB", MUTED, (1, 3))
                reference_line(ax, limit_gb, f"allocated by Slurm (--mem): {limit_gb:.0f} GB", INK2, (6, 3))
                if run.killed:
                    ax.scatter(t[-1], used[-1], marker="X", s=120, color=OOM_RED, zorder=5)
                if "cgroup_mem_kb" in sub.columns and sub["cgroup_mem_kb"].notna().any():
                    ax.legend(fontsize=8, frameon=False, loc="upper left", labelcolor=INK2)
                style(ax, f"{node} — memory used on the node",
                      ylabel="GB" if j == 0 else None)

        # --- per-rank panel ---------------------------------------------
        if "ranks" in rows:
            ax = axes[rows.index("ranks"), j]
            sub = run.proc[run.proc["node"] == node]
            if sub.empty:
                style(ax, "Memory used per rank (no samples)")
            else:
                try:
                    stats = rank_stats(run).xs(node, level="node")
                except KeyError:
                    stats = None
                if stats is None or stats.empty:
                    style(ax, "Memory used per rank (no samples)")
                else:
                    t = minutes_since(run, stats.index) / div
                    mean = gb(run, stats["mean"])
                    lo = gb(run, stats["min"])
                    hi = gb(run, stats["max"])
                    ax.fill_between(t, lo, hi, color=BAND, lw=0)
                    ax.plot(t, hi, color=ORANGE, lw=2, ls="--", label="largest rank")
                    ax.plot(t, mean, color=BLUE, lw=2, label="average")
                    ax.plot(t, lo, color=AQUA, lw=2, ls="-.", label="smallest rank")
                    ax.legend(fontsize=9, frameon=False, loc="upper left", ncol=3, labelcolor=INK2)
                    top_rank = max(top_rank, hi.max())
                    style(ax, f"Memory used per rank  ({int(stats['count'].max())} '{run.proc['command'].iloc[0]}' processes)",
                          ylabel="GB per rank" if j == 0 else None)

        axes[len(rows) - 1, j].set_xlabel(f"time since start [{unit}]", color=INK2, fontsize=10)

    if "node" in rows:
        axes[rows.index("node"), 0].set_ylim(0, top_node * 1.15)
    if "ranks" in rows:
        axes[rows.index("ranks"), 0].set_ylim(0, (top_rank * 1.3) if top_rank > 0 else 1)
    axes[0, 0].set_xlim(left=0)

    fig.suptitle(title or f"Memory — {run.label}", x=0.01, ha="left", fontsize=14, color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    save_fig(fig, base)


def plot_total(run: Run, base: Path):
    """Total memory over time (all ranks / all nodes)."""
    values, minutes, kind = run_total(run)
    if values is None:
        return
    what = "all ranks" if kind == "ranks" else "whole node (no per-process log)"
    fig, ax = plt.subplots(figsize=(14, 6))
    fig.patch.set_facecolor("#fcfcfb")
    ax.plot(minutes, values, linewidth=1.6, color=BLUE)
    if run.killed:
        ax.scatter(minutes[-1], values[-1], marker="X", s=140, color=OOM_RED, zorder=5, label="OOM KILLED")
        ax.legend(frameon=False, labelcolor=INK2)
    style(ax, f"Total memory usage over time — {run.label}  ({what})",
          xlabel="Time (minutes)", ylabel=f"Total {mem_label(run.mem)} (GB)")
    fig.tight_layout()
    save_fig(fig, base)


def plot_per_node(run: Run, base: Path):
    """One line per node."""
    if run.proc is None:
        print("Skipping per-node plot: no per-process log")
        return
    table = per_node_time_sum(run)
    minutes = minutes_since(run, table.index)
    fig, ax = plt.subplots(figsize=(14, 6))
    fig.patch.set_facecolor("#fcfcfb")
    table = table.ffill().bfill()
    for node in table.columns:
        ax.plot(minutes, gb(run, table[node]), linewidth=1.2, label=node)
    style(ax, f"Memory usage per node — {run.label}",
          xlabel="Time (minutes)", ylabel=f"{mem_label(run.mem)} (GB)")
    ax.legend(fontsize=9, ncol=2, frameon=False, labelcolor=INK2)
    fig.tight_layout()
    save_fig(fig, base)


def plot_top(run: Run, n: int, base: Path):
    """Top-N processes by peak memory."""
    if run.proc is None:
        print("Skipping top-N plot: no per-process log")
        return
    df = run.proc
    label = df["pid"].astype(str) + "_" + df["command"].astype(str)
    df = df.assign(_label=label)
    top_labels = df.groupby("_label", observed=True)[run.mem].max().nlargest(n).index
    sub = df[df["_label"].isin(top_labels)]
    table = sub.groupby(["_t", "_label"], observed=True)[run.mem].max().unstack("_label").sort_index()
    minutes = minutes_since(run, table.index)

    fig, ax = plt.subplots(figsize=(14, 6))
    fig.patch.set_facecolor("#fcfcfb")
    for col in table.columns:
        ax.plot(minutes, gb(run, table[col]), linewidth=0.9, label=col)
    style(ax, f"Top {n} processes by peak {mem_label(run.mem)} — {run.label}",
          xlabel="Time (minutes)", ylabel=f"{mem_label(run.mem)} (GB)")
    ax.legend(fontsize=7, ncol=3, frameon=False, labelcolor=INK2)
    fig.tight_layout()
    save_fig(fig, base)


def plot_compare(runs: list[Run], base: Path, limit_gb: float, title: str | None = None):
    """Overlay the total memory of several runs.

    A run without samples (for example one read from an sacct dump) is drawn as
    its measured peak, so it still appears in the comparison.
    """
    fig, ax = plt.subplots(figsize=(14, 6))
    fig.patch.set_facecolor("#fcfcfb")
    summary = []

    for index, run in enumerate(runs):
        color = CYCLE[index % len(CYCLE)]
        values, minutes, kind = run_total(run)
        if values is None:
            peak = run_peak_gb(run)
            if peak is None:
                continue
            summary.append((run, peak, kind))
            continue
        ax.plot(minutes, values, linewidth=1.4, color=color, label=run.label)
        if run.killed:
            ax.scatter(minutes[-1], values[-1], marker="X", s=140, color=OOM_RED, zorder=5)
        summary.append((run, float(np.max(values)), kind))

    # runs that only have a peak (sacct) — draw them as a reference line
    for index, run in enumerate(runs):
        if has_series(run) or run_peak_gb(run) is None:
            continue
        color = CYCLE[index % len(CYCLE)]
        peak = run_peak_gb(run)
        ax.axhline(peak, color=color, ls=":", lw=1.6)
        ax.annotate(f"{run.label} peak (sacct): {peak:.2f} GB", (0.01, peak),
                    xycoords=("axes fraction", "data"), xytext=(4, 4),
                    textcoords="offset points", fontsize=9, color=color)

    ax.set_xlabel("Time (minutes)", color=INK2, fontsize=10)
    ax.set_ylabel("Total memory (GB)", color=INK2, fontsize=10)
    if any(r.killed for r in runs):
        ax.scatter([], [], marker="X", s=140, color=OOM_RED, label="✕ = OOM KILLED")
    style(ax, title or ("Memory comparison: " + ", ".join(r.label for r in runs)))
    handles, labels = ax.get_legend_handles_labels()
    if handles:
        ax.legend(frameon=False, labelcolor=INK2)
    ax.grid(True, color=GRID, linewidth=0.8)
    fig.tight_layout()
    save_fig(fig, base)

    print("\n--- Comparison summary (peak of the total) ---")
    peaks = {}
    for run, peak, kind in summary:
        peaks[run.label] = peak
        print(f"{run.label:>24s}  peak: {peak:8.2f} GB"
              f"  ({kind}{', OOM killed' if run.killed else ''})")
    if len(summary) >= 2:
        first = summary[0][0].label
        for run, peak, _kind in summary[1:]:
            diff = peak - peaks[first]
            if peaks[first] > 0:
                print(f"{'Δ ' + run.label + ' - ' + first:>24s}  {diff:+8.2f} GB"
                      f"  ({diff / peaks[first] * 100:+.1f}%)")


def output_name(name: str, limit: int = 42) -> str:
    """The tail of a long dataset path, so it fits next to an axis."""
    if len(name) <= limit:
        return name
    parts = name.split("/")
    short = parts[-1]
    for part in reversed(parts[:-1]):
        if len(short) + len(part) + 1 > limit:
            break
        short = f"{part}/{short}"
    return short if short == name else "…/" + short


def fmt_value(value: float) -> str:
    return f"{value:.4g}" if np.isfinite(value) else "inf"


def _numeric_datasets(path_a: Path, path_b: Path, max_arrays: int = 6):
    """Read the numeric datasets of two outputs, biggest arrays first.

    Returns ``(arrays, scalars)``, each a list of ``(name, baseline, other)``.
    Arrays are what carries the physics (a pressure profile, an SED, a spectrum)
    and are what the figure overlays; the scalars are the parameters, which are
    compared point by point on a parity panel.
    """
    import h5py

    arrays, scalars = [], []
    with h5py.File(path_a, "r") as fa, h5py.File(path_b, "r") as fb:
        names: list[str] = []
        fa.visititems(lambda n, o: names.append(n)
                      if isinstance(o, h5py.Dataset) else None)
        for name in sorted(names):
            if name not in fb:
                continue
            data_a, data_b = fa[name], fb[name]
            if data_a.dtype.kind not in "fiub" or data_b.dtype.kind not in "fiub":
                continue
            a, b = np.asarray(data_a[()]), np.asarray(data_b[()])
            if a.ndim > 2 or b.ndim > 2:
                continue
            (arrays if a.ndim >= 1 else scalars).append((name, a, b))
    # the largest arrays are the informative ones; the scalars all fit one panel
    arrays.sort(key=lambda item: item[1].size, reverse=True)
    return arrays[:max_arrays], scalars


def _value_panel(ax, name: str, a: np.ndarray, b: np.ndarray, other_label: str):
    """Overlay the values of one dataset from both runs."""
    if a.ndim <= 1:
        series = [(a, b)]
    else:  # 2D: one line per column, a handful at most to stay readable
        columns = min(a.shape[1], b.shape[1], 8)
        series = [(a[:, i], b[:, i]) for i in range(columns)]
    for a_col, b_col in series:
        ax.plot(np.arange(a_col.size), a_col, color=BLUE, lw=1.2)
        ax.plot(np.arange(b_col.size), b_col, color=ORANGE, lw=1.2)
    style(ax, output_name(name, 46), ylabel="value")
    ax.set_xlabel("index", color=INK2, fontsize=9)
    ax.legend(handles=[plt.Line2D([], [], color=BLUE, lw=2, label="baseline"),
                       plt.Line2D([], [], color=ORANGE, lw=2, label=other_label)],
              fontsize=8, frameon=False, labelcolor=INK2)
    if a.shape == b.shape:
        worst = float(np.max(np.abs(a.astype("f8") - b.astype("f8")))) if a.size else 0.0
        ax.annotate(f"max|Δ| {worst:.3g}", (0.995, 0.04), xycoords="axes fraction",
                    ha="right", fontsize=8.5, color=MUTED)
    else:
        ax.annotate(f"different lengths: {a.size} vs {b.size}", (0.995, 0.04),
                    xycoords="axes fraction", ha="right", fontsize=8.5, color=OOM_RED)


def _scalar_panel(ax, scalars, other_label: str):
    """Parity plot of the scalar parameters: on the diagonal means identical."""
    values_a = np.array([float(a) for _n, a, _b in scalars])
    values_b = np.array([float(b) for _n, _a, b in scalars])
    log = bool(np.all(values_a > 0) and np.all(values_b > 0)
               and values_a.max() / values_a.min() > 1e3)
    lo = min(values_a.min(), values_b.min())
    hi = max(values_a.max(), values_b.max())
    if log:
        ax.set_xscale("log")
        ax.set_yscale("log")
        lo, hi = lo / 3, hi * 3
    if hi <= lo:                     # one parameter, or all of them equal
        pad = abs(lo) * 0.25 or 1.0
        lo, hi = lo - pad, hi + pad
    ax.plot([lo, hi], [lo, hi], color=MUTED, ls="--", lw=1.1, zorder=1)
    ax.scatter(values_a, values_b, s=18, color=BLUE, zorder=3)
    style(ax, f"{len(scalars)} scalar parameters", xlabel="baseline",
          ylabel=other_label)
    ax.set_xlim(lo, hi)
    ax.set_ylim(lo, hi)
    offset = int(np.argmax(np.abs(values_b - values_a))) if scalars else 0
    if scalars and not np.array_equal(values_a, values_b):
        ax.annotate(output_name(scalars[offset][0], 34),
                    (values_a[offset], values_b[offset]), xytext=(6, -10),
                    textcoords="offset points", fontsize=8, color=OOM_RED)
    ax.annotate(f"{len(scalars)} identical" if np.array_equal(values_a, values_b)
                else "off the dashed line = different",
                (0.995, 0.04), xycoords="axes fraction", ha="right",
                fontsize=8.5, color=AQUA if np.array_equal(values_a, values_b) else OOM_RED)


def plot_output_check(check, base: Path, rtol: float, baseline_label: str):
    """Compare the two runs' outputs *by value*: overlay the quantities.

    A thin roster bar says how the datasets split (identical / within the
    tolerance / outside it / structural), then the body of the figure is the
    data itself, one panel per dataset that carries values:

    * arrays (pressure profiles, SEDs, spectra, posteriors) are overlaid from
      both runs, so the shapes and the differences are visible directly;
    * the scalar parameters share a parity panel, where a point on the dashed
      diagonal means the two runs agree on that parameter.

    Differences that have no numeric value — a shape or dtype change — cannot be
    drawn and are named in the caption and the verdict.
    """
    measured = [d for d in check.datasets if d.rel is not None]
    changed = [d for d in measured if d.rel]     # moved at all, pass or fail
    failing = [d for d in measured if not d.same]
    structural = structural_messages(check)      # shape/dtype: nothing to plot
    identical = len(measured) - len(changed)
    largest = max((d.rel for d in measured), default=None)

    roster = [("identical", identical, BAND),
              ("within rtol", len(changed) - len(failing), BLUE),
              ("outside rtol", len(failing), OOM_RED),
              ("structural", len(structural), ORANGE)]
    roster = [row for row in roster if row[1]]

    # --- figure: a roster strip, then one panel per dataset with values ----
    arrays, scalars = ([], [])
    if check.kind == "hdf5" and check.baseline is not None and check.path is not None:
        arrays, scalars = _numeric_datasets(check.baseline, check.path)

    panels = len(arrays) + (1 if scalars else 0)
    columns = 2
    rows = max(1, -(-panels // columns))          # ceil
    fig = plt.figure(figsize=(13, 2.0 + 3.0 * (1 + rows)))
    gs = fig.add_gridspec(1 + rows, columns, height_ratios=[0.55] + [3.0] * rows,
                          hspace=0.75, wspace=0.22)
    fig.patch.set_facecolor("#fcfcfb")

    # the roster: a one-glance split of how the compared datasets behaved
    head = fig.add_subplot(gs[0, :])
    positions = np.arange(len(roster))[::-1]
    head.barh(positions, [row[1] for row in roster],
              color=[row[2] for row in roster], height=0.62)
    for position, (_label, count, color) in zip(positions, roster):
        head.annotate(str(count), (count, position), xytext=(4, 0),
                      textcoords="offset points", va="center", fontsize=8.5,
                      color=color)
    head.set_yticks(positions)
    head.set_yticklabels([row[0] for row in roster], fontsize=9, color=INK2)
    head.set_xticks([])
    head.set_xlim(0, max(row[1] for row in roster) * 1.25)
    head.grid(False)
    for side in ("top", "right", "left", "bottom"):
        head.spines[side].set_visible(False)
    head.set_title(f"Output check: {check.label} vs {baseline_label}",
                   color=INK, fontsize=11, loc="left")

    # the body: the quantities themselves, overlaid, then the parameters
    drawn = 0
    for name, a, b in arrays:
        _value_panel(fig.add_subplot(gs[1 + drawn // columns, drawn % columns]),
                     name, a, b, check.label)
        drawn += 1
    if scalars:
        _scalar_panel(fig.add_subplot(gs[1 + drawn // columns, drawn % columns]),
                      scalars, check.label)
        drawn += 1

    problems = []
    if failing:
        problems.append(f"{len(failing)} outside rtol {rtol:g}")
    if structural:
        problems.append(f"{len(structural)} structural")
    if not problems:
        verdict = f"\u2713 {check.total} datasets within rtol {rtol:g}"
    elif check.total:
        verdict = f"\u2717 {', '.join(problems)} of {check.total} datasets"
    else:
        verdict = f"\u2717 {', '.join(problems)}"
    if largest is not None:
        verdict += f"   |   largest {largest:.3g}"
    head.annotate(verdict, (1.0, 1.05), xycoords="axes fraction", ha="right",
                  va="bottom", fontsize=10.5, fontweight="bold",
                  color=AQUA if not problems else OOM_RED)

    if not drawn:
        # nothing with a value to show (a byte comparison, all-text datasets)
        body = fig.add_subplot(gs[1:, :])
        body.axis("off")
        if check.same:
            body.text(0.005, 0.9, "the compared outputs are identical", va="top",
                      fontsize=12, fontweight="bold", color=AQUA)
        else:
            body.text(0.005, 0.9, "no numeric dataset to compare", va="top",
                      fontsize=12, fontweight="bold", color=INK2)
            lines = (check.diffs + check.notes)[:6]
            if lines:
                body.text(0.005, 0.6, "\n".join(f"\u2022 {line}" for line in lines),
                          va="top", fontsize=9.5, color=OOM_RED, family="monospace")

    caption = [f"{check.path.name} vs {check.baseline.name}"]
    if arrays:
        caption.append(f"{len(arrays)} datasets with values overlaid")
    if structural:
        caption.append("structural: " + "; ".join(structural[:2]))
    fig.text(0.01, 0.008, " — ".join(caption), fontsize=8.5, color=MUTED, va="bottom")
    gs.update(left=0.055, right=0.985, top=0.92, bottom=0.07)
    save_fig(fig, base)


def print_summary(run: Run, limit_gb: float, project_ranks: int):
    print(f"\n=== {run.label} ({run.source}) ===")
    if run.proc is not None:
        series = total_series(run)
        peak_total = float(gb(run, series).max())
        print(f"  peak total {mem_label(run.mem)}: {peak_total:.2f} GB over {len(series)} samples")
    if run.node is not None:
        print("  peak memory on the node:")
        for node, sub in run.node.groupby("node", observed=True):
            used = sub["mem_used_kb"].max() / KB_PER_GB
            physical = sub["mem_total_kb"].iloc[0] / KB_PER_GB
            extra = ""
            if "cgroup_mem_kb" in sub.columns and sub["cgroup_mem_kb"].notna().any():
                extra = f", cgroup {sub['cgroup_mem_kb'].max() / KB_PER_GB:.1f} GB"
            print(f"    {node}: {used:.1f} GB  (physical {physical:.0f} GB, "
                  f"Slurm --mem {limit_gb:.0f} GB{extra})")
    if run.proc is not None:
        print(f"  per rank ({mem_label(run.mem)}):")
        for node, sub in run.proc.groupby("node", observed=True):
            stats = sub.groupby("_t", observed=True)[run.mem].agg(["min", "mean", "max", "count"])
            print(f"    {node}: average {stats['mean'].max() / KB_PER_GB:.2f} GB, "
                  f"smallest {stats['min'].min() / KB_PER_GB:.2f} GB, "
                  f"largest {stats['max'].max() / KB_PER_GB:.2f} GB  "
                  f"({int(stats['count'].max())} ranks at the peak of the average)")
            if run.node is not None:
                node_peak = run.node[run.node["node"] == node]["mem_used_kb"].max() / KB_PER_GB
                scaled = node_peak * project_ranks / max(int(stats["count"].max()), 1)
                print(f"      with {project_ranks} ranks/node, if node memory scales with the rank count: "
                      f"~{scaled:.0f} GB")
    if run.sacct:
        peak = run.sacct.get("peak_rss_kb")
        average = run.sacct.get("average_rss_kb")
        vm = run.sacct.get("max_vmsize_kb")
        text = f"  sacct: peak RSS {peak / KB_PER_GB:.2f} GB" if peak else "  sacct: no peak RSS"
        if average:
            text += f", average RSS {average / KB_PER_GB:.2f} GB"
        if vm:
            text += f", peak VmSize {vm / KB_PER_GB:.2f} GB"
        if run.sacct.get("elapsed_s"):
            text += f", elapsed {format_duration(run.sacct['elapsed_s'])}"
        print(text)
    print_timing(run)
    if has_series(run):
        samples = (run.proc if run.proc is not None else run.node)["_t"].nunique()
        print(f"  samples: {samples}  |  OOM killed: {'yes' if run.killed else 'no'}")
    else:
        print(f"  OOM killed: {'yes' if run.killed else 'no'}")


def print_timing(run: Run):
    timing = run.timing
    rows = [("wall clock (job start to end)", timing.get("wall_s")),
            ("TauREx total", timing.get("taurex_s")),
            ("optimizer.fit()", timing.get("retrieval_s")),
            ("  MultiNest sampling", timing.get("sampling_s")),
            ("  startup (modules + venv)", timing.get("startup_s"))]
    rows = [(name, value) for name, value in rows if value is not None]
    if not rows and timing.get("spy_samples") is None:
        return
    print("  timing:")
    for name, value in rows:
        print(f"    {name}: {format_duration(value)}")
    if timing.get("spy_samples") is not None:
        print(f"    py-spy CPU samples: {timing['spy_samples']}")


def plot_peaks(runs: list[Run], base: Path, title: str | None = None):
    """Bar chart of the peak memory, for runs that have no time series."""
    labels, values, colors = [], [], []
    for index, run in enumerate(runs):
        peak = run_peak_gb(run)
        if peak is None:
            continue
        labels.append(run.label)
        values.append(peak)
        colors.append(CYCLE[index % len(CYCLE)])
    if not labels:
        return

    fig, ax = plt.subplots(figsize=(max(5.0, 1.8 * len(labels)), 5))
    fig.patch.set_facecolor("#fcfcfb")
    ax.bar(labels, values, color=colors, width=0.62)
    for index, value in enumerate(values):
        ax.text(index, value, f" {value:.2f} GB", ha="center", va="bottom", fontsize=10,
                color=INK)
    ax.set_xlim(-0.6, len(labels) - 0.4)
    style(ax, title or "Peak memory per run (sacct)", ylabel="Peak RSS (GB)")
    fig.tight_layout()
    save_fig(fig, base)


# ----------------------------------------------------------------------
# Markdown report
# ----------------------------------------------------------------------

def write_report(runs: list[Run], path: Path, limit_gb: float, project_ranks: int,
                 checks: list | None = None, rtol: float = 1e-6):
    """Small shareable report: memory peaks plus the timing side by side."""
    lines = ["# TauREx memory report", ""]
    lines.append("* baseline: `{}`".format(runs[0].label))
    for run in runs:
        lines.append(f"* `{run.label}`: {run.source}"
                     + (f" (job {run.meta['jobid']})" if run.meta.get("jobid") else ""))
    lines += ["", "## Memory", ""]

    table = ["| measurement | " + " | ".join(r.label for r in runs) + " |",
             "|---" * (len(runs) + 1) + "|"]
    peaks = [run_peak_gb(run) for run in runs]
    table.append("| peak total (GB) | "
                 + " | ".join("n/a" if p is None else f"{p:.2f}" for p in peaks) + " |")
    node_peaks = []
    for run in runs:
        if run.node is not None:
            node_peaks.append(run.node["mem_used_kb"].max() / KB_PER_GB)
        else:
            node_peaks.append(None)
    table.append("| peak on one node (GB) | "
                 + " | ".join("n/a" if p is None else f"{p:.1f}" for p in node_peaks) + " |")
    table.append("| Slurm --mem per node (GB) | "
                 + " | ".join(f"{limit_gb:.0f}" for _ in runs) + " |")
    lines += table

    if len(runs) > 1 and peaks[0]:
        lines += ["", "Relative to the baseline:"]
        for run, peak in zip(runs[1:], peaks[1:]):
            if peak is None:
                lines.append(f"* `{run.label}`: no memory measurement")
            else:
                change = (peak - peaks[0]) / peaks[0] * 100.0
                lines.append(f"* `{run.label}`: {change:+.1f}% peak memory "
                             f"({peak:.2f} GB vs {peaks[0]:.2f} GB)")

    timing_rows = (("wall clock (job start to end)", "wall_s"),
                   ("TauREx total", "taurex_s"),
                   ("optimizer.fit()", "retrieval_s"),
                   ("MultiNest sampling", "sampling_s"))
    if any(run.timing.get("spy_samples") is not None or run.timing.get("wall_s") is not None
           for run in runs):
        lines += ["", "## Timing", "",
                  "| measurement | " + " | ".join(r.label for r in runs) + " |",
                  "|---" * (len(runs) + 1) + "|"]
        for name, key in timing_rows:
            lines.append(f"| {name} | "
                         + " | ".join(format_duration(run.timing.get(key)) for run in runs) + " |")
        if any(run.timing.get("spy_samples") is not None for run in runs):
            lines.append("| py-spy CPU samples | "
                         + " | ".join(str(run.timing.get("spy_samples", "n/a"))
                                      for run in runs) + " |")

    lines += ["", "## Per node", "",
              "| run | node | peak used (GB) | physical (GB) | --mem (GB) |",
              "|---|---|---|---|---|"]
    for run in runs:
        if run.node is None:
            continue
        for node, sub in run.node.groupby("node", observed=True):
            lines.append(f"| {run.label} | {node} | "
                         f"{sub['mem_used_kb'].max() / KB_PER_GB:.1f} | "
                         f"{sub['mem_total_kb'].iloc[0] / KB_PER_GB:.0f} | {limit_gb:.0f} |")
    lines += ["", f"Projected to {project_ranks} ranks/node, memory is assumed to scale "
                  "with the rank count; check the per-rank numbers before trusting it.", ""]

    if checks:
        lines += report_lines(checks, rtol)

    path.write_text("\n".join(lines) + "\n")
    print(f"Saved report to {path}")


# ----------------------------------------------------------------------
# Command line
# ----------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs", nargs="*", metavar="RUN",
                        help="a run to plot: a run name (label), a log file, a log "
                             "directory or LABEL=path[#jobid]")
    parser.add_argument("--compare", action="append", nargs="+", default=None, metavar="RUN",
                        help="run to overlay, given the same way as RUN; several may "
                             "follow at once and the option may be repeated; each run "
                             "is plotted on its own first")
    parser.add_argument("--compare2", default=None, metavar="RUN",
                        help="third run to overlay (legacy alias)")
    parser.add_argument("--logdir", default="memory_logs",
                        help="directory holding the runs, scanned when a run is given "
                             "as a name and when no run is given [memory_logs]")
    parser.add_argument("--job", default=None,
                        help="Slurm job id inside the log directory (default: latest found)")
    parser.add_argument("--select", default=None, metavar="GLOB",
                        help="plot every run in the directory whose label matches "
                             "(e.g. 'mem_64_*'), instead of a single job")
    parser.add_argument("--label", default=None, help="label of the run")
    parser.add_argument("--label-a", default=None, help="label of the first run (legacy)")
    parser.add_argument("--label-b", default=None, help="label of the second run (legacy)")
    parser.add_argument("--label-c", default=None, help="label of the third run (legacy)")
    parser.add_argument("--output-dir", default=None, help="where to write the figures")
    parser.add_argument("--rss", action="store_true",
                        help="plot RSS instead of Pss (double-counts shared memory)")
    parser.add_argument("--limit-gb", type=float, default=DEFAULT_LIMIT_GB,
                        help=f"memory allocated by Slurm per node, --mem [{DEFAULT_LIMIT_GB:.0f}]")
    parser.add_argument("--command", default=None,
                        help="only use processes with this name for the per-rank statistics")
    parser.add_argument("--project-ranks", type=int, default=DEFAULT_PROJECT_RANKS,
                        help=f"ranks/node for the extrapolation [{DEFAULT_PROJECT_RANKS}]")
    parser.add_argument("--per-node", action="store_true", help="also draw one line per node")
    parser.add_argument("--top", type=int, default=0, help="also draw the top-N processes by peak memory")
    parser.add_argument("--per-run", action="store_true",
                        help="with several runs, also draw the per-run overview and total "
                             "figures (already implied by --compare)")
    parser.add_argument("--no-overview", action="store_true", help="skip the overview figure")
    parser.add_argument("--no-total", action="store_true", help="skip the total figure")
    parser.add_argument("--title", default=None, help="custom title for the comparison figure")
    parser.add_argument("--check-output", action="append", default=None,
                        metavar="[LABEL=]FILE",
                        help="override the output file compared across the runs "
                             "(the check is automatic: it uses the -o recorded in "
                             "run_<jobid>.meta, and compares HDF5 dataset by dataset "
                             "or anything else byte by byte)")
    parser.add_argument("--no-check-output", action="store_true",
                        help="do not compare the outputs of the compared runs")
    parser.add_argument("--output-rtol", type=float, default=1e-6,
                        help="relative tolerance for the output check [1e-06]")
    parser.add_argument("--output-atol", type=float, default=0.0,
                        help="absolute tolerance for the output check [0]")
    parser.add_argument("--report", default=None, metavar="PATH",
                        help="also write a short markdown report of peaks and timings")
    parser.add_argument("--killed", dest="killed", action="store_true", default=None,
                        help="mark the runs as OOM-killed")
    parser.add_argument("--not-killed", dest="killed", action="store_false",
                        help="do not mark any run as OOM-killed")
    parser.add_argument("--killed-a", action="store_true", help="mark the first run as OOM-killed")
    parser.add_argument("--killed-b", action="store_true", help="mark the second run as OOM-killed")
    parser.add_argument("--killed-c", action="store_true", help="mark the third run as OOM-killed")
    return parser.parse_args(argv)


def split_spec(spec: str):
    """Parse '[LABEL=]PATH[#JOBID]'."""
    label, sep, path = spec.partition("=")
    if not (sep and path and "/" not in label and "\\" not in label):
        label, path = None, spec
    job = None
    if "#" in path:
        path, _, job = path.rpartition("#")
        job = job or None
    return label, Path(path).expanduser(), job


def is_run_name(path: Path) -> bool:
    """True for a bare name such as 'mem_64_good', i.e. a run label."""
    return path.parent == Path(".") and path.name not in ("", ".", "..")


def collect_specs(args):
    raw = [(None, s) for s in args.runs]
    if args.compare:
        raw += [(None, s) for group in args.compare for s in group]
    if args.compare2:
        raw.append((None, args.compare2))
    if not raw:
        # No explicit run: use the log directory, either picking one job
        # (--job / latest) or every run whose label matches --select.
        return [(args.label, Path(args.logdir).expanduser(), args.job)], args.job

    labels = [args.label_a, args.label_b, args.label_c]
    specs = []
    for i, (label, spec) in enumerate(raw):
        spec_label, path, job = split_spec(spec)
        if spec_label:
            chosen = spec_label
        elif i < len(labels) and labels[i]:
            chosen = labels[i]
        else:
            chosen = label
        specs.append((chosen, path, job))
    # A single run may use the global --job as a shortcut.
    if len(specs) == 1 and specs[0][2] is None:
        specs[0] = (specs[0][0], specs[0][1], args.job)
    return specs, args.job


def main(argv=None):
    args = parse_args(argv)
    specs, job = collect_specs(args)

    runs: list[Run] = []
    logdir = Path(args.logdir).expanduser()
    for label, path, spec_job in specs:
        if path.exists():
            # --select applies to the directory the run spec points at.
            select = args.select if path.is_dir() else None
            built = build_runs(path, spec_job if spec_job is not None else job, select=select)
            if not built:
                sys.exit(f"ERROR: no memory logs found in {path}")
            if label and not select:
                for run in built:
                    run.label = label
        elif is_run_name(path):
            # A bare name is a run label inside --logdir, so runs are compared
            # by name without needing their job ids.
            built = build_runs(logdir, spec_job, select=str(path))
            if not built:
                sys.exit(f"ERROR: no run named '{path}' in {logdir}")
            if label:
                for run in built:
                    run.label = label
        else:
            sys.exit(f"ERROR: {path} not found")
        runs += built

    if not runs:
        sys.exit("ERROR: nothing to plot")

    for run in runs:
        finalize(run, args.rss, args.command)
        run.timing = read_timing(run)
        run.killed = args.killed if args.killed is not None else detect_oom(run)

    for index, flag in enumerate((args.killed_a, args.killed_b, args.killed_c)):
        if flag and index < len(runs):
            runs[index].killed = True

    if args.output_dir:
        out_root = Path(args.output_dir)
        out_root.mkdir(parents=True, exist_ok=True)
    else:
        # Figures go next to the logs when they all come from one place, and to
        # the current directory when the runs are spread over several.
        roots = set()
        for run in runs:
            source = Path(run.source)
            roots.add(source if source.is_dir() else source.parent)
        out_root = roots.pop() if len(roots) == 1 else Path.cwd()

    for run in runs:
        print_summary(run, args.limit_gb, args.project_ranks)

    checks: list = []
    if len(runs) > 1 and not args.no_check_output:
        explicit = {}
        for spec in args.check_output or []:
            spec_label, spec_path, _ = split_spec(spec)
            explicit[spec_label] = str(spec_path)
        try:
            checks = compare_runs(runs, explicit, args.output_rtol, args.output_atol)
        except RuntimeError as exc:
            sys.exit(f"ERROR: {exc}")
        if checked_any(checks) or explicit:
            print(format_checks(checks, runs[0].label, args.output_rtol))
        for check in checks:
            # a truncated or unreadable output has nothing to draw: it is only
            # reported in the text, there is no comparison to make
            if check.datasets:
                plot_output_check(
                    check,
                    out_root / f"outputs_{safe(runs[0].label)}_vs_{safe(check.label)}",
                    args.output_rtol, runs[0].label)

    # --compare also draws each run on its own, so the runs are readable one by
    # one before they are overlaid.
    make_single = len(runs) == 1 or args.per_run or bool(args.compare or args.compare2)
    for run in runs:
        stem = out_root / f"memory_{safe(run.label)}"
        if make_single and not args.no_overview:
            plot_overview(run, stem, args.limit_gb, args.project_ranks, title=args.title)
        if make_single and not args.no_total:
            plot_total(run, stem.with_name(stem.name + "_total"))
        if args.per_node:
            plot_per_node(run, stem.with_name(stem.name + "_per_node"))
        if args.top > 0:
            plot_top(run, args.top, stem.with_name(stem.name + f"_top{args.top}"))

    if len(runs) > 1:
        base = out_root / ("compare_" + "_vs_".join(safe(r.label) for r in runs))
        plot_compare(runs, base, args.limit_gb, title=args.title)
    elif not has_series(runs[0]):
        plot_peaks(runs, out_root / f"memory_{safe(runs[0].label)}_peak")

    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        write_report(runs, report_path, args.limit_gb, args.project_ranks,
                     checks=checks, rtol=args.output_rtol)


if __name__ == "__main__":
    main()
