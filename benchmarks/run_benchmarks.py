"""
benchmarks/run_benchmarks.py — regression check for the whole valuation
pipeline (EDGAR pull -> adjustments -> DCF -> value per share), not just
model.py's math, which has its own self-tests.

Each benchmark company in cases.json is valued non-interactively: saved
answers for the prompts it needs, Enter for everything else. Every outside
call (EDGAR, Yahoo, Damodaran's site, the Treasury) is RECORDED to
fixtures/<TICKER>.json.gz on --refresh and REPLAYED otherwise, so a normal
run sees exactly the same inputs every time — the only thing that can move
a number is a code change. Results are compared against baseline.json.

USAGE (from the dcf-tool folder):
    python3 benchmarks/run_benchmarks.py                    # replay saved data, compare to baseline
    python3 benchmarks/run_benchmarks.py --only AAPL DOW     # just some companies
    python3 benchmarks/run_benchmarks.py --refresh           # pull live data, save it as the new snapshot
    python3 benchmarks/run_benchmarks.py --refresh-only load_damodaran_data
                                                             # re-record just one source, replay the rest
    python3 benchmarks/run_benchmarks.py --update-baseline   # accept this run's results as the baseline

A refresh changes the inputs, so after one, expect differences from the
baseline: they're today's data, not a bug. Review them, then
--update-baseline. Exit code 1 when anything moved more than --tolerance
(or a company failed), 0 otherwise.

Each company's full printed output is saved to logs/<TICKER>.log.
"""

import argparse
import builtins
import contextlib
import gzip
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import valuation  # noqa: E402  (needs the path set up first)

CASES_PATH = os.path.join(HERE, "cases.json")
BASELINE_PATH = os.path.join(HERE, "baseline.json")
FIXTURE_DIR = os.path.join(HERE, "fixtures")
LOG_DIR = os.path.join(HERE, "logs")
RESULTS_PATH = os.path.join(HERE, "results", "latest.json")

# Every function in valuation.py that reaches outside the machine. Patched
# at the module level, so calls from inside valuation.py go through the
# recorder/replayer too. Recorded at the highest level that still captures
# the raw data (e.g. load_damodaran_data rather than each page fetch), so
# fixtures don't depend on today's date or cache state.
OUTSIDE_CALLS = [
    "get_cik_for_ticker", "fetch_json", "fetch_json_or_none", "fetch_text_or_none",
    "adr_ratio_candidates", "get_market_data", "get_market_shares", "get_implied_volatility",
    "get_fx_rate_to_usd", "load_damodaran_data", "load_risk_free_rate",
]


class BenchmarkError(Exception):
    pass


def _to_json(name, value):
    # adr_ratio_candidates returns {float: count}; JSON object keys must be strings.
    if name == "adr_ratio_candidates":
        return [[k, v] for k, v in value.items()]
    return value


def _from_json(name, value):
    if name == "adr_ratio_candidates":
        return {float(k): v for k, v in value}
    return value


def install_fixtures(ticker, refresh, refresh_only=()):
    """Patch valuation's outside calls to record (refresh) or replay. With
    refresh_only, just those sources are called live and re-recorded into
    the existing fixture — for when new code needs data the snapshot
    doesn't have, without letting today's prices into the comparison.
    Returns (fixture path, calls dict)."""
    path = os.path.join(FIXTURE_DIR, f"{ticker}.json.gz")
    if refresh:
        calls = {}
    else:
        if not os.path.exists(path):
            raise BenchmarkError(f"no saved data for {ticker} — run with --refresh first")
        with gzip.open(path, "rt") as f:
            calls = json.load(f)
    # Only the outermost call is recorded, and anything it calls in turn
    # runs live with it (e.g. load_damodaran_data's own page fetches) —
    # replaying those from an older snapshot would mix old and new data.
    depth = [0]
    for name in OUTSIDE_CALLS:
        real = getattr(valuation, f"_real_{name}", None) or getattr(valuation, name)
        setattr(valuation, f"_real_{name}", real)

        def wrapper(*args, _name=name, _real=real, **kwargs):
            if depth[0] > 0:
                return _real(*args, **kwargs)
            key = f"{_name}|{json.dumps([args, kwargs], sort_keys=True, default=str)}"
            if refresh or _name in refresh_only:
                depth[0] += 1
                try:
                    result = _real(*args, **kwargs)
                finally:
                    depth[0] -= 1
                calls[key] = _to_json(_name, result)
                return result
            if key not in calls:
                raise BenchmarkError(f"{ticker}: no saved response for {key[:120]} — "
                                     "the code now asks for data the snapshot doesn't have; run --refresh")
            return _from_json(_name, calls[key])
        setattr(valuation, name, wrapper)
    return path, calls


