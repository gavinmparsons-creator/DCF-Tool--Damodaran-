"""
model.py — Standard WACC/FCFF DCF valuation, Damodaran-style.

This file has NO network calls. It's pure math, so it can be tested
against known numbers independently of whether EDGAR/market data
fetching is working. Run this file directly to see it validate itself
against a worked example.

CORE APPROACH (matches uValue's "WACC / cost of capital" model):
    1. Compute unlevered Free Cash Flow to the Firm (FCFF) for the most
       recent year:
           FCFF = EBIT * (1 - tax rate) + D&A - CapEx - change in
                  working capital
       (this script omits working capital changes by default since
       EDGAR doesn't cleanly expose them — see note in compute_fcff)
    2. Compute WACC:
           WACC = (E/V) * cost_of_equity + (D/V) * cost_of_debt * (1-tax)
           cost_of_equity = risk_free_rate + beta * equity_risk_premium
    3. Project FCFF forward for N years at a growth rate, discount each
       year back at WACC
    4. Compute terminal value at the end of the projection using the
       Gordon Growth formula, discount it back too
    5. Sum discounted FCFFs + discounted terminal value = Enterprise
       Value
    6. Equity Value = Enterprise Value - Debt + Cash
    7. Value per share = Equity Value / shares outstanding
"""

import math


def cost_of_equity(risk_free_rate, beta, equity_risk_premium):
    """CAPM: cost of equity = Rf + beta * ERP"""
    return risk_free_rate + beta * equity_risk_premium


def unlever_beta(levered_beta, tax_rate, debt_to_equity):
    """
    Strip out the effect of a company's own leverage from its observed
    (levered/regression) beta, using the standard Hamada formula:

        Unlevered beta = Levered beta / (1 + (1 - tax rate) * D/E)

    This is the "bottom-up beta" starting point Damodaran teaches:
    unlever several comparable companies' betas, average them (removing
    each company's own idiosyncratic leverage), then relever that
    average to the SPECIFIC company you're valuing using ITS OWN D/E.
    This is more reliable than trusting one company's raw historical
    regression beta, which is noisy and reflects a leverage level that
    may not even be current.
    """
    denominator = 1 + (1 - tax_rate) * debt_to_equity
    return levered_beta / denominator


def relever_beta(unlevered_beta, tax_rate, debt_to_equity):
    """
    Apply a target company's own D/E to an unlevered (asset) beta,
    producing a "relevered" beta appropriate for that company's actual
    capital structure. Inverse of unlever_beta.
    """
    return unlevered_beta * (1 + (1 - tax_rate) * debt_to_equity)


def weighted_unlevered_beta(betas_and_weights):
    """
    Damodaran's bottom-up beta for a company in more than one business:
    the average of each business's industry unlevered beta, weighted by
    how much of the company each business is (revenue share is the usual
    proxy; operating income or estimated value are better when known).

    betas_and_weights: list of (unlevered_beta, weight) pairs. Weights
    don't need to sum to 1 — they're normalized here, so revenue in
    dollars or percentages both work.
    """
    total_weight = sum(w for _, w in betas_and_weights)
    if not betas_and_weights or total_weight <= 0:
        raise ValueError("need at least one business with a positive weight")
    return sum(b * w for b, w in betas_and_weights) / total_weight


def compute_reinvestment_rate(capex, d_and_a, change_in_nwc, nopat):
    """
    Reinvestment rate = (CapEx - D&A + change in NWC) / NOPAT

    This is the fraction of after-tax operating profit being plowed
    back into the business (net of the depreciation that's already
    "recovering" old investment). Damodaran's framework ties growth
    directly to this number rather than treating growth as a free
    assumption — see implied_growth_rate below.

    Can occasionally come out negative (e.g. D&A exceeds CapEx in a
    declining-asset-base year) or above 100% (heavy investment phase)
    — both are real and meaningful, not bugs.
    """
    if nopat == 0:
        raise ValueError("NOPAT cannot be zero when computing reinvestment rate")
    net_capex = capex - d_and_a
    return (net_capex + change_in_nwc) / nopat


def compute_roic(nopat, invested_capital):
    """
    Return on Invested Capital = NOPAT / Invested Capital
    Invested Capital is commonly approximated as: Total Debt + Book
    Value of Equity - Cash (i.e., the capital actually deployed in
    operations, excluding excess cash sitting on the balance sheet).
    """
    if invested_capital == 0:
        raise ValueError("Invested capital cannot be zero when computing ROIC")
    return nopat / invested_capital


def implied_growth_rate(reinvestment_rate, roic):
    """
    Damodaran's fundamental growth formula:
        Expected growth = Reinvestment Rate * ROIC

    This ties the FCFF growth assumption to how much the company is
    actually reinvesting and how well it's historically converted that
    reinvestment into returns — instead of picking a growth number out
    of thin air. A company reinvesting little, or earning a low return
    on what it reinvests, should not be modeled as a fast grower no
    matter how a plain default assumption might treat it.
    """
    return reinvestment_rate * roic


def capitalize_rd(rd_by_year, amortizable_life):
    """
    Damodaran's R&D capitalization adjustment.

    Accounting expenses R&D as it's spent, even though for an R&D-driven
    company (software, pharma, semis) it's economically an investment in
    future earning power — no different from capex, just expensed
    instead of capitalized. Left as-is, this understates EBIT (R&D is a
    real investment, not a cost of this year's operations), understates
    invested capital (none of the cumulative research spend shows up as
    an asset), and overstates the reinvestment rate's blind spot (R&D
    spending doesn't count as "reinvestment" at all). For an R&D-heavy
    company with ordinary or negative reported EBIT, this is usually the
    actual reason the FCFF DCF looks broken — not distress, not a data
    problem, just R&D sitting in the wrong line item.

    The fix: treat each year's R&D as if it were capitalized and
    amortized straight-line over `amortizable_life` years (Damodaran's
    typical defaults: ~2yr consumer/retail, ~3yr tech/software, ~5yr
    industrial, ~10yr pharma/biotech — industry judgment, not something
    this function guesses). At any point, the "research asset" is the
    sum of each past year's R&D times the fraction of its amortizable
    life not yet used up; this year's amortization is the sum of each
    PRIOR year's R&D divided by the amortizable life (this year's own
    R&D hasn't started amortizing yet).

    rd_by_year: annual R&D expense, OLDEST FIRST, most recent year LAST.
    Shorter than amortizable_life is fine (young company without that
    much history) — missing older years are treated as $0, which
    understates the research asset for a young company, which is
    correct: it hasn't had time to build one up yet.

    Returns a dict:
        current_rd         — this year's R&D expense (the cash still
                              actually went out the door; see
                              ebit_adjustment's docstring note on FCFF)
        amortization        — this year's amortization of PRIOR years'
                              capitalized R&D
        research_asset      — cumulative unamortized research asset,
                              to be added to invested capital
        ebit_adjustment      — current_rd - amortization; add this to
                              reported EBIT to get R&D-adjusted EBIT
        years_of_data        — how many years of R&D history went in
                              (less than amortizable_life means the
                              research asset is understated by padding)

    Note on FCFF: capitalizing R&D doesn't change the cash that actually
    left the business — it only reclassifies an expense as an
    investment. The consistent way to carry this into compute_fcff is
    d_and_a -> d_and_a + amortization and capex -> capex + current_rd,
    alongside the adjusted EBIT; the net effect on FCFF is a small tax
    adjustment (ebit_adjustment * tax_rate), not a swing from negative
    to positive — the big swings land in EBIT, ROIC, and the
    reinvestment rate instead, which is the whole point.
    """
    n = amortizable_life
    if not (1 <= n <= 30):
        # Defense in depth: valuation.py's prompt already rejects anything
        # outside this range, but this function must never trust a caller
        # to have validated first — an unchecked n in the millions/billions
        # would try to pad `recent` to that length below and hang the
        # process. 30 years comfortably covers every real-world amortizable
        # life Damodaran's method uses (even the long pharma/biotech end).
        raise ValueError(f"amortizable_life must be between 1 and 30 years, got {n}")
    recent = list(rd_by_year[-n:])
    if len(recent) < n:
        recent = [0.0] * (n - len(recent)) + recent  # pad missing older years with $0

    current_rd = recent[-1]
    research_asset = 0.0
    amortization = 0.0
    for i, rd_value in enumerate(recent):
        years_ago = (n - 1) - i  # last entry (i=n-1) is the current year -> years_ago=0
        unamortized_fraction = (n - years_ago) / n
        research_asset += rd_value * unamortized_fraction
        if years_ago >= 1:
            amortization += rd_value / n

    return {
        "current_rd": current_rd,
        "amortization": amortization,
        "research_asset": research_asset,
        "ebit_adjustment": current_rd - amortization,
        "years_of_data": len(rd_by_year[-n:]),
    }


# Damodaran's "synthetic rating" tables: map a company's interest coverage
# ratio (EBIT / interest expense) to an implied bond rating and default
# spread, for companies that don't have an actual traded bond rating (or
# as a sanity check against one they do have). Two tables, because at the
# SAME coverage ratio a larger company is viewed as less likely to
# default than a smaller one — a small/thin-traded firm needs a notably
# higher coverage ratio to earn the same rating as a large-cap peer.
#
# Each entry is (minimum interest coverage ratio for this bucket, rating
# label, default spread over the risk-free rate). Checked in descending
# order of minimum coverage; a company falls into the first (highest)
# bucket its coverage ratio clears.
#
# The breakpoint STRUCTURE (more buckets, tighter small-firm breakpoints)
# follows Damodaran's documented methodology. The SPREAD values below are
# illustrative, not a transcription of his current published table — he
# republishes updated spreads periodically on his website as credit
# markets move, and this script has no live feed for that. Treat the
# resulting cost of debt as a reasonable mechanical estimate, and use the
# override hook in valuation.py to substitute current spread data (or a
# company's own actual bond yield) when precision matters.
LARGE_FIRM_RATING_TABLE = [
    (8.50, "AAA", 0.0069),
    (6.50, "AA", 0.0085),
    (5.50, "A+", 0.0100),
    (4.25, "A", 0.0120),
    (3.00, "A-", 0.0140),
    (2.50, "BBB", 0.0190),
    (2.25, "BB+", 0.0280),
    (2.00, "BB", 0.0320),
    (1.75, "B+", 0.0420),
    (1.50, "B", 0.0500),
    (1.25, "B-", 0.0620),
    (0.80, "CCC", 0.0900),
    (0.65, "CC", 0.1100),
    (0.20, "C", 0.1300),
    (float("-inf"), "D", 0.1500),
]

SMALL_FIRM_RATING_TABLE = [
    (12.50, "AAA", 0.0069),
    (9.50, "AA", 0.0085),
    (7.50, "A+", 0.0100),
    (6.00, "A", 0.0120),
    (4.50, "A-", 0.0140),
    (4.00, "BBB", 0.0190),
    (3.50, "BB+", 0.0280),
    (3.00, "BB", 0.0320),
    (2.50, "B+", 0.0420),
    (2.00, "B", 0.0500),
    (1.50, "B-", 0.0620),
    (1.25, "CCC", 0.0900),
    (0.80, "CC", 0.1100),
    (0.50, "C", 0.1300),
    (float("-inf"), "D", 0.1500),
]


def synthetic_credit_rating(interest_coverage_ratio, large_firm=True, spreads=None):
    """
    Map an interest coverage ratio to (rating_label, default_spread)
    using the large- or small-firm table above. A negative or very low
    ratio (EBIT doesn't cover interest expense at all) correctly falls
    through to the "D" bucket rather than erroring — that's a real,
    valid answer (distressed/defaulted-grade), not an edge case to guard
    against.

    `spreads` ({rating: spread}, e.g. Damodaran's current published
    table) replaces the illustrative spreads above for any rating it
    covers. The coverage breakpoints still come from the tables above —
    a spread depends on the rating, not the company's size, so one
    published spread table serves both the large- and small-firm
    breakpoints.
    """
    table = LARGE_FIRM_RATING_TABLE if large_firm else SMALL_FIRM_RATING_TABLE
    for min_coverage, rating, spread in table:
        if interest_coverage_ratio >= min_coverage:
            return rating, (spreads or {}).get(rating, spread)
    return table[-1][1], (spreads or {}).get(table[-1][1], table[-1][2])  # unreachable but safe


def synthetic_cost_of_debt(ebit, interest_expense, risk_free_rate, large_firm=True, spreads=None):
    """
    Damodaran's synthetic rating method, end to end: interest coverage
    ratio = EBIT / interest expense, mapped to a bond rating and default
    spread (see synthetic_credit_rating), then cost of debt = risk-free
    rate + that spread. This replaces guessing at a flat cost of debt
    with a number tied to how comfortably THIS company's own operating
    earnings cover its own interest payments — a thinly-covered,
    heavily-levered company (e.g. Charter, Sirius XM) gets a materially
    higher cost of debt than a comfortably-covered one, instead of both
    getting the same flat assumption.

    Returns (cost_of_debt, interest_coverage_ratio, rating, default_spread).
    If interest_expense is zero or negative (no debt, or a data oddity),
    there's no coverage ratio to compute at all — returns
    (None, None, None, None) so the caller can fall back to a default
    cost of debt instead of dividing by zero or a negative number.
    """
    if interest_expense <= 0:
        return None, None, None, None
    interest_coverage_ratio = ebit / interest_expense
    rating, spread = synthetic_credit_rating(interest_coverage_ratio, large_firm, spreads)
    return risk_free_rate + spread, interest_coverage_ratio, rating, spread


def wacc(equity_value, debt_value, cost_of_equity_pct, cost_of_debt_pct, tax_rate):
    """
    Weighted average cost of capital.
    equity_value / debt_value: market values (equity = market cap, debt = book value of debt is a common proxy)
    """
    total = equity_value + debt_value
    if total == 0:
        raise ValueError("Equity + debt value cannot be zero")
    e_weight = equity_value / total
    d_weight = debt_value / total
    after_tax_cost_of_debt = cost_of_debt_pct * (1 - tax_rate)
    return e_weight * cost_of_equity_pct + d_weight * after_tax_cost_of_debt


def compute_fcff(ebit, tax_rate, d_and_a, capex, change_in_nwc=0):
    """
    Unlevered Free Cash Flow to the Firm.
    NOTE: change_in_nwc (net working capital) defaults to 0 because it's
    not cleanly available from a single EDGAR tag — pulling it requires
    computing the delta in current assets minus current liabilities
    year over year. For a first-pass valuation this is an acceptable
    simplification; add it manually for a more precise number if you
    have the balance sheet handy.
    """
    nopat = ebit * (1 - tax_rate)
    return nopat + d_and_a - capex - change_in_nwc


def project_fcff(base_fcff, growth_rate, years):
    """Project FCFF forward at a constant growth rate for N years."""
    return [base_fcff * ((1 + growth_rate) ** t) for t in range(1, years + 1)]


def discount_cash_flows(cash_flows, discount_rate):
    """Discount a list of future cash flows (year 1, year 2, ...) back to present value."""
    return [cf / ((1 + discount_rate) ** (t + 1)) for t, cf in enumerate(cash_flows)]


def terminal_value(final_year_fcff, terminal_growth_rate, discount_rate):
    """Gordon Growth terminal value at the END of the projection period (not yet discounted to today)."""
    if discount_rate <= terminal_growth_rate:
        raise ValueError("Discount rate must exceed terminal growth rate (Gordon Growth requires this)")
    return final_year_fcff * (1 + terminal_growth_rate) / (discount_rate - terminal_growth_rate)


