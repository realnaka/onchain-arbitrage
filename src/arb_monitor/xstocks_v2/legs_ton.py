"""TON leg of chain -> Kraken. Builds cells. Does not broadcast.

The swap body is the ston.fi router v2 layout (opcode ``0x6664de2a``), the same
one ``@ston-fi/sdk`` ``BaseRouterV2_1.createSwapBody`` writes. A v1 body sent to
a v2 router is not a failed swap, it is a lost jetton -- so a router that is
not v2 is a refusal, not a guess. The jetton transfer is TEP-74.
"""
from __future__ import annotations

from decimal import Decimal

from pytoniq_core import Address, Cell, begin_cell
from pytoniq_core.crypto.keys import (
    mnemonic_is_valid, mnemonic_new, mnemonic_to_private_key,
    private_key_to_public_key)
from pytoniq_core.tlb.account import StateInit

from pytoniq.contract.contract import Contract
from pytoniq.contract.wallets.wallet import WALLET_V4_R2_CODE, WalletV4R2

#: ston.fi DEX v2 ``SWAP``. v1 is a different opcode and must not be substituted.
SWAP_OP = 0x6664DE2A
JETTON_TRANSFER_OP = 0x0F8A7EA5
#: What the router message attaches. The excess comes back to our wallet; the
#: wallet still has to be holding it or the message never leaves.
SWAP_ATTACH_NANOTON = 300_000_000
#: Forwarded *inside* the jetton transfer so the router still has gas after the
#: wallet keeps its own fee. The SDK's number; less than this and the router
#: refunds instead of swapping (ston-fi/sdk#52).
SWAP_FORWARD_NANOTON = 240_000_000
#: Deposit message: enough to deliver the Kraken memo inside the jetton notification.
DEPOSIT_ATTACH_NANOTON = 50_000_000
FORWARD_NOTIFY_NANOTON = 10_000_000
#: The part of a swap plus a deposit that does not come back, until a receipt
#: replaces it. Charged in the cost model. The attached 0.30 is not the cost.
BURN_TON = Decimal("0.08")
WALLET_ID = 698983191


def ton_method(methods) -> dict | None:
    """Kraken's ``<symbol> - TON`` deposit method, if it offers one.

    The network word lives in the method name (``SPYx - TON``) or, for a rail
    whose name has no network, in ``WithdrawMethods.network``. No match is a
    refusal, not a zero.
    """
    for row in methods or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("method") or "").lower()
        network = str(row.get("network") or "").lower()
        if ("ton" in name or "open network" in name
                or network in {"ton", "the open network"}):
            return row
    return None


def deposit_memo(address_row: dict | None) -> str:
    """The comment Kraken wants on this deposit, or ``""`` when the address is unique.

    A present-but-empty memo is "no comment". A missing row is not a memo of
    empty: the caller has to have read ``DepositAddresses`` first.
    """
    if not isinstance(address_row, dict):
        return ""
    for key in ("memo", "tag", "comment"):
        value = address_row.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _addr(value: str | None) -> Address | None:
    if not value:
        return None
    return Address(value)


def swap_body(*, ask_jetton_wallet: str, refund: str, receiver: str,
              min_ask_units: int, deadline: int, excess: str | None = None) -> Cell:
    """ston.fi v2 jetton-to-jetton forward payload.

    ``ask_jetton_wallet`` is the **router's** jetton wallet for the token we
    buy (the simulate response names it). ``receiver`` is where the bought
    jettons land -- our wallet, not Kraken. Kraken is a second message.
    """
    if int(min_ask_units) <= 0:
        raise ValueError("min_ask_units 必须大于 0")
    inner = (begin_cell()
             .store_coins(int(min_ask_units))
             .store_address(_addr(receiver))
             .store_coins(0)
             .store_maybe_ref(None)
             .store_coins(0)
             .store_maybe_ref(None)
             .store_uint(10, 16)          # referral bps; no referral address
             .store_address(None)
             .end_cell())
    return (begin_cell()
            .store_uint(SWAP_OP, 32)
            .store_address(_addr(ask_jetton_wallet))
            .store_address(_addr(refund))
            .store_address(_addr(excess or refund))
            .store_uint(int(deadline), 64)
            .store_ref(inner)
            .end_cell())


def comment_cell(text: str) -> Cell:
    """A TON text comment (opcode 0). Empty text is not a comment."""
    if not str(text):
        raise ValueError("comment 不能为空")
    return begin_cell().store_uint(0, 32).store_snake_string(str(text)).end_cell()


def jetton_transfer_body(*, amount: int, destination: str, response: str,
                         forward_nanoton: int, query_id: int = 0,
                         comment: str = "") -> Cell:
    """TEP-74 ``transfer``. ``comment`` rides in the forward payload.

    Kraken credits a TON deposit by the comment when the address row carries
    one. Omitting it is how a unique-looking transfer becomes an uncredited one.
    """
    if int(amount) <= 0:
        raise ValueError("transfer 数量必须大于 0")
    builder = (begin_cell()
               .store_uint(JETTON_TRANSFER_OP, 32)
               .store_uint(int(query_id), 64)
               .store_coins(int(amount))
               .store_address(_addr(destination))
               .store_address(_addr(response))
               .store_bit(False)
               .store_coins(int(forward_nanoton)))
    if comment:
        builder.store_bit(True).store_ref(comment_cell(comment))
    else:
        builder.store_bit(False)
    return builder.end_cell()


def wallet_from_mnemonic(words: list[str] | str) -> dict:
    """v4r2 address and keys. No network.

    The address is non-bounceable: the wallet can receive its first TON before
    it is deployed. A bounceable address would send that TON back.
    """
    if isinstance(words, str):
        words = words.split()
    if not mnemonic_is_valid(words):
        raise ValueError("mnemonic 无效")
    _public, private_key = mnemonic_to_private_key(words)
    public_key = private_key_to_public_key(private_key)
    data = WalletV4R2.create_data_cell(public_key, WALLET_ID, 0)
    state = StateInit(code=WALLET_V4_R2_CODE, data=data)
    address = Address((0, state.serialize().hash))
    return {
        "words": list(words),
        "private_key": private_key,
        "public_key": public_key,
        "wallet_id": WALLET_ID,
        "state_init": state,
        "address": address.to_str(is_bounceable=False, is_url_safe=True),
        "address_bounceable": address.to_str(is_bounceable=True, is_url_safe=True),
    }


def new_wallet() -> dict:
    return wallet_from_mnemonic(mnemonic_new())


def external_boc(*, private_key: bytes, seqno: int, wallet_id: int,
                 wallet_address: str, destination: str, value: int,
                 body: Cell, state_init: StateInit | None) -> bytes:
    """A signed v4 external message, ready for ``sendBoc``.

    ``state_init`` is attached only on the deploying message (seqno 0). A later
    message that carries it again is a different transaction.
    """
    from pytoniq_core.tlb.custom.wallet import WalletMessage

    message = WalletMessage(
        send_mode=3,
        message=Contract.create_internal_msg(
            dest=Address(destination), value=int(value), body=body))
    signed = WalletV4R2.raw_create_transfer_msg(
        private_key, int(seqno), int(wallet_id), [message])
    init = state_init if int(seqno) == 0 else None
    external = Contract.create_external_msg(
        dest=Address(wallet_address), state_init=init, body=signed)
    return external.serialize().to_boc()
