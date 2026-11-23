"""
재무제표 요약표: 연도별(yfinance가 주는 가장 오래된 회계연도부터) + 최근 5개 분기.

yfinance(Yahoo)는 연간 재무제표를 보통 4~5개 회계연도, 분기 재무제표를 6~7개 분기까지 준다.
가장 오래된 기간은 항목이 대부분 비어 있는 경우가 많으므로, 표시할 지표가 하나도 없는
기간(column)은 버린다.

분기 표의 기준:
- 매출액·영업이익·당기순이익·CAPEX·FCF: 해당 분기 단독 값
- 영업이익률·순이익률: 해당 분기 기준
- ROE·ROA: 분기 순이익 x 4 (연환산) / 분기말 자본·자산
- PER·배당성향: 최근 4개 분기 합(TTM) 기준. 4개 분기가 모이지 않으면 비워 둔다.
- PBR·부채비율·배당수익률: 분기말 시점 기준

계산 결과(JSON)를 kv_meta에 통째로 캐시한다(FIN_TTL). 조회 실패 시 오래된 캐시로 폴백한다.
"""

import json
import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import yfinance as yf

import db

FIN_TTL = timedelta(hours=24)
QUARTERS = 5
# 계산식/구조가 바뀌면 올린다. 캐시된 payload의 버전이 다르면 다시 조회한다.
CACHE_VERSION = 1

ROWS = {
    "revenue": (["Total Revenue", "Operating Revenue"], "income"),
    "op_income": (["Operating Income", "EBIT"], "income"),
    "net_income": (["Net Income Common Stockholders", "Net Income"], "income"),
    "total_assets": (["Total Assets"], "balance"),
    "total_liab": (["Total Liabilities Net Minority Interest", "Total Liabilities"], "balance"),
    "equity": (["Stockholders Equity", "Common Stock Equity"], "balance"),
    "shares": (["Ordinary Shares Number", "Share Issued"], "balance"),
    "capex": (["Capital Expenditure"], "cashflow"),
    "fcf": (["Free Cash Flow"], "cashflow"),
    "ocf": (["Operating Cash Flow"], "cashflow"),
    "div_paid": (["Cash Dividends Paid", "Common Stock Dividend Paid"], "cashflow"),
}

# 화면에 표시하는 순서. kind: amount(통화 금액) | pct(%) | x(배)
METRIC_ROWS = [
    ("revenue", "매출액", "amount"),
    ("op_income", "영업이익", "amount"),
    ("net_income", "당기순이익", "amount"),
    ("total_assets", "자산총계", "amount"),
    ("total_liab", "부채총계", "amount"),
    ("equity", "자본총계", "amount"),
    ("capex", "CAPEX", "amount"),
    ("fcf", "FCF", "amount"),
    ("op_margin", "영업이익률", "pct"),
    ("net_margin", "순이익률", "pct"),
    ("roe", "ROE", "pct"),
    ("roa", "ROA", "pct"),
    ("debt_ratio", "부채비율", "pct"),
    ("per", "PER", "x"),
    ("pbr", "PBR", "x"),
    ("div_yield", "배당수익률", "pct"),
    ("payout", "배당성향", "pct"),
]


def _nan(v):
    return v is None or (isinstance(v, float) and math.isnan(v))


def _row(stmts, key):
    names, which = ROWS[key]
    df = stmts.get(which)
    if df is None or df.empty:
        return None
    for name in names:
        if name in df.index:
            return df.loc[name]
    return None


def _get(stmts, key, date):
    row = _row(stmts, key)
    if row is None:
        return np.nan
    v = row.get(date, np.nan)
    return np.nan if v is None else float(v)


def _div(a, b, scale=1.0):
    if _nan(a) or _nan(b) or b == 0:
        return np.nan
    return a / b * scale


def _to_naive(ts):
    ts = pd.Timestamp(ts)
    return ts.tz_localize(None) if ts.tzinfo is not None else ts


def _price_at(prices, date):
    """date 당일 또는 그 이전 가장 최근 종가 (결산일이 휴장일인 경우 대비)."""
    if prices is None or prices.empty:
        return np.nan
    pos = prices.index.searchsorted(date, side="right") - 1
    if pos < 0:
        return np.nan
    # 결산일보다 열흘 넘게 이전 종가밖에 없으면 그 시점 주가로 보지 않는다(상장 전 등)
    if (date - prices.index[pos]).days > 10:
        return np.nan
    return float(prices.iloc[pos])


def _dps_ttm(dividends, date):
    if dividends is None or dividends.empty:
        return 0.0
    window = dividends[(dividends.index > date - pd.Timedelta(days=365)) & (dividends.index <= date)]
    return float(window.sum())


