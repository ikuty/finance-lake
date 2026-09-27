"""SQLiteスキーマとアクセスヘルパー。edinet-dlのfetch_progressと同じ
「日付単位で取得済み状態を記録し、電源障害等で欠けた日は自動的に再試行される」
という設計を踏襲する。
"""
from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS companies (
            edinet_code TEXT PRIMARY KEY,
            sec_code TEXT NOT NULL,
            jpx_code TEXT NOT NULL,
            filer_name TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_jpx_code ON companies(jpx_code)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tdnet_fetch_progress (
            event_date TEXT PRIMARY KEY,
            status TEXT,
            event_count INTEGER,
            message TEXT,
            fetched_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tdnet_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_date TEXT NOT NULL,
            kj_time TEXT,
            tdnet_code TEXT NOT NULL,
            edinet_code TEXT NOT NULL REFERENCES companies(edinet_code),
            company_name TEXT NOT NULL,
            title TEXT NOT NULL,
            disclosure_kind TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            UNIQUE (event_date, tdnet_code, kj_time, title)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_tdnet_events_edinet_code ON tdnet_events(edinet_code)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pdf_downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tdnet_event_id INTEGER NOT NULL REFERENCES tdnet_events(id),
            jpx_disclosure_date TEXT,
            jpx_title TEXT,
            pdf_url TEXT NOT NULL,
            status TEXT NOT NULL,
            local_path TEXT,
            message TEXT,
            downloaded_at TEXT NOT NULL,
            UNIQUE (tdnet_event_id, pdf_url)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pdf_downloads_status ON pdf_downloads(status)
    """)
    conn.commit()
    return conn


def upsert_company(conn: sqlite3.Connection, edinet_code: str, sec_code: str, jpx_code: str, filer_name: str) -> None:
    conn.execute(
        "INSERT INTO companies (edinet_code, sec_code, jpx_code, filer_name) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(edinet_code) DO UPDATE SET sec_code = excluded.sec_code, jpx_code = excluded.jpx_code, "
        "filer_name = excluded.filer_name",
        (edinet_code, sec_code, jpx_code, filer_name),
    )


def company_by_jpx_code(conn: sqlite3.Connection, jpx_code: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM companies WHERE jpx_code = ?", (jpx_code,)
    ).fetchone()
    return row


def already_done_tdnet(conn: sqlite3.Connection, event_date: str) -> bool:
    row = conn.execute(
        "SELECT status FROM tdnet_fetch_progress WHERE event_date = ?", (event_date,)
    ).fetchone()
    return row is not None and row[0] == "done"


def store_tdnet_progress(
    conn: sqlite3.Connection, event_date: str, status: str, event_count: int, message: str | None
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO tdnet_fetch_progress (event_date, status, event_count, message, fetched_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (event_date, status, event_count, message, datetime.datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()


def insert_tdnet_event(
    conn: sqlite3.Connection,
    event_date: str,
    kj_time: str,
    tdnet_code: str,
    edinet_code: str,
    company_name: str,
    title: str,
    disclosure_kind: str,
) -> int | None:
    """新規イベントのみ挿入しidを返す。既存(UNIQUE制約に抵触)ならNoneを返す。"""
    cur = conn.execute(
        "INSERT OR IGNORE INTO tdnet_events "
        "(event_date, kj_time, tdnet_code, edinet_code, company_name, title, disclosure_kind, detected_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_date, kj_time, tdnet_code, edinet_code, company_name, title, disclosure_kind,
            datetime.datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    return cur.lastrowid if cur.rowcount > 0 else None


def pending_tdnet_events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """まだpdf_downloadsに1件も紐づいていないtdnet_eventsを返す（未処理分）。"""
    conn.row_factory = sqlite3.Row
    return list(conn.execute("""
        SELECT e.* FROM tdnet_events e
        WHERE NOT EXISTS (SELECT 1 FROM pdf_downloads d WHERE d.tdnet_event_id = e.id)
        ORDER BY e.id
    """).fetchall())


def record_pdf_download(
    conn: sqlite3.Connection,
    tdnet_event_id: int,
    jpx_disclosure_date: str | None,
    jpx_title: str | None,
    pdf_url: str,
    status: str,
    local_path: str | None,
    message: str | None,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO pdf_downloads "
        "(tdnet_event_id, jpx_disclosure_date, jpx_title, pdf_url, status, local_path, message, downloaded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tdnet_event_id, jpx_disclosure_date, jpx_title, pdf_url, status, local_path, message,
            datetime.datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
