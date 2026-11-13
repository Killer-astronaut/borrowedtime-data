"""
Tests for build_market_data.py. Stdlib only, no network:

    python3 -m unittest discover -s tests

tests/samples/ holds real responses saved from each upstream source. When a
source changes its layout, the fix starts here: save the new response over the
old sample, watch the matching test fail, then fix the parser until it passes.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import build_market_data as bmd  # noqa: E402

SAMPLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")
NOW = datetime(2026, 11, 12, 21, 0, 0, tzinfo=timezone.utc)
TODAY = "2026-11-12"

GOLD_URLS = {name: url for name, url, _ in bmd.GOLD_SOURCES}
SWISSQUOTE_URL = GOLD_URLS["Swissquote"]
GOLD_API_URL = GOLD_URLS["gold-api.com"]
CURRENCY_API_URL = GOLD_URLS["currency-api"]


def sample(name):
    with open(os.path.join(SAMPLES, name), "rb") as handle:
        return handle.read()


def z1_zip(tables):
    """A Z.1-shaped bundle holding {member name: csv bytes}."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in tables.items():
            archive.writestr(name, body)
    return buffer.getvalue()


GOOD_Z1 = z1_zip({bmd.Z1_TABLE: sample("z1_F51_1_s.csv")})

PREVIOUS = {
    "schema": 1,
    "generated_at": "2026-11-11T18:41:53Z",
    "gold_usd_per_troy_oz": 3991.6,
    "gold_as_of": "2026-11-11",
    "gold_source": "Spot gold, USD per fine troy ounce",
    "us_equity_market_value_usd": 91857203000000.0,
    "equity_as_of": "2026-03-31",
    "equity_source": "Federal Reserve Z.1 Financial Accounts, corporate equities at market value",
    "all_gold_ever_mined_troy_oz": 6953081213.45,
    "all_gold_as_of": "2024-12-31",
    "all_gold_source": "World Gold Council, above-ground stocks",
}


def fetcher(responses):
    """Stands in for bmd.fetch. A response is bytes, or an exception to raise."""
    calls = []

    def fake(url, timeout, what):
        calls.append(url)
        response = responses[url]
        if isinstance(response, Exception):
            raise response
        return response

    fake.calls = calls
    return fake


def healthy(**overrides):
    responses = {
        SWISSQUOTE_URL: sample("swissquote_xau_usd.json"),
        GOLD_API_URL: sample("gold_api_xau.json"),
        CURRENCY_API_URL: sample("currency_api_xau.json"),
        bmd.Z1_CSV_ZIP: GOOD_Z1,
    }
    responses.update(overrides)
    return fetcher(responses)


DOWN = bmd.SourceError("unreachable after 3 attempts: Name or service not known")
ALL_GOLD_DOWN = {SWISSQUOTE_URL: DOWN, GOLD_API_URL: DOWN, CURRENCY_API_URL: DOWN}

# What each saved sample says, so the tests below read as "which source won".
SWISSQUOTE_PRICE = bmd.parse_swissquote(sample("swissquote_xau_usd.json"), TODAY)[0]
GOLD_API_PRICE = bmd.parse_gold_api(sample("gold_api_xau.json"), TODAY)[0]
CURRENCY_API_PRICE = bmd.parse_currency_api(sample("currency_api_xau.json"), TODAY)[0]


class QuietCase(unittest.TestCase):
    """The script narrates failures on stderr; keep that out of the test run."""

    def setUp(self):
        self.stderr = io.StringIO()
        redirect = contextlib.redirect_stderr(self.stderr)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)


# ── Gold parsers, against the saved responses ────────────────────────────────

