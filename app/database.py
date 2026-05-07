import sqlite3
import json
from datetime import date
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "books.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS weekly_deals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                week_date TEXT NOT NULL,
                title TEXT NOT NULL,
                author TEXT,
                isbn TEXT,
                kobo_url TEXT,
                cover_url TEXT,
                description TEXT,
                original_price INTEGER,
                deal_price INTEGER DEFAULT 99,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS book_ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                score TEXT,
                review_count INTEGER,
                source_url TEXT,
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (book_id) REFERENCES weekly_deals(id)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_week_title
                ON weekly_deals(week_date, title);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_rating_source
                ON book_ratings(book_id, source);
        """)


def upsert_book(week_date: str, book: dict) -> int:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO weekly_deals
                (week_date, title, author, isbn, kobo_url, cover_url, description, original_price)
            VALUES
                (:week_date, :title, :author, :isbn, :kobo_url, :cover_url, :description, :original_price)
            ON CONFLICT(week_date, title) DO UPDATE SET
                author = excluded.author,
                isbn = excluded.isbn,
                kobo_url = excluded.kobo_url,
                cover_url = excluded.cover_url,
                description = excluded.description,
                original_price = excluded.original_price
        """, {**book, "week_date": week_date})
        row = conn.execute(
            "SELECT id FROM weekly_deals WHERE week_date=? AND title=?",
            (week_date, book["title"])
        ).fetchone()
        return row["id"]


def upsert_rating(book_id: int, source: str, score: str, review_count: int, source_url: str):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO book_ratings (book_id, source, score, review_count, source_url)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(book_id, source) DO UPDATE SET
                score = excluded.score,
                review_count = excluded.review_count,
                source_url = excluded.source_url,
                updated_at = datetime('now')
        """, (book_id, source, score, review_count, source_url))


def get_this_week_books():
    today = date.today()
    # Find the most recent Thursday on or before today
    days_since_thursday = (today.weekday() - 3) % 7
    thursday = today.replace(day=today.day - days_since_thursday)
    week_str = thursday.isoformat()

    with get_conn() as conn:
        books = conn.execute(
            "SELECT * FROM weekly_deals WHERE week_date=? ORDER BY id",
            (week_str,)
        ).fetchall()
        result = []
        for book in books:
            ratings = conn.execute(
                "SELECT * FROM book_ratings WHERE book_id=?",
                (book["id"],)
            ).fetchall()
            result.append({
                **dict(book),
                "ratings": [dict(r) for r in ratings],
            })
        return result, week_str


def get_all_weeks():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT week_date FROM weekly_deals ORDER BY week_date DESC"
        ).fetchall()
        return [r["week_date"] for r in rows]


def get_books_by_week(week_date: str):
    with get_conn() as conn:
        books = conn.execute(
            "SELECT * FROM weekly_deals WHERE week_date=? ORDER BY id",
            (week_date,)
        ).fetchall()
        result = []
        for book in books:
            ratings = conn.execute(
                "SELECT * FROM book_ratings WHERE book_id=?",
                (book["id"],)
            ).fetchall()
            result.append({
                **dict(book),
                "ratings": [dict(r) for r in ratings],
            })
        return result