def install_answers(answers):
    """Replace input() with the case's saved answers: the first [substring,
    answer] whose substring appears in the prompt wins (a list answer is used
    up in order, then Enter); anything else gets Enter. A prompt that keeps
    coming back means the run needs a real answer it wasn't given."""
    queues = [[sub, list(ans) if isinstance(ans, list) else ans] for sub, ans in answers]
    seen = {}

    def fake_input(prompt=""):
        seen[prompt] = seen.get(prompt, 0) + 1
        if seen[prompt] > 5:
            raise BenchmarkError(f"stuck re-asking: {prompt.strip()[:100]!r} — add an answer in cases.json")
        answer = ""
        for sub, ans in queues:
            if sub in prompt:
                if isinstance(ans, list):
                    answer = ans.pop(0) if ans else ""
                else:
                    answer = ans
                break
        print(prompt + answer)
        return answer
    builtins.input = fake_input


def run_case(case, refresh, refresh_only=()):
    ticker = case["ticker"].upper()
    # The benchmarks keep their own saved industry / ADR / country choices,
    # so they're fixed and never touch the user's own data/ files.
    bench_data = os.path.join(HERE, "data")
    os.makedirs(bench_data, exist_ok=True)
    valuation.INDUSTRY_MAP_PATH = os.path.join(bench_data, "industry_map.json")
    valuation.ADR_RATIO_PATH = os.path.join(bench_data, "adr_ratios.json")
    valuation.COUNTRY_MAP_PATH = os.path.join(bench_data, "country_map.json")
    # A fresh, empty cache on refresh, so Damodaran's data really is re-fetched.
    valuation.DAMODARAN_CACHE_PATH = os.path.join(tempfile.mkdtemp(), "damodaran_cache.json")

    fixture_path, calls = install_fixtures(ticker, refresh, refresh_only)
    install_answers(case.get("answers", []))
    flags = case.get("flags", {})
    log = io.StringIO()
    try:
        with contextlib.redirect_stdout(log):
            summary = valuation.run_full_valuation(
                ticker, flags.get("stage"), narrative=flags.get("narrative", False),
                biotech=flags.get("biotech", False), reit=flags.get("reit", False),
                cyclical=flags.get("cyclical", False), financial=flags.get("financial", False))
    finally:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, f"{ticker}.log"), "w") as f:
            f.write(log.getvalue())
    if refresh or refresh_only:
        os.makedirs(FIXTURE_DIR, exist_ok=True)
        with gzip.open(fixture_path, "wt") as f:
            json.dump(calls, f)
    return summary


def flatten(summary, prefix=""):
    """{"dcf": {"base": 1.0}} -> {"dcf.base": 1.0}; numbers only."""
    out = {}
    for key, value in summary.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(flatten(value, name + "."))
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            out[name] = float(value)
    return out


