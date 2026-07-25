import math

from flask import Flask, jsonify, render_template

import tickers
from ratios import METRICS, get_ratio_history

app = Flask(__name__)


def _clean(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return round(float(v), 4)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/ratios/<path:ticker>")
def api_ratios(ticker):
    try:
        yf_ticker, display = tickers.resolve(ticker)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    try:
        df, cache_info = get_ratio_history(yf_ticker, years=3)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    if df.empty:
        return jsonify({"error": f"'{yf_ticker}'에 대한 데이터가 부족합니다."}), 400

    payload = {
        "ticker": yf_ticker,
        "display": display or yf_ticker,
        "dates": [d.strftime("%Y-%m-%d") for d in df["date"]],
        "metrics": {m: [_clean(v) for v in df[m]] for m in METRICS},
        "cache_info": cache_info,
    }
    return jsonify(payload)


if __name__ == "__main__":
    import os

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host=host, port=port, debug=debug)
