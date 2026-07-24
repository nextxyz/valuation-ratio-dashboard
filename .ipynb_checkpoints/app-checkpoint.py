import math

from flask import Flask, jsonify, render_template

from ratios import METRICS, get_ratio_history

app = Flask(__name__)


def _clean(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return round(float(v), 4)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/ratios/<ticker>")
def api_ratios(ticker):
    ticker = ticker.strip().upper()
    try:
        df, cache_info = get_ratio_history(ticker, years=3)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    if df.empty:
        return jsonify({"error": f"'{ticker}'에 대한 데이터가 부족합니다."}), 400

    payload = {
        "ticker": ticker,
        "dates": [d.strftime("%Y-%m-%d") for d in df["date"]],
        "metrics": {m: [_clean(v) for v in df[m]] for m in METRICS},
        "cache_info": cache_info,
    }
    return jsonify(payload)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
