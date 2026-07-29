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
- db.SCHEMA_VERSION: RAW_COLUMNS에 새 지표용 컬럼을 추가할 때마다 이 값을 올려야 한다.
  안 그러면 이미 캐시된 티커는 새 컬럼이 NULL인 채로 TTL이 지날 때까지(최대 30일) 남아있어
  새로 추가한 지표가 계속 비어 보인다.
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
    "invested_capital": ["Invested Capital"],
    "tax_rate": ["Tax Rate For Calcs"],
    "tax_provision": ["Tax Provision"],
    "pretax_income": ["Pretax Income"],
    "diluted_eps": ["Diluted EPS", "Basic EPS"],
}

METRICS = [
    "PER", "PBR", "PSR", "PEG", "EV_EBITDA", "DividendYield", "FCFYield",
    "ROE", "ROIC", "OperatingMargin", "DebtEquity", "MagicFormula", "GrahamNumber",
]


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


def _tax_rate(income, date):
    """실효세율. 'Tax Rate For Calcs'가 없으면 Tax Provision/Pretax Income으로 계산."""
    rate = _val(_get_row(income, "tax_rate"), date)
    if np.isnan(rate):
        tax = _val(_get_row(income, "tax_provision"), date)
        pretax = _val(_get_row(income, "pretax_income"), date)
        if np.isnan(tax) or np.isnan(pretax) or pretax == 0:
            return np.nan
        rate = tax / pretax
    # 적자인데 세금이 잡히는 등 비정상 구간에서 세율이 음수/100% 초과가 되면
    # NOPAT = EBIT x (1-세율) 이 과대·역전 계상되므로 [0, 1]로 클램프한다.
    return min(max(float(rate), 0.0), 1.0)


def _snapshot(date, income, balance, cashflow, prices, dividends):
    """특정 시점(회계연도 말 등)의 재무 스냅샷 -> raw 값 dict."""
    revenue = _val(_get_row(income, "revenue"), date)
    net_income = _val(_get_row(income, "net_income"), date)
    equity = _val(_get_row(balance, "equity"), date)
    # 항목이 아예 없으면 0이 아니라 NaN으로 둔다 (데이터 누락을 '무부채/무현금'으로 단정하지 않음)
    total_debt = _val(_get_row(balance, "total_debt"), date)
    cash = _val(_get_row(balance, "cash"), date)
    invested_capital = _val(_get_row(balance, "invested_capital"), date)

    shares = _val(_get_row(balance, "shares"), date)
    if np.isnan(shares):
        shares = _val(_get_row(income, "shares_fallback"), date)

    ebit = _val(_get_row(income, "ebit"), date)
    diluted_eps = _val(_get_row(income, "diluted_eps"), date)

    ebitda = _val(_get_row(income, "ebitda"), date)
    if np.isnan(ebitda):
        dep = _val(_get_row(income, "dep_amort"), date, 0.0)
        ebitda = ebit + dep if not np.isnan(ebit) else np.nan

    tax_rate = _tax_rate(income, date)
    nopat = ebit * (1 - tax_rate) if not np.isnan(ebit) and not np.isnan(tax_rate) else np.nan

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
        "total_debt": total_debt,
        "ebit": ebit,
        "invested_capital": invested_capital,
        "nopat": nopat,
        "diluted_eps": diluted_eps,
    }


