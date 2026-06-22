#!/usr/bin/env python3
"""Flight deals scanner — multi-origin, country-level destinations.

Scans specific or country-level destinations from one or more origins (or all
Polish airports), finds the cheapest fares via the Amadeus API, and alerts on
bargains (below target price, below historical percentile, or sharp price drop).

Run:
    python flight_scanner.py            # real scan (needs Amadeus credentials)
    python flight_scanner.py --demo     # offline demo with synthetic prices
    python flight_scanner.py --history  # stored price-history summary
    python flight_scanner.py --estimate # show API call count estimate and exit

Config schema supports:
    origins: single IATA / country code / list of IATAs / {group: "name"} / {country: "PL"}
    destinations: [{country: "ME"}, {destination: "BCN"}, ...]
    Legacy single-origin format (origin + routes) still works.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT        = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
AIRPORTS_PATH = ROOT / "data" / "airports.json"
ENV_PATH    = ROOT / ".env"
DB_PATH     = ROOT / "prices.db"

AMADEUS_HOSTS = {
    "test": "https://test.api.amadeus.com",
    "production": "https://api.amadeus.com",
}


# --------------------------------------------------------------------------- #
# Env / config
# --------------------------------------------------------------------------- #
def load_env(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config(path: Path = CONFIG_PATH) -> dict:
    if not path.exists():
        sys.exit(f"Config not found: {path}. Copy config.json and edit it.")
    with path.open() as fh:
        return json.load(fh)


def load_airports(path: Path = AIRPORTS_PATH) -> dict:
    if not path.exists():
        sys.exit(f"Airport data not found: {path}")
    with path.open() as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- #
# Resolver: country/group/IATA -> airport lists  (FLIGHT-1)
# --------------------------------------------------------------------------- #
def resolve_origins(origins_cfg, airports: dict) -> list[str]:
    """Expand origins config entry into a flat list of IATA codes."""
    if isinstance(origins_cfg, list):
        # list of IATA codes
        return [o.upper() for o in origins_cfg]
    if isinstance(origins_cfg, dict):
        if "group" in origins_cfg:
            key = origins_cfg["group"]
            if key not in airports.get("groups", {}):
                sys.exit(f"Unknown group '{key}'. Available: {list(airports['groups'].keys())}")
            return airports["groups"][key]
        if "country" in origins_cfg:
            code = origins_cfg["country"].upper()
            if code not in airports.get("countries", {}):
                sys.exit(f"Unknown country '{code}'. Available: {list(airports['countries'].keys())}")
            return airports["countries"][code]["airports"]
    if isinstance(origins_cfg, str):
        code = origins_cfg.upper()
        if code in airports.get("countries", {}):
            return airports["countries"][code]["airports"]
        return [code]  # direct IATA
    return [str(origins_cfg).upper()]


def resolve_destinations(destinations_cfg: list, airports: dict) -> list[dict]:
    """
    Expand destinations config list into resolved entries:
    Each entry: {key, label, airports: [IATA,...], target_price}
    """
    resolved = []
    for dest in destinations_cfg:
        if "country" in dest:
            code = dest["country"].upper()
            if code not in airports.get("countries", {}):
                sys.exit(f"Unknown destination country '{code}'.")
            country_data = airports["countries"][code]
            resolved.append({
                "key": code,
                "label": dest.get("label", country_data["name"]),
                "airports": country_data["airports"],
                "target_price": dest.get("target_price"),
            })
        elif "destination" in dest:
            iata = dest["destination"].upper()
            resolved.append({
                "key": iata,
                "label": dest.get("label", iata),
                "airports": [iata],
                "target_price": dest.get("target_price"),
            })
        elif "group" in dest:
            key = dest["group"]
            if key not in airports.get("groups", {}):
                sys.exit(f"Unknown destination group '{key}'.")
            resolved.append({
                "key": key,
                "label": dest.get("label", key),
                "airports": airports["groups"][key],
                "target_price": dest.get("target_price"),
            })
    return resolved


def normalise_config(cfg: dict, airports: dict) -> tuple[list[str], list[dict]]:
    """
    Support both legacy (origin + routes) and new (origins + destinations) formats.
    Returns (origins_list, resolved_destinations).
    """
    # --- Origins ---
    if "origins" in cfg:
        origins = resolve_origins(cfg["origins"], airports)
    elif "origin" in cfg:
        origins = resolve_origins(cfg["origin"], airports)
    else:
        sys.exit("Config must define 'origins' or 'origin'.")

    # --- Destinations ---
    if "destinations" in cfg:
        destinations = resolve_destinations(cfg["destinations"], airports)
    elif "routes" in cfg:
        # legacy format: [{destination: IATA, label, target_price}]
        destinations = resolve_destinations(cfg["routes"], airports)
    else:
        sys.exit("Config must define 'destinations' or 'routes'.")

    return origins, destinations


# --------------------------------------------------------------------------- #
# Storage  (extended for stops + duration: FLIGHT-3)
# --------------------------------------------------------------------------- #
def db_connect(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS observations (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            scanned_at    TEXT NOT NULL,
            origin        TEXT NOT NULL,
            destination   TEXT NOT NULL,
            depart_date   TEXT NOT NULL,
            return_date   TEXT,
            price         REAL NOT NULL,
            currency      TEXT NOT NULL,
            carriers      TEXT,
            stops         INTEGER,
            duration_min  INTEGER
        )
        """
    )
    # Migrate existing DBs that lack the new columns
    existing = {row[1] for row in conn.execute("PRAGMA table_info(observations)")}
    for col, typedef in [("stops", "INTEGER"), ("duration_min", "INTEGER")]:
        if col not in existing:
            conn.execute(f"ALTER TABLE observations ADD COLUMN {col} {typedef}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_route ON observations "
        "(origin, destination, depart_date, return_date)"
    )
    # IMP-2: monthly quota tracking
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scan_runs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at     TEXT NOT NULL,
            calls_made INTEGER NOT NULL DEFAULT 0,
            alerts     INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # IMP-3: notification dedup / cooldown
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sent_alerts (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_at        TEXT NOT NULL,
            origin         TEXT NOT NULL,
            destination    TEXT NOT NULL,
            depart_date    TEXT NOT NULL,
            return_date    TEXT,
            price_bucket   INTEGER NOT NULL,
            notified_price REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sent ON sent_alerts "
        "(origin, destination, depart_date, return_date, sent_at)"
    )
    conn.commit()
    return conn


