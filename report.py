"""
report.py — one command, one company: its record, what its price implies,
and how the value moves across stories.

valuation.py asks you ~60 questions and prints one valuation. This runs it
once on sensible defaults, captures the narrative model's inputs, and then
answers the questions an investor actually has:

  1. What has this business done?  Revenue growth, margins over its cycle,
     return on capital, reinvestment.
  2. What does the price assume?  The starting growth the price needs at
     today's margin and at the cycle-average margin, and the margin it
     needs at the company's own historical growth.
  3. How does value move?  A grid of growth x margin stories, each a value
     per share, beside the price.
  4. What to distrust.  Every data warning from the run.

Banks and brokers get the excess return model instead (--financial), REITs
the dividend model (--reit). Pre-production companies still need
valuation.py --project with the technical report's numbers.

USAGE (from the dcf-tool folder):
    python3 report.py NVDA
    python3 report.py KO PEP          # several, one report each

Writes reports/<TICKER>_<date>.md and prints it. Not investment advice.
"""

import argparse
import builtins
import contextlib
import io
import os
import re
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import model  # noqa: E402
import screener  # noqa: E402
import valuation  # noqa: E402

REPORT_DIR = os.path.join(HERE, "reports")

# SEC industry codes that pick a mode. Cyclicals are valued on their
# average margin over the cycle (Damodaran), not the latest year.
BANK_SIC = set(range(6020, 6040)) | {6211}
REIT_SIC = {6798}
CYCLICAL_SIC = (list(range(1000, 1500)) + list(range(2800, 2830)) + list(range(2860, 2900))
                + list(range(2900, 3000)) + list(range(3310, 3400)) + [1531, 1520, 6552]
                + list(range(3711, 3717)) + list(range(4400, 4500)) + list(range(3530, 3540)))
NEEDS = {"revenue", "operating_income", "cash", "shares_outstanding", "book_equity"}


def pick_mode(cik):
    sic, _ = valuation.get_sic(cik)
    sic = int(sic) if sic else 0
    if sic in BANK_SIC:
        return "bank", sic
    if sic in REIT_SIC:
        return "reit", sic
    if sic in CYCLICAL_SIC:
        return "cyclical", sic
    return "standard", sic


def run_capture(ticker, flags, answer_for):
    """Run valuation.run_full_valuation non-interactively. answer_for(prompt,
    printed_so_far) gives each answer ('' = Enter). Returns (summary, printed
    text, captured narrative inputs or None)."""
    # Uses your saved industry / ADR / country choices (data/), like an
    # interactive run, so a report agrees with what you've decided before.
    out, seen, captured = io.StringIO(), {}, {}

    def fake_input(prompt=""):
        seen[prompt] = seen.get(prompt, 0) + 1
        if seen[prompt] > 3:
            raise RuntimeError(f"needs a typed answer: {prompt.strip()[:80]}")
        answer = answer_for(prompt, out.getvalue()) or ""
        print(prompt + answer)
        return answer

    real_range, real_nol = model.run_valuation_range_narrative, model.nol_tax_rate_path

    def capture_range(*args, **kwargs):
        captured["range"] = (args, kwargs)
        return real_range(*args, **kwargs)

    def capture_nol(ebit_path, nol_balance, marginal_rate):
        captured["nol"] = (nol_balance, marginal_rate)
        return real_nol(ebit_path, nol_balance, marginal_rate)

    real_input = builtins.input
    builtins.input = fake_input
    model.run_valuation_range_narrative, model.nol_tax_rate_path = capture_range, capture_nol
    try:
        with contextlib.redirect_stdout(out):
            summary = valuation.run_full_valuation(ticker, None, **flags)
    finally:
        builtins.input = real_input
        model.run_valuation_range_narrative, model.nol_tax_rate_path = real_range, real_nol
    return summary, out.getvalue(), captured


