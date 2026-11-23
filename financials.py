"""
재무제표 요약표: 연도별 + 최근 5개 분기.

- 한국 상장사(000000.KS / .KQ): OpenDART에서 받아 최근 10년 + 최근 5개 분기만 캐시한다(dart.py).
- 그 외(해외 티커): OpenDART에 없으므로 아래의 yfinance 방식을 그대로 쓴다.

yfinance 저장 방식 (db.fin_snapshots):
- 원본 수치(매출·이익·잔액·현금흐름, 결산일 주가·주당배당금)만 (ticker, period, date) 단위로
  upsert하고 지우지 않는다. 비율(ROE, PER 등)은 읽을 때마다 원본으로부터 다시 계산한다.
- yfinance는 연간 4~5년, 분기 6~7개 분기만 주지만, 조회가 반복될수록 새 기간이 추가되고
  예전 기간은 남아 있으므로 시간이 지날수록 표에 보이는 과거가 길어진다.
- 새로 받은 값이 비어 있으면 기존 값을 유지한다(가장 오래된 열이 빈 채로 오는 경우 대비).
- 연간은 ANNUAL_TTL, 분기는 QUARTERLY_TTL마다 yfinance에서 다시 받는다. 그 사이엔 DB만 읽는다.
- FIN_SCHEMA_VERSION: 저장하는 원본 컬럼이나 그 의미가 바뀌면 올린다. 저장된 버전이 낮은
  티커는 TTL과 무관하게 다시 조회된다(기존 행은 지우지 않고 갱신).

분기 표의 기준:
- 매출액·영업이익·당기순이익·CAPEX·FCF: 해당 분기 단독 값
- 영업이익률·순이익률: 해당 분기 기준
- ROE·ROA: 분기 순이익 x 4 (연환산) / 분기말 자본·자산
- PER·배당성향: 연속된 4개 분기 합(TTM) 기준. DB에 연속 4개 분기가 없으면 비워 둔다.
- PBR·부채비율·배당수익률: 분기말 시점 기준
"""

import json
import math
import re
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import yfinance as yf

import dart
import db

_KR_TICKER_RE = re.compile(r"^([0-9A-Z]{6})\.(KS|KQ)$")

ANNUAL_TTL = timedelta(days=30)
QUARTERLY_TTL = timedelta(hours=24)
QUARTERS = 5
FIN_SCHEMA_VERSION = 1

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

AMOUNT_KEYS = ["revenue", "op_income", "net_income", "total_assets", "total_liab", "equity", "capex", "fcf"]

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


def _div(a, b, scale=1.0):
    if _nan(a) or _nan(b) or b == 0:
        return None
    return a / b * scale


def _pos(v):
    return v if not _nan(v) and v > 0 else None


def _to_naive(ts):
    ts = pd.Timestamp(ts)
    return ts.tz_localize(None) if ts.tzinfo is not None else ts


# ---------- yfinance -> 원본 수치 ----------


def _get(stmts, key, date):
    names, which = ROWS[key]
    df = stmts.get(which)
    if df is None or df.empty:
        return None
    for name in names:
        if name in df.index:
            v = df.loc[name].get(date)
            return None if _nan(v) else float(v)
    return None


def _price_at(prices, date):
    """date 당일 또는 그 이전 가장 최근 종가 (결산일이 휴장일인 경우 대비)."""
    if prices is None or prices.empty:
        return None
    pos = prices.index.searchsorted(date, side="right") - 1
    if pos < 0 or (date - prices.index[pos]).days > 10:  # 상장 전 등 그 시점 주가가 없음
        return None
    return float(prices.iloc[pos])


def _dps_ttm(dividends, date):
    if dividends is None or dividends.empty:
        return 0.0
    window = dividends[(dividends.index > date - pd.Timedelta(days=365)) & (dividends.index <= date)]
    return float(window.sum())


def _raw_rows(stmts, prices, dividends):
    """재무제표 묶음 -> [(date_str, raw dict)]. 금액 항목이 하나도 없는 기간은 저장하지 않는다."""
    dates = set()
    for df in stmts.values():
        if df is not None and not df.empty:
            dates.update(df.columns)

    rows = []
    for d in sorted(dates):
        raw = {k: _get(stmts, k, d) for k in ROWS if k != "ocf"}
        # CAPEX는 지출액을 양수로 저장(yfinance는 음수). FCF가 없으면 영업현금흐름 - CAPEX.
        if raw["capex"] is not None:
            raw["capex"] = abs(raw["capex"])
        if raw["fcf"] is None:
            ocf = _get(stmts, "ocf", d)
            if ocf is not None:
                raw["fcf"] = ocf - (raw["capex"] or 0.0)
        if all(raw[k] is None for k in AMOUNT_KEYS):
            continue
        raw["price"] = _price_at(prices, d)
        raw["dps_ttm"] = _dps_ttm(dividends, d) if raw["price"] is not None else None
        rows.append((d.strftime("%Y-%m-%d"), raw))
    return rows


