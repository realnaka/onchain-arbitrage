#!/bin/zsh
# One-shot: walk family 5 and then family 6 once each, real money.
#
# operator 2026-10-07: 「family 5 和 family 6 亏钱也要走通一次」。
#
# Both families run through the normal executor, so every gate still applies
# (kill / halt / v1 busy / one cycle in flight). This script only waits for the
# shared cycle lock and keeps the two runs strictly sequential -- another
# session in this repo is moving money too, and the lock is the only thing
# stopping two cycles from overlapping.
#
#   scripts/xstocks_v2_family56_run.sh >> data-v2/xstocks_v2_family56_run.log 2>&1
set -u

repo="${0:A:h:h}"
cd "${repo}" || exit 78
python="${repo}/.venv/bin/python"
log() { print -r -- "$(date '+%H:%M:%S') $*"; }

# Wait for the cycle lock. A lock whose owner is gone is NOT deleted here: an
# unknown state is a human decision, not a shell one.
wait_lock() {
  local limit="${1:-40}" holder
  for _ in $(seq 1 "${limit}"); do
    holder="$(cat data-v2/xstocks_v2_cycle.lock 2>/dev/null)"
    if [[ -z "${holder}" ]]; then return 0; fi
    if ! ps -p "${holder}" >/dev/null 2>&1; then
      log "STALE lock from dead pid ${holder}: stop, reconcile by hand"
      return 1
    fi
    sleep 30
  done
  log "timed out waiting for the cycle lock (holder ${holder})"
  return 1
}

already_done() {   # family
  "${python}" - "$1" <<'PY'
import json, sys
from pathlib import Path
family = sys.argv[1]
path = Path("data-v2/xstocks_v2_cycles.jsonl")
if not path.exists():
    raise SystemExit(1)
for line in path.read_text().splitlines():
    try:
        row = json.loads(line)
    except ValueError:
        continue
    if (row.get("family") == family and row.get("mode") == "live"
            and row.get("state") in ("sold", "probed")):
        raise SystemExit(0)
raise SystemExit(1)
PY
}

log "=== family 5: x-layer -> solana -> kraken ==="
if already_done chain_via_bridge_to_kraken; then
  log "family 5 已经有一次 sold/probed，跳过"
elif wait_lock 60; then
  env PYTHONPATH=src HTTPS_PROXY=http://127.0.0.1:4780 HTTP_PROXY=http://127.0.0.1:4780 \
    "${python}" scripts/xstocks_v2_solana_roundtrip.py once \
    --symbol SPYx --exit kraken --tokens 0.011 --price 780.4 \
    --ref-price 775.99 --assume-bridge-min 0.0095 \
    --fee-wei 791242725185203 --confirm-live
  log "family 5 exit=$?"
else
  log "family 5 没等到锁，未执行"
fi

log "=== family 6: kraken -> solana -> x-layer ==="
if already_done kraken_via_bridge_to_chain; then
  log "family 6 已经有一次 sold，跳过"
elif wait_lock 60; then
  env PYTHONPATH=src HTTPS_PROXY=http://127.0.0.1:4780 HTTP_PROXY=http://127.0.0.1:4780 \
    "${python}" scripts/xstocks_v2_xlayer_roundtrip.py once \
    --symbol SPYx --notional 39 --price 780 --assume-bridge-min 0.0095 \
    --confirm-live
  log "family 6 exit=$?"
else
  log "family 6 没等到锁，未执行"
fi

log "=== done; 余额快照与账本见 data-v2 ==="
env PYTHONPATH=src HTTPS_PROXY=http://127.0.0.1:4780 HTTP_PROXY=http://127.0.0.1:4780 \
  "${python}" scripts/xstocks_v2_balances.py show
