#!/bin/zsh
# Control the read-only xStocks v2 scanner and the stage-1 paper runner.
#
#   scripts/xstocks-v2-ctl.sh once                 # one scan round
#   scripts/xstocks-v2-ctl.sh full                 # one timed full bilateral scan
#   scripts/xstocks-v2-ctl.sh paper-once           # one paper round
#   scripts/xstocks-v2-ctl.sh paper [interval]     # 常驻：扫描 + 复价 + 判定（见下）
#   scripts/xstocks-v2-ctl.sh status
#   scripts/xstocks-v2-ctl.sh health [interval_seconds]
#   scripts/xstocks-v2-ctl.sh board [port]         # 只读看板，默认 8771
#   scripts/xstocks-v2-ctl.sh live [interval]      # 实盘执行器，要求三把钥匙 + 闸门通过
#   scripts/xstocks-v2-ctl.sh watch [interval]     # 报警 → 自动调 Codex 只读自检
#   scripts/xstocks-v2-ctl.sh watch-once [--dry-run]
#   scripts/xstocks-v2-ctl.sh stop
#
# **扫描不是一个独立进程**（2026-10-08 operator）：常驻的 paper runner 发现快照
# 超过 max_age_ms 就自己整轮重扫，`xstocks_v2_scan.json` 一直由它维护。原来的
# `scan` 常驻子命令已删除——它从来没在这个部署里跑过，留着只会让人以为有两个扫描器
# 抢同一把 Jupiter key。要一次性扫描用 `once` / `full`。
#
# Neither resident process can broadcast. phase=scan keeps every row
# non-executable, and the paper runner hard-codes broadcast=false.
set -u

script_dir="${0:A:h}"
repo_dir="${script_dir:h}"
python="${repo_dir}/.venv/bin/python"
v2_data="${repo_dir}/data-v2"
scan_pid="${v2_data}/xstocks-v2-scan.pid"
paper_pid="${v2_data}/xstocks-v2-paper.pid"

# Every v2 process that quotes Jupiter shares ONE request budget (the free key
# is 1 request/s). Without this file each process paces itself and the two of
# them together ask for ~1.8 rps, so Jupiter answers 429 to whoever is second
# (measured 2026-10-08). The file holds a single number: the earliest time the
# next request may go out.
export JUPITER_RATE_FILE="${v2_data}/xstocks_v2_jupiter_rate.json"

scan_pids() { pgrep -f "scripts/xstocks_v2_scan.py run" 2>/dev/null }
paper_pids() { pgrep -f "scripts/xstocks_v2_paper.py run" 2>/dev/null }
board_pids() { pgrep -f "scripts/xstocks_v2_board.py" 2>/dev/null }
live_pids() { pgrep -f "scripts/xstocks_v2_live.py run" 2>/dev/null }
watch_pids() { pgrep -f "scripts/xstocks_v2_watch.py run" 2>/dev/null }
discovery_pids() { pgrep -f "scripts/xstocks_v2_discovery.py run" 2>/dev/null }

# Lesson 6.4: v1's logs only rotated when the supervisor restarted, so a
# long-running process wrote one ever-growing file. Rotate on start instead.
rotate_log() {
  local log_file="$1" keep=3 max_bytes=20000000
  [[ -f "${log_file}" ]] || return 0
  local size
  size=$(stat -f%z "${log_file}" 2>/dev/null || echo 0)
  (( size < max_bytes )) && return 0
  local index
  for index in $(seq $((keep - 1)) -1 1); do
    [[ -f "${log_file}.${index}" ]] && mv "${log_file}.${index}" "${log_file}.$((index + 1))"
  done
  mv "${log_file}" "${log_file}.1"
}

start_detached() {
  local log_file="$1"; shift
  mkdir -p "${v2_data}"
  rotate_log "${log_file}"
  cd "${repo_dir}" || exit 78
  nohup "${python}" -c "
import os
os.setsid()
os.execv('${python}', ['${python}', $*])
" >> "${log_file}" 2>&1 &
  sleep 2
}

