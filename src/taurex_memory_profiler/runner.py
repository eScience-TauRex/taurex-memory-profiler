"""Run a command with the memory monitor beside it — the Python replacement
for ``mem-run``.

One line instead of the monitoring boilerplate::

    taurex-mem-run [options] [--] COMMAND [ARGS...]

so a normal job script changes in exactly one place::

    -mpirun -np 256 taurex -i parfile.par --retrieval -o output.hdf5 --light
    +taurex-mem-run -- mpirun -np 256 taurex -i parfile.par --retrieval -o output.hdf5 --light

It starts one :mod:`taurex_memory_profiler.monitor` per node (as an overlapping
Slurm step, or locally when not under Slurm), waits for the first samples, runs
the command, stops the monitors on every exit path (so the CSVs survive an OOM
kill), writes ``run_<jobid>.meta``, and prints the command to plot the result.
**The exit code of the command is returned unchanged**, so ``sbatch`` still
reports a failed or killed retrieval as such.

Options:
  -o, --outdir DIR     where the CSVs go                        [memory_logs]
  -i, --interval SEC   sampling interval                        [$MEM_INTERVAL or 5]
  -p, --pattern REGEX  process names to track                   [$MEM_PATTERN or .]
  -j, --jobid ID       job id used in the file names            [$SLURM_JOB_ID or local]
  -l, --label NAME     label for the plots                      [job id]
      --pyspy          also record a py-spy CPU flamegraph
      --pyspy-rate HZ  py-spy sampling rate                     [100]
      --pyspy-out F    py-spy output file                       [profile_<label>.svg]

Environment:
  MEM_INTERVAL  default sampling interval
  MEM_PATTERN   default process pattern
  MEM_RUN_WAIT  seconds to wait for the first samples [2*interval + 2]
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .monitor import stop_file_path

VALUE_OPTIONS = {"-o", "--outdir", "-i", "--interval", "-p", "--pattern",
                 "-j", "--jobid", "-l", "--label", "--pyspy-rate", "--pyspy-out"}


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def split_command(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split the command line at the first ``--`` or the first bare word, so
    that everything from there on belongs to the command being monitored."""
    options: list[str] = []
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "--":
            return options, argv[index + 1:]
        if not argument.startswith("-"):
            return options, argv[index:]
        options.append(argument)
        if argument in VALUE_OPTIONS and index + 1 < len(argv):
            index += 1
            options.append(argv[index])
        index += 1
    return options, []


def parse_args(argv: "list[str] | None" = None) -> argparse.Namespace:
    argv = list(sys.argv[1:] if argv is None else argv)
    options, command = split_command(argv)

    parser = argparse.ArgumentParser(
        prog="taurex-mem-run",
        description="Run COMMAND with a memory monitor sampling every node.",
        epilog="Anything after `--` is the command; its exit code is returned.",
    )
    parser.add_argument("-o", "--outdir", default="memory_logs",
                        help="where the CSVs go [memory_logs]")
    parser.add_argument("-i", "--interval", type=float,
                        default=float(os.environ.get("MEM_INTERVAL", 5)),
                        help="sampling interval in seconds [$MEM_INTERVAL or 5]")
    parser.add_argument("-p", "--pattern", default=os.environ.get("MEM_PATTERN", "."),
                        help="process names to track [$MEM_PATTERN or .]")
    parser.add_argument("-j", "--jobid", default=os.environ.get("SLURM_JOB_ID", "local"),
                        help="job id used in the file names [$SLURM_JOB_ID or local]")
    parser.add_argument("-l", "--label", default="",
                        help="label for the plots [job id]")
    parser.add_argument("--pyspy", action="store_true",
                        help="also record a py-spy CPU flamegraph")
    parser.add_argument("--pyspy-rate", default="100",
                        help="py-spy sampling rate in Hz [100]")
    parser.add_argument("--pyspy-out", default="",
                        help="py-spy output file [profile_<label>.svg]")

    args = parser.parse_args(options)
    if not command:
        parser.error("no command given (use -- before the command)")

    args.command = command
    args.outdir = args.outdir.rstrip("/") or "/"
    args.label = args.label or args.jobid
    args.pyspy_out = args.pyspy_out or f"profile_{args.label}.svg"
    return args


def monitor_command(args: argparse.Namespace) -> list[str]:
    return [sys.executable, "-m", "taurex_memory_profiler.monitor",
            "-o", args.outdir, "-i", str(args.interval), "-p", args.pattern,
            "-j", args.jobid]


def start_monitors(args: argparse.Namespace) -> subprocess.Popen:
    command = monitor_command(args)
    if os.environ.get("SLURM_JOB_ID"):
        nodes = os.environ.get("SLURM_NNODES", "1")
        # --overlap lets the monitoring step coexist with the job step
        command = ["srun", f"--nodes={nodes}", f"--ntasks={nodes}",
                   "--ntasks-per-node=1", "--cpus-per-task=1", "--overlap",
                   *command]
    return subprocess.Popen(command)


