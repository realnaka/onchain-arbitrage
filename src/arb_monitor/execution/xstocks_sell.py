"""模块 6：在 Kraken 卖出 xStock。硬编码，LLM 不参与。

要点（全部来自 2026-10-05 的实测教训）：
  * xStocks **不支持 `oflags=post_only`**（Kraken 回 `EGeneral:Invalid arguments:oflags`）。
    要当 maker，只能把卖价挂在**买一之上**，这样不会吃单。
  * maker 有返佣（−2 bps），taker 要付 10 bps，所以默认先挂单，超时才吃单。
  * 价格闸门：卖出价不得低于基准价的 99.5%。
  * 盘口闸门：我们的数量不能把 50 bps 内的买盘吃掉超过 max_book_share。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING

from arb_monitor.execution.xstocks_buy import Policy
from arb_monitor.venues.kraken_exit_gate import book_exit


def quantize_up(value: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return value
    steps = (value / tick).to_integral_value(rounding=ROUND_CEILING)
    return (steps * tick).quantize(tick)


@dataclass
class SellPlan:
    symbol: str
    tokens: Decimal
    ref: Decimal
    gate_price: Decimal
    best_bid: Decimal
    best_ask: Decimal
    maker_price: Decimal | None
    book: dict
    ok: bool
    reason: str
    taker_allowed: bool = False
    orders: list = field(default_factory=list)
    fill: dict | None = None


def plan_sell(policy: Policy, *, symbol: str, tokens: str, ref: Decimal,
              bids, asks, pair_status: str, tick: Decimal = Decimal("0.01"),
              order_min: Decimal | None = None) -> SellPlan:
    """只做计算，不碰网络。判定不过就返回 ok=False 与原因。"""
    tokens_dec = Decimal(str(tokens))
    levels = [(Decimal(str(r[0])), Decimal(str(r[1]))) for r in bids or []]
    best_bid = levels[0][0] if levels else Decimal(0)
    best_ask = Decimal(str(asks[0][0])) if asks else Decimal(0)
    # 闸门向上取整：宁可要求更高的价，也不让成交价掉到 99.5% 以下。
    gate = (ref * policy.min_price_vs_ref).quantize(Decimal("0.0001"),
                                                    rounding=ROUND_CEILING)
    book = book_exit(bids, tokens_dec)

    def fail(reason: str) -> SellPlan:
        return SellPlan(symbol, tokens_dec, ref, gate, best_bid, best_ask, None,
                        book, False, reason)

    if pair_status not in ("online", "post_only"):
        return fail(f"Kraken 状态 {pair_status or '未知'}")
    if order_min is not None and tokens_dec < order_min:
        return fail(f"数量 {tokens_dec} 低于 Kraken 最小 {order_min}")
    if not levels:
        return fail("盘口为空")
    if not book["ok"]:
        return fail(f"盘口吃不下：{book['reason']}")

    # maker 价：必须严格高于买一（否则会吃单），且仍然 ≥ 闸门价。
    # 买一低于闸门不影响「挂得更高」这条出路，所以闸门只约束实际成交价。
    maker = max(quantize_up(best_bid + tick, tick), quantize_up(gate, tick))
    if best_ask and maker >= best_ask:
        maker = None                      # 挂上去会变成吃单，放弃 maker
    taker_allowed = best_bid >= gate      # 吃买一也能过闸门
    if maker is None and not taker_allowed:
        return fail(f"既挂不上也吃不到 ≥ {gate} 的价（买一 {best_bid}）")
    return SellPlan(symbol, tokens_dec, ref, gate, best_bid, best_ask, maker,
                    book, True, "通过", taker_allowed=taker_allowed)


def run_sell(client, policy: Policy, plan: SellPlan, *, confirm: str = "",
             clock=time.time, sleep=time.sleep) -> SellPlan:
    """执行：先挂 maker，超时后（仍过闸门才）吃买一。confirm="live" 才会真下单。"""
    if not plan.ok:
        return plan
    if plan.maker_price is None:
        if not plan.taker_allowed:
            plan.ok = False
            plan.reason = "没有可用的 maker 价，且吃买一不过闸门"
            return plan
        plan.orders.append(_order(client, plan, plan.best_bid, confirm))
        plan.fill = _settle(client, plan, clock, sleep)
        return plan

    plan.orders.append(_order(client, plan, plan.maker_price, confirm))
    deadline = clock() + policy.maker_timeout_s
    while clock() < deadline:
        sleep(5)
        if _asset_gone(client, plan.symbol):
            plan.reason = "maker 成交"
            plan.fill = {"price": str(plan.maker_price), "kind": "maker"}
            return plan
    _cancel_all(client, plan)
    bid = _best_bid(client, plan.symbol)
    # 用**重读的**买一重新过闸门，不看挂单前那个过期的 taker_allowed。
    if bid is not None and bid >= plan.gate_price:
        plan.orders.append(_order(client, plan, bid, confirm))
        plan.fill = _settle(client, plan, clock, sleep)
        plan.reason = "maker 超时，改吃买一"
    else:
        plan.ok = False
        plan.reason = "maker 超时，买一已低于闸门，不卖"
    return plan


def _order(client, plan: SellPlan, price: Decimal, confirm: str) -> dict:
    return client.order(plan.symbol, str(plan.tokens), side="sell", ordertype="limit",
                        price=f"{price:.2f}", oflags="fciq", confirm=confirm)


def _asset_gone(client, symbol: str) -> bool:
    try:
        balance = client.balance()
    except Exception:  # noqa: BLE001
        return False
    base = f"{symbol}.T"
    held = balance.get(base) or balance.get(symbol) or Decimal(0)
    return Decimal(held) == 0


def _cancel_all(client, plan: SellPlan) -> None:
    try:
        for order in client.open_orders():
            descr = (order.get("descr") or {}).get("pair", "")
            if descr.startswith(plan.symbol):
                client._post("/0/private/CancelOrder", {"txid": order.get("id")})
    except Exception:  # noqa: BLE001
        pass


def _best_bid(client, symbol: str) -> Decimal | None:
    try:
        rows = client.depth(symbol)
    except Exception:  # noqa: BLE001
        return None
    bids = (rows or {}).get("bids") or []
    return Decimal(str(bids[0][0])) if bids else None


def _settle(client, plan: SellPlan, clock, sleep, tries: int = 6) -> dict | None:
    for _ in range(tries):
        sleep(5)
        if _asset_gone(client, plan.symbol):
            return {"kind": "filled"}
    return None
