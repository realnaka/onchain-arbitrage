"""Which Solana rails get a quote slot even when the ranking does not pick them.

Measured 2026-10-08 00:06: Kraken offers 300 (asset, hub) withdrawal rails, and
that round quoted 8 of them -- 6 Solana, 1 Ethereum, 1 TON. The other 160 Solana
rails produced **no row at all**. They were not unpriceable: every online xStock
has a Solana mint in the census. They simply did not win one of the
``stage2_max`` = 20 real-quote slots, which go to the largest Kraken-vs-reference
edge of the moment.

A rail that never reaches the top 20 is therefore never quoted, and the board can
never say whether it is worth a registration. Lessons section C is explicit:
"not sampled" is not "no opportunity".

So each round reserves ``[watch] rotation_slots`` of those slots for the Solana
rails that have waited the longest, oldest first. Like the dead-pool rotation,
the stamp moves when a rail is *looked at*, not when it looks good: a quote that
comes back empty still sends the name to the back of the queue, so a name that
always fails cannot hold a slot forever.
"""
from __future__ import annotations

import json
from pathlib import Path

#: The scan passes the policy value; this default keeps the module usable on its
#: own, where nothing is reserved (the ranking keeps every slot).
DEFAULT_SLOTS = 0


def load(path: Path | str) -> dict:
    """``{symbol: entry}``; a missing or unreadable file is an empty ledger."""
    try:
        payload = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    rails = payload.get("rails") if isinstance(payload, dict) else payload
    if not isinstance(rails, dict):
        return {}
    return {str(k): v for k, v in rails.items() if isinstance(v, dict)}


def save(path: Path | str, entries: dict, *, now_ms: int) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(
        {"saved_ms": now_ms, "rails": entries}, ensure_ascii=False, indent=1,
        sort_keys=True) + "\n")
    return target


def prune(entries: dict, *, candidates) -> dict:
    """Drop symbols that are no longer quotable, so the ledger cannot grow.

    An empty ``candidates`` is "this round learned nothing" -- a missing
    withdraw-methods cache, say -- never "no rail exists". Wiping the stamps
    then would reset the whole queue for no reason, which is exactly what the
    empty list from the 2026-10-08 join bug did (measured 00:20: the ledger was
    emptied of every stamp the earlier round had written).
    """
    keep = {str(symbol) for symbol in (candidates or ())}
    if not keep:
        return entries
    for symbol in [key for key in (entries or {}) if key not in keep]:
        entries.pop(symbol, None)
    return entries


def waiting(entries: dict, *, candidates, now_ms: int,
            exclude=()) -> list[dict]:
    """The quote queue, longest wait first.

    Every candidate is in the queue -- one that has never been quoted leads it
    (``last_slot_ms = None``), then the ones whose last slot is oldest. Nothing
    is dropped for being bad: whether the last quote found an edge is not this
    module's business, only whether the name was looked at.
    """
    skip = {str(symbol) for symbol in (exclude or ())}
    out: list[dict] = []
    for symbol in sorted({str(s) for s in (candidates or ())}):
        if symbol in skip:
            continue
        entry = (entries or {}).get(symbol) if isinstance(entries, dict) else None
        entry = entry if isinstance(entry, dict) else {}
        last = entry.get("last_slot_ms")
        last = int(last) if isinstance(last, (int, float)) else None
        out.append({
            "symbol": symbol,
            "last_slot_ms": last,
            "slots": int(entry.get("slots") or 0),
            "never_quoted": last is None,
            "waited_s": (None if last is None else max(0, (now_ms - last) // 1000)),
        })
    # ``None`` sorts before every timestamp, which is exactly the ordering the
    # queue wants: never quoted first, then the longest wait.
    out.sort(key=lambda item: (0 if item["last_slot_ms"] is None else 1,
                               item["last_slot_ms"] or 0, item["symbol"]))
    return out


def mark(entries: dict, symbols, *, now_ms: int) -> dict:
    """Record that these rails were looked at this round."""
    for symbol in {str(s) for s in (symbols or ())}:
        entry = entries.setdefault(symbol, {})
        entry["last_slot_ms"] = int(now_ms)
        entry["slots"] = int(entry.get("slots") or 0) + 1
        entry.setdefault("first_slot_ms", int(now_ms))
    return entries


def reserve_order(wanted, *, queue, slots: int, budget: int) -> tuple[list, list]:
    """This round's Solana quote list: ``wanted`` with ``slots`` given to the queue.

    Returns ``(list, picks)``. The reserve comes **out of the same budget**, not
    on top of it, so the round issues the same number of Jupiter quotes and its
    length -- and the freshness of the rows it produces -- does not change. What
    is given up is the tail of ``wanted`` (the weakest ranked names and the
    reverse-watch fill), never the top of it: the list keeps its order, and only
    the last entries make room.

    ``slots = 0`` or an empty queue returns ``wanted`` untouched. A queue entry
    that is already in ``wanted`` costs nothing: it is quoted once, in its place.
    """
    picks = [item for item in list(queue or []) if isinstance(item, dict)
             and item.get("symbol")][:max(0, int(slots))]
    already = {str((entry or {}).get("symbol")) for entry in (wanted or [])}
    fresh = [item for item in picks if str(item["symbol"]) not in already]
    if not fresh:
        return list(wanted or []), []
    fresh_symbols = {str(item["symbol"]) for item in fresh}
    kept = [entry for entry in (wanted or [])
            if str((entry or {}).get("symbol")) not in fresh_symbols]
    room = max(0, int(budget) - len(fresh))
    return kept[:room] + [{"symbol": str(item["symbol"])} for item in fresh], picks
