from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping


@dataclass(frozen=True)
class Reconciliation:
    expected_token_quantity: Decimal
    actual_token_quantity: Decimal
    expected_perp_quantity: Decimal
    actual_perp_quantity: Decimal
    token_delta: Decimal
    perp_delta: Decimal
    balanced: bool
    reason: str = ""


def reconcile(
    expected_token: Decimal,
    actual_token: Decimal,
    expected_perp: Decimal,
    actual_perp: Decimal,
    tolerance: Decimal = Decimal("0.000001"),
) -> Reconciliation:
    token_delta = actual_token - expected_token
    perp_delta = actual_perp - expected_perp
    balanced = abs(token_delta) <= tolerance and abs(perp_delta) <= tolerance
    return Reconciliation(
        expected_token, actual_token, expected_perp, actual_perp,
        token_delta, perp_delta, balanced,
        "" if balanced else "quantity_delta_exceeds_tolerance",
    )


@dataclass(frozen=True)
class BalanceReconciliation:
    expected: dict[str, Decimal]
    actual: dict[str, Decimal]
    deltas: dict[str, Decimal]
    balanced: bool
    reason: str = ""


def reconcile_balances(
    expected: Mapping[str, Decimal],
    actual: Mapping[str, Decimal],
    tolerance: Decimal = Decimal("0.000001"),
) -> BalanceReconciliation:
    currencies = set(expected) | set(actual)
    deltas = {ccy: Decimal(actual.get(ccy, 0)) - Decimal(expected.get(ccy, 0))
              for ccy in currencies}
    balanced = all(abs(delta) <= tolerance for delta in deltas.values())
    return BalanceReconciliation(
        {k: Decimal(v) for k, v in expected.items()},
        {k: Decimal(v) for k, v in actual.items()},
        deltas, balanced,
        "" if balanced else "balance_delta_exceeds_tolerance",
    )
