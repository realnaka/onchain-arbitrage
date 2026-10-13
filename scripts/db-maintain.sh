#!/bin/zsh
# Keep the monitor database from growing without bound.
#
#   scripts/db-maintain.sh --dry-run   # print the plan, change nothing
#   scripts/db-maintain.sh             # prune (online; the monitor keeps running)
#   scripts/db-maintain.sh --vacuum    # prune, then stop monitor+board, rewrite the
#                                      # file so the space returns to the disk, restart
#
# Why two modes: deleting rows only returns pages to SQLite's freelist — the file
# keeps its size until it is rewritten. Rewriting needs a quiet database, so the
# quiet part is its own step with its own flag. The pruning itself is safe to run
# against the live monitor: each batch writes its statistics and deletes its rows
# in one transaction.
#
# Thresholds live in config/hk_monitor.toml [retention]; see docs/retention.md.
set -u

script_dir="${0:A:h}"
repo_dir="${script_dir:h}"
python="${repo_dir}/.venv/bin/python"
db="${repo_dir}/data/hk_monitor.db"
config="${repo_dir}/config/hk_monitor.toml"

vacuum=0
dry=""
for arg in "$@"; do
  case "$arg" in
    --vacuum) vacuum=1 ;;
    --dry-run) dry="--dry-run" ;;
    -h|--help)
      print -u2 "usage: $0 [--dry-run] [--vacuum]"
      exit 0
      ;;
    *)
      print -u2 "unknown argument: $arg"
      exit 64
      ;;
  esac
done

if [[ ! -x "${python}" ]]; then
  print -u2 "missing venv interpreter: ${python}"
  exit 78
fi
if [[ ! -f "${db}" ]]; then
  print -u2 "missing monitor database: ${db}"
  exit 78
fi
cd "${repo_dir}" || exit 78

file_mb() {
  print -r -- "$(( $(stat -f%z "$1") / 1048576 ))"
}

monitor_running() { pgrep -f "arb_monitor hk-monitor --cycles 0" 2>/dev/null }
board_running() { pgrep -f "arb_monitor board --port" 2>/dev/null }
wrapper_running() { pgrep -f "run_hk_monitor\.sh" 2>/dev/null }

# Stopping is not "send a signal once": run_hk_monitor.sh is a supervisor that
# restarts the monitor 10 s after it exits, and it can win the race. If it does,
# the monitor keeps writing into the *old* inode while the rewrite swaps the file
# out from under it (observed 2026-09-17 15:18). So: kill the pair, then keep
# killing until the process tree is really gone.
stop_quietly() {
  local attempt pids
  for attempt in {1..15}; do
    pids="$(monitor_running) $(wrapper_running) $(board_running)"
    pids="$(print -r -- "${pids}" | tr ' ' '\n' | sort -u | tr '\n' ' ')"
    [[ -z "${pids// /}" ]] && return 0
    if [[ ${attempt} -eq 1 ]]; then
      "${script_dir}/board-ctl.sh" stop >/dev/null 2>&1 || true
      "${script_dir}/hk-monitor-ctl.sh" stop >/dev/null 2>&1 || true
    fi
    print -r -- "${pids}" | xargs kill -9 2>/dev/null
    sleep 1
  done
  print -u2 "still running after 15 s: ${pids}"
  return 1
}

monitor_was_running=0
board_was_running=0

restore_services() {
  # Whatever happened below, the monitoring must not stay down because of a
  # maintenance run: restarting is unconditional on the way out.
  if [[ "${board_was_running}" == "1" ]]; then
    "${script_dir}/board-ctl.sh" start >/dev/null 2>&1 || true
  fi
  if [[ "${monitor_was_running}" == "1" ]]; then
    "${script_dir}/hk-monitor-ctl.sh" start >/dev/null 2>&1 || true
  fi
}

print "== 清理前 =="
print "  ${db}: $(file_mb "${db}") MB"
"${python}" -m arb_monitor db-retention --config "${config}" --db "${db}" ${dry}
rc=$?
if [[ ${rc} -ne 0 ]]; then
  print -u2 "db-retention failed: ${rc}"
  exit ${rc}
fi

if [[ ${vacuum} -eq 1 && -z "${dry}" ]]; then
  # The rewrite needs ~2x the file size free (the copy plus the old file kept as
  # a backup), so refuse early instead of failing halfway through.
  need_mb=$(( $(file_mb "${db}") * 2 + 512 ))
  free_mb=$(( $(df -m "${repo_dir}" | awk 'NR==2 {print $4}') ))
  if [[ ${free_mb} -lt ${need_mb} ]]; then
    print -u2 "not enough free space: ${free_mb} MB free, ${need_mb} MB needed"
    exit 75
  fi
  [[ -n "$(monitor_running)" ]] && monitor_was_running=1
  [[ -n "$(board_running)" ]] && board_was_running=1
  trap restore_services EXIT INT TERM
  if ! stop_quietly; then
    print -u2 "拒绝重写：监控/看板仍在运行"
    exit 75
  fi
  print "== 重写文件 =="
  "${python}" -m arb_monitor db-retention --config "${config}" --db "${db}" --vacuum
  rc=$?
  restore_services
  trap - EXIT INT TERM
  if [[ ${rc} -ne 0 ]]; then
    print -u2 "db-retention --vacuum failed: ${rc}"
    exit ${rc}
  fi
fi

print "== 清理后 =="
print "  ${db}: $(file_mb "${db}") MB"
print "  统计仍在 event_daily_stats / venue_daily_stats；"
print "  旧库备份（若生成）在 data/ 下，核对后自行删除。"
