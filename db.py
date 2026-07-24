"""SQLite 캐시 레이어. 원본 재무 스냅샷(raw)만 저장하고, 비율(PER 등)은 매번 재계산한다."""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "cache.sqlite3"

RAW_COLUMNS = [
    "price", "revenue", "net_income", "equity", "shares",
    "ebitda", "fcf", "market_cap", "ev", "div_ttm",
]


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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ticker_meta (
                ticker TEXT PRIMARY KEY,
                annual_fetched_at TEXT,
                ttm_fetched_at TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()


def get_meta(ticker):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT annual_fetched_at, ttm_fetched_at FROM ticker_meta WHERE ticker = ?",
            (ticker,),
        ).fetchone()
        return dict(row) if row else {"annual_fetched_at": None, "ttm_fetched_at": None}
    finally:
        conn.close()


def touch_meta(ticker, annual_fetched_at=None, ttm_fetched_at=None):
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT annual_fetched_at, ttm_fetched_at FROM ticker_meta WHERE ticker = ?",
            (ticker,),
        ).fetchone()
        new_annual = annual_fetched_at if annual_fetched_at is not None else (existing["annual_fetched_at"] if existing else None)
        new_ttm = ttm_fetched_at if ttm_fetched_at is not None else (existing["ttm_fetched_at"] if existing else None)
        conn.execute(
            """
            INSERT INTO ticker_meta (ticker, annual_fetched_at, ttm_fetched_at)
            VALUES (?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                annual_fetched_at = excluded.annual_fetched_at,
                ttm_fetched_at = excluded.ttm_fetched_at
            """,
            (ticker, new_annual, new_ttm),
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


init_db()
