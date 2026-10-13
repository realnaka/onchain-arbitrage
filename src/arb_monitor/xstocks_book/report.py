"""Text report for the xStocks book. Reads the sqlite file and nothing else."""
from __future__ import annotations

import json
from decimal import Decimal

from arb_monitor.xstocks_book.store import BookStore


def _fmt(value) -> str:
    if value is None or value == "":
        return "-"
    try:
        text = format(Decimal(str(value)), "f")
    except Exception:
        return str(value)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _bar(title: str) -> str:
    return title + "\n" + "-" * len(title)


def render(store: BookStore, symbol: str | None = None) -> str:
    counts = store.counts()
    lines = [
        _bar("xStocks 目录"),
        f"正股 {counts['underlyings_live']} 个在册"
        f"（累计 {counts['underlyings']}，消失 {counts['underlyings'] - counts['underlyings_live']}）",
        f"部署 {counts['deployments_live']} 行在册"
        f"（累计 {counts['deployments']}，消失 {counts['deployments'] - counts['deployments_live']}）",
        f"桥 {counts['bridges_live']} 座在册",
        f"Kraken 现货 {counts['spot_exits_live']} 个交易对在册",
        "本阶段退出场所只有 Kraken。其他交易所未采集。",
        "",
    ]
    by_network: dict[str, dict[str, int]] = {}
    deployments = store.live_deployments(symbol)
    for row in deployments:
        bucket = by_network.setdefault(row["network"], {"native": 0, "wrapper_v2": 0})
        bucket[row["form"]] = bucket.get(row["form"], 0) + 1
    if not symbol:
        lines.append(_bar("各链部署"))
        if not by_network:
            lines.append("（无）")
        for network in sorted(by_network):
            bucket = by_network[network]
            lines.append(
                f"{network}  原生 {bucket.get('native', 0)}  wrapper {bucket.get('wrapper_v2', 0)}"
            )
        lines.append("")

    books = store.latest_observations("kraken_book")
    if symbol:
        books = [row for row in books if row["issuer_symbol"] == symbol]
    lines.append(_bar("Kraken 买盘"))
    if not books:
        lines.append("（还没有采样）")
    liquid = 0
    for row in books:
        size = row["bid_size"]
        has_size = size not in (None, "", "0")
        try:
            has_size = Decimal(str(size)) > 0
        except Exception:
            has_size = False
        if has_size and row["quality"] == "ok":
            liquid += 1
            state = "有买盘"
        elif row["quality"] == "empty_book":
            state = "无流动性"
        elif row["quality"] == "post_only":
            state = "仅限挂单，不能吃买盘"
        else:
            state = row["quality"]
        name = row["issuer_symbol"] or "未对应发行方"
        lines.append(
            f"{name}  {row['pair']}  买一 {_fmt(row['bid'])} × {_fmt(row['bid_size'])}"
            f"  50bps内名义 {_fmt(row['bid_notional_within_50bps'])}  {state}"
        )
    lines.append(f"有买盘 {liquid} / 采样 {len(books)}")
    lines.append("")

    chains = store.latest_observations("chain_ref")
    if symbol:
        chains = [row for row in chains if row["issuer_symbol"] == symbol]
    hits = [row for row in chains if row["screen_hit"]]
    unknown_ratio = sum(1 for row in chains if row["quality"] == "share_ratio_unknown")
    lines.append(_bar("折价筛选（代币比正股便宜至少 50 bps，且 Kraken 买盘有量）"))
    if not hits:
        lines.append("（无）")
    for row in hits:
        detail = {}
        try:
            detail = json.loads(row["detail_json"] or "{}")
        except json.JSONDecodeError:
            detail = {}
        lines.append(
            f"{row['issuer_symbol']}  {row['network']}  {row['form']}  {row['address']}"
            f"  代币 {_fmt(row['price'])}  正股 {_fmt(row['equity_usd'])}"
            f"  { _fmt(row['discount_bps']) } bps  路径 {detail.get('exit_path', '-')}"
            f"  参考价不可执行"
        )
    if unknown_ratio:
        lines.append(
            f"另有 {unknown_ratio} 行链上有价，但发行方接口没有给出每枚代币对应多少股，"
            "所以没有计算 50 bps。"
        )
    lines.append("")

    if symbol:
        lines.append(_bar(symbol))
        underlying = next((row for row in store.live_underlyings() if row["issuer_symbol"] == symbol), None)
        if underlying is None:
            lines.append("发行方目录里没有这个符号，或它已从最近一次成功同步里消失。")
        else:
            lines.append(
                f"{underlying['company_name']}  正股 {underlying['underlying_symbol'] or '-'}"
                f"  {underlying['listing_country'] or '上市地未知'}"
                f"  每股代币 {underlying['shares_per_token'] or '未知'}"
            )
            equity = next((row for row in store.latest_observations("equity")
                           if row["issuer_symbol"] == symbol), None)
            if equity is not None:
                lines.append(
                    f"正股参考 {_fmt(equity['equity_usd'])} 美元"
                    f"  会话 {equity['equity_session'] or '-'}"
                    f"  {equity['equity_price_kind'] or '-'}"
                )
        if not deployments:
            lines.append("（无在册部署）")
        chain_by = {(row["network"], row["form"], row["address"]): row for row in chains}
        for row in deployments:
            obs = chain_by.get((row["network"], row["form"], row["address"]))
            if obs is None:
                lines.append(f"{row['network']}  {row['form']}  {row['address']}  （未采样）")
                continue
            if obs["discount_bps"] in (None, ""):
                discount = "折价未计算"
            else:
                discount = f"折价 {_fmt(obs['discount_bps'])} bps"
            lines.append(
                f"{row['network']}  {row['form']}  {row['address']}"
                f"  价 {_fmt(obs['price'])}  质量 {obs['quality']}"
                f"  {discount}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
