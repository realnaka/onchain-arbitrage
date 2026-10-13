#!/bin/zsh
# Start / stop the xStocks auto chain (discovery → filter → execute → cash-back).
#
#   scripts/xstocks-auto-ctl.sh start     # 常驻（只按 policy；live 还需要 start-live）
#   scripts/xstocks-auto-ctl.sh start-live # 常驻 + --confirm-live（真钱）
#   scripts/xstocks-auto-ctl.sh once      # 只跑一轮
#   scripts/xstocks-auto-ctl.sh once-live # 只跑一轮，且显式允许 live
#   scripts/xstocks-auto-ctl.sh stop
#   scripts/xstocks-auto-ctl.sh status
#
# 真钱需要三样同时成立：ops/xstocks_policy.toml 的 mode=live、auto_orders=live、
# live_ack 非空（operator 专属），再加命令行的 --confirm-live。
set -u

script_dir="${0:A:h}"
repo_dir="${script_dir:h}"
python="${repo_dir}/.venv/bin/python"
log_file="${repo_dir}/data/logs/xstocks-auto.log"
pid_file="${repo_dir}/data/logs/xstocks-auto.pid"

running_pids() {
  pgrep -f "scripts/xstocks_auto_live.py" 2>/dev/null
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
  local argv="'${repo_dir}/scripts/xstocks_auto_live.py'"
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
  start)
    do_start
    ;;
  start-live)
    do_start live
    ;;
  once)
    cd "${repo_dir}" || exit 78
    "${python}" scripts/xstocks_auto_live.py --once
    ;;
  once-live)
    cd "${repo_dir}" || exit 78
    "${python}" scripts/xstocks_auto_live.py --once --confirm-live
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
