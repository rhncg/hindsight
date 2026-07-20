"""
db.py — local SQLite index for captured screenshots.

One table holds the source-of-truth rows (screenshot metadata + what
Gemma extracted); an FTS5 virtual table mirrors the searchable text
columns and stays in sync via triggers, so callers never write to the
search index directly.
"""

import json
import sqlite3
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path("data/hindsight.db")

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS screenshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at  TEXT NOT NULL,          -- ISO 8601, when the screenshot was taken
    analyzed_at  TEXT,                   -- ISO 8601, when Gemma finished analyzing it
    image_path   TEXT NOT NULL,
    image_hash   TEXT,                   -- perceptual hash, useful for debugging dedup
    app_name     TEXT,                   -- active window/app, nullable
    raw_text     TEXT,                   -- literal text Gemma extracted (numbers, IDs, names)
    summary      TEXT,                   -- Gemma's one-line description of the screen
    entities     TEXT,                   -- optional JSON blob of structured extras
    created_at   TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_screenshots_captured_at ON screenshots(captured_at);

CREATE VIRTUAL TABLE IF NOT EXISTS screenshots_fts USING fts5(
    raw_text,
    summary,
    app_name,
    content='screenshots',
    content_rowid='id',
    tokenize='porter unicode61'
);

-- Keep the FTS index in sync with the source table automatically.
CREATE TRIGGER IF NOT EXISTS screenshots_ai AFTER INSERT ON screenshots BEGIN
    INSERT INTO screenshots_fts(rowid, raw_text, summary, app_name)
    VALUES (new.id, new.raw_text, new.summary, new.app_name);
END;

CREATE TRIGGER IF NOT EXISTS screenshots_ad AFTER DELETE ON screenshots BEGIN
    INSERT INTO screenshots_fts(screenshots_fts, rowid, raw_text, summary, app_name)
    VALUES ('delete', old.id, old.raw_text, old.summary, old.app_name);
END;

CREATE TRIGGER IF NOT EXISTS screenshots_au AFTER UPDATE ON screenshots BEGIN
    INSERT INTO screenshots_fts(screenshots_fts, rowid, raw_text, summary, app_name)
    VALUES ('delete', old.id, old.raw_text, old.summary, old.app_name);
    INSERT INTO screenshots_fts(rowid, raw_text, summary, app_name)
    VALUES (new.id, new.raw_text, new.summary, new.app_name);
END;

-- Chat history: each conversation owns an ordered list of messages. These are
-- independent of the screenshot index above — clearing screenshot history does
-- not touch conversations, and vice versa.
CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL DEFAULT 'New chat',
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id  INTEGER NOT NULL,
    role             TEXT NOT NULL,          -- 'user' or 'assistant'
    content          TEXT NOT NULL,
    screenshots      TEXT,                   -- JSON list of related screenshots
    elapsed_s        REAL,                   -- seconds spent producing an answer
    created_at       TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id);
"""


def init_db(path: Path = DB_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        cur = conn.execute("SELECT sql FROM sqlite_master WHERE name='screenshots_fts'")
        row = cur.fetchone()

        needs_migration = False
        if row:
            sql = row[0]
            if "porter" not in sql:
                needs_migration = True

        if needs_migration:
            conn.executescript("""
                DROP TRIGGER IF EXISTS screenshots_ai;
                DROP TRIGGER IF EXISTS screenshots_ad;
                DROP TRIGGER IF EXISTS screenshots_au;
                DROP TABLE IF EXISTS screenshots_fts;
                CREATE VIRTUAL TABLE screenshots_fts USING fts5(
                    raw_text,
                    summary,
                    app_name,
                    content='screenshots',
                    content_rowid='id',
                    tokenize='porter unicode61'
                );
                INSERT INTO screenshots_fts(rowid, raw_text, summary, app_name)
                SELECT id, raw_text, summary, app_name FROM screenshots;
            """)

        conn.executescript(SCHEMA)

        msg_cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
        if "elapsed_s" not in msg_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN elapsed_s REAL")


@contextmanager
def get_conn(path: Path = DB_PATH):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def insert_screenshot(
    captured_at: str,
    image_path: str,
    raw_text: str,
    summary: str,
    analyzed_at: str | None = None,
    app_name: str | None = None,
    image_hash: str | None = None,
    entities: dict | None = None,
    path: Path = DB_PATH,
) -> int:
    """Called once per screenshot, after Gemma has analyzed it."""
    with get_conn(path) as conn:
        cur = conn.execute(
            """
            INSERT INTO screenshots
                (captured_at, analyzed_at, image_path, image_hash, app_name,
                 raw_text, summary, entities)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                captured_at,
                analyzed_at,
                image_path,
                image_hash,
                app_name,
                raw_text,
                summary,
                json.dumps(entities) if entities is not None else None,
            ),
        )
        conn.commit()
        row_id = cur.lastrowid
        assert row_id is not None
        return row_id


