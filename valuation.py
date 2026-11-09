"""
valuation.py — Pull US public company financials from SEC EDGAR and run a
WACC/DCF valuation, Damodaran-style.

USAGE (from Terminal):
    python3 valuation.py AAPL
    python3 valuation.py ARM --narrative              (story-based "Ginzu" DCF)
    python3 valuation.py SCHW --financial             (banks / brokers: excess return model)
    python3 valuation.py LAC --project                (pre-production mine or plant)
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
      identifying you (a name and email): the first run asks for it and
      saves it to data/sec_contact.txt, which stays on your machine. Or set
      the DCF_SEC_CONTACT environment variable ("Jane Doe jane@example.com").
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
import math
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


# ---- OPTIONAL: paste your Financial Modeling Prep key here ----
# Get a free key at financialmodelingprep.com. If left blank, or if the
# key doesn't work, the script will ask you to type in beta and share
# price by hand instead (takes 30 seconds, look them up on Yahoo Finance).
FMP_API_KEY = ""
# -----------------------------------------------------------------

SEC_CONTACT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "sec_contact.txt")
_sec_user_agent = None


def sec_user_agent():
    """The "Name email" SEC asks every EDGAR request to identify itself
    with — from DCF_SEC_CONTACT, else data/sec_contact.txt (kept out of
    git), else asked once and saved there. Kept out of the code so the
    repository can be public without anyone's email in it."""
    global _sec_user_agent
    if _sec_user_agent:
        return _sec_user_agent
    contact = os.environ.get("DCF_SEC_CONTACT", "").strip()
    if not contact:
        try:
            with open(SEC_CONTACT_PATH) as f:
                contact = f.read().strip()
        except OSError:
            pass
    if not contact:
        print("  SEC asks every EDGAR request to say who's making it (a name and an email).")
        contact = input("  Your name and email (e.g. Jane Doe jane@example.com; saved locally, asked once): ").strip()
        try:
            os.makedirs(os.path.dirname(SEC_CONTACT_PATH), exist_ok=True)
            with open(SEC_CONTACT_PATH, "w") as f:
                f.write(contact + "\n")
        except OSError:
            pass
    _sec_user_agent = contact
    return contact

EDGAR_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
EDGAR_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
FMP_PROFILE_URL = "https://financialmodelingprep.com/api/v3/profile/{ticker}?apikey={key}"

