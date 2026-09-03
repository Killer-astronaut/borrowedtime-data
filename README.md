# borrowedtime-data

The daily data feed behind the **Gold Standard** card in [Borrowed Time](https://apps.apple.com/app/borrowed-time), an iOS app that shows the U.S. national debt from public Treasury data.

**Feed URL:** `https://killer-astronaut.github.io/borrowedtime-data/market.json`

## What's in it

| Field | Source | Cadence |
|---|---|---|
| `gold_usd_per_troy_oz` | Spot gold | Daily |
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

- Gold price unavailable → the run fails and publishes nothing. The previous `market.json` stays up.
- Z.1 unavailable → the previous quarterly equity figures are carried forward, and the fresh gold price is still published. A source that changes four times a year shouldn't block the one that changes daily.
- Feed older than 21 days → **the app ignores it** and shows Treasury figures only. A dead job surfaces as missing rows, never as a stale number presented as current.

## Running it by hand

```
python3 build_market_data.py --out market.json
```
