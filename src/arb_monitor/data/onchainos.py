"""The only invoked CLI action is `swap quote` (read-only).

Authorization errors are measurement failures, never evidence that liquidity
has disappeared. Unknown successful response schemas also fail closed.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from decimal import Decimal

from arb_monitor.core.hk import SpotQuote, decimal
from arb_monitor.data.hk_cex import now_ms


class QuoteChannelHealth:
    """Shared, cross-probe view of whether the quote channel is answering.

    Each probe retries a transient failure on its own, which is right when one
    probe is unlucky and ruinous when the channel itself is down. Measured
    2026-09-21: 16 probes x (1 + ``quote_retries``) attempts with exponential
    backoff is how a grid that should take 6 s took 16-27 s against a 15 s
    freshness budget, and every one of those retries was aimed at a proxy that
    had already answered "Network unavailable" to the first probe.

    This is the shared memory that stops it. It counts *transport* failures
    across probes and cycles, opens a cooldown once the channel is clearly not
    there, and — while it recovers — allows exactly one probe through so the
    next cycle learns the answer instead of a whole grid rediscovering it.

    It is not a valve on quality: a refused probe is recorded as *not sampled*
    for this round, never as a price, and never as a filled-in previous quote.
    """

    OPEN_STATES = ("healthy", "open", "recovering")

    def __init__(self, *, failure_threshold: int = 3, cooldown_ms: int = 30_000,
                 cooldown_max_ms: int = 300_000, clock=now_ms):
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_ms = max(0, int(cooldown_ms))
        self.cooldown_max_ms = max(self.cooldown_ms, int(cooldown_max_ms))
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._open_until = 0
        self._wait = 0
        self._last_reason = ""
        self._probe_in_flight = False
        self.refused = 0
        self.succeeded = 0

    def admit(self) -> str | None:
        """``None`` to go ahead, or the reason this probe is not being sent."""
        now = self._clock()
        with self._lock:
            if self._open_until and now < self._open_until:
                self.refused += 1
                return f"quote_channel_open:{self._last_reason}"
            recovering = self._failures >= self.failure_threshold
            if recovering and self._probe_in_flight:
                self.refused += 1
                return f"quote_channel_probe_in_flight:{self._last_reason}"
            if recovering:
                self._probe_in_flight = True
            return None

    def note(self, ok: bool, reason: str = "") -> None:
        """Record what the channel just answered."""
        now = self._clock()
        with self._lock:
            self._probe_in_flight = False
            if ok:
                self._failures = 0
                self._open_until = 0
                self._wait = 0
                self._last_reason = ""
                self.succeeded += 1
                return
            self._failures += 1
            self._last_reason = str(reason)[:120]
            if self._failures < self.failure_threshold:
                return
            self._wait = (self.cooldown_ms if not self._wait
                          else min(self._wait * 2, self.cooldown_max_ms))
            self._open_until = now + self._wait

    def retry_allowed(self) -> bool:
        """Whether another attempt is worth its backoff right now."""
        now = self._clock()
        with self._lock:
            if self._open_until and now < self._open_until:
                return False
            return self._failures < self.failure_threshold

    def snapshot(self) -> dict:
        now = self._clock()
        with self._lock:
            if self._open_until and now < self._open_until:
                state = "open"
            elif self._failures >= self.failure_threshold:
                state = "recovering"
            else:
                state = "healthy"
            return {"state": state,
                    "consecutive_failures": self._failures,
                    "open_until_ms": self._open_until or None,
                    "retry_in_ms": max(0, self._open_until - now) if self._open_until else 0,
                    "last_reason": self._last_reason or None,
                    "refused": self.refused,
                    "succeeded": self.succeeded}


def route_names(value: object) -> tuple[str, ...]:
    found: set[str] = set()
    def visit(node: object) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("dexName", "dexId") and isinstance(v, (str, int)):
                    found.add(str(v))
                elif isinstance(v, (dict, list)):
                    visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)
    visit(value)
    return tuple(sorted(found))


def parse_quote(
    raw: dict, token: dict, quote_token: dict, side: str, amount: Decimal,
    started: int, received: int, ttl_ms: int,
    mode: str = "exactIn",
) -> SpotQuote:
    common = dict(
        asset=token["asset"], side=side, chain_id=token["chain_id"],
        token_address=token["address"], quote_address=quote_token["address"],
        quote_currency=quote_token["symbol"], requested_amount=amount,
        request_ts_ms=started, recv_ts_ms=received,
        expires_ts_ms=started + ttl_ms, source="onchainos:swap quote", raw=raw,
    )
    error = str(raw.get("error") or raw.get("msg") or "")
    code = str(raw.get("code", ""))
    match = re.search(r"code[=:]\s*(\d+)", error)
    if match:
        code = match[1]
    if raw.get("ok") is False or code not in ("", "0"):
        status = "auth_error" if code in {"50114", "50103", "50104", "50105", "50106", "50111"} else "api_error"
        return SpotQuote(**common, token_quantity=None, quote_amount=None,
                         status=status, error_code=code, error=error[:300])
    try:
        data = raw["data"]
        if isinstance(data, list):
            if len(data) != 1:
                raise ValueError("ambiguous quote result")
            data = data[0]
        if not isinstance(data, dict):
            # A funding/shortfall answer is not a price.
            raise ValueError("quote_unavailable_or_funding_required")
        from_token, to_token = data["fromToken"], data["toToken"]
        expected_from = quote_token if side == "buy" else token
        expected_to = token if side == "buy" else quote_token
        for actual, expected in ((from_token, expected_from), (to_token, expected_to)):
            if actual["tokenContractAddress"].lower() != expected["address"].lower():
                raise ValueError("quote token address mismatch")
            if int(actual["decimal"]) != expected["decimals"]:
                raise ValueError("quote decimals mismatch")
        if str(data.get("chainId", data.get("chainIndex", token["chain_id"]))) != token["chain_id"]:
            raise ValueError("quote chain mismatch")
        from_units, to_units = decimal(data["fromTokenAmount"]), decimal(data["toTokenAmount"])
        if from_units != from_units.to_integral_value() or to_units != to_units.to_integral_value():
            raise ValueError("fractional minimal token units")
        from_amount = from_units / Decimal(10) ** expected_from["decimals"]
        to_amount = to_units / Decimal(10) ** expected_to["decimals"]
        if mode == "exactOut":
            # The requested side is the output: it must be delivered exactly.
            if to_amount != amount:
                raise ValueError("exactOut delivered quantity mismatch")
        elif from_amount != amount or min(from_amount, to_amount) <= 0:
            raise ValueError("exactIn quote size mismatch or zero amount")
        if min(from_amount, to_amount) <= 0:
            raise ValueError("zero amount in quote")
        if mode == "exactOut":
            # buy: pay from_amount, receive exactly `amount` tokens.
            token_qty, quote_amt = amount, from_amount
        else:
            token_qty, quote_amt = ((to_amount, from_amount) if side == "buy"
                                    else (from_amount, to_amount))
        # estimateGasFee is often native-chain units. NEVER call that USDC.
        # A documented quote-currency gas field is required for a qualified Intent.
        return SpotQuote(
            **common, token_quantity=token_qty, quote_amount=quote_amt, status="ok",
            routes=route_names(data.get("dexRouterList", [])),
            gas_quote=None,
            price_impact_pct=decimal(data["priceImpactPercentage"])
                if data.get("priceImpactPercentage") not in ("", None) else None,
        )
    except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
        return SpotQuote(**common, token_quantity=None, quote_amount=None,
                         status="schema_error", error=str(exc)[:300])


class OnchainOSQuoteAdapter:
    def __init__(self, proxy: str | None = None, ttl_ms: int = 10000, executable: str = "onchainos"):
        self.proxy, self.ttl_ms, self.executable = proxy, ttl_ms, executable

    def quote(self, token: dict, quote_token: dict, side: str, amount: Decimal) -> SpotQuote:
        if side not in ("buy", "sell"):
            raise ValueError("side must be buy/sell")
        from_token, to_token = (quote_token, token) if side == "buy" else (token, quote_token)
        units = amount * Decimal(10) ** from_token["decimals"]
        if amount <= 0 or units != units.to_integral_value():
            raise ValueError("amount must be positive and exactly representable in token units")
        args = [
            self.executable, "swap", "quote", "--chain", token["chain_id"],
            "--from", from_token["address"], "--to", to_token["address"],
            "--amount", str(int(units)), "--swap-mode", "exactIn",
        ]
        started = now_ms()
        env = os.environ.copy()
        if self.proxy:
            env.update(HTTPS_PROXY=self.proxy, HTTP_PROXY=self.proxy)
        try:
            proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=20)
            raw = json.loads(proc.stdout, parse_float=Decimal)
            if proc.returncode and raw.get("ok") is not False:
                raw = {"ok": False, "error": f"CLI_exit:{proc.returncode}"}
        except (OSError, subprocess.TimeoutExpired, ValueError):
            raw = {"ok": False, "error": "quote_transport_or_cli_error"}
        return parse_quote(raw, token, quote_token, side, amount, started, now_ms(), self.ttl_ms)

    def quote_exact_out(self, token: dict, quote_token: dict,
                        share_quantity: Decimal) -> SpotQuote:
        """Price an exact token amount, in tokens not cash.

        The amount has to be an integer number of token units and has to convert
        to a legal contract size (at least minSz, a multiple of lotSz). That
        size is often a fraction of one share. A cash-sized probe is not used,
        because the token amount it returns is not chosen to land on the
        contract grid.
        """
        units = share_quantity * Decimal(10) ** int(token["decimals"])
        if share_quantity <= 0 or units != units.to_integral_value():
            raise ValueError("share count must be positive and exactly representable")
        args = [
            self.executable, "swap", "quote", "--chain", token["chain_id"],
            "--from", quote_token["address"], "--to", token["address"],
            "--amount", str(int(units)), "--swap-mode", "exactOut",
        ]
        started = now_ms()
        env = os.environ.copy()
        if self.proxy:
            env.update(HTTPS_PROXY=self.proxy, HTTP_PROXY=self.proxy)
        try:
            proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=20)
            raw = json.loads(proc.stdout, parse_float=Decimal)
            if proc.returncode and raw.get("ok") is not False:
                raw = {"ok": False, "error": f"CLI_exit:{proc.returncode}"}
        except (OSError, subprocess.TimeoutExpired, ValueError):
            raw = {"ok": False, "error": "quote_transport_or_cli_error"}
        return parse_quote(raw, token, quote_token, "buy", share_quantity,
                           started, now_ms(), self.ttl_ms, mode="exactOut")
