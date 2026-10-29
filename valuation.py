"""
valuation.py — Pull US public company financials from SEC EDGAR and run a
WACC/DCF valuation, Damodaran-style.

USAGE (from Terminal):
    python3 valuation.py AAPL
    python3 valuation.py MSFT --fmp-key YOUR_FMP_KEY_HERE
    python3 valuation.py VKTX --biotech --narrative   (drug pipeline rNPV + biotech defaults)
    python3 valuation.py O --reit                     (FFO/AFFO dividend model + NAV)
    python3 valuation.py DOW --cyclical --narrative   (normalize to the 10-yr cycle margin)

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

import os
import re
import csv
import io
import sys
import html
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
DEFAULT_RISK_FREE_RATE = 0.045   # fallback only — normally today's 10-year Treasury yield (see load_risk_free_rate)
DEFAULT_EQUITY_RISK_PREMIUM = 0.055  # fallback only — normally Damodaran's current implied ERP (see load_damodaran_data)
DEFAULT_MARGINAL_TAX_RATE = 0.25    # US federal + state statutory, for relevering betas (Damodaran relevers at the marginal rate)
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

    A missing "start" is only treated as a genuine instant fact by the
    caller (_recipe_annual_entries) when NO entry for that tag ever has
    one — some filers tag an interim per-share dividend DECLARATION as a
    duration fact with no "start" at all (not a Q4-footnote collision,
    a different EDGAR quirk), and that must not be waved through here.
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


def _fiscal_year_of_period(entry):
    """
    The fiscal year a reported PERIOD belongs to, from its end date.

    EDGAR's own "fy" field is the fiscal year of the FILING, not of the
    period: a FY2025 10-K also reports FY2024 and FY2023, all tagged
    fy=2025, so trailing series came out labeled "FY2023-FY2025" for four
    distinct years (and anything counting years from the labels, like the
    revenue CAGR, miscounted). Filers' own fy tags aren't reliable either
    (Target's tag two different years as fy=2018).

    A year ending January-March is labeled with the prior calendar year:
    Target's year ending Feb 1, 2025 is its fiscal 2024, and a 52/53-week
    year ending Jan 3, 2026 is really calendar 2025. (A few retailers,
    e.g. Walmart, name that year by its end instead — the labels are then
    one off from their naming but still unique and in order, which is
    what the math needs.) Falls back to the filing's fy if the end date
    can't be parsed.
    """
    end = entry.get("end", "")
    try:
        year, month = int(end[:4]), int(end[5:7])
    except ValueError:
        return entry.get("fy")
    return year - 1 if month <= 3 else year


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
            entries = units[unit_key]
            # If ANY entry for this tag+unit has a "start" date, this is a
            # duration concept for this filer, not an instant one — so an
            # entry missing "start" here isn't a balance-sheet-style point-
            # in-time fact, it's a duration fact that simply wasn't tagged
            # with a start (seen on quarterly dividend-declaration entries
            # mixed into an annual dividends-per-share series). Don't let
            # _is_full_year_period's instant-fact passthrough apply to it.
            is_duration_concept = any(e.get("start") for e in entries)
            for e in entries:
                if e.get("form") not in ("10-K", "20-F", "40-F") or e.get("fy") is None:
                    continue
                if is_duration_concept and not e.get("start"):
                    continue
                if not _is_full_year_period(e):
                    continue
                periods[e.get("end", "")] = (e["val"], unit_key, _fiscal_year_of_period(e))
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


# Interest-expense tags that hold a plain positive expense magnitude —
# the signed net-interest tags are left out, since a net figure can't be
# turned back into gross expense (see NET_INTEREST_TAGS).
GROSS_INTEREST_EXPENSE_TAGS = [t for t in METRIC_TAGS["interest_expense"] if t not in NET_INTEREST_TAGS]


def pretax_plus_interest_series(facts_json, years=5):
    """
    EBIT rebuilt as pretax income + interest expense, year by year, for
    filers whose income statement has no operating-income line at all
    (Dow goes straight from costs to pretax income, so OperatingIncomeLoss
    is never tagged). Only years with both figures are kept. It's an
    approximation: anything else between operating income and pretax
    income (equity-method earnings, other non-operating gains) stays in.
    Returns entries shaped like get_annual_series's (val, end,
    fiscal_year, unit), oldest first.
    """
    pretax = get_annual_series(facts_json, METRIC_TAGS["pretax_income"], years)
    _val, interest_tag, _fy, _unit = freshest_annual_value(facts_json, GROSS_INTEREST_EXPENSE_TAGS)
    if not pretax or interest_tag is None:
        return []
    interest_by_end = {e["end"]: e for e in get_annual_series(facts_json, [interest_tag], years)}
    series = []
    for p in pretax:
        i = interest_by_end.get(p["end"])
        if i is not None and i["val"] >= 0:
            series.append({**p, "val": p["val"] + i["val"]})
    return series


def cycle_margin_history(facts_json, years=10):
    """
    Up to `years` of matched (revenue, EBIT) pairs, one per fiscal year,
    oldest first — the raw material for normalizing a cyclical's margin
    over a full cycle (see model.cycle_margin_stats). Loss years are
    KEPT: troughs are part of the cycle, and the old approach of
    averaging only profitable years pushed the "normal" margin up.

    EBIT comes from operating income; for years where that isn't tagged
    (Dow has no operating-income line at all), it's filled in with
    pretax income + interest expense and the row is marked `rebuilt`.
    Revenue and EBIT are matched on the same period end and must be in
    the same currency (a margin is a ratio, so no FX conversion is
    needed). Returns a list of dicts: fiscal_year, revenue, ebit, rebuilt.
    """
    pull = years + 5  # extra headroom: not every year has both figures
    revenue = {e["end"]: e for e in get_annual_series(facts_json, METRIC_TAGS["revenue"], pull)}
    operating = {e["end"]: e for e in get_annual_series(facts_json, METRIC_TAGS["operating_income"], pull)}
    ebit = {e["end"]: e for e in pretax_plus_interest_series(facts_json, pull)}
    ebit.update(operating)
    rows = []
    for end in sorted(set(revenue) & set(ebit)):
        r, e = revenue[end], ebit[end]
        if r["unit"] != e["unit"]:
            continue
        rows.append({"fiscal_year": r["fiscal_year"], "revenue": r["val"], "ebit": e["val"],
                     "rebuilt": end not in operating})
    return rows[-years:]


def cycle_stats_or_none(rows):
    """model.cycle_margin_stats over history rows, or None when there's
    too little usable history (under 2 years with positive revenue)."""
    if len([r for r in rows if r["revenue"] > 0]) < 2:
        return None
    return model.cycle_margin_stats([r["ebit"] for r in rows], [r["revenue"] for r in rows])


def choose_cycle_margin(rows):
    """
    Interactive: print the year-by-year margin table for `rows` (from
    cycle_margin_history), let the user trim the window (spin-offs and
    big mergers make older years a different company — Dow before its
    2019 separation from DowDuPont, say), then pick which average to
    use. Returns a dict: margin, method, n_years, span — or None if
    there's too little history to average.
    """
    print(f"\n  {'Year':<6} {'Revenue':>18} {'EBIT':>18} {'Margin':>8}")
    for r in rows:
        margin = f"{r['ebit'] / r['revenue']:>7.1%}" if r["revenue"] > 0 else f"{'n/a':>7}"
        print(f"  FY{r['fiscal_year']:<4} {r['revenue']:>18,.0f} {r['ebit']:>18,.0f} {margin}"
              + ("  *" if r["rebuilt"] else ""))
    if any(r["rebuilt"] for r in rows):
        print("  * no operating income tagged that year — EBIT rebuilt as pretax income + interest")

    window = len(rows)
    while True:
        window_input = prompt_number(f"  Years to average, most recent first (Enter for all {len(rows)}): ",
                                     default=len(rows))
        if 2 <= window_input <= len(rows) and window_input == int(window_input):
            window = int(window_input)
            break
        print(f"    Please enter a whole number from 2 to {len(rows)}.")
    subset = rows[-window:]
    stats = cycle_stats_or_none(subset)
    if stats is None:
        print("  Not enough years with revenue in that window to average.")
        return None
    span = f"FY{subset[0]['fiscal_year']}-FY{subset[-1]['fiscal_year']}"
    print(f"  {span} ({stats['n_years']} yrs):  mean {stats['mean']:.1%}   "
          f"aggregate {stats['aggregate']:.1%}   median {stats['median']:.1%}")
    print("  mean = each year counts equally (Damodaran's usual cycle average); aggregate =")
    print("  total EBIT / total revenue, so big-revenue years count more; median = guards")
    print("  against one freak year.")
    methods = {"m": "mean", "a": "aggregate", "d": "median"}
    choice = input("  Use which? (m = mean, a = aggregate, d = median; Enter for mean): ").strip().lower()
    method = methods.get(choice, "mean")
    return {"margin": stats[method], "method": method, "n_years": stats["n_years"], "span": span}


def trailing_dividend_growth_rate(facts_json, years=6):
    """
    Average year-over-year dividend-per-share growth over the trailing
    window. This is the standard Gordon Growth Model input for a
    capital-intensive, dividend-paying business (utilities, REITs) where
    nearly all earnings get reinvested into a growing rate base rather
    than showing up as discretionary FCFF — see cycle_margin_history
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


