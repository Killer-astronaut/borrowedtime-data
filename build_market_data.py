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

  Gold spot     Two keyless sources run by unrelated operators, tried in
                order: api.gold-api.com, then Swissquote's public quote feed.
                Both are unofficial and unsupported, which is tolerable here
                precisely because only this script depends on them — and why
                there are two. Swap either and bump nothing: the app never
                sees the source. See GOLD_SOURCES.
  US equities   Federal Reserve Z.1 (Financial Accounts) — corporate equities
                at market value, issuer side, fetched and summed from the
                published CSV bundle. Public domain, quarterly, roughly a
                75-day lag. This is the honest number for "the entire stock
                market"; a daily index level is not, because an index level is
                not a market capitalization.
  Gold stock    World Gold Council above-ground stock estimate. Updated about
                once a year, so it is a reviewed constant here rather than a
                fetch — see ALL_GOLD_TONNES.

FAILURE BEHAVIOUR

  Each source fails on its own. Whatever was fetched is published; whatever
  was not is carried forward from the previous file under its own, older
  "as of" date. The one rule that keeps that honest: `generated_at` — the only
  thing the app's 21-day staleness check looks at — advances only when the
  gold price is fresh. A run that could not get gold can still publish a new
  quarter's equities, but it cannot make an old price look new.

  Exit status is 0 only when every source was fresh. Anything else exits 1
  with one line per failed source on stderr, naming the source and the field,
  so the scheduled job can raise its alarm — including for a Z.1 failure,
  which used to be carried forward in silence indefinitely.

