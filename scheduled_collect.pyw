"""Hourly collection for Windows Task Scheduler (.pyw runs with pythonw, so no console window flashes).

Always snapshots every hub's order book; refreshes daily history when it's over 12 hours old.
Log: data/collect.log
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
(ROOT / "data").mkdir(exist_ok=True)

from eve_market.__main__ import main  # noqa: E402

main(["collect", "--auto", "--log-file", str(ROOT / "data" / "collect.log")])