class GoldApiParser(QuietCase):
    def test_real_response(self):
        price, as_of = bmd.parse_gold_api(sample("gold_api_xau.json"), TODAY)
        self.assertAlmostEqual(price, 3875.899902)
        self.assertEqual(as_of, "2026-11-12")

    def test_missing_price_names_the_field(self):
        payload = json.loads(sample("gold_api_xau.json"))
        payload["value"] = payload.pop("price")  # the kind of rename that happens
        with self.assertRaisesRegex(bmd.SourceError, r"gold-api\.com: field 'price' is missing"):
            bmd.parse_gold_api(json.dumps(payload), TODAY)

    def test_wrong_metal_or_currency_is_refused(self):
        for field, value in (("symbol", "XAG"), ("currency", "EUR")):
            payload = json.loads(sample("gold_api_xau.json"))
            payload[field] = value
            with self.assertRaisesRegex(bmd.SourceError, f"field '{field}'"):
                bmd.parse_gold_api(json.dumps(payload), TODAY)

    def test_non_numeric_price(self):
        for bad in (None, "n/a", True, [], 0, -5, float("nan")):
            payload = json.loads(sample("gold_api_xau.json"))
            payload["price"] = bad
            with self.assertRaisesRegex(bmd.SourceError, "field 'price'", msg=repr(bad)):
                bmd.parse_gold_api(json.dumps(payload), TODAY)

    def test_html_error_page(self):
        with self.assertRaisesRegex(bmd.SourceError, r"gold-api\.com: response is not JSON"):
            bmd.parse_gold_api(b"<html>502 Bad Gateway</html>", TODAY)

    def test_unexpected_shape(self):
        with self.assertRaisesRegex(bmd.SourceError, "expected a JSON object"):
            bmd.parse_gold_api(b"[1, 2]", TODAY)

    def test_missing_or_odd_timestamp_falls_back_to_today(self):
        for stamp in (None, "", "a few seconds ago", 1794517165920):
            payload = json.loads(sample("gold_api_xau.json"))
            payload["updatedAt"] = stamp
            _, as_of = bmd.parse_gold_api(json.dumps(payload), TODAY)
            self.assertEqual(as_of, TODAY, msg=repr(stamp))


class SwissquoteParser(QuietCase):
    def test_real_response(self):
        price, as_of = bmd.parse_swissquote(sample("swissquote_xau_usd.json"), TODAY)
        # Saved seconds apart from the gold-api sample: the two sources should
        # be describing the same market.
        self.assertAlmostEqual(price, GOLD_API_PRICE, delta=GOLD_API_PRICE * 0.005)
        self.assertEqual(as_of, "2026-11-12")

    def test_one_odd_venue_does_not_move_the_price(self):
        payload = json.loads(sample("swissquote_xau_usd.json"))
        payload[0]["spreadProfilePrices"][0].update(bid=1.0, ask=2.0)
        price, _ = bmd.parse_swissquote(json.dumps(payload), TODAY)
        self.assertAlmostEqual(price, SWISSQUOTE_PRICE, delta=1.0)

    def test_renamed_quote_fields_name_the_field(self):
        payload = json.loads(sample("swissquote_xau_usd.json"))
        for venue in payload:
            venue["prices"] = venue.pop("spreadProfilePrices")
        with self.assertRaisesRegex(bmd.SourceError,
                                    "Swissquote: no 'bid'/'ask' pair found under 'spreadProfilePrices'"):
            bmd.parse_swissquote(json.dumps(payload), TODAY)

    def test_empty_and_wrong_shapes(self):
        for body in (b"[]", b"{}", b"null", b"not json"):
            with self.assertRaises(bmd.SourceError, msg=body):
                bmd.parse_swissquote(body, TODAY)

    def test_non_numeric_bid(self):
        payload = json.loads(sample("swissquote_xau_usd.json"))
        payload[0]["spreadProfilePrices"][0]["bid"] = "closed"
        with self.assertRaisesRegex(bmd.SourceError, "Swissquote: field 'bid'"):
            bmd.parse_swissquote(json.dumps(payload), TODAY)


