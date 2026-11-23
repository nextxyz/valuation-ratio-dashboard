"""
한국 상장사 재무제표: OpenDART(금융감독원 전자공시) API -> SQLite 캐시 -> 최근 10년 + 최근 5개 분기 표.

API 호출을 아끼는 방식:
- 정기보고서(사업/반기/분기보고서) 하나 = dart_reports 한 행. 한 번 받은 보고서는 다시 받지 않는다.
  (정정공시로 접수번호가 바뀐 경우만 다시 받아 덮어쓴다.)
- 종목마다 CHECK_TTL(24시간)에 한 번만 공시목록(list.json, 1회 호출)을 보고 새 보고서가 있는지 확인한다.
  그 사이의 조회는 DB만 읽는다. 첫 조회 때만 보고서 수만큼(약 16건 x 2) 호출이 몰린다.
- 표에 필요한 보고서(최근 사업보고서 10개 + 최근 정기보고서 6개)만 남기고 나머지는 지운다.
  새 분기가 나오면 가장 오래된 것이 밀려나므로 캐시는 늘 10년치를 유지한다(이력 누적 X).
- 비율(ROE, PER 등)은 저장하지 않고 읽을 때마다 원본 수치로 계산한다.

보고서별 원본 수치:
- 손익(IS/CIS)·현금흐름(CF)은 회계연도 시작부터의 누적값(cum)과 전년 동기 누적값(prev_cum)으로 저장한다.
  분기 단독값 = 이번 누적 - 직전 분기 누적, 4분기 = 사업보고서(연간) - 3분기 누적.
  TTM(최근 4개 분기 합) = 이번 누적 + 직전 연도 연간 - 전년 동기 누적.
- 재무상태표(BS)는 기말 잔액.
- 주식수는 주식의 총수 현황(stockTotqySttus)의 유통주식수 합계(보통+우선, 자기주식 제외).
  주당순이익(EPS)이 우선주를 포함해 계산되므로 PER = 시가총액 / 지배주주순이익 이 주가/EPS와 맞는다.
  보고서에 주식수가 비어 있으면(삼성전자 3분기 등) 직전 보고서 값을 쓴다.
- 기말 주가는 yfinance 종가(배당 미조정). 보고서를 새로 받을 때 한 번만 받아 같은 행에 저장한다.

- OpenDART에 재무제표가 없는 과거 연도(예: 금융지주는 2022년 이전이 비어 있음)는 1~2년 뒤
  사업보고서의 전기·전전기 값으로 채운다. 그래도 없으면 그 연도는 표에서 빠진다.

연결재무제표(CFS)를 우선하고, 연결 대상이 없는 회사는 별도재무제표(OFS)를 쓴다.
"""

import io
import json
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

import db

BASE_URL = "https://opendart.fss.or.kr/api"
CHECK_TTL = timedelta(hours=24)  # 새 보고서 확인(list.json) 주기
# 재무제표가 없다고 응답한 보고서: 결산 후 이 기간 안이면 공시 직후 반영 지연일 수 있어 다음 확인 때 다시 묻고,
# 그보다 오래된 보고서는 앞으로도 없을 것으로 보고 다시 묻지 않는다.
NODATA_RECENT = timedelta(days=400)
CORP_TTL = timedelta(days=7)  # 고유번호(corp_code) 목록 갱신 주기
ANNUAL_YEARS = 10
QUARTERS = 5
TIMEOUT = 15
WORKERS = 6
# 저장하는 원본 수치의 종류나 의미가 바뀌면 올린다. 버전이 낮은 보고서는 다시 받는다.
DATA_VERSION = 1

REPRT_CODE = {"q1": "11013", "h1": "11012", "q3": "11014", "annual": "11011"}
QUARTER_NO = {"q1": 1, "h1": 2, "q3": 3, "annual": 4}

