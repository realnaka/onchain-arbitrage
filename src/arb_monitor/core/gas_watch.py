"""Watch whether X Layer is still paying our gas.

``roundtrip_gas_budget_usdt = "0"`` in the cost model is not an assumption
about the protocol; it is an observation with a date. It holds because the
transactions we sent came back with ``effectiveGasPrice=0`` and ``l1Fee=0``.
That can stop at any time, and the moment it does, a cost model with a zero
gas budget is understating every trade.

So this module re-reads real receipts from our own transactions and answers one
question: has anything we sent actually paid gas? It never trusts the config,
and it never predicts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping

WEI_PER_NATIVE = Decimal(10) ** 18


@dataclass(frozen=True)
class GasSample:
    tx_hash: str
    status: str                 # filled | reverted | unknown
    gas_used: int = 0
    effective_gas_price_wei: int = 0
    l1_fee_wei: int = 0
    swap_path: bool = False     # produced by our own swap/approve execution

    @property
    def paid_native(self) -> Decimal:
        return (Decimal(self.effective_gas_price_wei) * Decimal(self.gas_used)
                + Decimal(self.l1_fee_wei)) / WEI_PER_NATIVE

    @property
    def paid(self) -> bool:
        return self.status == "filled" and self.paid_native > 0

    def as_dict(self) -> dict:
        return {"tx_hash": self.tx_hash, "status": self.status,
                "gas_used": self.gas_used,
                "effective_gas_price_wei": str(self.effective_gas_price_wei),
                "l1_fee_wei": str(self.l1_fee_wei),
                "paid_native": str(self.paid_native), "paid": self.paid,
                "swap_path": self.swap_path}


@dataclass
class GasObservation:
    status: str                 # sponsored | paid | paid_other | unknown
    checked: int
    paid_count: int
    samples: list[GasSample] = field(default_factory=list)
    note: str = ""

    @property
    def swap_path_paid(self) -> list[GasSample]:
        return [sample for sample in self.samples if sample.swap_path and sample.paid]

    @property
    def other_paid(self) -> list[GasSample]:
        return [sample for sample in self.samples if not sample.swap_path and sample.paid]

    @property
    def worst_paid_native(self) -> Decimal:
        return max((sample.paid_native for sample in self.samples), default=Decimal(0))

    def as_dict(self) -> dict:
        return {
            "status": self.status, "checked": self.checked,
            "paid_count": self.paid_count,
            "swap_path_paid_count": len(self.swap_path_paid),
            "worst_paid_native": str(self.worst_paid_native),
            "note": self.note,
            "samples": [sample.as_dict() for sample in self.samples],
        }


def observe_gas(rpc, tx_hashes: Iterable[str], *, limit: int = 10,
                swap_path_hashes: Iterable[str] | None = None) -> GasObservation:
    """Read the receipts of our own transactions and report who paid the gas.

    Two classes of transaction matter differently. A gas charge on a *swap or
    approval we sent* falsifies the ``gas=0`` budget the cost model uses. A
    charge on some other transaction (a transfer, a flagged transaction) is
    worth surfacing but is not evidence that the trade path lost sponsorship,
    and reporting it as if it were would train the operator to ignore the
    banner.
    """
    swap_path = {str(x).lower() for x in (swap_path_hashes or ())}
    samples: list[GasSample] = []
    errors = 0
    for tx_hash in list(tx_hashes)[:limit]:
        if not tx_hash:
            continue
        try:
            receipt = rpc.receipt(tx_hash)
        except Exception:  # noqa: BLE001 - an unreadable receipt is a gap, not a price
            errors += 1
            samples.append(GasSample(tx_hash=str(tx_hash), status="unknown",
                                     swap_path=str(tx_hash).lower() in swap_path))
            continue
        if not receipt:
            samples.append(GasSample(tx_hash=str(tx_hash), status="unknown",
                                     swap_path=str(tx_hash).lower() in swap_path))
            continue
        status = "filled" if str(receipt.get("status")) == "0x1" else "reverted"
        def _int(key: str) -> int:
            value = receipt.get(key) or "0x0"
            try:
                return int(str(value), 16)
            except (TypeError, ValueError):
                return 0
        samples.append(GasSample(
            tx_hash=str(tx_hash), status=status, gas_used=_int("gasUsed"),
            effective_gas_price_wei=_int("effectiveGasPrice"),
            l1_fee_wei=_int("l1Fee"),
            swap_path=str(tx_hash).lower() in swap_path))
    paid = [sample for sample in samples if sample.paid]
    swap_paid = [sample for sample in paid if sample.swap_path]
    other_paid = [sample for sample in paid if not sample.swap_path]
    if swap_paid:
        status, note = "paid", (
            f"{len(swap_paid)} 笔**swap/授权**交易实际支付了 gas —— "
            f"X Layer 的赞助已不适用于我们的交易路径，成本模型里的 gas=0 不再成立")
    elif other_paid:
        status, note = "paid_other", (
            f"{len(other_paid)} 笔非交易类交易支付了 gas（最大 "
            f"{max(sample.paid_native for sample in other_paid)} 原生币）；"
            f"我们的 swap/授权路径实测仍为 0，但值得盯住")
    elif samples and all(sample.status == "unknown" for sample in samples):
        status, note = "unknown", "所有回执都读不到，无法判断是否仍在赞助"
    elif not samples:
        status, note = "unknown", "没有可核对的本钱包交易"
    else:
        swap_count = sum(1 for sample in samples if sample.swap_path)
        status, note = "sponsored", (
            f"{len(samples)} 笔交易 effectiveGasPrice/l1Fee 均为 0"
            + (f"（其中 {swap_count} 笔是我们的 swap/授权）" if swap_count else ""))
    return GasObservation(status=status, checked=len(samples), paid_count=len(paid),
                          samples=samples, note=note)
