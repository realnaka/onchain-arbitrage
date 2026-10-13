#!/usr/bin/env python3
"""One minimum send on one bridge edge, with a receipt.

    scripts/xstocks_v2_edge_send.py plan --source eth --dest arbitrum --tokens 0.0006
    scripts/xstocks_v2_edge_send.py once --source eth --dest arbitrum --tokens 0.0006 --confirm-live

The path, in order. Any refusal stops before the wallet is asked to sign:

1. ``capability.bridge_status`` must be ``ready`` (``no_lane`` never signs).
2. The fee is read from the chain (``ccip_evm.fee_quote``), plus 2 % headroom.
3. ``fee`` in USDT must be known and at most ``capability.PROOF_CAP_USDT``.
4. The calldata is decoded back and validated.
5. ``eth_call`` with the real ``value`` must succeed.
6. Broadcast, then wait for the receipt. A hash the chain never saw
   (wallet history ``ERROR``, balance unchanged) is retried once.

The receipt row goes to ``data-v2/xstocks_v2_constructor_proof.json``.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arb_monitor.xstocks_v2 import capability as cap               # noqa: E402
from arb_monitor.xstocks_v2 import ccip_evm as EVM                  # noqa: E402
from arb_monitor.xstocks_v2 import native_reserve                   # noqa: E402
from arb_monitor.xstocks_v2 import routes                           # noqa: E402
from arb_monitor.xstocks_v2.feeds import evm as evm_feed            # noqa: E402
from arb_monitor.xstocks_v2.feeds.http import Http                  # noqa: E402

DATA = ROOT / "data-v2"
PROOF = DATA / "xstocks_v2_constructor_proof.json"
GAS_PRICES = DATA / "xstocks_v2_gas_prices.json"
PROXY = "http://127.0.0.1:4780"
EVM_WALLET = "0xabababababababababababababababababababab"
SPYX = "0x90a2a4c76b5d8c0bc892a69ea28aa775a8f2dd48"
SOL_WALLET = "AmkjszArkzSKfSUqqgFJLGe6ytjhJxjHFaaVrCxAf2ev"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
#: Wallet chain names that ``onchainos wallet`` accepts. HyperEVM's name in
#: that list is ``evm_hype``; ``hyperevm`` is rejected.
ONCHAINOS_CHAIN = {
    "eth": "ethereum",
    "x-layer": "xlayer",
    "bsc": "bsc",
    "arbitrum": "arbitrum",
    "optimism": "optimism",
    "ink": "ink",
    "hyperevm": "evm_hype",
}
FEE_ASSET = dict(EVM.FEE_SOURCE_ASSET)
#: Gas limit sent to the wallet = estimate x this. The wallet reserves
#: ``gas limit x its own fee cap`` before it signs (measured 2026-10-09: it
#: wanted 0.00054 ETH for a swap at 0.10 gwei network price), so a tight limit
#: is what lets a small balance sign at all.
GAS_LIMIT_MARGIN = Decimal("1.3")
RECEIPT_TRIES = 40
RECEIPT_PAUSE_S = 6.0


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


def onchainos(args: list[str], timeout: int = 120) -> dict:
    env = dict(os.environ, HTTPS_PROXY=PROXY, HTTP_PROXY=PROXY)
    done = subprocess.run(["onchainos", *args], capture_output=True, text=True,
                          timeout=timeout, env=env)
    text = (done.stdout or done.stderr or "").strip()
    for candidate in (text, *reversed(text.splitlines())):
        candidate = candidate.strip()
        if candidate.startswith("{"):
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    return {"ok": False, "error": f"unparsable: {text[:300]}"}


def rpc(http, chain: str, method: str, params: list, tries: int = 3):
    """Read-only JSON-RPC. Transport errors are retried; a node answer is not."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    for attempt in range(tries):
        try:
            status, raw = http.request(evm_feed.CHAINS[chain].rpc, data=body,
                                       headers={"Content-Type": "application/json"})
            break
        except Exception:  # noqa: BLE001 - TLS resets on the public node
            if attempt == tries - 1:
                raise
            time.sleep(2.0 * (attempt + 1))
    if status != 200:
        raise RuntimeError(f"{chain} rpc http_{status}")
    payload = json.loads(raw.decode("utf-8", "replace"))
    return payload.get("result"), payload.get("error")


def balance_of(http, chain: str, token: str, owner: str = EVM_WALLET) -> int:
    result, error = rpc(http, chain, "eth_call", [
        {"to": token, "data": "0x70a08231" + "0" * 24 + owner[2:].lower()}, "latest"])
    if error or result is None:
        raise RuntimeError(f"{chain} balanceOf unreadable: {error}")
    return int(result, 16)


def allowance(http, chain: str, token: str, spender: str,
              owner: str = EVM_WALLET) -> int:
    data = "0xdd62ed3e" + "0" * 24 + owner[2:].lower() + "0" * 24 + spender[2:].lower()
    result, error = rpc(http, chain, "eth_call", [{"to": token, "data": data}, "latest"])
    if error or result is None:
        raise RuntimeError(f"{chain} allowance unreadable: {error}")
    return int(result, 16)


