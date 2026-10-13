#!/bin/zsh
# Open or update a pull request from the current branch into main.
#
# Called from scripts/auto-backup.sh after a commit. It never pushes onto main:
# the refspec is the current branch name, and main / wip-snapshot are skipped.
# A failure here must not fail the commit or the snapshot, so every error exits 0.
set -u

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH}"
export GIT_TERMINAL_PROMPT=0

repo=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
cd "${repo}" || exit 0
branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null) || exit 0

case "${branch}" in
  main|wip-snapshot|HEAD)
    print "open-pr: skip ${branch}"
    exit 0
    ;;
esac

# Same-name refspec on purpose. `git push HEAD` with an upstream of main would
# be ambiguous; this line can only update refs/heads/<current branch>.
if ! git push -u origin "HEAD:refs/heads/${branch}"; then
  print "open-pr: push failed"
  exit 0
fi

if gh pr view --json number >/dev/null 2>&1; then
  print "open-pr: already open"
  exit 0
fi

if ! gh pr create --base main --fill --head "${branch}"; then
  print "open-pr: create failed"
  exit 0
fi
print "open-pr: created"
