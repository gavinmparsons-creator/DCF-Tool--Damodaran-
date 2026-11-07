"""
screener.py — run the valuation over a whole sector and rank by value / price.

valuation.py values one company at a time, interactively. This runs it
non-interactively across every exchange-listed company in a sector
(found by SEC industry code), accepting every default — Damodaran's
industry data, the company's own filings — and ranks the results. It's an
idea generator, not a verdict: defaults can't see what you know, and a
stock that looks cheap is often cheap for a reason. Every row carries the
data warnings from its run, so you can see which numbers to distrust.

USAGE (from the dcf-tool folder):
    python3 screener.py banks                      # regional banks, any size
    python3 screener.py builders --max-cap 5e9     # homebuilders under $5B
    python3 screener.py banks --limit 20           # quick test on 20 names

Writes screens/<sector>_<date>.csv (ranked) and screens/logs/<TICKER>.log
(each company's full run). Not investment advice.
"""

import argparse
import builtins
import contextlib
import csv
import io
import json
import multiprocessing
import os
import re
import sys
import tempfile
import time
from datetime import date
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import valuation  # noqa: E402

SCREEN_DIR = os.path.join(HERE, "screens")

# SEC industry (SIC) codes per sector, and the mode each is valued in.
SECTORS = {
    "banks": {"sic": [6021, 6022, 6029, 6035, 6036], "flags": {"financial": True},
              "about": "national and state commercial banks, savings institutions (--financial)",
              "needs": {"book_equity", "shares_outstanding"}},
    "builders": {"sic": [1531, 6552, 1520], "flags": {"cyclical": True},
                 "about": "homebuilders, land developers and residential contractors (Lennar files as one) "
                          "(--cyclical)",
                 "needs": {"revenue", "operating_income", "cash", "shares_outstanding", "book_equity"}},
}
EDGAR_SIC_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&SIC={sic}&type=10-K"
                 "&dateb=&owner=include&count=100&start={start}&output=atom")
EDGAR_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
LISTED_EXCHANGES = ("Nasdaq", "NYSE")

# Lines in a run's output that mean "check this number by hand".
WARNING_PATTERNS = [
    (r"tagged in thousands or millions", "share count re-scaled"),
    (r"ANNUALIZED from", "annualized (no 10-K yet)"),
    (r"lags the company's latest annual report|hasn't caught up", "SEC data lags filing"),
    (r"capped at 0-100%", "payout capped"),
    (r"Base-year FCFF is NEGATIVE", "negative cash flow"),
    (r"growth is destroying value|below the cost of capital", "returns below cost of capital"),
]


# Stale tags that don't move the value: R&D and dividends aren't used by
# these modes' main build (a tiny old R&D tag isn't capitalized into much).
IGNORED_STALE = {"rd_expense", "dividends_per_share"}