# key -> (재무제표 구분, [account_id 후보], [account_nm 후보])  앞쪽 후보가 우선
ACCOUNTS = {
    "revenue": ("IS", ["ifrs-full_Revenue"], ["매출액", "수익(매출액)", "영업수익", "매출"]),
    "op_income": ("IS", ["dart_OperatingIncomeLoss"], ["영업이익", "영업이익(손실)", "영업손익"]),
    "net_income": (
        "IS",
        ["ifrs-full_ProfitLossAttributableToOwnersOfParent", "ifrs-full_ProfitLoss"],
        ["지배기업 소유주지분", "지배기업의 소유주에게 귀속되는 당기순이익", "당기순이익", "당기순이익(손실)"],
    ),
    "total_assets": ("BS", ["ifrs-full_Assets"], ["자산총계"]),
    "total_liab": ("BS", ["ifrs-full_Liabilities"], ["부채총계"]),
    "equity": (
        "BS",
        ["ifrs-full_EquityAttributableToOwnersOfParent", "ifrs-full_Equity"],
        ["지배기업 소유주지분", "지배기업의 소유주에게 귀속되는 자본", "자본총계"],
    ),
    "ocf": ("CF", ["ifrs-full_CashFlowsFromUsedInOperatingActivities"], ["영업활동현금흐름", "영업활동으로 인한 현금흐름"]),
    "capex_ppe": (
        "CF",
        ["ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities", "ifrs-full_PurchaseOfPropertyPlantAndEquipment"],
        ["유형자산의 취득", "유형자산의취득", "유형자산 취득"],
    ),
    "capex_intangible": (
        "CF",
        ["ifrs-full_PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities", "ifrs-full_PurchaseOfIntangibleAssets"],
        ["무형자산의 취득", "무형자산의취득", "무형자산 취득"],
    ),
    "div_paid": (
        "CF",
        ["ifrs-full_DividendsPaidClassifiedAsFinancingActivities", "ifrs-full_DividendsPaidClassifiedAsOperatingActivities", "ifrs-full_DividendsPaid"],
        ["배당금의 지급", "배당금 지급", "배당의 지급", "현금배당금의 지급", "현금배당금 지급", "배당금 지급액"],
    ),
}
FLOW_KEYS = ["revenue", "op_income", "net_income", "ocf", "capex", "div_paid"]
STOCK_KEYS = ["total_assets", "total_liab", "equity"]

_REPORT_RE = re.compile(r"(사업|반기|분기)보고서\s*\((\d{4})\.(\d{2})\)")


class DartError(Exception):
    pass


# ---------- 설정 / HTTP ----------


def _load_dotenv():
    """프로젝트 폴더의 .env를 읽어 환경변수로 넣는다(이미 있는 값은 덮어쓰지 않는다)."""
    path = Path(__file__).parent / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


_load_dotenv()


def _api_key():
    key = os.environ.get("DART_API_KEY")
    if not key:
        raise DartError("DART_API_KEY가 설정되지 않았습니다(.env 파일 확인).")
    return key


