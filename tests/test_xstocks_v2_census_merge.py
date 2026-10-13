"""The census merge must not let a narrow scan erase a wide one.

Pinned because it happened: the first targeted run (2026-10-09, ``--symbols
FLWSx``) named all nine chains, the chain-level rule saw every chain as
"touched", and 24 candidates measured that afternoon disappeared from the file
the board reads. ``data/`` is not tracked by git, so there was nothing to
restore from.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path


def _census_script():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "v2census", root / "scripts" / "xstocks_chain_census.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OLD = {
    "generated_at": "old",
    "chains": {
        "eth": {"symbols_in_scope": ["AAPLx", "SPYx"], "pools_with_depth": 2,
                "deepest_measured_usd": "1893245"},
        "bsc": {"symbols_in_scope": ["MSTRx"], "pools_with_depth": 1},
    },
    "candidates": [{"slug": "eth", "symbol": "SPYx"},
                   {"slug": "bsc", "symbol": "MSTRx"}],
    "traps": [{"chain": "eth", "symbol": "AAPLx"}],
    "rejected": [{"chain": "eth", "symbol": "SPYx"}],
    "unresolved": [{"chain": "eth", "symbol": "TSLAx"}],
}


def _write(tmp: str, payload: dict | None = None) -> Path:
    path = Path(tmp) / "census.json"
    path.write_text(json.dumps(payload if payload is not None else OLD))
    return path


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.module = _census_script()

    def test_a_targeted_run_keeps_every_other_symbol(self):
        fresh = {
            "generated_at": "new",
            "chains": {"eth": {"symbols_in_scope": ["FLWSx"], "pools_with_depth": 0},
                       "bsc": {"symbols_in_scope": ["FLWSx"], "pools_with_depth": 0}},
            "candidates": [], "traps": [], "rejected": [], "unresolved": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            merged = self.module.merge_payload(
                fresh, _write(tmp), ["eth", "bsc"], ["FLWSx"])
        self.assertEqual({row["symbol"] for row in merged["candidates"]},
                         {"SPYx", "MSTRx"})
        self.assertEqual({row["symbol"] for row in merged["traps"]}, {"AAPLx"})
        self.assertEqual({row["symbol"] for row in merged["unresolved"]}, {"TSLAx"})

    def test_a_targeted_run_replaces_only_the_names_it_looked_at(self):
        fresh = {
            "generated_at": "new",
            "chains": {"eth": {"symbols_in_scope": ["SPYx"], "pools_with_depth": 9}},
            "candidates": [{"slug": "eth", "symbol": "SPYx", "depth_usd": "1"}],
            "traps": [], "rejected": [], "unresolved": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            merged = self.module.merge_payload(fresh, _write(tmp), ["eth"], ["SPYx"])
        rows = [row for row in merged["candidates"] if row["slug"] == "eth"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["depth_usd"], "1")
        self.assertEqual([row["symbol"] for row in merged["candidates"]
                          if row["slug"] == "bsc"], ["MSTRx"])

    def test_a_targeted_run_keeps_the_wider_counts_and_marks_itself_partial(self):
        fresh = {
            "generated_at": "new",
            "chains": {"eth": {"symbols_in_scope": ["FLWSx"], "pools_with_depth": 0,
                               "deepest_measured_usd": "0"}},
            "candidates": [], "traps": [], "rejected": [], "unresolved": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            merged = self.module.merge_payload(fresh, _write(tmp), ["eth"], ["FLWSx"])
        chain = merged["chains"]["eth"]
        self.assertTrue(chain["partial"])
        self.assertEqual(chain["symbols_in_scope"], ["AAPLx", "FLWSx", "SPYx"])
        self.assertEqual(chain["deepest_measured_usd"], "1893245")

    def test_a_full_run_still_owns_the_chains_it_names(self):
        fresh = {
            "generated_at": "new",
            "chains": {"eth": {"symbols_in_scope": ["AAPLx"], "pools_with_depth": 0}},
            "candidates": [{"slug": "eth", "symbol": "AAPLx"}],
            "traps": [], "rejected": [], "unresolved": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            merged = self.module.merge_payload(fresh, _write(tmp), ["eth"])
        # eth was rescanned, so its old rows go; bsc was not, so they stay.
        self.assertEqual([row["symbol"] for row in merged["candidates"]],
                         ["MSTRx", "AAPLx"])
        self.assertEqual(merged["chains"]["eth"]["symbols_in_scope"], ["AAPLx"])
        self.assertNotIn("partial", merged["chains"]["eth"])

    def test_census_does_not_shadow_its_own_scope_argument(self):
        """A local named ``symbols`` silently turned every run into a targeted one.

        It did on 2026-10-09: ``census()`` already built an address -> row map
        called ``symbols``, which shadowed the new scope parameter, so the
        targeted merge branch ran for full sweeps too and the chain summaries
        stopped being updated.
        """
        root = Path(__file__).resolve().parents[1]
        source = (root / "scripts" / "xstocks_chain_census.py").read_text()
        body = source.split("def census(", 1)[1].split("\ndef ", 1)[0]
        shadowed = [line.strip() for line in body.splitlines()
                    if re.match(r"\s*symbols\s*=", line)]
        self.assertEqual(shadowed, [])


class MergeReportTest(unittest.TestCase):
    """A86's missing line: what the merge is about to replace, said first.

    The count has to describe the merge exactly, in both modes, or it is one
    more number nobody can trust. ``kept_total`` is checked against the merge
    itself: every row the merge keeps is an old row, and it keeps exactly the
    number the report named.
    """

    def setUp(self):
        self.module = _census_script()

    def _kept_rows(self, merged: dict, fresh: dict) -> int:
        return sum(len(merged.get(name) or []) - len(fresh.get(name) or [])
                   for name in self.module.ROW_LISTS)

    def test_a_targeted_run_reports_that_it_replaces_nothing_here(self):
        fresh = {"generated_at": "new", "chains": {}, "candidates": [], "traps": [],
                 "rejected": [], "unresolved": []}
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp)
            report = self.module.merge_report(fresh, path, ["eth", "bsc"], ["FLWSx"])
            merged = self.module.merge_payload(fresh, path, ["eth", "bsc"], ["FLWSx"])
        self.assertEqual(report["mode"], "targeted")
        self.assertEqual(report["replaced_total"], 0)
        self.assertEqual(report["kept_total"], self._kept_rows(merged, fresh))

    def test_a_targeted_run_that_does_look_at_a_name_says_so(self):
        fresh = {"generated_at": "new", "chains": {},
                 "candidates": [], "traps": [], "rejected": [], "unresolved": []}
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp)
            report = self.module.merge_report(fresh, path, ["eth"], ["SPYx", "TSLAx"])
            merged = self.module.merge_payload(fresh, path, ["eth"], ["SPYx", "TSLAx"])
        # SPYx sits in candidates and rejected, TSLAx in unresolved.
        self.assertEqual(report["replaced"]["candidates"], 1)
        self.assertEqual(report["replaced"]["rejected"], 1)
        self.assertEqual(report["replaced"]["unresolved"], 1)
        self.assertEqual(report["replaced_total"], 3)
        self.assertEqual(report["kept_total"], self._kept_rows(merged, fresh))

    def test_a_full_run_counts_candidates_by_slug_like_the_merge_does(self):
        """``candidates`` rows are keyed by ``slug``, the others by ``chain``."""
        fresh = {"generated_at": "new", "chains": {},
                 "candidates": [], "traps": [], "rejected": [], "unresolved": []}
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp)
            report = self.module.merge_report(fresh, path, ["eth"])
            merged = self.module.merge_payload(fresh, path, ["eth"])
        self.assertEqual(report["mode"], "full")
        self.assertEqual(report["replaced"]["candidates"], 1)
        self.assertEqual(report["replaced"]["traps"], 1)
        self.assertEqual(report["replaced"]["rejected"], 1)
        self.assertEqual(report["replaced"]["unresolved"], 1)
        self.assertEqual(report["kept_total"], self._kept_rows(merged, fresh))
        # bsc was not swept, so its row survives.
        self.assertEqual([row["symbol"] for row in merged["candidates"]], ["MSTRx"])

    def test_a_missing_file_is_a_fresh_run_not_a_zero(self):
        fresh = {"generated_at": "new", "chains": {}, "candidates": []}
        with tempfile.TemporaryDirectory() as tmp:
            report = self.module.merge_report(fresh, Path(tmp) / "absent.json", ["eth"])
        self.assertEqual(report["mode"], "fresh")
        self.assertEqual(report["replaced_total"], 0)

    def test_the_report_is_written_into_the_payload_it_describes(self):
        """The receipt has to be in the file, not only on a scrolling stdout."""
        root = Path(__file__).resolve().parents[1]
        source = (root / "scripts" / "xstocks_chain_census.py").read_text()
        self.assertIn('payload["merge_report"] = report', source)


class PoolsReachTheScanTest(unittest.TestCase):
    """The scan quotes ``candidates`` only, so this cap *is* coverage.

    Measured 2026-10-10: ``per_chain[:3]`` left X Layer's 38 pools with depth
    arriving as 3, and the operator's question was simply 「为什么每轮监控的
    数量这么少呢？被哪里拦住了」. The default is now "keep all"; a cap stays
    available by flag.
    """

    def setUp(self):
        self.module = _census_script()

    def test_zero_keeps_every_pool(self):
        routes = [{"symbol": f"S{i}"} for i in range(38)]
        self.assertEqual(len(self.module.keep_per_chain(routes, 0)), 38)
        self.assertEqual(len(self.module.keep_per_chain(routes, None)), 38)

    def test_a_positive_cap_still_samples(self):
        routes = [{"symbol": f"S{i}"} for i in range(38)]
        self.assertEqual(len(self.module.keep_per_chain(routes, 3)), 3)

    def test_the_default_is_keep_all(self):
        import inspect
        signature = inspect.signature(self.module.census)
        self.assertEqual(signature.parameters["top_per_chain"].default, 0)

    def test_the_cli_default_is_keep_all(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "scripts" / "xstocks_chain_census.py").read_text()
        self.assertIn('parser.add_argument("--top-per-chain", type=int, default=0,',
                      source)


class IssuerUniverseTest(unittest.TestCase):
    def test_default_scope_includes_a_name_with_no_kraken_pair(self):
        census = _census_script()
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            create table underlyings (
                issuer_symbol text, underlying_symbol text,
                listing_country text, missing_since_ms integer);
            create table deployments (
                issuer_symbol text, network text, address text,
                form text, missing_since_ms integer);
            create table spot_exits (issuer_symbol text);
        """)
        conn.executemany(
            "insert into underlyings values (?,?,?,?)",
            [("AAPLx", "AAPL", None, None),
             ("FLWSx", "FLWS", None, None),
             ("GONEx", "GONE", None, 1)])
        conn.execute("insert into spot_exits values ('AAPLx')")
        conn.executemany(
            "insert into deployments values (?,?,?,?,?)",
            [("AAPLx", "Ethereum", "0x" + "a" * 40, "native", None),
             ("FLWSx", "Ethereum", "0x" + "b" * 40, "native", None)])
        rows = census.load_universe(conn, ["eth"])["eth"]
        self.assertEqual({row["symbol"] for row in rows}, {"AAPLx", "FLWSx"})

    def test_xlayer_gecko_asks_for_addresses_the_watchlist_missed(self):
        census = _census_script()
        rows = [{"address": "0x" + "a" * 40, "symbol": "AAA"},
                {"address": "0x" + "b" * 40, "symbol": "BBB"}]
        covered = {"0x" + "a" * 40}
        self.assertEqual(
            [row["symbol"] for row in census.gecko_targets("x-layer", rows, covered)],
            ["BBB"])
        self.assertEqual(
            [row["symbol"] for row in census.gecko_targets("mantle", rows, covered)],
            ["AAA", "BBB"])


if __name__ == "__main__":
    unittest.main()