class CurrencyApiParser(QuietCase):
    def test_real_response(self):
        price, as_of = bmd.parse_currency_api(sample("currency_api_xau.json"), "2026-11-13")
        self.assertAlmostEqual(price, 3904.20944053)
        self.assertEqual(as_of, "2026-11-13")
        # Saved a day after the other two samples; same market, a day's drift.
        self.assertAlmostEqual(price, GOLD_API_PRICE, delta=GOLD_API_PRICE * 0.02)

    def test_missing_usd_rate_names_the_field(self):
        payload = json.loads(sample("currency_api_xau.json"))
        del payload["xau"]["usd"]
        with self.assertRaisesRegex(bmd.SourceError, r"currency-api: field 'xau\.usd' is missing"):
            bmd.parse_currency_api(json.dumps(payload), TODAY)

    def test_renamed_base_names_the_field(self):
        payload = json.loads(sample("currency_api_xau.json"))
        payload["XAU"] = payload.pop("xau")
        with self.assertRaisesRegex(bmd.SourceError, "currency-api: field 'xau' is missing"):
            bmd.parse_currency_api(json.dumps(payload), TODAY)

    def test_snapshot_that_stopped_updating_is_refused(self):
        body = sample("currency_api_xau.json")  # dated 2026-11-13
        bmd.parse_currency_api(body, "2026-11-18")
        with self.assertRaisesRegex(bmd.SourceError, "6 days old; the snapshot has stopped updating"):
            bmd.parse_currency_api(body, "2026-11-19")

    def test_missing_or_odd_date_is_refused(self):
        # Unlike a live quote, a snapshot with no date cannot be assumed fresh.
        for bad in (None, "", "yesterday", 20261113):
            payload = json.loads(sample("currency_api_xau.json"))
            payload["date"] = bad
            with self.assertRaisesRegex(bmd.SourceError, "field 'date'", msg=repr(bad)):
                bmd.parse_currency_api(json.dumps(payload), TODAY)

    def test_wrong_shapes(self):
        for body in (b"[]", b"null", b"<html>", b'{"xau": 3904.2}'):
            with self.assertRaises(bmd.SourceError, msg=body):
                bmd.parse_currency_api(body, TODAY)


# ── Gold source selection ────────────────────────────────────────────────────

