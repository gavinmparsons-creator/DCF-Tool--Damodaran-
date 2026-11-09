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
    "builders": {"sic": [1531, 6552, 1520], "flags": {"cyclical": True, "insight": True},
                 "about": "homebuilders, land developers and residential contractors (Lennar files as one) "
                          "(--cyclical)",
                 "needs": {"revenue", "operating_income", "cash", "shares_outstanding", "book_equity"}},
    # Small caps: every listed operating company from $300M to $2B, on the
    # standard model. Financials, REITs and blank-check shells (SIC 6000-6999)
    # are left out — they need bank or REIT mode.
    # Any Damodaran industries from the sector map (python3 sectors.py first),
    # named with --industries: e.g. Machinery "Restaurant/Dining".
    # Any list of tickers (--tickers, or --tickers-file with one per line, e.g.
    # an earnings calendar); each company's mode comes from its SEC code.
    "tickers": {"universe": "list", "flags": {"insight": True},
                "about": "the tickers given, each valued in the mode its SEC industry code suggests",
                "needs": {"revenue", "operating_income", "cash", "shares_outstanding", "book_equity"}},
    "industries": {"universe": "sector_map", "flags": {"insight": True},
                   "about": "the industries named with --industries, from the sector map (sectors.py)",
                   "needs": {"revenue", "operating_income", "cash", "shares_outstanding", "book_equity"}},
    "smallcap": {"universe": "cap", "min_cap": 3e8, "max_cap": 2e9, "exclude_sic": (6000, 6999),
                 "flags": {"insight": True},
                 "about": "listed operating companies from $300M to $2B, excluding financials and REITs",
                 "needs": {"revenue", "operating_income", "cash", "shares_outstanding", "book_equity"}},
}
EDGAR_SIC_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&SIC={sic}&type=10-K"
                 "&dateb=&owner=include&count=100&start={start}&output=atom")
EDGAR_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
EDGAR_SHARES_FRAME_URL = "https://data.sec.gov/api/xbrl/frames/dei/EntityCommonStockSharesOutstanding/shares/{period}.json"
EDGAR_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
LISTED_EXCHANGES = ("Nasdaq", "NYSE")

