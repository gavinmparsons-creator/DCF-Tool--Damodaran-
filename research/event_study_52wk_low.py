"""
research/event_study_52wk_low.py — does a stock that sat at its 52-week
low for ~3 months and then moved up X% keep outperforming afterwards?

A historical event study, run BEFORE building any scanner around the idea.
Everything below was fixed before looking at a single result; changing a
threshold after seeing results would turn this into a search for a pattern
that worked by chance.

PRE-REGISTERED RULES
  Universe   Nasdaq + NYSE listings (SEC's exchange list), one primary
             ticker per company, current market cap >= $2B (SEC-reported
             shares outstanding x latest close).
  Data       Daily split/dividend-adjusted closes, ~12 years (Yahoo).
  Base       In the 63 trading days (~90 calendar days) ending 21 trading
             days before the event: at least 50 closes within 10% of the
             base's low, and that low within 2% of the 52-week low as of
             the end of the base — i.e. ~3 months sitting at the lows.
  Trigger    Within the next 21 trading days, the FIRST close at least X%
             above the base low. X = 10% and 20%, both reported, no others.
             One event per stock per 126 trading days (no overlaps).
  Outcome    Return over the next 21 / 63 / 126 / 252 trading days, minus
             the equal-weighted average return of every universe stock
             over the same days ("excess return").
  Test       Events are averaged within each calendar month first, then
             across months (t-stat across months), because events cluster
             (e.g. hundreds in the March 2020 rebound) and aren't
             independent. A pattern counts only if it has the same sign and
             |t| > 2 in BOTH halves (2015-2020 and 2021-2026), and survives
             dropping the Feb-Jun 2020 crash-and-rebound events.

KNOWN BIASES (not fixable with free data — interpret accordingly)
  * Survivorship / look-ahead: the universe is today's listed companies
    worth >= $2B today. A stock that bottomed in 2018 is in the sample
    partly BECAUSE it later recovered; ones that kept falling, delisted or
    went bankrupt are missing. This flatters "buy near the low". Comparing
    against the same universe on the same dates (rather than the S&P 500)
    shares the bias across both sides, but doesn't remove it.
  * ADR share counts are in ordinary shares, which can overstate an ADR's
    market cap and admit a few sub-$2B ADRs. Small effect on the study.

USAGE (from the tool's folder):
    python3 research/event_study_52wk_low.py            # builds/uses cached data in research/data/
    python3 research/event_study_52wk_low.py --rebuild  # re-download the universe and prices
"""

import argparse
import math
import os
import sys
import warnings
from datetime import date

warnings.filterwarnings("ignore")  # yfinance/pandas deprecation chatter

import numpy as np
import pandas as pd
import yfinance as yf

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import valuation  # noqa: E402  — reuses its EDGAR fetch (with the SEC User-Agent)

DATA_DIR = os.path.join(HERE, "data")
UNIVERSE_PATH = os.path.join(DATA_DIR, "universe.csv")
CLOSES_PATH = os.path.join(DATA_DIR, "closes.pkl")
REPORT_PATH = os.path.join(HERE, "results_52wk_low.md")

MIN_MARKET_CAP = 2e9
HISTORY_START = "2014-10-01"
BASE_DAYS, BASE_MIN_NEAR, BASE_NEAR_PCT = 63, 50, 0.10
BASE_AT_52WK_LOW_PCT = 0.02
MOVE_DAYS = 21
TRIGGERS = (0.10, 0.20)
HORIZONS = (21, 63, 126, 252)
COOLDOWN = 126
CRASH_WINDOW = ("2020-02-01", "2020-06-30")
SPLIT_DATE = "2021-01-01"


# ---------------------------------------------------------------- universe

