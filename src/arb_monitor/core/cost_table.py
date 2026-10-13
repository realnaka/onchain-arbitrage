"""Per asset / venue round-trip costs, with the reviewed global defaults kept.

Costs are not the same across assets. A HK name trades in a USDC pool on X
Layer with one venue's fee schedule; a US name trades in a USDG or USDT0 pool,
often with a different ticket size and a different spread. Treating one global
``RoundTripCosts`` as the answer for all of them is how a 20 bps bar gets
applied to a trade that costs 35 bps to round-trip.

So each ``[tokens.mappings."<venue>:<instrument>"]`` block may carry a ``cost``
sub-table. When it does not, the reviewed global value is used unchanged —
this module never invents a font of its own numbers, and it does not touch any
threshold: a cost record says what a round trip costs, not what profit is
required.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from decimal import Decimal, InvalidOperation

from arb_monitor.core.hk import RoundTripCosts

COST_FIELDS = tuple(f.name for f in fields(RoundTripCosts))
_DECIMAL_FIELDS = tuple(name for name in COST_FIELDS
                        if name not in {"verified", "valid_until_ms"})

__all__ = ["CostTable", "costs_from_spec", "COST_FIELDS"]


def costs_from_spec(base: RoundTripCosts, spec: dict,
                    *, verified: bool | None = None,
                    valid_until_ms: int | None = None) -> RoundTripCosts:
    """``base`` with the fields a mapping block overrides.

    Unknown keys are refused rather than ignored: a typo in a cost field would
    otherwise silently price the trade with the global default, which is the
    exact mistake this module exists to prevent.
    """
    raw = spec.get("cost") or {}
    if not isinstance(raw, dict):
        raise ValueError("mapping cost must be a table")
    unknown = sorted(set(raw) - set(COST_FIELDS))
    if unknown:
        raise ValueError(f"unknown cost fields: {', '.join(unknown)}")
    values: dict[str, object] = {}
    for name in _DECIMAL_FIELDS:
        if name not in raw:
            continue
        try:
            values[name] = Decimal(str(raw[name]))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"cost {name} is not a decimal: {raw[name]!r}") from exc
    if "verified" in raw:
        values["verified"] = bool(raw["verified"])
    if "valid_until_ms" in raw:
        values["valid_until_ms"] = int(raw["valid_until_ms"])
    out = replace(base, **values)
    if verified is not None:
        out = replace(out, verified=bool(verified))
    if valid_until_ms is not None:
        out = replace(out, valid_until_ms=int(valid_until_ms))
    return out


@dataclass(frozen=True)
class CostTable:
    """Costs looked up by the most specific key that exists."""

    default: RoundTripCosts
    #: keys: ``asset|venue|instrument``, ``venue:instrument`` or ``asset``
    overrides: dict[str, RoundTripCosts]

    def for_pair(self, asset: str, venue: str, instrument: str) -> RoundTripCosts:
        for key in (f"{asset}|{venue}|{instrument}", f"{venue}:{instrument}",
                    str(asset)):
            found = self.overrides.get(key)
            if found is not None:
                return found
        return self.default

    def is_override(self, asset: str, venue: str, instrument: str) -> bool:
        return self.for_pair(asset, venue, instrument) is not self.default

    def describe(self) -> list[dict]:
        return [
            {"key": key,
             "spot_fee_bps": str(costs.spot_fee_bps),
             "perp_fee_bps": str(costs.perp_fee_bps),
             "slippage_bps": str(costs.slippage_bps),
             "failure_bps": str(costs.failure_bps),
             "roundtrip_gas_budget_usdt": (None if costs.roundtrip_gas_budget_usdt is None
                                           else str(costs.roundtrip_gas_budget_usdt)),
             "verified": costs.verified,
             "valid_until_ms": costs.valid_until_ms}
            for key, costs in sorted(self.overrides.items())
        ]


def cost_table_from_tokens(default: RoundTripCosts,
                           tokens: list[dict]) -> CostTable:
    """Build the table from the configured token blocks.

    Only a block that actually carries a ``cost`` table becomes an override, so
    an uninteresting config produces an empty table and every lookup returns
    the reviewed global value.
    """
    overrides: dict[str, RoundTripCosts] = {}
    for token in tokens:
        asset = str(token.get("asset") or "")
        for key, spec in (token.get("mappings") or {}).items():
            if not isinstance(spec, dict) or "cost" not in spec:
                continue
            venue, _, instrument = str(key).partition(":")
            overrides[f"{asset}|{venue}|{instrument}"] = costs_from_spec(default, spec)
    return CostTable(default=default, overrides=overrides)