# Tags for a company's TOTAL debt, checked only when the usual current /
# long-term pair has gone stale. A stale debt tag means one of two very
# different things: the company RENAMED its debt tag (Realty Income: a
# FY2016 LongTermDebt of $4.0B, while it now reports $25B as
# NotesPayable) or it REPAID the debt and stopped reporting any (Viking:
# convertible notes last tagged FY2016-2018, repaid in 2019). A current
# figure under any of these tags means "renamed"; none at all means
# "repaid".
TOTAL_DEBT_TAGS = [
    "LongTermDebt",
    "DebtLongtermAndShorttermCombinedAmount",
    "LongTermDebtNoncurrent",
    "NotesPayable",
    "ConvertibleNotesPayable",
    "SeniorNotes",
]


def resolve_total_debt(financials, facts_json, stale, fx_currency, fx_rate):
    """
    Total debt for the equity bridge. When neither the current nor the
    long-term debt figure is stale, this is just their sum (each
    defaulting to $0 if missing — plenty of companies have no current
    portion, or no debt at all). When either is stale, see
    TOTAL_DEBT_TAGS above: use a current total-debt figure if the filer
    reports one under another tag, otherwise treat the stale amount as
    repaid. Either way the user gets the final say.
    """
    debt_keys = {"total_debt_current": "current portion of debt", "total_debt_noncurrent": "long-term debt"}
    stale_keys = [k for k in debt_keys if k in stale]
    if not stale_keys:
        return sum(get_metric_value(financials, k, label, stale, default=0.0) for k, label in debt_keys.items())

    latest_fy = max(info["fiscal_year"] for info in financials.values() if info["fiscal_year"] is not None)
    stale_total = sum(financials[k]["value"] for k in stale_keys)
    print(f"\n  Debt: {', '.join(stale_keys)} last reported FY"
          f"{min(financials[k]['fiscal_year'] for k in stale_keys)} (${stale_total:,.0f}) — stale.")
    val, tag, fy, unit = freshest_annual_value(facts_json, TOTAL_DEBT_TAGS)
    if val is not None and fy is not None and fy >= latest_fy:
        if fx_currency and unit == fx_currency:
            val *= fx_rate
        print(f"  The company now reports total debt under a different tag: ${val:,.0f} "
              f"(tag: {tag}, FY{fy}).")
        default = val
    else:
        fresh_parts = sum(get_metric_value(financials, k, label, stale, default=0.0)
                          for k, label in debt_keys.items() if k not in stale_keys)
        print("  No debt tag is reported for the latest year, which usually means the debt was")
        print("  repaid. Treating the stale amount as $0" +
              (f" (keeping the current ${fresh_parts:,.0f})." if fresh_parts else "."))
        default = fresh_parts
    print("  Check the latest 10-K balance sheet if unsure.")
    return prompt_number(f"  Press Enter to use ${default:,.0f} as total debt, or type a corrected amount: ",
                         default=default)


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