def compare(results, baseline, tolerance):
    """Print every number that moved more than `tolerance` (relative), plus
    numbers that appeared or disappeared. Returns the count of flags."""
    flags = 0
    for ticker in sorted(set(results) | set(baseline)):
        now, before = results.get(ticker), baseline.get(ticker)
        if now is None or before is None:
            print(f"  {ticker}: {'new (no baseline)' if before is None else 'missing from this run'}")
            flags += now is None
            continue
        if "error" in now:
            print(f"  {ticker}: FAILED — {now['error']}")
            flags += 1
            continue
        a, b = flatten(before), flatten(now)
        lines = []
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                lines.append(f"    {key}: {'added' if key not in a else 'removed'}"
                             f" ({b.get(key, a.get(key)):,.4g})")
                continue
            old, new = a[key], b[key]
            change = (new - old) / abs(old) if old else (0.0 if new == old else float("inf"))
            if abs(change) > tolerance:
                lines.append(f"    {key}: {old:,.4g} -> {new:,.4g}  ({change:+.1%})")
        if lines:
            flags += 1
            print(f"  {ticker}: {len(lines)} change(s)")
            print("\n".join(lines))
        else:
            print(f"  {ticker}: unchanged")
    return flags


def main():
    parser = argparse.ArgumentParser(description="Regression benchmarks for the valuation tool.")
    parser.add_argument("--refresh", action="store_true",
                        help="pull live data and save it as the new snapshot")
    parser.add_argument("--refresh-only", nargs="+", default=(), metavar="SOURCE", choices=OUTSIDE_CALLS,
                        help="re-record just these outside calls live (e.g. load_damodaran_data), replay the rest")
    parser.add_argument("--update-baseline", action="store_true",
                        help="accept this run's results as the new baseline")
    parser.add_argument("--only", nargs="+", metavar="TICKER", help="run just these companies")
    parser.add_argument("--tolerance", type=float, default=0.005,
                        help="relative change that counts as a difference (default 0.005 = 0.5%%)")
    args = parser.parse_args()

    with open(CASES_PATH) as f:
        cases = json.load(f)["cases"]
    if args.only:
        wanted = {t.upper() for t in args.only}
        cases = [c for c in cases if c["ticker"].upper() in wanted]

    real_input, real_stdout = builtins.input, sys.stdout
    results = {}
    for case in cases:
        ticker = case["ticker"].upper()
        print(f"{ticker:<6} {'refreshing' if args.refresh else 'replaying'}...", end=" ", flush=True)
        try:
            summary = run_case(case, args.refresh, tuple(args.refresh_only))
            results[ticker] = summary
            base = summary.get("dcf", summary.get("excess_return", {})).get("base")
            print("ok" + (f"  (DCF base ${base:,.2f})" if base is not None else ""))
        except (BenchmarkError, Exception) as e:  # one company failing shouldn't stop the rest
            results[ticker] = {"error": f"{type(e).__name__}: {e}"}
            print(f"FAILED — {type(e).__name__}: {e}")
        finally:
            builtins.input, sys.stdout = real_input, real_stdout

    os.makedirs(os.path.dirname(RESULTS_PATH), exist_ok=True)
    with open(RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=2, sort_keys=True, default=str)

    baseline = {}
    if os.path.exists(BASELINE_PATH):
        with open(BASELINE_PATH) as f:
            baseline = json.load(f)
    if args.only:
        baseline = {t: v for t, v in baseline.items() if t in results}

    print(f"\nCompared with baseline (tolerance {args.tolerance:.1%}):")
    flagged = compare(results, baseline, args.tolerance)

    if args.update_baseline:
        full = {}
        if os.path.exists(BASELINE_PATH):
            with open(BASELINE_PATH) as f:
                full = json.load(f)
        full.update({t: r for t, r in results.items() if "error" not in r})
        with open(BASELINE_PATH, "w") as f:
            json.dump(full, f, indent=2, sort_keys=True, default=str)
        print(f"\nBaseline updated ({len(results)} companies).")
        return 0
    return 1 if flagged else 0


if __name__ == "__main__":
    sys.exit(main())
