"""
티커의 최근 N년간 valuation 비율(PER, PBR, PSR, PEG, EV/EBITDA, 배당수익률, FCF수익률)을 계산.

데이터 출처: yfinance
- 연간 재무제표(income_stmt/balance_sheet/cashflow)는 보통 최근 4~5개 회계연도까지 제공됨
  -> 연도별 스냅샷으로 비율을 계산 (일별/분기별 연속 데이터는 무료 소스에서 제공되지 않음)
- 분기 재무제표(최근 4~5개 분기)로 계산 가능한 모든 TTM(최근 12개월) 포인트를 추가로 계산
  (보통 1~2개 분기 포인트가 추가됨. 캐시에 계속 쌓이면서 시간이 지날수록 촘촘해짐)

캐싱 (db.py, SQLite):
- 원본 재무 수치(raw)만 캐시하고, 비율은 매번 그 raw 값으로부터 재계산한다.
- 연간 스냅샷은 ANNUAL_TTL 동안, TTM 스냅샷은 TTM_TTL 동안 유효한 것으로 보고
  캐시가 유효하면 yfinance를 아예 호출하지 않는다.
- TTM은 (ticker, date) 로 저장되므로 분기가 바뀔 때마다 새 날짜의 row가 "추가"되고
  이전 분기의 TTM은 과거 데이터로 그대로 남는다 -> 시간이 지날수록 yfinance가 한 번에
  주는 것보다 더 긴 분기별 이력이 DB에 쌓인다.
"""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import yfinance as yf

import db

ANNUAL_TTL = timedelta(days=30)
TTM_TTL = timedelta(hours=24)

ROW_ALIASES = {
    "revenue": ["Total Revenue", "Operating Revenue"],
    "net_income": ["Net Income Common Stockholders", "Net Income"],
    "ebitda": ["EBITDA", "Normalized EBITDA"],
    "ebit": ["EBIT", "Operating Income"],
    "dep_amort": ["Reconciled Depreciation", "Depreciation And Amortization"],
    "equity": ["Stockholders Equity", "Common Stock Equity"],
    "total_debt": ["Total Debt"],
    "cash": ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"],
    "shares": ["Ordinary Shares Number", "Share Issued"],
    "shares_fallback": ["Diluted Average Shares", "Basic Average Shares"],
    "fcf": ["Free Cash Flow"],
    "op_cashflow": ["Operating Cash Flow"],
    "capex": ["Capital Expenditure"],
}

METRICS = ["PER", "PBR", "PSR", "PEG", "EV_EBITDA", "DividendYield", "FCFYield"]


def _get_row(df, key):
    if df is None or df.empty:
        return None
    for name in ROW_ALIASES[key]:
        if name in df.index:
            return df.loc[name]
    return None


def _val(row, col, default=np.nan):
    if row is None:
        return default
    v = row.get(col, default)
    return default if v is None else v


def _to_naive(ts):
    ts = pd.Timestamp(ts)
    return ts.tz_localize(None) if ts.tzinfo is not None else ts


def _nearest_price(prices, date):
    """date 이전(또는 당일)의 가장 최근 종가."""
    pos = prices.index.searchsorted(date, side="right") - 1
    if pos < 0:
        return np.nan
    return float(prices.iloc[pos])


def _trailing_dividends(dividends, date):
    if dividends is None or dividends.empty:
        return 0.0
    window = dividends[(dividends.index > date - pd.Timedelta(days=365)) & (dividends.index <= date)]
    return float(window.sum())


def _snapshot(date, income, balance, cashflow, prices, dividends):
    """특정 시점(회계연도 말 등)의 재무 스냅샷 -> raw 값 dict."""
    revenue = _val(_get_row(income, "revenue"), date)
    net_income = _val(_get_row(income, "net_income"), date)
    equity = _val(_get_row(balance, "equity"), date)
    total_debt = _val(_get_row(balance, "total_debt"), date, 0.0)
    cash = _val(_get_row(balance, "cash"), date, 0.0)

    shares = _val(_get_row(balance, "shares"), date)
    if np.isnan(shares):
        shares = _val(_get_row(income, "shares_fallback"), date)

    ebitda = _val(_get_row(income, "ebitda"), date)
    if np.isnan(ebitda):
        ebit = _val(_get_row(income, "ebit"), date)
        dep = _val(_get_row(income, "dep_amort"), date, 0.0)
        ebitda = ebit + dep if not np.isnan(ebit) else np.nan

    fcf = _val(_get_row(cashflow, "fcf"), date)
    if np.isnan(fcf):
        ocf = _val(_get_row(cashflow, "op_cashflow"), date)
        capex = _val(_get_row(cashflow, "capex"), date, 0.0)
        fcf = ocf + capex if not np.isnan(ocf) else np.nan  # capex는 이미 음수로 표기됨

    price = _nearest_price(prices, date)
    market_cap = price * shares if not np.isnan(shares) and not np.isnan(price) else np.nan
    ev = market_cap + total_debt - cash if not np.isnan(market_cap) else np.nan
    div_ttm = _trailing_dividends(dividends, date)

    return {
        "date": date,
        "price": price,
        "revenue": revenue,
        "net_income": net_income,
        "equity": equity,
        "shares": shares,
        "ebitda": ebitda,
        "fcf": fcf,
        "market_cap": market_cap,
        "ev": ev,
        "div_ttm": div_ttm,
    }