class GoldSelection(QuietCase):
    def test_every_source_is_asked_and_the_first_wins(self):
        fake = healthy()
        price, as_of = bmd.fetch_gold_price(PREVIOUS, NOW, fake)
        self.assertEqual(price, SWISSQUOTE_PRICE)
        self.assertEqual(as_of, "2026-11-12")
        # All three, even though the first answered: that is how a source that
        # has died behind a working one gets noticed.
        self.assertEqual(fake.calls, [SWISSQUOTE_URL, GOLD_API_URL, CURRENCY_API_URL])

    def test_order_is_live_quotes_then_the_daily_snapshot(self):
        self.assertEqual([name for name, _, _ in bmd.GOLD_SOURCES],
                         ["Swissquote", "gold-api.com", "currency-api"])

    def test_falls_back_when_a_source_is_unreachable(self):
        """The 2026-11-12 failure: the runner could not resolve a gold host."""
        price, _ = bmd.fetch_gold_price(PREVIOUS, NOW, healthy(**{SWISSQUOTE_URL: DOWN}))
        self.assertEqual(price, GOLD_API_PRICE)

    def test_survives_on_the_last_source_alone(self):
        fake = healthy(**{SWISSQUOTE_URL: DOWN, GOLD_API_URL: DOWN})
        price, as_of = bmd.fetch_gold_price(PREVIOUS, NOW, fake)
        self.assertEqual(price, CURRENCY_API_PRICE)
        self.assertEqual(as_of, "2026-11-13")  # the snapshot's own date

    def test_falls_back_when_a_source_changes_layout(self):
        fake = healthy(**{SWISSQUOTE_URL: b'{"data": {"xau": 3875.9}}'})
        self.assertEqual(bmd.fetch_gold_price(PREVIOUS, NOW, fake)[0], GOLD_API_PRICE)

    def test_falls_back_when_a_source_is_out_of_range(self):
        per_gram = healthy(**{SWISSQUOTE_URL: DOWN, GOLD_API_URL: b'{"price": 124.6}'})
        self.assertEqual(bmd.fetch_gold_price(PREVIOUS, NOW, per_gram)[0], CURRENCY_API_PRICE)

    def test_error_names_every_source_when_all_fail(self):
        fake = healthy(**{SWISSQUOTE_URL: b"[]", GOLD_API_URL: DOWN,
                          CURRENCY_API_URL: b'{"date": "2026-11-12", "xau": {}}'})
        with self.assertRaises(bmd.SourceError) as caught:
            bmd.fetch_gold_price(PREVIOUS, NOW, fake)
        message = str(caught.exception)
        self.assertIn("Swissquote: expected a non-empty JSON list", message)
        self.assertIn("Name or service not known", message)
        self.assertIn("currency-api: field 'xau.usd' is missing", message)

    def test_outcomes_record_every_source(self):
        quote, outcomes = bmd.survey_gold(PREVIOUS, NOW, healthy(**{GOLD_API_URL: DOWN}))
        self.assertEqual(quote[0], SWISSQUOTE_PRICE)
        self.assertEqual(list(outcomes), ["Swissquote", "gold-api.com", "currency-api"])
        self.assertIsNone(outcomes["Swissquote"])
        self.assertIn("Name or service not known", outcomes["gold-api.com"])
        self.assertIsNone(outcomes["currency-api"])

    def test_a_bug_in_one_parser_is_that_sources_failure(self):
        def explode(url, timeout, what):
            if url == SWISSQUOTE_URL:
                raise RecursionError("something nobody anticipated")
            return healthy()(url, timeout, what)

        quote, outcomes = bmd.survey_gold(PREVIOUS, NOW, explode)
        self.assertEqual(quote[0], GOLD_API_PRICE)
        self.assertIn("Swissquote: unexpected RecursionError", outcomes["Swissquote"])

    def test_implausible_jump_is_passed_over_for_a_plausible_source(self):
        # In range, but 48% below yesterday: platinum, or a currency mix-up.
        fake = healthy(**{SWISSQUOTE_URL: DOWN, GOLD_API_URL: b'{"price": 2010.0}'})
        quote, outcomes = bmd.survey_gold(PREVIOUS, NOW, fake)
        self.assertEqual(quote[0], CURRENCY_API_PRICE)
        self.assertIn("no other source confirms", outcomes["gold-api.com"])

    def test_implausible_jump_alone_is_refused(self):
        fake = healthy(**{SWISSQUOTE_URL: DOWN, GOLD_API_URL: b'{"price": 2010.0}',
                          CURRENCY_API_URL: DOWN})
        with self.assertRaisesRegex(bmd.SourceError, "no other source confirms"):
            bmd.fetch_gold_price(PREVIOUS, NOW, fake)

    def test_large_move_confirmed_by_a_second_source_is_accepted(self):
        crash = json.loads(sample("swissquote_xau_usd.json"))
        for venue in crash:
            for quote in venue["spreadProfilePrices"]:
                quote.update(bid=2005.0, ask=2006.0)
        fake = healthy(**{SWISSQUOTE_URL: json.dumps(crash).encode(),
                          GOLD_API_URL: b'{"price": 2010.0}',
                          CURRENCY_API_URL: DOWN})
        quote, outcomes = bmd.survey_gold(PREVIOUS, NOW, fake)
        self.assertAlmostEqual(quote[0], 2005.5)
        self.assertIsNone(outcomes["Swissquote"])
        self.assertIsNone(outcomes["gold-api.com"])

    def test_jump_check_is_skipped_without_a_recent_reference(self):
        fake = healthy(**{SWISSQUOTE_URL: DOWN, GOLD_API_URL: b'{"price": 2010.0}'})
        stale = dict(PREVIOUS, generated_at="2026-09-03T12:55:10Z")
        for previous in (None, stale):
            price, _ = bmd.fetch_gold_price(previous, NOW, fake)
            self.assertEqual(price, 2010.0)


# ── Source health ────────────────────────────────────────────────────────────

