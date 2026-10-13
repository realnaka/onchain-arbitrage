#!/bin/zsh
# Keep GitHub current without anyone having to remember.
#
# Three jobs, all safe to run while agents are editing:
#
#   1. push commits that already exist on this branch (fast-forward only);
#   2. open a pull request for that branch into main (never push the branch onto main);
#   3. snapshot the working tree — including files nobody has committed yet —
#      to the `wip-snapshot` branch.
#
# main moves only when the pull request is merged. A straight line of commits
# on a feature branch is not a reason to publish that branch as main.
#
# Job 2 is what covers the failure that actually happened on 2026-10-09:
# `capability.py` and `src/arb_monitor/data/` were untracked, so no amount of
# pushing committed work would have saved them.
#
# It never touches HEAD, the real index, or the working tree: the snapshot is
# built with a throwaway index (GIT_INDEX_FILE), so another agent's half-staged
# edit is left exactly as it was. Ignored paths stay out — `.gitignore` decides
# what "the work" is. Nothing here can delete anything.
#
# Install / disable: see docs/ci-2026-10-09.md.
set -u

script_dir="${0:A:h}"
repo_dir="${script_dir:h}"
cd "${repo_dir}" || exit 1

mkdir -p "${repo_dir}/data"
log="${repo_dir}/data/auto-backup.log"
note() { print -r -- "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "${log}"; }

branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null) || { note "not a git repo"; exit 0; }
[ "${branch}" = "HEAD" ] && { note "detached HEAD, skipped"; exit 0; }

# --- 1. existing commits ------------------------------------------------
upstream=$(git rev-parse --verify --quiet "refs/remotes/origin/${branch}") || upstream=""
if [ -n "${upstream}" ]; then
  if ! git merge-base --is-ancestor "${upstream}" HEAD 2>/dev/null; then
    # Someone rewrote history on purpose. Do not guess; the next human decides.
    note "branch ${branch} diverged from origin/${branch}; not pushing"
  elif [ "$(git rev-parse HEAD)" != "${upstream}" ]; then
    if git push origin "${branch}:${branch}" >> "${log}" 2>&1; then
      note "pushed ${branch} -> origin"
    else
      note "push of ${branch} failed"
    fi
  fi
fi

# A feature branch is published as itself. main is updated by merging the PR.
if [ "${branch}" != "wip-snapshot" ]; then
  /bin/zsh "${repo_dir}/scripts/open-pr.sh" >> "${log}" 2>&1 || note "open-pr failed"
fi

# --- 2. working-tree snapshot -------------------------------------------
tmp_index=$(mktemp "${TMPDIR:-/tmp}/arb-snapshot-index.XXXXXX")
rm -f "${tmp_index}"
export GIT_INDEX_FILE="${tmp_index}"
git read-tree HEAD 2>/dev/null
git add -A . >> "${log}" 2>&1
tree=$(git write-tree 2>> "${log}")
unset GIT_INDEX_FILE
rm -f "${tmp_index}"
[ -n "${tree}" ] || { note "write-tree failed"; exit 0; }

git fetch -q origin wip-snapshot >> "${log}" 2>&1
remote_tree=$(git rev-parse --verify --quiet "refs/remotes/origin/wip-snapshot^{tree}")
if [ -n "${remote_tree}" ] && [ "${remote_tree}" = "${tree}" ]; then
  exit 0   # nothing changed since the last snapshot; do not grow the chain
fi

# Parent on the previous snapshot so every push stays a fast-forward; the first
# one hangs off HEAD.
parent=$(git rev-parse --verify --quiet "refs/remotes/origin/wip-snapshot") || parent="HEAD"
message="wip snapshot $(date '+%Y-%m-%d %H:%M %Z')"
if ! commit=$(git commit-tree "${tree}" -p "${parent}" -m "${message}" 2>> "${log}"); then
  note "commit-tree failed"
  exit 0
fi
if git push -q origin "${commit}:refs/heads/wip-snapshot" >> "${log}" 2>&1; then
  note "snapshot pushed ${commit:0:12} (tree ${tree:0:12})"
else
  note "snapshot push failed"
fi
