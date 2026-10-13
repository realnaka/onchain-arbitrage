"""Solana -> X Layer CCIP send for xStocks. Construct, decode, validate.

This is the third CCIP dialect (spec section 7). The other two do not apply:

====== ===================================================================
EVM -> Solana  EVM ``send(uint64,bytes32,address,uint256,bytes)``,
               ``bitmap 772``, **11** Solana accounts
EVM -> EVM     standard CCIP ``extraArgs``; no Solana account list at all
Solana -> EVM  **this module**: a Solana program instruction, **27** accounts,
               no bitmap -- the bitmap is metadata for the SVM *destination*
====== ===================================================================

Ground truth is four mainnet transactions, decoded 2026-10-07 (read-only):

* ``REDACTED_SOLANA_SIGNATURE`` (QQQx)
* ``REDACTED_SOLANA_SIGNATURE`` (TSLAx)
* ``REDACTED_SOLANA_SIGNATURE`` (HOODx)
* ``REDACTED_SOLANA_SIGNATURE`` (TSLAx)

All four are Solana -> XLayer (destination selector 3016212468291539606).

Instruction data, 92 bytes, little endian::

    discriminator   8   sha256("global:ccip_send")[:8] == 6cd886bff9ea2154
    dest selector   8   u64 == 3016212468291539606
    receiver    u32+32   xStocks bridge contract on the destination chain,
                         20 byte EVM address left padded to 32
    recipient   u32+20   EVM address that receives the shares
    product id      8   u64 == bridges.products_json[].productId
    amount          8   u64, raw SVM amount (8 decimals)

The xStocks program then CPI-sends through the Chainlink CCIP router
``Ccip842gzYHhvdDkSyi2YVCoAWPbYJoApMFzSxQroE9C`` and emits a CCIP message with
**no token amounts**: the EVM receiver releases shares from its own custody
wallet. The message payload is::

    tokenReceiver 32   left padded EVM address (must not be all zero)
    tokenId        8   productId, big endian
    shares        32   amount scaled 8 -> 18 decimals, big endian

which is exactly what ``BackedCCIPReceiver._ccipReceive`` slices:
``data[0:32]`` / ``data[32:40]`` / ``data[40:72]``.

The fee is paid in **native SOL**, not LINK: ``feeTokenMint`` is wrapped SOL and
``feeTokenUserAssociatedAccount`` is the zero address. Measured 852448 and
744731.9 lamports (per message, amount independent) on 2026-10-07.

Nothing here signs or broadcasts. ``build_instruction`` returns an ordered
account list; the caller owns the transaction and the wallet.
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from typing import Sequence

# --------------------------------------------------------------------------
# base58 (stdlib only; no solana-py in this environment)
# --------------------------------------------------------------------------
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {char: index for index, char in enumerate(_B58)}


def b58encode(raw: bytes) -> str:
    number = int.from_bytes(raw, "big")
    out = ""
    while number:
        number, remainder = divmod(number, 58)
        out = _B58[remainder] + out
    pad = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * pad + out


def b58decode(text: str) -> bytes:
    number = 0
    for char in str(text).strip():
        index = _B58_INDEX.get(char)
        if index is None:
            raise ValueError(f"not base58: {char!r}")
        number = number * 58 + index
    body = number.to_bytes((number.bit_length() + 7) // 8, "big")
    pad = len(str(text).strip()) - len(str(text).strip().lstrip("1"))
    return b"\x00" * pad + body


def pubkey(value: object) -> bytes:
    """Accept a base58 string or raw 32 bytes, always return 32 bytes."""
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    else:
        raw = b58decode(str(value))
    if len(raw) != 32:
        raise ValueError(f"public key must be 32 bytes, got {len(raw)}")
    return raw


def to_b58(value: object) -> str:
    return b58encode(pubkey(value))


def evm_to_bytes32(address: str) -> bytes:
    """``0xabc...`` (20 bytes) -> 32 bytes, left padded. Zero stays zero."""
    text = str(address or "").strip()
    if text.startswith(("0x", "0X")):
        text = text[2:]
    if len(text) != 40:
        raise ValueError(f"EVM address must be 20 bytes, got {len(text) // 2}")
    try:
        raw = bytes.fromhex(text)
    except ValueError as exc:  # bad hex must never look like an address
        raise ValueError(f"EVM address is not hex: {address!r}") from exc
    return b"\x00" * 12 + raw


def bytes32_to_evm(raw: bytes) -> str:
    if len(raw) != 32:
        raise ValueError("expected 32 bytes")
    if raw[:12] != b"\x00" * 12:
        raise ValueError("top 12 bytes are not zero; not a padded EVM address")
    return "0x" + raw[12:].hex()


def is_zero(raw: object) -> bool:
    return pubkey(raw) == b"\x00" * 32


# --------------------------------------------------------------------------
# PDAs (ed25519 on-curve check, exactly the runtime rule)
# --------------------------------------------------------------------------
_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_PDA_MARKER = b"ProgramDerivedAddress"


def is_on_curve(compressed: bytes) -> bool:
    """True when the 32 bytes decompress to a valid ed25519 point."""
    if len(compressed) != 32:
        raise ValueError("point must be 32 bytes")
    y = int.from_bytes(compressed, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    y2 = (y * y) % _P
    numerator = (y2 - 1) % _P
    denominator = (_D * y2 + 1) % _P
    x2 = (numerator * pow(denominator, _P - 2, _P)) % _P
    if x2 == 0:
        return True
    return pow(x2, (_P - 1) // 2, _P) == 1


def create_program_address(seeds: Sequence[bytes], program_id: object) -> bytes:
    digest = hashlib.sha256(
        b"".join(seeds) + pubkey(program_id) + _PDA_MARKER).digest()
    if is_on_curve(digest):
        raise ValueError("seeds land on the ed25519 curve")
    return digest


def find_program_address(seeds: Sequence[bytes], program_id: object) -> tuple[bytes, int]:
    for bump in range(255, -1, -1):
        try:
            return create_program_address(list(seeds) + [bytes([bump])], program_id), bump
        except ValueError:
            continue
    raise ValueError("no viable bump seed")


def associated_token_address(owner: object, mint: object,
                             token_program: object = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb") -> bytes:
    """SPL associated token account: ``[owner, token_program, mint]``."""
    return find_program_address(
        [pubkey(owner), pubkey(token_program), pubkey(mint)], ATA_PROGRAM)[0]


def anchor_discriminator(name: str) -> bytes:
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


# --------------------------------------------------------------------------
# Chain / program constants (measured 2026-10-07)
# --------------------------------------------------------------------------
SOLANA_PROGRAM = "F2ZYgyh8YDG6rHo9F1T8Aw4WoTyy8eYN3GgM2LaPKcZN"
CCIP_ROUTER = "Ccip842gzYHhvdDkSyi2YVCoAWPbYJoApMFzSxQroE9C"
FEE_QUOTER = "FeeQPGkKDeRV1MgoYfMH6L8o3KeuYjwUZrgn4LRKfjHi"
RMN_REMOTE = "RmnXLft1mSEwDgMKu2okYuHkiazxntFFcZFrrcXxYg7"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TOKEN_LEGACY = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
WRAPPED_SOL = "So11111111111111111111111111111111111111112"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
# The measured samples carry a ComputeBudget instruction. Without it the default
# 200 000 CU budget is not enough: the fee quoter runs twice and the router
# bills once more, and the whole `ccip_send` dies with "Computational budget
# exceeded" *after* the token transfer, which is the worst place to fail.
DEFAULT_COMPUTE_UNIT_LIMIT = 400_000

SOLANA_CHAIN_SELECTOR = 124615329519749607
XLAYER_CHAIN_SELECTOR = 3016212468291539606
#: Destinations the issuer's Solana program lists, whose chain selectors are
#: already measured. Monad is on that list and is absent here until a lane
#: account is read — a missing selector is not zero. Mantle is not on the list.
SOLANA_DEST_SELECTORS = {
    "eth": 5009297550715157269,
    "bsc": 11344663589394136015,
    "arbitrum": 4949039107694359620,
    "x-layer": XLAYER_CHAIN_SELECTOR,
    "optimism": 3734403246176062136,
    "hyperevm": 2442541497099098535,
    "ink": 3461204551265785888,
}
XSTOCKS_BRIDGE_EVM = "0x9ec0e4a4c411493773e01e2abf4d42395788846b"
# Token authority of every custody token account, read from the three custody
# accounts we decoded (QQQx, TSLAx, HOODx).
POOL_AUTHORITY = "S7vYFFWH6BjJyEsdrPQpqpYTqLTrPRK6KW3VwsJuRaS"

CCIP_SEND_DISCRIMINATOR = anchor_discriminator("ccip_send")
TOKEN_CONFIG_DISCRIMINATOR = bytes.fromhex("5c49ff2b6b337565")
LANE_CONFIG_DISCRIMINATOR = bytes.fromhex("0db1e98dd41d9438")

# ``token_config`` layout (90 bytes, decoded 2026-10-07):
#   discriminator 8 | productId u64 LE 8 | mint 32 | **custody token account 32**
#   | tail 10
TOKEN_CONFIG_LEN = 90
TOKEN_CONFIG_PRODUCT_ID_OFFSET = 8
TOKEN_CONFIG_MINT_OFFSET = 16
TOKEN_CONFIG_CUSTODY_OFFSET = 48

# ``lane_config`` layout for one destination chain (54 bytes):
#   discriminator 8 | dest chain selector u64 LE 8 | flags 2 | vec u32 4
#   | bridge contract, 20 bytes, right padded to 32
LANE_CONFIG_LEN = 54
LANE_CONFIG_SELECTOR_OFFSET = 8
LANE_CONFIG_BRIDGE_OFFSET = 22


def parse_lane_config(raw: bytes) -> tuple[int, str]:
    """``(dest chain selector, bridge address)`` from one 54-byte lane account.

    Layout, measured: discriminator 8 | selector u64 LE 8 | flags 2 | vec
    length u32 4 | bridge address 20 bytes, right-padded to 32.
    """
    blob = bytes(raw)
    if len(blob) < LANE_CONFIG_LEN:
        raise ValueError(f"lane config is {len(blob)} bytes, need {LANE_CONFIG_LEN}")
    if blob[:8] != LANE_CONFIG_DISCRIMINATOR:
        raise ValueError("lane config discriminator")
    selector = int.from_bytes(
        blob[LANE_CONFIG_SELECTOR_OFFSET:LANE_CONFIG_SELECTOR_OFFSET + 8], "little")
    if selector <= 0:
        raise ValueError("lane config selector is zero")
    address = blob[LANE_CONFIG_BRIDGE_OFFSET:LANE_CONFIG_BRIDGE_OFFSET + 20]
    return selector, "0x" + address.hex()


def register_solana_dest(chain: str, selector: int) -> None:
    """Record a destination selector read from a lane account.

    Refuses a non-positive selector. Does not invent one.
    """
    if int(selector) <= 0:
        raise ValueError("selector must be positive")
    key = str(chain or "").strip().lower().replace("xlayer", "x-layer")
    SOLANA_DEST_SELECTORS[key] = int(selector)


def solana_dest_selector(chain: str) -> int | None:
    key = str(chain or "").strip().lower().replace("xlayer", "x-layer")
    return SOLANA_DEST_SELECTORS.get(key)


def instruction_for_dest(*, dest_chain: str, recipient: str, product_id: int,
                         amount: int, bridge_evm: str = XSTOCKS_BRIDGE_EVM) -> bytes:
    """Solana-source ``ccip_send`` for a destination that has a selector.

    The default ``instruction_data`` still targets X Layer. This is the entry
    that names the destination. A chain with no selector raises; it is not
    sent to X Layer by mistake.
    """
    selector = solana_dest_selector(dest_chain)
    if selector is None:
        raise ValueError(f"no solana destination selector for {dest_chain}")
    return instruction_data(
        recipient=recipient, product_id=product_id, amount=amount,
        dest_chain_selector=selector, bridge_evm=bridge_evm)

# The 27 account table. ``role`` is what the account is; ``index`` is its
# position, measured, not guessed.
ACCOUNT_COUNT = 27
# Live tokens in the message: 1 (signer), 4 (source ATA), 5 (custody ATA),
# 6 (mint), 26 (token config). Everything else is fixed for this lane.
VARIABLE_INDEXES = frozenset({1, 4, 5, 6, 26})

ROLE_CONFIG = "config"
ROLE_SIGNER = "signer"
ROLE_FEE_AUTHORITY = "fee_authority"
ROLE_SYSTEM = "system_program"
ROLE_SOURCE_ATA = "source_token_account"
ROLE_CUSTODY_ATA = "custody_token_account"
ROLE_MINT = "mint"
ROLE_ROUTER_PROGRAM = "ccip_router_program"
ROLE_ROUTER_CONFIG = "ccip_router_config"
ROLE_DEST_CHAIN_STATE = "dest_chain_state"
ROLE_ROUTER_NONCE = "router_nonce"
ROLE_FEE_QUOTER_PROGRAM = "fee_quoter_program"
ROLE_FEE_QUOTER_CONFIG = "fee_quoter_config"
ROLE_FEE_QUOTER_DEST = "fee_quoter_dest_chain"
ROLE_FEE_BILLING_TOKEN = "fee_quoter_billing_token_config"
ROLE_FEE_LINK_TOKEN = "fee_quoter_link_token_config"
ROLE_RMN_PROGRAM = "rmn_remote_program"
ROLE_RMN_CURSES = "rmn_curses"
ROLE_RMN_CONFIG = "rmn_remote_config"
ROLE_FEE_BILLING_SIGNER = "fee_billing_signer"
ROLE_FEE_TOKEN_RECEIVER = "fee_token_receiver"
ROLE_FEE_TOKEN_MINT = "fee_token_mint"
ROLE_FEE_TOKEN_PROGRAM = "fee_token_program"
ROLE_XSTOCK_TOKEN_PROGRAM = "xstock_token_program"
ROLE_SYSTEM_AGAIN = "system_program_again"
ROLE_LANE_CONFIG = "lane_config"
ROLE_TOKEN_CONFIG = "token_config"

# (index, role, pubkey or None, signer, writable)
LANE_ACCOUNTS: tuple[tuple[int, str, str | None, bool, bool], ...] = (
    (0, ROLE_CONFIG, "3VLdCUjPeBoyFbXQoQK1BpCd79jsCzdeJSnoifM2qN3B", False, False),
    (1, ROLE_SIGNER, None, True, True),
    (2, ROLE_FEE_AUTHORITY, "A8JPtC8mYrZKvainsrr3cGhTcoBeUV2vd8SXun55in3u", False, True),
    (3, ROLE_SYSTEM, SYSTEM_PROGRAM, False, False),
    (4, ROLE_SOURCE_ATA, None, False, True),
    (5, ROLE_CUSTODY_ATA, None, False, True),
    (6, ROLE_MINT, None, False, False),
    (7, ROLE_ROUTER_PROGRAM, CCIP_ROUTER, False, False),
    (8, ROLE_ROUTER_CONFIG, "3Yrg9E4ySAeRezgQY99NNarAmFLtixapga9MZb6y2dt3", False, False),
    (9, ROLE_DEST_CHAIN_STATE, "5iP5hCRwPjF7wMjTCaTfRQmcNXrvc1Tu2BKKFJ8NvJSd", False, True),
    (10, ROLE_ROUTER_NONCE, "DEYRxGEPPCvaVJJpmgcK8QSq1yNUAKwHvuA1V9UnVdAT", False, True),
    (11, ROLE_FEE_QUOTER_PROGRAM, FEE_QUOTER, False, False),
    (12, ROLE_FEE_QUOTER_CONFIG, "Jexa4tyW5xjDuucnNpTc3cz3Jsq1qY8fp4P4Dd4NQAb", False, False),
    (13, ROLE_FEE_QUOTER_DEST, "8muJ841RbE71zUWXpCiyR9UPYsku3sbymWS45jDiRu5m", False, False),
    (14, ROLE_FEE_BILLING_TOKEN, "7e8EXjofujRmNxSMwo8CT8yxs1xxTexx55BN62xwWrKN", False, False),
    (15, ROLE_FEE_LINK_TOKEN, "8qWXPpijw2y5kYhJhUvPwyju8baEDdV5jESKrTBdpYy6", False, False),
    (16, ROLE_RMN_PROGRAM, RMN_REMOTE, False, False),
    (17, ROLE_RMN_CURSES, "CpbeEvmTR4UE8CgDDL5b1nqjSz7JCD4wNJhxPLZRkSL1", False, False),
    (18, ROLE_RMN_CONFIG, "GbQFCDTPbhwPjeUvhu7hXEM3Wm2S6t3FCxoCmQtKxetw", False, False),
    (19, ROLE_FEE_BILLING_SIGNER, "2AjuzTy6z2webxEUu7eZ1DkAyLagZaqH2dgzhbBYjJiG", False, True),
    (20, ROLE_FEE_TOKEN_RECEIVER, "9TKiJBGMu1c6aqxaKuzV6G1otYaYM3PLZf5VoUHhjKY2", False, True),
    (21, ROLE_FEE_TOKEN_MINT, WRAPPED_SOL, False, False),
    (22, ROLE_FEE_TOKEN_PROGRAM, TOKEN_LEGACY, False, False),
    (23, ROLE_XSTOCK_TOKEN_PROGRAM, TOKEN_2022, False, False),
    (24, ROLE_SYSTEM_AGAIN, SYSTEM_PROGRAM, False, False),
    (25, ROLE_LANE_CONFIG, "5RxSBZJZ1Qai79MPrGTp6qiqp4Bw99F2nfB2ZPjsB5P5", False, False),
    (26, ROLE_TOKEN_CONFIG, None, False, False),
)

LANE_ROLES = tuple(row[1] for row in LANE_ACCOUNTS)

#: The issuer's lane config account per remote chain. The same account is slot
#: 25 of a Solana-source send to that chain and slot 3 of an EVM-source send
#: from it (``ccip_evm.SOURCE_LANE_ACCOUNT``). Read 2026-10-09 from successful
#: Solana-source sends to each chain (``scripts/xstocks_v2_solana_lane_read.py``,
#: ``data-v2/xstocks_v2_solana_lanes.json``).
LANE_CONFIG_ACCOUNT = {
    "x-layer": "5RxSBZJZ1Qai79MPrGTp6qiqp4Bw99F2nfB2ZPjsB5P5",
    "eth": "6VU9T42q3bGUTmoZ8LA5rxZ2Tdom86uLekj5c2N4w8mp",
    "bsc": "EAcd4PLvs42et73FSVoCCfbuXshZ9AE29yN5H6Cic8dz",
    "arbitrum": "5rjC32Zd4ugbU74ttkWubH836hSA7oBcCH9neeF2iY5p",
    "optimism": "9eXPiYQzGiCHE7K5CTfhf4TCny7TFdFeWTfgiyyiKWai",
    "hyperevm": "76xfTatJgf2GEFHmkPkkWZXmwbSyXp6yLYsR6XwSJdRz",
    "ink": "B5pRXw8nqyFc9EjkfdGrL2KdA4r1oQuY458RMkf9EAp1",
}
#: Slots that change with the destination chain. Everything else in
#: ``LANE_ACCOUNTS`` was identical across the six measured destinations.
DEST_INDEXES = frozenset({9, 10, 13, 25})


def dest_slots(dest_chain: str) -> dict[int, str]:
    """Slots 9, 10, 13 and 25 for one destination.

    9, 10 and 13 are CCIP PDAs keyed by the destination selector:
    ``["dest_chain_state", sel]`` and ``["nonce", sel, fee_authority]`` under
    the router, ``["dest_chain", sel]`` under the fee quoter. The derivation
    reproduces the measured X Layer table and six real sends from six
    different signers, so the nonce does not depend on the user. 25 is the
    lane config. A chain with no selector or no lane account raises.
    """
    key = str(dest_chain or "").strip().lower().replace("xlayer", "x-layer")
    selector = SOLANA_DEST_SELECTORS.get(key)
    lane = LANE_CONFIG_ACCOUNT.get(key)
    if selector is None or lane is None:
        raise ValueError(f"no measured solana lane for {dest_chain}")
    little = struct.pack("<Q", selector)
    authority = next(row[2] for row in LANE_ACCOUNTS if row[1] == ROLE_FEE_AUTHORITY)
    return {
        9: to_b58(find_program_address([b"dest_chain_state", little], CCIP_ROUTER)[0]),
        10: to_b58(find_program_address([b"nonce", little, pubkey(authority)], CCIP_ROUTER)[0]),
        13: to_b58(find_program_address([b"dest_chain", little], FEE_QUOTER)[0]),
        25: lane,
    }


# --------------------------------------------------------------------------
# instruction args
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class CcipSendArgs:
    dest_chain_selector: int
    receiver: bytes          # 32 bytes, the destination bridge contract
    recipient: bytes         # 20 bytes, the EVM address that gets the shares
    product_id: int
    amount: int              # raw SVM amount, 8 decimals

    def encode(self) -> bytes:
        if not 0 <= self.dest_chain_selector < 2**64:
            raise ValueError("dest chain selector out of range")
        if len(self.receiver) != 32:
            raise ValueError("receiver must be 32 bytes")
        if len(self.recipient) != 20:
            raise ValueError("recipient must be 20 bytes")
        if not 0 < self.product_id < 2**64:
            raise ValueError("product id must be a positive u64")
        if not 0 < self.amount < 2**64:
            raise ValueError("amount must be a positive u64")
        return b"".join((
            CCIP_SEND_DISCRIMINATOR,
            struct.pack("<Q", self.dest_chain_selector),
            struct.pack("<I", 32), self.receiver,
            struct.pack("<I", 20), self.recipient,
            struct.pack("<Q", self.product_id),
            struct.pack("<Q", self.amount),
        ))


def decode_args(data: bytes) -> CcipSendArgs:
    """Strict decode. Anything that is not exactly this shape raises."""
    raw = bytes(data)
    if raw[:8] != CCIP_SEND_DISCRIMINATOR:
        raise ValueError(f"not a ccip_send instruction: {raw[:8].hex()}")
    offset = 8

    def need(count: int) -> None:
        if offset + count > len(raw):
            raise ValueError("truncated instruction data")

    def u32() -> int:
        nonlocal offset
        need(4)
        value = struct.unpack_from("<I", raw, offset)[0]
        offset += 4
        return value

    def u64() -> int:
        nonlocal offset
        need(8)
        value = struct.unpack_from("<Q", raw, offset)[0]
        offset += 8
        return value

    def blob(length: int) -> bytes:
        nonlocal offset
        value = raw[offset:offset + length]
        if len(value) != length:
            raise ValueError("truncated instruction data")
        offset += length
        return value

    selector = u64()
    receiver = blob(u32())
    recipient = blob(u32())
    product_id = u64()
    amount = u64()
    if offset != len(raw):
        raise ValueError(f"{len(raw) - offset} trailing bytes after ccip_send args")
    if len(receiver) != 32:
        raise ValueError(f"receiver is {len(receiver)} bytes, expected 32")
    if len(recipient) != 20:
        raise ValueError(f"recipient is {len(recipient)} bytes, expected 20")
    return CcipSendArgs(selector, receiver, recipient, product_id, amount)


def message_payload(recipient: bytes, product_id: int, amount: int) -> bytes:
    """``tokenReceiver(32) || tokenId(8, BE) || shares(32, BE)``.

    ``amount`` is the raw SVM amount; the shares value is the same number scaled
    from 8 to 18 decimals, matching ``evmDecimals - svmDecimals`` for xStocks.
    """
    if len(recipient) != 20:
        raise ValueError("recipient must be 20 bytes")
    shares = amount * 10 ** 10
    if not 0 <= shares < 2**256:
        raise ValueError("shares out of range")
    return (b"\x00" * 12 + recipient
            + product_id.to_bytes(8, "big")
            + shares.to_bytes(32, "big"))


def parse_message_payload(data: bytes) -> tuple[bytes, int, int]:
    raw = bytes(data)
    if len(raw) != 72:
        raise ValueError(f"payload is {len(raw)} bytes, expected 72")
    return raw[:32], int.from_bytes(raw[32:40], "big"), int.from_bytes(raw[40:72], "big")


# --------------------------------------------------------------------------
# the account table
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class AccountMeta:
    pubkey: str
    role: str
    is_signer: bool
    is_writable: bool


def build_accounts(*, signer: object, mint: object, token_config: object,
                   token_config_data: bytes | None = None,
                   custody: object | None = None,
                   dest_chain: str = "x-layer") -> list[AccountMeta]:
    """The 27 accounts, in order, with the five live slots filled in.

    ``dest_chain`` fills slots 9, 10, 13 and 25 (``dest_slots``).

    ``custody`` defaults to the token config's own field. Reading it from the
    config beats deriving it: the custody account is **not** the canonical
    associated token account (checked 2026-10-07 -- ``ATA(POOL_AUTHORITY, mint)``
    gives a different address), so a derivation would have produced a plausible
    looking table that belongs to nobody.

    The source token account *is* derived. That one is the canonical ATA of the
    signer, and deriving it removes the class of bug where a stale table sends
    from somebody else's account.
    """
    mint_key = to_b58(mint)
    signer_key = to_b58(signer)
    if custody is None:
        if token_config_data is None:
            raise ValueError("custody account is required unless token_config_data is given")
        custody = token_config_custody(token_config_data)
    filled = {
        ROLE_SIGNER: signer_key,
        ROLE_SOURCE_ATA: to_b58(associated_token_address(signer, mint_key)),
        ROLE_CUSTODY_ATA: to_b58(custody),
        ROLE_MINT: mint_key,
        ROLE_TOKEN_CONFIG: to_b58(token_config),
    }
    per_dest = dest_slots(dest_chain)
    out: list[AccountMeta] = []
    for index, role, fixed, is_signer, is_writable in LANE_ACCOUNTS:
        key = per_dest.get(index) or filled.get(role, fixed)
        if key is None:
            raise ValueError(f"account {index} ({role}) has no value")
        out.append(AccountMeta(key, role, is_signer, is_writable))
    if out[5].pubkey == out[1].pubkey:
        raise ValueError("custody account equals the signer")
    if out[5].pubkey == out[4].pubkey:
        raise ValueError("custody account equals the source account")
    return out


def token_config_custody(raw: bytes, *, mint: object | None = None,
                         product_id: int | None = None) -> bytes:
    """Decode a ``token_config`` account and return its custody token account.

    Unknown is not zero: a short account, a wrong discriminator or a different
    mint raises rather than returning something that looks usable.
    """
    data = bytes(raw)
    if len(data) != TOKEN_CONFIG_LEN:
        raise ValueError(f"token config is {len(data)} bytes, expected {TOKEN_CONFIG_LEN}")
    if data[:8] != TOKEN_CONFIG_DISCRIMINATOR:
        raise ValueError(f"not a token config: {data[:8].hex()}")
    if mint is not None and to_b58(data[TOKEN_CONFIG_MINT_OFFSET:TOKEN_CONFIG_MINT_OFFSET + 32]) != to_b58(mint):
        raise ValueError("token config belongs to a different mint")
    if product_id is not None and struct.unpack_from(
            "<Q", data, TOKEN_CONFIG_PRODUCT_ID_OFFSET)[0] != int(product_id):
        raise ValueError("token config belongs to a different product id")
    return data[TOKEN_CONFIG_CUSTODY_OFFSET:TOKEN_CONFIG_CUSTODY_OFFSET + 32]


def token_config_product_id(raw: bytes) -> int:
    data = bytes(raw)
    if len(data) != TOKEN_CONFIG_LEN or data[:8] != TOKEN_CONFIG_DISCRIMINATOR:
        raise ValueError("not a token config account")
    return struct.unpack_from("<Q", data, TOKEN_CONFIG_PRODUCT_ID_OFFSET)[0]


def validate_accounts(accounts: Sequence[AccountMeta], *, signer: object,
                      mint: object, token_config: object,
                      custody: object, dest_chain: str = "x-layer") -> list[str]:
    """Every problem in the table, as stable strings. Empty list is good.

    ``mint_ownership`` is the important one: a table built for TSLAx must never
    be sent with QQQx's custody account, and vice versa.
    """
    problems: list[str] = []
    mint_key = to_b58(mint)
    if len(accounts) != ACCOUNT_COUNT:
        return [f"account_count:{len(accounts)}"]
    for index, (expected_index, role, _fixed, is_signer, is_writable) in enumerate(LANE_ACCOUNTS):
        row = accounts[index]
        if row.role != role:
            problems.append(f"role_order:{index}:{row.role}")
        if row.is_signer != is_signer:
            problems.append(f"signer_flag:{index}")
        if row.is_writable != is_writable:
            problems.append(f"writable_flag:{index}")
    if accounts[1].pubkey != to_b58(signer):
        problems.append("signer_slot")
    if accounts[6].pubkey != mint_key:
        problems.append("mint_slot")
    expected_source = to_b58(associated_token_address(signer, mint_key))
    if accounts[4].pubkey != expected_source:
        problems.append("source_ata_not_owner_mint")
    if accounts[5].pubkey != to_b58(custody):
        problems.append("custody_slot")
    if accounts[26].pubkey != to_b58(token_config):
        problems.append("token_config_slot")
    if accounts[4].pubkey == accounts[5].pubkey:
        problems.append("source_is_custody")
    try:
        for index, key in dest_slots(dest_chain).items():
            if accounts[index].pubkey != key:
                problems.append(f"dest_slot:{index}")
    except ValueError:
        problems.append(f"no_lane:{dest_chain}")
    for index, _role, fixed, _s, _w in LANE_ACCOUNTS:
        if fixed and index not in DEST_INDEXES and accounts[index].pubkey != fixed:
            problems.append(f"fixed_slot:{index}")
    # The system program legitimately appears twice (indexes 3 and 24). Every
    # other duplicate is a table that lost track of a role.
    keys = [row.pubkey for row in accounts if row.pubkey != SYSTEM_PROGRAM]
    if len(set(keys)) != len(keys):
        problems.append("duplicate_account")
    return problems


def validate_args(args: CcipSendArgs, *, product_id: int, amount: int,
                  dest_chain_selector: int = XLAYER_CHAIN_SELECTOR,
                  bridge_evm: str = XSTOCKS_BRIDGE_EVM) -> list[str]:
    problems: list[str] = []
    if args.dest_chain_selector != dest_chain_selector:
        problems.append(f"dest_selector:{args.dest_chain_selector}")
    if args.receiver != evm_to_bytes32(bridge_evm):
        problems.append("receiver_not_bridge")
    if args.receiver == b"\x00" * 32:
        problems.append("receiver_all_zero")
    if args.recipient == b"\x00" * 20:
        problems.append("recipient_all_zero")
    if args.product_id != product_id:
        problems.append(f"product_id:{args.product_id}")
    if args.amount != amount:
        problems.append(f"amount:{args.amount}")
    return problems


def canonical_xlayer_receiver() -> bytes:
    return evm_to_bytes32(XSTOCKS_BRIDGE_EVM)


def instruction_data(*, recipient: str, product_id: int, amount: int,
                     dest_chain_selector: int = XLAYER_CHAIN_SELECTOR,
                     bridge_evm: str = XSTOCKS_BRIDGE_EVM) -> bytes:
    """The 92 bytes an operator can decode before signing."""
    return CcipSendArgs(
        dest_chain_selector=dest_chain_selector,
        receiver=evm_to_bytes32(bridge_evm),
        recipient=bytes.fromhex(str(recipient).removeprefix("0x")),
        product_id=int(product_id),
        amount=int(amount),
    ).encode()


# --------------------------------------------------------------------------
# legacy transaction assembly (no address lookup tables, like the samples)
# --------------------------------------------------------------------------
def shortvec(value: int) -> bytes:
    out = bytearray()
    while True:
        chunk = value & 0x7F
        value >>= 7
        if value:
            out.append(chunk | 0x80)
        else:
            out.append(chunk)
            return bytes(out)


def _read_shortvec(raw: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        byte = raw[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7


def build_legacy_transaction(*, payer: object, accounts: Sequence[AccountMeta],
                             program_id: object, data: bytes,
                             recent_blockhash: bytes | object,
                             compute_unit_limit: int | None = DEFAULT_COMPUTE_UNIT_LIMIT) -> bytes:
    """Serialize a legacy transaction for ``simulateTransaction``.

    Signature slots are zero filled: the probe simulates with
    ``sigVerify: false`` before anything is signed, and the real signature is
    produced by the wallet from these same bytes.

    ``compute_unit_limit`` prepends ``ComputeBudget`` ``setComputeUnitLimit``.
    The measured transactions carry it; the default is 400 000 because 200 000
    is not enough for ``ccip_send``.
    """
    blockhash = (recent_blockhash if isinstance(recent_blockhash, (bytes, bytearray))
                 else b58decode(str(recent_blockhash)))
    if len(blockhash) != 32:
        raise ValueError("recent blockhash must be 32 bytes")
    payer_key = to_b58(payer)
    if not any(row.pubkey == payer_key and row.is_signer for row in accounts):
        raise ValueError("payer must appear as a writable signer in the account list")

    signers = [row for row in accounts if row.is_signer]
    writable = [row for row in accounts if not row.is_signer and row.is_writable]
    readonly = [row for row in accounts if not row.is_signer and not row.is_writable]
    unit_ix = None
    if compute_unit_limit is not None:
        if not 0 < compute_unit_limit < 1_400_001:
            raise ValueError("compute unit limit out of range")
        unit_ix = AccountMeta(COMPUTE_BUDGET_PROGRAM, "compute_budget", False, False)
        readonly = readonly + [unit_ix]
    # Writable signers, readonly signers, writable non-signers, readonly
    # non-signers. The fee payer has to be the first signer.
    ordered = list(signers) + writable + readonly
    if ordered[0].pubkey != payer_key:
        raise ValueError("fee payer must be the first signer")

    # The system program appears twice in the measured table (indexes 3 and
    # 24). The transaction header carries it once; the instruction keeps both
    # positions.
    keys: list[bytes] = []
    index_of: dict[bytes, int] = {}
    for row in ordered:
        key = pubkey(row.pubkey)
        if key not in index_of:
            index_of[key] = len(keys)
            keys.append(key)
    program = pubkey(program_id)
    if program not in index_of:
        index_of[program] = len(keys)
        keys.append(program)

    signer_count = len(signers)
    readonly_signed = sum(1 for row in signers if not row.is_writable)
    readonly_unsigned = len(keys) - signer_count - len(writable)

    # Instruction account order is the CALLER's order, not the key order. The
    # program id is not part of that vector: it gets its own index field.
    instruction_indexes = [index_of[pubkey(row.pubkey)] for row in accounts]

    instructions = []
    if unit_ix is not None:
        instructions.append((index_of[pubkey(COMPUTE_BUDGET_PROGRAM)],
                             [], struct.pack("<BI", 2, compute_unit_limit)))
    instructions.append((index_of[program], instruction_indexes, bytes(data)))

    message = bytearray()
    message += bytes([signer_count, readonly_signed, readonly_unsigned])
    message += shortvec(len(keys))
    for key in keys:
        message += key
    message += blockhash
    message += shortvec(len(instructions))
    for program_index, account_indexes, body in instructions:
        message.append(program_index)
        message += shortvec(len(account_indexes))
        message += bytes(account_indexes)
        message += shortvec(len(body))
        message += body

    raw = bytearray()
    raw += shortvec(signer_count)
    raw += b"\x00" * (64 * signer_count)
    raw += message
    return bytes(raw)


def parse_legacy_transaction(raw: bytes) -> dict:
    """Read back what is about to be signed. Unknown shapes raise."""
    data = bytes(raw)
    signer_count, offset = _read_shortvec(data, 0)
    offset += 64 * signer_count
    header = data[offset:offset + 3]
    offset += 3
    key_count, offset = _read_shortvec(data, offset)
    keys = [to_b58(data[offset + 32 * i:offset + 32 * (i + 1)]) for i in range(key_count)]
    offset += 32 * key_count
    blockhash = data[offset:offset + 32]
    offset += 32
    instruction_count, offset = _read_shortvec(data, offset)
    instructions = []
    for _ in range(instruction_count):
        program = keys[data[offset]]
        offset += 1
        account_count, offset = _read_shortvec(data, offset)
        accounts = [keys[data[offset + i]] for i in range(account_count)]
        offset += account_count
        length, offset = _read_shortvec(data, offset)
        body = data[offset:offset + length]
        offset += length
        instructions.append({"program_id": program, "accounts": accounts, "data": body})
    if offset != len(data):
        raise ValueError(f"{len(data) - offset} trailing bytes in the transaction")
    return {
        "signers": signer_count,
        "header": list(header),
        "keys": keys,
        "blockhash": blockhash,
        "instructions": instructions,
        "signer_slots": signer_count,
    }
