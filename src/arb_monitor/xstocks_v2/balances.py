"""Account balances for the v2 board: chain / account, asset, amount.

Two rules, both from the lessons file:

* **An unreadable balance is not a zero.** A failed read is written as
  ``amount: null`` plus the error text. The board prints 「读不到」; it must never
  print 0.00 for a wallet whose answer nobody received (lesson A6).
* **The board only renders what a process wrote.** Collection happens in the
  scan/paper round, next to the other reads, and lands in one file; the HTTP
  handler never calls a venue itself.

The units follow the chain: Solana xStocks are Token-2022 with a scaled UI, so
the raw amount and the UI amount are both kept -- ``raw = UI x 1e8 / multiplier``
(lesson D15), and the deposit leg needs the raw number.

Two sources, on purpose (2026-10-09, operator: 「没有 eth 和其他链的展示」):

* the **chain itself** (``eth_getBalance`` / ``balanceOf``) for the two chains we
  trade hardest -- an address list is precise, but it can only answer about the
  addresses somebody already wrote down;
* the **wallet service** (``onchainos wallet balance --chain X``) for every other
  chain -- it *discovers* what we hold, which is the only way the SPCXx that
  arrived on BSC (2026-10-09) and the MARSCAT on X Layer ever show up.

Where both answer, the chain read stays the amount (that is what an execution
would spend) and the wallet service contributes the tokens it found plus its own
USD valuation. A disagreement is printed on the row, never averaged away.
"""
from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BOOK = ROOT / "data" / "xstocks_book.sqlite"

XLAYER_RPC = "https://rpc.xlayer.tech"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
EVM_WALLET = "0xabababababababababababababababababababab"
SOLANA_WALLET = "AmkjszArkzSKfSUqqgFJLGe6ytjhJxjHFaaVrCxAf2ev"

USDG_XLAYER = "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8"
USDC_XLAYER = "0xb6ceceab302e2e4948951ee7843fc24e92933061"
#: The X Layer legs the strategy actually uses, in board order.
XLAYER_ASSETS = (("USDG", USDG_XLAYER, 6), ("USDC", USDC_XLAYER, 6))
SOLANA_QUOTES = {
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}
KRAKEN_ASSETS = ("ZUSD", "USDG", "USDC", "USDT")
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

#: Every chain the board shows, in board order: the two we trade hardest first,
#: then the chains money can land on. ``cli`` is the wallet service's chain name;
#: ``chain`` is our own key, so the on-chain readers merge into these rows.
CLI_CHAINS: tuple[tuple[str, str, str, str], ...] = (
    ("okb", "x-layer", "X Layer", EVM_WALLET),
    ("sol", "solana", "Solana", SOLANA_WALLET),
    ("eth", "eth", "Ethereum", EVM_WALLET),
    ("bnb", "bsc", "BNB Chain", EVM_WALLET),
    ("arb_eth", "arbitrum", "Arbitrum", EVM_WALLET),
    ("op_eth", "optimism", "Optimism", EVM_WALLET),
    ("base_eth", "base", "Base", EVM_WALLET),
    ("ink", "ink", "Ink", EVM_WALLET),
    ("evm_hype", "hyperevm", "HyperEVM", EVM_WALLET),
    ("mnt", "mantle", "Mantle", EVM_WALLET),
    ("mon", "monad", "Monad", EVM_WALLET),
)
#: A dashboard read must not stall the scan round. One route, one timeout per
#: chain, and one thread per chain so no chain waits behind another: the worst
#: case is a single timeout window (~15 s), not 11 of them. Measured 2026-10-09:
#: a healthy call is 0.08-0.15 s, and 8 s was already tight enough to time out
#: Arbitrum once while a live cycle was running (``local_http_proxy:timeout``).
CLI_READ_TIMEOUT_S = 15
CLI_WORKERS = 11
#: Amounts this close are the same number written twice: the service prints 18
#: significant digits while Solana's RPC rounds its UI string to 8 decimals
#: (QQQx: 0.00571966 vs 0.005719669528929697). 0.01 % is far below any real
#: difference -- a stale indexer is off by percent, not by a rounding step.
SAME_AMOUNT_TOLERANCE = Decimal("1e-4")
#: Below this, a difference is a rounding artifact of a wei nobody can spend.
DUST = Decimal("1e-9")


