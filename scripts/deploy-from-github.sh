#!/bin/zsh
# Fast-forward the runtime checkout to origin/main and restart what is already running.
#
# Refuses unless data/RUNTIME_ROLE contains "runner". The edit checkout does not
# have that file, so this script cannot deploy ~/Documents/arb.
#
# A dirty tree is left untouched. A cycle lock defers the live restart only.
# This script does not delete a lock, KILL_SWITCH, or AUTO_HALT.
set -u

export PATH="/home/operator/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH}"
export GIT_TERMINAL_PROMPT=0

# GitHub is reached through this Mac's local proxy; launchd does not inherit the
# system proxy setting, and the direct route fails often enough to matter
# ("Error in the HTTP2 framing layer", 2026-10-10 14:37:23, on this job's own
# fetch). Use the proxy when something is listening on it and leave the
# environment alone when it is not, so the same script still works on a laptop.
if [[ -z "${https_proxy:-}${HTTPS_PROXY:-}" && -x /usr/bin/nc ]] \
   && /usr/bin/nc -z -w 1 127.0.0.1 4780 >/dev/null 2>&1; then
  export https_proxy="http://127.0.0.1:4780"
fi

script_dir="${0:A:h}"
repo_dir="${ARB_DEPLOY_ROOT:-${script_dir:h}}"
cd "${repo_dir}" || exit 1

role_file="${repo_dir}/data/RUNTIME_ROLE"
if [[ ! -f "${role_file}" ]] || [[ "$(tr -d '[:space:]' < "${role_file}")" != "runner" ]]; then
  print "deploy: refusing; ${role_file} is not runner"
  exit 0
fi

mkdir -p "${repo_dir}/data"
log="${repo_dir}/data/deploy-from-github.log"
lockdir="${repo_dir}/data/deploy-from-github.lockdir"
note() { print -r -- "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "${log}"; }

if ! mkdir "${lockdir}" 2>/dev/null; then
  note "already running"
  exit 0
fi
trap 'rmdir "${lockdir}" 2>/dev/null || true' EXIT

branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null) || { note "not a git repo"; exit 0; }
if [[ "${branch}" != "main" ]]; then
  note "branch is ${branch}; runtime must stay on main"
  exit 0
fi

if [[ -n "$(git status --porcelain)" ]]; then
  note "working tree dirty; not pulling"
  exit 0
fi

if ! git fetch origin main >> "${log}" 2>&1; then
  note "fetch failed"
  exit 0
fi

local_sha=$(git rev-parse HEAD)
remote_sha=$(git rev-parse --verify --quiet refs/remotes/origin/main) || {
  note "origin/main missing"
  exit 0
}

if [[ "${local_sha}" != "${remote_sha}" ]]; then
  if ! git merge-base --is-ancestor "${local_sha}" "${remote_sha}" 2>/dev/null; then
    note "origin/main is not a fast-forward of HEAD; not pulling"
    exit 0
  fi
  if ! git pull --ff-only origin main >> "${log}" 2>&1; then
    note "pull failed"
    exit 0
  fi
  note "pulled $(git rev-parse --short HEAD)"
fi

head_sha=$(git rev-parse HEAD)
synced_file="${repo_dir}/data/deploy-synced-sha"
synced=""
[[ -f "${synced_file}" ]] && synced="$(tr -d '[:space:]' < "${synced_file}")"
pending_file="${repo_dir}/data/deploy-live-pending"
lock_file="${repo_dir}/data-v2/xstocks_v2_cycle.lock"

# Two different things can be owed, and they are not the same size. A new
# commit means "run everything again"; a pending live restart means "the live
# process still has to change over". Treating the second as the first restarted
# the board, the paper sampler, the watch and the discovery loop every five
# minutes for as long as a cycle lock was held (measured 2026-10-10: the board
# was killed and started again at 14:29 and 14:30 while one MSTRx cycle sat on
# the lock).
code_changed=0
[[ "${head_sha}" != "${synced}" ]] && code_changed=1
live_owed=0
[[ -f "${pending_file}" ]] && live_owed=1
if (( code_changed == 0 && live_owed == 0 )); then
  note "already current ${head_sha:0:12}"
  exit 0
fi

if (( code_changed )); then
  if [[ -n "${ARB_DEPLOY_SKIP_UV:-}" ]]; then
    note "uv skipped"
  elif ! uv sync --frozen --extra live --extra scripts >> "${log}" 2>&1; then
    note "uv sync failed; processes left on the previous code"
    exit 1
  else
    note "uv sync ok"
  fi
fi

typeset -a services
services=()
if [[ -n "${ARB_DEPLOY_SERVICES:-}" ]]; then
  services=("${(@s:,:)ARB_DEPLOY_SERVICES}")
else
  service_running() { pgrep -f "${repo_dir}/scripts/${1}" >/dev/null 2>&1 }
  service_running "xstocks_v2_board.py" && services+=board
  service_running "xstocks_v2_paper.py run" && services+=paper
  service_running "xstocks_v2_live.py run" && services+=live
  service_running "xstocks_v2_watch.py run" && services+=watch
  service_running "xstocks_v2_discovery.py run" && services+=discovery
fi

want_live=0
(( live_owed )) && want_live=1
for name in "${services[@]}"; do
  [[ "${name}" == "live" ]] && want_live=1
done

if (( code_changed == 0 )); then
  # No new code: the only service that may still need a hand is live.
  services=()
fi

restart_one() {
  local name="$1"
  if [[ -n "${ARB_DEPLOY_RESTART_LOG:-}" ]]; then
    print -r -- "${name}" >> "${ARB_DEPLOY_RESTART_LOG}"
    note "restart recorded ${name}"
    return 0
  fi
  local pattern
  local -a cmd
  case "${name}" in
    board) pattern="xstocks_v2_board.py"; cmd=(board) ;;
    paper) pattern="xstocks_v2_paper.py run"; cmd=(paper 180) ;;
    live) pattern="xstocks_v2_live.py run"; cmd=(live 180) ;;
    watch) pattern="xstocks_v2_watch.py run"; cmd=(watch 30) ;;
    discovery) pattern="xstocks_v2_discovery.py run"; cmd=(discovery 900) ;;
    *) note "unknown service ${name}"; return 0 ;;
  esac
  local pids
  pids="$(pgrep -f "${repo_dir}/scripts/${pattern}" 2>/dev/null || true)"
  if [[ -n "${pids// /}" ]]; then
    print "${pids}" | xargs kill 2>/dev/null || true
    sleep 2
  fi
  if [[ ! -x "${repo_dir}/scripts/xstocks-v2-ctl.sh" ]]; then
    note "ctl missing; did not start ${name}"
    return 0
  fi
  if "${repo_dir}/scripts/xstocks-v2-ctl.sh" "${cmd[@]}"; then
    note "restarted ${name}"
  else
    note "restart of ${name} failed"
  fi
}

for name in "${services[@]}"; do
  [[ "${name}" == "live" ]] && continue
  [[ -z "${name}" ]] && continue
  restart_one "${name}"
done

if (( want_live )); then
  if [[ -f "${lock_file}" ]]; then
    print -r -- "1" > "${pending_file}"
    note "cycle lock present; live restart deferred"
  else
    restart_one live
    rm -f "${pending_file}"
    note "live restart done"
  fi
fi

if (( code_changed )); then
  print -r -- "${head_sha}" > "${synced_file}"
  note "synced ${head_sha:0:12}"
fi
