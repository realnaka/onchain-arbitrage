"""Deterministic network policy for exchange and on-chain read/write paths.

This module is intentionally mechanical: no model or human interpretation is
needed on the hot path. Ambiguous writes are never replayed.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NetworkRoute:
    name: str
    proxy: str | None
    timeout_seconds: int = 15


DEFAULT_ROUTES = (
    NetworkRoute("local_http_proxy", "http://127.0.0.1:4780"),
    NetworkRoute("local_socks_proxy", "socks5://127.0.0.1:4781"),
    NetworkRoute("direct", None),
)


def route_environment(route: NetworkRoute) -> dict[str, str]:
    """Return environment overrides for CLI/API subprocesses."""
    if route.proxy is None:
        return {}
    return {
        "ARB_HTTP_PROXY": route.proxy,
        "HTTP_PROXY": route.proxy,
        "HTTPS_PROXY": route.proxy,
    }


def retry_policy(*, write: bool, may_have_reached_remote: bool) -> str:
    """Return a deterministic action for a transport outcome."""
    if write and may_have_reached_remote:
        return "STOP_AND_QUERY_IDEMPOTENCY_KEY"
    if write:
        return "STOP_AND_ESCALATE"
    return "BOUNDED_READ_ONLY_RETRY"