class Story:
    """The narrative model's inputs for one company, captured from a run, so
    any growth x margin story can be valued without asking again."""

    def __init__(self, captured):
        args, kwargs = captured["range"]
        (self.revenue, _g, self.margin_now, _t, _tax, self.s2c, rate, self.terminal_growth,
         self.debt, self.cash, self.shares) = args
        self.wacc = rate[1] if isinstance(rate, tuple) else rate
        self.ronc = kwargs.get("return_on_new_capital")
        self.nol, self.tax = captured.get("nol", (0.0, 0.25))

    def value(self, growth, margin, convergence=None, ronc="default"):
        revenue_path = model.project_revenue_path(self.revenue, growth, self.terminal_growth, 10)
        margin_path = model.project_margin_path(self.margin_now, margin, 10, convergence)
        taxes = model.nol_tax_rate_path(model.project_ebit_path(revenue_path, margin_path), self.nol, self.tax)
        result = model.run_valuation_range_narrative(
            self.revenue, growth, self.margin_now, margin, taxes, self.s2c, self.wacc, self.terminal_growth,
            self.debt, self.cash, self.shares, years=10,
            return_on_new_capital=self.ronc if ronc == "default" else ronc,
            margin_convergence_years=convergence)
        return result["base"]["value_per_share"]

    def solve(self, price, vary, fixed, lo, hi):
        """The growth (vary='growth') or margin (vary='margin') that makes the
        value equal `price`, holding the other at `fixed`; None if out of [lo, hi]."""
        f = (lambda x: self.value(x, fixed)) if vary == "growth" else (lambda x: self.value(fixed, x))
        try:
            if not f(lo) <= price <= f(hi):
                return None
        except Exception:
            return None
        for _ in range(60):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if f(mid) < price else (lo, mid)
        return (lo + hi) / 2


def _num(pattern, text, scale=1.0):
    m = re.search(pattern, text)
    return float(m.group(1).replace(",", "")) * scale if m else None


def pct(x, digits=1):
    return "n/a" if x is None else f"{x:.{digits}%}"


def money(x):
    return "n/a" if x is None else f"${x:,.2f}"


def history(facts):
    """Revenue by year, 5-year growth, and margin over the cycle."""
    revenue = valuation.get_annual_series(facts, valuation.METRIC_TAGS["revenue"], 6)
    cagr, span = valuation.trailing_revenue_cagr(facts)
    rows = valuation.cycle_margin_history(facts)
    stats = valuation.cycle_stats_or_none(rows)
    return revenue, cagr, span, rows, stats


