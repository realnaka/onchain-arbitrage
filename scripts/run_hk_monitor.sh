#!/bin/zsh
# 24/7 read-only xStocks basis monitor.
#
# The on-chain leg trades around the clock, so this loop does not stop for the
# HK or US cash sessions. It only reads public data: CEX public order books via
# CCXT, the X Layer JSON-RPC endpoint, and public reference HTTP. It never
# places an order, signs a transaction or moves funds.
#
# Usage: scripts/run_hk_monitor.sh [interval_seconds]
# Env:   ARB_HTTP_PROXY (default http://127.0.0.1:4780, the measured local proxy)
set -u

script_dir="${0:A:h}"
repo_dir="${script_dir:h}"
interval="${1:-60}"
proxy="${ARB_HTTP_PROXY:-http://127.0.0.1:4780}"
log_dir="${repo_dir}/data/logs"
log_file="${log_dir}/hk-monitor.log"
python="${repo_dir}/.venv/bin/python"

mkdir -p "${log_dir}"
# Keep the same inode when rotating: the supervisor opens the redirect before
# this script runs, so a `mv` would leave the live process writing into the
# archive file (observed 2026-09-15: hk-monitor.log.1 kept growing).
if [[ -f "${log_file}" && $(stat -f%z "${log_file}") -gt 20000000 ]]; then
  cp "${log_file}" "${log_file}.1"
  : > "${log_file}"
fi

if [[ ! -x "${python}" ]]; then
  print -u2 "missing venv interpreter: ${python}"
  exit 78
fi

cd "${repo_dir}" || exit 78
export PYTHONPATH="${repo_dir}/src"
export PYTHONUNBUFFERED=1

# Secrets (Feishu webhook) live outside the committed config.
if [[ -f "${repo_dir}/config/notify.env" ]]; then
  set -a
  source "${repo_dir}/config/notify.env"
  set +a
fi

# Restart on crash instead of leaving a silently dead monitor. A non-zero exit
# from one cycle is not fatal: the next cycle re-reads every source.
while true; do
  print "[$(date '+%Y-%m-%d %H:%M:%S%z')] starting monitor interval=${interval}s proxy=${proxy}"
  "${python}" -m arb_monitor hk-monitor --cycles 0 --interval "${interval}" \
    --proxy "${proxy}" 2>&1
  code=$?
  print "[$(date '+%Y-%m-%d %H:%M:%S%z')] monitor exited code=${code}; restarting in 10s"
  sleep 10
done