# Short-term investments / marketable securities held alongside cash.
# Damodaran counts cash AND marketable securities in the equity bridge —
# cash-rich biotechs in particular park most of their money here (Viking:
# $166M cash but another $540M in available-for-sale securities). Kept
# out of METRIC_TAGS so companies without any don't get a spurious
# "missing metric" warning. Freshest tag wins; the tags overlap, so only
# one is ever used, never summed.
SHORT_TERM_INVESTMENT_TAGS = [
    "ShortTermInvestments",
    "MarketableSecuritiesCurrent",
    "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
    "AvailableForSaleSecuritiesCurrent",
    "HeldToMaturitySecuritiesCurrent",
    "OtherShortTermInvestments",
]


def parse_number(text):
    """Accept numbers typed with commas or a leading $ (e.g. '5,000,000,000'
    or '$186.72') in addition to plain digits."""
    cleaned = text.strip().replace(",", "").replace("$", "")
    return float(cleaned)


def prompt_number(prompt, default=None):
    """
    Ask for a number until one parses. Blank input returns `default` when
    there is one; with no default, blank or unparseable input re-asks
    instead of crashing the whole run with a ValueError (which previously
    threw away every answer typed so far).
    """
    while True:
        text = input(prompt).strip()
        if not text and default is not None:
            return default
        try:
            return parse_number(text)
        except ValueError:
            print("    Please type a number (e.g. 1000000000)" +
                  (", or press Enter for the default." if default is not None else "."))


def get_metric_value(financials, key, label, stale_metrics=None, return_override_flag=False, default=None):
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

    `default` is what pressing Enter means when the metric is MISSING —
    for items a small company may simply not report separately because
    they're immaterial (D&A, capex, current debt), $0 is usually the
    right answer, so the caller passes 0.0. Without a default, a number
    is required.
    """
    info = financials.get(key, {})
    if info.get("value") is not None:
        if stale_metrics and key in stale_metrics:
            print(f"  '{label}' resolved to a STALE value: {info['value']:,.0f} (FY{info['fiscal_year']}).")
            val = prompt_number(f"  Press Enter to keep it, or type a corrected {label} "
                                "(in dollars, e.g. 1000000000): ", default=info["value"])
            overridden = val != info["value"]
            return (val, overridden) if return_override_flag else val
        return (info["value"], False) if return_override_flag else info["value"]
    print(f"  '{label}' was not found on EDGAR under any known tag.")
    if default is not None:
        print(f"  Many smaller companies don't report it separately because it's immaterial —")
        print(f"  ${default:,.0f} is usually right then. Check the 10-K's cash-flow statement if unsure.")
        val = prompt_number(f"  Enter {label} manually (in dollars), or press Enter for ${default:,.0f}: ",
                            default=default)
    else:
        val = prompt_number(f"  Enter {label} manually (in dollars, e.g. 1000000000): ")
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


# ---------------------------------------------------------------------
# DAMODARAN DATA: industry betas and the current implied equity risk
# premium, pulled from his site so every run uses the same consistent,
# current inputs instead of a noisy single-company regression beta and a
# hardcoded ERP. A company's own regression beta has a standard error of
# roughly 0.2-0.3; an industry average across dozens or hundreds of firms
# cancels most of that noise (the error shrinks roughly with the square
# root of the number of firms), which is why Damodaran's bottom-up beta
# starts from the industry, not the company.
#
# Cached in data/damodaran_cache.json (git-ignored) and refreshed when
# more than a week old — the betas change each January, the ERP monthly.
# If his site can't be reached, the last cached values are used, and with
# no cache at all the run falls back to the regression beta and the
# DEFAULT_EQUITY_RISK_PREMIUM above.
# ---------------------------------------------------------------------

DAMODARAN_BETAS_URL = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/Betas.html"
DAMODARAN_HOME_URL = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/home.htm"
EDGAR_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DAMODARAN_CACHE_PATH = os.path.join(DATA_DIR, "damodaran_cache.json")
INDUSTRY_MAP_PATH = os.path.join(DATA_DIR, "industry_map.json")
DAMODARAN_CACHE_MAX_AGE_DAYS = 7


def fetch_text_or_none(url):
    """Fetch a web page as text, or None on any failure. Damodaran's site
    isn't EDGAR, so it gets an ordinary browser User-Agent."""
    try:
        with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def _html_table_rows(page):
    """Every <tr> on the page as a list of cleaned cell strings."""
    rows = []
    for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", page):
        cells = re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", row)
        rows.append([" ".join(html.unescape(re.sub(r"<[^>]+>", "", c)).split()) for c in cells])
    return rows


def parse_damodaran_betas(page):
    """
    Parse Damodaran's US industry beta table (Betas.html). Columns are
    found by header name, not position, so a reordered table still
    parses. Returns {"updated": "January 2026", "industries": {name:
    {"firms", "beta", "de_ratio", "unlevered_beta", "unlevered_beta_cash"}}},
    or None if the table isn't recognizable.
    """
    rows = _html_table_rows(page)
    header = next((r for r in rows if r and r[0] == "Industry Name"), None)
    if header is None:
        return None
    def col(name):
        return next(i for i, h in enumerate(header) if h.lower() == name.lower())
    try:
        i_firms, i_beta, i_de = col("Number of firms"), col("Beta"), col("D/E Ratio")
        i_unlev, i_cash = col("Unlevered beta"), col("Unlevered beta corrected for cash")
    except StopIteration:
        return None
    industries = {}
    for r in rows:
        if len(r) <= max(i_firms, i_beta, i_de, i_unlev, i_cash) or r[0] == "Industry Name":
            continue
        try:
            industries[r[0]] = {
                "firms": int(r[i_firms]), "beta": float(r[i_beta]),
                "de_ratio": float(r[i_de].rstrip("%")) / 100,
                "unlevered_beta": float(r[i_unlev]), "unlevered_beta_cash": float(r[i_cash]),
            }
        except ValueError:
            continue
    updated = re.search(r"(?i)Updated in ([A-Za-z]+ \d{4})", page)
    return {"updated": updated.group(1) if updated else "unknown date", "industries": industries} if industries else None


