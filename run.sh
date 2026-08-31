#!/usr/bin/env bash
# 기본은 로컬 전용(127.0.0.1)으로, 디버그 모드는 꺼서 띄운다.
# (Flask 디버거가 외부에 노출되면 원격 코드 실행 위험이 있으므로 항상 꺼둔다.)
#
# 같은 네트워크의 다른 PC에서 접근해야 할 때만 HOST를 명시적으로 넘긴다:
#   HOST=0.0.0.0 ./run.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

export HOST="${HOST:-127.0.0.1}"
export PORT="${PORT:-5000}"
export FLASK_DEBUG="${FLASK_DEBUG:-0}"

echo "Starting server on http://${HOST}:${PORT} (debug=${FLASK_DEBUG})"
exec "$SCRIPT_DIR/.venv/bin/python3" app.py
