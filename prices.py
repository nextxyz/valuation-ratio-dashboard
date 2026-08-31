"""N개 종목의 주가 이력을 받아 기준일 대비 상대 수익률로 정규화한다.

- 데이터 출처: yfinance `Ticker.history(start, end, auto_adjust=True)`
  auto_adjust=True 이므로 배당 재투자/액면분할이 반영된 총수익률(Total Return) 기준이다.
- 시장이 다르면 거래일(휴장일)이 다르므로, 모든 종목의 거래일을 합집합으로 모은 뒤
  각 종목은 직전 종가를 유지(forward fill)한다. 상장 이전 구간은 채우지 않고 None으로 둔다.
- 정규화: 각 종목이 기간 안에서 처음 값을 갖는 날을 그 종목의 기준가(base)로 잡고
  (price / base - 1) * 100 을 수익률(%)로 반환한다. 주가의 절대 크기는 쓰지 않는다.

가격은 DB에 캐시하지 않고, 프로세스 내 짧은 TTL 메모리 캐시만 둔다
(재무제표와 달리 매일 바뀌고, history 호출 자체가 충분히 빠르다).
"""

import math
import threading
from datetime import date, datetime, timedelta

import pandas as pd
import yfinance as yf

import tickers

MAX_TICKERS = 8  # 카테고리 색상 슬롯 수와 동일
_CACHE_TTL = timedelta(minutes=10)

_cache = {}
_cache_lock = threading.Lock()


def _parse_date(s, field):
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d").date()
    except Exception:
        raise ValueError(f"{field} 날짜 형식이 올바르지 않습니다 (YYYY-MM-DD).")


def _cache_get(key):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and (datetime.now() - hit[0]) <= _CACHE_TTL:
            return hit[1]
        _cache.pop(key, None)
    return None


def _cache_put(key, value):
    with _cache_lock:
        if len(_cache) > 64:  # 만료된 항목은 읽을 때만 지워지므로 가끔 한 번 청소한다
            now = datetime.now()
            for k in [k for k, (ts, _) in _cache.items() if (now - ts) > _CACHE_TTL]:
                _cache.pop(k, None)
        _cache[key] = (datetime.now(), value)


def fetch_close(yf_ticker, start, end, need_name=True):
    """(pd.Series[date -> close], company_name). 조회 실패/데이터 없음이면 (빈 Series, name).

    need_name=False면 회사명 조회(get_info, 추가 네트워크 호출)를 건너뛴다.
    한글 종목처럼 KRX 목록에서 이미 이름을 아는 경우에 쓴다.
    """
    key = (yf_ticker, start.isoformat(), end.isoformat(), need_name)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    t = yf.Ticker(yf_ticker)
    # yfinance의 end는 exclusive -> to 당일을 포함시키려면 하루 더한다.
    hist = t.history(start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(), auto_adjust=True)

    if hist is None or hist.empty or "Close" not in hist:
        series = pd.Series(dtype="float64")
    else:
        series = hist["Close"].dropna()
        series.index = [pd.Timestamp(ix).date() for ix in series.index]

    name = None
    if need_name and not series.empty:
        try:
            info = t.get_info()
            name = info.get("shortName") or info.get("longName")
        except Exception:
            pass

    result = (series, name)
    _cache_put(key, result)
    return result


def _drawdown(values):
    """정규화 수익률(%) 시퀀스의 최대 낙폭(%). 값이 없으면 None."""
    peak = None
    mdd = 0.0
    for v in values:
        if v is None:
            continue
        level = 1.0 + v / 100.0
        if peak is None or level > peak:
            peak = level
        if peak and peak > 0:
            mdd = min(mdd, (level / peak - 1.0) * 100.0)
    return None if peak is None else mdd


def compare(queries, start, end):
    """입력 종목들의 기준일 대비 상대 수익률을 계산해 프론트가 그대로 그릴 payload를 만든다."""
    if not queries:
        raise ValueError("종목을 하나 이상 입력하세요.")
    if len(queries) > MAX_TICKERS:
        raise ValueError(f"종목은 최대 {MAX_TICKERS}개까지 비교할 수 있습니다.")

    start = start if isinstance(start, date) else _parse_date(start, "시작")
    end = end if isinstance(end, date) else _parse_date(end, "종료")
    if start >= end:
        raise ValueError("시작일은 종료일보다 앞서야 합니다.")

    resolved = []
    seen = set()
    for q in queries:
        yf_ticker, display = tickers.resolve(q)
        if yf_ticker in seen:
            continue
        seen.add(yf_ticker)
        resolved.append((q, yf_ticker, display))

    raw = {}
    warnings = []
    for q, yf_ticker, display in resolved:
        try:
            series, name = fetch_close(yf_ticker, start, end, need_name=not display)
        except Exception as e:
            raise ValueError(f"'{q}' 주가를 불러오지 못했습니다: {e}")
        if series.empty:
            warnings.append(f"'{q}': 해당 기간에 주가 데이터가 없습니다.")
            continue
        raw[yf_ticker] = (series, name)

    if not raw:
        raise ValueError("해당 기간에 주가 데이터를 가진 종목이 없습니다.")

    all_dates = sorted({d for series, _ in raw.values() for d in series.index})

    series_out = []
    for _q, yf_ticker, display in resolved:
        if yf_ticker not in raw:
            continue
        series, name = raw[yf_ticker]
        label = display or (f"{name} ({yf_ticker})" if name else yf_ticker)

        # 합집합 날짜에 맞춰 정렬 + 직전값 채움. 첫 거래일 이전은 채우지 않는다.
        aligned = series.reindex(all_dates).ffill()
        first_valid = series.index[0]

        base = float(series.loc[first_valid])
        prices, returns = [], []
        for d in all_dates:
            v = aligned.get(d)
            if d < first_valid or v is None or (isinstance(v, float) and math.isnan(v)):
                prices.append(None)
                returns.append(None)
            else:
                prices.append(round(float(v), 4))
                returns.append(round((float(v) / base - 1.0) * 100.0, 4))

        last = next((r for r in reversed(returns) if r is not None), None)
        mdd = _drawdown(returns)
        span_days = (all_dates[-1] - first_valid).days
        cagr = None
        # 1년 프리셋(첫 거래일 기준 ~363일)도 포함되도록 살짝 여유를 둔다.
        if last is not None and span_days >= 300:
            cagr = round(((1.0 + last / 100.0) ** (365.0 / span_days) - 1.0) * 100.0, 4)

        series_out.append({
            "ticker": yf_ticker,
            "display": label,
            "base_date": first_valid.isoformat(),
            "base_price": round(base, 4),
            "prices": prices,
            "returns": returns,
            "total_return": last,
            "cagr": cagr,
            "mdd": round(mdd, 4) if mdd is not None else None,
        })

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "dates": [d.isoformat() for d in all_dates],
        "series": series_out,
        "warnings": warnings,
        "basis": "배당 재투자·액면분할 반영 수정주가(총수익률) 기준",
    }


if __name__ == "__main__":
    import json
    import sys

    args = sys.argv[1:]
    qs = args[:-2] if len(args) > 2 else ["AAPL", "삼성전자"]
    s, e = (args[-2], args[-1]) if len(args) > 2 else ("2024-01-01", "2024-12-31")
    out = compare(qs, s, e)
    for row in out["series"]:
        print(f"{row['display']:<30} base {row['base_date']} -> {row['total_return']:+.2f}%  MDD {row['mdd']:.2f}%")
    print(f"points: {len(out['dates'])}, warnings: {out['warnings']}")
