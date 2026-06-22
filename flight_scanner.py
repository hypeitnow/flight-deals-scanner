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
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
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
        if not self.key or not self.secret:
            raise RuntimeError(
                "Missing AMADEUS_CLIENT_ID / AMADEUS_CLIENT_SECRET. "
                "Add them to .env (see .env.example) or run with --demo."
            )

    def _token_valid(self) -> bool:
        return self._token is not None and time.time() < self._token_expiry - 30

    def _authenticate(self) -> None:
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
def notify(alerts: list[dict], cfg: dict) -> None:
    if not alerts:
        return
    lines = ["✈️  BARGAIN ALERTS", "=" * 70]
    for a in alerts:
        route = f"{a['origin']}->{a['destination']}"
        dates = a["depart_date"] + (f"..{a['return_date']}" if a["return_date"] else " (one-way)")
        stops_str = ("direct" if a.get("stops") == 0
                     else f"{a.get('stops')} stop(s)" if a.get("stops") is not None else "")
        lines.append(
            f"{route:12} {dates:22} {a['price']:>8.0f} {a['currency']}"
            f"  {stops_str:10} [{a['reason']}]"
        )
    message = "\n".join(lines)

    tg = cfg.get("notify", {}).get("telegram", {})
    if tg.get("enabled"):
        try:
            _post_telegram(tg, message)
            print("  (sent Telegram notification)")
        except Exception as e:
            print(f"  (telegram failed: {e})")

    hook = cfg.get("notify", {}).get("webhook", {})
    if hook.get("enabled") and hook.get("url"):
        try:
            _post_webhook(hook["url"], {"text": message, "alerts": alerts})
            print("  (webhook notification sent)")
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
# Scan  (FLIGHT-2: multi-origin; FLIGHT-4: country-level aggregation)
# --------------------------------------------------------------------------- #
def run_scan(cfg: dict, conn, fetcher, origins: list[str], destinations: list[dict]) -> list[dict]:
    currency   = cfg.get("currency", "EUR")
    adults     = cfg.get("adults", 1)
    non_stop   = cfg.get("non_stop", False)
    max_offers = cfg.get("max_offers_per_query", 5)
    rules      = cfg.get("bargain", {})
    limits     = cfg.get("limits", {})
    date_pairs = generate_date_pairs(cfg["scan"])
    now        = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    estimated = estimate_calls(origins, destinations, date_pairs)
    print(f"Scanning {len(origins)} origin(s) × {sum(len(d['airports']) for d in destinations)}"
          f" dest airport(s) × {len(date_pairs)} date(s)")
    check_budget(estimated, limits)
    print()

    all_alerts: list[dict] = []

    # --- FLIGHT-4: iterate per destination group (country / airport) ---
    for dest_group in destinations:
        label      = dest_group["label"]
        target     = dest_group.get("target_price")
        dest_airports = dest_group["airports"]

        # Collect best offer per (origin, dest_airport) pair
        # best_by_pair: {(origin, dest_iata): obs_dict}
        best_by_pair: dict[tuple[str,str], dict] = {}
        group_alerts: list[dict] = []

        for origin in origins:
            for dest_iata in dest_airports:
                for depart, ret in date_pairs:
                    offer = fetcher(
                        origin, dest_iata, depart, ret,
                        adults, currency, max_offers, non_stop,
                    )
                    if not offer:
                        continue
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
                    record_observation(conn, obs)
                    history = route_price_history(conn, origin, dest_iata, depart, ret)
                    is_bargain, reason = evaluate_bargain(offer["price"], target, history, rules)
                    if is_bargain:
                        group_alerts.append({**obs, "reason": reason})
                    pair = (origin, dest_iata)
                    if pair not in best_by_pair or offer["price"] < best_by_pair[pair]["price"]:
                        best_by_pair[pair] = obs

        all_alerts.extend(group_alerts)

        # --- FLIGHT-4: country-level aggregation ---
        if not best_by_pair:
            print(f"  {label}: no offers found")
            continue

        # Rank all (origin→dest) pairs by price
        ranked = sorted(best_by_pair.values(), key=lambda o: o["price"])
        best   = ranked[0]
        stops_str = ("direct"
                     if best.get("stops") == 0
                     else f"{best['stops']} stop(s)"
                     if best.get("stops") is not None
                     else "?")
        dur = (f" {best['duration_min']//60}h{best['duration_min']%60:02d}m"
               if best.get("duration_min") else "")
        fire = " 🔥" if group_alerts else ""

        print(f"  {'['+label+']':22}  best: {best['origin']}→{best['destination']}"
              f"  {best['price']:>8.0f} {best['currency']}"
              f"  {stops_str}{dur}"
              f"  {best['depart_date']}{('..'+best['return_date']) if best['return_date'] else ''}"
              f"{fire}")

        # Show top-3 alternative origins if multiple origins were scanned
        if len(origins) > 1:
            seen = set()
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
    return all_alerts


# --------------------------------------------------------------------------- #
# Demo fetcher  (updated for stops/duration + new country destinations)
# --------------------------------------------------------------------------- #
def make_demo_fetcher():
    import random
    rnd = random.Random(42)

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
    parser.add_argument("--config",   default=str(CONFIG_PATH))
    args = parser.parse_args()

    load_env()
    conn    = db_connect()
    airports = load_airports()

    if args.history:
        show_history(conn)
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
        notify(alerts, cfg)
    else:
        print("No bargains this run (prices stored to history for future comparison).")


if __name__ == "__main__":
    main()