def parse_damodaran_erp(page):
    """
    Read the headline implied ERP off Damodaran's home page, e.g.
    "Implied ERP on October 1, 2026 = 4.20% (Trailing 12 month, with
    adjusted payout)" — his primary monthly estimate. (His ERPbymonth.xlsx
    and annual history page lag well behind the home page.) Returns
    {"erp": 0.042, "as_of": "October 1, 2026"} or None.
    """
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page)).split())
    m = re.search(r"Implied ERP on ([A-Za-z]+ \d{1,2}, \d{4}) = ([\d.\s]+)%", text)
    if not m:
        return None
    try:
        return {"erp": float(m.group(2).replace(" ", "")) / 100, "as_of": m.group(1)}
    except ValueError:
        return None


def load_damodaran_data():
    """
    Industry betas and the current implied ERP, from cache when it's
    under DAMODARAN_CACHE_MAX_AGE_DAYS old, otherwise freshly fetched.
    Each piece falls back independently: a failed fetch keeps that
    piece's cached copy (noted as stale). Returns {"betas": ... or None,
    "erp": ... or None}.
    """
    cache = {}
    try:
        with open(DAMODARAN_CACHE_PATH) as f:
            cache = json.load(f)
    except (OSError, ValueError):
        pass
    now = datetime.now(timezone.utc)
    def is_fresh(piece):
        try:
            fetched = datetime.fromisoformat(cache[piece]["fetched"])
            return (now - fetched).days < DAMODARAN_CACHE_MAX_AGE_DAYS
        except (KeyError, ValueError, TypeError):
            return False

    changed = False
    for piece, url, parser in (("betas", DAMODARAN_BETAS_URL, parse_damodaran_betas),
                               ("erp", DAMODARAN_HOME_URL, parse_damodaran_erp)):
        if is_fresh(piece):
            continue
        page = fetch_text_or_none(url)
        parsed = parser(page) if page else None
        if parsed:
            cache[piece] = {"fetched": now.isoformat(), "data": parsed}
            changed = True
        elif piece in cache:
            print(f"  (Couldn't refresh Damodaran's {piece} — using the copy cached "
                  f"{cache[piece]['fetched'][:10]}.)")
    if changed:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(DAMODARAN_CACHE_PATH, "w") as f:
                json.dump(cache, f, indent=1)
        except OSError:
            pass  # caching is a convenience; the run continues without it
    return {piece: cache[piece]["data"] if piece in cache else None for piece in ("betas", "erp")}


# The US Treasury's daily par yield curve, one CSV per calendar year,
# newest day first — official, free, no API key.
TREASURY_YIELD_URL = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
                      "daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
                      "&field_tdr_date_value={year}&page&_format=csv")
RISK_FREE_CACHE_MAX_AGE_HOURS = 24


def parse_treasury_10yr(csv_text):
    """The most recent 10-year yield in a Treasury yield-curve CSV, as
    {"rate": 0.0505, "as_of": "10/28/2026"}, or None. Rows are newest
    first, but the max date is taken anyway rather than trusting order."""
    try:
        rows = [r for r in csv.DictReader(io.StringIO(csv_text)) if r.get("10 Yr") and r.get("Date")]
        latest = max(rows, key=lambda r: datetime.strptime(r["Date"], "%m/%d/%Y"))
        return {"rate": float(latest["10 Yr"]) / 100, "as_of": latest["Date"]}
    except (ValueError, KeyError):
        return None


def load_risk_free_rate():
    """
    Today's 10-year Treasury yield — Damodaran's risk-free rate for a US
    dollar valuation, and the same rate he measures his implied ERP
    against, so the two stay consistent. Cached (in the same file as his
    data) for a day; a failed fetch falls back to the cached yield, and
    with no cache at all returns None so the caller uses
    DEFAULT_RISK_FREE_RATE. Early January, before the new year has any
    data, falls back to last year's file.
    """
    cache = {}
    try:
        with open(DAMODARAN_CACHE_PATH) as f:
            cache = json.load(f)
    except (OSError, ValueError):
        pass
    now = datetime.now(timezone.utc)
    cached = cache.get("riskfree")
    try:
        if cached and (now - datetime.fromisoformat(cached["fetched"])).total_seconds() < \
                RISK_FREE_CACHE_MAX_AGE_HOURS * 3600:
            return cached["data"]
    except (KeyError, ValueError, TypeError):
        pass
    parsed = None
    for year in (now.year, now.year - 1):
        page = fetch_text_or_none(TREASURY_YIELD_URL.format(year=year))
        parsed = parse_treasury_10yr(page) if page else None
        if parsed:
            break
    if parsed:
        cache["riskfree"] = {"fetched": now.isoformat(), "data": parsed}
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(DAMODARAN_CACHE_PATH, "w") as f:
                json.dump(cache, f, indent=1)
        except OSError:
            pass
        return parsed
    if cached:
        print(f"  (Couldn't refresh the Treasury yield — using the copy cached {cached['fetched'][:10]}.)")
        return cached["data"]
    return None


