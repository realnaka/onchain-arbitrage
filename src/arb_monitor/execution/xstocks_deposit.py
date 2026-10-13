"""模块 5：把 xStock 从自己的 Solana 钱包转进 Kraken。硬编码，LLM 不参与。

这一模块只做四件事，**都不签名**：
  1. 取 Kraken 的充值地址（现查，不抄上一次的）
  2. 校验数量 ≥ 最小充值量 —— 低于最小值的充值会永久不到账
  3. 编码转账指令（SPL / Token-2022 的 TransferChecked + 幂等建 ATA）
  4. 链上模拟

签名与广播交给钱包通道（`onchainos` 的 SPL 转账）。这样"动私钥"永远是单独一步。
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from decimal import Decimal

TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
TRANSFER_CHECKED = 12
CREATE_IDEMPOTENT = 1
XSTOCK_DECIMALS = 8        # Solana 上 xStock 的 svmDecimals


@dataclass
class DepositPlan:
    symbol: str
    mint: str
    destination: str
    method: str
    minimum: Decimal
    tokens: Decimal
    raw_amount: int
    decimals: int
    token_program: str
    ok: bool
    reason: str
    wallet_source: str = ""


def token_program_for(mint: str, *, token2022: bool = True) -> str:
    return TOKEN_2022_PROGRAM if token2022 else SPL_TOKEN_PROGRAM


def to_raw(amount: Decimal, decimals: int) -> int:
    """按整数放大到最小单位；不四舍五入（宁可少转，不可多转）。"""
    scaled = amount * (Decimal(10) ** decimals)
    return int(scaled.to_integral_value(rounding="ROUND_FLOOR"))


def transfer_checked_data(amount: Decimal, decimals: int) -> bytes:
    """SPL/Token-2022 TransferChecked 指令数据：opcode(12) + u64 + u8。"""
    raw = to_raw(amount, decimals)
    if raw <= 0:
        raise ValueError("转账数量太小，最小单位后为 0")
    return bytes([TRANSFER_CHECKED]) + struct.pack("<Q", raw) + bytes([decimals])


def create_ata_idempotent_data() -> bytes:
    return bytes([CREATE_IDEMPOTENT])


def find_solana_method(methods: list[dict], symbol: str) -> dict | None:
    """Solana 充值方法。Kraken 有的写「X - Solana」，有的不写链名（地址相同）。"""
    for method in methods or []:
        name = str(method.get("method") or "")
        if "Solana" in name or name.strip() == symbol:
            return method
    return None


def plan_deposit(*, symbol: str, mint: str, tokens: Decimal, methods: list[dict],
                 addresses: list[dict], decimals: int = XSTOCK_DECIMALS,
                 token2022: bool = True) -> DepositPlan:
    """纯计算：不做网络请求。`methods`/`addresses` 由调用方从 Kraken 现取。"""
    method = find_solana_method(methods, symbol)
    if method is None:
        return DepositPlan(symbol, mint, "", "", Decimal(0), tokens, 0, decimals,
                           token_program_for(mint, token2022=token2022), False,
                           "Kraken 没有 Solana 充值方法")
    minimum = Decimal(str(method.get("minimum") or "0"))
    address = ""
    for row in addresses or []:
        address = str(row.get("address") or "")
        if address:
            break
    program = token_program_for(mint, token2022=token2022)
    if not address:
        return DepositPlan(symbol, mint, "", method["method"], minimum, tokens, 0,
                           decimals, program, False, "没有拿到充值地址（不要凭记忆填）")
    if tokens < minimum:
        return DepositPlan(symbol, mint, address, method["method"], minimum, tokens, 0,
                           decimals, program, False,
                           f"{tokens} 低于最小充值 {minimum}——低于最小值会永久不到账")
    try:
        raw = to_raw(tokens, decimals)
    except Exception as exc:  # noqa: BLE001
        return DepositPlan(symbol, mint, address, method["method"], minimum, tokens, 0,
                           decimals, program, False, str(exc))
    if raw <= 0:
        return DepositPlan(symbol, mint, address, method["method"], minimum, tokens, raw,
                           decimals, program, False, "数量取整后为 0，不转")
    return DepositPlan(symbol, mint, address, method["method"], minimum, tokens, raw,
                       decimals, program, True,
                       f"可充 {tokens} → {address}（最小 {minimum}）")


def wallet_payload(plan: DepositPlan) -> str:
    """给钱包 CLI 的参数包。广播前必须先拿它做一次模拟。"""
    if not plan.ok:
        raise ValueError(f"计划不可执行：{plan.reason}")
    return json.dumps({
        "action": "spl_transfer",
        "mint": plan.mint,
        "to": plan.destination,
        "amount": str(plan.tokens),
        "amount_raw": plan.raw_amount,
        "decimals": plan.decimals,
        "token_program": plan.token_program,
        "transfer_checked_data": transfer_checked_data(plan.tokens, plan.decimals).hex(),
        "create_ata_idempotent_data": create_ata_idempotent_data().hex(),
    }, ensure_ascii=False)