def stable_growth_fcff(final_year_nopat, terminal_growth_rate, return_on_new_capital):
    """
    FCFF in the first stable-growth year, Damodaran-style: growth isn't
    free, so a company growing at g forever must reinvest g / RONIC of its
    after-tax operating income every year, where RONIC is the return it
    earns on that new capital:

        FCFF(n+1) = NOPAT(n) x (1 + g) x (1 - g / RONIC)

    A negative g gives a negative reinvestment rate — capital released as
    the business shrinks — which is left as is.
    """
    if return_on_new_capital <= 0:
        raise ValueError("return_on_new_capital must be positive")
    reinvestment_rate = terminal_growth_rate / return_on_new_capital
    return final_year_nopat * (1 + terminal_growth_rate) * (1 - reinvestment_rate)


def terminal_value_consistent(final_year_nopat, terminal_growth_rate, discount_rate,
                              return_on_new_capital=None):
    """
    Gordon Growth terminal value with reinvestment consistent with growth
    (see stable_growth_fcff), instead of growing whatever the last
    forecast year's FCFF happened to be. The plain terminal_value() above
    silently assumes the final year's reinvestment rate holds forever —
    too much reinvestment for a company still in a heavy-investment year,
    too little (free growth) for one whose final year happens to be light
    — and the terminal value is usually most of the total.

    return_on_new_capital defaults to the discount rate: Damodaran's
    standard assumption that competition drives excess returns on NEW
    investment to zero in perpetuity, which also makes the value
    insensitive to the growth rate chosen (growth that earns only its
    cost of capital adds nothing). Pass a higher figure for a durable
    competitive advantage.
    """
    if discount_rate <= terminal_growth_rate:
        raise ValueError("Discount rate must exceed terminal growth rate (Gordon Growth requires this)")
    ronic = discount_rate if return_on_new_capital is None else return_on_new_capital
    return stable_growth_fcff(final_year_nopat, terminal_growth_rate, ronic) / (discount_rate - terminal_growth_rate)


def enterprise_value(base_fcff, growth_rate, discount_rate, terminal_growth_rate, years=5):
    """
    Full DCF: project FCFF, discount it, add discounted terminal value.
    Returns enterprise value (before subtracting debt / adding cash).
    """
    projected = project_fcff(base_fcff, growth_rate, years)
    discounted = discount_cash_flows(projected, discount_rate)
    pv_of_explicit_period = sum(discounted)

    tv = terminal_value(projected[-1], terminal_growth_rate, discount_rate)
    pv_of_terminal_value = tv / ((1 + discount_rate) ** years)

    return pv_of_explicit_period + pv_of_terminal_value


def equity_value_per_share(ev, total_debt, cash, shares_outstanding):
    """Bridge from Enterprise Value to per-share equity value."""
    equity_val = ev - total_debt + cash
    return equity_val / shares_outstanding, equity_val


def ev_revenue_valuation(revenue, multiple, total_debt, cash, shares_outstanding):
    """
    Rule-of-thumb fallback for companies where the FCFF-based DCF breaks
    down (negative base-year FCFF — a high-growth company still running
    a GAAP operating loss while investing heavily in capex, or a
    genuinely distressed one; see the caller's negative-FCFF warning).

    EV/Revenue is the standard quick-and-dirty multiple for unprofitable
    high-growth SaaS/tech, since it still works when EBITDA/EBIT/FCFF are
    negative or near-zero. The multiple itself is not computed here —
    it has to come from real comparable-company data (no free, reliable
    API source for peer multiples exists in this script), so the caller
    supplies it rather than this function inventing one.

    Returns (value_per_share, equity_value), same shape as
    equity_value_per_share, using the same EV -> equity bridge.
    """
    ev = revenue * multiple
    return equity_value_per_share(ev, total_debt, cash, shares_outstanding)


def run_valuation_range(base_fcff, discount_rate, terminal_growth_rate,
                         total_debt, cash, shares_outstanding,
                         growth_rate=0.0, years=5,
                         wacc_spread=0.01, growth_spread=0.005):
    """
    Run the DCF three times — low/base/high — by flexing WACC and
    terminal growth rate slightly in opposite directions. This produces
    a defensible range instead of a single point estimate.

    LOW case:  higher discount rate, lower terminal growth (more conservative)
    HIGH case: lower discount rate, higher terminal growth (more optimistic)
    """
    scenarios = {
        "low":  (discount_rate + wacc_spread, terminal_growth_rate - growth_spread),
        "base": (discount_rate, terminal_growth_rate),
        "high": (discount_rate - wacc_spread, terminal_growth_rate + growth_spread),
    }

    results = {}
    for label, (dr, tgr) in scenarios.items():
        ev = enterprise_value(base_fcff, growth_rate, dr, tgr, years)
        per_share, equity_val = equity_value_per_share(ev, total_debt, cash, shares_outstanding)
        results[label] = {
            "discount_rate": dr,
            "terminal_growth_rate": tgr,
            "enterprise_value": ev,
            "equity_value": equity_val,
            "value_per_share": per_share,
        }
    return results


def project_fcff_multi_stage(base_fcff, high_growth_rate, high_growth_years,
                              fade_years, terminal_growth_rate):
    """
    Three-stage FCFF projection (Damodaran-style life-cycle model):

      Stage 1 — HIGH GROWTH ("young company"): base_fcff compounds at a
        constant high_growth_rate for high_growth_years. A flat single-
        stage DCF implicitly assumes EVERY company is already in this
        stage, then falls off a cliff straight to terminal growth — fine
        for a mature company, wrong for anything still compounding.

      Stage 2 — FADE ("growing/maturing company"): growth declines
        linearly from high_growth_rate down to terminal_growth_rate over
        fade_years, as competitors erode excess returns and growth
        converges toward the economy's long-run rate. This is the piece
        a flat single-stage DCF skips entirely.

      Stage 3 — MATURE ("stable company"): implicit, not returned here —
        the caller discounts a Gordon Growth terminal value off the last
        projected year using terminal_growth_rate (see
        enterprise_value_multi_stage).

    high_growth_years=N, fade_years=0 reduces this to exactly
    project_fcff(base_fcff, high_growth_rate, N) — the old flat-growth
    model is just the "mature, no runway" special case of this one.

    Returns a flat list of projected FCFF values, oldest (year 1) first.
    """
    projected = []
    fcff = base_fcff
    for _ in range(high_growth_years):
        fcff = fcff * (1 + high_growth_rate)
        projected.append(fcff)
    for t in range(1, fade_years + 1):
        g = high_growth_rate + (terminal_growth_rate - high_growth_rate) * (t / fade_years)
        fcff = fcff * (1 + g)
        projected.append(fcff)
    return projected


def enterprise_value_multi_stage(base_fcff, high_growth_rate, high_growth_years,
                                  fade_years, terminal_growth_rate, discount_rate,
                                  base_nopat=None, return_on_new_capital=None, return_breakdown=False,
                                  base_revenue=None, working_capital_ratio=0.0):
    """
    Full three-stage DCF: project FCFF through the high-growth and fade
    stages, discount each year back, add the discounted Gordon Growth
    terminal value. Returns enterprise value (before subtracting debt /
    adding cash).

    With base_nopat given, the terminal value is reinvestment-consistent
    (terminal_value_consistent): NOPAT is grown along the same growth path
    as FCFF, and stable-growth FCFF is rebuilt from it with reinvestment
    = g / return_on_new_capital (default: the discount rate). Without it,
    the final projected FCFF is grown as is (the older behavior).

    return_breakdown=True returns {"ev", "pv_explicit", "pv_terminal"}
    instead of the bare EV — for the terminal-value-share sanity check.

    Working capital (Damodaran): with base_revenue and a non-cash
    working-capital ratio (% of revenue) given, each explicit year's FCFF
    is reduced by ratio x that year's revenue increase — revenue grows
    along the same path as FCFF. A negative ratio (paid before
    delivering) makes growth release cash. The consistent terminal value
    needs no separate term: its reinvestment = g / RONIC covers all
    reinvestment, working capital included.
    """
    projected = project_fcff_multi_stage(base_fcff, high_growth_rate, high_growth_years,
                                          fade_years, terminal_growth_rate)
    if not projected:
        raise ValueError("high_growth_years + fade_years must be at least 1")
    if base_revenue and working_capital_ratio:
        revenue_path = [base_revenue] + project_fcff_multi_stage(base_revenue, high_growth_rate, high_growth_years,
                                                                 fade_years, terminal_growth_rate)
        projected = [fcff - working_capital_ratio * (revenue_path[t + 1] - revenue_path[t])
                     for t, fcff in enumerate(projected)]

    discounted = discount_cash_flows(projected, discount_rate)
    pv_of_explicit_period = sum(discounted)

    total_years = len(projected)
    if base_nopat is not None:
        growth_factor = project_fcff_multi_stage(1.0, high_growth_rate, high_growth_years,
                                                 fade_years, terminal_growth_rate)[-1]
        tv = terminal_value_consistent(base_nopat * growth_factor, terminal_growth_rate, discount_rate,
                                       return_on_new_capital)
    else:
        tv = terminal_value(projected[-1], terminal_growth_rate, discount_rate)
    pv_of_terminal_value = tv / ((1 + discount_rate) ** total_years)

    if return_breakdown:
        return {"ev": pv_of_explicit_period + pv_of_terminal_value,
                "pv_explicit": pv_of_explicit_period, "pv_terminal": pv_of_terminal_value}
    return pv_of_explicit_period + pv_of_terminal_value


def run_valuation_range_multi_stage(base_fcff, high_growth_rate, high_growth_years,
                                     fade_years, discount_rate, terminal_growth_rate,
                                     total_debt, cash, shares_outstanding,
                                     wacc_spread=0.01, growth_spread=0.005,
                                     base_nopat=None, return_on_new_capital=None,
                                     base_revenue=None, working_capital_ratio=0.0):
    """
    Same low/base/high sensitivity range as run_valuation_range, but off
    the three-stage (high growth -> fade -> terminal) FCFF projection
    instead of one flat growth rate for the whole explicit period.
    base_nopat / return_on_new_capital / base_revenue / working_capital_ratio:
    see enterprise_value_multi_stage (a None return_on_new_capital means
    each scenario's own WACC).
    """
    scenarios = {
        "low":  (discount_rate + wacc_spread, terminal_growth_rate - growth_spread),
        "base": (discount_rate, terminal_growth_rate),
        "high": (discount_rate - wacc_spread, terminal_growth_rate + growth_spread),
    }

    results = {}
    for label, (dr, tgr) in scenarios.items():
        ev = enterprise_value_multi_stage(base_fcff, high_growth_rate, high_growth_years,
                                           fade_years, tgr, dr, base_nopat, return_on_new_capital,
                                           base_revenue=base_revenue, working_capital_ratio=working_capital_ratio)
        per_share, equity_val = equity_value_per_share(ev, total_debt, cash, shares_outstanding)
        results[label] = {
            "discount_rate": dr,
            "terminal_growth_rate": tgr,
            "enterprise_value": ev,
            "equity_value": equity_val,
            "value_per_share": per_share,
        }
    return results


def solve_implied_growth_rate(target_price, base_fcff, high_growth_years, fade_years,
                               discount_rate, terminal_growth_rate,
                               total_debt, cash, shares_outstanding,
                               lo=-0.95, hi=5.0, iterations=200,
                               base_nopat=None, return_on_new_capital=None,
                               base_revenue=None, working_capital_ratio=0.0):
    """
    Binary-search for the high-growth-stage rate that makes the per-share
    DCF value equal target_price, holding WACC, terminal growth, stage
    duration, and the balance-sheet inputs fixed. Answers "what growth
    rate is the market actually pricing in?" directly, instead of just
    juxtaposing the mechanical reinvestment-rate-derived growth next to
    a valuation gap and leaving the reader to guess what would close it.

    Only meaningful when base_fcff is positive: growth rate compounds
    whatever sign the base cash flow already has, so a negative base
    FCFF can never be grown into a positive valuation (see enterprise
    value tables run for Samsara) — there is no solution in that case,
    and this returns None rather than a misleading number. Also returns
    None if the target price falls outside what's reachable within
    [lo, hi] (e.g. requires negative-infinity or implausibly explosive
    growth), rather than silently returning a boundary value.
    """
    if base_fcff <= 0:
        return None

    def price_for_growth(g):
        ev = enterprise_value_multi_stage(base_fcff, g, high_growth_years, fade_years,
                                           terminal_growth_rate, discount_rate,
                                           base_nopat, return_on_new_capital,
                                           base_revenue=base_revenue, working_capital_ratio=working_capital_ratio)
        per_share, _ = equity_value_per_share(ev, total_debt, cash, shares_outstanding)
        return per_share

    lo_price, hi_price = price_for_growth(lo), price_for_growth(hi)
    if not (lo_price <= target_price <= hi_price):
        return None

    for _ in range(iterations):
        mid = (lo + hi) / 2
        if price_for_growth(mid) < target_price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def dividend_discount_value_per_share(base_dividend_per_share, growth_rate, high_growth_years,
                                       fade_years, terminal_growth_rate, cost_of_equity):
    """
    Dividend Discount Model — the right tool for a capital-intensive,
    regulated business (a utility, a REIT) where nearly all earnings get
    reinvested into a growing rate base rather than showing up as
    discretionary FCFF. A company like that can have reinvestment rate
    near 100% (routine and expected, not distress or a data problem —
    see trailing_normalized_ebit's docstring), which drives base-year
    FCFF toward zero and breaks the unlevered-FCFF DCF even though the
    business is healthy. Valuing the dividend actually paid to
    shareholders sidesteps that entirely.

    Discounts at the COST OF EQUITY, not WACC — a dividend is a cash
    flow to equity holders specifically, already net of debt service, so
    there's no enterprise-value-to-equity bridge needed (no debt/cash
    adjustment, unlike the FCFF-based functions above). Reuses the same
    three-stage (high growth -> fade -> terminal) projection shape as
    the FCFF model, since a dividend stream compounds the same way any
    other cash flow does.
    """
    projected = project_fcff_multi_stage(base_dividend_per_share, growth_rate,
                                          high_growth_years, fade_years, terminal_growth_rate)
    if not projected:
        raise ValueError("high_growth_years + fade_years must be at least 1")

    discounted = discount_cash_flows(projected, cost_of_equity)
    pv_of_explicit_period = sum(discounted)

    total_years = len(projected)
    tv = terminal_value(projected[-1], terminal_growth_rate, cost_of_equity)
    pv_of_terminal_value = tv / ((1 + cost_of_equity) ** total_years)

    return pv_of_explicit_period + pv_of_terminal_value


def run_dividend_discount_range(base_dividend_per_share, growth_rate, high_growth_years, fade_years,
                                 cost_of_equity, terminal_growth_rate,
                                 coe_spread=0.01, growth_spread=0.005):
    """
    Same low/base/high sensitivity range as run_valuation_range_multi_stage,
    but for the dividend discount model: flexes cost of equity and
    terminal growth instead of WACC and terminal growth.
    """
    scenarios = {
        "low":  (cost_of_equity + coe_spread, terminal_growth_rate - growth_spread),
        "base": (cost_of_equity, terminal_growth_rate),
        "high": (cost_of_equity - coe_spread, terminal_growth_rate + growth_spread),
    }
    results = {}
    for label, (coe, tgr) in scenarios.items():
        value = dividend_discount_value_per_share(base_dividend_per_share, growth_rate,
                                                    high_growth_years, fade_years, tgr, coe)
        results[label] = {
            "cost_of_equity": coe,
            "terminal_growth_rate": tgr,
            "value_per_share": value,
        }
    return results


