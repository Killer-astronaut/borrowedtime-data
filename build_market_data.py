#!/usr/bin/env python3
"""
build_market_data.py — generate market.json, the daily feed behind the
Gold Standard card.

Run this once a day (cron, launchd, or a GitHub Actions schedule) and publish
the resulting market.json to any static host. The app reads exactly one file
from that host and nothing else.

WHY A FEED INSTEAD OF THE APP CALLING A PRICE API DIRECTLY

  Neither number below is published by a federal agency in a form we can hit
  from the app. Spot gold is a licensed benchmark — LBMA owns it, which is why
  FRED dropped its gold series in 2023 — and every free tier that does expose a
  price is metered per key. Pointing N installs at one free-tier key is how the
  key gets revoked. One fetch a day into a CDN-cached file costs nothing, keeps
  the key on this machine, and lets a dead source be fixed by editing this
  script instead of shipping a build.

  The app degrades to Treasury data alone if this file is unreachable, so a
  failed run is a missing row on one card, never a broken counter.

SOURCES

  Gold spot     api.gold-api.com — keyless. Unofficial and unsupported, which
                is tolerable here precisely because only this script depends on
                it. Swap it and bump nothing: the app never sees the source.
  US equities   Federal Reserve Z.1 (Financial Accounts) — corporate equities
                at market value, issuer side, fetched and summed from the
                published CSV bundle. Public domain, quarterly, roughly a
                75-day lag. This is the honest number for "the entire stock
                market"; a daily index level is not, because an index level is
                not a market capitalization.
  Gold stock    World Gold Council above-ground stock estimate. Updated about
                once a year, so it is a reviewed constant here rather than a
                fetch — see ALL_GOLD_TONNES.

Stdlib only. Usage:  python3 tools/build_market_data.py [--out market.json]
"""

import argparse
import csv
import io
import json
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone

SCHEMA = 1

USER_AGENT = "BorrowedTime/market-data"

GOLD_URL = "https://api.gold-api.com/price/XAU"

# Troy ounces per tonne. Converted here so the app never carries a unit
# conversion it could get wrong in a future edit.
TROY_OZ_PER_TONNE = 32150.7466

# World Gold Council, above-ground stock through end-2024. Revisit annually;
# it moves about 3,600 t a year and the headline it feeds is not sensitive to
# a single year's mining.
ALL_GOLD_TONNES = 216265.0
ALL_GOLD_AS_OF = "2024-12-31"
ALL_GOLD_SOURCE = "World Gold Council, above-ground stocks"

# Federal Reserve Z.1 (Financial Accounts), corporate equities at market value.
#
# "The entire stock market" is the sum of two issuer-side series:
#   LM103164105  Nonfinancial corporate business; corporate equities; liability
#   LM793164105  Domestic financial sectors;      corporate equities; liability
#
# Summing the issuer side (not the "all sectors; asset" line) is deliberate:
# the asset line includes foreign equities held by U.S. residents, which is a
# different thing from what American corporations are worth.
#
# CAVEAT worth keeping in the UI copy: this is all U.S. corporate equity at
# market value, which includes closely-held and unlisted corporations — it is
# NOT a count of listed public companies. Do not label it "every public
# company"; the figure is bigger than that and the app's credibility rests on
# the label matching the series.
#
# Parsed by series ID out of the CSV header rather than by column position,
# so a re-ordered release fails loudly instead of silently reading a
# neighbouring series.
Z1_CSV_ZIP = "https://www.federalreserve.gov/releases/z1/current/z1_csv_files.zip"
Z1_TABLE = "csv/F51_1_s.csv"
Z1_EQUITY_SERIES = ("LM103164105.Q", "LM793164105.Q")
Z1_SOURCE = "Federal Reserve Z.1 Financial Accounts, corporate equities at market value"

# Refuse a price outside this band. A feed that starts returning 0, or a
# per-gram price mislabelled as per-ounce, would otherwise sail through and
# put an absurd headline in front of users.
GOLD_SANE_RANGE = (200.0, 100000.0)


