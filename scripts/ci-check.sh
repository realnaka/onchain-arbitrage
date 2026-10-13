#!/bin/zsh
# The same two checks .github/workflows/ci.yml runs, on this machine.
#
#   scripts/ci-check.sh
#
#   1. `uv lock --check` — pyproject and uv.lock still agree. A stale lock is a
#      real defect: until 2026-10-09 uv.lock had no pytoniq in it at all, so CI
#      would have failed on install while every local run passed.
#   2. the full suite — `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`.
#
# This uses the *local* .venv, so green here does not prove a clean machine can
# install the project. `uv sync --frozen` in CI is what proves that. This is the
# fast "did I break something" gate before pushing.
set -u
script_dir="${0:A:h}"
repo_dir="${script_dir:h}"
cd "${repo_dir}" || exit 1

fail=0

echo "== uv lock --check =="
if uv lock --check; then
  echo "   lock is up to date"
else
  echo "   STALE: run 'uv lock' and commit the result"
  fail=1
fi

echo "== unittest discover -s tests =="
if PYTHONPATH=src .venv/bin/python -m unittest discover -s tests; then
  echo "   tests OK"
else
  echo "   TESTS FAILED"
  fail=1
fi

if [ "${fail}" -eq 0 ]; then
  echo "== ci-check: OK =="
else
  echo "== ci-check: FAILED =="
fi
exit "${fail}"
