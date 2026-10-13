#!/bin/zsh
# Start / stop / inspect the combined read-only board (market + account tabs).
#
#   scripts/board-ctl.sh start [port]
#   scripts/board-ctl.sh stop
#   scripts/board-ctl.sh status
#
# One process, one URL, two tabs. It only reads: the monitor database for the
# market side, and OKX signed GETs + the X Layer wallet balance + the execution
# ledger for the account side. It never places, cancels, signs or transfers
# anything.
set -u

script_dir="${0:A:h}"
repo_dir="${script_dir:h}"
python="${repo_dir}/.venv/bin/python"
log_file="${repo_dir}/data/logs/board.log"
pid_file="${repo_dir}/data/logs/board.pid"

running_pids() {
  pgrep -f "arb_monitor board --port" 2>/dev/null
}

case "${1:-status}" in
  start)
    port="${2:-8765}"
    stale="$(running_pids)"
    if [[ -n "${stale// /}" ]]; then
      print "already running: ${stale//$'\n'/ }"
      exit 0
    fi
    if [[ ! -x "${python}" ]]; then
      print -u2 "missing venv interpreter: ${python}"
      exit 78
    fi
    mkdir -p "${repo_dir}/data/logs"
    cd "${repo_dir}" || exit 78
    # New session, so the board is not reaped with the shell that started it
    # (same reason hk-monitor-ctl.sh does this).
    nohup "${python}" -c "
import os
os.setsid()
os.execv('${python}', ['${python}', '-m', 'arb_monitor', 'board', '--port', '${port}'])
" >> "${log_file}" 2>&1 &
    sleep 6
    pids="$(running_pids)"
    if [[ -z "${pids}" ]]; then
      print -u2 "failed to start; see ${log_file}"
      exit 1
    fi
    print "${pids}" | head -1 > "${pid_file}"
    print "started: ${pids//$'\n'/ } → http://127.0.0.1:${port}/ (log ${log_file})"
    ;;
  stop)
    pids="$(running_pids)"
    if [[ -z "${pids}" ]]; then
      print "not running"
      exit 0
    fi
    print "${pids}" | xargs kill 2>/dev/null
    sleep 2
    print "stopped: ${pids//$'\n'/ }"
    ;;
  status)
    pids="$(running_pids)"
    if [[ -z "${pids}" ]]; then
      print "board: not running"
      exit 0
    fi
    print "board: running (${pids//$'\n'/ })"
    ;;
  *)
    print -u2 "usage: $0 {start [port]|stop|status}"
    exit 64
    ;;
esac