def stop_monitors(process: subprocess.Popen, args: argparse.Namespace) -> None:
    """Ask every monitor to stop through the shared stop file, then make sure
    the launcher is gone. The monitors never remove the stop file themselves:
    with several nodes they would race."""
    stop_file = stop_file_path(Path(args.outdir), args.jobid)
    try:
        stop_file.touch()
        deadline = time.monotonic() + args.interval + 10
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.2)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        else:
            process.wait()
    finally:
        stop_file.unlink(missing_ok=True)


def write_meta(args: argparse.Namespace, outdir: Path) -> Path:
    """Metadata read back by the plots (label, and where the job log is)."""
    job_name = os.environ.get("SLURM_JOB_NAME")
    submit_dir = os.environ.get("SLURM_SUBMIT_DIR", os.getcwd())
    lines = [
        f"label={args.label}",
        f"jobid={args.jobid}",
        f"nodes={os.environ.get('SLURM_NNODES', '1')}",
        f"command={' '.join(args.command)}",
        f"interval={args.interval}",
        f"pattern={args.pattern}",
        f"pyspy={int(args.pyspy)}",
        f"pyspy_out={args.pyspy_out}",
    ]
    if job_name:
        lines.append(f"log={submit_dir}/slurm_{job_name}_{args.jobid}.out")
        lines.append(f"err={submit_dir}/slurm_{job_name}_{args.jobid}.err")
    lines.append(f"start={_iso_now()}")
    path = outdir / f"run_{args.jobid}.meta"
    path.write_text("\n".join(lines) + "\n")
    return path


def wait_for_samples(args: argparse.Namespace, outdir: Path) -> None:
    """Wait for the first samples, so even a crash-on-startup run has data."""
    wait = float(os.environ.get("MEM_RUN_WAIT", 2 * args.interval + 2))
    if wait > 0:
        time.sleep(wait)

    sampled = False
    node_logs = sorted(outdir.glob(f"node_memory_{args.jobid}_*.csv"))
    if not node_logs:
        print("WARNING: no node memory log was created")
    for path in node_logs:
        samples = max(len(path.read_text().splitlines()) - 1, 0)
        print(f"{path.name}: {samples} samples so far")
        sampled = sampled or samples >= 1
    if not sampled:
        print("WARNING: the sampler has no samples yet, continuing anyway")


def run_command(args: argparse.Namespace) -> int:
    started = time.monotonic()
    print(f"Job Started: {_iso_now()}")
    if args.pyspy:
        status = subprocess.call(["py-spy", "record", "--rate", args.pyspy_rate,
                                  "--subprocesses", "-o", args.pyspy_out,
                                  "--", *args.command])
    else:
        status = subprocess.call(args.command)
    print(f"Job Finished: {_iso_now()}")
    print(f"Exit code: {status}   Runtime: {int(time.monotonic() - started)} s")
    return status


def main(argv: "list[str] | None" = None) -> int:
    args = parse_args(argv)
    outdir = Path(args.outdir)

    if args.pyspy and shutil.which("py-spy") is None:
        print("ERROR: --pyspy given but py-spy is not on PATH (pip install py-spy)",
              file=sys.stderr)
        return 1

    outdir.mkdir(parents=True, exist_ok=True)
    stop_file_path(outdir, args.jobid).unlink(missing_ok=True)

    print("\n".join([
        "============================================================",
        "MEMORY MONITORED RUN",
        "============================================================",
        f"Command  : {' '.join(args.command)}",
        f"Job ID   : {args.jobid}   (label '{args.label}')",
        f"Nodes    : {os.environ.get('SLURM_NNODES', '1')}",
        f"Interval : {args.interval} s   (pattern '{args.pattern}')",
        f"Logs     : {outdir}",
        *([f"py-spy   : {args.pyspy_rate} Hz -> {args.pyspy_out}"] if args.pyspy else []),
        "============================================================",
    ]))

    monitors = start_monitors(args)
    try:
        write_meta(args, outdir)
        wait_for_samples(args, outdir)
        status = run_command(args)
    finally:
        stop_monitors(monitors, args)

    print("\n".join([
        "============================================================",
        "Plot with:",
        f"  taurex-mem-plot --logdir {outdir} --job {args.jobid} "
        f"--per-node --top 10 --report {outdir}/report_{args.jobid}.md",
        *([f"  (CPU flamegraph: {args.pyspy_out})"] if args.pyspy else []),
        "============================================================",
    ]))
    return status


if __name__ == "__main__":
    sys.exit(main())
