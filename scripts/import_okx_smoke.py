from pathlib import Path
from arb_monitor.execution.ledger import ExecutionLedger

ledger = ExecutionLedger(Path("data/execution.db"))
ledger.record_settled_group(
    "okx-smoke-20260915", "okx-smoke-20260915", [
        {"order_id": "3923967805011759104", "side": "sell", "inst_id": "XIAOMI-USDT-SWAP",
         "fill_size": "1", "fill_price": "3.441", "fee": "0.00154845",
         "fee_currency": "USDT", "execution_type": "T", "reduce_only": False},
        {"order_id": "3923967898829950976", "side": "buy", "inst_id": "XIAOMI-USDT-SWAP",
         "fill_size": "1", "fill_price": "3.443", "fee": "0.00154935",
         "fee_currency": "USDT", "execution_type": "T", "reduce_only": True},
    ], realized_pnl="-0.00509780", created_ts_ms=1789445756359)
print(ledger.last("okx-smoke-20260915"))
ledger.close()
