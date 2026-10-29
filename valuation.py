"""
valuation.py — Pull US public company financials from SEC EDGAR and run a
WACC/DCF valuation, Damodaran-style.

USAGE (from Terminal):
    python3 valuation.py AAPL
    python3 valuation.py MSFT --fmp-key YOUR_FMP_KEY_HERE
    python3 valuation.py VKTX --biotech --narrative   (drug pipeline rNPV + biotech defaults)
    python3 valuation.py O --reit                     (FFO/AFFO dividend model + NAV)

WHAT IT DOES:
    1. Looks up the company's CIK number from its ticker (via SEC EDGAR)
    2. Pulls the last several years of financials from EDGAR's XBRL API
       (revenue, operating income, D&A, capex, debt, cash, shares out)
    3. Pulls market data (share price, beta) from Financial Modeling Prep
       if you provide an API key; otherwise you'll be prompted to enter
       beta and risk-free rate manually
    4. Runs a standard unlevered FCFF / WACC DCF and prints a valuation
       range (low / base / high) based on flexing the discount rate and
       terminal growth rate

WHY A RANGE, NOT ONE NUMBER:
    DCF output is extremely sensitive to your assumptions about terminal
    growth and discount rate. A single point estimate implies false
    precision. This script perturbs both by a small amount and shows you
    the resulting spread — which is closer to how professionals actually
    use these models.

NOTES:
    - No API key is needed for EDGAR. SEC requires a User-Agent header
      identifying you — edit YOUR_NAME / YOUR_EMAIL below before running.
    - This is a starting model (WACC/FCFF DCF only). APV and DGM can be
      added later the same way — see compute_dcf() for where to extend.
    - EDGAR XBRL tag names are NOT perfectly standardized across
      companies. This script tries a list of common alternate tags per
      metric (see METRIC_TAGS) and uses the first one that has data. If a
      metric truly can't be found, it's reported as missing so you can
      fill it in by hand rather than the script silently guessing.
    - Foreign private issuers (e.g. Nokia) file Form 20-F under IFRS
      instead of a 10-K under US GAAP. METRIC_TAGS includes the common
      ifrs-full tag names alongside the us-gaap ones, and financials are
      pulled from whichever taxonomy the filer actually uses. IFRS filers
      usually report in a non-USD currency, which get_fx_rate_to_usd()
      converts to USD (live rate, ECB-sourced) before the DCF math runs,
      so it stays consistent with the USD market data.
"""

import sys
import json
import time
import argparse
import http.cookiejar
from datetime import datetime, timezone
from urllib.request import Request, urlopen, build_opener, HTTPCookieProcessor
from urllib.error import HTTPError

import model  # our DCF calculation engine — see model.py

# Optional convenience layer — NOT a required dependency. The rest of this
# script (and this import) works with zero `pip install` steps on a bare
# Python 3 stdlib, which matters for sharing this as two plain files with
# someone non-technical. If yfinance happens to be installed, get_market_data
# uses it as a free, no-API-key way to skip manual beta/price entry; if it's
# not installed, that code path is simply never reached.
try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False

# ---- EDIT THESE TWO LINES BEFORE RUNNING ----
YOUR_NAME = "Gavin Parsons"
YOUR_EMAIL = "you@example.com"
# ----------------------------------------------

# ---- OPTIONAL: paste your Financial Modeling Prep key here ----
# Get a free key at financialmodelingprep.com. If left blank, or if the
# key doesn't work, the script will ask you to type in beta and share
# price by hand instead (takes 30 seconds, look them up on Yahoo Finance).
FMP_API_KEY = ""
# -----------------------------------------------------------------

USER_AGENT = f"{YOUR_NAME} {YOUR_EMAIL}"

EDGAR_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
EDGAR_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
FMP_PROFILE_URL = "https://financialmodelingprep.com/api/v3/profile/{ticker}?apikey={key}"

# Reasonable defaults used if you don't override them at the prompt.
DEFAULT_RISK_FREE_RATE = 0.045   # ~10-year Treasury yield, adjust as needed
DEFAULT_EQUITY_RISK_PREMIUM = 0.055  # Damodaran's commonly cited long-run US ERP
DEFAULT_TERMINAL_GROWTH = 0.025  # roughly long-run GDP growth
DEFAULT_FCFF_GROWTH = 0.05       # near-term growth rate for projected FCFF
DEFAULT_COST_OF_DEBT = 0.05      # fallback only — used when interest expense isn't found at all; normally replaced by the synthetic-rating cost of debt (see run_full_valuation)

# Alternate XBRL tags to try for each metric, in priority order.
# EDGAR doesn't use one consistent tag name across all filers, so we
# try the most common ones and use whichever has data.
METRIC_TAGS = {
    "revenue": [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "Revenue",  # ifrs-full, pre-IFRS 15 (superseded ~2018 by the tag below)
        "RevenueFromContractsWithCustomers",  # ifrs-full, IFRS 15
    ],
    "operating_income": [
        "OperatingIncomeLoss",
        "ProfitLossFromOperatingActivities",  # ifrs-full
    ],
    "depreciation_amortization": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "DepreciationAndAmortization",
        "DepreciationAndAmortisationExpense",  # ifrs-full
        "AdjustmentsForDepreciationAndAmortisationExpense",  # ifrs-full: e.g. SAP, the cash-flow-statement add-back concept rather than an income-statement expense tag
        # Composite: some filers (Okta, Accenture) never tag one combined
        # D&A figure at all — it's split across separate cash-flow line
        # items. NOTE: deliberately NOT summing in "OtherDepreciationAndAmortization"
        # here — for Okta that tag IS the combined total (summing it in would
        # double it), while for Accenture it's a small genuine residual on
        # top. Since we can't tell those apart per-filer, this recipe takes
        # the safer miss (slightly understating Accenture-like filers by an
        # immaterial residual) over the worse one (silently doubling Okta-like
        # filers). "OtherDepreciationAndAmortization" is listed separately,
        # below, as a bare last-resort fallback for filers who tag ONLY that.
        ["Depreciation", "AmortizationOfIntangibleAssets"],
        ["DepreciationExpense", "AmortisationExpense"],  # ifrs-full (British spelling), same split-tag pattern
        "OtherDepreciationAndAmortization",
    ],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsForCapitalImprovements",
        "PaymentsToAcquireProductiveAssets",
        "PaymentsToAcquireOtherProductiveAssets",
        "PaymentsForProceedsFromProductiveAssets",
        "PurchaseOfPropertyPlantAndEquipmentIntangibleAssetsOtherThanGoodwillInvestmentPropertyAndOtherNoncurrentAssets",  # ifrs-full
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",  # ifrs-full: e.g. Telesat, PP&E-only capex tagged separately from the combined concept above
        "PaymentsToAcquireOtherPropertyPlantAndEquipment",  # e.g. Alibaba: stopped tagging this after FY2020, so it'll resolve stale — the stale-override prompt is the fix, not a new tag
    ],
    "total_debt_current": [
        "LongTermDebtCurrent",
        "DebtCurrent",
        "CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings",  # ifrs-full
        "ConvertibleDebtCurrent",  # growth-stage tech filers often carry converts, not term loans
        "LongTermDebtAndCapitalLeaseObligationsCurrent",  # e.g. Sirius XM: combined debt+lease presentation
        "ShorttermBorrowings",  # ifrs-full casing
        "ShortTermBorrowings",  # us-gaap casing (genuinely a different tag — XBRL names are case-sensitive) — e.g. GE Vernova
        "ConvertibleNotesPayableCurrent",  # e.g. Akamai
    ],
    "total_debt_noncurrent": [
        "LongTermDebtNoncurrent",
        "LongTermDebt",
        "LongtermBorrowings",  # ifrs-full
        "ConvertibleDebtNoncurrent",
        "OtherLongTermDebt",  # e.g. Accenture: no LongTermDebt(Noncurrent) tag at all, genuinely low-leverage
        "LongTermDebtAndCapitalLeaseObligations",  # e.g. Sirius XM: combined debt+lease presentation
        "ConvertibleLongTermNotesPayable",  # e.g. Akamai
    ],
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "CashAndCashEquivalents",  # ifrs-full
    ],
    "shares_outstanding": [
        "CommonStockSharesOutstanding",
        "EntityCommonStockSharesOutstanding",
        "NumberOfSharesOutstanding",  # ifrs-full
        "WeightedAverageNumberOfDilutedSharesOutstanding",  # last resort: a fiscal-year average, not a point-in-time count, but some filers never tag an actual share count in companyfacts
    ],
    "income_tax_expense": [
        "IncomeTaxExpenseBenefit",
        "IncomeTaxExpenseContinuingOperations",  # ifrs-full
    ],
    "pretax_income": [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        "ProfitLossBeforeTax",  # ifrs-full
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic",  # e.g. Eversource: purely domestic filer, never tags a combined (domestic+foreign) total since there's no foreign component to add
    ],
    "book_equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "EquityAttributableToOwnersOfParent",  # ifrs-full, excludes noncontrolling interest
        "Equity",  # ifrs-full, includes noncontrolling interest — last resort
    ],
    "dividends_per_share": [
        "CommonStockDividendsPerShareDeclared",
        "CommonStockDividendsPerShareCashPaid",
    ],
    "rd_expense": [
        "ResearchAndDevelopmentExpense",  # shared tag name across us-gaap and ifrs-full
    ],
    "interest_expense": [
        "InterestExpense",
        "InterestExpenseDebt",
        "InterestExpenseNonoperating",
        "InterestExpenseOperating",  # e.g. Coherent, Cloudflare
        "FinanceCosts",  # ifrs-full
        # Last-resort fallbacks: a signed NET interest income/(expense)
        # figure, for filers (e.g. Charter) who stopped tagging gross
        # interest expense entirely. See NET_INTEREST_TAGS below — the
        # caller only trusts these when the sign indicates genuine net
        # EXPENSE; a cash-rich filer (e.g. Toast) can show net interest
        # INCOME here instead, which isn't usable as an expense figure
        # at all (there's no way to recover gross expense from it).
        "InterestIncomeExpenseNonoperatingNet",
        "InterestIncomeExpenseNet",
        # Absolute last resort: cash interest PAID (a supplemental cash-
        # flow-statement disclosure, "net" of capitalized interest, not
        # net of interest income — always a positive outflow magnitude by
        # convention, unlike the signed tags above) — e.g. GE Vernova,
        # which doesn't tag an accrual-basis interest expense concept at
        # all. Cash paid can differ a bit from accrued expense on timing,
        # but it's a reasonable proxy when nothing else exists.
        "InterestPaidNet",
    ],
}

# Tags whose value is a SIGNED net interest income/(expense) figure,
# rather than a plain positive expense magnitude — see the caller in
# run_full_valuation for how the sign is used.
NET_INTEREST_TAGS = {"InterestIncomeExpenseNonoperatingNet", "InterestIncomeExpenseNet"}

# Multi-stage growth presets, keyed by rough company life-cycle stage.
# high_growth_years: years the reinvestment-rate-derived growth rate holds
# flat before it starts fading. fade_years: years it takes to linearly
# fade down to the terminal growth rate. A flat single-stage DCF is just
# the "mature" case pulled forward to year 1 — fine for a mature company,
# wrong for anything still compounding (see model.project_fcff_multi_stage).
STAGE_PRESETS = {
    "young":   {"high_growth_years": 5, "fade_years": 10},
    "growing": {"high_growth_years": 3, "fade_years": 5},
    "mature":  {"high_growth_years": 0, "fade_years": 3},
}

# Selector aliases so a stage can be typed at the interactive prompt OR
# passed as --stage on the command line (e.g. `--stage growth` to run a
# ticker as a growth company non-interactively) using whichever word
# comes naturally, not just the y/g/m shorthand.
STAGE_ALIASES = {
    "": "mature", "m": "mature", "mature": "mature", "value": "mature", "stable": "mature",
    "y": "young", "young": "young", "early": "young", "startup": "young",
    "g": "growing", "growing": "growing", "growth": "growing",
}


def resolve_stage(raw):
    """
    Turn a stage selector into (high_growth_years, fade_years, label).
    Accepts a preset name/alias (young/growth/mature and synonyms), the
    y/g/m shorthand, a custom "high_years,fade_years" pair, or blank
    (defaults to mature). Falls back to mature (with a printed note) if
    it can't parse the input at all, rather than crashing.
    """
    key = (raw or "").strip().lower()
    if key in STAGE_ALIASES:
        stage_name = STAGE_ALIASES[key]
        preset = STAGE_PRESETS[stage_name]
        return preset["high_growth_years"], preset["fade_years"], stage_name
    try:
        hy, fy = key.split(",")
        return int(hy), int(fy), f"custom({int(hy)},{int(fy)})"
    except ValueError:
        print(f"  Could not parse stage '{raw}' — defaulting to mature.")
        preset = STAGE_PRESETS["mature"]
        return preset["high_growth_years"], preset["fade_years"], "mature"


# Clinical-phase aliases for the --biotech pipeline prompts, mapped to
# model.PHASE_APPROVAL_PROBABILITY's keys. Same idea as STAGE_ALIASES:
# accept whatever comes naturally (p2, 2, phase 2, II).
PHASE_ALIASES = {
    "pre": "preclinical", "preclinical": "preclinical", "0": "preclinical",
    "p1": "phase1", "1": "phase1", "i": "phase1", "phase1": "phase1", "phase 1": "phase1",
    "p2": "phase2", "2": "phase2", "ii": "phase2", "phase2": "phase2", "phase 2": "phase2",
    "p3": "phase3", "3": "phase3", "iii": "phase3", "phase3": "phase3", "phase 3": "phase3",
    "filed": "filed", "f": "filed", "nda": "filed", "bla": "filed",
    "approved": "approved", "a": "approved", "marketed": "approved",
}