def native_balance(http, chain: str, owner: str = EVM_WALLET) -> int:
    result, error = rpc(http, chain, "eth_getBalance", [owner, "latest"])
    if error or result is None:
        raise RuntimeError(f"{chain} native balance unreadable: {error}")
    return int(result, 16)


def gas_usd(asset: str) -> Decimal | None:
    try:
        row = json.loads(GAS_PRICES.read_text())["prices"][asset]
        return Decimal(str(row["usd"]))
    except (OSError, KeyError, ValueError, TypeError, ArithmeticError):
        return None


def message_id(receipt: dict) -> str:
    """The bridge contract's own event carries the CCIP message id as topic 1."""
    for row in receipt.get("logs") or []:
        if str(row.get("address", "")).lower() == EVM.BRIDGE_CONTRACT and len(row.get("topics") or []) > 1:
            return row["topics"][1]
    return ""


def wait_receipt(http, chain: str, tx_hash: str) -> dict | None:
    for _ in range(RECEIPT_TRIES):
        result, _error = rpc(http, chain, "eth_getTransactionReceipt", [tx_hash])
        if result:
            return result
        time.sleep(RECEIPT_PAUSE_S)
    return None


def wallet_status(chain: str, tx_hash: str) -> str:
    out = onchainos(["wallet", "history", "--chain", ONCHAINOS_CHAIN[chain],
                     "--limit", "20"])
    rows = out.get("data") or []
    if rows and isinstance(rows[0], dict) and "orderList" in rows[0]:
        rows = rows[0]["orderList"]
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("txHash") == tx_hash:
            return f"{row.get('txStatus')}:{row.get('failReason') or ''}"
    return "unknown"


def record(source: str, dest: str, **fields) -> None:
    """Update the one receipt row for this edge. Other rows are untouched."""
    report = json.loads(PROOF.read_text())
    rows = [row for row in report["rows"]
            if row["source"] == source and row["dest"] == dest
            and not row["kind"].startswith("erc4626")]
    if len(rows) != 1:
        raise RuntimeError(f"{len(rows)} receipt rows for {source}->{dest}")
    rows[0].update(fields)
    PROOF.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n")


SLOT0_DISCRIMINATOR = "8d422fd555c247a6"


def _solana_account(http, key: str) -> tuple[str, bytes] | None:
    import base64
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                       "params": [key, {"encoding": "base64"}]}).encode()
    status, raw = http.request(SOLANA_RPC, data=body,
                               headers={"Content-Type": "application/json"})
    if status != 200:
        raise RuntimeError(f"solana rpc http_{status}")
    value = (json.loads(raw.decode("utf-8", "replace")).get("result") or {}).get("value")
    if not value:
        return None
    return value["owner"], base64.b64decode(value["data"][0])


def solana_accounts(http, *, source: str, symbol: str) -> tuple[tuple[str, ...], dict, list[str]]:
    """The 11 accounts for ``source -> solana``, every slot read from the chain.

    Refuses (non-empty problems) when any slot cannot be confirmed: slot 0 must
    be this source's own marker account, slot 3 must be a lane config naming
    this source's selector, slot 4 must be this mint's token config, and the
    destination token account must already exist and belong to the wallet.
    """
    import importlib.util
    from arb_monitor.xstocks_v2 import ccip_solana as SVM
    spec = importlib.util.spec_from_file_location(
        "v2probe_edge", ROOT / "scripts" / "xstocks_v2_ccip_solana_probe.py")
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    product = probe.book_symbol(symbol)
    mint = product["mint"]
    problems: list[str] = []
    config_key, config_data = probe.discover_token_config(probe.Http(), symbol, mint)
    custody = SVM.to_b58(SVM.token_config_custody(
        config_data, mint=mint, product_id=product["product_id"]))
    live_config = _solana_account(http, config_key)
    if not live_config or live_config[0] != SVM.SOLANA_PROGRAM or live_config[1] != config_data:
        problems.append("token_config_changed_on_chain")
    slot0 = EVM.SOURCE_SLOT0.get(source)
    if slot0 is None:
        problems.append(f"no_slot0:{source}")
        return (), product, problems
    row = _solana_account(http, slot0)
    if not row or row[0] != SVM.SOLANA_PROGRAM or row[1][:8].hex() != SLOT0_DISCRIMINATOR:
        problems.append("slot0_not_issuer_marker")
    for index, key in EVM.SHARED_SLOTS.items():
        row = _solana_account(http, key)
        if not row or row[0] != SVM.SOLANA_PROGRAM:
            problems.append(f"shared_slot_missing:{index}")
    lane_key = EVM.SOURCE_LANE_ACCOUNT.get(source)
    row = _solana_account(http, lane_key) if lane_key else None
    if not row:
        problems.append("lane_missing")
    else:
        selector, bridge = SVM.parse_lane_config(row[1])
        if selector != EVM.SOURCE_SELECTORS.get(source):
            problems.append(f"lane_selector:{selector}")
        if bridge.lower() != EVM.BRIDGE_CONTRACT:
            problems.append(f"lane_bridge:{bridge}")
    ata = SVM.to_b58(SVM.associated_token_address(SOL_WALLET, mint))
    row = _solana_account(http, ata)
    if not row:
        problems.append(f"destination_ata_missing:{ata}")
    elif row[0] != SVM.TOKEN_2022 or SVM.to_b58(row[1][0:32]) != mint \
            or SVM.to_b58(row[1][32:64]) != SOL_WALLET:
        problems.append("destination_ata_not_ours")
    accounts = EVM.build_accounts(source_chain=source, mint=mint, token_config=config_key,
                                  custody=custody, owner=SOL_WALLET, destination_ata=ata)
    problems += EVM.validate_accounts(accounts, source_chain=source, mint=mint,
                                      custody=custody, owner=SOL_WALLET, destination_ata=ata)
    product.update(token_config=config_key, custody=custody, ata=ata)
    return accounts, product, problems


