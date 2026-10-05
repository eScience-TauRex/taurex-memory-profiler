# TauREx memory toolbox

Measure and plot where the memory of a TauREx run goes: per node, per MPI rank,
and per process, with optional CPU profiling and timing from the same run.

* per-process **RSS, Pss, RssAnon/RssFile/RssShmem, VmSize, VmPeak** and threads
* node memory against the **physical memory**, the Slurm **`--mem`** limit and
  the job **cgroup** counter (the number the OOM killer uses)
* one monitor per node, so multi-node runs are covered
* Pss by default, which counts MPI-shared pages (opacities) once
* 2- and many-way comparisons, per-node and top-N breakdowns, OOM detection
* optional py-spy CPU flamegraph and wall-clock/fit/sampling timing
* peak memory from `sacct` when a run had no monitor
* a shareable markdown report

| file | role |
|---|---|
| [`mem-run`](./mem-run)                 | run any command with the monitor beside it — the one-liner |
| [`memory_monitor.sh`](./memory_monitor.sh) | samples node + every process into two CSVs per node |
| [`plot_memory.py`](./plot_memory.py)   | all the figures, the text summary and the report |
| [`tests/test_plot_memory.py`](./tests/test_plot_memory.py) | self-contained checks |
| [`pyproject.toml`](./pyproject.toml)   | dependencies, the `memory-plot` command and the test/lint config |

Requirements: bash ≥ 4.2, GNU awk, `pgrep`, and `python3` with `numpy`, `pandas`
and `matplotlib`. `py-spy` only if you want the CPU flamegraph.

Everything can be driven from the toolbox directory; the only thing you have to
decide is where the sampler writes and reads its logs (`-o` / `--logdir`,
default `./memory_logs`).

## Install

```bash
cd memory_toolbox
python3 -m venv .venv && source .venv/bin/activate

pip install -e .            # numpy, pandas, matplotlib
pip install -e ".[pyspy]"   # …plus py-spy, for the CPU flamegraph
```

That gives you the same plotter as a `memory-plot` command (`memory-plot` and
`python plot_memory.py` are interchangeable). The shell side is not a Python
package: put `mem-run` and `memory_monitor.sh` on your `PATH`, or spell out
`memory_toolbox/mem-run` as in the examples below. On a cluster the interpreter
often comes from a module, and `pip install -e .` is then only needed once per
environment.

The checks need no test runner — `python tests/test_plot_memory.py` — but
`pip install -e ".[tests]"` adds pytest if you prefer `pytest tests/`.

## Quick start

```bash
cd memory_toolbox

# 1. monitored run: prefix your MPI command with mem-run and give it a label
mem-run -l mem_64_good -- mpirun -np 256 taurex -i parfile.par --retrieval -o output.hdf5 --light

# 2. plot the latest run in ./memory_logs
python plot_memory.py

# 3. compare a group of runs (see "Working with a grid of runs")
python plot_memory.py --logdir memory_logs --select 'mem_64_*' \
    --report memory_logs/report_64.md
```

---

## Running a job under the monitor: `mem-run`

```bash
mem-run [options] [--] COMMAND [ARGS...]
```

`mem-run` is a wrapper: it starts one `memory_monitor.sh` per node, waits for the
first samples, runs your command, stops the monitors on every exit path (so the
CSVs survive an OOM kill), writes `run_<jobid>.meta`, and prints the command to
plot the result. **It returns your command's exit code unchanged**, so `sbatch`
still reports a failed or killed retrieval as such.

An existing job script changes in exactly one line:

```diff
-mpirun -np 256 taurex -i parfile.par --retrieval -o output.hdf5 --light
+mem-run -l mem_128_good -- mpirun -np 256 taurex -i parfile.par --retrieval -o output.hdf5 --light
```

Everything else — modules, venv, `#SBATCH` lines, `-np 256` — stays in your
script. A complete example:

```bash
#!/bin/bash
#SBATCH --nodes=2
#SBATCH --ntasks-per-node=128
#SBATCH --mem=224G
#SBATCH --partition=rome
#SBATCH --time=2-00:00:00
#SBATCH --output=slurm_%x_%j.out

module purge
module load 2025 foss/2025b Python/3.13.5-GCCcore-14.3.0
export LD_LIBRARY_PATH=/projects/prjs1336/Software/2026/MultiNest/lib:$LD_LIBRARY_PATH
source /projects/prjs1336/Software/2026/taurex34/bin/activate

mem-run -l mem_128_good -- mpirun -np 256 taurex -i parfile.par --retrieval -o output.hdf5 --light
```

Put `mem-run` on your `PATH`, or spell out `memory_toolbox/mem-run` as above.

### Options

| option | default | meaning |
|---|---|---|
| `-o, --outdir DIR` | `memory_logs` | where the CSVs go |
| `-i, --interval SEC` | `5` | sampling interval (may be < 1) |
| `-p, --pattern REGEX` | `.` | only track process names matching, e.g. `'taurex\|prterun'` |
| `-j, --jobid ID` | `$SLURM_JOB_ID` or `local` | used in the file names |
| `-l, --label NAME` | job id | label used by the plots |
| `--pyspy` | off | also record a py-spy CPU flamegraph |
| `--pyspy-rate HZ` | `100` | py-spy sampling rate |
| `--pyspy-out FILE` | `profile_<label>.svg` | py-spy output file |

