#!/bin/zsh
# Start / stop / inspect the 24/7 read-only monitor.
#
#   scripts/hk-monitor-ctl.sh start [interval_seconds]
#   scripts/hk-monitor-ctl.sh stop
#   scripts/hk-monitor-ctl.sh status
#
# Why not a LaunchAgent: macOS blocks launchd-spawned processes from reading
# ~/Documents ("Operation not permitted" even as the same user). Either grant
# /bin/zsh Full Disk Access in System Settings > Privacy & Security, or keep
# the repository outside ~/Documents, and the LaunchAgent in scripts/ can be
# used instead. Until then this detached-session runner is what survives.
set -u

script_dir="${0:A:h}"
repo_dir="${script_dir:h}"
runner="${script_dir}/run_hk_monitor.sh"
db="${repo_dir}/data/hk_monitor.db"
pid_file="${repo_dir}/data/logs/hk-monitor.pid"
log_file="${repo_dir}/data/logs/hk-monitor.log"

running_pids() {
  pgrep -f "arb_monitor hk-monitor --cycles 0" 2>/dev/null
}

wrapper_pids() {
  pgrep -f "run_hk_monitor\.sh" 2>/dev/null
}

case "${1:-status}" in
  start)
    interval="${2:-60}"
    # Guard against orphans from an earlier wrapper: two samplers would double
    # every event and make the recorded cadence a lie.
    stale="$(running_pids) $(wrapper_pids)"
    if [[ -n "${stale// /}" ]]; then
      print "already running: ${stale//$'\n'/ }"
      exit 0
    fi
    mkdir -p "${repo_dir}/data/logs"
    cd "${repo_dir}" || exit 78
    # New session (setsid) so the monitor is not reaped with this shell.
    nohup .venv/bin/python -c "
import os
os.setsid()
os.execv('/bin/zsh', ['/bin/zsh', '${runner}', '${interval}'])
" >> "${log_file}" 2>&1 &
    sleep 6
    pids="$(running_pids)"
    if [[ -z "${pids}" ]]; then
      print -u2 "failed to start; see ${log_file}"
      exit 1
    fi
    print "${pids}" | head -1 > "${pid_file}"
    print "started: ${pids//$'\n'/ } (log ${log_file})"
    ;;
  stop)
    pids="$(running_pids) $(wrapper_pids)"
    pids="$(print -r -- "${pids}" | tr ' ' '\n' | sort -u | tr '\n' ' ')"
    if [[ -z "${pids}" ]]; then
      print "not running"
      exit 0
    fi
    print -r -- "${pids}" | xargs kill 2>/dev/null
    # Wait out the monitor's own shutdown grace before forcing. On stop it waits
    # for an in-flight execution task to finish and writes the task down if it
    # cannot; it never cancels an order, so a longer wait cannot leave anything
    # half-done that a shorter one would have fixed. SIGKILLing early would undo
    # exactly that bookkeeping. `shutdown_grace_seconds` lives in
    # config/automation_policy.toml ([execution]) and defaults to 20 s.
    grace="$(sed -n 's/^[[:space:]]*shutdown_grace_seconds[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*/\1/p' \
      "${repo_dir}/config/automation_policy.toml" 2>/dev/null | head -1)"
    [[ -z "${grace}" ]] && grace=20
    wait_for=$((grace + 10))
    for _ in $(seq 1 "${wait_for}"); do
      leftover="$(running_pids) $(wrapper_pids)"
      [[ -z "${leftover// /}" ]] && break
      sleep 1
    done
    leftover="$(running_pids) $(wrapper_pids)"
    if [[ -n "${leftover// /}" ]]; then
      print -r -- "${leftover}" | xargs kill -9 2>/dev/null
      sleep 1
    fi
    print "stopped: ${pids}"
    rm -f "${pid_file}"
    ;;
  status)
    pids="$(running_pids)"
    if [[ -z "${pids}" ]]; then
      # pgrep can be blocked in restricted shells; fall back to how fresh the
      # last recorded event is instead of claiming a false "not running".
      if [[ -f "${db}" ]]; then
        fresh="$(sqlite3 "${db}" "select case when max(recv_ts_ms) > (strftime('%s','now')-300)*1000 then 'fresh' else 'stale' end from monitor_events;")"
      else
        fresh="missing"
      fi
      if [[ "${fresh}" == "fresh" ]]; then
        print "monitor: process not visible here, but events are fresh (<=300s) — likely running"
      else
        print "monitor: not running (last event: ${fresh})"
      fi
    else
      print "monitor: running (${pids//$'\n'/ }) wrappers=$(wrapper_pids | tr '\n' ' ')"
    fi
    if [[ -f "${db}" ]]; then
      sqlite3 "${db}" "select 'last event: ' || datetime(max(recv_ts_ms)/1000,'unixepoch','+8 hours') from monitor_events;"
      sqlite3 "${db}" "select 'onchain: ' || coalesce(json_extract(payload_json,'\$.onchain_status'),'n/a') || ' pools=' || coalesce(json_extract(payload_json,'\$.onchain_pools_read'),0) from monitor_events where kind='run_summary' order by recv_ts_ms desc limit 1;"
    fi
    ;;
  *)
    print -u2 "usage: $0 {start [interval]|stop|status}"
    exit 64
    ;;
esac
