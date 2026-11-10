#!/usr/bin/env python3
"""Zipline-reloaded pilot: AAPL dual-SMA backtest on a csvdir bundle.

Steps:
  1. Fetch AAPL daily OHLC via the Yahoo chart API (direct, no yfinance)
     for a ~3-year window ending today (UTC).
  2. Write the data in zipline's csvdir bundle format:
       data/csvdir/daily/AAPL.csv
     with columns date,open,high,low,close,volume,dividend,split.
     The csvdir loader (zipline.data.bundles.csvdir, bundle name 'csvdir')
     derives the asset metadata itself (start_date, end_date,
     auto_close_date, symbol from the file name, exchange 'CSVDIR')
     and reads the directory from the CSVDIR environment variable.
  3. Ingest with the zipline bundles API, equivalent to the CLI command
       CSVDIR=<data/csvdir> zipline ingest -b csvdir
  4. Run a dual-SMA (50/200) algorithm with run_algorithm on bundle
     'csvdir', starting after enough sessions exist for the 200-day SMA.
  5. Write results/zipline_result.md with the final portfolio value and
     total return taken exactly from the perf DataFrame. On any failure,
     write the exact step, command and error to the same file and exit 1.
"""
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path.cwd()
RESULTS = ROOT / "results"
RESULTS.mkdir(parents=True, exist_ok=True)
CSV_ROOT = ROOT / "data" / "csvdir"
DAILY_DIR = CSV_ROOT / "daily"

# Hermetic zipline root inside the workspace; set before importing zipline.
os.environ["ZIPLINE_ROOT"] = str(ROOT / ".zipline-root")
os.environ["CSVDIR"] = str(CSV_ROOT)

SYMBOL = "AAPL"
CAPITAL_BASE = 100000.0
FAST_WINDOW = 50
SLOW_WINDOW = 200
WARMUP_SESSIONS = SLOW_WINDOW + 10

RESULT_FILE = RESULTS / "zipline_result.md"


def write_failure(step, command, exc):
    tb = traceback.format_exc()
    RESULT_FILE.write_text(
        "# Zipline-reloaded pilot - FAILED\n\n"
        f"- Symbol: {SYMBOL}\n"
        f"- Failed step: {step}\n"
        f"- Command: `{command}`\n"
        f"- Error type: {type(exc).__name__}\n"
        f"- Error: {exc}\n\n"
        "## Traceback\n\n```\n" + tb + "```\n"
    )
    print(f"FAILED at step {step}: {exc}", flush=True)
    print(tb, flush=True)