# Rough years from TODAY until launch for a drug at each phase — a
# prompt default only, overridable per drug. Approximates typical
# remaining phase durations plus ~1 year of FDA review.
PHASE_DEFAULT_YEARS_TO_LAUNCH = {
    "preclinical": 9, "phase1": 7, "phase2": 5, "phase3": 3, "filed": 1, "approved": 0,
}

# EDGAR facts are reported in the filer's own currency (US domestic 10-K
# filers: USD; foreign private issuers filing 20-F under IFRS: often EUR,
# GBP, JPY, etc). Market data (share price) is always pulled in USD, so
# any non-USD financials get converted below — otherwise WACC/FCFF would
# silently mix currencies.
FX_RATE_URL = "https://api.frankfurter.dev/v1/latest?from={currency}&to=USD"


def fetch_json(url):
    """Fetch a URL and parse it as JSON, sending the required SEC User-Agent header."""
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        print(f"ERROR fetching {url}: HTTP {e.code}")
        if e.code == 403:
            print("This usually means the User-Agent header is missing or malformed.")
            print("Edit YOUR_NAME and YOUR_EMAIL at the top of this script.")
        sys.exit(1)


def get_cik_for_ticker(ticker):
    """Look up a company's 10-digit zero-padded CIK from its ticker symbol."""
    data = fetch_json(EDGAR_TICKER_MAP_URL)
    ticker = ticker.upper()
    for entry in data.values():
        if entry["ticker"].upper() == ticker:
            return str(entry["cik_str"]).zfill(10), entry["title"]
    print(f"Could not find ticker '{ticker}' in SEC's company list.")
    sys.exit(1)


def _is_full_year_period(entry):
    """True if an XBRL fact's start/end span a genuine ~12-month period.

    Guards against a real EDGAR quirk: many 10-Ks/20-Fs include a
    "Quarterly Data" footnote that discloses each quarter's figure (e.g.
    Q1-Q4 operating income) INSIDE the annual filing. Those quarterly
    facts are tagged with the same form and fiscal year, and the Q4
    entry's "end" date is identical to the full year's "end" date (both
    fall on the fiscal year-end). Without this check, that can pick up a
    single quarter's figure instead of the full year's — silently
    understating flow metrics (revenue, EBIT, etc.) by roughly 4x.
    Point-in-time facts (balance sheet items like cash or debt) have no
    "start" date and aren't affected by this issue.
    """
    from datetime import date
    start = entry.get("start")
    end = entry.get("end")
    if not start or not end:
        return True  # instantaneous fact (e.g. balance sheet item) — no duration to check
    try:
        d1 = date.fromisoformat(start)
        d2 = date.fromisoformat(end)
    except ValueError:
        return True  # unparseable dates — don't block on this, let it through
    days = (d2 - d1).days
    return 335 <= days <= 380  # true annual period, not a quarter


def _all_taxonomy_tags(facts_json):
    """Merge every taxonomy EDGAR gives us for this filer (us-gaap for
    domestic 10-K filers, ifrs-full for foreign private issuers filing
    20-F) into one dict — tag names don't collide across taxonomies in
    practice."""
    all_tags = {}
    for taxonomy_facts in facts_json.get("facts", {}).values():
        all_tags.update(taxonomy_facts)
    return all_tags


def _recipe_annual_entries(all_tags, recipe):
    """
    A "recipe" is either a single XBRL tag name (str), or a list/tuple of
    tag names whose values get SUMMED — for filers who report a metric as
    separate line items (e.g. Okta/Accenture report D&A as "Depreciation"
    + "AmortizationOfIntangibleAssets" instead of one combined tag)
    instead of one pre-totaled concept.

    Summing is only valid when every tag in the recipe reports the SAME
    period (same end date) in the SAME currency — otherwise you'd add a
    prior year's depreciation to this year's amortization, or EUR to USD.
    So this requires ALL of the recipe's tags to have a matching-period,
    matching-unit entry before it counts that period at all; a recipe
    with one missing tag contributes nothing, not a silent partial sum.

    Returns {end_date: (summed_val, unit, fiscal_year)} for every period
    where the whole recipe resolved.
    """
    tags = (recipe,) if isinstance(recipe, str) else tuple(recipe)
    per_tag_periods = []
    for tag in tags:
        if tag not in all_tags:
            return {}
        units = all_tags[tag].get("units", {})
        periods = {}
        for unit_key in units:  # whatever currency/unit this filer actually reports in — USD, EUR, CAD, shares, ...
            for e in units[unit_key]:
                if e.get("form") not in ("10-K", "20-F", "40-F") or e.get("fy") is None:
                    continue
                if not _is_full_year_period(e):
                    continue
                periods[e.get("end", "")] = (e["val"], unit_key, e.get("fy"))
        if not periods:
            return {}
        per_tag_periods.append(periods)

    common_dates = set(per_tag_periods[0])
    for p in per_tag_periods[1:]:
        common_dates &= set(p)

    result = {}
    for end_date in common_dates:
        entries_here = [p[end_date] for p in per_tag_periods]
        units_here = {unit for _, unit, _ in entries_here}
        if len(units_here) != 1:
            continue  # tags disagree on currency for this period — can't sum, skip it
        total = sum(val for val, _, _ in entries_here)
        result[end_date] = (total, units_here.pop(), entries_here[0][2])
    return result


def _recipe_label(recipe):
    """Human-readable tag label for a recipe, for the "(tag: ...)" report line."""
    return recipe if isinstance(recipe, str) else "+".join(recipe)


def get_latest_annual_value(facts_json, tag_candidates):
    """
    Given the full EDGAR companyfacts JSON and a list of candidate
    "recipes" (see _recipe_annual_entries — each one a single tag name,
    or a list of tag names to sum for filers who split a metric across
    several line items), return the most recent ANNUAL (10-K, 20-F, or
    40-F — a Canadian MJDS filer's annual-report equivalent) value
    across ALL candidates — not just the first one that happens to
    have data. Companies switch tags over time (e.g. Apple reported
    revenue under "Revenues" through FY2018, then switched to
    "RevenueFromContractWithCustomerExcludingAssessedTax" after adopting
    ASC 606). We collect the latest entry from every candidate and pick
    whichever is most recent overall, instead of stopping at the first
    one with any data. On a tie (two candidates both resolve the same
    period), the one listed FIRST wins — so list a more complete/precise
    recipe before a rougher fallback for the same underlying figure.

    Returns (value, tag_used, fiscal_year, unit) or (None, None, None, None)
    if no candidate had any valid annual data. `unit` is the currency the
    value is reported in ("USD", "EUR", ...) or "shares" for share counts
    — callers need this to know whether FX conversion is required.
    """
    all_tags = _all_taxonomy_tags(facts_json)
    best = None  # will hold (end_date, val, label, fy, unit)

    for recipe in tag_candidates:
        periods = _recipe_annual_entries(all_tags, recipe)
        if not periods:
            continue
        end_date = max(periods)
        val, unit, fy = periods[end_date]
        if best is None or end_date > best[0]:
            best = (end_date, val, _recipe_label(recipe), fy, unit)

    if best is None:
        return None, None, None, None
    _, val, tag, fy, unit = best
    return val, tag, fy, unit


def get_annual_series(facts_json, tag_candidates, years=5):
    """
    Like get_latest_annual_value, but returns up to the last `years`
    annual data points instead of just the latest one — so a metric can
    be trailing-averaged rather than read off a single (possibly lumpy)
    year. CapEx especially, and to a lesser extent D&A, can swing a lot
    year to year (a big equipment buy, an acquisition-driven step-up in
    amortization); averaging over several years is the standard way to
    normalize that before it feeds into FCFF or the reinvestment rate.

    Deduplicates by period end date — if a company switches tags (or
    reporting styles) mid-series, more than one candidate recipe can
    resolve the same fiscal year-end. The recipe that appears FIRST in
    tag_candidates wins that tie, matching get_latest_annual_value.

    Returns a list of dicts {"end", "val", "tag", "fiscal_year", "unit"}
    sorted oldest-to-newest (length up to `years`, possibly shorter or
    empty if the filer has less history or no candidate resolved).
    """
    all_tags = _all_taxonomy_tags(facts_json)
    by_end_date = {}

    for recipe in tag_candidates:
        periods = _recipe_annual_entries(all_tags, recipe)
        label = _recipe_label(recipe)
        for end_date, (val, unit, fy) in periods.items():
            if end_date not in by_end_date:
                by_end_date[end_date] = {
                    "end": end_date, "val": val, "tag": label,
                    "fiscal_year": fy, "unit": unit,
                }

    entries = sorted(by_end_date.values(), key=lambda e: e["end"])
    return entries[-years:]


def trailing_average(facts_json, tag_candidates, years=5):
    """
    Average a metric's annual values over up to the last `years` fiscal
    years instead of reading just the latest one. Returns
    (avg_value, unit, year_range_str, n_years_used), or
    (None, None, None, 0) if no data was found at all.
    """
    series = get_annual_series(facts_json, tag_candidates, years)
    if not series:
        return None, None, None, 0
    units_seen = {e["unit"] for e in series}
    if len(units_seen) > 1:
        # Reporting currency changed mid-series (rare) — use only the
        # most recent currency's entries so we don't average across FX.
        latest_unit = series[-1]["unit"]
        series = [e for e in series if e["unit"] == latest_unit]
    avg = sum(e["val"] for e in series) / len(series)
    year_range = f"FY{series[0]['fiscal_year']}-FY{series[-1]['fiscal_year']}"
    return avg, series[-1]["unit"], year_range, len(series)


def trailing_effective_tax_rate(facts_json, years=5, min_rate=0.0, max_rate=0.50, fallback=0.21):
    """
    Average the effective tax rate (tax expense / pretax income) over up
    to `years` fiscal years instead of reading a single year. A cyclical
    company can have a near-zero or even negative pretax income in a
    trough year — tax expense doesn't collapse in lockstep (taxes owed
    across jurisdictions don't move with a thin consolidated pretax
    number), so the ratio blows up to something absurd (seen on Vishay:
    -1,145% one year, 135% the next) even in years with perfectly normal
    tax expense.

    Averaging the raw ratios doesn't fix this — one absurd year still
    dominates the average. Instead, each year's ratio is sanity-checked
    against [min_rate, max_rate] (a normal real-world range) and only the
    years that pass get averaged; a year distorted by a near-zero
    denominator is excluded rather than diluted in. Falls back to
    `fallback` (the US statutory rate) if no year in the window has a
    sane ratio at all.

    Returns (rate, year_range_str_or_None, n_years_used).
    """
    tax_series = get_annual_series(facts_json, METRIC_TAGS["income_tax_expense"], years)
    pretax_series = get_annual_series(facts_json, METRIC_TAGS["pretax_income"], years)
    pretax_by_end = {e["end"]: e for e in pretax_series}

    sane_rates, fiscal_years = [], []
    for tax_entry in tax_series:
        pretax_entry = pretax_by_end.get(tax_entry["end"])
        if pretax_entry is None or pretax_entry["val"] == 0:
            continue
        rate = tax_entry["val"] / pretax_entry["val"]
        if min_rate <= rate <= max_rate:
            sane_rates.append(rate)
            fiscal_years.append(tax_entry["fiscal_year"])

    if not sane_rates:
        return fallback, None, 0
    year_range = f"FY{min(fiscal_years)}-FY{max(fiscal_years)}" if len(fiscal_years) > 1 else f"FY{fiscal_years[0]}"
    return sum(sane_rates) / len(sane_rates), year_range, len(sane_rates)


def trailing_revenue_cagr(facts_json, years=5):
    """
    Compound annual growth rate of revenue over the trailing window —
    printed as a REFERENCE point before the narrative DCF's initial-
    growth-rate prompt (model.py's project_revenue_path), not used to
    set anything automatically. A young company's own trailing CAGR can
    be a reasonable anchor, or it can be noisy/not predictive at all
    (a huge base-effect year, a recent pivot) — it's a data point for
    judgment, not a substitute for it.

    Returns (cagr, year_range_str), or (None, None) if fewer than two
    data points are available, or the oldest value isn't positive
    (a CAGR over a loss-to-something swing isn't a meaningful number).
    """
    series = get_annual_series(facts_json, METRIC_TAGS["revenue"], years)
    if len(series) < 2 or series[0]["val"] <= 0:
        return None, None
    oldest, newest = series[0], series[-1]
    n_periods = newest["fiscal_year"] - oldest["fiscal_year"]
    if n_periods <= 0:
        return None, None
    cagr = (newest["val"] / oldest["val"]) ** (1 / n_periods) - 1
    return cagr, f"FY{oldest['fiscal_year']}-FY{newest['fiscal_year']}"


def trailing_normalized_ebit(facts_json, current_revenue, years=5):
    """
    When the latest fiscal year's EBIT is negative, the single-year FCFF
    DCF breaks (see the negative-FCFF warning in run_full_valuation) —
    whether from genuine distress or a cyclical trough. One fix would be
    reverting to the last year EBIT was positive, but that reuses a
    stale absolute dollar figure: if revenue has grown (or shrunk) since
    then, that old EBIT no longer reflects the business at its current
    scale. Instead, this averages the EBIT MARGIN (EBIT/revenue) over
    trailing years where EBIT was positive, then applies that margin to
    CURRENT revenue — smoothing past the bad year while keeping the
    dollar scale current.

    Only positive-EBIT years count toward the average (the margin
    equivalent of "the last year that showed a positive growth rate").

    Returns (normalized_ebit, margin, year_range_str, n_years_used), or
    (None, None, None, 0) if no trailing year had positive EBIT at all —
    true for companies that have never been GAAP-profitable (Zscaler,
    Snowflake, Cloudflare, Datadog, Samsara all fall in this bucket this
    session); the EV/Revenue fallback is the only option for those.
    """
    ebit_series = get_annual_series(facts_json, METRIC_TAGS["operating_income"], years)
    revenue_series = get_annual_series(facts_json, METRIC_TAGS["revenue"], years)
    revenue_by_end = {e["end"]: e for e in revenue_series}

    margins, fiscal_years = [], []
    for ebit_entry in ebit_series:
        if ebit_entry["val"] <= 0:
            continue
        revenue_entry = revenue_by_end.get(ebit_entry["end"])
        if revenue_entry is None or revenue_entry["val"] == 0:
            continue
        margins.append(ebit_entry["val"] / revenue_entry["val"])
        fiscal_years.append(ebit_entry["fiscal_year"])

    if not margins:
        return None, None, None, 0
    avg_margin = sum(margins) / len(margins)
    year_range = f"FY{min(fiscal_years)}-FY{max(fiscal_years)}" if len(fiscal_years) > 1 else f"FY{fiscal_years[0]}"
    return avg_margin * current_revenue, avg_margin, year_range, len(margins)


