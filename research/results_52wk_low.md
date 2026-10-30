# 52-week-low base-and-breakout event study

Run 2026-10-29 on 1693 Nasdaq/NYSE stocks worth >= $2B today, 2014-10-01 to 2026-10-29. Rules, test and known biases: see the docstring of research/event_study_52wk_low.py (fixed before any results were seen). Excess return = the stock's return minus the equal-weighted average of all universe stocks over the same days. Not investment advice.

## Trigger: first close 10% above the base low

**Pre-registered verdict** (same sign and |t| > 2 in 2015-20, 2021-26, and excluding Feb-Jun 2020):

- 21 days: not consistent (2015-20 -0.94% t=-1.5; 2021-26 +0.38% t=+0.3; ex-2020-crash -0.16% t=-0.2)
- 63 days: not consistent (2015-20 -1.18% t=-1.2; 2021-26 -2.82% t=-1.9; ex-2020-crash -2.05% t=-2.2)
- 126 days: CONSISTENT — events UNDERperform the universe (2015-20 -4.00% t=-2.3; 2021-26 -6.37% t=-3.4; ex-2020-crash -5.20% t=-3.9)
- 252 days: CONSISTENT — events UNDERperform the universe (2015-20 -8.30% t=-2.6; 2021-26 -11.72% t=-3.0; ex-2020-crash -9.71% t=-3.8)

### All events

| Horizon | Events | Months | Avg excess return | t-stat | % beating universe |
|---|---|---|---|---|---|
| 21 trading days | 544 | 110 | -0.21% | -0.30 | 43% |
| 63 trading days | 539 | 108 | -2.08% | -2.22 | 42% |
| 126 trading days | 521 | 105 | -5.26% | -4.03 | 42% |
| 252 trading days | 486 | 100 | -10.04% | -3.93 | 36% |

### 2015-2020

| Horizon | Events | Months | Avg excess return | t-stat | % beating universe |
|---|---|---|---|---|---|
| 21 trading days | 291 | 49 | -0.94% | -1.53 | 43% |
| 63 trading days | 291 | 49 | -1.18% | -1.19 | 43% |
| 126 trading days | 291 | 49 | -4.00% | -2.25 | 46% |
| 252 trading days | 291 | 49 | -8.30% | -2.56 | 38% |

### 2021-2026

| Horizon | Events | Months | Avg excess return | t-stat | % beating universe |
|---|---|---|---|---|---|
| 21 trading days | 253 | 61 | +0.38% | +0.33 | 44% |
| 63 trading days | 248 | 59 | -2.82% | -1.88 | 40% |
| 126 trading days | 230 | 56 | -6.37% | -3.37 | 37% |
| 252 trading days | 195 | 51 | -11.72% | -2.97 | 32% |

### Excluding the Feb-Jun 2020 crash and rebound

| Horizon | Events | Months | Avg excess return | t-stat | % beating universe |
|---|---|---|---|---|---|
| 21 trading days | 538 | 109 | -0.16% | -0.23 | 44% |
| 63 trading days | 533 | 107 | -2.05% | -2.17 | 42% |
| 126 trading days | 515 | 104 | -5.20% | -3.95 | 42% |
| 252 trading days | 480 | 99 | -9.71% | -3.79 | 36% |

## Trigger: first close 20% above the base low

**Pre-registered verdict** (same sign and |t| > 2 in 2015-20, 2021-26, and excluding Feb-Jun 2020):

- 21 days: not consistent (2015-20 -1.50% t=-1.5; 2021-26 -2.94% t=-3.0; ex-2020-crash -2.44% t=-3.8)
- 63 days: CONSISTENT — events UNDERperform the universe (2015-20 -4.56% t=-2.6; 2021-26 -4.11% t=-2.7; ex-2020-crash -4.14% t=-3.7)
- 126 days: CONSISTENT — events UNDERperform the universe (2015-20 -8.19% t=-3.2; 2021-26 -9.74% t=-3.3; ex-2020-crash -8.85% t=-4.5)
- 252 days: not consistent (2015-20 -19.64% t=-2.9; 2021-26 -7.80% t=-1.8; ex-2020-crash -11.69% t=-2.9)

### All events

| Horizon | Events | Months | Avg excess return | t-stat | % beating universe |
|---|---|---|---|---|---|
| 21 trading days | 391 | 100 | -2.29% | -3.23 | 40% |
| 63 trading days | 388 | 98 | -4.31% | -3.76 | 38% |
| 126 trading days | 362 | 95 | -9.00% | -4.63 | 32% |
| 252 trading days | 330 | 89 | -13.79% | -3.39 | 31% |

### 2015-2020

| Horizon | Events | Months | Avg excess return | t-stat | % beating universe |
|---|---|---|---|---|---|
| 21 trading days | 175 | 45 | -1.50% | -1.49 | 38% |
| 63 trading days | 175 | 45 | -4.56% | -2.61 | 41% |
| 126 trading days | 175 | 45 | -8.19% | -3.24 | 38% |
| 252 trading days | 175 | 45 | -19.64% | -2.87 | 39% |

### 2021-2026

| Horizon | Events | Months | Avg excess return | t-stat | % beating universe |
|---|---|---|---|---|---|
| 21 trading days | 216 | 55 | -2.94% | -2.95 | 42% |
| 63 trading days | 213 | 53 | -4.11% | -2.68 | 36% |
| 126 trading days | 187 | 50 | -9.74% | -3.32 | 27% |
| 252 trading days | 155 | 44 | -7.80% | -1.83 | 23% |

### Excluding the Feb-Jun 2020 crash and rebound

| Horizon | Events | Months | Avg excess return | t-stat | % beating universe |
|---|---|---|---|---|---|
| 21 trading days | 387 | 97 | -2.44% | -3.75 | 41% |
| 63 trading days | 384 | 95 | -4.14% | -3.67 | 38% |
| 126 trading days | 358 | 92 | -8.85% | -4.51 | 32% |
| 252 trading days | 326 | 86 | -11.69% | -2.94 | 32% |