# Reasonable defaults used if you don't override them at the prompt.
DEFAULT_RISK_FREE_RATE = 0.045   # fallback only — normally today's 10-year Treasury yield (see load_risk_free_rate)
DEFAULT_EQUITY_RISK_PREMIUM = 0.055  # fallback only — normally Damodaran's current implied ERP (see load_damodaran_data)
DEFAULT_MARGINAL_TAX_RATE = 0.25    # US federal + state statutory, for relevering betas (Damodaran relevers at the marginal rate)
DEFAULT_TERMINAL_GROWTH = 0.025  # roughly long-run GDP growth
# Damodaran's line between his large- and small-firm rating tables; also
# where --financial switches a bank to the money-center beta.
LARGE_FIRM_MARKET_CAP = 5_000_000_000
# --financial: above this a bank also takes the money-center industry ROE
# as its stable-ROE default. A judgment line (not Damodaran's): the money-
# center average is ~15 giant banks, which mid-size banks don't earn like.
MONEY_CENTER_ROE_MARKET_CAP = 100_000_000_000
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
        # ifrs-full: short-term borrowings and the current portion of long-term
        # borrowings are separate concepts by definition (their total is the
        # combined tag above), so a filer tagging both needs them summed (FEMSA)
        ("ShorttermBorrowings", "CurrentPortionOfLongtermBorrowings"),
        "ShorttermBorrowings",  # ifrs-full casing
        "ShortTermBorrowings",  # us-gaap casing (genuinely a different tag — XBRL names are case-sensitive) — e.g. GE Vernova
        "ConvertibleNotesPayableCurrent",  # e.g. Akamai
        "CurrentPortionOfLongtermBorrowings",  # ifrs-full: TSMC, whose short-term borrowings stopped in FY2021
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
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAndDiscontinuedOperations",  # e.g. Hovnanian
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
        "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",  # e.g. Biogen switched to
        # this tag in FY2021, which breaks out acquired in-process R&D (an M&A-driven, lumpy
        # expense) separately — the old tag above just stops being filed and goes stale.
    ],
    "interest_expense": [
        "InterestExpense",
        "InterestExpenseDebt",
        "InterestExpenseNonoperating",
        "InterestExpenseOperating",  # e.g. Coherent, Cloudflare
        "FinanceCosts",  # ifrs-full
        # Homebuilders capitalize interest into their houses and expense it
        # through cost of sales, so the income statement shows little or no
        # interest expense (D.R. Horton, Meritage) — this is the interest
        # actually charged that year, the financing cost to add back to EBIT.
        "RealEstateInventoryCapitalizedInterestCostsCostOfSales1",
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


_SEC_LAST_REQUEST = [0.0]
_JSON_CACHE = {}  # url -> parsed JSON: the ticker map for the whole run, and the last few companies' facts


def fetch_json(url):
    """Fetch a URL and parse it as JSON, sending the required SEC User-Agent
    header. Paced to SEC's 10-requests-a-second limit and retried with
    backoff when SEC answers 429 (too many requests) or a 5xx: a screen
    running several companies at once got rate-limited on 480 of 700.
    The ticker map and the last few companies' facts are kept in memory,
    so a company's filings are downloaded once per run."""
    if url in _JSON_CACHE:
        return _JSON_CACHE[url]
    attempts = 9
    for attempt in range(attempts):
        wait = 0.12 - (time.time() - _SEC_LAST_REQUEST[0])
        if wait > 0:
            time.sleep(wait)
        _SEC_LAST_REQUEST[0] = time.time()
        try:
            with urlopen(Request(url, headers={"User-Agent": sec_user_agent()}), timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                # SEC blocks a client for about 10 minutes after a burst: back off
                # 2s, 4s, ... 256s (about 8.5 minutes in all) before giving up.
                pause = 2 * 2 ** attempt
                print(f"  (SEC answered HTTP {e.code}; waiting {pause}s and retrying)", file=sys.stderr)
                time.sleep(pause)
                continue
            print(f"ERROR fetching {url}: HTTP {e.code}")
            if e.code == 403:
                print("This usually means the User-Agent header is missing or malformed.")
                print(f"Check the name and email in {SEC_CONTACT_PATH} (or DCF_SEC_CONTACT).")
            sys.exit(1)
        except OSError as e:  # a timeout or dropped connection
            if attempt < 5:
                time.sleep(2 * 2 ** attempt)
                continue
            print(f"ERROR fetching {url}: {e}")
            sys.exit(1)
    if url != EDGAR_TICKER_MAP_URL:
        for old in [k for k in _JSON_CACHE if k != EDGAR_TICKER_MAP_URL][:-2]:
            del _JSON_CACHE[old]  # keep memory small across a long screen
    _JSON_CACHE[url] = data
    return data


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


def _is_currency_code(unit):
    """"MXN", "USD" (ISO codes) — not "shares", "pure" or "MXN/shares"."""
    return len(unit) == 3 and unit.isalpha() and unit.isupper()


_PRIMARY_CURRENCY_CACHE = [None, None, None]  # (sample tag dict kept alive, tag count, currency)


def primary_reporting_currency(all_tags):
    """
    The currency the filer actually keeps its books in: the majority
    currency among the facts in its most recent annual report.

    Some foreign filers tag a USD "convenience translation" alongside
    their own currency under the SAME tag (FEMSA: 148 of its 337 tags in
    both MXN and USD), but only for some years — its pre-tax income has
    USD for 2017, 2023 and 2024 and nothing in between. Letting both
    through meant the currency of each year depended on JSON key order,
    and multi-year figures (the trailing tax rate) mixed years translated
    at the filer's own historical rates with years we convert at today's.
    The latest report with currency facts decides, so a filer that
    switched currencies is read in the one it uses now.
    """
    sample = next(iter(all_tags.values()), None)
    cache = _PRIMARY_CURRENCY_CACHE
    if cache[0] is sample and cache[1] == len(all_tags):
        return cache[2]
    # Currency facts per annual report: {accn: (filed, {currency: count})}.
    # Only reports with currency facts count — SEC's companyfacts can lag a
    # new filing and carry just its cover-page share counts (TSMC's and
    # FEMSA's FY2025 20-Fs), which would otherwise leave no currency to pick.
    reports = {}
    for data in all_tags.values():
        for unit, entries in data.get("units", {}).items():
            if not _is_currency_code(unit):
                continue
            for e in entries:
                if e.get("form") in ("10-K", "20-F", "40-F") and e.get("accn"):
                    filed, counts = reports.setdefault(e["accn"], (e.get("filed", ""), {}))
                    counts[unit] = counts.get(unit, 0) + 1
    currency = None
    if reports:
        _, counts = max(reports.values(), key=lambda r: r[0])
        currency = max(sorted(counts), key=counts.get)
    cache[:] = [sample, len(all_tags), currency]
    return currency


def _units_to_read(units, primary):
    """A tag's units minus any other-currency duplicate of the primary
    currency — "USD" dropped when "MXN" is there, "USD/shares" when
    "MXN/shares" is. A tag with no primary-currency version keeps
    whatever it has."""
    if not primary:
        return list(units)
    def split(unit):
        head, _, rest = unit.partition("/")
        return head, rest
    has_primary = {split(u)[1] for u in units if split(u)[0] == primary}
    return [u for u in units
            if not (_is_currency_code(split(u)[0]) and split(u)[0] != primary and split(u)[1] in has_primary)]


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
        # whatever currency/unit this filer actually reports in — USD, EUR, CAD, shares, ... —
        # but only its own currency when a convenience translation sits beside it
        for unit_key in _units_to_read(units, primary_reporting_currency(all_tags)):
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
    series = annual_revenue_series(facts_json, years)
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


# After-tax income from continuing operations, NCI-inclusive — used to
# rebuild pretax income (this + income tax expense) for a filer with no
# "before income taxes" subtotal tagged at all. Found live on Simon
# Property Group (SPG): checked whether this was a REIT-wide gap first
# (it isn't — Public Storage, Prologis, Realty Income, VICI, and Equinix
# all tag the usual pretax subtotal fine) — it's SPG-specific, but the
# reason this particular rebuild is a SAFE substitute (rather than just
# another approximation) IS REIT-specific: a REIT's corporate income tax
# is near-zero by construction (it must distribute ~90%+ of taxable
# income to keep that status), so after-tax and pretax income are
# already close to the same number for a REIT. For an ordinary taxable
# company missing this tag, don't add it here — subtracting the wrong
# amount of tax would be a real error, not a rounding one.
CONTINUING_OPS_INCOME_TAGS = [
    "IncomeLossFromContinuingOperationsIncludingPortionAttributableToNoncontrollingInterest",
    "ProfitLoss",  # SPG itself switched to this tag after FY2016 for the same line
    "NetIncomeLoss",
]


def pretax_from_continuing_ops_series(facts_json, years=5):
    """
    Pretax income rebuilt as (after-tax income from continuing
    operations, NCI-inclusive) + income tax expense — the mirror image
    of pretax_plus_interest_series's EBIT rebuild, for a filer with no
    pretax-income tag at all. Only years with both figures are kept.
    """
    income = get_annual_series(facts_json, CONTINUING_OPS_INCOME_TAGS, years)
    tax_by_end = {e["end"]: e for e in get_annual_series(facts_json, METRIC_TAGS["income_tax_expense"], years)}
    series = []
    for inc in income:
        t = tax_by_end.get(inc["end"])
        if t is not None:
            series.append({**inc, "val": inc["val"] + t["val"]})
    return series


def annual_revenue_series(facts_json, years=10):
    """Revenue by fiscal year, oldest first, like get_annual_series — but
    when several revenue tags report the same year, the largest wins: total
    revenue is never smaller than a piece of it, and a filer can tag a small
    sub-line under a generic tag (MGP Ingredients' 'Revenues' held $13-23M a
    year in 2013-2018 beside $318-376M of real sales, which made its cycle
    margin 70%)."""
    by_end = {}
    for recipe in METRIC_TAGS["revenue"]:
        for e in get_annual_series(facts_json, [recipe], years + 10):
            best = by_end.get(e["end"])
            if best is None or (e["unit"] == best["unit"] and e["val"] > best["val"]):
                by_end[e["end"]] = e
    return [by_end[end] for end in sorted(by_end)][-years:]


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
    revenue = {e["end"]: e for e in annual_revenue_series(facts_json, pull)}
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


# ---------------------------------------------------------------------
# LATEST-QUARTER DATA: Damodaran values off the most recent trailing
# twelve months, not the last 10-K, which can be up to a year old by the
# time the next one is filed. Flow figures (revenue, EBIT, ...) become
#     TTM = last fiscal year + this year's year-to-date - last year's
#           same year-to-date
# using the year-to-date columns in the latest 10-Q (each 10-Q reports
# both the current and prior-year YTD). Balance-sheet figures (cash,
# debt, equity, shares) simply take the latest 10-Q's value. When there's
# no 10-Q newer than the 10-K — right after a 10-K, or for 20-F filers,
# who don't file 10-Qs — the annual figure stands.
# ---------------------------------------------------------------------

def _days(start, end):
    from datetime import date
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def _quarterly_entries(facts_json, tag, unit):
    """All 10-Q entries for one tag in one unit."""
    entries = _all_taxonomy_tags(facts_json).get(tag, {}).get("units", {}).get(unit, [])
    return [e for e in entries if e.get("form") == "10-Q" and e.get("end")]


def latest_quarter_instant(facts_json, tag, unit, after_end):
    """The newest 10-Q balance-sheet (instant) value for `tag` dated after
    `after_end`, as (value, end_date), or (None, None)."""
    instants = [e for e in _quarterly_entries(facts_json, tag, unit)
                if not e.get("start") and e["end"] > after_end]
    if not instants:
        return None, None
    latest = max(instants, key=lambda e: (e["end"], e.get("filed", "")))
    return latest["val"], latest["end"]


def trailing_twelve_months(facts_json, tag, unit, annual_end, annual_val):
    """
    TTM for a flow figure: annual_val (the fiscal year ending annual_end)
    + the latest year-to-date period that starts right after annual_end
    - the matching prior-year year-to-date. Returns (ttm, ytd_end) or
    (None, None) when there's no newer year-to-date figure or no matching
    prior-year one to swap out. Dates are matched with a week's slack for
    52/53-week fiscal years.
    """
    entries = [e for e in _quarterly_entries(facts_json, tag, unit) if e.get("start")]
    ytd = [e for e in entries if e["end"] > annual_end and 0 < _days(annual_end, e["start"]) <= 8]
    if not ytd:
        return None, None
    current = max(ytd, key=lambda e: (e["end"], e.get("filed", "")))
    length = _days(current["start"], current["end"])
    prior = [e for e in entries
             if abs(_days(e["end"], current["end"]) - 364) <= 8
             and abs(_days(e["start"], e["end"]) - length) <= 8]
    if not prior:
        return None, None
    prior_ytd = max(prior, key=lambda e: e.get("filed", ""))
    return annual_val + current["val"] - prior_ytd["val"], current["end"]


def quarterly_spike(facts_json, tag, unit):
    """
    (latest, typical, end_date) when the latest tagged three-month quarter of
    `tag` is far out of line with the four before it — more than three times
    their median and at least twice the median's size above it, or a
    profit/loss flip worth more than three median quarters; else None. Needs
    at least three earlier quarters within the last ~15 months.
    """
    quarters = {}
    for e in _all_taxonomy_tags(facts_json).get(tag, {}).get("units", {}).get(unit, []):
        if e.get("start") and e.get("end") and 80 < _days(e["start"], e["end"]) < 100:
            quarters[e["end"]] = e["val"]
    ends = sorted(quarters)
    if len(ends) < 4:
        return None
    latest_end = ends[-1]
    prior = [quarters[e] for e in ends[-5:-1] if 0 < _days(e, latest_end) <= 460]
    if len(prior) < 3:
        return None
    typical = sorted(prior)[len(prior) // 2] if len(prior) % 2 else sum(sorted(prior)[len(prior) // 2 - 1:len(prior) // 2 + 1]) / 2
    latest = quarters[latest_end]
    gap = latest - typical
    if abs(gap) <= 3 * max(abs(typical), 1.0) or abs(gap) < 2 * abs(typical):
        return None
    return latest, typical, latest_end


def _period_label(start, end):
    days = _days(start, end)
    return (f"quarter ending {end}" if days < 120 else
            f"year to date ending {end}" if days < 330 else f"year ending {end}")


def latest_reported_dilution(facts_json):
    """
    The company's own reported dilution: weighted-average DILUTED shares
    minus BASIC shares for the most recent period that reports both (a
    10-Q quarter if newer than the 10-K, else the fiscal year). Diluted
    shares are computed by the company under the treasury-stock method
    from every actual option tranche, RSU and convertible, and audited —
    no assumed option term, volatility or averaged strike, which is why
    this is used instead of valuing options with Black-Scholes.

    In a loss period diluted equals basic — every option and RSU would
    shrink the loss per share, so all are excluded as anti-dilutive —
    and the difference says nothing. The company then reports the count
    it excluded (AntidilutiveSecuritiesExcludedFromComputationOfEarnings-
    PerShareAmount; Roblox: 37.3M, about 5% of its shares), which is used
    instead when filed for the same period. That count is gross: options
    are counted in full whatever their strike, and convertibles as if
    converted.

    Returns (dilutive_shares, period_label, source) with source
    "diluted-basic" or "antidilutive"; (0.0, label, "diluted-basic") when
    diluted equals basic and no excluded count is filed for that period
    (so "none reported", not "none exists"); (None, None, None) when the
    counts aren't reported at all.
    """
    tags = _all_taxonomy_tags(facts_json)
    def periods(tag):
        out = {}
        for e in tags.get(tag, {}).get("units", {}).get("shares", []):
            if e.get("form") in ("10-K", "10-Q", "20-F", "40-F") and e.get("start") and e.get("end"):
                out[(e["start"], e["end"])] = e["val"]  # later filings overwrite, so restatements win
        return out
    diluted = periods("WeightedAverageNumberOfDilutedSharesOutstanding")
    basic = periods("WeightedAverageNumberOfSharesOutstandingBasic")
    common = set(diluted) & set(basic)
    if not common:
        return None, None, None
    # Most recent end date; for that date, the shortest period (the quarter) is the most current.
    start, end = max(common, key=lambda p: (p[1], p[0]))
    label = _period_label(start, end)
    difference = max(0.0, diluted[(start, end)] - basic[(start, end)])
    if difference == 0:
        # Often filed only for the year to date (Roblox, Snowflake, Viking),
        # not the quarter: same end date, shortest period available.
        same_end = [(p, val) for p, val in
                    periods("AntidilutiveSecuritiesExcludedFromComputationOfEarningsPerShareAmount").items()
                    if p[1] == end and val]
        if same_end:
            (p_start, _), excluded = max(same_end, key=lambda item: item[0][0])
            return excluded, _period_label(p_start, end), "antidilutive"
    return difference, label, "diluted-basic"


def freshest_instant(facts_json, tag_candidates):
    """freshest_annual_value for a balance-sheet item, upgraded to the
    latest 10-Q value when one is newer — so items pulled outside the
    main financials dict (short-term investments, leases, the equity
    bridge) are dated the same as cash and debt. Same return shape."""
    val, tag, fy, unit = freshest_annual_value(facts_json, tag_candidates)
    if val is None:
        # No annual report yet (a recent IPO): the latest 10-Q balance.
        for candidate in tag_candidates:
            if isinstance(candidate, str):
                q_val, q_end = latest_quarter_instant(facts_json, candidate, "USD", "")
                if q_val is not None:
                    return q_val, candidate, int(q_end[:4]), "USD"
        return val, tag, fy, unit
    if not tag or "+" in tag:
        return val, tag, fy, unit
    periods = _recipe_annual_entries(_all_taxonomy_tags(facts_json), tag)
    if periods:
        q_val, _ = latest_quarter_instant(facts_json, tag, unit, max(periods))
        if q_val is not None:
            val = q_val
    return val, tag, fy, unit


def ttm_from_quarters(facts_json, tag_candidates):
    """Trailing twelve months of a flow figure from 10-Qs alone, for a
    company with no annual report yet: the last four tagged quarters when
    they're back to back, otherwise the latest year-to-date figure
    annualized. Returns (value, label) or (None, None)."""
    for tag in tag_candidates:
        entries = [e for e in _quarterly_entries(facts_json, tag, "USD") if e.get("start")]
        if not entries:
            continue
        quarters = {e["end"]: e["val"] for e in entries if _days(e["start"], e["end"]) < 100}
        ends = sorted(quarters)[-4:]
        if len(ends) == 4 and _days(ends[0], ends[-1]) <= 290:
            return sum(quarters[e] for e in ends), f"four quarters to {ends[-1]}"
        latest_end = max(e["end"] for e in entries)
        ytd = max((e for e in entries if e["end"] == latest_end), key=lambda e: _days(e["start"], e["end"]))
        days = _days(ytd["start"], ytd["end"])
        return ytd["val"] * 365 / days, f"{round(days / 30.4)} months to {latest_end}, annualized"
    return None, None


def year_to_date_growth(facts_json, tag_candidates):
    """Growth of the latest 10-Q year-to-date figure over the same period a
    year earlier — the freshest read on a trend, which a fiscal-year figure
    can miss by up to a year (Exzeo: +62% in FY2025, +4% in 2026's first
    half). Returns (growth, label) or (None, None)."""
    for tag in tag_candidates:
        entries = [e for e in _quarterly_entries(facts_json, tag, "USD") if e.get("start")]
        if not entries:
            continue
        latest_end = max(e["end"] for e in entries)
        current = max((e for e in entries if e["end"] == latest_end), key=lambda e: _days(e["start"], e["end"]))
        length = _days(current["start"], current["end"])
        prior = [e for e in entries if abs(_days(e["end"], latest_end) - 364) <= 8
                 and abs(_days(e["start"], e["end"]) - length) <= 8]
        if prior and prior[-1]["val"] > 0:
            months = round(length / 30.4)
            return current["val"] / prior[-1]["val"] - 1, f"{months} months to {latest_end} vs a year earlier"
    return None, None


def freshest_ttm(facts_json, tag_candidates):
    """freshest_annual_value for a flow figure, upgraded to trailing twelve
    months when a newer 10-Q year-to-date exists. Returns (value, tag,
    fiscal_year, unit, as_of) — as_of is the TTM end date or None."""
    val, tag, fy, unit = freshest_annual_value(facts_json, tag_candidates)
    if val is None or not tag or "+" in tag:
        return val, tag, fy, unit, None
    periods = _recipe_annual_entries(_all_taxonomy_tags(facts_json), tag)
    if periods:
        ttm, as_of = trailing_twelve_months(facts_json, tag, unit, max(periods), val)
        if ttm is not None:
            return ttm, tag, fy, unit, as_of
    return val, tag, fy, unit, None


OPERATING_CASH_FLOW_TAGS = [
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    "CashFlowsFromUsedInOperatingActivities",  # ifrs-full
    "CashFlowsFromUsedInOperatingActivitiesContinuingOperations",  # ifrs-full
]
STOCK_COMPENSATION_TAGS = [
    "ShareBasedCompensation",
    "AllocatedShareBasedCompensationExpense",
    "AdjustmentsForSharebasedPayments",  # ifrs-full
]


def reported_cash_flow(facts_json, capex, fx_currency, fx_rate):
    """
    The company's actual cash generation, from its cash-flow statement:
    operating cash flow - stock-based compensation - capex, trailing twelve
    months where a newer 10-Q exists. Stock compensation is added back in
    operating cash flow but is a real expense (Damodaran), so it comes
    back out. Working capital — e.g. customers paying in advance — is in
    it as reported (Roblox: +$1.8B operating cash flow on an operating loss).
    Returns (cash_flow, description) or (None, None) without operating cash flow.
    """
    ocf, ocf_tag, ocf_fy, ocf_unit, ocf_asof = freshest_ttm(facts_json, OPERATING_CASH_FLOW_TAGS)
    if ocf is None:
        return None, None
    sbc, _, sbc_fy, sbc_unit, _ = freshest_ttm(facts_json, STOCK_COMPENSATION_TAGS)
    if sbc is None or sbc_fy is None or sbc_fy < ocf_fy:
        sbc, sbc_unit = 0.0, None  # not reported for the same year — none subtracted
    def usd(val, unit):
        return val * fx_rate if fx_currency and unit == fx_currency else val
    ocf, sbc = usd(ocf, ocf_unit), usd(sbc, sbc_unit)
    period = f"TTM to {ocf_asof}" if ocf_asof else f"FY{ocf_fy}"
    return ocf - sbc - capex, (f"operating cash flow ${ocf:,.0f} - stock compensation ${sbc:,.0f} "
                               f"- capex ${capex:,.0f} ({period})")


# Which METRIC_TAGS entries are flows (TTM) vs balance-sheet instants
# (latest quarter). Interest expense and R&D stay annual: both are
# re-pulled with their own multi-year logic elsewhere (the synthetic
# rating, R&D capitalization), so updating only the headline figure would
# leave them inconsistent. Dividends stay annual too.
TTM_FLOW_METRICS = {"revenue", "operating_income", "depreciation_amortization", "capex",
                    "income_tax_expense", "pretax_income"}
LATEST_QUARTER_INSTANT_METRICS = {"total_debt_current", "total_debt_noncurrent", "cash",
                                  "shares_outstanding", "book_equity"}


def apply_latest_quarter(facts_json, financials):
    """Update `financials` in place with TTM flows and latest-quarter
    balances wherever a 10-Q is newer than the 10-K. Each updated entry
    gets an "as_of" date and "ttm"/"quarterly" flag for the report;
    fiscal_year is left as the annual one, which the stale-data checks
    compare on. Split-line recipes (summed tags) stay annual."""
    all_tags = _all_taxonomy_tags(facts_json)
    for metric, info in financials.items():
        tag = info.get("tag")
        if info.get("value") is None or not tag or "+" in tag:
            continue
        periods = _recipe_annual_entries(all_tags, tag)
        if not periods:
            continue
        annual_end = max(periods)
        if metric in TTM_FLOW_METRICS:
            ttm, as_of = trailing_twelve_months(facts_json, tag, info["unit"], annual_end, info["value"])
            if ttm is None:
                # The quarters can sit under a different tag than the annual
                # figure (AppLovin: FY2025 "Revenues", 10-Qs under
                # RevenueFromContractWithCustomer...) — left alone, revenue
                # stayed a year old while operating income moved to TTM (a
                # 108% margin). Same fiscal year-end, so the quarters bridge.
                for other in METRIC_TAGS[metric]:
                    if isinstance(other, str) and other != tag and annual_end in _recipe_annual_entries(all_tags, other):
                        ttm, as_of = trailing_twelve_months(facts_json, other, info["unit"], annual_end, info["value"])
                        if ttm is not None:
                            break
            if ttm is not None:
                info.update(value=ttm, as_of=as_of, ttm=True)
        elif metric in LATEST_QUARTER_INSTANT_METRICS:
            val, as_of = latest_quarter_instant(facts_json, tag, info["unit"], annual_end)
            if val is not None:
                info.update(value=val, as_of=as_of, quarterly=True)


def apply_quarterly_only(facts_json, financials):
    """
    For a company that has filed 10-Qs but no annual report yet (a recent
    IPO — Generate Biomedicines listed in February 2026), every annual
    lookup comes back empty. Fill `financials` in place from the 10-Qs:
    balance-sheet items at the latest quarter; flows as the sum of the
    last four tagged quarters when they're there, otherwise the latest
    year-to-date figure ANNUALIZED (an IPO's 10-Qs rarely reach back a
    full year — the rest is in the S-1, which isn't in XBRL). Annualized
    entries are flagged in the report as such.
    """
    for metric, info in financials.items():
        if metric == "dividends_per_share":
            continue
        for recipe in METRIC_TAGS[metric]:
            if not isinstance(recipe, str):
                continue
            units = _all_taxonomy_tags(facts_json).get(recipe, {}).get("units", {})
            unit = "shares" if "shares" in units else ("USD" if "USD" in units else None)
            if unit is None:
                continue
            entries = _quarterly_entries(facts_json, recipe, unit)
            if metric in LATEST_QUARTER_INSTANT_METRICS:
                instants = [e for e in entries if not e.get("start")]
                if not instants:
                    continue
                latest = max(instants, key=lambda e: (e["end"], e.get("filed", "")))
                info.update(value=latest["val"], tag=recipe, unit=unit, fiscal_year=int(latest["end"][:4]),
                            as_of=latest["end"], quarterly=True)
                break
            durations = [e for e in entries if e.get("start")]
            if not durations:
                continue
            quarters = {}
            for e in durations:
                if _days(e["start"], e["end"]) < 100:
                    quarters[e["end"]] = e["val"]
            ends = sorted(quarters)[-4:]
            if len(ends) == 4 and _days(ends[0], ends[-1]) <= 290:  # four back-to-back quarters
                info.update(value=sum(quarters[e] for e in ends), tag=recipe, unit=unit,
                            fiscal_year=int(ends[-1][:4]), as_of=ends[-1], ttm=True)
                break
            latest_end = max(e["end"] for e in durations)
            ytd = max((e for e in durations if e["end"] == latest_end), key=lambda e: _days(e["start"], e["end"]))
            days = _days(ytd["start"], ytd["end"])
            info.update(value=ytd["val"] * 365 / days, tag=recipe, unit=unit, fiscal_year=int(latest_end[:4]),
                        as_of=latest_end, annualized_months=round(days / 30.4))
            break


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
    "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",  # e.g. Air Products ($17.7B, FY2025)
    "DebtAndCapitalLeaseObligations",
    "LongTermDebtNoncurrent",
    "NotesPayable",
    "ConvertibleNotesPayable",
    "SeniorNotes",
    "LongTermNotesAndLoans",  # e.g. Lithium Americas' DOE loan ($988M drawn, June 2026)
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
    val, tag, fy, unit = freshest_instant(facts_json, TOTAL_DEBT_TAGS)  # latest 10-Q, like cash and debt
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


_YAHOO_INFO = {}
# Screens set this: implied volatility is reference-only, and Yahoo's options
# endpoint is the first to rate-limit a batch of hundreds of companies.
SKIP_IMPLIED_VOLATILITY = False


def yahoo_info(ticker):
    """yfinance's info dict for `ticker`, fetched once per run: the price,
    beta and share-count lookups share it, which halves the calls a screen
    makes and keeps it under Yahoo's rate limit. Raises what yfinance raises."""
    if ticker not in _YAHOO_INFO:
        _YAHOO_INFO[ticker] = yf.Ticker(ticker).info
    return _YAHOO_INFO[ticker]


def yahoo_last_close(ticker):
    """The latest close from Yahoo's price history (a different endpoint from
    info, rarely rate-limited), or None."""
    try:
        hist = yf.Ticker(ticker).history(period="5d", auto_adjust=False)["Close"].dropna()
        return float(hist.iloc[-1]) if len(hist) else None
    except Exception:
        return None


def get_market_data(ticker):
    """
    Try to pull beta and current share price, trying each source in turn:
    yfinance (if installed — free, no API key needed) first, then
    Financial Modeling Prep (if a key is set), then manual entry. Each
    tier falls through to the next on any failure (not installed, missing
    key, rate limit, network issue, missing field) — the script always
    produces a result, it never hard-fails just because a market-data
    source had a problem.

    A price with no beta is accepted (beta returned as None): Yahoo has
    no beta for some ADRs (FEMSA), and the beta the valuation uses is
    Damodaran's industry beta anyway — the company's own regression beta
    is only shown for comparison.
    """
    if YFINANCE_AVAILABLE:
        try:
            info = yahoo_info(ticker)
            beta = info.get("beta")
            price = info.get("currentPrice") or info.get("regularMarketPrice")
            if price is not None:
                print(f"  Pulled from yfinance: beta={beta if beta is not None else 'not available'}, "
                      f"share price=${price}")
                return (float(beta) if beta is not None else None), float(price)
            else:
                print("  yfinance response was missing the share price — trying next source.")
        except Exception as e:
            print(f"  yfinance request failed ({e}) — trying next source.")
        close = yahoo_last_close(ticker)
        if close is not None:
            print(f"  Pulled from Yahoo's price history: share price=${close:,.2f} (no beta — the industry beta is used)")
            return None, close

    if FMP_API_KEY:
        url = FMP_PROFILE_URL.format(ticker=ticker.upper(), key=FMP_API_KEY)
        try:
            req = Request(url, headers={"User-Agent": sec_user_agent()})
            with urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data and isinstance(data, list) and len(data) > 0:
                profile = data[0]
                beta = profile.get("beta")
                price = profile.get("price")
                if price is not None:
                    print(f"  Pulled from FMP: beta={beta if beta is not None else 'not available'}, "
                          f"share price=${price}")
                    return (float(beta) if beta is not None else None), float(price)
                else:
                    print("  FMP response was missing the share price — falling back to manual entry.")
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
    beta = prompt_number("  Enter beta (e.g. 1.2), or press Enter to skip (the industry beta is used anyway): ",
                         default="")
    beta = None if beta == "" else beta
    price = prompt_number("  Enter current share price (e.g. 185.50): ")
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
    "CurrentInvestments",  # ifrs-full (FEMSA)
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


def get_metric_value(financials, key, label, stale_metrics=None, return_override_flag=False, default=None,
                     default_reason=None):
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
        if default_reason:
            print(f"  {default_reason}")
        else:
            print(f"  Many smaller companies don't report it separately because it's immaterial —")
            print(f"  ${default:,.0f} is usually right then. Check the 10-K's cash-flow statement if unsure.")
        val = prompt_number(f"  Enter {label} manually, or press Enter for {default:,.0f}: ",
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
    if all(info["value"] is None for info in results.values()):
        apply_quarterly_only(facts, results)  # a recent IPO: 10-Qs but no annual report yet
    else:
        apply_latest_quarter(facts, results)
    return results, facts


def get_fx_rate_to_usd(currency):
    """Look up a live currency->USD rate (ECB-sourced, via frankfurter.dev,
    no API key required). The ECB doesn't publish every currency — not the
    Taiwan dollar, for one — so Yahoo's market rate is tried next, then
    manual entry."""
    url = FX_RATE_URL.format(currency=currency)
    try:
        data = fetch_json_or_none(url)
        if data:
            return data["rates"]["USD"], data.get("date")
    except Exception:
        pass
    if YFINANCE_AVAILABLE:
        try:
            info = yf.Ticker(f"{currency}USD=X").info
            rate = info.get("regularMarketPrice") or info.get("previousClose")
            if rate:
                from datetime import date
                print(f"  (The ECB doesn't publish {currency} — using Yahoo's market rate.)")
                return float(rate), date.today().isoformat()
        except Exception:
            pass
    print(f"  Could not fetch a live {currency}->USD exchange rate.")
    rate = prompt_number(f"  Enter the {currency}->USD exchange rate manually (e.g. 1.16): ")
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
    req = Request(url, headers={"User-Agent": sec_user_agent()})
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
            period = (f"ANNUALIZED from {info['annualized_months']} months to {info['as_of']}"
                      if info.get("annualized_months") else
                      f"TTM to {info['as_of']}" if info.get("ttm") else
                      f"as of {info['as_of']}" if info.get("quarterly") else f"FY{info['fiscal_year']}")
            print(f"  {metric:28s}: {display:>20s}   (tag: {info['tag']}, {period}){flag}")

    if stale:
        print(f"\n  ⚠⚠ WARNING — possibly stale data: {', '.join(stale)}")
        print("     This usually means the company switched XBRL tags and this")
        print("     script's fallback list doesn't include their current one yet.")
        print("     Don't trust these figures without checking the actual 10-K.")
    annualized = [metric for metric, info in financials.items() if info.get("annualized_months")]
    if annualized:
        print(f"\n  ⚠ No annual report yet (a recent IPO?): {', '.join(annualized)} are the latest")
        print("    year-to-date figures annualized from the 10-Qs. A full year is in the S-1 prospectus.")
    if missing:
        print(f"\n  ⚠ Missing metrics: {', '.join(missing)}")
        print("    You'll need to fill these in manually from the 10-K.")
    print()
    return missing, stale


def print_quarterly_annualization_warning(financials):
    """
    Loud, repeated warning for a brand-new-IPO filer with no 10-K/20-F
    yet, where apply_quarterly_only had to extrapolate a partial-year
    10-Q into a full year (e.g. Generate Biomedicines, listed Feb 2026:
    a 6-month YTD figure x 12/6). print_financials_report already notes
    this once, quietly, at the very top of a run — easy to scroll past
    by the time a value-per-share number shows up pages later. This
    prints the same warning again, loudly, immediately after EVERY
    final valuation output in the run, so it's still visible right next
    to the number it undermines. A no-op (prints nothing) once a real
    annual filing exists and apply_latest_quarter's TTM-from-4-quarters
    path is used instead — summing four REPORTED quarters isn't the
    same approximation as extrapolating one partial one.
    """
    affected = {m: info["annualized_months"] for m, info in financials.items() if info.get("annualized_months")}
    if not affected:
        return
    detail = ", ".join(f"{m} ({mo:.0f}mo of data)" for m, mo in affected.items())
    bar = "!" * 64
    print(f"\n  {bar}")
    print("  !!  DATA-QUALITY WARNING: NO FULL FISCAL YEAR EXISTS YET")
    print(f"  !!  {detail}")
    print("  !!  was stretched from a partial-year 10-Q to a full year by simple")
    print("  !!  extrapolation (x 12/n_months) — not a real trailing twelve months.")
    print("  !!  Seasonality, ramp effects, and one-off items in that window are")
    print("  !!  compounded into every number above. Treat this as a rough")
    print("  !!  directional read, not a trustworthy valuation, until a real")
    print("  !!  annual filing (or four consecutive 10-Qs) exists.")
    print(f"  {bar}")


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
DAMODARAN_COUNTRY_URL = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/ctryprem.html"
DAMODARAN_RATINGS_URL = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/ratings.html"
DAMODARAN_MARGIN_URL = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/margin.html"
DAMODARAN_WC_URL = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/wcdata.html"
DAMODARAN_ROIC_URL = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/roc.html"
DAMODARAN_ROE_URL = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/roe.html"
DAMODARAN_CAPEX_URL = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/capex.html"
COUNTRY_MAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "country_map.json")
EDGAR_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DAMODARAN_CACHE_PATH = os.path.join(DATA_DIR, "damodaran_cache.json")
INDUSTRY_MAP_PATH = os.path.join(DATA_DIR, "industry_map.json")
DAMODARAN_CACHE_MAX_AGE_DAYS = 7
DAMODARAN_CACHE_VERSION = 2  # bump when the parsed shape changes, so old caches refresh


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


def parse_damodaran_countries(page):
    """
    Parse Damodaran's country risk table (ctryprem.html, his January
    update): {"updated": "January 5, 2026", "countries": {name: {"crp",
    "erp", "tax", "rating"}}}, or None. Columns are found by header name.
    Annotations like "Turkey (updated February 2026)" are trimmed to the
    country name. (He also posts April/July spreadsheet updates; this
    reads the January web table.)
    """
    rows = _html_table_rows(page)
    header = next((r for r in rows if r and r[0] == "Country"), None)
    if header is None:
        return None
    try:
        i_rating = header.index("Moody's rating")
        i_crp, i_erp = header.index("Country Risk Premium"), header.index("Equity Risk Premium")
        i_tax = header.index("Corporate Tax Rate")
    except ValueError:
        return None
    countries = {}
    for r in rows:
        if len(r) <= max(i_crp, i_erp, i_tax) or r[0] in ("", "Country"):
            continue
        name = re.sub(r"\s*\(updated [^)]*\)", "", r[0]).strip()
        try:
            countries[name] = {"crp": float(r[i_crp].rstrip("%")) / 100, "erp": float(r[i_erp].rstrip("%")) / 100,
                               "tax": float(r[i_tax].rstrip("%")) / 100 if r[i_tax] not in ("", "NA") else None,
                               "rating": r[i_rating]}
        except ValueError:
            continue
    updated = re.search(r"(?i)updated:\s*([A-Za-z]+ \d{1,2}, \d{4})", page)
    return ({"updated": updated.group(1) if updated else "unknown date", "countries": countries}
            if "United States" in countries else None)


def parse_damodaran_spreads(page):
    """
    Damodaran's current default spread for each bond rating, from his
    ratings page (large non-financial firms' table, January 2026 at the
    time of writing): {"as_of": "January 2026", "spreads": {"BBB": 0.0111,
    ...}}, or None. His labels ("Baa2/BBB") are reduced to the S&P part
    the rating tables in model.py use. Only the spreads are taken — the
    coverage breakpoints stay model.py's (the large-firm ones match his
    exactly; the small-firm ones aren't on this page).
    """
    spreads = {}
    for r in _html_table_rows(page):
        if len(r) >= 4 and "/" in r[2] and r[3].endswith("%"):
            try:
                spreads.setdefault(r[2].split("/")[-1].strip(), float(r[3].rstrip("%")) / 100)
            except ValueError:
                continue
    as_of = re.search(r"(?i)as of ([A-Za-z]+ \d{4})", page)
    return ({"as_of": as_of.group(1) if as_of else "unknown date", "spreads": spreads}
            if {"AAA", "BBB", "D"} <= set(spreads) else None)


def _parse_industry_column(page, column, percent=True):
    """{industry: value} for one column of a Damodaran industry table,
    found by header name; with the page's "Updated in ..." date. None if
    the column isn't there. percent=False for a plain ratio (e.g. 2.80)."""
    rows = _html_table_rows(page)
    header = next((r for r in rows if r and r[0] == "Industry Name"), None)
    if header is None or column not in header:
        return None
    i = header.index(column)
    values = {}
    for r in rows:
        if len(r) > i and r[0] not in ("", "Industry Name"):
            try:
                values[r[0]] = float(r[i].rstrip("%")) / (100 if percent else 1)
            except ValueError:
                continue
    updated = re.search(r"(?i)Updated in ([A-Za-z]+ \d{4})", page)
    return {"updated": updated.group(1) if updated else "January 2026 dataset",
            "values": values} if values else None


def parse_damodaran_margins(page):
    """Industry pre-tax operating margin, lease- and R&D-adjusted — the
    same adjustments this tool makes to EBIT, so a company's margin and
    the industry's compare like for like."""
    return _parse_industry_column(page, "Pre-tax Lease & R&D adj Margin")


def parse_damodaran_roic(page):
    """Industry after-tax return on invested capital, lease- and R&D-
    adjusted (same basis as this tool's invested capital)."""
    return _parse_industry_column(page, "Lease & R&D adjusted after-tax ROIC")


def parse_damodaran_roe(page):
    """Industry return on equity (net income / book equity), unadjusted —
    the stable-ROE anchor for banks and brokerages (--financial)."""
    return _parse_industry_column(page, "ROE (unadjusted)")


def parse_damodaran_sales_to_capital(page):
    """Industry sales / invested capital (trailing 12 months) — the
    narrative DCF's default reinvestment ratio. Invested capital here is
    book debt + equity - cash, goodwill included, R&D not capitalized."""
    return _parse_industry_column(page, "Sales/ Invested Capital (LTM)", percent=False)


def parse_damodaran_working_capital(page):
    """Industry non-cash working capital as a share of revenue
    (receivables + inventory - payables, cash and debt excluded)."""
    return _parse_industry_column(page, "Non-cash WC/ Sales")


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
            return ((now - fetched).days < DAMODARAN_CACHE_MAX_AGE_DAYS
                    and cache[piece].get("version") == DAMODARAN_CACHE_VERSION)
        except (KeyError, ValueError, TypeError):
            return False

    changed = False
    for piece, url, parser in (("betas", DAMODARAN_BETAS_URL, parse_damodaran_betas),
                               ("erp", DAMODARAN_HOME_URL, parse_damodaran_erp),
                               ("countries", DAMODARAN_COUNTRY_URL, parse_damodaran_countries),
                               ("ratings", DAMODARAN_RATINGS_URL, parse_damodaran_spreads),
                               ("margins", DAMODARAN_MARGIN_URL, parse_damodaran_margins),
                               ("roic", DAMODARAN_ROIC_URL, parse_damodaran_roic),
                               ("wc", DAMODARAN_WC_URL, parse_damodaran_working_capital),
                               ("roe", DAMODARAN_ROE_URL, parse_damodaran_roe),
                               ("s2c", DAMODARAN_CAPEX_URL, parse_damodaran_sales_to_capital)):
        if is_fresh(piece):
            continue
        page = fetch_text_or_none(url)
        parsed = parser(page) if page else None
        if parsed:
            cache[piece] = {"fetched": now.isoformat(), "data": parsed, "version": DAMODARAN_CACHE_VERSION}
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
    return {piece: cache[piece]["data"] if piece in cache else None
            for piece in ("betas", "erp", "countries", "ratings", "margins", "roic", "wc", "roe", "s2c")}


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
    (2082, 2085, "Beverage (Alcoholic)"),  # brewers, wineries, distillers
    (2080, 2087, "Beverage (Soft)"),  # 2080 "Beverages" is Coca-Cola and PepsiCo; 2086-2087 soft drinks

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
    (3559, 3559, "Semiconductor Equip"),  # "special industry machinery": Lam Research, ASML
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


# ---------------------------------------------------------------------
# ADRs: a foreign company's filings count its ORDINARY shares, but the US
# price is per American Depositary Share, which can stand for several of
# them (Alibaba: 1 ADS = 8 ordinary shares; TSMC 5; BP 6; JD 2). Mixing
# the two made value per share come out ratio-times too low and market
# cap ratio-times too high — which also corrupted D/E, the relevered beta
# and the WACC weights. The ratio isn't an XBRL tag, so it's read off the
# latest 20-F's cover page ("each ADS representing eight ordinary
# shares"), cross-checked against the ratio implied by the market's own
# ADS count (filed ordinary shares / Yahoo's shares), confirmed, and
# saved per ticker in data/adr_ratios.json. Everything per-share is then
# done in ADS units.
# ---------------------------------------------------------------------

ADR_RATIO_PATH = os.path.join(DATA_DIR, "adr_ratios.json")
FOREIGN_ANNUAL_FORMS = ("20-F", "40-F")
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                 "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15, "twenty": 20}
# The keyword is a whole word ("this leads to a final dividend of €2.70 per
# ordinary share" read as 2.7 ordinary shares per ADS for ASML), and a
# number right after a currency sign is a price, not a share count.
_ADR_RATIO_PATTERN = re.compile(
    r"(?i)\b(?:ADSs?|American Depositary Shares?|depositary shares?)\b[^.;]{0,140}?(?<![€$£¥])(?<![€$£¥] )\b"
    r"(one|two|three|four|five|six|seven|eight|nine|ten|twelve|fifteen|twenty|\d+(?:\.\d+)?)"
    r"\s*(?:\(\d+(?:\.\d+)?\)\s*)?(?:[\w\-]+\s+){0,3}?(?:ordinary|common)\s+shares?")


def latest_annual_filing(cik):
    """(form, document URL) of the most recent 10-K / 20-F / 40-F, from
    EDGAR's submissions index, or (None, None)."""
    data = fetch_json_or_none(EDGAR_SUBMISSIONS_URL.format(cik=cik))
    try:
        recent = data["filings"]["recent"]
        i = next(i for i, f in enumerate(recent["form"]) if f in ("10-K",) + FOREIGN_ANNUAL_FORMS)
        url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
               f"{recent['accessionNumber'][i].replace('-', '')}/{recent['primaryDocument'][i]}")
        return recent["form"][i], url
    except (TypeError, KeyError, StopIteration):
        return None, None


def warn_if_edgar_data_lags(cik, financials):
    """
    Warn when the company has filed a newer annual report than the one
    the numbers came from. SEC's companyfacts (where every figure here
    comes from) can lag a filing by months — FEMSA's and TSMC's FY2025
    20-Fs, filed April 2026, were still missing from it in October — and
    since every figure is then equally old, the per-metric staleness
    check can't see it. Compares the latest 10-K/20-F/40-F's period in
    EDGAR's filing index with the statements' fiscal year. Returns True
    if it warned.
    """
    data = fetch_json_or_none(EDGAR_SUBMISSIONS_URL.format(cik=cik))
    try:
        recent = data["filings"]["recent"]
        i = next(i for i, f in enumerate(recent["form"]) if f in ("10-K",) + FOREIGN_ANNUAL_FORMS)
        form, report_date, filed = recent["form"][i], recent["reportDate"][i], recent["filingDate"][i]
    except (TypeError, KeyError, StopIteration):
        return False
    years = [info["fiscal_year"] for info in financials.values()
             if info["fiscal_year"] is not None and info.get("tag") != "EntityCommonStockSharesOutstanding"]
    filing_fy = _fiscal_year_of_period({"end": report_date})
    if not years or not report_date or filing_fy is None or filing_fy <= max(years):
        return False
    print(f"  ⚠⚠ WARNING — EDGAR's data is behind the company's filings: the latest {form}")
    print(f"     (FY{filing_fy}, period ending {report_date}, filed {filed}) isn't in SEC's XBRL data yet,")
    print(f"     so these figures are from FY{max(years)}. Check the key figures against the new {form}")
    print("     and type corrections where asked below, or treat this valuation as a year old.")
    return True


def adr_ratio_candidates(filing_url):
    """Every 'ADS ... represents N ordinary/common shares' number in the
    filing, as {ratio: times mentioned}. Large filings (BP's 20-F is
    ~30 MB) take a few seconds, which is why the answer gets saved."""
    try:
        with urlopen(Request(filing_url, headers={"User-Agent": sec_user_agent()}), timeout=90) as resp:
            page = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return {}
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page)).split())
    counts = {}
    for m in _ADR_RATIO_PATTERN.finditer(text):
        word = m.group(1).lower()
        try:
            value = float(_NUMBER_WORDS.get(word, word))
        except ValueError:
            continue
        if 0 < value <= 100:
            counts[value] = counts.get(value, 0) + 1
    return counts