def record_observation(conn: sqlite3.Connection, obs: dict) -> None:
    conn.execute(
        """INSERT INTO observations
           (scanned_at, origin, destination, depart_date, return_date,
            price, currency, carriers, stops, duration_min)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            obs["scanned_at"], obs["origin"], obs["destination"],
            obs["depart_date"], obs.get("return_date"), obs["price"],
            obs["currency"], obs.get("carriers", ""),
            obs.get("stops"), obs.get("duration_min"),
        ),
    )
    conn.commit()


def route_price_history(conn, origin, destination, depart_date, return_date) -> list[float]:
    cur = conn.execute(
        """SELECT price FROM observations
           WHERE origin=? AND destination=? AND depart_date=?
             AND IFNULL(return_date,'')=IFNULL(?, '')
           ORDER BY scanned_at""",
        (origin, destination, depart_date, return_date),
    )
    return [row[0] for row in cur.fetchall()]


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


# --------------------------------------------------------------------------- #
# IMP-2: Monthly quota tracking
# --------------------------------------------------------------------------- #
def record_scan_run(conn: sqlite3.Connection, calls_made: int, alerts: int) -> None:
    conn.execute(
        "INSERT INTO scan_runs (run_at, calls_made, alerts) VALUES (?,?,?)",
        (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), calls_made, alerts),
    )
    conn.commit()


def monthly_usage(conn: sqlite3.Connection) -> tuple[int, list[dict]]:
    """Return (total_calls_this_month, list_of_run_dicts)."""
    first_of_month = dt.date.today().replace(day=1).isoformat()
    cur = conn.execute(
        "SELECT run_at, calls_made, alerts FROM scan_runs WHERE run_at >= ? ORDER BY run_at",
        (first_of_month,),
    )
    rows = [{"run_at": r[0], "calls_made": r[1], "alerts": r[2]} for r in cur.fetchall()]
    return sum(r["calls_made"] for r in rows), rows


# --------------------------------------------------------------------------- #
# IMP-3: Notification dedup / cooldown
# --------------------------------------------------------------------------- #
def should_notify_alert(
    conn: sqlite3.Connection, alert: dict, cooldown_hours: float
) -> bool:
    """Return True if this alert should trigger a notification.

    Suppressed within cooldown_hours of a previous notification for the same
    route+dates, unless price improved by > 5 %.
    """
    cutoff = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=cooldown_hours)
    ).isoformat(timespec="seconds")
    row = conn.execute(
        """SELECT notified_price FROM sent_alerts
           WHERE origin=? AND destination=? AND depart_date=?
             AND IFNULL(return_date,'') = IFNULL(?, '')
             AND sent_at > ?
           ORDER BY sent_at DESC LIMIT 1""",
        (
            alert["origin"], alert["destination"], alert["depart_date"],
            alert.get("return_date"), cutoff,
        ),
    ).fetchone()
    if row is None:
        return True  # no recent notification → send
    # Re-alert if price dropped > 5 % from the last notified price
    return float(alert["price"]) < row[0] * 0.95


def record_sent_alert(conn: sqlite3.Connection, alert: dict) -> None:
    bucket = int(alert["price"] / 50) * 50
    conn.execute(
        """INSERT INTO sent_alerts
           (sent_at, origin, destination, depart_date, return_date,
            price_bucket, notified_price)
           VALUES (?,?,?,?,?,?,?)""",
        (
            dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            alert["origin"], alert["destination"], alert["depart_date"],
            alert.get("return_date"), bucket, alert["price"],
        ),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# Amadeus API client  (FLIGHT-3: stops + duration)
# --------------------------------------------------------------------------- #
def _parse_iso_duration(s: str) -> int:
    """Convert ISO 8601 duration string like PT2H30M to total minutes."""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", s or "")
    if not m:
        return 0
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


class AmadeusClient:
    def __init__(self, env: str = "test"):
        self.base = AMADEUS_HOSTS.get(env, AMADEUS_HOSTS["test"])
        self.key = os.environ.get("AMADEUS_CLIENT_ID")
        self.secret = os.environ.get("AMADEUS_CLIENT_SECRET")
        self._token: str | None = None
        self._token_expiry = 0.0
        self._lock = threading.Lock()  # IMP-6: thread-safe token refresh
        if not self.key or not self.secret:
            raise RuntimeError(
                "Missing AMADEUS_CLIENT_ID / AMADEUS_CLIENT_SECRET. "
                "Add them to .env (see .env.example) or run with --demo."
            )

    def _token_valid(self) -> bool:
        return self._token is not None and time.time() < self._token_expiry - 30

    def _authenticate(self) -> None:
        with self._lock:
            if self._token_valid():  # double-check: another thread may have refreshed
                return
            data = urllib.parse.urlencode(
                {"grant_type": "client_credentials",
                 "client_id": self.key,
                 "client_secret": self.secret}
            ).encode()
            req = urllib.request.Request(
                f"{self.base}/v1/security/oauth2/token", data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.load(resp)
            self._token = payload["access_token"]
            self._token_expiry = time.time() + payload.get("expires_in", 1799)

    def _get(self, path: str, params: dict) -> dict:
        if not self._token_valid():
            self._authenticate()
        url = f"{self.base}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._token}"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=40) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(2 ** attempt)
                    continue
                if e.code == 401:
                    self._token = None  # invalidate so _authenticate proceeds
                    self._authenticate()
                    req.add_unredirected_header("Authorization", f"Bearer {self._token}")
                    continue
                body = e.read().decode(errors="replace")
                raise RuntimeError(f"Amadeus {e.code} on {path}: {body[:300]}") from e
        raise RuntimeError(f"Amadeus repeated rate-limit on {path}")

    def cheapest_offer(
        self, origin, destination, depart_date, return_date,
        adults, currency, max_offers, non_stop,
    ) -> dict | None:
        params = {
            "originLocationCode": origin,
            "destinationLocationCode": destination,
            "departureDate": depart_date,
            "adults": adults,
            "currencyCode": currency,
            "max": max_offers,
        }
        if return_date:
            params["returnDate"] = return_date
        if non_stop:
            params["nonStop"] = "true"
        data = self._get("/v2/shopping/flight-offers", params).get("data", [])
        if not data:
            return None
        best = min(data, key=lambda o: float(o["price"]["total"]))
        carriers = ",".join(sorted(set(best.get("validatingAirlineCodes", []))))

        # Outbound itinerary stops + duration  (FLIGHT-3)
        outbound = best.get("itineraries", [{}])[0]
        segments = outbound.get("segments", [])
        stops = sum(s.get("numberOfStops", 0) for s in segments) + max(len(segments) - 1, 0)
        duration_min = _parse_iso_duration(outbound.get("duration", ""))

        return {
            "price": float(best["price"]["total"]),
            "currency": best["price"].get("currency", currency),
            "carriers": carriers,
            "stops": stops,
            "duration_min": duration_min,
        }


# --------------------------------------------------------------------------- #
# Date generation
# --------------------------------------------------------------------------- #
def generate_date_pairs(scan: dict) -> list[tuple[str, str | None]]:
    date_from = dt.date.fromisoformat(scan["date_from"])
    date_to   = dt.date.fromisoformat(scan["date_to"])
    weekdays  = set(scan.get("weekdays") or range(7))
    trip_lengths = scan.get("trip_length_days") or [None]
    cap = scan.get("max_dates_per_route", 8)

    pairs: list[tuple[str, str | None]] = []
    day = date_from
    while day <= date_to and len(pairs) < cap:
        if day.weekday() in weekdays:
            for length in trip_lengths:
                if length is None:
                    pairs.append((day.isoformat(), None))
                else:
                    ret = day + dt.timedelta(days=int(length))
                    if ret <= date_to + dt.timedelta(days=7):
                        pairs.append((day.isoformat(), ret.isoformat()))
                if len(pairs) >= cap:
                    break
        day += dt.timedelta(days=1)
    return pairs


# --------------------------------------------------------------------------- #
# API-budget guardrails  (FLIGHT-6)
# --------------------------------------------------------------------------- #
def estimate_calls(origins: list[str], destinations: list[dict], date_pairs: list) -> int:
    total_dest_airports = sum(len(d["airports"]) for d in destinations)
    return len(origins) * total_dest_airports * len(date_pairs)


def check_budget(estimated: int, limits: dict) -> None:
    """Print estimate; exit if over budget cap."""
    cap = limits.get("max_api_calls", 500)
    print(f"  Estimated API calls: {estimated}  (cap: {cap})")
    if estimated > cap:
        sys.exit(
            f"\nAborted: {estimated} estimated API calls exceeds the cap of {cap}.\n"
            f"Reduce origins, destinations, max_dates_per_route, or raise limits.max_api_calls."
        )


# --------------------------------------------------------------------------- #
# Bargain detection  (FLIGHT-5: target applies per resolved destination)
# --------------------------------------------------------------------------- #
def evaluate_bargain(price, target_price, history, rules) -> tuple[bool, str]:
    reasons = []
    if target_price is not None and price <= target_price:
        reasons.append(f"<= target {target_price:.0f}")
    prior = history[:-1] if history else []
    if len(prior) >= rules.get("min_history", 5):
        thresh = percentile(prior, rules.get("percentile", 25))
        if thresh is not None and price <= thresh:
            reasons.append(f"<= p{rules.get('percentile', 25):.0f} ({thresh:.0f})")
        prev_min = min(prior)
        drop_pct = (prev_min - price) / prev_min * 100 if prev_min else 0
        if drop_pct >= rules.get("drop_pct_alert", 15):
            reasons.append(f"-{drop_pct:.0f}% vs prev low")
    return bool(reasons), "; ".join(reasons)


# --------------------------------------------------------------------------- #
# Notification
# --------------------------------------------------------------------------- #
def notify(alerts: list[dict], cfg: dict, conn: sqlite3.Connection | None = None) -> None:
    if not alerts:
        return

    tg   = cfg.get("notify", {}).get("telegram", {})
    hook = cfg.get("notify", {}).get("webhook", {})
    has_channel = tg.get("enabled") or (hook.get("enabled") and hook.get("url"))

    # IMP-3: cooldown dedup — only filter when a real channel is configured
    if has_channel and conn is not None:
        cooldown = cfg.get("notify", {}).get("cooldown_hours", 24)
        to_notify = [a for a in alerts if should_notify_alert(conn, a, cooldown)]
        if not to_notify:
            print("  (all alerts suppressed by cooldown — no new notifications)")
            return
    else:
        to_notify = alerts

    lines = ["✈️  BARGAIN ALERTS", "=" * 70]
    for a in to_notify:
        route = f"{a['origin']}->{a['destination']}"
        dates = a["depart_date"] + (f"..{a['return_date']}" if a["return_date"] else " (one-way)")
        stops_str = ("direct" if a.get("stops") == 0
                     else f"{a.get('stops')} stop(s)" if a.get("stops") is not None else "")
        lines.append(
            f"{route:12} {dates:22} {a['price']:>8.0f} {a['currency']}"
            f"  {stops_str:10} [{a['reason']}]"
        )
    message = "\n".join(lines)

    if tg.get("enabled"):
        try:
            _post_telegram(tg, message)
            print("  (sent Telegram notification)")
            if conn is not None:
                for a in to_notify:
                    record_sent_alert(conn, a)
        except Exception as e:
            print(f"  (telegram failed: {e})")

    if hook.get("enabled") and hook.get("url"):
        try:
            _post_webhook(hook["url"], {"text": message, "alerts": to_notify})
            print("  (webhook notification sent)")
            if conn is not None:
                for a in to_notify:
                    record_sent_alert(conn, a)
        except Exception as e:
            print(f"  (webhook failed: {e})")


def _post_telegram(tg: dict, text: str) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", tg.get("bot_token", ""))
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", tg.get("chat_id", ""))
    if not token or not chat_id:
        raise RuntimeError("missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=20):
        pass


def _post_webhook(url: str, payload: dict) -> None:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20):
        pass


# --------------------------------------------------------------------------- #
# Scan  (IMP-1: error isolation; IMP-2: quota tracking; IMP-6: concurrent)
# --------------------------------------------------------------------------- #
def run_scan(cfg: dict, conn, fetcher, origins: list[str], destinations: list[dict]) -> list[dict]:
    currency    = cfg.get("currency", "EUR")
    adults      = cfg.get("adults", 1)
    non_stop    = cfg.get("non_stop", False)
    max_offers  = cfg.get("max_offers_per_query", 5)
    rules       = cfg.get("bargain", {})
    limits      = cfg.get("limits", {})
    max_workers = limits.get("max_workers", 4)
    date_pairs  = generate_date_pairs(cfg["scan"])
    now         = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    total_dest = sum(len(d["airports"]) for d in destinations)
    estimated  = estimate_calls(origins, destinations, date_pairs)
    print(f"Scanning {len(origins)} origin(s) × {total_dest}"
          f" dest airport(s) × {len(date_pairs)} date(s)")

    # IMP-2: monthly quota check
    if "monthly_cap" in limits:
        month_used, _ = monthly_usage(conn)
        remaining = limits["monthly_cap"] - month_used
        print(f"  Monthly usage: {month_used}/{limits['monthly_cap']} used  ({remaining} remaining)")
        if month_used + estimated > limits["monthly_cap"]:
            sys.exit(
                f"\nAborted: {month_used} used + {estimated} estimated = "
                f"{month_used + estimated} > monthly cap {limits['monthly_cap']}.\n"
                "Reduce scope, wait until next month, or raise limits.monthly_cap."
            )

    check_budget(estimated, limits)
    print()

    # Per-group accumulators (all written from the main thread via as_completed)
    group_best_by_pair: list[dict] = [{} for _ in destinations]
    group_alerts: list[list[dict]] = [[] for _ in destinations]
    route_errors: list[str] = []
    calls_made = 0

    # IMP-6: flat task list for concurrent execution
    tasks = [
        (g_idx, origin, dest_iata, depart, ret)
        for g_idx, dest_group in enumerate(destinations)
        for origin in origins
        for dest_iata in dest_group["airports"]
        for depart, ret in date_pairs
    ]

    def _fetch(g_idx, origin, dest_iata, depart, ret):
        """Worker: call fetcher. IMP-1: catches all exceptions."""
        try:
            offer = fetcher(origin, dest_iata, depart, ret, adults, currency, max_offers, non_stop)
            return g_idx, origin, dest_iata, depart, ret, offer, None
        except Exception as exc:
            return g_idx, origin, dest_iata, depart, ret, None, str(exc)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_fetch, *t) for t in tasks]
        for future in as_completed(futures):
            g_idx, origin, dest_iata, depart, ret, offer, err = future.result()
            calls_made += 1  # as_completed yields in the main thread — no lock needed

            if err:
                route_errors.append(f"{origin}→{dest_iata} {depart}: {err}")
                continue
            if not offer:
                continue

            target = destinations[g_idx].get("target_price")
            obs = {
                "scanned_at":   now,
                "origin":       origin,
                "destination":  dest_iata,
                "depart_date":  depart,
                "return_date":  ret,
                "price":        offer["price"],
                "currency":     offer["currency"],
                "carriers":     offer.get("carriers", ""),
                "stops":        offer.get("stops"),
                "duration_min": offer.get("duration_min"),
            }
            # DB writes are in the main thread — no lock needed
            record_observation(conn, obs)
            history = route_price_history(conn, origin, dest_iata, depart, ret)
            is_bargain, reason = evaluate_bargain(offer["price"], target, history, rules)
            if is_bargain:
                group_alerts[g_idx].append({**obs, "reason": reason})
            pair = (origin, dest_iata)
            prev = group_best_by_pair[g_idx].get(pair)
            if prev is None or offer["price"] < prev["price"]:
                group_best_by_pair[g_idx][pair] = obs

    # IMP-1: surface route errors
    if route_errors:
        print(f"  ⚠️  {len(route_errors)} route(s) failed:")
        for msg in route_errors[:10]:
            print(f"    {msg}")
        if len(route_errors) > 10:
            print(f"    ... and {len(route_errors) - 10} more")
        print()

    # Aggregate and print per destination group
    all_alerts: list[dict] = []
    for g_idx, dest_group in enumerate(destinations):
        label        = dest_group["label"]
        best_by_pair = group_best_by_pair[g_idx]
        alerts       = group_alerts[g_idx]
        all_alerts.extend(alerts)

        if not best_by_pair:
            print(f"  {label}: no offers found")
            continue

        ranked = sorted(best_by_pair.values(), key=lambda o: o["price"])
        best   = ranked[0]
        stops_str = ("direct"
                     if best.get("stops") == 0
                     else f"{best['stops']} stop(s)"
                     if best.get("stops") is not None
                     else "?")
        dur  = (f" {best['duration_min']//60}h{best['duration_min']%60:02d}m"
                if best.get("duration_min") else "")
        fire = " 🔥" if alerts else ""
        print(f"  {'['+label+']':22}  best: {best['origin']}→{best['destination']}"
              f"  {best['price']:>8.0f} {best['currency']}"
              f"  {stops_str}{dur}"
              f"  {best['depart_date']}{('..'+best['return_date']) if best['return_date'] else ''}"
              f"{fire}")

        if len(origins) > 1:
            seen  = set()
            count = 0
            for obs in ranked:
                key = obs["origin"]
                if key in seen or key == best["origin"]:
                    continue
                seen.add(key)
                s = ("direct" if obs.get("stops") == 0
                     else f"{obs['stops']} stop(s)" if obs.get("stops") is not None else "?")
                print(f"  {'':22}  alt:  {obs['origin']}→{obs['destination']}"
                      f"  {obs['price']:>8.0f} {obs['currency']}  {s}"
                      f"  {obs['depart_date']}{('..'+obs['return_date']) if obs['return_date'] else ''}")
                count += 1
                if count >= 3:
                    break

    print()
    # IMP-2: record this run for monthly quota tracking
    record_scan_run(conn, calls_made, len(all_alerts))
    return all_alerts


# --------------------------------------------------------------------------- #
# Demo fetcher  (per-call deterministic seed — thread-safe; IMP-6)
# --------------------------------------------------------------------------- #
def make_demo_fetcher():
    import random

    BASE_PRICES = {
        "TGD": 480, "TIV": 460,  # Montenegro
        "SJJ": 520, "OMO": 580, "TZL": 590, "BNX": 600,  # Bosnia
        "ZAG": 350, "SPU": 420, "DBV": 500,  # Croatia
        "BCN": 380, "FCO": 340, "CDG": 300, "LIS": 450, "ATH": 420,
        "BEG": 280, "SKP": 320,
    }
    ORIGIN_FACTOR = {
        "WAW": 1.0, "WMI": 0.95, "KRK": 0.92, "KTW": 0.90,
        "GDN": 1.05, "WRO": 0.98,
    }

    def fetcher(origin, dest, depart, ret, adults, currency, max_offers, non_stop):
        # Per-call deterministic seed: same inputs → same result, thread-safe
        seed_str = f"{origin}|{dest}|{depart}|{ret}"
        seed = int(hashlib.md5(seed_str.encode()).hexdigest()[:8], 16)
        rnd = random.Random(seed)

        base     = BASE_PRICES.get(dest, 400)
        seasonal = 80 if depart[5:7] in ("07", "08") else 0
        factor   = ORIGIN_FACTOR.get(origin, 1.0)
        price    = (base + seasonal + rnd.randint(-100, 130)) * factor
        stops    = rnd.choice([0, 0, 1])
        dur      = 90 + rnd.randint(0, 120) + stops * 90
        return {
            "price":        float(max(round(price, 2), 90)),
            "currency":     currency,
            "carriers":     rnd.choice(["FR", "W6", "LO", "KL", "U2", "RYR"]),
            "stops":        stops,
            "duration_min": dur,
        }

    return fetcher


# --------------------------------------------------------------------------- #
# History view
# --------------------------------------------------------------------------- #
def show_history(conn) -> None:
    cur = conn.execute(
        """SELECT origin, destination, COUNT(*), MIN(price), AVG(price), MAX(price), currency
           FROM observations GROUP BY origin, destination, currency
           ORDER BY destination, origin"""
    )
    rows = cur.fetchall()
    if not rows:
        print("No observations stored yet. Run a scan first.")
        return
    print(f"{'ROUTE':14} {'N':>4} {'MIN':>8} {'AVG':>8} {'MAX':>8}  CUR")
    print("-" * 52)
    for o, d, n, mn, avg, mx, cur_ in rows:
        print(f"{o+'->'+d:14} {n:>4} {mn:>8.0f} {avg:>8.0f} {mx:>8.0f}  {cur_}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description="Scan routes/countries for flight bargains.")
    parser.add_argument("--demo",     action="store_true",
                        help="Offline demo with synthetic prices (no API key needed).")
    parser.add_argument("--history",  action="store_true",
                        help="Show stored price-history summary and exit.")
    parser.add_argument("--estimate", action="store_true",
                        help="Print estimated API call count and exit (no scan).")
    parser.add_argument("--quota",    action="store_true",
                        help="Show month-to-date API call usage and exit.")  # IMP-2
    parser.add_argument("--config",   default=str(CONFIG_PATH))
    args = parser.parse_args()

    load_env()
    conn     = db_connect()
    airports = load_airports()

    if args.history:
        show_history(conn)
        return

    # IMP-2: --quota shows month-to-date usage without needing a config
    if args.quota:
        total, runs = monthly_usage(conn)
        print(f"Month-to-date API calls: {total}  (Amadeus free tier: ~2000/month)")
        if runs:
            print(f"\nRecent runs this month ({len(runs)} total):")
            for r in runs[-15:]:
                print(f"  {r['run_at']}  calls: {r['calls_made']:>4}  alerts: {r['alerts']}")
        else:
            print("\nNo scan runs recorded this month.")
        return

    cfg = load_config(Path(args.config))
    origins, destinations = normalise_config(cfg, airports)

    if args.estimate:
        date_pairs = generate_date_pairs(cfg["scan"])
        est = estimate_calls(origins, destinations, date_pairs)
        total_dest = sum(len(d["airports"]) for d in destinations)
        cap = cfg.get("limits", {}).get("max_api_calls", 500)
        print(f"Origins:            {len(origins)}  {origins[:6]}{'...' if len(origins)>6 else ''}")
        print(f"Destination groups: {len(destinations)}")
        print(f"Dest airports:      {total_dest}  "
              f"{[a for d in destinations for a in d['airports']][:8]}...")
        print(f"Date pairs:         {len(date_pairs)}")
        print(f"Estimated calls:    {est}  (cap: {cap})")
        if est > cap:
            print("⚠️  Over cap — scan would be aborted. Reduce scope or raise limits.max_api_calls.")
        else:
            print("✅ Within cap.")
        return

    if args.demo:
        print("== DEMO MODE (synthetic prices, no API calls) ==\n")
        fetcher = make_demo_fetcher()
    else:
        env = cfg.get("amadeus_env", "test")
        try:
            client = AmadeusClient(env=env)
        except RuntimeError as e:
            sys.exit(f"Error: {e}")
        fetcher = client.cheapest_offer

    alerts = run_scan(cfg, conn, fetcher, origins, destinations)
    if alerts:
        notify(alerts, cfg, conn)  # IMP-3: pass conn for cooldown dedup
    else:
        print("No bargains this run (prices stored to history for future comparison).")


if __name__ == "__main__":
    main()