Stdlib only. Usage:  python3 build_market_data.py [--out market.json]
Tests:               python3 -m unittest discover -s tests
"""

import argparse
import csv
import http.client
import io
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone

SCHEMA = 1

USER_AGENT = "BorrowedTime/market-data"

GOLD_SOURCE_LABEL = "Spot gold, USD per fine troy ounce"

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
# so a re-ordered release still reads the right columns. The table's file name
# is only a first guess for the same reason: if the Fed renumbers its tables,
# the bundle is searched for whichever CSV carries both series.
Z1_CSV_ZIP = "https://www.federalreserve.gov/releases/z1/current/z1_csv_files.zip"
Z1_TABLE = "csv/F51_1_s.csv"
Z1_EQUITY_SERIES = ("LM103164105.Q", "LM793164105.Q")
Z1_SOURCE = "Federal Reserve Z.1 Financial Accounts, corporate equities at market value"

# A scheduled run gets one shot a day, and the runner's DNS resolver failing
# for half a second is enough to lose it — that is what took the feed down on
# 2026-09-13, with the source itself perfectly healthy a moment later. So every
# fetch gets a few attempts before the day is written off. The delays are short
# on purpose: a source that is genuinely gone should still fail the run inside a
# minute and raise the alarm, not hold a job open waiting for it to come back.
FETCH_BACKOFF_SECONDS = (2.0, 8.0)
FETCH_ATTEMPTS = len(FETCH_BACKOFF_SECONDS) + 1  # one try, then one per delay

# Worth another attempt: the server is overloaded, rate-limiting, or behind a
# proxy having a bad moment. Anything else (a 404 from a source that moved, a
# 401 from one that started demanding a key) is a real change that wants a
# human, so it fails immediately rather than three times slowly.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

# Refuse a price outside this band. A feed that starts returning 0, or a
# per-gram price mislabelled as per-ounce, would otherwise sail through and
# put an absurd headline in front of users.
GOLD_SANE_RANGE = (200.0, 100000.0)

# The band above is wide enough to admit the wrong metal or the wrong currency.
# So a price that has moved further than this from the last published one —
# when that one is recent enough to compare against — is only believed if a
# second source independently says the same thing. Gold's worst day on record
# is under 10%; a real move this size would show up on both sources.
GOLD_MAX_MOVE = 0.20
GOLD_MOVE_WINDOW = timedelta(days=10)
GOLD_AGREEMENT = 0.02

# Same idea for equities: U.S. corporate equity has been tens of trillions of
# dollars for decades. A total outside this band means the units changed
# (the table is in millions) or the wrong series was read.
EQUITY_SANE_RANGE = (1e13, 1e15)

FEED_KEYS = (
    "schema", "generated_at",
    "gold_usd_per_troy_oz", "gold_as_of", "gold_source",
    "us_equity_market_value_usd", "equity_as_of", "equity_source",
    "all_gold_ever_mined_troy_oz", "all_gold_as_of", "all_gold_source",
)


class SourceError(Exception):
    """One upstream source could not supply its figure.

    The message always names the source and, where it got that far, the field
    — it is quoted verbatim in the failure issue, and "KeyError: 'price'" is
    not something anyone can act on from a phone.
    """


def fetch(url, timeout, what):
    """GET url and return the body, retrying the failures worth retrying.

    `what` names the source in the error the alarm ends up quoting: "gold
    source unreachable" is a starting point, "<urlopen error ...>" on its own
    is not.
    """
    last_error = None

    for attempt in range(1, FETCH_ATTEMPTS + 1):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if response.status != 200:
                    raise SourceError(f"{what} returned HTTP {response.status}")
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code not in RETRYABLE_STATUS:
                raise SourceError(f"{what} returned HTTP {error.code}") from error
            last_error = error
        # URLError (DNS, refused connection, TLS), a read that timed out, and a
        # connection reset mid-transfer are all OSError; a truncated response is
        # not, hence the second class.
        except (OSError, http.client.HTTPException) as error:
            last_error = error

        if attempt < FETCH_ATTEMPTS:
            delay = FETCH_BACKOFF_SECONDS[attempt - 1]
            print(f"  {what}: {last_error}; retrying in {delay:.0f}s "
                  f"({attempt}/{FETCH_ATTEMPTS - 1})", file=sys.stderr)
            time.sleep(delay)

    raise SourceError(
        f"{what} unreachable after {FETCH_ATTEMPTS} attempts: {last_error}"
    ) from last_error


# ── Gold ─────────────────────────────────────────────────────────────────────

def _load_json(body, what):
    try:
        return json.loads(body)
    except ValueError as error:
        raise SourceError(f"{what}: response is not JSON ({error})") from error


def _positive_number(value, what, field):
    # bool is an int in Python; a source answering `true` is not a price.
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise SourceError(f"{what}: field '{field}' is not a number: {value!r}")
    try:
        number = float(value)
    except ValueError as error:
        raise SourceError(f"{what}: field '{field}' is not a number: {value!r}") from error
    if not number > 0:  # also refuses NaN
        raise SourceError(f"{what}: field '{field}' is not positive: {value!r}")
    return number


def parse_gold_api(body, today):
    """api.gold-api.com/price/XAU -> (price, as_of).

    {"name": "Gold", "symbol": "XAU", "currency": "USD", "price": 3875.9,
     "updatedAt": "2026-11-12T20:59:11Z", ...}
    """
    what = "gold-api.com"
    payload = _load_json(body, what)
    if not isinstance(payload, dict):
        raise SourceError(f"{what}: expected a JSON object, got {type(payload).__name__}")
    if "price" not in payload:
        raise SourceError(f"{what}: field 'price' is missing (fields present: "
                          f"{', '.join(sorted(payload)) or 'none'})")
    # Both are optional in the sense that their absence is tolerated, but if
    # the source says it is quoting something else, believe it.
    if payload.get("symbol", "XAU") != "XAU":
        raise SourceError(f"{what}: field 'symbol' is {payload['symbol']!r}, expected 'XAU'")
    if payload.get("currency", "USD") != "USD":
        raise SourceError(f"{what}: field 'currency' is {payload['currency']!r}, expected 'USD'")

    price = _positive_number(payload["price"], what, "price")
    return price, _iso_date(payload.get("updatedAt"), today)


def parse_swissquote(body, today):
    """Swissquote public quotes, XAU/USD -> (price, as_of).

    [{"topo": {...}, "ts": 1794517165920,
      "spreadProfilePrices": [{"spreadProfile": "premium",
                               "bid": 3875.011, "ask": 3875.669, ...}, ...]},
     ...]

    One entry per trading venue, several spread profiles each. The mid of every
    bid/ask pair is taken and the median used, so one venue quoting something
    odd does not move the figure.
    """
    what = "Swissquote"
    payload = _load_json(body, what)
    if not isinstance(payload, list) or not payload:
        raise SourceError(f"{what}: expected a non-empty JSON list of venues")

    mids, stamps = [], []
    for venue in payload:
        if not isinstance(venue, dict):
            continue
        quotes = venue.get("spreadProfilePrices")
        for quote in quotes if isinstance(quotes, list) else ():
            if isinstance(quote, dict) and "bid" in quote and "ask" in quote:
                bid = _positive_number(quote["bid"], what, "bid")
                ask = _positive_number(quote["ask"], what, "ask")
                mids.append((bid + ask) / 2)
        if isinstance(venue.get("ts"), (int, float)) and not isinstance(venue["ts"], bool):
            stamps.append(venue["ts"])

    if not mids:
        raise SourceError(f"{what}: no 'bid'/'ask' pair found under 'spreadProfilePrices'")

    as_of = today
    if stamps:
        try:
            as_of = datetime.fromtimestamp(max(stamps) / 1000, timezone.utc).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            pass  # A nonsense timestamp is not a reason to refuse a good price.
    return statistics.median(mids), as_of


def _iso_date(value, fallback):
    """The leading YYYY-MM-DD of a timestamp, or `fallback` if it isn't one."""
    text = str(value or "")[:10]
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return fallback
    return text


