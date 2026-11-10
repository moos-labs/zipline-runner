# zipline-runner

Run [Zipline-reloaded](https://github.com/stefan-jansen/zipline-reloaded)
backtests in GitHub Actions, following the moos-labs runner pattern:
nothing is hardcoded in the workflow — the commands to execute live in
the `COMMANDS` repository Actions variable and results are collected
from `results/` as a workflow artifact.

## Pattern

- `.github/workflows/run.yml` — trigger `workflow_dispatch` **only**;
  Python 3.11; `pip install -r requirements.txt`; a step named
  **Run commands** whose run line is exactly `run: ${{ vars.COMMANDS }}`;
  a final `actions/upload-artifact@v4` step (`name: results`,
  `path: results/`, `if: always()`).
- Repo Actions variable `COMMANDS`:

  ```
  mkdir -p results
  set -o pipefail; python scripts/zipline_pilot.py 2>&1 | tee results/run.log
  ```

  `set -o pipefail` matters: without it the `tee` at the end of the pipe
  would mask a failing script and the run would show green.
- `scripts/` holds generic scripts; parameters (symbol, windows,
  capital) are constants at the top of the script, not baked into the
  workflow.

## Pilot: `scripts/zipline_pilot.py`

1. Fetches AAPL daily OHLC for a ~3-year window from Yahoo's chart API
   directly (`query1.finance.yahoo.com/v8/finance/chart/AAPL`,
   `period1`/`period2`/`interval=1d`, via `requests`). No yfinance.
2. Writes it in Zipline's **csvdir** bundle format:
   `data/csvdir/daily/AAPL.csv` with columns
   `date,open,high,low,close,volume,dividend,split`
   (raw, unadjusted prices; dividend amounts and split ratios filled
   from the chart API `events=div,split`, defaults `0.0` / `1.0`).
3. Ingests bundle `csvdir` — the built-in bundle registered by
   `zipline.data.bundles.csvdir`. The loader reads the directory from
   the `CSVDIR` environment variable, expects `<csvdir>/daily/<SYMBOL>.csv`
   (and/or `minute/`), and **derives the asset metadata itself**:
   `start_date` / `end_date` from the CSV, `auto_close_date` = last
   date + 1 day, `symbol` from the file name, exchange hardcoded to
   `CSVDIR` (aliased to the NYSE calendar). There is no separate
   metadata file to write.
   The script calls the ingest API (`zipline.data.bundles.ingest`),
   the equivalent of `CSVDIR=... zipline ingest -b csvdir`.
4. Runs a dual-SMA **50/200** crossover (long 100% when fast > slow,
   flat otherwise) with `run_algorithm(..., bundle='csvdir')`,
   capital base 100000. The backtest starts after 210 sessions of
   warmup so the 200-day SMA is defined from the first traded day.
5. Writes `results/zipline_result.md`: window, and the final portfolio
   value / total return taken **exactly** from the returned perf
   DataFrame (`perf['portfolio_value'].iloc[-1]`). If any step blocks,
   the file records the exact step, command and error instead, and the
   script exits non-zero.

## Honest notes

- `zipline-reloaded` is pinned to `3.2.1` in `requirements.txt`
  (the version whose csvdir loader source this pilot was checked
  against; it supports Python 3.11 and caps pandas < 3.0).
- Yahoo's chart API intermittently returns **429 from GitHub runner
  IPs**. That is a data-fetch failure, not a Zipline failure; the
  result file will say so verbatim and one re-dispatch usually fixes it.
- Prices are raw OHLC; Zipline applies the dividend/split adjustments
  from the same CSV when computing adjusted `price` history. AAPL has
  paid quarterly dividends in the window (filled into the `dividend`
  column); its last split (4-for-1, 2020) predates the window, so the
  `split` column is all `1.0` here.
- Daily-data only: no minute bars are written, and the algorithm runs
  at `data_frequency='daily'`.
- This is a smoke-test pilot for the ingest → backtest path, not
  investment research: one symbol, no costs model beyond Zipline's
  defaults, no benchmark comparison.
