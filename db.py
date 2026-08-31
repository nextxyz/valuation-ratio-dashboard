"""SQLite 캐시 레이어. 원본 재무 스냅샷(raw)만 저장하고, 비율(PER 등)은 매번 재계산한다."""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "cache.sqlite3"

RAW_COLUMNS = [
    "price", "revenue", "net_income", "equity", "shares",
    "ebitda", "fcf", "market_cap", "ev", "div_ttm",
    "total_debt", "ebit", "invested_capital", "nopat",
    "diluted_eps",
]

# RAW_COLUMNS에 새 컬럼을 추가하거나, 기존 컬럼에 저장되는 값의 의미가 바뀌면 이 값을 1 증가시킨다.
# ticker_meta.schema_version이 이보다 낮은 티커는 TTL과 무관하게 강제로 다시 조회된다.
#   v1: 초기 스키마
#   v2: diluted_eps 추가 + price/market_cap/ev를 배당 미조정(auto_adjust=False) 기준으로 변경
#       + total_debt 누락 시 0이 아닌 NULL 저장
SCHEMA_VERSION = 2


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    try:
        cols_sql = ", ".join(f"{c} REAL" for c in RAW_COLUMNS)
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS ratio_snapshots (
                ticker TEXT NOT NULL,
                date TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('annual', 'ttm')),
                {cols_sql},
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (ticker, date)
            )
        """)
        for col in RAW_COLUMNS:
            try:
                conn.execute(f"ALTER TABLE ratio_snapshots ADD COLUMN {col} REAL")
                conn.commit()
            except sqlite3.OperationalError:
                pass  # 기존 DB에 이미 컬럼이 있으면 무시 (구버전 DB 마이그레이션)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ticker_meta (
                ticker TEXT PRIMARY KEY,
                annual_fetched_at TEXT,
                ttm_fetched_at TEXT,
                company_name TEXT,
                schema_version INTEGER
            )
        """)
        for col, coltype in [("company_name", "TEXT"), ("schema_version", "INTEGER")]:
            try:
                conn.execute(f"ALTER TABLE ticker_meta ADD COLUMN {col} {coltype}")
                conn.commit()
            except sqlite3.OperationalError:
                pass  # 기존 DB에 이미 컬럼이 있으면 무시 (예전 버전 DB 마이그레이션)
        # 한글 종목명 <-> 코드 매핑 (KRX 상장 목록)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_map (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                market TEXT NOT NULL,
                name_norm TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stock_map_name_norm ON stock_map(name_norm)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kv_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # 방문 집계. day는 KST 기준 날짜(YYYY-MM-DD), visitor는 쿠키에 심은 랜덤 ID.
        # PK 앞자리가 day라 "그날의 행"만 훑는 조회는 이 인덱스로 끝난다.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS visits (
                day TEXT NOT NULL,
                visitor TEXT NOT NULL,
                hits INTEGER NOT NULL DEFAULT 1,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                PRIMARY KEY (day, visitor)
            )
        """)
        conn.commit()
    finally:
        conn.close()


def get_meta(ticker):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT annual_fetched_at, ttm_fetched_at, company_name, schema_version FROM ticker_meta WHERE ticker = ?",
            (ticker,),
        ).fetchone()
        if not row:
            return {"annual_fetched_at": None, "ttm_fetched_at": None, "company_name": None, "schema_version": 0}
        d = dict(row)
        d["schema_version"] = d["schema_version"] or 0  # 예전 DB(컬럼 추가 전)는 0으로 취급 -> 항상 최신 스키마보다 낮음
        return d
    finally:
        conn.close()


def touch_meta(ticker, annual_fetched_at=None, ttm_fetched_at=None, company_name=None, schema_version=None):
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT annual_fetched_at, ttm_fetched_at, company_name, schema_version FROM ticker_meta WHERE ticker = ?",
            (ticker,),
        ).fetchone()
        new_annual = annual_fetched_at if annual_fetched_at is not None else (existing["annual_fetched_at"] if existing else None)
        new_ttm = ttm_fetched_at if ttm_fetched_at is not None else (existing["ttm_fetched_at"] if existing else None)
        new_name = company_name if company_name is not None else (existing["company_name"] if existing else None)
        new_schema = schema_version if schema_version is not None else (existing["schema_version"] if existing else None)
        conn.execute(
            """
            INSERT INTO ticker_meta (ticker, annual_fetched_at, ttm_fetched_at, company_name, schema_version)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                annual_fetched_at = excluded.annual_fetched_at,
                ttm_fetched_at = excluded.ttm_fetched_at,
                company_name = excluded.company_name,
                schema_version = excluded.schema_version
            """,
            (ticker, new_annual, new_ttm, new_name, new_schema),
        )
        conn.commit()
    finally:
        conn.close()


def upsert_snapshot(ticker, date_str, kind, raw, fetched_at):
    conn = get_connection()
    try:
        cols = ["ticker", "date", "kind"] + RAW_COLUMNS + ["fetched_at"]
        placeholders = ", ".join("?" for _ in cols)
        values = [ticker, date_str, kind] + [raw.get(c) for c in RAW_COLUMNS] + [fetched_at]
        conn.execute(
            f"""
            INSERT INTO ratio_snapshots ({", ".join(cols)})
            VALUES ({placeholders})
            ON CONFLICT(ticker, date) DO UPDATE SET
                kind = excluded.kind,
                {", ".join(f"{c} = excluded.{c}" for c in RAW_COLUMNS)},
                fetched_at = excluded.fetched_at
            """,
            values,
        )
        conn.commit()
    finally:
        conn.close()


def get_kv(key):
    conn = get_connection()
    try:
        row = conn.execute("SELECT value FROM kv_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def set_kv(key, value):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO kv_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def replace_stock_map(rows, fetched_at):
    """상장 목록 전체를 교체한다. rows: [(code, name, market, name_norm), ...]"""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM stock_map")
        conn.executemany(
            "INSERT OR REPLACE INTO stock_map (code, name, market, name_norm) VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.execute(
            "INSERT INTO kv_meta (key, value) VALUES ('stock_map_fetched_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (fetched_at,),
        )
        conn.commit()
    finally:
        conn.close()


def stock_map_count():
    conn = get_connection()
    try:
        return conn.execute("SELECT COUNT(*) AS n FROM stock_map").fetchone()["n"]
    finally:
        conn.close()


def find_stock_by_name(name_norm):
    """정규화된 이름과 정확히 일치하는 종목."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT code, name, market FROM stock_map WHERE name_norm = ? ORDER BY market",
            (name_norm,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def find_stock_by_code(code):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT code, name, market FROM stock_map WHERE code = ?", (code,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def search_stock_by_name(name_norm, limit=8):
    """정규화된 이름을 부분 포함하는 후보(정확 일치 실패 시 안내용)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT code, name, market FROM stock_map WHERE name_norm LIKE ? ORDER BY LENGTH(name), name LIMIT ?",
            (f"%{name_norm}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def fetch_snapshots(ticker, since_date=None):
    conn = get_connection()
    try:
        if since_date is not None:
            rows = conn.execute(
                "SELECT * FROM ratio_snapshots WHERE ticker = ? AND date >= ? ORDER BY date",
                (ticker, since_date),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM ratio_snapshots WHERE ticker = ? ORDER BY date",
                (ticker,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def record_visit(day, visitor, now_iso):
    """(day, visitor) 한 건을 기록한다. 같은 사람이 그날 또 오면 hits만 올라간다."""
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO visits (day, visitor, hits, first_seen, last_seen)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(day, visitor) DO UPDATE SET
                hits = hits + 1,
                last_seen = excluded.last_seen
            """,
            (day, visitor, now_iso, now_iso),
        )
        conn.commit()
    finally:
        conn.close()


def visit_stats(day):
    """그날의 (순방문자 수, 총 방문 횟수)."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS visitors, COALESCE(SUM(hits), 0) AS hits FROM visits WHERE day = ?",
            (day,),
        ).fetchone()
        return row["visitors"], row["hits"]
    finally:
        conn.close()


init_db()
