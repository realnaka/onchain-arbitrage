#!/bin/zsh
# Start / stop / inspect the read-only account + risk board.
#
#   scripts/account-board-ctl.sh start [port]
#   scripts/account-board-ctl.sh stop
#   scripts/account-board-ctl.sh status
#
# The board only reads: OKX account endpoints (signed GET only), the X Layer
# wallet balance via the OnchainOS CLI, and the local execution ledger. It never
# places, cancels, signs or transfers anything.
set -u

script_dir="${0:A:h}"
repo_dir="${script_dir:h}"
python="${repo_dir}/.venv/bin/python"
log_file="${repo_dir}/data/logs/account-board.log"
pid_file="${repo_dir}/data/logs/account-board.pid"
okx_env="${repo_dir}/config/okx-demo.env"

running_pids() {
  pgrep -f "arb_monitor account-dashboard" 2>/dev/null
}

case "${1:-status}" in
  start)
    port="${2:-8766}"
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
    export PYTHONPATH="${repo_dir}/src"
    export PYTHONUNBUFFERED=1
    # Reviewed proxy route. Reads fail closed to UNKNOWN if it is down; there is
    # no silent direct fallback for account data.
    export ARB_HTTP_PROXY="${ARB_HTTP_PROXY:-http://127.0.0.1:4780}"
    nohup "${python}" -c "
import os
os.setsid()
os.execv('${python}', ['${python}', '-m', 'arb_monitor', 'account-dashboard',
                       '--port', '${port}', '--okx-env', '${okx_env}'])
" >> "${log_file}" 2>&1 &
    sleep 5
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
    print -r -- "${pids}" | xargs kill 2>/dev/null
    sleep 2
    leftover="$(running_pids)"
    if [[ -n "${leftover// /}" ]]; then
      print -r -- "${leftover}" | xargs kill -9 2>/dev/null
    fi
    rm -f "${pid_file}"
    print "stopped: ${pids//$'\n'/ }"
    ;;
  status)
    pids="$(running_pids)"
    if [[ -z "${pids}" ]]; then
      print "account board: not running (log ${log_file})"
    else
      print "account board: running (${pids//$'\n'/ })"
    fi
    ;;
  *)
    print -u2 "usage: $0 {start [port]|stop|status}"
    exit 64
    ;;
esac
