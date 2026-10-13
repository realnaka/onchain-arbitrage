#!/bin/zsh
# Post-reboot restore for the processes that macOS does NOT bring back.
#
#   scripts/post-reboot-restore.sh [--with-hk-board]
#
# Why this exists (recorded 2026-10-06, operator rebooted the Mac):
# launchd restarts only what is registered as a service. The project's
# own control scripts start their processes with nohup+setsid (`*-ctl.sh`
# explains why a LaunchAgent was rejected for the sampler), so a reboot kills
# them and *nothing* starts them again. This script is the one command that
# re-creates the pre-reboot state.
#
# Comes back by itself after a reboot, no action needed here:
#   com.arcusfarm.mainnet, com.arcusfarm.watchdog, ai.openclaw.node,
#   ai.hermes.gateway, com.openclaw.listing-monitor, com.okx.a2a,
#   com.operator.codex-model-router, com.operator.codex-proxy-bypass.
# NOT loaded, so it does not come back either: ai.openclaw.gateway.
#
# The hk basis sampler is deliberately NOT started here: the operator said on
# 2026-10-06 that it does not need to run. The market board (8765) reads the
# sampler's database, so without it the page shows stale rows — it is therefore
# only started with --with-hk-board.
set -u

script_dir="${0:A:h}"
repo_dir="${script_dir:h}"
proxy_url="http://127.0.0.1:4780"
with_hk_board=0
[[ "${1:-}" == "--with-hk-board" ]] && with_hk_board=1

# 1. Everything network-facing fails closed without the shared local proxy.
#    Check the listener first (cheap, no traffic); fall back to one real
#    request through it, because a listening port is not proof the tunnel works.
if lsof -nP -iTCP:4780 -sTCP:LISTEN >/dev/null 2>&1 \
   && curl -sS --max-time 8 -x "${proxy_url}" -o /dev/null \
        "https://api.kraken.com/0/public/Time" >/dev/null 2>&1; then
  print "proxy 4780: ok"
else
  print -u2 "proxy 4780: NOT usable — start 海豚湾.app first, then re-run this script."
  print -u2 "  (every account/quote path fails closed to UNKNOWN without it)"
  exit 1
fi

cd "${repo_dir}" || exit 78

# 2. The read-only pages. Each *-ctl.sh is a no-op when the page already runs,
#    so this script is safe to re-run.
rc=0
scripts/account-board-ctl.sh start 8766 || rc=1
scripts/xstocks-gap-ctl.sh start 8770 || rc=1
# Refresh the Kraken exit/funding route matrix before the v4 watch reads it.
.venv/bin/python scripts/xstocks_route_matrix.py || rc=1
# Refresh Kraken market-unit / minimum-order config (read-only validate).
.venv/bin/python scripts/kraken_xstocks_units.py || rc=1
# Read-only multi-chain v4 watch; writes only data/xstocks_v4_watch.json.
scripts/xstocks-v4-ctl.sh start 300 || rc=1
# Ethereum auto chain; policy default is off, so this is idle/paper only until
# operator flips both execution keys.
scripts/xstocks-eth-auto-ctl.sh start || rc=1
if (( with_hk_board )); then
  scripts/board-ctl.sh start 8765 || rc=1
else
  print "market board 8765: skipped (hk sampler is parked by operator decision;"
  print "  pass --with-hk-board to start it anyway — it would show stale rows)"
fi

# 3. arcus-farmer is launchd's job, but its dashboard has a known port race on
#    restart: the predecessor can hold 127.0.0.1:8788 for longer than main.py's
#    20 s retry, and the new process then trades WITHOUT the dashboard for the
#    rest of its life (observed 2026-10-06 11:22, fixed by a second restart).
#    So the reboot checklist is not done until the page answers.
snapshot="$(curl -sS --max-time 8 "http://127.0.0.1:8788/api/snapshot" 2>/dev/null)"
if [[ -n "${snapshot}" ]]; then
  print "arcus-farmer: dashboard ok — ${snapshot[1,220]}"
else
  print -u2 "arcus-farmer: no dashboard on 8788."
  print -u2 "  check for 'running WITHOUT the dashboard' in"
  print -u2 "  /home/operator/Documents/ChatGPT/skarb/arcus-farmer/logs/com.arcusfarm.mainnet.out.log"
  print -u2 "  if present: launchctl kickstart -k gui/\$(id -u)/com.arcusfarm.mainnet"
  rc=1
fi

# 4. The watchdog is a separate launchd job and it WILL fire once right after a
#    reboot: state/mainnet.watchdog still holds the pre-reboot timestamp, so its
#    first sample is hours stale and it cancels every resting order. That is the
#    designed behaviour, and the bot re-covers afterwards — so the only useful
#    check is that the SELL cover exists again, not that nothing was cancelled.
farmer_db="/home/operator/Documents/ChatGPT/skarb/arcus-farmer/state/mainnet.db"
if [[ -f "${farmer_db}" ]]; then
  cover="$(sqlite3 "${farmer_db}" \
    "select 'SELL=' || sum(status='OPEN' and side='SELL') || ' BUY=' || sum(status='OPEN' and side='BUY') from orders;" 2>/dev/null)"
  print "arcus-farmer open orders: ${cover}  (expect SELL >= 12 covering inventory)"
fi

exit "${rc}"
