#!/usr/bin/env python3
"""xStocks 自动链：发现 → 复核 → 执行（买/充/卖）→ 记账 → 回款判定 → 告警。

    scripts/xstocks-auto-ctl.sh start|stop|status       # 常驻
    scripts/xstocks_auto_live.py --once                 # 只跑一轮
    scripts/xstocks_auto_live.py --once --confirm-live   # live 还要求这个显式开关

三条不变量：
  1. **判定全是硬编码**（`xstocks_auto` 模块），LLM 不在下单路径上。
  2. **每一步失败即停**；广播后没有确认 → UNKNOWN，停机等人对账，不重播。
  3. **一笔在飞**：同一时间只允许一笔（`xstocks_cycle.CycleLock`）。

阈值只有一份来源：`ops/xstocks_policy.toml`。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.execution import xstocks_auto as auto          # noqa: E402
from arb_monitor.execution import xstocks_buy as buy            # noqa: E402
from arb_monitor.execution import xstocks_deposit as dep        # noqa: E402
from arb_monitor.execution import xstocks_sell as sell          # noqa: E402
from arb_monitor.execution.xstocks_cycle import (               # noqa: E402
    CycleBusy, CycleLock)
from arb_monitor.ops.notify import FeishuNotifier               # noqa: E402
from arb_monitor.venues.kraken_spot import (                    # noqa: E402
    KrakenError, KrakenSpotClient, load_credentials)

PROXY = "http://127.0.0.1:4780"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
XLAYER_RPC = "https://rpc.xlayer.tech"
JUP_BASE = "https://api.jup.ag"
JUP_PROGRAM = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
WALLET = "AmkjszArkzSKfSUqqgFJLGe6ytjhJxjHFaaVrCxAf2ev"
EVM_WALLET = "0xabababababababababababababababababababab"
USDG_XLAYER = "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8"
SOL_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
SIZES = (Decimal("25"), Decimal("10"), Decimal("5"), Decimal("2"))


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


def b58(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + out


def load_env_file(path: Path) -> None:
    """把 config/*.env 的键导进环境（已有值不覆盖）。"""
    try:
        lines = Path(path).read_text().splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


class Http:
    """统一走本地代理的 HTTP 客户端。"""

    def __init__(self, timeout: float = 30.0):
        import requests

        self.session = requests.Session()
        self.session.proxies = {"http": PROXY, "https": PROXY}
        self.timeout = timeout
        self.headers = {"x-api-key": os.environ.get("JUPITER_API_KEY", "")}

    def get(self, url: str, **params):
        headers = self.headers if "jup.ag" in url else {}
        return self.session.get(url, params=params or None, headers=headers,
                                timeout=self.timeout).json()

    def post_json(self, url: str, payload):
        headers = self.headers if "jup.ag" in url else {}
        return self.session.post(url, json=payload, headers=headers,
                                 timeout=self.timeout).json()


class Jupiter:
    def __init__(self, http: Http):
        self.http = http

    def quote(self, input_mint: str, output_mint: str, raw_amount: int,
              slippage_bps: int) -> dict:
        return self.http.get(f"{JUP_BASE}/swap/v1/quote", inputMint=input_mint,
                             outputMint=output_mint, amount=raw_amount,
                             slippageBps=slippage_bps)

    def build_swap(self, quote: dict) -> dict:
        return self.http.post_json(f"{JUP_BASE}/swap/v1/swap", {
            "quoteResponse": quote, "userPublicKey": WALLET,
            "wrapAndUnwrapSol": True, "dynamicComputeUnitLimit": True})

    def sol_usd(self) -> Decimal:
        try:
            price = self.http.get(f"{JUP_BASE}/price/v3",
                                  ids="So11111111111111111111111111111111111111112")
            return Decimal(str(list(price.values())[0]["usdPrice"]))
        except Exception:  # noqa: BLE001
            return Decimal("0")


class Solana:
    def __init__(self, http: Http):
        self.http = http

    def rpc(self, method: str, params: list, tries: int = 5):
        last = None
        for _ in range(tries):
            try:
                payload = self.http.post_json(SOLANA_RPC,
                                              {"jsonrpc": "2.0", "id": 1,
                                               "method": method, "params": params})
                if "result" in payload:
                    return payload["result"]
                last = payload.get("error")
            except Exception as exc:  # noqa: BLE001
                last = exc
            time.sleep(2)
        raise RuntimeError(f"rpc {method} 失败：{last}")

    def raw_balance(self, mint: str) -> int:
        rows = self.rpc("getTokenAccountsByOwner",
                        [WALLET, {"mint": mint}, {"encoding": "jsonParsed"}])
        return sum(int(r["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
                   for r in rows["value"])

    def lamports(self) -> int:
        return int(self.rpc("getBalance", [WALLET])["value"])

    def simulate(self, tx_b64: str) -> dict:
        value = self.rpc("simulateTransaction",
                         [tx_b64, {"encoding": "base64", "sigVerify": False,
                                   "replaceRecentBlockhash": True}])["value"]
        return {"ok": not value.get("err"), "err": value.get("err"),
                "post": value.get("postTokenBalances") or []}


class Wallet:
    """OnchainOS TEE 钱包：签名与广播只在这里发生。"""

    def __init__(self, env: dict | None = None, runner=subprocess.run):
        self.env = dict(os.environ if env is None else env,
                        HTTPS_PROXY=PROXY, HTTP_PROXY=PROXY)
        self.runner = runner

    def _call(self, args: list[str]) -> dict:
        out = self.runner(["onchainos"] + args, capture_output=True, text=True,
                          env=self.env, timeout=180).stdout
        try:
            return json.loads(out)
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": out[:300]}

    def swap(self, tx_b64: str) -> dict:
        tx = b58(base64.b64decode(tx_b64))
        res = self._call(["wallet", "contract-call", "--chain", "solana",
                          "--to", JUP_PROGRAM, "--unsigned-tx", tx, "--from", WALLET])
        return {"ok": bool(res.get("ok")), "tx": (res.get("data") or {}).get("txHash"),
                "error": res.get("error")}

    def send(self, mint: str, recipient: str, raw_amount: int) -> dict:
        res = self._call(["wallet", "send", "--chain", "solana",
                          "--contract-token", mint, "--recipient", recipient,
                          "--amt", str(raw_amount), "--from", WALLET])
        return {"ok": bool(res.get("ok")), "tx": (res.get("data") or {}).get("txHash"),
                "error": res.get("error")}


class XLayerRead:
    """只读 X Layer 余额（USDG + 原生 OKB），用于两链资金告警。"""

    def __init__(self, http: Http):
        self.http = http

    def balances(self) -> dict:
        data = "0x70a08231" + "0" * 24 + EVM_WALLET[2:].lower()
        rows = self.http.post_json(XLAYER_RPC, [
            {"jsonrpc": "2.0", "id": "gas", "method": "eth_getBalance",
             "params": [EVM_WALLET, "latest"]},
            {"jsonrpc": "2.0", "id": "usdg", "method": "eth_call",
             "params": [{"to": USDG_XLAYER, "data": data}, "latest"]},
        ])
        rows = rows if isinstance(rows, list) else []
        out = {"okb": None, "usdg": None}
        for row in rows:
            rid, raw = row.get("id"), row.get("result")
            if not raw or raw == "0x":
                continue
            if rid == "gas":
                out["okb"] = Decimal(int(raw, 16)) / Decimal(10 ** 18)
            elif rid == "usdg":
                out["usdg"] = Decimal(int(raw, 16)) / Decimal(10 ** 6)
        return out


class KrakenIO:
    def __init__(self, client: KrakenSpotClient):
        self.client = client

    def balance(self) -> dict:
        return self.client.balance()

    def depth(self, symbol: str, count: int = 25) -> dict:
        return self.client.depth(symbol, count=count)

    def deposit_plan(self, symbol: str, mint: str, tokens: Decimal,
                     decimals: int = dep.XSTOCK_DECIMALS) -> dep.DepositPlan:
        methods = self.client.deposit_methods(symbol)
        method = dep.find_solana_method(methods, symbol)
        addresses = (self.client.deposit_addresses(symbol, method["method"])
                     if method else [])
        return dep.plan_deposit(symbol=symbol, mint=mint, tokens=tokens,
                                methods=methods, addresses=addresses, decimals=decimals)

    def withdraw(self, asset: str, key: str, amount: Decimal) -> dict:
        return self.client._post("/0/private/Withdraw",
                                 {"asset": asset, "key": key, "amount": str(amount)})

    def withdraw_addresses(self) -> list[dict]:
        return self.client._post("/0/private/WithdrawAddresses", {}) or []


class Notify:
    """signal=机会/成交；alert=异常/停机/亏损/缺钱。复用现有飞书群机器人。"""

    def __init__(self):
        settings = {"channel": "webhook",
                    "webhook_url": os.environ.get("ARB_FEISHU_WEBHOOK", ""),
                    "alert_channel": "webhook",
                    "alert_webhook_url": os.environ.get("ARB_FEISHU_ALERT_WEBHOOK", "")}
        self.signal = FeishuNotifier(settings, audience="signal")
        self.alert = FeishuNotifier(settings, audience="alert")

    def send(self, audience: str, text: str) -> dict:
        target = self.alert if audience == "alert" else self.signal
        try:
            return target.send(text)
        except Exception as exc:  # noqa: BLE001
            log(f"通知失败（{audience}）：{type(exc).__name__}: {exc}")
            return {"ok": False, "detail": str(exc)[:200]}


# ---------------------------------------------------------------------------
# 一轮：巡检 → 回款 → 复核 → 执行 → 记账
# ---------------------------------------------------------------------------
class AutoChain:
    def __init__(self, *, policy: auto.AutoPolicy, jupiter: Jupiter, solana: Solana,
                 wallet: Wallet, kraken: KrakenIO, xlayer: XLayerRead, notify: Notify,
                 confirm_live: bool = False, sleep=time.sleep, now=time.time):
        self.policy = policy
        self.jupiter = jupiter
        self.solana = solana
        self.wallet = wallet
        self.kraken = kraken
        self.xlayer = xlayer
        self.notify = notify
        self.confirm_live = confirm_live
        self.sleep = sleep
        self.now = now
        self.seen = auto.load_transfer_seen()
        self._last_review: dict | None = None

    # --- 闸门与余额 ----------------------------------------------------
    def _live(self) -> bool:
        ok, why = self.policy.live_ok()
        if not ok:
            return False
        if not self.confirm_live:
            log(f"live 已授权，但命令行没给 --confirm-live（{why}）")
            return False
        return True

    def _balances(self) -> dict:
        sol_usdt = Decimal(self.solana.raw_balance(SOL_USDT)) / Decimal(10 ** 6)
        sol_usdc = Decimal(self.solana.raw_balance(SOL_USDC)) / Decimal(10 ** 6)
        lamports = self.solana.lamports()
        kraken = self.kraken.balance()
        xl = self.xlayer.balances()
        return {
            "solana_usdt": sol_usdt, "solana_usdc": sol_usdc,
            "solana_lamports": lamports,
            "kraken_usd": Decimal(str(kraken.get("ZUSD") or 0)),
            "kraken_usdt": Decimal(str(kraken.get("USDT") or 0)),
            "xlayer_usdg": xl["usdg"], "xlayer_okb": xl["okb"],
            "kraken_holdings": {k: str(v) for k, v in kraken.items()
                                if k.endswith(".T") and v},
        }

    def _funding_source(self, balances: dict) -> tuple[str, Decimal]:
        if balances["solana_usdt"] >= balances["solana_usdc"]:
            return SOL_USDT, balances["solana_usdt"]
        return SOL_USDC, balances["solana_usdc"]

    @staticmethod
    def _top_bid(bids) -> Decimal:
        return Decimal(str(bids[0][0])) if bids else Decimal(0)

    # --- 复核（固定编码，用新鲜报价） -----------------------------------
    def _review(self, hit: dict, balances: dict) -> dict | None:
        symbol, mint = str(hit["symbol"]), str(hit["address"])
        sol_usd = self.jupiter.sol_usd()
        input_mint, spend = self._funding_source(balances)
        if spend <= 0:
            log(f"{symbol}: 链上没有 USDC/USDT，跳过")
            return None
        first = auto.transfer_is_first(symbol, self.seen)
        best = None
        for size in [s for s in SIZES if s <= spend]:
            quote = self.jupiter.quote(input_mint, mint, int(size * Decimal(10 ** 6)),
                                       self.policy.max_slippage_bps)
            if "outAmount" not in quote:
                log(f"{symbol}@{size}U 报价失败：{json.dumps(quote)[:120]}")
                continue
            spent, tokens, route, impact = buy.parse_quote(
                quote, swap_mint_decimals=6, token_decimals=dep.XSTOCK_DECIMALS)
            book = self.kraken.depth(symbol)
            bids = book.get("bids") or []
            plan = sell.plan_sell(self.policy, symbol=symbol, tokens=str(tokens),
                                  ref=Decimal(str(hit.get("ref") or "0")), bids=bids,
                                  asks=book.get("asks") or [], pair_status="online")
            deposit = self.kraken.deposit_plan(symbol, mint, tokens)
            ok, why, stats = auto.review_ok(
                self.policy, size_usd=size, tokens=tokens,
                sell_price=self._top_bid(bids), sol_usd=sol_usd,
                first_transfer=first, risk=buy.classify_risk(impact),
                book_ok=plan.ok, book_reason=plan.reason,
                deposit_ok=deposit.ok, deposit_reason=deposit.reason)
            log(f"{symbol}@{size}U 复核：{why}")
            self._last_review = {"ts_ms": int(self.now() * 1000), "symbol": symbol,
                                 "size_usd": str(size), "ok": ok, "reason": why}
            if ok:
                best = {"hit": hit, "size": size, "quote": quote, "tokens": tokens,
                        "spent": spent, "impact": impact, "route": route,
                        "plan": plan, "deposit": deposit, "stats": stats,
                        "input_mint": input_mint}
        return best

    # --- 执行 ----------------------------------------------------------
    def _execute(self, chosen: dict) -> dict:
        hit, size = chosen["hit"], chosen["size"]
        symbol, mint = str(hit["symbol"]), str(hit["address"])
        # 这一笔是不是该标的的首次充值——决定扣哪一档转账费（首次含替 Kraken 建账户）。
        first_transfer = auto.transfer_is_first(symbol, self.seen)
        row = auto.build_ledger_row(symbol=symbol, network="Solana", size_usd=size,
                                    state="refused", reason="复核通过", now=self.now(),
                                    mode=self.policy.auto_orders)
        swap = self.jupiter.build_swap(chosen["quote"])
        if "swapTransaction" not in swap:
            row["reason"] = f"构造交易失败 {str(swap)[:120]}"
            return row
        sim = self.solana.simulate(swap["swapTransaction"])
        if not sim["ok"]:
            row["reason"] = f"模拟失败 {sim['err']}"
            return row
        quoted_raw = int(chosen["quote"]["outAmount"])
        simulated = 0
        for entry in sim.get("post") or []:
            if entry.get("mint") == mint and entry.get("owner") == WALLET:
                simulated = int(entry["uiTokenAmount"]["amount"])
        if simulated and simulated < quoted_raw * 99 // 100:
            row["reason"] = f"模拟到手 {simulated} < 报价 {quoted_raw}"
            return row
        log(f"{symbol}: 模拟通过，到手 {simulated or quoted_raw} 最小单位")
        row["fees"]["network"] = "simulated"
        if not self._live():
            row.update(state="paper",
                       reason=f"不广播（净利 {chosen['stats']['net_usdt']:.4f}）")
            self.notify.send("signal", f"🧪 paper {symbol} {size}U：净利 "
                                       f"{chosen['stats']['net_usdt']:.4f} USDT（未广播）")
            return row

        before = self.solana.raw_balance(mint)
        res = self.wallet.swap(swap["swapTransaction"])
        if not res["ok"]:
            row.update(state="stopped", reason=f"买入广播失败 {res['error']}")
            return row
        row["txids"]["buy"] = res["tx"]
        row["bought_at_ms"] = int(self.now() * 1000)
        held = self._wait_balance(mint, before, tries=12, pause=4)
        if held is None:
            row.update(state="unknown",
                       reason="买入已广播但余额没增加——停机对账，不重播")
            self.notify.send("alert", f"⚠️ {symbol} 买入广播后余额没变"
                                     f"（tx {res['tx']}），自动链停机等人对账。")
            return row
        tokens = Decimal(held) / Decimal(10 ** 8)
        row["tokens"] = str(tokens)
        row["buy_price"] = str(chosen["spent"] / tokens)
        row.update(state="bought", reason="买入确认")

        deposit = self.kraken.deposit_plan(symbol, mint, tokens)
        if not deposit.ok:
            row.update(state="stopped", reason=f"充值闸门不过：{deposit.reason}")
            self.notify.send("alert", f"⚠️ {symbol} 已买到 {tokens} 枚，但充值闸门不过："
                                     f"{deposit.reason}；币在我们钱包。")
            return row
        sent = self.wallet.send(mint, deposit.destination, deposit.raw_amount)
        if not sent["ok"]:
            row.update(state="stopped", reason=f"转账失败（币在钱包）：{sent['error']}")
            self.notify.send("alert", f"⚠️ {symbol} 转账失败（币在我们钱包）："
                                     f"{sent['error']}")
            return row
        row["txids"]["deposit"] = sent["tx"]
        self.seen = auto.mark_transfer_seen(symbol, self.seen)
        # 入账数量 = 链上原始枚数 × 乘数（2026-10-06 实盘核实），按增量判定，
        # 免得旧尘埃把「已入账」骗过去。
        before_credit = self._kraken_units(symbol)
        expected_credit = tokens * self._multiplier(symbol)
        credit = self._wait_credit(symbol, expected_credit, before_credit)
        if credit is None:
            row.update(state="unknown",
                       reason="充值已发出但 Kraken 没确认入账——停机对账，别重复转")
            self.notify.send("alert", f"⚠️ {symbol} 已转出 {tokens} 但 Kraken 没入账"
                                     f"（tx {sent['tx']}），停机对账。")
            return row
        row["credited_at_ms"] = int(self.now() * 1000)
        row["credit_wait_s"] = round(row["credited_at_ms"] / 1000
                                     - row["bought_at_ms"] / 1000, 1)
        row["fees"]["credited_units"] = str(credit)
        row.update(state="credited", reason=f"Kraken 入账 {credit}")

        sold = self._sell(symbol, credit, hit)
        if not sold["ok"]:
            row.update(state="stopped", reason=f"卖出没成：{sold['reason']}")
            self.notify.send("alert", f"⚠️ {symbol} 卖出没成：{sold['reason']}；"
                                     "币留在 Kraken 等人处理。")
            return row
        row["txids"]["sell"] = sold.get("txid")
        row["sell_price"] = str(sold["price"])
        row["sold_at_ms"] = int(self.now() * 1000)
        stats = auto.estimate_net(self.policy, size_usd=size, tokens=credit,
                                  sell_price=sold["price"],
                                  sol_usd=self.jupiter.sol_usd(),
                                  first_transfer=first_transfer)
        # Kraken 回单里有实际成交额与手续费时，用实际值覆盖模型值（更准）。
        if sold.get("cost") is not None and sold.get("fee") is not None:
            cost = Decimal(str(sold["cost"]))
            paid = Decimal(str(sold["fee"]))
            stats["gross_usdt"] = cost - size
            stats["sell_fee_usdt"] = paid
            stats["net_usdt"] = cost - paid - size - stats["chain_fee_usdt"]
            row["sell_price"] = str(sold.get("avg") or sold["price"])
        row["gross_usdt"] = str(stats["gross_usdt"])
        row["net_usdt"] = str(stats["net_usdt"])
        row["refundable_rent_usdt"] = str(stats["rent_refundable_usdt"])
        row["fees"].update({"sell_fee_usdt": str(stats["sell_fee_usdt"]),
                            "chain_fee_usdt": str(stats["chain_fee_usdt"])})
        row.update(state="sold", reason="两腿完成")
        return row

    def _wait_balance(self, mint: str, before: int, *, tries: int, pause: int):
        for _ in range(tries):
            self.sleep(pause)
            now = self.solana.raw_balance(mint)
            if now > before:
                return now - before
        return None

    @staticmethod
    def _multiplier(symbol: str) -> Decimal:
        """链上原始枚数 → Kraken 记账单位（= 原始 × 乘数，实盘核实过）。"""
        try:
            raw = json.loads((ROOT / "data" / "xstocks_multipliers.json").read_text())
            return Decimal(str((raw.get(symbol) or {}).get("current") or "1"))
        except Exception:  # noqa: BLE001
            return Decimal("1")

    def _kraken_units(self, symbol: str) -> Decimal:
        try:
            return Decimal(str(self.kraken.balance().get(f"{symbol}.T") or 0))
        except KrakenError:
            return Decimal(0)

    def _wait_credit(self, symbol: str, expected: Decimal, before: Decimal, *,
                     tries: int = 60, pause: int = 15):
        """按**数量增量**判入账：≥ 预期到账 × 0.99 才算（尘埃骗不过去）。"""
        want = max(expected, Decimal("0.0000001")) * Decimal("0.99")
        for _ in range(tries):
            self.sleep(pause)
            try:
                held = Decimal(str(self.kraken.balance().get(f"{symbol}.T") or 0))
            except KrakenError:
                continue
            if held >= before + want:
                return held - before
        return None

    def _held_amount(self, symbol: str) -> Decimal | None:
        try:
            return Decimal(str(self.kraken.balance().get(f"{symbol}.T") or 0))
        except KrakenError:
            return None                    # 读不到就标未知，别误判成交

    def _cancel(self, symbol: str) -> None:
        try:
            for order in self.kraken.client.open_orders():
                if str((order.get("descr") or {}).get("pair", "")).startswith(symbol):
                    self.kraken.client._post("/0/private/CancelOrder",
                                             {"txid": order.get("id")})
        except Exception:  # noqa: BLE001
            pass

    def _sell(self, symbol: str, tokens: Decimal, hit: dict) -> dict:
        """吃买一（taker）。2 分钟不成交就撤单重挂一次，再不成停下。"""
        for attempt in range(2):
            book = self.kraken.depth(symbol)
            bids = book.get("bids") or []
            plan = sell.plan_sell(self.policy, symbol=symbol, tokens=str(tokens),
                                  ref=Decimal(str(hit.get("ref") or "0")), bids=bids,
                                  asks=book.get("asks") or [], pair_status="online")
            if not plan.ok:
                return {"ok": False, "reason": plan.reason}
            price = plan.best_bid
            before = self._held_amount(symbol)
            order = self.kraken.client.order(symbol, str(tokens), side="sell",
                                             ordertype="limit", price=f"{price:.2f}",
                                             oflags="fciq", confirm="live")
            txid = (order.get("txid") or [None])[0]
            for _ in range(24):                      # 最多 2 分钟
                self.sleep(5)
                held = self._held_amount(symbol)
                if held is not None and (before is None
                                         or held <= before - tokens * Decimal("0.9")):
                    filled = self._fill_detail(txid)
                    return {"ok": True, "price": price, "txid": txid, **filled}
            self._cancel(symbol)
            log(f"{symbol}: 第 {attempt + 1} 次挂出未成交，撤单重挂")
        return {"ok": False, "reason": "两次挂出都没成交"}

    def _fill_detail(self, txid: str | None) -> dict:
        """回读 Kraken 订单，拿实际成交额 / 手续费 / 均价（失败就留空，用模型值）。"""
        if not txid:
            return {}
        try:
            rows = self.kraken.client._post("/0/private/QueryOrders",
                                            {"txid": txid, "trades": True})
            order = (rows or {}).get(txid) or {}
            cost, fee, vol = order.get("cost"), order.get("fee"), order.get("vol_exec")
            avg = (Decimal(str(cost)) / Decimal(str(vol))) if cost and vol else None
            return {"cost": cost, "fee": fee, "vol_exec": vol,
                    "avg": str(avg) if avg else None}
        except Exception:  # noqa: BLE001
            return {}

    # --- 回款 ----------------------------------------------------------
    def _do_withdraw(self, decision: dict, state: auto.AutoState) -> None:
        if not self._live():
            log(f"回款有额度（{decision['reason']}），但当前不是 live+confirm，不执行")
            return
        key = decision["key"]
        known = {str(row.get("key") or "") for row in self.kraken.withdraw_addresses()}
        if key not in known:
            self.notify.send("alert", f"⚠️ Kraken 提币地址簿里没有 key={key}，回款中止"
                                     "（地址只能你在 App 里加）。")
            return
        try:
            result = self.kraken.withdraw(decision["asset"], key, decision["amount"])
        except Exception as exc:  # noqa: BLE001
            self.notify.send("alert", f"⚠️ 回款失败：{type(exc).__name__}: {exc}")
            return
        log(f"回款已提交 {decision['amount']} {decision['asset']}：{result}")
        self.notify.send("signal", f"💸 已从 Kraken 提 {decision['amount']} "
                                   f"{decision['asset']} 回 Solana（{decision['reason']}）")
        auto.append_ledger(auto.build_ledger_row(
            symbol="-", network="Kraken", size_usd=decision["amount"],
            state="withdraw", reason=decision["reason"], now=self.now(),
            withdrew_usdt=str(decision["amount"])))
        state.last_cycle = {"state": "withdraw", "amount": str(decision["amount"])}

    # --- 成交后的通知与熔断 ---------------------------------------------
    def _after_cycle(self, row: dict, state: auto.AutoState,
                     tracker: auto.AlertTracker, day: str) -> None:
        if row.get("state") != "sold":
            level = "alert" if row.get("state") in ("unknown", "stopped") else "signal"
            icon = "⚠️" if level == "alert" else "ℹ️"
            self.notify.send(level, f"{icon} {row['symbol']} {row['size_usd']}U → "
                                    f"{row['state']}：{row['reason']}")
            return
        net = Decimal(str(row["net_usdt"]))
        icon = "✅" if net >= 0 else "🔻"
        self.notify.send("signal",
                         f"{icon} {row['symbol']} {row['size_usd']}U 完成：买入 "
                         f"{row['buy_price']} → 卖出 {row['sell_price']}，净利 "
                         f"{row['net_usdt']} USDT（tx {row['txids'].get('buy')}）")
        streak = auto.loss_streak(auto.read_ledger())
        state.loss_streak = streak
        if net < 0:
            self.notify.send("alert", f"🔻 {row['symbol']} 这一笔净亏 {row['net_usdt']} "
                                     f"USDT，请检查。")
        if streak >= self.policy.loss_pause_trades and not state.pause_reason:
            state.pause_reason = f"连续 {streak} 笔净亏（{day}）"
            auto.PAUSE_PATH.write_text(state.pause_reason)
            self.notify.send("alert", f"⛔ 连续 {streak} 笔净亏，自动链已软暂停；"
                                     f"处理完删除 {auto.PAUSE_PATH} 再恢复。")
        state.alerts_sent = tracker.to_json()

    # --- 一轮 ----------------------------------------------------------
    def run_once(self, *, state: auto.AutoState | None = None) -> auto.AutoState:
        policy = self.policy
        state = state or auto.read_state()
        rows = auto.read_ledger()
        state.refresh_day(auto.day_key(self.now()), rows)
        state.mode = policy.auto_orders
        state.running = True
        state.updated_ms = int(self.now() * 1000)
        tracker = auto.AlertTracker(state.alerts_sent)
        day = state.day

        try:
            balances = self._balances()
        except Exception as exc:  # noqa: BLE001
            state.note = f"余额读取失败：{type(exc).__name__}: {exc}"
            log(state.note)
            return state
        state.balances = {k: (str(v) if isinstance(v, Decimal) else v)
                          for k, v in balances.items()}
        onchain_usdt = balances["solana_usdt"] + balances["solana_usdc"]

        for alert in auto.funding_alerts(
                policy, solana_usdt=onchain_usdt,
                xlayer_usdg=balances["xlayer_usdg"],
                sol_lamports=balances["solana_lamports"]):
            if tracker.should_send(alert["key"], day):
                tracker.mark(alert["key"], day)
                auto.write_alert(alert)
                self.notify.send("alert", alert["text"])

        decision = auto.treasury_decision(policy, kraken_usd=balances["kraken_usd"],
                                          onchain_usdt=balances["solana_usdt"])
        if decision["action"] == "withdraw":
            self._do_withdraw(decision, state)
        elif decision["action"] == "wait":
            log(f"回款：{decision['reason']}")

        snapshot = auto.load_snapshot()
        age = auto.snapshot_age_s(snapshot, now_ms=int(self.now() * 1000))
        ok, why = auto.open_gate(policy, state, snapshot_age=age,
                                 kill_switch=auto.KILL_SWITCH.exists(),
                                 auto_halt=auto.AUTO_HALT.exists(),
                                 onchain_usdt=onchain_usdt)
        if not ok:
            state.phase = "idle"
            state.note = why
            state.alerts_sent = tracker.to_json()
            log(f"不开仓：{why}")
            return state

        state.phase = "scanning"
        candidates = auto.order_candidates(snapshot, policy)
        if not candidates:
            state.phase = "idle"
            state.note = "本轮没有候选（净利、风险、Solana 三条都过才算）"
            log(state.note)
            state.alerts_sent = tracker.to_json()
            return state
        state.phase = "reviewing"
        chosen = None
        for hit in candidates:
            chosen = self._review(hit, balances)
            if chosen:
                break
        state.last_review = self._last_review
        if not chosen:
            state.phase = "idle"
            state.note = "候选都复核不过（以新鲜报价/盘口/充值为准）"
            log(state.note)
            state.alerts_sent = tracker.to_json()
            return state

        state.phase = "buying"
        try:
            with CycleLock():
                row = self._execute(chosen)
        except CycleBusy:
            state.phase = "idle"
            state.note = "另一笔在飞（cycle lock），本轮跳过"
            state.alerts_sent = tracker.to_json()
            return state
        auto.append_ledger(row)
        state.last_cycle = row
        state.phase = "idle"
        self._after_cycle(row, state, tracker, day)
        return state


def build(policy: auto.AutoPolicy, confirm_live: bool) -> AutoChain:
    http = Http()
    key, secret = load_credentials(ROOT / "config" / "kraken.env")
    client = KrakenSpotClient(key, secret, proxy=PROXY, timeout=40)
    return AutoChain(policy=policy, jupiter=Jupiter(http), solana=Solana(http),
                     wallet=Wallet(), kraken=KrakenIO(client), xlayer=XLayerRead(http),
                     notify=Notify(), confirm_live=confirm_live)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="xStocks 自动链")
    parser.add_argument("--once", action="store_true", help="只跑一轮")
    parser.add_argument("--confirm-live", action="store_true",
                        help="live 的显式确认（还要 policy 的两把钥匙）")
    parser.add_argument("--policy", default=str(auto.POLICY_PATH))
    args = parser.parse_args(argv)

    for name in ("notify.env", "jupiter.env"):
        load_env_file(ROOT / "config" / name)
    policy = auto.AutoPolicy.load(args.policy)
    log(f"自动链启动：mode={policy.mode} auto_orders={policy.auto_orders} "
        f"单笔≤{policy.per_trade_quote_usdt}U 日≤{policy.daily_open_quote_usdt}U/"
        f"{policy.daily_trades_max}笔")
    if auto.PAUSE_PATH.exists():
        log(f"注意：软暂停文件在（{auto.PAUSE_PATH.read_text().strip()}）")

    chain = build(policy, args.confirm_live)
    if args.once:
        state = chain.run_once()
        state.running = False          # 一次性跑完就不算「进程在跑」
        auto.write_state(state)
        log(f"本轮结束：phase={state.phase} note={state.note}")
        return 0

    state = auto.read_state()
    while True:
        try:
            state = chain.run_once(state=state)
        except KeyboardInterrupt:
            state.running = False
            auto.write_state(state)
            return 0
        except Exception as exc:  # noqa: BLE001
            state.note = f"循环异常：{type(exc).__name__}: {exc}"
            state.phase = "error"
            log(state.note)
        auto.write_state(state)
        time.sleep(policy.poll_interval_s)


if __name__ == "__main__":
    raise SystemExit(main())
