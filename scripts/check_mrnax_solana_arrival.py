#!/usr/bin/env python
"""Read-only check: has the MRNAx test bridge reached our Solana wallet?

Exit 0 when the balance of mint XsViHjyRbiSBskjizb1gszGTmZqtAH8TuqwJ7sB5AcP
on AmkjszArkzSKfSUqqgFJLGe6ytjhJxjHFaaVrCxAf2ev is at least 0.00005.
The on-chain minimum transfer is 0.0001.
Exit 2 when it is not there yet. Exit 3 when the RPC call fails.

The X Layer send can be confirmed long before Solana creates the token
account. This script only looks at Solana.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from decimal import Decimal

OWNER = "AmkjszArkzSKfSUqqgFJLGe6ytjhJxjHFaaVrCxAf2ev"
MINT = "XsViHjyRbiSBskjizb1gszGTmZqtAH8TuqwJ7sB5AcP"
MINIMUM = Decimal("0.00005")
RPC = "https://api.mainnet-beta.solana.com"


def main() -> int:
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("ARB_HTTP_PROXY") or "http://127.0.0.1:4780"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [OWNER, {"mint": MINT}, {"encoding": "jsonParsed"}],
    }).encode()
    request = urllib.request.Request(RPC, data=body, headers={"content-type": "application/json"})
    try:
        with opener.open(request, timeout=25) as response:
            payload = json.loads(response.read().decode())
    except Exception as exc:
        print(f"mrnax_solana rpc_error {type(exc).__name__}")
        return 3
    if payload.get("error"):
        print("mrnax_solana rpc_error", payload["error"])
        return 3
    accounts = ((payload.get("result") or {}).get("value")) or []
    total = Decimal(0)
    for account in accounts:
        info = (((account.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
        amount = ((info.get("tokenAmount") or {}).get("uiAmountString")) or "0"
        total += Decimal(amount)
    arrived = total >= MINIMUM
    print(f"mrnax_solana amount={total} arrived={'yes' if arrived else 'no'} accounts={len(accounts)}")
    return 0 if arrived else 2


if __name__ == "__main__":
    sys.exit(main())
