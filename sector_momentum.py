"""
Sector Momentum Backtest
------------------------
Each month, hold the N sector ETFs with the highest trailing return over the
past L months (equal-weighted), rebalance monthly, and compare against SPY.

Data: monthly total return index (dividends reinvested) pulled from Bloomberg
with BDH(..., "TOT_RETURN_INDEX_GROSS_DVDS", ..., "Per=M").

Run:  python sector_momentum.py
Outputs go to the ./output folder.
"""

from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # save charts to files (works in WSL without a display)
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
DATA_FILE = "sector-etfs-monthly-raw-data (1).csv"
BENCHMARK = "SPY"
TOP_N = 3                  # number of sectors to hold
LOOKBACK = 12              # main strategy lookback in months
LOOKBACKS_TO_TEST = [3, 6, 12]
COST_BPS = 5               # trading cost per dollar traded, in basis points
OUTPUT_DIR = Path("output")


# ---------------------------------------------------------------------------
# 1. Load and clean the data
# ---------------------------------------------------------------------------
def load_prices(path: str) -> pd.DataFrame:
    # utf-8-sig strips the BOM Excel adds, so the first column reads "Date"
    df = pd.read_csv(path, encoding="utf-8-sig")
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date").sort_index()

    # "XLK US Equity" -> "XLK", "SPY US EQUITY" -> "SPY"
    df.columns = [c.split()[0].upper() for c in df.columns]

    df = df.apply(pd.to_numeric, errors="coerce")
    if df.isna().any().any():
        print("Warning: missing values found:\n", df.isna().sum()[df.isna().sum() > 0])
    return df


# ---------------------------------------------------------------------------
# 2. Strategy
# ---------------------------------------------------------------------------
def run_strategy(prices: pd.DataFrame, lookback: int, top_n: int, cost_bps: float):
    sectors = prices.drop(columns=[BENCHMARK])
    returns = sectors.pct_change()                       # monthly returns
    momentum = sectors / sectors.shift(lookback) - 1     # trailing L-month return

    # Rank sectors each month (1 = best). Hold the top N, equal-weighted.
    ranks = momentum.rank(axis=1, ascending=False)
    weights = (ranks <= top_n).astype(float) / top_n
    weights[momentum.isna().any(axis=1)] = np.nan       # no signal until enough history

    # KEY STEP (avoids lookahead bias): weights chosen at the end of month t
    # earn the returns of month t+1.
    held = weights.shift(1)
    gross = (held * returns).sum(axis=1, min_count=1)

    # Turnover = fraction of the portfolio traded at each rebalance
    turnover = held.diff().abs().sum(axis=1)
    net = gross - turnover * cost_bps / 10_000

    return net.dropna(), weights.dropna()


# ---------------------------------------------------------------------------
# 3. Performance metrics
# ---------------------------------------------------------------------------
def max_drawdown(returns: pd.Series) -> float:
    wealth = (1 + returns).cumprod()
    return (wealth / wealth.cummax() - 1).min()


def metrics(returns: pd.Series, bench: pd.Series) -> dict:
    years = len(returns) / 12
    total = (1 + returns).prod()
    vol = returns.std() * np.sqrt(12)
    return {
        "CAGR": total ** (1 / years) - 1,
        "Volatility": vol,
        "Sharpe (rf=0)": returns.mean() * 12 / vol,
        "Max Drawdown": max_drawdown(returns),
        "Beta vs SPY": returns.cov(bench) / bench.var(),
        "% Months Beat SPY": (returns > bench).mean(),
        "Growth of $1": total,
    }