def main():
    step = "fetch AAPL daily OHLC from the Yahoo chart API"
    command = "python scripts/zipline_pilot.py"
    try:
        # ---- Step 1: fetch -------------------------------------------------
        import requests

        end_exclusive = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        start_dt = end_exclusive - timedelta(days=3 * 365)
        period1 = int(start_dt.timestamp())
        period2 = int(end_exclusive.timestamp())
        url = (
            "https://query1.finance.yahoo.com/v8/finance/chart/AAPL"
            f"?period1={period1}&period2={period2}&interval=1d&events=div,split"
        )
        print(f"GET {url}", flush=True)
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (zipline-runner pilot)"},
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        chart = payload["chart"]["result"][0]
        timestamps = chart["timestamp"]
        quote = chart["indicators"]["quote"][0]
        events = chart.get("events") or {}

        et = ZoneInfo("America/New_York")

        def to_et_date(ts):
            return (
                datetime.fromtimestamp(ts, tz=timezone.utc)
                .astimezone(et)
                .date()
            )

        dividends = {}
        for ev in (events.get("dividends") or {}).values():
            dividends[to_et_date(ev["date"])] = float(ev["amount"])
        splits = {}
        for ev in (events.get("splits") or {}).values():
            # Yahoo gives numerator/denominator (e.g. 4/1 for a 4-for-1
            # split). The csvdir loader stores ratio = 1 / split_value,
            # so the CSV column holds numerator/denominator.
            splits[to_et_date(ev["date"])] = (
                float(ev["numerator"]) / float(ev["denominator"])
            )

        import pandas as pd

        rows = []
        for i, ts in enumerate(timestamps):
            close = quote["close"][i]
            if close is None:
                continue
            day = to_et_date(ts)
            rows.append(
                {
                    "date": day.isoformat(),
                    "open": quote["open"][i],
                    "high": quote["high"][i],
                    "low": quote["low"][i],
                    "close": close,
                    "volume": quote["volume"][i],
                    "dividend": dividends.get(day, 0.0),
                    "split": splits.get(day, 1.0),
                }
            )
        df = pd.DataFrame(rows)
        if df.empty:
            raise ValueError("Yahoo chart API returned no usable rows")
        first_session = df["date"].iloc[0]
        last_session = df["date"].iloc[-1]
        print(
            f"Fetched {len(df)} sessions: {first_session} .. {last_session}",
            flush=True,
        )

        # ---- Step 2: write csvdir bundle files -----------------------------
        step = "write csvdir bundle files (data/csvdir/daily/AAPL.csv)"
        DAILY_DIR.mkdir(parents=True, exist_ok=True)
        csv_path = DAILY_DIR / f"{SYMBOL}.csv"
        df.to_csv(csv_path, index=False)
        print(f"Wrote {csv_path}", flush=True)

        # ---- Step 3: ingest -------------------------------------------------
        step = "ingest csvdir bundle"
        command = (
            "CSVDIR=data/csvdir zipline ingest -b csvdir "
            "(via zipline.data.bundles.ingest API)"
        )
        from zipline.data import bundles

        bundles.ingest("csvdir", environ=os.environ, show_progress=True)
        print("Ingest succeeded for bundle 'csvdir'", flush=True)
        command = "python scripts/zipline_pilot.py"

        # ---- Step 4: run the dual-SMA algorithm -----------------------------
        step = "run_algorithm dual-SMA on bundle 'csvdir'"
        if len(df) <= WARMUP_SESSIONS + 20:
            raise ValueError(
                f"only {len(df)} sessions fetched; need more than "
                f"{WARMUP_SESSIONS + 20} for a {SLOW_WINDOW}-day SMA backtest"
            )
        backtest_start = df["date"].iloc[WARMUP_SESSIONS]
        backtest_end = last_session

        from zipline import run_algorithm
        from zipline.api import (
            order_target_percent,
            record,
            set_benchmark,
            symbol,
        )

        def initialize(context):
            context.asset = symbol(SYMBOL)
            set_benchmark(symbol(SYMBOL))
            context.invested = False

        def handle_data(context, data):
            prices = data.history(context.asset, "price", SLOW_WINDOW, "1d")
            if prices is None or len(prices.dropna()) < SLOW_WINDOW:
                return
            fast_sma = prices[-FAST_WINDOW:].mean()
            slow_sma = prices.mean()
            record(sma_fast=float(fast_sma), sma_slow=float(slow_sma))
            if fast_sma > slow_sma and not context.invested:
                order_target_percent(context.asset, 1.0)
                context.invested = True
            elif fast_sma < slow_sma and context.invested:
                order_target_percent(context.asset, 0.0)
                context.invested = False

        perf = run_algorithm(
            start=pd.Timestamp(backtest_start, tz="UTC"),
            end=pd.Timestamp(backtest_end, tz="UTC"),
            initialize=initialize,
            handle_data=handle_data,
            capital_base=CAPITAL_BASE,
            data_frequency="daily",
            bundle="csvdir",
            environ=os.environ,
        )

        # ---- Step 5: report exactly from the perf DataFrame -----------------
        step = "write results from perf DataFrame"
        final_value = float(perf["portfolio_value"].iloc[-1])
        total_return = final_value / CAPITAL_BASE - 1.0
        lines = [
            "# Zipline-reloaded pilot - AAPL dual-SMA (50/200)",
            "",
            f"- Tool: zipline-reloaded (pip `zipline-reloaded`), bundle `csvdir`",
            f"- Data source: Yahoo chart API (direct via requests, no yfinance): {url}",
            f"- Requested window (period1..period2, UTC dates): "
            f"{start_dt.date().isoformat()} .. {end_exclusive.date().isoformat()}",
            f"- Sessions fetched: {len(df)} ({first_session} .. {last_session})",
            f"- csvdir file: data/csvdir/daily/{SYMBOL}.csv "
            "(columns date,open,high,low,close,volume,dividend,split; "
            "loader derives start/end/auto-close metadata and exchange CSVDIR)",
            "- Ingest: SUCCEEDED (`zipline ingest -b csvdir` equivalent, "
            "via zipline.data.bundles.ingest)",
            f"- Backtest window: {backtest_start} .. {backtest_end} "
            f"(started after {WARMUP_SESSIONS} warmup sessions)",
            f"- Strategy: dual SMA {FAST_WINDOW}/{SLOW_WINDOW} on {SYMBOL}, "
            "long 100% when fast > slow, flat otherwise",
            f"- Capital base: {CAPITAL_BASE}",
            f"- Final portfolio value (perf['portfolio_value'].iloc[-1]): {final_value!r}",
            f"- Total return (final / capital_base - 1): {total_return!r} "
            f"({total_return * 100:.4f}%)",
            "",
        ]
        RESULT_FILE.write_text("\n".join(lines))
        print("\n".join(lines), flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001 - report exact blocker, then fail
        write_failure(step, command, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