def get_market_shares(ticker):
    """The market's own share count for this ticker — for an ADR, in ADS
    units — from yfinance (implied shares cover every share class) or
    FMP (market cap / price). None if neither is available."""
    if YFINANCE_AVAILABLE:
        try:
            info = yahoo_info(ticker)
            shares = info.get("impliedSharesOutstanding") or info.get("sharesOutstanding")
            if shares:
                return float(shares)
        except Exception:
            pass
    if FMP_API_KEY:
        try:
            with urlopen(Request(FMP_PROFILE_URL.format(ticker=ticker.upper(), key=FMP_API_KEY),
                                 headers={"User-Agent": sec_user_agent()}), timeout=20) as resp:
                profile = json.loads(resp.read().decode("utf-8"))[0]
            if profile.get("mktCap") and profile.get("price"):
                return float(profile["mktCap"]) / float(profile["price"])
        except Exception:
            pass
    return None


def resolve_adr_ratio(ticker, cik, filed_shares):
    """
    Ordinary shares per ADS for a foreign (20-F/40-F) filer, 1.0 for
    everyone else. Returns (ratio, market_shares, counts_disagree).
    Prefers a saved answer; otherwise the cover page's most-mentioned
    figure (word and digit forms counted together — Baidu says "eight"
    and "8" eleven times, "1"/"one" nine), falling back to the
    market-implied ratio only when the cover page has no ratio sentence
    at all. The market-implied ratio is a CROSS-CHECK, not an override:
    when it disagrees, the usual culprit is the filed share count, not
    the ratio — Baidu tags only one share class, so filed / market came
    out 0.10 — and counts_disagree tells the caller to default the share
    count to the market's ADS count x ratio instead.
    """
    form, filing_url = latest_annual_filing(cik)
    if form not in FOREIGN_ANNUAL_FORMS:
        return 1.0, None, False
    market_shares = get_market_shares(ticker)
    implied = filed_shares / market_shares if filed_shares and market_shares else None
    try:
        with open(ADR_RATIO_PATH) as f:
            saved = json.load(f).get(ticker.upper())
    except (OSError, ValueError):
        saved = None

    print(f"\n{'='*60}")
    print(f"  ADR ratio (foreign filer — files a {form})")
    print(f"{'='*60}")
    if saved:
        ratio, source = float(saved), "saved choice"
    else:
        print("  Reading the ADS ratio off the annual report's cover page (one-time, then saved)...")
        candidates = adr_ratio_candidates(filing_url) if filing_url else {}
        if candidates:
            # Most mentions wins; closeness to the market-implied ratio only breaks ties.
            ratio = max(candidates, key=lambda c: (candidates[c], -abs(c - implied) if implied else 0))
            source = f"cover page ({candidates[ratio]} mention(s))"
        elif implied:
            nearest = round(implied)
            ratio = float(nearest) if nearest and abs(implied - nearest) / nearest < 0.05 else round(implied, 2)
            source = "market-implied (no ratio sentence found)"
        else:
            ratio, source = 1.0, "default (no ratio found)"
    print(f"  Ordinary shares per ADS: {ratio:g}  ({source})")
    if implied:
        print(f"  Cross-check: filed ordinary shares / market ADS count = {implied:.2f}")
    ratio = prompt_number(f"  Press Enter to use {ratio:g}, or type the ratio (ordinary shares per ADS): ",
                          default=ratio)
    if ratio <= 0:
        print("  The ratio has to be positive — using 1.")
        ratio = 1.0
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        try:
            with open(ADR_RATIO_PATH) as f:
                all_saved = json.load(f)
        except (OSError, ValueError):
            all_saved = {}
        all_saved[ticker.upper()] = ratio
        with open(ADR_RATIO_PATH, "w") as f:
            json.dump(all_saved, f, indent=2, sort_keys=True)
    except OSError:
        pass
    counts_disagree = bool(implied) and abs(implied - ratio) / ratio > 0.15
    if counts_disagree:
        print(f"  ⚠ The filed share count implies {implied:.2f}, not {ratio:g} — usually the filing")
        print("    counts only one share class, or the count is out of date. The share count")
        print("    below defaults to the market's ADS count x the ratio instead.")
    return ratio, market_shares, counts_disagree


# ---------------------------------------------------------------------
# COUNTRY RISK (Damodaran's operations-based ERP): a company earning its
# money in a riskier country carries that country's risk whatever its
# domicile. His country table gives each country a premium over a mature
# market (CRP); the company's premium is the revenue-weighted average of
# its countries' CRPs. Today's implied ERP is measured on the S&P 500,
# which already includes the US's own CRP (0.23% since the Aa1
# downgrade), so the adjusted ERP is
#     implied US ERP + (weighted CRP - US CRP)
# — a 100%-US company gets the implied ERP unchanged.
# ---------------------------------------------------------------------

def edgar_domicile(cik):
    """The company's country per EDGAR (business address, else country of
    incorporation), as the name EDGAR uses, or "United States" for a US
    address. None if EDGAR doesn't say."""
    data = fetch_json_or_none(EDGAR_SUBMISSIONS_URL.format(cik=cik)) or {}
    business = (data.get("addresses") or {}).get("business") or {}
    for code, description, foreign in (
            (business.get("stateOrCountry"), business.get("stateOrCountryDescription"),
             business.get("isForeignLocation")),
            (data.get("stateOfIncorporation"), data.get("stateOfIncorporationDescription"), None)):
        if not code or not code.strip():
            continue
        if foreign != 1 and len(code) == 2 and code.isalpha() and code.isupper() and description == code:
            return "United States"  # US states come back as their own two-letter code
        if description and description.strip():
            return description.strip()
    return None


def pick_one_country(countries, prompt):
    """Search Damodaran's country list by name; returns the exact name, or
    None if the user presses Enter to stop."""
    while True:
        query = input(prompt).strip().lower()
        if not query:
            return None
        exact = [n for n in countries if n.lower() == query]
        matches = exact or [n for n in countries if query in n.lower()]
        if not matches:
            print("    No country matches that — try part of the name (e.g. china, kingdom).")
            continue
        if len(matches) == 1:
            return matches[0]
        for i, name in enumerate(matches, 1):
            print(f"    {i:>2}. {name:<34} CRP {countries[name]['crp']:.2%}")
        pick = input("    Number (Enter to search again): ").strip()
        if pick.isdigit() and 1 <= int(pick) <= len(matches):
            return matches[int(pick) - 1]


def choose_country_mix(ticker, domicile, countries):
    """
    Revenue-weighted country mix for this company, [[country, weight], ...].
    Offers the saved mix, else 100% the EDGAR domicile (a proxy, flagged as
    such); 'mix' enters revenue shares by country from the filing's
    geographic revenue note — Damodaran's preferred basis. An explicit
    choice is saved per ticker in data/country_map.json; just accepting
    the domicile proxy isn't, so a later run doesn't present the proxy as
    a saved decision.
    """
    try:
        with open(COUNTRY_MAP_PATH) as f:
            saved_all = json.load(f)
    except (OSError, ValueError):
        saved_all = {}
    saved = saved_all.get(ticker.upper())
    if saved and all(n in countries for n, _ in saved):
        default, label = saved, "Saved mix"
    elif domicile in countries:
        default, label = [[domicile, 1.0]], "Default (domicile — a proxy for where revenue is earned)"
    else:
        default, label = [["United States", 1.0]], (f"Default (EDGAR domicile '{domicile}' not in the table)"
                                                   if domicile else "Default (no domicile on EDGAR)")
    print(f"  {label}: " + " + ".join(f"{n} {w:.0%}" for n, w in default))
    answer = input("  Press Enter to use it, type a country name, or 'mix' to enter revenue shares\n"
                   "  by country (from the filing's geographic revenue note): ").strip()
    if not answer:
        chosen = default
    elif answer.lower() == "mix":
        print("  Add each country with its share of revenue. For a region the filing only reports")
        print("  in aggregate (e.g. \"Europe\"), pick a representative country — itself a rough proxy.")
        chosen = []
        while True:
            name = pick_one_country(countries, "    Country (Enter when done): ")
            if name is None:
                break
            share = prompt_number(f"    Share of revenue for {name} (e.g. 40 for 40%): ")
            if share > 0:
                chosen.append([name, share / 100])
        chosen = chosen or default
    else:
        exact = [n for n in countries if n.lower() == answer.lower()]
        matches = exact or [n for n in countries if answer.lower() in n.lower()]
        name = matches[0] if len(matches) == 1 else pick_one_country(countries, "  Search country (Enter to keep the default): ")
        chosen = [[name, 1.0]] if name else default
    if not answer and not saved:
        return chosen  # accepting the domicile proxy isn't a choice worth remembering as one
    saved_all[ticker.upper()] = chosen
    try:
        os.makedirs(os.path.dirname(COUNTRY_MAP_PATH), exist_ok=True)
        with open(COUNTRY_MAP_PATH, "w") as f:
            json.dump(saved_all, f, indent=2, sort_keys=True)
    except OSError:
        pass
    return chosen


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


def industry_average(damodaran, piece, mix):
    """Revenue-weighted industry figure (margin or ROIC) for the chosen
    industry mix, with the dataset date; (None, None) if unavailable."""
    data = (damodaran or {}).get(piece)
    if not data or not mix or not all(name in data["values"] for name, _ in mix):
        return None, None
    return model.weighted_unlevered_beta([(data["values"][n], w) for n, w in mix]), data["updated"]


