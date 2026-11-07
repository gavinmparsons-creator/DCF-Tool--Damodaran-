"""
journal.py — a decision journal: every buy, pass or watch, scored against
simply owning the market.

The only honest test of whether stock picking works for you is a record
made at the time — the price, what you believed, and what the model said —
checked later against an index fund over the same days. Passes count too:
a stock you passed on that then doubled is as informative as a winner.

USAGE (from the dcf-tool folder):
    python3 journal.py add NPB buy "MPP lender at 0.9x book earning 14%; margin compression is the risk"
    python3 journal.py add KO pass "Price needs 30%+ growth; record is 5%" --value 41.67
    python3 journal.py show                 # every entry vs SPY since its date
    python3 journal.py close NPB "Thesis broke: brokered funding cost up"

The model value defaults to the base value in the latest report for that
ticker (reports/<TICKER>_<date>.md), if there is one. Entries live in
data/journal.csv — on your machine only, never in git. Not investment advice.
"""

import argparse
import csv
import glob
import os
import re
import sys
import warnings
from datetime import date

warnings.filterwarnings("ignore")  # yfinance/pandas deprecation chatter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
JOURNAL_PATH = os.path.join(HERE, "data", "journal.csv")
FIELDS = ["date", "ticker", "action", "price", "model_value", "thesis", "closed", "close_note"]
BENCHMARK = "SPY"


def load():
    try:
        with open(JOURNAL_PATH, newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def save(rows):
    os.makedirs(os.path.dirname(JOURNAL_PATH), exist_ok=True)
    with open(JOURNAL_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def latest_report_value(ticker):
    """Base value per share from the newest report for `ticker`, or None."""
    paths = sorted(glob.glob(os.path.join(HERE, "reports", f"{ticker.upper()}_*.md")))
    if not paths:
        return None
    text = open(paths[-1]).read()
    m = (re.search(r"\*\*Base \$([\d,]+\.\d+)\*\*", text) or re.search(r"base \$([\d,]+\.\d+)", text))
    return float(m.group(1).replace(",", "")) if m else None


def closes(tickers, start):
    """{ticker: pandas Series of daily closes from `start`} via Yahoo."""
    import yfinance as yf
    data = yf.download(sorted(set(tickers)), start=start, progress=False, auto_adjust=True)["Close"]
    if hasattr(data, "columns"):
        return {t: data[t].dropna() for t in data.columns}
    return {tickers[0]: data.dropna()}


def cmd_add(args):
    import yfinance as yf
    ticker = args.ticker.upper()
    price = args.price
    if price is None:
        hist = yf.Ticker(ticker).history(period="5d", auto_adjust=True)["Close"]
        if hist.empty:
            sys.exit(f"No price found for {ticker}; pass --price.")
        price = float(hist.iloc[-1])
    value = args.value if args.value is not None else latest_report_value(ticker)
    rows = load()
    rows.append({"date": date.today().isoformat(), "ticker": ticker, "action": args.action, "price": f"{price:.2f}",
                 "model_value": f"{value:.2f}" if value is not None else "", "thesis": args.thesis,
                 "closed": "", "close_note": ""})
    save(rows)
    print(f"Added: {args.action} {ticker} at ${price:,.2f}" + (f" (model value ${value:,.2f})" if value else ""))


def cmd_close(args):
    rows = load()
    for row in reversed(rows):
        if row["ticker"] == args.ticker.upper() and not row["closed"]:
            row["closed"], row["close_note"] = date.today().isoformat(), args.note
            save(rows)
            print(f"Closed the open {row['action']} on {row['ticker']} from {row['date']}.")
            return
    sys.exit(f"No open entry for {args.ticker.upper()}.")


def cmd_show(_args):
    rows = load()
    if not rows:
        print("The journal is empty. Add an entry: python3 journal.py add TICKER buy \"why\"")
        return
    start = min(r["date"] for r in rows)
    prices = closes([r["ticker"] for r in rows] + [BENCHMARK], start)
    spy = prices.get(BENCHMARK)
    print(f"{'Date':<11}{'Ticker':<7}{'Action':<7}{'Entry':>9}{'Model':>9}{'Now':>9}{'Return':>9}"
          f"{'SPY':>8}{'vs SPY':>8}  Thesis")
    scored = []
    for r in rows:
        series = prices.get(r["ticker"])
        end = r["closed"] or None
        now = ret = bench = None
        if series is not None and len(series):
            s = series[:end] if end else series
            now = float(s.iloc[-1])
            ret = now / float(r["price"]) - 1
        if spy is not None and len(spy):
            b = spy[r["date"]:end] if end else spy[r["date"]:]
            if len(b) > 1:
                bench = float(b.iloc[-1]) / float(b.iloc[0]) - 1
        excess = ret - bench if ret is not None and bench is not None else None
        # A pass "wins" when the stock then lagged the market.
        if excess is not None:
            scored.append(excess if r["action"] == "buy" else -excess if r["action"] == "pass" else None)
        fmt = lambda x: "" if x is None else f"{x:+.1%}"
        model_value = f"{float(r['model_value']):,.2f}" if r["model_value"] else ""
        print(f"{r['date']:<11}{r['ticker']:<7}{r['action']:<7}{float(r['price']):>9,.2f}"
              f"{model_value:>9}{(now or 0):>9,.2f}"
              f"{fmt(ret):>9}{fmt(bench):>8}{fmt(excess):>8}  {r['thesis'][:60]}"
              + (f"  [closed {r['closed']}]" if r["closed"] else ""))
    scored = [x for x in scored if x is not None]
    if scored:
        wins = sum(x > 0 for x in scored)
        print(f"\nBuys and passes scored against {BENCHMARK} over the same days: {wins} of {len(scored)} beat it, "
              f"average {sum(scored) / len(scored):+.1%}. (A pass scores when the stock then lagged.)")
        if len(scored) < 20:
            print("Too few decisions, or too little time, to tell skill from luck yet — keep recording.")


def main():
    parser = argparse.ArgumentParser(description="Decision journal scored against the market.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add", help="record a decision")
    add.add_argument("ticker")
    add.add_argument("action", choices=["buy", "pass", "watch"])
    add.add_argument("thesis", help="what you believe, in a sentence")
    add.add_argument("--price", type=float, help="your price (default: latest close)")
    add.add_argument("--value", type=float, help="model value per share (default: latest report's base)")
    close = sub.add_parser("close", help="close the latest open entry for a ticker (sold, or thesis dropped)")
    close.add_argument("ticker")
    close.add_argument("note")
    sub.add_parser("show", help="every entry against the market")
    args = parser.parse_args()
    {"add": cmd_add, "close": cmd_close, "show": cmd_show}[args.cmd](args)


if __name__ == "__main__":
    main()
