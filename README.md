# dcf-tool

A command-line valuation tool that pulls a public company's financials straight from SEC EDGAR and values it the way Aswath Damodaran teaches: a DCF built from the company's own filings, his industry data, and an explicit story about growth, margins, reinvestment and risk.

It's built for judgment, not false precision. Every input it pulls is shown with its source tag and date. Every assumption is asked about, with a sourced default. Every result comes with a low/base/high range and what the current price implies.

```
python3 valuation.py ARM --narrative
```

## What it does

1. **Pulls the filings.** Revenue, operating income, capex, D&A, debt, cash, leases, shares, R&D, tax-loss carryforwards and more from EDGAR's XBRL API. It handles US GAAP and IFRS, foreign filers (20-F/40-F) with FX conversion and ADR ratios, and trailing-twelve-month figures from the latest 10-Q. It flags stale, missing or mis-scaled data instead of silently using it.
2. **Builds the cost of capital from market data and Damodaran's tables.**
   - The 10-year Treasury yield and Damodaran's current implied equity risk premium, with country risk weighted by where revenue is earned.
   - A bottom-up industry beta, relevered to the company's debt.
   - A cost of debt from a synthetic rating on interest coverage, or from the company's actual bond rating.
3. **Applies Damodaran's adjustments.**
   - R&D counted as an investment; operating leases counted as debt.
   - Cycle-average margins for cyclicals; working-capital investment.
   - A full equity bridge (minority interests, equity stakes, pensions, dilution).
   - A failure probability taken from bond-rating default rates.
4. **Values the company several ways and shows what the price implies.** The market-implied growth rate, EV/revenue multiple, return on equity or full-output revenue, depending on the model.

## Models

| Flag | For | Method |
|---|---|---|
| *(none)* | Profitable, established companies | Multi-stage FCFF DCF. Growth = reinvestment rate × return on capital, fading to a terminal rate. New investment after the forecast earns its cost of capital. |
| `--narrative` | Story-driven or high-growth companies | Damodaran's "Ginzu" build. Revenue growth, target margin (and the year it's reached), and sales-to-capital (default: his industry ratio) as separate paths. Adds tax-loss carryforwards, a declining early-stage cost of capital, a failure-probability overlay, and checks on whether the story is plausible. |
| `--cyclical` | Commodity and cyclical businesses | Normalizes to the company's average margin over up to 10 years. |
| `--biotech` | Clinical-stage pharma | Drug-by-drug pipeline value, weighted by probability of approval. |
| `--reit` | REITs | Dividend discount model on FFO/AFFO, cross-checked against net asset value. |
| `--financial` | Banks and brokers | Excess return model: book equity + PV of (ROE − cost of equity) × book equity. |
| `--project` | Pre-production miners and plants | Project NPV from the technical report's timing, capex, output, price and margin, with a failure probability. |
| `--stage young\|growth\|mature\|N,M` | Any (standard model) | Sets the high-growth and fade years. |

Modes combine, e.g. `--narrative --cyclical` or `--biotech --narrative`.

## Beyond one valuation

Three tools build on `valuation.py` for finding and tracking ideas:

| Tool | What it does |
|---|---|
| `report.py TICKER` | A one-command company report. It runs the tool once on defaults, picking bank, REIT, cyclical or standard mode from the SEC industry code, and writes `reports/<TICKER>_<date>.md` with four sections:<br>1. **The record:** revenue, growth, cycle margins, return on capital.<br>2. **What the price implies:** the growth it needs at today's, cycle-average and industry margins, and the margin it needs at the company's own growth.<br>3. **A growth × margin grid** of values per share, including what a lasting competitive edge is worth.<br>4. **Every data warning.** |
| `screener.py SECTOR` | Values every listed company in a sector non-interactively and ranks them, with each run's data warnings. Sectors:<br>• `banks`: excess return model.<br>• `builders`: homebuilders and land developers.<br>• `smallcap`: every operating company from $300M to $2B.<br>For builders and small caps it also reports a **record value** (what the company is worth if it keeps its own 5-year growth at today's margin) and the growth the price needs against the growth it delivered. `--answer "PROMPT=VALUE"` overrides a default across the whole screen. |
| `digest.py SCREEN.csv` | Turns a screen into a short brief:<br>• top ideas, which must have clean data, a positive margin, and a record value 1.2x–10x the price;<br>• the most demanding prices: the widest gap between the growth needed and the growth delivered.<br>`--reports N` adds a full report on each of the top N. |
| `journal.py` | A decision journal. Record every buy, pass or watch with the price, your thesis and the model's value, then score each against simply owning the S&P 500 (SPY) over the same days. A pass scores when the stock then lagged. |

```bash
python3 report.py NPB
python3 screener.py smallcap --workers 6
python3 digest.py screens/smallcap_2026-11-07.csv --reports 5
python3 journal.py add NPB watch "Mortgage-warehouse lender at 0.9x book; brokered funding is the risk"
python3 journal.py show
```

Screens, reports and the journal stay on your machine (git-ignored).

## Sample output

```
  VALUATION RANGE — Apple Inc. (AAPL)
  Current market price: $348.91

  LOW  : $     81.87 per share   (WACC=11.49%, terminal growth=2.00%)
  BASE : $     91.11 per share   (WACC=10.49%, terminal growth=2.50%)
  HIGH : $    102.35 per share   (WACC=9.49%, terminal growth=3.00%)

  Market-implied growth rate
  ... the current price ($348.91) implies a high-growth-stage rate of 212.94%
  (vs. the 5.94% mechanical estimate above).
```

The point isn't the single number. It's seeing exactly which story the price requires.

## Setup

Requires Python 3.9+. Market data comes from [yfinance](https://pypi.org/project/yfinance/):

```bash
pip install yfinance
python3 valuation.py AAPL
```

On the first run it asks for a name and email. SEC requires every EDGAR request to identify itself. The contact is saved to `data/sec_contact.txt`, which is git-ignored. You can set `DCF_SEC_CONTACT="Jane Doe jane@example.com"` instead. Most prompts have a sourced default, so you can press Enter through them and override only what you have a view on.

## Files

- `valuation.py`: data pulls (EDGAR, Yahoo, Treasury, Damodaran's datasets), the interactive build, and the modes.
- `model.py`: the valuation math only, with hand-worked self-tests (`python3 model.py`).
- `benchmarks/`: a regression suite. Twelve companies, each chosen to exercise a different part of the tool (Apple, Dow, Walmart, Coca-Cola, Netflix, Viking, Realty Income, Alibaba, TSMC, Snowflake, Schwab, Generate Biomedicines). They're valued against recorded data, so only a code change can move a number:

  ```bash
  python3 benchmarks/run_benchmarks.py
  ```
- `research/`: a pre-registered event study (52-week-low base-and-breakout) and its results.

## Sources

- **Company data:** [SEC EDGAR XBRL API](https://www.sec.gov/edgar/sec-api-documentation).
- **Industry betas, margins, returns, working capital, sales-to-capital, country risk, rating spreads and default rates:** Aswath Damodaran's [data page](https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datacurrent.html).
- **Implied equity risk premium:** [Damodaran's home page](https://pages.stern.nyu.edu/~adamodar/).
- **Risk-free rate:** [US Treasury daily yield curve](https://home.treasury.gov/resource-center/data-chart-center/interest-rates).
- **Prices and FX:** Yahoo Finance via yfinance; ECB reference rates via [Frankfurter](https://frankfurter.dev).

## Disclaimer

This is an educational and analytical tool, not investment advice. Its outputs depend on the assumptions you enter and on filings data that is sometimes stale or mis-tagged. The tool flags what it can, but check the numbers that matter against the filings themselves.