def company_report(ticker):
    cik, name = valuation.get_cik_for_ticker(ticker)
    mode, sic = pick_mode(cik)
    lines = [f"# {name} ({ticker.upper()})", "",
             f"*Report of {date.today().isoformat()} from the DCF tool on its defaults — "
             f"SEC industry code {sic}, valued as **{mode}**. Not investment advice.*", ""]

    if mode in ("bank", "reit"):
        flags = {"financial": True} if mode == "bank" else {"reit": True}
        answers = (lambda p, _t: "0.76" if "type a beta to use as is" in p else "")
        summary, text, _ = run_capture(ticker, flags, answers)
        price = summary.get("share_price") or _num(r"Current market price: \$([\d,]+\.?\d*)", text) \
            or _num(r"share price=\$([\d,]+\.?\d*)", text)
        warnings, _cap = screener.run_warnings(text, {"book_equity", "shares_outstanding"}, ticker, price)
        lines += [f"**Price:** {money(price)}", ""]
        if mode == "bank":
            er = summary.get("excess_return", {})
            implied = _num(r"implies a stable ROE of (-?[\d.]+)%", text, 0.01)
            table = re.findall(r"  FY(\d{4})\s+([\d,-]+)\s+([\d,]+)\s+(-?[\d.]+)%", text)
            lines += ["## Return on equity", "",
                      "| Year | Return on equity |", "|---|---|"] + [f"| FY{y} | {r}% |" for y, _ni, _eq, r in table] + [
                      "", f"Current (latest year) **{pct(er.get('current_roe'))}**; the model fades it to "
                      f"**{pct(er.get('stable_roe'))}** (industry). The price implies a long-run return of "
                      f"**{pct(implied)}**.", "",
                      "## Value (excess return model, money-center beta)", "",
                      f"Low {money(er.get('low'))} · **Base {money(er.get('base'))}** · High {money(er.get('high'))}", ""]
        else:
            ddm, nav = summary.get("ddm", {}), summary.get("nav_per_share")
            lines += ["## Value (REIT dividend model)", "",
                      f"Dividend model: low {money(ddm.get('low'))} · **base {money(ddm.get('base'))}** · "
                      f"high {money(ddm.get('high'))}", f"Net asset value: {money(nav)} per share", ""]
        lines += ["## Data warnings", "", warnings or "None found.", ""]
        return "\n".join(lines)

    facts = valuation.fetch_json(valuation.EDGAR_FACTS_URL.format(cik=cik))
    revenue, cagr, span, cycle_rows, stats = history(facts)
    start_growth = min(max(cagr if cagr is not None else 0.05, -0.05), 0.40)

    def answers(prompt, printed):
        if "Initial revenue growth" in prompt:
            return f"{start_growth * 100:.2f}"
        if "Early-stage cost of capital" in prompt:
            wacc = re.findall(r"Mature WACC \(computed above\): ([\d.]+)%", printed)
            return wacc[-1] if wacc else ""
        if "Target (mature) operating margin" in prompt:
            now = re.findall(r"Current operating margin: (-?[\d.]+)%", printed)
            return now[-1] if now else ""
        if "Actual bond rating" in prompt:
            return ""
        if "Probability of failure as a percent" in prompt:
            return "10"
        return ""

    flags = {"narrative": True, "cyclical": mode == "cyclical"}
    summary, text, captured = run_capture(ticker, flags, answers)
    price = summary.get("share_price") or _num(r"Current market price: \$([\d,]+\.?\d*)", text) \
        or _num(r"share price=\$([\d,]+\.?\d*)", text)
    warnings, market_cap = screener.run_warnings(text, NEEDS, ticker, price)
    roic = _num(r"ROIC:\s+(-?[\d.]+)%", text, 0.01)
    reinvest = _num(r"Reinvestment rate:\s+(-?[\d.]+)%", text, 0.01)
    implied_stage = _num(r"high-growth-stage rate of (-?[\d.]+)%", text, 0.01)
    industry_margin = _num(r"Industry operating margin: (-?[\d.]+)%", text, 0.01)
    dcf = summary.get("dcf", {})

    lines += [f"**Price:** {money(price)}" + (f" · **Market cap:** ${market_cap / 1e9:,.2f}B" if market_cap else ""),
              f"**Cost of capital:** {pct(summary.get('wacc'), 2)} · **Cost of equity:** "
              f"{pct(summary.get('cost_of_equity'), 2)}", ""]

    # 1. The record
    lines += ["## 1. The record", ""]
    if revenue:
        lines += ["| Year | Revenue |", "|---|---|"] + [
            f"| FY{e['fiscal_year']} | {'$' if e['unit'] == 'USD' else e['unit'] + ' '}{e['val'] / 1e9:,.2f}B |"
            for e in revenue] + ([""] if revenue[-1]["unit"] == "USD" else
                                 ["", f"*Revenue as filed, in {revenue[-1]['unit']}; the valuation converts to USD.*", ""])
    last_year = (revenue[-1]["val"] / revenue[-2]["val"] - 1
                 if len(revenue) >= 2 and revenue[-2]["val"] > 0 else None)
    ytd, ytd_label = valuation.year_to_date_growth(facts, [t for t in valuation.METRIC_TAGS["revenue"]
                                                           if isinstance(t, str)])
    lines += [f"- Revenue growth {span or ''}: **{pct(cagr)}** a year; latest year "
              f"(FY{revenue[-1]['fiscal_year'] if revenue else ''}) **{pct(last_year)}**"
              + (" — a big break from the trend (an acquisition, a divestiture, a new product?)"
                 if last_year is not None and cagr is not None and abs(last_year - cagr) > 0.20 else ""),
              *([f"- This year so far ({ytd_label}): **{pct(ytd)}**"
                 + (" — far off the multi-year rate; the stories below start from that rate, so check which "
                    "one you believe" if cagr is not None and abs(ytd - cagr) > 0.15 else "")] if ytd is not None else []),
              f"- Operating margin over the cycle (as reported): mean "
              f"**{pct(stats['mean']) if stats else 'n/a'}**, median {pct(stats['median']) if stats else 'n/a'} "
              f"({stats['n_years'] if stats else 0} years)",
              f"- Return on invested capital: **{pct(roic)}**; reinvests {pct(reinvest)} of after-tax operating income",
              ""]

    story = Story(captured) if "range" in captured else None
    if story is None:
        lines += ["## 2. What the price implies", "",
                  "The narrative model couldn't run (no positive revenue to grow from).", "",
                  f"Standard model base value: {money(dcf.get('base'))}.", "",
                  "## Data warnings", "", warnings or "None found.", ""]
        return "\n".join(lines)

    # The cycle history is reported margin; the story's margins count R&D as
    # investment (and leases as debt). Shift the cycle average by today's gap
    # between the two, so all three margins are on the story's basis.
    rd_adjustment = _num(r"EBIT adjustment \(added to reported EBIT\):\s+([+-]?[\d,]+)", text) or 0.0
    lease_adjustment = _num(r"-> debt \+\$[\d,]+, EBIT \+\$([\d,]+)", text) or 0.0
    offset = (rd_adjustment + lease_adjustment) / story.revenue if story.revenue else 0.0
    cycle_margin = stats["mean"] + offset if stats and stats["mean"] + offset > 0 else None
    margins = [("today's", story.margin_now)]
    if cycle_margin is not None and abs(cycle_margin - story.margin_now) > 0.01:
        margins.append(("cycle average", cycle_margin))
    if industry_margin is not None and industry_margin > 0 and all(abs(industry_margin - m) > 0.01 for _, m in margins):
        margins.append(("industry", industry_margin))
    growths = sorted({round(g, 3) for g in (0.0, start_growth / 2, start_growth, start_growth + 0.05, start_growth + 0.10)
                      if g > -0.06})

    # 2. What the price implies
    lines += ["## 2. What the price implies", ""]
    if price:
        for label, margin in margins:
            if margin <= 0:
                continue  # growth alone can't help a business that loses money on each sale
            g = story.solve(price, "growth", margin, -0.50, 2.0)
            lines.append(f"- At **{label} margin ({pct(margin)})**, the price needs starting revenue growth of "
                         + (f"**{pct(g)}**" + (" — the price assumes the business shrinks" if g < 0 else "")
                            if g is not None else "**more than 200%** — no plausible growth gets there"
                            if story.value(2.0, margin) < price else "**below -50%** — the price is below even a collapsing business")
                         + f" (fading to {pct(story.terminal_growth)} by year 10); its own record is {pct(cagr)}.")
        m = story.solve(price, "margin", start_growth, -0.2, 0.9)
        capped = " — its record, capped at 40%" if cagr is not None and cagr > 0.40 else (
            " — its record" if cagr is not None else " — a placeholder, no usable revenue history")
        lines.append(f"- At **starting growth of {pct(start_growth)}**{capped}, the price needs a long-run operating "
                     f"margin of **{pct(m) if m is not None else 'over 90%'}** (today {pct(story.margin_now)}).")
        if implied_stage is not None:
            lines.append(f"- The standard model (growth from reinvestment) says the price needs {pct(implied_stage)} "
                         "growth for three years, then a fade.")
        lines.append("")

    # 3. The story grid
    lines += ["## 3. Value per share across stories", "",
              f"Starting growth fades to {pct(story.terminal_growth)} by year 10; the margin reaches its target by year "
              f"10. Cost of capital {pct(story.wacc, 2)}, sales-to-capital {story.s2c:.2f}. "
              f"Price {money(price)}.", "",
              "| Starting growth | " + " | ".join(f"{label} margin {pct(m)}" for label, m in margins) + " |",
              "|---|" + "---|" * len(margins)]
    for g in growths:
        cells = []
        for _, margin in margins:
            try:
                v = story.value(g, margin)
                cells.append(f"{money(v)}" + (f" ({v / price:.2f}x)" if price else ""))
            except Exception:
                cells.append("n/a")
        lines.append(f"| {pct(g)} | " + " | ".join(cells) + " |")
    if roic is not None and roic > story.wacc + 0.02:
        # Damodaran's default lets excess returns end after year 10 (new
        # investment earns the cost of capital). A durable moat keeps them.
        try:
            moat = story.value(start_growth, story.margin_now, ronc=roic)
            plain = story.value(start_growth, story.margin_now)
            lines += ["", f"**If its edge lasts:** with new investment after year 10 still earning today's "
                      f"{pct(roic)} return on capital (instead of the {pct(story.wacc, 2)} cost of capital), its own "
                      f"growth at today's margin is worth **{money(moat)}** instead of {money(plain)}"
                      + (f" ({moat / price:.2f}x the price)." if price else ".")]
        except Exception:
            pass
    lines += ["", f"Standard model (growth from reinvestment): low {money(dcf.get('low'))} · base "
              f"{money(dcf.get('base'))} · high {money(dcf.get('high'))}.", ""]

    # 4. Warnings
    lines += ["## 4. Data warnings", "", warnings or "None found.", "",
              "*How to use this: pick the row and column you actually believe — the company's record is the "
              "anchor — and compare that value with the price. If the price needs growth or margins well past "
              "anything in section 1, the market is paying for a story the record doesn't show yet.*", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="One-command company report.")
    parser.add_argument("tickers", nargs="+")
    args = parser.parse_args()
    os.makedirs(REPORT_DIR, exist_ok=True)
    for ticker in args.tickers:
        try:
            report = company_report(ticker)
        except BaseException as e:
            report = f"# {ticker.upper()}\n\nCouldn't build a report: {type(e).__name__}: {e}\n"
        path = os.path.join(REPORT_DIR, f"{ticker.upper()}_{date.today().isoformat()}.md")
        with open(path, "w") as f:
            f.write(report)
        print(report)
        print(f"(saved to {path})\n")


if __name__ == "__main__":
    main()
