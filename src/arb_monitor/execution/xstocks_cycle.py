"""模块 7：一笔 xStocks 现货差价的完整编排。硬编码，LLM 不介入决策。

一条状态机，四条硬规则：
  1. **同时只允许一笔在飞**（文件锁）。第二条起直接拒绝。
  2. **每一步失败都 fail-closed**：不自动重试、不猜、不"大概没问题"。
  3. **未知状态即停机**：广播了但没拿到确认，就停下等人对账，绝不重播。
  4. **每一步都记时间**——尤其是 Kraken 入账那一段，它是唯一还没实测过的数字。

三条腿（买 / 充 / 卖）用注入的方式传进来，所以这个模块本身可以完全离线测试。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
LOCK_PATH = ROOT / "data" / "xstocks_cycle.lock"
LEDGER_PATH = ROOT / "data" / "xstocks_cycles.jsonl"

# 状态：越往后越接近完成。UNKNOWN 一律停机，不进下一步。
PLANNED = "planned"
REFUSED = "refused"            # 闸门不过，没动钱
BOUGHT = "bought"
UNKNOWN = "unknown"            # 动作发出但结果不明——停机对账
TRANSFERRED = "transferred"
CREDITED = "credited"
SOLD = "sold"
STOPPED = "stopped"            # 中途停下，币停在哪写清楚


@dataclass
class CycleResult:
    symbol: str
    size_usd: Decimal
    state: str = PLANNED
    reason: str = ""
    tokens: Decimal = Decimal(0)
    buy_price: Decimal | None = None
    sell_price: Decimal | None = None
    net_usdt: Decimal | None = None
    held_at: str = "wallet"           # 收尾时币停在哪：wallet / kraken / nowhere
    marks: dict = field(default_factory=dict)   # 各步骤时间戳
    notes: list = field(default_factory=list)

    def mark(self, name: str, now: float) -> None:
        self.marks[name] = round(now, 3)

    def elapsed(self, start: str, end: str) -> float | None:
        a, b = self.marks.get(start), self.marks.get(end)
        return None if a is None or b is None else round(b - a, 2)

    def timings(self) -> dict:
        return {
            "buy_s": self.elapsed("quote", "buy_ok"),
            "transfer_s": self.elapsed("transfer_sent", "transfer_ok"),
            "kraken_credit_s": self.elapsed("transfer_ok", "credited"),
            "sell_s": self.elapsed("sell_sent", "sell_ok"),
            "total_s": self.elapsed("quote", "sell_ok"),
        }

    def to_json(self) -> dict:
        data = asdict(self)
        for key in ("size_usd", "tokens", "buy_price", "sell_price", "net_usdt"):
            if data[key] is not None:
                data[key] = str(data[key])
        data["timings"] = self.timings()
        return data


class CycleBusy(RuntimeError):
    pass


class CycleLock:
    """同时只允许一笔。用 O_EXCL 建文件，退出时删掉。"""

    def __init__(self, path: Path = LOCK_PATH):
        self.path = path

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            age = time.time() - self.path.stat().st_mtime
            raise CycleBusy(
                f"已有另一笔在飞（锁 {self.path}，{age/60:.1f} 分钟前建的）。"
                f"确认那笔的状态以后再删。") from exc
        with os.fdopen(fd, "w") as handle:
            handle.write(str(os.getpid()))
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)
        return False


def record(result: CycleResult, path: Path = LEDGER_PATH) -> Path:
    """追加一行账。一行一笔，便于随时翻。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(result.to_json(), ensure_ascii=False) + "\n")
    return path


def run_cycle(*, symbol: str, size_usd: Decimal,
              buy_fn, deposit_fn, sell_fn,
              now=time.time, ledger: Path = LEDGER_PATH,
              lock_path: Path = LOCK_PATH) -> CycleResult:
    """买 → 充 → 卖。每一步都要拿到明确结果，否则停机。

    * ``buy_fn()``      -> {"ok": bool, "tokens": str, "price": str, "reason": str,
                            "broadcast": bool}
    * ``deposit_fn(tokens)`` -> {"ok": bool, "sent": bool, "reason": str}
    * ``sell_fn(tokens)``    -> {"ok": bool, "price": str, "net": str, "reason": str}
    """
    result = CycleResult(symbol=symbol, size_usd=Decimal(size_usd))
    with CycleLock(lock_path):
        result.mark("quote", now())
        buy = buy_fn()
        if not buy.get("ok"):
            result.state = REFUSED
            result.reason = f"买入闸门不过：{buy.get('reason')}"
            record(result, ledger)
            return result
        if buy.get("broadcast") and not buy.get("confirmed", True):
            result.state = UNKNOWN
            result.reason = "买入已广播但没有确认——停机对账，不重播"
            result.held_at = "unknown"
            record(result, ledger)
            return result
        result.tokens = Decimal(str(buy["tokens"]))
        result.buy_price = Decimal(str(buy["price"]))
        result.mark("buy_ok", now())

        dep = deposit_fn(result.tokens)
        if not dep.get("ok"):
            result.state = STOPPED
            result.reason = f"充值不许发：{dep.get('reason')}"
            result.held_at = "wallet"
            record(result, ledger)
            return result
        result.mark("transfer_sent", now())
        if not dep.get("credited"):
            result.state = UNKNOWN
            result.reason = ("充值已发出但 Kraken 未确认入账——停机对账，"
                             "不要重复转账")
            result.held_at = "in_transit"
            record(result, ledger)
            return result
        result.mark("transfer_ok", now())
        result.mark("credited", now())
        result.state = CREDITED

        sell = sell_fn(result.tokens)
        result.mark("sell_sent", now())
        if not sell.get("ok"):
            result.state = STOPPED
            result.reason = f"卖出没成：{sell.get('reason')}"
            result.held_at = "kraken"      # 币已经进了 Kraken，停在那里等人处理
            record(result, ledger)
            return result
        result.sell_price = Decimal(str(sell["price"]))
        if sell.get("net") is not None:
            result.net_usdt = Decimal(str(sell["net"]))
        result.mark("sell_ok", now())
        result.state = SOLD
        result.held_at = "nowhere"
        result.reason = "两腿都完成"
        record(result, ledger)
        return result
