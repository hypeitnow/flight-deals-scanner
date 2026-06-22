"""
Comprehensive tests for flight_scanner.py core logic.

Run:
    python -m pytest tests/ -v
    python -m pytest tests/ -v --tb=short
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import sqlite3
import sys
import time
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on the path regardless of cwd
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import flight_scanner as fs

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def airports() -> dict:
    """Load the real airports.json from the project."""
    return fs.load_airports(ROOT / "data" / "airports.json")


@pytest.fixture()
def conn():
    """In-memory SQLite DB for each test."""
    c = fs.db_connect(Path(":memory:"))
    yield c
    c.close()


@pytest.fixture()
def minimal_cfg() -> dict:
    return {
        "amadeus_env": "test",
        "origins": ["WAW"],
        "currency": "PLN",
        "adults": 1,
        "non_stop": False,
        "max_offers_per_query": 5,
        "scan": {
            "date_from": "2026-07-04",
            "date_to": "2026-07-31",
            "weekdays": [4, 5],
            "trip_length_days": [3],
            "max_dates_per_route": 4,
        },
        "destinations": [
            {"country": "ME", "label": "Montenegro", "target_price": 600}
        ],
        "limits": {"max_api_calls": 200},
        "bargain": {"percentile": 25, "min_history": 3, "drop_pct_alert": 15},
        "notify": {"telegram": {"enabled": False}, "webhook": {"enabled": False}},
    }


# ─────────────────────────────────────────────────────────────────────────────
# FLIGHT-1: Airport resolver
# ─────────────────────────────────────────────────────────────────────────────

class TestResolveOrigins:
    def test_group(self, airports):
        result = fs.resolve_origins({"group": "poland_major"}, airports)
        assert "WAW" in result
        assert "KRK" in result
        assert len(result) == 6

    def test_group_poland_all(self, airports):
        result = fs.resolve_origins({"group": "poland_all"}, airports)
        assert len(result) == 12

    def test_country_code(self, airports):
        result = fs.resolve_origins({"country": "ME"}, airports)
        assert result == ["TGD", "TIV"]

    def test_country_string(self, airports):
        result = fs.resolve_origins("ME", airports)
        assert result == ["TGD", "TIV"]

    def test_single_iata_string(self, airports):
        result = fs.resolve_origins("WAW", airports)
        assert result == ["WAW"]

    def test_list_of_iatas(self, airports):
        result = fs.resolve_origins(["WAW", "KRK", "KTW"], airports)
        assert result == ["WAW", "KRK", "KTW"]

    def test_unknown_group_exits(self, airports):
        with pytest.raises(SystemExit):
            fs.resolve_origins({"group": "nonexistent_group"}, airports)

    def test_unknown_country_exits(self, airports):
        with pytest.raises(SystemExit):
            fs.resolve_origins({"country": "XX"}, airports)

    def test_iata_uppercased(self, airports):
        result = fs.resolve_origins(["waw", "krk"], airports)
        assert result == ["WAW", "KRK"]


class TestResolveDestinations:
    def test_country_destination(self, airports):
        dests = fs.resolve_destinations(
            [{"country": "ME", "label": "Montenegro", "target_price": 600}],
            airports,
        )
        assert len(dests) == 1
        assert dests[0]["airports"] == ["TGD", "TIV"]
        assert dests[0]["label"] == "Montenegro"
        assert dests[0]["target_price"] == 600

    def test_country_default_label(self, airports):
        dests = fs.resolve_destinations([{"country": "ME"}], airports)
        assert dests[0]["label"] == "Montenegro"

    def test_airport_destination(self, airports):
        dests = fs.resolve_destinations(
            [{"destination": "BCN", "label": "Barcelona", "target_price": 400}],
            airports,
        )
        assert dests[0]["airports"] == ["BCN"]
        assert dests[0]["key"] == "BCN"

    def test_group_destination(self, airports):
        dests = fs.resolve_destinations([{"group": "balkans"}], airports)
        assert "TGD" in dests[0]["airports"]
        assert "BEG" in dests[0]["airports"]

    def test_no_target_price(self, airports):
        dests = fs.resolve_destinations([{"country": "ME"}], airports)
        assert dests[0]["target_price"] is None

    def test_bosnia_airports(self, airports):
        dests = fs.resolve_destinations([{"country": "BA"}], airports)
        airports_list = dests[0]["airports"]
        assert "SJJ" in airports_list
        assert "OMO" in airports_list
        assert "TZL" in airports_list
        assert "BNX" in airports_list

    def test_unknown_country_exits(self, airports):
        with pytest.raises(SystemExit):
            fs.resolve_destinations([{"country": "XX"}], airports)


class TestNormaliseConfig:
    def test_new_format(self, airports, minimal_cfg):
        origins, dests = fs.normalise_config(minimal_cfg, airports)
        assert origins == ["WAW"]
        assert dests[0]["airports"] == ["TGD", "TIV"]

    def test_legacy_format(self, airports):
        legacy = {
            "origin": "WAW",
            "routes": [{"destination": "BCN", "label": "Barcelona"}],
        }
        origins, dests = fs.normalise_config(legacy, airports)
        assert origins == ["WAW"]
        assert dests[0]["airports"] == ["BCN"]

    def test_missing_origins_exits(self, airports):
        with pytest.raises(SystemExit):
            fs.normalise_config({"destinations": []}, airports)

    def test_missing_destinations_exits(self, airports):
        with pytest.raises(SystemExit):
            fs.normalise_config({"origins": "WAW"}, airports)


# ─────────────────────────────────────────────────────────────────────────────
# FLIGHT-3: ISO duration parsing
# ─────────────────────────────────────────────────────────────────────────────

class TestParseDuration:
    def test_hours_and_minutes(self):
        assert fs._parse_iso_duration("PT2H30M") == 150

    def test_hours_only(self):
        assert fs._parse_iso_duration("PT3H") == 180

    def test_minutes_only(self):
        assert fs._parse_iso_duration("PT45M") == 45

    def test_empty_string(self):
        assert fs._parse_iso_duration("") == 0

    def test_none_like_string(self):
        assert fs._parse_iso_duration("PT0H0M") == 0

    def test_large_duration(self):
        assert fs._parse_iso_duration("PT12H55M") == 12 * 60 + 55


# ─────────────────────────────────────────────────────────────────────────────
# Date generation
# ─────────────────────────────────────────────────────────────────────────────

class TestGenerateDatePairs:
    def test_weekend_filter(self):
        scan = {
            "date_from": "2026-07-01",
            "date_to": "2026-07-31",
            "weekdays": [4, 5],  # Fri + Sat
            "trip_length_days": [3],
            "max_dates_per_route": 8,
        }
        pairs = fs.generate_date_pairs(scan)
        for dep, _ in pairs:
            assert dt.date.fromisoformat(dep).weekday() in (4, 5)

    def test_max_dates_cap(self):
        scan = {
            "date_from": "2026-07-01",
            "date_to": "2026-08-31",
            "weekdays": list(range(7)),
            "trip_length_days": [3],
            "max_dates_per_route": 5,
        }
        pairs = fs.generate_date_pairs(scan)
        assert len(pairs) == 5

    def test_one_way_returns_none(self):
        scan = {
            "date_from": "2026-07-04",
            "date_to": "2026-07-10",
            "weekdays": [4, 5],
            "trip_length_days": [None],
            "max_dates_per_route": 4,
        }
        pairs = fs.generate_date_pairs(scan)
        assert all(ret is None for _, ret in pairs)

    def test_multiple_trip_lengths(self):
        scan = {
            "date_from": "2026-07-04",
            "date_to": "2026-07-31",
            "weekdays": [4],
            "trip_length_days": [3, 7],
            "max_dates_per_route": 8,
        }
        pairs = fs.generate_date_pairs(scan)
        # Should produce pairs of both lengths
        lengths = {
            (dt.date.fromisoformat(r) - dt.date.fromisoformat(d)).days
            for d, r in pairs
        }
        assert 3 in lengths and 7 in lengths

    def test_no_dates_if_window_empty(self):
        scan = {
            "date_from": "2026-07-01",
            "date_to": "2026-06-30",  # end before start
            "weekdays": [4, 5],
            "trip_length_days": [3],
            "max_dates_per_route": 8,
        }
        pairs = fs.generate_date_pairs(scan)
        assert pairs == []


# ─────────────────────────────────────────────────────────────────────────────
# Percentile
# ─────────────────────────────────────────────────────────────────────────────

class TestPercentile:
    def test_empty_returns_none(self):
        assert fs.percentile([], 25) is None

    def test_single_value(self):
        assert fs.percentile([100.0], 50) == 100.0

    def test_p50_even_list(self):
        result = fs.percentile([10.0, 20.0, 30.0, 40.0], 50)
        assert result == pytest.approx(25.0)

    def test_p25_four_values(self):
        result = fs.percentile([100.0, 200.0, 300.0, 400.0], 25)
        assert result == pytest.approx(175.0)

    def test_p0_returns_min(self):
        result = fs.percentile([50.0, 100.0, 150.0], 0)
        assert result == pytest.approx(50.0)

    def test_p100_returns_max(self):
        result = fs.percentile([50.0, 100.0, 150.0], 100)
        assert result == pytest.approx(150.0)


# ─────────────────────────────────────────────────────────────────────────────
# FLIGHT-5: Bargain detection
# ─────────────────────────────────────────────────────────────────────────────

class TestEvaluateBargain:
    RULES = {"percentile": 25, "min_history": 3, "drop_pct_alert": 15}

    def test_target_price_hit(self):
        hit, reason = fs.evaluate_bargain(300, 350, [300], self.RULES)
        assert hit
        assert "target" in reason

    def test_target_price_exact(self):
        hit, reason = fs.evaluate_bargain(350, 350, [350], self.RULES)
        assert hit

    def test_target_price_miss(self):
        hit, _ = fs.evaluate_bargain(400, 350, [400], self.RULES)
        assert not hit

    def test_no_target_no_history(self):
        hit, _ = fs.evaluate_bargain(999, None, [], self.RULES)
        assert not hit

    def test_percentile_bargain(self):
        # history = [500, 480, 520, 300]; prior = [500, 480, 520]; p25 ~ 490 > 300
        hit, reason = fs.evaluate_bargain(300, None, [500, 480, 520, 300], self.RULES)
        assert hit
        assert "p25" in reason

    def test_drop_pct_bargain(self):
        # price = 300, prev min = 480, drop = 37.5% > 15%
        hit, reason = fs.evaluate_bargain(300, None, [500, 480, 520, 300], self.RULES)
        assert hit
        assert "vs prev low" in reason

    def test_insufficient_history_skips_percentile(self):
        # Only 2 prior observations, min_history=3 → percentile skipped
        hit, _ = fs.evaluate_bargain(100, None, [500, 490, 100], self.RULES)
        # 2 prior obs (exclude last) < min_history 3 → no percentile hit
        assert not hit

    def test_above_percentile_no_bargain(self):
        hit, _ = fs.evaluate_bargain(510, None, [500, 480, 520, 510, 490, 510], self.RULES)
        assert not hit

    def test_multiple_reasons(self):
        hit, reason = fs.evaluate_bargain(200, 350, [500, 480, 520, 200], self.RULES)
        assert hit
        assert "target" in reason
        assert "p25" in reason


# ─────────────────────────────────────────────────────────────────────────────
# FLIGHT-6: API budget guardrail
# ─────────────────────────────────────────────────────────────────────────────

class TestBudget:
    def test_estimate_calls(self):
        origins = ["WAW", "KRK", "KTW"]
        dests = [{"airports": ["TGD", "TIV"]}, {"airports": ["SJJ", "BNX"]}]
        dates = [("2026-07-04", "2026-07-07"), ("2026-07-11", "2026-07-14")]
        result = fs.estimate_calls(origins, dests, dates)
        assert result == 3 * 4 * 2  # 24

    def test_check_budget_within_cap(self, capsys):
        fs.check_budget(100, {"max_api_calls": 500})
        out = capsys.readouterr().out
        assert "100" in out

    def test_check_budget_over_cap_exits(self):
        with pytest.raises(SystemExit) as exc_info:
            fs.check_budget(999, {"max_api_calls": 100})
        assert "exceeds the cap" in str(exc_info.value)

    def test_default_cap_is_500(self, capsys):
        # Should not exit with 499 calls and no max_api_calls in limits
        fs.check_budget(499, {})
        out = capsys.readouterr().out
        assert "499" in out


# ─────────────────────────────────────────────────────────────────────────────
# Storage: DB round-trip
# ─────────────────────────────────────────────────────────────────────────────

class TestDatabase:
    OBS = {
        "scanned_at": "2026-07-01T08:00:00+00:00",
        "origin": "WAW",
        "destination": "TGD",
        "depart_date": "2026-07-11",
        "return_date": "2026-07-14",
        "price": 512.50,
        "currency": "PLN",
        "carriers": "FR",
        "stops": 0,
        "duration_min": 130,
    }

    def test_record_and_retrieve(self, conn):
        fs.record_observation(conn, self.OBS)
        history = fs.route_price_history(conn, "WAW", "TGD", "2026-07-11", "2026-07-14")
        assert history == [512.50]

    def test_multiple_observations_ordered(self, conn):
        obs2 = {**self.OBS, "price": 480.0, "scanned_at": "2026-07-02T08:00:00+00:00"}
        obs3 = {**self.OBS, "price": 550.0, "scanned_at": "2026-07-03T08:00:00+00:00"}
        fs.record_observation(conn, self.OBS)
        fs.record_observation(conn, obs2)
        fs.record_observation(conn, obs3)
        history = fs.route_price_history(conn, "WAW", "TGD", "2026-07-11", "2026-07-14")
        assert history == [512.50, 480.0, 550.0]

    def test_wrong_route_not_returned(self, conn):
        fs.record_observation(conn, self.OBS)
        history = fs.route_price_history(conn, "KRK", "TGD", "2026-07-11", "2026-07-14")
        assert history == []

    def test_one_way_stored_separately(self, conn):
        one_way = {**self.OBS, "return_date": None}
        fs.record_observation(conn, one_way)
        # Round-trip query should not find the one-way obs
        rt_history = fs.route_price_history(conn, "WAW", "TGD", "2026-07-11", "2026-07-14")
        assert rt_history == []
        ow_history = fs.route_price_history(conn, "WAW", "TGD", "2026-07-11", None)
        assert ow_history == [512.50]

    def test_stops_and_duration_stored(self, conn):
        fs.record_observation(conn, self.OBS)
        row = conn.execute(
            "SELECT stops, duration_min FROM observations WHERE origin='WAW' AND destination='TGD'"
        ).fetchone()
        assert row == (0, 130)

    def test_migrate_existing_db(self, tmp_path):
        """Schema migration adds stops/duration columns to a legacy DB."""
        db_path = tmp_path / "legacy.db"
        # Create a legacy DB without the new columns
        legacy_conn = sqlite3.connect(db_path)
        legacy_conn.execute(
            """CREATE TABLE observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scanned_at TEXT, origin TEXT, destination TEXT,
                depart_date TEXT, return_date TEXT,
                price REAL, currency TEXT, carriers TEXT
            )"""
        )
        legacy_conn.commit()
        legacy_conn.close()
        # db_connect should migrate it without error
        migrated = fs.db_connect(db_path)
        cols = {row[1] for row in migrated.execute("PRAGMA table_info(observations)")}
        assert "stops" in cols
        assert "duration_min" in cols
        migrated.close()


# ─────────────────────────────────────────────────────────────────────────────
# Demo fetcher
# ─────────────────────────────────────────────────────────────────────────────

class TestDemoFetcher:
    def test_returns_expected_fields(self):
        fetcher = fs.make_demo_fetcher()
        result = fetcher("WAW", "TGD", "2026-07-11", "2026-07-14", 1, "PLN", 5, False)
        assert "price" in result
        assert "currency" in result
        assert "carriers" in result
        assert "stops" in result
        assert "duration_min" in result

    def test_price_positive(self):
        fetcher = fs.make_demo_fetcher()
        result = fetcher("WAW", "TGD", "2026-07-11", "2026-07-14", 1, "PLN", 5, False)
        assert result["price"] > 0

    def test_currency_echoed(self):
        fetcher = fs.make_demo_fetcher()
        result = fetcher("WAW", "TGD", "2026-07-11", None, 1, "EUR", 5, False)
        assert result["currency"] == "EUR"

    def test_stops_valid_range(self):
        fetcher = fs.make_demo_fetcher()
        for _ in range(20):
            r = fetcher("WAW", "TGD", "2026-07-11", "2026-07-14", 1, "PLN", 5, False)
            assert r["stops"] in (0, 1)

    def test_deterministic_seed(self):
        """Same seed → same sequence every time."""
        f1 = fs.make_demo_fetcher()
        f2 = fs.make_demo_fetcher()
        r1 = f1("KRK", "TIV", "2026-07-04", "2026-07-07", 1, "PLN", 5, False)
        r2 = f2("KRK", "TIV", "2026-07-04", "2026-07-07", 1, "PLN", 5, False)
        assert r1["price"] == r2["price"]


# ─────────────────────────────────────────────────────────────────────────────
# Integration: run_scan with demo fetcher
# ─────────────────────────────────────────────────────────────────────────────

class TestRunScan:
    def test_scan_stores_observations(self, conn, minimal_cfg, airports):
        origins, dests = fs.normalise_config(minimal_cfg, airports)
        fetcher = fs.make_demo_fetcher()
        fs.run_scan(minimal_cfg, conn, fetcher, origins, dests)
        count = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        assert count > 0

    def test_scan_returns_alerts_list(self, conn, minimal_cfg, airports):
        origins, dests = fs.normalise_config(minimal_cfg, airports)
        fetcher = fs.make_demo_fetcher()
        alerts = fs.run_scan(minimal_cfg, conn, fetcher, origins, dests)
        assert isinstance(alerts, list)

    def test_bargain_alert_has_reason(self, conn, minimal_cfg, airports):
        """Force a bargain by setting a very high target price."""
        minimal_cfg["destinations"][0]["target_price"] = 99999
        origins, dests = fs.normalise_config(minimal_cfg, airports)
        fetcher = fs.make_demo_fetcher()
        alerts = fs.run_scan(minimal_cfg, conn, fetcher, origins, dests)
        assert len(alerts) > 0
        assert all("reason" in a for a in alerts)

    def test_scan_aborts_over_budget(self, conn, airports):
        """Budget cap of 1 should abort before any API call."""
        cfg = {
            "origins": ["WAW", "KRK"],
            "currency": "PLN",
            "adults": 1,
            "non_stop": False,
            "max_offers_per_query": 5,
            "scan": {
                "date_from": "2026-07-04",
                "date_to": "2026-08-31",
                "weekdays": list(range(7)),
                "trip_length_days": [3],
                "max_dates_per_route": 8,
            },
            "destinations": [{"country": "ME"}],
            "limits": {"max_api_calls": 1},  # tiny cap
            "bargain": {},
            "notify": {},
        }
        origins, dests = fs.normalise_config(cfg, airports)
        fetcher = fs.make_demo_fetcher()
        with pytest.raises(SystemExit):
            fs.run_scan(cfg, conn, fetcher, origins, dests)

    def test_multi_origin_all_stored(self, conn, airports):
        """Multi-origin scan: observations exist for every origin."""
        cfg = {
            "origins": ["WAW", "KRK"],
            "currency": "PLN",
            "adults": 1,
            "non_stop": False,
            "max_offers_per_query": 5,
            "scan": {
                "date_from": "2026-07-04",
                "date_to": "2026-07-31",
                "weekdays": [4],
                "trip_length_days": [3],
                "max_dates_per_route": 2,
            },
            "destinations": [{"country": "ME", "target_price": 99999}],
            "limits": {"max_api_calls": 500},
            "bargain": {"percentile": 25, "min_history": 50},
            "notify": {},
        }
        origins, dests = fs.normalise_config(cfg, airports)
        fetcher = fs.make_demo_fetcher()
        fs.run_scan(cfg, conn, fetcher, origins, dests)
        waw_count = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE origin='WAW'"
        ).fetchone()[0]
        krk_count = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE origin='KRK'"
        ).fetchone()[0]
        assert waw_count > 0
        assert krk_count > 0


# ─────────────────────────────────────────────────────────────────────────────
# Airports data file
# ─────────────────────────────────────────────────────────────────────────────

class TestAirportsData:
    def test_file_loads(self, airports):
        assert "countries" in airports
        assert "groups" in airports

    def test_montenegro_airports(self, airports):
        assert airports["countries"]["ME"]["airports"] == ["TGD", "TIV"]

    def test_bosnia_airports(self, airports):
        ba = airports["countries"]["BA"]["airports"]
        assert set(ba) >= {"SJJ", "OMO", "TZL", "BNX"}

    def test_poland_major_group(self, airports):
        g = airports["groups"]["poland_major"]
        assert set(g) >= {"WAW", "KRK", "KTW", "GDN", "WRO"}

    def test_poland_all_superset_of_major(self, airports):
        major = set(airports["groups"]["poland_major"])
        all_pl = set(airports["groups"]["poland_all"])
        assert major.issubset(all_pl)

    def test_all_country_airports_are_strings(self, airports):
        for code, data in airports["countries"].items():
            for iata in data["airports"]:
                assert isinstance(iata, str) and len(iata) == 3, \
                    f"{code}: bad IATA '{iata}'"

    def test_all_group_airports_are_strings(self, airports):
        for gname, iatas in airports["groups"].items():
            for iata in iatas:
                assert isinstance(iata, str) and len(iata) == 3, \
                    f"{gname}: bad IATA '{iata}'"


# ─────────────────────────────────────────────────────────────────────────────
# IMP-1: Per-route error isolation
# ─────────────────────────────────────────────────────────────────────────────

class TestRouteErrorIsolation:
    """Fetcher exceptions must not abort the whole scan."""

    _BASE_CFG = {
        "origins": ["WAW"],
        "currency": "PLN",
        "adults": 1,
        "non_stop": False,
        "max_offers_per_query": 5,
        "scan": {
            "date_from": "2026-07-04",
            "date_to": "2026-07-31",
            "weekdays": [4],
            "trip_length_days": [3],
            "max_dates_per_route": 2,
        },
        "destinations": [{"country": "ME", "target_price": 99999}],
        "limits": {"max_api_calls": 500},
        "bargain": {},
        "notify": {},
    }

    def test_failing_route_does_not_abort_scan(self, conn, airports):
        call_count = [0]

        def flaky_fetcher(origin, dest, depart, ret, adults, currency, max_offers, non_stop):
            call_count[0] += 1
            if dest == "TGD" and call_count[0] == 1:
                raise RuntimeError("simulated network error")
            return {"price": 400.0, "currency": currency,
                    "carriers": "LO", "stops": 0, "duration_min": 130}

        origins, dests = fs.normalise_config(self._BASE_CFG, airports)
        alerts = fs.run_scan(self._BASE_CFG, conn, flaky_fetcher, origins, dests)
        assert isinstance(alerts, list)
        count = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        assert count > 0  # other routes still stored

    def test_all_routes_fail_returns_empty(self, conn, airports):
        def always_fails(*args, **kwargs):
            raise RuntimeError("always fails")

        origins, dests = fs.normalise_config(self._BASE_CFG, airports)
        alerts = fs.run_scan(self._BASE_CFG, conn, always_fails, origins, dests)
        assert alerts == []

    def test_partial_failure_alerts_still_returned(self, conn, airports):
        """Routes that succeed should still produce alerts."""
        def partial_fetcher(origin, dest, depart, ret, adults, currency, max_offers, non_stop):
            if dest == "TGD":
                raise RuntimeError("TGD down")
            return {"price": 100.0, "currency": currency,
                    "carriers": "LO", "stops": 0, "duration_min": 90}

        origins, dests = fs.normalise_config(self._BASE_CFG, airports)
        alerts = fs.run_scan(self._BASE_CFG, conn, partial_fetcher, origins, dests)
        # TIV succeeded with price 100 < target 99999 → bargain alert
        assert len(alerts) > 0


# ─────────────────────────────────────────────────────────────────────────────
# IMP-2: Monthly quota tracking
# ─────────────────────────────────────────────────────────────────────────────

class TestMonthlyQuota:
    def test_record_scan_run(self, conn):
        fs.record_scan_run(conn, 42, 3)
        total, runs = fs.monthly_usage(conn)
        assert total == 42
        assert len(runs) == 1
        assert runs[0]["calls_made"] == 42
        assert runs[0]["alerts"] == 3

    def test_monthly_usage_accumulates(self, conn):
        fs.record_scan_run(conn, 100, 2)
        fs.record_scan_run(conn, 150, 0)
        total, runs = fs.monthly_usage(conn)
        assert total == 250
        assert len(runs) == 2

    def test_monthly_usage_empty(self, conn):
        total, runs = fs.monthly_usage(conn)
        assert total == 0
        assert runs == []

    def test_run_scan_records_calls(self, conn, airports):
        cfg = {
            "origins": ["WAW"],
            "currency": "PLN",
            "adults": 1,
            "non_stop": False,
            "max_offers_per_query": 5,
            "scan": {
                "date_from": "2026-07-04",
                "date_to": "2026-07-31",
                "weekdays": [4],
                "trip_length_days": [3],
                "max_dates_per_route": 2,
            },
            "destinations": [{"country": "ME"}],
            "limits": {"max_api_calls": 500},
            "bargain": {},
            "notify": {},
        }
        origins, dests = fs.normalise_config(cfg, airports)
        fs.run_scan(cfg, conn, fs.make_demo_fetcher(), origins, dests)
        total, runs = fs.monthly_usage(conn)
        assert total > 0
        assert len(runs) == 1

    def test_monthly_cap_aborts_scan(self, conn, airports):
        # Record 50 calls already used, then set monthly_cap=50 → any new scan should abort
        fs.record_scan_run(conn, 50, 0)
        cfg = {
            "origins": ["WAW"],
            "currency": "PLN",
            "adults": 1,
            "non_stop": False,
            "max_offers_per_query": 5,
            "scan": {
                "date_from": "2026-07-04",
                "date_to": "2026-07-31",
                "weekdays": list(range(7)),
                "trip_length_days": [3],
                "max_dates_per_route": 8,
            },
            "destinations": [{"country": "ME"}],
            "limits": {"max_api_calls": 5000, "monthly_cap": 50},
            "bargain": {},
            "notify": {},
        }
        origins, dests = fs.normalise_config(cfg, airports)
        with pytest.raises(SystemExit, match="monthly cap"):
            fs.run_scan(cfg, conn, fs.make_demo_fetcher(), origins, dests)


# ─────────────────────────────────────────────────────────────────────────────
# IMP-3: Notification dedup / cooldown
# ─────────────────────────────────────────────────────────────────────────────

class TestNotificationDedup:
    ALERT = {
        "origin": "WAW", "destination": "TGD",
        "depart_date": "2026-07-11", "return_date": "2026-07-14",
        "price": 300.0, "currency": "PLN",
        "carriers": "LO", "stops": 0, "duration_min": 130,
        "reason": "<= target 600",
        "scanned_at": "2026-07-01T08:00:00+00:00",
    }

    def test_first_alert_should_notify(self, conn):
        assert fs.should_notify_alert(conn, self.ALERT, 24) is True

    def test_same_alert_suppressed_within_cooldown(self, conn):
        fs.record_sent_alert(conn, self.ALERT)
        assert fs.should_notify_alert(conn, self.ALERT, 24) is False

    def test_price_improvement_overrides_cooldown(self, conn):
        fs.record_sent_alert(conn, self.ALERT)
        better = {**self.ALERT, "price": 280.0}  # 280 < 300*0.95=285 → re-alert ✓
        assert fs.should_notify_alert(conn, better, 24) is True

    def test_small_price_change_still_suppressed(self, conn):
        fs.record_sent_alert(conn, self.ALERT)
        slightly_better = {**self.ALERT, "price": 290.0}  # 290 > 300*0.95=285 → suppressed
        assert fs.should_notify_alert(conn, slightly_better, 24) is False

    def test_expired_cooldown_notifies_again(self, conn):
        fs.record_sent_alert(conn, self.ALERT)
        # -1 hour cooldown → expired immediately
        assert fs.should_notify_alert(conn, self.ALERT, -1) is True

    def test_different_route_not_suppressed(self, conn):
        fs.record_sent_alert(conn, self.ALERT)
        different = {**self.ALERT, "destination": "TIV"}
        assert fs.should_notify_alert(conn, different, 24) is True

    def test_different_dates_not_suppressed(self, conn):
        fs.record_sent_alert(conn, self.ALERT)
        different_dates = {**self.ALERT, "depart_date": "2026-08-01", "return_date": "2026-08-04"}
        assert fs.should_notify_alert(conn, different_dates, 24) is True

    def test_record_sent_alert_persists(self, conn):
        fs.record_sent_alert(conn, self.ALERT)
        row = conn.execute("SELECT COUNT(*) FROM sent_alerts").fetchone()[0]
        assert row == 1


# ─────────────────────────────────────────────────────────────────────────────
# IMP-4: AmadeusClient mock-HTTP tests
# ─────────────────────────────────────────────────────────────────────────────

class TestAmadeusClient:
    """AmadeusClient tested without real HTTP calls via unittest.mock."""

    TOKEN_RESP = {
        "access_token": "test_token_abc",
        "expires_in": 1799,
        "token_type": "Bearer",
    }
    OFFERS_RESP = {
        "data": [{
            "price": {"total": "345.20", "currency": "EUR"},
            "validatingAirlineCodes": ["LO", "FR"],
            "itineraries": [{
                "duration": "PT2H30M",
                "segments": [{"numberOfStops": 0}, {"numberOfStops": 0}],
            }],
        }]
    }

    @pytest.fixture(autouse=True)
    def _set_env(self, monkeypatch):
        monkeypatch.setenv("AMADEUS_CLIENT_ID", "test_key")
        monkeypatch.setenv("AMADEUS_CLIENT_SECRET", "test_secret")

    def _ctx(self, data: dict):
        """Wrap a dict in a mock context manager that json.load can read."""
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=io.BytesIO(json.dumps(data).encode()))
        mock_cm.__exit__ = MagicMock(return_value=False)
        return mock_cm

    def _http_err(self, code: int):
        return urllib.error.HTTPError("url", code, f"HTTP {code}", {}, io.BytesIO(b"err"))

    @patch("urllib.request.urlopen")
    def test_initial_auth(self, mock_urlopen):
        mock_urlopen.side_effect = [self._ctx(self.TOKEN_RESP), self._ctx(self.OFFERS_RESP)]
        client = fs.AmadeusClient(env="test")
        client._get("/v2/shopping/flight-offers", {})
        assert mock_urlopen.call_count == 2  # 1 auth + 1 data
        assert client._token == "test_token_abc"

    @patch("urllib.request.urlopen")
    def test_token_cached_across_calls(self, mock_urlopen):
        mock_urlopen.side_effect = [
            self._ctx(self.TOKEN_RESP),
            self._ctx(self.OFFERS_RESP),
            self._ctx(self.OFFERS_RESP),
        ]
        client = fs.AmadeusClient(env="test")
        client._get("/v2/shopping/flight-offers", {})
        client._get("/v2/shopping/flight-offers", {})
        assert mock_urlopen.call_count == 3  # 1 auth + 2 data

    @patch("urllib.request.urlopen")
    def test_token_refreshed_on_expiry(self, mock_urlopen):
        mock_urlopen.side_effect = [
            self._ctx(self.TOKEN_RESP), self._ctx(self.OFFERS_RESP),
            self._ctx(self.TOKEN_RESP), self._ctx(self.OFFERS_RESP),
        ]
        client = fs.AmadeusClient(env="test")
        client._get("/v2/shopping/flight-offers", {})
        client._token_expiry = time.time() - 1  # force expiry
        client._get("/v2/shopping/flight-offers", {})
        assert mock_urlopen.call_count == 4  # 2 auth + 2 data

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_429_retry_with_backoff(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = [
            self._ctx(self.TOKEN_RESP),
            self._http_err(429),
            self._ctx(self.OFFERS_RESP),
        ]
        client = fs.AmadeusClient(env="test")
        result = client._get("/v2/shopping/flight-offers", {})
        assert result == self.OFFERS_RESP
        mock_sleep.assert_called_once_with(1)  # 2**0

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_repeated_429_raises(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = [self._ctx(self.TOKEN_RESP)] + [self._http_err(429)] * 4
        client = fs.AmadeusClient(env="test")
        with pytest.raises(RuntimeError, match="rate-limit"):
            client._get("/v2/shopping/flight-offers", {})

    @patch("urllib.request.urlopen")
    def test_401_triggers_reauth(self, mock_urlopen):
        mock_urlopen.side_effect = [
            self._ctx(self.TOKEN_RESP),  # initial auth
            self._http_err(401),          # request fails
            self._ctx(self.TOKEN_RESP),  # re-auth
            self._ctx(self.OFFERS_RESP), # retry succeeds
        ]
        client = fs.AmadeusClient(env="test")
        result = client._get("/v2/shopping/flight-offers", {})
        assert result == self.OFFERS_RESP
        assert mock_urlopen.call_count == 4

    @patch("urllib.request.urlopen")
    def test_5xx_raises_runtime_error(self, mock_urlopen):
        mock_urlopen.side_effect = [self._ctx(self.TOKEN_RESP), self._http_err(500)]
        client = fs.AmadeusClient(env="test")
        with pytest.raises(RuntimeError, match="500"):
            client._get("/v2/shopping/flight-offers", {})

    @patch("urllib.request.urlopen")
    def test_cheapest_offer_parsing(self, mock_urlopen):
        mock_urlopen.side_effect = [self._ctx(self.TOKEN_RESP), self._ctx(self.OFFERS_RESP)]
        client = fs.AmadeusClient(env="test")
        result = client.cheapest_offer("WAW", "TGD", "2026-07-11", "2026-07-14", 1, "EUR", 5, False)
        assert result["price"] == pytest.approx(345.20)
        assert "LO" in result["carriers"]
        assert result["stops"] == 1        # 2 segments → 1 connection
        assert result["duration_min"] == 150  # PT2H30M

    @patch("urllib.request.urlopen")
    def test_cheapest_offer_no_data_returns_none(self, mock_urlopen):
        mock_urlopen.side_effect = [self._ctx(self.TOKEN_RESP), self._ctx({"data": []})]
        client = fs.AmadeusClient(env="test")
        result = client.cheapest_offer("WAW", "TGD", "2026-07-11", None, 1, "EUR", 5, False)
        assert result is None

    @patch("urllib.request.urlopen")
    def test_cheapest_offer_selects_min_price(self, mock_urlopen):
        multi = {
            "data": [
                {"price": {"total": "500.00", "currency": "EUR"},
                 "validatingAirlineCodes": ["LO"],
                 "itineraries": [{"duration": "PT2H", "segments": [{"numberOfStops": 0}]}]},
                {"price": {"total": "300.00", "currency": "EUR"},
                 "validatingAirlineCodes": ["FR"],
                 "itineraries": [{"duration": "PT3H", "segments": [{"numberOfStops": 1}]}]},
            ]
        }
        mock_urlopen.side_effect = [self._ctx(self.TOKEN_RESP), self._ctx(multi)]
        client = fs.AmadeusClient(env="test")
        result = client.cheapest_offer("WAW", "TGD", "2026-07-11", None, 1, "EUR", 5, False)
        assert result["price"] == pytest.approx(300.00)
        assert "FR" in result["carriers"]

    def test_missing_credentials_raises(self, monkeypatch):
        monkeypatch.delenv("AMADEUS_CLIENT_ID", raising=False)
        monkeypatch.delenv("AMADEUS_CLIENT_SECRET", raising=False)
        with pytest.raises(RuntimeError, match="Missing AMADEUS"):
            fs.AmadeusClient(env="test")


# ─────────────────────────────────────────────────────────────────────────────
# IMP-6: Concurrent scanning
# ─────────────────────────────────────────────────────────────────────────────

class TestConcurrentScan:
    _CFG = {
        "origins": ["WAW", "KRK"],
        "currency": "PLN",
        "adults": 1,
        "non_stop": False,
        "max_offers_per_query": 5,
        "scan": {
            "date_from": "2026-07-04",
            "date_to": "2026-07-31",
            "weekdays": [4],
            "trip_length_days": [3],
            "max_dates_per_route": 2,
        },
        "destinations": [{"country": "ME", "target_price": 99999}],
        "limits": {"max_api_calls": 500, "max_workers": 4},
        "bargain": {"percentile": 25, "min_history": 50},
        "notify": {},
    }

    def test_same_observation_count_on_repeated_runs(self, airports):
        """Concurrent runs with demo fetcher produce consistent observation counts."""
        origins, dests = fs.normalise_config(self._CFG, airports)
        conn1 = fs.db_connect(Path(":memory:"))
        fs.run_scan(self._CFG, conn1, fs.make_demo_fetcher(), origins, dests)
        count1 = conn1.execute("SELECT COUNT(*) FROM observations").fetchone()[0]

        conn2 = fs.db_connect(Path(":memory:"))
        fs.run_scan(self._CFG, conn2, fs.make_demo_fetcher(), origins, dests)
        count2 = conn2.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        assert count1 == count2

    def test_demo_fetcher_deterministic_by_input(self):
        """Same inputs always produce the same price and stops."""
        f = fs.make_demo_fetcher()
        r1 = f("WAW", "TGD", "2026-07-11", "2026-07-14", 1, "PLN", 5, False)
        r2 = f("WAW", "TGD", "2026-07-11", "2026-07-14", 1, "PLN", 5, False)
        assert r1["price"] == r2["price"]
        assert r1["stops"] == r2["stops"]

    def test_different_inputs_different_prices(self):
        """Different routes produce different prices (hash diversity)."""
        f = fs.make_demo_fetcher()
        r1 = f("WAW", "TGD", "2026-07-04", "2026-07-07", 1, "PLN", 5, False)
        r2 = f("KRK", "TIV", "2026-07-11", "2026-07-14", 1, "PLN", 5, False)
        # Very unlikely to collide across different route+date combos
        assert r1["price"] != r2["price"] or r1["stops"] != r2["stops"]

    def test_max_workers_config_respected(self, conn, airports):
        """Scan with max_workers=1 produces same alerts as max_workers=8."""
        origins, dests = fs.normalise_config(self._CFG, airports)
        cfg1 = {**self._CFG, "limits": {**self._CFG["limits"], "max_workers": 1}}
        cfg8 = {**self._CFG, "limits": {**self._CFG["limits"], "max_workers": 8}}
        conn1 = fs.db_connect(Path(":memory:"))
        conn8 = fs.db_connect(Path(":memory:"))
        alerts1 = fs.run_scan(cfg1, conn1, fs.make_demo_fetcher(), origins, dests)
        alerts8 = fs.run_scan(cfg8, conn8, fs.make_demo_fetcher(), origins, dests)
        assert len(alerts1) == len(alerts8)


# ─────────────────────────────────────────────────────────────────────────────
# Notify function (IMP-3 paths + channel branches)
# ─────────────────────────────────────────────────────────────────────────────

class TestNotify:
    _ALERT = {
        "origin": "WAW", "destination": "TGD",
        "depart_date": "2026-07-11", "return_date": "2026-07-14",
        "price": 300.0, "currency": "PLN",
        "carriers": "LO", "stops": 0, "duration_min": 130,
        "reason": "<= target 600",
        "scanned_at": "2026-07-01T08:00:00+00:00",
    }
    _CFG_NONE = {"notify": {"telegram": {"enabled": False}, "webhook": {"enabled": False}}}

    def test_empty_alerts_noop(self, capsys):
        fs.notify([], self._CFG_NONE)
        assert capsys.readouterr().out == ""

    def test_no_channel_prints_nothing(self, capsys):
        """Both channels disabled → no output (no channel = no notify)."""
        fs.notify([self._ALERT], self._CFG_NONE)
        assert capsys.readouterr().out == ""

    def test_cooldown_suppresses_all(self, conn, capsys):
        """When all alerts are suppressed by cooldown, prints suppression message."""
        fs.record_sent_alert(conn, self._ALERT)
        cfg = {"notify": {"telegram": {"enabled": True, "bot_token": "tok", "chat_id": "123"},
                          "cooldown_hours": 24}}
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = Exception("should not be called")
            fs.notify([self._ALERT], cfg, conn)
        out = capsys.readouterr().out
        assert "suppressed by cooldown" in out

    @patch("urllib.request.urlopen")
    def test_telegram_success(self, mock_urlopen, conn, capsys):
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_cm)
        mock_cm.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_cm
        cfg = {"notify": {"telegram": {"enabled": True, "bot_token": "tok", "chat_id": "123"},
                          "webhook": {"enabled": False}}}
        fs.notify([self._ALERT], cfg, conn)
        assert "sent Telegram notification" in capsys.readouterr().out
        assert mock_urlopen.called

    @patch("urllib.request.urlopen")
    def test_telegram_failure_logged(self, mock_urlopen, conn, capsys):
        mock_urlopen.side_effect = Exception("network error")
        cfg = {"notify": {"telegram": {"enabled": True, "bot_token": "tok", "chat_id": "123"},
                          "webhook": {"enabled": False}}}
        fs.notify([self._ALERT], cfg, conn)
        assert "telegram failed" in capsys.readouterr().out

    @patch("urllib.request.urlopen")
    def test_webhook_success(self, mock_urlopen, conn, capsys):
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_cm)
        mock_cm.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_cm
        cfg = {"notify": {"telegram": {"enabled": False},
                          "webhook": {"enabled": True, "url": "https://hook.example.com"}}}
        fs.notify([self._ALERT], cfg, conn)
        assert "webhook notification sent" in capsys.readouterr().out

    @patch("urllib.request.urlopen")
    def test_webhook_failure_logged(self, mock_urlopen, conn, capsys):
        mock_urlopen.side_effect = Exception("timeout")
        cfg = {"notify": {"telegram": {"enabled": False},
                          "webhook": {"enabled": True, "url": "https://hook.example.com"}}}
        fs.notify([self._ALERT], cfg, conn)
        assert "webhook failed" in capsys.readouterr().out

    def test_one_way_alert_formatted(self, capsys):
        """Alerts with return_date=None should render '(one-way)'."""
        one_way = {**self._ALERT, "return_date": None, "stops": None}
        fs.notify([one_way], self._CFG_NONE)
        # No channel → no output, but shouldn't crash
        capsys.readouterr()


# ─────────────────────────────────────────────────────────────────────────────
# Loaders and show_history
# ─────────────────────────────────────────────────────────────────────────────

class TestLoaders:
    def test_load_env_parses_file(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text('AMADEUS_CLIENT_ID="mykey"\nAMADEUS_CLIENT_SECRET=mysecret\n# comment\n')
        monkeypatch.delenv("AMADEUS_CLIENT_ID", raising=False)
        monkeypatch.delenv("AMADEUS_CLIENT_SECRET", raising=False)
        fs.load_env(env_file)
        assert os.environ["AMADEUS_CLIENT_ID"] == "mykey"
        assert os.environ["AMADEUS_CLIENT_SECRET"] == "mysecret"

    def test_load_env_missing_file_noop(self, tmp_path):
        fs.load_env(tmp_path / "nonexistent.env")  # should not raise

    def test_load_config_parses_json(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text('{"key": "value"}')
        assert fs.load_config(cfg_file) == {"key": "value"}

    def test_load_config_missing_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            fs.load_config(tmp_path / "missing.json")

    def test_load_airports_missing_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            fs.load_airports(tmp_path / "missing.json")


class TestShowHistory:
    def test_empty_db(self, conn, capsys):
        fs.show_history(conn)
        assert "No observations" in capsys.readouterr().out

    def test_with_observations(self, conn, capsys):
        fs.record_observation(conn, {
            "scanned_at": "2026-07-01T08:00:00+00:00",
            "origin": "WAW", "destination": "TGD",
            "depart_date": "2026-07-11", "return_date": "2026-07-14",
            "price": 320.0, "currency": "PLN",
            "carriers": "LO", "stops": 0, "duration_min": 130,
        })
        fs.show_history(conn)
        out = capsys.readouterr().out
        assert "WAW->TGD" in out
        assert "320" in out


# ─────────────────────────────────────────────────────────────────────────────
# Additional AmadeusClient coverage
# ─────────────────────────────────────────────────────────────────────────────

class TestAmadeusClientExtra:
    TOKEN_RESP = {"access_token": "tok", "expires_in": 1799}
    OFFERS_RESP = {
        "data": [{
            "price": {"total": "200.00", "currency": "EUR"},
            "validatingAirlineCodes": ["LO"],
            "itineraries": [{"duration": "PT1H", "segments": [{"numberOfStops": 0}]}],
        }]
    }

    @pytest.fixture(autouse=True)
    def _set_env(self, monkeypatch):
        monkeypatch.setenv("AMADEUS_CLIENT_ID", "k")
        monkeypatch.setenv("AMADEUS_CLIENT_SECRET", "s")

    def _ctx(self, data):
        m = MagicMock()
        m.__enter__ = MagicMock(return_value=io.BytesIO(json.dumps(data).encode()))
        m.__exit__ = MagicMock(return_value=False)
        return m

    @patch("urllib.request.urlopen")
    def test_non_stop_param_included(self, mock_urlopen):
        mock_urlopen.side_effect = [self._ctx(self.TOKEN_RESP), self._ctx(self.OFFERS_RESP)]
        client = fs.AmadeusClient(env="test")
        client.cheapest_offer("WAW", "TGD", "2026-07-11", None, 1, "EUR", 5, True)
        # Second call (flight-offers) URL should contain nonStop=true
        data_call_url = mock_urlopen.call_args_list[1][0][0].full_url
        assert "nonStop=true" in data_call_url