def _normalize(df):
    if df is None or df.empty:
        return df
    df = df.copy()
    df.columns = [_to_naive(c) for c in df.columns]
    return df


def _fetch_and_store(ticker_symbol, need_annual, need_quarterly, now_iso):
    t = yf.Ticker(ticker_symbol)
    stmts = {}
    if need_annual:
        stmts["annual"] = {
            "income": _normalize(t.income_stmt),
            "balance": _normalize(t.balance_sheet),
            "cashflow": _normalize(t.cashflow),
        }
        if stmts["annual"]["income"] is None or stmts["annual"]["income"].empty:
            raise ValueError(f"'{ticker_symbol}' 재무제표 데이터를 가져올 수 없습니다.")
    if need_quarterly:
        stmts["quarterly"] = {
            "income": _normalize(t.quarterly_income_stmt),
            "balance": _normalize(t.quarterly_balance_sheet),
            "cashflow": _normalize(t.quarterly_cashflow),
        }

    all_dates = [c for s in stmts.values() for df in s.values() if df is not None for c in df.columns]
    if not all_dates:
        raise ValueError(f"'{ticker_symbol}' 재무제표 데이터를 가져올 수 없습니다.")

    # 배당 미조정 실제 주가 (ratios.py와 같은 이유: 과거 PER이 싸게 보이는 왜곡 방지)
    start = (min(all_dates) - pd.Timedelta(days=30)).strftime("%Y-%m-%d")
    hist = t.history(start=start, auto_adjust=False)
    prices = hist["Close"] if not hist.empty else pd.Series(dtype=float)
    prices.index = [_to_naive(d) for d in prices.index]

    dividends = t.dividends
    if dividends is not None and not dividends.empty:
        dividends = dividends.copy()
        dividends.index = [_to_naive(d) for d in dividends.index]

    for period, s in stmts.items():
        db.upsert_fin_snapshots(ticker_symbol, period, _raw_rows(s, prices, dividends), now_iso)

    currency = None
    try:
        info = t.info or {}
        currency = info.get("financialCurrency") or info.get("currency")
    except Exception:
        pass
    return currency


# ---------- 원본 수치 -> 표 ----------


def _ratios(r, ni_annual, div_paid_annual, roe_roa_ni):
    """r: 원본 행. ni_annual/div_paid_annual: PER·배당성향에 쓸 연간(또는 TTM) 값.
    roe_roa_ni: ROE·ROA 분자(연간은 그대로, 분기는 x4 연환산)."""
    price, shares, dps = r["price"], r["shares"], r["dps_ttm"]
    equity_pos = _pos(r["equity"])
    market_cap = price * shares if not _nan(price) and not _nan(shares) else None

    # 적자면 PER·배당성향은 의미가 없으므로 비운다
    per = _div(market_cap, ni_annual) if _pos(ni_annual) else None
    payout = None
    if _pos(ni_annual):
        if not _nan(div_paid_annual):
            payout = abs(div_paid_annual) / ni_annual * 100
        elif not _nan(dps) and not _nan(shares) and dps > 0:
            payout = dps * shares / ni_annual * 100
        elif dps == 0:
            payout = 0.0

    return {
        "op_margin": _div(r["op_income"], r["revenue"], 100),
        "net_margin": _div(r["net_income"], r["revenue"], 100),
        "roe": _div(roe_roa_ni, equity_pos, 100),
        "roa": _div(roe_roa_ni, r["total_assets"], 100),
        "debt_ratio": _div(r["total_liab"], equity_pos, 100),
        "per": per,
        "pbr": _div(market_cap, equity_pos),
        "div_yield": _div(dps, _pos(price), 100),
        "payout": payout,
    }


def _ttm(rows, i, key):
    """rows[i]를 끝으로 하는 연속된 4개 분기 합. 중간에 빠진 분기가 있거나 값이 비면 None."""
    if i < 3:
        return None
    window = rows[i - 3 : i + 1]
    span = (pd.Timestamp(window[-1]["date"]) - pd.Timestamp(window[0]["date"])).days
    if not 250 <= span <= 300:  # 분기말 4개가 연속이면 약 273일
        return None
    vals = [w[key] for w in window]
    if any(_nan(v) for v in vals):
        return None
    return float(sum(vals))


def _annual_table(rows):
    cols = []
    for r in rows:
        cols.append({**{k: r[k] for k in AMOUNT_KEYS}, **_ratios(r, r["net_income"], r["div_paid"], r["net_income"])})
    labels = [f"{r['date'][:4]}.{r['date'][5:7]}" for r in rows]
    return cols, labels


def _quarterly_table(rows):
    cols = []
    for i, r in enumerate(rows):
        annualized = r["net_income"] * 4 if not _nan(r["net_income"]) else None
        ratios = _ratios(r, _ttm(rows, i, "net_income"), _ttm(rows, i, "div_paid"), annualized)
        cols.append({**{k: r[k] for k in AMOUNT_KEYS}, **ratios})
    labels = []
    for r in rows:
        d = pd.Timestamp(r["date"])
        labels.append(f"{d.year}.{(d.month - 1) // 3 + 1}Q")
    return cols[-QUARTERS:], labels[-QUARTERS:]