def _capex_fcf(stmts, date):
    """CAPEX는 지출액을 양수로 표시한다(yfinance는 음수로 줌). FCF가 없으면 영업현금흐름 - CAPEX."""
    capex_raw = _get(stmts, "capex", date)
    capex = abs(capex_raw) if not _nan(capex_raw) else np.nan
    fcf = _get(stmts, "fcf", date)
    if _nan(fcf):
        ocf = _get(stmts, "ocf", date)
        if not _nan(ocf):
            fcf = ocf - (capex if not _nan(capex) else 0.0)
    return capex, fcf


def _valuation(net_income_annual, equity, shares, price, dps, div_paid_annual):
    """PER·PBR·배당수익률·배당성향. 순이익/자본이 0 이하면 PER/PBR은 의미가 없으므로 비운다."""
    market_cap = price * shares if not _nan(price) and not _nan(shares) else np.nan
    per = _div(market_cap, net_income_annual) if not _nan(net_income_annual) and net_income_annual > 0 else np.nan
    pbr = _div(market_cap, equity) if not _nan(equity) and equity > 0 else np.nan
    div_yield = _div(dps, price, 100) if not _nan(price) and price > 0 else np.nan

    payout = np.nan
    if not _nan(net_income_annual) and net_income_annual > 0:
        if not _nan(div_paid_annual):
            payout = abs(div_paid_annual) / net_income_annual * 100
        elif not _nan(shares) and dps > 0:
            payout = dps * shares / net_income_annual * 100
        elif dps == 0:
            payout = 0.0
    return per, pbr, div_yield, payout


def _annual_column(stmts, date, prices, dividends):
    revenue = _get(stmts, "revenue", date)
    op_income = _get(stmts, "op_income", date)
    net_income = _get(stmts, "net_income", date)
    assets = _get(stmts, "total_assets", date)
    liab = _get(stmts, "total_liab", date)
    equity = _get(stmts, "equity", date)
    shares = _get(stmts, "shares", date)
    capex, fcf = _capex_fcf(stmts, date)
    price = _price_at(prices, date)
    dps = _dps_ttm(dividends, date)
    per, pbr, div_yield, payout = _valuation(
        net_income, equity, shares, price, dps, _get(stmts, "div_paid", date)
    )
    equity_pos = equity if not _nan(equity) and equity > 0 else np.nan
    return {
        "revenue": revenue,
        "op_income": op_income,
        "net_income": net_income,
        "total_assets": assets,
        "total_liab": liab,
        "equity": equity,
        "capex": capex,
        "fcf": fcf,
        "op_margin": _div(op_income, revenue, 100),
        "net_margin": _div(net_income, revenue, 100),
        "roe": _div(net_income, equity_pos, 100),
        "roa": _div(net_income, assets, 100),
        "debt_ratio": _div(liab, equity_pos, 100),
        "per": per,
        "pbr": pbr,
        "div_yield": div_yield,
        "payout": payout,
    }


def _ttm(stmts, key, dates, i):
    """dates[i]를 끝으로 하는 4개 분기 합. 하나라도 비면 NaN."""
    if i < 3:
        return np.nan
    vals = [_get(stmts, key, d) for d in dates[i - 3 : i + 1]]
    if any(_nan(v) for v in vals):
        return np.nan
    return float(sum(vals))


def _quarter_column(stmts, dates, i, prices, dividends):
    date = dates[i]
    col = _annual_column(stmts, date, prices, dividends)  # 분기 단독 값·분기말 잔액은 그대로 사용

    net_income = col["net_income"]
    equity = col["equity"]
    equity_pos = equity if not _nan(equity) and equity > 0 else np.nan
    annualized = net_income * 4 if not _nan(net_income) else np.nan
    col["roe"] = _div(annualized, equity_pos, 100)
    col["roa"] = _div(annualized, col["total_assets"], 100)

    ni_ttm = _ttm(stmts, "net_income", dates, i)
    per, _, _, payout = _valuation(
        ni_ttm,
        equity,
        _get(stmts, "shares", date),
        _price_at(prices, date),
        _dps_ttm(dividends, date),
        _ttm(stmts, "div_paid", dates, i),
    )
    col["per"] = per
    col["payout"] = payout
    return col


def _period_dates(stmts):
    dates = set()
    for df in stmts.values():
        if df is not None and not df.empty:
            dates.update(_to_naive(c) for c in df.columns)
    return sorted(dates)


def _normalize(df):
    if df is None or df.empty:
        return df
    df = df.copy()
    df.columns = [_to_naive(c) for c in df.columns]
    return df