# Tried in order; the first believable price wins. They share nothing — not a
# host, a DNS zone, or an operator — which is the point: the 2026-11-12 failure
# was the runner being unable to resolve the one and only gold host for ten
# seconds. To add or replace a source, write a parser that returns
# (price, as_of) or raises SourceError, save a real response under
# tests/samples/, and add a line here.
GOLD_SOURCES = (
    ("gold-api.com", "https://api.gold-api.com/price/XAU", parse_gold_api),
    ("Swissquote",
     "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAU/USD",
     parse_swissquote),
)


def recent_gold_price(previous, now):
    """The last published price, if it is recent enough to compare against."""
    if not previous:
        return None
    try:
        generated = datetime.strptime(previous["generated_at"], "%Y-%m-%dT%H:%M:%SZ")
        price = float(previous["gold_usd_per_troy_oz"])
    except (KeyError, TypeError, ValueError):
        return None
    age = now - generated.replace(tzinfo=timezone.utc)
    if price > 0 and timedelta(0) <= age <= GOLD_MOVE_WINDOW:
        return price
    return None


def fetch_gold_price(previous=None, now=None, fetcher=fetch):
    """USD per fine troy ounce, with the source's own date.

    Raises SourceError listing what went wrong with every source if none of
    them produced a believable price.
    """
    now = now or datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    reference = recent_gold_price(previous, now)
    low, high = GOLD_SANE_RANGE

    failures = []
    unconfirmed = []  # prices that parsed but moved too far to trust alone

    for name, url, parse in GOLD_SOURCES:
        try:
            price, as_of = parse(fetcher(url, timeout=30, what=name), today)
            if not low < price < high:
                raise SourceError(f"{name}: price {price} is outside the sane range "
                                  f"{low:,.0f}–{high:,.0f} USD/oz")
        except SourceError as error:
            print(f"  gold: {error}", file=sys.stderr)
            failures.append(str(error))
            continue

        if reference is None or abs(price - reference) <= GOLD_MAX_MOVE * reference:
            return price, as_of
        if any(abs(price - other) <= GOLD_AGREEMENT * other for other in unconfirmed):
            return price, as_of  # a big move, but two sources agree on it

        unconfirmed.append(price)
        message = (f"{name}: price {price:,.2f} is {abs(price - reference) / reference:.0%} "
                   f"away from the last published {reference:,.2f} and no other "
                   f"source confirms it")
        print(f"  gold: {message}", file=sys.stderr)
        failures.append(message)

    raise SourceError("gold price: " + "; ".join(failures))


# ── Equities ─────────────────────────────────────────────────────────────────