class Health(QuietCase):
    ERROR = "gold-api.com unreachable after 3 attempts: Name or service not known"

    def run_days(self, days, outcomes_for):
        """Feed update_health one run per day; returns (health, warnings) of the last."""
        health = warnings = None
        for day in days:
            health, warnings = bmd.update_health(health, outcomes_for(day), day)
        return health, warnings

    def failing(self, _day=None):
        return {"Swissquote": None, "gold-api.com": self.ERROR, "currency-api": None}

    def test_healthy_sources_are_recorded_as_such(self):
        health, warnings = bmd.update_health(None, dict.fromkeys(GOLD_URLS), TODAY)
        self.assertEqual(warnings, [])
        self.assertEqual(health, {name: {"failing_since": None, "last_error": None}
                                  for name in GOLD_URLS})

    def test_no_warning_before_the_third_day(self):
        health, warnings = self.run_days(["2026-11-12", "2026-11-13"], self.failing)
        self.assertEqual(warnings, [])
        self.assertEqual(health["gold-api.com"],
                         {"failing_since": "2026-11-12", "last_error": self.ERROR})

    def test_warning_on_the_third_day_names_source_date_and_error(self):
        _, warnings = self.run_days(["2026-11-12", "2026-11-13", "2026-11-14"], self.failing)
        self.assertEqual(len(warnings), 1)
        self.assertIn("gold source gold-api.com has failed on every run since 2026-11-12 (3 days)",
                      warnings[0])
        self.assertIn("Name or service not known", warnings[0])

    def test_warning_repeats_while_it_keeps_failing(self):
        days = [f"2026-11-{d}" for d in range(12, 20)]
        _, warnings = self.run_days(days, self.failing)
        self.assertIn("since 2026-11-12 (8 days)", warnings[0])

    def test_two_runs_on_one_day_count_once(self):
        days = ["2026-11-12", "2026-11-12", "2026-11-12", "2026-11-13"]
        _, warnings = self.run_days(days, self.failing)
        self.assertEqual(warnings, [])

    def test_one_good_run_resets_the_clock(self):
        def outcomes(day):
            return dict.fromkeys(GOLD_URLS) if day == "2026-11-13" else self.failing()

        health, warnings = self.run_days(
            ["2026-11-11", "2026-11-12", "2026-11-13", "2026-11-14", "2026-11-15"], outcomes)
        self.assertEqual(warnings, [])
        self.assertEqual(health["gold-api.com"]["failing_since"], "2026-11-14")

    def test_mangled_health_file_starts_again(self):
        for junk in (None, [], "x", {"gold-api.com": "broken"},
                     {"gold-api.com": {"failing_since": "soon"}},
                     {"gold-api.com": {"failing_since": "2027-01-01"}}):  # in the future
            health, warnings = bmd.update_health(junk, self.failing(), TODAY)
            self.assertEqual(warnings, [], msg=repr(junk))
            self.assertEqual(health["gold-api.com"]["failing_since"], TODAY, msg=repr(junk))

    def test_a_removed_source_is_dropped_from_the_record(self):
        health, _ = bmd.update_health(
            {"metals.live": {"failing_since": "2026-01-01", "last_error": "gone"}},
            self.failing(), TODAY)
        self.assertNotIn("metals.live", health)


# ── Z.1 ──────────────────────────────────────────────────────────────────────