# Lines in a run's output that mean "check this number by hand".
WARNING_PATTERNS = [
    (r"tagged in thousands or millions", "share count re-scaled"),
    (r"ANNUALIZED from", "annualized (no 10-K yet)"),
    (r"lags the company's latest annual report|hasn't caught up", "SEC data lags filing"),
    (r"capped at 0-100%", "payout capped"),
    (r"far from its typical", "one-time quarter normalized"),
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


def _recent_quarters(n=3):
    """The last n calendar quarters as SEC frame names, newest first (CY2026Q3I...)."""
    y, q = date.today().year, (date.today().month - 1) // 3 + 1
    out = []
    for _ in range(n):
        q -= 1
        if q == 0:
            y, q = y - 1, 4
        out.append(f"CY{y}Q{q}I")
    return out


def listed_market_caps():
    """{cik: (ticker, name, market cap)} for every Nasdaq/NYSE company with a
    cover-page share count: SEC's share counts for every filer (one frames
    file) x Yahoo's last close (batches of 400)."""
    import yfinance as yf
    listed = {}
    for cik, name, ticker, exchange in json.loads(_get(EDGAR_TICKERS_URL))["data"]:
        if exchange in LISTED_EXCHANGES and ticker and "-" not in ticker:
            listed.setdefault(int(cik), (ticker, name))
    shares = {}
    for period in reversed(_recent_quarters()):  # newest overwrites older
        try:
            for row in json.loads(_get(EDGAR_SHARES_FRAME_URL.format(period=period)))["data"]:
                shares[int(row["cik"])] = row["val"]
        except Exception:
            pass
    candidates = {cik: v for cik, v in listed.items() if cik in shares}
    tickers = [t for t, _ in candidates.values()]
    prices = {}
    for i in range(0, len(tickers), 400):
        batch = tickers[i:i + 400]
        try:
            close = yf.download(batch, period="5d", progress=False, auto_adjust=True)["Close"]
            last = close.ffill().iloc[-1]
            prices.update({t: float(last[t]) for t in batch if t in last and last[t] == last[t]})
        except Exception:
            pass
    return {cik: (ticker, name, prices[ticker] * shares[cik])
            for cik, (ticker, name) in candidates.items() if prices.get(ticker)}


def industry_codes(ciks):
    """{cik: SEC industry (SIC) code, 0 if unknown}. Codes almost never
    change, so each one found is kept for good (screens/sic_codes.json):
    only new companies cost a request, paced to SEC's limit."""
    sic_cache_path = os.path.join(SCREEN_DIR, "sic_codes.json")
    try:
        with open(sic_cache_path) as f:
            sic_cache = json.load(f)
    except (OSError, ValueError):
        sic_cache = {}
    todo = [cik for cik in ciks if str(cik) not in sic_cache]
    print(f"  looking up {len(todo)} new industry codes ({len(ciks) - len(todo)} known)...", flush=True)
    for i, cik in enumerate(todo, start=1):
        try:
            sic_cache[str(cik)] = int(json.loads(_get(EDGAR_SUBMISSIONS_URL.format(cik=cik))).get("sic") or 0)
        except Exception:
            sic_cache[str(cik)] = 0
        if i % 200 == 0:
            print(f"    {i}/{len(todo)}", flush=True)
            os.makedirs(SCREEN_DIR, exist_ok=True)
            with open(sic_cache_path, "w") as f:  # save as it goes, so an interrupted run isn't wasted
                json.dump(sic_cache, f)
        time.sleep(0.12)  # SEC asks for no more than 10 requests a second
    os.makedirs(SCREEN_DIR, exist_ok=True)
    with open(sic_cache_path, "w") as f:
        json.dump(sic_cache, f)
    return {cik: sic_cache[str(cik)] for cik in ciks}


def cap_universe(spec):
    """[(ticker, cik, name, sic)] for Nasdaq/NYSE companies whose market cap
    is in the sector's range, minus the excluded SIC codes."""
    in_range = [(t, cik, n) for cik, (t, n, cap) in listed_market_caps().items()
                if spec["min_cap"] <= cap <= spec["max_cap"]]
    print(f"  {len(in_range)} in the market-cap range", flush=True)
    codes = industry_codes([cik for _, cik, _ in in_range])
    lo, hi = spec.get("exclude_sic", (0, -1))
    return [(t, cik, n, codes[cik]) for t, cik, n in in_range if not lo <= codes[cik] <= hi]


def earnings_calendar(day, min_cap=3e8):
    """Tickers reporting earnings on `day` (YYYY-MM-DD) from Yahoo's
    calendar, worth at least `min_cap`. Non-US listings (OTC ADRs ending in
    F/Y) drop out later, when they aren't found on SEC's Nasdaq/NYSE list."""
    import yfinance as yf
    from datetime import datetime, timedelta
    end = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
    cal = yf.Calendars(start=day, end=end)
    tickers, offset = [], 0
    while offset <= 2000:
        try:
            df = cal.get_earnings_calendar(limit=100, offset=offset, filter_most_active=False).reset_index()
        except Exception:
            break
        if df.empty:
            break
        df = df[df["Event Start Date"].astype(str).str.startswith(day)]
        tickers += [str(t) for t, cap in zip(df["Symbol"], df["Marketcap"].fillna(0)) if cap >= min_cap]
        offset += 100
    return list(dict.fromkeys(tickers))


def market_cap(ticker):
    """Market cap from Yahoo, or None."""
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            price = valuation.get_market_data(ticker)[1]
            shares = valuation.get_market_shares(ticker)
        return price * shares if price and shares else None
    except Exception:
        return None


def run_warnings(text, needs, ticker=None, price=None):
    """The data warnings in one run's printed output, as a '; '-joined
    string, and the market cap (filed basic shares x price, or None).
    `needs`: the metrics the model used counts as missing (a bank has no
    revenue line). With a ticker, the filed share count is also checked
    against the market's."""
    warnings = {label for pattern, label in WARNING_PATTERNS if re.search(pattern, text)}
    missing = re.search(r"Missing metrics: (.*)", text)
    missing = [m for m in missing.group(1).split(", ") if m in needs] if missing else []
    stale = re.search(r"possibly stale data: (.*)", text)
    stale = [m for m in stale.group(1).split(", ") if m not in IGNORED_STALE] if stale else []
    if "now reports total debt under a different tag" in text:  # stale debt tags, but a current total was found
        stale = [m for m in stale if not m.startswith("total_debt")]
    if "Rebuilt as pretax income" in text:  # no (current) operating-income line, but rebuilt from pretax + interest
        missing = [m for m in missing if m != "operating_income"]
        stale = [m for m in stale if m != "operating_income"]
    if missing:
        warnings.add("missing " + "/".join(missing))
    if stale:
        warnings.add("stale " + "/".join(stale))
    market_cap = None
    shares = re.search(r"Shares: ([\d,]+) basic", text)
    if shares and price:
        filed = float(shares.group(1).replace(",", ""))
        market_cap = filed * price
        # A dual-class or Up-C company can file one class's count (Smith
        # Douglas: 9.2M Class A of ~51M economic shares), inflating value per share.
        try:
            market_shares = valuation.get_market_shares(ticker) if ticker else None
        except Exception:
            market_shares = None
        if market_shares and not 0.67 <= filed / market_shares <= 1.5:
            warnings.add(f"share count {filed / market_shares:.2f}x the market's (dual class / Up-C?)")
    return "; ".join(sorted(warnings)), market_cap


def _private_choice_files():
    """Point this worker at its own copy of your saved industry / ADR /
    country choices: it reads what you've decided (a report agrees with
    your runs) but never writes your files, which you may be using in a
    terminal at the same moment."""
    if getattr(_private_choice_files, "done", False):
        return
    work = tempfile.mkdtemp()
    for attr in ("INDUSTRY_MAP_PATH", "ADR_RATIO_PATH", "COUNTRY_MAP_PATH"):
        source = getattr(valuation, attr)
        target = os.path.join(work, os.path.basename(source))
        try:
            with open(source) as f, open(target, "w") as g:
                g.write(f.read())
        except OSError:
            pass
        setattr(valuation, attr, target)
    _private_choice_files.done = True


def insight_one(ticker, flags, needs, log_dir):
    """value_one plus the narrative model's view (report.py's capture): the
    record value — worth per share if the company keeps its own 5-year
    revenue growth at today's margin — and the starting growth the price
    needs at today's margin, against that record."""
    import report  # imports this module; loaded by the time a worker calls this
    _private_choice_files()
    row = {"ticker": ticker}
    text = ""
    try:
        cik, _ = valuation.get_cik_for_ticker(ticker)
        facts = valuation.fetch_json(valuation.EDGAR_FACTS_URL.format(cik=cik))
        cagr, _span = valuation.trailing_revenue_cagr(facts)
        growth = min(max(cagr if cagr is not None else 0.0, -0.05), 0.40)

        def answer_for(prompt, printed):
            if "Initial revenue growth" in prompt:
                return f"{growth * 100:.2f}"
            if "Early-stage cost of capital" in prompt:
                found = re.findall(r"Mature WACC \(computed above\): ([\d.]+)%", printed)
                return found[-1] if found else ""
            if "Target (mature) operating margin" in prompt:
                found = re.findall(r"Current operating margin: (-?[\d.]+)%", printed)
                return found[-1] if found else ""
            if "Probability of failure as a percent" in prompt:
                return "10"
            return ""

        summary, text, captured = report.run_capture(ticker, dict(flags, narrative=True), answer_for)
        values = summary.get("dcf") or {}
        row.update(low=values.get("low"), base=values.get("base"), high=values.get("high"),
                   wacc=summary.get("wacc"), hist_growth=cagr)
        price = summary.get("share_price")
        if "range" in captured and price:
            story = report.Story(captured)
            row["margin_now"] = story.margin_now
            row["record_value"] = story.value(growth, story.margin_now)
            if story.margin_now > 0:
                row["implied_growth"] = story.solve(price, "growth", story.margin_now, -0.50, 2.0)
    except BaseException as e:
        row["error"] = f"{type(e).__name__}: {e}"[:160]
    price = re.search(r"(?:Current market price|share price)=?:? ?\$([\d,]+\.?\d*)", text)
    row["price"] = float(price.group(1).replace(",", "")) if price else None
    row["warnings"], row["market_cap"] = run_warnings(text, needs, ticker, row["price"])
    if row.get("base") is not None and row.get("price"):
        row["value_to_price"] = row["base"] / row["price"]
    if row.get("record_value") is not None and row.get("price"):
        row["record_to_price"] = row["record_value"] / row["price"]
    if row.get("implied_growth") is not None and row.get("hist_growth") is not None:
        row["expectations_gap"] = row["implied_growth"] - row["hist_growth"]
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, f"{ticker}.log"), "w") as f:
        f.write(text)
    return row


