#!/bin/zsh
# Start / stop the xStocks book sampler. It is not the basis monitor.
#
#   scripts/xstocks-book-ctl.sh start
#   scripts/xstocks-book-ctl.sh stop
#   scripts/xstocks-book-ctl.sh status
#
# The daily catalog is a separate command and is not started here:
#   PYTHONPATH=src .venv/bin/python scripts/xstocks_book.py refresh
set -u

script_dir="${0:A:h}"
repo_dir="${script_dir:h}"
pid_file="${repo_dir}/data/logs/xstocks-book.pid"
log_file="${repo_dir}/data/logs/xstocks-book.log"

running_pids() {
  pgrep -f "scripts/xstocks_book.py sample" 2>/dev/null
}

case "${1:-status}" in
  start)
    stale="$(running_pids)"
    if [[ -n "${stale}" ]]; then
      print "already running: ${stale//$'\n'/ }"
      exit 0
    fi
    mkdir -p "${repo_dir}/data/logs"
    cd "${repo_dir}" || exit 78
    nohup "${repo_dir}/.venv/bin/python" "${repo_dir}/scripts/xstocks_book.py" sample \
      >> "${log_file}" 2>&1 &
    echo $! > "${pid_file}"
    sleep 1
    pids="$(running_pids)"
    if [[ -z "${pids}" ]]; then
      print -u2 "failed to start; see ${log_file}"
      exit 1
    fi
    print "started: ${pids//$'\n'/ } (log ${log_file})"
    ;;
  stop)
    pids="$(running_pids)"
    if [[ -z "${pids}" ]]; then
      print "not running"
      exit 0
    fi
    print -r -- "${pids}" | xargs kill 2>/dev/null
    for _ in $(seq 1 10); do
      [[ -z "$(running_pids)" ]] && break
      sleep 1
    done
    if [[ -n "$(running_pids)" ]]; then
      print -r -- "$(running_pids)" | xargs kill -9 2>/dev/null
    fi
    print "stopped"
    ;;
  status)
    pids="$(running_pids)"
    if [[ -z "${pids}" ]]; then
      print "not running"
      exit 1
    fi
    print "running: ${pids//$'\n'/ }"
    ;;
  *)
    print -u2 "usage: $0 {start|stop|status}"
    exit 64
    ;;
esac