def trailing_dividend_growth_rate(facts_json, years=6):
    """
    Average year-over-year dividend-per-share growth over the trailing
    window. This is the standard Gordon Growth Model input for a
    capital-intensive, dividend-paying business (utilities, REITs) where
    nearly all earnings get reinvested into a growing rate base rather
    than showing up as discretionary FCFF — see trailing_normalized_ebit
    and the DDM functions in model.py for why a dividend discount model
    is the right tool for that profile instead of an FCFF-based DCF.

    Uses one more year than the other trailing windows by default
    (years=6) since computing N growth rates needs N+1 data points.

    Returns (latest_dps, avg_growth_rate, year_range_str, n_growth_points, unit),
    or (None, None, None, 0, None) if the filer doesn't pay a dividend at
    all (no data under either candidate tag) or has fewer than 2 years of
    dividend history to compute even one growth rate from. `unit` is the
    per-share unit the dividend is reported in (e.g. "USD/shares") — the
    caller needs this to know whether latest_dps requires FX conversion;
    this function doesn't convert it itself since growth_rate is a ratio
    that's currency-invariant regardless.
    """
    series = get_annual_series(facts_json, METRIC_TAGS["dividends_per_share"], years)
    if len(series) < 2:
        return None, None, None, 0, None

    growth_rates = []
    for prev, curr in zip(series, series[1:]):
        if prev["val"] > 0:
            growth_rates.append(curr["val"] / prev["val"] - 1)

    if not growth_rates:
        return series[-1]["val"], None, None, 0, series[-1]["unit"]
    year_range = f"FY{series[0]['fiscal_year']}-FY{series[-1]['fiscal_year']}"
    return series[-1]["val"], sum(growth_rates) / len(growth_rates), year_range, len(growth_rates), series[-1]["unit"]


def freshest_annual_value(facts_json, tag_candidates):
    """
    Like get_latest_annual_value, but instead of taking the FIRST tag
    in the list that has any data, checks every tag and returns the one
    with the most recent fiscal year. Filers switch tags over time, and
    REITs are especially prone to it — e.g. Prologis stopped using
    DepreciationDepletionAndAmortization in FY2015 for
    DepreciationAndAmortization, and CommonStockDividendsPerShareDeclared
    is years stale for Prologis and Simon while ...CashPaid is current.
    Returns (value, tag, fiscal_year, unit), all None if no tag has data.
    """
    best = (None, None, None, None)
    for tag in tag_candidates:
        val, found_tag, fy, unit = get_latest_annual_value(facts_json, [tag])
        if val is not None and fy is not None and (best[2] is None or fy > best[2]):
            best = (val, found_tag, fy, unit)
    return best


def reit_metric(facts_json, label, tag_candidates, latest_fy, fx_currency, fx_rate, default, per_share=False):
    """
    Pull one REIT-specific figure (freshest tag wins), show where it came
    from, and let the user keep it or type a correction. Falls back to
    `default` when no tag has data for the latest fiscal year — a stale
    figure is shown for reference but not used, since a REIT's asset
    base and payout can move a lot in two or three years.
    """
    val, tag, fy, unit = freshest_annual_value(facts_json, tag_candidates)
    if val is not None and fx_currency and unit and (
            unit == fx_currency or (per_share and unit.startswith(f"{fx_currency}/"))):
        val *= fx_rate
    fmt = (lambda x: f"${x:,.2f}") if per_share else (lambda x: f"${x:,.0f}")
    if val is not None and fy is not None and fy >= latest_fy:
        print(f"  {label}: {fmt(val)}   (tag: {tag}, FY{fy})")
        chosen = val
    else:
        if val is not None:
            print(f"  {label}: {fmt(val)} on record is STALE (tag: {tag}, FY{fy}) — not used.")
        else:
            print(f"  {label}: not found on EDGAR.")
        print(f"    Using {fmt(default)} instead.")
        chosen = default
    override = input(f"    Press Enter to keep {fmt(chosen)}, or type a corrected amount: ").strip()
    return parse_number(override) if override else chosen


def get_market_data(ticker):
    """
    Try to pull beta and current share price, trying each source in turn:
    yfinance (if installed — free, no API key needed) first, then
    Financial Modeling Prep (if a key is set), then manual entry. Each
    tier falls through to the next on any failure (not installed, missing
    key, rate limit, network issue, missing field) — the script always
    produces a result, it never hard-fails just because a market-data
    source had a problem.
    """
    if YFINANCE_AVAILABLE:
        try:
            info = yf.Ticker(ticker).info
            beta = info.get("beta")
            price = info.get("currentPrice") or info.get("regularMarketPrice")
            if beta is not None and price is not None:
                print(f"  Pulled from yfinance: beta={beta}, share price=${price}")
                return float(beta), float(price)
            else:
                print("  yfinance response was missing beta or price — trying next source.")
        except Exception as e:
            print(f"  yfinance request failed ({e}) — trying next source.")

    if FMP_API_KEY:
        url = FMP_PROFILE_URL.format(ticker=ticker.upper(), key=FMP_API_KEY)
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data and isinstance(data, list) and len(data) > 0:
                profile = data[0]
                beta = profile.get("beta")
                price = profile.get("price")
                if beta is not None and price is not None:
                    print(f"  Pulled from FMP: beta={beta}, share price=${price}")
                    return float(beta), float(price)
                else:
                    print("  FMP response was missing beta or price — falling back to manual entry.")
            else:
                print("  FMP returned no data for this ticker — falling back to manual entry.")
        except HTTPError as e:
            print(f"  FMP request failed (HTTP {e.code}) — falling back to manual entry.")
        except Exception as e:
            print(f"  FMP request failed ({e}) — falling back to manual entry.")
    else:
        print("  No FMP API key set — using manual entry.")

    # Manual fallback
    print(f"  Look up '{ticker.upper()}' on Yahoo Finance or Google Finance for these:")
    beta = parse_number(input("  Enter beta (e.g. 1.2): "))
    price = parse_number(input("  Enter current share price (e.g. 185.50): "))
    return beta, price


def parse_number(text):
    """Accept numbers typed with commas or a leading $ (e.g. '5,000,000,000'
    or '$186.72') in addition to plain digits."""
    cleaned = text.strip().replace(",", "").replace("$", "")
    return float(cleaned)


def get_metric_value(financials, key, label, stale_metrics=None, return_override_flag=False):
    """Pull a metric's value out of the financials dict, or prompt the
    user to type it in manually if EDGAR didn't have it under any of our
    candidate tags.

    If `key` is in `stale_metrics` (metrics print_financials_report
    flagged as years behind the others — a real, seen-this-session
    failure mode: a stale tag can resolve to a non-None value from a
    LONG-outdated filing, which previously meant it silently fed bad
    data into the DCF with no chance to fix it, unlike genuinely MISSING
    data which already got this same override opportunity), offer the
    same manual-override prompt as missing data, except pressing Enter
    keeps the stale value instead of being required to type something.

    If return_override_flag is True, returns (value, was_overridden)
    instead of just value — was_overridden is True whenever the number
    came from the user rather than EDGAR (a typed stale-override, or a
    manually-entered missing value). Callers that separately compute a
    multi-year trailing average from the SAME (possibly equally stale)
    tag data need this flag: otherwise a user's correction to the latest
    year gets silently discarded whenever the trailing-average lookup
    still finds 2+ historical data points from that same outdated tag —
    which was exactly the bug this parameter exists to prevent.
    """
    info = financials.get(key, {})
    if info.get("value") is not None:
        if stale_metrics and key in stale_metrics:
            print(f"  '{label}' resolved to a STALE value: {info['value']:,.0f} (FY{info['fiscal_year']}).")
            override = input(f"  Press Enter to keep it, or type a corrected {label} "
                              "(in dollars, e.g. 1000000000): ").strip()
            if override:
                val = parse_number(override)
                return (val, True) if return_override_flag else val
        return (info["value"], False) if return_override_flag else info["value"]
    print(f"  '{label}' was not found on EDGAR under any known tag.")
    val = parse_number(input(f"  Enter {label} manually (in dollars, e.g. 1000000000): "))
    return (val, True) if return_override_flag else val


def pull_financials(cik):
    """Pull all the metrics we need for a given CIK. Returns (results, facts):
    results is a dict of metric_name -> {value, tag, fiscal_year, unit}, with
    None values for anything EDGAR didn't have under any of our candidate
    tags; facts is the raw companyfacts JSON, kept around so callers can
    also pull a multi-year series (see get_annual_series/trailing_average)."""
    facts = fetch_json(EDGAR_FACTS_URL.format(cik=cik))
    results = {}
    for metric, tags in METRIC_TAGS.items():
        val, tag, fy, unit = get_latest_annual_value(facts, tags)
        results[metric] = {"value": val, "tag": tag, "fiscal_year": fy, "unit": unit}
    return results, facts


def get_fx_rate_to_usd(currency):
    """Look up a live currency->USD rate (ECB-sourced, via frankfurter.dev,
    no API key required). Falls back to manual entry if the request fails."""
    url = FX_RATE_URL.format(currency=currency)
    try:
        data = fetch_json_or_none(url)
        if data:
            return data["rates"]["USD"], data.get("date")
    except Exception:
        pass
    print(f"  Could not fetch a live {currency}->USD exchange rate.")
    rate = parse_number(input(f"  Enter the {currency}->USD exchange rate manually (e.g. 1.16): "))
    return rate, None


# Yahoo's options-chain endpoint expects a browser-style request (unlike
# EDGAR, which explicitly wants an identifying User-Agent) and requires a
# session cookie + CSRF "crumb" token — a plain request gets rejected with
# "Invalid Crumb". It's also unofficial and rate-limits aggressively, so
# every function touching it is designed to degrade silently (return
# None) rather than block the rest of a valuation run.
YAHOO_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def get_yahoo_session():
    """
    Yahoo's own web client fetches a session cookie, then a CSRF crumb
    tied to it, before any options-chain request succeeds. Returns
    (opener, crumb) — the opener carries the cookie jar for all
    subsequent requests — or (None, None) if anything in the sequence
    fails.
    """
    cookie_jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookie_jar))
    try:
        opener.open(Request("https://fc.yahoo.com", headers={"User-Agent": YAHOO_USER_AGENT}), timeout=10)
    except Exception:
        pass  # this URL 404s even in the successful case — only the cookie it sets matters
    try:
        crumb_resp = opener.open(
            Request("https://query2.finance.yahoo.com/v1/test/getcrumb",
                     headers={"User-Agent": YAHOO_USER_AGENT}), timeout=10)
        crumb = crumb_resp.read().decode("utf-8").strip()
        if not crumb or "Too Many Requests" in crumb or "<html" in crumb.lower():
            return None, None
        return opener, crumb
    except Exception:
        return None, None


def get_implied_volatility(ticker, target_days=30):
    """
    Current market-implied volatility from Yahoo Finance's live options
    chain — a genuinely better volatility reference than a company's own
    disclosed Black-Scholes grant-date assumption, which can be stale by
    a year or more (see the option-dilution discussion this was scoped
    alongside). PRINTED AS A REFERENCE DATA POINT ONLY — nothing in this
    tool currently consumes it as a model input; the full option-
    dilution build (Black-Scholes subtraction from equity value) was
    deliberately not built, so there's nothing downstream for this
    number to feed yet.

    Picks the expiration closest to `target_days` days out (avoiding
    0DTE/weekly noise right at expiration), then averages the at-the-
    money call's and put's implied volatility at that expiration — a
    simple straddle-style estimate, not a full volatility-surface fit.

    Returns (implied_vol, expiration_date_str, days_out), or
    (None, None, None) if the pull fails for any reason.
    """
    opener, crumb = get_yahoo_session()
    if opener is None:
        return None, None, None
    try:
        base_url = f"https://query1.finance.yahoo.com/v7/finance/options/{ticker}?crumb={crumb}"
        base_resp = opener.open(Request(base_url, headers={"User-Agent": YAHOO_USER_AGENT}), timeout=10)
        result = json.loads(base_resp.read().decode("utf-8"))["optionChain"]["result"][0]
        current_price = result["quote"]["regularMarketPrice"]
        expiration_dates = result["expirationDates"]
        if not expiration_dates:
            return None, None, None

        now = time.time()
        target_ts = now + target_days * 86400
        best_expiration = min(expiration_dates, key=lambda ts: abs(ts - target_ts))
        days_out = round((best_expiration - now) / 86400)

        exp_resp = opener.open(
            Request(f"{base_url}&date={best_expiration}", headers={"User-Agent": YAHOO_USER_AGENT}), timeout=10)
        chain = json.loads(exp_resp.read().decode("utf-8"))["optionChain"]["result"][0]["options"][0]

        def atm_implied_vol(contracts):
            if not contracts:
                return None
            closest = min(contracts, key=lambda c: abs(c["strike"] - current_price))
            return closest.get("impliedVolatility")

        ivs = [iv for iv in (atm_implied_vol(chain.get("calls", [])),
                              atm_implied_vol(chain.get("puts", []))) if iv is not None]
        if not ivs:
            return None, None, None

        exp_date_str = datetime.fromtimestamp(best_expiration, tz=timezone.utc).strftime("%Y-%m-%d")
        return sum(ivs) / len(ivs), exp_date_str, days_out
    except Exception:
        return None, None, None


