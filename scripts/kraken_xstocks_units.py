#!/usr/bin/env python3
"""Build the Kraken xStocks unit/minimum configuration without buying tokens.

Kraken's private ``AddOrder`` endpoint accepts ``validate=true``. It does not
place an order and does not require the account to hold the asset. The returned
description contains both sides of the unit conversion, for example:

    sell 0.054600 STRCx/USD (0.050009 STRCSPV/USD @ limit 108.46)

From that we derive:

* market units per balance unit: the volume unit Kraken accepts (1 for xStocks);
* underlying units per token;
* the effective minimum order in market/token units.

A separate, optional field records the deposit-credit ratio. That ratio is only
known after a real deposit; it is not guessed from the multiplier.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.venues.kraken_spot import (  # noqa: E402
    KrakenError, KrakenSpotClient, load_credentials)

BOOK = ROOT / "data" / "xstocks_book.sqlite"
#: The table is **runtime state**: it is rewritten from Kraken's own answers
#: (nightly, plus one call per new listing). It used to be written into
#: ``config/``, which is tracked, so every refresh left the runner's checkout
#: dirty and ``scripts/deploy-from-github.sh`` refused to pull -- the trading
#: Mac stopped taking new code seven minutes after the runtime clone existed
#: (2026-10-10, log line "working tree dirty; not pulling"). It now lands under
#: the gitignored ``data-v2/``; ``config/`` keeps the committed seed.
OUT = ROOT / "data-v2" / "kraken_xstocks_units.toml"
SEED = ROOT / "config" / "kraken_xstocks_units.toml"
PROXY = "http://127.0.0.1:4780"
VALIDATE_PRICE = "1000.00"
VALIDATE_VOLUME = "1"
DESCR_RE = re.compile(
    r"(?:buy|sell)\s+([0-9.]+)\s+(\S+?)/(\S+)\s+"
    r".*?\(([0-9.]+)\s+(\S+?)/(\S+)\s+@")


def _decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def parse_order_descr(description: str) -> dict | None:
    """Parse Kraken's one-line order description."""
    match = DESCR_RE.search(str(description or ""))
    if not match:
        return None
    volume, market_symbol, quote, underlying_amount, underlying_symbol, underlying_quote = match.groups()
    volume_d = _decimal(volume)
    underlying_d = _decimal(underlying_amount)
    if not volume_d or not underlying_d:
        return None
    return {
        "market_volume": volume_d,
        "market_symbol": market_symbol,
        "quote": quote,
        "underlying_volume": underlying_d,
        "underlying_symbol": underlying_symbol,
        "underlying_quote": underlying_quote,
        "underlying_per_market_unit": underlying_d / volume_d,
    }


def load_symbols() -> list[str]:
    conn = sqlite3.connect(f"file:{BOOK}?mode=ro", uri=True)
    try:
        return [str(row[0]) for row in conn.execute(
            "select distinct issuer_symbol from spot_exits "
            "where issuer_symbol is not null and issuer_symbol != '' "
            "order by issuer_symbol")]
    finally:
        conn.close()


def load_public_pairs() -> dict[str, dict]:
    url = ("https://api.kraken.com/0/public/AssetPairs"
           "?aclass_base=tokenized_asset")
    request = urllib.request.Request(url, headers={"User-Agent": "arb-monitor/0.1"})
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    with opener.open(request, timeout=30) as response:
        payload = json.loads(response.read())
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    out: dict[str, dict] = {}
    for row in (payload.get("result") or {}).values():
        wsname = str(row.get("wsname") or "")
        if not wsname.endswith("/USD"):
            continue
        symbol = wsname.split("/", 1)[0]
        out[symbol] = row
    return out


def load_existing_deposit_evidence() -> dict[str, dict]:
    """Read the previous generated TOML without requiring a TOML writer."""
    out: dict[str, dict] = {}
    for path in evidence_sources():
        # The seed is read first and the live file last: a ratio measured here
        # wins over the committed one, but a ratio merged through a pull
        # request is not shadowed by a live file that predates it.
        current = ""
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if line.startswith("[kraken_units.") and line.endswith("]"):
                # A table key may be quoted (``[kraken_units."BRK.Bx"]``) because
                # a bare dotted key would be read as nesting. Store the real
                # symbol.
                current = line[len("[kraken_units."):-1].strip('"')
                out.setdefault(current, {})
                continue
            if not current or "=" not in line or line.startswith("#"):
                continue
            key, value = [part.strip() for part in line.split("=", 1)]
            if key.startswith("deposit_credit_"):
                try:
                    out[current][key] = json.loads(value)
                except json.JSONDecodeError:
                    out[current][key] = value.strip('"')
    return out


def evidence_sources() -> list[Path]:
    """The files that may hold the previous table, weakest evidence first."""
    return [path for path in (SEED, OUT) if path.exists()]


def toml_value(value) -> str:
    if value is None:
        return '""'
    return json.dumps(str(value), ensure_ascii=False)


BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def toml_key(key: str) -> str:
    """Quote a table key unless it is a bare TOML key.

    ``BRK.Bx`` is a real Kraken symbol. Written bare as
    ``[kraken_units.BRK.Bx]``, TOML reads that as three nested tables. The
    reader then sees a symbol called ``BRK`` with an empty row, and ``BRK.Bx``
    never enters the scan -- measured 2026-10-09, when a new-listing check
    reported ``BRK.Bx`` as missing from a file that already contained it.
    """
    return key if BARE_KEY.match(key) else json.dumps(key, ensure_ascii=False)


