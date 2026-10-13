"""Direct, read-only X Layer (chain 196) reads for xStocks tokenized equities.

No wallet, no private key, no transaction submission, no signing. This module
answers one question the CEX side cannot: what does the tokenized stock trade
for on-chain right now, 24/7, when the underlying share market is closed?

Two independent on-chain observations are produced:

1. ``pool_state`` - Uniswap-V3-style ``slot0``/``liquidity`` plus actual token
   balances of a specific pool contract. Depth is measured, not assumed: a pool
   with liquidity 0 (many xStocks pools are initialized but empty) is reported
   as such and never becomes a "price".
2. ``trade_print`` - decoded transfers of real swap transactions: the trader's
   stablecoin leg divided by the trader's token leg, taken from the receipt of
   the transaction itself. This works for aggregator/RFQ routes that are not a
   single readable AMM pool.

Neither slot0 nor a past trade is the order. A sized quote is an ``eth_call``
to QuoterV2 (``quote_single``). Signing and broadcasting stay on OnchainOS.
"""
from __future__ import annotations

from decimal import Decimal, localcontext
import json
import time
import urllib.error
import urllib.request

XLAYER_CHAIN_ID = 196
DEFAULT_RPC_URL = "https://rpc.xlayer.tech"
# Public read-only limits observed on this RPC: 100 blocks per eth_getLogs.
# A batch of 12 calls is rejected with -32014 "too many RPC calls" (measured
# 2026-09-23). 10 is the largest batch that returns a list.
MAX_LOG_BLOCK_RANGE = 100
MAX_BATCH_CALLS = 10

UNISWAP_V3_FACTORY = "0x4b2ab38dbf28d31d467aa8993f6c2585981d6804"
# Official Uniswap v3 QuoterV2 on X Layer. Checked 2026-09-23: its factory()
# returns the factory above. eth_call only; it does not sign or broadcast.
QUOTER_V2 = "0xd1b797d92d87b688193a2b976efc8d577d204343"

# Stablecoins seen as the quote leg of X Layer xStocks pools/routes. The USDG,
# USDT0 and USDC/USDT legs are all treated as 1 USD for screening only; a real
# depeg or a non-1.00 quote token would need its own verified rate.
STABLE_TOKENS: dict[str, tuple[str, int]] = {
    "0xb6ceceab302e2e4948951ee7843fc24e92933061": ("USDC", 6),
    "0x779ded0c9e1022225f8e0630b35a9b54be713736": ("USDT0", 6),
    "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8": ("USDG", 6),
    "0x1e4a5963abfd975d8c9021ce480b42188849d41d": ("USDT", 6),
}

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
BURN_ADDRESSES = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dEaD".lower(),
}

SELECTORS = {
    "token0": "0x0dfe1681",
    "token1": "0xd21220a7",
    "fee": "0xddca3f43",
    "liquidity": "0x1a686502",
    "slot0": "0x3850c7bd",
    "getPool": "0x1698ee82",
}
FEES = ("100", "500", "3000", "10000")


def quote_single(rpc, quoter: str, *, token_in: str, token_out: str,
                 amount_raw: int, fee: int, exact_out: bool) -> int | None:
    """One pool's amount in or out, from QuoterV2. ``None`` when the call reverts.

    ``exact_out`` asks ``quoteExactOutputSingle`` (how much token_in buys
    ``amount_raw`` of token_out). Otherwise ``quoteExactInputSingle``.
    """
    from arb_monitor.core.evm import encode_call

    if amount_raw <= 0:
        return None
    signature = ("quoteExactOutputSingle((address,address,uint256,uint24,uint160))"
                 if exact_out else
                 "quoteExactInputSingle((address,address,uint256,uint24,uint160))")
    data = encode_call(signature, token_in, token_out, str(amount_raw), str(int(fee)), "0")
    try:
        out = rpc.call(quoter, data)
    except Exception:
        return None
    word = str(out or "")
    if word.startswith("0x"):
        word = word[2:]
    if len(word) < 64:
        return None
    amount = int(word[:64], 16)
    return amount if amount > 0 else None


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def pad(address: str) -> str:
    return address.lower().removeprefix("0x").rjust(64, "0")


def address_from_word(word: str) -> str:
    return "0x" + word[-40:].lower()


def block_timestamp_ms(timestamp_hex: str) -> int:
    return int(timestamp_hex, 16) * 1000


