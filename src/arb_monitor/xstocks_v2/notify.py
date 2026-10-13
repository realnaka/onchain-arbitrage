"""Feishu alerts for v2. Silent HALT is the dangerous state.

The gate semantics are useless if nobody is told: ``unknown`` and a ``stopped``
with inventory both keep the lock and stop trading until a human reconciles,
and until 2026-10-07 the v2 tree had no notification path at all (operator
found this when asking why a HALT had not been reported).

Two channels, same as v1: ``signal`` for opportunities and fills, ``alert`` for
anything that needs a person (unknown, stop with inventory, killed switch).
Credentials come from ``config/notify.env``; a missing webhook degrades to the
lark CLI, and a failed send never raises -- losing an alert is bad, breaking the
cycle that produced it is worse.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = ROOT / "config" / "notify.env"


def load_env_file(path: Path | str = ENV_FILE) -> None:
    """Export ``config/notify.env`` into the environment (never overwriting)."""
    try:
        lines = Path(path).read_text().splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def settings(env: dict | None = None) -> dict:
    env = env if env is not None else os.environ
    return {
        "channel": "webhook",
        "webhook_url": env.get("ARB_FEISHU_WEBHOOK", ""),
        "alert_channel": "webhook",
        "alert_webhook_url": env.get("ARB_FEISHU_ALERT_WEBHOOK", ""),
    }


def sender(audience: str = "alert", *, factory=None):
    """Build a ``send(text) -> dict`` callable, or a no-op when unconfigured."""
    load_env_file()
    if factory is None:
        from arb_monitor.ops.notify import FeishuNotifier

        factory = FeishuNotifier
    built = factory(settings(), audience=audience)

    def send(text: str) -> dict:
        try:
            return built.send(text)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return send


def format_hold(*, symbol: str, family: str, state: str, held_at: str,
                reason: str, extra: str = "") -> str:
    """One HALT message an operator can act on without reading the ledger."""
    where = {
        "wallet": "币在自己钱包",
        "in_bridge": "币在桥上",
        "dest_wallet": "币在目的链钱包",
        "kraken": "币在 Kraken",
        "unknown": "币的位置未知",
    }.get(held_at, held_at or "位置未知")
    lines = [
        f"[xStocks v2 停机] {symbol} / {family}",
        f"状态：{state}（{where}）",
        f"原因：{reason}",
        "动作：对账后删除 data-v2/xstocks_v2_HALT 与锁，才恢复开仓。",
    ]
    if extra:
        lines.append(extra)
    return "\n".join(lines)