def _has_core(col):
    """금액 지표가 하나도 없는 기간은 표에서 뺀다 (yfinance의 가장 오래된 열은 대부분 비어 있음)."""
    return any(not _nan(col[k]) for k, _, kind in METRIC_ROWS if kind == "amount")


def _clean(v):
    if _nan(v) or (isinstance(v, float) and math.isinf(v)):
        return None
    return float(v)


def _table(columns, labels):
    return {
        "periods": labels,
        "rows": {k: [_clean(c[k]) for c in columns] for k, _, _ in METRIC_ROWS},
    }


def _quarter_label(d):
    return f"{d.year}.{(d.month - 1) // 3 + 1}Q"


def _build(ticker_symbol):
    t = yf.Ticker(ticker_symbol)

    annual = {
        "income": _normalize(t.income_stmt),
        "balance": _normalize(t.balance_sheet),
        "cashflow": _normalize(t.cashflow),
    }
    quarterly = {
        "income": _normalize(t.quarterly_income_stmt),
        "balance": _normalize(t.quarterly_balance_sheet),
        "cashflow": _normalize(t.quarterly_cashflow),
    }
    if annual["income"] is None or annual["income"].empty:
        raise ValueError(f"'{ticker_symbol}' 재무제표 데이터를 가져올 수 없습니다.")

    a_dates = _period_dates(annual)
    q_dates = _period_dates(quarterly)
    oldest = min(a_dates + q_dates)

    # 배당 미조정 실제 주가 (ratios.py와 같은 이유: 과거 PER이 싸게 보이는 왜곡 방지)
    hist = t.history(start=(oldest - pd.Timedelta(days=30)).strftime("%Y-%m-%d"), auto_adjust=False)
    prices = hist["Close"] if not hist.empty else pd.Series(dtype=float)
    prices.index = [_to_naive(d) for d in prices.index]

    dividends = t.dividends
    if dividends is not None and not dividends.empty:
        dividends = dividends.copy()
        dividends.index = [_to_naive(d) for d in dividends.index]

    a_cols, a_labels = [], []
    for d in a_dates:
        col = _annual_column(annual, d, prices, dividends)
        if _has_core(col):
            a_cols.append(col)
            a_labels.append(f"{d.year}.{d.month:02d}")

    q_cols, q_labels = [], []
    for i, d in enumerate(q_dates):
        col = _quarter_column(quarterly, q_dates, i, prices, dividends)
        if _has_core(col):
            q_cols.append(col)
            q_labels.append(_quarter_label(d))
    q_cols, q_labels = q_cols[-QUARTERS:], q_labels[-QUARTERS:]

    currency = None
    try:
        info = t.info or {}
        currency = info.get("financialCurrency") or info.get("currency")
    except Exception:
        pass

    return {
        "version": CACHE_VERSION,
        "currency": currency,
        "metrics": [{"key": k, "label": label, "kind": kind} for k, label, kind in METRIC_ROWS],
        "annual": _table(a_cols, a_labels),
        "quarterly": _table(q_cols, q_labels),
    }


def get_financials(ticker_symbol, force_refresh=False):
    """Returns: (payload dict, source) — source는 "cache" | "fetched" | "cache(stale)"."""
    ticker_symbol = ticker_symbol.strip().upper()
    key = f"financials:{ticker_symbol}"
    now = datetime.now(timezone.utc)

    cached = None
    raw = db.get_kv(key)
    if raw:
        try:
            cached = json.loads(raw)
        except ValueError:
            cached = None

    if cached and not force_refresh and cached.get("payload", {}).get("version") == CACHE_VERSION:
        fetched_at = datetime.fromisoformat(cached["fetched_at"])
        if now - fetched_at <= FIN_TTL:
            return cached["payload"], "cache"

    try:
        payload = _build(ticker_symbol)
    except Exception:
        if cached and cached.get("payload"):
            return cached["payload"], "cache(stale)"
        raise

    db.set_kv(key, json.dumps({"fetched_at": now.isoformat(), "payload": payload}))
    return payload, "fetched"


if __name__ == "__main__":
    import sys

    import tickers

    ticker, _ = tickers.resolve(sys.argv[1] if len(sys.argv) > 1 else "AAPL")
    payload, source = get_financials(ticker, force_refresh=True)
    print(ticker, payload["currency"], source)
    for name in ("annual", "quarterly"):
        tbl = payload[name]
        df = pd.DataFrame(tbl["rows"], index=tbl["periods"]).T
        pd.set_option("display.width", 200)
        print(df)
