#!/usr/bin/env python3
"""Read the per-destination slots of the Solana-source 27-account table. Never signs.

    scripts/xstocks_v2_solana_lane_read.py            # all destinations with a selector
    scripts/xstocks_v2_solana_lane_read.py eth bsc

For each destination it finds one **successful** top-level ``ccip_send`` to the
issuer program whose lane slot (25) is that destination's lane account, decodes
the 27 accounts and the destination selector out of the instruction data, and
writes them to ``data-v2/xstocks_v2_solana_lanes.json``. A slot that differs
from the X Layer table is printed; nothing is filled in by hand.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.xstocks_v2 import ccip_evm as EVM                  # noqa: E402
from arb_monitor.xstocks_v2 import ccip_solana as C                 # noqa: E402
from arb_monitor.xstocks_v2.feeds.http import Http                  # noqa: E402

SOLANA_RPC = "https://api.mainnet-beta.solana.com"
OUT = ROOT / "data-v2" / "xstocks_v2_solana_lanes.json"
SIGNATURE_LIMIT = 40


def rpc(http, method: str, params: list):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for attempt in range(6):
        status, raw = http.request(SOLANA_RPC, data=body,
                                   headers={"Content-Type": "application/json"})
        payload = json.loads(raw.decode("utf-8", "replace")) if status == 200 else {}
        error = payload.get("error") or ({} if status == 200 else {"code": status})
        if error.get("code") in (429, -32429) or status == 429:
            time.sleep(3 + 2 * attempt)
            continue
        if error:
            raise RuntimeError(f"{method}: {error}")
        return payload.get("result")
    raise RuntimeError(f"{method}: rate limited")


def sample_for(http, dest: str) -> dict | None:
    lane = EVM.SOURCE_LANE_ACCOUNT.get(dest)
    selector = C.solana_dest_selector(dest)
    if not lane or selector is None:
        return None
    for row in rpc(http, "getSignaturesForAddress", [lane, {"limit": SIGNATURE_LIMIT}]):
        if row.get("err"):
            continue
        tx = rpc(http, "getTransaction", [row["signature"], {
            "encoding": "json", "maxSupportedTransactionVersion": 0}])
        time.sleep(0.4)
        if not tx or (tx.get("meta") or {}).get("err"):
            continue
        message = tx["transaction"]["message"]
        keys = list(message["accountKeys"])
        loaded = tx["meta"].get("loadedAddresses") or {}
        keys += loaded.get("writable", []) + loaded.get("readonly", [])
        for ix in message["instructions"]:
            if keys[ix["programIdIndex"]] != C.SOLANA_PROGRAM:
                continue
            accounts = [keys[i] for i in ix["accounts"]]
            if len(accounts) != C.ACCOUNT_COUNT or accounts[25] != lane:
                continue
            try:
                args = C.decode_args(C.b58decode(ix["data"]))
            except ValueError:
                continue
            if args.dest_chain_selector != selector:
                continue
            return {"signature": row["signature"], "block_time": row.get("blockTime"),
                    "selector": selector, "accounts": accounts}
    return None


def main() -> int:
    http = Http()
    wanted = sys.argv[1:] or [chain for chain in C.SOLANA_DEST_SELECTORS if chain != "x-layer"]
    try:
        report = json.loads(OUT.read_text())
    except (OSError, ValueError):
        report = {}
    reference = {index: key for index, _role, key, _s, _w in C.LANE_ACCOUNTS if key}
    for dest in wanted:
        found = sample_for(http, dest)
        if not found:
            print(f"{dest}: no successful ccip_send in the last {SIGNATURE_LIMIT} lane signatures")
            report[dest] = {"found": False, "checked_at": int(time.time())}
            continue
        diff = {index: found["accounts"][index] for index, key in reference.items()
                if found["accounts"][index] != key}
        variable = sorted(C.VARIABLE_INDEXES)
        print(f"{dest}: {found['signature'][:20]} differs from X Layer at "
              f"{sorted(set(diff) - set(variable))}")
        report[dest] = {"found": True, "checked_at": int(time.time()), **found,
                        "per_dest_slots": {str(k): v for k, v in diff.items()
                                           if k not in C.VARIABLE_INDEXES}}
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n")
    print(f"-> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