# ---------------------------------------------------------------------
# NARRATIVE DCF ("Ginzu"-style, Damodaran's actual template): revenue,
# margin, and reinvestment are forecast as three SEPARATE year-by-year
# paths instead of compounding one lump FCFF number at a single
# mechanical growth rate (reinvestment rate x ROIC). That single-number
# approach is still the right quick tool for a stable, mature company —
# this is the one built for a young/story-driven company, where "how
# fast does revenue grow" and "what margin does this business mature
# into" are genuinely different questions with different answers.
# ---------------------------------------------------------------------

def project_revenue_path(current_revenue, initial_growth_rate, terminal_growth_rate, years=10):
    """
    Revenue growth fades LINEARLY from initial_growth_rate (year 1) down
    to terminal_growth_rate (the final year) over the forecast window —
    Damodaran's standard discipline that a young company's growth rate
    can't stay elevated forever; it has to converge toward something
    close to the risk-free/stable-economy rate by year 10, since nothing
    can outgrow the overall economy in perpetuity (that's what
    terminal_growth_rate already represents everywhere else in this
    file). Returns a list of revenue LEVELS (not growth rates), oldest
    (year 1) first, length `years`.
    """
    if years < 1:
        raise ValueError("years must be at least 1")
    if current_revenue <= 0:
        raise ValueError("current_revenue must be positive")
    revenues = []
    rev = current_revenue
    for t in range(1, years + 1):
        g = (terminal_growth_rate if years == 1
             else initial_growth_rate + (terminal_growth_rate - initial_growth_rate) * ((t - 1) / (years - 1)))
        rev = rev * (1 + g)
        revenues.append(rev)
    return revenues


def project_margin_path(current_margin, target_margin, years=10):
    """
    Operating margin converges LINEARLY from current_margin (today's —
    possibly negative, possibly cyclically depressed or elevated) to
    target_margin (a mature/peer-average margin you supply) by the final
    forecast year. Per Damodaran, this is the single most important
    input in the whole narrative DCF — it's worth more scrutiny than any
    other assumption here. Returns a list of margins (decimals, e.g.
    0.20 for 20%), oldest (year 1) first, length `years`.
    """
    if years < 1:
        raise ValueError("years must be at least 1")
    return [current_margin + (target_margin - current_margin) * (t / years) for t in range(1, years + 1)]


def project_wacc_path(initial_wacc, mature_wacc, years=10):
    """
    Cost of capital fades LINEARLY from initial_wacc (year 1 — a young,
    undiversified company's elevated cost of capital; Damodaran's rough
    shortcut is something like the 90th percentile of all firms) down
    to mature_wacc (the final year — just the normal CAPM/cost-of-debt
    WACC this script already computes, used flat everywhere else in
    this file) over the forecast window. Same fade shape and same
    year-1-exact / year-N-exact convention as project_revenue_path.

    Returns a list of discount rates, oldest (year 1) first, length
    `years`.
    """
    if years < 1:
        raise ValueError("years must be at least 1")
    return [initial_wacc if years == 1
            else initial_wacc + (mature_wacc - initial_wacc) * ((t - 1) / (years - 1))
            for t in range(1, years + 1)]


def discount_cash_flows_variable_rate(cash_flows, wacc_path):
    """
    Discount a list of future cash flows back to present value using a
    DIFFERENT rate each year, instead of one flat rate for the whole
    period (see discount_cash_flows). Year t's cash flow is discounted
    by the CUMULATIVE PRODUCT of every year's rate up through t —
    (1+wacc_1) x (1+wacc_2) x ... x (1+wacc_t) — NOT (1+wacc_t)**t,
    since the cash flow has to pass through each year's own discount
    rate in sequence on its way back to today; using just the final
    year's rate raised to a power would ignore every other year's rate
    entirely. Reduces to exactly discount_cash_flows's result whenever
    wacc_path is constant (see the self-test).
    """
    if len(cash_flows) != len(wacc_path):
        raise ValueError("cash_flows and wacc_path must be the same length")
    discounted = []
    cumulative_factor = 1.0
    for cf, r in zip(cash_flows, wacc_path):
        cumulative_factor *= (1 + r)
        discounted.append(cf / cumulative_factor)
    return discounted


def project_ebit_path(revenue_path, margin_path):
    """EBIT_t = Revenue_t x Margin_t, year by year."""
    if len(revenue_path) != len(margin_path):
        raise ValueError("revenue_path and margin_path must be the same length")
    return [r * m for r, m in zip(revenue_path, margin_path)]


def reinvestment_from_sales_to_capital(revenue_path, sales_to_capital_ratio, base_revenue):
    """
    Reinvestment_t = (Revenue_t - Revenue_{t-1}) / sales_to_capital_ratio.

    Damodaran's standard way to size how much new capital a company's
    growth requires without forecasting capex/D&A line by line: the
    ratio answers "how much new revenue does $1 of invested capital
    produce" (2.0 means $1 buys $2 of revenue) — usually an industry
    average, since a young company's own trailing ratio is too noisy to
    use directly. `base_revenue` is year-0 (last actual) revenue, so
    year 1's reinvestment is sized off the step up from base_revenue to
    revenue_path[0]. A declining-revenue year produces NEGATIVE
    reinvestment (capital released) — left as-is rather than floored at
    zero, matching Damodaran's own treatment; this is also exactly how a
    heavy early-year capex program against a small or negative EBIT
    makes FCFF strongly negative, as it should (see the docstring on
    project_fcff_narrative).
    """
    if sales_to_capital_ratio <= 0:
        raise ValueError("sales_to_capital_ratio must be positive")
    reinvestments = []
    prev = base_revenue
    for rev in revenue_path:
        reinvestments.append((rev - prev) / sales_to_capital_ratio)
        prev = rev
    return reinvestments


def project_fcff_narrative(revenue_path, margin_path, tax_rate, sales_to_capital_ratio, base_revenue):
    """
    FCFF_t = EBIT_t x (1 - tax_rate) - Reinvestment_t, where EBIT_t comes
    from project_ebit_path (revenue x margin, not a single compounded
    FCFF number) and Reinvestment_t comes from the sales-to-capital
    ratio. This is the actual replacement for "last year's FCFF x growth
    rate" — revenue, margin, and reinvestment are free to move
    independently of each other, year by year.

    `tax_rate` can be a single flat rate (float) or a list matching
    revenue_path's length — e.g. the output of nol_tax_rate_path below,
    an NOL-driven ramp from ~0% toward the marginal rate.

    Returns a list of FCFF values, oldest (year 1) first, same length as
    revenue_path. Early years can easily be NEGATIVE here (heavy
    reinvestment against a still-small or negative margin) — that's the
    correct behavior for a young company, not a bug to work around.
    """
    n = len(revenue_path)
    if len(margin_path) != n:
        raise ValueError("revenue_path and margin_path must be the same length")
    tax_rates = tax_rate if isinstance(tax_rate, list) else [tax_rate] * n
    if len(tax_rates) != n:
        raise ValueError("tax_rate list must be the same length as revenue_path")
    ebit_path = project_ebit_path(revenue_path, margin_path)
    reinvestment_path = reinvestment_from_sales_to_capital(revenue_path, sales_to_capital_ratio, base_revenue)
    return [ebit * (1 - t) - reinv for ebit, t, reinv in zip(ebit_path, tax_rates, reinvestment_path)]


def nol_tax_rate_path(ebit_path, nol_balance, marginal_tax_rate):
    """
    Per-year EFFECTIVE tax rate given a starting pool of accumulated net
    operating losses — Damodaran's standard treatment for a young
    company carrying NOLs: the effective rate starts near zero (losses
    shelter early taxable income dollar for dollar) and rises to the
    marginal rate once the pool is exhausted, rather than assuming the
    marginal rate applies from year 1.

    A projected year with EBIT <= 0 owes no tax (rate 0) — you can't be
    taxed on a loss — and that loss ADDS to the NOL pool for future
    years, rather than assuming any existing shield just sits idle. A
    positive-EBIT year shelters as much of it as the remaining pool
    covers; the rest is taxed at marginal_tax_rate, and the pool is
    drawn down by whatever was sheltered.

    Returns a list of effective tax rates (floats), same length as
    ebit_path, DIRECTLY usable as the `tax_rate` argument to
    project_fcff_narrative / project_narrative_path /
    enterprise_value_narrative (all three already accept a per-year list
    there, not just a flat rate).
    """
    rates = []
    remaining_nol = nol_balance
    for ebit in ebit_path:
        if ebit <= 0:
            rates.append(0.0)
            remaining_nol += -ebit
            continue
        sheltered = min(ebit, remaining_nol)
        taxable = ebit - sheltered
        remaining_nol -= sheltered
        rates.append((taxable * marginal_tax_rate) / ebit)
    return rates


def project_narrative_path(current_revenue, initial_growth_rate, current_margin, target_margin,
                            tax_rate, sales_to_capital_ratio, terminal_growth_rate, years=10):
    """
    Convenience wrapper that builds and returns every year-by-year path
    at once (revenue, margin, EBIT, reinvestment, FCFF) — for a caller
    that wants to PRINT the full build for transparency, same spirit as
    how run_full_valuation prints the multi-stage growth path today.
    Takes the same inputs as enterprise_value_narrative, minus
    discount_rate (not needed to build the path itself, only to
    discount it).
    """
    revenue_path = project_revenue_path(current_revenue, initial_growth_rate, terminal_growth_rate, years)
    margin_path = project_margin_path(current_margin, target_margin, years)
    ebit_path = project_ebit_path(revenue_path, margin_path)
    reinvestment_path = reinvestment_from_sales_to_capital(revenue_path, sales_to_capital_ratio, current_revenue)
    fcff_path = project_fcff_narrative(revenue_path, margin_path, tax_rate, sales_to_capital_ratio, current_revenue)
    return {
        "revenue": revenue_path,
        "margin": margin_path,
        "ebit": ebit_path,
        "reinvestment": reinvestment_path,
        "fcff": fcff_path,
    }


def enterprise_value_narrative(current_revenue, initial_growth_rate, current_margin, target_margin,
                                tax_rate, sales_to_capital_ratio, discount_rate, terminal_growth_rate,
                                years=10, return_on_new_capital=None, return_breakdown=False):
    """
    Full narrative DCF (Damodaran's "Ginzu"-style build): rebuilds the
    revenue and margin paths fresh off the terminal_growth_rate passed
    in (rather than reusing a path built elsewhere), the same way
    enterprise_value_multi_stage rebuilds its growth path fresh each
    call — this matters because run_valuation_range_narrative flexes
    terminal_growth_rate across low/base/high scenarios, and the
    revenue fade target has to move with it for the explicit period to
    stay consistent with the terminal value's growth assumption.

    `discount_rate` can be a single flat WACC (float — current behavior,
    unchanged) or a list matching `years` in length (e.g.
    project_wacc_path's output — a declining cost-of-capital glide path
    for a young, undiversified company). When it's a list: the explicit
    period is discounted with the CUMULATIVE-PRODUCT method
    (discount_cash_flows_variable_rate) instead of one flat exponent;
    the terminal value's Gordon Growth formula uses the FINAL year's
    rate (the stable, mature-company assumption — the perpetuity beyond
    year `years` is assumed to run at that steady-state cost of
    capital); and the terminal value itself is discounted back to today
    using that SAME cumulative product through year `years` — not
    (1+final_rate)**years, which would wrongly ignore every year's
    higher earlier rate.

    The terminal value is reinvestment-consistent (Damodaran's Ginzu
    does the same): it's rebuilt from the final year's NOPAT (EBIT x (1 -
    that year's tax rate)) with reinvestment = g / return_on_new_capital,
    rather than growing the final year's FCFF — whose reinvestment came
    from the sales-to-capital ratio and has nothing to do with
    perpetual growth. return_on_new_capital defaults to the (final-year)
    discount rate; see terminal_value_consistent.

    Returns enterprise value (before subtracting debt / adding cash), or
    with return_breakdown=True {"ev", "pv_explicit", "pv_terminal"}.
    """
    revenue_path = project_revenue_path(current_revenue, initial_growth_rate, terminal_growth_rate, years)
    margin_path = project_margin_path(current_margin, target_margin, years)
    fcff_path = project_fcff_narrative(revenue_path, margin_path, tax_rate, sales_to_capital_ratio, current_revenue)
    final_tax = tax_rate[-1] if isinstance(tax_rate, list) else tax_rate
    final_nopat = revenue_path[-1] * margin_path[-1] * (1 - final_tax)

    if isinstance(discount_rate, list):
        if len(discount_rate) != years:
            raise ValueError("discount_rate list must be the same length as years")
        discounted = discount_cash_flows_variable_rate(fcff_path, discount_rate)
        pv_of_explicit_period = sum(discounted)

        tv = terminal_value_consistent(final_nopat, terminal_growth_rate, discount_rate[-1],
                                       return_on_new_capital)
        cumulative_factor = 1.0
        for r in discount_rate:
            cumulative_factor *= (1 + r)
        pv_of_terminal_value = tv / cumulative_factor
    else:
        discounted = discount_cash_flows(fcff_path, discount_rate)
        pv_of_explicit_period = sum(discounted)

        tv = terminal_value_consistent(final_nopat, terminal_growth_rate, discount_rate,
                                       return_on_new_capital)
        pv_of_terminal_value = tv / ((1 + discount_rate) ** years)

    if return_breakdown:
        return {"ev": pv_of_explicit_period + pv_of_terminal_value,
                "pv_explicit": pv_of_explicit_period, "pv_terminal": pv_of_terminal_value}
    return pv_of_explicit_period + pv_of_terminal_value


def narrative_roic_path(invested_capital, reinvestment_path, ebit_path, tax_rate):
    """
    Return on invested capital each forecast year for the narrative path:
    NOPAT in year t / the capital in place at the START of year t, where
    capital = today's invested capital + all reinvestment through year
    t-1. Damodaran's check on a narrative: if the year-10 return is below
    the cost of capital, the growth destroys value; if it's far above the
    industry's, the story needs a durable competitive advantage to be
    believable.

    `tax_rate` is a flat rate or a per-year list (e.g. the NOL ramp).
    Returns a list of ROICs (None for a year whose starting capital isn't
    positive), same length as ebit_path.
    """
    n = len(ebit_path)
    if len(reinvestment_path) != n:
        raise ValueError("reinvestment_path and ebit_path must be the same length")
    taxes = tax_rate if isinstance(tax_rate, list) else [tax_rate] * n
    roics, capital = [], invested_capital
    for ebit, tax, reinvestment in zip(ebit_path, taxes, reinvestment_path):
        roics.append(ebit * (1 - tax) / capital if capital > 0 else None)
        capital += reinvestment
    return roics