# Two real companies per Damodaran industry group with a GENUINELY
# sustained (multi-decade, not just this year) above-industry margin or
# ROIC, and the actual mechanism behind it — printed next to the "needs
# a lasting edge" / "needs a durable competitive advantage" sanity-check
# warnings below so that flag is a concrete comparison to argue against
# ("is this company's edge really like Costco's?") instead of a number
# to glance past. Deliberately NOT exhaustive across all ~94 Damodaran
# groups: many (commodity/cyclical industries especially — Steel, Coal,
# Shipbuilding, Trucking) don't have a real sustained-moat exemplar at
# all, and guessing one just to fill the table would be worse than
# leaving it blank. Covers the industries actually run this session
# plus other unambiguous, widely-covered cases; add more as they come
# up rather than padding this out preemptively. No bank/money-center
# entry on purpose: exemplar_financial_snapshot's EBIT fallback (pretax
# income + interest expense, for filers with no OperatingIncomeLoss tag)
# assumes interest expense is a financing cost to add back, which is
# backwards for a bank or custody bank — interest expense IS their cost
# of revenue, so the rebuild returns a meaningless 90%+ "margin" (caught
# live on JPM, BAC, State Street). Banks don't have an operating-margin
# concept this framework can represent at all; that's what --financial
# mode's excess-return-on-equity model is for instead. Tickers only — the
# actual margin/ROIC is pulled fresh from EDGAR each run (see
# exemplar_financial_snapshot), not hardcoded, so it can't go stale.
INDUSTRY_EXEMPLARS = {
    "Semiconductor": [
        ("TSM", "foundry process-node scale rivals can't match"),
        ("NVDA", "CUDA software ecosystem lock-in"),
    ],
    "Semiconductor Equip": [
        ("ASML", "sole-source EUV lithography monopoly"),
        ("KLAC", "process-control tooling entrenched in fab workflows"),
    ],
    "Software (System & Application)": [
        ("MSFT", "enterprise switching costs across the Office/Azure stack"),
        ("ADBE", "Creative/Document Cloud subscription lock-in"),
    ],
    "Software (Internet)": [
        ("GOOGL", "search distribution + data network effects"),
        ("META", "social-graph network effects"),
    ],
    "Restaurant/Dining": [
        ("MCD", "owned real estate + franchise royalty model"),
        ("CMG", "vertical integration + kitchen throughput"),
    ],
    "Retail (General)": [
        ("COST", "membership-fee float + bulk buying power"),
        ("WMT", "logistics density + scale buying power"),
    ],
    "Insurance (General)": [
        ("CB", "specialty underwriting discipline"),
        ("MKL", "niche underwriting + investment compounding"),
    ],
    "Insurance (Prop/Cas.)": [
        ("PGR", "telematics data + pricing-segmentation edge"),
        ("TRV", "underwriting discipline at scale"),
    ],
    "Drugs (Biotechnology)": [
        ("REGN", "VelocImmune antibody-discovery platform"),
        ("VRTX", "first-in-class franchise + regulatory exclusivity"),
    ],
    "Drugs (Pharmaceutical)": [
        ("LLY", "GLP-1 franchise + patent exclusivity"),
        ("JNJ", "diversified pipeline + commercial scale"),
    ],
    "R.E.I.T.": [
        ("PSA", "self-storage density in supply-constrained markets"),
        ("PLD", "logistics real estate scale + tenant switching costs"),
    ],
    "Retail (REITs)": [
        ("SPG", "dominant mall portfolio, hard to replicate"),
        ("O", "triple-net scale + tenant diversification"),
    ],
    "Beverage (Soft)": [
        ("KO", "global bottling network + brand"),
        ("PEP", "snack + beverage portfolio distribution scale"),
    ],
    "Household Products": [
        ("PG", "brand portfolio + retail shelf-space scale"),
        ("CL", "global distribution + brand"),
    ],
    "Apparel": [
        ("NKE", "brand + global distribution"),
        ("LULU", "vertical premium-brand model"),
    ],
    "Hotel/Gaming": [
        ("MAR", "asset-light franchise + loyalty-program scale"),
        ("LVS", "integrated-resort license scarcity"),
    ],
    "Transportation (Railroads)": [
        ("UNP", "duopoly rail network, right-of-way can't be replicated"),
        ("NSC", "same — regional rail duopoly"),
    ],
    "Aerospace/Defense": [
        ("LMT", "sole-source, decades-long weapons-program contracts"),
        ("TDG", "sole-source aftermarket parts monopoly pricing"),
    ],
    "Information Services": [
        ("SPGI", "ratings duopoly + index licensing"),
        ("MCO", "ratings duopoly"),
    ],
    "Investments & Asset Management": [
        ("BLK", "iShares ETF scale + distribution"),
        ("TROW", "no-distribution-cost direct-sold fund scale"),
    ],
    "Financial Svcs. (Non-bank & Insurance)": [
        ("V", "two-sided payment-network effects"),
        ("MA", "two-sided payment-network effects"),
    ],
    "Healthcare Products": [
        ("ISRG", "installed-base + instrument/service razor-razorblade lock-in"),
        ("SYK", "surgeon training + relationship lock-in"),
    ],
    "Business & Consumer Services": [
        ("ADP", "payroll-switching-cost lock-in"),
        ("EFX", "credit-bureau data oligopoly"),
    ],
}


def exemplar_financial_snapshot(ticker):
    """
    Best-effort current operating margin for a named moat exemplar (see
    INDUSTRY_EXEMPLARS) — pulled fresh from EDGAR the same way as the
    main run, but silently returns None on ANY failure (ticker not
    found, EDGAR unreachable, no usable data). This is a reference
    footnote, not a required input, so unlike get_metric_value it never
    prompts for manual entry — an exemplar with a hole in its data just
    doesn't print that line.

    Two things borrowed from the main pipeline, not simplified away:
    - Falls back to pretax income + interest expense when EBIT isn't
      tagged (same as run_full_valuation's own rebuild) — several
      exemplars (banks, insurers, retailers that go straight to pretax
      income on their income statement) have no OperatingIncomeLoss tag
      at all, which without this fallback read as a lookup failure for
      8 of the first 23 industries tried, not as the moat check for
      their actual business.
    - Requires revenue and EBIT to resolve to the SAME (or adjacent)
      fiscal year. Pulling each independently otherwise risks a stale
      tag on one side (e.g. a filer that stopped tagging
      OperatingIncomeLoss years ago but still tags revenue under the
      current ASC 606 tag) silently dividing a 2014 EBIT by a 2025
      revenue — caught live on KLAC, Travelers, and J&J before this
      check was added.
    """
    try:
        data = fetch_json_or_none(EDGAR_TICKER_MAP_URL)
        if not data:
            return None
        cik = next((str(e["cik_str"]).zfill(10) for e in data.values()
                   if e["ticker"].upper() == ticker.upper()), None)
        if cik is None:
            return None
        facts = fetch_json_or_none(EDGAR_FACTS_URL.format(cik=cik))
        if not facts:
            return None
        revenue, _, rev_fy, rev_unit = get_latest_annual_value(facts, METRIC_TAGS["revenue"])
        ebit, _, ebit_fy, ebit_unit = get_latest_annual_value(facts, METRIC_TAGS["operating_income"])
        # Missing OR stale (some filers tagged OperatingIncomeLoss years
        # ago, then stopped — KLAC, Travelers, J&J, Public Storage all
        # resolved a real-looking but decade-old EBIT this way) — try
        # the pretax+interest rebuild either way, and only use it if
        # IT'S actually fresh.
        if ebit is None or ebit_fy is None or not revenue or abs(rev_fy - ebit_fy) > 1:
            rebuilt = pretax_plus_interest_series(facts, years=1)
            if rebuilt and abs(rev_fy - rebuilt[-1]["fiscal_year"]) <= 1:
                latest = rebuilt[-1]
                ebit, ebit_fy, ebit_unit = latest["val"], latest["fiscal_year"], latest["unit"]
        if not revenue or ebit is None or rev_unit != ebit_unit or abs(rev_fy - ebit_fy) > 1:
            return None
        return {"margin": ebit / revenue, "fiscal_year": max(rev_fy, ebit_fy)}
    except Exception:
        return None


def print_moat_exemplars(mix):
    """
    Printed only when the margin or ROIC sanity check above actually
    fired — real companies in the SAME Damodaran industry group with a
    genuinely sustained (not just-this-year) above-industry edge, and
    the mechanism behind it, so that warning is something concrete to
    argue against rather than a number to glance past. A no-op for any
    industry not yet in INDUSTRY_EXEMPLARS (most of them — see that
    dict's docstring for why this is deliberately partial), or if both
    of an industry's exemplar lookups fail.
    """
    for name, _ in mix:
        exemplars = INDUSTRY_EXEMPLARS.get(name)
        if not exemplars:
            continue
        lines = []
        for ticker, mechanism in exemplars:
            snap = exemplar_financial_snapshot(ticker)
            if snap is None:
                continue
            lines.append(f"    {ticker}: {snap['margin']:.1%} operating margin "
                        f"(FY{snap['fiscal_year']}) — {mechanism}")
        if lines:
            print(f"  Real-world moat check ({name}) — is this story really built like one of these?")
            for line in lines:
                print(line)


def print_terminal_share(breakdown, years):
    """Sanity check shared by both DCFs: how much of the value is the
    terminal value. Returns the share (None when EV isn't positive)."""
    ev = breakdown["ev"]
    if ev <= 0:
        print("  Terminal value share: n/a (enterprise value isn't positive)")
        return None
    share = breakdown["pv_terminal"] / ev
    print(f"  Terminal value share: {share:.0%} of the base value comes from after year {years}"
          + ("  ⚠ the value rests almost entirely on the long run" if share > 0.85 else ""))
    if breakdown["pv_explicit"] < 0:
        print("    (the forecast years are a net cost — the business consumes cash before it pays off)")
    return share


# --financial: what the excess return model reads. Net income to COMMON
# (after preferred dividends), common equity (total less preferred), and
# cash returned to common holders (dividends + buybacks).
COMMON_NET_INCOME_TAGS = ["NetIncomeLossAvailableToCommonStockholdersBasic"]
# Carrying value first: some banks tag PreferredStockValue at par alone
# (Capital One: $0 on $0.01 par) and put the real $5.4B in the APIC tag.
PREFERRED_EQUITY_TAGS = ["PreferredStockIncludingAdditionalPaidInCapitalNetOfDiscount",
                         "PreferredStockIncludingAdditionalPaidInCapital",
                         "PreferredStockValue", "PreferredStockValueOutstanding"]
PREFERRED_DIVIDEND_TAGS = ["DividendsPreferredStockCash", "DividendsPreferredStock",
                           "PreferredStockDividendsIncomeStatementImpact"]
COMMON_DIVIDEND_TAGS = ["PaymentsOfDividendsCommonStock", "DividendsCommonStockCash", "DividendsCommonStock"]
BUYBACK_TAGS = ["PaymentsForRepurchaseOfCommonStock"]


def run_financial_firm_valuation(summary, facts, fx_currency, fx_rate, financials, book_equity, shares_out,
                                 share_price, coe, damodaran, mix, risk_free_rate, beta_source, erp_source):
    """
    --financial: Damodaran's excess return model for a bank or brokerage,
    in place of the FCFF DCF (see model.excess_return_equity_value for why
    the firm-level model breaks for these). Reads ROE and payout history
    from the 10-Ks, anchors stable ROE on Damodaran's industry ROE, prints
    the build and a low/base/high range, and records them in `summary`.
    """
    def by_year(tags, years=7):
        return {e["fiscal_year"]: e["val"] * (fx_rate if fx_currency and e["unit"] == fx_currency else 1)
                for e in get_annual_series(facts, tags, years)}

    print(f"\n{'='*60}")
    print("  Excess return model (financial service firm, Damodaran)")
    print(f"{'='*60}")
    print("  A bank's or broker's debt is raw material, not financing, so it's valued at the")
    print("  equity level: book equity + PV of (ROE - cost of equity) x book equity.")

    # Some banks tag equity only with noncontrolling interest included (Park
    # National); IFRS banks use EquityAttributableToOwnersOfParent.
    equity_hist = by_year(["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
                           "EquityAttributableToOwnersOfParent"])
    preferred_hist = by_year(PREFERRED_EQUITY_TAGS)
    ni_common = by_year(COMMON_NET_INCOME_TAGS)
    ni_total, pref_divs = (by_year(["NetIncomeLoss", "ProfitLossAttributableToOwnersOfParent"]),
                           by_year(PREFERRED_DIVIDEND_TAGS))
    for fy, ni in ni_total.items():
        if fy not in ni_common:
            ni_common[fy] = ni - pref_divs.get(fy, 0.0)
    dividends, buybacks = by_year(COMMON_DIVIDEND_TAGS), by_year(BUYBACK_TAGS)
    common_equity = {fy: eq - preferred_hist.get(fy, 0.0) for fy, eq in equity_hist.items()}

    rows = []
    for fy in sorted(ni_common):
        if fy - 1 in common_equity and common_equity[fy - 1] > 0:
            returned = dividends.get(fy, 0.0) + buybacks.get(fy, 0.0)
            rows.append((fy, ni_common[fy], common_equity[fy - 1], returned))
    rows = rows[-5:]
    if not rows:
        print("  ⚠ Not enough net income / equity history on EDGAR for ROE — type the inputs below.")
    else:
        print(f"\n  {'FY':<7}{'NI to common':>16}{'Begin common eq.':>19}{'ROE':>8}"
              f"{'Div + buybacks':>17}{'Payout':>9}")
        for fy, ni, eq, returned in rows:
            print(f"  FY{fy:<5}{ni:>16,.0f}{eq:>19,.0f}{ni / eq:>8.1%}{returned:>17,.0f}"
                  + (f"{returned / ni:>9.0%}" if ni > 0 else f"{'n/a':>9}"))
        print("  (ROE = net income to common / common equity at the start of the year;")
        print("  payout = dividends + buybacks to common, since buybacks shrink book equity too.)")

    pref_now, _pref_tag, pref_fy, pref_unit = freshest_instant(facts, PREFERRED_EQUITY_TAGS)
    latest_equity_fy = max(equity_hist) if equity_hist else None
    if pref_now is None or (latest_equity_fy and pref_fy is not None and pref_fy < latest_equity_fy):
        pref_now = 0.0  # none outstanding, or a stale tag for preferred since redeemed
    elif fx_currency and pref_unit == fx_currency:
        pref_now *= fx_rate
    common_bv = book_equity - pref_now
    print(f"\n  Common book equity: ${book_equity:,.0f} total - ${pref_now:,.0f} preferred = ${common_bv:,.0f}")

    latest_roe = rows[-1][1] / rows[-1][2] if rows else None
    avg_roe = sum(ni / eq for _, ni, eq, _ in rows) / len(rows) if rows else None
    if rows:
        print(f"  ROE: latest FY{rows[-1][0]} {latest_roe:.1%}, {len(rows)}-yr average {avg_roe:.1%}")
    elif common_bv > 0:
        # No annual history yet (a recent listing): trailing net income from
        # the 10-Qs over today's common equity.
        ttm_ni, label = ttm_from_quarters(facts, COMMON_NET_INCOME_TAGS + ["NetIncomeLoss"])
        if ttm_ni is not None:
            latest_roe = ttm_ni / common_bv
            print(f"  ROE from the 10-Qs ({label}): {latest_roe:.1%} on today's common equity")
    current_roe = prompt_number(
        "  Current ROE as a percent " + (f"(Enter for the latest, {latest_roe:.1%}): " if latest_roe is not None
                                         else ": "),
        default=latest_roe * 100 if latest_roe is not None else None) / 100

    recent = rows[-3:]
    ni_sum = sum(ni for _, ni, _, _ in recent)
    default_payout = (sum(r for _, _, _, r in recent) / ni_sum) if recent and ni_sum > 0 else None
    if default_payout is None and rows and rows[-1][1] > 0:
        recent = rows[-1:]  # losses in the window: the latest profitable year alone
        default_payout = rows[-1][3] / rows[-1][1]
    if default_payout is None:
        print("  No profitable year on record to measure a payout against — defaulting to 0% (everything retained).")
        default_payout = 0.0
    elif default_payout is not None:
        clipped = min(max(default_payout, 0.0), 1.0)
        print(f"  Payout over FY{recent[0][0]}-FY{recent[-1][0]}: {default_payout:.0%}"
              + (" (capped at 0-100%)" if clipped != default_payout else ""))
        default_payout = clipped
    current_payout = prompt_number(
        "  Payout ratio as a percent " + (f"(Enter for {default_payout:.0%}): " if default_payout is not None else ": "),
        default=default_payout * 100 if default_payout is not None else None) / 100
    print(f"  -> growth in book equity now: {current_roe:.1%} x (1 - {current_payout:.0%}) "
          f"= {current_roe * (1 - current_payout):.1%}")

    industry_roe, roe_date = industry_average(damodaran, "roe", mix)
    print(f"\n  Cost of equity: {coe:.2%}")
    if industry_roe is not None:
        print(f"  Industry ROE: {industry_roe:.1%}  ({' + '.join(n for n, _ in mix)}; Damodaran, {roe_date})")
        default_stable = industry_roe
    else:
        print("  Damodaran's industry ROE isn't available — defaulting stable ROE to the cost of equity")
        print("  (no excess returns in stable growth).")
        default_stable = coe
    stable_roe = prompt_number(f"  Stable ROE the firm converges to, as a percent (Enter for {default_stable:.1%}): ",
                               default=default_stable * 100) / 100
    years = int(prompt_number("  Years to reach stable ROE and payout (Enter for 5): ", default=5))
    years = min(max(years, 1), 20)
    tg_input = input(f"  Terminal growth (Enter for {DEFAULT_TERMINAL_GROWTH:.2%}, roughly long-run GDP growth): ").strip()
    terminal_growth = parse_number(tg_input) / 100 if tg_input else DEFAULT_TERMINAL_GROWTH
    if terminal_growth >= risk_free_rate:
        print(f"  ⚠ Terminal growth is at or above the risk-free rate ({risk_free_rate:.2%}).")

    dilution, dil_label, _dil_source = latest_reported_dilution(facts)
    shares = shares_out + (dilution or 0.0)
    print(f"  Shares: {shares_out:,.0f} basic + {dilution or 0:,.0f} dilutive"
          + (f" ({dil_label})" if dil_label else "") + f" = {shares:,.0f}")

    try:
        roe_path, payout_path, stable_payout = model.financial_firm_paths(
            current_roe, stable_roe, current_payout, terminal_growth, years)
        breakdown = model.excess_return_equity_value(common_bv, roe_path, payout_path, coe, stable_roe,
                                                     terminal_growth, return_breakdown=True)
        value_range = model.run_excess_return_range(common_bv, current_roe, stable_roe, current_payout, coe,
                                                    terminal_growth, shares, years)
    except ValueError as e:
        print(f"  ⚠ Can't value with these inputs: {e}")
        return

    print(f"\n  {'Yr':>3}{'ROE':>8}{'Payout':>8}{'Begin book equity':>20}{'Excess return':>16}")
    bv = common_bv
    for t in range(years):
        print(f"  {t + 1:>3}{roe_path[t]:>8.1%}{payout_path[t]:>8.0%}{bv:>20,.0f}"
              f"{(roe_path[t] - coe) * bv:>16,.0f}")
        bv = breakdown["book_path"][t]
    print(f"  Stable: ROE {stable_roe:.1%}, payout {stable_payout:.0%} (= 1 - g/ROE), growth {terminal_growth:.2%}")

    print(f"\n  Book equity:                 ${common_bv:>18,.0f}")
    print(f"  + PV of excess returns:      ${breakdown['pv_excess']:>18,.0f}   (years 1-{years})")
    print(f"  + PV of terminal excess:     ${breakdown['pv_terminal']:>18,.0f}")
    print(f"  = Common equity value:       ${breakdown['equity']:>18,.0f}")

    print(f"\n  Assumptions: cost of equity {coe:.2%} (beta {beta_source}; ERP {erp_source})")
    print(f"\n{'='*60}")
    print(f"  VALUATION RANGE — {summary['company']} ({summary['ticker']}), excess return model")
    print(f"{'='*60}")
    if share_price:
        print(f"  Current market price: ${share_price:,.2f}\n")
    for label in ("low", "base", "high"):
        r = value_range[label]
        print(f"  {label.upper():5s}: ${r['value_per_share']:>10,.2f} per share   "
              f"(cost of equity={r['cost_of_equity']:.2%}, terminal growth={r['terminal_growth_rate']:.2%})")
    base = value_range["base"]["value_per_share"]
    print(f"\n  Price / book: market {share_price * shares / common_bv:.2f}x, "
          f"this value {breakdown['equity'] / common_bv:.2f}x" if share_price and common_bv > 0 else "")

    # What stable ROE does the price imply (everything else held)? Value
    # rises with stable ROE, so bisect between just above g and 100%.
    if share_price:
        def value_at(roe):
            r_path, p_path, _ = model.financial_firm_paths(current_roe, roe, current_payout, terminal_growth, years)
            return model.excess_return_equity_value(common_bv, r_path, p_path, coe, roe, terminal_growth) / shares
        lo, hi = terminal_growth + 1e-4, 1.0
        if value_at(lo) <= share_price <= value_at(hi):
            for _ in range(100):
                mid = (lo + hi) / 2
                lo, hi = (mid, hi) if value_at(mid) < share_price else (lo, mid)
            print(f"  The price (${share_price:,.2f}) implies a stable ROE of {lo:.1%} "
                  f"(vs. {stable_roe:.1%} assumed, cost of equity {coe:.2%}).")
    print_quarterly_annualization_warning(financials)

    summary["excess_return"] = {label: value_range[label]["value_per_share"] for label in ("low", "base", "high")}
    summary["excess_return"].update(current_roe=current_roe, stable_roe=stable_roe, payout=current_payout,
                                    common_book_equity=common_bv)
    summary["terminal_growth"] = terminal_growth


