"""New-listing detection: the part that answers "first moment" for a new IPO.

Pinned here because the failure mode is silent: if discovery reports "nothing
new" when a source is actually unreadable, a listing like OURAx stays invisible
and no scan row is ever produced for it.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from arb_monitor.xstocks_v2 import discovery
from arb_monitor.xstocks_v2.feeds import census
from arb_monitor.xstocks_v2.feeds.http import HttpError


def _script():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "v2discovery", root / "scripts" / "xstocks_v2_discovery.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _units_script():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "v2units", root / "scripts" / "kraken_xstocks_units.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _nightly_due(now: float, at: str, last_day):
    return _script().nightly_due(now=now, at=at, last_day=last_day)


class _FakeHttp:
    """Routes by URL substring; records what was asked for."""

    def __init__(self, routes):
        self.routes = routes
        self.urls: list[str] = []

    def request(self, url, *, data=None, headers=None):
        self.urls.append(url)
        for needle, answer in self.routes.items():
            if needle in url:
                if isinstance(answer, Exception):
                    raise answer
                payload = answer
                if isinstance(payload, dict):
                    payload = json.dumps(payload).encode()
                return 200, payload
        raise HttpError(url, "no route")


def _kraken(*symbols):
    return {"error": [], "result": {
        f"X{sym}ZUSD": {"base": sym, "wsname": f"{sym}/USD"} for sym in symbols}}


def _issuer(page, nodes, *, has_next):
    return {"nodes": nodes, "page": {"currentPage": page, "hasNextPage": has_next}}


def _node(symbol, **rest):
    return {"symbol": symbol, **rest}


class KrakenSymbolsTest(unittest.TestCase):
    def test_reads_every_pair_and_skips_junk_rows(self):
        http = _FakeHttp({"AssetPairs": {"error": [], "result": {
            "A": {"base": "AAPLx", "wsname": "AAPLx/USD"},
            "B": "not-a-dict",
            "C": {"base": "", "wsname": "x"},
        }}})
        self.assertEqual(list(discovery.kraken_live_symbols(http)), ["AAPLx"])

    def test_kraken_error_list_is_an_error_not_an_empty_listing(self):
        http = _FakeHttp({"AssetPairs": {"error": ["EGeneral:Invalid"]}})
        with self.assertRaises(HttpError):
            discovery.kraken_live_symbols(http)


class IssuerSymbolsTest(unittest.TestCase):
    def test_walks_every_page_and_collects_deployments(self):
        http = _FakeHttp({
            "page=1": _issuer(1, [_node("SPCXx", isin="CH1", underlyingSymbol="SPCX",
                                        deployments=[{"network": "Solana"},
                                                     {"network": "Ethereum"}])],
                              has_next=True),
            "page=2": _issuer(2, [_node("OURAx", isin="CH2", underlyingSymbol="OURA",
                                        deployments=[{"network": "Solana"}])],
                              has_next=False),
            "public/assets": _issuer(0, [_node("AAPLx", underlyingSymbol="AAPL")],
                                     has_next=True),
        })
        out = discovery.issuer_live_symbols(http)
        self.assertEqual(sorted(out), ["AAPLx", "OURAx", "SPCXx"])
        self.assertEqual(out["SPCXx"]["networks"], ["Ethereum", "Solana"])
        self.assertEqual(out["OURAx"]["underlying"], "OURA")

    def test_a_page_that_repeats_the_previous_one_ends_the_walk(self):
        # An API that ignores out-of-range pages returns page 1 forever; without
        # this guard the loop would run until MAX_PAGES.
        http = _FakeHttp({
            "?page=": _issuer(1, [_node("AAPLx")], has_next=True),
            "public/assets": _issuer(1, [_node("AAPLx")], has_next=True),
        })
        out = discovery.issuer_live_symbols(http, max_pages=9)
        self.assertEqual(list(out), ["AAPLx"])
        self.assertEqual(len(http.urls), 2)


class KnownUniverseTest(unittest.TestCase):
    def test_known_kraken_symbols_comes_from_the_units_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "units.toml"
            path.write_text(
                '[kraken_units.SPCXx]\nstatus = "online"\n\n'
                '[kraken_units.QQQx]\nstatus = "online"\n')
            self.assertEqual(discovery.known_kraken_symbols(path), {"SPCXx", "QQQx"})

    def test_a_missing_units_file_is_an_empty_set_not_a_crash(self):
        self.assertEqual(discovery.known_kraken_symbols("/nonexistent/units.toml"), set())

    def test_known_issuer_symbols_skips_delisted_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "book.sqlite"
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE underlyings (issuer_symbol TEXT, missing_since_ms INTEGER)")
            conn.executemany("INSERT INTO underlyings VALUES (?, ?)",
                             [("SPCXx", None), ("GONEx", 123)])
            conn.commit()
            conn.close()
            self.assertEqual(discovery.known_issuer_symbols(path), {"SPCXx"})


class DottedSymbolTest(unittest.TestCase):
    """``BRK.Bx`` must survive the units file, both ways.

    2026-10-09: the generator wrote ``[kraken_units.BRK.Bx]`` unquoted, TOML
    read that as three nested tables, and the scan has been given a symbol
    called ``BRK`` with an empty row ever since. ``BRK.Bx`` was invisible to
    every v2 round -- not because it was unlisted, but because of a key.
    """

    LEGACY = '[kraken_units.BRK.Bx]\nstatus = "online"\neffective_min_units = "1"\n'

    def test_census_reads_a_dotted_symbol_out_of_the_legacy_nesting(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "units.toml"
            path.write_text(self.LEGACY)
            parsed = census.kraken_symbols(path)
        self.assertEqual(list(parsed), ["BRK.Bx"])
        self.assertEqual(parsed["BRK.Bx"]["status"], "online")

    def test_census_reads_the_fixed_file_the_same_way(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "units.toml"
            path.write_text('[kraken_units."BRK.Bx"]\nstatus = "online"\n')
            self.assertEqual(list(census.kraken_symbols(path)), ["BRK.Bx"])

    def test_generator_quotes_only_what_needs_it(self):
        units = _units_script()
        self.assertEqual(units.toml_key("AAPLx"), "AAPLx")
        self.assertEqual(units.toml_key("BRK.Bx"), '"BRK.Bx"')

    def test_generated_file_round_trips_a_dotted_symbol(self):
        units = _units_script()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "units.toml"
            units.write_toml({"version": "1", "generated_at": "now",
                              "rows": {"BRK.Bx": {"status": "online"},
                                       "AAPLx": {"status": "online"}}}, path)
            parsed = census.kraken_symbols(path)
        self.assertEqual(sorted(parsed), ["AAPLx", "BRK.Bx"])
        self.assertEqual(parsed["BRK.Bx"]["status"], "online")

    def test_a_real_symbol_is_not_read_as_a_prefix_of_another(self):
        parsed = census.kraken_symbols(discovery.DEFAULT_UNITS)
        self.assertIn("BRK.Bx", parsed)
        self.assertNotIn("BRK", parsed)


class DetectTest(unittest.TestCase):
    def _report(self, *, known_kraken, known_issuer, kraken, issuer):
        return discovery.detect(
            known_kraken=set(known_kraken), known_issuer=set(known_issuer),
            kraken_live={s: {"pair": f"{s}/USD"} for s in kraken},
            issuer_live={s: {"underlying": s.rstrip("x"), "networks": ["Solana"]}
                         for s in issuer})

    def test_a_symbol_on_kraken_but_not_in_the_units_file_is_new(self):
        report = self._report(known_kraken={"AAPLx"}, known_issuer={"AAPLx", "OURAx"},
                              kraken=["AAPLx", "OURAx"], issuer=["AAPLx", "OURAx"])
        self.assertEqual(report["new_on_kraken"], ["OURAx"])
        self.assertEqual(report["new_symbols"], ["OURAx"])
        self.assertEqual(report["new_rows"][0]["kraken_pair"], "OURAx/USD")

    def test_a_symbol_at_the_issuer_only_is_new(self):
        report = self._report(known_kraken={"AAPLx"}, known_issuer={"AAPLx"},
                              kraken=["AAPLx"], issuer=["AAPLx", "OURAx"])
        self.assertEqual(report["new_on_kraken"], [])
        self.assertEqual(report["new_at_issuer"], ["OURAx"])
        self.assertFalse(report["new_rows"][0]["on_kraken"])

    def test_everything_known_reports_nothing(self):
        report = self._report(known_kraken={"AAPLx"}, known_issuer={"AAPLx"},
                              kraken=["AAPLx"], issuer=["AAPLx"])
        self.assertEqual(report["new_symbols"], [])


class CheckTest(unittest.TestCase):
    def test_one_bad_source_is_an_error_and_not_an_empty_listing(self):
        http = _FakeHttp({
            "AssetPairs": _kraken("AAPLx"),
            "public/assets": RuntimeError("proxy down"),
        })
        report = discovery.check(http, units_path="/nonexistent", book_path="/nonexistent")
        self.assertFalse(report["ok"])
        self.assertEqual(report["sources"]["issuer"], "error")
        self.assertIn("issuer", report["errors"])
        self.assertIn("读不到 issuer", discovery.format_message(report))

    def test_a_new_symbol_reaches_the_message(self):
        http = _FakeHttp({
            "AssetPairs": _kraken("AAPLx", "OURAx"),
            "public/assets": _issuer(0, [_node("AAPLx"), _node("OURAx", underlyingSymbol="OURA",
                                                        deployments=[{"network": "Solana"}])],
                                     has_next=False),
        })
        report = discovery.check(http, units_path="/nonexistent", book_path="/nonexistent")
        self.assertEqual(report["new_symbols"], ["AAPLx", "OURAx"])
        text = discovery.format_message(report)
        self.assertIn("OURAx", text)
        self.assertIn("OURA", text)

    def test_script_is_hermetic_dry_run_writes_nothing(self):
        module = _script()
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "discovery.json"
            fake = _FakeHttp({
                "AssetPairs": _kraken("AAPLx"),
                "public/assets": _issuer(0, [_node("AAPLx")], has_next=False),
            })
            with unittest.mock.patch.object(module, "Http", lambda **kw: fake):
                code = module.main(["--units", "/nonexistent", "--book", "/nonexistent",
                                    "--report", str(report_path), "--dry-run", "--json"])
            self.assertEqual(code, 0)
            self.assertFalse(report_path.exists())

    def test_script_writes_the_report_when_not_dry_run(self):
        module = _script()
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "discovery.json"
            fake = _FakeHttp({
                "AssetPairs": _kraken("AAPLx"),
                "public/assets": _issuer(0, [_node("AAPLx")], has_next=False),
            })
            with unittest.mock.patch.object(module, "Http", lambda **kw: fake):
                code = module.main(["--units", "/nonexistent", "--book", "/nonexistent",
                                    "--report", str(report_path)])
            self.assertEqual(code, 0)
            written = json.loads(report_path.read_text())
            self.assertTrue(written["ok"])
            self.assertIn("message", written)

    def test_script_exits_two_when_a_source_fails(self):
        module = _script()
        with unittest.mock.patch.object(module.discovery, "check") as patched, \
                unittest.mock.patch.object(module, "Http"):
            patched.return_value = {"ok": False, "errors": {"kraken": "boom"},
                                    "new_symbols": []}
            code = module.main(["--dry-run"])
        self.assertEqual(code, 2)


class _Done:
    def __init__(self, returncode=0, stdout="ok"):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


class IngestTest(unittest.TestCase):
    """A new listing must widen the universe, not only raise a flag."""

    def _run(self, report):
        module = _script()
        calls: list[list[str]] = []

        def runner(cmd, **kwargs):
            calls.append(list(cmd))
            return _Done()

        result = module.run_ingest(report, runner=runner)
        return result, calls

    def test_a_new_issuer_symbol_refreshes_the_catalog_then_scans_its_pools(self):
        result, calls = self._run({"new_at_issuer": ["FLWSx"], "new_on_kraken": []})
        self.assertEqual([step["name"] for step in result["steps"]],
                         ["catalog_refresh", "chain_census"])
        self.assertEqual(len(calls), 2)
        self.assertIn("xstocks_book.py", " ".join(calls[0]))
        self.assertIn("refresh", calls[0])
        # The catalog has to be written before the census reads the book.
        self.assertIn("xstocks_chain_census.py", " ".join(calls[1]))
        self.assertEqual(calls[1][calls[1].index("--symbols") + 1], "FLWSx")

    def test_a_new_kraken_symbol_validates_only_that_symbol(self):
        result, calls = self._run({"new_at_issuer": [], "new_on_kraken": ["OURAx", "XYZx"]})
        self.assertEqual([step["name"] for step in result["steps"]], ["kraken_units"])
        self.assertIn("--symbols", calls[0])
        self.assertEqual(calls[0][calls[0].index("--symbols") + 1], "OURAx,XYZx")

    def test_nothing_new_runs_nothing(self):
        result, calls = self._run({"new_at_issuer": [], "new_on_kraken": []})
        self.assertFalse(result["ran"])
        self.assertEqual(calls, [])

    def test_both_gaps_run_both_steps_in_order(self):
        result, _ = self._run({"new_at_issuer": ["FLWSx"], "new_on_kraken": ["OURAx"]})
        self.assertEqual([step["name"] for step in result["steps"]],
                         ["catalog_refresh", "chain_census", "kraken_units"])

    def test_the_nightly_census_gets_a_longer_budget_than_a_targeted_one(self):
        """A 15-minute cap would kill the nightly sweep every night."""
        module = _script()
        seen: list[float] = []

        def runner(cmd, **kwargs):
            seen.append(kwargs.get("timeout"))
            return _Done()

        module.run_nightly(runner=runner)
        self.assertEqual(len(seen), 3)
        self.assertEqual(len(set(seen[:2])), 1)          # catalog + units: default
        self.assertGreater(seen[2], seen[0])
        self.assertGreaterEqual(seen[2], 3600)

    def test_a_failed_step_is_reported_not_raised(self):
        module = _script()

        def runner(cmd, **kwargs):
            return _Done(returncode=1, stdout="boom")

        result = module.run_ingest({"new_at_issuer": ["FLWSx"], "new_on_kraken": []},
                                   runner=runner)
        self.assertEqual(result["steps"][0]["returncode"], 1)
        self.assertEqual(result["steps"][0]["tail"], ["boom"])

    def test_main_reports_what_it_ingested(self):
        module = _script()
        first = {"ok": True, "errors": {}, "new_symbols": ["FLWSx"],
                 "new_rows": [{"symbol": "FLWSx", "on_kraken": False}],
                 "new_at_issuer": ["FLWSx"], "new_on_kraken": []}
        after = {"ok": True, "errors": {}, "new_symbols": []}
        with unittest.mock.patch.object(module.discovery, "check",
                                        side_effect=[first, after]) as checked, \
                unittest.mock.patch.object(module, "Http", lambda **kw: None), \
                unittest.mock.patch.object(module, "run_ingest",
                                           return_value={"ran": True, "steps": [
                                               {"name": "catalog_refresh",
                                                "cmd": [], "returncode": 0, "tail": []}]}):
            code = module.main(["--ingest", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(checked.call_count, 2)


class NotifyTest(unittest.TestCase):
    """The job runs every few minutes, so "nothing changed" must be silent."""

    QUIET = {"ok": True, "errors": {}, "new_symbols": [], "new_rows": [],
             "kraken_live_count": 1, "issuer_live_count": 1}

    def _main(self, module, previous, current, path):
        import arb_monitor.xstocks_v2.notify as v2notify
        path.write_text(json.dumps(previous))
        with unittest.mock.patch.object(module.discovery, "check", return_value=current), \
                unittest.mock.patch.object(module, "Http", lambda **kw: None), \
                unittest.mock.patch.object(v2notify, "sender") as sender:
            module.main(["--notify", "--report", str(path)])
        return sender

    def test_a_repeat_is_not_sent_again(self):
        module = _script()
        with tempfile.TemporaryDirectory() as tmp:
            sender = self._main(module, self.QUIET, dict(self.QUIET),
                                Path(tmp) / "d.json")
        sender.assert_not_called()

    def test_a_new_symbol_is_sent(self):
        module = _script()
        current = {**self.QUIET, "new_symbols": ["OURAx"],
                   "new_rows": [{"symbol": "OURAx", "on_kraken": True,
                                 "kraken_pair": "OURAx/USD", "underlying": "OURA",
                                 "networks": ["Solana"]}]}
        with tempfile.TemporaryDirectory() as tmp:
            sender = self._main(module, self.QUIET, current, Path(tmp) / "d.json")
        sender.assert_called_once()
        self.assertEqual(sender.call_args[0][0], "signal")
        self.assertIn("OURAx", sender.return_value.call_args[0][0])


class LoopTest(unittest.TestCase):
    """The resident loop: cheap checks, one full sync a night."""

    def test_nightly_is_due_once_a_day_after_the_hour(self):
        import datetime
        at = "02:00"
        before = datetime.datetime(2026, 10, 10, 1, 59).timestamp()
        after = datetime.datetime(2026, 10, 10, 2, 0).timestamp()
        same_day = "2026-10-10"
        self.assertFalse(_nightly_due(before, at, None))
        self.assertTrue(_nightly_due(after, at, None))
        self.assertFalse(_nightly_due(after, at, same_day))

    def test_a_missed_minute_still_runs_later_that_day(self):
        import datetime
        late = datetime.datetime(2026, 10, 10, 9, 30).timestamp()
        self.assertTrue(_nightly_due(late, "02:00", None))
        self.assertFalse(_nightly_due(late, "02:00", "2026-10-10"))

    def test_the_loop_checks_then_runs_the_full_sync_once(self):
        module = _script()
        calls: list[str] = []
        import datetime
        # Start at local noon, then advance a whole day per tick. A clock that
        # starts at the epoch reads 00:00 local on a UTC runner and 08:00 at
        # UTC+8, so "did the 02:00 nightly fire" used to depend on the
        # machine's timezone; it did not fire in CI and main's CI was red.
        now = {"ms": datetime.datetime(2026, 10, 10, 12, 0).timestamp()}
        ticks = {"n": 0}

        def clock():
            return now["ms"]

        def sleep(seconds):
            now["ms"] += 86_400.0
            ticks["n"] += 1
            if ticks["n"] > 2:
                raise KeyboardInterrupt

        with unittest.mock.patch.object(module.discovery, "check",
                                        return_value={"ok": True, "errors": {},
                                                      "new_symbols": []}), \
                unittest.mock.patch.object(module, "write_json"), \
                unittest.mock.patch.object(module, "read_json", return_value={}), \
                unittest.mock.patch.object(module, "run_nightly",
                                           side_effect=lambda **kw: (
                                               calls.append("nightly"),
                                               {"steps": [], "ran": True})[1]):
            with self.assertRaises(KeyboardInterrupt):
                module.run_loop(interval=1, nightly="02:00",
                                http_factory=lambda: None, runner=lambda *a, **k: None,
                                sleep=sleep, clock=clock)
        self.assertTrue(calls)

    def test_no_nightly_means_checks_only(self):
        module = _script()
        ticks = {"n": 0.0}

        def sleep(seconds):
            ticks["n"] += 1.0
            if ticks["n"] > 1:
                raise KeyboardInterrupt

        with unittest.mock.patch.object(module.discovery, "check",
                                        return_value={"ok": True, "errors": {},
                                                      "new_symbols": []}), \
                unittest.mock.patch.object(module, "write_json"), \
                unittest.mock.patch.object(module, "read_json", return_value={}), \
                unittest.mock.patch.object(module, "run_nightly") as nightly:
            with self.assertRaises(KeyboardInterrupt):
                module.run_loop(interval=1, nightly=None, http_factory=lambda: None,
                                runner=lambda *a, **k: None, sleep=sleep,
                                clock=lambda: 0.0)
        nightly.assert_not_called()


class PendingRailTest(unittest.TestCase):
    """A token that exists but has no priceable route must stay visible.

    Kraken is not the only exit -- ``dex_to_dex`` and ``chain_to_chain`` rows
    are any-to-any and need no Kraken leg -- so "no Kraken pair" is not what
    makes a symbol pending. It stays pending until it can be priced somewhere.
    """

    def test_a_new_issuer_symbol_without_a_kraken_pair_is_pending(self):
        report = {"new_at_issuer": ["FLWSx"], "kraken_symbols": ["AAPLx"]}
        self.assertEqual(discovery.update_pending_rail(report), ["FLWSx"])

    def test_an_entry_leaves_the_list_when_kraken_carries_it(self):
        report = {"new_at_issuer": [], "kraken_symbols": ["AAPLx", "FLWSx"]}
        self.assertEqual(
            discovery.update_pending_rail(report, {"pending_rail": ["FLWSx"]}), [])

    def test_an_entry_also_leaves_when_the_census_found_a_pool(self):
        report = {"new_at_issuer": [], "kraken_symbols": []}
        self.assertEqual(
            discovery.update_pending_rail(report, {"pending_rail": ["FLWSx"]},
                                          priced_symbols={"FLWSx"}), [])

    def test_census_pool_symbols_reads_the_pool_lists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "census.json"
            path.write_text(json.dumps({
                "chains": {"eth": {"symbols_in_scope": ["FLWSx"]}},
                "candidates": [{"symbol": "SPYx"}],
                "traps": [{"symbol": "TSLAx"}],
                "rejected": [{"symbol": "MSTRx"}],
                "unresolved": [{"symbol": "QQQx"}],
            }))
            self.assertEqual(discovery.census_pool_symbols(path),
                             {"SPYx", "TSLAx", "MSTRx", "QQQx"})

    def test_a_name_we_looked_at_and_found_no_pool_for_stays_pending(self):
        # FLWSx is in symbols_in_scope on every chain and has no pool anywhere.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "census.json"
            path.write_text(json.dumps({
                "chains": {"eth": {"symbols_in_scope": ["FLWSx"]}},
                "candidates": [], "traps": [], "rejected": [], "unresolved": [],
            }))
            report = {"new_at_issuer": [], "kraken_symbols": []}
            self.assertEqual(
                discovery.update_pending_rail(
                    report, {"pending_rail": ["FLWSx"]},
                    priced_symbols=discovery.census_pool_symbols(path)),
                ["FLWSx"])
            self.assertNotIn("FLWSx", discovery.census_pool_symbols(path))

    def test_a_symbol_already_on_kraken_is_never_pending(self):
        report = {"new_at_issuer": ["OURAx"], "kraken_symbols": ["OURAx"]}
        self.assertEqual(discovery.update_pending_rail(report), [])

    def test_the_list_survives_a_quiet_round(self):
        report = {"new_at_issuer": [], "kraken_symbols": ["AAPLx"]}
        self.assertEqual(
            discovery.update_pending_rail(report, {"pending_rail": ["FLWSx"]}),
            ["FLWSx"])

    def test_check_reports_the_kraken_symbols_it_saw(self):
        http = _FakeHttp({
            "AssetPairs": _kraken("AAPLx", "OURAx"),
            "public/assets": _issuer(0, [_node("AAPLx")], has_next=False),
        })
        report = discovery.check(http, units_path="/nonexistent", book_path="/nonexistent")
        self.assertEqual(report["kraken_symbols"], ["AAPLx", "OURAx"])

    def test_seed_pending_records_a_symbol_that_was_ingested_by_hand(self):
        module = _script()
        quiet = {"ok": True, "errors": {}, "new_symbols": [], "new_rows": [],
                 "new_at_issuer": [], "new_on_kraken": [],
                 "kraken_symbols": ["AAPLx"]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.json"
            with unittest.mock.patch.object(module.discovery, "check",
                                            return_value=dict(quiet)), \
                    unittest.mock.patch.object(module, "Http", lambda **kw: None):
                module.main(["--report", str(path), "--seed-pending", "FLWSx"])
            written = json.loads(path.read_text())
        self.assertEqual(written["pending_rail"], ["FLWSx"])
if __name__ == "__main__":
    unittest.main()