# SEC industry (SIC) code ranges -> Damodaran industry, checked in order,
# FIRST match wins — so narrow codes are listed before the broad range
# they sit inside. Only a SUGGESTION: SIC codes are old and coarse (Viking,
# a clinical-stage biotech, files as 2834 "Pharmaceutical Preparations"),
# so the user always confirms, and the choice is saved per ticker.
SIC_TO_DAMODARAN_INDUSTRY = [
    (100, 999, "Farming/Agriculture"),
    (1040, 1049, "Precious Metals"),
    (1000, 1099, "Metals & Mining"),
    (1200, 1299, "Coal & Related Energy"),
    (1311, 1311, "Oil/Gas (Production and Exploration)"),
    (1381, 1389, "Oilfield Svcs/Equip."),
    (1400, 1499, "Metals & Mining"),
    (1520, 1531, "Homebuilding"),
    (1500, 1799, "Engineering/Construction"),
    (2080, 2085, "Beverage (Alcoholic)"),
    (2086, 2087, "Beverage (Soft)"),
    (2000, 2099, "Food Processing"),
    (2100, 2199, "Tobacco"),
    (2200, 2399, "Apparel"),
    (2400, 2499, "Paper/Forest Products"),
    (2500, 2599, "Furn/Home Furnishings"),
    (2650, 2679, "Packaging & Container"),
    (2600, 2699, "Paper/Forest Products"),
    (2700, 2799, "Publishing & Newspapers"),
    (2836, 2836, "Drugs (Biotechnology)"),
    (2830, 2835, "Drugs (Pharmaceutical)"),
    (2840, 2844, "Household Products"),
    (2800, 2829, "Chemical (Basic)"),
    (2860, 2869, "Chemical (Basic)"),  # industrial organics: commodity petrochemicals (Lyondell, Westlake)
    (2850, 2899, "Chemical (Specialty)"),
    (2900, 2999, "Oil/Gas (Integrated)"),
    (3000, 3099, "Rubber& Tires"),
    (3100, 3199, "Shoe"),
    (3200, 3299, "Building Materials"),
    (3310, 3329, "Steel"),
    (3300, 3399, "Metals & Mining"),
    (3570, 3579, "Computers/Peripherals"),
    (3400, 3599, "Machinery"),
    (3630, 3659, "Electronics (Consumer & Office)"),
    (3600, 3629, "Electrical Equipment"),
    (3660, 3669, "Telecom. Equipment"),
    (3674, 3674, "Semiconductor"),
    (3670, 3699, "Electronics (General)"),
    (3714, 3714, "Auto Parts"),
    (3710, 3716, "Auto & Truck"),
    (3720, 3729, "Aerospace/Defense"),
    (3730, 3732, "Shipbuilding & Marine"),
    (3740, 3743, "Transportation (Railroads)"),
    (3760, 3769, "Aerospace/Defense"),
    (3812, 3812, "Aerospace/Defense"),
    (3840, 3851, "Healthcare Products"),
    (3800, 3899, "Electronics (General)"),
    (3900, 3999, "Recreation"),
    (4011, 4013, "Transportation (Railroads)"),
    (4200, 4299, "Trucking"),
    (4400, 4499, "Shipbuilding & Marine"),
    (4500, 4599, "Air Transport"),
    (4600, 4699, "Oil/Gas Distribution"),
    (4000, 4799, "Transportation"),
    (4812, 4812, "Telecom (Wireless)"),
    (4830, 4839, "Broadcasting"),
    (4840, 4849, "Cable TV"),
    (4800, 4899, "Telecom. Services"),
    (4920, 4925, "Oil/Gas Distribution"),
    (4940, 4949, "Utility (Water)"),
    (4950, 4959, "Environmental & Waste Services"),
    (4900, 4919, "Power"),
    (4900, 4999, "Utility (General)"),
    (5140, 5149, "Food Wholesalers"),
    (5000, 5199, "Retail (Distributors)"),
    (5200, 5299, "Retail (Building Supply)"),
    (5300, 5399, "Retail (General)"),
    (5400, 5499, "Retail (Grocery and Food)"),
    (5500, 5599, "Retail (Automotive)"),
    (5800, 5899, "Restaurant/Dining"),
    (5600, 5999, "Retail (Special Lines)"),
    (6020, 6029, "Banks (Regional)"),
    (6000, 6099, "Banks (Regional)"),
    (6282, 6282, "Investments & Asset Management"),
    (6200, 6299, "Brokerage & Investment Banking"),
    (6311, 6311, "Insurance (Life)"),
    (6331, 6331, "Insurance (Prop/Cas.)"),
    (6300, 6399, "Insurance (General)"),
    (6798, 6798, "R.E.I.T."),
    (6500, 6599, "Real Estate (Operations & Services)"),
    (6100, 6199, "Financial Svcs. (Non-bank & Insurance)"),
    (6700, 6799, "Investments & Asset Management"),
    (7000, 7099, "Hotel/Gaming"),
    (7310, 7319, "Advertising"),
    (7370, 7370, "Software (Internet)"),
    (7372, 7372, "Software (System & Application)"),
    (7374, 7374, "Information Services"),
    (7371, 7379, "Computer Services"),
    (7200, 7399, "Business & Consumer Services"),
    (7800, 7999, "Entertainment"),
    (8060, 8069, "Hospitals/Healthcare Facilities"),
    (8000, 8099, "Healthcare Support Services"),
    (8200, 8299, "Education"),
    (8731, 8731, "Drugs (Biotechnology)"),
    (8711, 8711, "Engineering/Construction"),
    (8000, 8999, "Business & Consumer Services"),
]


def get_sic(cik):
    """(SIC code as int, its description) from EDGAR, or (None, None)."""
    data = fetch_json_or_none(EDGAR_SUBMISSIONS_URL.format(cik=cik))
    try:
        return int(data["sic"]), data.get("sicDescription") or ""
    except (TypeError, KeyError, ValueError):
        return None, None


def suggest_industry(sic, biotech=False):
    """First SIC_TO_DAMODARAN_INDUSTRY match for this code, or None. In
    --biotech mode, any drug-manufacturing code suggests Biotechnology —
    a clinical-stage company's risk looks like biotech's, not big pharma's,
    whatever its SIC code says."""
    if sic is None:
        return None
    if biotech and 2830 <= sic <= 2836:
        return "Drugs (Biotechnology)"
    for low, high, industry in SIC_TO_DAMODARAN_INDUSTRY:
        if low <= sic <= high:
            return industry
    return None


