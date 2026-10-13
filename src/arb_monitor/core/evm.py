"""Minimal EVM helpers: keccak-256 and 4-byte function selectors.

The project already hard-codes selectors it read from live pool contracts. That
works for a fixed set of calls, but not for asking a token contract a question
nobody has hard-coded yet — for example the corporate-action multiplier that
decides how many underlying shares one tokenised share represents.

Keccak-256 (the pre-standard padding Ethereum uses) is implemented here rather
than pulled in as a dependency, and it is checked against selectors already
verified in this repository.
"""
from __future__ import annotations

_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_ROTATION = [
    [0, 36, 3, 41, 18],
    [1, 44, 10, 45, 2],
    [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56],
    [27, 20, 39, 8, 14],
]
_MASK = (1 << 64) - 1


def _keccak_f(state: list[list[int]]) -> None:
    for round_index in range(24):
        # theta
        c = [state[x][0] ^ state[x][1] ^ state[x][2] ^ state[x][3] ^ state[x][4]
             for x in range(5)]
        d = [c[(x - 1) % 5] ^ (((c[(x + 1) % 5] << 1) | (c[(x + 1) % 5] >> 63)) & _MASK)
             for x in range(5)]
        for x in range(5):
            for y in range(5):
                state[x][y] ^= d[x]
        # rho + pi
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                r = _ROTATION[x][y]
                value = state[x][y]
                rotated = ((value << r) | (value >> (64 - r))) & _MASK if r else value
                b[y][(2 * x + 3 * y) % 5] = rotated
        # chi
        for x in range(5):
            for y in range(5):
                state[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y])
        # iota
        state[0][0] ^= _RC[round_index]


def keccak256(data: bytes) -> bytes:
    """Keccak-256 as used by Ethereum (0x01 padding, not SHA3-256)."""
    rate = 136
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate != 0:
        padded.append(0x00)
    padded[-1] ^= 0x80
    state = [[0] * 5 for _ in range(5)]
    for offset in range(0, len(padded), rate):
        block = padded[offset:offset + rate]
        for i in range(rate // 8):
            lane = int.from_bytes(block[i * 8:(i + 1) * 8], "little")
            state[i % 5][i // 5] ^= lane
        _keccak_f(state)
    out = bytearray()
    for i in range(4):
        out += state[i % 5][i // 5].to_bytes(8, "little")
    return bytes(out)


def selector(signature: str) -> str:
    """First four bytes of keccak-256 of a canonical signature, as 0x-hex."""
    return "0x" + keccak256(signature.encode()).hex()[:8]


def encode_call(signature: str, *args: str) -> str:
    """Calldata for a function taking 32-byte words (addresses and uints)."""
    words = []
    for arg in args:
        if arg.startswith("0x"):
            words.append(arg[2:].lower().rjust(64, "0"))
        else:
            words.append(f"{int(arg):064x}")
    return selector(signature) + "".join(words)


def decode_uint(word: str) -> int:
    return int(word, 16)


def decode_uint_fixed(word: str, decimals: int = 18):
    """Decode a fixed-point uint (Backed's multiplier uses 18 decimals)."""
    from decimal import Decimal
    return Decimal(decode_uint(word)) / (Decimal(10) ** decimals)
