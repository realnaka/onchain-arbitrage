"""SQLite catalog for the xStocks book. A separate file from the live monitor."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS underlyings (
  issuer_symbol TEXT PRIMARY KEY,
  underlying_symbol TEXT NOT NULL,
  listing_country TEXT,
  token_isin TEXT,
  underlying_isin TEXT,
  name TEXT NOT NULL,
  company_name TEXT NOT NULL,
  min_order_fiat_value TEXT,
  shares_per_token TEXT,
  first_seen_ms INTEGER NOT NULL,
  last_seen_ms INTEGER NOT NULL,
  missing_since_ms INTEGER
);
CREATE TABLE IF NOT EXISTS deployments (
  issuer_symbol TEXT NOT NULL,
  network TEXT NOT NULL,
  chain_id TEXT,
  address TEXT NOT NULL,
  form TEXT NOT NULL,
  atomic_swaps INTEGER,
  stables_json TEXT NOT NULL,
  shares_per_token TEXT,
  first_seen_ms INTEGER NOT NULL,
  last_seen_ms INTEGER NOT NULL,
  missing_since_ms INTEGER,
  PRIMARY KEY (issuer_symbol, network, form, address)
);
CREATE TABLE IF NOT EXISTS bridges (
  source_network TEXT NOT NULL,
  contract TEXT NOT NULL,
  chain_id TEXT,
  managed_by TEXT,
  destinations_json TEXT NOT NULL,
  products_json TEXT NOT NULL,
  first_seen_ms INTEGER NOT NULL,
  last_seen_ms INTEGER NOT NULL,
  missing_since_ms INTEGER,
  PRIMARY KEY (source_network, contract)
);
CREATE TABLE IF NOT EXISTS spot_exits (
  venue TEXT NOT NULL,
  pair TEXT NOT NULL,
  base TEXT NOT NULL,
  quote TEXT NOT NULL,
  wsname TEXT,
  issuer_symbol TEXT,
  product_form TEXT NOT NULL,
  status TEXT,
  ordermin TEXT,
  lot_decimals INTEGER,
  pair_decimals INTEGER,
  deposit_networks TEXT NOT NULL,
  first_seen_ms INTEGER NOT NULL,
  last_seen_ms INTEGER NOT NULL,
  missing_since_ms INTEGER,
  PRIMARY KEY (venue, pair)
);
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  observed_at_ms INTEGER NOT NULL,
  kind TEXT NOT NULL,
  issuer_symbol TEXT,
  venue TEXT,
  network TEXT,
  form TEXT,
  address TEXT,
  pair TEXT,
  price TEXT,
  bid TEXT,
  bid_size TEXT,
  ask TEXT,
  bid_notional_within_50bps TEXT,
  equity_usd TEXT,
  equity_session TEXT,
  equity_price_kind TEXT,
  discount_bps TEXT,
  screen_hit INTEGER NOT NULL DEFAULT 0,
  quality TEXT NOT NULL,
  source TEXT NOT NULL,
  detail_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_obs_lookup
  ON observations(kind, issuer_symbol, observed_at_ms);
CREATE TABLE IF NOT EXISTS refresh_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at_ms INTEGER NOT NULL,
  finished_at_ms INTEGER,
  kind TEXT NOT NULL,
  ok INTEGER NOT NULL,
  summary_json TEXT NOT NULL
);
"""


def _text(value) -> str | None:
    if value is None:
        return None
    return str(value)


class BookStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def _upsert(self, sql: str, params: tuple) -> None:
        self.conn.execute(sql, params)

    def upsert_underlying(self, row: dict, now_ms: int) -> None:
        self.conn.execute(
            """INSERT INTO underlyings(
                 issuer_symbol, underlying_symbol, listing_country, token_isin,
                 underlying_isin, name, company_name, min_order_fiat_value,
                 shares_per_token, first_seen_ms, last_seen_ms, missing_since_ms)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)
               ON CONFLICT(issuer_symbol) DO UPDATE SET
                 underlying_symbol=excluded.underlying_symbol,
                 listing_country=excluded.listing_country,
                 token_isin=excluded.token_isin,
                 underlying_isin=excluded.underlying_isin,
                 name=excluded.name,
                 company_name=excluded.company_name,
                 min_order_fiat_value=excluded.min_order_fiat_value,
                 shares_per_token=excluded.shares_per_token,
                 last_seen_ms=excluded.last_seen_ms,
                 missing_since_ms=NULL""",
            (row["issuer_symbol"], row["underlying_symbol"], row["listing_country"],
             row["token_isin"], row["underlying_isin"], row["name"], row["company_name"],
             row["min_order_fiat_value"], row["shares_per_token"], now_ms, now_ms),
        )

    def upsert_deployment(self, row: dict, now_ms: int) -> None:
        atomic = None if row["atomic_swaps"] is None else int(row["atomic_swaps"])
        self.conn.execute(
            """INSERT INTO deployments(
                 issuer_symbol, network, chain_id, address, form, atomic_swaps,
                 stables_json, shares_per_token, first_seen_ms, last_seen_ms,
                 missing_since_ms)
               VALUES(?,?,?,?,?,?,?,?,?,?,NULL)
               ON CONFLICT(issuer_symbol, network, form, address) DO UPDATE SET
                 chain_id=excluded.chain_id,
                 atomic_swaps=excluded.atomic_swaps,
                 stables_json=excluded.stables_json,
                 shares_per_token=excluded.shares_per_token,
                 last_seen_ms=excluded.last_seen_ms,
                 missing_since_ms=NULL""",
            (row["issuer_symbol"], row["network"], row["chain_id"], row["address"],
             row["form"], atomic, row["stables_json"], row["shares_per_token"],
             now_ms, now_ms),
        )

    def upsert_bridge(self, row: dict, now_ms: int) -> None:
        self.conn.execute(
            """INSERT INTO bridges(
                 source_network, contract, chain_id, managed_by, destinations_json,
                 products_json, first_seen_ms, last_seen_ms, missing_since_ms)
               VALUES(?,?,?,?,?,?,?,?,NULL)
               ON CONFLICT(source_network, contract) DO UPDATE SET
                 chain_id=excluded.chain_id,
                 managed_by=excluded.managed_by,
                 destinations_json=excluded.destinations_json,
                 products_json=excluded.products_json,
                 last_seen_ms=excluded.last_seen_ms,
                 missing_since_ms=NULL""",
            (row["source_network"], row["contract"], row["chain_id"], row["managed_by"],
             row["destinations_json"], row["products_json"], now_ms, now_ms),
        )

    def upsert_exit(self, row: dict, now_ms: int) -> None:
        self.conn.execute(
            """INSERT INTO spot_exits(
                 venue, pair, base, quote, wsname, issuer_symbol, product_form,
                 status, ordermin, lot_decimals, pair_decimals, deposit_networks,
                 first_seen_ms, last_seen_ms, missing_since_ms)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
               ON CONFLICT(venue, pair) DO UPDATE SET
                 base=excluded.base,
                 quote=excluded.quote,
                 wsname=excluded.wsname,
                 issuer_symbol=excluded.issuer_symbol,
                 product_form=excluded.product_form,
                 status=excluded.status,
                 ordermin=excluded.ordermin,
                 lot_decimals=excluded.lot_decimals,
                 pair_decimals=excluded.pair_decimals,
                 deposit_networks=excluded.deposit_networks,
                 last_seen_ms=excluded.last_seen_ms,
                 missing_since_ms=NULL""",
            (row["venue"], row["pair"], row["base"], row["quote"], row.get("wsname"),
             row.get("issuer_symbol"), row["product_form"], row.get("status"),
             row.get("ordermin"), row.get("lot_decimals"), row.get("pair_decimals"),
             row["deposit_networks"], now_ms, now_ms),
        )

    def mark_missing(self, table: str, key_cols: list[str], seen: set[tuple], now_ms: int) -> int:
        cols = ", ".join(key_cols)
        rows = self.conn.execute(f"SELECT {cols}, missing_since_ms FROM {table}").fetchall()
        marked = 0
        for row in rows:
            key = tuple(row[col] for col in key_cols)
            if key in seen or row["missing_since_ms"] is not None:
                continue
            where = " AND ".join(f"{col}=?" for col in key_cols)
            self.conn.execute(
                f"UPDATE {table} SET missing_since_ms=? WHERE {where}",
                (now_ms, *key),
            )
            marked += 1
        return marked

    def commit(self) -> None:
        self.conn.commit()

    def add_observation(self, row: dict) -> None:
        self.conn.execute(
            """INSERT INTO observations(
                 observed_at_ms, kind, issuer_symbol, venue, network, form, address,
                 pair, price, bid, bid_size, ask, bid_notional_within_50bps,
                 equity_usd, equity_session, equity_price_kind, discount_bps,
                 screen_hit, quality, source, detail_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["observed_at_ms"], row["kind"], row.get("issuer_symbol"), row.get("venue"),
             row.get("network"), row.get("form"), row.get("address"), row.get("pair"),
             _text(row.get("price")), _text(row.get("bid")), _text(row.get("bid_size")),
             _text(row.get("ask")), _text(row.get("bid_notional_within_50bps")),
             _text(row.get("equity_usd")), row.get("equity_session"),
             row.get("equity_price_kind"), _text(row.get("discount_bps")),
             int(row.get("screen_hit") or 0), row["quality"], row["source"],
             row.get("detail_json") or "{}"),
        )

    def record_run(self, *, started_at_ms: int, finished_at_ms: int, kind: str,
                   ok: bool, summary_json: str) -> None:
        self.conn.execute(
            """INSERT INTO refresh_runs(started_at_ms, finished_at_ms, kind, ok, summary_json)
               VALUES(?,?,?,?,?)""",
            (started_at_ms, finished_at_ms, kind, int(ok), summary_json),
        )
        self.conn.commit()

    def live_underlyings(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM underlyings WHERE missing_since_ms IS NULL ORDER BY issuer_symbol"))

    def issuer_symbols(self) -> set[str]:
        return {row["issuer_symbol"] for row in self.live_underlyings()}

    def live_deployments(self, issuer_symbol: str | None = None) -> list[sqlite3.Row]:
        if issuer_symbol is None:
            return list(self.conn.execute(
                """SELECT * FROM deployments WHERE missing_since_ms IS NULL
                   ORDER BY issuer_symbol, network, form"""))
        return list(self.conn.execute(
            """SELECT * FROM deployments
               WHERE issuer_symbol=? AND missing_since_ms IS NULL
               ORDER BY network, form""",
            (issuer_symbol,),
        ))

    def live_bridges(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM bridges WHERE missing_since_ms IS NULL ORDER BY source_network"))

    def live_exits(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            """SELECT * FROM spot_exits WHERE missing_since_ms IS NULL
               ORDER BY issuer_symbol, pair"""))

    def counts(self) -> dict[str, int]:
        out = {}
        for table in ("underlyings", "deployments", "bridges", "spot_exits"):
            total, live = self.conn.execute(
                f"""SELECT COUNT(*),
                    SUM(CASE WHEN missing_since_ms IS NULL THEN 1 ELSE 0 END)
                    FROM {table}"""
            ).fetchone()
            out[table] = int(total)
            out[f"{table}_live"] = int(live or 0)
        venues = [row["venue"] for row in self.conn.execute("SELECT DISTINCT venue FROM spot_exits")]
        out["exit_venues"] = len(venues)
        return out

    def exit_venues(self) -> list[str]:
        return [row["venue"] for row in self.conn.execute(
            "SELECT DISTINCT venue FROM spot_exits ORDER BY venue")]

    def latest_observations(self, kind: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            """SELECT * FROM observations
               WHERE id IN (
                 SELECT MAX(id) FROM observations WHERE kind=?
                 GROUP BY ifnull(issuer_symbol,''), ifnull(network,''), ifnull(form,''),
                          ifnull(address,''), ifnull(pair,'')
               )
               ORDER BY issuer_symbol, network, form, pair""",
            (kind,),
        ))