def validate_symbol(client: KrakenSpotClient, symbol: str) -> dict | None:
    last: Exception | None = None
    for attempt in range(3):
        for volume in (VALIDATE_VOLUME, "10", "100"):
            try:
                order = client.validate_order(
                    symbol, volume, side="sell", ordertype="limit",
                    price=VALIDATE_PRICE)
                parsed = parse_order_descr((order.get("descr") or {}).get("order", ""))
                if parsed:
                    return parsed
                last = RuntimeError("unparsable_descr")
                break
            except KrakenError as exc:
                text = ";".join(exc.errors)
                if "volume minimum not met" in text and volume != "100":
                    continue
                last = exc
                break
            except (OSError, RuntimeError) as exc:
                last = exc
                break
        time.sleep(0.4 * (attempt + 1))
    if last:
        raise last
    return None


def load_existing_rows() -> dict[str, dict]:
    """The rows of the previous file, so a targeted run can merge into them."""
    import tomllib

    from arb_monitor.xstocks_v2.feeds.census import flatten_units

    rows: dict[str, dict] = {}
    for path in evidence_sources():
        try:
            raw = tomllib.loads(path.read_text())
        except (OSError, ValueError):
            continue
        rows.update({symbol: dict(row) for symbol, row in
                     flatten_units(raw.get("kraken_units") or {}).items()})
    return rows


def build(symbols: list[str] | None = None) -> dict:
    """Validate every symbol, or only ``symbols`` and merge into the old file.

    A targeted run exists because a new listing must enter the scan the moment
    Kraken carries it, and re-validating all 177 symbols to add one costs 177
    private calls.
    """
    universe = load_symbols()
    pairs = load_public_pairs()
    evidence = load_existing_deposit_evidence()
    for row in evidence.values():
        if ("deposit_credit_units_per_token" in row
                and "deposit_credit_units_per_balance_unit" not in row):
            row["deposit_credit_units_per_balance_unit"] = row.pop(
                "deposit_credit_units_per_token")
    existing = evidence.setdefault("STRCx", {})
    existing.setdefault("deposit_credit_units_per_balance_unit", "1")
    existing.setdefault(
        "deposit_credit_source",
        "tx 0xb1cc8612; 0.05184276 chain -> 0.051843 Kraken")
    existing.setdefault("deposit_credit_verified_at", "2026-10-07")
    # The only exact live sample we have in this repository today.
    key, secret = load_credentials(ROOT / "config" / "kraken.env")
    client = KrakenSpotClient(key, secret, proxy=PROXY)
    targets = [str(item) for item in (symbols or universe)]
    rows: dict[str, dict] = load_existing_rows() if symbols else {}
    failures: dict[str, str] = {}
    for index, symbol in enumerate(targets, start=1):
        pair = pairs.get(symbol) or {}
        try:
            parsed = validate_symbol(client, symbol)
        except (KrakenError, OSError, RuntimeError) as exc:
            failures[symbol] = str(exc)[:240]
            continue
        if not parsed:
            failures[symbol] = "unparsable_descr"
            continue
        ordermin = _decimal(pair.get("ordermin"))
        eff_min = None
        if ordermin and parsed["underlying_per_market_unit"] > 0:
            eff_min = ordermin / parsed["underlying_per_market_unit"]
        row = {
            "market_units_per_balance_unit": "1",
            "underlying_symbol": parsed["underlying_symbol"],
            "underlying_per_market_unit": format(parsed["underlying_per_market_unit"], "f"),
            "pair_ordermin": None if ordermin is None else format(ordermin, "f"),
            "effective_min_units": None if eff_min is None else format(eff_min, "f"),
            "status": pair.get("status") or "",
        }
        row.update(evidence.get(symbol) or {})
        rows[symbol] = row
        if index % 25 == 0:
            print(f"validated {index}/{len(targets)}", flush=True)
        time.sleep(0.05)
    return {
        "version": "1",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "validate": {"volume": VALIDATE_VOLUME, "price": VALIDATE_PRICE},
        "targets": targets,
        "rows": rows,
        "failures": failures,
    }


def write_toml(payload: dict, path: Path = OUT) -> None:
    lines = [
        "# Generated by scripts/kraken_xstocks_units.py.",
        "# Market/min values come from Kraken AddOrder validate=true.",
        "# deposit_credit_* is only filled after a real deposit sample.",
        f'version = "{payload["version"]}"',
        f'generated_at = {toml_value(payload["generated_at"])}',
        "",
    ]
    for symbol, row in sorted(payload["rows"].items()):
        lines.append(f"[kraken_units.{toml_key(symbol)}]")
        for key in (
            "market_units_per_balance_unit",
            "underlying_symbol",
            "underlying_per_market_unit",
            "pair_ordermin",
            "effective_min_units",
            "status",
            "deposit_credit_units_per_balance_unit",
            "deposit_credit_source",
            "deposit_credit_verified_at",
        ):
            if key in row and row[key] is not None:
                lines.append(f"{key} = {toml_value(row[key])}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(OUT))
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--symbols", default=None,
                        help="comma separated: validate only these and merge "
                             "them into the existing file (a new listing costs "
                             "1 private call, not 177)")
    args = parser.parse_args(argv)
    symbols = None
    if args.symbols:
        symbols = [item.strip() for item in args.symbols.split(",") if item.strip()]
    payload = build(symbols)
    if not args.no_write:
        write_toml(payload, Path(args.out))
    mode = "targeted" if symbols else "full"
    print(f"{mode}: validated={len(payload['targets'])} rows={len(payload['rows'])} "
          f"failures={len(payload['failures'])} out={args.out}")
    for symbol, reason in list(payload["failures"].items())[:20]:
        print(f"  {symbol}: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