def prepare(http, *, source: str, dest: str, amount: int, token: str,
            symbol: str = "SPYx") -> dict:
    """Everything up to (not including) the signature. Never broadcasts."""
    plan = {"source": source, "dest": dest, "amount": amount, "token": token,
            "problems": []}
    status = cap.bridge_status(source, dest)
    plan["status"] = status
    if status != cap.READY:
        plan["problems"].append(status)
        return plan
    dialect = routes.dialect(source, dest)
    plan["dialect"] = dialect
    if dialect not in ("evm_to_evm", "evm_to_solana"):
        plan["problems"].append(f"dialect:{dialect}")
        return plan
    if dialect == "evm_to_solana" and amount % (10 ** 10):
        plan["problems"].append("amount_not_whole_at_8_decimals")
        return plan
    if source not in ONCHAINOS_CHAIN:
        plan["problems"].append(f"no_signer:{source}")
        return plan
    held = balance_of(http, source, token)
    plan["held"] = held
    if held < amount:
        plan["problems"].append(f"token_short:{held}<{amount}")
    allowed = allowance(http, source, token, EVM.BRIDGE_CONTRACT)
    plan["allowance"] = allowed
    if allowed < amount:
        plan["problems"].append(f"allowance_short:{allowed}<{amount}")
    fee, why = EVM.fee_quote(http=http, rpc=evm_feed.CHAINS[source].rpc,
                             source_chain=source, sender=EVM_WALLET, dest_chain=dest)
    if fee is None:
        plan["problems"].append(f"fee_unknown:{why}")
        return plan
    fee_wei = int(fee * Decimal(10 ** 18))
    fee_wei += fee_wei // 50
    plan["fee_wei"] = fee_wei
    price = gas_usd(FEE_ASSET[source])
    plan["cost_usdt"] = None if price is None else (Decimal(fee_wei) / Decimal(10 ** 18) * price)
    allowed_cost, why_cost = cap.may_broadcast(plan["cost_usdt"])
    if not allowed_cost:
        plan["problems"].append(why_cost)
    if dialect == "evm_to_evm":
        data, _ = EVM.build_call_evm(source_chain=source, dest_chain=dest, token=token,
                                     amount=amount, receiver=EVM_WALLET)
        problems = EVM.validate_call_evm(EVM.decode_calldata(data), dest_chain=dest,
                                         receiver=EVM_WALLET, amount=amount)
    else:
        accounts, product, problems = solana_accounts(http, source=source, symbol=symbol)
        plan["solana"] = {"accounts": list(accounts), "token_config": product.get("token_config"),
                          "custody": product.get("custody"), "ata": product.get("ata")}
        if problems or not accounts:
            plan["problems"] += problems
            return plan
        data, call = EVM.build_call(source_chain=source, token=token, amount=amount,
                                    receiver=SOL_WALLET, accounts=accounts)
        problems = EVM.validate_call(EVM.decode_calldata(data), source_chain=source,
                                     mint=product["mint"], custody=product["custody"],
                                     owner=SOL_WALLET, amount=amount)
    plan["problems"] += problems
    plan["data"] = data
    call = {"from": EVM_WALLET, "to": EVM.BRIDGE_CONTRACT, "value": hex(fee_wei),
            "data": data}
    _result, error = rpc(http, source, "eth_call", [call, "latest"])
    if error:
        plan["problems"].append(f"dry_run:{EVM.decode_revert_reason(error)}")
        return plan
    gas, error = rpc(http, source, "eth_estimateGas", [call])
    if error or not gas:
        plan["problems"].append(f"estimate_gas:{EVM.decode_revert_reason(error)}")
        return plan
    plan["gas_limit"] = int(Decimal(int(gas, 16)) * GAS_LIMIT_MARGIN)
    gas_price, _ = rpc(http, source, "eth_gasPrice", [])
    plan["native"] = native_balance(http, source)
    plan["gas_price"] = int(gas_price, 16) if gas_price else None
    return plan


