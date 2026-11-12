# borrowedtime-data

The daily data feed behind the **Gold Standard** card in [Borrowed Time](https://apps.apple.com/app/borrowed-time), an iOS app that shows the U.S. national debt from public Treasury data.

**Feed URL:** `https://killer-astronaut.github.io/borrowedtime-data/market.json`

## What's in it

| Field | Source | Cadence |
|---|---|---|
| `gold_usd_per_troy_oz` | Spot gold (gold-api.com, falling back to Swissquote's public quotes) | Daily |
| `us_equity_market_value_usd` | Federal Reserve Z.1, corporate equities at market value (issuer side, series `LM103164105` + `LM793164105`) | Quarterly, ~75-day lag |
| `all_gold_ever_mined_troy_oz` | World Gold Council above-ground stocks | Reviewed annually |

Everything here is either public-domain U.S. government data or a published aggregate. The app reads this one file and nothing else.

## Why this repo exists

The app can't call a gold price API directly. Spot gold is a licensed benchmark — LBMA owns it, which is why FRED dropped its gold series in 2023 — and the free tiers that do expose a price are metered per key. Pointing every install at one free-tier key is how the key gets revoked.

So: one fetch a day into a CDN-cached static file. No key ships in the app, every install reads the same cached file, and a dead upstream source is fixed by editing `build_market_data.py` here rather than shipping an App Store build.

## How it runs

`.github/workflows/refresh.yml` runs `build_market_data.py` daily, validates the output, and commits `market.json` only if the numbers changed. GitHub Pages serves it from the default branch.

The generator is stdlib-only — no dependencies to rot.

**Failure behaviour is deliberate:**

- Each source fails on its own. Whatever was fetched is published; whatever was not keeps its previous value *and its previous "as of" date*.
- Gold has two unrelated keyless sources, tried in order (`GOLD_SOURCES` in the script). A price that has moved more than 20% from the last published one is only believed if both agree.
- `generated_at` advances only when the gold price is fresh. It is the one field the app's staleness check reads, so a run that could not get gold can publish a new quarter's equities but cannot make an old price look new.
- Any failed source fails the run — after a second attempt five minutes later — and opens (or comments on) a `feed-failure` issue that quotes which source and which field. The next clean run closes it.
- Feed older than 21 days → **the app ignores it** and shows Treasury figures only. A dead job surfaces as missing rows, never as a stale number presented as current.

## When a source changes

The issue names the source and the field. Then:

1. Save the source's new response over its sample in `tests/samples/`.
2. `python3 -m unittest discover -s tests` — the failing test shows what the parser no longer finds.
3. Fix the parser in `build_market_data.py` (or add a replacement source to `GOLD_SOURCES`) until the tests pass.

The app decodes exactly the eleven fields in `market.json` and refuses any `schema` other than 1, so the file's shape is not something to change here; there is a test holding it still.

## Running it by hand

```
python3 build_market_data.py --out market.json
```

Tests (stdlib only, no network):

```
python3 -m unittest discover -s tests
```