def row(asset: str, amount=None, *, raw=None, error: str = "",
        note: str = "", address: str = "", usd=None) -> dict:
    """One line of the board. ``amount is None`` means 「读不到」, not zero."""
    return {
        "asset": asset,
        "amount": None if amount is None else format(Decimal(str(amount)), "f"),
        "raw": None if raw is None else str(raw),
        "readable": amount is not None,
        "error": error,
        "note": note,
        #: Token address (``""`` = the chain's own gas token). Lets two sources
        #: recognise the same asset, and lets the page show the exact contract.
        "address": str(address or "").lower(),
        #: The wallet service's own USD valuation. Displayed with a label; never
        #: used as a price in the strategy (that is ``gas_prices`` / the pools).
        "usd": None if usd is None else str(usd),
    }


def group(chain: str, account: str, rows: list[dict], *, label: str = "",
          note: str = "", usd_total=None, source: str = "") -> dict:
    return {"chain": chain, "account": account, "label": label or chain,
            "rows": rows, "note": note,
            "usd_total": None if usd_total is None else str(usd_total),
            "source": source}


def _dec(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


def _row_key(item: dict) -> str:
    """Identity of an asset inside one chain: its address, else its symbol."""
    address = str(item.get("address") or "").lower()
    return address or str(item.get("asset") or "").lower()


def _same_amount(left, right) -> bool:
    """Is ``left`` the same money as ``right``, written to another width?"""
    first, second = _dec(left), _dec(right)
    if first is None or second is None:
        return False
    slack = max(abs(second), abs(first)) * SAME_AMOUNT_TOLERANCE
    return abs(first - second) <= slack


def _worth_flagging(left, right) -> bool:
    """Two readings of *money* disagree. Dust (1 wei residues) never counts:
    the two sources round it differently and neither number is spendable."""
    first, second = _dec(left), _dec(right)
    if first is None or second is None:
        return False
    if min(abs(first), abs(second)) < DUST:
        return False
    return not _same_amount(first, second)


def solana_symbols(book: str | Path = DEFAULT_BOOK) -> dict[str, str]:
    """``mint -> symbol`` for every Solana deployment the issuer lists."""
    try:
        con = sqlite3.connect(str(book))
        try:
            rows = con.execute(
                "select issuer_symbol, address from deployments "
                "where lower(network) = 'solana'").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    return {str(address): str(symbol) for symbol, address in rows}


def xlayer_forms(book: str | Path = DEFAULT_BOOK) -> tuple:
    """``(symbol, native, wrapper)`` for xStocks deployed on X Layer.

    Both addresses come from the issuer's own deployment table
    (``data/xstocks_book.sqlite``), never from a guess; ``wrapper`` is the
    ``wrapper_v2`` row. A missing form becomes a 「读不到」 row, not a skip.
    """
    try:
        con = sqlite3.connect(str(book))
    except sqlite3.Error:
        return ()
    try:
        rows = con.execute(
            "select issuer_symbol, form, address from deployments "
            "where lower(network) = 'xlayer'").fetchall()
    except sqlite3.Error:
        return ()
    finally:
        con.close()
    forms: dict[str, dict] = {}
    for symbol, form, address in rows:
        slot = "wrapper" if str(form).startswith("wrapper") else "native"
        forms.setdefault(str(symbol), {})[slot] = str(address)
    return tuple((symbol, item.get("native"), item.get("wrapper"))
                 for symbol, item in sorted(forms.items()))


def watched_forms(book: str | Path = DEFAULT_BOOK,
                  path: str | Path | None = None) -> tuple:
    """The X Layer forms worth asking the chain about: the census symbols.

    ``xlayer_forms`` lists every deployment (1271 rows on 2026-10-07). Asking
    about all of them is 2 500 calls and hung the first version of this module;
    the pool census is the list of symbols we could actually trade.
    """
    wanted = census_symbols(path=path)
    forms = xlayer_forms(book)
    if not wanted:
        return forms
    return tuple(entry for entry in forms if entry[0] in wanted)


def census_symbols(chain: str = "x-layer",
                   path: str | Path | None = None) -> set[str]:
    """Symbols the pool census actually found a deep pool for on one chain.

    Used to keep the balance refresh small: the deployment table lists every
    xStock, and asking the chain about all of them every round is RPC we do not
    need (the scanner's own budget is shared).
    """
    import json
    path = Path(path) if path else (ROOT / "data" / "xstocks_chain_census.json")
    try:
        census = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return set()
    wanted = "x-layer" if chain == "x-layer" else chain
    return {str(entry.get("symbol") or "")
            for entry in (census.get("candidates") or [])
            if str(entry.get("slug")) == wanted and entry.get("symbol")}


#: X Layer answers a JSON-RPC batch of at most 10 (policy ``jsonrpc_batch_max``).
BATCH_MAX = 10


def collect_evm(http, *, rpc_url: str = XLAYER_RPC, owner: str = EVM_WALLET,
                assets=XLAYER_ASSETS, wrappers=()) -> dict:
    """X Layer: native gas token, the stables, and the xStock forms we hold.

    One batched round trip per ten calls instead of one per call: the scanner is
    sampling through the same proxy, and 24 sequential round trips (measured
    2026-10-07) took longer than the whole scan round.
    """
    # (name, token address, call, decimals). ``call is None`` = the address is
    # unknown, which becomes 「读不到」 and never a 0.
    calls: list[tuple[str, str, dict | None, int]] = [(
        "OKB", "", {"method": "eth_getBalance", "params": [owner, "latest"]}, 18)]
    balance_of = "0x70a08231" + "0" * 24 + owner[2:].lower()
    for symbol, token, decimals in assets:
        calls.append((symbol, str(token), {"method": "eth_call", "params": [
            {"to": token, "data": balance_of}, "latest"]}, decimals))
    for symbol, native, wrapper in wrappers:
        if native:
            calls.append((f"{symbol} 原生", str(native),
                          {"method": "eth_call", "params": [
                {"to": native, "data": balance_of}, "latest"]}, 18))
        else:
            calls.append((f"{symbol} 原生", "", None, 18))
        if wrapper:
            calls.append((f"w{symbol}", str(wrapper),
                          {"method": "eth_call", "params": [
                {"to": wrapper, "data": balance_of}, "latest"]}, 18))
        else:
            calls.append((f"w{symbol}", "", None, 18))
    rows: list[dict] = []
    for start in range(0, len(calls), BATCH_MAX):
        chunk = calls[start:start + BATCH_MAX]
        live = [item for item in chunk if item[2] is not None]
        payload = [dict(call, jsonrpc="2.0", id=index + 1)
                   for index, (_k, _a, call, _d) in enumerate(live)]
        answers: dict[int, object] = {}
        failure = ""
        if payload:
            try:
                _status, response = http.post_json(rpc_url, payload)
                for item in response or []:
                    if isinstance(item, dict):
                        answers[item.get("id")] = item
            except Exception as exc:  # noqa: BLE001
                failure = f"{type(exc).__name__}: {exc}"[:120]
        cursor = 0
        for name, address, call, decimals in chunk:
            if call is None:
                rows.append(row(name, None, error="address unknown",
                                address=address))
                continue
            cursor += 1
            if failure:
                rows.append(row(name, None, error=failure, address=address))
                continue
            answer = answers.get(cursor) or {}
            if answer.get("error") is not None:
                rows.append(row(name, None,
                                error=str(answer["error"])[:120],
                                address=address))
                continue
            raw = answer.get("result")
            if not raw or raw == "0x":
                rows.append(row(name, None,
                                error="empty eth_call result" if call["method"] == "eth_call"
                                else "empty rpc result", address=address))
                continue
            rows.append(row(name, Decimal(int(raw, 16)) / Decimal(10 ** decimals),
                            raw=int(raw, 16),
                            note="原生 gas" if name == "OKB" else "",
                            address=address))
    return group("x-layer", owner, rows, label="X Layer")


def collect_solana(http, *, owner: str = SOLANA_WALLET,
                   symbols: dict[str, str] | None = None) -> dict:
    """Solana: SOL, the two stables, and every xStock mint we hold."""
    symbols = symbols if symbols is not None else solana_symbols()
    rows: list[dict] = []
    try:
        payload = _solana_rpc(http, "getBalance", [owner])
        lamports = ((payload.get("result") or {}).get("value"))
        rows.append(row("SOL", Decimal(lamports) / Decimal(10 ** 9),
                        raw=lamports, note="桥费/网络费"))
    except Exception as exc:  # noqa: BLE001
        rows.append(row("SOL", None, error=f"{type(exc).__name__}: {exc}"[:120]))
    for name, mint in SOLANA_QUOTES.items():
        rows.append(_solana_mint(http, owner, name, mint))
    held = _solana_held_mints(http, owner)
    if held is None:
        rows.append(row("xStock(raw)", None, error="getTokenAccountsByOwner failed"))
    else:
        for mint, info in sorted(held.items()):
            if mint in SOLANA_QUOTES.values():
                continue
            name = symbols.get(mint, mint[:8] + "…")
            rows.append(row(name, info.get("ui"), raw=info.get("raw"),
                            note="Token-2022 raw", address=mint))
    return group("solana", owner, rows, label="Solana")


def _solana_rpc(http, method: str, params: list) -> dict:
    _status, payload = http.post_json(
        SOLANA_RPC, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    return payload if isinstance(payload, dict) else {}


def _solana_mint(http, owner: str, name: str, mint: str) -> dict:
    try:
        payload = _solana_rpc(http, "getTokenAccountsByOwner",
                              [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
        rows = (payload.get("result") or {}).get("value") or []
        total_raw = 0
        for entry in rows:
            info = (((entry.get("account") or {}).get("data") or {})
                    .get("parsed", {})).get("info", {})
            total_raw += int((info.get("tokenAmount") or {}).get("amount") or 0)
        # Solana wraps the answer: {"result": {"value": [...]}}. Reading
        # ``value`` off the envelope silently reports a funded wallet as empty.
        if not rows and "result" not in payload:
            return row(name, None, error="unwrapped rpc answer", address=mint)
        return row(name, Decimal(total_raw) / Decimal(10 ** 6), raw=total_raw,
                   address=mint)
    except Exception as exc:  # noqa: BLE001
        return row(name, None, error=f"{type(exc).__name__}: {exc}"[:120],
                   address=mint)


def _solana_held_mints(http, owner: str) -> dict | None:
    out: dict[str, dict] = {}
    for program in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        try:
            payload = _solana_rpc(http, "getTokenAccountsByOwner",
                                  [owner, {"programId": program},
                                   {"encoding": "jsonParsed"}])
        except Exception:  # noqa: BLE001
            return None
        rows = (payload.get("result") or {}).get("value")
        if rows is None:
            return None
        for entry in rows:
            info = (((entry.get("account") or {}).get("data") or {})
                    .get("parsed", {})).get("info", {})
            amount = info.get("tokenAmount") or {}
            raw = int(amount.get("amount") or 0)
            if raw == 0:
                continue
            out[info.get("mint", "?")] = {
                "raw": raw, "ui": amount.get("uiAmountString")}
    return out


def collect_kraken(client, *, assets=KRAKEN_ASSETS) -> dict:
    """Kraken: the cash assets plus every tokenized ``*.T`` balance we hold."""
    try:
        balance = client.balance()
    except Exception as exc:  # noqa: BLE001
        return group("kraken", "api", [row(a, None,
                                           error=f"{type(exc).__name__}: {exc}"[:120])
                                       for a in assets], label="Kraken")
    rows = [row(asset, balance.get(asset, Decimal("0"))) for asset in assets]
    for asset, amount in sorted(balance.items()):
        if asset.endswith(".T") and amount:
            rows.append(row(asset, amount, note="tokenized xStock"))
    return group("kraken", "api", rows, label="Kraken")


def _default_wallet_runner():
    """The wallet service, on the one route this host actually has.

    Built here (not at import) so the module stays importable without the CLI,
    and pinned to the reviewed local proxy: a *read* has no state to protect, but
    three routes x 11 chains x timeout would outlive the scan round that calls it.
    """
    from arb_monitor.execution.network_policy import DEFAULT_ROUTES
    from arb_monitor.execution.wallet_cli import OnchainOSRunner
    return OnchainOSRunner(read_timeout=CLI_READ_TIMEOUT_S,
                           routes=tuple(DEFAULT_ROUTES[:1]))


def collect_chain(runner, cli_slug: str, chain: str, label: str,
                  account: str) -> dict:
    """One chain from the wallet service. Never raises; a failure is 「读不到」."""
    from arb_monitor.execution.wallet_cli import wallet_balances
    try:
        assets = wallet_balances(runner, cli_slug)
    except Exception as exc:  # noqa: BLE001
        return group(chain, account, [row(
            "(整链)", None, note="钱包服务读不到这条链",
            error=f"{type(exc).__name__}: {exc}"[:120])],
            label=label, note="读不到 ≠ 0", source="indexer")
    rows: list[dict] = []
    total = Decimal("0")
    priced = True
    for asset in assets:
        symbol = str(asset.get("symbol") or asset.get("token_address") or "?")
        usd = asset.get("usd_value")
        value = _dec(usd)
        if value is None:
            priced = False
        else:
            total += value
        rows.append(row(symbol, asset.get("balance"),
                        raw=asset.get("raw_balance"),
                        address=asset.get("token_address") or "",
                        note="indexer", usd=usd))
    if not rows:
        # The service answered and found nothing. That is a claim of zero from a
        # named source, so it is printed as such -- not as a blank card.
        return group(chain, account, rows, label=label,
                     note="indexer：这条链没有余额（服务返回空）",
                     usd_total=Decimal("0"), source="indexer")
    return group(chain, account, rows, label=label, source="indexer",
                 usd_total=total if priced else None)


def collect_chains(runner, chains=CLI_CHAINS, *,
                   workers: int = CLI_WORKERS) -> list[dict]:
    """One group per chain, in ``chains`` order. A slow chain never blocks the
    others: the pool is capped and each call carries its own timeout."""
    jobs = [(cli_slug, chain, label, account)
            for cli_slug, chain, label, account in chains]
    if not jobs:
        return []
    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs)))) as pool:
        futures = [pool.submit(collect_chain, runner, *job) for job in jobs]
        for job, future in zip(jobs, futures):
            try:
                out.append(future.result())
            except Exception as exc:  # noqa: BLE001
                out.append(group(job[1], job[3], [row(
                    "(整链)", None, note="钱包服务读不到这条链",
                    error=f"{type(exc).__name__}: {exc}"[:120])],
                    label=job[2], note="读不到 ≠ 0", source="indexer"))
    return out


