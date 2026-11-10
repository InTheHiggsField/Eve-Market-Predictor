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
python -m eve_market collect     # ~13 months of daily history for the tracked items + a live order-book snapshot
python -m eve_market backtest    # walk-forward evaluation vs. naive baselines
python -m eve_market forecast --out data/forecast.csv   # predicted 7-day % change per item
```
Options: `--horizon N` (days ahead), `--model gbm|ridge`,
`--region` / `--location` (default The Forge / Jita 4-4), `--no-snapshots`.

The tracked items live in `data_sets/tracked_items.py` as `TRACKED_ITEMS = {"in-game name": type_id}`; only those are collected. PLEX is fetched from its global market (region 19000001), everything else from Jita. Data is stored in `data/market.db` (SQLite, git-ignored);
re-running `collect` upserts history and appends a new order snapshot, so run it daily (e.g. Task Scheduler)
to build up bid/ask history that ESI does not keep for you.

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
- `tests/` - `python -m pytest`
