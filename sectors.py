"""
sectors.py — every listed company by industry and size, to choose what to screen next.

Maps each Nasdaq/NYSE company to its SEC industry code, the Damodaran
industry the valuation tool would suggest for it, and a size band, then
counts companies and market value per industry. The industry codes are
kept for good (screens/sic_codes.json), so only the first run is slow
(~15 minutes at SEC's request limit).

USAGE (from the dcf-tool folder):
    python3 sectors.py                 # the table, largest industries first
    python3 sectors.py --by count      # most companies first

Writes screens/sectors_<date>.csv (one row per company) and
screens/sector_summary_<date>.csv (one row per industry).
"""

import argparse
import csv
import os
import sys
from collections import defaultdict
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import screener  # noqa: E402
import valuation  # noqa: E402

SIZES = [("small", 0, 2e9), ("mid", 2e9, 10e9), ("large", 10e9, float("inf"))]

# SEC's top-level divisions, for a broad grouping beside Damodaran's industries.
DIVISIONS = [(100, 999, "Agriculture"), (1000, 1499, "Mining & energy"), (1500, 1799, "Construction"),
             (2000, 3999, "Manufacturing"), (4000, 4999, "Transport, telecom & utilities"),
             (5000, 5199, "Wholesale"), (5200, 5999, "Retail"), (6000, 6799, "Finance, insurance & real estate"),
             (7000, 8999, "Services"), (9000, 9999, "Public administration & other")]


def division(sic):
    return next((name for lo, hi, name in DIVISIONS if lo <= sic <= hi), "Unknown")


def size_band(cap):
    return next(name for name, lo, hi in SIZES if lo <= cap < hi)


def main():
    parser = argparse.ArgumentParser(description="Every listed company by industry and size.")
    parser.add_argument("--by", choices=["value", "count"], default="value")
    parser.add_argument("--min-cap", type=float, default=3e8, help="leave out companies below this (default $300M)")
    args = parser.parse_args()

    print("Market caps for every listed company...", flush=True)
    caps = {cik: v for cik, v in screener.listed_market_caps().items() if v[2] >= args.min_cap}
    print(f"  {len(caps)} companies worth ${args.min_cap / 1e6:,.0f}M or more", flush=True)
    codes = screener.industry_codes(list(caps))

    rows = []
    for cik, (ticker, name, cap) in caps.items():
        sic = codes[cik]
        rows.append({"ticker": ticker, "company": name, "market_cap": round(cap), "sic": sic,
                     "division": division(sic), "industry": valuation.suggest_industry(sic) or "Unmapped",
                     "size": size_band(cap)})
    stamp = date.today().isoformat()
    os.makedirs(screener.SCREEN_DIR, exist_ok=True)
    with open(os.path.join(screener.SCREEN_DIR, f"sectors_{stamp}.csv"), "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: -r["market_cap"]))

    groups = defaultdict(lambda: {"count": 0, "value": 0.0, "small": 0, "mid": 0, "large": 0, "division": "",
                                  "biggest": []})
    for r in rows:
        g = groups[r["industry"]]
        g["count"] += 1
        g["value"] += r["market_cap"]
        g[r["size"]] += 1
        g["division"] = r["division"]
        g["biggest"].append((r["market_cap"], r["ticker"]))
    summary = []
    for industry, g in groups.items():
        biggest = ", ".join(t for _, t in sorted(g["biggest"], reverse=True)[:4])
        summary.append({"industry": industry, "division": g["division"], "companies": g["count"],
                        "small": g["small"], "mid": g["mid"], "large": g["large"],
                        "market_value_bn": round(g["value"] / 1e9, 1), "largest": biggest})
    summary.sort(key=lambda r: -(r["market_value_bn"] if args.by == "value" else r["companies"]))
    with open(os.path.join(screener.SCREEN_DIR, f"sector_summary_{stamp}.csv"), "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(summary[0]))
        writer.writeheader()
        writer.writerows(summary)

    print(f"\n{'Industry':<38}{'Cos':>5}{'Small':>7}{'Mid':>6}{'Large':>7}{'Value $bn':>11}  Largest")
    for r in summary:
        print(f"{r['industry'][:37]:<38}{r['companies']:>5}{r['small']:>7}{r['mid']:>6}{r['large']:>7}"
              f"{r['market_value_bn']:>11,.0f}  {r['largest']}")
    print(f"\nSmall < $2B, mid $2-10B, large > $10B. Saved screens/sectors_{stamp}.csv and sector_summary_{stamp}.csv.")


if __name__ == "__main__":
    main()