def _ttm_snapshots(q_income, q_balance, q_cashflow, prices, dividends):
    """분기 재무제표로 계산 가능한 모든 TTM(최근 12개월) 스냅샷 목록.

    Yahoo/yfinance는 보통 분기 데이터를 최근 4~5개 분기까지만 제공하므로,
    4개 분기 윈도우를 만들 수 있는 시점마다(최근 데이터면 보통 1~2개) TTM row를 만든다.
    분기가 지날 때마다 이 함수가 새 날짜의 TTM을 하나씩 더 만들어내고, 그게 DB에 누적되면서
    시간이 지날수록 분기별 이력이 촘촘해진다.
    """
    rev_row = _get_row(q_income, "revenue")
    if rev_row is None:
        return []

    quarter_dates = sorted(rev_row.dropna().index)
    if len(quarter_dates) < 4:
        return []

    results = []
    for i in range(3, len(quarter_dates)):
        window = quarter_dates[i - 3 : i + 1]
        date = window[-1]

        def ttm_sum(row, window=window):
            if row is None:
                return np.nan
            vals = [row.get(c) for c in window]
            if any(v is None or (isinstance(v, float) and np.isnan(v)) for v in vals):
                return np.nan
            return float(np.sum(vals))

        revenue = ttm_sum(_get_row(q_income, "revenue"))
        net_income = ttm_sum(_get_row(q_income, "net_income"))

        ebitda = ttm_sum(_get_row(q_income, "ebitda"))
        if np.isnan(ebitda):
            ebit = ttm_sum(_get_row(q_income, "ebit"))
            dep = ttm_sum(_get_row(q_income, "dep_amort"))
            ebitda = ebit + (dep if not np.isnan(dep) else 0.0) if not np.isnan(ebit) else np.nan

        fcf = ttm_sum(_get_row(q_cashflow, "fcf"))
        if np.isnan(fcf):
            ocf = ttm_sum(_get_row(q_cashflow, "op_cashflow"))
            capex = ttm_sum(_get_row(q_cashflow, "capex"))
            fcf = ocf + (capex if not np.isnan(capex) else 0.0) if not np.isnan(ocf) else np.nan

        equity = _val(_get_row(q_balance, "equity"), date)
        total_debt = _val(_get_row(q_balance, "total_debt"), date, 0.0)
        cash = _val(_get_row(q_balance, "cash"), date, 0.0)
        shares = _val(_get_row(q_balance, "shares"), date)
        if np.isnan(shares):
            shares = _val(_get_row(q_income, "shares_fallback"), date)

        price = _nearest_price(prices, date)
        market_cap = price * shares if not np.isnan(shares) and not np.isnan(price) else np.nan
        ev = market_cap + total_debt - cash if not np.isnan(market_cap) else np.nan
        div_ttm = _trailing_dividends(dividends, date)

        results.append({
            "date": date,
            "price": price,
            "revenue": revenue,
            "net_income": net_income,
            "equity": equity,
            "shares": shares,
            "ebitda": ebitda,
            "fcf": fcf,
            "market_cap": market_cap,
            "ev": ev,
            "div_ttm": div_ttm,
        })

    return results


def _yoy_eps_growth_pct(df, tolerance_days=45):
    """각 row마다 '1년 전' 시점에 가장 가까운 과거 row를 찾아 EPS 성장률(%)을 계산.

    연간 스냅샷과 분기 TTM 스냅샷이 섞여 있으므로 단순히 바로 이전 row와 비교하면
    분기 간(QoQ) 변화를 연간(YoY) 성장률처럼 취급하는 오류가 생긴다. 대신 날짜 기준으로
    1년 전에 가장 가까운(허용오차 tolerance_days 이내) row를 찾아서 비교한다.
    """
    dates = df["date"].tolist()
    eps = df["eps"].tolist()
    growth = [np.nan] * len(df)

    for i in range(len(df)):
        target = dates[i] - pd.DateOffset(years=1)
        best_j, best_diff = None, None
        for j in range(i):
            diff = abs((dates[j] - target).days)
            if diff <= tolerance_days and (best_diff is None or diff < best_diff):
                best_j, best_diff = j, diff
        if best_j is None:
            continue
        prev_eps = eps[best_j]
        cur_eps = eps[i]
        if prev_eps is None or cur_eps is None:
            continue
        if isinstance(prev_eps, float) and np.isnan(prev_eps):
            continue
        if isinstance(cur_eps, float) and np.isnan(cur_eps):
            continue
        if prev_eps == 0:
            continue
        growth[i] = (cur_eps - prev_eps) / abs(prev_eps) * 100

    return pd.Series(growth, index=df.index)