def _call(endpoint, **params):
    """JSON API 호출. 데이터 없음(013)은 None, 그 외 오류는 DartError."""
    r = requests.get(f"{BASE_URL}/{endpoint}", params={"crtfc_key": _api_key(), **params}, timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()
    status = d.get("status")
    if status == "000":
        return d
    if status == "013":
        return None
    raise DartError(f"OpenDART 오류 {status}: {d.get('message')}")


def _now():
    return datetime.now(timezone.utc)


def _older_than(iso, ttl):
    return iso is None or _now() - datetime.fromisoformat(iso) > ttl


# ---------- 고유번호(corp_code) ----------


def _refresh_corp_codes():
    r = requests.get(f"{BASE_URL}/corpCode.xml", params={"crtfc_key": _api_key()}, timeout=60)
    r.raise_for_status()
    try:
        zf = zipfile.ZipFile(io.BytesIO(r.content))
    except zipfile.BadZipFile:
        raise DartError(f"고유번호 목록을 받지 못했습니다: {r.text[:200]}")
    root = ET.fromstring(zf.read(zf.namelist()[0]))
    rows = []
    for el in root.iter("list"):
        stock = (el.findtext("stock_code") or "").strip()
        if stock:  # 상장사만
            rows.append((el.findtext("corp_code").strip(), stock, (el.findtext("corp_name") or "").strip()))
    if not rows:
        raise DartError("고유번호 목록이 비어 있습니다.")
    db.replace_dart_corps(rows, _now().isoformat())


def corp_code_for(stock_code):
    """6자리 종목코드 -> DART 고유번호. 상장사가 아니거나(ETF 등) 목록에 없으면 None."""
    fetched_at = db.get_kv("dart_corp_fetched_at")
    if _older_than(fetched_at, CORP_TTL):
        try:
            _refresh_corp_codes()
        except Exception:
            if fetched_at is None:
                raise
    code = db.find_dart_corp(stock_code)
    if code is None and _older_than(db.get_kv("dart_corp_fetched_at"), timedelta(days=1)):
        _refresh_corp_codes()  # 신규 상장 등: 하루 한 번까지만 목록을 다시 받는다
        code = db.find_dart_corp(stock_code)
    return code


# ---------- 공시 목록 -> 필요한 보고서 ----------


def _month_end(y, m):
    return (pd.Timestamp(year=y, month=m, day=1) + pd.offsets.MonthEnd(0)).date()


def _list_reports(corp_code):
    """정기보고서 목록(최종본만). [{period_end, kind, fy, rcept_no}] 기간 오름차순."""
    bgn = date.today().replace(year=date.today().year - ANNUAL_YEARS - 2).strftime("%Y%m%d")
    items, page = [], 1
    while True:
        d = _call("list.json", corp_code=corp_code, bgn_de=bgn, pblntf_ty="A", last_reprt_at="Y",
                  page_no=page, page_count=100)
        if d is None:
            break
        items += d.get("list", [])
        if page >= int(d.get("total_page", 1)):
            break
        page += 1

    parsed = {}
    for it in items:
        m = _REPORT_RE.search(it.get("report_nm", ""))
        if not m:
            continue
        typ, y, mo = m.group(1), int(m.group(2)), int(m.group(3))
        pe = _month_end(y, mo)
        prev = parsed.get(pe)
        if prev is None or it["rcept_no"] > prev["rcept_no"]:  # 같은 기간이 둘이면 나중 접수본
            parsed[pe] = {"period_end": pe, "typ": typ, "rcept_no": it["rcept_no"]}

    annual_months = [p["period_end"].month for p in parsed.values() if p["typ"] == "사업"]
    fy_end = max(set(annual_months), key=annual_months.count) if annual_months else 12

    out = []
    for pe, p in sorted(parsed.items()):
        offset = (pe.month - fy_end) % 12  # 결산월로부터 몇 달 뒤인지
        if p["typ"] == "사업":
            kind = "annual"
        elif offset == 6:
            kind = "h1"
        elif offset == 3:
            kind = "q1"
        elif offset == 9:
            kind = "q3"
        else:
            continue  # 결산월 변경 등으로 분기가 맞지 않는 보고서
        fy = pe.year if pe.month <= fy_end else pe.year + 1  # 회계연도 = 결산일이 속한 해
        out.append({"period_end": pe.isoformat(), "kind": kind, "fy": fy, "rcept_no": p["rcept_no"]})
    return out


def _needed(reports):
    """최근 사업보고서 10개 + 최근 정기보고서 6개(5개 분기 + 누적값 차감용 직전 분기)."""
    annual = [r for r in reports if r["kind"] == "annual"][-ANNUAL_YEARS:]
    recent = reports[-(QUARTERS + 1):]
    keep = {r["period_end"]: r for r in annual + recent}
    return [keep[k] for k in sorted(keep)]


# ---------- 보고서 하나 받기 ----------


def _num(s):
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _pick(items, key):
    """ACCOUNTS[key]에 해당하는 계정 한 줄. account_id 우선, 없으면 계정명."""
    sj, ids, names = ACCOUNTS[key]
    sjs = ("IS", "CIS") if sj == "IS" else (sj,)
    rows = [it for it in items if it.get("sj_div") in sjs]
    for aid in ids:
        for s in sjs:
            for it in rows:
                if it.get("account_id") == aid and it.get("sj_div") == s:
                    return it
    for nm in names:
        for it in rows:
            if (it.get("account_nm") or "").replace(" ", "") == nm.replace(" ", ""):
                return it
    return None


def _combine_capex(ppe, intan):
    """CAPEX = 유형자산 취득 + 무형자산 취득 (지출액을 양수로). 둘 다 없으면 None."""
    if ppe is None and intan is None:
        return None
    return sum(abs(x) for x in (ppe, intan) if x is not None)


def _extract(items, kind):
    """전체 재무제표 응답 -> {key: {"cum", "prev_cum", "q"} | 잔액, "prior": {...}}.

    사업보고서는 전기(p1)·전전기(p2) 값도 함께 담는다. OpenDART에 재무제표가 없는 과거 연도
    (금융지주의 2022년 이전 등)를 뒤 연도 보고서의 전기·전전기 값으로 채우는 데 쓴다.
    """
    picked = {key: _pick(items, key) for key in ACCOUNTS}
    has_cf = any(it.get("sj_div") == "CF" for it in items)

    def field(key, name):
        it = picked[key]
        return None if it is None else _num(it.get(name))

    def flow(key):
        if ACCOUNTS[key][0] == "IS" and kind in ("h1", "q3"):
            # 반기·3분기 손익: thstrm=3개월, thstrm_add=누적
            return {"cum": field(key, "thstrm_add_amount"), "prev_cum": field(key, "frmtrm_add_amount"),
                    "q": field(key, "thstrm_amount")}
        # 1분기·연간 손익, 그리고 현금흐름(분기보고서도 thstrm이 누적).
        # 1분기 손익은 전년 동기 값이 frmtrm_add_amount에만 들어 있는 경우가 있다.
        prev = field(key, "frmtrm_amount")
        if prev is None:
            prev = field(key, "frmtrm_add_amount")
        return {"cum": field(key, "thstrm_amount"), "prev_cum": prev, "q": None}

    out = {}
    for key, (sj, _, _) in ACCOUNTS.items():
        if picked[key] is None:
            out[key] = None
        elif sj == "BS":
            out[key] = field(key, "thstrm_amount")
        else:
            out[key] = flow(key)

    ppe, intan = out.pop("capex_ppe"), out.pop("capex_intangible")
    out["capex"] = None if ppe is None and intan is None else {
        f: _combine_capex((ppe or {}).get(f), (intan or {}).get(f)) for f in ("cum", "prev_cum", "q")
    }
    # 배당금 지급: 현금흐름표가 있는데 줄이 없거나 비어 있으면 그 기간엔 지급하지 않은 것(0)으로 본다
    if has_cf:
        dp = out["div_paid"] or {}
        out["div_paid"] = {f: abs(dp.get(f) or 0.0) for f in ("cum", "prev_cum")}
        out["div_paid"]["q"] = abs(dp["q"]) if dp.get("q") is not None else None
    elif out["div_paid"] is not None:
        out["div_paid"] = {f: (abs(v) if v is not None else None) for f, v in out["div_paid"].items()}

    if kind == "annual":
        out["prior"] = {}
        for tag, name in (("p1", "frmtrm_amount"), ("p2", "bfefrmtrm_amount")):
            vals = {key: field(key, name) for key in ACCOUNTS}
            vals["capex"] = _combine_capex(vals.pop("capex_ppe"), vals.pop("capex_intangible"))
            if has_cf:
                vals["div_paid"] = abs(vals["div_paid"] or 0.0)
            elif vals["div_paid"] is not None:
                vals["div_paid"] = abs(vals["div_paid"])
            out["prior"][tag] = vals
    return out


def _shares(corp_code, bsns_year, reprt_code):
    d = _call("stockTotqySttus.json", corp_code=corp_code, bsns_year=bsns_year, reprt_code=reprt_code)
    if d is None:
        return None
    by_se = {(it.get("se") or "").strip(): _num(it.get("distb_stock_co")) for it in d.get("list", [])}
    return by_se.get("합계") or by_se.get("보통주")


def _fetch_report(corp_code, rep):
    """보고서 하나의 재무제표 + 주식수. 재무제표가 없으면 {"fin": False, "shares": ...}."""
    reprt_code = REPRT_CODE[rep["kind"]]
    # 12월 결산이 아닌 회사는 사업연도 표기가 회사마다 달라서 두 가지를 다 시도한다
    years = [rep["fy"]] if rep["period_end"][5:7] == "12" else [rep["fy"], rep["fy"] - 1]
    for bsns_year in years:
        for fs_div in ("CFS", "OFS"):
            d = _call("fnlttSinglAcntAll.json", corp_code=corp_code, bsns_year=bsns_year,
                      reprt_code=reprt_code, fs_div=fs_div)
            if d and d.get("list"):
                data = _extract(d["list"], rep["kind"])
                data["fs_div"] = fs_div
                data["fin"] = True
                data["shares"] = _shares(corp_code, bsns_year, reprt_code)
                return data
    # 재무제표는 없어도 사업보고서의 주식수는 뒤 연도 값으로 채운 행의 시가총액 계산에 쓴다
    shares = _shares(corp_code, years[0], reprt_code) if rep["kind"] == "annual" else None
    return {"fin": False, "shares": shares}


def _naive_index(s):
    s = s.copy()
    s.index = [pd.Timestamp(i).tz_localize(None) if pd.Timestamp(i).tzinfo else pd.Timestamp(i) for i in s.index]
    return s


def _prices_at(ticker, dates):
    """각 결산일 당일 또는 직전 거래일의 '그 당시 실제' 종가.

    yfinance 종가는 auto_adjust=False여도 액면분할은 소급 반영돼 있다(삼성전자 2018년 50:1 분할 전 가격이
    1/50로 나옴). 보고서의 주식수는 분할 전 그대로이므로, 결산일 이후 분할 비율을 곱해 당시 가격으로 되돌린다.
    그러면 같은 행의 주가 x 주식수가 이후 분할과 무관하게 맞는다.
    """
    if not dates:
        return {}
    t = yf.Ticker(ticker)
    start = (pd.Timestamp(min(dates)) - pd.Timedelta(days=15)).strftime("%Y-%m-%d")
    end = (pd.Timestamp(max(dates)) + pd.Timedelta(days=2)).strftime("%Y-%m-%d")
    hist = t.history(start=start, end=end, auto_adjust=False)
    if hist.empty:
        return {}
    closes = _naive_index(hist["Close"])
    splits = t.splits
    splits = _naive_index(splits[splits > 0]) if splits is not None and not splits.empty else pd.Series(dtype=float)
    out = {}
    for d in dates:
        ts = pd.Timestamp(d)
        pos = closes.index.searchsorted(ts, side="right") - 1
        if pos >= 0 and (ts - closes.index[pos]).days <= 10:
            factor = float(splits[splits.index > closes.index[pos]].prod()) if not splits.empty else 1.0
            out[d] = float(closes.iloc[pos]) * factor
    return out


# ---------- 동기화 ----------


def sync(corp_code, ticker, force=False):
    """필요하면 OpenDART에서 새 보고서를 받아 캐시를 갱신한다. 반환: "cache" | "fetched"."""
    check_key = f"dart_checked:{corp_code}"
    if not force and not _older_than(db.get_kv(check_key), CHECK_TTL):
        return "cache"

    needed = _needed(_list_reports(corp_code))
    if not needed:
        raise DartError("OpenDART에 정기보고서가 없습니다.")

    cached = {r["period_end"]: r for r in db.fetch_dart_reports(corp_code)}
    todo = []
    for rep in needed:
        c = cached.get(rep["period_end"])
        if (
            c is None
            or c["rcept_no"] != rep["rcept_no"]  # 정정공시
            or (c["status"] == "nodata"
                and date.today() - date.fromisoformat(rep["period_end"]) < NODATA_RECENT)
            or (c["status"] == "ok" and json.loads(c["data"]).get("v") != DATA_VERSION)
        ):
            todo.append(rep)

    if todo:
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            results = list(ex.map(lambda r: _fetch_report(corp_code, r), todo))
        try:
            prices = _prices_at(ticker, [rep["period_end"] for rep in todo])
        except Exception:
            prices = {}
        now_iso = _now().isoformat()
        rows = []
        for rep, data in zip(todo, results):
            data["price"] = prices.get(rep["period_end"])
            data["v"] = DATA_VERSION
            rows.append((rep["period_end"], rep["kind"], rep["fy"], rep["rcept_no"],
                         "ok" if data.pop("fin") else "nodata", json.dumps(data), now_iso))
        db.upsert_dart_reports(corp_code, rows)

    # 표에 쓰지 않는(10년보다 오래됐거나 밀려난) 보고서는 지운다
    db.prune_dart_reports(corp_code, [r["period_end"] for r in needed])
    db.set_kv(check_key, _now().isoformat())
    return "fetched" if todo else "cache"


# ---------- 캐시 -> 표 ----------


def _flow(rep, key, field):
    v = rep[key]
    return None if v is None else v.get(field)


def _sub(a, b):
    return None if a is None or b is None else a - b


def _add(*xs):
    return None if any(x is None for x in xs) else sum(xs)


def _build_rows(reports):
    """캐시된 보고서 -> (연간 행 목록, 분기 행 목록). 각 행은 financials._ratios가 읽는 원본 dict."""
    loaded = [{**json.loads(r["data"] or "{}"), "status": r["status"], "period_end": r["period_end"],
               "kind": r["kind"], "fy": r["fy"]} for r in reports]
    annual_ok = {r["fy"]: r for r in loaded if r["kind"] == "annual" and r["status"] == "ok"}

    reps = []
    last_shares = None
    for r in loaded:
        if r["status"] != "ok":
            r = _from_later_annual(r, annual_ok)
            if r is None:
                continue
        last_shares = r.get("shares") or last_shares  # 주식수가 빈 보고서는 직전 값으로
        reps.append({**r, "shares": last_shares})

    by_fy = {}
    for r in reps:
        by_fy.setdefault(r["fy"], {})[QUARTER_NO[r["kind"]]] = r
    annual_cum = {fy: q[4] for fy, q in by_fy.items() if 4 in q}

    def base(r):
        return {
            "date": r["period_end"],
            **{k: r[k] for k in STOCK_KEYS},
            "shares": r["shares"],
            "price": r.get("price"),
            "dps_ttm": None,
        }

    annual_rows = []
    for r in reps:
        if r["kind"] != "annual":
            continue
        row = base(r)
        for k in FLOW_KEYS:
            row[k] = _flow(r, k, "cum")
        row["fcf"] = _sub(row["ocf"], row["capex"] or 0.0) if row["ocf"] is not None else None
        row["ni_ttm"], row["div_ttm"] = row["net_income"], row["div_paid"]
        annual_rows.append(row)

    quarter_rows = []
    for r in reps:
        n = QUARTER_NO[r["kind"]]
        prev = by_fy[r["fy"]].get(n - 1) if n > 1 else None
        if n > 1 and prev is None:
            continue  # 직전 분기 누적이 없으면 분기 단독값을 낼 수 없다
        row = base(r)
        for k in FLOW_KEYS:
            cum = _flow(r, k, "cum")
            direct = _flow(r, k, "q")
            row[k] = direct if direct is not None else (cum if n == 1 else _sub(cum, _flow(prev, k, "cum")))
        row["fcf"] = _sub(row["ocf"], row["capex"] or 0.0) if row["ocf"] is not None else None

        # TTM = 이번 누적 + 직전 연도 연간 - 전년 동기 누적 (4분기는 연간 그대로)
        def ttm(k):
            cum = _flow(r, k, "cum")
            if n == 4:
                return cum
            last_year = annual_cum.get(r["fy"] - 1)
            if last_year is None:
                return None
            return _sub(_add(cum, _flow(last_year, k, "cum")), _flow(r, k, "prev_cum"))

        row["ni_ttm"], row["div_ttm"] = ttm("net_income"), ttm("div_paid")
        quarter_rows.append(row)

    return annual_rows, quarter_rows[-QUARTERS:]


def _from_later_annual(r, annual_ok):
    """재무제표가 없는 사업보고서 연도를 1~2년 뒤 사업보고서의 전기·전전기 값으로 채운다."""
    if r["kind"] != "annual":
        return None
    for later, tag in ((annual_ok.get(r["fy"] + 1), "p1"), (annual_ok.get(r["fy"] + 2), "p2")):
        vals = ((later or {}).get("prior") or {}).get(tag)
        if not vals or all(vals.get(k) is None for k in ("revenue", "net_income", "total_assets")):
            continue
        row = {**r, "status": "ok"}
        for k in STOCK_KEYS:
            row[k] = vals.get(k)
        for k in FLOW_KEYS:
            row[k] = {"cum": vals.get(k), "prev_cum": None, "q": None}
        return row
    return None


def get_tables(stock_code, ticker, force=False):
    """반환: (annual_rows, quarter_rows, source, fs_div). 상장사가 아니면 DartError."""
    corp_code = corp_code_for(stock_code)
    if corp_code is None:
        raise DartError(f"'{stock_code}'는 OpenDART에 등록된 상장사가 아닙니다(ETF 등은 재무제표 없음).")
    try:
        source = sync(corp_code, ticker, force=force)
    except Exception:
        if not db.fetch_dart_reports(corp_code):
            raise
        source = "cache(stale)"  # OpenDART 조회 실패 -> 캐시로 폴백
    reports = db.fetch_dart_reports(corp_code)
    annual, quarterly = _build_rows(reports)
    if not annual and not quarterly:
        raise DartError("OpenDART에서 재무제표 데이터를 찾지 못했습니다.")
    latest = next((json.loads(r["data"]) for r in reversed(reports) if r["status"] == "ok"), {})
    return annual, quarterly, source, latest.get("fs_div")