def _get(url):
    with urlopen(Request(url, headers={"User-Agent": valuation.sec_user_agent()}), timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def sector_universe(sector):
    """[(ticker, cik, name, sic)] for every Nasdaq/NYSE company filing
    10-Ks under the sector's SIC codes."""
    listed = {}
    for cik, name, ticker, exchange in json.loads(_get(EDGAR_TICKERS_URL))["data"]:
        if exchange in LISTED_EXCHANGES and ticker:
            listed.setdefault(int(cik), (ticker, name))
    found = []
    for sic in SECTORS[sector]["sic"]:
        start = 0
        while True:
            page = _get(EDGAR_SIC_URL.format(sic=sic, start=start))
            ciks = [int(c) for c in re.findall(r"<cik>(\d+)</cik>", page)]
            for cik in ciks:
                if cik in listed:
                    found.append((listed[cik][0], cik, listed[cik][1], sic))
            if len(ciks) < 100:
                break
            start += 100
            time.sleep(0.15)  # SEC asks for no more than 10 requests a second
    seen, unique = set(), []
    for row in found:
        if row[0] not in seen:
            seen.add(row[0])
            unique.append(row)
    return unique


def market_cap(ticker):
    """Market cap from Yahoo, or None."""
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            price = valuation.get_market_data(ticker)[1]
            shares = valuation.get_market_shares(ticker)
        return price * shares if price and shares else None
    except Exception:
        return None


def value_one(args):
    """Run valuation.py on one ticker with every default accepted. Returns a
    result row (dict). Runs in a worker process: its own saved-choice
    files, so parallel runs can't overwrite each other's industry picks."""
    ticker, flags, needs, log_dir, answers = args
    work = tempfile.mkdtemp()
    valuation.INDUSTRY_MAP_PATH = os.path.join(work, "industry_map.json")
    valuation.ADR_RATIO_PATH = os.path.join(work, "adr_ratios.json")
    valuation.COUNTRY_MAP_PATH = os.path.join(work, "country_map.json")
    seen = {}

    def enter_everything(prompt=""):
        seen[prompt] = seen.get(prompt, 0) + 1
        if seen[prompt] > 3:
            raise RuntimeError(f"needs a typed answer: {prompt.strip()[:80]}")
        answer = next((value for sub, value in answers if sub in prompt), "")
        print(prompt + answer)
        return answer

    builtins.input = enter_everything
    out = io.StringIO()
    row = {"ticker": ticker}
    try:
        with contextlib.redirect_stdout(out):
            summary = valuation.run_full_valuation(ticker, None, **flags)
        values = summary.get("excess_return") or summary.get("dcf") or {}
        row.update(low=values.get("low"), base=values.get("base"), high=values.get("high"),
                   cost_of_equity=summary.get("cost_of_equity"), wacc=summary.get("wacc"))
        if "excess_return" in summary:
            er = summary["excess_return"]
            row.update(roe=er.get("current_roe"), stable_roe=er.get("stable_roe"),
                       book_equity=er.get("common_book_equity"))
    except BaseException as e:  # a sys.exit inside the tool shouldn't stop the screen
        row["error"] = f"{type(e).__name__}: {e}"[:160]
    text = out.getvalue()
    price = re.search(r"(?:Current market price|share price)=?:? ?\$([\d,]+\.?\d*)", text)
    row["price"] = float(price.group(1).replace(",", "")) if price else None
    implied = re.search(r"implies a (?:stable ROE|high-growth-stage rate) of (-?[\d.]+)%", text)
    row["price_implies"] = float(implied.group(1)) / 100 if implied else None
    warnings = {label for pattern, label in WARNING_PATTERNS if re.search(pattern, text)}
    # Only metrics this sector's model uses count as missing (a bank has no revenue line).
    missing = re.search(r"Missing metrics: (.*)", text)
    missing = [m for m in missing.group(1).split(", ") if m in needs] if missing else []
    stale = re.search(r"possibly stale data: (.*)", text)
    stale = [m for m in stale.group(1).split(", ") if m not in IGNORED_STALE] if stale else []
    if "Rebuilt as pretax income" in text:  # no (current) operating-income line, but rebuilt from pretax + interest
        missing = [m for m in missing if m != "operating_income"]
        stale = [m for m in stale if m != "operating_income"]
    if missing:
        warnings.add("missing " + "/".join(missing))
    if stale:
        warnings.add("stale " + "/".join(stale))
    row["warnings"] = "; ".join(sorted(warnings))
    shares = re.search(r"Shares: ([\d,]+) basic", text)
    if shares and row["price"]:
        filed = float(shares.group(1).replace(",", ""))
        row["market_cap"] = filed * row["price"]
        # A dual-class or Up-C company can file one class's count (Smith
        # Douglas: 9.2M Class A of ~51M economic shares), inflating value per share.
        try:
            market_shares = valuation.get_market_shares(ticker)
        except Exception:
            market_shares = None
        if market_shares and not 0.67 <= filed / market_shares <= 1.5:
            row["warnings"] = "; ".join(filter(None, [row["warnings"], f"share count {filed / market_shares:.2f}x "
                                                     "the market's (dual class / Up-C?)"]))
    if row.get("base") is not None and row.get("price"):
        row["value_to_price"] = row["base"] / row["price"]
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, f"{ticker}.log"), "w") as f:
        f.write(text)
    return row


