#!/usr/bin/env python3
"""Build one xStock swap with the execution settings and simulate it.

    scripts/xstocks_swap_sim.py XOPx 20        # 花 20 USDC 买 XOPx

The transaction is the real one Jupiter would hand us, with the slippage and
Jito tip from the board. It is simulated on chain with ``sigVerify=false``:
no key is read, nothing is signed, nothing is broadcast. Use it to see the
true fill and the true cost before deciding to send anything.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import time
import sys
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROXY = "http://127.0.0.1:4780"
OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
)
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/537.36"}


def board():
    spec = importlib.util.spec_from_file_location("board", ROOT / "scripts" / "xstocks_gap_live.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


API_HDR: dict[str, str] = {}
# The board holds the free key's 1 rps budget. A diagnostic run must not eat
# it, so this script quotes on the keyless host instead.
BASE = "https://lite-api.jup.ag"


def api(url: str, data: bytes | None = None, timeout: int = 40, tries: int = 3):
    """The shared local proxy drops connections under load; retry a few times."""
    last: Exception | None = None
    for _ in range(tries):
        headers = {"Accept": "application/json", **API_HDR, **UA}
        if data:
            headers["Content-Type"] = "application/json"
        try:
            with OPENER.open(
                urllib.request.Request(url, data=data, headers=headers), timeout=timeout
            ) as r:
                return json.load(r)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            last = RuntimeError(f"HTTP {exc.code}: {detail}")
            if exc.code < 500:
                raise last
            time.sleep(3)
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(3)
    raise last  # type: ignore[misc]


def rpc(method: str, params: list):
    return api(
        "https://api.mainnet-beta.solana.com",
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        timeout=60,
    )["result"]


def mint_of(symbol: str) -> str:
    conn = sqlite3.connect(f"file:{ROOT / 'data' / 'xstocks_book.sqlite'}?mode=ro", uri=True)
    row = conn.execute(
        """
        SELECT d.address FROM deployments d
        JOIN spot_exits s ON s.issuer_symbol = d.issuer_symbol AND s.missing_since_ms IS NULL
        WHERE d.issuer_symbol = ? AND d.missing_since_ms IS NULL
          AND d.network = 'Solana' AND d.form = 'native'
        """,
        (symbol,),
    ).fetchone()
    conn.close()
    if not row:
        raise SystemExit(f"找不到 {symbol} 的 Solana mint")
    return row[0]


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    symbol = sys.argv[1]
    size = Decimal(sys.argv[2])
    m = board()
    API_HDR.clear()
    mint = mint_of(symbol)
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    via_sol = "--via-sol" in sys.argv
    stock = Decimal(argv[2]) if len(argv) > 2 else None
    wallet = argv[3] if len(argv) > 3 else "AmkjszArkzSKfSUqqgFJLGe6ytjhJxjHFaaVrCxAf2ev"
    sol_px: Decimal | None = None
    try:
        sol_px = Decimal(str(api(f"{BASE}/price/v3?ids={m.SOL_MINT}", timeout=20)[m.SOL_MINT]["usdPrice"]))
    except Exception:  # noqa: BLE001
        pass
    if via_sol:
        if not sol_px:
            raise SystemExit("拿不到 SOL 价格，无法按 USDC 金额换 SOL")
        inp_mint, raw = m.SOL_MINT, int(size / sol_px * Decimal(10) ** 9)
        input_label = f"{size / sol_px:.6f} SOL（≈{size} USDC）"
    else:
        inp_mint, raw = m.SOL_USDC, int(size * Decimal(10) ** 6)
        input_label = f"{size} USDC"
    print(f"{symbol}  花 {size} USDC  滑点 {m.SLIPPAGE_BPS} bps  "
          f"Jito 小费 {m.JITO_TIP_LAMPORTS} lamports  仅模拟，不签名不广播")
    quote = api(
        f"{BASE}/swap/v1/quote?inputMint={inp_mint}&outputMint={mint}"
        f"&amount={raw}&slippageBps={m.SLIPPAGE_BPS}",
        timeout=30,
    )
    if "outAmount" not in quote:
        raise SystemExit("报价失败：" + str(quote.get("errorCode") or quote.get("error")))
    quoted = Decimal(quote["outAmount"]) / Decimal(10) ** 8
    print(f"  付 {input_label}  →  报价到手 {quoted} {symbol}  "
          f"单价 {size / quoted} USDC/枚  "
          f"冲击 {float(quote['priceImpactPct']) * 100:.3f}%  "
          f"路由 {' → '.join(s['swapInfo']['label'] for s in quote['routePlan'])}")
    body = {
        "quoteResponse": quote,
        "userPublicKey": wallet,
        "prioritizationFeeLamports": {"jitoTipLamports": m.JITO_TIP_LAMPORTS},
    }
    swap = api(f"{BASE}/swap/v1/swap", json.dumps(body).encode(), timeout=40)
    sim = rpc("simulateTransaction", [swap["swapTransaction"], {
        "encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True}])["value"]
    print(f"  模拟 err={sim.get('err')}  CU={sim.get('unitsConsumed')}")
    got = None
    for bal in sim.get("postTokenBalances") or []:
        if bal["mint"] == mint and bal.get("owner") == wallet:
            got = Decimal(bal["uiTokenAmount"]["uiAmountString"])
    if got is None:
        print(f"  模拟没有产生代币余额（err={sim.get('err')}）。")
        if not via_sol:
            print(f"  这条腿要花 USDC，而钱包 {wallet[:8]}… 可能没有 USDC。"
                  f"加 --via-sol 改成 SOL 入金再试。")
        return
    print(f"  真实到手 {got} {symbol}  单价 {size / got} USDC/枚  "
          f"与报价差 {(got / quoted - 1) * 100:+.3f}%")
    sol_delta = (sim["postBalances"][0] - sim["preBalances"][0]) / 1e9
    print(f"  钱包 SOL 变化 {sol_delta:+.9f}（含 Jito 小费与账户租金，租金关账户可退）")
    try:
        m._sol_usd = sol_px
        # 净利用 Kraken 真实买一（2026-10-06 起口径）：Kraken 收的枚数就是这里
        # 显示出来的 UI 枚数，正股价只作参考。
        sell = (m.kraken_best_bids([symbol]) or {}).get(symbol)
        if sell:
            gross, net = m.edge_usdt(size, got, sell, symbol=symbol)
            print(f"  按 Kraken 买一 {sell} 卖出（扣 {m.EXIT_COST_BPS} bps）："
                  f"毛利 {m._money(gross)} 净利 {m._money(net)} USDT")
        elif stock is not None:
            print(f"  Kraken 没有买一，不出净利；正股参考 {stock}")
    except Exception as exc:  # noqa: BLE001
        print("  净利没算：", type(exc).__name__, exc)


if __name__ == "__main__":
    main()