def search_text(
    query: str,
    since: str | None = None,
    until: str | None = None,
    limit: int = 10,
    path: Path = DB_PATH,
) -> list[sqlite3.Row]:
    """
    Full-text search across raw_text, summary, and app_name, ranked by
    relevance (bm25), optionally narrowed to a time range.
    """
    sql = """
        SELECT s.*, bm25(screenshots_fts) AS rank
        FROM screenshots_fts
        JOIN screenshots s ON s.id = screenshots_fts.rowid
        WHERE screenshots_fts MATCH ?
    """
    params: list = [query]

    if since:
        sql += " AND s.captured_at >= ?"
        params.append(since)
    if until:
        sql += " AND s.captured_at <= ?"
        params.append(until)

    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)

    with get_conn(path) as conn:
        return conn.execute(sql, params).fetchall()


def clear_all(path: Path = DB_PATH) -> list[str]:
    """
    Delete every screenshot row, returning the image paths that were
    referenced so the caller can remove the files from disk. The FTS index
    stays in sync automatically via the AFTER DELETE trigger.
    """
    with get_conn(path) as conn:
        paths = [
            row["image_path"]
            for row in conn.execute("SELECT image_path FROM screenshots")
            if row["image_path"]
        ]
        conn.execute("DELETE FROM screenshots")
        seq_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
        ).fetchone()
        if seq_exists:
            conn.execute("DELETE FROM sqlite_sequence WHERE name = 'screenshots'")
        conn.commit()
    return paths


def get_recent(limit: int = 20, path: Path = DB_PATH) -> list[sqlite3.Row]:
    with get_conn(path) as conn:
        return conn.execute(
            "SELECT * FROM screenshots ORDER BY captured_at DESC LIMIT ?", (limit,)
        ).fetchall()


def get_by_time_range(
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
    path: Path = DB_PATH,
) -> list[sqlite3.Row]:
    """Chronological browse within a time window, no keyword match required."""
    sql = "SELECT * FROM screenshots WHERE 1=1"
    params: list = []
    if since:
        sql += " AND captured_at >= ?"
        params.append(since)
    if until:
        sql += " AND captured_at <= ?"
        params.append(until)
    sql += " ORDER BY captured_at DESC LIMIT ?"
    params.append(limit)

    with get_conn(path) as conn:
        return conn.execute(sql, params).fetchall()


def create_conversation(title: str = "New chat", path: Path = DB_PATH) -> int:
    with get_conn(path) as conn:
        cur = conn.execute("INSERT INTO conversations (title) VALUES (?)", (title,))
        conn.commit()
        row_id = cur.lastrowid
        assert row_id is not None
        return row_id


def list_conversations(path: Path = DB_PATH) -> list[sqlite3.Row]:
    """Most-recently-active conversations first."""
    with get_conn(path) as conn:
        return conn.execute(
            "SELECT * FROM conversations ORDER BY updated_at DESC, id DESC"
        ).fetchall()


def get_conversation_messages(
    conversation_id: int, path: Path = DB_PATH
) -> list[sqlite3.Row]:
    with get_conn(path) as conn:
        return conn.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()


def add_message(
    conversation_id: int,
    role: str,
    content: str,
    screenshots: list | None = None,
    elapsed_s: float | None = None,
    path: Path = DB_PATH,
) -> int:
    """Append a message and bump the conversation's updated_at so it sorts
    to the top of the list."""
    with get_conn(path) as conn:
        cur = conn.execute(
            """
            INSERT INTO messages (conversation_id, role, content, screenshots, elapsed_s)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                role,
                content,
                json.dumps(screenshots) if screenshots else None,
                elapsed_s,
            ),
        )
        conn.execute(
            "UPDATE conversations SET updated_at = datetime('now') WHERE id = ?",
            (conversation_id,),
        )
        conn.commit()
        row_id = cur.lastrowid
        assert row_id is not None
        return row_id


def rename_conversation(conversation_id: int, title: str, path: Path = DB_PATH) -> None:
    with get_conn(path) as conn:
        conn.execute(
            "UPDATE conversations SET title = ? WHERE id = ?", (title, conversation_id)
        )
        conn.commit()


def get_conversation_overview(path: Path = DB_PATH) -> list[sqlite3.Row]:
    """Conversations with their message counts, for the data browser."""
    with get_conn(path) as conn:
        return conn.execute(
            """
            SELECT c.id, c.title, c.created_at, c.updated_at,
                   COUNT(m.id) AS message_count
            FROM conversations c
            LEFT JOIN messages m ON m.conversation_id = c.id
            GROUP BY c.id
            ORDER BY c.updated_at DESC, c.id DESC
            """
        ).fetchall()


def delete_conversation(conversation_id: int, path: Path = DB_PATH) -> None:
    with get_conn(path) as conn:
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        conn.commit()


if __name__ == "__main__":
    init_db()
    new_id = insert_screenshot(
        captured_at="2026-07-18T14:32:01",
        analyzed_at="2026-07-18T14:32:29",
        image_path="data/screenshots/2026-07-18_14-32-01.png",
        image_hash="a1b2c3d4",
        app_name="Gmail",
        raw_text="Invoice #4521 due July 25 from Acme Supplies",
        summary="Gmail inbox showing an invoice email from Acme Supplies",
        entities={"amount_due_by": "2026-07-25", "sender": "Acme Supplies"},
    )
    print("inserted row id:", new_id)

    results = search_text("invoice")
    print(f"found {len(results)} result(s)")
    for r in results:
        print(dict(r))