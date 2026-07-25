"""사용자 입력(한글 종목명 / 6자리 코드 / 영문 티커)을 yfinance 티커로 변환.

- KRX 상장 목록(코드·한글명·시장구분)은 FinanceDataReader로 받아 SQLite(db.stock_map)에 캐시한다.
- 목록은 LISTING_TTL 동안 유효한 것으로 보고, 그 안에는 네트워크 호출을 하지 않는다.
- yfinance 티커 접미사: 코스피=.KS, 코스닥/코스닥글로벌/코넥스=.KQ
"""

import re
from datetime import datetime, timedelta, timezone

import db

LISTING_TTL = timedelta(days=7)

_HANGUL_RE = re.compile(r"[가-힣]")
_CODE_RE = re.compile(r"^\d{6}$")

# FinanceDataReader의 Market 값 -> yfinance 접미사
MARKET_SUFFIX = {
    "KOSPI": ".KS",
    "KOSDAQ": ".KQ",
    "KOSDAQ GLOBAL": ".KQ",
    "KONEX": ".KQ",
}


def _now():
    return datetime.now(timezone.utc)


def _normalize(name):
    """공백 제거 + 소문자화(영문 혼용 대비). 한글은 그대로."""
    return re.sub(r"\s+", "", name or "").lower()


def has_hangul(s):
    return bool(_HANGUL_RE.search(s or ""))


def _suffix(market):
    return MARKET_SUFFIX.get(market, ".KS")


def to_yf_ticker(code, market):
    return f"{str(code).zfill(6)}{_suffix(market)}"


def _parse_ts(s):
    return datetime.fromisoformat(s) if s else None


def ensure_listing(force=False):
    """KRX 상장 목록 캐시를 확보한다. TTL이 유효하면 아무것도 하지 않는다."""
    last = _parse_ts(db.get_kv("stock_map_fetched_at"))
    fresh = last is not None and (_now() - last) <= LISTING_TTL
    if not force and fresh and db.stock_map_count() > 0:
        return

    try:
        import FinanceDataReader as fdr  # 지연 import: 한글 조회가 처음 필요할 때만 로드
        listing = fdr.StockListing("KRX")
    except Exception:
        # 네트워크/라이브러리 문제 -> 기존 캐시가 있으면 그대로 쓰고, 없으면 에러
        if db.stock_map_count() > 0:
            return
        raise

    rows = []
    for code, name, market in zip(listing["Code"], listing["Name"], listing["Market"]):
        if not code or not name or market not in MARKET_SUFFIX:
            continue
        rows.append((str(code).zfill(6), str(name), str(market), _normalize(str(name))))

    if rows:
        db.replace_stock_map(rows, _now().isoformat())


def resolve(query):
    """입력을 (yf_ticker, display) 로 변환.

    - 한글명   : 정확 일치하는 종목의 코드+접미사로 변환. 없으면 후보를 담은 ValueError.
    - 6자리코드: 상장 목록에서 시장을 찾아 접미사를 붙인다(못 찾으면 그대로).
    - 그 외    : 그대로 대문자화(영문 티커, 이미 .KS/.KQ가 붙은 코드 등).

    display 는 사람이 읽기 좋은 이름(예: "삼성전자 (005930)")이며 없으면 None.
    """
    q = (query or "").strip()
    if not q:
        raise ValueError("티커 또는 종목명을 입력하세요.")

    if has_hangul(q):
        ensure_listing()
        norm = _normalize(q)
        matches = db.find_stock_by_name(norm)
        if not matches:
            candidates = db.search_stock_by_name(norm)
            if not candidates:
                raise ValueError(f"'{query}'에 해당하는 종목을 찾을 수 없습니다.")
            hint = ", ".join(f"{c['name']}({c['code']})" for c in candidates)
            raise ValueError(f"'{query}'와 정확히 일치하는 종목이 없습니다. 혹시: {hint}")
        m = matches[0]
        return to_yf_ticker(m["code"], m["market"]), f"{m['name']} ({m['code']})"

    if _CODE_RE.match(q):
        ensure_listing()
        found = db.find_stock_by_code(q)
        if found:
            return to_yf_ticker(found["code"], found["market"]), f"{found['name']} ({found['code']})"
        return q, None  # 목록에 없으면 그대로 (yfinance가 판단)

    return q.upper(), None


if __name__ == "__main__":
    import sys

    query = sys.argv[1] if len(sys.argv) > 1 else "삼성전자"
    yf_ticker, display = resolve(query)
    print(f"input   : {query}")
    print(f"ticker  : {yf_ticker}")
    print(f"display : {display}")