class Z1Table(QuietCase):
    def setUp(self):
        super().setUp()
        self.text = sample("z1_F51_1_s.csv").decode("utf-8")

    def test_real_table(self):
        total, as_of = bmd.parse_z1_table(self.text)
        # 2026:Q2 — 83,050,847 + 26,193,848 (millions)
        self.assertEqual(total, 109244695000000.0)
        self.assertEqual(as_of, "2026-06-30")

    def test_reordered_columns_read_the_same_figures(self):
        rows = [line.split(",") for line in self.text.strip().split("\n")]
        reordered = "\n".join(",".join(reversed(row)) for row in rows)
        self.assertEqual(bmd.parse_z1_table(reordered), bmd.parse_z1_table(self.text))

    def test_blank_newest_row_is_skipped(self):
        width = len(self.text.split("\n", 1)[0].split(","))
        text = self.text.rstrip("\n") + "\n2026:Q3" + "," * (width - 1) + "\n"
        self.assertEqual(bmd.parse_z1_table(text)[1], "2026-06-30")

    def test_short_trailing_row_is_skipped(self):
        text = self.text.rstrip("\n") + "\n2026:Q3,123\n"
        self.assertEqual(bmd.parse_z1_table(text)[1], "2026-06-30")

    def test_missing_series_is_named(self):
        text = self.text.replace("LM793164105.Q", "LM793164999.Q")
        with self.assertRaisesRegex(bmd.SourceError,
                                    r"Z\.1: series LM793164105\.Q missing from the table header"):
            bmd.parse_z1_table(text)

    def test_non_numeric_value_names_series_and_period(self):
        text = self.text.replace("26193848", "ND")
        with self.assertRaisesRegex(bmd.SourceError,
                                    r"series LM793164105\.Q for 2026:Q2 is not a number"):
            bmd.parse_z1_table(text)

    def test_changed_units_are_refused(self):
        # Billions instead of millions: every figure a thousandth of its size.
        header, *rows = self.text.strip().split("\n")
        tail = rows[-1].split(",")
        scaled = ",".join([tail[0]] + [str(float(v) / 1000) if v.strip() else v for v in tail[1:]])
        with self.assertRaisesRegex(bmd.SourceError, "outside the sane range"):
            bmd.parse_z1_table("\n".join([header] + rows[:-1] + [scaled]))

    def test_changed_period_label_is_named(self):
        text = self.text.replace("2026:Q2", "2026-06-30")
        with self.assertRaisesRegex(bmd.SourceError, "column 'date' is '2026-06-30'"):
            bmd.parse_z1_table(text)

    def test_empty_table(self):
        with self.assertRaises(bmd.SourceError):
            bmd.parse_z1_table("")

    def test_quarter_end(self):
        self.assertEqual(
            [bmd.quarter_end(f"2026:Q{q}") for q in (1, 2, 3, 4)],
            ["2026-03-31", "2026-06-30", "2026-09-30", "2026-12-31"])
        for bad in ("2026:Q5", "2026", "Q1", "", "26:Q1"):
            with self.assertRaises(bmd.SourceError, msg=bad):
                bmd.quarter_end(bad)


class Z1Bundle(QuietCase):
    def test_known_table(self):
        self.assertEqual(bmd.read_z1_table(GOOD_Z1), sample("z1_F51_1_s.csv").decode())

    def test_renamed_table_is_found_by_its_series(self):
        bundle = z1_zip({
            "csv/L1_1_s.csv": b"date,LM000000001.Q\n2026:Q2,1\n",
            "csv/F52_1_s.csv": sample("z1_F51_1_s.csv"),
        })
        total, as_of = bmd.parse_z1_table(bmd.read_z1_table(bundle))
        self.assertEqual((total, as_of), (109244695000000.0, "2026-06-30"))
        self.assertIn("update Z1_TABLE", self.stderr.getvalue())

    def test_series_gone_from_the_bundle(self):
        bundle = z1_zip({"csv/L1_1_s.csv": b"date,LM000000001.Q\n2026:Q2,1\n"})
        with self.assertRaisesRegex(bmd.SourceError, r"Z\.1: no CSV in the bundle has both series"):
            bmd.read_z1_table(bundle)

    def test_not_a_zip(self):
        with self.assertRaisesRegex(bmd.SourceError, r"Z\.1: the download is not a zip"):
            bmd.read_z1_table(b"<html>Service Unavailable</html>")


# ── What gets published ──────────────────────────────────────────────────────

