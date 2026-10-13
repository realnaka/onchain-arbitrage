"""One place that assembles the live execution stack.

Both the interactive CLI (``arb_monitor full-chain``) and the unattended
executor need exactly the same objects, built exactly the same way: the OKX
read client, the trade client whose ``allow_orders`` flag is the last lock, the
X Layer wallet leg, the CLI runner behind it, and the ledger. Assembling them
twice is how two paths drift apart — one keeps the reviewed allowlist and the
other forgets to disable signing — so there is one builder and both call it.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from arb_monitor.apps.account_dashboard import load_toml, okx_read_env
from arb_monitor.execution.full_chain import FullChainRunner
from arb_monitor.execution.ledger import ExecutionLedger
from arb_monitor.execution.mev import MEVPolicy
from arb_monitor.execution.okx_account import OKXReadOnlyClient
from arb_monitor.execution.okx_trade import OKXTradeClient
from arb_monitor.execution.okx_wallet import OKXAgenticWalletAdapter
from arb_monitor.execution.onchain_wallet import OnchainWalletLeg
from arb_monitor.execution.wallet_cli import (
    OnchainOSRunner, wallet_addresses, wallet_balances,
)

EXECUTION_LEDGER = "data/execution.db"


class LiveStackError(RuntimeError):
    """The live stack cannot be assembled inside the reviewed policy."""


@dataclass
class LiveStack:
    """Everything one signed run needs, and nothing that outlives it."""

    config: dict
    policy: dict
    env: dict
    read: OKXReadOnlyClient
    runner: OnchainOSRunner
    wallet_address: str
    wallet_leg: OnchainWalletLeg
    trade: OKXTradeClient
    ledger: ExecutionLedger
    chain: FullChainRunner
    live: bool = False

    def balances(self) -> list[dict]:
        return wallet_balances(self.runner, "xlayer")

    def quote_balance(self, balances: list[dict] | None = None) -> Decimal | None:
        """How much of the allowlisted payment token the wallet can spend."""
        rows = balances if balances is not None else self.balances()
        policy_quote = ((self.policy.get("allowlist") or {}).get("quote_tokens") or [""])[0]
        for row in rows:
            if (row.get("token_address") or "").lower() == str(policy_quote).lower():
                return Decimal(str(row["balance"]))
        return None

    def open_contracts(self) -> Decimal:
        positions = self.read.positions()
        return Decimal(sum(
            (Decimal(str(row.get("pos") or 0)).copy_abs()
             for row in positions if Decimal(str(row.get("pos") or 0)) != 0),
            Decimal(0),
        ))

    def close(self) -> None:
        self.ledger.close()


def build_live_stack(*, config_path: str = "config/hk_monitor.toml",
                     policy_path: str = "config/automation_policy.toml",
                     okx_env: str = "config/okx-demo.env",
                     ledger_path: str = EXECUTION_LEDGER,
                     live: bool = False,
                     runner: Any = None) -> LiveStack:
    """Assemble the stack. ``live`` is the only switch that enables writes.

    Reads (quotes, balances, positions) always work; ``live=False`` produces a
    stack that can plan and read but whose trade client refuses every order and
    whose wallet adapter refuses every signature.
    """
    # The monitor merges signed_depth_mappings.toml into tokens. The executor
    # has to see the same list, or an allowlisted name such as CRWV is
    # "not in monitor config" at the moment of the order.
    from arb_monitor.apps.hk_monitor import load_config
    config = load_config(config_path)
    policy = load_toml(policy_path)
    env = okx_read_env(okx_env)
    # Signed GETs are idempotent, and the project's own policy already says so:
    # `[network] read_retries` (2) / `write_retries` (0). The executor reads the
    # position snapshot on every cycle, so a single-shot read turned one proxy
    # hiccup into a failed cycle — and, before 2026-09-21, into `AUTO_HALT`.
    network = policy.get("network") or {}
    read_retries = int(network.get("read_retries") or 0)
    # "direct" is how the config spells "no proxy"; the client wants None.
    routes = tuple(None if str(route) == "direct" else str(route)
                   for route in (network.get("routes") or []))
    read = OKXReadOnlyClient.from_env(env, read_retries=read_retries, routes=routes)
    if not read.configured:
        raise LiveStackError("okx_credentials_missing")
    proxy = env.get("ARB_HTTP_PROXY")
    cli = runner if runner is not None else OnchainOSRunner()
    try:
        wallet_address = wallet_addresses(cli, "xlayer")["address"]
    except Exception as exc:  # noqa: BLE001 - no wallet, no execution
        raise LiveStackError(f"wallet_address_unavailable:{type(exc).__name__}") from exc
    allowlisted = [str(x).lower()
                   for x in (policy.get("allowlist") or {}).get("wallets") or []]
    if allowlisted and wallet_address.lower() not in allowlisted:
        raise LiveStackError(f"wallet_not_allowlisted:{wallet_address}")
    adapter = OKXAgenticWalletAdapter(
        cli,
        wallet_address=wallet_address,
        chain_id="196",
        allowed_tokens=frozenset({
            str(x).lower() for x in (policy.get("allowlist") or {}).get("spot_tokens") or []
        } | {
            str(x).lower() for x in (policy.get("allowlist") or {}).get("quote_tokens") or []
        }),
        allowed_routers=frozenset(),   # filled from the quote's own router on first use
        allow_live_signing=live,
        max_slippage_pct=Decimal("0.8"),
        mev_policy=MEVPolicy(enabled=False, require_private_route=False),
        router_discovery=True,
    )
    wallet_leg = OnchainWalletLeg(
        wallet=adapter, address=wallet_address, chain_id="196",
        rpc_url=(config.get("onchain") or {}).get("rpc_url"), proxy=proxy,
        # The same runner the adapter uses. It lets the leg read the allowance
        # and the balance by itself, which is what the entry pre-flight needs.
        runner=cli)
    trade = OKXTradeClient(read=read, allow_orders=live)
    ledger = ExecutionLedger(ledger_path)
    chain = FullChainRunner(wallet_leg=wallet_leg, okx=trade, policy=policy,
                            monitor_config=config, ledger=ledger)
    return LiveStack(config=config, policy=policy, env=env, read=read, runner=cli,
                     wallet_address=wallet_address, wallet_leg=wallet_leg,
                     trade=trade, ledger=ledger, chain=chain, live=live)
