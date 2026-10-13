#!/bin/zsh
# Control the read-only multi-chain Uniswap v4 xStocks watch.
#
#   scripts/xstocks-v4-ctl.sh start [interval_seconds]
#   scripts/xstocks-v4-ctl.sh once
#   scripts/xstocks-v4-ctl.sh routes
#   scripts/xstocks-v4-ctl.sh units
#   scripts/xstocks-v4-ctl.sh stop
#   scripts/xstocks-v4-ctl.sh status
#
# It writes data/xstocks_v4_watch.json. It does not sign or submit orders.
set -u

script_dir="${0:A:h}"
repo_dir="${script_dir:h}"
python="${repo_dir}/.venv/bin/python"
log_file="${repo_dir}/data/logs/xstocks-v4.log"
pid_file="${repo_dir}/data/logs/xstocks-v4.pid"

running_pids() {
  pgrep -f "scripts/xstocks_v4_watch.py" 2>/dev/null
}

case "${1:-status}" in
  start)
    interval="${2:-300}"
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
os.execv('${python}', ['${python}', '${repo_dir}/scripts/xstocks_v4_watch.py',
                       '--interval', '${interval}'])
" >> "${log_file}" 2>&1 &
    sleep 2
    pids="$(running_pids)"
    if [[ -z "${pids}" ]]; then
      print -u2 "failed to start; see ${log_file}"
      exit 1
    fi
    print "${pids}" | head -1 > "${pid_file}"
    print "started: ${pids//$'\n'/ } → data/xstocks_v4_watch.json (log ${log_file})"
    ;;
  once)
    cd "${repo_dir}" || exit 78
    exec "${python}" scripts/xstocks_v4_watch.py --once
    ;;
  routes)
    cd "${repo_dir}" || exit 78
    exec "${python}" scripts/xstocks_route_matrix.py
    ;;
  units)
    cd "${repo_dir}" || exit 78
    exec "${python}" scripts/kraken_xstocks_units.py
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
    print -u2 "usage: $0 {start [interval_seconds]|once|routes|units|stop|status}"
    exit 64
    ;;
esac
