"""Append-only cycle ledger for v2.

Same shape as the v1 ledger on purpose: one JSON object per line, one line per
state transition, and never rewritten. A reader can always tell where the coins
are (``held_at``) and whether the next round may open a new position.
"""
from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path

from arb_monitor.xstocks_v2.store import append_jsonl, read_jsonl

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PATH = ROOT / "data-v2" / "xstocks_v2_cycles.jsonl"

REFUSED = "refused"
REVERTED = "reverted"
SOLD = "sold"
STOPPED = "stopped"
UNKNOWN = "unknown"
PAPER = "paper"
#: The route was walked once on purpose and the coins are still ours, in our own
#: wallet. ``probed`` is not ``sold`` (nothing was sold) and not ``stopped``
#: (nothing went wrong): the next trade deposits this inventory with its own leg.
PROBED = "probed"

HOLD_STATES = frozenset({STOPPED, UNKNOWN})
#: States that leave inventory with us while the cycle lock is free.
INVENTORY_STATES = frozenset({PROBED})

# Every state that leaves coins somewhere other than flat.
HELD_AT = frozenset({"wallet", "in_bridge", "dest_wallet", "kraken", "unknown"})

#: A clean finish: flat, or holding on purpose where a later cycle expects it.
FLAT_STATES = frozenset({REFUSED, REVERTED, SOLD})


def row(*, symbol: str, chain: str, family: str, state: str, mode: str,
        size_usdt, tokens=None, buy_price=None, sell_price=None,
        edge_t0_bps=None, edge_after_requote_bps=None, net_usdt=None,
        held_at: str = "", reason: str = "", costs: dict | None = None,
        extra: dict | None = None, final_held_at: str = "") -> dict:
    """One ledger line.

    Money is converted to strings here rather than at the edges: a ``Decimal``
    that reaches ``json.dumps`` without ``default=`` raises, and a ledger that
    cannot be printed is a ledger nobody reads (found 2026-10-07 when the paper
    CLI crashed *after* writing the file).
    """
    return _plain({
        "ts_ms": int(time.time() * 1000),
        "symbol": symbol,
        "chain": chain,
        "family": family,
        "state": state,
        "mode": mode,
        "size_usdt": size_usdt,
        "tokens": tokens,
        "buy_price_usd": buy_price,
        "sell_price_usd": sell_price,
        "edge_t0_bps": edge_t0_bps,
        "edge_after_requote_bps": edge_after_requote_bps,
        "net_usdt": net_usdt,
        "held_at": held_at if held_at in HELD_AT else "",
        # Where the coins really are when the cycle ended **on purpose** (a
        # sample probe). Without it, ``effective_held_at`` falls back to the
        # old Solana default ``dest_wallet`` -- right for a Solana probe, wrong
        # for an Ethereum one, whose slice is on Kraken (2026-10-09).
        **({"final_held_at": final_held_at} if final_held_at else {}),
        "reason": reason,
        "costs": dict(costs or {}),
        "extra": dict(extra or {}),
    })