def run_valuation_range_narrative(current_revenue, initial_growth_rate, current_margin, target_margin,
                                   tax_rate, sales_to_capital_ratio, discount_rate, terminal_growth_rate,
                                   total_debt, cash, shares_outstanding,
                                   years=10, wacc_spread=0.01, growth_spread=0.005,
                                   return_on_new_capital=None):
    """
    Same low/base/high sensitivity range as run_valuation_range_multi_stage,
    off the narrative (revenue x margin x reinvestment) build instead of
    the single-growth-rate FCFF compounding.

    The LOW/BASE/HIGH labels assume "higher discount rate + lower terminal
    growth = more conservative = lower value" — true whenever cash flows
    are mostly positive, but it can INVERT for a young company whose
    explicit-period FCFF is negative for most of the forecast (heavy
    reinvestment against a still-small or negative margin, per
    project_fcff_narrative): a higher discount rate shrinks those
    negative present values toward zero FASTER than it shrinks the
    (positive) terminal value, which can make the "LOW" scenario come
    out higher than "HIGH." Found on a real run (TeraWulf) during
    sanity-checking this model against companies from earlier in this
    session. The three numbers are each computed correctly regardless —
    only the ordering assumption behind the labels can break down — so
    this detects that case and returns it as `ordering_warning` (a
    string describing it, or None when the normal low < base < high
    ordering holds) instead of silently re-sorting or re-labeling the
    scenarios.

    `discount_rate` can be a single flat WACC (float — existing
    behavior, unchanged) or a (initial_wacc, mature_wacc) TUPLE, which
    builds a declining cost-of-capital glide path (project_wacc_path)
    for each scenario instead. In the glide case, low/base/high flex
    BOTH endpoints by wacc_spread in the same direction — keeps the
    glide's shape, just shifts the whole curve up or down — rather than
    flexing a single number. Each scenario's result also carries
    `wacc_path` (the full per-year list used) alongside `discount_rate`
    (set to that scenario's mature_wacc, for backward-compatible
    display) so a caller can print the actual glide if it wants to.
    """
    is_glide = isinstance(discount_rate, tuple)
    if is_glide:
        initial_wacc, mature_wacc = discount_rate
        scenarios = {
            "low":  (initial_wacc + wacc_spread, mature_wacc + wacc_spread, terminal_growth_rate - growth_spread),
            "base": (initial_wacc, mature_wacc, terminal_growth_rate),
            "high": (initial_wacc - wacc_spread, mature_wacc - wacc_spread, terminal_growth_rate + growth_spread),
        }
    else:
        scenarios = {
            "low":  (discount_rate + wacc_spread, terminal_growth_rate - growth_spread),
            "base": (discount_rate, terminal_growth_rate),
            "high": (discount_rate - wacc_spread, terminal_growth_rate + growth_spread),
        }
    results = {}
    for label, scenario in scenarios.items():
        if is_glide:
            dr_initial, dr_mature, tgr = scenario
            dr = project_wacc_path(dr_initial, dr_mature, years)
            display_rate = dr_mature
        else:
            dr, tgr = scenario
            display_rate = dr
        ev = enterprise_value_narrative(current_revenue, initial_growth_rate, current_margin, target_margin,
                                         tax_rate, sales_to_capital_ratio, dr, tgr, years,
                                         return_on_new_capital)
        per_share, equity_val = equity_value_per_share(ev, total_debt, cash, shares_outstanding)
        results[label] = {
            "discount_rate": display_rate,
            "wacc_path": dr if is_glide else None,
            "terminal_growth_rate": tgr,
            "enterprise_value": ev,
            "equity_value": equity_val,
            "value_per_share": per_share,
        }

    results["ordering_warning"] = ordering_warning_for_range(
        results["low"]["value_per_share"], results["base"]["value_per_share"], results["high"]["value_per_share"])
    return results


def ordering_warning_for_range(low_value, base_value, high_value):
    """
    Shared ordering-inversion check, factored out of
    run_valuation_range_narrative so it can also validate a low/base/
    high triple AFTER some further transform of those three numbers —
    e.g. the failure-probability overlay below blends each scenario's
    going-concern value with a FIXED liquidation value; that's a linear
    transform with the same (positive) weight applied to all three
    scenarios, so it preserves whatever ordering the going-concern
    numbers already had, including an inversion — worth re-checking
    rather than assuming it carried over correctly.

    Returns a warning string, or None if low_value < base_value <
    high_value holds.
    """
    if low_value < base_value < high_value:
        return None
    return (
        "LOW/BASE/HIGH came out of order (not low < base < high): "
        f"low=${low_value:,.2f}, base=${base_value:,.2f}, high=${high_value:,.2f}. "
        "This happens when most of the explicit-period FCFF is negative — a "
        "higher discount rate shrinks those negative present values toward "
        "zero faster than it shrinks the terminal value, which can flip the "
        "usual 'higher WACC = more conservative' relationship. Each number is "
        "still computed correctly; treat these three as a SPREAD, not an "
        "ordered range, when you see this warning."
    )


# ---------------------------------------------------------------------
# FAILURE-PROBABILITY OVERLAY: a DCF (narrative or otherwise) implicitly
# assumes the company SURVIVES to realize the modeled value — WACC
# prices business/market risk, not "this specific company might run out
# of cash and cease to exist entirely," which is a real and separate
# risk for a young, cash-burning company. Damodaran's fix: estimate the
# probability of failure from cash burn and runway, then blend the
# going-concern value with a distressed liquidation value.
# ---------------------------------------------------------------------

# (min_runway_years, probability_of_failure) tuples, DESCENDING runway
# order — the first threshold the company's own runway meets or exceeds
# wins, same table-lookup shape as synthetic_credit_rating. ILLUSTRATIVE:
# unlike the bond-rating spread table (which at least loosely tracks
# real credit-spread bands), there's no published "runway -> failure
# probability" standard to anchor this to — treat it as a reasonable
# monotonic starting point (more runway = lower risk, strictly), and
# override it with sector-specific judgment or your own data when you
# have it, the same way you would the spread table above.
FAILURE_PROBABILITY_TABLE = [
    (5.0, 0.02),
    (3.0, 0.05),
    (2.0, 0.10),
    (1.0, 0.20),
    (0.5, 0.35),
    (0.0, 0.50),
]


def estimate_runway_years(cash, annual_burn_rate):
    """
    Years of cash runway at the current burn rate. Returns float('inf')
    when annual_burn_rate <= 0 — the company isn't burning cash (its
    base-year FCFF is already non-negative), so there's no cash-burn-
    driven failure risk to estimate from this angle at all.
    """
    if annual_burn_rate <= 0:
        return float("inf")
    return cash / annual_burn_rate


def failure_probability_from_runway(runway_years, table=None):
    """
    Table lookup: runway -> probability of failure (see
    FAILURE_PROBABILITY_TABLE's docstring on why this is illustrative,
    not an authoritative standard). An infinite runway (no cash burn)
    naturally lands in the longest-runway, lowest-probability bucket.
    """
    if table is None:
        table = FAILURE_PROBABILITY_TABLE
    for min_runway, probability in table:
        if runway_years >= min_runway:
            return probability
    return table[-1][1]


def failure_adjusted_value_per_share(going_concern_value_per_share, probability_of_failure,
                                      book_equity, shares_outstanding, liquidation_haircut=0.50):
    """
    Blends a going-concern value (the narrative DCF's low/base/high, or
    any other per-share value) with a distressed LIQUIDATION value:

        value = P(survive) x going_concern_value + P(fail) x liquidation_value

    liquidation_value_per_share is a haircut on book equity — assets
    rarely fetch full book value in a forced sale (tangible assets like
    cash and receivables often do; intangibles/goodwill usually don't).
    liquidation_haircut defaults to 50%, a rough Damodaran-style
    starting point; override it with your own judgment about this
    company's specific asset mix.

    Returns (blended_value_per_share, liquidation_value_per_share).
    """
    if not (0.0 <= probability_of_failure <= 1.0):
        raise ValueError("probability_of_failure must be between 0 and 1")
    liquidation_value_per_share = (book_equity * liquidation_haircut) / shares_outstanding
    probability_of_survival = 1.0 - probability_of_failure
    blended = (probability_of_survival * going_concern_value_per_share
               + probability_of_failure * liquidation_value_per_share)
    return blended, liquidation_value_per_share


# ---------------------------------------------------------------------
# BIOTECH / DRUG PIPELINE: a clinical-stage biotech's value is mostly a
# handful of drug candidates, each of which will either get approved
# and earn money for the life of its patent, or fail and be worth
# nothing. Neither the mechanical FCFF DCF (no positive base-year cash
# flow to compound) nor the narrative DCF (needs revenue today to grow
# from) fits a pre-revenue company. Damodaran's two tools for this:
#   1. Probability-weight each drug by the odds it gets through the
#      remaining clinical phases (a decision tree collapsed into one
#      cumulative probability) — i.e. risk-adjusted NPV (rNPV), summed
#      across the pipeline.
#   2. Value a patent / undeveloped drug as a CALL OPTION on its
#      commercial cash flows (his Avonex example): strike = development
#      cost, life = remaining exclusivity, dividend yield = 1/life (each
#      year of delay burns a year of patent protection).
# ---------------------------------------------------------------------

# Cumulative probability that a drug CURRENTLY in each phase eventually
# gets approved — the product of every remaining phase-transition rate.
# Phase 1 through filed follow BIO/Informa/QLS "Clinical Development
# Success Rates 2011-2020" (all-indication averages: P1->P2 52.0%,
# P2->P3 28.9%, P3->filing 57.8%, filing->approval 90.6%). Preclinical
# has no comparable published figure in that study; 5% is a rough
# placeholder. ILLUSTRATIVE averages, same caveat as the spread and
# runway tables above: oncology runs well below these, hematology and
# rare-disease programs with validated biology well above — override
# with indication-specific rates when you have them.
PHASE_APPROVAL_PROBABILITY = {
    "preclinical": 0.05,
    "phase1": 0.079,
    "phase2": 0.151,
    "phase3": 0.524,
    "filed": 0.906,
    "approved": 1.0,
}


def drug_commercial_cash_flows(peak_sales, years_to_launch, ramp_years, exclusivity_years,
                                operating_margin, tax_rate):
    """
    Year-by-year after-tax operating cash flow from one drug IF it's
    approved: zero until launch, sales ramping linearly to peak_sales
    over ramp_years, held at peak until exclusivity runs out, then
    stopping. Stopping at loss of exclusivity is deliberately
    conservative — generic/biosimilar erosion typically takes 80-90%+
    of a small-molecule's sales within a year or two, and modeling a
    long tail would put most of the value in the least-knowable years.

    operating_margin is the DRUG-LEVEL margin (after COGS and the
    drug's own commercial spend, before unallocated corporate overhead
    and research on other programs).

    Returns a list of cash flows, year 1 first, length
    years_to_launch + exclusivity_years.
    """
    if ramp_years < 1:
        raise ValueError("ramp_years must be at least 1")
    if years_to_launch < 0 or exclusivity_years < 1:
        raise ValueError("years_to_launch must be >= 0 and exclusivity_years >= 1")
    cash_flows = [0.0] * years_to_launch
    for k in range(1, exclusivity_years + 1):
        sales = peak_sales * min(k / ramp_years, 1.0)
        cash_flows.append(sales * operating_margin * (1 - tax_rate))
    return cash_flows


def drug_rnpv(peak_sales, years_to_launch, ramp_years, exclusivity_years, operating_margin,
              tax_rate, discount_rate, remaining_dev_cost, probability_of_approval):
    """
    Risk-adjusted NPV of one drug candidate:

        rNPV = P(approval) x PV(commercial cash flows) - PV(remaining development cost)

    The discount rate is the company's ordinary cost of capital — the
    clinical risk is already in P(approval), so piling a "biotech risk
    premium" onto the discount rate as well would count it twice.

    remaining_dev_cost is spread evenly over the years until launch
    (charged today if the drug is already approved) and treated as
    CERTAIN. In reality a later phase's cost is only spent if the
    earlier one succeeds, so this is conservative — most noticeably for
    early-stage drugs, where most of the remaining cost sits in a Phase
    3 that will probably never run.

    Returns a dict with the pieces (pv_commercial, pv_dev_cost,
    unrisked_npv, rnpv, probability_of_approval) so a caller can show
    the build, not just the answer.
    """
    if not (0.0 <= probability_of_approval <= 1.0):
        raise ValueError("probability_of_approval must be between 0 and 1")
    commercial = drug_commercial_cash_flows(peak_sales, years_to_launch, ramp_years,
                                            exclusivity_years, operating_margin, tax_rate)
    pv_commercial = sum(discount_cash_flows(commercial, discount_rate))
    if years_to_launch == 0:
        pv_dev_cost = remaining_dev_cost
    else:
        per_year = remaining_dev_cost / years_to_launch
        pv_dev_cost = sum(discount_cash_flows([per_year] * years_to_launch, discount_rate))
    return {
        "pv_commercial": pv_commercial,
        "pv_dev_cost": pv_dev_cost,
        "unrisked_npv": pv_commercial - pv_dev_cost,
        "rnpv": probability_of_approval * pv_commercial - pv_dev_cost,
        "probability_of_approval": probability_of_approval,
    }


def _normal_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def patent_option_value(pv_commercial, development_cost, exclusivity_years, volatility,
                        risk_free_rate, dividend_yield=None):
    """
    Damodaran's patent-as-option valuation (Black-Scholes with a
    continuous dividend yield):

        S = PV of the drug's commercial cash flows if developed today
        K = PV of the cost to develop and launch it
        t = years of exclusivity left on the patent
        sigma = volatility of S — the uncertainty in the drug's
                commercial value (not the stock's volatility, though
                that's sometimes used as a rough proxy)
        y = cost of delay, 1/t by default: every year spent waiting is a
            year of patent protection lost

    This captures something rNPV can't: a drug with a negative NPV today
    can still be worth something, because the company can wait for
    better information (trial readouts, market size, pricing) before
    committing the spend. It values the RIGHT to commercialize; it does
    NOT include clinical failure risk, so for a drug still in trials
    multiply by P(approval) before comparing it to the rNPV.

    Returns a dict: value, d1, d2, dividend_yield.
    """
    if pv_commercial <= 0 or development_cost <= 0:
        raise ValueError("pv_commercial and development_cost must both be positive for the option model")
    if exclusivity_years <= 0 or volatility <= 0:
        raise ValueError("exclusivity_years and volatility must be positive")
    t = exclusivity_years
    y = (1.0 / t) if dividend_yield is None else dividend_yield
    d1 = (math.log(pv_commercial / development_cost) + (risk_free_rate - y + volatility ** 2 / 2) * t) \
        / (volatility * math.sqrt(t))
    d2 = d1 - volatility * math.sqrt(t)
    value = (pv_commercial * math.exp(-y * t) * _normal_cdf(d1)
             - development_cost * math.exp(-risk_free_rate * t) * _normal_cdf(d2))
    return {"value": value, "d1": d1, "d2": d2, "dividend_yield": y}


# ---------------------------------------------------------------------
# REIT: a REIT must pay out at least 90% of taxable income, so its
# dividend is close to its free cash flow to equity — the dividend
# discount model above is the natural tool. Three REIT-specific fixes on
# top of it, following Damodaran's treatment:
#   1. Accounting depreciation on buildings overstates how fast real
#      estate actually loses value, so net income understates cash
#      earnings. FFO adds it back; AFFO then subtracts the capex needed
#      to keep the buildings competitive.
#   2. A REIT grows mostly by issuing new shares and debt, not by
#      retaining earnings — so historical dividend growth overstates
#      what EXISTING shareholders get. Growth should come from
#      fundamentals: retention ratio x return on equity.
#   3. Cross-check the cash-flow value against what the buildings are
#      worth directly: net operating income / cap rate, less debt (NAV).
# ---------------------------------------------------------------------