def send(http, plan: dict) -> dict:
    """Broadcast once; if the hash never reaches the chain, retry once."""
    source = plan["source"]
    # A94: the wallet reserves ``limit x its own fee cap`` before signing, and
    # a thin balance comes back as 10004 -- which used to be an ``unknown`` and
    # a HALT. The plan already read both numbers; refuse here with the reason.
    # ``native_balance`` is wei. ``shortfall`` compares whole coins. Passing
    # the wei count made every chain look funded, and the wallet's own
    # refusal then came back with an empty error (Ink, 2026-10-10).
    native = plan.get("native")
    balance = None if native is None else Decimal(native) / Decimal(10 ** 18)
    short = native_reserve.shortfall(balance=balance,
                                     gas_limit=plan.get("gas_limit"),
                                     gas_price_wei=plan.get("gas_price"))
    if short:
        return {"sent": False, "reason": short}
    for attempt in (1, 2):
        out = onchainos(["wallet", "contract-call", "--chain", ONCHAINOS_CHAIN[source],
                         "--to", EVM.BRIDGE_CONTRACT, "--input-data", plan["data"],
                         "--amt", str(plan["fee_wei"]), "--gas-limit", str(plan["gas_limit"]),
                         "--from", EVM_WALLET])
        tx_hash = str(((out.get("data") or {}) if out.get("ok") else {}).get("txHash") or "")
        if not tx_hash.startswith("0x"):
            return {"sent": False, "reason": f"wallet_refused:{str(out.get('error'))[:200]}"}
        log(f"attempt {attempt}: broadcast {tx_hash}")
        receipt = wait_receipt(http, source, tx_hash)
        if receipt:
            ok = receipt.get("status") == "0x1"
            return {"sent": ok, "tx": tx_hash,
                    "reason": "source_confirmed" if ok else "source_reverted",
                    "message_id": message_id(receipt),
                    "block": int(receipt["blockNumber"], 16)}
        status = wallet_status(source, tx_hash)
        log(f"no receipt for {tx_hash}; wallet says {status}")
        if not status.startswith("ERROR"):
            return {"sent": False, "tx": tx_hash, "reason": f"unknown:{status}"}
    return {"sent": False, "tx": tx_hash, "reason": "never_broadcast_twice"}


LANES_FILE = DATA / "xstocks_v2_solana_lanes.json"
SOL_SIGNATURE_FEE = 5000


def _sol_rpc(http, method: str, params: list):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for attempt in range(5):
        try:
            status, raw = http.request(SOLANA_RPC, data=body,
                                       headers={"Content-Type": "application/json"})
        except Exception:  # noqa: BLE001
            time.sleep(2.0 * (attempt + 1))
            continue
        payload = json.loads(raw.decode("utf-8", "replace")) if status == 200 else {}
        if status == 429 or (payload.get("error") or {}).get("code") == 429:
            time.sleep(3.0 * (attempt + 1))
            continue
        if payload.get("error"):
            raise RuntimeError(f"{method}: {payload['error']}")
        return payload.get("result")
    raise RuntimeError(f"{method}: no answer")


