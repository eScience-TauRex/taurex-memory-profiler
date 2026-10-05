#!/usr/bin/env python3
"""Compare the output files of the runs that are being compared.

A memory comparison only means something if the runs did the same work, so the
plotter also checks their outputs: two HDF5 files are walked dataset by dataset
and compared within a tolerance, any other file byte by byte.

The output of a run is the ``-o`` of the command recorded in its
``run_<jobid>.meta``, looked up next to the logs and one directory up; pass
``--check-output`` to point at it explicitly.
"""

from __future__ import annotations

import hashlib
import shlex
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

CHUNK = 1 << 16
MAX_LISTED = 10


@dataclass
class DatasetDiff:
    """One dataset of the compared outputs."""

    name: str
    rel: float | None = None     # max|a-b| / max|b|, None when not numeric
    left: float | None = None    # the worst element: the baseline value
    right: float | None = None   #                    and the other run's value
    same: bool = True            # within --output-rtol/--output-atol
    message: str = ""


@dataclass
class Check:
    """One run's output compared against the baseline run's output."""

    label: str
    path: Path | None = None          # this run's output, None when not found
    baseline: Path | None = None      # the output it was compared with
    kind: str = "missing"             # 'hdf5', 'bytes' or 'missing'
    same: bool = False
    total: int = 0                    # datasets compared (HDF5)
    worst: float | None = None        # largest relative difference
    datasets: list[DatasetDiff] = field(default_factory=list)   # numeric, worst first
    diffs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)   # structure, or why not checked
    note: str = ""


def output_argument(command: str) -> str | None:
    """The file after ``-o``/``--output`` in a recorded command line."""
    if not command:
        return None
    parts = shlex.split(command)
    for i, part in enumerate(parts):
        if part in ("-o", "--output") and i + 1 < len(parts):
            return parts[i + 1]
        if part.startswith("--output=") and len(part) > len("--output="):
            return part[len("--output="):]
    return None


def _roots(source: str) -> list[Path]:
    """Where a relative output file is looked for: the log dir, its parent, cwd."""
    base = Path(source).expanduser()
    if not base.is_dir():
        base = base.parent
    seen, roots = set(), []
    for root in (base, base.parent, Path.cwd()):
        if root.resolve() not in seen:
            seen.add(root.resolve())
            roots.append(root)
    return roots


def locate_output(source: str, name: str) -> Path | None:
    """Resolve an output file name against the run's surroundings."""
    want = Path(name).expanduser()
    if want.is_absolute():
        return want if want.is_file() else None
    for root in _roots(source):
        candidate = root / want
        if candidate.is_file():
            return candidate
    return None


def run_output(source: str, meta: dict, label: str | None,
               explicit: dict) -> tuple[Path | None, str]:
    """A run's output file: an explicit one, else the recorded ``-o``.

    Returns the path and how it was found (``None`` when nothing was given).
    """
    for key in (label, meta.get("label"), None):
        if key in explicit:
            name = explicit[key]
            return locate_output(source, name), f"--check-output {name}"
    name = output_argument(meta.get("command", ""))
    if not name:
        return None, ""
    return locate_output(source, name), f"-o {name}"


def _label_of(run) -> str:
    return run.label or run.meta.get("label") or run.meta.get("jobid") or run.source


def _digest(path: Path) -> str:
    sha = hashlib.sha1()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            sha.update(block)
    return sha.hexdigest()[:12]


def _short(text: str, limit: int = 48) -> str:
    text = text if len(text) <= limit else text[: limit - 3] + "..."
    return repr(text)


def _as_text(values: np.ndarray) -> list[str]:
    return [v.decode() if isinstance(v, bytes) else str(v)
            for v in np.asarray(values, dtype=object).reshape(-1)]


