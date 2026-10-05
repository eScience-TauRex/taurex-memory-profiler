#!/bin/bash
# ============================================================
# memory_monitor.sh — unified TauREx memory sampler
# ============================================================
#
# Every INTERVAL seconds it samples both the node and every
# process, and appends one row per sample to two CSV files:
#
#   <outdir>/node_memory_<jobid>_<node>.csv  one row per node sample
#   <outdir>/memory_<jobid>_<node>.csv       one row per process sample
#
# Node file : MemTotal/MemAvailable/MemUsed, the Slurm job cgroup
#             counter (the number the OOM killer looks at), swap,
#             load average and the per-user RSS/Pss summary.
# Process   : RSS, Pss, VSZ, VmPeak, RssAnon/RssFile/RssShmem,
#             VmData and thread count for every process of the user.
#
# Why Pss: RSS double-counts pages shared between MPI ranks (shared
# opacities, mmap'ed libraries). Pss (Proportional Set Size, from
# /proc/<pid>/smaps_rollup) divides every shared page by the number
# of processes mapping it, so sum(Pss) is the true physical
# footprint — use it to size `--mem`. RSS is still logged for
# comparison and for kernels without smaps_rollup.
#
# Works both inside a Slurm allocation (one monitor per node, see
# mem-run) and standalone on a login node.
#
# Usage:
#   ./memory_monitor.sh [-o OUTDIR] [-i INTERVAL] [-p PATTERN] [-j JOBID]
#
#   -o, --outdir DIR     directory for the CSVs        [memory_logs]
#   -i, --interval SEC   sampling interval, may be < 1  [$MEM_INTERVAL or 5]
#   -p, --pattern REGEX  only track processes whose name matches
#                        (default ".", i.e. every process of the user;
#                        use e.g. 'taurex|prtrun' to reduce noise)
#   -j, --jobid ID       job id used in the file names  [$SLURM_JOB_ID or local]
#
# Stop it with SIGTERM/SIGINT, or by creating <outdir>/.stop_<jobid>
# (the latter is what mem-run uses to stop every node at once).
#
# The CSVs are appended and flushed after every sample, so they
# survive even if the job is OOM-killed.
# ============================================================

set -u

OUTDIR="memory_logs"
INTERVAL="${MEM_INTERVAL:-5}"
PATTERN="${MEM_PATTERN:-.}"
JOBID="${SLURM_JOB_ID:-local}"

usage() {
    sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        -o|--outdir)   OUTDIR="$2"; shift 2 ;;
        -i|--interval) INTERVAL="$2"; shift 2 ;;
        -p|--pattern)  PATTERN="$2"; shift 2 ;;
        -j|--jobid)    JOBID="$2"; shift 2 ;;
        -h|--help)     usage 0 ;;
        *) echo "memory_monitor.sh: unknown option '$1'" >&2; usage 1 ;;
    esac
done

OUTDIR="${OUTDIR%/}"
NODE="$(hostname -s)"
MYUID="$(id -u)"

PROCESS_LOG="${OUTDIR}/memory_${JOBID}_${NODE}.csv"
NODE_LOG="${OUTDIR}/node_memory_${JOBID}_${NODE}.csv"
STOP_FILE="${OUTDIR}/.stop_${JOBID}"

# The stop file is created/removed by the launcher, never by the
# monitors themselves: with several nodes they would race.
trap 'exit 0' TERM INT

mkdir -p "$OUTDIR"

# ------------------------------------------------------------
# Locate the Slurm job cgroup memory counter (this is what the
# OOM killer looks at). Works for cgroup v2 and v1; left empty
# if neither is found.
# ------------------------------------------------------------

CGROUP_FILE=""

REL=$(awk -F: '$1 == "0" {print $3}' /proc/self/cgroup 2>/dev/null)
if [ -n "$REL" ]; then
    DIR="/sys/fs/cgroup${REL}"
    case "$DIR" in
        */job_${JOBID}*) DIR="${DIR%%/job_${JOBID}*}/job_${JOBID}" ;;
    esac
    [ -r "$DIR/memory.current" ] && CGROUP_FILE="$DIR/memory.current"
fi

if [ -z "$CGROUP_FILE" ]; then
    REL=$(awk -F: '$2 ~ /(^|,)memory(,|$)/ {print $3}' /proc/self/cgroup 2>/dev/null)
    if [ -n "$REL" ]; then
        DIR="/sys/fs/cgroup/memory${REL}"
        case "$DIR" in
            */job_${JOBID}*) DIR="${DIR%%/job_${JOBID}*}/job_${JOBID}" ;;
        esac
        [ -r "$DIR/memory.usage_in_bytes" ] && CGROUP_FILE="$DIR/memory.usage_in_bytes"
    fi
fi

echo "============================================================"
echo "MEMORY MONITOR"
echo "============================================================"
echo "Node       : ${NODE}"
echo "Job ID     : ${JOBID}"
echo "Interval   : ${INTERVAL} s"
echo "Pattern    : ${PATTERN}"
echo "Process log: ${PROCESS_LOG}"
echo "Node log   : ${NODE_LOG}"
echo "Cgroup file: ${CGROUP_FILE:-not found}"
echo "============================================================"

# ------------------------------------------------------------
# CSV headers
# ------------------------------------------------------------

echo "timestamp,node,pid,ppid,command,rss_kb,pss_kb,vsz_kb,vmpeak_kb,data_kb,threads,rss_anon_kb,rss_file_kb,rss_shmem_kb,elapsed_s" \
    > "$PROCESS_LOG"

echo "timestamp,node,mem_total_kb,mem_available_kb,mem_used_kb,mem_used_percent,cgroup_mem_kb,swap_used_kb,shmem_kb,load1,load5,load15,user_procs,user_rss_kb,user_pss_kb,user_rss_anon_kb,elapsed_s" \
    > "$NODE_LOG"

