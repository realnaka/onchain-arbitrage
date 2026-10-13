#!/usr/bin/env python3
"""Stage 1 paper runner: chain -> Kraken, three legs, no broadcast.

    scripts/xstocks_v2_paper.py status
    scripts/xstocks_v2_paper.py once
    scripts/xstocks_v2_paper.py run --interval 120

The runner reads the stage-0 scan, re-quotes the sell leg at the real book, and
records a decision per candidate. It never signs: ``auto_orders`` is off, the
phase is not executable, and ``broadcast`` is hard-coded False in the decision
module.

Why it re-quotes instead of trusting the scan: the scan's spread is measured at
t0. By the time a human or a program reacts, the book has moved, and a path that
bridges or transfers takes minutes. The gate reads
``edge_after_requote_bps`` (spec section 5.3).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.xstocks_v2 import balances, ledger, paper, scan, units  # noqa: E402
from arb_monitor.xstocks_v2 import cycle as cycle_core  # noqa: E402
from arb_monitor.xstocks_v2 import live as live_mod  # noqa: E402
from arb_monitor.xstocks_v2.feeds import evm, kraken  # noqa: E402
from arb_monitor.xstocks_v2.feeds.jupiter import JupiterFeed  # noqa: E402
from arb_monitor.xstocks_v2.feeds.http import Http  # noqa: E402
from arb_monitor.xstocks_v2.policy import V2Policy  # noqa: E402
from arb_monitor.xstocks_v2.store import write_json  # noqa: E402

DATA = scan.DEFAULT_DATA
SCAN = DATA / "xstocks_v2_scan.json"
STATE = DATA / "xstocks_v2_paper.json"
EXECUTABLE_PHASES = {"paper_kraken", "atomic", "ccip", "kraken_withdraw"}
# Gas tokens per chain, priced from the same venue we eventually sell on.
NATIVE_PAIR = {"solana": "SOL", "eth": "ETH", "x-layer": "POL",
               "bsc": "BNB", "arbitrum": "ETH", "optimism": "ETH",
               "ink": "ETH", "monad": "MON", "hyperevm": "HYPE", "mantle": "MNT"}


def _d(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def native_usd(http: Http, chains) -> dict[str, Decimal]:
    """Price each chain's gas token in USD.

    One implementation, in ``feeds.kraken``: the live runner prices the same
    cost for the same row, and two sources for one cost is how one trade ends
    up with two nets.
    """
    wanted = sorted({NATIVE_PAIR[chain] for chain in chains if chain in NATIVE_PAIR})
    return kraken.native_usd(http, wanted)


def prune_every_hour(data_dir, rounds: int, interval_s: float, *,
                     now_ms: int | None = None, keep_paper_days: int = 2):
    """Ledger retention, once an hour, from the paper loop.

    Extracted so the arithmetic and the path join can be tested: the first
    version did ``data_dir / ledger.DEFAULT_PATH.name`` with ``data_dir`` still a
    string, which only showed up as ``prune_error: TypeError`` in the live log
    (measured 2026-10-07 23:0x). Returns ``None`` when this round is not the
    hourly one, so the caller can stay quiet.
    """
    period = max(1, int(3600 / max(30.0, float(interval_s))))
    if rounds % period != 1:
        return None
    return ledger.prune(Path(data_dir) / ledger.DEFAULT_PATH.name,
                        keep_paper_days=keep_paper_days, now_ms=now_ms)


def gate(policy: V2Policy, data_dir: Path) -> tuple[bool, str]:
    """One place decides whether a new position may be opened."""
    paths = cycle_core.halt_paths(data_dir)
    busy, why = cycle_core.v1_busy(ROOT)
    allowed, reason = cycle_core.open_gate(
        halt=paths["halt"], lock=paths["lock"], v1_busy=busy,
        kill=paths["kill"].exists(), auto_halt=paths["auto_halt"].exists())
    if not allowed:
        return False, reason if reason != "v1_live_busy" else f"v1_live_busy: {why}"
    # The lock is taken per cycle, not held across the whole scan.
    return True, "ok"


def run_once(*, policy: V2Policy | None = None, data_dir: Path | str = DATA,
             http: Http | None = None, rescan: bool = True) -> dict:
    started = time.monotonic()
    policy = policy or V2Policy.load()
    data_dir = Path(data_dir)
    http = http or Http()
    allowed, reason = gate(policy, data_dir)
    # Live also counts as "evaluate": the paper runner is the signal recorder and
    # the live executor is the actor. Both read the same gate.
    phase_allows = (policy.phase in EXECUTABLE_PHASES
                    and policy.auto_orders in {"paper", "live"})

    scan_path = data_dir / SCAN.name
    snapshot = json.loads(scan_path.read_text()) if scan_path.exists() else {}
    age_ms = time.time() * 1000 - float(snapshot.get("generated_ms") or 0)
    refreshed = False
    if rescan and age_ms > policy.max_age_ms:
        # A paper decision on an old scan is a decision about the past. Refresh
        # instead of relaxing the freshness rule.
        snapshot = scan.run_scan(policy=policy, http=http, data_dir=data_dir)
        refreshed = True
        age_ms = time.time() * 1000 - float(snapshot.get("generated_ms") or 0)
    rows = snapshot.get("rows") or []
    candidates = [row for row in rows
                  if row.get("family") == paper.CHAIN_TO_KRAKEN
                  and row.get("chain") in scan.PHASE_CHAINS]
    # Only names already near the threshold are worth a depth call.
    session = str(snapshot.get("session") or "calendar_unknown")
    floor = policy.required_edge_bps(session) - policy.stage2_buffer_bps
    near = [row for row in candidates
            if (_d(row.get("edge_t0_bps")) or Decimal("-1e9")) >= floor]

    chains = sorted({row["chain"] for row in near})
    natives = native_usd(http, chains) if near else {}
    gas_prices: dict[str, int | None] = {}
    for chain in chains:
        config = evm.CHAINS.get(chain)
        gas_prices[chain] = evm.gas_price_wei(http, config) if config else None

    feed = JupiterFeed(http)
    multipliers = scan.load_multipliers()
    # The wallet's two stablecoins, read once per round: funding is decided at
    # execution (operator 2026-10-08: 机会发现不看钱包，缺什么就换什么), but the
    # paper runner has to publish the *same* net the live gate would (A40).
    raw_stables = {name: live_mod.spl_balance(http, mint) for name, mint
                   in live_mod.SOLANA_QUOTES.items()}
    # A42: a failed read is not an empty wallet.
    unreadable_stables = [name for name, value in raw_stables.items()
                          if value is None]
    stables = {name: (value or Decimal(0))
               for name, value in raw_stables.items()}
    conversion_bps = None
    if stables.get("USDC", 0) < policy.per_trade_quote_usdt and stables.get("USDT", 0) > 0:
        conversion_bps = live_mod.conversion_cost_bps(
            feed=feed, size_usdt=policy.per_trade_quote_usdt, policy=policy)
    decisions: list[dict] = []
    for row in near:
        symbol = row["symbol"]
        # The size the scan priced this row at: for a pool-side row that is
        # ``min(ticket, depth x share)``, not the global ceiling. Judging the
        # re-quote at the ceiling while the row says 60 USDT is two answers to
        # one question (2026-10-08).
        size = _d(row.get("notional_usdt")) or policy.per_trade_quote_usdt
        # Both legs are re-priced back to back. Re-checking one side against a
        # scan-time price on the other is not a re-quote: it marks every row
        # stale, which is true and useless (measured 2026-10-07).
        entry = scan.requote_buy(
            symbol=symbol, chain=row["chain"], policy=policy, http=http,
            feed=feed) or {}
        depth = kraken.fetch_depth(http, [symbol])
        depth_recv = int(time.time() * 1000)
        book = (depth["depth"] or {}).get(symbol) or {}
        kraken_bid = kraken.book_side(book.get("bids"), side="sell")
        fresh_buy = entry.get("buy")
        buy_price = fresh_buy.price_usd if fresh_buy is not None else None
        # Prefer the token count the fresh quote actually returned; dividing the
        # notional by a price assumes a precision the venue did not give us.
        tokens = entry.get("tokens") if entry else None
        if tokens is None and buy_price and buy_price > 0:
            tokens = size / buy_price
        skew_ms = (None if fresh_buy is None
                   else abs(depth_recv - int(fresh_buy.recv_ms)))
        fresh_row = {**row}
        if buy_price is not None:
            fresh_row["buy_price_usd"] = format(buy_price, "f")
        chain = row["chain"]
        # How many Kraken credit units one on-chain token becomes. Solana scales
        # its raw amount by the mint's multiplier; the EVM chains do not. The
        # sale has to be priced in the same unit as the Kraken bid, or the row
        # and its net disagree by (multiplier − 1) bps (2026-10-08).
        credit_per_token = (multipliers.get(symbol, Decimal(1))
                            if chain in units.SCALED_RAW_CHAINS else Decimal(1))
        # The first transfer to a mint carries the account rent; every later one
        # is the base fee. Charging the first one forever is 145 bps on a 25
        # USDT ticket, which is enough to hide a real row (2026-10-08).
        first_for_mint = live_mod.first_transfer_for(
            symbol, DATA / live_mod.TRANSFER_SEEN_NAME)
        if chain == "solana":
            transfer, transfer_note = paper.transfer_cost_usdt(
                chain=chain, first_for_mint=first_for_mint,
                sol_usd=natives.get("SOL"), policy=policy)
        else:
            transfer = Decimal(0)
            transfer_note = "gas_quote_required"
        gas, _gas_note = paper.gas_cost_usdt(
            chain=chain, gas_price_wei=gas_prices.get(chain),
            native_usd=natives.get(NATIVE_PAIR.get(chain, "")), policy=policy)
        def judge(step: Decimal, step_entry: dict) -> dict:
            """Decide one size. The base ticket decides whether the row is real;
            the ladder then steps up while it still pays (operator 2026-10-08:
            "25 起报，过了门槛的再往上加档")."""
            step_buy = step_entry.get("buy")
            step_price = step_buy.price_usd if step_buy is not None else None
            step_tokens = step_entry.get("tokens")
            if step_tokens is None and step_price and step_price > 0:
                step_tokens = step / step_price
            step_skew = (None if step_buy is None
                         else abs(depth_recv - int(step_buy.recv_ms)))
            step_row = {**row}
            if step_price is not None:
                step_row["buy_price_usd"] = format(step_price, "f")
            # The Kraken leg is priced on the real book **for this rung's size**
            # (operator 2026-10-08: orderbook 只回答「这个量能成交到什么均价」,
            # 能不能做由利润判). Using the best bid here is what let a 2 USD order
            # print +203 bps on a 25 USDT ticket (MCDx).
            credits = (None if step_tokens is None
                       else step_tokens * credit_per_token)
            fill = (kraken.book_fill(book.get("bids"), credits, side="sell")
                    if credits is not None and credits > 0 else None)
            if fill is None or fill["vwap"] is None or not fill["ok"]:
                result = {"state": "refused", "reason": "kraken_depth_short",
                          "net_usdt": None, "edge_after_requote_bps": None,
                          "costs": {}, "broadcast": False,
                          "kraken_bid_at_size": None,
                          "sell_basis": "kraken_book_vwap",
                          "book_levels_used": (None if fill is None
                                               else fill["levels_used"]),
                          # ``book_fill`` reports ``short`` as a decimal
                          # **string** ("0.491489"), not a Decimal. Formatting it
                          # with ``"f"`` raised
                          # ``ValueError: Unknown format code 'f' for object of
                          # type 'str'`` and killed the whole round -- 27 times
                          # up to round 267 (A47, found 2026-10-09). It only
                          # fires when the book cannot fill the size, which is
                          # why it looked intermittent.
                          "book_short": (None if fill is None
                                         else str(fill["short"]))}
                result.update({"size_usdt": step, "tokens": step_tokens,
                               "buy_price_usd": step_price,
                               "pair_skew_ms": step_skew,
                               "transfer_note": transfer_note,
                               "first_for_mint": first_for_mint})
                return result
            sell_vwap = fill["vwap"]
            slack = step * Decimal(policy.max_slippage_bps) / Decimal(10000)
            funding = live_mod.funding_plan(usdc=stables.get("USDC"),
                                            usdt=stables.get("USDT"),
                                            need=step, slack=slack)
            if funding["funding"] == "insufficient":
                result = {"state": "refused",
                          "reason": ("wallet_unreadable" if unreadable_stables
                                     else "funding_short"),
                          "net_usdt": None, "edge_after_requote_bps": None,
                          "costs": {}, "broadcast": False,
                          "kraken_bid_at_size": format(sell_vwap, "f"),
                          "sell_basis": "kraken_book_vwap",
                          "funding": "insufficient",
                          # "funding_short" alone was one word for 4,882 rows a
                          # day: it never said which chain, which ticket, or how
                          # far off the wallet was (operator 2026-10-10).
                          "funding_gap": live_mod.funding_gap(funding)}
                result.update({"size_usdt": step, "tokens": step_tokens,
                               "buy_price_usd": step_price,
                               "pair_skew_ms": step_skew,
                               "transfer_note": transfer_note,
                               "first_for_mint": first_for_mint})
                return result
            conversion_usdt = Decimal(0)
            if funding["funding"] in ("convert_usdt_to_usdc",
                                      "top_up_usdc_from_usdt"):
                conversion_usdt = (None if conversion_bps is None else
                                   funding["convert_amount_usdt"]
                                   * conversion_bps / Decimal(10000))
            result = paper.decide(
                row=step_row, policy=policy, kraken_bid=sell_vwap,
                size_usdt=step, tokens=step_tokens, transfer_usdt=transfer,
                gas_usdt=gas, phase_allows=phase_allows and allowed,
                slot_free=True, reason_if_busy=reason,
                scan_fresh=age_ms <= policy.max_age_ms, pair_skew_ms=step_skew,
                session_label=session, credit_per_token=credit_per_token,
                conversion_usdt=conversion_usdt,
                leg_note={
                    "sell_basis": "kraken_book_vwap",
                    "kraken_best_bid": format(fill["best"], "f"),
                    "kraken_bid_at_size": format(sell_vwap, "f"),
                    "book_levels_used": fill["levels_used"],
                    "book_slip_bps": (None if fill["slip_bps"] is None
                                      else format(fill["slip_bps"], "f")),
                    "book_notional_usd": format(fill["notional_usd"], "f"),
                })
            result.update({"size_usdt": step, "tokens": step_tokens,
                           "buy_price_usd": step_price, "pair_skew_ms": step_skew,
                           "kraken_bid_at_size": format(sell_vwap, "f"),
                           "sell_basis": "kraken_book_vwap",
                           "kraken_best_bid": format(fill["best"], "f"),
                           "funding": funding["funding"],
                           "convert_amount_usdt": format(
                               funding["convert_amount_usdt"], "f"),
                           "conversion_usdt": (None if conversion_usdt is None
                                               else format(conversion_usdt, "f")),
                           "transfer_note": transfer_note,
                           "first_for_mint": first_for_mint})
            return result

        judged = judge(size, entry)
        ladder_note = [f"{size}:{judged['state']}:{judged['reason']}"]
        if judged["state"] == "paper":
            for step in live_mod.ladder_sizes(
                    base=size, ceiling=policy.per_trade_quote_usdt)[1:]:
                step_entry = scan.requote_buy(
                    symbol=symbol, chain=row["chain"], policy=policy, http=http,
                    feed=feed, notional=step) or {}
                attempt = judge(step, step_entry)
                ladder_note.append(f"{step}:{attempt['state']}:{attempt['reason']}")
                if attempt["state"] != "paper":
                    # A rung that fails only on the **fixed** part of the cost is
                    # not the end of the ladder: the transfer/bridge fee is per
                    # message, so a bigger ticket can still pay (measured
                    # 2026-10-08: the x-layer->solana hop was net +0.099 USDT at
                    # 25 and +0.79 at 200). A rung whose *edge* fell under the
                    # bar cannot be rescued by size -- the bar is in bps.
                    if attempt["reason"] == "below_net":
                        continue
                    break
                judged = attempt
                entry = step_entry
        decision = judged
        size = judged["size_usdt"]
        tokens = judged["tokens"]
        buy_price = judged["buy_price_usd"]
        # The price the decision used, not the best bid we happened to read
        # first: otherwise the ledger's ``sell_price`` would disagree with its
        # own net (2026-10-08).
        used_bid = judged.get("kraken_bid_at_size") or kraken_bid
        entry = ledger.row(
            symbol=symbol, chain=chain, family=row["family"], state=decision["state"],
            mode="paper", size_usdt=size, tokens=tokens,
            buy_price=buy_price, sell_price=used_bid,
            edge_t0_bps=row.get("edge_t0_bps"),
            edge_after_requote_bps=decision.get("edge_after_requote_bps"),
            net_usdt=decision.get("net_usdt"), reason=decision["reason"],
            costs=decision.get("costs"),
            extra={"blockers": row.get("blockers"),
                   "kraken_bid_at_size": used_bid,
                   "kraken_best_bid": judged.get("kraken_best_bid"),
                   "sell_basis": judged.get("sell_basis"),
                   "book_levels_used": judged.get("book_levels_used"),
                   "book_short": judged.get("book_short"),
                   "funding": judged.get("funding"),
                   "funding_gap": judged.get("funding_gap") or {},
                   "convert_amount_usdt": judged.get("convert_amount_usdt"),
                   "conversion_usdt": judged.get("conversion_usdt"),
                   "buy_requoted": fresh_buy is not None,
                   "buy_recv_ms": fresh_buy.recv_ms if fresh_buy is not None else None,
                   # The unit the sale was priced in and the transfer line it
                   # paid, on the row itself: these two are what the 2026-10-08
                   # unit mismatch and the first-transfer rent hid.
                   "credit_per_token": format(credit_per_token, "f"),
                   "first_for_mint": first_for_mint,
                   "transfer_note": transfer_note})
        ledger.record(entry, data_dir / "xstocks_v2_cycles.jsonl")
        decisions.append(entry)

    state = {
        "updated_ms": int(time.time() * 1000),
        "phase": policy.phase,
        "auto_orders": policy.auto_orders,
        "broadcast": False,
        "gate": {"allowed": allowed, "reason": reason},
        "scan_age_s": round(age_ms / 1000, 1),
        "scan_refreshed": refreshed,
        "candidates": len(candidates),
        "near": len(near),
        "states": _tally(decisions),
        "decisions": decisions[-40:],
        "note": ("阶段 1 纸面。不签名。门槛只看复价后的净价差；"
                 "t0 过门槛而复价不过的行记为 false_t0。"),
    }
    write_json(data_dir / STATE.name, state)
    state["round_seconds"] = round(time.monotonic() - started, 2)
    return state


def _tally(decisions: list[dict]) -> dict:
    out: dict[str, int] = {}
    for entry in decisions:
        key = f"{entry['state']}:{entry['reason'] or 'ok'}"
        out[key] = out.get(key, 0) + 1
    return out


def status(policy: V2Policy | None = None, data_dir: Path | str = DATA) -> dict:
    policy = policy or V2Policy.load()
    data_dir = Path(data_dir)
    allowed, reason = gate(policy, data_dir)
    return {
        "phase": policy.phase,
        "auto_orders": policy.auto_orders,
        "gate_allowed": allowed,
        "gate_reason": reason,
        "open_positions": [entry.get("symbol") for entry in ledger.open_positions(
            data_dir / "xstocks_v2_cycles.jsonl")],
        "broadcast": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    once = sub.add_parser("once")
    once.add_argument("--data-dir", default=str(DATA))
    run = sub.add_parser("run")
    run.add_argument("--interval", type=float, default=120.0)
    run.add_argument("--data-dir", default=str(DATA))
    run.add_argument("--rounds", type=int, default=0)
    args = parser.parse_args()
    data_dir = getattr(args, "data_dir", str(DATA))

    if args.cmd == "status":
        print(json.dumps(status(data_dir=data_dir), ensure_ascii=False, default=str))
        return 0
    if args.cmd == "once":
        print(json.dumps(run_once(data_dir=data_dir), ensure_ascii=False, default=str))
        return 0
    rounds = 0
    while args.rounds == 0 or rounds < args.rounds:
        rounds += 1
        started = time.monotonic()
        try:
            state = run_once(data_dir=data_dir)
            print(json.dumps({"round": rounds, "gate": state["gate"],
                              "candidates": state["candidates"],
                              "states": state["states"]}, ensure_ascii=False), flush=True)
            # The board only renders what a process wrote: the balance snapshot
            # is collected here, next to the other reads. Best effort -- a venue
            # that will not answer becomes a 「读不到」 row, not a stopped round.
            try:
                balances.refresh(data_dir=data_dir)
            except Exception as exc:  # noqa: BLE001
                print(json.dumps({"balances_error": f"{type(exc).__name__}: {exc}"},
                                 ensure_ascii=False), flush=True)
            # Ledger retention, once an hour. The file grew ~7k rows a day with
            # no policy (measured 2026-10-07: 1656 rows / 838 KB); only *paper*
            # rows older than two days are dropped, and live rows never are.
            try:
                pruned = prune_every_hour(data_dir, rounds, args.interval)
                if pruned and pruned.get("dropped"):
                    print(json.dumps({"ledger_pruned": pruned},
                                     ensure_ascii=False), flush=True)
            except Exception as exc:  # noqa: BLE001
                print(json.dumps({"prune_error": f"{type(exc).__name__}: {exc}"},
                                 ensure_ascii=False), flush=True)
        except Exception as exc:  # noqa: BLE001
            # The traceback matters: this log has carried
            # ``ValueError: Unknown format code 'f' for object of type 'str'``
            # about once every ten rounds since round 4 (27 times up to round
            # 263) and one line of message is not enough to find it.
            print(json.dumps({"round": rounds,
                              "error": f"{type(exc).__name__}: {exc}",
                              "traceback": traceback.format_exc()[-2000:]},
                             ensure_ascii=False), flush=True)
            time.sleep(15)
            continue
        time.sleep(max(10.0, args.interval - (time.monotonic() - started)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