def _dataset_diff(name: str, left, right, rtol: float, atol: float) -> DatasetDiff | None:
    """The measurement of one dataset, or None when there is nothing to say.

    The relative difference is ``max|a-b| / max|b|``, so it is not inflated by
    elements that are simply close to zero: 1 means the dataset changed by its
    own magnitude, 0.01 by one percent of it. Every numeric dataset gets one, so
    the figure can show the whole spread and not only the failures.
    """
    a, b = np.asarray(left), np.asarray(right)
    if a.shape != b.shape:
        return DatasetDiff(name=name, same=False,
                           message=f"{name}: shape {a.shape} vs {b.shape}")
    if a.dtype.kind in "fiub" and b.dtype.kind in "fiub":
        if a.size == 0:
            return DatasetDiff(name=name)
        same = bool(np.allclose(a, b, rtol=rtol, atol=atol, equal_nan=True))
        delta = np.abs(a.astype("f8") - b.astype("f8")).reshape(-1)
        worst = int(np.argmax(delta))
        largest = float(np.max(np.abs(b)))
        if largest > 0:
            rel = float(delta[worst]) / largest
        else:
            rel = 0.0 if delta[worst] == 0 else float("inf")
        diff = DatasetDiff(name=name, rel=rel, same=same,
                           left=float(a.reshape(-1)[worst]),
                           right=float(b.reshape(-1)[worst]))
        if not same:
            diff.message = (f"{name}: relative difference {rel:.3g} "
                            f"({diff.left:.6g} vs {diff.right:.6g})")
        return diff
    text_a, text_b = _as_text(a), _as_text(b)
    if text_a == text_b:
        return None
    for i, (x, y) in enumerate(zip(text_a, text_b)):
        if x != y:
            return DatasetDiff(name=name, same=False,
                               message=f"{name}[{i}]: {_short(x)} vs {_short(y)}")
    return DatasetDiff(name=name, same=False,
                       message=f"{name}: {len(text_a)} vs {len(text_b)} values")


def compare_hdf5(path_a: Path, path_b: Path, rtol: float, atol: float):
    """(datasets compared, measured datasets, differences, structure notes)."""
    try:
        import h5py
    except ImportError:
        raise RuntimeError(
            f"{path_a.name} is an HDF5 file but h5py is not installed "
            "(pip install 'taurex-memory-profiler[hdf5]')") from None

    measured: list[DatasetDiff] = []
    failed: list[DatasetDiff] = []
    notes: list[str] = []
    total = 0

    def datasets(handle) -> dict:
        found: dict = {}
        handle.visititems(
            lambda name, obj: found.__setitem__(name, obj)
            if isinstance(obj, h5py.Dataset) else None)
        return found

    # A run killed before it finished writing (an OOM kill, typically) leaves an
    # incomplete HDF5 file behind, which h5py refuses to open; report that as a
    # note instead of letting the whole comparison die on it.
    try:
        with h5py.File(path_a, "r") as fa, h5py.File(path_b, "r") as fb:
            in_a, in_b = datasets(fa), datasets(fb)
            for name in sorted(set(in_a) - set(in_b)):
                notes.append(f"{name}: only in {path_a.name}")
            for name in sorted(set(in_b) - set(in_a)):
                notes.append(f"{name}: only in {path_b.name}")
            for name in sorted(set(in_a) & set(in_b)):
                if in_a[name].dtype != in_b[name].dtype:
                    notes.append(f"{name}: {in_a[name].dtype} vs {in_b[name].dtype}")
                total += 1
                diff = _dataset_diff(name, in_a[name][()], in_b[name][()], rtol, atol)
                if diff is None:
                    continue
                if diff.rel is not None:
                    measured.append(diff)
                if not diff.same:
                    failed.append(diff)
    except OSError as exc:
        return 0, [], [], [f"unreadable HDF5 output ({path_a.name} vs "
                           f"{path_b.name}): {exc}"]

    measured.sort(key=lambda d: d.rel, reverse=True)
    failed.sort(key=lambda d: (d.rel is None, -d.rel if d.rel is not None else 0.0))
    return total, measured, failed, notes


HDF5_SUFFIXES = (".h5", ".hdf5")


def compare_pair(path_a: Path, path_b: Path, rtol: float = 1e-6, atol: float = 0.0):
    """Compare two output files: HDF5 dataset by dataset, else byte by byte."""
    if not path_b.is_file():
        return {"kind": "missing", "diffs": [], "notes": [f"{path_b.name} not found"],
                "worst": None}
    hdf5_a = path_a.suffix.lower() in HDF5_SUFFIXES
    hdf5_b = path_b.suffix.lower() in HDF5_SUFFIXES
    if hdf5_a != hdf5_b:
        return {"kind": "other", "total": 0, "diffs": [], "worst": None,
                "notes": [f"different kinds of file: {path_a.name} vs {path_b.name}"]}
    if hdf5_a:
        total, measured, failed, notes = compare_hdf5(path_a, path_b, rtol, atol)
        worst = next((d.rel for d in failed if d.rel is not None), None)
        return {"kind": "hdf5", "total": total, "datasets": measured,
                "diffs": failed, "notes": notes, "worst": worst}
    size_a, size_b = path_a.stat().st_size, path_b.stat().st_size
    if size_a == size_b and _digest(path_a) == _digest(path_b):
        return {"kind": "bytes", "total": 1, "diffs": [], "notes": [], "worst": None}
    return {"kind": "bytes", "total": 1, "diffs": [], "worst": None,
            "notes": [f"not byte-identical: {size_a} vs {size_b} bytes, "
                      f"{_digest(path_a)} vs {_digest(path_b)}"]}