def run_project_valuation(summary, facts, fx_currency, fx_rate, financials, book_equity, shares_out,
                          share_price, market_cap, total_debt, cash, coe, risk_free_rate,
                          marginal_tax_rate, damodaran, mix):
    """
    --project: a company whose value is a mine or plant not yet producing
    (see model.project_cash_flow_paths). The operating story — when it
    produces, how much, at what margin, what's left to spend — comes from
    the company's technical report or guidance; EDGAR supplies the balance
    sheet, the current burn, the NOL pool and the share count. Prints the
    year-by-year build, the value with and without the failure overlay,
    and the full-output revenue the price implies.
    """
    print(f"\n{'='*60}")
    print("  Project valuation (pre-production, Damodaran-style project NPV)")
    print(f"{'='*60}")
    print("  The value is the project: what the finished mine or plant will earn, minus what it")
    print("  still costs to build and run the company until then. These inputs come from the")
    print("  technical report (feasibility study) or company guidance — not from the filings.")

    capex_ttm = financials.get("capex", {}).get("value") or 0.0
    ppe, _ppe_tag, _ppe_fy, ppe_unit = freshest_instant(facts, ["PropertyPlantAndEquipmentNet"])
    if ppe is not None and fx_currency and ppe_unit == fx_currency:
        ppe *= fx_rate
    print(f"\n  For reference: capex last 12 months ${capex_ttm:,.0f}; plant & equipment on the books "
          + (f"${ppe:,.0f}." if ppe is not None else "n/a."))
    build_years = int(prompt_number("  Years until first production (0 if it starts this year): "))
    remaining_capex = prompt_number("  Capex still to spend before production, in dollars "
                                    "(the company's share, net of grants already received): ")

    cash_flow, cash_flow_note = reported_cash_flow(facts, capex_ttm, fx_currency, fx_rate)
    default_overhead = max(0.0, -(cash_flow + capex_ttm)) if cash_flow is not None else None
    if default_overhead is not None:
        print(f"  Current cash burn before capex: ${default_overhead:,.0f}  "
              f"({cash_flow_note}, capex added back)")
    overhead = prompt_number("  Annual corporate costs until production, in dollars" +
                             (f" (Enter for ${default_overhead:,.0f}): " if default_overhead is not None else ": "),
                             default=default_overhead)

    full_revenue = prompt_number("  Annual revenue at full output, in dollars (capacity x expected price): ")
    industry_margin, margin_date = industry_average(damodaran, "margins", mix)
    if industry_margin is not None:
        print(f"  Industry operating margin: {industry_margin:.1%}  ({' + '.join(n for n, _ in mix)}; "
              f"Damodaran, {margin_date}) — for reference")
    margin = prompt_number("  Operating margin at full output, after depreciation, as a percent: ") / 100
    ramp_years = int(prompt_number("  Years to ramp up to full output (Enter for 1 = full output in the "
                                   "first production year): ", default=1))
    life_input = input("  Production life in years for a mine with a set reserve (Enter for a plant that runs\n"
                       "  indefinitely): ").strip()
    life_years = int(parse_number(life_input)) if life_input else None
    share = prompt_number("  Company's share of the project as a percent (Enter for 100): ", default=100) / 100

    # Cost of capital: market-value weights; debt at the actual rating's
    # spread when there is one, otherwise priced like equity — a
    # pre-production company's synthetic rating is always D.
    print(f"\n  Cost of equity: {coe:.2%}")
    cost_of_debt = coe
    rating = None
    while True:
        typed = input("  Actual bond rating, if it has one (Enter to price its debt at the cost of equity): ").strip()
        if not typed:
            break
        rated, label, _spread = model.cost_of_debt_from_rating(
            typed, risk_free_rate, (damodaran.get("ratings") or {}).get("spreads"))
        if rated is not None:
            cost_of_debt, rating = rated, typed
            print(f"  {typed} -> {label}: cost of debt {cost_of_debt:.2%}")
            break
        print("    Couldn't read that as a rating (AAA down to D, S&P or Moody's).")
    cod_input = input(f"  Press Enter for a {cost_of_debt:.2%} cost of debt, or type the actual rate on its "
                      "loans (e.g. 5 for 5%): ").strip()
    if cod_input:
        cost_of_debt = parse_number(cod_input) / 100
    computed = model.wacc(market_cap, total_debt, coe, cost_of_debt, marginal_tax_rate)
    discount_rate = prompt_number(f"  Cost of capital (Enter for {computed:.2%}): ", default=computed * 100) / 100

    nol_raw, nol_tag, _nol_fy, nol_unit = get_latest_annual_value(
        facts, ["DeferredTaxAssetsOperatingLossCarryforwards", "OperatingLossCarryforwards"])
    nol_balance = nol_raw * fx_rate if nol_raw is not None and nol_unit == fx_currency else (nol_raw or 0.0)
    print(f"  NOL carryforward on record: ${nol_balance:,.0f}" + (f" (tag: {nol_tag})" if nol_tag else ""))

    def build(full_rev, rate):
        paths = model.project_cash_flow_paths(full_rev * share, margin, build_years, ramp_years,
                                              remaining_capex, 0.0, life_years)
        paths["ebit"] = [e - (overhead if t < build_years else 0.0) for t, e in enumerate(paths["ebit"])]
        taxes = model.nol_tax_rate_path(paths["ebit"], nol_balance, marginal_tax_rate)
        return paths, model.project_value(paths, taxes, rate, marginal_tax_rate, perpetual=life_years is None), taxes

    paths, result, taxes = build(full_revenue, discount_rate)
    print(f"\n  {'Yr':>3} {'Revenue':>16} {'EBIT':>16} {'Tax%':>6} {'Capex':>16} {'FCFF':>16}")
    n_years = len(paths["revenue"])
    shown = sorted(set(range(min(n_years, build_years + ramp_years + 2))) | {n_years - 1})  # a 40-year mine: build, ramp, last
    for i, t in enumerate(shown):
        if i and t != shown[i - 1] + 1:
            print(f"  {'...':>3}  (years {shown[i - 1] + 2}-{t} the same as year {shown[i - 1] + 1})")
        print(f"  {t + 1:>3} {paths['revenue'][t]:>16,.0f} {paths['ebit'][t]:>16,.0f} {taxes[t]:>5.0%} "
              f"{paths['capex'][t]:>16,.0f} {result['fcff'][t]:>16,.0f}")
    if life_years is None:
        print(f"  Then full output forever: terminal value = NOPAT / {discount_rate:.2%} "
              "(new investment earns its cost of capital).")

    minority, _m_tag, _m_fy, m_unit = freshest_instant(facts, ["MinorityInterest", "NoncontrollingInterests"])
    minority = (minority or 0.0) * (fx_rate if fx_currency and m_unit == fx_currency else 1.0)
    if share < 1.0:
        print(f"  Note: debt and cash below are the consolidated figures; if some belong to the project")
        print(f"  partner (a joint-venture loan, say), the company's share of them is what counts.")
    if share < 1.0 and minority:
        print(f"  Partner's share is already left out ({1 - share:.0%}), so its book minority interest "
              f"(${minority:,.0f}) isn't subtracted again.")
        minority = 0.0
    dilution, dil_label, _dil_source = latest_reported_dilution(facts)
    shares = shares_out + (dilution or 0.0)
    print(f"\n  Bridge: debt ${total_debt:,.0f}, minority interest ${minority:,.0f}, cash ${cash:,.0f}, "
          f"shares {shares:,.0f}")
    print("  (From the latest 10-Q — a deal closed since then, like USA Rare Earth's Serra Verde purchase,")
    print("  isn't in it yet.)")
    if input("  Press Enter to accept, or type e to edit: ").strip().lower() == "e":
        total_debt = prompt_number(f"    Debt (Enter for ${total_debt:,.0f}): ", default=total_debt)
        minority = prompt_number(f"    Minority interest (Enter for ${minority:,.0f}): ", default=minority)
        cash = prompt_number(f"    Cash and investments (Enter for ${cash:,.0f}): ", default=cash)
        shares = prompt_number(f"    Shares, including dilution (Enter for {shares:,.0f}): ", default=shares)
        dilution = None
    equity = result["value"] - total_debt - minority + cash
    print(f"\n  PV of build years + production:  ${result['pv_explicit']:>18,.0f}")
    if life_years is None:
        print(f"  + PV of terminal value:          ${result['pv_terminal']:>18,.0f}")
    print(f"  - Debt:                          ${total_debt:>18,.0f}")
    if minority:
        print(f"  - Minority interest (book):      ${minority:>18,.0f}")
    print(f"  + Cash and investments:          ${cash:>18,.0f}")
    print(f"  = Equity value:                  ${equity:>18,.0f}   over {shares:,.0f} shares"
          + (f" (incl. {dilution:,.0f} dilutive, {dil_label})" if dilution else ""))
    print("  (Remaining capex is in the cash flows, so the shares it will take to fund it aren't")
    print("  counted again — new shares sold at fair value don't change value per share.)")

    if rating and model.failure_probability_from_rating(rating) is not None:
        default_fail = model.failure_probability_from_rating(rating)
        print(f"  10-year default probability for {rating}: {default_fail:.2%}  ({model.RATING_DEFAULT_SOURCE})")
        probability_of_failure = prompt_number(f"  Probability the project never reaches production, as a percent "
                                               f"(Enter for {default_fail:.1%}): ", default=default_fail * 100) / 100
    else:
        print("  No published base rate fits a single project's construction risk — this one is judgment")
        print("  (financing secured? permits? technology proven at scale?).")
        probability_of_failure = prompt_number("  Probability the project never reaches production, as a "
                                               "percent (e.g. 30 for 30%): ") / 100
    haircut_input = input("  Liquidation haircut on book equity (e.g. 50 for 50%; Enter for 50%): ").strip()
    liquidation_haircut = parse_number(haircut_input) / 100 if haircut_input else 0.50

    print(f"\n{'='*60}")
    print(f"  VALUATION RANGE — {summary['company']} ({summary['ticker']}), project NPV")
    print(f"{'='*60}")
    if share_price:
        print(f"  Current market price: ${share_price:,.2f}\n")
    values = {}
    for label, rate in (("low", discount_rate + 0.01), ("base", discount_rate), ("high", discount_rate - 0.01)):
        going = (build(full_revenue, rate)[1]["value"] - total_debt - minority + cash) / shares
        blended, liquidation = model.failure_adjusted_value_per_share(
            going, probability_of_failure, book_equity, shares, liquidation_haircut)
        values[label] = blended
        print(f"  {label.upper():5s}: ${blended:>10,.2f} per share   (cost of capital {rate:.2%}; going-concern "
              f"${going:,.2f}, liquidation ${liquidation:,.2f}, P(fail)={probability_of_failure:.0%})")
    print_quarterly_annualization_warning(financials)

    # Full-output revenue the price implies, everything else held — the
    # usual crux (capacity x price). Value rises with revenue; bisect.
    if share_price:
        def per_share(full_rev):
            going = (build(full_rev, discount_rate)[1]["value"] - total_debt - minority + cash) / shares
            return model.failure_adjusted_value_per_share(going, probability_of_failure, book_equity,
                                                          shares, liquidation_haircut)[0]
        lo, hi = 0.0, max(full_revenue, 1.0) * 1000
        if per_share(lo) <= share_price <= per_share(hi):
            for _ in range(100):
                mid = (lo + hi) / 2
                lo, hi = (mid, hi) if per_share(mid) < share_price else (lo, mid)
            print(f"\n  The price (${share_price:,.2f}) implies full-output revenue of ${lo:,.0f} a year"
                  + (f" ({lo / full_revenue:.1f}x the ${full_revenue:,.0f} assumed)," if full_revenue else ","))
            print(f"  at the same {margin:.0%} margin, timing and {probability_of_failure:.0%} failure probability.")
        elif per_share(lo) > share_price:
            print(f"\n  The price (${share_price:,.2f}) is below the value even with no revenue at all.")

    summary["project"] = dict(values, project_value=result["value"], equity_value=equity,
                              probability_of_failure=probability_of_failure, discount_rate=discount_rate)


