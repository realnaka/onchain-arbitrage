"""Re-check a token↔underlying↔perpetual mapping against live sources.

``config/hk_monitor.toml`` may claim "1 token = 1 share, 1 contract = 1 share".
That claim is worth exactly as much as the evidence behind it and how recently
it was checked, so this module rebuilds it from four independent reads:

1. **issuer registry** — the token's ISIN and the underlying ISIN it names;
2. **on-chain reserve** — the vault's ``totalAssets()`` versus the native tokens
   it actually holds (``wrapper.asset()`` plus ``balanceOf``), which is what
   makes the wrapper backed rather than merely branded as such;
3. **on-chain conversion rate** — ``wrapper.convertToAssets(1e18)``, the number
   of native tokens one wrapper token redeems for. It equals the issuer's
   corporate-action ``multiplier()`` on every xStock measured (7/7 US, 6/6 HK,
   2026-09-22), and it *is* the shares-per-token of the wrapper we trade: the
   US wrappers are worth 1.0000–1.0059 shares, not 1. Assuming "1 token = 1
   share" is the mistake this check exists to catch;
4. **exchange contract spec** — contract value, its currency and the contract
   multiplier, which is what makes one contract one share rather than a guess.

Nothing here trades. It exists so ``verified = true`` keeps paying rent: a
drifted ratio is caught by re-running the check, and every check names the
source it read.

Layer 4 has two shapes, because the venues do not describe a contract the same
way. OKX publishes ``ctVal``/``ctValCcy``/``ctMult``. Gate publishes
``quanto_multiplier`` (shares per contract — and it is **not** the same for every
asset: measured 2026-09-17 it is 1 for XIAOMI/SHEIN/KUAISHOU and 0.1 for
TENCENT/POPMART/MEITUAN). Bybit publishes no size field at all for linear perps
(one contract is one coin) and says which stock it is instead. Bitget publishes
``sizeMultiplier`` (0.01 for every HK stock contract here).

So a non-OKX mapping is verified by (a) the venue's own size field matching what
the config claims, and (b) the venue's own *identity* fields naming the same
stock — because "the multiplier is 0.1" is worth nothing if the contract is a
different company's.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .evm import encode_call, decode_uint

ONE_SHARE_18 = 10 ** 18

# One native token is 1e18 wei. ``totalAssets()`` is computed through the
# vault's own share math while ``balanceOf()`` is a plain ERC-20 balance, so the
# two agree only to the vault's internal rounding, not to the last wei. Measured
# 2026-09-22: a healthy AAPLx vault differed by 54,959 wei (5.5e-14 token,
# 6.4e-17 relative) on an 855.8-token reserve, which an exact = comparison
# rejected. A genuinely short vault is off by orders of magnitude more than one
# part in a billion of a token, so that is the bound.
_BACKING_WEI_TOL = ONE_SHARE_18 // 10 ** 9


def _same_reserve(declared_assets: int, observed_reserve: int) -> bool:
    """Is the vault's own ``totalAssets()`` the reserve it actually holds?

    Equal within integer-rounding (see ``_BACKING_WEI_TOL``), and the reserve
    must be non-empty — an empty vault "matches" nothing.
    """
    return (observed_reserve > 0
            and abs(declared_assets - observed_reserve) <= _BACKING_WEI_TOL)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    source: str

    def as_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail,
                "source": self.source}


def _call(rpc, address: str, signature: str, *args: str) -> str | None:
    try:
        out = rpc.call(address, encode_call(signature, *args))
    except Exception:  # noqa: BLE001 - a revert is a result, not a crash
        return None
    return out if out and out != "0x" else None


def decimal_from_word(word: str, decimals: int = 18) -> Decimal:
    """A 32-byte word as a fixed-point Decimal (xStocks use 18 decimals)."""
    return Decimal(decode_uint(word)) / (Decimal(10) ** decimals)


def _hk_code(value) -> str | None:
    """Normalise an HKEX code for comparison: 700 == 00700 == "700".

    The issuer writes ``700``, Bybit writes ``00700`` and the exchange naming
    uses ``0700`` — the same stock in three spellings, so a string compare would
    "catch" a mismatch that does not exist.
    """
    text = str(value or "").strip()
    if not text.isdigit():
        return None
    return str(int(text))


def _squash(text) -> str:
    return "".join(ch for ch in str(text or "").lower() if ch.isalnum())


def verify_venue_contract(*, spec: dict, venue_contract: dict) -> list[Check]:
    """Layer 4 for gate / bybit / bitget, from the venue's own instrument row.

    Two questions, kept separate: *how big* is one contract (the multiplier the
    hedge arithmetic depends on), and *which stock* is it. The multiplier comes
    from ``perp_contract_multiplier`` in the config, so a venue silently changing
    a contract's size shows up as a failed check instead of a mis-sized hedge.
    """
    venue = str(venue_contract.get("venue") or "?")
    instrument_id = str(venue_contract.get("instrument_id") or "")
    raw = venue_contract.get("raw") or {}
    checks: list[Check] = []

    observed = venue_contract.get("multiplier")
    expected = spec.get("perp_contract_multiplier")
    field_name = {"gate": "quanto_multiplier", "bitget": "sizeMultiplier",
                  "bybit": "(linear perp: one contract is one coin)"}.get(venue, "multiplier")
    same = (expected is None
            or (observed is not None
                and Decimal(str(observed)) == Decimal(str(expected))))
    checks.append(Check(
        "perp_contract_size", bool(same),
        f"{venue}:{instrument_id} {field_name} = {observed} "
        f"(1 contract = {observed} coin/share); configured "
        f"perp_contract_multiplier = {expected}"
        + ("" if same else " — venue and config disagree"),
        f"{venue} instrument catalogue"))

    # Identity: the venue saying "this is that stock" in its own words.
    # The perp's own value currency is the asset ticker ("SHEIN"), which is what
    # the venues put in baseCoin / the instrument name — not the issuer's token
    # symbol ("SHEINx"), and not the wrapper symbol ("wSHEINx").
    asset = str(spec.get("perp_value_currency") or spec.get("asset") or "").upper()
    issuer_code = _hk_code(spec.get("issuer_underlying_symbol")
                           or spec.get("underlying_symbol") or spec.get("hk_code"))
    if venue == "bybit":
        ticker_raw = str(raw.get("underlyingTicker") or "")
        ticker = _hk_code(ticker_raw)
        # Bybit is not uniform: most HK names carry a zero-padded HKEX code
        # ("01024"), but SHEIN carries the ticker string "SHEIN". Accept either
        # match, and say which one was used — a silent "close enough" here is
        # how a mapping ends up pointing at the wrong company.
        matches_code = ticker is not None and issuer_code is not None and ticker == issuer_code
        matches_name = bool(ticker_raw) and _squash(ticker_raw) == _squash(asset)
        identity_ok = (str(raw.get("symbolType")) == "stock"
                       and str(raw.get("marketRegion")) == "HK"
                       and (matches_code or matches_name))
        detail = (f'symbolType={raw.get("symbolType")} marketRegion={raw.get("marketRegion")} '
                  f'underlyingTicker={ticker_raw} fullName={raw.get("fullName")} '
                  f'→ 与发行方的 {issuer_code} 匹配={matches_code}，'
                  f'与资产代码 {asset} 匹配={matches_name}')
    elif venue == "bitget":
        identity_ok = (str(raw.get("baseCoin") or "").upper() == asset
                       and str(raw.get("isRwa") or "").upper() == "YES")
        detail = (f'baseCoin={raw.get("baseCoin")} isRwa={raw.get("isRwa")} '
                  f'quoteCoin={raw.get("quoteCoin")} → expected {asset}')
    elif venue == "gate":
        identity_ok = (str(raw.get("type") or "") == "direct"
                       and instrument_id.upper().startswith(asset))
        detail = (f'name={raw.get("name")} type={raw.get("type")} '
                  f'→ direct (USD-linear) contract named after {asset}')
    elif venue in {"binance", "aster"}:
        # Binance lists the same stock twice: a USD-priced name ticker
        # (TENCENTUSDT, 54.61) and an HKD-priced HK-code ticker (HK0700USDT,
        # 428.09 — ratio 7.8403 = USD/HKD, measured 2026-09-17). Only the name
        # ticker may be mapped to a USD-priced token, so an HK-code base asset is
        # refused here even though the venue calls both HK_EQUITY.
        # Aster's catalogue is Binance-shaped and is held to the same rule.
        base = str(raw.get("baseAsset") or "").upper()
        is_hk_equity = (str(raw.get("underlyingType")) == "HK_EQUITY"
                        or venue == "aster")
        identity_ok = (is_hk_equity and base == asset and not base.startswith("HK"))
        detail = (f'underlyingType={raw.get("underlyingType")} baseAsset={base} '
                  f'quoteAsset={raw.get("quoteAsset")} → expected {asset} '
                  f'(USD-priced name ticker, not an HK-code/HKD one)')
    elif venue == "hyperliquid":
        # Stock perps live in the builder-deployed "xyz" dex and are named
        # "xyz:<TICKER>"; the suffix has to be this asset, not merely some asset.
        name = str(raw.get("name") or "")
        suffix = name.split(":")[-1].upper()
        identity_ok = name.startswith("xyz:") and suffix == asset
        detail = (f'dex name={name} szDecimals={raw.get("szDecimals")} '
                  f'→ expected xyz:{asset}')
    elif venue == "lighter":
        # Lighter publishes the multiplier itself, so both the name and the unit
        # are readable from the venue's own row.
        symbol = str(raw.get("symbol") or "").upper()
        identity_ok = (symbol == asset
                       and str(raw.get("market_type") or "") == "perp"
                       and str(raw.get("status") or "") == "active")
        detail = (f'symbol={symbol} market_type={raw.get("market_type")} '
                  f'status={raw.get("status")} multiplier={raw.get("multiplier")} '
                  f'→ expected {asset} perp')
    else:
        identity_ok, detail = False, f"no identity check implemented for {venue}"
    checks.append(Check("perp_venue_identity", identity_ok, detail,
                        f"{venue} instrument catalogue"))
    checks.append(Check(
        "perp_contract_active", bool(venue_contract.get("active")),
        f"{venue}:{instrument_id} active={venue_contract.get('active')}",
        f"{venue} instrument catalogue"))
    return checks


def verify_token_mapping(*, spec: dict, issuer_asset: dict | None, instrument: dict,
                         rpc, venue_contract: dict | None = None) -> list[Check]:
    """Every check that has to pass before the ratio may be called verified."""
    checks: list[Check] = []
    if venue_contract:
        # The venue's own row replaces the OKX-shaped spec for venues that do
        # not publish ctVal; both cannot apply to the same mapping.
        checks.extend(verify_venue_contract(spec=spec, venue_contract=venue_contract))
    return checks + _verify_token_layers(spec=spec, issuer_asset=issuer_asset,
                                         instrument=instrument, rpc=rpc,
                                         with_okx_spec=venue_contract is None)


def _verify_token_layers(*, spec: dict, issuer_asset: dict | None, instrument: dict,
                         rpc, with_okx_spec: bool = True) -> list[Check]:
    checks: list[Check] = []
    token_symbol = str(spec.get("token_symbol") or "")
    native = str(spec.get("native_address") or "").lower()
    wrapper = str(spec.get("wrapper_address") or "").lower()

    # 1. The issuer's own registry: which security is this token?
    if issuer_asset is None:
        checks.append(Check("issuer_registry", False,
                            f"issuer has no record for {token_symbol or 'token'}",
                            "api.backed.fi/api/v2/public/assets"))
    else:
        underlying = issuer_asset.get("underlying") or {}
        token_isin = str(issuer_asset.get("isin") or "")
        underlying_isin = str(underlying.get("isin") or "")
        ok = (issuer_asset.get("symbol") == token_symbol
              and token_isin == str(spec.get("token_isin") or token_isin)
              and underlying_isin == str(spec.get("underlying_isin") or underlying_isin))
        checks.append(Check(
            "issuer_registry", bool(ok and token_isin and underlying_isin),
            f'{token_symbol} ISIN {token_isin} → underlying '
            f'{underlying.get("symbol")} ISIN {underlying_isin}'
            + ("" if ok else " (does not match the configured expectation)"),
            "api.backed.fi/api/v2/public/assets"))

    # 2. The wrapper is fully backed: it holds exactly the native tokens it says
    #    it holds.
    #
    #    A wrapped xStock is an ERC-4626 vault whose *rate* is the issuer's
    #    corporate-action multiplier, so "one wrapper token equals one native
    #    token" is NOT the invariant. Until 2026-09-22 this check asserted
    #    ``totalSupply == balanceOf`` and failed 5 of the 7 US tokens for no
    #    reason (AAPLx 1.0033, NVDAx 1.0017, MSFTx 1.0059, METAx 1.0029,
    #    GOOGLx 1.0024 — all healthy vaults; TSLAx and AMZNx passed only
    #    because their rate happened to be 1). What has to hold is the reserve
    #    identity, measured 7/7 US and 6/6 HK on 2026-09-22:
    #    ``totalAssets() == native.balanceOf(wrapper)``.
    declared = _call(rpc, wrapper, "asset()")
    declared_address = ("0x" + declared[-40:]).lower() if declared else None
    supply = _call(rpc, wrapper, "totalSupply()")
    reserve = _call(rpc, native, "balanceOf(address)", wrapper) if native else None
    total_assets = _call(rpc, wrapper, "totalAssets()")
    multiplier = _call(rpc, native, "multiplier()") if native else None
    if reserve is None or (total_assets is None and (supply is None or multiplier is None)):
        checks.append(Check("wrapper_fully_backed", False,
                            "could not read the vault's assets, its supply or the "
                            "native reserve it claims",
                            f"eth_call {wrapper}.totalAssets / {native}.balanceOf"))
    elif total_assets is not None:
        held, claimed = decode_uint(total_assets), decode_uint(reserve)
        backed = _same_reserve(held, claimed)
        checks.append(Check(
            "wrapper_fully_backed", backed,
            f"wrapper totalAssets() {held} vs native "
            f"balanceOf(wrapper) {claimed}"
            + (f" (off by {abs(held - claimed)} wei, within rounding)"
               if backed and held != claimed else "")
            + ("" if backed else " (the vault does not hold the assets it minted "
                                 "shares against)"),
            f"eth_call {wrapper}.totalAssets / {native}.balanceOf"))
    else:
        # Older vaults expose no totalAssets(); the same identity is recoverable
        # from supply × rate, which the next check cross-checks against the
        # issuer's multiplier.
        implied = decode_uint(supply) * decode_uint(multiplier) // ONE_SHARE_18
        backed = _same_reserve(implied, decode_uint(reserve))
        checks.append(Check(
            "wrapper_fully_backed", backed,
            f"wrapper totalSupply {decode_uint(supply)} × multiplier "
            f"{decode_uint(multiplier)} / 1e18 = {implied} wei vs native "
            f"balanceOf(wrapper) = {decode_uint(reserve)} wei"
            + ("" if backed else " (the vault does not hold the assets it minted "
                                 "shares against)"),
            f"eth_call {wrapper}.totalSupply / {native}.multiplier / "
            f"{native}.balanceOf"))
    checks.append(Check(
        "wrapper_declared_asset",
        declared_address == native and bool(native),
        f"wrapper.asset() = {declared_address or 'unreadable'} (expected {native or 'n/a'})",
        f"eth_call {wrapper}.asset"))

    # 3. The vault's conversion rate, and the shares-per-token it implies.
    #
    #    ``convertToAssets(1e18)`` is the number of native tokens one wrapper
    #    token redeems for, and on every xStock measured so far it equals the
    #    issuer's ``multiplier()`` exactly (7/7 US and 6/6 HK, 2026-09-22) —
    #    the vault uses the corporate-action factor as its exchange rate. That
    #    equality is checked on its own, because it is what makes
    #    ``shares_per_token = multiplier()`` a measured fact rather than a
    #    convention: the wrapper we trade is worth ``rate`` native units, and a
    #    native unit is one share (cross-checked against the venue on
    #    2026-09-22 — OKX spot XAAPL traded 339.72 against its own AAPL perp at
    #    339.855, i.e. 1.00 share, while the wAAPLx pool traded at 341.11).
    rate = _call(rpc, wrapper, "convertToAssets(uint256)", str(ONE_SHARE_18))
    if rate is None or multiplier is None:
        checks.append(Check("vault_conversion_rate", False,
                            "could not read wrapper.convertToAssets() or "
                            "native.multiplier()",
                            f"eth_call {wrapper}.convertToAssets / {native}.multiplier"))
    else:
        same = decode_uint(rate) == decode_uint(multiplier)
        checks.append(Check(
            "vault_conversion_rate", same,
            f"wrapper.convertToAssets(1.0) = {decimal_from_word(rate)} native token(s) "
            f"vs native.multiplier() = {decimal_from_word(multiplier)}"
            + ("" if same else " (the vault's rate and the issuer's corporate-action "
                               "factor disagree)"),
            f"eth_call {wrapper}.convertToAssets / {native}.multiplier"))

    native_shares = Decimal(str(spec.get("native_shares_per_unit") or "1"))
    if rate is None:
        checks.append(Check("share_conversion", False,
                            "could not read wrapper.convertToAssets(); the configured "
                            f"shares_per_token = {spec.get('shares_per_token')} is "
                            "unverified",
                            f"eth_call {wrapper}.convertToAssets"))
    else:
        measured = decimal_from_word(rate) * native_shares
        configured = Decimal(str(spec.get("shares_per_token") or "0"))
        ok = measured == configured
        checks.append(Check(
            "share_conversion", ok,
            f"1 {spec.get('token_symbol') or 'token'} redeems for "
            f"{decimal_from_word(rate)} native token(s) × "
            f"{native_shares} share(s) per native token = {measured} share(s); "
            f"configured shares_per_token = {configured}"
            + ("" if ok else " — a corporate action moved the ratio"),
            f"eth_call {wrapper}.convertToAssets + issuer token semantics"))

    # 4. The exchange contract: one contract has to be one share.
    if not with_okx_spec:
        return checks
    ct_val = str(instrument.get("ctVal") or "")
    ct_val_ccy = str(instrument.get("ctValCcy") or "")
    ct_mult = str(instrument.get("ctMult") or "")
    expected_ccy = str(spec.get("perp_value_currency") or "").upper()
    ok = (ct_val in {"1", "1.0"} and ct_mult in {"1", "1.0"}
          and (not expected_ccy or ct_val_ccy.upper() == expected_ccy))
    checks.append(Check(
        "perp_contract_value", ok,
        f'ctVal={ct_val} ctValCcy={ct_val_ccy} ctMult={ct_mult} '
        f'→ 1 contract = {ct_val} {ct_val_ccy}',
        "okx /api/v5/public/instruments"))
    return checks


def summarize(checks: list[Check]) -> dict:
    return {
        "ok": all(check.ok for check in checks),
        "passed": sum(1 for check in checks if check.ok),
        "failed": [check.name for check in checks if not check.ok],
        "checks": [check.as_dict() for check in checks],
    }
