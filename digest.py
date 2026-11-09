"""
digest.py — turn a screen's hundreds of rows into a short brief.

A screen ranks everything, including rows whose numbers rest on a data
problem. The digest keeps the rows worth a human's time:

  * Top ideas: clean data (no warning that moves the value), a positive
    margin, and a record value at least 1.2x the price — but under 10x,
    since a result that extreme is almost always a data problem.
  * Most demanding prices: the widest gap between the growth the price
    needs and the growth the company has delivered.
  * With --reports N, a full report.py report on each of the top N.

USAGE (from the dcf-tool folder):
    python3 digest.py screens/smallcap_2026-11-07.csv
    python3 digest.py screens/smallcap_2026-11-07.csv --reports 5

Writes reports/digest_<screen>.md and prints it. Not investment advice.
"""

import argparse
import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Warnings that don't by themselves make a row's value untrustworthy.
BENIGN = ("returns below cost of capital", "one-time quarter normalized")


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def clean(row):
    parts = [w.strip() for w in (row.get("warnings") or "").split(";") if w.strip()]
    return all(any(p.startswith(b) for b in BENIGN) for p in parts)


def fmt_pct(x):
    return "" if x is None else f"{x:.1%}"


def main():
    parser = argparse.ArgumentParser(description="Turn a screen into a short brief.")
    parser.add_argument("screen", help="a CSV from screener.py")
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--reports", type=int, default=0, help="run report.py on the top N ideas")
    args = parser.parse_args()

    rows = list(csv.DictReader(open(args.screen)))
    name = os.path.splitext(os.path.basename(args.screen))[0]
    has_record = "record_to_price" in rows[0] and any(r.get("record_to_price") for r in rows)
    key = "record_to_price" if has_record else "value_to_price"
    if has_record:
        # A theme screen mixes models: a utility's dividend-model value stands in
        # for the record value, and its fundamental growth for the record.
        for r in rows:
            if not r.get("record_to_price") and r.get("value_to_price") and r.get("group"):
                r.update(record_to_price=r["value_to_price"], record_value=r["base"], model_value="1")
                if r.get("implied_growth") and r.get("hist_growth"):
                    r["expectations_gap"] = str(num(r["implied_growth"]) - num(r["hist_growth"]))
    valued = [r for r in rows if num(r.get(key)) is not None]
    # A margin over 90% means the revenue or EBIT figure is wrong, not that
    # the business is that good (Verra Mobility read 114%).
    ideas = [r for r in valued if clean(r) and 1.2 <= num(r[key]) < 10
             and (not has_record or r.get("model_value") or 0 < (num(r.get("margin_now")) or 0) <= 0.9)]
    ideas.sort(key=lambda r: num(r[key]), reverse=True)

    lines = [f"# Digest: {name}", "",
             f"{len(rows)} companies screened, {len(valued)} valued, **{len(ideas)} clean ideas** at 1.2x or more "
             f"({'record value' if has_record else 'model value'} / price). Not investment advice: each is a reason "
             "to read the filings, not a decision.", "",
             "## Top ideas", "",
             "| Ticker | Company | Price | " + ("Record value | x price | Grew (5 yr) | Price needs | Margin |"
                                              if has_record else "Value | x price |"),
             "|---|---|---|" + ("---|" * (5 if has_record else 2))]
    for r in ideas[:args.top]:
        cells = [r["ticker"], (r.get("name") or "")[:32].title(), f"${num(r['price']):,.2f}"]
        if has_record:
            implied = num(r.get("implied_growth"))
            cells += [f"${num(r['record_value']):,.2f}", f"{num(r[key]):.2f}x", fmt_pct(num(r.get("hist_growth"))),
                      fmt_pct(implied) if implied is not None else "below -50%", fmt_pct(num(r.get("margin_now")))]
        else:
            cells += [f"${num(r['base']):,.2f}", f"{num(r[key]):.2f}x"]
        lines.append("| " + " | ".join(cells) + " |")

    if has_record:
        demanding = [r for r in valued if (num(r.get("expectations_gap")) or 0) > 0 and clean(r)
                     and (r.get("model_value") or (num(r.get("margin_now")) or 0) > 0)
                     and (num(r.get("price")) or 0) >= 1
                     and (num(r.get("market_cap")) or 0) >= 1e8]
        demanding.sort(key=lambda r: num(r["expectations_gap"]), reverse=True)
        lines += ["", "## Most demanding prices", "",
                  "Clean-data companies whose price needs the most growth beyond what they've delivered — "
                  "the market is paying for a story their record doesn't show yet.", "",
                  "| Ticker | Company | Price | Grew (5 yr) | Price needs | Record / price |", "|---|---|---|---|---|---|"]
        for r in demanding[:10]:
            lines.append(f"| {r['ticker']} | {(r.get('name') or '')[:32].title()} | ${num(r['price']):,.2f} | "
                         f"{fmt_pct(num(r.get('hist_growth')))} | {fmt_pct(num(r.get('implied_growth')))} | "
                         f"{num(r[key]):.2f}x |")

    runway = [r for r in rows if num(r.get("runway_years")) is not None or num(r.get("burn")) is not None]
    if runway:
        runway.sort(key=lambda r: num(r.get("runway_years")) if num(r.get("runway_years")) is not None else 1e9)
        lines += ["", "## Pre-revenue: what the price pays for", "",
                  "No earnings to discount yet. Enterprise value is what the market pays for the story beyond the "
                  "cash in hand; runway is how long that cash lasts at the trailing year's spending (operating cash "
                  "flow plus capex). Under two years means more shares or debt are coming.", "",
                  "| Ticker | Company | Market cap | Cash | Burn / yr | Runway | EV beyond cash |",
                  "|---|---|---|---|---|---|---|"]
        bn = lambda x: "" if x is None else f"${x / 1e9:,.2f}B"
        for r in runway:
            years = num(r.get("runway_years"))
            lines.append(f"| {r['ticker']} | {(r.get('name') or '')[:32].title()} | {bn(num(r.get('market_cap')))} | "
                         f"{bn(num(r.get('cash')))} | {bn(num(r.get('burn')))} | "
                         f"{'cash-flow positive' if years is None else f'{years:.1f} yrs'} | {bn(num(r.get('ev')))} |")

    if any(r.get("group") for r in rows):
        lines += ["", "## Every company by group", ""]
        for group in dict.fromkeys(r.get("group") for r in rows if r.get("group")):
            if all(num(r.get("burn")) is not None for r in rows if r.get("group") == group):
                continue  # pre-revenue groups are in the table above
            lines += [f"**{group}**", "", "| Ticker | Price | Value / price | Grew | Price needs | Margin | Warnings |",
                      "|---|---|---|---|---|---|---|"]
            for r in [r for r in rows if r.get("group") == group and num(r.get("runway_years")) is None
                      and num(r.get("burn")) is None]:
                rp = num(r.get(key))
                implied = num(r.get("implied_growth"))
                price = num(r.get("price"))
                lines.append(f"| {r['ticker']} | {'' if price is None else f'${price:,.2f}'} | "
                             f"{'' if rp is None else f'{rp:.2f}x'} | {fmt_pct(num(r.get('hist_growth')))} | "
                             f"{fmt_pct(implied)} | {fmt_pct(num(r.get('margin_now')))} | "
                             f"{r.get('warnings') or r.get('error') or ''} |")
            lines.append("")

    dropped = len(valued) - len([r for r in valued if clean(r)])
    lines += ["", f"*{dropped} valued rows were left out for data warnings (stale or missing figures, a share count "
              "far from the market's, negative cash flow). Their logs are in screens/logs/.*", ""]

    if args.reports:
        import report
        lines += ["## Reports on the top ideas", ""]
        for r in ideas[:args.reports]:
            try:
                body = report.company_report(r["ticker"])
            except BaseException as e:
                body = f"# {r['ticker']}\n\nCouldn't build a report: {type(e).__name__}: {e}\n"
            lines += [body.replace("\n# ", "\n### ").replace("\n## ", "\n#### ", -1), ""]

    text = "\n".join(lines)
    os.makedirs(os.path.join(HERE, "reports"), exist_ok=True)
    path = os.path.join(HERE, "reports", f"digest_{name}.md")
    with open(path, "w") as f:
        f.write(text)
    print(text)
    print(f"(saved to {path})")


if __name__ == "__main__":
    main()