class Build(QuietCase):
    def test_all_fresh(self):
        feed, problems, outcomes = bmd.build(PREVIOUS, NOW, healthy())
        self.assertEqual(problems, [])
        self.assertEqual(outcomes, dict.fromkeys(GOLD_URLS))
        self.assertEqual(feed["generated_at"], "2026-11-12T21:00:00Z")
        self.assertEqual(feed["gold_usd_per_troy_oz"], round(SWISSQUOTE_PRICE, 2))
        self.assertEqual(feed["gold_as_of"], "2026-11-12")
        self.assertEqual(feed["us_equity_market_value_usd"], 109244695000000.0)
        self.assertEqual(feed["equity_as_of"], "2026-06-30")

    def test_shape_is_exactly_what_the_app_decodes(self):
        """MarketSnapshot.CodingKeys in the app; schema 1. Changing this set
        needs an app release, which is the thing this feed exists to avoid."""
        feed, _, _ = bmd.build(None, NOW, healthy())
        self.assertEqual(sorted(feed), sorted([
            "schema", "generated_at",
            "gold_usd_per_troy_oz", "gold_as_of", "gold_source",
            "us_equity_market_value_usd", "equity_as_of", "equity_source",
            "all_gold_ever_mined_troy_oz", "all_gold_as_of", "all_gold_source",
        ]))
        self.assertEqual(feed["schema"], 1)
        for key in ("gold_usd_per_troy_oz", "us_equity_market_value_usd",
                    "all_gold_ever_mined_troy_oz"):
            self.assertIsInstance(feed[key], float)
            self.assertGreater(feed[key], 0)
        # The app decodes generated_at with JSONDecoder's .iso8601 strategy.
        datetime.strptime(feed["generated_at"], "%Y-%m-%dT%H:%M:%SZ")

    def test_one_gold_source_down_is_not_a_problem(self):
        feed, problems, outcomes = bmd.build(PREVIOUS, NOW, healthy(**{GOLD_API_URL: DOWN}))
        self.assertEqual(problems, [])
        self.assertEqual(feed["generated_at"], "2026-11-12T21:00:00Z")
        self.assertIsNotNone(outcomes["gold-api.com"])

    def test_z1_down_still_publishes_gold_and_reports_it(self):
        feed, problems, _ = bmd.build(PREVIOUS, NOW, healthy(**{bmd.Z1_CSV_ZIP: DOWN}))
        self.assertEqual(feed["gold_usd_per_troy_oz"], round(SWISSQUOTE_PRICE, 2))
        self.assertEqual(feed["generated_at"], "2026-11-12T21:00:00Z")
        # Carried forward under its own, older date.
        self.assertEqual(feed["us_equity_market_value_usd"], PREVIOUS["us_equity_market_value_usd"])
        self.assertEqual(feed["equity_as_of"], "2026-03-31")
        self.assertEqual(len(problems), 1)

    def test_gold_down_still_publishes_equities_without_moving_the_clock(self):
        feed, problems, _ = bmd.build(PREVIOUS, NOW, healthy(**ALL_GOLD_DOWN))
        self.assertEqual(feed["us_equity_market_value_usd"], 109244695000000.0)
        self.assertEqual(feed["equity_as_of"], "2026-06-30")
        # The old price keeps its old dates. generated_at is what the app's
        # 21-day check reads, so it must not advance on a stale price.
        self.assertEqual(feed["gold_usd_per_troy_oz"], PREVIOUS["gold_usd_per_troy_oz"])
        self.assertEqual(feed["gold_as_of"], PREVIOUS["gold_as_of"])
        self.assertEqual(feed["generated_at"], PREVIOUS["generated_at"])
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("gold price:"))

    def test_everything_down_leaves_the_previous_file_as_it_was(self):
        down = healthy(**ALL_GOLD_DOWN, **{bmd.Z1_CSV_ZIP: DOWN})
        feed, problems, _ = bmd.build(PREVIOUS, NOW, down)
        self.assertEqual(feed, PREVIOUS)
        self.assertEqual(len(problems), 2)

    def test_no_previous_file_and_a_failed_source_publishes_nothing(self):
        for responses in (ALL_GOLD_DOWN, {bmd.Z1_CSV_ZIP: DOWN}):
            for previous in (None, {"schema": 2}, {"schema": 1}, "garbage"):
                feed, problems, _ = bmd.build(previous, NOW, healthy(**responses))
                self.assertIsNone(feed)
                self.assertTrue(problems)

    def test_a_bug_in_one_source_does_not_take_down_the_other(self):
        def explode(url, timeout, what):
            if url == bmd.Z1_CSV_ZIP:
                raise MemoryError("something nobody anticipated")
            return healthy()(url, timeout, what)

        feed, problems, _ = bmd.build(PREVIOUS, NOW, explode)
        self.assertEqual(feed["gold_usd_per_troy_oz"], round(SWISSQUOTE_PRICE, 2))
        self.assertIn("Z.1: unexpected MemoryError", problems[0])


