#!/bin/zsh
# Start / stop the Ethereum xStocks auto chain.
#
#   scripts/xstocks-eth-auto-ctl.sh start       # resident; policy decides off/paper/live
#   scripts/xstocks-eth-auto-ctl.sh start-live  # pass --confirm-live
#   scripts/xstocks-eth-auto-ctl.sh once
#   scripts/xstocks-eth-auto-ctl.sh once-live
#   scripts/xstocks-eth-auto-ctl.sh stop
#   scripts/xstocks-eth-auto-ctl.sh status
#
# Live needs all three: ops/xstocks_eth_policy.toml mode=live,
# auto_orders=live, non-empty live_ack, plus --confirm-live.
set -u

script_dir="${0:A:h}"
repo_dir="${script_dir:h}"
python="${repo_dir}/.venv/bin/python"
log_file="${repo_dir}/data/logs/xstocks-eth-auto.log"
pid_file="${repo_dir}/data/logs/xstocks-eth-auto.pid"

running_pids() {
  pgrep -f "scripts/xstocks_eth_auto_live.py" 2>/dev/null
}

do_start() {
  local live_flag="${1:-}"
  local stale
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
  local argv="'${repo_dir}/scripts/xstocks_eth_auto_live.py'"
  [[ "${live_flag}" == "live" ]] && argv="${argv}, '--confirm-live'"
  nohup "${python}" -c "
import os
os.setsid()
os.execv('${python}', ['${python}', ${argv}])
" >> "${log_file}" 2>&1 &
  sleep 2
  local pids
  pids="$(running_pids)"
  if [[ -z "${pids}" ]]; then
    print -u2 "failed to start; see ${log_file}"
    exit 1
  fi
  print "${pids}" | head -1 > "${pid_file}"
  print "started: ${pids//$'\n'/ } (log ${log_file})"
}

case "${1:-status}" in
  start) do_start ;;
  start-live) do_start live ;;
  once)
    cd "${repo_dir}" || exit 78
    "${python}" scripts/xstocks_eth_auto_live.py --once
    ;;
  once-live)
    cd "${repo_dir}" || exit 78
    "${python}" scripts/xstocks_eth_auto_live.py --once --confirm-live
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
    print -u2 "usage: $0 {start|start-live|once|once-live|stop|status}"
    exit 64
    ;;
esac
