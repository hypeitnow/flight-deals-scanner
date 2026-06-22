# Flight Deals Scanner

Watch **countries or specific airports** for bargain fares from **multiple Polish origins**
(or any airport). The scanner tells you which departure airport gives the best deal,
ranks alternatives, and alerts when a price drops below your target or its historical low.

Built on the **Amadeus Self-Service API** (free tier: ~2,000 requests/month, open
self-signup) — Skyscanner's `robots.txt` explicitly forbids its deals/calendar pages
and internal APIs, so this uses Amadeus for the same fare data within terms of service.

## What it does

- Define destinations **by country** (`ME` = Montenegro, `BA` = Bosnia, `HR` = Croatia)
  or by specific airport IATA code — the scanner expands countries to all their airports.
- Define origins as a **group** (`poland_major`, `poland_all`), a country code (`PL`),
  a list of IATAs, or a single airport.
- It queries the **cheapest fare + stops + flight duration** for every
  origin × destination airport × date combination.
- Stores every price in a local SQLite history (`prices.db`).
- **Ranks departure airports** per destination country — shows which Polish city
  to fly from and highlights alternatives.
- Flags a **bargain** when a price is:
  - `<=` your `target_price` for that destination, **or**
  - `<=` the route's historical *p25* (after `min_history` observations), **or**
  - a `drop_pct_alert`% drop below the previous low.
- **API-budget guardrail**: estimates call count before scanning; aborts if over cap.
- Optional **Telegram / webhook** notifications on bargains.

## Setup

```bash
cd ~/repos/flight-deals-scanner
cp .env.example .env        # paste your Amadeus key & secret
# edit config.json -> set origins, currency, and your destinations
```

Get free Amadeus credentials at <https://developers.amadeus.com/> → **Create app** →
copy **API Key** and **API Secret** into `.env`. No `pip install` needed (stdlib only).

## Usage

```bash
# Offline demo with synthetic prices (no API key needed):
python3 flight_scanner.py --demo

# Preview how many API calls a scan would make, without scanning:
python3 flight_scanner.py --estimate

# Real scan (uses .env credentials + config.json):
python3 flight_scanner.py

# Show stored price-history summary per route:
python3 flight_scanner.py --history
```

### Example output (demo, Poland → Balkans)

```
Scanning 6 origin(s) × 13 dest airport(s) × 4 date(s)
  Estimated API calls: 312  (cap: 500)

  [Montenegro]             best: WRO→TIV    431 PLN  1 stop(s) 4h52m  2026-07-04..2026-07-11 🔥
                           alt:  KRK→TGD    442 PLN  1 stop(s)  2026-07-03..2026-07-08
                           alt:  WMI→TIV    443 PLN  direct     2026-07-03..2026-07-08
  [Bosnia & Herzegovina]   best: KTW→SJJ    465 PLN  1 stop(s) 3h08m  2026-07-03..2026-07-08 🔥
                           alt:  WMI→SJJ    476 PLN  direct     2026-07-04..2026-07-11
  [Croatia]                best: WMI→ZAG    324 PLN  direct 2h30m  2026-07-03..2026-07-08 🔥
                           alt:  WAW→ZAG    359 PLN  direct     2026-07-04..2026-07-09
```

Run daily so the history builds up and the percentile/drop detection becomes meaningful:

```cron
# crontab -e  — scan every morning at 08:00
0 8 * * *  cd ~/repos/flight-deals-scanner && /usr/bin/python3 flight_scanner.py >> scan.log 2>&1
```

## Picking destinations

### By country (recommended)
Use the 2-letter ISO country code. The scanner expands it to all known airports:

```json
"destinations": [
  { "country": "ME", "label": "Montenegro",          "target_price": 600 },
  { "country": "BA", "label": "Bosnia & Herzegovina", "target_price": 650 },
  { "country": "HR", "label": "Croatia",              "target_price": 500 }
]
```

Supported countries include all major European destinations plus Turkey, Morocco,
Egypt, UAE, Thailand, Japan, and more. See `data/airports.json` for the full list.

### By specific airport
```json
"destinations": [
  { "destination": "TIV", "label": "Tivat (Montenegro)", "target_price": 550 }
]
```

### By named group
```json
"destinations": [
  { "group": "med_beach", "label": "Med beaches", "target_price": 500 }
]
```
Available groups: `balkans`, `med_beach`, `western_europe` (see `data/airports.json`).

## Picking origins

### All major Polish airports (recommended starting point)
```json
"origins": { "group": "poland_major" }
```
Expands to: WAW, WMI, KRK, KTW, GDN, WRO.

### All Polish airports (more thorough, more API calls)
```json
"origins": { "group": "poland_all" }
```
Adds: POZ, RZE, SZZ, BZG, SZY, LUZ.

### By country code
```json
"origins": { "country": "PL" }
```

### Specific list
```json
"origins": ["WAW", "KRK", "KTW"]
```

### Single airport (legacy / simple mode)
```json
"origins": "WAW"
```

## Config reference (`config.json`)

| Field | Meaning |
|---|---|
| `amadeus_env` | `test` (sandbox, free) or `production` (live fares, needs prod app) |
| `origins` | Origin(s): group name, country code, IATA list, or single IATA |
| `currency` | Price currency (`PLN`, `EUR`, …) |
| `non_stop` | `true` = direct flights only |
| `scan.date_from/to` | Travel window to search |
| `scan.weekdays` | Departure weekdays, `0`=Mon…`6`=Sun (e.g. `[4,5]` = Fri+Sat) |
| `scan.trip_length_days` | Round-trip nights, e.g. `[5,7]`; use `[null]` for one-way |
| `scan.max_dates_per_route` | Cap on date pairs per route (controls API calls) |
| `destinations[].country` | Destination country code — expands to all its airports |
| `destinations[].destination` | OR a single destination IATA code |
| `destinations[].target_price` | Hard bargain threshold for this destination |
| `limits.max_api_calls` | Scan aborts if estimated calls exceed this (default: 500) |
| `bargain.percentile` | Historical percentile threshold for bargain alert (e.g. 25) |
| `bargain.min_history` | Observations needed before percentile logic activates |
| `bargain.drop_pct_alert` | % drop vs previous low that triggers an alert |

## Managing API quota

The free Amadeus test tier gives ~2,000 calls/month. Use `--estimate` to preview:

```bash
python3 flight_scanner.py --estimate
# Origins: 6  ['WAW', 'WMI', 'KRK', 'KTW', 'GDN', 'WRO']
# Destination groups: 3 | Dest airports: 13
# Date pairs: 4
# Estimated calls: 312  (cap: 500)  ✅ Within cap.
```

Tips to reduce calls:
- Lower `scan.max_dates_per_route` (4 is a good daily value)
- Use `"group": "poland_major"` instead of `poland_all`
- Use `"non_stop": true` to cut irrelevant routes
- Raise `limits.max_api_calls` only after verifying your monthly quota

## Notes

- The free **test** environment returns realistic but cached fares — great for
  development. Switch `amadeus_env` to `production` for live booking-grade prices
  (requires a separate production app on the Amadeus portal).
- This is for **personal deal-watching**. Don't redistribute fares commercially
  without an appropriate Amadeus commercial agreement.
- Airport lists in `data/airports.json` are curated and editable — add or remove
  airports per country as needed.