def main():
    parser = argparse.ArgumentParser(description="Run the valuation over a sector and rank by value / price.")
    parser.add_argument("sector", choices=sorted(SECTORS))
    parser.add_argument("--min-cap", type=float, default=0, help="minimum market cap in dollars (e.g. 3e8)")
    parser.add_argument("--max-cap", type=float, default=float("inf"), help="maximum market cap (e.g. 5e9)")
    parser.add_argument("--limit", type=int, help="value only the first N companies (a quick test)")
    parser.add_argument("--workers", type=int, default=4, help="companies valued at once (default 4)")
    parser.add_argument("--answer", action="append", default=[], metavar="PROMPT=VALUE",
                        help="answer every prompt containing PROMPT with VALUE instead of Enter, e.g. "
                             "--answer \"type a beta to use as is=0.76\" (repeatable)")
    args = parser.parse_args()

    spec = SECTORS[args.sector]
    print(f"Finding {args.sector}: {spec['about']}...")
    universe = sector_universe(args.sector)
    print(f"  {len(universe)} listed companies under SIC {', '.join(map(str, spec['sic']))}")
    valuation.load_damodaran_data()  # warm the cache once, before the workers start

    if args.min_cap or args.max_cap != float("inf"):
        print(f"Checking market caps (${args.min_cap:,.0f} to ${args.max_cap:,.0f})...")
        kept = []
        for ticker, cik, name, sic in universe:
            cap = market_cap(ticker)
            if cap is not None and args.min_cap <= cap <= args.max_cap:
                kept.append((ticker, cik, name, sic))
        universe = kept
        print(f"  {len(universe)} in range")
    if args.limit:
        universe = universe[:args.limit]

    log_dir = os.path.join(SCREEN_DIR, "logs", args.sector)
    names = {t: (n, s) for t, _, n, s in universe}
    rows = []
    print(f"Valuing {len(universe)} companies ({args.workers} at a time; each takes ~20-60 seconds)...")
    with multiprocessing.Pool(args.workers) as pool:
        answers = [tuple(a.split("=", 1)) for a in args.answer]
        jobs = [(t, spec["flags"], spec["needs"], log_dir, answers) for t, _, _, _ in universe]
        for i, row in enumerate(pool.imap_unordered(value_one, jobs), start=1):
            row["name"], row["sic"] = names[row["ticker"]]
            rows.append(row)
            status = (f"{row['value_to_price']:.2f}x" if row.get("value_to_price") else
                      row.get("error", "no value")[:60])
            print(f"  [{i}/{len(universe)}] {row['ticker']:<6} {status}", flush=True)

    rows.sort(key=lambda r: r.get("value_to_price") or -1, reverse=True)
    os.makedirs(SCREEN_DIR, exist_ok=True)
    path = os.path.join(SCREEN_DIR, f"{args.sector}_{date.today().isoformat()}.csv")
    fields = ["ticker", "name", "sic", "market_cap", "price", "low", "base", "high", "value_to_price", "price_implies",
              "roe", "stable_roe", "cost_of_equity", "wacc", "book_equity", "warnings", "error"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nRanked results: {path}")
    print(f"{'Ticker':<7}{'Price':>10}{'Base value':>12}{'Value/price':>13}  Warnings")
    for r in rows[:25]:
        if r.get("value_to_price"):
            print(f"{r['ticker']:<7}{r['price']:>10,.2f}{r['base']:>12,.2f}{r['value_to_price']:>12.2f}x  "
                  f"{r['warnings']}")
    print("\nDefaults only — an idea list, not a verdict. Read a company's log before trusting its row.")


if __name__ == "__main__":
    main()
