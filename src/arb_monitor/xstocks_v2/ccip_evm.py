"""EVM -> Solana CCIP send for xStocks: construct, decode, validate.

The first of the three dialects (spec section 7) and the one that already has a
successful sample: ``0x0000000000000000000000000000000000000000000000000000000000000000``
on X Layer, 2026-09-26. It is a *manual* transaction -- the repo never had code
for it, which is why the X Layer -> Solana direction could not be practised
(operator, 2026-10-07).

Call shape::

    send(uint64 destinationChainSelector,
         bytes32 receiver,        # our Solana pubkey, 32 bytes
         address token,           # the native xStock on THIS source chain
         uint256 amount,          # 18 decimals, source chain scale
         bytes extraArgs)

``extraArgs`` is **not** Chainlink's tagged generic SVM structure. It is the
issuer's own ``abi.encode(uint64 bitmap, bytes32[] accounts)`` with **bitmap
772** and **11 accounts**. The bitmap is metadata for the *destination*
(SVM) execution: it marks accounts 2, 8 and 9 writable.

What may never be reused across chains or tokens (lessons 4.1/4.2 -- copying the
wrong table locks the coins):

* slot 3 embeds the **source chain selector**; it changes with the source chain.
* slots 4, 5, 8, 9 are per token (token config, mint, custody, our ATA).
* the **destination ATA must already exist** before the message executes, or
  Token-2022 answers ``IncorrectProgramId`` and the coins stay locked.

Nothing here signs or broadcasts. The fee (``msg.value``) must be read per
transaction: on X Layer it was ``0.000791242725185203 OKB`` and **value = 0
reverts**.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from arb_monitor.core.evm import keccak256
from arb_monitor.xstocks_v2 import ccip_solana as svm
from arb_monitor.xstocks_v2.routes import norm as chain_norm

SEND_SIGNATURE = "send(uint64,bytes32,address,uint256,bytes)"
SEND_SELECTOR = keccak256(SEND_SIGNATURE.encode())[:4]

BRIDGE_CONTRACT = "0x9ec0e4a4c411493773e01e2abf4d42395788846b"
SOLANA_CHAIN_SELECTOR = 124615329519749607
BITMAP_SVM = 772                     # writable: accounts 2, 8, 9
ACCOUNT_COUNT = 11

#: The second dialect: **EVM → EVM**. It is *not* the issuer's bitmap+accounts
#: blob (that one is the destination-execution metadata for Solana); the docs
#: call it "标准 CCIP extraArgs" (``docs/xstocks-v2-ccip-solana-2026-10-07.md``,
#: dialect table). Chainlink's tagged generic args are the standard since CCIP
#: 1.2: ``GenericExtraArgsV2 = 0x181dcf10 || abi.encode(uint256 gasLimit, bool
#: allowOutOfOrderExecution)``.
#:
#: Measured 2026-10-09 on X Layer (read-only ``eth_call`` with the fee paid from
#: a state override): the bridge contract reaches its own fee check and forwards
#: to the CCIP router for *every* candidate the repo could think of (empty,
#: tagged v2, tagged v1, and the Solana blob all answer ``InsufficientFee``
#: first). So the dialect cannot be told apart by simulation -- the probe on the
#: wire is what settles it, and the standard tagged struct is what we send.
GENERIC_EXTRA_ARGS_V2_TAG = 0x181DCF10
EVMExtraArgsV2_DEFAULT_GAS = 400_000


def encode_extra_args_evm(*, gas_limit: int = EVMExtraArgsV2_DEFAULT_GAS,
                          allow_out_of_order: bool = True) -> bytes:
    """``0x181dcf10 || abi.encode(uint256 gasLimit, bool allowOutOfOrderExecution)``."""
    if gas_limit <= 0:
        raise ValueError("gas limit must be positive")
    return (GENERIC_EXTRA_ARGS_V2_TAG.to_bytes(4, "big")
            + (gas_limit).to_bytes(32, "big")
            + (1 if allow_out_of_order else 0).to_bytes(32, "big"))


def is_evm_destination(chain: str) -> bool:
    return chain_norm(chain) in EVM_DESTINATIONS

# Chain selectors and the slot-3 lane account, both measured 2026-09-26.
SOURCE_SELECTORS = {
    "eth": 5009297550715157269,
    "bsc": 11344663589394136015,
    "arbitrum": 4949039107694359620,
    "x-layer": 3016212468291539606,
    "optimism": 3734403246176062136,
    "hyperevm": 2442541497099098535,
    "ink": 3461204551265785888,
}
SOURCE_LANE_ACCOUNT = {
    "eth": "6VU9T42q3bGUTmoZ8LA5rxZ2Tdom86uLekj5c2N4w8mp",
    "bsc": "EAcd4PLvs42et73FSVoCCfbuXshZ9AE29yN5H6Cic8dz",
    "arbitrum": "5rjC32Zd4ugbU74ttkWubH836hSA7oBcCH9neeF2iY5p",
    "x-layer": "5RxSBZJZ1Qai79MPrGTp6qiqp4Bw99F2nfB2ZPjsB5P5",
    "optimism": "9eXPiYQzGiCHE7K5CTfhf4TCny7TFdFeWTfgiyyiKWai",
    "hyperevm": "76xfTatJgf2GEFHmkPkkWZXmwbSyXp6yLYsR6XwSJdRz",
    "ink": "B5pRXw8nqyFc9EjkfdGrL2KdA4r1oQuY458RMkf9EAp1",
}

#: Slot 0 of the 11 accounts **changes with the source chain**. Read
#: 2026-10-09 from successful CCIP offramp executions on Solana (the
#: issuer-program inner instruction, accounts 3..13 = slots 0..10), one per
#: source lane, e.g. eth ``5uRJQCLQZaqE4hcv…``, x-layer ``vE2iU1a4XHQM7sak…``.
#: All are 8-byte accounts of the issuer program with the same discriminator.
#: Optimism had no execution in the last 25 lane signatures: it is absent, and
#: a missing slot is a refusal, never the X Layer value.
SOURCE_SLOT0 = {
    "x-layer": "Eqebtzd3ZeZkML1DHqwo9tViEcLXrkohqJ4McwgtEJCo",
    "eth": "65KwH3jCiXwboz2zk1CEDHG8w28rDr4f299t8Sq5vUPs",
    "bsc": "A7CJyJZhAmiv9CfJ2pZVw5zTuAPECQsvq8dkhiAMSUJM",
    "arbitrum": "GR5USh7Jo54evmHfaf5ht3BLXxJUURCZEzDRhhqiuNXH",
    "hyperevm": "6zeDkAqMbsbJpUpFcfTgFqk7gyvULNjpLktZeRLDRjAy",
    "ink": "2FcHr2jptYZCEwC3kdXKLkRE2JSWvTeaVkGjx19Pvzti",
}
#: Slots 1, 2 and 7 are the same for every source lane (same six executions).
SHARED_SLOTS = {
    1: "3VLdCUjPeBoyFbXQoQK1BpCd79jsCzdeJSnoifM2qN3B",
    2: "DVhKqb6u4Ukrj217Goi8HtCiJ8yLBUaoVJQpy4hYgtDr",
    7: "8sBYczs8BUvFpWXn8rM5uY2UHUEizC3J1QXjpfdPDJJ8",
}

#: Chains the issuer's bridge can deliver to that are **not** Solana. The
#: ``bridges`` table in ``data/xstocks_book.sqlite`` lists the destinations the
#: contract itself advertises: Ethereum, BinanceSmartChain, Arbitrum, Solana.
#: A CCIP chain selector identifies a *chain*, not a direction, so the source
#: table above is the right value for the destination argument too.
EVM_DESTINATIONS = ("eth", "bsc", "arbitrum", "optimism", "hyperevm", "ink",
                    "x-layer")

# The only table we have a success sample for. Anything still holding one of
# these values while the mint says otherwise is a refusal, not a warning.
MRNAX_SAMPLE = {
    "mint": "XsViHjyRbiSBskjizb1gszGTmZqtAH8TuqwJ7sB5AcP",
    "token_config": "9Unx3Ey7EfnXVGbPST7aGjN6Gqbvye6wQb2wLJ19cCNf",
    "custody": "9awzznyBPLZRr8AJUT1BgD7JpPBtCobPYFrL7CNefU7b",
    "ata": "67asuidxpJYv1FxGmekDPPyixCU7zxtQTNu4QjxLy2xs",
    "accounts": (
        "Eqebtzd3ZeZkML1DHqwo9tViEcLXrkohqJ4McwgtEJCo",
        "3VLdCUjPeBoyFbXQoQK1BpCd79jsCzdeJSnoifM2qN3B",
        "DVhKqb6u4Ukrj217Goi8HtCiJ8yLBUaoVJQpy4hYgtDr",
        "5RxSBZJZ1Qai79MPrGTp6qiqp4Bw99F2nfB2ZPjsB5P5",
        "9Unx3Ey7EfnXVGbPST7aGjN6Gqbvye6wQb2wLJ19cCNf",
        "XsViHjyRbiSBskjizb1gszGTmZqtAH8TuqwJ7sB5AcP",
        "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
        "8sBYczs8BUvFpWXn8rM5uY2UHUEizC3J1QXjpfdPDJJ8",
        "9awzznyBPLZRr8AJUT1BgD7JpPBtCobPYFrL7CNefU7b",
        "67asuidxpJYv1FxGmekDPPyixCU7zxtQTNu4QjxLy2xs",
        "AmkjszArkzSKfSUqqgFJLGe6ytjhJxjHFaaVrCxAf2ev",
    ),
}


# ---------------------------------------------------------------- abi
def _word(value: int) -> bytes:
    if not 0 <= value < 2**256:
        raise ValueError("word out of range")
    return value.to_bytes(32, "big")


def address_word(address: str) -> bytes:
    text = str(address or "").strip()
    if text.startswith(("0x", "0X")):
        text = text[2:]
    if len(text) != 40:
        raise ValueError(f"EVM address must be 20 bytes, got {len(text) // 2}")
    try:
        raw = bytes.fromhex(text)
    except ValueError as exc:
        raise ValueError(f"EVM address is not hex: {address!r}") from exc
    return b"\x00" * 12 + raw


def encode_extra_args(accounts: Sequence[object], *, bitmap: int = BITMAP_SVM) -> bytes:
    """``abi.encode(uint64 bitmap, bytes32[] accounts)``."""
    if len(accounts) != ACCOUNT_COUNT:
        raise ValueError(f"{len(accounts)} accounts, expected {ACCOUNT_COUNT}")
    return (_word(bitmap) + _word(64) + _word(len(accounts))
            + b"".join(svm.pubkey(item) for item in accounts))


def decode_extra_args(data: bytes) -> tuple[int, tuple[bytes, ...]]:
    """Strict decode. Anything that is not this exact tuple raises."""
    raw = bytes(data)
    if len(raw) < 96 or (len(raw) - 96) % 32:
        raise ValueError(f"extraArgs is {len(raw)} bytes, not a (uint64, bytes32[]) blob")
    bitmap = int.from_bytes(raw[0:32], "big")
    if int.from_bytes(raw[32:64], "big") != 64:
        raise ValueError("array offset is not 0x40")
    count = int.from_bytes(raw[64:96], "big")
    if 96 + 32 * count != len(raw):
        raise ValueError(f"declared {count} accounts do not fill {len(raw)} bytes")
    accounts = tuple(raw[96 + 32 * i:128 + 32 * i] for i in range(count))
    return bitmap, accounts


def build_calldata(*, dest_selector: int, receiver: object, token: str,
                   amount: int, extra_args: bytes) -> str:
    """The full ``send(...)`` calldata, hex encoded with ``0x``."""
    if not 0 <= dest_selector < 2**64:
        raise ValueError("destination chain selector out of range")
    if amount <= 0:
        raise ValueError("amount must be positive")
    receiver = svm.pubkey(receiver)
    if receiver == b"\x00" * 32:
        raise ValueError("receiver is all zero: the message would be unpayable")
    body = bytes(extra_args)
    head = (_word(dest_selector) + receiver + address_word(token) + _word(amount)
            + _word(160))
    tail = _word(len(body)) + body + b"\x00" * ((-len(body)) % 32)
    return "0x" + (SEND_SELECTOR + head + tail).hex()


@dataclass(frozen=True)
class SendCall:
    dest_selector: int
    receiver: bytes
    token: str
    amount: int
    #: ``evm_to_solana`` carries the issuer's bitmap + 11 accounts;
    #: ``evm_to_evm`` carries Chainlink's tagged generic args and has neither.
    dialect: str
    bitmap: int | None
    accounts: tuple[bytes, ...]


def decode_calldata(data: str) -> SendCall:
    """Read back exactly what is about to be sent, before signing."""
    raw = bytes.fromhex(str(data).removeprefix("0x"))
    if raw[:4] != SEND_SELECTOR:
        raise ValueError(f"not a CCIP send: {raw[:4].hex()}")
    args = raw[4:]
    if len(args) < 160 + 32:
        raise ValueError("truncated calldata")
    dest = int.from_bytes(args[0:32], "big")
    receiver = args[32:64]
    if args[64:76] != b"\x00" * 12:
        raise ValueError("token word has non-zero padding")
    token = "0x" + args[76:96].hex()
    amount = int.from_bytes(args[96:128], "big")
    offset = int.from_bytes(args[128:160], "big")
    if offset != 160:
        raise ValueError(f"bytes offset {offset} != 160")
    length = int.from_bytes(args[160:192], "big")
    body = args[192:192 + length]
    if len(body) != length:
        raise ValueError("truncated extraArgs")
    if body[:4] == GENERIC_EXTRA_ARGS_V2_TAG.to_bytes(4, "big"):
        # The second dialect: Chainlink's tagged generic args. There is no
        # bitmap and no account list, and reading them out of these bytes would
        # invent both.
        return SendCall(dest, receiver, token, amount, "evm_to_evm", None, ())
    bitmap, accounts = decode_extra_args(body)
    return SendCall(dest, receiver, token, amount, "evm_to_solana", bitmap,
                    accounts)


# ---------------------------------------------------------------- table
def build_accounts(*, source_chain: str, mint: object, token_config: object,
                   custody: object, owner: object,
                   destination_ata: object | None = None,
                   source_lane: object | None = None,
                   per_token: Sequence[object] | None = None) -> tuple[str, ...]:
    """The 11 accounts in order.

    Slots 4 (token config), 5 (mint), 8 (custody), 9 (our ATA) and 10 (owner)
    are derived or read out of the token config. Slots 1, 2 and 7 are the
    receiving program's own accounts (``SHARED_SLOTS``). Slot 0 depends on the
    source chain (``SOURCE_SLOT0``); a chain without a measured slot 0 raises.
    Slot 3 is the source-lane account.
    """
    chain = chain_norm(source_chain)
    lane = svm.pubkey(source_lane) if source_lane else svm.pubkey(
        SOURCE_LANE_ACCOUNT.get(chain) or "")
    mint_key = svm.pubkey(mint)
    ata = svm.pubkey(destination_ata) if destination_ata else \
        svm.associated_token_address(owner, mint_key)
    if per_token is None:
        slot0 = SOURCE_SLOT0.get(chain)
        if slot0 is None:
            raise ValueError(f"no measured slot 0 for source chain {chain}")
        per_token = (slot0, SHARED_SLOTS[1], SHARED_SLOTS[2], SHARED_SLOTS[7])
    if len(per_token) != 4:
        raise ValueError("per_token needs slots 0, 1, 2 and 7")
    return (
        svm.to_b58(per_token[0]), svm.to_b58(per_token[1]),
        svm.to_b58(per_token[2]), svm.to_b58(lane),
        svm.to_b58(token_config), svm.to_b58(mint_key),
        svm.TOKEN_2022, svm.to_b58(per_token[3]),
        svm.to_b58(custody), svm.to_b58(ata), svm.to_b58(owner),
    )


def validate_accounts(accounts: Sequence[object], *, source_chain: str,
                      mint: object, custody: object, owner: object,
                      destination_ata: object | None = None) -> list[str]:
    """Every problem, as stable strings. Empty list is good."""
    problems: list[str] = []
    if len(accounts) != ACCOUNT_COUNT:
        return [f"account_count:{len(accounts)}"]
    keys = [svm.to_b58(item) for item in accounts]
    chain = chain_norm(source_chain)
    mint_key = svm.to_b58(mint)
    ata = svm.to_b58(destination_ata) if destination_ata else \
        svm.to_b58(svm.associated_token_address(owner, mint_key))
    if keys[3] != SOURCE_LANE_ACCOUNT.get(chain):
        problems.append(f"lane_slot:{chain}")
    if keys[0] != SOURCE_SLOT0.get(chain):
        problems.append(f"slot0_not_source_chain:{chain}")
    for index, expected in SHARED_SLOTS.items():
        if keys[index] != expected:
            problems.append(f"shared_slot:{index}")
    if keys[5] != mint_key:
        problems.append("mint_slot")
    if keys[8] != svm.to_b58(custody):
        problems.append("custody_slot")
    if keys[9] != ata:
        problems.append("destination_ata_slot")
    if keys[10] != svm.to_b58(owner):
        problems.append("owner_slot")
    if keys[6] != svm.TOKEN_2022:
        problems.append("token_program_slot")
    # a table still carrying the MRNAx sample for a different mint is the exact
    # shape of the 2026-09-26 loss
    if mint_key != MRNAX_SAMPLE["mint"]:
        for index, sample in ((4, MRNAX_SAMPLE["token_config"]),
                              (8, MRNAX_SAMPLE["custody"]),
                              (9, MRNAX_SAMPLE["ata"])):
            if keys[index] == sample:
                problems.append(f"mrna_sample_leftover:{index}")
    return problems


def validate_call(call: SendCall, *, source_chain: str, mint: object,
                  custody: object, owner: object, amount: int) -> list[str]:
    if call.dialect != "evm_to_solana":
        # The Solana validator reads a bitmap and 11 Solana accounts out of the
        # message. An EVM-destination message has neither, and answering
        # ``account_count:0`` would read like a truncated account table rather
        # than "this is the other dialect".
        return [f"wrong_dialect:{call.dialect}"]
    problems = list(validate_accounts(list(call.accounts), source_chain=source_chain,
                                      mint=mint, custody=custody, owner=owner))
    if call.bitmap != BITMAP_SVM:
        problems.append(f"bitmap:{call.bitmap}")
    if call.dest_selector != SOLANA_CHAIN_SELECTOR:
        problems.append(f"dest_selector:{call.dest_selector}")
    if not call.receiver or call.receiver == b"\x00" * 32:
        problems.append("receiver_all_zero")
    if call.receiver != svm.pubkey(owner):
        problems.append("receiver_not_owner")
    if call.token == "0x" + "00" * 20:
        problems.append("token_all_zero")
    elif len(call.token) != 42:
        problems.append(f"token_shape:{call.token}")
    if call.amount != amount:
        problems.append(f"amount:{call.amount}")
    return problems


def validate_call_evm(call: SendCall, *, dest_chain: str, receiver: str,
                      amount: int) -> list[str]:
    """Every problem with an **EVM-destination** call, as stable strings.

    The EVM dialect has no bitmap and no account list, so the checks are the
    ones that can actually lose coins here: the lane selector, a receiver that
    really is a 20-byte address, and an amount that matches what we sized.
    """
    problems: list[str] = []
    if call.dialect != "evm_to_evm":
        return [f"wrong_dialect:{call.dialect}"]
    expected = DEST_SELECTORS.get(chain_norm(dest_chain))
    if expected is None:
        problems.append(f"no_dest_selector:{dest_chain}")
    elif int(call.dest_selector) != int(expected):
        problems.append("dest_selector_mismatch")
    raw = bytes(call.receiver)
    if len(raw) != 32 or raw[:12] != b"\x00" * 12:
        problems.append("receiver_not_an_evm_address")
    else:
        wanted = str(receiver or "").strip().lower()
        if not (wanted.startswith("0x") and len(wanted) == 42):
            problems.append("receiver_malformed")
        elif bytes.fromhex(wanted[2:]) != raw[12:]:
            problems.append("receiver_mismatch")
    if call.bitmap is not None or call.accounts:
        problems.append("solana_metadata_on_evm_call")
    if amount <= 0 or int(call.amount) != int(amount):
        problems.append("amount_mismatch")
    return problems


def build_call(*, source_chain: str, token: str, amount: int, receiver: object,
               accounts: Sequence[object],
               dest_selector: int = SOLANA_CHAIN_SELECTOR,
               bitmap: int = BITMAP_SVM) -> tuple[str, SendCall]:
    """``(calldata, decoded)`` for a send we are about to broadcast."""
    data = build_calldata(dest_selector=dest_selector, receiver=receiver,
                          token=token, amount=amount,
                          extra_args=encode_extra_args(accounts, bitmap=bitmap))
    return data, decode_calldata(data)


def fee_guard(fee_wei: int | None) -> tuple[bool, str]:
    """Value 0 reverts on the source chain: the fee has to be read, not assumed."""
    if fee_wei is None:
        return False, "fee_unknown"
    if fee_wei <= 0:
        return False, "fee_zero"
    return True, "ok"


# ---------------------------------------------------------------- fee quote
#: ``InsufficientFee(uint256 provided, uint256 required)``, the source bridge's
#: own error. Measured 2026-10-09 on X Layer with a real ``send`` calldata: the
#: dry run answers
#: ``0x785fa129 <provided> <required>`` -- two words, and the second one **is**
#: the fee. The same call at exactly ``required`` succeeds and returns the
#: message id, so the boundary is exact rather than approximate.
#:
#: A dry run at ``value = 0`` is how the fee gets read: the bridge asks for the
#: fee *before* it spends the value, so the answer does not depend on our
#: balance, and it does not depend on the allowance either once the token call
#: is stubbed (see ``FEE_QUOTE_STUB``).
FEE_REVERT_SELECTOR = "785fa129"

#: Bytecode that answers every call with a 32-byte ``1``. It replaces the token
#: contract for the fee quote only, because the bridge transfers the token
#: *before* it checks the fee (measured: with the real token and zero allowance
#: the dry run answers ``ERC20: insufficient allowance`` at every value). The
#: stub must not change the fee: it only says "the transfer succeeded".
FEE_QUOTE_STUB = "0x600160005260206000f3"

#: One xStock is deployed at the same address on every EVM chain (SPYx here).
#: Measured 2026-10-09: the fee is identical for SPYx and NVDAx, and identical
#: for 0.001, 0.01 and 1.0 tokens -- it is per message, per lane.
FEE_QUOTE_TOKEN = "0x90a2a4c76b5d8c0bc892a69ea28aa775a8f2dd48"
FEE_QUOTE_AMOUNT = 10 ** 16
FEE_QUOTE_RECEIVER = "AmkjszArkzSKfSUqqgFJLGe6ytjhJxjHFaaVrCxAf2ev"

#: Destination chain selectors we can build a ``send`` message for.
DEST_SELECTORS = {
    "solana": SOLANA_CHAIN_SELECTOR,
    **{chain: SOURCE_SELECTORS[chain] for chain in EVM_DESTINATIONS
       if chain in SOURCE_SELECTORS},
}


def build_call_evm(*, source_chain: str, dest_chain: str, token: str, amount: int,
                   receiver: str,
                   gas_limit: int = EVMExtraArgsV2_DEFAULT_GAS,
                   allow_out_of_order: bool = True) -> tuple[str, "SendCall"]:
    """``send(...)`` for an **EVM destination** — the second dialect.

    The receiver is the destination-side EVM address, left padded to 32 bytes.
    Nothing here borrows from the Solana dialect: no bitmap, no 11 accounts, no
    destination ATA. Getting that wrong locks coins on the far side (lessons
    4.1/4.2), so the two constructors are deliberately separate functions.
    """
    chain = chain_norm(dest_chain)
    selector = DEST_SELECTORS.get(chain)
    if selector is None:
        raise ValueError(f"no destination selector for {dest_chain}")
    address = str(receiver or "").strip().lower()
    if not (address.startswith("0x") and len(address) == 42):
        raise ValueError(f"receiver is not an EVM address: {receiver!r}")
    raw = bytes.fromhex(address[2:])
    receiver_word = b"\x00" * 12 + raw
    extra = encode_extra_args_evm(gas_limit=gas_limit,
                                  allow_out_of_order=allow_out_of_order)
    calldata = build_calldata(dest_selector=selector, receiver=receiver_word,
                              token=token, amount=amount, extra_args=extra)
    return calldata, SendCall(dest_selector=selector, receiver=receiver_word,
                              token=token, amount=amount,
                              dialect="evm_to_evm",
                              bitmap=None, accounts=())
#: Which gas token pays the fee on each source chain.
#: What pays the bridge fee on each source. L2s pay their own native ETH, so
#: they share the ETH price. HyperEVM pays HYPE. A chain absent from this map
#: cannot be quoted, and an unquoted fee is not zero.
FEE_SOURCE_ASSET = {
    "x-layer": "OKB",
    "eth": "ETH",
    "bsc": "BNB",
    "arbitrum": "ETH",
    "optimism": "ETH",
    "ink": "ETH",
    "hyperevm": "HYPE",
}


def decode_fee_revert(data: object) -> tuple[int, int] | None:
    """``(provided, required)`` from the bridge's fee revert, or ``None``.

    Anything that is not exactly that shape returns ``None``: a different
    revert means the fee is *unknown*, and an unknown fee is a refusal.
    """
    text = str(data or "")
    if not text.startswith("0x"):
        return None
    blob = text[2:]
    if len(blob) < 8 + 128 or blob[:8].lower() != FEE_REVERT_SELECTOR:
        return None
    try:
        provided = int(blob[8:72], 16)
        required = int(blob[72:136], 16)
    except ValueError:
        return None
    return provided, required


def fee_quote(*, http, rpc: str, source_chain: str, sender: str,
              dest_chain: str = "solana", token: str | None = None,
              amount: int | None = None,
              receiver: object = FEE_QUOTE_RECEIVER) -> tuple[Decimal | None, str]:
    """Read this lane's bridge fee from the chain, in the source gas token.

    One ``eth_call`` per edge per round. It replaces a hand-refreshed table as
    the *source* of the fee: ``data-v2/xstocks_v2_bridge_fees.json`` keeps the
    receipts, and stays the answer of record when the chain cannot be asked.

    Returns ``(Decimal native, source_tag)`` or ``(None, why it is unknown)``.
    Nothing here signs, and ``value`` is always zero -- so the quote cannot
    spend anything even if the caller is wrong about the rest.
    """
    if chain_norm(source_chain) not in FEE_SOURCE_ASSET:
        return None, f"no_fee_asset:{chain_norm(source_chain)}"
    if DEST_SELECTORS.get(chain_norm(dest_chain)) is None:
        return None, f"no_dest_selector:{chain_norm(dest_chain)}"
    try:
        data = build_calldata(
            dest_selector=DEST_SELECTORS[chain_norm(dest_chain)],
            receiver=receiver, token=token or FEE_QUOTE_TOKEN,
            amount=int(FEE_QUOTE_AMOUNT if amount is None else amount),
            extra_args=encode_extra_args(MRNAX_SAMPLE["accounts"]))
    except (ValueError, TypeError) as exc:
        return None, f"bad_call:{exc}"
    payload = [{
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [
            {"from": sender, "to": BRIDGE_CONTRACT, "value": "0x0", "data": data},
            "latest",
            # The overrides are what make the read possible: a balance for the
            # sender (so the node's own "insufficient funds" check cannot
            # pre-empt the answer) and a stub token (so the transfer that runs
            # before the fee check cannot fail on our zero allowance).
            {str(sender): {"balance": hex(10 ** 18)},
             str(token or FEE_QUOTE_TOKEN): {"code": FEE_QUOTE_STUB}},
        ],
    }]
    try:
        status, body = http.request(rpc, data=json.dumps(payload).encode(),
                                    headers={"Content-Type": "application/json"})
    except Exception as exc:  # noqa: BLE001 - a read that does not answer
        return None, f"rpc:{type(exc).__name__}"
    if status != 200:
        return None, f"rpc:http_{status}"
    try:
        row = json.loads(body.decode("utf-8", "replace"))[0]
    except (ValueError, IndexError, KeyError, TypeError):
        return None, "rpc:bad_json"
    if row.get("result") is not None:
        # A call that succeeds at value 0 is not the fee we measured; treat it
        # as unknown rather than as "free".
        return None, "dry_run_ok_at_zero_value"
    decoded = decode_fee_revert((row.get("error") or {}).get("data"))
    if decoded is None:
        return None, f"dry_run:{str((row.get('error') or {}).get('message') or '')[:60]}"
    provided, required = decoded
    if required <= 0:
        return None, f"fee_zero:{provided}"
    return (Decimal(required) / Decimal(10 ** 18),
            f"ccip-dryrun:{chain_norm(source_chain)}->{chain_norm(dest_chain)}")


#: ``keccak("Error(string)")[:4]``. A revert that carries it can be read as text;
#: anything else (a custom error, an empty revert) keeps the node's own message.
ERROR_STRING_SELECTOR = "08c379a0"


def decode_revert_reason(error: dict | None) -> str:
    """A short human-readable reason for an ``eth_call`` revert.

    Nodes answer ``{"code":3,"message":"execution reverted: ERC20: insufficient
    allowance","data":"0x08c379a0…"}``. The ``data`` is the ABI-encoded
    ``Error(string)``; when it is missing or a different shape, the message the
    node itself wrote is the fallback. A caller that needs to tell "the approve
    has not run yet" from a real problem needs this text, not a boolean.
    """
    if not isinstance(error, dict):
        return str(error or "")[:200]
    data = str(error.get("data") or "")
    text = _decode_error_string(data)
    if text:
        return text[:200]
    return str(error.get("message") or error)[:200]


def _decode_error_string(data: str) -> str:
    """``Error(string)`` payload -> text, or ``""`` when the shape is not that."""
    if not data.startswith("0x"):
        return ""
    try:
        blob = bytes.fromhex(data[2:])
        if blob[:4].hex() != ERROR_STRING_SELECTOR:
            return ""
        # ``Error(string)`` = selector + ABI-encoded (string): the argument block
        # starts after the selector, its first word is the offset to the string,
        # and the string itself is a length word followed by its bytes.
        head = blob[4:]
        if len(head) < 64:
            return ""
        offset = int.from_bytes(head[0:32], "big")
        if offset > len(head) - 32:
            return ""
        length = int.from_bytes(head[offset:offset + 32], "big")
        text = head[offset + 32:offset + 32 + length]
        if len(text) != length:
            return ""
        return text.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""