def _plain(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def record(entry: dict, path: Path | str = DEFAULT_PATH) -> Path:
    return append_jsonl(path, entry)


def prune(path: Path | str = DEFAULT_PATH, *, keep_paper_days: int = 2,
          now_ms: int | None = None, dry_run: bool = False) -> dict:
    """Drop old *paper* rows; keep every real-money row and recent paper ones.

    The file was append-only with no retention (measured 2026-10-07: 1656 rows /
    838 KB, growing ~7k rows a day because a paper round writes ~60 lines). The
    rows that matter -- ``mode == "live"`` -- are the ones that say where the
    coins are, so they are never touched; a paper row older than
    ``keep_paper_days`` is only statistics, and the statistics the board shows
    come from the last 400 rows anyway.

    A line that does not parse is kept: a broken line is evidence, and silently
    deleting it would be the same class of mistake as printing 0 for a failed
    read. The rewrite is atomic (temp file + rename) so a crash cannot leave a
    half-written ledger.
    """
    path = Path(path)
    if not path.exists():
        return {"kept": 0, "dropped": 0, "malformed": 0, "bytes_before": 0,
                "bytes_after": 0, "dry_run": bool(dry_run)}
    cutoff = (now_ms if now_ms is not None else int(time.time() * 1000)) \
        - keep_paper_days * 24 * 60 * 60 * 1000
    kept: list[str] = []
    dropped = malformed = 0
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            kept.append(line)
            malformed += 1
            continue
        if not isinstance(entry, dict):
            kept.append(line)
            malformed += 1
            continue
        real = str(entry.get("mode") or "live") == "live"
        ts = entry.get("ts_ms")
        old = isinstance(ts, (int, float)) and ts < cutoff
        if real or not old:
            kept.append(line)
        else:
            dropped += 1
    before = path.stat().st_size
    if not dry_run and dropped:
        tmp = path.with_suffix(path.suffix + ".prune.tmp")
        tmp.write_text("".join(line + "\n" for line in kept))
        tmp.replace(path)
    after = path.stat().st_size
    return {"kept": len(kept), "dropped": dropped, "malformed": malformed,
            "bytes_before": before, "bytes_after": after,
            "dry_run": bool(dry_run)}


def recent(path: Path | str = DEFAULT_PATH, limit: int = 50) -> list[dict]:
    return read_jsonl(path, limit=limit)


def last_holding(path: Path | str = DEFAULT_PATH) -> dict | None:
    """The most recent line that could still mean coins are out of our hands."""
    for entry in reversed(recent(path, limit=None)):
        if not _is_real(entry):
            continue
        if entry.get("state") in HOLD_STATES | INVENTORY_STATES:
            return entry
        if entry.get("state") in FLAT_STATES:
            return None
    return None


def _is_real(entry: dict, modes: tuple[str, ...] = ("live",)) -> bool:
    """Only rows that moved real money count as positions.

    The paper runner writes to the same file. Letting its ``refused`` lines be
    "the last word" about a symbol erases a real position: measured 2026-10-07,
    a paper round after the NVDAx probe cleared NVDAx from ``open_positions``
    while 0.0065 NVDAx was sitting in our Solana wallet.
    """
    return str(entry.get("mode") or "live") in modes


def effective_held_at(entry: dict) -> str:
    """Where the coins *are*, for a row that was written before its fix.

    ``probed`` means "we bought the minimum, crossed, and stopped on purpose",
    so the coins are in our own wallet on the destination side. The first probe
    row (NVDAx, 2026-10-07 21:36) was written before ``final_held_at`` existed
    and therefore carries the *in-flight worst case* of the last step
    (``in_bridge``) -- which reads on the board as "a message is still crossing"
    long after the coins landed. New rows write ``final_held_at`` explicitly; an
    old row without it is read as ``dest_wallet``, never left as ``in_bridge``.

    Nothing else is rewritten here: ``stopped``/``unknown`` keep the position
    that was recorded, because for those the worst case is the honest one.
    """
    recorded = str(entry.get("held_at") or "")
    final = str(entry.get("final_held_at") or "")
    if final:
        return final
    if str(entry.get("state")) == PROBED:
        return "dest_wallet"
    return recorded


def open_positions(path: Path | str = DEFAULT_PATH,
                   modes: tuple[str, ...] = ("live",)) -> list[dict]:
    """Symbols whose last *real* word was ``held``, not a clean close.

    ``modes`` is the set of ledgers to believe. Paper rows are not inventory.
    ``held_at`` is normalised through ``effective_held_at`` so a ``probed`` row
    written before ``final_held_at`` existed does not read as still in flight.
    """
    out = []
    for entry in latest_by_symbol(path, modes).values():
        if entry.get("state") not in HOLD_STATES | INVENTORY_STATES:
            continue
        normalised = dict(entry, held_at=effective_held_at(entry))
        out.append(normalised)
    return out


def latest_by_symbol(path: Path | str = DEFAULT_PATH,
                     modes: tuple[str, ...] = ("live",)) -> dict[str, dict]:
    """The last *real* row per symbol, over the whole ledger.

    ``recent()`` defaults to 50 lines, and one scan round writes about 60: the
    NVDAx probe row fell out of that window and the position disappeared from
    the board while the coins were still in our wallet (found 2026-10-07).
    Position questions have to read the ledger, not its tail.
    """
    verdicts: dict[str, dict] = {}
    for entry in recent(path, limit=None):
        if not _is_real(entry, modes):
            continue
        symbol = str(entry.get("symbol") or "")
        if symbol:
            verdicts[symbol] = entry
    return verdicts


def carried_inventory(path: Path | str = DEFAULT_PATH,
                      modes: tuple[str, ...] = ("live",)) -> dict[str, dict]:
    """``symbol -> last probed row``: coins we already hold on purpose.

    The probe is the first slice of a trade, so the next cycle has to know the
    slice is sitting in the wallet. Without this the remainder would be sized as
    if we were flat, and the probed tokens would be stranded.
    """
    out: dict[str, dict] = {}
    for symbol, entry in latest_by_symbol(path, modes).items():
        if entry.get("state") == PROBED:
            out[symbol] = dict(entry, held_at=effective_held_at(entry))
    return out