def sqrt_price_to_price(
    sqrt_price_x96: int, base_decimals: int, quote_decimals: int, base_is_token0: bool
) -> Decimal:
    """Human price of one whole base token in quote units.

    ``slot0.sqrtPriceX96`` encodes ``sqrt(token1_raw / token0_raw) * 2**96``.
    Decimals and token ordering are applied here so callers never compare a raw
    ratio against a human price. When the base token is ``token1`` the ratio is
    inverted *before* the decimal adjustment, which is not the same as
    inverting the already-adjusted price.
    """
    if sqrt_price_x96 <= 0:
        raise ValueError("non-positive sqrt price")
    with localcontext() as ctx:
        ctx.prec = 50
        ratio = (Decimal(sqrt_price_x96) / (Decimal(2) ** 96)) ** 2
        decimals = Decimal(10) ** (base_decimals - quote_decimals)
        return ratio * decimals if base_is_token0 else decimals / ratio


def decode_transfers(logs: list[dict]) -> list[dict]:
    """Normalise ERC-20 Transfer logs. Only well-formed 3-topic logs are kept."""
    rows = []
    for log in logs:
        topics = log.get("topics") or []
        if len(topics) < 3 or topics[0].lower() != TRANSFER_TOPIC:
            continue
        data = log.get("data") or "0x0"
        try:
            value = int(data, 16)
        except ValueError:
            continue
        rows.append({
            "token": (log.get("address") or "").lower(),
            "from": address_from_word(topics[1]),
            "to": address_from_word(topics[2]),
            "raw_value": value,
            "block_number": int(log.get("blockNumber", "0x0"), 16),
        })
    return rows


def counterparty_flows(receipt: dict, token_address: str,
                       token_decimals: int) -> list[dict]:
    """Per-address net flow of one token and of the stablecoin legs.

    Aggregator routes are multi-hop, so the buyer/seller is not necessarily the
    transaction sender: xStocks buys are minted through an issuer contract and
    routed through a stable pool. Netting each address independently recovers
    the real counterparty that exchanged token for stablecoin, whatever hops
    the route used. Addresses that moved token but no stable (mints, burns,
    token-for-token rotation) simply produce no price.
    """
    token = token_address.lower()
    flows: dict[str, dict] = {}
    symbols: dict[str, set] = {}
    for row in decode_transfers(receipt.get("logs") or []):
        if row["from"] == row["to"]:
            continue
        for address, sign in ((row["from"], -1), (row["to"], 1)):
            entry = flows.setdefault(address, {"token": Decimal(0), "stable": Decimal(0)})
            if row["token"] == token:
                entry["token"] += sign * Decimal(row["raw_value"]) / (Decimal(10) ** token_decimals)
                continue
            stable = STABLE_TOKENS.get(row["token"])
            if stable is None:
                continue
            symbol, decimals = stable
            entry["stable"] += sign * Decimal(row["raw_value"]) / (Decimal(10) ** decimals)
            symbols.setdefault(address, set()).add(symbol)
    prints = []
    for address, entry in flows.items():
        token_delta, stable_delta = entry["token"], entry["stable"]
        if address in BURN_ADDRESSES or token_delta == 0 or stable_delta == 0:
            continue
        # One side pays quote and receives base, or the exact reverse.
        if (token_delta > 0) == (stable_delta > 0):
            continue
        quantity = abs(token_delta)
        if quantity <= 0:
            continue
        prints.append({
            "counterparty": address,
            "side": "buy" if token_delta > 0 else "sell",
            "token_quantity": quantity,
            "quote_amount_usd": abs(stable_delta),
            "price_usd": abs(stable_delta) / quantity,
            "quote_symbols": sorted(symbols.get(address, ())),
            "tx_hash": receipt.get("transactionHash"),
            "block_number": int(receipt.get("blockNumber", "0x0"), 16),
        })
    # Largest quote leg first: the primary counterparty of the route, not a
    # dust hop or a fee collector.
    return sorted(prints, key=lambda p: p["quote_amount_usd"], reverse=True)


def trade_print_from_receipt(receipt: dict, token_address: str,
                             token_decimals: int) -> dict | None:
    """One print per transaction, or ``None`` when it is not a token swap.

    The transaction sender is preferred when it has a clean leg: that is the
    price a real user paid or received, including any aggregator or pool fee
    deducted on the way. Otherwise the largest counter-leg (the venue) is used,
    which is how market-maker rotations and routed mint/redeem trades are read.
    """
    prints = counterparty_flows(receipt, token_address, token_decimals)
    if not prints:
        return None
    sender = (receipt.get("from") or "").lower()
    for print_row in prints:
        if print_row["counterparty"] == sender:
            return print_row
    return prints[0]