class Main(QuietCase):
    def run_main(self, fake, previous=PREVIOUS, health=None, track_health=False):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = os.path.join(directory.name, "market.json")
        self.health_path = os.path.join(directory.name, "source_health.json")
        if previous is not None:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(previous, handle, indent=2)
                handle.write("\n")
        if health is not None:
            with open(self.health_path, "w", encoding="utf-8") as handle:
                json.dump(health, handle)

        # build() binds its default fetcher at definition time, so swap build
        # itself for one that uses the fake.
        real_build = bmd.build
        bmd.build = lambda prev, now: real_build(prev, now, fetcher=fake)
        self.addCleanup(setattr, bmd, "build", real_build)

        arguments = ["--out", path]
        if track_health or health is not None:
            arguments += ["--health", self.health_path]
        with contextlib.redirect_stdout(io.StringIO()):
            status = bmd.main(arguments)
        try:
            with open(path, encoding="utf-8") as handle:
                return status, handle.read()
        except FileNotFoundError:
            return status, None

    def read_health(self):
        with open(self.health_path, encoding="utf-8") as handle:
            return json.load(handle)

    def test_success_exits_zero(self):
        status, written = self.run_main(healthy())
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(written)["gold_usd_per_troy_oz"], round(SWISSQUOTE_PRICE, 2))
        self.assertTrue(written.endswith("}\n"))
        self.assertFalse(os.path.exists(self.health_path))  # only when asked for

    def test_partial_failure_exits_nonzero_and_names_the_source(self):
        status, written = self.run_main(healthy(**{bmd.Z1_CSV_ZIP: DOWN}))
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(written)["gold_usd_per_troy_oz"], round(SWISSQUOTE_PRICE, 2))
        self.assertIn("FAILED: ", self.stderr.getvalue())

    def test_total_failure_leaves_the_file_byte_identical(self):
        """So the workflow sees nothing to publish."""
        down = healthy(**ALL_GOLD_DOWN, **{bmd.Z1_CSV_ZIP: DOWN})
        before = json.dumps(PREVIOUS, indent=2) + "\n"
        status, written = self.run_main(down)
        self.assertEqual(status, 1)
        self.assertEqual(written, before)

    def test_failure_with_no_previous_file_writes_nothing(self):
        status, written = self.run_main(healthy(**ALL_GOLD_DOWN), previous=None)
        self.assertEqual(status, 1)
        self.assertIsNone(written)

    def test_health_file_is_written_when_asked_for(self):
        status, _ = self.run_main(healthy(), track_health=True)
        self.assertEqual(status, 0)
        self.assertEqual(self.read_health(), {name: {"failing_since": None, "last_error": None}
                                              for name in GOLD_URLS})
        self.assertNotIn("WARNING", self.stderr.getvalue())

    def test_a_source_failing_behind_a_working_one_exits_zero_and_is_remembered(self):
        status, written = self.run_main(healthy(**{GOLD_API_URL: DOWN}), track_health=True)
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(written)["gold_usd_per_troy_oz"], round(SWISSQUOTE_PRICE, 2))
        self.assertIsNotNone(self.read_health()["gold-api.com"]["failing_since"])
        self.assertNotIn("WARNING", self.stderr.getvalue())

    def test_a_source_failing_for_days_warns_without_failing_the_run(self):
        long_ago = {"gold-api.com": {"failing_since": "2026-01-01", "last_error": "x"}}
        status, _ = self.run_main(healthy(**{GOLD_API_URL: DOWN}), health=long_ago)
        self.assertEqual(status, 0)
        self.assertRegex(
            self.stderr.getvalue(),
            r"(?m)^WARNING: gold source gold-api\.com has failed on every run since 2026-01-01")
        self.assertEqual(self.read_health()["gold-api.com"]["failing_since"], "2026-01-01")

    def test_recovery_clears_the_record(self):
        long_ago = {"gold-api.com": {"failing_since": "2026-01-01", "last_error": "x"}}
        self.run_main(healthy(), health=long_ago)
        self.assertIsNone(self.read_health()["gold-api.com"]["failing_since"])
        self.assertNotIn("WARNING", self.stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