def _ttm_snapshots(q_income, q_balance, q_cashflow, prices, dividends, skip_dates=frozenset()):
    """분기 재무제표로 계산 가능한 모든 TTM(최근 12개월) 스냅샷 목록.

    Yahoo/yfinance는 보통 분기 데이터를 최근 4~5개 분기까지만 제공하므로,
    4개 분기 윈도우를 만들 수 있는 시점마다(최근 데이터면 보통 1~2개) TTM row를 만든다.
    분기가 지날 때마다 이 함수가 새 날짜의 TTM을 하나씩 더 만들어내고, 그게 DB에 누적되면서
    시간이 지날수록 분기별 이력이 촘촘해진다.

    skip_dates: 이 날짜의 TTM은 만들지 않는다. 12월 결산 기업처럼 회계연도말이 분기말과
    겹치면 TTM 종료일이 연간 결산일과 같아지는데, 그러면 (ticker, date)가 같아서 감사받은
    연간 수치가 4개 분기 단순합으로 덮어써진다(삼성전자 EBIT 기준 6.6% 차이). 연간 수치를
    정본으로 보고 그 날짜는 건너뛴다.
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
        if date.strftime("%Y-%m-%d") in skip_dates:
            continue

        def ttm_sum(row, window=window):
            if row is None:
                return np.nan
            vals = [row.get(c) for c in window]
            if any(v is None or (isinstance(v, float) and np.isnan(v)) for v in vals):
                return np.nan
            return float(np.sum(vals))

        revenue = ttm_sum(_get_row(q_income, "revenue"))
        net_income = ttm_sum(_get_row(q_income, "net_income"))
        ebit = ttm_sum(_get_row(q_income, "ebit"))
        diluted_eps = ttm_sum(_get_row(q_income, "diluted_eps"))  # TTM EPS = 최근 4개 분기 EPS 합

        ebitda = ttm_sum(_get_row(q_income, "ebitda"))
        if np.isnan(ebitda):
            dep = ttm_sum(_get_row(q_income, "dep_amort"))
            ebitda = ebit + (dep if not np.isnan(dep) else 0.0) if not np.isnan(ebit) else np.nan

        # 세율은 분기마다 다르므로, EBIT을 먼저 합산한 뒤 평균세율을 곱하지 않고
        # 분기별로 세후영업이익을 구한 다음 그 값들을 합산한다.
        nopat = 0.0
        for c in window:
            ebit_row = _get_row(q_income, "ebit")
            ebit_q = ebit_row.get(c) if ebit_row is not None else None
            tax_rate_q = _tax_rate(q_income, c)
            if ebit_q is None or (isinstance(ebit_q, float) and np.isnan(ebit_q)) or np.isnan(tax_rate_q):
                nopat = np.nan
                break
            nopat += ebit_q * (1 - tax_rate_q)

        fcf = ttm_sum(_get_row(q_cashflow, "fcf"))
        if np.isnan(fcf):
            ocf = ttm_sum(_get_row(q_cashflow, "op_cashflow"))
            capex = ttm_sum(_get_row(q_cashflow, "capex"))
            fcf = ocf + (capex if not np.isnan(capex) else 0.0) if not np.isnan(ocf) else np.nan

        equity = _val(_get_row(q_balance, "equity"), date)
        total_debt = _val(_get_row(q_balance, "total_debt"), date)
        cash = _val(_get_row(q_balance, "cash"), date)
        invested_capital = _val(_get_row(q_balance, "invested_capital"), date)
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
            "total_debt": total_debt,
            "ebit": ebit,
            "invested_capital": invested_capital,
            "nopat": nopat,
            "diluted_eps": diluted_eps,
        })

    return results


def _yoy_eps_growth_pct(df, tolerance_days=45):
    """각 row마다 '1년 전' 시점에 가장 가까운 과거 row를 찾아 EPS 성장률(%)을 계산.

    연간 스냅샷과 분기 TTM 스냅샷이 섞여 있으므로 단순히 바로 이전 row와 비교하면
    분기 간(QoQ) 변화를 연간(YoY) 성장률처럼 취급하는 오류가 생긴다. 대신 날짜 기준으로
    1년 전에 가장 가까운(허용오차 tolerance_days 이내) row를 찾아서 비교한다.

    EPS 산출 기준(eps_basis)이 다른 두 시점은 비교하지 않는다. 공시 희석EPS와
    순이익/주식수 계산값은 우선주가 있는 종목에서 14%까지 벌어지므로, 기준이 섞이면
    실제로는 없는 성장/역성장이 만들어진다.
    """
    dates = df["date"].tolist()
    eps = df["eps"].tolist()
    basis = df["eps_basis"].tolist()
    growth = [np.nan] * len(df)

    for i in range(len(df)):
        target = dates[i] - pd.DateOffset(years=1)
        best_j, best_diff = None, None
        for j in range(i):
            if basis[j] != basis[i]:
                continue
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
        # 전기가 적자면 성장률이 의미를 갖지 못한다. 적자 -> 흑자 전환을 '고성장'으로 잡으면
        # PEG가 터무니없이 낮게(=저평가처럼) 나오므로 전기 흑자일 때만 계산한다.
        if prev_eps <= 0:
            continue
        growth[i] = (cur_eps - prev_eps) / prev_eps * 100

    return pd.Series(growth, index=df.index)


def _derive_ratios(rows):
    """raw 스냅샷 목록(dict, date 포함) -> 비율까지 계산된 DataFrame."""
    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)

    # EPS는 회사가 공시한 희석EPS를 우선 사용한다. 순이익/기말주식수로 직접 계산하면
    # 우선주가 있는 종목(삼성전자 등)에서 분자는 전사 순이익, 분모는 보통주만이 되어
    # 14%까지 벌어진다. 공시값이 없을 때만 직접 계산으로 폴백하고, 어느 기준으로 구했는지
    # 표시해 둔다(PEG에서 기준이 다른 시점끼리 비교하지 않도록).
    reported = df["diluted_eps"].notna()
    df["eps"] = df["diluted_eps"].where(reported, df["net_income"] / df["shares"])
    df["eps_basis"] = np.where(reported, "reported", "derived")

    # 자기자본이 0 이하(자본잠식)면 이를 분모로 쓰는 지표는 값이 나와도 의미가 없다.
    # 예: 순이익 -200 / 자기자본 -500 -> ROE +40% 로 우량주처럼 보인다.
    equity_pos = df["equity"].where(df["equity"] > 0)

    # EV가 0 이하(순현금 > 시총+부채인 넷넷주)면 EV 기반 지표는 부호가 뒤집혀 의미가 반대가 된다.
    # 예: EBIT +60 / EV -200 -> 이익수익률 -30% 로, 극단적 저평가 기업이 최악처럼 보인다.
    ev_pos = df["ev"].where(df["ev"] > 0)

    df["PER"] = df["market_cap"] / df["net_income"]
    df["PBR"] = df["market_cap"] / equity_pos
    df["PSR"] = df["market_cap"] / df["revenue"]
    df["EV_EBITDA"] = ev_pos / df["ebitda"]
    df["DividendYield"] = df["div_ttm"] / df["price"] * 100
    df["FCFYield"] = df["fcf"] / df["market_cap"] * 100

    eps_growth_pct = _yoy_eps_growth_pct(df)
    peg = df["PER"] / eps_growth_pct
    peg[(eps_growth_pct <= 0)] = np.nan  # 이익이 역성장/적자면 PEG는 의미 없음
    df["PEG"] = peg

    df["ROE"] = df["net_income"] / equity_pos * 100
    df["ROIC"] = df["nopat"] / df["invested_capital"].where(df["invested_capital"] > 0) * 100
    df["OperatingMargin"] = df["ebit"] / df["revenue"] * 100
    df["DebtEquity"] = df["total_debt"] / equity_pos * 100

    earnings_yield = df["ebit"] / ev_pos * 100  # 그린블랏 방식 이익수익률 (EBIT/EV)
    df["MagicFormula"] = df["ROIC"] + earnings_yield  # 그린블랏 마법공식 = ROIC + 이익수익률

    # 그레이엄 넘버는 EPS와 BPS가 '각각' 양수여야 한다. 곱만 검사하면 둘 다 음수인
    # (적자 + 자본잠식) 기업이 통과해서 우량주보다 높은 값이 나온다.
    eps_pos = df["eps"].where(df["eps"] > 0)
    bps_pos = (df["equity"] / df["shares"]).where(lambda s: s > 0)
    df["GrahamNumber"] = np.sqrt(22.5 * eps_pos * bps_pos)

    for col in METRICS:
        df[col] = df[col].replace([np.inf, -np.inf], np.nan)

    return df


def _parse_ts(s):
    return datetime.fromisoformat(s) if s else None


def _fetch_company_name(t):
    """Yahoo Finance에서 회사명(longName/shortName)을 가져온다. 실패하면 None."""
    try:
        info = t.info
    except Exception:
        return None
    if not info:
        return None
    return info.get("longName") or info.get("shortName")


def get_ratio_history(ticker_symbol, years=3, force_refresh=False):
    """DB 캐시를 우선 사용하고, 캐시가 없거나 TTL이 지난 부분만 Yahoo Finance(yfinance)에서 새로 받아온다.

    Returns: (DataFrame, cache_info dict, company_name) — cache_info는 {"annual": ..., "ttm": ...}
    각 값은 "cache" | "fetched" | "cache(stale)" 중 하나. company_name은 없으면 None.
    """
    ticker_symbol = ticker_symbol.strip().upper()
    now = _now()

    meta = db.get_meta(ticker_symbol)
    annual_at = _parse_ts(meta["annual_fetched_at"])
    ttm_at = _parse_ts(meta["ttm_fetched_at"])
    company_name = meta["company_name"]
    schema_outdated = meta["schema_version"] < db.SCHEMA_VERSION

    need_annual = force_refresh or annual_at is None or (now - annual_at) > ANNUAL_TTL or schema_outdated
    need_ttm = force_refresh or ttm_at is None or (now - ttm_at) > TTM_TTL or schema_outdated
    need_name = not company_name

    existing_rows = db.fetch_snapshots(ticker_symbol)
    cache_info = {"annual": "cache", "ttm": "cache"}

    if need_annual or need_ttm or need_name:
        t = yf.Ticker(ticker_symbol)

        if need_annual or need_ttm:
            try:
                # auto_adjust=False: 분할은 소급 조정되지만 배당은 차감되지 않은 '그날 실제 주가'.
                # 기본값(True)은 배당까지 소급 차감해서 과거 주가가 실제보다 낮게 나오고
                # (고배당주는 -20%대), 그 결과 PER 등이 과거일수록 싸게 보이는 가짜 추세가 생긴다.
                # Yahoo의 주식수도 분할 기준으로 재작성되므로 이 조합이 정합적이다.
                hist = t.history(period=f"{years + 2}y", auto_adjust=False)
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
                    db.touch_meta(ticker_symbol, annual_fetched_at=fetched_at_str, schema_version=db.SCHEMA_VERSION)
                    cache_info["annual"] = "fetched"

                if need_ttm:
                    q_income = t.quarterly_income_stmt
                    q_balance = t.quarterly_balance_sheet
                    q_cashflow = t.quarterly_cashflow
                    # 연간 결산일과 겹치는 TTM은 만들지 않는다 (감사받은 연간 수치를 정본으로 둠)
                    annual_dates = {
                        r["date"] for r in db.fetch_snapshots(ticker_symbol) if r["kind"] == "annual"
                    }
                    for ttm in _ttm_snapshots(
                        q_income, q_balance, q_cashflow, prices, dividends, skip_dates=annual_dates
                    ):
                        db.upsert_snapshot(ticker_symbol, ttm["date"].strftime("%Y-%m-%d"), "ttm", ttm, fetched_at_str)
                    db.touch_meta(ticker_symbol, ttm_fetched_at=fetched_at_str, schema_version=db.SCHEMA_VERSION)
                    cache_info["ttm"] = "fetched"

            except Exception:
                if not existing_rows:
                    raise
                # Yahoo Finance 호출 실패(네트워크 등) -> 캐시된 값으로 폴백
                cache_info = {"annual": "cache(stale)", "ttm": "cache(stale)"}

        if need_name:
            fetched_name = _fetch_company_name(t)
            if fetched_name:
                company_name = fetched_name
                db.touch_meta(ticker_symbol, company_name=company_name)

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

    return df, cache_info, company_name


def _now():
    return datetime.now(timezone.utc)


def compute_ratios(ticker_symbol, years=3):
    """캐시 정보 없이 DataFrame만 필요할 때 쓰는 얇은 wrapper (CLI 등)."""
    df, _, _ = get_ratio_history(ticker_symbol, years=years)
    return df


if __name__ == "__main__":
    import sys

    import tickers

    query = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    ticker, display = tickers.resolve(query)
    result, info, company_name = get_ratio_history(ticker)
    pd.set_option("display.width", 160)
    print(f"{query} -> {ticker} ({display or company_name})")
    print(f"cache_info: {info}")
    print(result[["date", "price"] + METRICS])