def _derive_ratios(rows):
    """raw 스냅샷 목록(dict, date 포함) -> 비율까지 계산된 DataFrame."""
    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)

    df["eps"] = df["net_income"] / df["shares"]
    df["PER"] = df["market_cap"] / df["net_income"]
    df["PBR"] = df["market_cap"] / df["equity"]
    df["PSR"] = df["market_cap"] / df["revenue"]
    df["EV_EBITDA"] = df["ev"] / df["ebitda"]
    df["DividendYield"] = df["div_ttm"] / df["price"] * 100
    df["FCFYield"] = df["fcf"] / df["market_cap"] * 100

    eps_growth_pct = _yoy_eps_growth_pct(df)
    peg = df["PER"] / eps_growth_pct
    peg[(eps_growth_pct <= 0)] = np.nan  # 이익이 역성장/적자면 PEG는 의미 없음
    df["PEG"] = peg

    for col in METRICS:
        df[col] = df[col].replace([np.inf, -np.inf], np.nan)

    return df


def _parse_ts(s):
    return datetime.fromisoformat(s) if s else None


def get_ratio_history(ticker_symbol, years=3, force_refresh=False):
    """DB 캐시를 우선 사용하고, 캐시가 없거나 TTL이 지난 부분만 yfinance에서 새로 받아온다.

    Returns: (DataFrame, cache_info dict) — cache_info는 {"annual": ..., "ttm": ...}
    각 값은 "cache" | "fetched" | "cache(stale)" 중 하나.
    """
    ticker_symbol = ticker_symbol.strip().upper()
    now = _now()

    meta = db.get_meta(ticker_symbol)
    annual_at = _parse_ts(meta["annual_fetched_at"])
    ttm_at = _parse_ts(meta["ttm_fetched_at"])

    need_annual = force_refresh or annual_at is None or (now - annual_at) > ANNUAL_TTL
    need_ttm = force_refresh or ttm_at is None or (now - ttm_at) > TTM_TTL

    existing_rows = db.fetch_snapshots(ticker_symbol)
    cache_info = {"annual": "cache", "ttm": "cache"}

    if need_annual or need_ttm:
        try:
            t = yf.Ticker(ticker_symbol)
            hist = t.history(period=f"{years + 2}y")
            if hist.empty:
                raise ValueError(f"'{ticker_symbol}' 가격 데이터를 가져올 수 없습니다. 티커를 확인하세요.")

            prices = hist["Close"]
            prices.index = [_to_naive(d) for d in prices.index]

            dividends = t.dividends
            if dividends is not None and not dividends.empty:
                dividends = dividends.copy()
                dividends.index = [_to_naive(d) for d in dividends.index]

            fetched_at_str = now.isoformat()

            if need_annual:
                income, balance, cashflow = t.income_stmt, t.balance_sheet, t.cashflow
                if income is None or income.empty:
                    raise ValueError(f"'{ticker_symbol}' 재무제표 데이터를 가져올 수 없습니다.")
                fiscal_dates = sorted(pd.Timestamp(c) for c in income.columns)
                for d in fiscal_dates:
                    raw = _snapshot(d, income, balance, cashflow, prices, dividends)
                    db.upsert_snapshot(ticker_symbol, d.strftime("%Y-%m-%d"), "annual", raw, fetched_at_str)
                db.touch_meta(ticker_symbol, annual_fetched_at=fetched_at_str)
                cache_info["annual"] = "fetched"

            if need_ttm:
                q_income = t.quarterly_income_stmt
                q_balance = t.quarterly_balance_sheet
                q_cashflow = t.quarterly_cashflow
                for ttm in _ttm_snapshots(q_income, q_balance, q_cashflow, prices, dividends):
                    db.upsert_snapshot(ticker_symbol, ttm["date"].strftime("%Y-%m-%d"), "ttm", ttm, fetched_at_str)
                db.touch_meta(ticker_symbol, ttm_fetched_at=fetched_at_str)
                cache_info["ttm"] = "fetched"

        except Exception:
            if not existing_rows:
                raise
            # yfinance 호출 실패(네트워크 등) -> 캐시된 값으로 폴백
            cache_info = {"annual": "cache(stale)", "ttm": "cache(stale)"}

    all_cached = db.fetch_snapshots(ticker_symbol)
    if not all_cached:
        raise ValueError(f"'{ticker_symbol}'에 대한 데이터가 없습니다.")

    rows = [
        {**{c: r[c] for c in db.RAW_COLUMNS}, "date": pd.Timestamp(r["date"])}
        for r in all_cached
    ]
    df = _derive_ratios(rows)

    cutoff = pd.Timestamp.now().normalize() - pd.DateOffset(years=years)
    df = df[df["date"] >= cutoff].reset_index(drop=True)

    return df, cache_info


def _now():
    return datetime.now(timezone.utc)


def compute_ratios(ticker_symbol, years=3):
    """캐시 정보 없이 DataFrame만 필요할 때 쓰는 얇은 wrapper (CLI 등)."""
    df, _ = get_ratio_history(ticker_symbol, years=years)
    return df


if __name__ == "__main__":
    import sys

    ticker = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    result, info = get_ratio_history(ticker)
    pd.set_option("display.width", 160)
    print(f"cache_info: {info}")
    print(result[["date", "price"] + METRICS])
