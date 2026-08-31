import math
import os
import re
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, make_response, render_template, request, url_for

import db
import prices
import tickers
from ratios import METRICS, get_ratio_history

app = Flask(__name__)

# 방문 집계: 서버가 어느 타임존에 있든 "오늘"은 KST 기준으로 센다.
KST = ZoneInfo("Asia/Seoul")
VISITOR_COOKIE = "vid"
VISITOR_COOKIE_MAX_AGE = 60 * 60 * 24 * 730  # 2년

# 쿠키로 들어온 값은 우리가 발급한 형식(uuid4 hex)일 때만 신뢰한다.
# 검증 없이 그대로 쓰면 아무 문자열이나 보내 visits 테이블을 부풀릴 수 있다.
_VISITOR_RE = re.compile(r"^[0-9a-f]{32}$")

_BOT_RE = re.compile(
    r"bot|crawl|spider|slurp|curl|wget|python-requests|httpx|okhttp|java/|go-http|"
    r"headless|phantom|puppeteer|playwright|lighthouse|monitor|uptime|pingdom|scan|"
    r"facebookexternalhit|preview|fetcher|feed",
    re.I,
)


def _is_bot(user_agent):
    # UA가 아예 없는 요청도 사람이 브라우저로 들어온 경우는 아니다.
    return not user_agent or bool(_BOT_RE.search(user_agent))


def _kst_today():
    return datetime.now(KST).strftime("%Y-%m-%d")


@app.context_processor
def _static_url_helper():
    """템플릿용 static_url(): /static/main.js?v=<파일 수정시각>.

    Flask는 static에 Cache-Control: no-cache를 붙여 매번 재검증하게 하지만,
    앞단 CDN(Cloudflare의 Browser Cache TTL 기본 4시간)이 이를 덮어써서
    배포 직후 방문자가 새 HTML + 낡은 JS 조합을 받는 일이 생긴다.
    파일이 바뀌면 URL이 바뀌게 해서 그 창을 아예 없앤다.
    """

    def static_url(filename):
        try:
            version = int(os.stat(os.path.join(app.static_folder, filename)).st_mtime)
        except OSError:
            return url_for("static", filename=filename)
        return url_for("static", filename=filename, v=version)

    return {"static_url": static_url}


def _clean(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return round(float(v), 4)


@app.route("/")
def index():
    """대시보드 페이지. 렌더링하면서 오늘(KST) 방문을 한 건 기록한다.

    집계는 이 라우트에서만 한다(정적 파일·API 호출은 세지 않는다). 쿠키 하나로
    사람을 구분하므로 같은 브라우저로 여러 번 들어와도 순방문자는 1명이다.
    """
    visitor = request.cookies.get(VISITOR_COOKIE)
    is_new_visitor = not visitor or not _VISITOR_RE.match(visitor)
    if is_new_visitor:
        visitor = uuid.uuid4().hex

    counted = not _is_bot(request.headers.get("User-Agent", ""))
    visitors_today = None
    if counted:
        day = _kst_today()
        # 집계가 실패해도 페이지는 떠야 한다. 카운터는 어디까지나 부가 기능이다.
        try:
            db.record_visit(day=day, visitor=visitor, now_iso=datetime.now(timezone.utc).isoformat())
            visitors_today = db.visit_stats(day)[0]
        except Exception:
            visitors_today = None

    resp = make_response(render_template("index.html", visitors_today=visitors_today))
    if counted and is_new_visitor:
        resp.set_cookie(
            VISITOR_COOKIE,
            visitor,
            max_age=VISITOR_COOKIE_MAX_AGE,
            httponly=True,
            samesite="Lax",
        )
    return resp


@app.route("/api/ratios/<path:ticker>")
def api_ratios(ticker):
    try:
        yf_ticker, display = tickers.resolve(ticker)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    try:
        df, cache_info, company_name = get_ratio_history(yf_ticker, years=3)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    if df.empty:
        return jsonify({"error": f"'{yf_ticker}'에 대한 데이터가 부족합니다."}), 400

    if not display and company_name:
        display = f"{company_name} ({yf_ticker})"

    payload = {
        "ticker": yf_ticker,
        "display": display or yf_ticker,
        "dates": [d.strftime("%Y-%m-%d") for d in df["date"]],
        "metrics": {m: [_clean(v) for v in df[m]] for m in METRICS},
        "cache_info": cache_info,
    }
    return jsonify(payload)


@app.route("/api/compare")
def api_compare():
    """N개 종목의 기준일 대비 상대 수익률. ?tickers=AAPL,삼성전자&from=YYYY-MM-DD&to=YYYY-MM-DD"""
    raw = request.args.get("tickers", "")
    queries = [q.strip() for q in raw.split(",") if q.strip()]

    try:
        payload = prices.compare(queries, request.args.get("from", ""), request.args.get("to", ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"주가를 불러오지 못했습니다: {e}"}), 400

    return jsonify(payload)


if __name__ == "__main__":
    import os

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug)