def merge_group(base: dict, extra: dict) -> dict:
    """Add the wallet service's rows to a chain we already read on-chain.

    The chain read keeps the amount (it is what an execution would spend). The
    service adds the assets the fixed address list never asked about, its USD
    valuation, and a visible note when the two numbers disagree.
    """
    rows = list(base.get("rows") or [])
    by_key = {_row_key(item): item for item in rows}
    for item in extra.get("rows") or []:
        key = _row_key(item)
        mine = by_key.get(key)
        if mine is None:
            fresh = dict(item)
            fresh["note"] = "indexer" if not fresh.get("note") else fresh["note"]
            rows.append(fresh)
            by_key[key] = fresh
            continue
        if mine.get("usd") is None:
            mine["usd"] = item.get("usd")
        other, mine_amount = item.get("amount"), mine.get("amount")
        if _worth_flagging(mine_amount, other):
            flag = f"⚠ indexer 读 {other}"
            note = str(mine.get("note") or "")
            mine["note"] = f"{note}；{flag}" if note else flag
    out = dict(base)
    out["rows"] = rows
    if not out.get("note"):
        out["note"] = extra.get("note") or ""
    if out.get("usd_total") is None:
        out["usd_total"] = extra.get("usd_total")
    if not out.get("source"):
        out["source"] = extra.get("source") or ""
    return out