# ------------------------------------------------------------
# Main monitoring loop
# ------------------------------------------------------------

INTERVAL_MS=$(awk -v i="$INTERVAL" 'BEGIN { printf "%d", i * 1000 + 0.5 }')
T0_MS=$(date +%s%3N)
NEXT_MS=$T0_MS

while [ ! -e "$STOP_FILE" ]; do

    # ISO timestamp with milliseconds: readable, sortable and
    # precise enough for sampling intervals well below a second.
    TS=$(date '+%Y-%m-%d %H:%M:%S.%3N')
    NOW_MS=$(date +%s%3N)
    ELAPSED=$(awk -v a="$NOW_MS" -v b="$T0_MS" 'BEGIN { printf "%.3f", (a - b) / 1000 }')

    # ========================================================
    # PROCESS MEMORY
    #
    # One awk call reads /proc/<pid>/status (and smaps_rollup for
    # Pss) of every process owned by this user whose name matches
    # PATTERN, appends one row per process to the process log and
    # returns "n_procs,sum_rss,sum_pss,sum_rss_anon" for the node
    # log.
    #
    # NOTE: never assign to PPID in bash, it is a read-only shell
    # variable (this is what killed an earlier version of this
    # sampler).
    # ========================================================

    SUMMARY=$(pgrep -u "$MYUID" | awk \
        -v ts="$TS" \
        -v node="$NODE" \
        -v el="$ELAPSED" \
        -v pat="$PATTERN" \
        -v out="$PROCESS_LOG" '
        {
            pid = $1
            f = "/proc/" pid "/status"
            name = ""; parent = 0
            rss = 0; vsz = 0; vpeak = 0; shm = 0; data = 0; thr = 0; anon = 0; file = 0
            pss = 0

            # processes that vanished in the meantime are simply skipped
            while ((getline line < f) > 0) {
                split(line, a, " ")
                if      (a[1] == "Name:")     { name = substr(line, 6); gsub(/^[ \t]+/, "", name); gsub(/,/, "_", name) }
                else if (a[1] == "PPid:")     parent = a[2]
                else if (a[1] == "VmSize:")   vsz    = a[2]
                else if (a[1] == "VmPeak:")   vpeak  = a[2]
                else if (a[1] == "VmRSS:")    rss    = a[2]
                else if (a[1] == "RssAnon:")  anon   = a[2]
                else if (a[1] == "RssFile:")  file   = a[2]
                else if (a[1] == "RssShmem:") shm    = a[2]
                else if (a[1] == "VmData:")   data   = a[2]
                else if (a[1] == "Threads:")  thr    = a[2]
            }
            close(f)

            if (name == "" || name !~ pat) next

            # Pss from smaps_rollup (Linux 4.14+) — single fast read
            sf = "/proc/" pid "/smaps_rollup"
            while ((getline line < sf) > 0) {
                if (line ~ /^Pss:/) { split(line, a, " "); pss = a[2]; break }
            }
            close(sf)

            if (rss > 0) {
                printf "%s,%s,%s,%s,%s,%d,%d,%d,%d,%d,%d,%d,%d,%d,%s\n", \
                    ts, node, pid, parent, name, rss, pss, vsz, vpeak, data, thr, anon, file, shm, el >> out
                n++; trss += rss; tpss += pss; tanon += anon
            }
        }
        END { printf "%d,%d,%d,%d", n, trss, tpss, tanon }
    ' 2>/dev/null)

    SUMMARY=${SUMMARY:-0,0,0,0}
    IFS=, read -r NPROCS TRSS TPSS TANON <<< "$SUMMARY"

    # ========================================================
    # NODE MEMORY
    # ========================================================

    CGROUP_KB=""
    if [ -n "$CGROUP_FILE" ] && read -r CGROUP_BYTES < "$CGROUP_FILE" 2>/dev/null; then
        CGROUP_KB=$((CGROUP_BYTES / 1024))
    fi

    awk \
        -v ts="$TS" \
        -v node="$NODE" \
        -v el="$ELAPSED" \
        -v cg="$CGROUP_KB" \
        -v nprocs="$NPROCS" \
        -v trss="$TRSS" \
        -v tpss="$TPSS" \
        -v tanon="$TANON" '
        /^MemTotal:/     { total = $2 }
        /^MemAvailable:/ { avail = $2 }
        /^Shmem:/        { shmem = $2 }
        /^SwapTotal:/    { swapt = $2 }
        /^SwapFree:/     { swapf = $2 }
        FILENAME == "/proc/loadavg" { l1 = $1; l5 = $2; l15 = $3 }
        END {
            used = total - avail
            swap = swapt - swapf
            printf "%s,%s,%d,%d,%d,%.2f,%s,%d,%d,%s,%s,%s,%s,%s,%s,%s,%s\n", \
                ts, node, total, avail, used, (total > 0 ? 100 * used / total : 0), \
                cg, swap, shmem, l1, l5, l15, nprocs, trss, tpss, tanon, el
        }
    ' /proc/meminfo /proc/loadavg >> "$NODE_LOG"

    # ========================================================
    # Sleep until the next sample (no drift)
    # ========================================================

    NEXT_MS=$((NEXT_MS + INTERVAL_MS))
    NOW_MS=$(date +%s%3N)
    WAIT_MS=$((NEXT_MS - NOW_MS))

    if [ "$WAIT_MS" -gt 0 ]; then
        sleep "$(awk -v w="$WAIT_MS" 'BEGIN { printf "%.3f", w / 1000 }')"
    else
        NEXT_MS=$NOW_MS
    fi

done
