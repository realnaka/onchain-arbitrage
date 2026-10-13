"""Load ops/xstocks_v2_policy.toml. Thresholds do not live in code."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
POLICY_PATH = ROOT / "ops" / "xstocks_v2_policy.toml"


def _d(value) -> Decimal:
    return Decimal(str(value))


@dataclass(frozen=True)
class V2Policy:
    per_trade_quote_usdt: Decimal
    min_trade_quote_usdt: Decimal
    min_net_profit_usdt: Decimal
    #: Slippage tolerance handed to the venue's own quote call (bps). Declared in
    #: ``[trade]`` since 2026-10-07 but never read: ``execute_cycle`` asked for it
    #: and raised ``AttributeError`` the first time a chain -> Kraken plan really
    #: reached the executor (measured 2026-10-08).
    max_slippage_bps: int
    min_net_edge_bps: Decimal
    min_net_edge_bps_open: Decimal
    min_net_edge_bps_closed: Decimal
    session_split: bool
    #: The sell leg's floor relative to the price the decision used. Same number
    #: as v1's ``ops/xstocks_policy.toml`` (0.995). It exists because the sell
    #: primitive reads it: on 2026-10-08 a filled buy sat unsold on Kraken
    #: because ``V2Policy`` lacked the field and the leg raised AttributeError.
    min_price_vs_ref: Decimal
    stage2_buffer_bps: Decimal
    stage2_max: int
    max_age_ms: int
    max_pair_skew_ms: int
    dead_pool_confirm_hits: int
    dead_pool_ttl_s: int
    dead_pool_recheck_max: int
    #: Stage-2 slots reserved for the Solana rails that have waited the longest.
    #: 0 keeps the old behaviour: the ranking spends every slot.
    rotation_slots: int
    jupiter_rps: Decimal
    jsonrpc_batch_max: int
    full_scan_timeout_s: int
    kraken_taker_bps: Decimal
    slippage_buffer_bps: Decimal
    sol_transfer_first: Decimal
    sol_transfer_repeat: Decimal
    evm_gas_units: Decimal
    max_depth_share: Decimal
    venue_round_trip_max_bps: Decimal
    max_price_impact_bps: Decimal
    max_price_outlier_bps: Decimal
    phase: str
    mode: str
    auto_orders: str
    live_ack: str
    eth_usdc_floor_usdt: Decimal
    eth_gas_floor_eth: Decimal
    credit_mismatch_ratio: Decimal
    #: Sample-probe budget (operator 2026-10-09: 缺样本不是拒绝理由，但要走最小额，
    #: 默认 3 次/天、每次 ≤2 USDT). 0 / empty means the sample stage is closed --
    #: a missing cap is a refusal, never "unlimited" (see ``probe.budget_decide``).
    probe_max_per_day: int
    probe_max_usdt: Decimal

    @classmethod
    def load(cls, path: Path | str = POLICY_PATH) -> "V2Policy":
        raw = tomllib.loads(Path(path).read_text())
        trade, watch = raw["trade"], raw["watch"]
        execution, capital = raw["execution"], raw["capital"]
        feeds = raw.get("feeds") or {}
        costs = raw.get("costs") or {}
        probe_cfg = raw.get("probe") or {}
        return cls(
            per_trade_quote_usdt=_d(trade["per_trade_quote_usdt"]),
            # The floor is the *smallest ticket worth doing*, so it can never be
            # larger than the ticket itself -- if it were, every pool would read
            # as too thin and the board would go silent (fail closed, but blind).
            min_trade_quote_usdt=min(
                _d(trade.get("min_trade_quote_usdt", "25")),
                _d(trade["per_trade_quote_usdt"])),
            min_net_profit_usdt=_d(trade["min_net_profit_usdt"]),
            max_slippage_bps=int(trade.get("max_slippage_bps", 10)),
            min_net_edge_bps=_d(trade["min_net_edge_bps"]),
            min_net_edge_bps_open=_d(trade.get("min_net_edge_bps_open",
                                               trade["min_net_edge_bps"])),
            min_net_edge_bps_closed=_d(trade.get("min_net_edge_bps_closed",
                                                 trade["min_net_edge_bps"])),
            session_split=bool(trade["session_split"]),
            min_price_vs_ref=_d(trade.get("min_price_vs_ref", "0.995")),
            stage2_buffer_bps=_d(trade["stage2_buffer_bps"]),
            stage2_max=int(trade["stage2_max"]),
            max_age_ms=int(watch["max_age_ms"]),
            max_pair_skew_ms=int(watch["max_pair_skew_ms"]),
            dead_pool_confirm_hits=int(watch.get("dead_pool_confirm_hits", 2)),
            dead_pool_ttl_s=int(watch.get("dead_pool_ttl_s", 3600)),
            dead_pool_recheck_max=int(watch.get("dead_pool_recheck_max", 4)),
            rotation_slots=max(0, int(watch.get("rotation_slots", 0))),
            jupiter_rps=_d(feeds.get("jupiter_rps", "0.9")),
            jsonrpc_batch_max=int(feeds.get("jsonrpc_batch_max", 10)),
            full_scan_timeout_s=int(feeds.get("full_scan_timeout_s", 900)),
            kraken_taker_bps=_d(costs.get("kraken_taker_bps", "10")),
            slippage_buffer_bps=_d(costs.get("slippage_buffer_bps", "10")),
            sol_transfer_first=_d(costs.get("sol_transfer_first", "0.00313")),
            sol_transfer_repeat=_d(costs.get("sol_transfer_repeat", "0.000015")),
            evm_gas_units=_d(costs.get("evm_gas_units", "441831")),
            max_depth_share=_d(costs.get("max_depth_share", "0.2")),
            venue_round_trip_max_bps=_d(costs.get("venue_round_trip_max_bps", "150")),
            max_price_impact_bps=_d(costs.get("max_price_impact_bps", "300")),
            max_price_outlier_bps=_d(costs.get("max_price_outlier_bps", "3000")),
            phase=str(execution["phase"]),
            mode=str(execution["mode"]),
            auto_orders=str(execution["auto_orders"]),
            live_ack=str(execution.get("live_ack") or ""),
            eth_usdc_floor_usdt=_d(capital["eth_usdc_floor_usdt"]),
            eth_gas_floor_eth=_d(capital["eth_gas_floor_eth"]),
            credit_mismatch_ratio=_d(capital["credit_mismatch_ratio"]),
            probe_max_per_day=max(0, int(probe_cfg.get("max_per_day", 0))),
            probe_max_usdt=_d(probe_cfg.get("max_usdt_per_probe", "0")),
        )

    def required_edge_bps(self, session_label: str) -> Decimal:
        """The net spread this session has to clear.

        Operator 2026-10-07: the regular US session needs 20 bps, closed hours
        need 4 bps. Premarket, after-hours and the night session count as
        closed, per the same instruction. A date the calendar cannot place is
        ``calendar_unknown`` and takes the **stricter** side, because that is
        the answer we cannot check.
        """
        if not self.session_split:
            return self.min_net_edge_bps
        if session_label == "regular":
            return self.min_net_edge_bps_open
        if session_label in {"premarket", "after_hours", "closed"}:
            return self.min_net_edge_bps_closed
        return max(self.min_net_edge_bps_open, self.min_net_edge_bps_closed)

    def live_broadcast_allowed(self) -> tuple[bool, str]:
        if self.phase == "scan":
            return False, "phase=scan"
        if self.auto_orders != "live" or self.mode != "live":
            return False, "live 开关未开"
        if not self.live_ack.strip():
            return False, "live_ack 为空"
        return True, "live 已授权"