def _sol_token_raw(http, mint: str) -> int:
    rows = _sol_rpc(http, "getTokenAccountsByOwner",
                    [SOL_WALLET, {"mint": mint}, {"encoding": "jsonParsed"}])["value"]
    return sum(int(row["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
               for row in rows)


def prepare_solana(http, *, dest: str, symbol: str, amount_raw: int) -> dict:
    """Solana-source send to ``dest``: 27 accounts, simulation, cost. Never signs."""
    import base64
    import importlib.util
    from arb_monitor.xstocks_v2 import ccip_solana as C
    plan = {"source": "solana", "dest": dest, "symbol": symbol, "amount_raw": amount_raw,
            "problems": []}
    status = cap.bridge_status("solana", dest)
    plan["status"] = status
    if status != cap.READY:
        plan["problems"].append(status)
        return plan
    spec = importlib.util.spec_from_file_location(
        "v2probe_edge_sol", ROOT / "scripts" / "xstocks_v2_ccip_solana_probe.py")
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    product = probe.book_symbol(symbol)
    mint = product["mint"]
    held = _sol_token_raw(http, mint)
    plan["held_raw"] = held
    if held < amount_raw:
        plan["problems"].append(f"token_short:{held}<{amount_raw}")
    config_key, config_data = probe.discover_token_config(probe.Http(), symbol, mint)
    live_config = _solana_account(http, config_key)
    if not live_config or live_config[1] != config_data:
        plan["problems"].append("token_config_changed_on_chain")
    custody = C.token_config_custody(config_data, mint=mint, product_id=product["product_id"])
    accounts = C.build_accounts(signer=SOL_WALLET, mint=mint, token_config=config_key,
                                custody=custody, dest_chain=dest)
    plan["problems"] += C.validate_accounts(accounts, signer=SOL_WALLET, mint=mint,
                                            token_config=config_key, custody=custody,
                                            dest_chain=dest)
    try:
        sample = json.loads(LANES_FILE.read_text()).get(dest) or {}
    except (OSError, ValueError):
        sample = {}
    if not sample.get("found"):
        plan["problems"].append(f"no_lane_sample:{dest}")
    else:
        for index in sorted(C.DEST_INDEXES):
            if sample["accounts"][index] != accounts[index].pubkey:
                plan["problems"].append(f"sample_mismatch:{index}")
    for index in sorted(C.DEST_INDEXES):
        if not _solana_account(http, accounts[index].pubkey):
            plan["problems"].append(f"dest_slot_missing_on_chain:{index}")
    lane = _solana_account(http, accounts[25].pubkey)
    if lane:
        selector, bridge = C.parse_lane_config(lane[1])
        if selector != C.solana_dest_selector(dest):
            plan["problems"].append(f"lane_selector:{selector}")
        if bridge.lower() != C.XSTOCKS_BRIDGE_EVM:
            plan["problems"].append(f"lane_bridge:{bridge}")
    data = C.instruction_for_dest(dest_chain=dest, recipient=EVM_WALLET,
                                  product_id=product["product_id"], amount=amount_raw)
    plan["problems"] += C.validate_args(C.decode_args(data), product_id=product["product_id"],
                                        amount=amount_raw,
                                        dest_chain_selector=C.solana_dest_selector(dest))
    if plan["problems"]:
        return plan
    blockhash = _sol_rpc(http, "getLatestBlockhash", [{"commitment": "finalized"}])["value"]["blockhash"]
    tx = C.build_legacy_transaction(payer=SOL_WALLET, accounts=accounts,
                                    program_id=C.SOLANA_PROGRAM, data=data,
                                    recent_blockhash=blockhash)
    before = _sol_rpc(http, "getBalance", [SOL_WALLET])["value"]
    value = _sol_rpc(http, "simulateTransaction", [base64.b64encode(tx).decode(), {
        "encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True,
        "commitment": "processed",
        "accounts": {"encoding": "base64", "addresses": [SOL_WALLET]}}])["value"]
    if value.get("err"):
        plan["problems"].append(f"simulation:{json.dumps(value['err'])[:160]}")
        plan["logs"] = (value.get("logs") or [])[-6:]
        return plan
    after = (value.get("accounts") or [{}])[0].get("lamports")
    if after is None:
        plan["problems"].append("simulation_no_balance")
        return plan
    lamports = before - after + SOL_SIGNATURE_FEE
    plan["sol_cost"] = lamports
    plan["sol_balance"] = before
    price = gas_usd("SOL")
    plan["cost_usdt"] = None if price is None else Decimal(lamports) / Decimal(10 ** 9) * price
    allowed, why = cap.may_broadcast(plan["cost_usdt"])
    if not allowed:
        plan["problems"].append(why)
    if before < lamports * 2:
        plan["problems"].append(f"sol_short:{before}<{lamports * 2}")
    plan["tx"] = C.b58encode(tx)
    plan["mint"] = mint
    plan["build"] = (accounts, data)
    return plan


def send_solana(http, plan: dict) -> dict:
    """Broadcast once; a signature the chain never saw is retried once."""
    from arb_monitor.xstocks_v2 import ccip_solana as C
    accounts, data = plan["build"]
    unsigned = plan["tx"]
    for attempt in (1, 2):
        before = _sol_token_raw(http, plan["mint"])
        out = onchainos(["wallet", "contract-call", "--chain", "solana", "--to", C.SOLANA_PROGRAM,
                         "--unsigned-tx", unsigned, "--from", SOL_WALLET])
        sig = str(((out.get("data") or {}) if out.get("ok") else {}).get("txHash") or "")
        if not sig:
            return {"sent": False, "reason": f"wallet_refused:{str(out.get('error'))[:200]}"}
        log(f"attempt {attempt}: broadcast {sig}")
        for _ in range(20):
            time.sleep(4)
            status = _sol_rpc(http, "getSignatureStatuses",
                              [[sig], {"searchTransactionHistory": True}])["value"][0]
            if status and status.get("confirmationStatus") in ("confirmed", "finalized"):
                ok = status.get("err") is None
                return {"sent": ok, "tx": sig,
                        "reason": "source_confirmed" if ok else f"source_failed:{status.get('err')}"}
        after = _sol_token_raw(http, plan["mint"])
        if after != before:
            return {"sent": False, "tx": sig, "reason": "unknown:balance_moved_without_status"}
        log(f"{sig} never landed; balance unchanged")
        blockhash = _sol_rpc(http, "getLatestBlockhash", [{"commitment": "finalized"}])["value"]["blockhash"]
        unsigned = C.b58encode(C.build_legacy_transaction(
            payer=SOL_WALLET, accounts=accounts, program_id=C.SOLANA_PROGRAM, data=data,
            recent_blockhash=blockhash))
    return {"sent": False, "tx": sig, "reason": "never_landed_twice"}


def solana_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("solplan", "solonce"))
    parser.add_argument("--dest", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--raw", required=True, type=int, help="Solana raw amount (8 decimals)")
    parser.add_argument("--confirm-live", action="store_true")
    args = parser.parse_args(argv)
    http = Http()
    dest = routes.norm(args.dest)
    plan = prepare_solana(http, dest=dest, symbol=args.symbol, amount_raw=args.raw)
    shown = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in plan.items()
             if k not in ("tx", "build")}
    log(json.dumps(shown, ensure_ascii=False))
    if plan["problems"]:
        log(f"REFUSED solana->{dest}: {plan['problems']}")
        return 2
    if args.mode == "solplan":
        return 0
    if not args.confirm_live:
        raise SystemExit("solonce needs --confirm-live")
    result = send_solana(http, plan)
    log(json.dumps(result))
    record("solana", dest, sent=bool(result["sent"]), tx=result.get("tx", ""),
           reason=result["reason"], constructed=True,
           cost_usdt=format(plan["cost_usdt"].quantize(Decimal("0.001")), "f"),
           sent_symbol=args.symbol, sent_raw=str(args.raw),
           fee_native=f"{format(Decimal(plan['sol_cost']) / Decimal(10 ** 9), 'f')} SOL",
           arrived_tokens=None, note="27 accounts: slots 9/10/13/25 per destination, "
           "matched a mainnet send in data-v2/xstocks_v2_solana_lanes.json")
    return 0 if result["sent"] else 1


def redeem(http, *, wrapper: str, confirm: bool) -> int:
    """ERC-4626 ``redeem`` of every share we hold in ``wrapper`` on Ethereum."""
    from arb_monitor.xstocks_v2 import wrap as wrap_core
    shares = balance_of(http, "eth", wrapper)
    if shares <= 0:
        log("REFUSED redeem: no shares")
        return 2
    data = wrap_core.redeem_calldata(shares=shares, receiver=EVM_WALLET, owner=EVM_WALLET)
    call = {"from": EVM_WALLET, "to": wrapper, "value": "0x0", "data": data}
    result, error = rpc(http, "eth", "eth_call", [call, "latest"])
    if error:
        log(f"REFUSED redeem dry_run: {EVM.decode_revert_reason(error)}")
        return 2
    assets = int(result, 16) if result else None
    gas, error = rpc(http, "eth", "eth_estimateGas", [call])
    if error or not gas:
        log(f"REFUSED redeem estimate: {EVM.decode_revert_reason(error)}")
        return 2
    gas_limit = int(Decimal(int(gas, 16)) * GAS_LIMIT_MARGIN)
    log(f"redeem shares {shares} -> assets {assets} (dry run), gas limit {gas_limit}")
    if not confirm:
        return 0
    before = balance_of(http, "eth", SPYX)
    out = onchainos(["wallet", "contract-call", "--chain", "ethereum", "--to", wrapper,
                     "--input-data", data, "--gas-limit", str(gas_limit), "--from", EVM_WALLET])
    tx_hash = str(((out.get("data") or {}) if out.get("ok") else {}).get("txHash") or "")
    if not tx_hash.startswith("0x"):
        log(f"redeem refused by wallet: {str(out.get('error'))[:200]}")
        return 1
    receipt = wait_receipt(http, "eth", tx_hash)
    after = balance_of(http, "eth", SPYX)
    ok = bool(receipt and receipt.get("status") == "0x1")
    log(json.dumps({"tx": tx_hash, "ok": ok, "spyx_before": before, "spyx_after": after}))
    report = json.loads(PROOF.read_text())
    for row in report["rows"]:
        if row["kind"] == "erc4626_redeem":
            row.update(sent=ok, tx=tx_hash, constructed=True,
                       reason="redeemed" if ok else "redeem_failed",
                       wrapper=wrapper, shares=str(shares),
                       received_tokens=format(Decimal(after - before) / Decimal(10 ** 18), "f"))
    PROOF.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n")
    return 0 if ok else 1


ROUTE_LEDGER = DATA / "xstocks_v2_route_samples.jsonl"
FEES_FILE = DATA / "xstocks_v2_bridge_fees.json"
SOURCE_ASSET = {**FEE_ASSET, "solana": "SOL"}


def _paid_value(http, chain: str, tx_hash: str) -> Decimal | None:
    """``msg.value`` of an EVM bridge send: the fee we paid, headroom included."""
    try:
        row, _ = rpc(http, chain, "eth_getTransactionByHash", [tx_hash])
    except Exception:  # noqa: BLE001
        return None
    if not row or not row.get("value"):
        return None
    return Decimal(int(row["value"], 16)) / Decimal(10 ** 18)


def sync_ledgers(http=None) -> dict:
    """Arrived proof rows -> route ledger + bridge fee receipts. Idempotent by tx.

    Only ``reason == "arrived"`` opens an edge: a source-chain confirmation is
    not an arrival (lessons 4.1).
    """
    from arb_monitor.xstocks_v2 import scan
    from arb_monitor.xstocks_v2.store import read_jsonl
    http = http or Http()
    report = json.loads(PROOF.read_text())
    seen = {(row.get("source_chain"), row.get("dest_chain"), row.get("tx") or row.get("x_layer_tx"))
            for row in read_jsonl(ROUTE_LEDGER)}
    fees = json.loads(FEES_FILE.read_text())
    added_routes, added_fees = [], []
    for row in report["rows"]:
        if row.get("reason") != "arrived" or not row.get("tx"):
            continue
        source, dest, tx = row["source"], row["dest"], row["tx"]
        symbol = row.get("arrived_symbol") or "SPYx"
        if (source, dest, tx) not in seen:
            scan.record_route_sample(scan.route_sample(
                symbol=symbol, source_chain=source, dest_chain=dest, arrived=True,
                source="edge_send_min_proof",
                extra={"tx": tx, "arrived_tokens": row.get("arrived_tokens"),
                       "message_id": row.get("message_id", "")}), ROUTE_LEDGER)
            added_routes.append(f"{symbol} {source}->{dest}")
        key = f"{source}->{dest}"
        edge = fees["edges"].get(key) or {}
        if tx in (edge.get("evidence") or []):
            continue
        asset = SOURCE_ASSET.get(source)
        amount = None
        if row.get("fee_native"):
            amount = Decimal(str(row["fee_native"]).split()[0])
        elif source in ("eth", "x-layer"):
            amount = _paid_value(http, source, tx)
        if asset is None or amount is None or amount <= 0:
            continue
        evidence = list(edge.get("evidence") or []) + [tx]
        fees["edges"][key] = {
            **edge, "fee_asset": asset,
            "fee_amount": format(max(amount, Decimal(str(edge.get("fee_amount") or 0))), "f"),
            "samples": int(edge.get("samples") or 0) + 1, "evidence": evidence,
            "measured_ms": int(time.time() * 1000),
            "note": edge.get("note") or "value paid by the min-proof send (incl. 2% headroom)",
        }
        added_fees.append(key)
    if added_fees:
        FEES_FILE.write_text(json.dumps(fees, ensure_ascii=False, indent=1) + "\n")
    return {"routes": added_routes, "fees": added_fees}


USDC_ETH = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
NATIVE = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
OKX_APPROVE_PROXY = "0x40aa958dd87fc8305b97f2ba922cddca374bcd7f"
#: The wallet reserves ``gas limit x its own fee cap`` before signing (A94). A
#: USDC -> ETH swap used 162 000 gas (2026-10-09, ``0xd866c61d…``) and an
#: approve about 56 000, so these limits keep the reserve small enough to sign
#: from a near-empty balance while leaving room for the real use.
SWAP_GAS_LIMIT = 220_000
APPROVE_GAS_LIMIT = 70_000
GAS_TOPUP_MAX_USDC = Decimal("2")


def _contract_call(chain: str, to: str, data: str, gas_limit: int, value: int = 0) -> str:
    args = ["wallet", "contract-call", "--chain", ONCHAINOS_CHAIN[chain], "--to", to,
            "--input-data", data, "--gas-limit", str(gas_limit), "--from", EVM_WALLET]
    if value:
        args += ["--amt", str(value)]
    out = onchainos(args)
    tx_hash = str(((out.get("data") or {}) if out.get("ok") else {}).get("txHash") or "")
    if not tx_hash.startswith("0x"):
        raise RuntimeError(f"wallet refused: {str(out.get('error'))[:200]}")
    return tx_hash


def eth_gas_topup(http, *, usdc: Decimal) -> dict:
    """USDC -> ETH on Ethereum, signed with tight gas limits. Never above 2 USDC."""
    if usdc <= 0 or usdc > GAS_TOPUP_MAX_USDC:
        return {"ok": False, "reason": f"amount_out_of_range:{usdc}"}
    raw = int(usdc * Decimal(10 ** 6))
    before = native_balance(http, "eth")
    if allowance(http, "eth", USDC_ETH, OKX_APPROVE_PROXY) < raw:
        data = "0x095ea7b3" + "0" * 24 + OKX_APPROVE_PROXY[2:] + format(raw, "064x")
        tx = _contract_call("eth", USDC_ETH, data, APPROVE_GAS_LIMIT)
        receipt = wait_receipt(http, "eth", tx)
        if not receipt or receipt.get("status") != "0x1":
            return {"ok": False, "reason": "approve_not_confirmed", "approve_tx": tx}
        log(f"approve {tx}")
    out = onchainos(["swap", "swap", "--chain", "ethereum", "--from", USDC_ETH, "--to", NATIVE,
                     "--amount", str(raw), "--slippage", "1", "--wallet", EVM_WALLET,
                     "--gas-level", "slow"])
    rows = out.get("data") or [{}]
    row = rows[0] if isinstance(rows, list) else rows
    tx_fields = row.get("tx") or {}
    if not out.get("ok") or str(tx_fields.get("to") or "").lower() != "0x8feab81d36e7576107d5de0758c1b839be31b4f6":
        return {"ok": False, "reason": f"swap_data:{str(out.get('error'))[:160]}"}
    gas_limit = min(int(tx_fields.get("gas") or SWAP_GAS_LIMIT), SWAP_GAS_LIMIT)
    call = {"from": EVM_WALLET, "to": tx_fields["to"], "value": "0x0", "data": tx_fields["data"]}
    _r, error = rpc(http, "eth", "eth_call", [call, "latest"])
    if error:
        return {"ok": False, "reason": f"swap_dry_run:{EVM.decode_revert_reason(error)}"}
    tx = _contract_call("eth", tx_fields["to"], tx_fields["data"], gas_limit)
    receipt = wait_receipt(http, "eth", tx)
    after = native_balance(http, "eth")
    ok = bool(receipt and receipt.get("status") == "0x1" and after > before)
    return {"ok": ok, "swap_tx": tx, "eth_before": str(Decimal(before) / Decimal(10 ** 18)),
            "eth_after": str(Decimal(after) / Decimal(10 ** 18))}


def mark_main(argv: list[str]) -> int:
    """``mark --source eth --dest ink arrived_tokens=0.0006 reason=arrived``."""
    parser = argparse.ArgumentParser()
    parser.add_argument("mode")
    parser.add_argument("--source", required=True)
    parser.add_argument("--dest", required=True)
    parser.add_argument("fields", nargs="+")
    args = parser.parse_args(argv)
    fields = dict(item.split("=", 1) for item in args.fields)
    record(routes.norm(args.source), routes.norm(args.dest), **fields)
    log(f"recorded {args.source}->{args.dest}: {fields}")
    if fields.get("reason") == "arrived":
        log(f"ledgers: {sync_ledgers()}")
    return 0


def main() -> int:
    if len(sys.argv) > 2 and sys.argv[1] == "eth-gas":
        result = eth_gas_topup(Http(), usdc=Decimal(sys.argv[2]))
        log(json.dumps(result))
        return 0 if result["ok"] else 1
    if len(sys.argv) > 1 and sys.argv[1] == "ledger":
        log(json.dumps(sync_ledgers(), ensure_ascii=False))
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "mark":
        return mark_main(sys.argv[1:])
    if len(sys.argv) > 1 and sys.argv[1] in ("solplan", "solonce"):
        return solana_main(sys.argv[1:])
    if len(sys.argv) > 1 and sys.argv[1] == "redeem":
        parser = argparse.ArgumentParser()
        parser.add_argument("mode")
        parser.add_argument("--wrapper", required=True)
        parser.add_argument("--confirm-live", action="store_true")
        args = parser.parse_args()
        return redeem(Http(), wrapper=args.wrapper.lower(), confirm=args.confirm_live)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "once"))
    parser.add_argument("--source", required=True)
    parser.add_argument("--dest", required=True)
    parser.add_argument("--tokens", required=True, help="xStock amount, 18 decimals")
    parser.add_argument("--token", default=SPYX)
    parser.add_argument("--symbol", default="SPYx")
    parser.add_argument("--confirm-live", action="store_true")
    args = parser.parse_args()
    source, dest = routes.norm(args.source), routes.norm(args.dest)
    amount = int(Decimal(args.tokens) * Decimal(10 ** 18))
    http = Http()
    plan = prepare(http, source=source, dest=dest, amount=amount, token=args.token,
                   symbol=args.symbol)
    shown = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in plan.items() if k != "data"}
    log(json.dumps(shown, ensure_ascii=False))
    if plan["problems"]:
        log(f"REFUSED {source}->{dest}: {plan['problems']}")
        return 2
    if args.mode == "plan":
        return 0
    if not args.confirm_live:
        raise SystemExit("once needs --confirm-live")
    result = send(http, plan)
    log(json.dumps(result))
    fee_native = format(Decimal(plan["fee_wei"]) / Decimal(10 ** 18), "f")
    record(source, dest, sent=bool(result["sent"]), tx=result.get("tx", ""),
           reason=result["reason"], constructed=True,
           cost_usdt=format(plan["cost_usdt"].quantize(Decimal("0.001")), "f"),
           sent_tokens=format(Decimal(amount) / Decimal(10 ** 18), "f"),
           fee_native=f"{fee_native} {FEE_ASSET[source]}",
           message_id=result.get("message_id", ""), arrived_tokens=None)
    return 0 if result["sent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