def collect(*, http=None, kraken_client=None, book: str | Path = DEFAULT_BOOK,
            wrappers=(), now_ms: int | None = None, wallet_runner=None,
            cli_chains=CLI_CHAINS, use_indexer: bool = True) -> dict:
    """One balances snapshot. Any failed read stays a 「读不到」 row."""
    http = http or _default_http()
    on_chain = {entry["chain"]: entry for entry in (
        collect_evm(http, wrappers=wrappers or watched_forms(book)),
        collect_solana(http, symbols=solana_symbols(book)))}
    if use_indexer:
        runner = wallet_runner or _default_wallet_runner()
        for entry in collect_chains(runner, cli_chains):
            chain = entry["chain"]
            on_chain[chain] = (merge_group(on_chain[chain], entry)
                               if chain in on_chain else entry)
    groups: list[dict] = []
    for _slug, chain, _label, _account in cli_chains:
        if chain in on_chain:
            groups.append(on_chain.pop(chain))
    groups.extend(sorted(on_chain.values(), key=lambda item: str(item["chain"])))
    if kraken_client is not None:
        groups.append(collect_kraken(kraken_client))
    return {
        "as_of_ms": now_ms or int(time.time() * 1000),
        "scope": {entry["chain"]: entry["account"] for entry in groups},
        "groups": groups,
        "note": "读不到 ≠ 0：amount 为 null 的行是读取失败。",
    }