Environment: `MEM_MONITOR` (path to `memory_monitor.sh`, default next to
`mem-run`), `MEM_RUN_WAIT` (seconds to wait for the first samples, default
`2*interval + 2`). Under Slurm the monitors run as one overlapping `srun` step
per node; without Slurm only the local node is monitored, which makes `mem-run`
usable on a login node for short tests.

### CPU profile and timing (`--pyspy`)

`--pyspy` wraps the run in `py-spy record --subprocesses` (so the MPI ranks are
followed too) and writes a flamegraph. The `Samples: N` line py-spy prints is
read back by `plot_memory.py`, which puts the CPU cost next to the memory cost.
Requires `pip install py-spy`; the job fails immediately with a clear message if
it is missing. A 128-rank flamegraph is large, so use `--pyspy-out` to keep it
out of the way.

---

## Plotting: `plot_memory.py`

```bash
python plot_memory.py [RUN ...] [options]
```

### Choosing what to plot

| you have | command |
|---|---|
| one log directory, most recent run | `python plot_memory.py` |
| several jobs in one directory | `--job 12345` for one, `--select 'mem_64_*'` for all matching |
| one directory per run | `good=logs_good --compare bad=logs_bad` |
| individual CSVs | `mem_64.csv --compare mem_64_bad.csv` |
| only an `sacct` dump | `sacct_26968927.txt` |

A `RUN` is a CSV file, a directory of logs, or `LABEL=path[#jobid]`. `--select`
matches the run label (from `run_<jobid>.meta`) or the job id, and picks up every
match, so a whole grid in one directory plots in one command. With no `RUN` the
tool scans `--logdir` (default `memory_logs`) and uses the most recent job unless
`--job` or `--select` is given. Labels default to the `.meta` label, so name your
runs with `-l` when you submit them.

### Options

| option | meaning |
|---|---|
| `--compare RUN` | overlay another run (repeatable, any number) |
| `--compare2 RUN` | third run (shorthand) |
| `--label NAME`, `--label-a/-b/-c NAME` | labels for the runs |
| `--job ID` | one job inside a log directory |
| `--select GLOB` | every run in the directory whose label or job id matches |
| `--logdir DIR` | directory scanned when no RUN is given |
| `--rss` | plot RSS instead of Pss |
| `--command NAME` | only use these processes for the per-rank statistics |
| `--limit-gb GB` | Slurm `--mem` per node, drawn as a reference line [224] |
| `--project-ranks N` | ranks/node for the memory extrapolation [128] |
| `--per-node` | also draw one line per node |
| `--top N` | also draw the top-N processes by peak memory |
| `--per-run` | with several runs, also write the per-run figures |
| `--report PATH` | also write a markdown report (peaks, limits, timings) |
| `--title TEXT` | custom title for the comparison figure |
| `--output-dir DIR` | where to write the figures |
| `--killed` / `--not-killed` | force the OOM marker instead of auto-detecting it |

### What you get

Figures (PNG + PDF) are written next to the logs, in the current directory when
the runs come from several directories, or wherever `--output-dir` points:

* `memory_<label>.png` — overview: node memory used vs the physical memory, the
  Slurm `--mem` allocation and the cgroup counter, one column per node, with the
  per-rank smallest/average/largest below it.
* `memory_<label>_total.png` — total memory over time (all ranks, all nodes).
* `compare_<a>_vs_<b>[_vs_...].png` — overlay of the runs, with a peak table.
* `memory_<label>_peak.png` — bar chart of the peak, for runs that only have an
  `sacct` dump.
* `memory_<label>_per_node.png`, `memory_<label>_top<N>.png` — optional breakdowns.

The text summary and the report give the peak total, the peak per node (next to
the `--mem` limit), the per-rank statistics with the extrapolation to
`--project-ranks` ranks per node, and — when the job log is next to the run — the
wall clock, TauREx runtime, `optimizer.fit()`, MultiNest sampling and the py-spy
CPU sample count.

### Peaks without a monitor (`sacct`)

If a run has no sampler CSV (the monitor was not running, or you only have the
job id), use the Slurm accounting:

```bash
sacct -j 26968927 -P --format=JobID,Elapsed,MaxRSS,MaxVMSize,AveRSS > sacct_26968927.txt
python plot_memory.py sacct_26968927.txt --compare mem_64_good=memory_logs#12345
```

The dump is detected by its `MaxRSS` column; it contributes a peak to the
summary, the report and the comparison figure. A bare number is read as KiB,
which is what `sacct` reports.

---

## Working with a grid of runs

The point of a grid is to compare the same retrieval under different conditions:
node layout (32/64/128 ranks per node), code variant (`good`/`bad`/`float32`), or
environment (taurex vs taurex4-pytorch vs jax). Whatever the axis, the workflow
is the same: one job per cell with a distinct label, then plot by label.

