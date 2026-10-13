#!/bin/zsh
# Start / stop the resident xStocks discount page.
#
#   scripts/xstocks-gap-ctl.sh start [port]
#   scripts/xstocks-gap-ctl.sh stop
#   scripts/xstocks-gap-ctl.sh status
#
# Read-only. It quotes public prices and does not place orders.
set -u

script_dir="${0:A:h}"
repo_dir="${script_dir:h}"
python="${repo_dir}/.venv/bin/python"
log_file="${repo_dir}/data/logs/xstocks-gap.log"
pid_file="${repo_dir}/data/logs/xstocks-gap.pid"

running_pids() {
  pgrep -f "scripts/xstocks_gap_live.py" 2>/dev/null
}

case "${1:-status}" in
  start)
    port="${2:-8770}"
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
    nohup "${python}" -c "
import os
os.setsid()
os.execv('${python}', ['${python}', '${repo_dir}/scripts/xstocks_gap_live.py', '--port', '${port}'])
" >> "${log_file}" 2>&1 &
    sleep 2
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
    sleep 1
    print "stopped: ${pids//$'\n'/ }"
    ;;
  status)
    pids="$(running_pids)"
    if [[ -z "${pids}" ]]; then
      print "not running"
      exit 1
    fi
    print "running (${pids//$'\n'/ })"
    ;;
  *)
    print -u2 "usage: $0 {start [port]|stop|status}"
    exit 64
    ;;
esac
