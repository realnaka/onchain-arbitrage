"""Concrete OnchainOS CLI runner for reads, quotes and TEE contract calls.

The runner is a thin, testable boundary: it spawns the already-authenticated
CLI, injects the reviewed proxy routes, parses JSON and raises on anything that
is not a clean success. It never sees a private key - signing stays inside the
wallet service - and it never retries a write.

Route order is the reviewed one and is fixed in code:
``http://127.0.0.1:4780`` → ``socks5://127.0.0.1:4781`` → direct.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .network_policy import DEFAULT_ROUTES, NetworkRoute, route_environment


class WalletCliError(RuntimeError):
    """The CLI call did not produce a usable answer."""


def _default_binary() -> str:
    return (os.environ.get("ARB_ONCHAINOS_BIN")
            or shutil.which("onchainos")
            or os.path.expanduser("~/.local/bin/onchainos"))


@dataclass
class OnchainOSRunner:
    """Callable runner suitable for ``OKXAgenticWalletAdapter``."""

    binary: str = ""
    routes: tuple[NetworkRoute, ...] = DEFAULT_ROUTES
    read_timeout: int = 30
    write_timeout: int = 90
    run: Any = None       # injectable subprocess.run for tests
    environ: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        self.binary = self.binary or _default_binary()
        self.run = self.run or subprocess.run

    @staticmethod
    def is_write(argv: Sequence[str]) -> bool:
        """Writes may have reached the remote side; they are never replayed."""
        lowered = [str(x).lower() for x in argv]
        if "quote" in lowered or "balance" in lowered or "addresses" in lowered:
            return False
        return True

    def __call__(self, argv: Sequence[str]) -> dict[str, Any]:
        command = [self.binary, *[str(x) for x in argv]]
        write = self.is_write(argv)
        timeout = self.write_timeout if write else self.read_timeout
        base_env = dict(self.environ if self.environ is not None else os.environ)
        failures = []
        # Reads may try every reviewed route. A write uses the first route only:
        # a timeout there means the state is unknown, not "try again elsewhere".
        routes = list(self.routes) if not write else list(self.routes[:1])
        for route in routes:
            env = {**base_env, **route_environment(route)}
            try:
                completed = self.run(command, capture_output=True, text=True,
                                     timeout=timeout, env=env)
            except subprocess.TimeoutExpired:
                failures.append(f"{route.name}:timeout")
                continue
            except OSError as exc:
                failures.append(f"{route.name}:{type(exc).__name__}")
                continue
            stdout = (completed.stdout or "").strip()
            parsed = _parse_json(stdout)
            if completed.returncode != 0:
                # A JSON body is an answer, not a dead route: only transport
                # failures are worth trying down the next route.
                if parsed is None or is_route_failure(parsed.get("error", "")):
                    failures.append(f"{route.name}:exit{completed.returncode}")
                    if write:
                        raise WalletCliError(
                            "write_failed_unknown_state:"
                            + (completed.stderr or stdout)[-200:]
                        )
                    continue
                parsed.setdefault("_route", route.name)
                return parsed
            if parsed is None:
                failures.append(f"{route.name}:unparsable_output")
                continue
            if parsed.get("ok") is False:
                failures.append(f"{route.name}:api_error")
                continue
            parsed.setdefault("_route", route.name)
            return parsed
        raise WalletCliError("all_routes_failed:" + ",".join(failures))


def _parse_json(text: str) -> dict[str, Any] | None:
    """The CLI prints one JSON object per call; tolerate leading log lines."""
    for candidate in (text, *reversed(text.splitlines())):
        candidate = candidate.strip()
        if not candidate.startswith("{"):
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def is_route_failure(error: str) -> bool:
    """Did this failure come from *our* route, or from the venue answering?

    Both arrive as JSON with exit code 1, and they need opposite handling. A
    transport failure ("Network unavailable", "Failed to fetch ...") means this
    route is unusable and the next reviewed route should be tried. Anything else
    — a rate limit, an API error, a validation refusal — is the venue talking,
    and re-sending it down another route just repeats the same answer.
    """
    text = str(error or "").lower()
    markers = ("network unavailable", "failed to fetch", "connection", "timed out",
               "timeout", "no route to host", "dns")
    return any(marker in text for marker in markers)


def wallet_addresses(runner: OnchainOSRunner, chain: str = "xlayer") -> dict[str, str]:
    """Address for one chain from the CLI's per-chain payload shape."""
    payload = runner(("wallet", "addresses", "--chain", chain))
    data = payload.get("data") or {}
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        raise WalletCliError("wallet_address_not_returned")
    # The CLI groups addresses by chain ("xlayer": [{"address": ..., ...}]).
    candidates = [data.get(chain), data.get("addresses"), data.get("evm")]
    candidates += [value for value in data.values() if isinstance(value, list)]
    for group in candidates:
        if not isinstance(group, list):
            continue
        for item in group:
            if isinstance(item, dict) and item.get("address"):
                return {"address": str(item["address"]), "chain": chain,
                        "chain_index": item.get("chainIndex")}
    for key in ("address", "evmAddress", "walletAddress"):
        if data.get(key):
            return {"address": str(data[key]), "chain": chain}
    raise WalletCliError("wallet_address_not_returned")


def wallet_balances(runner: OnchainOSRunner, chain: str = "xlayer") -> list[dict]:
    """Normalised token balances. USD valuation is the venue's, not ours."""
    payload = runner(("wallet", "balance", "--chain", chain))
    data = payload.get("data") or {}
    details = data.get("details") if isinstance(data, dict) else None
    rows: list[dict] = []
    for group in details or []:
        for asset in (group.get("tokenAssets") or []):
            rows.append({
                "symbol": asset.get("symbol"),
                "token_address": (asset.get("tokenAddress") or "").lower() or None,
                "balance": asset.get("balance"),
                "raw_balance": asset.get("rawBalance"),
                "decimals": asset.get("decimal"),
                "price_usd": asset.get("tokenPrice"),
                "usd_value": asset.get("usdValue"),
                "chain_index": asset.get("chainIndex"),
            })
    return rows


def wallet_history(runner: OnchainOSRunner, chain: str = "xlayer",
                   limit: int = 20) -> list[dict]:
    payload = runner(("wallet", "history", "--chain", chain, "--limit", str(int(limit))))
    data = payload.get("data") or {}
    if isinstance(data, list):
        orders: list[dict] = []
        for block in data:
            orders.extend(block.get("orderList") or [])
        return orders
    if isinstance(data, dict):
        orders = list(data.get("orderList") or [])
        for block in data.get("data") or []:
            orders.extend(block.get("orderList") or [])
        return orders
    return []