def compute_ffo(net_income, real_estate_depreciation, gains_on_property_sales, real_estate_impairments=0.0):
    """
    Funds From Operations (Nareit definition):
        FFO = net income + real estate depreciation & amortization
              + impairments of real estate - gains on property sales

    Gains are subtracted because selling a building is a one-off
    liquidation of the asset base, not recurring operating earnings
    (a LOSS on sale is passed in as a negative gain and so gets added
    back). Impairments are added back for the same reason depreciation
    is: a non-cash write-down of a building's book value.
    """
    return net_income + real_estate_depreciation + real_estate_impairments - gains_on_property_sales


def compute_affo(ffo, maintenance_capex):
    """
    Adjusted FFO = FFO - recurring (maintenance) capex: roofs, HVAC,
    tenant improvements and leasing commissions needed just to keep the
    existing buildings leased. This is the closest thing a REIT has to
    sustainable free cash flow to equity, and what a dividend should be
    measured against — a dividend above AFFO is being funded by asset
    sales, new debt or new shares, not by the buildings themselves.

    Growth capex (new developments, acquisitions) is deliberately NOT
    subtracted: that's reinvestment for growth, funded mostly by new
    capital, not a cost of maintaining today's cash flow.
    """
    return ffo - maintenance_capex


def reit_fundamental_growth(dividends, affo, book_equity):
    """
    Damodaran's fundamental growth check, on an AFFO basis:

        retention ratio = 1 - dividends / AFFO
        return on equity = AFFO / book equity
        expected growth = retention ratio x return on equity

    Uses AFFO rather than net income because a REIT's depreciation-
    depressed net income routinely sits BELOW its dividend, which would
    show a payout over 100% and a meaningless negative retention ratio
    for a perfectly healthy REIT.

    Growth CAN come out negative (dividend above AFFO): that's a real
    signal — the dividend isn't covered by recurring cash flow — so
    it's returned as-is, not floored at zero.

    Returns a dict: payout_ratio, retention_ratio, roe, growth.
    """
    if affo <= 0:
        raise ValueError("AFFO must be positive to compute fundamental growth")
    if book_equity <= 0:
        raise ValueError("book_equity must be positive to compute return on equity")
    payout_ratio = dividends / affo
    retention_ratio = 1.0 - payout_ratio
    roe = affo / book_equity
    return {
        "payout_ratio": payout_ratio,
        "retention_ratio": retention_ratio,
        "roe": roe,
        "growth": retention_ratio * roe,
    }


def reit_nav_per_share(noi, cap_rate, cash, total_debt, preferred_equity, shares_outstanding):
    """
    Net Asset Value: what the property portfolio is worth valued
    directly, the way a buyer of the buildings would price it.

        property value = NOI / cap rate
        NAV = property value + cash - debt - preferred equity

    A cap rate is the market's going yield on property: NOI / price.
    It's (cost of capital - growth) in Gordon-growth form, so this is a
    one-line DCF of the property cash flows, priced off what similar
    buildings trade for rather than a WACC built up from CAPM.

    Returns a dict: property_value, nav, nav_per_share.
    """
    if cap_rate <= 0:
        raise ValueError("cap_rate must be positive")
    if shares_outstanding <= 0:
        raise ValueError("shares_outstanding must be positive")
    property_value = noi / cap_rate
    nav = property_value + cash - total_debt - preferred_equity
    return {
        "property_value": property_value,
        "nav": nav,
        "nav_per_share": nav / shares_outstanding,
    }


def share_count_growth_rate(share_counts):
    """
    Compound annual growth in shares outstanding, OLDEST FIRST. For a
    REIT this is the dilution rate: how much of the headline dividend
    and FFO growth is just the company getting bigger by issuing stock,
    rather than each existing share getting more. Returns None with
    fewer than 2 data points or a non-positive starting count.
    """
    if len(share_counts) < 2 or share_counts[0] <= 0:
        return None
    years = len(share_counts) - 1
    return (share_counts[-1] / share_counts[0]) ** (1 / years) - 1


# ---------------------------------------------------------------------
# CYCLICALS: a cyclical's earnings in any one year say more about where
# the cycle is than about the business. Damodaran's fix is to value it on
# a NORMALIZED margin — the average over a full cycle — applied to
# current revenue. A full cycle has troughs in it, so loss years count:
# averaging only the profitable years pushes the "normal" margin up and
# the valuation with it.
# ---------------------------------------------------------------------

