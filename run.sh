#!/usr/bin/env bash
# 외부(네트워크상의 다른 PC)에서 접근 가능하도록 0.0.0.0으로 서버를 띄운다.
# 디버그 모드는 반드시 꺼서 실행한다 (Flask 디버거가 외부에 노출되면 원격 코드 실행 위험).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

export HOST="0.0.0.0"
export PORT="${PORT:-5000}"
export FLASK_DEBUG="0"

echo "Starting server on http://${HOST}:${PORT} (debug off)"
exec "$SCRIPT_DIR/.venv/bin/python3" app.py