def fetch_json_or_none(url):
    """Like fetch_json, but returns None on failure instead of exiting —
    used for the FX lookup, which has a manual-entry fallback."""
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def get_reporting_currency(financials):
    """The non-USD currency EDGAR's financials came back in, if any (a
    single filer normally reports in one currency). Excludes "shares"
    and compound per-share units like "USD/shares" (dividends per
    share, EPS-shaped tags) — those aren't currencies to convert, they
    already carry whatever currency they're denominated in baked into
    the unit string itself (XBRL's "/" units aren't on our FX radar)."""
    currencies = {
        info["unit"] for info in financials.values()
        if info["unit"] not in (None, "shares", "USD") and "/" not in info["unit"]
    }
    return sorted(currencies)[0] if currencies else None


def convert_financials_to_usd(financials, currency, rate):
    """EDGAR reports each filer's financials in its own currency — USD for
    domestic 10-K filers, but often EUR/GBP/JPY for a 20-F foreign private
    issuer. Market data (share price) is always USD, so anything not
    already in USD gets converted here before it touches the DCF math."""
    for info in financials.values():
        if info["unit"] == currency and info["value"] is not None:
            info["value"] *= rate
            info["unit"] = "USD"


def print_financials_report(company_name, financials):
    print(f"\n{'='*60}")
    print(f"  EDGAR data pulled for: {company_name}")
    print(f"{'='*60}")
    missing = []
    stale = []

    fiscal_years = [info["fiscal_year"] for info in financials.values() if info["fiscal_year"] is not None]
    most_recent_fy = max(fiscal_years) if fiscal_years else None

    for metric, info in financials.items():
        if info["value"] is None:
            missing.append(metric)
            print(f"  {metric:28s}: NOT FOUND (check manually)")
        else:
            val = info["value"]
            display = f"{val:,.0f}" if abs(val) >= 1000 else f"{val:,.4f}"
            flag = ""
            if most_recent_fy is not None and info["fiscal_year"] is not None \
                    and most_recent_fy - info["fiscal_year"] >= 2:
                flag = "  ⚠ STALE — years behind other metrics, verify manually"
                stale.append(metric)
            print(f"  {metric:28s}: {display:>20s}   (tag: {info['tag']}, FY{info['fiscal_year']}){flag}")

    if stale:
        print(f"\n  ⚠⚠ WARNING — possibly stale data: {', '.join(stale)}")
        print("     This usually means the company switched XBRL tags and this")
        print("     script's fallback list doesn't include their current one yet.")
        print("     Don't trust these figures without checking the actual 10-K.")
    if missing:
        print(f"\n  ⚠ Missing metrics: {', '.join(missing)}")
        print("    You'll need to fill these in manually from the 10-K.")
    print()
    return missing, stale