def compare_runs(runs: list, explicit: dict | None = None,
                 rtol: float = 1e-6, atol: float = 0.0) -> list[Check]:
    """Compare the output of every run against the first (baseline) run."""
    explicit = {k: str(v) for k, v in (explicit or {}).items()}
    baseline, base_how = run_output(runs[0].source, runs[0].meta,
                                    _label_of(runs[0]), explicit)
    checks = []
    for run in runs[1:]:
        check = Check(label=_label_of(run), baseline=baseline)
        check.path, how = run_output(run.source, run.meta, _label_of(run), explicit)
        if baseline is None:
            check.note = (f"the baseline output is not available ({base_how} not found)"
                          if base_how else
                          "the baseline run has no output file recorded; "
                          "use --check-output")
        elif check.path is None:
            check.note = (f"{how} not found" if how else
                          "no output file recorded in the run metadata; "
                          "use --check-output")
        else:
            result = compare_pair(baseline, check.path, rtol, atol)
            check.kind = result["kind"]
            check.total = result.get("total", 0)
            check.worst = result.get("worst")
            check.datasets = result.get("datasets", [])
            check.diffs = [diff.message for diff in result.get("diffs", [])]
            check.notes = result.get("notes", [])
            check.same = not check.diffs and not check.notes
        checks.append(check)
    return checks


def checked_any(checks: list[Check]) -> bool:
    """True when at least one run was actually compared."""
    return any(check.kind != "missing" for check in checks)


def format_checks(checks: list[Check], baseline_label: str, rtol: float) -> str:
    """The stdout block of the output check."""
    lines = [f"--- Output check (baseline {baseline_label}) ---"]
    for check in checks:
        if check.kind == "missing":
            lines.append(f"  {check.label}: NOT CHECKED - {check.note}")
            continue
        if check.same:
            if check.kind == "hdf5":
                unit = f"{check.total} datasets, rtol {rtol:g}"
            else:
                unit = "byte-identical"
            lines.append(f"  {check.label}: {check.path.name} - same ({unit})")
            continue
        problems = check.diffs + check.notes
        if check.kind == "hdf5":
            worst = "" if check.worst is None else f", largest {check.worst:.3g} relative"
            count = (f"{len(problems)} of {check.total} datasets" if check.total
                     else "unreadable output")
            head = (f"{check.label}: {check.path.name} - DIFFERENT from "
                    f"{check.baseline.name} ({count}{worst})")
        else:
            head = (f"{check.label}: {check.path.name} - DIFFERENT from "
                    f"{check.baseline.name}")
        lines.append(head)
        for problem in problems[:MAX_LISTED]:
            lines.append(f"    - {problem}")
        if len(problems) > MAX_LISTED:
            lines.append(f"    - ... and {len(problems) - MAX_LISTED} more")
    return "\n".join(lines)


def report_lines(checks: list[Check], rtol: float) -> list[str]:
    """The markdown section of the output check."""
    lines = ["", "## Output check", "",
             "The runs are only comparable if they wrote the same output.", "",
             "| run | output | verdict |", "|---|---|---|"]
    for check in checks:
        if check.kind == "missing":
            lines.append(f"| `{check.label}` | - | not checked ({check.note}) |")
        elif check.same:
            unit = f"{check.total} datasets" if check.kind == "hdf5" else "byte-identical"
            lines.append(f"| `{check.label}` | `{check.path.name}` | same ({unit}) |")
        elif check.kind == "hdf5":
            count = len(check.diffs) + len(check.notes)
            worst = "" if check.worst is None else f", largest {check.worst:.3g} relative"
            what = (f"{count} of {check.total} datasets" if check.total
                    else "unreadable output")
            lines.append(f"| `{check.label}` | `{check.path.name}` | "
                         f"**different** ({what}{worst}) |")
        else:
            lines.append(f"| `{check.label}` | `{check.path.name}` | **different** |")
    for check in checks:
        problems = check.diffs + check.notes
        if check.kind == "missing" or check.same or not problems:
            continue
        title = f"`{check.path.name}` vs `{check.baseline.name}`"
        if check.kind == "hdf5":
            title += f" (tolerance rtol {rtol:g}, largest difference first)"
        lines += ["", title + ":", ""]
        for problem in problems[:MAX_LISTED]:
            lines.append(f"* `{problem}`")
        if len(problems) > MAX_LISTED:
            lines.append(f"* ... and {len(problems) - MAX_LISTED} more")
    lines.append("")
    return lines