def read_z1_table(payload):
    """The text of the CSV carrying both equity series, out of the Z.1 bundle."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as error:
        raise SourceError(f"Z.1: the download is not a zip archive ({error})") from error

    with archive:
        names = archive.namelist()
        # The known table first, then every other CSV: a renumbered table is
        # found by what it contains rather than by what it is called.
        candidates = [Z1_TABLE] if Z1_TABLE in names else []
        candidates += sorted(name for name in names
                             if name.lower().endswith(".csv") and name != Z1_TABLE)
        for name in candidates:
            try:
                text = archive.read(name).decode("utf-8-sig")
            except (UnicodeDecodeError, zipfile.BadZipFile, OSError):
                continue
            header = [column.strip() for column in text.split("\n", 1)[0].split(",")]
            if all(series in header for series in Z1_EQUITY_SERIES):
                if name != Z1_TABLE:
                    print(f"  Z.1: table moved from {Z1_TABLE} to {name}; "
                          f"update Z1_TABLE", file=sys.stderr)
                return text

    raise SourceError(
        f"Z.1: no CSV in the bundle has both series {' and '.join(Z1_EQUITY_SERIES)} "
        f"in its header ({len(names)} files searched; expected {Z1_TABLE})")


def parse_z1_table(text):
    """Z.1 table CSV -> (total equities in dollars, quarter-end date).

    date,LM893064105.Q,LM103164105.Q,...,LM793164105.Q,...
    2026:Q2,...,83050847,...,26193848,...          (millions of dollars)
    """
    rows = [row for row in csv.reader(io.StringIO(text)) if row]
    if not rows:
        raise SourceError("Z.1: the equities table is empty")

    header = [column.strip() for column in rows[0]]
    missing = [series for series in Z1_EQUITY_SERIES if series not in header]
    if missing:
        raise SourceError(f"Z.1: series {', '.join(missing)} missing from the table header "
                          f"(layout changed; header starts {', '.join(header[:4])})")
    if "date" not in header:
        raise SourceError(f"Z.1: column 'date' missing from the table header "
                          f"(header starts {', '.join(header[:4])})")
    date_index = header.index("date")
    indices = [header.index(series) for series in Z1_EQUITY_SERIES]

    # Walk backwards to the newest row that actually carries both figures;
    # the final row of a fresh release is occasionally still blank.
    for row in reversed(rows[1:]):
        if len(row) <= max(indices + [date_index]):
            continue
        values = [row[index].strip() for index in indices]
        period = row[date_index].strip()
        if not all(values):
            continue

        total = 0.0
        for series, value in zip(Z1_EQUITY_SERIES, values):
            try:
                total += float(value)
            except ValueError as error:
                raise SourceError(f"Z.1: series {series} for {period} is not a "
                                  f"number: {value!r}") from error
        total *= 1e6  # millions -> dollars

        low, high = EQUITY_SANE_RANGE
        if not low < total < high:
            raise SourceError(f"Z.1: equities total {total:,.0f} for {period} is outside "
                              f"the sane range (units or series changed?)")
        return total, quarter_end(period)

    raise SourceError(f"Z.1: no row carries both {' and '.join(Z1_EQUITY_SERIES)}")


def quarter_end(label):
    """'2026:Q1' -> '2026-03-31'. The app shows this beside a live counter,
    so it has to be the period end, not the release date."""
    try:
        year, quarter = label.split(":Q")
        if not (len(year) == 4 and year.isdigit() and quarter in ("1", "2", "3", "4")):
            raise ValueError(label)
    except ValueError as error:
        raise SourceError(f"Z.1: column 'date' is {label!r}, expected a quarter "
                          f"like '2026:Q1'") from error
    return f"{year}-{['03-31', '06-30', '09-30', '12-31'][int(quarter) - 1]}"


def fetch_us_equity_market_value(fetcher=fetch):
    """Total market value of U.S.-issued corporate equities, in dollars, with
    the quarter-end date it is measured at."""
    # federalreserve.gov rejects urllib's default User-Agent with a 403, which
    # fetch() sends straight to the caller rather than retrying.
    return parse_z1_table(read_z1_table(fetcher(Z1_CSV_ZIP, timeout=180, what="Z.1")))


# ── Assembly ─────────────────────────────────────────────────────────────────

def usable_previous(previous):
    """The previous file, if it is one this script could have written."""
    if not isinstance(previous, dict) or previous.get("schema") != SCHEMA:
        return None
    try:
        datetime.strptime(previous["generated_at"], "%Y-%m-%dT%H:%M:%SZ")
        if not (float(previous["gold_usd_per_troy_oz"]) > 0
                and float(previous["us_equity_market_value_usd"]) > 0):
            return None
        for key in ("gold_as_of", "equity_as_of", "equity_source"):
            if not isinstance(previous[key], str):
                return None
    except (KeyError, TypeError, ValueError):
        return None
    return previous


def build(previous=None, now=None, fetcher=fetch):
    """Returns (feed, problems).

    `problems` has one line per source that failed. `feed` is what should be
    on disk afterwards: fresh figures where a source answered, the previous
    file's figures (and dates) where it did not, or None if a source failed
    and there is no previous file to fall back on.
    """
    now = now or datetime.now(timezone.utc)
    previous = usable_previous(previous)
    problems = []

    gold = equity = None
    # The broad catch is deliberate: a source answering in a shape nobody
    # anticipated must not take the other source down with it.
    try:
        gold = fetch_gold_price(previous, now, fetcher)
    except SourceError as error:
        problems.append(str(error))
    except Exception as error:  # noqa: BLE001
        problems.append(f"gold price: unexpected {type(error).__name__}: {error}")
    try:
        equity = fetch_us_equity_market_value(fetcher)
    except SourceError as error:
        problems.append(str(error))
    except Exception as error:  # noqa: BLE001
        problems.append(f"Z.1: unexpected {type(error).__name__}: {error}")

    if previous is None and (gold is None or equity is None):
        return None, problems

    if gold:
        price, gold_as_of = gold
        # Only a fresh gold price moves the clock the app judges staleness by.
        generated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        price, gold_as_of = previous["gold_usd_per_troy_oz"], previous["gold_as_of"]
        generated_at = previous["generated_at"]

    if equity:
        equities, equity_as_of = equity
        equity_source = Z1_SOURCE
    else:
        equities = previous["us_equity_market_value_usd"]
        equity_as_of = previous["equity_as_of"]
        equity_source = previous["equity_source"]

    feed = {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "gold_usd_per_troy_oz": round(float(price), 2),
        "gold_as_of": gold_as_of,
        "gold_source": GOLD_SOURCE_LABEL,
        "us_equity_market_value_usd": round(float(equities), 2),
        "equity_as_of": equity_as_of,
        "equity_source": equity_source,
        "all_gold_ever_mined_troy_oz": round(ALL_GOLD_TONNES * TROY_OZ_PER_TONNE, 2),
        "all_gold_as_of": ALL_GOLD_AS_OF,
        "all_gold_source": ALL_GOLD_SOURCE,
    }
    assert tuple(feed) == FEED_KEYS  # the app decodes exactly these
    return feed, problems


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate market.json, the daily feed behind the Gold Standard card.")
    parser.add_argument("--out", default="market.json", help="output path")
    args = parser.parse_args(argv)

    previous = None
    try:
        with open(args.out, encoding="utf-8") as handle:
            previous = json.load(handle)
    except (OSError, ValueError):
        pass  # First run, or an unreadable file we are about to replace.

    feed, problems = build(previous)

    if feed is None:
        # Write nothing. Whatever is published stays up, which is far better
        # than replacing a good file with a broken one.
        print("market data build failed; nothing written:", file=sys.stderr)
    else:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(feed, handle, indent=2)
            handle.write("\n")
        fresh_gold = feed["generated_at"] != (previous or {}).get("generated_at")
        print(f"wrote {args.out}")
        print(f"  gold      ${feed['gold_usd_per_troy_oz']:,.2f}/oz  ({feed['gold_as_of']})"
              f"{'' if fresh_gold else '  [carried forward]'}")
        print(f"  equities  ${feed['us_equity_market_value_usd'] / 1e12:,.2f}T  "
              f"({feed['equity_as_of']})")
        if problems:
            print("market data build incomplete; figures that could not be refreshed "
                  "were carried forward under their old dates:", file=sys.stderr)

    for problem in problems:
        print(f"FAILED: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