def build_universe():
    """Nasdaq/NYSE companies worth >= $2B, one ticker each."""
    exchange = valuation.fetch_json("https://www.sec.gov/files/company_tickers_exchange.json")
    fields = exchange["fields"]
    rows = [dict(zip(fields, r)) for r in exchange["data"]]
    # One ticker per company: SEC lists a company's primary listing first;
    # later rows for the same CIK are other classes, preferreds or warrants.
    primary = {}
    for r in rows:
        if r["exchange"] in ("Nasdaq", "NYSE") and r["cik"] not in primary:
            primary[r["cik"]] = r
    # Shares outstanding for every filer at once (SEC "frames"): the latest
    # reported cover-page count per company across recent quarters.
    shares = {}
    for frame in ("CY2025Q1I", "CY2025Q2I", "CY2025Q3I", "CY2025Q4I", "CY2026Q1I", "CY2026Q2I", "CY2026Q3I"):
        data = valuation.fetch_json_or_none(
            f"https://data.sec.gov/api/xbrl/frames/dei/EntityCommonStockSharesOutstanding/shares/{frame}.json")
        for d in (data or {}).get("data", []):
            if d["cik"] not in shares or d["end"] >= shares[d["cik"]][1]:
                shares[d["cik"]] = (d["val"], d["end"])
    candidates = [r for r in primary.values() if r["cik"] in shares]
    tickers = [r["ticker"] for r in candidates]
    print(f"  {len(primary)} Nasdaq/NYSE companies; {len(candidates)} with SEC share counts — pricing them...")
    last = {}
    for i in range(0, len(tickers), 200):
        chunk = tickers[i:i + 200]
        px = yf.download(chunk, period="5d", interval="1d", auto_adjust=True, progress=False, threads=True)["Close"]
        if isinstance(px, pd.Series):
            px = px.to_frame(chunk[0])
        for t in px.columns:
            series = px[t].dropna()
            if len(series):
                last[t] = float(series.iloc[-1])
    out = []
    for r in candidates:
        price = last.get(r["ticker"])
        if price:
            cap = price * shares[r["cik"]][0]
            if cap >= MIN_MARKET_CAP:
                out.append({"ticker": r["ticker"], "cik": r["cik"], "name": r["name"],
                            "exchange": r["exchange"], "market_cap": cap})
    universe = pd.DataFrame(out).sort_values("market_cap", ascending=False)
    os.makedirs(DATA_DIR, exist_ok=True)
    universe.to_csv(UNIVERSE_PATH, index=False)
    return universe


def download_closes(tickers):
    frames = []
    for i in range(0, len(tickers), 200):
        chunk = list(tickers[i:i + 200])
        print(f"  prices {i + 1}-{i + len(chunk)} of {len(tickers)}", flush=True)
        px = yf.download(chunk, start=HISTORY_START, interval="1d", auto_adjust=True,
                         progress=False, threads=True)["Close"]
        if isinstance(px, pd.Series):
            px = px.to_frame(chunk[0])
        frames.append(px)
    closes = pd.concat(frames, axis=1).sort_index()
    closes = closes.loc[:, ~closes.columns.duplicated()].dropna(how="all")
    closes.to_pickle(CLOSES_PATH)
    return closes


# ------------------------------------------------------------------ events

def find_events(closes, trigger):
    """
    (ticker index, event day index, base low) for every event, applying the
    pre-registered rules. Vectorized per stock with sliding windows; only
    the cooldown is applied in a loop, over the (few) qualifying days.
    """
    from numpy.lib.stride_tricks import sliding_window_view
    values = closes.to_numpy()
    low252 = closes.rolling(252, min_periods=252).min().to_numpy()
    n_days, n_tickers = values.shape
    events = []
    first = BASE_DAYS + MOVE_DAYS  # first day t with a full base window [t-84, t-21)
    if n_days <= first:
        return events
    for j in range(n_tickers):
        col = values[:, j]
        # windows[k] = closes on days k .. k+62, i.e. the base for event day t = k + 84
        windows = sliding_window_view(col, BASE_DAYS)[:n_days - first]
        t_idx = np.arange(first, n_days)
        with np.errstate(invalid="ignore"):
            complete = np.isfinite(windows).all(axis=1)
            base_low = windows.min(axis=1)
            near = (windows <= base_low[:, None] * (1 + BASE_NEAR_PCT)).sum(axis=1)
            low_at_base_end = low252[t_idx - MOVE_DAYS - 1, j]
            at_52wk_low = base_low <= low_at_base_end * (1 + BASE_AT_52WK_LOW_PCT)
            level = base_low * (1 + trigger)
            # highest close in the move window before today (days t-21 .. t-1)
            prior_max = pd.Series(col).rolling(MOVE_DAYS, min_periods=MOVE_DAYS).max().shift(1).to_numpy()[t_idx]
            hit = (complete & np.isfinite(low_at_base_end) & at_52wk_low & (near >= BASE_MIN_NEAR)
                   & (col[t_idx] >= level) & (prior_max < level))
        last_event = -10 ** 9
        for k in np.flatnonzero(hit):
            t = int(t_idx[k])
            if t - last_event >= COOLDOWN:
                events.append((j, t, float(base_low[k])))
                last_event = t
    return events


def excess_returns(closes, events):
    """Forward and excess returns for each event at each horizon."""
    values = closes.to_numpy()
    dates = closes.index
    universe_fwd = {h: (closes.shift(-h) / closes - 1).mean(axis=1, skipna=True).to_numpy() for h in HORIZONS}
    rows = []
    for j, t, base_low in events:
        row = {"ticker": closes.columns[j], "date": dates[t]}
        for h in HORIZONS:
            if t + h < len(dates) and np.isfinite(values[t + h, j]):
                own = values[t + h, j] / values[t, j] - 1
                row[f"ret_{h}"], row[f"excess_{h}"] = own, own - universe_fwd[h][t]
        rows.append(row)
    return pd.DataFrame(rows)