### 1. Label every run

Submit one job per cell, passing a distinct `-l mem_<config>_<variant>`
(`mem_64_good`, `mem_128_float32`, …), and let every run write to the same
`-o`/`--logdir` (default `memory_logs`) so they land together. That label ends up
in `run_<jobid>.meta` and in the figure names, which is exactly what makes
`--select` work later. A grid that varies the node layout is just the same job
script submitted with different `--nodes`/`--ntasks-per-node` and `-l`.

### 2. Plot the grid

One run per configuration (`--select 'mem_64_*'`) gives the comparison figure.
Measuring across the whole grid at once, the report lines up every run, so the
peaks and timings can be read across the grid in one table:

```bash
python plot_memory.py --logdir memory_logs --select 'mem_*' \
    --report memory_logs/report_grid.md
```

Or one report per axis value, e.g. for a scaling study:

```bash
for cfg in 32 64 128; do
    python plot_memory.py --logdir memory_logs --select "mem_${cfg}_*" \
        --title "${cfg} ranks/node" --report "memory_logs/report_${cfg}.md"
done
```

### 3. Read the comparison

* **Peak per node against `--mem`** is the headline: it tells you whether the run
  fits and how much headroom is left. The cgroup line is what the OOM killer
  actually watches.
* **Pss, not RSS**, sizes `--mem` — RSS double-counts shared MPI pages.
* **Per-rank largest vs average** shows imbalance: a few heavy ranks can push a
  node over the limit while the average looks fine.
* **The extrapolation** (`--project-ranks`) estimates the node footprint at a
  different rank count, assuming memory scales with the ranks; use it to pick a
  layout, then confirm with a real run.
* **The report** puts memory and timing side by side, which is what you need to
  choose between environments — a run is only better if it is not slower.

### 4. Share it

The `--report` markdown plus the PNG/PDF figures are self-contained: they list
the runs, the peaks against the limit, the relative differences, the timings and
the per-node table. Copy them next to the logs, or commit them with the results.

---

## `memory_monitor.sh` (used by `mem-run`)

Normally you never call this directly; `mem-run` does. Run it standalone to
attach the sampler to something already running:

```bash
./memory_monitor.sh [-o OUTDIR] [-i INTERVAL] [-p PATTERN] [-j JOBID]
```

| option | default | meaning |
|---|---|---|
| `-o, --outdir` | `memory_logs` | where the CSVs go |
| `-i, --interval` | `5` (`$MEM_INTERVAL`) | seconds between samples, may be < 1 |
| `-p, --pattern` | `.` | only track process names matching this regex |
| `-j, --jobid` | `$SLURM_JOB_ID` or `local` | used in the file names |

Stop it with `SIGTERM`, or (from another shell) by creating
`<outdir>/.stop_<jobid>` — that file is also how `mem-run` stops every node at
once.

It writes, per node:

```
memory_logs/memory_<jobid>_<node>.csv        one row per process per sample
memory_logs/node_memory_<jobid>_<node>.csv   one row per node per sample
```

Process columns: `timestamp,node,pid,ppid,command,rss_kb,pss_kb,vsz_kb,vmpeak_kb,`
`data_kb,threads,rss_anon_kb,rss_file_kb,rss_shmem_kb,elapsed_s`.

Node columns: `timestamp,node,mem_total_kb,mem_available_kb,mem_used_kb,`
`mem_used_percent,cgroup_mem_kb,swap_used_kb,shmem_kb,load1,load5,load15,`
`user_procs,user_rss_kb,user_pss_kb,user_rss_anon_kb,elapsed_s`.

The CSVs are line-buffered, so they survive an OOM kill.

> Keep the interval sane: 5 s over 48 h and 128 ranks is ~300 MB per node, while
> 0.01 s would be ~100 GB. Sub-second sampling is only for short runs.

---

## Troubleshooting

* **"no memory logs found"** — the sampler writes to `memory_logs/` relative to
  the job's working directory. Pass the right one with `--logdir` (or `-o` when
  sampling).
* **A run has no timing rows** — timing comes from the Slurm log; make sure
  `#SBATCH --output` writes `slurm_<jobname>_<jobid>.out` in the submit
  directory, as the example does.
* **No comparison figure** — a single run gives the overview and total figures; a
  comparison needs two or more runs.
* **`--select` matches nothing** — it matches the label from
  `run_<jobid>.meta` or the job id; the error lists what is available.
* **`pss_kb` is 0 for some processes** — `/proc/<pid>/smaps_rollup` was not
  readable for them (usually processes of another user). The plotter falls back
  to RSS only when the whole column is empty.
* **OOM not marked** — detection scans the `*.out` logs next to the run for
  `oom_kill` / `Out of memory`; use `--killed` when the log is elsewhere.
* **`mem-run` not found** — it must be reachable from the job; put it on `PATH`
  or use a path relative to the submit directory (Slurm starts jobs there).