def run_full_valuation(ticker, stage_arg=None, narrative=False, biotech=False, reit=False):
    cik, company_name = get_cik_for_ticker(ticker)
    print(f"Found CIK {cik} for {company_name}")

    financials, facts = pull_financials(cik)
    fx_currency = get_reporting_currency(financials)
    fx_rate = 1.0
    if fx_currency:
        fx_rate, fx_as_of = get_fx_rate_to_usd(fx_currency)
        note = f"as of {fx_as_of}" if fx_as_of else "manually entered"
        # Some foreign filers convenience-translate most figures to USD
        # but leave a specific disclosure (e.g. a notes-only line item)
        # tagged only in the local currency — conversion below is already
        # per-metric (only touches whatever's actually tagged in
        # fx_currency), so say precisely which metrics are affected
        # rather than implying the whole filing is in that currency.
        # Compare against metrics that COULD plausibly be in a foreign
        # currency (excluding share counts and per-share compounds, which
        # can never equal a bare currency code like "EUR") — not the full
        # financials dict, which always includes those and would make
        # the "fully uniform" case below essentially unreachable.
        currency_eligible = [key for key, info in financials.items()
                              if info["unit"] not in (None, "shares") and "/" not in info["unit"]]
        affected = [key for key in currency_eligible if financials[key]["unit"] == fx_currency]
        if len(affected) == len(currency_eligible):
            scope = "Financials are reported in"
        else:
            scope = f"{', '.join(affected)} (only) reported in"
        print(f"\n  {scope} {fx_currency} — converting to USD at "
              f"{fx_rate:.4f} ({note}) to match the USD market data below.")
        convert_financials_to_usd(financials, fx_currency, fx_rate)
    missing, stale = print_financials_report(company_name, financials)

    print(f"{'='*60}")
    print("  Market data")
    print(f"{'='*60}")
    beta, share_price = get_market_data(ticker)

    # ---- Implied volatility: REFERENCE ONLY — nothing below consumes
    # this as a model input (no option-dilution feature was built; see
    # that discussion). Printed because it's a genuinely better read on
    # the market's own risk assessment than a company's stale disclosed
    # Black-Scholes assumption, in case you want it for your own
    # judgment calls elsewhere (e.g. sizing the WACC-glide spread, or a
    # qualitative adjustment). Best-effort: Yahoo's options endpoint is
    # unofficial, rate-limited, and can fail silently — that's fine,
    # this never blocks the rest of the run. ----
    implied_vol, iv_expiration, iv_days_out = get_implied_volatility(ticker)
    if implied_vol is not None:
        print(f"  Market-implied volatility (~{iv_days_out}-day, ATM straddle, exp {iv_expiration}): "
              f"{implied_vol:.1%}  (reference only — not used below)")
    else:
        print("  Market-implied volatility: unavailable (Yahoo's options endpoint didn't respond —")
        print("  unofficial/rate-limited, this is expected to fail sometimes).")

    # ---- Assemble DCF inputs ----
    revenue = get_metric_value(financials, "revenue", "revenue", stale)
    ebit = get_metric_value(financials, "operating_income", "EBIT / operating income", stale)

    # ---- R&D capitalization (Damodaran-style) ----
    # Accounting expenses R&D as incurred, but for an R&D-driven business
    # (software, pharma, semis) it's economically an investment in future
    # earning power, no different from capex — just sitting in the wrong
    # line item. Left as expensed, EBIT is understated, invested capital
    # excludes the entire cumulative research asset, and the reinvestment
    # rate can't see R&D spending as "reinvestment" at all. For an
    # R&D-heavy company with weak or negative reported EBIT, THIS is
    # usually the real reason the FCFF DCF looks broken, not distress —
    # run this before the negative-EBIT check below so a genuine R&D
    # distortion gets fixed before falling back to margin normalization.
    rd_result = None
    latest_rd_raw, _rd_tag, _rd_fy, _rd_unit = get_latest_annual_value(facts, METRIC_TAGS["rd_expense"])
    if latest_rd_raw is not None:
        # Convert ONLY if THIS metric's own unit is actually fx_currency —
        # a filer can convenience-translate most figures to USD while
        # leaving a specific disclosure (e.g. a notes-only line item) in
        # the local currency (see get_reporting_currency's docstring).
        # Blanket-applying fx_rate just because SOME OTHER metric needed
        # conversion would wrongly shrink/inflate a figure that was
        # already correctly in USD (caught on Alibaba: R&D was already
        # USD, but got multiplied by the CNY rate meant for D&A alone).
        rd_is_fx = _rd_unit == fx_currency
        latest_rd_display = latest_rd_raw * fx_rate if rd_is_fx else latest_rd_raw
        print(f"\n{'='*60}")
        print("  R&D capitalization (Damodaran-style)")
        print(f"{'='*60}")
        print(f"  This company reports R&D expense (latest FY: ${latest_rd_display:,.0f}"
              + (f", converted from {fx_currency}" if rd_is_fx else "") + ").")
        print("  Capitalizing it like capex: amortized straight-line over an assumed useful")
        print("  life, added back to EBIT net of that amortization, and added to invested")
        print("  capital. Typical lives: ~2yr consumer/retail, ~3yr tech/software, ~5yr")
        print("  industrial, ~10yr pharma/biotech — an industry judgment call, not something")
        print("  this script can determine for you.")
        default_life = 10 if biotech else 3
        life_input = input(f"  Amortizable life in years (Enter for {default_life}): ").strip()
        try:
            amortizable_life = int(parse_number(life_input)) if life_input else default_life
            if not (1 <= amortizable_life <= 30):
                raise ValueError
        except ValueError:
            print(f"  Not a valid life (must be a whole number from 1-30) — defaulting to {default_life}.")
            amortizable_life = default_life
        rd_series_raw = get_annual_series(facts, METRIC_TAGS["rd_expense"], years=amortizable_life)
        rd_values = [e["val"] * fx_rate if e["unit"] == fx_currency else e["val"] for e in rd_series_raw]
        rd_result = model.capitalize_rd(rd_values, amortizable_life)
        print(f"  Current-year R&D:                          ${rd_result['current_rd']:,.0f}")
        print(f"  Amortization of prior years' R&D:          ${rd_result['amortization']:,.0f}")
        print(f"  Research asset (added to invested capital): ${rd_result['research_asset']:,.0f}")
        print(f"  EBIT adjustment (added to reported EBIT):   {rd_result['ebit_adjustment']:+,.0f}")
        if rd_result["years_of_data"] < amortizable_life:
            print(f"  Note: only {rd_result['years_of_data']} year(s) of R&D history available "
                  f"(need {amortizable_life}) — the research asset is understated for a company")
            print("  this young, since it hasn't had time to build one up yet.")
        ebit += rd_result["ebit_adjustment"]
        print(f"  EBIT after R&D capitalization:              ${ebit:,.0f}")

    if ebit < 0:
        latest_ebit = ebit
        normalized_ebit, margin, margin_years, margin_n = trailing_normalized_ebit(facts, revenue)
        print(f"\n{'='*60}")
        print("  EBIT normalization (latest FY is negative)")
        print(f"{'='*60}")
        print(f"  Latest FY EBIT: ${latest_ebit:,.0f}" +
              (f"  (margin {latest_ebit / revenue:.1%})" if revenue else "  (no revenue yet)"))
        if normalized_ebit is not None:
            print(f"  Normalized:     ${normalized_ebit:,.0f}  ({margin:.1%} margin, averaged over "
                  f"{margin_n} profitable year(s), {margin_years}, applied to current revenue)")
            print("  Using the normalized figure below instead of the negative latest-FY EBIT.")
            ebit = normalized_ebit
        else:
            print("  No trailing year had positive EBIT at all — this company has never been")
            print("  GAAP-profitable, so there's no margin to normalize. Keeping the negative")
            print("  latest-FY EBIT; the FCFF-based DCF below will likely still break (see the")
            print("  negative-FCFF warning) — the EV/Revenue fallback is the better tool here.")

    pretax_income = get_metric_value(financials, "pretax_income", "pretax income", stale)
    tax_expense = get_metric_value(financials, "income_tax_expense", "income tax expense", stale)

    # CapEx and D&A are naturally lumpy (a big equipment buy, an
    # acquisition-driven step-up in amortization) — a single year can swing
    # the reinvestment rate and FCFF hard in either direction. Damodaran
    # normalizes both over a multi-year window before using them, so we
    # use a trailing 5-year average here instead of the latest year alone.
    latest_d_and_a, d_and_a_overridden = get_metric_value(
        financials, "depreciation_amortization", "depreciation & amortization", stale, return_override_flag=True)
    latest_capex, capex_overridden = get_metric_value(
        financials, "capex", "capital expenditures", stale, return_override_flag=True)
    da_avg, _da_unit, da_years, da_n = trailing_average(facts, METRIC_TAGS["depreciation_amortization"])
    capex_avg, _capex_unit, capex_years, capex_n = trailing_average(facts, METRIC_TAGS["capex"])
    # Convert only the series that's actually denominated in fx_currency —
    # trailing_average reports each series' OWN unit; trust that, not the
    # filer-wide fx_currency flag (see the R&D fix above for why).
    if da_avg is not None and _da_unit == fx_currency:
        da_avg *= fx_rate
    if capex_avg is not None and _capex_unit == fx_currency:
        capex_avg *= fx_rate
    # A stale-override (or manually-entered missing value) takes precedence
    # over the trailing average even when that average found 2+ data
    # points — those points come from the SAME tag the override just
    # corrected, so the "average" is built from equally outdated data,
    # not a genuine multi-year normalization.
    use_da_avg = da_n >= 2 and not d_and_a_overridden
    use_capex_avg = capex_n >= 2 and not capex_overridden
    d_and_a = da_avg if use_da_avg else latest_d_and_a
    capex = capex_avg if use_capex_avg else latest_capex

    print(f"\n{'='*60}")
    print("  CapEx / D&A normalization")
    print(f"{'='*60}")
    print(f"  D&A     latest FY: ${latest_d_and_a:>18,.0f}   "
          + (f"{da_n}-yr avg ({da_years}): ${d_and_a:,.0f}" if use_da_avg else "(not enough history for an average — using latest FY)"))
    print(f"  CapEx   latest FY: ${latest_capex:>18,.0f}   "
          + (f"{capex_n}-yr avg ({capex_years}): ${capex:,.0f}" if use_capex_avg else "(not enough history for an average — using latest FY)"))
    print("  Using the multi-year average (where available) for FCFF and the")
    print("  reinvestment rate below, instead of one potentially lumpy year.")
    if d_and_a_overridden and da_n >= 2:
        print(f"  (Ignoring the {da_n}-yr D&A trailing average — it's built from the same")
        print("   outdated tag your override just corrected.)")
    if capex_overridden and capex_n >= 2:
        print(f"  (Ignoring the {capex_n}-yr capex trailing average — it's built from the same")
        print("   outdated tag your override just corrected.)")

    if rd_result is not None:
        d_and_a += rd_result["amortization"]
        capex += rd_result["current_rd"]
        print(f"  Folding in R&D: D&A +${rd_result['amortization']:,.0f} (R&D amortization), "
              f"CapEx +${rd_result['current_rd']:,.0f} (current R&D as investment).")

    debt_current = get_metric_value(financials, "total_debt_current", "current portion of debt", stale)
    debt_noncurrent = get_metric_value(financials, "total_debt_noncurrent", "long-term debt", stale)
    cash = get_metric_value(financials, "cash", "cash and equivalents", stale)
    shares_out = get_metric_value(financials, "shares_outstanding", "shares outstanding", stale)

    total_debt = debt_current + debt_noncurrent
    book_equity = get_metric_value(financials, "book_equity", "total stockholders' equity (book value)", stale)

    latest_tax_rate = tax_expense / pretax_income if pretax_income else None
    effective_tax_rate, tax_rate_years, tax_rate_n = trailing_effective_tax_rate(facts)
    print(f"\n{'='*60}")
    print("  Effective tax rate normalization")
    print(f"{'='*60}")
    latest_display = f"{latest_tax_rate:.1%}" if latest_tax_rate is not None else "n/a (zero pretax income)"
    print(f"  Latest FY:  {latest_display}")
    if tax_rate_n >= 1:
        print(f"  Using:      {effective_tax_rate:.1%}   ({tax_rate_n}-yr avg of sane years, {tax_rate_years})")
    else:
        print(f"  Using:      {effective_tax_rate:.1%}   (no year in the trailing window had a sane "
              "tax-expense/pretax-income ratio — falling back to the US statutory rate)")
    print("  A single year's ratio can blow up when pretax income is near zero or")
    print("  negative (cyclical trough) even though tax expense itself is normal —")
    print("  years outside a 0-50% sane range are excluded rather than averaged in.")

    nopat = ebit * (1 - effective_tax_rate)
    fcff = model.compute_fcff(ebit, effective_tax_rate, d_and_a, capex)

    market_cap = share_price * shares_out

    # ---- Bottom-up beta: unlever the raw beta at this company's own current
    # leverage, then relever it — this is a no-op unless you override with an
    # industry-average unlevered beta below, but it exposes the mechanics so
    # you can see how much leverage is driving the raw beta. ----
    debt_to_equity = total_debt / market_cap if market_cap else 0
    unlevered_beta = model.unlever_beta(beta, effective_tax_rate, debt_to_equity)

    print(f"\n{'='*60}")
    print("  Beta check (bottom-up)")
    print(f"{'='*60}")
    print(f"  Raw beta (as entered/pulled):     {beta:.2f}")
    print(f"  Company's D/E (debt/market cap):  {debt_to_equity:.2f}")
    print(f"  Implied UNLEVERED (asset) beta:   {unlevered_beta:.2f}")
    print("  (This strips out the effect of this company's own leverage.")
    print("   For a more reliable beta, look up 2-3 direct competitors,")
    print("   unlever each the same way, average them, then relever that")
    print("   average to THIS company's D/E above — a raw single-company")
    print("   regression beta is noisy, especially for small/thin-traded stocks.)")
    override = input("  Press Enter to use the raw beta as-is, or type an\n"
                      "  industry-average UNLEVERED beta to relever instead: ").strip()
    if override:
        industry_unlevered_beta = parse_number(override)
        beta = model.relever_beta(industry_unlevered_beta, effective_tax_rate, debt_to_equity)
        print(f"  Using relevered beta: {beta:.2f}")

    # Computed here (rather than down with WACC) because the dividend
    # discount model below needs cost of equity too, before WACC exists.
    coe = model.cost_of_equity(DEFAULT_RISK_FREE_RATE, beta, DEFAULT_EQUITY_RISK_PREMIUM)

    # ---- Reinvestment-rate-derived growth (Damodaran fundamental growth) ----
    # NOTE: change in net working capital defaults to 0 here, same simplification
    # as in compute_fcff — see that function's docstring for why.
    invested_capital = total_debt + book_equity - cash
    if rd_result is not None:
        invested_capital += rd_result["research_asset"]
    try:
        reinvestment_rate = model.compute_reinvestment_rate(capex, d_and_a, 0, nopat)
        roic = model.compute_roic(nopat, invested_capital)
        implied_growth = model.implied_growth_rate(reinvestment_rate, roic)
    except (ZeroDivisionError, ValueError):
        reinvestment_rate = roic = None
        implied_growth = DEFAULT_FCFF_GROWTH

    print(f"\n{'='*60}")
    print("  Growth check (reinvestment-rate-derived)")
    print(f"{'='*60}")
    if reinvestment_rate is not None:
        print(f"  Reinvestment rate:  {reinvestment_rate:.2%}   (CapEx - D&A) / NOPAT"
              + ("  [both R&D-adjusted]" if rd_result is not None else ""))
        print(f"  ROIC:               {roic:.2%}   NOPAT / Invested Capital"
              + ("  [includes research asset]" if rd_result is not None else ""))
        print(f"  Implied growth:     {implied_growth:.2%}   Reinvestment rate x ROIC")
    else:
        print(f"  Could not compute (missing invested capital or NOPAT) —")
        print(f"  falling back to flat default of {DEFAULT_FCFF_GROWTH:.2%}")
    print("  This replaces the old flat 5% assumption with a number tied to")
    print("  how much this specific company reinvests and what it earns on it.")
    growth_input = input(f"  Press Enter to use {implied_growth:.2%}, or type an override as a percent (e.g. 7 for 7%): ").strip()
    fcff_growth = parse_number(growth_input) / 100 if growth_input else implied_growth
    if growth_input:
        print(f"  Using override growth rate: {fcff_growth:.2%}")

    # ---- Synthetic credit rating (Damodaran-style cost of debt): a flat
    # assumed cost of debt treats a comfortably-covered, low-leverage
    # company the same as a thinly-covered, heavily-levered one (this is
    # exactly what produced Charter's and Sirius XM's distorted WACC
    # earlier — see that analysis). Interest coverage (EBIT / interest
    # expense) maps to an implied bond rating and default spread instead,
    # so the cost of debt actually reflects THIS company's own ability to
    # cover its own interest payments. ----
    latest_interest_expense, _ie_tag, _ie_fy, _ie_unit = get_latest_annual_value(facts, METRIC_TAGS["interest_expense"])
    if latest_interest_expense is not None:
        if _ie_tag in NET_INTEREST_TAGS:
            # These last-resort fallback tags store a SIGNED net interest
            # income/(expense) figure — negative is genuine net expense
            # (e.g. Charter, heavily levered: take the magnitude), but a
            # POSITIVE value means net interest INCOME (e.g. Toast, cash-
            # rich with little real debt) — there's no way to recover a
            # gross interest expense figure from a net-income number, so
            # treat it as no usable data rather than fabricating an
            # "expense" out of what's actually income.
            latest_interest_expense = abs(latest_interest_expense) if latest_interest_expense < 0 else None
        elif latest_interest_expense < 0:
            # A "plain" expense-labeled tag (InterestExpense etc.) is
            # conventionally always a positive magnitude; a negative value
            # here is unusual/likely mistagged — don't trust it either.
            latest_interest_expense = None
    if latest_interest_expense is not None and _ie_unit == fx_currency:
        latest_interest_expense *= fx_rate
    is_large_firm = market_cap > 5_000_000_000
    synthetic_cod, interest_coverage, synthetic_rating, default_spread = None, None, None, None
    if latest_interest_expense:
        synthetic_cod, interest_coverage, synthetic_rating, default_spread = model.synthetic_cost_of_debt(
            ebit, latest_interest_expense, DEFAULT_RISK_FREE_RATE, large_firm=is_large_firm)

    print(f"\n{'='*60}")
    print("  Synthetic credit rating (cost of debt)")
    print(f"{'='*60}")
    if synthetic_cod is not None:
        print(f"  Interest expense:        ${latest_interest_expense:,.0f}")
        print(f"  Interest coverage (EBIT/interest expense): {interest_coverage:.2f}x")
        print(f"  Implied rating:          {synthetic_rating}  "
              f"({'large' if is_large_firm else 'small'}-firm table, market cap ${market_cap:,.0f})")
        print(f"  Default spread:          {default_spread:.2%}  over the risk-free rate")
        print(f"  Synthetic cost of debt:  {synthetic_cod:.2%}  "
              f"({DEFAULT_RISK_FREE_RATE:.2%} risk-free + {default_spread:.2%} spread)")
        print("  Spread values are illustrative (Damodaran republishes current spreads")
        print("  periodically as credit markets move) — override below with the company's")
        print("  actual bond yield if you have one, for precision.")
        cod_input = input(f"  Press Enter to use {synthetic_cod:.2%}, or type an override as a "
                           "percent (e.g. 6 for 6%): ").strip()
        cost_of_debt = parse_number(cod_input) / 100 if cod_input else synthetic_cod
        if cod_input:
            print(f"  Using override cost of debt: {cost_of_debt:.2%}")
    else:
        print("  No interest expense found (or the company carries no interest-bearing debt) —")
        print(f"  falling back to the flat default of {DEFAULT_COST_OF_DEBT:.2%}.")
        cost_of_debt = DEFAULT_COST_OF_DEBT

    computed_discount_rate = model.wacc(market_cap, total_debt, coe, cost_of_debt, effective_tax_rate)

    # ---- Discount rate override: CAPM/WACC only prices in beta (market
    # risk). It has no way to see country risk, regulatory exposure, a
    # weak balance sheet the market hasn't repriced yet, or anything else
    # you know that isn't in a covariance with the market. This is the hook
    # for that judgment. ----
    print(f"\n{'='*60}")
    print("  Discount rate (WACC)")
    print(f"{'='*60}")
    print(f"  Computed WACC:  {computed_discount_rate:.2%}   "
          f"(cost of equity {coe:.2%} via CAPM, cost of debt {cost_of_debt:.2%})")
    wacc_input = input("  Press Enter to use the computed WACC, or type an override as a\n"
                       "  percent to reflect risk CAPM doesn't price in (e.g. 12 for 12%): ").strip()
    discount_rate = parse_number(wacc_input) / 100 if wacc_input else computed_discount_rate
    if wacc_input:
        print(f"  Using override WACC: {discount_rate:.2%}")

    # ---- Terminal growth override: DEFAULT_TERMINAL_GROWTH is a generic
    # long-run-GDP proxy. A durable moat can support a higher terminal rate;
    # a structurally declining industry may deserve a lower one. ----
    print(f"\n{'='*60}")
    print("  Terminal growth rate")
    print(f"{'='*60}")
    tg_input = input(f"  Press Enter to use {DEFAULT_TERMINAL_GROWTH:.2%} (roughly long-run GDP growth),\n"
                      "  or type an override as a percent (e.g. 3 for 3%): ").strip()
    terminal_growth = parse_number(tg_input) / 100 if tg_input else DEFAULT_TERMINAL_GROWTH
    if tg_input:
        print(f"  Using override terminal growth rate: {terminal_growth:.2%}")

    # ---- Growth stage: a flat growth rate for N years then a hard drop to
    # terminal growth is only realistic for a mature company. A young or
    # still-growing company gets a multi-year runway at its high-growth
    # rate, then a fade down to terminal growth instead of a cliff. ----
    print(f"\n{'='*60}")
    print("  Growth stage (multi-stage DCF)")
    print(f"{'='*60}")
    print("  Pick the life-cycle stage that best fits this company, or type your")
    print("  own 'high-growth years,fade years':")
    print(f"    y = young    ({STAGE_PRESETS['young']['high_growth_years']} yrs at the growth rate above, "
          f"then a {STAGE_PRESETS['young']['fade_years']}-yr fade to terminal growth)")
    print(f"    g = growing  ({STAGE_PRESETS['growing']['high_growth_years']} yrs at the growth rate above, "
          f"then a {STAGE_PRESETS['growing']['fade_years']}-yr fade to terminal growth)")
    print(f"    m = mature   (no high-growth phase, just a {STAGE_PRESETS['mature']['fade_years']}-yr fade "
          "to terminal growth)")
    print("  (aliases also work: growth/growing, young/early/startup, mature/value/stable)")
    if stage_arg is not None:
        high_growth_years, fade_years, stage_label = resolve_stage(stage_arg)
        print(f"  Using --stage {stage_arg!r} from the command line: {stage_label}")
    else:
        stage_input = input("  Enter y/g/m, an alias, custom 'years,years', or Enter for mature: ")
        high_growth_years, fade_years, stage_label = resolve_stage(stage_input)
    if high_growth_years == 0 and fade_years == 0:
        fade_years = 1  # need at least one explicit year to project and discount

    growth_path_fcff = model.project_fcff_multi_stage(
        fcff, fcff_growth, high_growth_years, fade_years, terminal_growth)
    growth_path = []
    prev = fcff
    for val in growth_path_fcff:
        growth_path.append(val / prev - 1)
        prev = val
    print(f"  Using {high_growth_years} high-growth year(s) + {fade_years} fade year(s) "
          f"({len(growth_path)}-year explicit horizon).")
    print("  Year-by-year growth: " + ", ".join(f"{g:.1%}" for g in growth_path))

    # ---- Dividend discount model: a reinvestment rate this high is
    # routine for a capital-intensive, regulated business (a utility
    # plowing nearly all earnings into a growing rate base) — not
    # distress, not a data problem, just the wrong cash-flow framework.
    # The FCFF DCF above will read as near-worthless (base-year FCFF
    # near zero) even though the business is healthy; the DDM below
    # values what shareholders actually receive instead. ----
    if not reit and reinvestment_rate is not None and reinvestment_rate >= 0.80:
        latest_dps, div_growth, div_years, div_n, _dps_unit = trailing_dividend_growth_rate(facts)
        # A per-share unit is compound (e.g. "CNY/shares"), not a bare
        # currency code, so it can't be compared to fx_currency with
        # simple equality the way other metrics' units can.
        if latest_dps is not None and fx_currency and _dps_unit and _dps_unit.startswith(f"{fx_currency}/"):
            latest_dps *= fx_rate
        print(f"\n{'='*60}")
        print("  Dividend discount model (high reinvestment rate detected)")
        print(f"{'='*60}")
        print(f"  Reinvestment rate of {reinvestment_rate:.0%} is typical for a capital-intensive,")
        print("  regulated business (utility, REIT) — nearly all earnings go back into a")
        print("  growing rate base rather than showing up as discretionary FCFF, which can")
        print("  make the FCFF-based DCF above read as near-worthless for a healthy company.")
        if latest_dps is not None and div_growth is not None:
            print(f"  Latest dividend/share: ${latest_dps:.2f}   Historical growth: {div_growth:.2%} "
                  f"({div_n} yr(s), {div_years})")
            ddm_results = model.run_dividend_discount_range(
                latest_dps, div_growth, high_growth_years, fade_years, coe, terminal_growth)
            print(f"  Discounting at cost of equity ({coe:.2%}, not WACC — a dividend is a cash")
            print("  flow to equity holders, already net of debt service):")
            for label in ("low", "base", "high"):
                r = ddm_results[label]
                print(f"    {label.upper():5s}: ${r['value_per_share']:>10,.2f} per share   "
                      f"(cost of equity={r['cost_of_equity']:.2%}, terminal growth={r['terminal_growth_rate']:.2%})")
        elif latest_dps is not None:
            print(f"  Latest dividend/share: ${latest_dps:.2f}, but not enough history to compute a")
            print("  growth rate — can't run the DDM without at least 2 years of dividend data.")
        else:
            print("  This company doesn't pay a dividend at all, so there's nothing for a DDM")
            print("  to value either — neither cash-flow framework fits well here.")

    # ---- REIT mode (--reit): a REIT must pay out 90%+ of taxable income,
    # so its dividend IS roughly its free cash flow to equity and the DDM
    # is the primary valuation, not a fallback. Replaces the auto-DDM
    # block above with Damodaran's REIT fixes: FFO/AFFO instead of
    # depreciation-depressed net income, growth from fundamentals
    # (retention x ROE) instead of historical dividend growth that was
    # mostly funded by issuing new shares, and a NAV (NOI / cap rate)
    # cross-check. See model.py's REIT section. ----
    if reit:
        print(f"\n{'='*60}")
        print("  REIT valuation (FFO / AFFO, fundamental growth, NAV)")
        print(f"{'='*60}")
        latest_fy = max(info["fiscal_year"] for info in financials.values() if info["fiscal_year"] is not None)
        reit_args = (latest_fy, fx_currency, fx_rate)
        net_income = reit_metric(facts, "Net income to common", [
            "NetIncomeLossAvailableToCommonStockholdersBasic", "NetIncomeLoss"], *reit_args, default=0.0)
        re_depreciation = reit_metric(facts, "Real estate depreciation & amortization", [
            "DepreciationAndAmortization", "DepreciationDepletionAndAmortization",
            "DepreciationAmortizationAndAccretionNet"], *reit_args, default=latest_d_and_a)
        gains_on_sale = reit_metric(facts, "Gains on property sales", [
            "GainLossOnSaleOfProperties", "GainsLossesOnSalesOfInvestmentRealEstate",
            "GainLossOnSaleOfRealEstateInvestmentProperty", "GainLossOnDispositionOfAssets"],
            *reit_args, default=0.0)
        impairments = reit_metric(facts, "Real estate impairments", ["ImpairmentOfRealEstate"],
                                  *reit_args, default=0.0)
        # Maintenance capex is the weakest number here: few REITs tag it
        # separately from growth capex, and companies define it differently
        # (the supplemental package's AFFO reconciliation is the real
        # source). 10% of depreciation is a rough placeholder only.
        print("  Maintenance capex: recurring spend to keep existing buildings leased. Most")
        print("  REITs report it only in their supplemental package's AFFO reconciliation —")
        print("  check there; the default below is a rough placeholder.")
        maintenance_capex = reit_metric(facts, "Maintenance capex", ["PaymentsForCapitalImprovements"],
                                        *reit_args, default=0.10 * re_depreciation)
        dps = reit_metric(facts, "Dividend per share", [
            "CommonStockDividendsPerShareCashPaid", "CommonStockDividendsPerShareDeclared"],
            *reit_args, default=0.0, per_share=True)
        preferred = reit_metric(facts, "Preferred equity", ["PreferredStockValue"], *reit_args, default=0.0)
        # Some REITs (e.g. Realty Income) don't tag operating income at all;
        # rebuild it from net income + interest - gains (REITs pay ~no tax).
        operating_income = reit_metric(facts, "Operating income", ["OperatingIncomeLoss"], *reit_args,
                                       default=net_income + (latest_interest_expense or 0.0) - gains_on_sale)

        ffo = model.compute_ffo(net_income, re_depreciation, gains_on_sale, impairments)
        affo = model.compute_affo(ffo, maintenance_capex)
        dividends = dps * shares_out
        print(f"\n  FFO:   ${ffo:>16,.0f}   (${ffo / shares_out:,.2f}/share, "
              f"price/FFO {share_price / (ffo / shares_out):.1f}x)" if ffo > 0 else f"\n  FFO:   ${ffo:>16,.0f}")
        print(f"  AFFO:  ${affo:>16,.0f}   (${affo / shares_out:,.2f}/share, "
              f"price/AFFO {share_price / (affo / shares_out):.1f}x)" if affo > 0 else f"  AFFO:  ${affo:>16,.0f}")
        print(f"  Dividends: ${dividends:,.0f}   (${dps:,.2f}/share, yield {dps / share_price:.2%})")

        # ---- Growth: fundamentals first, history for reference only ----
        fundamental = None
        if affo > 0 and book_equity > 0:
            fundamental = model.reit_fundamental_growth(dividends, affo, book_equity)
            print(f"\n  Payout of AFFO: {fundamental['payout_ratio']:.0%}   Retention: "
                  f"{fundamental['retention_ratio']:.0%}   AFFO / book equity: {fundamental['roe']:.2%}")
            print(f"  Fundamental growth (retention x ROE): {fundamental['growth']:.2%}")
            if fundamental["payout_ratio"] > 1:
                print("  ⚠ The dividend exceeds AFFO — it's being funded by asset sales, new debt or")
                print("    new shares, not by the buildings. Expect a cut or slower growth.")
        else:
            print("\n  Can't compute fundamental growth (AFFO or book equity isn't positive).")
        # Historical growth off the SAME fresh dividend tag used above, not
        # trailing_dividend_growth_rate's Declared-first list — that tag is
        # stale and garbled for some REITs (Prologis: $0.03, showing 50%/yr).
        _dps_val, dps_tag, _dps_fy, _dps_unit = freshest_annual_value(facts, [
            "CommonStockDividendsPerShareCashPaid", "CommonStockDividendsPerShareDeclared"])
        dps_series = get_annual_series(facts, [dps_tag], years=6) if dps_tag else []
        hist_growth = model.share_count_growth_rate([e["val"] for e in dps_series])  # same CAGR math
        if hist_growth is not None:
            print(f"  Historical dividend growth: {hist_growth:.2%}/yr (FY{dps_series[0]['fiscal_year']}-"
                  f"FY{dps_series[-1]['fiscal_year']}) — reference only: REITs grow")
            print("  mostly by issuing shares, so this can overstate per-share growth.")
        share_series = get_annual_series(facts, ["WeightedAverageNumberOfDilutedSharesOutstanding"], years=6)
        dilution = model.share_count_growth_rate([e["val"] for e in share_series])
        if dilution is not None:
            span = f"FY{share_series[0]['fiscal_year']}-FY{share_series[-1]['fiscal_year']}"
            print(f"  Diluted share count growth: {dilution:.2%}/yr ({span})" +
                  ("  ⚠ heavy issuance — headline growth is" if dilution > 0.05 else ""))
            if dilution > 0.05:
                print("    largely the company getting bigger, not each share earning more.")
        default_growth = fundamental["growth"] if fundamental else terminal_growth
        if fundamental and fundamental["roe"] > 0.25:
            # Years of dividends above net income (normal for a REIT, since
            # depreciation depresses net income) can shrink book equity to a
            # sliver — e.g. Simon showed 113% "ROE" — making retention x ROE
            # meaningless.
            print(f"  ⚠ AFFO / book equity of {fundamental['roe']:.0%} isn't a meaningful return: book")
            print("    equity has been shrunk by years of payouts above net income. Defaulting")
            print("    growth to the terminal rate instead; set your own below.")
            default_growth = terminal_growth
        growth_input = input(f"  Dividend growth rate for the DDM (e.g. 3 for 3%; Enter for "
                             f"{default_growth:.2%}): ").strip()
        reit_growth = parse_number(growth_input) / 100 if growth_input else default_growth

        if dps > 0:
            ddm_results = model.run_dividend_discount_range(
                dps, reit_growth, high_growth_years, fade_years, coe, terminal_growth)
            print(f"\n  Dividend discount model (cost of equity {coe:.2%}, {high_growth_years} yrs at "
                  f"{reit_growth:.2%}, {fade_years}-yr fade to {terminal_growth:.2%}):")
            for label in ("low", "base", "high"):
                r = ddm_results[label]
                print(f"    {label.upper():5s}: ${r['value_per_share']:>10,.2f} per share   "
                      f"(cost of equity={r['cost_of_equity']:.2%}, terminal growth={r['terminal_growth_rate']:.2%})")
        else:
            print("\n  No dividend per share to discount — skipping the DDM.")

        # ---- NAV cross-check: value the buildings directly ----
        estimated_noi = operating_income + re_depreciation + impairments
        print(f"\n  NAV cross-check (net operating income / cap rate)")
        print(f"  Estimated NOI (operating income + D&A + impairments): ${estimated_noi:,.0f}")
        print("  This includes corporate overhead, so it runs a bit below the property-level")
        print("  NOI in the supplemental package — use that figure if you have it.")
        noi_input = input("  Press Enter to keep it, or type the reported NOI: ").strip()
        noi = parse_number(noi_input) if noi_input else estimated_noi
        # Re-pull debt with freshest-tag-wins: the general debt lookup takes
        # the first tag with any data, and REITs often switch to tags it
        # doesn't check (Realty Income: a FY2016 LongTermDebt of $4.0B vs a
        # current NotesPayable of $25B). Debt is a big share of a REIT's
        # capital, so a stale figure swings NAV hard.
        print("  Total debt: notes + mortgages + term loans + credit line — check the balance")
        print("  sheet if the figure below looks low.")
        reit_debt = reit_metric(facts, "Total debt", [
            "LongTermDebt", "LongTermDebtNoncurrent", "DebtLongtermAndShorttermCombinedAmount",
            "NotesPayable"], *reit_args, default=total_debt)
        implied_enterprise = market_cap + reit_debt + preferred - cash
        if implied_enterprise > 0 and noi > 0:
            print(f"  Market-implied cap rate (NOI / (market cap + debt + preferred - cash)): "
                  f"{noi / implied_enterprise:.2%}")
        print("  Typical cap rates vary by property type: ~4.5-5.5% industrial/data centers,")
        print("  ~5-6% apartments, ~6-7% net lease, ~6.5-8% retail/office. Check recent")
        print("  transactions for this REIT's sector.")
        cap_input = input("  Cap rate (e.g. 6.5 for 6.5%; Enter for 6.5%): ").strip()
        cap_rate = parse_number(cap_input) / 100 if cap_input else 0.065
        nav = model.reit_nav_per_share(noi, cap_rate, cash, reit_debt, preferred, shares_out)
        premium = share_price / nav["nav_per_share"] - 1 if nav["nav_per_share"] > 0 else None
        print(f"  Property value: ${nav['property_value']:,.0f}   + cash ${cash:,.0f}   - debt ${reit_debt:,.0f}"
              f"   - preferred ${preferred:,.0f}")
        print(f"  NAV: ${nav['nav']:,.0f}")
        print(f"  NAV per share: ${nav['nav_per_share']:,.2f}   vs current price ${share_price:,.2f}" +
              (f"  ({premium:+.0%} {'premium' if premium >= 0 else 'discount'} to NAV)" if premium is not None else ""))

    print(f"\n{'='*60}")
    print("  Assumptions used")
    print(f"{'='*60}")
    print(f"  Effective tax rate:        {effective_tax_rate:.2%}")
    print(f"  Beta:                      {beta:.2f}")
    print(f"  Cost of equity (CAPM):     {coe:.2%}")
    print(f"  WACC (discount rate):      {discount_rate:.2%}")
    print(f"  Market cap:                ${market_cap:,.0f}")
    print(f"  Base-year FCFF:            ${fcff:,.0f}")
    print(f"  High-growth-stage rate:    {fcff_growth:.2%}  (reinvestment-rate-derived; type an override above to change)")
    print(f"  Assumed terminal growth:   {terminal_growth:.2%}  (type an override above to change)")
    if rd_result is not None:
        print(f"  R&D capitalized:           EBIT {rd_result['ebit_adjustment']:+,.0f}, "
              f"invested capital +${rd_result['research_asset']:,.0f} (see above)")

    results = model.run_valuation_range_multi_stage(
        base_fcff=fcff,
        high_growth_rate=fcff_growth,
        high_growth_years=high_growth_years,
        fade_years=fade_years,
        discount_rate=discount_rate,
        terminal_growth_rate=terminal_growth,
        total_debt=total_debt,
        cash=cash,
        shares_outstanding=shares_out,
    )

    # ---- Qualitative adjustment: anything not captured above — moat
    # strength, management quality, a pending catalyst or lawsuit, a
    # competitive threat, industry headwinds — gets layered on here as an
    # explicit, logged haircut/premium rather than folded invisibly into
    # one of the mechanical inputs above. ----
    print(f"\n{'='*60}")
    print("  Qualitative adjustment (optional)")
    print(f"{'='*60}")
    print("  Everything above is mechanical. If you have a qualitative view this")
    print("  model can't see, apply it here as a straight haircut/premium to the")
    print("  valuation range — e.g. -15 for a 15% haircut, 10 for a 10% premium.")
    qual_input = input("  Adjustment as a percent (Enter to skip): ").strip()
    qual_adjustment = parse_number(qual_input) / 100 if qual_input else 0.0
    qual_note = ""
    if qual_adjustment:
        qual_note = input("  One-line reason, for the record: ").strip()
        for r in results.values():
            r["value_per_share"] *= (1 + qual_adjustment)
            r["equity_value"] *= (1 + qual_adjustment)
        print(f"  Applying {qual_adjustment:+.0%} to the valuation range"
              + (f" — {qual_note}" if qual_note else "") + ".")

    print(f"\n{'='*60}")
    print(f"  VALUATION RANGE — {company_name} ({ticker.upper()})")
    print(f"{'='*60}")
    print(f"  Current market price: ${share_price:,.2f}\n")
    for label in ("low", "base", "high"):
        r = results[label]
        print(f"  {label.upper():5s}: ${r['value_per_share']:>10,.2f} per share   "
              f"(WACC={r['discount_rate']:.2%}, terminal growth={r['terminal_growth_rate']:.2%})")
    if qual_adjustment:
        print(f"\n  ({qual_adjustment:+.0%} qualitative adjustment applied"
              + (f": {qual_note}" if qual_note else "") + ")")

    # ---- Market-implied growth rate: rather than just showing the
    # mechanical reinvestment-rate-derived growth next to a valuation
    # gap and leaving the reader to guess what would close it, solve
    # directly for the growth rate the current price implies, holding
    # everything else fixed. Only solvable when base_fcff is positive —
    # growth rate compounds whatever sign the base cash flow already
    # has, so a negative base FCFF has no growth-rate solution at all
    # (see model.solve_implied_growth_rate's docstring). ----
    if fcff > 0:
        implied_growth = model.solve_implied_growth_rate(
            share_price, fcff, high_growth_years, fade_years,
            discount_rate, terminal_growth, total_debt, cash, shares_out)
        print(f"\n{'='*60}")
        print("  Market-implied growth rate")
        print(f"{'='*60}")
        if implied_growth is not None:
            print(f"  Holding WACC ({discount_rate:.2%}), terminal growth ({terminal_growth:.2%}), and the "
                  f"{high_growth_years}+{fade_years}-year")
            print(f"  stage duration fixed, the current price (${share_price:,.2f}) implies a")
            print(f"  high-growth-stage rate of {implied_growth:.2%} "
                  f"(vs. the {fcff_growth:.2%} mechanical estimate above).")
        else:
            print(f"  No growth rate in a plausible range (-95% to +500%) reconciles "
                  f"${share_price:,.2f} to")
            print("  this DCF under the current WACC/terminal-growth/stage assumptions — the gap")
            print("  is too large to close with growth rate alone. Try a WACC override, a longer")
            print("  stage (young/growing), or a qualitative premium instead.")

    # ---- Market-implied EV/Revenue multiple: unlike the growth-rate
    # solve above (which needs a positive base-year FCFF to mean
    # anything), this is pure arithmetic off numbers already on hand
    # (current market cap, debt, cash, revenue) — what multiple the
    # CURRENT price already implies. Always computable, so it prints on
    # every run, not just the negative-FCFF ones — a second, independent
    # cross-check on what the market is actually paying for, alongside
    # the DCF-based growth-rate read above. Not a comp-based "fair"
    # multiple, just what today's price backs out to. ----
    implied_ev = market_cap + total_debt - cash
    implied_multiple = implied_ev / revenue if revenue else None
    if implied_multiple is not None:
        print(f"\n{'='*60}")
        print("  Market-implied EV/Revenue multiple")
        print(f"{'='*60}")
        print(f"  The current price (${share_price:,.2f}) implies an EV of ${implied_ev:,.0f}")
        print(f"  (market cap ${market_cap:,.0f} + debt ${total_debt:,.0f} - cash ${cash:,.0f}),")
        print(f"  i.e. the market is pricing this at {implied_multiple:.2f}x revenue (${revenue:,.0f}).")
        print("  For context, look up 2-3 direct peers' EV/Revenue — is that multiple rich,")
        print("  cheap, or in line for this kind of business?")

    if fcff < 0:
        print(f"\n  ⚠⚠ Base-year FCFF is NEGATIVE (${fcff:,.0f}) — this breaks the Gordon")
        print("     Growth framework the DCF relies on (it assumes a positive cash flow")
        print("     compounding into a positive terminal value), which is why the range")
        print("     above may be negative or otherwise nonsensical. This happens for two")
        print("     different reasons, and this script doesn't distinguish them:")
        print("       - structurally negative EBIT (a genuinely distressed company), or")
        print("       - a cyclically depressed EBIT year colliding with a heavy ongoing")
        print("         capex program (normal EBIT, abnormally low base year)")
        print("     Either way, don't trust this output — a single-year DCF isn't the")
        print("     right tool here. Check the 10-K to see which case this is.")

        print(f"\n{'='*60}")
        print("  Rule-of-thumb fallback: EV/Revenue")
        print(f"{'='*60}")
        print("  A negative-FCFF company (high-growth-but-unprofitable, or distressed)")
        print("  is usually valued off a revenue multiple instead of a DCF. There's no")
        print("  reliable free data source for comparable-company multiples, so look up")
        print(f"  2-3 peers' EV/Revenue yourself — {company_name}'s own revenue here is")
        print(f"  ${revenue:,.0f} (the market-implied multiple is shown above).")
        multiple_input = input("  Enter an EV/Revenue multiple to apply (e.g. 8 for 8x), or Enter to skip: ").strip()
        if multiple_input:
            multiple = parse_number(multiple_input)
            rev_value_per_share, rev_equity_value = model.ev_revenue_valuation(
                revenue, multiple, total_debt, cash, shares_out)
            print(f"  EV ({multiple:g}x revenue): ${revenue * multiple:,.0f}   "
                  f"Equity value: ${rev_equity_value:,.0f}   "
                  f"Per share: ${rev_value_per_share:,.2f}")

    # ---- Drug pipeline (--biotech): a clinical-stage biotech's value is
    # a handful of drug candidates, each approved-or-not, so neither DCF
    # above fits it well — no positive base-year FCFF to compound, and
    # often no revenue for the narrative build to grow from. Value each
    # drug as a probability-weighted (rNPV) cash flow stream and sum
    # them, with Damodaran's patent-as-option valuation alongside as a
    # cross-check. See model.py's BIOTECH / DRUG PIPELINE section. ----
    lead_phase = None
    if biotech:
        print(f"\n{'='*60}")
        print("  Drug pipeline (risk-adjusted NPV, sum of the parts)")
        print(f"{'='*60}")
        print("  Cumulative probability of approval by current phase (BIO/Informa 2011-2020")
        print("  all-indication averages — oncology runs lower, rare disease higher):")
        for phase, prob in model.PHASE_APPROVAL_PROBABILITY.items():
            print(f"    {phase:<12} {prob:>6.1%}")
        print(f"  Cash flows are discounted at the WACC above ({discount_rate:.2%}) — clinical")
        print("  risk is carried by P(approval), so no extra risk premium on the rate.")
        drug_margin_input = input("  Drug-level operating margin once on market (e.g. 40 for 40%; "
                                  "Enter for 40%): ").strip()
        drug_margin = parse_number(drug_margin_input) / 100 if drug_margin_input else 0.40
        drug_tax_input = input("  Tax rate on drug profits (e.g. 25 for 25%; Enter for 25%): ").strip()
        drug_tax_rate = parse_number(drug_tax_input) / 100 if drug_tax_input else 0.25
        vol_input = input("  Volatility of a drug's commercial value, for the option cross-check\n"
                          "  (e.g. 40 for 40%; Enter for 40%): ").strip()
        drug_volatility = parse_number(vol_input) / 100 if vol_input else 0.40

        drugs = []
        print("\n  Enter each drug candidate (lead asset first). For a commercial-stage company,")
        print("  enter marketed products too (phase 'approved') — otherwise the total below")
        print("  covers only the pipeline. Press Enter at the name prompt when done.")
        while True:
            name = input("\n  Drug name (Enter to finish): ").strip()
            if not name:
                break
            phase_raw = input("    Phase (pre / p1 / p2 / p3 / filed / approved): ").strip().lower()
            phase = PHASE_ALIASES.get(phase_raw)
            if phase is None:
                print(f"    Could not parse phase '{phase_raw}' — defaulting to phase2.")
                phase = "phase2"
            default_prob = model.PHASE_APPROVAL_PROBABILITY[phase]
            prob_input = input(f"    P(approval) (e.g. 30 for 30%; Enter for {default_prob:.1%}): ").strip()
            prob_approval = parse_number(prob_input) / 100 if prob_input else default_prob
            peak_sales = parse_number(input("    Peak annual sales in dollars (e.g. 1500000000): "))
            default_years = PHASE_DEFAULT_YEARS_TO_LAUNCH[phase]
            ytl_input = input(f"    Years until launch (Enter for {default_years}): ").strip()
            years_to_launch = int(parse_number(ytl_input)) if ytl_input else default_years
            ramp_input = input("    Years to ramp to peak sales after launch (Enter for 5): ").strip()
            ramp_years = int(parse_number(ramp_input)) if ramp_input else 5
            excl_input = input("    Years of exclusivity after launch (Enter for 10): ").strip()
            exclusivity_years = int(parse_number(excl_input)) if excl_input else 10
            cost_input = input("    Remaining development + launch cost in dollars (Enter for 0): ").strip()
            dev_cost = parse_number(cost_input) if cost_input else 0.0
            drugs.append({
                "name": name, "phase": phase,
                "result": model.drug_rnpv(peak_sales, years_to_launch, ramp_years, exclusivity_years,
                                          drug_margin, drug_tax_rate, discount_rate, dev_cost, prob_approval),
                "exclusivity_years": exclusivity_years,
            })

        if drugs:
            lead_phase = drugs[0]["phase"]
            print(f"\n  {'Drug':<16} {'Phase':<12} {'P(appr)':>8} {'Unrisked NPV':>16} {'rNPV':>16} "
                  f"{'Option x P':>16}")
            for d in drugs:
                r = d["result"]
                if r["pv_commercial"] > 0 and r["pv_dev_cost"] > 0:
                    option = model.patent_option_value(r["pv_commercial"], r["pv_dev_cost"],
                                                       d["exclusivity_years"], drug_volatility,
                                                       DEFAULT_RISK_FREE_RATE)
                    option_display = f"{option['value'] * r['probability_of_approval']:>16,.0f}"
                else:
                    option_display = f"{'n/a':>16}"  # option model needs a positive cost and value
                print(f"  {d['name'][:16]:<16} {d['phase']:<12} {r['probability_of_approval']:>7.1%} "
                      f"{r['unrisked_npv']:>16,.0f} {r['rnpv']:>16,.0f} {option_display}")
            pipeline_value = sum(d["result"]["rnpv"] for d in drugs)
            pipeline_equity = pipeline_value + cash - total_debt
            pipeline_per_share = pipeline_equity / shares_out
            print(f"\n  Pipeline rNPV:        ${pipeline_value:,.0f}")
            print(f"  + cash - debt:        ${pipeline_equity:,.0f}   (cash ${cash:,.0f}, debt ${total_debt:,.0f})")
            print(f"  Value per share:      ${pipeline_per_share:,.2f}   vs current price ${share_price:,.2f}")
            print("  Not included: corporate overhead and research not tied to any listed drug,")
            print("  and future dilution from raising cash to fund development — both lower")
            print("  this number. The option column values each drug's flexibility to wait for")
            print("  better information: well above rNPV means the drug's value rests on that")
            print("  flexibility; well below means it's clearly worth developing now, and the")
            print("  option model's 1/n cost of delay is penalizing the wait it assumes.")

    # ---- Narrative DCF (Damodaran's actual "Ginzu"-style build):
    # revenue, margin, and reinvestment are forecast as three SEPARATE
    # year-by-year paths instead of compounding one lump FCFF number at
    # a single mechanical growth rate (reinvestment rate x ROIC) — see
    # model.py's project_revenue_path / project_margin_path /
    # reinvestment_from_sales_to_capital. Opt-in via --narrative because
    # it needs three genuine judgment calls nothing in a 10-K answers
    # for you (initial growth rate, target mature margin, sales-to-
    # capital ratio) — this is a SEPARATE, additional view, not a
    # replacement: the mechanical model above is still the faster tool
    # for a quick read on a stable, mature company. ----
    if narrative and revenue <= 0:
        print("\n  Skipping the narrative DCF: it grows revenue from today's level, and this")
        print("  company has none yet. " + ("The pipeline (rNPV) valuation above is the one to use."
                                         if biotech else "Use --biotech for a pipeline (rNPV) valuation instead."))
        narrative = False
    if narrative:
        print(f"\n{'='*60}")
        print("  Narrative DCF (revenue x margin x reinvestment)")
        print(f"{'='*60}")
        current_margin = ebit / revenue if revenue else 0.0
        cagr, cagr_years = trailing_revenue_cagr(facts)
        print(f"  Current operating margin: {current_margin:.1%}" +
              (f"   (trailing revenue CAGR {cagr:.1%}, {cagr_years} — reference only)" if cagr is not None else ""))
        print("  Reference mature margins (Damodaran's examples): ~20% software, ~8-10%")
        print("  retail, ~12-15% industrial/hardware. Pick what this business converges")
        print("  toward, not today's number.")
        growth_input = input(f"  Initial revenue growth rate, fading to {terminal_growth:.2%} (terminal) by "
                              "year 10\n  (e.g. 30 for 30%; Enter for 0%): ").strip()
        initial_growth = parse_number(growth_input) / 100 if growth_input else 0.0
        margin_input = input("  Target (mature) operating margin (e.g. 20 for 20%; Enter to hold today's "
                              "margin flat): ").strip()
        target_margin = parse_number(margin_input) / 100 if margin_input else current_margin
        trailing_s2c = revenue / invested_capital if invested_capital else None
        print("  Sales-to-capital ratio: how much new revenue $1 of new capital buys (2.0 ="
              " $1 buys $2 of revenue).")
        print(f"  This company's own trailing ratio (revenue / invested capital): " +
              (f"{trailing_s2c:.2f}" if trailing_s2c else "n/a") +
              " — noisy for a young firm; an industry average is usually better.")
        s2c_input = input("  Sales-to-capital ratio (e.g. 2 for 2.0; Enter to use the trailing ratio "
                           "above, or 1.0 if unavailable): ").strip()
        sales_to_capital = (parse_number(s2c_input) if s2c_input
                             else (trailing_s2c if trailing_s2c and trailing_s2c > 0 else 1.0))

        # ---- NOL tax-rate ramp: accumulated net operating losses shelter
        # future taxable income, so the effective rate should start near
        # zero and rise to the marginal rate as the pool is used up,
        # instead of a flat rate applying from year 1 — exactly the gap
        # called out when this build was scoped. Optional: a mature,
        # consistently-profitable company often has no NOLs on record at
        # all, so this doesn't force a manual-entry prompt the way a core
        # metric would — missing just means "no shield," a sensible
        # default, not an error. ----
        nol_raw, _nol_tag, _nol_fy, _nol_unit = get_latest_annual_value(
            facts, ["DeferredTaxAssetsOperatingLossCarryforwards", "OperatingLossCarryforwards"])
        nol_balance = nol_raw * fx_rate if nol_raw is not None and _nol_unit == fx_currency else (nol_raw or 0.0)
        print(f"  NOL carryforward on record: ${nol_balance:,.0f} (tag: {_nol_tag})" +
              ("" if nol_balance else "  (none found — no tax shield to apply)"))
        if nol_balance and _nol_tag == "DeferredTaxAssetsOperatingLossCarryforwards":
            print("  Note: this tag is sometimes the TAX-EFFECTED deferred tax asset (gross NOL x")
            print("  tax rate) rather than the gross carryforward — some filers report it either")
            print("  way. Check the 10-K's tax footnote for the actual gross NOL figure if this")
            print("  looks too small to be the pre-tax loss balance.")
        nol_input = input("  Press Enter to use this balance as the GROSS NOL pool, or type a "
                           "corrected amount (e.g. 0 to ignore it): ").strip()
        if nol_input:
            nol_balance = parse_number(nol_input)

        # ---- Marginal tax rate (the ceiling the NOL ramp rises to once
        # the pool is exhausted): deliberately NOT the same as
        # effective_tax_rate above. That's a TRAILING AVERAGE, and for an
        # NOL-carrying company it's often already suppressed by the same
        # real NOL usage this ramp is trying to model explicitly —
        # reusing it as "the fully-taxed rate" double-counts the shield
        # (found live on Toast: its trailing effective rate was ~1-4%,
        # so post-NOL years showed only a ~4% tax rate instead of a real
        # company's statutory ~25-27% combined federal+state). Default
        # to a plain statutory estimate instead. ----
        print(f"  Trailing effective tax rate (used elsewhere above): {effective_tax_rate:.1%} — often")
        print("  already suppressed by real NOL usage, so NOT a good proxy for the rate this")
        print("  company pays once its NOLs are exhausted. Use a statutory estimate instead.")
        marginal_input = input("  Marginal tax rate once NOLs run out (e.g. 25 for 25%; Enter for "
                                "25%): ").strip()
        marginal_tax_rate = parse_number(marginal_input) / 100 if marginal_input else 0.25

        # ---- Declining discount-rate glide path: a young, undiversified
        # company's true cost of capital is higher than the mature WACC
        # already computed above — it fades toward that mature rate as
        # the business survives and de-risks, instead of using one flat
        # rate for the whole 10-year forecast. Default the early-stage
        # rate to mature WACC + 7 points (a round-number stand-in for
        # Damodaran's "roughly the 90th percentile of all firms" —
        # nothing here pulls an actual cross-firm WACC distribution). ----
        default_initial_wacc = discount_rate + 0.07
        print(f"  Mature WACC (computed above): {discount_rate:.2%}. A young, undiversified company's")
        print("  true cost of capital starts higher and fades toward that mature rate as the")
        print("  business survives and de-risks — Damodaran's rough shortcut is something like")
        print("  the 90th percentile of all firms' cost of capital.")
        initial_wacc_input = input(f"  Early-stage cost of capital (e.g. 25 for 25%; Enter for "
                                    f"{default_initial_wacc:.1%}): ").strip()
        initial_wacc = parse_number(initial_wacc_input) / 100 if initial_wacc_input else default_initial_wacc

        narrative_years = 10
        wacc_path = model.project_wacc_path(initial_wacc, discount_rate, narrative_years)

        # The NOL ramp needs the EBIT path up front (to know how much
        # taxable income there is to shelter each year) — build it once
        # off the BASE-case terminal growth rate. The low/base/high range
        # below flexes terminal growth (and the WACC glide) slightly,
        # which shifts the revenue (and so EBIT) path a little each
        # scenario; reusing this same rate list across all three is a
        # known, minor simplification rather than re-deriving the NOL
        # ramp three times.
        base_revenue_path = model.project_revenue_path(revenue, initial_growth, terminal_growth, narrative_years)
        base_margin_path = model.project_margin_path(current_margin, target_margin, narrative_years)
        base_ebit_path = model.project_ebit_path(base_revenue_path, base_margin_path)
        tax_rate_path = model.nol_tax_rate_path(base_ebit_path, nol_balance, marginal_tax_rate)

        narrative_path = model.project_narrative_path(
            revenue, initial_growth, current_margin, target_margin,
            tax_rate_path, sales_to_capital, terminal_growth, years=narrative_years)
        print(f"\n  {'Yr':>3} {'Revenue':>16} {'Margin':>8} {'EBIT':>16} {'Tax%':>7} {'WACC':>7} "
              f"{'Reinvest':>16} {'FCFF':>16}")
        for t in range(narrative_years):
            print(f"  {t + 1:>3} {narrative_path['revenue'][t]:>16,.0f} {narrative_path['margin'][t]:>7.1%} "
                  f"{narrative_path['ebit'][t]:>16,.0f} {tax_rate_path[t]:>6.1%} {wacc_path[t]:>6.1%} "
                  f"{narrative_path['reinvestment'][t]:>16,.0f} {narrative_path['fcff'][t]:>16,.0f}")

        narrative_range = model.run_valuation_range_narrative(
            revenue, initial_growth, current_margin, target_margin,
            tax_rate_path, sales_to_capital, (initial_wacc, discount_rate), terminal_growth,
            total_debt, cash, shares_out, years=narrative_years)
        print()
        for label in ("low", "base", "high"):
            r = narrative_range[label]
            print(f"  {label.upper():5s}: ${r['value_per_share']:>10,.2f} per share   "
                  f"(mature WACC={r['discount_rate']:.2%}, terminal growth={r['terminal_growth_rate']:.2%})")
        if narrative_range["ordering_warning"]:
            print(f"\n  ⚠ {narrative_range['ordering_warning']}")

        # ---- Failure-probability overlay: every DCF above (including
        # this narrative one) implicitly assumes the company SURVIVES to
        # realize the modeled value — WACC prices business/market risk,
        # not "this specific company might run out of cash before any of
        # this plays out," which is a real, separate risk for a young,
        # cash-burning company. Blend the going-concern value with a
        # distressed liquidation value, weighted by an estimated
        # probability of failure from current cash burn and runway. ----
        print(f"\n{'='*60}")
        print("  Failure-probability overlay")
        print(f"{'='*60}")
        current_burn_rate = -fcff if fcff < 0 else 0.0
        runway = model.estimate_runway_years(cash, current_burn_rate)
        runway_display = f"{runway:.1f} years" if runway != float("inf") else "N/A (current FCFF isn't negative)"
        print(f"  Cash: ${cash:,.0f}   Current annual burn rate: ${current_burn_rate:,.0f}   Runway: {runway_display}")
        default_failure_prob = model.failure_probability_from_runway(runway)
        print(f"  Runway-implied failure probability (illustrative table — there's no published")
        print(f"  standard for this, override with sector judgment if you have it): {default_failure_prob:.0%}")
        default_source = "runway-implied"
        if lead_phase is not None:
            # For a cash-burning biotech the bigger risk is usually the lead
            # drug failing in the clinic, not the runway itself — Damodaran
            # sets P(fail) off the clinical stage for exactly this case. A
            # company already funding itself survives a trial failure, so
            # there it's shown for reference only.
            clinical_failure_prob = 1.0 - model.PHASE_APPROVAL_PROBABILITY[lead_phase]
            if current_burn_rate > 0:
                default_failure_prob = clinical_failure_prob
                default_source = "lead-drug clinical"
                print(f"  Lead drug is in {lead_phase}: clinical failure probability "
                      f"{clinical_failure_prob:.0%} (1 - P(approval)) — used as the default below,")
                print("  since a cash-burning company usually can't survive its lead drug failing.")
            else:
                print(f"  Lead drug is in {lead_phase} (clinical failure probability {clinical_failure_prob:.0%}),")
                print("  but this company isn't burning cash, so a trial failure wouldn't end it —")
                print("  keeping the runway-implied default.")
        prob_input = input(f"  Probability of failure (e.g. 20 for 20%; Enter to use the {default_source} "
                            "figure above): ").strip()
        probability_of_failure = parse_number(prob_input) / 100 if prob_input else default_failure_prob
        haircut_input = input("  Liquidation haircut on book equity (e.g. 50 for 50%; Enter for "
                               "50%): ").strip()
        liquidation_haircut = parse_number(haircut_input) / 100 if haircut_input else 0.50

        blended_values = {}
        for label in ("low", "base", "high"):
            going_concern = narrative_range[label]["value_per_share"]
            blended, liquidation_value = model.failure_adjusted_value_per_share(
                going_concern, probability_of_failure, book_equity, shares_out, liquidation_haircut)
            blended_values[label] = blended
            print(f"  {label.upper():5s}: ${blended:>10,.2f} per share   "
                  f"(going-concern ${going_concern:,.2f}, liquidation ${liquidation_value:,.2f}, "
                  f"P(fail)={probability_of_failure:.0%})")
        blend_ordering_warning = model.ordering_warning_for_range(
            blended_values["low"], blended_values["base"], blended_values["high"])
        if blend_ordering_warning:
            print(f"\n  ⚠ {blend_ordering_warning}")

    print("\n  This is a mechanical DCF estimate based on the last 10-K only, plus")
    print("  whatever WACC / terminal growth / qualitative overrides you entered")
    print("  above. It still won't catch anything you didn't think to override —")
    print("  use this as a starting anchor, then layer your own judgment on top,")
    print("  same as the uValue workflow you described.\n")

    if missing:
        print(f"  ⚠ Note: {', '.join(missing)} had to be entered manually — EDGAR")
        print("    didn't have them under any tag this script currently checks.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pull EDGAR financials + market data for a US public company and run a DCF.")
    parser.add_argument("ticker", help="Stock ticker, e.g. AAPL")
    parser.add_argument(
        "--stage", default=None,
        help="Skip the interactive growth-stage prompt and run with this life-cycle "
             "stage: young (aliases: early, startup), growth/growing, or mature "
             "(aliases: value, stable) — or a custom 'high_years,fade_years' pair, "
             "e.g. --stage growth or --stage 4,8.",
    )
    parser.add_argument(
        "--narrative", action="store_true",
        help="Also run the narrative DCF (Damodaran's 'Ginzu'-style build): revenue, "
             "margin, and reinvestment forecast as three separate year-by-year paths "
             "instead of one mechanical growth rate. Prompts for three extra judgment "
             "inputs (initial growth rate, target margin, sales-to-capital ratio) that "
             "no 10-K can answer for you. Printed as an additional section, not a "
             "replacement for the mechanical model above it.",
    )
    parser.add_argument(
        "--biotech", action="store_true",
        help="Biotech/pharma mode: defaults R&D amortizable life to 10 years, adds a "
             "drug-by-drug pipeline valuation (probability-of-approval-weighted rNPV, "
             "with a patent-as-option cross-check), and with --narrative sets the "
             "failure probability from the lead drug's clinical phase.",
    )
    parser.add_argument(
        "--reit", action="store_true",
        help="REIT mode: always runs the dividend discount model, with FFO/AFFO in place "
             "of net income, growth from fundamentals (AFFO retention x ROE) instead of "
             "historical dividend growth, a share-dilution check, and a NAV (NOI / cap "
             "rate) cross-check.",
    )
    args = parser.parse_args()

    run_full_valuation(args.ticker, args.stage, args.narrative, args.biotech, args.reit)