def month_clustered(df, column):
    """Mean of monthly-average excess returns, its t-stat, and counts."""
    d = df.dropna(subset=[column])
    if d.empty:
        return None
    monthly = d.groupby(d["date"].dt.to_period("M"))[column].mean()
    n = len(monthly)
    mean = monthly.mean()
    t = mean / (monthly.std(ddof=1) / math.sqrt(n)) if n > 1 and monthly.std(ddof=1) > 0 else float("nan")
    return {"events": len(d), "months": n, "mean": mean, "t": t, "hit": (d[column] > 0).mean()}


# ------------------------------------------------------------------ report

def summarize(df, label):
    lines = [f"\n### {label}\n", "| Horizon | Events | Months | Avg excess return | t-stat | % beating universe |",
             "|---|---|---|---|---|---|"]
    for h in HORIZONS:
        s = month_clustered(df, f"excess_{h}")
        if s:
            lines.append(f"| {h} trading days | {s['events']} | {s['months']} | {s['mean']:+.2%} | "
                         f"{s['t']:+.2f} | {s['hit']:.0%} |")
    return "\n".join(lines)


def verdict(df):
    """Pre-registered pass/fail per horizon."""
    first = df[df["date"] < SPLIT_DATE]
    second = df[df["date"] >= SPLIT_DATE]
    no_crash = df[~df["date"].between(*CRASH_WINDOW)]
    out = []
    for h in HORIZONS:
        a, b, c = (month_clustered(x, f"excess_{h}") for x in (first, second, no_crash))
        if not (a and b and c):
            out.append(f"- {h} days: not enough events to judge")
            continue
        same_sign = math.copysign(1, a["mean"]) == math.copysign(1, b["mean"]) == math.copysign(1, c["mean"])
        passes = same_sign and abs(a["t"]) > 2 and abs(b["t"]) > 2 and abs(c["t"]) > 2
        direction = "OUTperform" if a["mean"] > 0 else "UNDERperform"
        out.append(f"- {h} days: {f'CONSISTENT — events {direction} the universe' if passes else 'not consistent'} "
                   f"(2015-20 {a['mean']:+.2%} t={a['t']:+.1f}; 2021-26 {b['mean']:+.2%} t={b['t']:+.1f}; "
                   f"ex-2020-crash {c['mean']:+.2%} t={c['t']:+.1f})")
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description="52-week-low base-and-breakout event study.")
    parser.add_argument("--rebuild", action="store_true", help="re-download the universe and prices")
    args = parser.parse_args()

    if args.rebuild or not os.path.exists(UNIVERSE_PATH):
        print("Building universe...")
        universe = build_universe()
    else:
        universe = pd.read_csv(UNIVERSE_PATH)
    print(f"Universe: {len(universe)} companies >= ${MIN_MARKET_CAP / 1e9:.0f}B")

    if args.rebuild or not os.path.exists(CLOSES_PATH):
        print("Downloading price history...")
        closes = download_closes(universe["ticker"].tolist())
    else:
        closes = pd.read_pickle(CLOSES_PATH)
    closes = closes.loc[:, closes.notna().sum() > 300]
    print(f"Prices: {closes.shape[1]} tickers, {closes.index.min().date()} to {closes.index.max().date()}")

    report = [f"# 52-week-low base-and-breakout event study\n",
              f"Run {date.today()} on {closes.shape[1]} Nasdaq/NYSE stocks worth >= $2B today, "
              f"{closes.index.min().date()} to {closes.index.max().date()}. "
              "Rules, test and known biases: see the docstring of research/event_study_52wk_low.py "
              "(fixed before any results were seen). Excess return = the stock's return minus the "
              "equal-weighted average of all universe stocks over the same days. Not investment advice."]
    for trigger in TRIGGERS:
        print(f"Finding events for a {trigger:.0%} move off a 3-month base at the 52-week low...")
        df = excess_returns(closes, find_events(closes, trigger))
        report.append(f"\n## Trigger: first close {trigger:.0%} above the base low\n")
        report.append(f"**Pre-registered verdict** (same sign and |t| > 2 in 2015-20, 2021-26, and excluding "
                      f"Feb-Jun 2020):\n\n{verdict(df)}")
        report.append(summarize(df, "All events"))
        report.append(summarize(df[df["date"] < SPLIT_DATE], "2015-2020"))
        report.append(summarize(df[df["date"] >= SPLIT_DATE], "2021-2026"))
        report.append(summarize(df[~df["date"].between(*CRASH_WINDOW)], "Excluding the Feb-Jun 2020 crash and rebound"))
    text = "\n".join(report) + "\n"
    with open(REPORT_PATH, "w") as f:
        f.write(text)
    print(text)


if __name__ == "__main__":
    main()