def cycle_margin_stats(ebit_by_year, revenue_by_year):
    """
    Operating margin over a cycle, three ways. Both inputs are lists of
    the same length, one entry per year (order doesn't matter for the
    statistics). Years with zero or negative revenue are skipped — a
    margin isn't defined there — but loss years are KEPT.

        mean       — simple average of each year's margin: every year
                     counts equally (Damodaran's usual "average margin
                     over the cycle")
        aggregate  — total EBIT / total revenue: big-revenue years count
                     more, which matters when revenue itself swings with
                     the cycle (commodity prices, volumes)
        median     — middle year's margin: a check against one freak
                     year (a huge write-down, a one-off windfall) pulling
                     the mean around

    Returns a dict: margins (per year, same order as given, None where
    skipped), mean, aggregate, median, n_years. Raises ValueError if no
    year has positive revenue.
    """
    if len(ebit_by_year) != len(revenue_by_year):
        raise ValueError("ebit_by_year and revenue_by_year must be the same length")
    margins = [e / r if r > 0 else None for e, r in zip(ebit_by_year, revenue_by_year)]
    used = [(e, r, m) for e, r, m in zip(ebit_by_year, revenue_by_year, margins) if m is not None]
    if not used:
        raise ValueError("need at least one year with positive revenue")
    valid = sorted(m for _, _, m in used)
    n = len(valid)
    median = valid[n // 2] if n % 2 else (valid[n // 2 - 1] + valid[n // 2]) / 2
    return {
        "margins": margins,
        "mean": sum(valid) / n,
        "aggregate": sum(e for e, _, _ in used) / sum(r for _, r, _ in used),
        "median": median,
        "n_years": n,
    }


# ---------------------------------------------------------------------
# SELF-TEST: validate the math against a simple worked example.
# Run this file directly (python3 model.py) to see it check itself.
# ---------------------------------------------------------------------
if __name__ == "__main__":
    print("Running self-test with a simple hand-checkable example...\n")

    # Simple example: EBIT of $1000, 25% tax rate, $100 D&A, $150 capex
    ebit = 1000
    tax_rate = 0.25
    d_and_a = 100
    capex = 150

    fcff = compute_fcff(ebit, tax_rate, d_and_a, capex)
    expected_fcff = 1000 * 0.75 + 100 - 150  # = 750 + 100 - 150 = 700
    print(f"FCFF computed: {fcff}  (expected {expected_fcff})")
    assert fcff == expected_fcff, "FCFF calculation mismatch!"

    # CAPM check: Rf=4%, beta=1.2, ERP=5.5% -> cost of equity = 4 + 1.2*5.5 = 10.6%
    coe = cost_of_equity(0.04, 1.2, 0.055)
    expected_coe = 0.04 + 1.2 * 0.055
    print(f"Cost of equity: {coe:.4f}  (expected {expected_coe:.4f})")
    assert abs(coe - expected_coe) < 1e-9, "Cost of equity mismatch!"

    # WACC check: E=800, D=200, coe=10.6%, cod=5%, tax=25%
    # weights: E=0.8, D=0.2. after-tax cod = 5%*0.75=3.75%
    # WACC = 0.8*10.6% + 0.2*3.75% = 8.48% + 0.75% = 9.23%
    w = wacc(800, 200, coe, 0.05, 0.25)
    expected_w = 0.8 * coe + 0.2 * (0.05 * 0.75)
    print(f"WACC: {w:.4f}  (expected {expected_w:.4f})")
    assert abs(w - expected_w) < 1e-9, "WACC calculation mismatch!"

    # Terminal value check: final FCFF=700, terminal growth=2%, discount=9.23%
    tv = terminal_value(700, 0.02, w)
    expected_tv = 700 * 1.02 / (w - 0.02)
    print(f"Terminal value: {tv:.2f}  (expected {expected_tv:.2f})")
    assert abs(tv - expected_tv) < 1e-6, "Terminal value calculation mismatch!"

    # Unlever/relever beta check: levered beta=1.5, tax=25%, D/E=0.5
    # unlevered = 1.5 / (1 + 0.75*0.5) = 1.5 / 1.375 = 1.0909...
    unlevered = unlever_beta(1.5, 0.25, 0.5)
    expected_unlevered = 1.5 / (1 + 0.75 * 0.5)
    print(f"Unlevered beta: {unlevered:.4f}  (expected {expected_unlevered:.4f})")
    assert abs(unlevered - expected_unlevered) < 1e-9, "Unlever beta mismatch!"

    # Relevering back at the same D/E should recover the original beta
    relevered = relever_beta(unlevered, 0.25, 0.5)
    print(f"Relevered beta (should recover 1.5): {relevered:.4f}")
    assert abs(relevered - 1.5) < 1e-9, "Relever beta did not recover original value!"

    # Relevering to a HIGHER D/E should produce a HIGHER beta (more leverage = more risk)
    relevered_higher_leverage = relever_beta(unlevered, 0.25, 2.0)
    print(f"Relevered beta at D/E=2.0 (should be > 1.5): {relevered_higher_leverage:.4f}")
    assert relevered_higher_leverage > 1.5, "Higher leverage should produce higher beta!"

    # Reinvestment rate + implied growth check:
    # NOPAT=700, CapEx=150, D&A=100, change_in_nwc=20
    # reinvestment rate = (150 - 100 + 20) / 700 = 70/700 = 0.10
    reinv_rate = compute_reinvestment_rate(capex=150, d_and_a=100, change_in_nwc=20, nopat=700)
    expected_reinv = (150 - 100 + 20) / 700
    print(f"Reinvestment rate: {reinv_rate:.4f}  (expected {expected_reinv:.4f})")
    assert abs(reinv_rate - expected_reinv) < 1e-9, "Reinvestment rate mismatch!"

    # ROIC check: NOPAT=700, invested capital=5000 -> 14%
    roic = compute_roic(nopat=700, invested_capital=5000)
    print(f"ROIC: {roic:.4f}  (expected {700/5000:.4f})")
    assert abs(roic - 700 / 5000) < 1e-9, "ROIC mismatch!"

    # Implied growth = reinvestment rate * ROIC = 0.10 * 0.14 = 0.014
    g = implied_growth_rate(reinv_rate, roic)
    expected_g = expected_reinv * (700 / 5000)
    print(f"Implied growth rate: {g:.4f}  (expected {expected_g:.4f})")
    assert abs(g - expected_g) < 1e-9, "Implied growth rate mismatch!"

    # R&D capitalization check: 3 years of R&D growing 10%/yr: 100, 110, 121
    # (oldest first, 121 = current year). amortizable_life=3.
    # Hand-computed:
    #   research_asset = 100*(1/3) + 110*(2/3) + 121*(3/3)
    #                  = 33.3333 + 73.3333 + 121 = 227.6667
    #   amortization   = 100/3 + 110/3 = 70.0 (only PRIOR years amortize)
    #   ebit_adjustment = current_rd(121) - amortization(70) = 51.0
    rd_result = capitalize_rd([100, 110, 121], amortizable_life=3)
    expected_research_asset = 100 * (1 / 3) + 110 * (2 / 3) + 121 * (3 / 3)
    expected_amortization = 100 / 3 + 110 / 3
    expected_ebit_adj = 121 - expected_amortization
    print(f"R&D capitalization — research asset: {rd_result['research_asset']:.4f}  "
          f"(expected {expected_research_asset:.4f})")
    print(f"R&D capitalization — amortization:   {rd_result['amortization']:.4f}  "
          f"(expected {expected_amortization:.4f})")
    print(f"R&D capitalization — EBIT adjustment: {rd_result['ebit_adjustment']:.4f}  "
          f"(expected {expected_ebit_adj:.4f})")
    assert abs(rd_result["research_asset"] - expected_research_asset) < 1e-9, "R&D research asset mismatch!"
    assert abs(rd_result["amortization"] - expected_amortization) < 1e-9, "R&D amortization mismatch!"
    assert abs(rd_result["ebit_adjustment"] - expected_ebit_adj) < 1e-9, "R&D EBIT adjustment mismatch!"
    assert rd_result["current_rd"] == 121, "R&D current-year value mismatch!"

    # Padding check: a young company with only 2 years of R&D history
    # against a 3-year amortizable life should treat the missing oldest
    # year as $0, not error or silently use only 2 years as if life=2.
    rd_short = capitalize_rd([110, 121], amortizable_life=3)
    expected_short_asset = 0 * (1 / 3) + 110 * (2 / 3) + 121 * (3 / 3)
    print(f"R&D capitalization (short history, padded) — research asset: "
          f"{rd_short['research_asset']:.4f}  (expected {expected_short_asset:.4f})")
    assert abs(rd_short["research_asset"] - expected_short_asset) < 1e-9, "R&D padding mismatch!"
    assert rd_short["years_of_data"] == 2, "years_of_data should report actual history available, not the padded length!"

    # Synthetic credit rating checks: pick coverage ratios that land
    # cleanly inside a bucket for each table (avoiding the exact
    # breakpoints, which is what the size-comparison test below exists
    # to check instead).
    rating, spread = synthetic_credit_rating(7.0, large_firm=True)
    print(f"Synthetic rating, coverage=7.0, large firm: {rating}  spread={spread:.4f}  (expected AA, 0.0085)")
    assert rating == "AA" and abs(spread - 0.0085) < 1e-9, "Large-firm rating table lookup mismatch!"

    rating, spread = synthetic_credit_rating(0.1, large_firm=True)
    print(f"Synthetic rating, coverage=0.1, large firm: {rating}  spread={spread:.4f}  (expected D, 0.1500)")
    assert rating == "D" and abs(spread - 0.15) < 1e-9, "Distressed-coverage rating should fall through to D!"

    # Same coverage ratio, large vs. small firm: the small-firm table
    # must never give an EQUAL-OR-BETTER rating (lower-or-equal spread)
    # than the large-firm table — that's the whole point of having two
    # tables (a smaller company is viewed as riskier at the same
    # coverage ratio).
    _, spread_large = synthetic_credit_rating(5.0, large_firm=True)
    _, spread_small = synthetic_credit_rating(5.0, large_firm=False)
    print(f"Coverage=5.0: large-firm spread={spread_large:.4f}  vs  small-firm spread={spread_small:.4f}")
    assert spread_small >= spread_large, "Small-firm table should never be more lenient than the large-firm table!"

    # synthetic_cost_of_debt wrapper: EBIT=700, interest expense=100 ->
    # coverage=7.0 -> AA (0.0085 spread, from the lookup above) -> cost
    # of debt = risk_free_rate + spread.
    cod, coverage, rating, spread = synthetic_cost_of_debt(700, 100, risk_free_rate=0.04, large_firm=True)
    expected_cod = 0.04 + 0.0085
    print(f"Synthetic cost of debt: {cod:.4f}  (expected {expected_cod:.4f}), coverage={coverage:.2f}, rating={rating}")
    assert abs(cod - expected_cod) < 1e-9, "Synthetic cost of debt mismatch!"
    assert abs(coverage - 7.0) < 1e-9, "Interest coverage ratio mismatch!"

    # Zero/negative interest expense must return all-None, not divide by
    # zero or silently return a nonsense ratio.
    none_result = synthetic_cost_of_debt(700, 0, risk_free_rate=0.04)
    print(f"Synthetic cost of debt with zero interest expense: {none_result}  (expected all-None)")
    assert none_result == (None, None, None, None), "Zero interest expense should return all-None, not divide by zero!"

    # Full range test — just confirm it runs and low < base < high in value
    results = run_valuation_range(
        base_fcff=fcff,
        discount_rate=w,
        terminal_growth_rate=0.02,
        total_debt=200,
        cash=50,
        shares_outstanding=100,
        growth_rate=0.03,
    )
    print("\nValuation range test (arbitrary example company):")
    for label in ("low", "base", "high"):
        r = results[label]
        print(f"  {label:5s}: EV=${r['enterprise_value']:,.0f}  "
              f"per-share=${r['value_per_share']:,.2f}  "
              f"(WACC={r['discount_rate']:.2%}, terminal growth={r['terminal_growth_rate']:.2%})")

    assert results["low"]["value_per_share"] < results["base"]["value_per_share"] < results["high"]["value_per_share"], \
        "Range ordering is wrong — low should be < base < high!"

    # Multi-stage backward-compatibility check: high_growth_years=N,
    # fade_years=0 must exactly reproduce the flat single-stage model.
    multi_stage_projected = project_fcff_multi_stage(fcff, 0.03, 5, 0, 0.02)
    flat_projected = project_fcff(fcff, 0.03, 5)
    print(f"\nMulti-stage (no fade) vs flat projection: {multi_stage_projected} vs {flat_projected}")
    assert all(abs(a - b) < 1e-6 for a, b in zip(multi_stage_projected, flat_projected)), \
        "Multi-stage with fade_years=0 should match flat projection!"

    ev_multi_stage = enterprise_value_multi_stage(fcff, 0.03, 5, 0, 0.02, w)
    ev_flat = enterprise_value(fcff, 0.03, w, 0.02, years=5)
    print(f"Multi-stage EV (no fade): ${ev_multi_stage:,.2f}  (expected ${ev_flat:,.2f})")
    assert abs(ev_multi_stage - ev_flat) < 1e-6, "Multi-stage EV with fade_years=0 should match flat-model EV!"

    # Fade stage should linearly interpolate: high_growth_rate=10%,
    # terminal=2%, 4 fade years -> growth of 8%, 6%, 4%, 2% in those years.
    faded = project_fcff_multi_stage(1000, 0.10, 0, 4, 0.02)
    growth_path = []
    prev = 1000
    for val in faded:
        growth_path.append(round(val / prev - 1, 4))
        prev = val
    expected_path = [0.08, 0.06, 0.04, 0.02]
    print(f"Fade growth path: {growth_path}  (expected {expected_path})")
    assert growth_path == expected_path, "Fade stage should linearly interpolate growth down to terminal!"

    # A young company (long high-growth + long fade) should be worth more
    # than a mature company (no high-growth, short fade) given the same
    # starting FCFF, discount rate, and terminal growth — the whole point
    # of the multi-stage model is to give a growing company credit for a
    # longer runway instead of collapsing straight to terminal value.
    young_value = enterprise_value_multi_stage(fcff, 0.15, 5, 10, 0.02, w)
    mature_value = enterprise_value_multi_stage(fcff, 0.15, 0, 3, 0.02, w)
    print(f"Young-company EV: ${young_value:,.0f}  vs mature-company EV: ${mature_value:,.0f}")
    assert young_value > mature_value, "A longer growth runway should be worth more, all else equal!"

    # Implied-growth-rate solver: round-trip check — compute a price at a
    # known growth rate, then solve backward for that growth rate from
    # the price, and recover the original input.
    known_growth = 0.12
    rt_ev = enterprise_value_multi_stage(fcff, known_growth, 3, 5, 0.025, w)
    rt_price, _ = equity_value_per_share(rt_ev, 200, 50, 100)
    solved_growth = solve_implied_growth_rate(
        rt_price, fcff, 3, 5, w, 0.025, total_debt=200, cash=50, shares_outstanding=100)
    print(f"Implied-growth solver round-trip: input {known_growth:.4%} -> price {rt_price:.2f} "
          f"-> solved {solved_growth:.4%}")
    assert abs(solved_growth - known_growth) < 1e-4, "Implied-growth solver did not recover the known rate!"

    # A negative base FCFF has no growth-rate solution at all (see the
    # Samsara conversation) — the solver must return None, not a
    # misleading number.
    no_solution = solve_implied_growth_rate(50.0, -1000, 0, 3, w, 0.025,
                                             total_debt=0, cash=0, shares_outstanding=100)
    print(f"Implied-growth solver on negative base FCFF: {no_solution} (expected None)")
    assert no_solution is None, "Negative base FCFF must have no growth-rate solution!"

    # Dividend discount model check: one explicit year then terminal
    # value, hand-computed against the closed-form Gordon Growth formula.
    # D0=$2.00, g=3%, r=8%: D1 = 2.00*1.03 = 2.06
    # TV at end of year 1 = D1*(1+g)/(r-g) = 2.06*1.03/0.05 = 42.436
    # Value today = D1/(1+r) + TV/(1+r)
    d0, g, r = 2.00, 0.03, 0.08
    ddm_value = dividend_discount_value_per_share(d0, g, 0, 1, g, r)
    d1 = d0 * (1 + g)
    tv_year1 = d1 * (1 + g) / (r - g)
    expected_ddm = d1 / (1 + r) + tv_year1 / (1 + r)
    print(f"DDM value: ${ddm_value:.4f}  (expected ${expected_ddm:.4f})")
    assert abs(ddm_value - expected_ddm) < 1e-6, "DDM value doesn't match the closed-form Gordon Growth calc!"

    # DDM range: higher cost of equity (low case) should be worth less,
    # same low < base < high convention as the FCFF range.
    ddm_results = run_dividend_discount_range(d0, g, 0, 1, r, g)
    print(f"DDM range: low=${ddm_results['low']['value_per_share']:.2f}  "
          f"base=${ddm_results['base']['value_per_share']:.2f}  "
          f"high=${ddm_results['high']['value_per_share']:.2f}")
    assert ddm_results["low"]["value_per_share"] < ddm_results["base"]["value_per_share"] < ddm_results["high"]["value_per_share"], \
        "DDM range ordering is wrong — low should be < base < high!"

    # ---- Narrative DCF (revenue x margin x reinvestment) checks ----

    # Revenue path: current=$100, initial growth=40%, terminal growth=3%,
    # 4 years. Growth fades linearly: 40%, 27.667%, 15.333%, 3%.
    rev_path = project_revenue_path(100, 0.40, 0.03, years=4)
    g1 = 0.40
    g2 = 0.40 + (0.03 - 0.40) * (1 / 3)
    g3 = 0.40 + (0.03 - 0.40) * (2 / 3)
    g4 = 0.03
    r1 = 100 * (1 + g1)
    r2 = r1 * (1 + g2)
    r3 = r2 * (1 + g3)
    r4 = r3 * (1 + g4)
    print(f"\nRevenue path: {[f'{v:.4f}' for v in rev_path]}  "
          f"(expected {[f'{v:.4f}' for v in [r1, r2, r3, r4]]})")
    for computed, expected in zip(rev_path, [r1, r2, r3, r4]):
        assert abs(computed - expected) < 1e-6, "Revenue path mismatch!"

    # Margin path: current=-10% (young, unprofitable), target=20%, 4 years.
    # Converges linearly: -10 + (30)*(1/4, 2/4, 3/4, 4/4) = -2.5%, 5%, 12.5%, 20%.
    margin_path = project_margin_path(-0.10, 0.20, years=4)
    expected_margins = [-0.10 + 0.30 * (t / 4) for t in range(1, 5)]
    print(f"Margin path: {[f'{v:.4f}' for v in margin_path]}  "
          f"(expected {[f'{v:.4f}' for v in expected_margins]})")
    for computed, expected in zip(margin_path, expected_margins):
        assert abs(computed - expected) < 1e-9, "Margin path mismatch!"
    assert abs(margin_path[-1] - 0.20) < 1e-9, "Margin path must hit target_margin exactly in the final year!"

    # EBIT path = revenue x margin, element-wise.
    ebit_path = project_ebit_path(rev_path, margin_path)
    for computed, rev, mar in zip(ebit_path, rev_path, margin_path):
        assert abs(computed - rev * mar) < 1e-6, "EBIT path mismatch!"

    # Reinvestment from sales-to-capital ratio: base_revenue=100,
    # sales_to_capital=2.0 -> reinvestment_t = (rev_t - rev_{t-1}) / 2.0.
    reinv_path = reinvestment_from_sales_to_capital(rev_path, 2.0, base_revenue=100)
    expected_reinv = [(r1 - 100) / 2.0, (r2 - r1) / 2.0, (r3 - r2) / 2.0, (r4 - r3) / 2.0]
    print(f"Reinvestment path: {[f'{v:.4f}' for v in reinv_path]}  "
          f"(expected {[f'{v:.4f}' for v in expected_reinv]})")
    for computed, expected in zip(reinv_path, expected_reinv):
        assert abs(computed - expected) < 1e-6, "Reinvestment path mismatch!"

    # A shrinking-revenue year must produce NEGATIVE reinvestment (capital
    # released), not a floored-at-zero value — matches Damodaran's own
    # treatment (see the docstring).
    shrinking_reinv = reinvestment_from_sales_to_capital([90], 2.0, base_revenue=100)
    print(f"Reinvestment on a shrinking year: {shrinking_reinv[0]:.4f}  (expected {(90 - 100) / 2.0:.4f})")
    assert shrinking_reinv[0] < 0, "A revenue decline must produce negative reinvestment!"

    # FCFF narrative = EBIT*(1-tax) - reinvestment, flat tax rate.
    fcff_narrative_path = project_fcff_narrative(rev_path, margin_path, 0.25, 2.0, base_revenue=100)
    expected_fcff_narrative = [e * 0.75 - ri for e, ri in zip(ebit_path, reinv_path)]
    print(f"Narrative FCFF path: {[f'{v:.4f}' for v in fcff_narrative_path]}  "
          f"(expected {[f'{v:.4f}' for v in expected_fcff_narrative]})")
    for computed, expected in zip(fcff_narrative_path, expected_fcff_narrative):
        assert abs(computed - expected) < 1e-6, "Narrative FCFF path mismatch!"

    # Year 1 here should be NEGATIVE: heavy reinvestment (40% growth off a
    # $100 base, divided by a 2.0 sales-to-capital ratio) against a still-
    # deeply-negative margin (-2.5%) — exactly the "FCFF ends up negative
    # in the early years, as it should" behavior from the Ginzu framework,
    # which the old single-growth-rate model couldn't represent at all.
    assert fcff_narrative_path[0] < 0, "A young, unprofitable, heavily-reinvesting year 1 should show negative FCFF!"

    # A list tax_rate (future NOL ramp) must line up element-wise with a
    # flat rate repeated the same number of times.
    fcff_flat_tax = project_fcff_narrative(rev_path, margin_path, 0.25, 2.0, base_revenue=100)
    fcff_list_tax = project_fcff_narrative(rev_path, margin_path, [0.25, 0.25, 0.25, 0.25], 2.0, base_revenue=100)
    for a, b in zip(fcff_flat_tax, fcff_list_tax):
        assert abs(a - b) < 1e-9, "A repeated-flat-rate list must match the scalar tax_rate case!"

    # project_narrative_path should bundle the same five paths computed
    # individually above.
    bundle = project_narrative_path(100, 0.40, -0.10, 0.20, 0.25, 2.0, 0.03, years=4)
    assert bundle["revenue"] == rev_path and bundle["margin"] == margin_path, \
        "project_narrative_path's bundled paths must match the individually-computed ones!"

    # ---- NOL tax-rate ramp ----
    # EBIT path: -100 (loss, adds to the NOL pool), 50, 80, 120. Starting
    # NOL=100, marginal rate=25%.
    #   Yr1: EBIT=-100 -> rate=0, pool = 100 + 100 = 200
    #   Yr2: EBIT=50  -> sheltered=min(50,200)=50,  taxable=0,  pool=150, rate=0
    #   Yr3: EBIT=80  -> sheltered=min(80,150)=80,  taxable=0,  pool=70,  rate=0
    #   Yr4: EBIT=120 -> sheltered=min(120,70)=70,  taxable=50, pool=0,
    #        rate = (50*0.25)/120 = 0.104166...
    nol_rates = nol_tax_rate_path([-100, 50, 80, 120], nol_balance=100, marginal_tax_rate=0.25)
    expected_nol_rates = [0.0, 0.0, 0.0, (50 * 0.25) / 120]
    print(f"NOL tax-rate path: {[f'{r:.6f}' for r in nol_rates]}  "
          f"(expected {[f'{r:.6f}' for r in expected_nol_rates]})")
    for computed, expected in zip(nol_rates, expected_nol_rates):
        assert abs(computed - expected) < 1e-9, "NOL tax-rate path mismatch!"
    assert nol_rates[0] == 0.0 and nol_rates[1] == 0.0, \
        "Early years fully sheltered by NOLs must show a 0% effective rate!"
    assert abs(nol_rates[-1] - 0.25) < 0.25 and nol_rates[-1] > 0, \
        "The final year (NOL exhausted) should show a PARTIAL rate between 0% and the marginal rate!"

    # With NO NOL balance at all, every positive-EBIT year should pay
    # exactly the marginal rate — the "rises to the marginal rate"
    # ceiling, confirmed as an actual ceiling, not just rhetoric.
    no_nol_rates = nol_tax_rate_path([50, 80, 120], nol_balance=0, marginal_tax_rate=0.25)
    assert all(abs(r - 0.25) < 1e-9 for r in no_nol_rates), \
        "With zero NOL balance, every positive-EBIT year must pay the full marginal rate!"

    # Feeding an NOL-driven rate list into project_fcff_narrative should
    # match a hand-computed FCFF using those exact per-year rates.
    ebit_for_nol = [-100, 50, 80, 120]
    nol_fcff = project_fcff_narrative([100, 150, 230, 350], [-1.0, 1 / 3, 80 / 230, 120 / 350],
                                       nol_rates, 1.0, base_revenue=100)
    # EBIT path here is engineered to match ebit_for_nol exactly (revenue x margin):
    for e, r, rev, mar in zip(ebit_for_nol, nol_rates, [100, 150, 230, 350],
                               [-1.0, 1 / 3, 80 / 230, 120 / 350]):
        assert abs(rev * mar - e) < 1e-9, "Test setup error: revenue x margin must reproduce ebit_for_nol!"
    expected_nol_fcff = [e * (1 - r) - reinv for e, r, reinv in
                         zip(ebit_for_nol, nol_rates,
                             reinvestment_from_sales_to_capital([100, 150, 230, 350], 1.0, 100))]
    print(f"Narrative FCFF with NOL ramp: {[f'{v:.4f}' for v in nol_fcff]}  "
          f"(expected {[f'{v:.4f}' for v in expected_nol_fcff]})")
    for computed, expected in zip(nol_fcff, expected_nol_fcff):
        assert abs(computed - expected) < 1e-6, "Narrative FCFF with NOL-driven tax rate mismatch!"

    # Enterprise value: discount the FCFF path and add the discounted,
    # reinvestment-consistent terminal value built from final-year NOPAT:
    #   NOPAT_4 = revenue_4 x 20% target margin x (1 - 25%)
    #   TV = NOPAT_4 x 1.03 x (1 - 0.03 / 0.12) / (0.12 - 0.03)
    # (return on new capital defaults to the 12% discount rate, so
    # reinvestment is 25% of NOPAT).
    disc_rate = 0.12
    ev_narrative = enterprise_value_narrative(100, 0.40, -0.10, 0.20, 0.25, 2.0, disc_rate, 0.03, years=4)
    expected_discounted = sum(discount_cash_flows(fcff_narrative_path, disc_rate))
    final_nopat_4 = project_revenue_path(100, 0.40, 0.03, 4)[-1] * 0.20 * 0.75
    expected_tv = (final_nopat_4 * 1.03 * (1 - 0.03 / 0.12) / (0.12 - 0.03)) / ((1 + disc_rate) ** 4)
    print(f"Narrative EV: ${ev_narrative:,.2f}  (expected ${expected_discounted + expected_tv:,.2f})")
    assert abs(ev_narrative - (expected_discounted + expected_tv)) < 1e-6, "Narrative enterprise value mismatch!"

    # Narrative valuation range: same low < base < high ordering as every
    # other range function (lower discount rate / higher terminal growth
    # in the HIGH case should be worth more).
    narrative_range = run_valuation_range_narrative(100, 0.40, -0.10, 0.20, 0.25, 2.0,
                                                     disc_rate, 0.03,
                                                     total_debt=50, cash=20, shares_outstanding=10,
                                                     years=4)
    print(f"Narrative range: low=${narrative_range['low']['value_per_share']:.2f}  "
          f"base=${narrative_range['base']['value_per_share']:.2f}  "
          f"high=${narrative_range['high']['value_per_share']:.2f}  "
          f"(ordering_warning: {narrative_range['ordering_warning']})")
    assert narrative_range["low"]["value_per_share"] < narrative_range["base"]["value_per_share"] < narrative_range["high"]["value_per_share"], \
        "Narrative range ordering is wrong — low should be < base < high!"
    assert narrative_range["ordering_warning"] is None, \
        "A normally-ordered range must report ordering_warning=None!"

    # Ordering-INVERSION detection: a young, heavily-levered, deeply-
    # negative-margin growth story (most of the explicit-period FCFF
    # negative) can flip the usual low < base < high relationship — a
    # higher discount rate shrinks those negative present values toward
    # zero FASTER than it shrinks the terminal value. Found on a real
    # run (TeraWulf) during sanity-checking; reproduced here with round
    # numbers of the same shape so it's self-contained. The three
    # numbers are each still computed correctly — only the ordering
    # assumption breaks — so this must be FLAGGED, not silently hidden.
    inverted_range = run_valuation_range_narrative(1000, 0.35, -1.10, 0.20, 0.08, 1.0, 0.24, 0.025,
                                                    total_debt=3000, cash=2000, shares_outstanding=100,
                                                    years=10)
    print(f"Inverted-case range: low=${inverted_range['low']['value_per_share']:.2f}  "
          f"base=${inverted_range['base']['value_per_share']:.2f}  "
          f"high=${inverted_range['high']['value_per_share']:.2f}  "
          f"(ordering_warning: {'set' if inverted_range['ordering_warning'] else 'None'})")
    assert not (inverted_range["low"]["value_per_share"] < inverted_range["base"]["value_per_share"] < inverted_range["high"]["value_per_share"]), \
        "This case is supposed to reproduce the inversion — if it's not inverted, the repro case itself needs revisiting!"
    assert inverted_range["ordering_warning"] is not None, \
        "An inverted range MUST set ordering_warning instead of staying silent!"
    assert "out of order" in inverted_range["ordering_warning"], \
        "ordering_warning should explain what went wrong, not just be a generic truthy value!"

    # A higher target margin, all else equal, must produce a higher
    # enterprise value — the "most important input" claim should hold up
    # mechanically, not just rhetorically.
    ev_low_margin = enterprise_value_narrative(100, 0.40, -0.10, 0.10, 0.25, 2.0, disc_rate, 0.03, years=4)
    ev_high_margin = enterprise_value_narrative(100, 0.40, -0.10, 0.30, 0.25, 2.0, disc_rate, 0.03, years=4)
    print(f"EV at 10% target margin: ${ev_low_margin:,.2f}  vs 30% target margin: ${ev_high_margin:,.2f}")
    assert ev_high_margin > ev_low_margin, "A higher target margin should produce a higher enterprise value!"

    # A tighter (lower) sales-to-capital ratio means more capital needed
    # per dollar of growth, i.e. heavier reinvestment drag — enterprise
    # value should come out LOWER, all else equal.
    ev_efficient_capital = enterprise_value_narrative(100, 0.40, -0.10, 0.20, 0.25, 4.0, disc_rate, 0.03, years=4)
    ev_capital_hungry = enterprise_value_narrative(100, 0.40, -0.10, 0.20, 0.25, 1.0, disc_rate, 0.03, years=4)
    print(f"EV at sales-to-capital=4.0: ${ev_efficient_capital:,.2f}  "
          f"vs sales-to-capital=1.0: ${ev_capital_hungry:,.2f}")
    assert ev_efficient_capital > ev_capital_hungry, \
        "A more capital-efficient (higher sales-to-capital) business should be worth more!"

    # ---- Declining discount-rate glide path ----

    # project_wacc_path: initial=20%, mature=10%, 5 years. Fades
    # linearly: year1=20% exactly, year5=10% exactly, same convention
    # as project_revenue_path.
    wacc_path_test = project_wacc_path(0.20, 0.10, years=5)
    expected_wacc_path = [0.20 + (0.10 - 0.20) * (t / 4) for t in range(5)]
    print(f"WACC glide path: {[f'{r:.4f}' for r in wacc_path_test]}  "
          f"(expected {[f'{r:.4f}' for r in expected_wacc_path]})")
    for computed, expected in zip(wacc_path_test, expected_wacc_path):
        assert abs(computed - expected) < 1e-9, "WACC glide path mismatch!"
    assert abs(wacc_path_test[0] - 0.20) < 1e-9, "Year 1 of the glide must equal initial_wacc exactly!"
    assert abs(wacc_path_test[-1] - 0.10) < 1e-9, "The final year of the glide must equal mature_wacc exactly!"

    # discount_cash_flows_variable_rate: a CONSTANT wacc_path must
    # reproduce discount_cash_flows's result exactly — this is the
    # critical correctness check for the cumulative-product math.
    flat_cfs = [100, 120, 140]
    flat_discounted = discount_cash_flows(flat_cfs, 0.10)
    variable_discounted_constant = discount_cash_flows_variable_rate(flat_cfs, [0.10, 0.10, 0.10])
    print(f"Variable-rate discounting at a CONSTANT rate: {[f'{v:.4f}' for v in variable_discounted_constant]}  "
          f"(expected {[f'{v:.4f}' for v in flat_discounted]})")
    for computed, expected in zip(variable_discounted_constant, flat_discounted):
        assert abs(computed - expected) < 1e-9, \
            "A constant wacc_path must reproduce discount_cash_flows's result exactly!"

    # Hand-computed check with genuinely DIFFERENT rates per year:
    # CF=[100,100], rates=[0.20, 0.10].
    #   Yr1: 100 / 1.20 = 83.3333
    #   Yr2: 100 / (1.20 * 1.10) = 100 / 1.32 = 75.7576
    varying_discounted = discount_cash_flows_variable_rate([100, 100], [0.20, 0.10])
    expected_varying = [100 / 1.20, 100 / (1.20 * 1.10)]
    print(f"Variable-rate discounting, varying rates: {[f'{v:.4f}' for v in varying_discounted]}  "
          f"(expected {[f'{v:.4f}' for v in expected_varying]})")
    for computed, expected in zip(varying_discounted, expected_varying):
        assert abs(computed - expected) < 1e-6, "Variable-rate (cumulative product) discounting mismatch!"

    # enterprise_value_narrative with a WACC glide (discount_rate as a
    # list): must SANDWICH between "flat at the high initial rate the
    # whole time" (lowest value — a higher rate always reduces PV) and
    # "flat at the low mature rate the whole time" (highest value).
    glide = project_wacc_path(0.20, 0.08, years=4)
    ev_glide = enterprise_value_narrative(100, 0.40, -0.10, 0.20, 0.25, 2.0, glide, 0.03, years=4)
    ev_flat_high = enterprise_value_narrative(100, 0.40, -0.10, 0.20, 0.25, 2.0, 0.20, 0.03, years=4)
    ev_flat_low = enterprise_value_narrative(100, 0.40, -0.10, 0.20, 0.25, 2.0, 0.08, 0.03, years=4)
    print(f"EV glide path: ${ev_glide:,.2f}  (flat-at-initial ${ev_flat_high:,.2f}, "
          f"flat-at-mature ${ev_flat_low:,.2f})")
    assert ev_flat_high < ev_glide < ev_flat_low, \
        "A declining WACC glide's EV must sit strictly between the two flat-rate extremes!"

    # run_valuation_range_narrative with discount_rate as a tuple
    # (initial_wacc, mature_wacc): builds a glide per scenario, flexing
    # BOTH endpoints. Normal low < base < high ordering should still
    # hold on a mostly-positive-FCFF example, and each result should
    # carry its own wacc_path.
    glide_range = run_valuation_range_narrative(100, 0.40, -0.10, 0.20, 0.25, 2.0,
                                                 (0.20, 0.08), 0.03,
                                                 total_debt=50, cash=20, shares_outstanding=10,
                                                 years=4)
    print(f"Glide range: low=${glide_range['low']['value_per_share']:.2f}  "
          f"base=${glide_range['base']['value_per_share']:.2f}  "
          f"high=${glide_range['high']['value_per_share']:.2f}  "
          f"(ordering_warning: {glide_range['ordering_warning']})")
    assert glide_range["low"]["value_per_share"] < glide_range["base"]["value_per_share"] < glide_range["high"]["value_per_share"], \
        "Glide-path range ordering is wrong — low should be < base < high!"
    assert glide_range["ordering_warning"] is None, "A normally-ordered glide range must report ordering_warning=None!"
    assert glide_range["base"]["wacc_path"] == project_wacc_path(0.20, 0.08, years=4), \
        "The base scenario's wacc_path must match the un-flexed (initial_wacc, mature_wacc) glide!"
    assert glide_range["low"]["wacc_path"][0] > glide_range["base"]["wacc_path"][0], \
        "The LOW scenario's glide must start HIGHER than base (more conservative)!"

    # Passing a flat float (not a tuple) to run_valuation_range_narrative
    # must behave EXACTLY as before — backward compatibility, not just
    # "doesn't crash."
    flat_range_regression = run_valuation_range_narrative(100, 0.40, -0.10, 0.20, 0.25, 2.0,
                                                           disc_rate, 0.03,
                                                           total_debt=50, cash=20, shares_outstanding=10,
                                                           years=4)
    assert flat_range_regression["base"]["wacc_path"] is None, \
        "A flat discount_rate must NOT produce a wacc_path — only the glide case should!"
    for label in ("low", "base", "high"):
        assert abs(flat_range_regression[label]["value_per_share"] - narrative_range[label]["value_per_share"]) < 1e-9, \
            "Passing a flat float must reproduce the original (pre-glide-feature) narrative range exactly!"

    # ---- Failure-probability overlay ----

    # estimate_runway_years: cash=100, burn=25 -> 4.0 years.
    assert abs(estimate_runway_years(100, 25) - 4.0) < 1e-9, "Runway calculation mismatch!"
    # No burn at all (FCFF already non-negative) -> infinite runway.
    assert estimate_runway_years(100, 0) == float("inf"), "Zero burn rate must mean infinite runway!"
    assert estimate_runway_years(100, -50) == float("inf"), "A negative 'burn rate' (i.e. positive FCFF) must mean infinite runway!"

    # failure_probability_from_runway: exact threshold boundaries.
    assert failure_probability_from_runway(5.0) == 0.02, "Runway exactly at a threshold must hit that threshold's bucket!"
    assert failure_probability_from_runway(4.9) == 0.05, "Just under a threshold must fall to the NEXT (higher-probability) bucket!"
    assert failure_probability_from_runway(0.3) == 0.50, "Very short runway must hit the worst (highest-probability) bucket!"
    assert failure_probability_from_runway(float("inf")) == 0.02, "Infinite runway must hit the best (lowest-probability) bucket!"
    print(f"Failure probability at runway=4.0yr: {failure_probability_from_runway(4.0):.0%}  "
          f"(expected {0.05:.0%})")
    assert failure_probability_from_runway(4.0) == 0.05, "Runway lookup mismatch at 4.0 years!"

    # The table itself must be monotonic: more runway can never mean a
    # HIGHER failure probability than less runway — if someone edits the
    # table later, this catches a broken ordering immediately.
    sorted_table = sorted(FAILURE_PROBABILITY_TABLE, key=lambda pair: pair[0])
    for (shorter_runway, higher_or_equal_prob), (longer_runway, lower_or_equal_prob) in zip(sorted_table, sorted_table[1:]):
        assert higher_or_equal_prob >= lower_or_equal_prob, \
            "FAILURE_PROBABILITY_TABLE must be monotonic — more runway can't mean a higher failure probability!"

    # failure_adjusted_value_per_share: going_concern=$10/share, P(fail)=20%,
    # book_equity=$500, shares=100, haircut=50%.
    #   liquidation value/share = 500*0.5/100 = 2.5
    #   blended = 0.8*10 + 0.2*2.5 = 8 + 0.5 = 8.5
    blended, liq_value = failure_adjusted_value_per_share(10.0, 0.20, 500, 100, liquidation_haircut=0.50)
    print(f"Failure-adjusted value: ${blended:.4f}  (expected $8.5000), liquidation value: ${liq_value:.4f}  (expected $2.5000)")
    assert abs(liq_value - 2.5) < 1e-9, "Liquidation value per share mismatch!"
    assert abs(blended - 8.5) < 1e-9, "Failure-adjusted blended value mismatch!"

    # An invalid probability must raise, not silently produce nonsense.
    try:
        failure_adjusted_value_per_share(10.0, 1.5, 500, 100)
        raise AssertionError("failure_adjusted_value_per_share must reject probability_of_failure outside [0, 1]!")
    except ValueError:
        pass

    # Monotonicity: holding going_concern_value > liquidation_value,
    # raising the failure probability must pull the blended value DOWN
    # toward (never past) the liquidation value.
    low_fail_prob, _ = failure_adjusted_value_per_share(10.0, 0.05, 500, 100, 0.50)
    high_fail_prob, _ = failure_adjusted_value_per_share(10.0, 0.50, 500, 100, 0.50)
    print(f"Blended value at P(fail)=5%: ${low_fail_prob:.4f}  vs P(fail)=50%: ${high_fail_prob:.4f}")
    assert low_fail_prob > high_fail_prob > liq_value, \
        "A higher failure probability must pull the blended value DOWN toward the liquidation value!"

    # ordering_warning_for_range (the refactored-out helper): same
    # behavior as before the refactor, callable standalone on any three
    # numbers — including AFTER a failure-probability blend, since that
    # blend is a same-sign linear transform that preserves whatever
    # ordering the going-concern numbers already had.
    assert ordering_warning_for_range(1.0, 2.0, 3.0) is None, "Normally-ordered triple must report no warning!"
    assert ordering_warning_for_range(-6.57, -6.61, -6.66) is not None, "An inverted triple must report a warning!"
    inverted_low, _ = failure_adjusted_value_per_share(-6.57, 0.30, 500, 100, 0.50)
    inverted_base, _ = failure_adjusted_value_per_share(-6.61, 0.30, 500, 100, 0.50)
    inverted_high, _ = failure_adjusted_value_per_share(-6.66, 0.30, 500, 100, 0.50)
    assert ordering_warning_for_range(inverted_low, inverted_base, inverted_high) is not None, \
        "An inverted going-concern ordering must STILL be flagged after the failure-probability blend!"

    # PHASE_APPROVAL_PROBABILITY: later phases must never be LESS likely
    # to reach approval than earlier ones, and the Phase 2 figure must
    # match the product of its published transition rates.
    phase_order = ["preclinical", "phase1", "phase2", "phase3", "filed", "approved"]
    for earlier, later in zip(phase_order, phase_order[1:]):
        assert PHASE_APPROVAL_PROBABILITY[earlier] < PHASE_APPROVAL_PROBABILITY[later], \
            "PHASE_APPROVAL_PROBABILITY must rise with each later phase!"
    assert abs(PHASE_APPROVAL_PROBABILITY["phase2"] - 0.289 * 0.578 * 0.906) < 0.001, \
        "Phase 2 approval probability must equal the product of its remaining transition rates!"

    # drug_commercial_cash_flows: peak $100, launch after 1 yr, 2-yr ramp,
    # 3 yrs exclusivity, 50% margin, 20% tax ->
    #   [0, 100*0.5*0.5*0.8=20, 100*1.0*0.5*0.8=40, 40]
    drug_cfs = drug_commercial_cash_flows(100, 1, 2, 3, 0.50, 0.20)
    print(f"Drug cash flows: {drug_cfs}  (expected [0, 20, 40, 40])")
    assert drug_cfs == [0.0, 20.0, 40.0, 40.0], "Drug commercial cash flow mismatch!"

    # drug_rnpv: peak $100, launch after 1 yr, 1-yr ramp, 2 yrs exclusivity,
    # 50% margin, 0% tax, 10% discount rate, $10 dev cost, P(approval)=50%.
    #   commercial CFs = [0, 50, 50]; PV = 50/1.1^2 + 50/1.1^3
    #   dev cost PV = 10/1.1
    #   rNPV = 0.5 * PV(commercial) - PV(dev)
    rnpv = drug_rnpv(100, 1, 1, 2, 0.50, 0.0, 0.10, 10, 0.50)
    expected_pv_commercial = 50 / 1.1 ** 2 + 50 / 1.1 ** 3
    expected_rnpv = 0.5 * expected_pv_commercial - 10 / 1.1
    print(f"Drug rNPV: {rnpv['rnpv']:.4f}  (expected {expected_rnpv:.4f})")
    assert abs(rnpv["pv_commercial"] - expected_pv_commercial) < 1e-9, "Drug PV of commercial cash flows mismatch!"
    assert abs(rnpv["rnpv"] - expected_rnpv) < 1e-9, "Drug rNPV mismatch!"
    # An already-approved drug's remaining cost is charged today, undiscounted.
    approved = drug_rnpv(100, 0, 1, 1, 0.50, 0.0, 0.10, 10, 1.0)
    assert abs(approved["rnpv"] - (50 / 1.1 - 10)) < 1e-9, "Approved-drug rNPV mismatch!"

    # patent_option_value: textbook Black-Scholes check with the dividend
    # yield forced to zero — S=100, K=100, t=1, sigma=20%, r=5% -> 10.4506.
    option = patent_option_value(100, 100, 1, 0.20, 0.05, dividend_yield=0.0)
    print(f"Option value (no dividend yield): {option['value']:.4f}  (expected 10.4506)")
    assert abs(option["value"] - 10.4506) < 1e-4, "Black-Scholes option value mismatch!"
    # Default cost of delay is 1/t, and charging it must LOWER the value.
    option_with_delay_cost = patent_option_value(100, 100, 10, 0.20, 0.05)
    option_without = patent_option_value(100, 100, 10, 0.20, 0.05, dividend_yield=0.0)
    assert abs(option_with_delay_cost["dividend_yield"] - 0.10) < 1e-12, "Default dividend yield must be 1/t!"
    assert option_with_delay_cost["value"] < option_without["value"], \
        "Charging a cost of delay must lower the patent option's value!"

    # compute_ffo / compute_affo: net income $100, depreciation $80,
    # impairments $5, gains on sale $15 -> FFO = 100 + 80 + 5 - 15 = 170;
    # maintenance capex $20 -> AFFO = 150.
    ffo = compute_ffo(100, 80, 15, 5)
    affo = compute_affo(ffo, 20)
    print(f"FFO: {ffo}  (expected 170)   AFFO: {affo}  (expected 150)")
    assert ffo == 170 and affo == 150, "FFO/AFFO mismatch!"
    # A LOSS on sale (negative gain) must be added back, not subtracted.
    assert compute_ffo(100, 80, -10) == 190, "A loss on property sales must increase FFO!"

    # reit_fundamental_growth: dividends $120, AFFO $150, book equity $1,500
    #   payout = 0.8, retention = 0.2, ROE = 0.10, growth = 0.02
    reit_growth = reit_fundamental_growth(120, 150, 1500)
    print(f"REIT fundamental growth: {reit_growth['growth']:.4f}  (expected 0.0200)")
    assert abs(reit_growth["payout_ratio"] - 0.8) < 1e-12, "REIT payout ratio mismatch!"
    assert abs(reit_growth["growth"] - 0.02) < 1e-12, "REIT fundamental growth mismatch!"
    # A dividend above AFFO must show as NEGATIVE growth, not be floored.
    assert reit_fundamental_growth(180, 150, 1500)["growth"] < 0, \
        "A dividend above AFFO must produce negative fundamental growth!"

    # reit_nav_per_share: NOI $60, cap rate 6% -> property value $1,000;
    # + cash $50 - debt $400 - preferred $50 = NAV $600; / 100 shares = $6.
    nav = reit_nav_per_share(60, 0.06, 50, 400, 50, 100)
    print(f"REIT NAV per share: {nav['nav_per_share']:.4f}  (expected 6.0000)")
    assert abs(nav["property_value"] - 1000) < 1e-9, "REIT property value mismatch!"
    assert abs(nav["nav_per_share"] - 6.0) < 1e-9, "REIT NAV per share mismatch!"

    # share_count_growth_rate: 100 -> 121 over 2 years = 10%/yr.
    dilution = share_count_growth_rate([100, 110, 121])
    print(f"Share count growth: {dilution:.4f}  (expected 0.1000)")
    assert abs(dilution - 0.10) < 1e-12, "Share count growth mismatch!"
    assert share_count_growth_rate([100]) is None, "One data point must return None!"

    # cycle_margin_stats: three years — EBIT 20 on 100 (20%), -10 on 50
    # (-20%), 30 on 150 (20%).
    #   mean = (0.20 - 0.20 + 0.20) / 3 = 0.0667
    #   aggregate = (20 - 10 + 30) / (100 + 50 + 150) = 40 / 300 = 0.1333
    #   median = 0.20
    cycle = cycle_margin_stats([20, -10, 30], [100, 50, 150])
    print(f"Cycle margins: mean {cycle['mean']:.4f} (expected 0.0667), aggregate "
          f"{cycle['aggregate']:.4f} (expected 0.1333), median {cycle['median']:.4f} (expected 0.2000)")
    assert abs(cycle["mean"] - 0.2 / 3) < 1e-12, "Cycle mean margin mismatch!"
    assert abs(cycle["aggregate"] - 40 / 300) < 1e-12, "Cycle aggregate margin mismatch!"
    assert abs(cycle["median"] - 0.20) < 1e-12, "Cycle median margin mismatch!"
    # The loss year must be KEPT — dropping it is exactly the upward bias
    # this function exists to avoid.
    assert cycle["n_years"] == 3, "Loss years must count toward the cycle average!"
    # Even-count median averages the middle two; zero-revenue years are skipped.
    even = cycle_margin_stats([10, 20, 5, 0], [100, 100, 100, 0])
    assert even["n_years"] == 3 and even["margins"][3] is None, "Zero-revenue year must be skipped!"
    assert abs(cycle_margin_stats([10, 30], [100, 100])["median"] - 0.20) < 1e-12, \
        "Even-count median must average the middle two margins!"

    # Value breakdowns must add up to the bare EV, for both DCFs.
    bd = enterprise_value_multi_stage(100, 0.05, 2, 0, 0.02, 0.10, base_nopat=150, return_breakdown=True)
    assert abs(bd["ev"] - (105 / 1.1 + 110.25 / 1.21 + 1686.825 / 1.21)) < 1e-6 \
        and abs(bd["pv_explicit"] + bd["pv_terminal"] - bd["ev"]) < 1e-9, \
        "Multi-stage breakdown must add up to the enterprise value!"
    assert abs(bd["pv_terminal"] - 1686.825 / 1.21) < 1e-6, "Multi-stage terminal PV mismatch!"
    bd_n = enterprise_value_narrative(100, 0.40, -0.10, 0.20, 0.25, 2.0, 0.12, 0.03, years=4, return_breakdown=True)
    assert abs(bd_n["ev"] - enterprise_value_narrative(100, 0.40, -0.10, 0.20, 0.25, 2.0, 0.12, 0.03, years=4)) < 1e-9, \
        "Narrative breakdown EV must match the bare EV!"

    # narrative_roic_path: capital 100, reinvest [10, 20], EBIT [20, 33], tax 25%:
    #   year 1: 20 x 0.75 / 100 = 15%;  year 2: 33 x 0.75 / (100 + 10) = 22.5%
    roic_path = narrative_roic_path(100, [10, 20], [20, 33], 0.25)
    print(f"Narrative ROIC path: {roic_path}  (expected [0.15, 0.225])")
    assert abs(roic_path[0] - 0.15) < 1e-12 and abs(roic_path[1] - 0.225) < 1e-12, "Narrative ROIC path mismatch!"
    assert narrative_roic_path(0, [10], [20], 0.25) == [None], "Non-positive starting capital must give None!"

    # weighted_unlevered_beta: 60% in a 0.64 business, 40% in a 0.82 one
    #   = 0.6*0.64 + 0.4*0.82 = 0.384 + 0.328 = 0.712; weights in dollars
    #   (600, 400) must give the same answer, since they're normalized.
    mixed_beta = weighted_unlevered_beta([(0.64, 0.6), (0.82, 0.4)])
    print(f"Weighted unlevered beta: {mixed_beta:.4f}  (expected 0.7120)")
    assert abs(mixed_beta - 0.712) < 1e-12, "Weighted unlevered beta mismatch!"
    assert abs(weighted_unlevered_beta([(0.64, 600), (0.82, 400)]) - 0.712) < 1e-12, \
        "Weights must be normalized, so dollar weights give the same beta!"
    # Relevering the industry beta: 0.64 at D/E 0.5, 25% tax = 0.64*(1+0.75*0.5) = 0.88.
    assert abs(relever_beta(0.64, 0.25, 0.5) - 0.88) < 1e-12, "Relevered beta mismatch!"

    # stable_growth_fcff: NOPAT 100, g 3%, RONIC 12% -> reinvest 25%:
    #   100 x 1.03 x 0.75 = 77.25
    stable_fcff = stable_growth_fcff(100, 0.03, 0.12)
    print(f"Stable-growth FCFF: {stable_fcff:.4f}  (expected 77.2500)")
    assert abs(stable_fcff - 77.25) < 1e-9, "Stable-growth FCFF mismatch!"
    # With RONIC = discount rate (the default), TV = NOPAT x (1+g) / r, so
    # growth that only earns its cost of capital adds (almost) nothing.
    for g in (0.01, 0.03, 0.05):
        tv_c = terminal_value_consistent(100, g, 0.10)
        assert abs(tv_c - 100 * (1 + g) / 0.10) < 1e-9, \
            "With RONIC = discount rate, TV must equal NOPAT x (1+g) / r!"
    # A moat (RONIC above the discount rate) makes growth worth something.
    assert terminal_value_consistent(100, 0.03, 0.10, 0.20) > terminal_value_consistent(100, 0.03, 0.10), \
        "RONIC above the cost of capital must raise the terminal value!"
    try:
        terminal_value_consistent(100, 0.03, 0.10, 0.0)
        raise AssertionError("A non-positive return on new capital must be rejected!")
    except ValueError:
        pass

    # enterprise_value_multi_stage with base_nopat: FCFF 100, NOPAT 150,
    # 2 yrs at 5%, no fade, g 2%, r 10%:
    #   FCFF path [105, 110.25]; NOPAT_2 = 150 x 1.05^2 = 165.375
    #   TV = 165.375 x 1.02 x (1 - 0.02/0.10) / 0.08 = 1686.825
    #   EV = 105/1.1 + 110.25/1.21 + 1686.825/1.21
    # Working capital, worked by hand: FCFF 100 growing 5% for 2 years, revenue
    # 1000 -> 1050 -> 1102.5, 10% of revenue tied up in working capital, so
    # year-1 FCFF 105 - 0.10 x 50 = 100, year-2 110.25 - 0.10 x 52.5 = 105.
    # The consistent terminal value (from NOPAT) is unchanged.
    wc_bd = enterprise_value_multi_stage(100, 0.05, 2, 0, 0.02, 0.10, base_nopat=150, return_breakdown=True,
                                         base_revenue=1000, working_capital_ratio=0.10)
    assert abs(wc_bd["pv_explicit"] - (100 / 1.10 + 105 / 1.10 ** 2)) < 1e-9
    assert abs(wc_bd["pv_terminal"] - bd["pv_terminal"]) < 1e-9
    # A negative ratio releases cash as revenue grows.
    assert enterprise_value_multi_stage(100, 0.05, 2, 0, 0.02, 0.10, base_nopat=150, base_revenue=1000,
                                        working_capital_ratio=-0.10) > bd["ev"]
    ev_consistent = enterprise_value_multi_stage(100, 0.05, 2, 0, 0.02, 0.10, base_nopat=150)
    expected_ev_consistent = 105 / 1.1 + 110.25 / 1.21 + 1686.825 / 1.21
    print(f"Multi-stage EV, consistent TV: {ev_consistent:.4f}  (expected {expected_ev_consistent:.4f})")
    assert abs(ev_consistent - expected_ev_consistent) < 1e-6, "Consistent multi-stage EV mismatch!"
    # Without base_nopat the older behavior is unchanged.
    assert abs(enterprise_value_multi_stage(100, 0.05, 2, 0, 0.02, 0.10)
               - (105 / 1.1 + 110.25 / 1.21 + terminal_value(110.25, 0.02, 0.10) / 1.21)) < 1e-6, \
        "Multi-stage EV without base_nopat must keep the old terminal value!"


    # synthetic_credit_rating with published spreads: coverage 2.6x on the
    # large-firm table is BBB (illustrative 1.90%); a published BBB spread
    # of 1.11% must replace it, and a rating the published table lacks
    # keeps the built-in spread.
    assert synthetic_credit_rating(2.6, True) == ("BBB", 0.0190), "Built-in BBB spread mismatch!"
    assert synthetic_credit_rating(2.6, True, {"BBB": 0.0111}) == ("BBB", 0.0111), \
        "A published spread must replace the built-in one!"
    assert synthetic_credit_rating(10, True, {"BBB": 0.0111}) == ("AAA", 0.0069), \
        "A rating missing from the published table must keep the built-in spread!"
    published_cod = synthetic_cost_of_debt(260, 100, 0.05, True, {"BBB": 0.0111})
    assert abs(published_cod[0] - 0.0611) < 1e-12, "Synthetic cost of debt must use the published spread!"

    print("\n✓ All self-tests passed. The math is internally consistent.")
