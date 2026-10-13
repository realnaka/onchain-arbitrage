"""Pools measured dead, with evidence, a time to live, and a rotation.

Measured 2026-10-07: every round re-quoted pools that the previous round had
already rejected for depth -- the 29 USD MSTRx/BSC pool, the 3.77 USD FluxBeam
pool, the 811 USD Manifest pool. Each of those is a real Jupiter/EVM call per
round, and they crowd the 429 budget the live names need (a round was taking
84 s with `http_429 x6`), while the board's "highest spread" table stayed filled
with 414,906 bps fakes.

A pool goes on the list when a row is rejected with ``below_depth`` -- that
blocker already means "the book cannot take our size, or the same pool gives
back less than the round-trip allowance" (it is not a staleness or rail
problem).

Three lessons from 2026-10-08 are built in, because a wrong entry costs real
opportunity:

* **A verdict must carry its evidence.** The entry keeps the measured depth,
  the effective ticket, the same-pool round trip and which of the three signals
  fired, so the board can be read -- and challenged -- after the fact.
* **One measurement is not a verdict.** A pool is only skipped after
  ``confirm_hits`` consecutive rejections; the first one still gets quoted.
  Measured 2026-10-08: TSLAx/x-layer and SPCXx/x-layer were marked dead in one
  round, and the next hour measured a clean 10.5 / 11.2 bps round trip.
* **A wrong entry must be able to expire.** Besides the TTL, each round re-quotes
  the ``limit`` pools it has not looked at for the longest, and *clears* an entry
  whose re-measurement comes back clean.

Nothing is remembered forever: an entry expires after ``ttl_ms`` and the pool is
quoted again. Liquidity appears without asking us.
"""
from __future__ import annotations

import json
from pathlib import Path

#: The scan passes the policy value; this default keeps the module usable on its
#: own, where one measurement is enough (no budget to re-check).
DEFAULT_CONFIRM_HITS = 1
#: Backstop only -- the scan uses ``[watch] dead_pool_ttl_s``.
DEFAULT_TTL_MS = 6 * 60 * 60 * 1000


def key(*, venue: str, pool_id: str = "", chain: str = "",
        symbol: str = "") -> str:
    """Stable identity for a pool, or for a mint when the venue has none.

    Jupiter rows carry no pool id (the route is chosen at swap time), so their
    identity is the (chain, symbol) the quotes were asked for. An EVM pool is
    identified by its address, which is what the quoter reads.
    """
    pool = str(pool_id or "").strip().lower()
    if pool:
        return f"{str(venue or '?').lower()}:{pool}"
    return f"{str(venue or '?').lower()}:{str(chain or '?').lower()}:{str(symbol or '?')}"


def load(path: Path | str) -> dict:
    """``{key: entry}``; a missing or unreadable file is an empty list."""
    try:
        payload = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    pools = payload.get("pools") if isinstance(payload, dict) else payload
    if not isinstance(pools, dict):
        return {}
    return {str(k): v for k, v in pools.items() if isinstance(v, dict)}


def save(path: Path | str, entries: dict, *, now_ms: int) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(
        {"saved_ms": now_ms, "pools": entries}, ensure_ascii=False, indent=1,
        sort_keys=True) + "\n")
    return target


def is_dead(entries: dict, pool_key: str, *, now_ms: int,
            ttl_ms: int = DEFAULT_TTL_MS,
            confirm_hits: int = DEFAULT_CONFIRM_HITS) -> dict | None:
    """The entry when this pool is still known dead, else ``None``.

    ``confirm_hits`` is how many rejections it takes to *act* on the verdict.
    With ``confirm_hits = 2`` a pool rejected once is quoted again next round,
    and only skipped when the second round agrees.
    """
    entry = (entries or {}).get(pool_key)
    if not isinstance(entry, dict):
        return None
    # A missing ``hits`` means the entry was written before the counter existed
    # (or by hand), and being on the list at all is one measurement -- not zero.
    if int(entry.get("hits") or 1) < max(1, int(confirm_hits)):
        return None
    since = entry.get("since_ms")
    if not isinstance(since, (int, float)):
        return None
    if now_ms - since > ttl_ms:
        return None
    return entry


