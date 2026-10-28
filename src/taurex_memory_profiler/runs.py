"""The files one run leaves in a log directory, and the removal of the runs a
newer run with the same label has replaced.

``taurex-mem-run`` writes ``run_<jobid>.meta`` and, per node, one
``memory_<jobid>_<node>.csv`` and one ``node_memory_<jobid>_<node>.csv``; the
plotter writes its figures, its py-spy flamegraph and the report next to them,
named after the label. The run name is that label, so two runs sharing a label
also share the figure names: this module finds those runs and offers to delete
the older one, which is the clutter that would otherwise pile up.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

NODE_RE = re.compile(r"^node_memory_(?P<job>.+?)_(?P<node>.+)\.csv$")
PROC_RE = re.compile(r"^memory_(?P<job>.+?)_(?P<node>.+)\.csv$")

# The files the plotter names after the label rather than after the job id.
FIGURE_PATTERNS = ("memory_", "compare_", "report_", "profile_")
FIGURE_SUFFIXES = (".png", ".svg", ".pdf", ".md", ".html")


def safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "run"


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


def run_ids(base: Path) -> list[str]:
    """Every job id with logs in `base`."""
    ids = {path.name[len("run_"):-len(".meta")]
           for path in base.glob("run_*.meta") if path.is_file()}
    for path in base.glob("*memory_*.csv"):
        match = NODE_RE.match(path.name) or PROC_RE.match(path.name)
        if match:
            ids.add(match["job"])
    return sorted(ids)


def log_files(base: Path, jobid: str) -> list[Path]:
    """The logs of one run: its CSVs and its meta file."""
    files = [base / f"run_{jobid}.meta"]
    files += sorted(base.glob(f"memory_{jobid}_*.csv"))
    files += sorted(base.glob(f"node_memory_{jobid}_*.csv"))
    files += sorted(base.glob(f"report_{jobid}*.md"))
    return [path for path in files if path.is_file()]


def run_files(base: Path, jobid: str, label: str | None = None) -> list[Path]:
    """Everything one run left in `base`.

    With `label`, the figures and the report the plotter named after that label
    are included too: they are overwritten by the next run with the same name,
    so they belong to the name and not to a single job id. The CSVs are always
    matched by job id, so a label that happens to look like one cannot take the
    logs of the run that is being kept.
    """
    files = log_files(base, jobid)
    if label:
        stem = safe(label)
        for pattern in FIGURE_PATTERNS:
            for path in sorted(base.glob(f"{pattern}*{stem}*")):
                if path.is_file() and path.suffix in FIGURE_SUFFIXES:
                    files.append(path)
    return files


def label_of(base: Path, jobid: str) -> str:
    """The name a run is known by: its label, or the job id when it has none."""
    return read_meta(base, jobid).get("label") or jobid


def last_write(base: Path, jobid: str) -> float:
    """When the run last wrote anything (0 when nothing is left of it)."""
    return max((path.stat().st_mtime for path in log_files(base, jobid)), default=0.0)


def duplicates(base: Path, label: str) -> list[str]:
    """The job ids of the runs in `base` labelled `label`, oldest write first."""
    ids = [jobid for jobid in run_ids(base) if label_of(base, jobid) == label]
    return sorted(ids, key=lambda jobid: last_write(base, jobid))


def prune_duplicates(base: Path, label: str, keep: str | None = None,
                     policy: str | None = None) -> list[str]:
    """Delete the runs that share a label with the run that is kept.

    `keep` is the job id that stays — the run about to start, in
    ``taurex-mem-run``; without it the run written last stays. `policy` answers
    the question without asking: ``"delete"`` or ``"keep"``. Without a policy
    the question is put to the user, and only to a terminal: a batch job keeps
    everything and says so. Returns the job ids that were deleted.
    """
    ids = duplicates(base, label)
    if keep is not None:
        ids = [jobid for jobid in ids if jobid != keep]
        total = len(ids) + 1
    else:
        total = len(ids)
        ids = ids[:-1]
    if not ids:
        return []

    if policy is None:
        if not sys.stdin.isatty():
            print(f"NOTE: {total} runs are labelled '{label}' in {base}; keeping them "
                  "all (no terminal to ask on: use --delete-old or --keep-old)")
            return []
        listing = "\n".join(f"  {jobid}" for jobid in ids)
        try:
            answer = input(f"{total} runs are labelled '{label}' in {base}:\n{listing}\n"
                           f"Delete the {len(ids)} older one(s), so they do not clutter "
                           "the plots? [y/N] ")
        except EOFError:  # no answer to be had: same as "no"
            answer = ""
        policy = "delete" if answer.strip().lower() in ("y", "yes") else "keep"
    if policy != "delete":
        return []

    for jobid in ids:
        removed = run_files(base, jobid, label)
        for path in removed:
            path.unlink()
        print(f"Deleted the old run {jobid} (label '{label}', {len(removed)} files)")
    return ids