def vwap(prints: list[dict]) -> dict | None:
    """Volume-weighted on-chain print over the decoded window."""
    if not prints:
        return None
    volume = sum((p["quote_amount_usd"] for p in prints), Decimal(0))
    quantity = sum((p["token_quantity"] for p in prints), Decimal(0))
    if quantity <= 0 or volume <= 0:
        return None
    buys = [p for p in prints if p["side"] == "buy"]
    sells = [p for p in prints if p["side"] == "sell"]
    result = {
        "vwap_usd": volume / quantity,
        "volume_usd": volume,
        "token_volume": quantity,
        "print_count": len(prints),
        "buy_count": len(buys),
        "sell_count": len(sells),
        "quote_symbols": sorted({s for p in prints for s in p.get("quote_symbols", ())}),
    }
    if sells:
        # A "sell" print is a user selling the token into the on-chain venue:
        # the best of those is that venue's bid.
        result["onchain_bid_usd"] = max(p["price_usd"] for p in sells)
        result["onchain_bid_size"] = max(
            (p["token_quantity"] for p in sells
             if p["price_usd"] == result["onchain_bid_usd"]), default=Decimal(0))
    if buys:
        # A "buy" print is a user paying stable for the token: the best of
        # those is the venue's ask. Prints, not resting orders, so the pair is
        # only quotable when both sides were observed.
        result["onchain_ask_usd"] = min(p["price_usd"] for p in buys)
        result["onchain_ask_size"] = max(
            (p["token_quantity"] for p in buys
             if p["price_usd"] == result["onchain_ask_usd"]), default=Decimal(0))
    if "onchain_bid_usd" in result and "onchain_ask_usd" in result:
        result["onchain_spread_bps"] = (
            (result["onchain_ask_usd"] - result["onchain_bid_usd"])
            / result["onchain_ask_usd"] * 10000)
    return result


def best_pool_reference(pools: list[dict], min_depth_usd: Decimal) -> dict | None:
    """Deepest pool above the depth floor. Dust pools are never a price."""
    usable = [
        p for p in pools
        if p.get("depth_usd") is not None
        and p["depth_usd"] >= min_depth_usd
        and p.get("price_usd") is not None
        and p.get("liquidity", 0) > 0
    ]
    if not usable:
        return None
    return max(usable, key=lambda p: p["depth_usd"])


class XLayerRPC:
    """Minimal JSON-RPC client. Read-only methods only."""

    def __init__(self, url: str = DEFAULT_RPC_URL, proxy: str | None = None,
                 timeout: float = 20.0, retries: int = 3):
        self.url = url
        self.timeout = timeout
        self.retries = retries
        handlers = [urllib.request.ProxyHandler(
            {"http": proxy, "https": proxy})] if proxy else []
        self.opener = urllib.request.build_opener(*handlers)

    def _post(self, payload: dict | list) -> object:
        body = json.dumps(payload).encode()
        last: Exception | None = None
        for attempt in range(self.retries):
            request = urllib.request.Request(
                self.url, data=body, headers={"content-type": "application/json"})
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode())
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last = exc
                time.sleep(0.4 * (attempt + 1))
        raise RuntimeError(f"xlayer_rpc_failed:{type(last).__name__}:{str(last)[:120]}")

    def single(self, method: str, params: list) -> object:
        out = self._post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if not isinstance(out, dict) or "result" not in out:
            raise RuntimeError(f"xlayer_rpc_error:{json.dumps(out)[:160]}")
        return out["result"]

    def batch(self, calls: list[tuple[str, list]]) -> list:
        """Ordered results. Falls back to single calls on any batch rejection."""
        results: list = []
        for start in range(0, len(calls), MAX_BATCH_CALLS):
            chunk = calls[start:start + MAX_BATCH_CALLS]
            payload = [{"jsonrpc": "2.0", "id": index, "method": method, "params": params}
                       for index, (method, params) in enumerate(chunk)]
            try:
                out = self._post(payload)
            except RuntimeError:
                out = None
            if isinstance(out, list) and len(out) == len(chunk):
                by_id = {item.get("id"): item.get("result") for item in out}
                results.extend(by_id.get(index) for index in range(len(chunk)))
            else:
                results.extend(self.single(method, params) for method, params in chunk)
        return results

    def block_number(self) -> int:
        return int(self.single("eth_blockNumber", []), 16)

    def block(self, number: int) -> dict:
        return self.single("eth_getBlockByNumber", [hex(number), False])

    def call(self, to: str, data: str, block: str = "latest") -> str:
        return self.single("eth_call", [{"to": to, "data": data}, block])

    def receipt(self, tx_hash: str) -> dict:
        return self.single("eth_getTransactionReceipt", [tx_hash])

    def transfer_logs(self, token: str, from_block: int, to_block: int) -> list[dict]:
        logs: list[dict] = []
        start = max(from_block, 0)
        while start <= to_block:
            end = min(start + MAX_LOG_BLOCK_RANGE - 1, to_block)
            chunk = self.single("eth_getLogs", [{
                "address": token, "topics": [TRANSFER_TOPIC],
                "fromBlock": hex(start), "toBlock": hex(end),
            }])
            logs.extend(chunk or [])
            start = end + 1
        return logs