def load_industry_map():
    """Saved per-ticker industry choices ({TICKER: {"industries": [[name,
    weight], ...]}}), or {} if none yet."""
    try:
        with open(INDUSTRY_MAP_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_industry_choice(ticker, industries, sic):
    """Remember this ticker's industry mix so every later run uses the
    same beta without asking again. Kept in data/industry_map.json, which
    is meant to be committed — it's a record of your judgment calls."""
    saved = load_industry_map()
    saved[ticker.upper()] = {"industries": industries, "sic": sic}
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(INDUSTRY_MAP_PATH, "w") as f:
            json.dump(saved, f, indent=2, sort_keys=True)
    except OSError:
        print("  (Couldn't save the industry choice — it'll be asked again next run.)")


def pick_one_industry(industries, prompt):
    """Search Damodaran's industry list by name and pick one; returns the
    exact industry name, or None if the user presses Enter to stop."""
    while True:
        query = input(prompt).strip().lower()
        if not query:
            return None
        matches = [n for n in industries if query in n.lower()]
        if not matches:
            print("    No industry matches that — try a shorter word (e.g. chem, soft, retail).")
            continue
        for i, name in enumerate(matches, 1):
            d = industries[name]
            print(f"    {i:>2}. {name:<42} unlevered beta {d['unlevered_beta_cash']:.2f}  ({d['firms']} firms)")
        pick = input("    Number (Enter to search again): ").strip()
        if pick.isdigit() and 1 <= int(pick) <= len(matches):
            return matches[int(pick) - 1]


def choose_industries(ticker, sic, sic_description, industries, biotech=False):
    """
    Decide which Damodaran industry (or weighted mix, for a company in
    several businesses) this company's beta comes from. Offers the saved
    choice for this ticker if there is one, otherwise the SIC-based
    suggestion; Enter accepts, a word searches, 'mix' builds a weighted
    blend. Saves whatever is chosen. Returns [[name, weight], ...] or
    None if no industry was chosen.
    """
    saved = load_industry_map().get(ticker.upper())
    saved_mix = saved["industries"] if saved and all(n in industries for n, _ in saved["industries"]) else None
    suggestion = suggest_industry(sic, biotech)
    if sic is not None:
        print(f"  SEC industry code: {sic} {sic_description}")
    if saved_mix:
        label = " + ".join(f"{n} ({w:.0%})" for n, w in saved_mix) if len(saved_mix) > 1 else saved_mix[0][0]
        print(f"  Saved choice for {ticker.upper()}: {label}")
        default = saved_mix
    elif suggestion in industries:
        print(f"  Suggested industry: {suggestion}")
        default = [[suggestion, 1.0]]
    else:
        print("  No industry suggestion for this company's SEC code.")
        default = None

    answer = input("  Press Enter to use it, type part of a name to search (e.g. chem), or 'mix' for\n"
                   "  a company in several businesses: " if default else
                   "  Type part of an industry name to search (e.g. chem), or 'mix': ").strip()
    if not answer and default:
        chosen = default
    elif answer.lower() == "mix":
        chosen = []
        print("  Add each business with its share of revenue. Enter a blank name when done.")
        while True:
            name = pick_one_industry(industries, "    Business industry (search word, Enter when done): ")
            if name is None:
                break
            share = prompt_number(f"    Share of revenue for {name} (e.g. 60 for 60%): ")
            chosen.append([name, share / 100])
        chosen = [[n, w] for n, w in chosen if w > 0] or None
    else:
        matches = [n for n in industries if answer.lower() in n.lower()]
        if len(matches) == 1:
            chosen = [[matches[0], 1.0]]
        else:
            if matches:
                for i, name in enumerate(matches, 1):
                    d = industries[name]
                    print(f"    {i:>2}. {name:<42} unlevered beta {d['unlevered_beta_cash']:.2f}  ({d['firms']} firms)")
                pick = input("    Number (Enter to search again): ").strip()
                if pick.isdigit() and 1 <= int(pick) <= len(matches):
                    name = matches[int(pick) - 1]
                else:
                    name = pick_one_industry(industries, "  Search industry (Enter to skip): ")
            else:
                print("    No industry matches that.")
                name = pick_one_industry(industries, "  Search industry (Enter to skip): ")
            chosen = [[name, 1.0]] if name else None
    if chosen:
        save_industry_choice(ticker, chosen, sic)
    return chosen


def run_full_valuation(ticker, stage_arg=None, narrative=False, biotech=False, reit=False,
                       cyclical=False):
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
    rebuilt = pretax_plus_interest_series(facts, years=1)
    if financials["operating_income"]["value"] is None and rebuilt:
        latest_rebuilt = rebuilt[-1]
        if fx_currency and latest_rebuilt["unit"] == fx_currency:
            latest_rebuilt = {**latest_rebuilt, "val": latest_rebuilt["val"] * fx_rate}
        print("  'EBIT / operating income' isn't tagged — this company's income statement has no")
        print(f"  operating-income line. Rebuilt as pretax income + interest expense (FY"
              f"{latest_rebuilt['fiscal_year']}): ${latest_rebuilt['val']:,.0f}")
        ebit = prompt_number("  Press Enter to use it, or type a corrected EBIT (in dollars): ",
                             default=latest_rebuilt["val"])
    else:
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

    # ---- Cycle normalization (Damodaran-style): a cyclical's latest
    # year mostly says where the cycle is, not what the business earns.
    # Value it on its average margin over a full cycle (up to 10 years,
    # loss years included — see cycle_margin_history) applied to current
    # revenue. Runs when EBIT is negative; when it's positive but under
    # half the cycle average (Olin: +$5.3M on a 0.1% margin vs ~10%, which
    # otherwise gives ~0x interest coverage, a "D" rating and a 19.5%
    # cost of debt); or always with --cyclical. Historical margins are
    # as reported, so the current year's R&D-capitalization adjustment
    # is compared out and added back on top of the normalized figure. ----
    rd_ebit_adjustment = rd_result["ebit_adjustment"] if rd_result else 0.0
    cycle_rows = cycle_margin_history(facts) if revenue else []
    full_cycle = cycle_stats_or_none(cycle_rows)
    reported_margin = (ebit - rd_ebit_adjustment) / revenue if revenue else None
    is_trough = (full_cycle is not None and reported_margin is not None and full_cycle["mean"] > 0
                 and reported_margin < 0.5 * full_cycle["mean"])
    cycle_choice = None
    if ebit < 0 or is_trough or (cyclical and full_cycle is not None):
        reason = ("latest FY is negative" if ebit < 0 else
                  "latest FY looks like a cyclical trough" if is_trough else "--cyclical")
        print(f"\n{'='*60}")
        print(f"  EBIT normalization over the cycle ({reason})")
        print(f"{'='*60}")
        print(f"  Latest FY EBIT: ${ebit:,.0f}" +
              (f"  (margin {ebit / revenue:.1%})" if revenue else "  (no revenue yet)"))
        if full_cycle is None:
            print("  Not enough revenue/EBIT history to average a margin over — keeping the")
            print("  latest-FY EBIT; if it's negative the FCFF-based DCF below will likely still")
            print("  break (see the negative-FCFF warning) — the EV/Revenue fallback is the")
            print("  better tool here.")
        else:
            if is_trough:
                print(f"  That's under half the {full_cycle['n_years']}-year average margin "
                      f"({full_cycle['mean']:.1%}).")
            cycle_choice = choose_cycle_margin(cycle_rows)
            if cycle_choice is not None and cycle_choice["margin"] <= 0:
                print(f"  The {cycle_choice['method']} margin over {cycle_choice['span']} is a loss "
                      f"({cycle_choice['margin']:.1%}) — there's no")
                print("  profitable 'normal' to revert to, so keeping the latest-FY EBIT. If it's")
                print("  negative, the EV/Revenue fallback below is the better tool.")
                cycle_choice = None
            elif cycle_choice is not None:
                normalized_ebit = cycle_choice["margin"] * revenue + rd_ebit_adjustment
                print(f"  Normalized EBIT: ${normalized_ebit:,.0f}  ({cycle_choice['margin']:.1%} "
                      f"{cycle_choice['method']} margin, {cycle_choice['span']}, x current revenue"
                      + (f", + R&D adjustment {rd_ebit_adjustment:+,.0f}" if rd_ebit_adjustment else "") + ")")
                print("  For a cyclical, the normalized figure is the better base; for a business in")
                print("  structural decline, keep the latest figure.")
                keep = input("  Press Enter to use the normalized EBIT, or type k to keep the latest: ").strip().lower()
                if keep != "k":
                    ebit = normalized_ebit
                    print("  Using the normalized figure below (including for the credit rating).")
                else:
                    cycle_choice = None

    pretax_income = get_metric_value(financials, "pretax_income", "pretax income", stale)
    tax_expense = get_metric_value(financials, "income_tax_expense", "income tax expense", stale)

    # CapEx and D&A are naturally lumpy (a big equipment buy, an
    # acquisition-driven step-up in amortization) — a single year can swing
    # the reinvestment rate and FCFF hard in either direction. Damodaran
    # normalizes both over a multi-year window before using them, so we
    # use a trailing 5-year average here instead of the latest year alone.
    latest_d_and_a, d_and_a_overridden = get_metric_value(
        financials, "depreciation_amortization", "depreciation & amortization", stale,
        return_override_flag=True, default=0.0)
    latest_capex, capex_overridden = get_metric_value(
        financials, "capex", "capital expenditures", stale, return_override_flag=True, default=0.0)
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

    total_debt = resolve_total_debt(financials, facts, stale, fx_currency, fx_rate)
    cash = get_metric_value(financials, "cash", "cash and equivalents", stale)
    sti_val, sti_tag, sti_fy, sti_unit = freshest_annual_value(facts, SHORT_TERM_INVESTMENT_TAGS)
    cash_fy = financials["cash"]["fiscal_year"]
    if sti_val and cash_fy is not None and sti_fy >= cash_fy:
        if fx_currency and sti_unit == fx_currency:
            sti_val *= fx_rate
        print(f"  Short-term investments: ${sti_val:,.0f} (tag: {sti_tag}, FY{sti_fy}) — added to cash,")
        print(f"  as Damodaran does. Cash + investments: ${cash + sti_val:,.0f}")
        cash += sti_val
    shares_out = get_metric_value(financials, "shares_outstanding", "shares outstanding", stale)

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

    # ---- Bottom-up beta (Damodaran): a single company's regression beta
    # has a standard error of ~0.2-0.3 (Lyondell's came out 0.35), so the
    # default is the INDUSTRY's unlevered beta — averaged across dozens or
    # hundreds of firms, which cancels most of that noise — relevered to
    # THIS company's market D/E at the marginal tax rate. Uses the
    # cash-corrected unlevered beta (the business's own risk), since the
    # DCF adds cash back separately. The regression beta stays on screen
    # for comparison, and any number can still be typed in. ----
    debt_to_equity = total_debt / market_cap if market_cap else 0
    regression_beta = beta
    damodaran = load_damodaran_data()

    print(f"\n{'='*60}")
    print("  Beta (bottom-up, Damodaran industry average)")
    print(f"{'='*60}")
    print(f"  Regression beta (from market data): {regression_beta:.2f}  — one company's, noisy")
    print(f"  Company's D/E (debt/market cap):    {debt_to_equity:.2f}")
    beta_source = "regression beta (as pulled)"
    industry_beta = None
    if damodaran["betas"]:
        industries = damodaran["betas"]["industries"]
        if fx_currency:
            print("  Note: this is a foreign filer — these are Damodaran's US industry betas;")
            print("  his regional tables can be used by typing an unlevered beta below.")
        sic, sic_description = get_sic(cik)
        mix = choose_industries(ticker, sic, sic_description, industries, biotech)
        if mix:
            unlevered = model.weighted_unlevered_beta(
                [(industries[name]["unlevered_beta_cash"], weight) for name, weight in mix])
            industry_beta = model.relever_beta(unlevered, DEFAULT_MARGINAL_TAX_RATE, debt_to_equity)
            for name, weight in mix:
                d = industries[name]
                print(f"    {name:<42} unlevered {d['unlevered_beta_cash']:.2f}  ({d['firms']} firms)"
                      + (f"  x {weight:.0%}" if len(mix) > 1 else ""))
            print(f"  Unlevered beta: {unlevered:.2f}  -> relevered at D/E {debt_to_equity:.2f}, "
                  f"{DEFAULT_MARGINAL_TAX_RATE:.0%} marginal tax: {industry_beta:.2f}")
            print(f"  (Damodaran's US industry betas, updated {damodaran['betas']['updated']})")
            industry_label = " + ".join(name for name, _ in mix)
    else:
        print("  Damodaran's industry betas couldn't be loaded (no connection and no cache).")

    if industry_beta is not None:
        answer = input(f"  Press Enter to use the industry beta {industry_beta:.2f}, type r for the regression\n"
                       "  beta, or type an UNLEVERED beta to relever instead: ").strip().lower()
    else:
        answer = input("  Press Enter to use the regression beta, or type an UNLEVERED beta to\n"
                       "  relever instead: ").strip().lower()
    if answer == "r" or (not answer and industry_beta is None):
        beta = regression_beta
    elif not answer:
        beta = industry_beta
        beta_source = f"industry: {industry_label}, unlevered {unlevered:.2f}, relevered"
    else:
        try:
            typed_unlevered = parse_number(answer)
            beta = model.relever_beta(typed_unlevered, DEFAULT_MARGINAL_TAX_RATE, debt_to_equity)
            beta_source = f"typed unlevered {typed_unlevered:.2f}, relevered"
        except ValueError:
            print("  Couldn't read that — using the " +
                  ("industry beta." if industry_beta is not None else "regression beta."))
            beta = industry_beta if industry_beta is not None else regression_beta
            if industry_beta is not None:
                beta_source = f"industry: {industry_label}, unlevered {unlevered:.2f}, relevered"
    print(f"  Using beta: {beta:.2f}")

    # ---- Risk-free rate: today's 10-year Treasury yield, not a fixed
    # number — and the same rate Damodaran's implied ERP below is measured
    # against, so the two move together. ----
    print(f"\n{'='*60}")
    print("  Risk-free rate")
    print(f"{'='*60}")
    treasury = load_risk_free_rate()
    if treasury:
        default_rf = treasury["rate"]
        risk_free_source = f"10-year Treasury, {treasury['as_of']}"
        print(f"  10-year Treasury yield on {treasury['as_of']}: {default_rf:.2%}  (US Treasury daily yield curve)")
    else:
        default_rf = DEFAULT_RISK_FREE_RATE
        risk_free_source = "fallback default (Treasury yield couldn't be loaded)"
        print(f"  The Treasury yield couldn't be loaded — falling back to {default_rf:.2%}.")
    if fx_currency:
        print(f"  Note: this is a US-dollar risk-free rate; the financials were converted from {fx_currency}")
        print("  to USD above, so the dollar rate is the consistent one.")
    risk_free_rate = prompt_number(f"  Press Enter to use {default_rf:.2%}, or type a rate as a percent "
                                   "(e.g. 4.5 for 4.5%): ", default=default_rf * 100) / 100
    if abs(risk_free_rate - default_rf) > 1e-12:
        risk_free_source = "typed"

    # ---- Equity risk premium: Damodaran's current implied ERP (what the
    # S&P 500's price implies investors expect over the risk-free rate,
    # updated monthly) rather than a fixed long-run number. ----
    print(f"\n{'='*60}")
    print("  Equity risk premium")
    print(f"{'='*60}")
    if damodaran["erp"]:
        default_erp = damodaran["erp"]["erp"]
        erp_source = f"Damodaran implied ERP, {damodaran['erp']['as_of']}"
        print(f"  Damodaran's implied ERP on {damodaran['erp']['as_of']}: {default_erp:.2%}")
        print(f"  (Measured against the 10-year Treasury yield — the risk-free rate above.)")
    else:
        default_erp = DEFAULT_EQUITY_RISK_PREMIUM
        erp_source = "fallback default (Damodaran's ERP couldn't be loaded)"
        print(f"  Damodaran's current ERP couldn't be loaded — falling back to {default_erp:.2%}.")
    erp = prompt_number(f"  Press Enter to use {default_erp:.2%}, or type an ERP as a percent (e.g. 5 for 5%): ",
                        default=default_erp * 100) / 100
    if abs(erp - default_erp) > 1e-12:
        erp_source = "typed"

    # Computed here (rather than down with WACC) because the dividend
    # discount model below needs cost of equity too, before WACC exists.
    coe = model.cost_of_equity(risk_free_rate, beta, erp)

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
            ebit, latest_interest_expense, risk_free_rate, large_firm=is_large_firm)

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
              f"({risk_free_rate:.2%} risk-free + {default_spread:.2%} spread)")
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
    print(f"  Beta:                      {beta:.2f}  ({beta_source})")
    print(f"  Risk-free rate:            {risk_free_rate:.2%}  ({risk_free_source})")
    print(f"  Equity risk premium:       {erp:.2%}  ({erp_source})")
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
                                                       risk_free_rate)
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
        if cycle_choice is not None:
            # Already normalized to the cycle average above, so that's the
            # natural default target too — a cyclical reverts to it.
            margin_input = input(f"  Target (mature) operating margin (e.g. 20 for 20%; Enter for the "
                                 f"{cycle_choice['span']} cycle {cycle_choice['method']}, "
                                 f"{cycle_choice['margin']:.1%}): ").strip()
            default_target = cycle_choice["margin"]
        else:
            margin_input = input("  Target (mature) operating margin (e.g. 20 for 20%; Enter to hold today's "
                                  "margin flat): ").strip()
            default_target = current_margin
        target_margin = parse_number(margin_input) / 100 if margin_input else default_target
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
    parser.add_argument(
        "--cyclical", action="store_true",
        help="Cyclical mode: always normalizes EBIT to the company's average margin over "
             "a full cycle (up to 10 years, loss years included), with a year-by-year "
             "table and a choice of window and averaging method, and uses that margin as "
             "the narrative DCF's default target. Without this flag, normalization still "
             "runs when EBIT is negative or under half its cycle average.",
    )
    args = parser.parse_args()

    run_full_valuation(args.ticker, args.stage, args.narrative, args.biotech, args.reit, args.cyclical)