def fmt_table(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy().astype(object)
    for col in df.columns:
        for idx in df.index:
            v = df.loc[idx, col]
            if pd.isna(v):
                out.loc[idx, col] = "-"
            elif idx in ("Sharpe (rf=0)", "Beta vs SPY"):
                out.loc[idx, col] = f"{v:.2f}"
            elif idx == "Growth of $1":
                out.loc[idx, col] = f"${v:.2f}"
            else:
                out.loc[idx, col] = f"{v:.1%}"
    return out


# ---------------------------------------------------------------------------
# 4. Main
# ---------------------------------------------------------------------------
def main():
    OUTPUT_DIR.mkdir(exist_ok=True)
    prices = load_prices(DATA_FILE)
    spy_all = prices[BENCHMARK].pct_change()

    print(f"Loaded {len(prices)} months: {prices.index[0]:%b %Y} to {prices.index[-1]:%b %Y}")
    print(f"Sectors: {', '.join(c for c in prices.columns if c != BENCHMARK)}\n")

    # Run every lookback, then line them all up on the same start date
    # (set by the longest lookback) so the comparison is fair.
    results = {f"{lb}M Momentum": run_strategy(prices, lb, TOP_N, COST_BPS)[0]
               for lb in LOOKBACKS_TO_TEST}
    start = max(r.index[0] for r in results.values())
    results = {k: v[v.index >= start] for k, v in results.items()}
    spy = spy_all[spy_all.index >= start]
    results["SPY (buy & hold)"] = spy

    # --- Summary table
    table = pd.DataFrame({name: metrics(r, spy) for name, r in results.items()})
    table.loc["% Months Beat SPY", "SPY (buy & hold)"] = np.nan
    print(f"Performance {start:%b %Y} to {spy.index[-1]:%b %Y} "
          f"(top {TOP_N}, {COST_BPS} bps costs)")
    print(fmt_table(table).to_string(), "\n")
    table.to_csv(OUTPUT_DIR / "summary.csv")

    # --- Year-by-year: main strategy vs SPY
    main_name = f"{LOOKBACK}M Momentum"
    yearly = pd.DataFrame({
        main_name: results[main_name].groupby(results[main_name].index.year)
                                     .apply(lambda r: (1 + r).prod() - 1),
        "SPY": spy.groupby(spy.index.year).apply(lambda r: (1 + r).prod() - 1),
    })
    yearly["Difference"] = yearly[main_name] - yearly["SPY"]
    print("Calendar-year returns (partial years at the start and end):")
    print(yearly.map(lambda v: f"{v:+.1%}").to_string(), "\n")
    yearly.to_csv(OUTPUT_DIR / "yearly_returns.csv")

    # --- Current holdings
    _, weights = run_strategy(prices, LOOKBACK, TOP_N, COST_BPS)
    latest = weights.iloc[-1]
    print(f"Holdings for next month (signal as of {weights.index[-1]:%b %d, %Y}): "
          f"{', '.join(latest[latest > 0].index)}")

    # --- Chart 1: growth of $1
    fig, ax = plt.subplots(figsize=(11, 6))
    for name, r in results.items():
        style = {"color": "black", "linewidth": 2.5} if "SPY" in name else {"linewidth": 1.6}
        ax.plot((1 + r).cumprod(), label=name, **style)
    ax.set_title(f"Sector Momentum vs. SPY: Growth of $1 (top {TOP_N} sectors, monthly rebalance)")
    ax.set_ylabel("Value of $1")
    ax.set_xlabel("Date")
    ax.grid(alpha=0.3)
    ax.legend()
    fig.text(0.01, 0.01,
             f"Holds the {TOP_N} sector ETFs with the highest trailing return, equal-weighted. "
             f"Includes dividends and {COST_BPS} bps trading costs. Data: Bloomberg.",
             fontsize=8, style="italic", color="gray")
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(OUTPUT_DIR / "growth_of_1.png", dpi=150)

    # --- Chart 2: drawdowns
    fig, ax = plt.subplots(figsize=(11, 4))
    for name in (main_name, "SPY (buy & hold)"):
        w = (1 + results[name]).cumprod()
        ax.fill_between(w.index, w / w.cummax() - 1, 0, alpha=0.4, label=name)
    ax.set_title("Drawdown from Previous Peak")
    ax.yaxis.set_major_formatter(plt.matplotlib.ticker.PercentFormatter(1.0))
    ax.grid(alpha=0.3)
    ax.legend(loc="lower left")
    fig.tight_layout()
    fig.savefig(OUTPUT_DIR / "drawdowns.png", dpi=150)

    print(f"\nSaved charts and tables to ./{OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