def value_one(args):
    """Run valuation.py on one ticker with every default accepted. Returns a
    result row (dict). Runs in a worker process: its own saved-choice
    files, so parallel runs can't overwrite each other's industry picks."""
    ticker, flags, needs, log_dir, answers = args
    valuation.SKIP_IMPLIED_VOLATILITY = True
    if flags.get("insight"):
        return insight_one(ticker, {k: v for k, v in flags.items() if k != "insight"}, needs, log_dir)
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
    row["warnings"], row["market_cap"] = run_warnings(text, needs, ticker, row["price"])
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
    parser.add_argument("--tickers", nargs="+", default=[], metavar="TICKER", help="for 'tickers'")
    parser.add_argument("--tickers-file", help="for 'tickers': a file with one ticker per line (or a CSV whose "
                                               "first column is the ticker)")
    parser.add_argument("--earnings", metavar="YYYY-MM-DD",
                        help="for 'tickers': every company reporting earnings that day (Yahoo's calendar), "
                             "US-listed and worth $300M+")
    parser.add_argument("--label", help="name for the results file (default: the sector)")
    parser.add_argument("--industries", nargs="+", default=[], metavar="NAME",
                        help="for 'industries': Damodaran industry names from sectors.py's table")
    parser.add_argument("--answer", action="append", default=[], metavar="PROMPT=VALUE",
                        help="answer every prompt containing PROMPT with VALUE instead of Enter, e.g. "
                             "--answer \"type a beta to use as is=0.76\" (repeatable)")
    args = parser.parse_args()

    spec = SECTORS[args.sector]
    print(f"Finding {args.sector}: {spec['about']}...")
    label = args.label or args.sector
    if spec.get("universe") == "sector_map":
        import glob
        maps = sorted(glob.glob(os.path.join(SCREEN_DIR, "sectors_*.csv")))
        if not maps or not args.industries:
            sys.exit("Run python3 sectors.py first, and name industries with --industries.")
        wanted = {n.lower() for n in args.industries}
        with open(maps[-1]) as f:
            universe = [(r["ticker"], None, r["company"], int(r["sic"] or 0)) for r in csv.DictReader(f)
                        if r["industry"].lower() in wanted]
        label = "_".join(re.sub(r"[^a-z]+", "", n.lower())[:12] for n in args.industries)
        print(f"  {len(universe)} companies in {', '.join(args.industries)} (from {os.path.basename(maps[-1])})")
    elif spec.get("universe") == "list":
        tickers = [t.upper() for t in args.tickers]
        if args.earnings:
            found = earnings_calendar(args.earnings)
            print(f"  {len(found)} companies report on {args.earnings} (Yahoo's calendar, $300M+)")
            tickers += found
            label = args.label or f"earnings_{args.earnings}"
        if args.tickers_file:
            with open(args.tickers_file) as f:
                for i, row in enumerate(csv.reader(f)):
                    if row and row[0].strip() and not (i == 0 and row[0].strip().lower() in ("symbol", "ticker")):
                        tickers.append(row[0].strip().upper())
        exchange_data = json.loads(_get(EDGAR_TICKERS_URL))["data"]
        listed = {t: (int(cik), name) for cik, name, t, _ex in exchange_data if t}
        on_exchange = {t for _cik, _name, t, ex in exchange_data if t and ex in LISTED_EXCHANGES}
        # Calendar entries include foreign over-the-counter tickers (SoftBank's
        # SFTBF) that file nothing the tool can value: keep Nasdaq/NYSE only.
        known = [t for t in dict.fromkeys(tickers) if t in listed and (not args.earnings or t in on_exchange)]
        codes = industry_codes([listed[t][0] for t in known])
        universe = [(t, listed[t][0], listed[t][1], codes[listed[t][0]]) for t in known]
        print(f"  {len(universe)} of {len(set(tickers))} tickers found on EDGAR")
    elif spec.get("universe") == "cap":
        # The universe takes ~15 minutes to build (SEC's rate limit on the
        # industry-code lookups), so it's saved for the day and reused.
        cache = os.path.join(SCREEN_DIR, f"universe_{args.sector}_{date.today().isoformat()}.json")
        try:
            with open(cache) as f:
                universe = [tuple(row) for row in json.load(f)]
        except (OSError, ValueError):
            universe = cap_universe(spec)
            os.makedirs(SCREEN_DIR, exist_ok=True)
            with open(cache, "w") as f:
                json.dump(universe, f)
        print(f"  {len(universe)} listed companies from ${spec['min_cap']:,.0f} to ${spec['max_cap']:,.0f}")
    else:
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

    log_dir = os.path.join(SCREEN_DIR, "logs", label)
    names = {t: (n, s) for t, _, n, s in universe}
    rows = []
    print(f"Valuing {len(universe)} companies ({args.workers} at a time; each takes ~20-60 seconds)...")
    with multiprocessing.Pool(args.workers) as pool:
        answers = [tuple(a.split("=", 1)) for a in args.answer]
        def flags_for(sic):
            # A list mixes industries: banks and REITs need their own models.
            if spec.get("universe") == "list":
                if 6020 <= sic <= 6039 or sic == 6211:
                    return {"financial": True}, SECTORS["banks"]["needs"]
                if sic == 6798:
                    return {"reit": True}, spec["needs"]
            return spec["flags"], spec["needs"]
        jobs = [(t, *flags_for(sic), log_dir, answers) for t, _, _, sic in universe]
        for i, row in enumerate(pool.imap_unordered(value_one, jobs), start=1):
            row["name"], row["sic"] = names[row["ticker"]]
            rows.append(row)
            status = (f"{row['record_to_price']:.2f}x record" if row.get("record_to_price") is not None else
                      f"{row['value_to_price']:.2f}x" if row.get("value_to_price") else
                      row.get("error", "no value")[:60])
            print(f"  [{i}/{len(universe)}] {row['ticker']:<6} {status}", flush=True)

    rank_by = "record_to_price" if spec["flags"].get("insight") else "value_to_price"
    rows.sort(key=lambda r: r.get(rank_by) if r.get(rank_by) is not None else -1e9, reverse=True)
    os.makedirs(SCREEN_DIR, exist_ok=True)
    path = os.path.join(SCREEN_DIR, f"{label}_{date.today().isoformat()}.csv")
    fields = ["ticker", "name", "sic", "market_cap", "price", "low", "base", "high", "value_to_price",
              "record_value", "record_to_price", "hist_growth", "implied_growth", "expectations_gap", "margin_now",
              "price_implies",
              "roe", "stable_roe", "cost_of_equity", "wacc", "book_equity", "warnings", "error"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nRanked results: {path}")
    if rank_by == "record_to_price":
        pct = lambda x: "" if x is None else f"{x:.1%}"
        print(f"{'Ticker':<7}{'Price':>10}{'Record value':>14}{'x price':>9}{'Grew':>8}{'Price needs':>13}  Warnings")
        for r in rows[:25]:
            if r.get("record_to_price") is not None:
                print(f"{r['ticker']:<7}{r['price']:>10,.2f}{r['record_value']:>14,.2f}{r['record_to_price']:>8.2f}x"
                      f"{pct(r.get('hist_growth')):>8}{pct(r.get('implied_growth')):>13}  {r['warnings']}")
    else:
        print(f"{'Ticker':<7}{'Price':>10}{'Base value':>12}{'Value/price':>13}  Warnings")
        for r in rows[:25]:
            if r.get("value_to_price"):
                print(f"{r['ticker']:<7}{r['price']:>10,.2f}{r['base']:>12,.2f}{r['value_to_price']:>12.2f}x  "
                      f"{r['warnings']}")
    print("\nDefaults only — an idea list, not a verdict. Read a company's log before trusting its row.")


if __name__ == "__main__":
    main()