case "${1:-status}" in
  once)
    cd "${repo_dir}" || exit 78
    exec env PYTHONPATH=src "${python}" scripts/xstocks_v2_scan.py once
    ;;
  full)
    cd "${repo_dir}" || exit 78
    exec env PYTHONPATH=src "${python}" scripts/xstocks_v2_scan.py once --full-bilateral
    ;;
  paper-once)
    cd "${repo_dir}" || exit 78
    exec env PYTHONPATH=src "${python}" scripts/xstocks_v2_paper.py once
    ;;
  paper)
    interval="${2:-180}"
    pids="$(paper_pids)"
    if [[ -n "${pids// /}" ]]; then print "already running: ${pids//$'\n'/ }"; exit 0; fi
    start_detached "${v2_data}/xstocks-v2-paper.log" \
      "'${repo_dir}/scripts/xstocks_v2_paper.py', 'run', '--interval', '${interval}'"
    pids="$(paper_pids)"
    [[ -z "${pids// /}" ]] && { print -u2 "failed to start; see ${v2_data}/xstocks-v2-paper.log"; exit 1 }
    print "${pids}" | head -1 > "${paper_pid}"
    print "paper started: ${pids//$'\n'/ } → ${v2_data}/xstocks_v2_paper.json"
    ;;
  stop)
    for pattern in "scripts/xstocks_v2_scan.py run" "scripts/xstocks_v2_paper.py run" \
                   "scripts/xstocks_v2_board.py" "scripts/xstocks_v2_live.py run" \
                   "scripts/xstocks_v2_watch.py run" "scripts/xstocks_v2_discovery.py run"; do
      pids="$(pgrep -f "${pattern}" 2>/dev/null)"
      if [[ -n "${pids// /}" ]]; then
        print "${pids}" | xargs kill 2>/dev/null
        print "stopped ${pattern}: ${pids//$'\n'/ }"
      else
        print "not running: ${pattern}"
      fi
    done
    ;;
  board)
    port="${2:-8771}"
    pids="$(board_pids)"
    if [[ -n "${pids// /}" ]]; then print "already running: ${pids//$'\n'/ }"; exit 0; fi
    start_detached "${v2_data}/xstocks-v2-board.log" \
      "'${repo_dir}/scripts/xstocks_v2_board.py', '--port', '${port}'"
    pids="$(board_pids)"
    [[ -z "${pids// /}" ]] && { print -u2 "failed to start; see ${v2_data}/xstocks-v2-board.log"; exit 1 }
    print "${pids}" | head -1 > "${v2_data}/xstocks-v2-board.pid"
    print "board started: ${pids//$'\n'/ } → http://127.0.0.1:${port}/"
    ;;
  live)
    interval="${2:-180}"
    pids="$(live_pids)"
    if [[ -n "${pids// /}" ]]; then print "already running: ${pids//$'\n'/ }"; exit 0; fi
    cd "${repo_dir}" || exit 78
    # Refuse to start a money-moving process whose own gate is closed.
    state="$(PYTHONPATH=src "${python}" scripts/xstocks_v2_live.py status)"
    if [[ "${state}" != *'"allowed": true'* ]]; then
      print -u2 "实盘闸门未通过，拒绝启动：${state}"
      exit 78
    fi
    start_detached "${v2_data}/xstocks-v2-live.log" \
      "'${repo_dir}/scripts/xstocks_v2_live.py', 'run', '--interval', '${interval}', '--confirm-live'"
    pids="$(live_pids)"
    [[ -z "${pids// /}" ]] && { print -u2 "failed to start; see ${v2_data}/xstocks-v2-live.log"; exit 1 }
    print "${pids}" | head -1 > "${v2_data}/xstocks-v2-live.pid"
    print "live started: ${pids//$'\n'/ } → ${v2_data}/xstocks_v2_live.json"
    ;;
  watch)
    # The alert watcher: reads the on-disk state and sends Codex after a
    # problem. It never writes a switch and never starts a runner, so it is
    # safe to leave resident; it starts no money-moving process of its own.
    interval="${2:-30}"
    pids="$(watch_pids)"
    if [[ -n "${pids// /}" ]]; then print "already running: ${pids//$'\n'/ }"; exit 0; fi
    start_detached "${v2_data}/xstocks-v2-watch.log" \
      "'${repo_dir}/scripts/xstocks_v2_watch.py', 'run', '--interval', '${interval}'"
    pids="$(watch_pids)"
    [[ -z "${pids// /}" ]] && { print -u2 "failed to start; see ${v2_data}/xstocks-v2-watch.log"; exit 1 }
    print "watch started: ${pids//$'\n'/ } → ${v2_data}/investigations/alert_state.json"
    ;;
  watch-once)
    cd "${repo_dir}" || exit 78
    exec env PYTHONPATH=src "${python}" scripts/xstocks_v2_watch.py once "${@:2}"
    ;;
  discovery)
    # New-listing watch. Cheap by design: two HTTP calls per round, and it only
    # widens the universe when something actually appeared (catalog refresh, or
    # one private Kraken validate per new pair). The ~1400-call chain census is
    # reserved for the nightly full sync it runs itself at --nightly.
    interval="${2:-900}"
    pids="$(discovery_pids)"
    if [[ -n "${pids// /}" ]]; then print "already running: ${pids//$'\n'/ }"; exit 0; fi
    start_detached "${v2_data}/xstocks-v2-discovery.log" \
      "'${repo_dir}/scripts/xstocks_v2_discovery.py', 'run', '--interval', '${interval}'"
    pids="$(discovery_pids)"
    [[ -z "${pids// /}" ]] && { print -u2 "failed to start; see ${v2_data}/xstocks-v2-discovery.log"; exit 1 }
    print "discovery started: ${pids//$'\n'/ } → ${v2_data}/xstocks_v2_discovery.json"
    ;;
  discovery-once)
    cd "${repo_dir}" || exit 78
    exec env PYTHONPATH=src "${python}" scripts/xstocks_v2_discovery.py check --ingest "${@:2}"
    ;;
  status)
    cd "${repo_dir}" || exit 78
    for label in paper board live watch discovery; do
      case "${label}" in
        paper) pids="$(paper_pids)" ;;
        board) pids="$(board_pids)" ;;
        live) pids="$(live_pids)" ;;
        watch) pids="$(watch_pids)" ;;
        *) pids="$(discovery_pids)" ;;
      esac
      if [[ -z "${pids// /}" ]]; then print "${label}: not running"; else print "${label}: running (${pids//$'\n'/ })"; fi
    done
    # The paper runner owns the scan (see the header): report its snapshot age.
    PYTHONPATH=src "${python}" scripts/xstocks_v2_paper.py status
    PYTHONPATH=src "${python}" scripts/xstocks_v2_live.py status
    ;;
  health)
    # Lesson 6.3: a silent runner and a quiet market look identical. Compare
    # the heartbeat against wall time instead of trusting the process list.
    interval="${2:-180}"
    cd "${repo_dir}" || exit 78
    PYTHONPATH=src "${python}" - "${interval}" <<'PY'
import json, sys, time
from pathlib import Path
sys.path.insert(0, "src")
from arb_monitor.xstocks_v2 import health
interval = float(sys.argv[1])
report = health.check(
    paths={
        "scan": Path("data-v2/xstocks_v2_scan.json"),
        "paper": Path("data-v2/xstocks_v2_paper.json"),
    },
    now_ms=int(time.time() * 1000),
    interval_s=interval,
)
reason = health.alert_text(report)
print(json.dumps({**report, "alert": reason}, ensure_ascii=False))
raise SystemExit(0 if report["status"] == health.OK else 1)
PY
    ;;
  *)
    print -u2 "usage: $0 {once|full|paper-once|scan [interval]|paper [interval]|board [port]|live [interval]|watch [interval]|watch-once [--dry-run]|discovery [interval]|discovery-once|stop|status|health [interval]}"
    exit 64
    ;;
esac