def refresh(*, data_dir: str | Path | None = None, http=None,
            kraken_client=None, book: str | Path = DEFAULT_BOOK,
            wallet_runner=None, cli_chains=CLI_CHAINS,
            use_indexer: bool = True) -> dict:
    """Collect and write the snapshot the board renders. **Never raises.**

    The resident scan/paper round calls this: the board stays read-only, and a
    venue that will not answer produces a 「读不到」 row instead of stopping the
    round or looking like an empty wallet.
    """
    data_dir = Path(data_dir) if data_dir else (ROOT / "data-v2")
    out = data_dir / "xstocks_v2_balances.json"
    if kraken_client is None:
        try:
            from arb_monitor.venues.kraken_spot import (KrakenSpotClient,
                                                        load_credentials)
            key, secret = load_credentials(ROOT / "config" / "kraken.env")
            kraken_client = KrakenSpotClient(key, secret,
                                             proxy="http://127.0.0.1:4780")
        except Exception:  # noqa: BLE001
            kraken_client = None
    try:
        snapshot = collect(http=http, kraken_client=kraken_client, book=book,
                           wrappers=watched_forms(book),
                           wallet_runner=wallet_runner, cli_chains=cli_chains,
                           use_indexer=use_indexer)
    except Exception as exc:  # noqa: BLE001
        snapshot = {"as_of_ms": int(time.time() * 1000), "groups": [],
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                    "note": "读不到 ≠ 0：amount 为 null 的行是读取失败。"}
    try:
        from arb_monitor.xstocks_v2.store import write_json
        out.parent.mkdir(parents=True, exist_ok=True)
        write_json(out, snapshot)
    except Exception:  # noqa: BLE001
        pass
    return snapshot


def _default_http():
    from arb_monitor.xstocks_v2.feeds.http import Http
    return Http()