def fetch_gold_price():
    """USD per fine troy ounce, with the source's own timestamp."""
    request = urllib.request.Request(GOLD_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"gold source returned HTTP {response.status}")
        payload = json.load(response)

    price = float(payload["price"])
    low, high = GOLD_SANE_RANGE
    if not low < price < high:
        raise RuntimeError(f"gold price {price} outside sane range {GOLD_SANE_RANGE}")

    as_of = str(payload.get("updatedAt", ""))[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return price, as_of


def fetch_us_equity_market_value():
    """Total market value of U.S.-issued corporate equities, in dollars.

    Returns (value, period) where period is the Z.1 quarter label, e.g.
    "2026:Q1", normalised to the quarter-end date the app displays.
    """
    # federalreserve.gov rejects urllib's default User-Agent with a 403.
    request = urllib.request.Request(Z1_CSV_ZIP, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=180) as response:
        if response.status != 200:
            raise RuntimeError(f"Z.1 returned HTTP {response.status}")
        payload = response.read()

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        with archive.open(Z1_TABLE) as handle:
            rows = list(csv.reader(io.TextIOWrapper(handle, encoding="utf-8")))

    header = [column.strip() for column in rows[0]]
    try:
        indices = [header.index(series) for series in Z1_EQUITY_SERIES]
    except ValueError as error:
        raise RuntimeError(f"Z.1 layout changed, series missing: {error}") from error

    # Walk backwards to the newest row that actually carries both figures;
    # the final row of a fresh release is occasionally still blank.
    for row in reversed(rows[1:]):
        values = [row[index].strip() for index in indices]
        if all(values):
            total = sum(float(value) for value in values) * 1e6  # millions -> dollars
            if total <= 0:
                raise RuntimeError("Z.1 equities total is not positive")
            return total, quarter_end(row[0].strip())

    raise RuntimeError("no Z.1 row carried both equity series")


def quarter_end(label):
    """'2026:Q1' -> '2026-03-31'. The app shows this beside a live counter,
    so it has to be the period end, not the release date."""
    year, quarter = label.split(":Q")
    return f"{year}-{['03-31', '06-30', '09-30', '12-31'][int(quarter) - 1]}"


def build(previous=None):
    # Gold is the number that actually moves day to day, so it is the one
    # allowed to fail the run. Equities change once a quarter; if the 8 MB Z.1
    # bundle is briefly unreachable there is no reason to also withhold a fresh
    # gold price, so the previous published values are carried forward.
    price, gold_as_of = fetch_gold_price()

    try:
        equities, equity_as_of = fetch_us_equity_market_value()
        equity_source = Z1_SOURCE
    except (urllib.error.URLError, RuntimeError, KeyError, ValueError) as error:
        if not previous or not previous.get("us_equity_market_value_usd"):
            raise
        print(f"  Z.1 unavailable ({error}); carrying forward previous equities",
              file=sys.stderr)
        equities = previous["us_equity_market_value_usd"]
        equity_as_of = previous["equity_as_of"]
        equity_source = previous["equity_source"]

    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "gold_usd_per_troy_oz": round(price, 2),
        "gold_as_of": gold_as_of,
        "gold_source": "Spot gold, USD per fine troy ounce",
        "us_equity_market_value_usd": round(equities, 2),
        "equity_as_of": equity_as_of,
        "equity_source": equity_source,
        "all_gold_ever_mined_troy_oz": round(ALL_GOLD_TONNES * TROY_OZ_PER_TONNE, 2),
        "all_gold_as_of": ALL_GOLD_AS_OF,
        "all_gold_source": ALL_GOLD_SOURCE,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="market.json", help="output path")
    args = parser.parse_args()

    previous = None
    try:
        with open(args.out, encoding="utf-8") as handle:
            previous = json.load(handle)
    except (OSError, ValueError):
        pass  # First run, or an unreadable file we are about to replace.

    try:
        feed = build(previous)
    except (urllib.error.URLError, RuntimeError, KeyError, ValueError) as error:
        # Exit non-zero and write nothing. The previously published file stays
        # up and the app keeps serving yesterday's price, which is far better
        # than replacing a good file with a broken one.
        print(f"market data build failed: {error}", file=sys.stderr)
        return 1

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(feed, handle, indent=2)
        handle.write("\n")

    print(f"wrote {args.out}")
    print(f"  gold      ${feed['gold_usd_per_troy_oz']:,.2f}/oz  ({feed['gold_as_of']})")
    print(f"  equities  ${feed['us_equity_market_value_usd'] / 1e12:,.2f}T  ({feed['equity_as_of']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