class XLayerOnchainAdapter:
    """Configured pools and decoded trade prints for xStocks tokens."""

    def __init__(self, settings: dict, tokens: list[dict], rpc: XLayerRPC):
        self.settings = settings
        self.tokens = [t for t in tokens if t.get("onchain_scan", True)]
        self.rpc = rpc
        self.min_depth_usd = Decimal(str(settings.get("min_pool_depth_usd", "1000")))
        # Dust legs (a 1-wei stable transfer, a fee refund) would otherwise set
        # the best on-chain ask to ~0 and poison every screening spread.
        self.min_notional_usd = Decimal(str(settings.get("min_trade_notional_usd", "10")))

    def pool_states(self) -> list[dict]:
        """Read every configured pool. Empty pools stay in the output, marked."""
        wanted = [(token, pool) for token in self.tokens for pool in token.get("pools", [])]
        if not wanted:
            return []
        calls: list[tuple[str, list]] = []
        for _, pool in wanted:
            address = pool["address"].lower()
            base = pool["base_address"].lower()
            quote = pool["quote_address"].lower()
            balance = "0x70a08231" + pad(pool["address"])
            calls += [
                ("eth_call", [{"to": address, "data": SELECTORS["token0"]}, "latest"]),
                ("eth_call", [{"to": address, "data": SELECTORS["token1"]}, "latest"]),
                ("eth_call", [{"to": address, "data": SELECTORS["liquidity"]}, "latest"]),
                ("eth_call", [{"to": address, "data": SELECTORS["slot0"]}, "latest"]),
                ("eth_call", [{"to": base, "data": balance}, "latest"]),
                ("eth_call", [{"to": quote, "data": balance}, "latest"]),
            ]
        results = self.rpc.batch(calls)
        header = self.rpc.block(self.rpc.block_number())
        block_number = int(header["number"], 16)
        block_ts_ms = block_timestamp_ms(header["timestamp"])
        rows = []
        for index, (token, pool) in enumerate(wanted):
            slice_ = results[index * 6:index * 6 + 6]
            if not all(isinstance(item, str) and item.startswith("0x") and len(item) >= 66
                       for item in slice_[:4]):
                # A dropped/empty RPC reply must never be decoded as price 0.
                rows.append({**pool, "asset": token["asset"], "error": "incomplete_rpc_reply",
                             "block_number": block_number, "block_ts_ms": block_ts_ms,
                             "recv_ts_ms": now_ms()})
                continue
            token0 = address_from_word(slice_[0])
            token1 = address_from_word(slice_[1])
            liquidity = int(slice_[2], 16)
            slot0 = slice_[3][2:]
            sqrt_price = int(slot0[0:64], 16)
            tick = int(slot0[64:128], 16)
            if tick >= 2 ** 255:
                tick -= 2 ** 256
            base_address = pool["base_address"].lower()
            quote_address = pool["quote_address"].lower()
            if {token0, token1} != {base_address, quote_address}:
                rows.append({**pool, "asset": token["asset"], "error": "pool_token_mismatch",
                             "block_number": block_number, "block_ts_ms": block_ts_ms})
                continue
            base_decimals = int(pool["base_decimals"])
            quote_decimals = int(pool["quote_decimals"])
            try:
                price = sqrt_price_to_price(sqrt_price, base_decimals, quote_decimals,
                                            base_address == token0)
            except ValueError:
                # A pool that was never initialized (or a contract that only
                # looks like a V3 pool) must not abort the other reads.
                rows.append({**pool, "asset": token["asset"], "token0": token0,
                             "token1": token1, "sqrt_price_x96": sqrt_price, "tick": tick,
                             "liquidity": liquidity, "price_usd": None, "depth_usd": None,
                             "error": "pool_not_initialized",
                             "block_number": block_number, "block_ts_ms": block_ts_ms,
                             "recv_ts_ms": now_ms()})
                continue
            if not all(isinstance(item, str) and item.startswith("0x") and len(item) >= 66
                       for item in slice_[4:6]):
                rows.append({**pool, "asset": token["asset"], "error": "incomplete_rpc_reply",
                             "block_number": block_number, "block_ts_ms": block_ts_ms,
                             "recv_ts_ms": now_ms()})
                continue
            base_amount = Decimal(int(slice_[4], 16)) / (Decimal(10) ** base_decimals)
            quote_amount = Decimal(int(slice_[5], 16)) / (Decimal(10) ** quote_decimals)
            rows.append({
                **pool, "asset": token["asset"], "token0": token0, "token1": token1,
                "sqrt_price_x96": sqrt_price, "tick": tick, "liquidity": liquidity,
                "price_usd": price, "base_amount": base_amount, "quote_amount": quote_amount,
                "depth_usd": quote_amount + base_amount * price,
                "block_number": block_number, "block_ts_ms": block_ts_ms, "recv_ts_ms": now_ms(),
            })
        return rows

    def trade_prints(self, lookback_blocks: int, max_prints: int) -> dict[str, dict]:
        """Decode recent trades per asset from real transaction receipts."""
        head = self.rpc.block_number()
        from_block = max(0, head - lookback_blocks + 1)
        head_ts = block_timestamp_ms(self.rpc.block(head)["timestamp"])
        # Age is counted in blocks and converted with a block time measured on
        # this chain, not guessed: the RPC limits eth_getLogs to 100 blocks.
        reference = self.rpc.block(max(0, head - 100))
        span = max(1, head - int(reference["number"], 16))
        block_time_ms = max(1, (head_ts - block_timestamp_ms(reference["timestamp"])) // span)
        out: dict[str, dict] = {}
        for token in self.tokens:
            if not token.get("trade_scan", True):
                continue
            try:
                logs = self.rpc.transfer_logs(token["address"], from_block, head)
            except RuntimeError as exc:
                out[token["asset"]] = {"error": str(exc)[:160], "prints": [],
                                       "lookback_blocks": lookback_blocks, "head_block": head,
                                       "block_ts_ms": head_ts, "recv_ts_ms": now_ms(),
                                       "block_time_ms": block_time_ms}
                continue
            hashes: list[str] = []
            for log in reversed(logs):
                tx_hash = log.get("transactionHash")
                if tx_hash and tx_hash not in hashes:
                    hashes.append(tx_hash)
                if len(hashes) >= max_prints:
                    break
            prints = []
            skipped_dust = 0
            if hashes:
                receipts = self.rpc.batch(
                    [("eth_getTransactionReceipt", [tx_hash]) for tx_hash in hashes])
                for receipt in receipts:
                    if not receipt:
                        continue
                    raw = trade_print_from_receipt(
                        receipt, token["address"], int(token["decimals"]))
                    if raw is None:
                        continue
                    if raw["quote_amount_usd"] < self.min_notional_usd:
                        skipped_dust += 1
                        continue
                    age_blocks = max(0, head - raw["block_number"])
                    raw["age_blocks"] = age_blocks
                    raw["age_ms_estimate"] = age_blocks * block_time_ms
                    prints.append(raw)
            summary = {
                "asset": token["asset"], "symbol": token["symbol"],
                "lookback_blocks": lookback_blocks, "head_block": head,
                "block_ts_ms": head_ts, "recv_ts_ms": now_ms(),
                "block_time_ms": block_time_ms,
                "prints": prints, "transfers_seen": len(logs),
                "min_notional_usd": self.min_notional_usd,
                "dust_prints_skipped": skipped_dust,
            }
            aggregate = vwap(prints)
            if aggregate is not None:
                summary.update(aggregate)
                summary["last_print"] = prints[0]
            out[token["asset"]] = summary
        return out