def _table(cols, labels):
    def clean(v):
        return None if _nan(v) or math.isinf(v) else float(v)

    return {"periods": labels, "rows": {k: [clean(c[k]) for c in cols] for k, _, _ in METRIC_ROWS}}


# ---------- 진입점 ----------


def _meta_key(ticker):
    return f"fin_meta:{ticker}"


def _load_meta(ticker):
    raw = db.get_kv(_meta_key(ticker))
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def _expired(meta, field, ttl, now):
    at = meta.get(field)
    return at is None or now - datetime.fromisoformat(at) > ttl


def _dart_table(rows, labels, quarterly):
    cols = []
    for r in rows:
        roe_roa_ni = r["net_income"] * 4 if quarterly and not _nan(r["net_income"]) else r["net_income"]
        ratios = _ratios(r, r["ni_ttm"], r["div_ttm"], roe_roa_ni)
        # 배당수익률 = (연간 또는 TTM) 배당금 지급액 / 기말 시가총액
        market_cap = r["price"] * r["shares"] if not _nan(r["price"]) and not _nan(r["shares"]) else None
        ratios["div_yield"] = _div(r["div_ttm"], _pos(market_cap), 100)
        cols.append({**{k: r[k] for k in AMOUNT_KEYS}, **ratios})
    return _table(cols, labels)


def _get_dart_financials(stock_code, ticker_symbol, force_refresh):
    annual, quarterly, source, fs_div = dart.get_tables(stock_code, ticker_symbol, force=force_refresh)
    a_labels = [f"{r['date'][:4]}.{r['date'][5:7]}" for r in annual]
    q_labels = [f"{r['date'][:4]}.{(int(r['date'][5:7]) - 1) // 3 + 1}Q" for r in quarterly]
    # 예전 yfinance 방식으로 쌓아둔 이 종목의 재무제표 행은 더 이상 쓰지 않으므로 정리
    db.delete_fin_snapshots(ticker_symbol)
    payload = {
        "currency": "KRW",
        "data_source": "OpenDART " + ("연결" if fs_div == "CFS" else "별도"),
        "metrics": [{"key": k, "label": label, "kind": kind} for k, label, kind in METRIC_ROWS],
        "annual": _dart_table(annual, a_labels, quarterly=False),
        "quarterly": _dart_table(quarterly, q_labels, quarterly=True),
    }
    return payload, source


def get_financials(ticker_symbol, force_refresh=False):
    """Returns: (payload dict, source) — source는 "cache" | "fetched" | "cache(stale)"."""
    ticker_symbol = ticker_symbol.strip().upper()
    m = _KR_TICKER_RE.match(ticker_symbol)
    if m:
        return _get_dart_financials(m.group(1), ticker_symbol, force_refresh)
    now = datetime.now(timezone.utc)
    meta = _load_meta(ticker_symbol)
    outdated = meta.get("version", 0) < FIN_SCHEMA_VERSION

    need_annual = force_refresh or outdated or _expired(meta, "annual_at", ANNUAL_TTL, now)
    need_quarterly = force_refresh or outdated or _expired(meta, "quarterly_at", QUARTERLY_TTL, now)

    source = "cache"
    if need_annual or need_quarterly:
        try:
            currency = _fetch_and_store(ticker_symbol, need_annual, need_quarterly, now.isoformat())
            if need_annual:
                meta["annual_at"] = now.isoformat()
            if need_quarterly:
                meta["quarterly_at"] = now.isoformat()
            meta["currency"] = currency or meta.get("currency")
            meta["version"] = FIN_SCHEMA_VERSION
            db.set_kv(_meta_key(ticker_symbol), json.dumps(meta))
            source = "fetched"
        except Exception:
            if not db.fetch_fin_snapshots(ticker_symbol, "annual"):
                raise
            source = "cache(stale)"  # Yahoo 조회 실패 -> 쌓여 있는 값으로 폴백

    annual = _table(*_annual_table(db.fetch_fin_snapshots(ticker_symbol, "annual")))
    quarterly = _table(*_quarterly_table(db.fetch_fin_snapshots(ticker_symbol, "quarterly")))

    payload = {
        "currency": meta.get("currency"),
        "data_source": "Yahoo Finance",
        "metrics": [{"key": k, "label": label, "kind": kind} for k, label, kind in METRIC_ROWS],
        "annual": annual,
        "quarterly": quarterly,
    }
    return payload, source


if __name__ == "__main__":
    import sys

    import tickers

    ticker, _ = tickers.resolve(sys.argv[1] if len(sys.argv) > 1 else "AAPL")
    payload, source = get_financials(ticker, force_refresh="--refresh" in sys.argv)
    print(ticker, payload["currency"], source)
    pd.set_option("display.width", 200)
    for name in ("annual", "quarterly"):
        tbl = payload[name]
        print(pd.DataFrame(tbl["rows"], index=tbl["periods"]).T)
