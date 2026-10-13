"""Directed route filter. The graph does not execute."""
from __future__ import annotations

# Canonical spelling: the census one ("eth", "x-layer"). The bridge table writes
# "XLayer", this module was originally written with "xlayer", and the census
# writes "x-layer". Comparing the raw strings meant an edge that does not exist
# looked present: ``has_bridge("x-layer", "hyperevm")`` returned True because
# only the "xlayer" spelling was listed as missing (found 2026-10-07).
_ALIASES = {
    "xlayer": "x-layer",
    "x layer": "x-layer",
    "x_layer": "x-layer",
    "ethereum": "eth",
}


def norm(chain: object) -> str:
    """One spelling per chain for every comparison in this module."""
    key = str(chain or "").strip().lower()
    return _ALIASES.get(key, key)


EVM = frozenset({
    "eth", "x-layer", "bsc", "arbitrum", "optimism",
    "monad", "hyperevm", "ink", "mantle",
})

# Edges absent from data/xstocks_book.sqlite bridges on 2026-10-07.
MISSING_EDGES = frozenset({
    (norm(source), norm(dest)) for source, dest in (
        ("solana", "mantle"),
        ("monad", "hyperevm"),
        ("xlayer", "hyperevm"),
        ("xlayer", "optimism"),
        ("ink", "hyperevm"),
        ("ink", "optimism"),
        ("mantle", "solana"),
        ("mantle", "hyperevm"),
        ("mantle", "optimism"),
        ("optimism", "mantle"),
        ("optimism", "hyperevm"),
        ("optimism", "ink"),
        ("optimism", "xlayer"),
        ("hyperevm", "mantle"),
        ("hyperevm", "ink"),
        ("hyperevm", "xlayer"),
        ("hyperevm", "optimism"),
        ("hyperevm", "monad"),
    )
})

BITMAP_SOLANA = 772


def has_bridge(source: str, dest: str) -> bool:
    return (norm(source), norm(dest)) not in MISSING_EDGES


def dialect(source: str, dest: str) -> str:
    src, dst = norm(source), norm(dest)
    if not has_bridge(src, dst):
        return "no_edge"
    if src == "solana":
        return "solana_source"
    if dst == "solana" and src in EVM:
        return "evm_to_solana"
    if src in EVM and dst in EVM:
        return "evm_to_evm"
    return "unknown"


def extra_args_ok(kind: str, *, bitmap: int | None = None, n_accounts: int | None = None,
                  source_selector: int | None = None,
                  expected_selector: int | None = None) -> tuple[bool, str]:
    """Three CCIP dialects. Bitmap 772 belongs only to EVM to Solana."""
    if kind == "evm_to_solana":
        if bitmap != BITMAP_SOLANA or n_accounts != 11:
            return False, "bad_svm_args"
        if source_selector != expected_selector:
            return False, "source_selector"
        return True, "ok"
    if kind == "evm_to_evm":
        if bitmap == BITMAP_SOLANA:
            return False, "bitmap_772_on_evm"
        return True, "ok"
    if kind == "solana_source":
        return False, "solana_source_rejects_evm_send"
    return False, "no_edge"


def same_pool(buy_pool: str, sell_pool: str) -> bool:
    return buy_pool.lower() == sell_pool.lower()