def mark(entries: dict, pool_key: str, *, now_ms: int, reason: str,
         depth_usd=None, notional_usdt=None, round_trip_bps=None,
         detail: str = "") -> dict:
    """Record (or refresh) one dead pool. Never resets ``since_ms``.

    Every argument that was measured is stored, and a missing measurement stays
    absent rather than becoming a zero: the board must be able to say "we did
    not read it", not "it is fine".
    """
    entry = entries.setdefault(pool_key, {})
    entry.setdefault("since_ms", now_ms)
    entry["last_seen_ms"] = now_ms
    entry["hits"] = int(entry.get("hits") or 0) + 1
    entry["reason"] = str(reason)
    if detail:
        entry["detail"] = str(detail)
    if depth_usd is not None:
        entry["depth_usd"] = str(depth_usd)
    if notional_usdt is not None:
        entry["notional_usdt"] = str(notional_usdt)
    if round_trip_bps is not None:
        entry["round_trip_bps"] = str(round_trip_bps)
    return entry


def clear(entries: dict, pool_key: str) -> dict | None:
    """Drop an entry whose re-measurement came back clean. Returns it, or None."""
    entry = (entries or {}).pop(pool_key, None)
    return entry if isinstance(entry, dict) else None


#: Verdicts this code does not hand out any more. An entry that only carries one
#: of them must not keep suppressing a pool: the rule that produced it is gone,
#: so nothing would ever refresh or contradict it.
#:
#: ``same_pool_round_trip`` (retired 2026-10-08, operator): a pool with no
#: liquidity on one side is a fact about the pool, not about our trade -- we
#: cross it once. It had put 9 EVM pools and 24 Solana mints out of monitoring.
RETIRED_DETAILS = frozenset({"same_pool_round_trip"})


def drop_retired(entries: dict) -> dict:
    """Remove entries whose verdict was retired; return the ones removed."""
    removed = {pool_key: entry for pool_key, entry in (entries or {}).items()
               if isinstance(entry, dict)
               and str(entry.get("detail") or "") in RETIRED_DETAILS}
    for pool_key in removed:
        entries.pop(pool_key, None)
    return removed


def recheck_keys(entries: dict, *, now_ms: int, limit: int,
                 ttl_ms: int = DEFAULT_TTL_MS) -> list[str]:
    """Keys to quote this round even though they are marked dead.

    Rotation, not "the oldest verdict": the order is how long ago the pool was
    last *looked at*, and a re-quote refreshes that stamp whether the pool is
    still bad or not. Every entry is therefore revisited in turn, and one round
    spends at most ``limit`` extra quotes -- the Jupiter 429 budget is why this
    is bounded at all.
    """
    if limit <= 0:
        return []
    live: list[tuple[float, str]] = []
    for pool_key, entry in (entries or {}).items():
        if not isinstance(entry, dict):
            continue
        since = entry.get("since_ms")
        if not isinstance(since, (int, float)) or now_ms - since > ttl_ms:
            continue
        last = entry.get("last_seen_ms")
        stamp = float(last) if isinstance(last, (int, float)) else float(since)
        live.append((stamp, str(pool_key)))
    live.sort()
    return [pool_key for _, pool_key in live[:limit]]


def sweep(entries: dict, *, now_ms: int, ttl_ms: int = DEFAULT_TTL_MS) -> dict:
    """Drop expired entries in place; return the ones that were dropped."""
    dropped = {k: v for k, v in (entries or {}).items()
               if not isinstance(v, dict)
               or not isinstance(v.get("since_ms"), (int, float))
               or now_ms - v["since_ms"] > ttl_ms}
    for pool_key in dropped:
        entries.pop(pool_key, None)
    return dropped