def run_full_valuation(ticker, stage_arg=None, narrative=False, biotech=False, reit=False,
                       cyclical=False, financial=False, project=False):
    """
    Interactive valuation of one company. Prints the full build, and
    returns a summary dict of the key inputs and results (beta, ERP, WACC,
    low/base/high values per share, and whichever of the narrative,
    pipeline, DDM and NAV views ran) — what benchmarks/run_benchmarks.py
    compares between runs, so it doesn't have to scrape printed text.
    """
    cik, company_name = get_cik_for_ticker(ticker)
    print(f"Found CIK {cik} for {company_name}")
    summary = {"ticker": ticker.upper(), "company": company_name}

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
    warn_if_edgar_data_lags(cik, financials)

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
    implied_vol, iv_expiration, iv_days_out = (None, None, None) if SKIP_IMPLIED_VOLATILITY \
        else get_implied_volatility(ticker)
    if implied_vol is not None:
        print(f"  Market-implied volatility (~{iv_days_out}-day, ATM straddle, exp {iv_expiration}): "
              f"{implied_vol:.1%}  (reference only — not used below)")
    else:
        print("  Market-implied volatility: unavailable (Yahoo's options endpoint didn't respond —")
        print("  unofficial/rate-limited, this is expected to fail sometimes).")

    # ---- Assemble DCF inputs ----
    def operating_metric(key, label, **kwargs):
        """get_metric_value, except under --financial or --project: neither
        uses the firm's current operating figures, so a missing or stale
        one isn't asked for (Synchrony has no revenue tag; a mine under
        construction has no revenue at all)."""
        if financial or project:
            value = financials[key]["value"] or 0.0
            return (value, False) if kwargs.get("return_override_flag") else value
        return get_metric_value(financials, key, label, stale, **kwargs)

    revenue = operating_metric("revenue", "revenue")
    rebuilt = pretax_plus_interest_series(facts, years=1)
    # A stale operating-income tag (Lennar stopped tagging it after FY2020)
    # is worse than a current rebuild from pretax income + interest.
    oi_stale = ("operating_income" in stale and rebuilt and financials["operating_income"]["fiscal_year"] is not None
                and rebuilt[-1]["fiscal_year"] > financials["operating_income"]["fiscal_year"])
    if oi_stale:
        print(f"  Operating income is only tagged through FY{financials['operating_income']['fiscal_year']} — "
              "rebuilding it from pretax income + interest instead.")
        stale.remove("operating_income")
    if (financials["operating_income"]["value"] is None or oi_stale) and rebuilt:
        latest_rebuilt = rebuilt[-1]
        period_label = f"FY{latest_rebuilt['fiscal_year']}"
        # Keep the rebuild on the same TTM basis as revenue: TTM pretax
        # (already in financials) + TTM interest expense.
        pretax_info = financials["pretax_income"]
        _iv, interest_tag, _ify, interest_unit = freshest_annual_value(facts, GROSS_INTEREST_EXPENSE_TAGS)
        if pretax_info.get("ttm") and interest_tag:
            interest_periods = _recipe_annual_entries(_all_taxonomy_tags(facts), interest_tag)
            if interest_periods:
                i_end = max(interest_periods)
                interest_ttm, _ = trailing_twelve_months(facts, interest_tag, interest_unit, i_end,
                                                          interest_periods[i_end][0])
                if interest_ttm is not None:
                    # pretax_info's value is already FX-converted; convert interest to match
                    if fx_currency and interest_unit == fx_currency:
                        interest_ttm *= fx_rate
                    latest_rebuilt = {**latest_rebuilt, "val": pretax_info["value"] + interest_ttm,
                                      "unit": None}
                    period_label = f"TTM to {pretax_info['as_of']}"
        if fx_currency and latest_rebuilt["unit"] == fx_currency:
            latest_rebuilt = {**latest_rebuilt, "val": latest_rebuilt["val"] * fx_rate}
        if financial or project:
            ebit = latest_rebuilt["val"]  # unused: these modes don't value today's operations
        else:
            print("  'EBIT / operating income' isn't tagged — this company's income statement has no")
            print(f"  operating-income line. Rebuilt as pretax income + interest expense ({period_label}): "
                  f"${latest_rebuilt['val']:,.0f}")
            ebit = prompt_number("  Press Enter to use it, or type a corrected EBIT (in dollars): ",
                                 default=latest_rebuilt["val"])
    else:
        ebit = operating_metric("operating_income", "EBIT / operating income")

    # ---- One-time quarter in the TTM (Damodaran: value normal earnings,
    # not a one-off). A gain on selling a business or a licensing windfall
    # lands in one quarter and the trailing twelve months take all of it:
    # Kennametal's September 2026 quarter showed $379M of operating income
    # against a typical $40-80M; Nurix $559M after years of quarterly
    # losses. The default swaps that quarter for the company's typical one. ----
    oi_info = financials["operating_income"]
    if not (financial or project) and oi_info.get("ttm") and oi_info.get("tag") and "+" not in oi_info["tag"]:
        spike = quarterly_spike(facts, oi_info["tag"], oi_info["unit"])
        if spike:
            latest, typical, end = (x * (fx_rate if fx_currency and oi_info["unit"] == fx_currency else 1)
                                    if isinstance(x, (int, float)) else x for x in spike)
            adjusted = ebit - latest + typical
            print(f"\n  ⚠ The quarter ending {end} had operating income of ${latest:,.0f}, far from its typical")
            print(f"  ${typical:,.0f} a quarter (median of the four before) — usually a one-time item")
            print("  (a gain on a sale, a licensing payment, an impairment). Check the 10-Q.")
            keep = input(f"  Press Enter to use TTM with that quarter at its typical level (${adjusted:,.0f}),\n"
                         f"  or type k to keep the reported TTM (${ebit:,.0f}): ").strip().lower()
            if keep != "k":
                ebit = adjusted

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
    if latest_rd_raw is not None and not financial and not project:
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
    if not financial and not project and (ebit < 0 or is_trough or (cyclical and full_cycle is not None)):
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

    pretax_rebuilt = pretax_from_continuing_ops_series(facts, years=1)
    if financials["pretax_income"]["value"] is None and pretax_rebuilt:
        latest_pretax_rebuilt = pretax_rebuilt[-1]
        if fx_currency and latest_pretax_rebuilt["unit"] == fx_currency:
            latest_pretax_rebuilt = {**latest_pretax_rebuilt, "val": latest_pretax_rebuilt["val"] * fx_rate}
        if financial or project:
            pretax_income = latest_pretax_rebuilt["val"]
        else:
            print("  'pretax income' isn't tagged at all — some REITs skip a before-tax subtotal since")
            print(f"  they owe little or no corporate tax. Rebuilt as income from continuing ops + tax "
                  f"expense (FY{latest_pretax_rebuilt['fiscal_year']}): ${latest_pretax_rebuilt['val']:,.0f}")
            pretax_income = prompt_number("  Press Enter to use it, or type a corrected pretax income "
                                          "(in dollars): ", default=latest_pretax_rebuilt["val"])
    else:
        pretax_income = operating_metric("pretax_income", "pretax income")
    tax_expense = operating_metric("income_tax_expense", "income tax expense")

    # CapEx and D&A are naturally lumpy (a big equipment buy, an
    # acquisition-driven step-up in amortization) — a single year can swing
    # the reinvestment rate and FCFF hard in either direction. Damodaran
    # normalizes both over a multi-year window before using them, so we
    # use a trailing 5-year average here instead of the latest year alone.
    latest_d_and_a, d_and_a_overridden = operating_metric(
        "depreciation_amortization", "depreciation & amortization", return_override_flag=True, default=0.0)
    latest_capex, capex_overridden = operating_metric(
        "capex", "capital expenditures", return_override_flag=True, default=0.0)
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

    if financial:  # only shown as D/E beside the beta; deposits make it meaningless anyway
        total_debt = sum(financials[k]["value"] or 0.0 for k in ("total_debt_current", "total_debt_noncurrent"))
    else:
        total_debt = resolve_total_debt(financials, facts, stale, fx_currency, fx_rate)
    cash = operating_metric("cash", "cash and equivalents")
    sti_val, sti_tag, sti_fy, sti_unit = freshest_instant(facts, SHORT_TERM_INVESTMENT_TAGS)
    cash_fy = financials["cash"]["fiscal_year"]
    if sti_val and cash_fy is not None and sti_fy >= cash_fy:
        if fx_currency and sti_unit == fx_currency:
            sti_val *= fx_rate
        print(f"  Short-term investments: ${sti_val:,.0f} (tag: {sti_tag}, FY{sti_fy}) — added to cash,")
        print(f"  as Damodaran does. Cash + investments: ${cash + sti_val:,.0f}")
        cash += sti_val
    adr_ratio, market_shares, adr_counts_disagree = resolve_adr_ratio(
        ticker, cik, financials["shares_outstanding"]["value"])
    filed_count = financials["shares_outstanding"]["value"]
    share_scale = 1.0  # power of ten the filing's share counts are off by (applied to dilution too)
    if adr_ratio == 1.0 and filed_count:
        # Scale check for domestic filers: Tempus AI tags its share counts
        # in thousands (174,264 for 174.3M) and has no cover-page count, so
        # market cap came out 1,000x too small. Only an order-of-magnitude
        # gap counts — a dual-class company's filed count can legitimately
        # differ from the market's by a share class.
        domestic_market = get_market_shares(ticker)
        if domestic_market and not 0.01 <= filed_count / domestic_market <= 100:
            print(f"\n  ⚠ Filed share count {filed_count:,.0f} (tag: {financials['shares_outstanding']['tag']}) is "
                  f"{domestic_market / filed_count:,.0f}x off the market's {domestic_market:,.0f} —")
            print("  most likely tagged in thousands or millions in the filing.")
            shares_out = prompt_number("  Press Enter to use the market's count, or type the share count: ",
                                       default=domestic_market)
            financials["shares_outstanding"]["value"] = shares_out
            share_scale = 10.0 ** round(math.log10(shares_out / filed_count))
        elif domestic_market and filed_count / domestic_market < 0.67:
            # Far fewer filed shares than the market counts: usually a second
            # share class the cover page lists separately — an Up-C's Class B
            # (Smith Douglas: 9.2M Class A of ~51M economic shares) or a
            # founder class. Value per share has to spread over all of them.
            print(f"\n  ⚠ Filed share count {filed_count:,.0f} (tag: {financials['shares_outstanding']['tag']}) is "
                  f"only {filed_count / domestic_market:.0%} of the market's {domestic_market:,.0f} — usually a")
            print("  second share class (an Up-C's Class B, a founder class) that also owns the business.")
            shares_out = prompt_number("  Press Enter to use the market's count, or type the share count: ",
                                       default=domestic_market)
            financials["shares_outstanding"]["value"] = shares_out
    if adr_counts_disagree and market_shares:
        filed_count = financials["shares_outstanding"]["value"]
        print(f"  Filed ordinary shares: {filed_count:,.0f}   Market ADS count x ratio: "
              f"{market_shares * adr_ratio:,.0f}")
        shares_out = prompt_number("  Press Enter to use the market-based count, or type the ordinary share count: ",
                                   default=market_shares * adr_ratio)
    else:
        shares_out = get_metric_value(
            financials, "shares_outstanding", "shares outstanding (ordinary)", stale,
            default=market_shares * adr_ratio if market_shares else None,
            default_reason="Default: the market's ADS count x the ADR ratio." if market_shares else None)
    if adr_ratio != 1.0:
        shares_out /= adr_ratio
        print(f"  Per-share math in ADS units: {shares_out:,.0f} ADS-equivalent shares "
              f"({adr_ratio:g} ordinary shares each).")

    book_equity = get_metric_value(financials, "book_equity", "total stockholders' equity (book value)", stale)

    # ---- Operating leases as debt (Damodaran): a lease commitment is a
    # fixed obligation like a loan, and since 2019 (ASC 842) companies
    # report the present value of it on the balance sheet. Treat it as
    # debt — which feeds D/E, WACC and invested capital — and move the
    # financing part of lease cost out of operating expenses: operating
    # lease cost is roughly depreciation of the leased asset plus
    # interest, so EBIT gets the imputed interest (liability x the
    # company's own disclosed lease discount rate) added back. Interest
    # coverage below includes that lease interest too. Matters most for
    # retailers, restaurants and airlines (Walmart: $15.6B of operating
    # leases). Finance leases are left alone: many filers already fold
    # them into long-term debt, so adding them could double count.
    #
    # IFRS filers (IFRS 16) report every lease this way already: the
    # lease cost is split into depreciation (in operating profit) and
    # interest (in finance costs), so operating profit already excludes
    # the interest and the interest expense already includes it. Only the
    # liability is added to debt — no EBIT add-back, no extra coverage
    # interest, or the lease interest would be counted twice. ----
    lease_liability, lease_interest = 0.0, 0.0
    # The cover-page share count is dated after year-end (TSMC's FY2025
    # 20-F cover, while SEC still only has its FY2024 statements), so it
    # doesn't count toward the statements' year — otherwise every FY2024
    # balance-sheet item below would be dropped as stale.
    fiscal_years_seen = [info["fiscal_year"] for info in financials.values()
                         if info["fiscal_year"] is not None and info.get("tag") != "EntityCommonStockSharesOutstanding"]
    # None when every metric was manually entered (e.g. a brand-new IPO with no
    # EDGAR data at all yet) — there's no freshness baseline to compare a lease
    # tag against, and no EDGAR lease data to find anyway, so skip this block.
    latest_fy_all = max(fiscal_years_seen) if fiscal_years_seen else None
    ll_val, ll_tag, ll_fy, ll_unit = (None, None, None, None) if latest_fy_all is None else \
        freshest_instant(facts, ["OperatingLeaseLiability"])
    if latest_fy_all is not None and (ll_val is None or ll_fy < latest_fy_all):
        parts = [freshest_instant(facts, [t]) for t in
                 ("OperatingLeaseLiabilityCurrent", "OperatingLeaseLiabilityNoncurrent")]
        if all(v is not None and fy is not None and fy >= latest_fy_all for v, _, fy, _ in parts):
            ll_val, ll_tag, ll_fy, ll_unit = (sum(v for v, _, _, _ in parts),
                                              "OperatingLeaseLiabilityCurrent+Noncurrent", parts[0][2], parts[0][3])
    ifrs_leases = False
    if latest_fy_all is not None and (ll_val is None or ll_fy < latest_fy_all):
        i_val, i_tag, i_fy, i_unit = freshest_instant(
            facts, ["LeaseLiabilities", ("CurrentLeaseLiabilities", "NoncurrentLeaseLiabilities")])
        if i_val and i_fy is not None and i_fy >= latest_fy_all:
            ll_val, ll_tag, ll_fy, ll_unit, ifrs_leases = i_val, i_tag, i_fy, i_unit, True
    if ll_val and ll_fy is not None and ll_fy >= latest_fy_all:
        if fx_currency and ll_unit == fx_currency:
            ll_val *= fx_rate
    if financial:
        pass  # leases are an operating-side adjustment; the equity model doesn't use debt
    elif ifrs_leases:
        print(f"\n{'='*60}")
        print("  Leases (treated as debt, Damodaran-style — IFRS 16)")
        print(f"{'='*60}")
        print(f"  Lease liabilities: ${ll_val:,.0f}  (tag: {ll_tag}, FY{ll_fy})")
        print("  -> debt +${:,.0f}. No EBIT add-back: under IFRS 16 operating profit already".format(ll_val))
        print("  excludes lease interest, and interest expense already includes it.")
        if input("  Press Enter to include leases, or type n to leave them out: ").strip().lower() != "n":
            lease_liability = ll_val
            total_debt += lease_liability
    elif ll_val and ll_fy is not None and ll_fy >= latest_fy_all:
        rate_val, _, rate_fy, _ = freshest_annual_value(facts, ["OperatingLeaseWeightedAverageDiscountRatePercent"])
        if rate_val and rate_fy is not None and rate_fy >= latest_fy_all and 0 < rate_val < 0.30:
            lease_rate, rate_note = rate_val, "the company's disclosed lease discount rate"
        else:
            lease_rate, rate_note = DEFAULT_COST_OF_DEBT, "a default rate (no lease discount rate disclosed)"
        print(f"\n{'='*60}")
        print("  Operating leases (treated as debt, Damodaran-style)")
        print(f"{'='*60}")
        print(f"  Operating lease liability: ${ll_val:,.0f}  (tag: {ll_tag}, FY{ll_fy})")
        print(f"  Imputed lease interest:    ${ll_val * lease_rate:,.0f}  ({lease_rate:.2%}, {rate_note})")
        print(f"  -> debt +${ll_val:,.0f}, EBIT +${ll_val * lease_rate:,.0f} (lease interest is financing, not operating)")
        if input("  Press Enter to include leases, or type n to leave them out: ").strip().lower() != "n":
            lease_liability, lease_interest = ll_val, ll_val * lease_rate
            total_debt += lease_liability
            ebit += lease_interest

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

    # ---- Country exposure and marginal tax rate. Asked here, before beta,
    # because the marginal tax rate is needed to relever the beta (and
    # later for terminal-year NOPAT and after-tax pensions), and the same
    # country answer feeds the ERP's country risk premium below.
    #
    # Tax and ERP use different country bases, following Damodaran: the
    # ERP is weighted by where REVENUE is earned (operating risk), but the
    # marginal tax rate is the statutory rate where PROFITS are taxed —
    # normally the domicile. The rate comes from his country table (a
    # published figure) instead of a flat 25% US assumption for everyone.
    domicile = edgar_domicile(cik)
    country_mix = None
    marginal_tax_rate, tax_source = DEFAULT_MARGINAL_TAX_RATE, "US statutory default"
    if damodaran["countries"]:
        countries = damodaran["countries"]["countries"]
        print(f"\n{'='*60}")
        print(f"  Country exposure (Damodaran's country table, {damodaran['countries']['updated']})")
        print(f"{'='*60}")
        country_mix = choose_country_mix(ticker, domicile, countries)
        if domicile in countries and countries[domicile]["tax"] is not None:
            tax_country = domicile
        elif len(country_mix) == 1 and countries[country_mix[0][0]]["tax"] is not None:
            tax_country = country_mix[0][0]  # no domicile on EDGAR — use the country named (TSMC: Taiwan)
        else:
            tax_country = None
        if tax_country:
            marginal_tax_rate = countries[tax_country]["tax"]
            tax_source = f"{tax_country} statutory rate, Damodaran's country table"
    print(f"  Marginal tax rate: {marginal_tax_rate:.2%}  ({tax_source})")
    print("  (Where profits are taxed, normally the domicile. If most profits are taxed elsewhere —")
    print("  e.g. a Hong Kong-addressed company whose profits are mainland Chinese, at 25% — type that.)")
    typed_tax = prompt_number(f"  Press Enter to use {marginal_tax_rate:.2%}, or type the marginal tax rate as a percent: ",
                              default=marginal_tax_rate * 100) / 100
    if abs(typed_tax - marginal_tax_rate) > 1e-12:
        marginal_tax_rate, tax_source = typed_tax, "typed"

    print(f"\n{'='*60}")
    print("  Beta (bottom-up, Damodaran industry average)")
    print(f"{'='*60}")
    print("  Regression beta (from market data): " + (f"{regression_beta:.2f}  — one company's, noisy"
                                                       if regression_beta is not None else "not available"))
    print(f"  Company's D/E (debt/market cap):    {debt_to_equity:.2f}")
    beta_source = "regression beta (as pulled)"
    industry_beta = None
    mix = None  # the chosen industry mix, reused by the sanity checks
    beta_mix = roe_mix = None  # --financial: the mixes behind the beta and the industry ROE
    if damodaran["betas"]:
        industries = damodaran["betas"]["industries"]
        if fx_currency:
            print("  Note: this is a foreign filer — these are Damodaran's US industry betas;")
            print("  his regional tables can be used by typing an unlevered beta below.")
        sic, sic_description = get_sic(cik)
        mix = choose_industries(ticker, sic, sic_description, industries, biotech)
        if mix and financial:
            # Damodaran uses a bank's or broker's industry beta as is: its
            # D/E is mostly deposits, so unlevering and relevering it means
            # nothing (and regulation keeps leverage similar across firms).
            # Damodaran's regional-bank beta (0.40) averages hundreds of
            # small, thinly traded banks, whose measured betas are dragged
            # down by stale prices. A large bank trades every second, so it
            # takes the money-center beta instead (same $5B line as the
            # large-firm rating table). Its industry ROE switches only above
            # MONEY_CENTER_ROE_MARKET_CAP: liquidity is about size, returns
            # about the business.
            beta_mix = roe_mix = mix
            if (market_cap > LARGE_FIRM_MARKET_CAP and [name for name, _ in mix] == ["Banks (Regional)"]
                    and "Bank (Money Center)" in industries):
                beta_mix = [("Bank (Money Center)", 1.0)]
                if market_cap > MONEY_CENTER_ROE_MARKET_CAP:
                    roe_mix = beta_mix
                print(f"  Large bank (market cap ${market_cap / 1e9:,.0f}B > ${LARGE_FIRM_MARKET_CAP / 1e9:,.0f}B): "
                      f"using the money-center beta, not the regional one\n"
                      f"  ({industries['Banks (Regional)']['beta']:.2f}, {industries['Banks (Regional)']['firms']} "
                      "mostly small, thinly traded banks)"
                      + (f"; over ${MONEY_CENTER_ROE_MARKET_CAP / 1e9:,.0f}B, its industry ROE too." if roe_mix is beta_mix
                         else f". Industry ROE stays regional (under ${MONEY_CENTER_ROE_MARKET_CAP / 1e9:,.0f}B)."))
            unlevered = model.weighted_unlevered_beta(
                [(industries[name]["beta"], weight) for name, weight in beta_mix])
            industry_beta = unlevered
            for name, weight in beta_mix:
                print(f"    {name:<42} beta {industries[name]['beta']:.2f}  ({industries[name]['firms']} firms)"
                      + (f"  x {weight:.0%}" if len(beta_mix) > 1 else ""))
            print(f"  Industry beta: {industry_beta:.2f}  (levered, used as is — no relevering for a financial firm)")
            print(f"  (Damodaran's US industry betas, updated {damodaran['betas']['updated']})")
            industry_label = " + ".join(name for name, _ in beta_mix)
        elif mix:
            unlevered = model.weighted_unlevered_beta(
                [(industries[name]["unlevered_beta_cash"], weight) for name, weight in mix])
            industry_beta = model.relever_beta(unlevered, marginal_tax_rate, debt_to_equity)
            for name, weight in mix:
                d = industries[name]
                print(f"    {name:<42} unlevered {d['unlevered_beta_cash']:.2f}  ({d['firms']} firms)"
                      + (f"  x {weight:.0%}" if len(mix) > 1 else ""))
            print(f"  Unlevered beta: {unlevered:.2f}  -> relevered at D/E {debt_to_equity:.2f}, "
                  f"{marginal_tax_rate:.1%} marginal tax: {industry_beta:.2f}")
            print(f"  (Damodaran's US industry betas, updated {damodaran['betas']['updated']})")
            industry_label = " + ".join(name for name, _ in mix)
    else:
        print("  Damodaran's industry betas couldn't be loaded (no connection and no cache).")

    if industry_beta is not None and regression_beta is None and financial:
        answer = input(f"  Press Enter to use the industry beta {industry_beta:.2f}, or type a beta to use as is: "
                       ).strip().lower()
        answer = "" if answer == "r" else answer
    elif industry_beta is not None and regression_beta is None:
        answer = input(f"  Press Enter to use the industry beta {industry_beta:.2f}, or type an UNLEVERED beta\n"
                       "  to relever instead: ").strip().lower()
        answer = "" if answer == "r" else answer
    elif industry_beta is not None and financial:
        answer = input(f"  Press Enter to use the industry beta {industry_beta:.2f}, type r for the regression\n"
                       "  beta, or type a beta to use as is: ").strip().lower()
    elif industry_beta is not None:
        answer = input(f"  Press Enter to use the industry beta {industry_beta:.2f}, type r for the regression\n"
                       "  beta, or type an UNLEVERED beta to relever instead: ").strip().lower()
    elif regression_beta is None:
        answer = str(prompt_number("  No industry or regression beta — type an UNLEVERED beta to relever: "))
    else:
        answer = input("  Press Enter to use the regression beta, or type an UNLEVERED beta to\n"
                       "  relever instead: ").strip().lower()
    if answer == "r" or (not answer and industry_beta is None):
        beta = regression_beta
    elif not answer:
        beta = industry_beta
        beta_source = (f"industry: {industry_label}, levered, as is" if financial else
                       f"industry: {industry_label}, unlevered {unlevered:.2f}, relevered")
    elif financial:
        try:
            beta = parse_number(answer)
            beta_source = "typed"
        except ValueError:
            print("  Couldn't read that — using the industry beta.")
            beta = industry_beta if industry_beta is not None else regression_beta
            beta_source = f"industry: {industry_label}, levered, as is" if industry_beta is not None else beta_source
    else:
        try:
            typed_unlevered = parse_number(answer)
            beta = model.relever_beta(typed_unlevered, marginal_tax_rate, debt_to_equity)
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

    # Country risk: operations-weighted CRP on top of the US implied ERP
    # (see the COUNTRY RISK section above for the formula), using the
    # country mix chosen before beta.
    country_adjustment = 0.0
    if country_mix:
        countries = damodaran["countries"]["countries"]
        us_crp = countries["United States"]["crp"]
        print(f"\n  Country risk (Damodaran's country premiums, {damodaran['countries']['updated']})")
        weighted_crp = model.weighted_unlevered_beta([(countries[n]["crp"], w) for n, w in country_mix])
        for name, weight in country_mix:
            print(f"    {name:<28} CRP {countries[name]['crp']:.2%}"
                  + (f"  x {weight:.0%}" if len(country_mix) > 1 else ""))
        country_adjustment = weighted_crp - us_crp
        print(f"  Weighted CRP {weighted_crp:.2%} - US CRP {us_crp:.2%} = {country_adjustment:+.2%} "
              f"-> ERP {default_erp:.2%} {country_adjustment:+.2%} = {default_erp + country_adjustment:.2%}")
        if country_adjustment:
            default_erp += country_adjustment
            erp_source += f", {country_adjustment:+.2%} country risk"
    else:
        print("  (Damodaran's country premiums couldn't be loaded — no country adjustment.)")
    erp = prompt_number(f"  Press Enter to use {default_erp:.2%}, or type an ERP as a percent (e.g. 5 for 5%): ",
                        default=default_erp * 100) / 100
    if abs(erp - default_erp) > 1e-12:
        erp_source = "typed"

    # Computed here (rather than down with WACC) because the dividend
    # discount model below needs cost of equity too, before WACC exists.
    coe = model.cost_of_equity(risk_free_rate, beta, erp)
    summary.update(beta=beta, risk_free_rate=risk_free_rate, erp=erp, cost_of_equity=coe)

    if financial:
        run_financial_firm_valuation(summary, facts, fx_currency, fx_rate, financials, book_equity, shares_out,
                                     share_price, coe, damodaran, roe_mix or mix, risk_free_rate,
                                     beta_source, erp_source)
        return summary
    if project:
        run_project_valuation(summary, facts, fx_currency, fx_rate, financials, book_equity, shares_out,
                              share_price, market_cap, total_debt, cash, coe, risk_free_rate,
                              marginal_tax_rate, damodaran, mix)
        return summary

    # ---- Reinvestment-rate-derived growth (Damodaran fundamental growth) ----
    # NOTE: change in net working capital defaults to 0 here, same simplification
    # as in compute_fcff — see that function's docstring for why.
    invested_capital = total_debt + book_equity - cash
    if rd_result is not None:
        invested_capital += rd_result["research_asset"]
    # Growth = reinvestment rate x ROIC only means something when the
    # company earns a profit on positive capital. With negative NOPAT both
    # factors turn negative and multiply into a large POSITIVE "growth"
    # (Roblox: -176% x -29% = +51%; Viking: +140%) — an artifact of signs.
    # For a money-loser growth has to come from a revenue story instead
    # (the narrative DCF), so nothing is offered as derived.
    growth_undefined = nopat <= 0 or invested_capital <= 0

    # ---- Working capital (Damodaran): non-cash working capital (receivables
    # + inventory - payables) is reinvestment, like net capex — revenue
    # growth ties more of it up (or, paid in advance, releases it). A single
    # company's year-to-year change is noisy, so the ratio is his industry
    # figure for the chosen mix, applied to revenue growth. ----
    wc_ratio, wc_updated = industry_average(damodaran, "wc", mix)
    print(f"\n{'='*60}")
    print("  Working capital (non-cash, as % of revenue)")
    print(f"{'='*60}")
    if wc_ratio is not None:
        print(f"  Industry: {wc_ratio:.1%}  ({' + '.join(n for n, _ in mix)}; Damodaran, {wc_updated})")
        print("  Each year's revenue increase ties up this share of it in receivables and")
        print("  inventory net of payables (a negative figure means growth releases cash).")
        wc_ratio = prompt_number(f"  Press Enter to use {wc_ratio:.1%}, or type a percent (e.g. 5 for 5%): ",
                                 default=wc_ratio * 100) / 100
    else:
        print("  No industry figure available for this company's industry mix.")
        wc_ratio = prompt_number("  Type a percent of revenue, or Enter for 0% (working capital ignored): ",
                                 default=0.0) / 100
    if abs(wc_ratio) > 0.5:
        print(f"  ⚠ {wc_ratio:.0%} is unusually large — industries like REITs and financials carry loans")
        print("  and receivables that aren't operating working capital. Check before relying on it.")
    # For the reinvestment rate, the average yearly revenue increase over the
    # same five years the CapEx and D&A averages use — one year's jump
    # (TSMC: +34%) would swing the implied growth by itself.
    revenue_history = get_annual_series(facts, METRIC_TAGS["revenue"], years=6)
    avg_change_in_wc = 0.0
    if wc_ratio and len(revenue_history) >= 2 and revenue_history[-1]["val"]:
        avg_increase = ((revenue_history[-1]["val"] - revenue_history[0]["val"])
                        / (len(revenue_history) - 1) / revenue_history[-1]["val"])  # share of revenue, currency-free
        avg_change_in_wc = wc_ratio * revenue * avg_increase
        print(f"  Average yearly working-capital change (FY{revenue_history[0]['fiscal_year']}-"
              f"FY{revenue_history[-1]['fiscal_year']} revenue growth): ${avg_change_in_wc:,.0f}")

    try:
        reinvestment_rate = model.compute_reinvestment_rate(capex, d_and_a, avg_change_in_wc, nopat)
        roic = model.compute_roic(nopat, invested_capital)
        implied_growth = 0.0 if growth_undefined else model.implied_growth_rate(reinvestment_rate, roic)
    except (ZeroDivisionError, ValueError):
        reinvestment_rate = roic = None
        implied_growth = DEFAULT_FCFF_GROWTH

    print(f"\n{'='*60}")
    print("  Growth check (reinvestment-rate-derived)")
    print(f"{'='*60}")
    if reinvestment_rate is not None:
        print(f"  Reinvestment rate:  {reinvestment_rate:.2%}   (CapEx - D&A"
              + (" + change in working capital" if avg_change_in_wc else "") + ") / NOPAT"
              + ("  [R&D-adjusted]" if rd_result is not None else ""))
        print(f"  ROIC:               {roic:.2%}   NOPAT / Invested Capital"
              + ("  [includes research asset]" if rd_result is not None else ""))
        if growth_undefined:
            print("  Implied growth:     not meaningful — " +
                  ("NOPAT is negative" if nopat <= 0 else "invested capital is negative") +
                  ", so reinvestment rate x ROIC")
            print("  multiplies two negatives into a spurious positive. Growth for a company that")
            print("  isn't yet profitable has to come from a revenue story: use --narrative.")
            print("  Defaulting to 0% (no growth assumed) for this DCF.")
        else:
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
    is_large_firm = market_cap > LARGE_FIRM_MARKET_CAP
    synthetic_cod, interest_coverage, synthetic_rating, default_spread = None, None, None, None
    actual_rating, rating_asked = None, False  # asked with the cost of debt, reused by the failure overlay
    if lease_interest:
        # Leases are debt now, so their interest counts toward coverage.
        latest_interest_expense = (latest_interest_expense or 0.0) + lease_interest
    if latest_interest_expense:
        synthetic_cod, interest_coverage, synthetic_rating, default_spread = model.synthetic_cost_of_debt(
            ebit, latest_interest_expense, risk_free_rate, large_firm=is_large_firm,
            spreads=(damodaran.get("ratings") or {}).get("spreads"))

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
        if damodaran.get("ratings"):
            print(f"  Spread: Damodaran's published spread for {synthetic_rating} "
                  f"(as of {damodaran['ratings']['as_of']}).")
        else:
            print("  Spread values are the built-in illustrative ones (Damodaran's published table")
            print("  couldn't be loaded).")
        # An actual rating beats the synthetic one when the company has
        # one, and it matters most for a money-loser: negative operating
        # income always maps to D (a 24% cost of debt for Tempus), which
        # Damodaran says not to use for a going concern. The same rating
        # sets the failure overlay's default probability below.
        if ebit <= 0:
            print("  Operating income isn't positive, so the synthetic rating is D — not meant for a going")
            print("  concern. An actual bond rating gives a better cost of debt (and failure probability).")
        rating_asked = True
        default_cod, cod_label = synthetic_cod, f"the synthetic {synthetic_cod:.2%}"
        while True:
            typed = input("  Actual bond rating, if it has one (S&P or Moody's, e.g. BBB+ or Baa1 — in the 10-K's\n"
                          "  debt discussion or on the agency's site; Enter to use the synthetic): ").strip()
            if not typed:
                break
            rated_cod, rated_label, rated_spread = model.cost_of_debt_from_rating(
                typed, risk_free_rate, (damodaran.get("ratings") or {}).get("spreads"))
            if rated_cod is not None:
                actual_rating = typed
                default_cod, cod_label = rated_cod, f"{rated_cod:.2%} for the actual {typed}"
                print(f"  Actual rating {typed} -> spread table row {rated_label}: {risk_free_rate:.2%} + "
                      f"{rated_spread:.2%} = {rated_cod:.2%}")
                break
            print("    Couldn't read that as a rating (AAA down to D, S&P or Moody's).")
        cod_input = input(f"  Press Enter to use {cod_label}, or type the company's actual bond yield as a "
                           "percent (e.g. 6 for 6%): ").strip()
        cost_of_debt = parse_number(cod_input) / 100 if cod_input else default_cod
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
    summary["wacc"] = discount_rate
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

    # ---- Return on new capital in stable growth (Damodaran): growth past
    # the forecast isn't free — growing at g forever takes reinvesting
    # g / RONIC of NOPAT every year. Both DCFs build their terminal value
    # this way (model.terminal_value_consistent) instead of growing the
    # last forecast year's cash flow, whose reinvestment says nothing
    # about perpetual growth. His default sets RONIC to the cost of
    # capital: competition eventually wipes out excess returns on NEW
    # investment, so growth stops adding value. Only a durable moat
    # justifies more. ----
    print(f"\n{'='*60}")
    print("  Return on new capital in stable growth")
    print(f"{'='*60}")
    print(f"  Current ROIC: " + (f"{roic:.2%}" if roic is not None else "n/a") +
          f"   WACC: {discount_rate:.2%}")
    print(f"  Default: return on new capital = WACC, so stable-growth reinvestment is")
    print(f"  {terminal_growth:.2%} / {discount_rate:.2%} = {terminal_growth / discount_rate:.0%} of NOPAT "
          "and growth adds no value (Damodaran's")
    print("  standard assumption). Type a higher return only for a durable competitive advantage.")
    while True:
        ronic_input = input("  Press Enter for WACC, or type a return on new capital as a percent "
                            "(e.g. 12 for 12%): ").strip()
        if not ronic_input:
            return_on_new_capital = None
            break
        try:
            return_on_new_capital = parse_number(ronic_input) / 100
        except ValueError:
            print("    Please type a number.")
            continue
        if return_on_new_capital <= 0:
            print("    It has to be positive.")
            continue
        print(f"  Using {return_on_new_capital:.2%}: stable-growth reinvestment = "
              f"{terminal_growth / return_on_new_capital:.0%} of NOPAT.")
        break
    ronic_label = (f"{return_on_new_capital:.2%}" if return_on_new_capital is not None
                   else "= WACC (no excess returns on new investment)")
    # The terminal NOPAT basis is taxed at the marginal rate: effective
    # rates drift toward it as tax breaks and NOL shields run out, and
    # Damodaran's Ginzu uses the marginal rate in stable growth.
    terminal_nopat_basis = ebit * (1 - marginal_tax_rate)

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
        if latest_dps is not None:
            latest_dps *= adr_ratio  # per ordinary share in the filing -> per ADS
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
            summary["ddm"] = {label: ddm_results[label]["value_per_share"] for label in ("low", "base", "high")}
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
            *reit_args, default=0.0, per_share=True) * adr_ratio
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
            summary["ddm"] = {label: ddm_results[label]["value_per_share"] for label in ("low", "base", "high")}
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
        summary["nav_per_share"] = nav["nav_per_share"]
        premium = share_price / nav["nav_per_share"] - 1 if nav["nav_per_share"] > 0 else None
        print(f"  Property value: ${nav['property_value']:,.0f}   + cash ${cash:,.0f}   - debt ${reit_debt:,.0f}"
              f"   - preferred ${preferred:,.0f}")
        print(f"  NAV: ${nav['nav']:,.0f}")
        print(f"  NAV per share: ${nav['nav_per_share']:,.2f}   vs current price ${share_price:,.2f}" +
              (f"  ({premium:+.0%} {'premium' if premium >= 0 else 'discount'} to NAV)" if premium is not None else ""))
        print_quarterly_annualization_warning(financials)

    # ---- Equity bridge (Damodaran): the DCF values the OPERATING business;
    # getting from there to value per share means subtracting every other
    # claim on it and adding every non-operating asset it doesn't count:
    #   - debt (incl. operating leases, above)
    #   - minority interests: the slice of consolidated subsidiaries owned
    #     by outsiders, whose cash flows ARE in the consolidated DCF
    #   - preferred stock
    #   - unfunded pensions, after tax (a debt-like promise, tax-deductible
    #     when paid)
    #   + cash and short-term investments (above)
    #   + equity-method stakes in other companies, whose earnings sit
    #     below operating income and so are outside the DCF
    # Employee options, RSUs and convertibles enter as the company's own
    # reported dilution — diluted minus basic shares (latest_reported_
    # dilution) — added to the share count. Valuing options with Black-
    # Scholes instead would need an option term and volatility that EDGAR
    # doesn't carry, i.e. assumed inputs fed into a model; the reported
    # treasury-stock count uses every real tranche and is audited, at the
    # cost of counting out-of-the-money options as zero. Book values
    # stand in for market values on minority interests and equity stakes
    # (as reported, rather than estimated). Each item can be edited. ----
    def current_value(tags, instant=True):
        val, tag, fy, unit = (freshest_instant if instant else freshest_annual_value)(facts, tags)
        if val is None or fy is None or fy < latest_fy_all:
            return 0.0, None
        if fx_currency and unit and (unit == fx_currency or unit.startswith(f"{fx_currency}/")):
            val *= fx_rate  # a per-share price like an option strike is "TWD/shares", not "TWD"
        return val, tag

    minority_interest, _ = current_value(["MinorityInterest", "NoncontrollingInterests"])  # us-gaap, ifrs-full
    bridge_preferred, _ = current_value(PREFERRED_EQUITY_TAGS)
    pension_liability, _ = current_value(["DefinedBenefitPensionPlanLiabilitiesNoncurrent",
                                          "PensionAndOtherPostretirementDefinedBenefitPlansLiabilitiesNoncurrent",
                                          "LiabilityDefinedBenefitPlanNoncurrent"])
    equity_investments, _ = current_value(["EquityMethodInvestments",
                                           "InvestmentAccountedForUsingEquityMethod"])  # us-gaap, ifrs-full
    other_dilution_shares, dilution_period, dilution_source = latest_reported_dilution(facts)
    dilution_reported = other_dilution_shares is not None
    converts_in_debt = [financials[k]["tag"] for k in ("total_debt_current", "total_debt_noncurrent")
                        if financials.get(k, {}).get("tag") and "Convertible" in financials[k]["tag"]
                        and financials[k].get("value")]
    # The scale fix only applies to diluted - basic: those are the same
    # weighted-average tags the mis-scaled share count came from. Tempus
    # files its anti-dilutive count in plain shares (23.4M), so scaling it
    # too made 23.4 billion.
    if dilution_source == "diluted-basic":
        other_dilution_shares = (other_dilution_shares or 0.0) * share_scale
    other_dilution_shares = (other_dilution_shares or 0.0) / adr_ratio  # filed in ordinary shares -> ADS units

    def show_bridge():
        print(f"  - Debt (incl. operating leases):          ${total_debt:>18,.0f}")
        print(f"  - Minority interests (book):              ${minority_interest:>18,.0f}")
        print(f"  - Preferred stock:                        ${bridge_preferred:>18,.0f}")
        print(f"  - Unfunded pensions (x {1 - marginal_tax_rate:.0%} after tax):     "
              f"${pension_liability * (1 - marginal_tax_rate):>18,.0f}")
        print(f"  + Cash and short-term investments:        ${cash:>18,.0f}")
        print(f"  + Equity-method investments (book):       ${equity_investments:>18,.0f}")
        print(f"  Shares: {shares_out:,.0f} basic + {other_dilution_shares:,.0f} options/RSUs/converts "
              f"= {shares_out + other_dilution_shares:,.0f}")
        if dilution_source == "antidilutive":
            print(f"    (diluted = basic in that loss period, {dilution_period}, so this is the count the company")
            print("    reported EXCLUDED as anti-dilutive — gross: options in full whatever their strike)")
            if converts_in_debt:
                print(f"    ⚠ Debt includes convertible notes ({', '.join(converts_in_debt)}). If the filing's")
                print("    anti-dilutive table counts their conversion shares, subtract those (type e) — they're")
                print("    already valued as debt, so counting them as shares too would charge for them twice.")
        else:
            print("    (dilution = the company's reported diluted - basic shares, "
                  + (f"{dilution_period})" if dilution_reported else "not reported)"))

    print(f"\n{'='*60}")
    print("  Equity bridge (operating value -> value per share)")
    print(f"{'='*60}")
    show_bridge()
    if dilution_reported and other_dilution_shares == 0:
        print("  (Diluted = basic for that period: usual when the company had a loss, since dilution")
        print("  is then excluded as anti-dilutive — so none is REPORTED, which doesn't mean none")
        print("  exists. Enter a count below if you have it from the filing's equity note.)")
    if input("  Press Enter to accept, or type e to edit each item: ").strip().lower() == "e":
        minority_interest = prompt_number(f"    Minority interests (Enter for ${minority_interest:,.0f}): ",
                                          default=minority_interest)
        bridge_preferred = prompt_number(f"    Preferred stock (Enter for ${bridge_preferred:,.0f}): ",
                                         default=bridge_preferred)
        pension_liability = prompt_number(f"    Unfunded pension, before tax (Enter for ${pension_liability:,.0f}): ",
                                          default=pension_liability)
        equity_investments = prompt_number(f"    Equity-method investments (Enter for ${equity_investments:,.0f}): ",
                                           default=equity_investments)
        other_dilution_shares = prompt_number(f"    Dilutive shares — options/RSUs/converts (Enter for "
                                              f"{other_dilution_shares:,.0f}): ", default=other_dilution_shares)
        show_bridge()
    # What the per-share math subtracts and adds; total_debt itself stays
    # pure debt, since that's what belongs in D/E and WACC.
    bridge_debt = (total_debt + minority_interest + bridge_preferred
                   + pension_liability * (1 - marginal_tax_rate))
    bridge_cash = cash + equity_investments
    value_shares = shares_out + other_dilution_shares

    print(f"\n{'='*60}")
    print("  Assumptions used")
    summary.update(revenue=revenue, ebit=ebit, fcff=fcff, total_debt=total_debt, cash=cash,
                   bridge_debt=bridge_debt, bridge_cash=bridge_cash, shares=value_shares,
                   adr_ratio=adr_ratio, terminal_growth=terminal_growth,
                   return_on_new_capital=return_on_new_capital, share_price=share_price,
                   working_capital_ratio=wc_ratio)
    print(f"{'='*60}")
    print(f"  Effective tax rate:        {effective_tax_rate:.2%}")
    print(f"  Beta:                      {beta:.2f}  ({beta_source})")
    print(f"  Risk-free rate:            {risk_free_rate:.2%}  ({risk_free_source})")
    print(f"  Equity risk premium:       {erp:.2%}  ({erp_source})")
    print(f"  Cost of equity (CAPM):     {coe:.2%}")
    print(f"  WACC (discount rate):      {discount_rate:.2%}")
    print(f"  Market cap:                ${market_cap:,.0f}")
    print(f"  Base-year FCFF:            ${fcff:,.0f}")
    print(f"  Working capital:           {wc_ratio:.1%} of each year's revenue increase"
          + ("" if wc_ratio else " (ignored)"))
    print(f"  High-growth-stage rate:    {fcff_growth:.2%}  (reinvestment-rate-derived; type an override above to change)")
    print(f"  Assumed terminal growth:   {terminal_growth:.2%}  (type an override above to change)")
    print(f"  Return on new capital:     {ronic_label}  (stable-growth reinvestment = g / this)")
    if lease_liability:
        print(f"  Leases:                    ${lease_liability:,.0f} added to debt, " +
              ("no EBIT add-back (IFRS 16: already excluded)" if ifrs_leases
               else f"${lease_interest:,.0f} added back to EBIT"))
    print(f"  Equity bridge:             claims beyond debt ${bridge_debt - total_debt:,.0f}, "
          f"non-operating assets beyond cash ${equity_investments:,.0f}, "
          f"{other_dilution_shares:,.0f} extra shares")
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
        total_debt=bridge_debt,
        cash=bridge_cash,
        shares_outstanding=value_shares,
        base_nopat=terminal_nopat_basis,
        return_on_new_capital=return_on_new_capital,
        base_revenue=revenue,
        working_capital_ratio=wc_ratio,
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
    summary["dcf"] = {label: results[label]["value_per_share"] for label in ("low", "base", "high")}
    print(f"{'='*60}")
    print(f"  Current market price: ${share_price:,.2f}\n")
    for label in ("low", "base", "high"):
        r = results[label]
        print(f"  {label.upper():5s}: ${r['value_per_share']:>10,.2f} per share   "
              f"(WACC={r['discount_rate']:.2%}, terminal growth={r['terminal_growth_rate']:.2%})")
    if qual_adjustment:
        print(f"\n  ({qual_adjustment:+.0%} qualitative adjustment applied"
              + (f": {qual_note}" if qual_note else "") + ")")
    print_quarterly_annualization_warning(financials)

    # ---- Sanity checks (Damodaran): diagnostics only — they compare what
    # the valuation implies against arithmetic and published industry
    # figures and never change a number. ----
    print(f"\n  Sanity checks (base case)")
    base_breakdown = model.enterprise_value_multi_stage(
        fcff, fcff_growth, high_growth_years, fade_years, terminal_growth, discount_rate,
        base_nopat=terminal_nopat_basis, return_on_new_capital=return_on_new_capital, return_breakdown=True,
        base_revenue=revenue, working_capital_ratio=wc_ratio)
    summary["sanity"] = {"tv_share_dcf": print_terminal_share(base_breakdown, high_growth_years + fade_years)}

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
            discount_rate, terminal_growth, bridge_debt, bridge_cash, value_shares,
            base_nopat=terminal_nopat_basis, return_on_new_capital=return_on_new_capital,
            base_revenue=revenue, working_capital_ratio=wc_ratio)
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
    implied_ev = market_cap + bridge_debt - bridge_cash
    implied_multiple = implied_ev / revenue if revenue else None
    if implied_multiple is not None:
        print(f"\n{'='*60}")
        print("  Market-implied EV/Revenue multiple")
        print(f"{'='*60}")
        print(f"  The current price (${share_price:,.2f}) implies an EV of ${implied_ev:,.0f}")
        print(f"  (market cap ${market_cap:,.0f} + debt and other claims ${bridge_debt:,.0f} - cash and "
              f"investments ${bridge_cash:,.0f}),")
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
                revenue, multiple, bridge_debt, bridge_cash, value_shares)
            print(f"  EV ({multiple:g}x revenue): ${revenue * multiple:,.0f}   "
                  f"Equity value: ${rev_equity_value:,.0f}   "
                  f"Per share: ${rev_value_per_share:,.2f}")
            print_quarterly_annualization_warning(financials)

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
        drug_tax_input = input(f"  Tax rate on drug profits (e.g. 25 for 25%; Enter for the marginal "
                               f"{marginal_tax_rate:.1%}): ").strip()
        drug_tax_rate = parse_number(drug_tax_input) / 100 if drug_tax_input else marginal_tax_rate
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
            peak_sales = prompt_number("    Peak annual sales in dollars (e.g. 1500000000): ")
            default_years = PHASE_DEFAULT_YEARS_TO_LAUNCH[phase]
            ytl_input = input(f"    Years until launch (Enter for {default_years}): ").strip()
            years_to_launch = int(parse_number(ytl_input)) if ytl_input else default_years
            ramp_input = input("    Years to ramp to peak sales after launch (Enter for 5): ").strip()
            ramp_years = int(parse_number(ramp_input)) if ramp_input else 5
            excl_input = input("    Years of exclusivity after launch (Enter for 10): ").strip()
            exclusivity_years = int(parse_number(excl_input)) if excl_input else 10
            # Cost by stage, each weighted by the chance of reaching it — a
            # Phase 1 drug's Phase 3 is only run about 15% of the time.
            stage_labels = {"preclinical": "preclinical work", "phase1": "Phase 1", "phase2": "Phase 2",
                            "phase3": "Phase 3", "launch": "filing + launch"}
            stages = model.remaining_stages(phase)
            if not stages:
                cost_input = input("    Remaining cost in dollars, charged now (Enter for 0): ").strip()
                dev_cost = parse_number(cost_input) if cost_input else 0.0
            else:
                stage_costs = {}
                for i, stage in enumerate(stages):
                    when = ("certain" if i == 0 and stage != "launch" else
                            "only if approved" if stage == "launch" else
                            f"only if {stage_labels[stages[i - 1]]} succeeds")
                    label = (f"Remaining development cost in {stage_labels[stage]}" if i == 0 and stage != "launch"
                             else f"Cost of {stage_labels[stage]}")
                    text = input(f"    {label}, in dollars ({when}; Enter for 0): ").strip()
                    stage_costs[stage] = parse_number(text) if text else 0.0
                dev_cost, rows = model.expected_development_cost(stage_costs, phase, prob_approval)
                if any(cost for _, cost, _ in rows):
                    print("    Expected cost: " + " + ".join(
                        f"{stage_labels[st]} ${c:,.0f} x {r:.0%}" for st, c, r in rows if c) + f" = ${dev_cost:,.0f}")
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
            pipeline_equity = pipeline_value + bridge_cash - bridge_debt
            pipeline_per_share = pipeline_equity / value_shares
            summary["pipeline_per_share"] = pipeline_per_share
            print(f"\n  Pipeline rNPV:        ${pipeline_value:,.0f}")
            print(f"  + cash - claims:      ${pipeline_equity:,.0f}   (cash and investments ${bridge_cash:,.0f}, "
                  f"debt and other claims ${bridge_debt:,.0f})")
            print(f"  Value per share:      ${pipeline_per_share:,.2f}   vs current price ${share_price:,.2f}")
            print("  Not included: corporate overhead and research not tied to any listed drug,")
            print("  and future dilution from raising cash to fund development — both lower")
            print("  this number. The option column values each drug's flexibility to wait for")
            print("  better information: well above rNPV means the drug's value rests on that")
            print("  flexibility; well below means it's clearly worth developing now, and the")
            print("  option model's 1/n cost of delay is penalizing the wait it assumes.")
            print_quarterly_annualization_warning(financials)

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
        # Damodaran's industry margin is lease- and R&D-adjusted, the same
        # basis as this EBIT, so it's the natural mature target to start from.
        industry_margin, margin_updated = industry_average(damodaran, "margins", mix)
        if industry_margin is not None:
            print(f"  Industry operating margin: {industry_margin:.1%}  ({' + '.join(n for n, _ in mix)}; Damodaran,")
            print(f"  {margin_updated}, lease & R&D adjusted — the same basis as the EBIT here).")
        print("  Pick the margin this business converges toward, not today's number.")
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
        elif industry_margin is not None:
            margin_input = input(f"  Target (mature) operating margin (e.g. 20 for 20%; Enter for the industry's "
                                 f"{industry_margin:.1%}): ").strip()
            default_target = industry_margin
        else:
            margin_input = input("  Target (mature) operating margin (e.g. 20 for 20%; Enter to hold today's "
                                  "margin flat): ").strip()
            default_target = current_margin
        target_margin = parse_number(margin_input) / 100 if margin_input else default_target
        narrative_years = 10
        convergence_input = input(f"  Year the margin reaches that target, then holds (Ginzu's year of "
                                  f"convergence;\n  e.g. 2 for fast cost cuts; Enter for year {narrative_years}): ").strip()
        margin_convergence_years = int(parse_number(convergence_input)) if convergence_input else None
        if margin_convergence_years is not None and not 1 <= margin_convergence_years <= narrative_years:
            print(f"  Must be 1-{narrative_years} — using year {narrative_years}.")
            margin_convergence_years = None
        trailing_s2c = revenue / invested_capital if invested_capital else None
        # Damodaran's industry ratio is the default: a company's own is
        # skewed by its acquisition history (Global Payments: 0.25, from
        # $46B of goodwill and acquired intangibles) or by the research
        # asset this tool capitalizes (ARM: 0.50), and is negative or
        # meaningless for a young firm.
        industry_s2c, s2c_date = industry_average(damodaran, "s2c", mix)
        print("  Sales-to-capital ratio: how much new revenue $1 of new capital buys (2.0 ="
              " $1 buys $2 of revenue).")
        # The company's own ratio is distorted when much of its capital is
        # acquisition goodwill (Global Payments: 0.25) or capitalized R&D
        # (ARM: 0.50); when neither is, a ratio well above the industry's is
        # the business model itself (Costco: 13.0 vs retail's 3.5, since
        # members and suppliers fund its growth) and is the better default.
        goodwill_val = (freshest_instant(facts, ["Goodwill"])[0] or 0.0) * (fx_rate if fx_currency else 1.0)
        intangibles_val = (freshest_instant(facts, ["IntangibleAssetsNetExcludingGoodwill"])[0] or 0.0) \
            * (fx_rate if fx_currency else 1.0)
        research_val = rd_result["research_asset"] if rd_result else 0.0
        distorted_share = ((goodwill_val + intangibles_val + research_val) / invested_capital
                           if invested_capital and invested_capital > 0 else 1.0)
        print(f"  This company's own trailing ratio (revenue / invested capital): " +
              (f"{trailing_s2c:.2f}" if trailing_s2c else "n/a") +
              f" — {distorted_share:.0%} of its invested capital is goodwill, intangibles or capitalized R&D.")
        own_is_clean = (trailing_s2c and industry_s2c and trailing_s2c > industry_s2c and 0 <= distorted_share < 0.25)
        if industry_s2c:
            print(f"  Industry: {industry_s2c:.2f}  ({' + '.join(n for n, _ in mix)}; Damodaran, {s2c_date};"
                  " goodwill in,")
            print("  R&D not capitalized — a little generous for an R&D-heavy company here).")
            default_s2c, default_label = industry_s2c, f"the industry's {industry_s2c:.2f}"
            # Revenue per dollar of operating capital (goodwill, intangibles and
            # capitalized R&D taken out): what growing the business itself costs.
            operating_capital = invested_capital - (goodwill_val + intangibles_val + research_val) \
                if invested_capital else 0
            operating_s2c = revenue / operating_capital if operating_capital > 0 else None
            if own_is_clean:
                print("  Its own ratio is above the industry's and its capital is mostly operating assets, so its")
                print("  own is the default: the business needs less capital to grow than its industry does.")
                default_s2c, default_label = trailing_s2c, f"its own {trailing_s2c:.2f}"
            elif operating_s2c and industry_s2c > 3 * operating_s2c:
                # The industry needs far less capital than this business does
                # (Viemed owns its rental equipment; Healthcare Support Services
                # is mostly asset-light services at 11.75), so the industry
                # would let it grow almost free.
                print(f"  The industry's ratio is over 3x this company's on its operating capital ({operating_s2c:.2f},")
                print("  goodwill, intangibles and R&D taken out): its business needs much more capital to grow,")
                print("  so that is the default.")
                default_s2c, default_label = operating_s2c, f"its operating-capital {operating_s2c:.2f}"
        elif trailing_s2c and trailing_s2c > 0:
            default_s2c, default_label = trailing_s2c, f"the trailing {trailing_s2c:.2f}"
        else:
            default_s2c, default_label = 1.0, "1.0"
        s2c_input = input(f"  Sales-to-capital ratio (e.g. 2 for 2.0; Enter for {default_label}): ").strip()
        sales_to_capital = parse_number(s2c_input) if s2c_input else default_s2c

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
        marginal_input = input(f"  Marginal tax rate once NOLs run out (e.g. 25 for 25%; Enter for "
                               f"{marginal_tax_rate:.1%}, the rate chosen above): ").strip()
        if marginal_input:
            marginal_tax_rate = parse_number(marginal_input) / 100

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
        base_margin_path = model.project_margin_path(current_margin, target_margin, narrative_years,
                                                    margin_convergence_years)
        base_ebit_path = model.project_ebit_path(base_revenue_path, base_margin_path)
        tax_rate_path = model.nol_tax_rate_path(base_ebit_path, nol_balance, marginal_tax_rate)

        narrative_path = model.project_narrative_path(
            revenue, initial_growth, current_margin, target_margin,
            tax_rate_path, sales_to_capital, terminal_growth, years=narrative_years,
            margin_convergence_years=margin_convergence_years)
        print(f"\n  {'Yr':>3} {'Revenue':>16} {'Margin':>8} {'EBIT':>16} {'Tax%':>7} {'WACC':>7} "
              f"{'Reinvest':>16} {'FCFF':>16}")
        for t in range(narrative_years):
            print(f"  {t + 1:>3} {narrative_path['revenue'][t]:>16,.0f} {narrative_path['margin'][t]:>7.1%} "
                  f"{narrative_path['ebit'][t]:>16,.0f} {tax_rate_path[t]:>6.1%} {wacc_path[t]:>6.1%} "
                  f"{narrative_path['reinvestment'][t]:>16,.0f} {narrative_path['fcff'][t]:>16,.0f}")

        narrative_range = model.run_valuation_range_narrative(
            revenue, initial_growth, current_margin, target_margin,
            tax_rate_path, sales_to_capital, (initial_wacc, discount_rate), terminal_growth,
            bridge_debt, bridge_cash, value_shares, years=narrative_years,
            return_on_new_capital=return_on_new_capital, margin_convergence_years=margin_convergence_years)
        print()
        for label in ("low", "base", "high"):
            r = narrative_range[label]
            print(f"  {label.upper():5s}: ${r['value_per_share']:>10,.2f} per share   "
                  f"(mature WACC={r['discount_rate']:.2%}, terminal growth={r['terminal_growth_rate']:.2%})")
        summary["narrative"] = {label: narrative_range[label]["value_per_share"] for label in ("low", "base", "high")}
        if narrative_range["ordering_warning"]:
            print(f"\n  ⚠ {narrative_range['ordering_warning']}")
        print_quarterly_annualization_warning(financials)

        # ---- Narrative sanity checks (Damodaran): is the story possible?
        # Diagnostics only — nothing here changes a value. ----
        print(f"\n  Sanity checks (base case)")
        narrative_breakdown = model.enterprise_value_narrative(
            revenue, initial_growth, current_margin, target_margin, tax_rate_path, sales_to_capital,
            wacc_path, terminal_growth, narrative_years, return_on_new_capital, return_breakdown=True,
            margin_convergence_years=margin_convergence_years)
        sanity = summary.setdefault("sanity", {})
        sanity["tv_share_narrative"] = print_terminal_share(narrative_breakdown, narrative_years)
        year10_revenue = narrative_path["revenue"][-1]
        print(f"  Revenue: ${revenue:,.0f} today -> ${year10_revenue:,.0f} in year {narrative_years} "
              f"({year10_revenue / revenue:.1f}x, {(year10_revenue / revenue) ** (1 / narrative_years) - 1:.1%}/yr)")
        roic_path = model.narrative_roic_path(invested_capital, narrative_path["reinvestment"],
                                              narrative_path["ebit"], tax_rate_path)
        roic_10, final_wacc = roic_path[-1], wacc_path[-1]
        industry_margin, margin_date = industry_average(damodaran, "margins", mix)
        industry_roic, roic_date = industry_average(damodaran, "roic", mix)
        margin_10 = narrative_path["margin"][-1]
        sanity.update(margin_year10=margin_10, roic_year10=roic_10)
        print(f"  Year-{narrative_years} operating margin: {margin_10:.1%}" +
              (f"   industry: {industry_margin:.1%} (Damodaran, {margin_date}, lease & R&D adjusted)"
               if industry_margin is not None else "   (no industry figure — industry not chosen)"))
        needs_moat_check = False
        if industry_margin is not None and industry_margin > 0 and margin_10 > 1.5 * industry_margin:
            print(f"    ⚠ {margin_10 / industry_margin:.1f}x the industry's margin — the story needs a lasting edge")
            needs_moat_check = True
        if roic_10 is None:
            print("  Year-10 return on capital: n/a (capital in place isn't positive)")
        else:
            print(f"  Year-{narrative_years} return on capital: {roic_10:.1%}   vs. WACC {final_wacc:.1%}" +
                  (f"   industry: {industry_roic:.1%} (Damodaran, lease & R&D adjusted)"
                   if industry_roic is not None else ""))
            if roic_10 < final_wacc:
                print("    ⚠ below the cost of capital — at this point growth is destroying value")
            elif industry_roic is not None and industry_roic > 0 and roic_10 > 2 * industry_roic:
                print(f"    ⚠ {roic_10 / industry_roic:.1f}x the industry's return — needs a durable competitive advantage")
                needs_moat_check = True
        if industry_margin is not None or industry_roic is not None:
            print("    (Industry figures are the latest year's, so a cyclical industry at a trough reads low.)")
        if needs_moat_check:
            print_moat_exemplars(mix)

        # ---- Failure-probability overlay: every DCF above (including
        # this narrative one) implicitly assumes the company SURVIVES to
        # realize the modeled value — WACC prices business/market risk,
        # not "this specific company might run out of cash before any of
        # this plays out," which is a real, separate risk for a young,
        # cash-burning company. Blend the going-concern value with a
        # distressed liquidation value, weighted by a probability of
        # failure. The default is Damodaran's first approach: the 10-year
        # default rate for the company's bond rating. ----
        print(f"\n{'='*60}")
        print("  Failure-probability overlay")
        print(f"{'='*60}")
        # Burn is measured from the cash-flow statement, not the DCF's FCFF:
        # FCFF here counts R&D as investment and leaves out working capital,
        # so it can show a burn for a company whose cash is actually growing
        # (Roblox: FCFF -$1.2B, cash flow after stock compensation positive).
        actual_cash_flow, cash_flow_note = reported_cash_flow(
            facts, financials.get("capex", {}).get("value") or 0.0, fx_currency, fx_rate)
        if actual_cash_flow is not None:
            print(f"  Cash flow: ${actual_cash_flow:,.0f} = {cash_flow_note}")
            current_burn_rate = -actual_cash_flow if actual_cash_flow < 0 else 0.0
        else:
            print("  No operating cash flow reported — using the DCF's base-year FCFF as the burn.")
            current_burn_rate = -fcff if fcff < 0 else 0.0
        runway = model.estimate_runway_years(cash, current_burn_rate)
        runway_display = f"{runway:.1f} years" if runway != float("inf") else "N/A (not burning cash)"
        print(f"  Cash: ${cash:,.0f}   Current annual burn rate: ${current_burn_rate:,.0f}   Runway: {runway_display}")
        # The synthetic rating only means something on positive operating
        # income: a money-loser always comes out D, and Damodaran's note on
        # that is not to use it for a going concern — use a real rating.
        rating, rating_source = None, None
        if actual_rating and model.failure_probability_from_rating(actual_rating) is not None:
            rating, rating_source = actual_rating, "actual rating, typed above"
        elif synthetic_rating and ebit > 0 and model.rating_bucket(synthetic_rating) not in (None, "D"):
            rating, rating_source = synthetic_rating, "synthetic rating from interest coverage"
        elif rating_asked:
            print("  Operating income isn't positive and no actual rating was given above, so there's no")
            print("  rating-implied default probability — type one below.")
        else:
            print("  Operating income isn't positive, so the synthetic rating (D) doesn't apply to a going")
            print("  concern. The company's actual bond rating (S&P or Moody's — in the 10-K's debt")
            print("  discussion or on the agency's site) gives the default probability instead.")
            while True:
                typed = input("  Actual bond rating (e.g. BB+ or Ba1), or Enter if it has none: ").strip()
                if not typed or model.failure_probability_from_rating(typed) is not None:
                    break
                print("    Couldn't read that as a going-concern rating (AAA down to C).")
            if typed:
                rating, rating_source = typed, "actual rating, typed"
        default_failure_prob = model.failure_probability_from_rating(rating) if rating else None
        if default_failure_prob is not None:
            print(f"  10-year default probability for {rating} ({rating_source}): {default_failure_prob:.2%}")
            print(f"  ({model.RATING_DEFAULT_SOURCE})")
        default_source = "rating-implied"
        if lead_phase is not None:
            # For a cash-burning biotech the bigger risk is usually the lead
            # drug failing in the clinic, not the runway itself — Damodaran
            # sets P(fail) off the clinical stage for exactly this case. A
            # company already funding itself survives a trial failure, so
            # there it's shown for reference only.
            clinical_failure_prob = 1.0 - model.PHASE_APPROVAL_PROBABILITY[lead_phase]
            if current_burn_rate > 0 or default_failure_prob is None:
                default_failure_prob = clinical_failure_prob
                default_source = "lead-drug clinical"
                print(f"  Lead drug is in {lead_phase}: clinical failure probability "
                      f"{clinical_failure_prob:.0%} (1 - P(approval)) — used as the default below,")
                print("  since a cash-burning company usually can't survive its lead drug failing.")
            else:
                print(f"  Lead drug is in {lead_phase} (clinical failure probability {clinical_failure_prob:.0%}),")
                print("  but this company isn't burning cash, so a trial failure wouldn't end it —")
                print("  keeping the rating-implied default.")
        if default_failure_prob is not None:
            probability_of_failure = prompt_number(
                f"  Probability of failure (e.g. 20 for 20%; Enter to use the {default_source} figure above): ",
                default=default_failure_prob * 100) / 100
        else:
            print("  No rating to base a default on. Damodaran's other published route is survival rates by")
            print("  company age and sector (Bureau of Labor Statistics), meant for young private firms.")
            probability_of_failure = prompt_number("  Probability of failure as a percent (e.g. 5 for 5%): ") / 100
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
        summary["narrative_failure_adjusted"] = dict(blended_values)
        blend_ordering_warning = model.ordering_warning_for_range(
            blended_values["low"], blended_values["base"], blended_values["high"])
        if blend_ordering_warning:
            print(f"\n  ⚠ {blend_ordering_warning}")
        print_quarterly_annualization_warning(financials)

    print("\n  This is a mechanical DCF estimate based on the last 10-K only, plus")
    print("  whatever WACC / terminal growth / qualitative overrides you entered")
    print("  above. It still won't catch anything you didn't think to override —")
    print("  use this as a starting anchor, then layer your own judgment on top,")
    print("  same as the uValue workflow you described.\n")

    if missing:
        print(f"  ⚠ Note: {', '.join(missing)} had to be entered manually — EDGAR")
        print("    didn't have them under any tag this script currently checks.\n")
    return summary


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
        "--financial", action="store_true",
        help="Bank / brokerage mode: values the equity with Damodaran's excess return model "
             "(book equity + PV of ROE above the cost of equity) instead of the FCFF DCF, "
             "whose debt, EBIT and reinvestment don't mean anything for a financial firm.",
    )
    parser.add_argument(
        "--project", action="store_true",
        help="Pre-production mode (a mine or plant still being built — Lithium Americas, USA Rare "
             "Earth): values the project from the technical report's timing, capex, output, "
             "price and margin, with a failure probability, instead of a DCF of today's operations.",
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

    run_full_valuation(args.ticker, args.stage, args.narrative, args.biotech, args.reit, args.cyclical,
                       args.financial, args.project)
