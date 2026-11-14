## Eve-Market-Predictor
This application will query the ESI API for market data and use said data to make predictions about which products will rise in value or fall.

### Setup (Windows)
```
py -3.13 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### Usage
```
python -m eve_market collect     # ~13 months of daily history for every tracked item + a live order-book snapshot
python -m eve_market backfill    # one-off: daily history back to 2003 from the EVE Ref archive (~resumable)
python -m eve_market backtest    # walk-forward evaluation vs. naive baselines
python -m eve_market health      # market health score and warning flags per item
python -m eve_market forecast --out data/forecast.csv   # predicted 7-day % change per item
python -m eve_market dashboard   # local website at http://127.0.0.1:5000
python -m eve_market site        # static copy of the dashboard in site/ (for GitHub Pages)
```

The dashboard has three pages: **Market data** (ticker ribbon, sortable table, price/volume charts),
**Predictions** (forecast graphs with past out-of-sample predictions and backtest scores), and **About**
(how the data and models work and how to reproduce them).
Options: `--market Jita Amarr Dodixie Hek Rens Zarzakh` (collect/backfill default to all hubs; backtest/forecast take one, default Jita), `--list core|commodities|all` (collect defaults to all, backtest/forecast to core), `--horizon N` (days ahead), `--model gbm|ridge`,
`--no-snapshots`.

The tracked items live in `data_sets/tracked_items.py` as `{"in-game name": type_id}` dictionaries: `TRACKED_ITEMS` (core: PLEX, injectors, ships) and `COMMODITY_ITEMS` (minerals, compressed ore, ship components). Only those are collected. Trade hubs live in `data_sets/markets.py` (Jita, Amarr, Dodixie, Hek, Rens, and null-sec Zarzakh): price history comes from each hub's region, bid/ask snapshots from its main station. PLEX history comes from its global market (region 19000001). Data is stored in `data/market.db` (SQLite, git-ignored);
re-running `collect` upserts history and appends a new order snapshot, so run it daily (e.g. Task Scheduler)
to build up bid/ask history that ESI does not keep for you.

### Public website (GitHub Pages)
GitHub Pages only serves static files, so `python -m eve_market site` renders a static copy of the dashboard
into `site/`: the four pages, plus every answer the dashboard's API can give saved as JSON under `site/data/`
(one file per hub, item, list, model and horizon). Preview it with `python -m http.server -d site 8000`.

`.github/workflows/pages.yml` keeps the public copy current. Every 6 hours it backfills any missing history
(from `BACKFILL_START`, default 2022-01-01), runs `collect` against the public ESI endpoints, rebuilds the
site and deploys it. It needs no secrets. Its database lives in the Actions cache, separate from your local
`data/market.db`, so the public station charts start from its own snapshots (4 a day rather than hourly).

To turn it on: make the repository public (Pages on a free account needs that), then **Settings > Pages >
Build and deployment > Source: GitHub Actions**, then **Actions > Publish dashboard > Run workflow**. The first
run takes longest because of the backfill. The site appears at
`https://<user>.github.io/Eve-Market-Predictor/`. GitHub pauses scheduled workflows after 60 days with no
commits, so push something (or re-enable it on the Actions tab) if updates stop.

### Station prices (hourly snapshots)
ESI only has region-level history, so station prices are built from order-book snapshots. A Windows scheduled
task, **EVE Market Collect**, runs `scheduled_collect.pyw` every hour: it snapshots every hub station and refreshes
daily history when it's over 12 hours old (log: `data/collect.log`). The dashboard shows the station mid price and
its premium over the region average. After ~45 days of snapshots a station-level model takes over the station
forecasts (`.\eve backtest --station --market Jita`). To remove the task:
`Unregister-ScheduledTask -TaskName "EVE Market Collect"`.

### Character login (citadel markets)
Player-owned citadels (e.g. low-sec trade Keepstars) need a logged-in character:
1. Create an app at https://developers.eveonline.com/applications with callback `http://localhost:8765/callback`
   and scopes `esi-markets.structure_markets.v1`, `esi-universe.read_structures.v1`.
2. Copy `.env.example` to `.env` and fill in the Client ID and Secret (`.env` is git-ignored; never commit it).
3. `python -m eve_market login` opens EVE's login page; the refresh token is saved to `data/esi_token.json`.
4. `python -m eve_market hubs --security low` ranks where the tracked items trade, with citadel names.
   Add a citadel to `data_sets/markets.py` (its structure ID as `station_id`) to track it; `collect` then
   reads its full market with the saved login.

### How the baseline works
- **Data**: ESI `/markets/{region}/history` (daily average/high/low/volume) and `/markets/{region}/orders`.
- **Target**: log return of the daily average price over the next `horizon` days.
- **Features**: past returns (1/3/7/14/30d), rolling volatility, distance from 7/30d moving averages,
  daily high-low range, volume z-score, day of week. One model pooled across all items.
- **Models**: Ridge regression and gradient boosting, compared against "no change" and "momentum".
- **Backtest**: 5 walk-forward folds of 21 days; training data ends `horizon` days before each fold to avoid leakage.

### Layout
- `eve_market/esi.py` - ESI client (User-Agent, retries, error-limit backoff, pagination)
- `eve_market/storage.py` - SQLite schema and helpers
- `eve_market/collect.py` - collection job
- `eve_market/forecast.py` - features, backtest, predictions
- `eve_market/health.py` - market health metrics (liquidity, spread, depth, impact), score and flags
- `eve_market/dashboard/` - Flask + Chart.js dashboard
- `eve_market/site.py` - static export of the dashboard for GitHub Pages
- `.github/workflows/pages.yml` - scheduled collect, build and deploy to GitHub Pages
- `tests/` - `python -m pytest`
