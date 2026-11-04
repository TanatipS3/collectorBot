"""SQLite OCR state: every document, every page, how it was read and the text.

Only the main thread touches the database. Each page is committed as it finishes, so a run
that is stopped or killed loses at most the pages in flight.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    doc_id     TEXT PRIMARY KEY,  -- first 16 hex digits of the file's SHA-256
    path       TEXT NOT NULL,     -- relative to the downloads dir, as the collector saved it
    sha256     TEXT,
    size       INTEGER,
    status     TEXT NOT NULL DEFAULT 'new',  -- new | inspected | done | skipped | error
    pages      INTEGER,
    ocr_pages  INTEGER,           -- pages that need OCR
    attempts   INTEGER NOT NULL DEFAULT 0,  -- inspections that crashed the PDF reader
    note       TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS pages (
    doc_id     TEXT NOT NULL,
    page       INTEGER NOT NULL,
    method     TEXT NOT NULL,     -- text (the PDF's own text layer) | ocr
    reason     TEXT,              -- why it needs OCR: scanned page, unreadable text layer, ...
    status     TEXT NOT NULL,     -- pending | running | done | failed
    prio       INTEGER NOT NULL DEFAULT 1,  -- 0: one of its document's first pages
    doc_rank   INTEGER NOT NULL DEFAULT 0,  -- its document's OCR page count: small ones first
    text       TEXT,
    engine     TEXT,              -- text-layer | primary | backup
    model      TEXT,
    prompt_version TEXT,
    suspect    TEXT,              -- comma-separated reasons to look at the page
    notes      TEXT,
    attempts   INTEGER NOT NULL DEFAULT 0,
    error      TEXT,
    seconds    REAL,
    tokens_out INTEGER,
    updated_at TEXT,
    PRIMARY KEY (doc_id, page)
);
CREATE INDEX IF NOT EXISTS idx_pages_queue ON pages(status, prio, doc_rank, doc_id, page);
CREATE INDEX IF NOT EXISTS idx_pages_time ON pages(updated_at);
"""

# The first pages of every document are read before the rest: they say what a document is,
# which is what the categorization step needs, so it can start long before OCR finishes.
FIRST_PAGES = 3

# Pages read from the PDF's own text layer; blank pages (no text, no ink) are not among them.
TEXT_LAYER = "method='text' AND status='done' AND COALESCE(reason, '') != 'blank'"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class OcrStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # --- documents -------------------------------------------------------------------------

    def add_docs(self, rows: list[tuple]) -> int:
        """rows: (doc_id, path, sha256, size, status, note). Known documents are left alone."""
        cur = self.db.executemany(
            "INSERT OR IGNORE INTO docs (doc_id, path, sha256, size, status, note, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)", [(*r, _now()) for r in rows])
        self.db.commit()
        return cur.rowcount

    def docs_to_inspect(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT doc_id, path, attempts FROM docs WHERE status='new' ORDER BY size").fetchall()

    def count_crash(self, doc_id: str) -> int:
        self.db.execute("UPDATE docs SET attempts=attempts+1 WHERE doc_id=?", (doc_id,))
        self.db.commit()
        return self.doc(doc_id)["attempts"]

    def save_inspection(self, doc_id: str, pages: list[dict]) -> int:
        """Store the pages; returns how many need OCR."""
        ocr = [p for p in pages if p["method"] == "ocr"]
        now = _now()
        self.db.executemany(
            "INSERT OR REPLACE INTO pages (doc_id, page, method, reason, status, prio, doc_rank,"
            " text, engine, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(doc_id, p["page"], p["method"], p["reason"],
              "pending" if p["method"] == "ocr" else "done",
              0 if p["page"] <= FIRST_PAGES else 1, len(ocr),
              p["text"] if p["method"] == "text" else None,
              "text-layer" if p["method"] == "text" else None, now) for p in pages])
        self.db.execute(
            "UPDATE docs SET status=?, pages=?, ocr_pages=?, note=NULL, updated_at=? WHERE doc_id=?",
            ("inspected" if ocr else "done", len(pages), len(ocr), now, doc_id))
        self.db.commit()
        return len(ocr)

    def mark_doc(self, doc_id: str, status: str, note: str | None = None) -> None:
        self.db.execute("UPDATE docs SET status=?, note=?, updated_at=? WHERE doc_id=?",
                        (status, note, _now(), doc_id))
        self.db.commit()

    def doc(self, doc_id: str) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM docs WHERE doc_id=?", (doc_id,)).fetchone()

    def doc_pages(self, doc_id: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM pages WHERE doc_id=? ORDER BY page", (doc_id,)).fetchall()

    def first_pages_text(self, doc_id: str, n: int) -> list[str]:
        """The text of the document's first n pages that have been read, in order."""
        return [r[0] for r in self.db.execute(
            "SELECT text FROM pages WHERE doc_id=? AND page<=? AND status='done' ORDER BY page",
            (doc_id, n))]

    def finish_doc_if_complete(self, doc_id: str) -> bool:
        """Mark the document done once no page is waiting; True if it is complete."""
        open_pages = self.db.execute(
            "SELECT COUNT(*) FROM pages WHERE doc_id=? AND status IN ('pending', 'running')",
            (doc_id,)).fetchone()[0]
        if open_pages:
            return False
        self.mark_doc(doc_id, "done")
        return True

    def all_docs(self) -> dict[str, sqlite3.Row]:
        """path -> document, with its page counts."""
        rows = self.db.execute("""
            SELECT d.*,
                   SUM(p.method='text') AS pages_text,
                   SUM(p.method='ocr' AND p.status='done') AS pages_ocr,
                   SUM(p.status='failed') AS pages_failed,
                   SUM(p.suspect IS NOT NULL AND p.suspect != '') AS pages_suspect
            FROM docs d LEFT JOIN pages p ON p.doc_id = d.doc_id
            GROUP BY d.doc_id""")
        return {r["path"]: r for r in rows}

    def done_docs(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT doc_id FROM docs WHERE status='done'")]

    # --- the OCR queue ---------------------------------------------------------------------

    def reset_claims(self) -> int:
        # Pages a stopped run was still reading: not their fault, so the try is not counted.
        cur = self.db.execute("UPDATE pages SET status='pending', attempts=MAX(attempts-1, 0)"
                              " WHERE status='running'")
        self.db.commit()
        return cur.rowcount

    def requeue_failed(self, max_attempts: int) -> int:
        cur = self.db.execute(
            "UPDATE pages SET status='pending' WHERE status='failed' AND attempts < ?", (max_attempts,))
        self.db.execute(
            "UPDATE docs SET status='inspected' WHERE status='done' AND doc_id IN"
            " (SELECT doc_id FROM pages WHERE status='pending')")
        self.db.commit()
        return cur.rowcount

    def claim(self, n: int) -> list[sqlite3.Row]:
        rows = self.db.execute(
            "SELECT p.doc_id, p.page, p.attempts, d.path FROM pages p JOIN docs d USING (doc_id)"
            " WHERE p.status='pending' ORDER BY p.prio, p.doc_rank, p.doc_id, p.page LIMIT ?",
            (n,)).fetchall()
        self.db.executemany(
            "UPDATE pages SET status='running', attempts=attempts+1 WHERE doc_id=? AND page=?",
            [(r["doc_id"], r["page"]) for r in rows])
        self.db.commit()
        return rows

    def release(self, doc_id: str, page: int, counted: bool = False) -> None:
        """Back to the queue. Unless counted, the try is undone: the failure was not the page's."""
        self.db.execute("UPDATE pages SET status='pending', attempts=attempts-? WHERE doc_id=? AND page=?",
                        (0 if counted else 1, doc_id, page))
        self.db.commit()

    def pending_pages(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM pages WHERE status='pending'").fetchone()[0]

    def finish_page(self, doc_id: str, page: int, **fields) -> None:
        self.set_pages([(doc_id, page, fields)])

    def set_pages(self, updates: list[tuple[str, int, dict]]) -> None:
        """(doc_id, page, {column: value}) for each page, in one commit. A page set to pending
        reopens its document."""
        now = _now()
        for doc_id, page, fields in updates:
            fields = {**fields, "updated_at": now}
            cols = ", ".join(f"{k}=?" for k in fields)
            self.db.execute(f"UPDATE pages SET {cols} WHERE doc_id=? AND page=?",
                            (*fields.values(), doc_id, page))
            if fields.get("status") == "pending":
                self.db.execute("UPDATE docs SET status='inspected' WHERE doc_id=? AND status='done'",
                                (doc_id,))
        self.db.commit()

    # --- another run's state, the text-layer pages read again by OCR -----------------------

    def copy_from(self, source: Path) -> None:
        """Start from another run's state: every document and page as it stands there."""
        src = sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            src.backup(self.db)
        finally:
            src.close()

    def queue_text_layer(self, reason: str) -> int:
        """Every page taken from the PDF's text layer (blank pages aside) goes in the OCR queue;
        its documents are open again, the small ones first. Returns how many pages."""
        now = _now()
        cur = self.db.execute(
            "UPDATE pages SET method='ocr', reason=?, status='pending', text=NULL, engine=NULL, attempts=0,"
            f" updated_at=? WHERE {TEXT_LAYER}", (reason, now))
        self.db.execute(
            "UPDATE docs SET status='inspected', updated_at=?, ocr_pages=(SELECT COUNT(*) FROM pages p"
            " WHERE p.doc_id=docs.doc_id AND p.method='ocr') WHERE doc_id IN"
            " (SELECT doc_id FROM pages WHERE status='pending')", (now,))
        self.db.execute("UPDATE pages SET doc_rank=(SELECT ocr_pages FROM docs d WHERE d.doc_id=pages.doc_id)"
                        " WHERE status='pending'")
        self.db.commit()
        return cur.rowcount

    def has_open_pages(self, doc_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM pages WHERE doc_id=? AND status IN ('pending', 'running')"
                               " LIMIT 1", (doc_id,)).fetchone() is not None

    # --- progress --------------------------------------------------------------------------

    def counts(self) -> dict:
        docs = {r[0]: r[1] for r in self.db.execute("SELECT status, COUNT(*) FROM docs GROUP BY status")}
        pages = {(r[0], r[1]): r[2] for r in self.db.execute(
            "SELECT method, status, COUNT(*) FROM pages GROUP BY method, status")}
        engines = {r[0]: r[1] for r in self.db.execute(
            "SELECT engine, COUNT(*) FROM pages WHERE method='ocr' AND status='done' GROUP BY engine")}
        suspect = self.db.execute(
            "SELECT COUNT(*) FROM pages WHERE suspect IS NOT NULL AND suspect != ''").fetchone()[0]
        first = self.db.execute(
            "SELECT COUNT(*) FROM pages WHERE method='ocr' AND prio=0 AND status IN ('pending', 'running')"
        ).fetchone()[0]
        return {"docs": docs, "pages": pages, "engines": engines, "suspect": suspect,
                "first_pages_left": first}

    def ocr_rate(self, minutes: int = 10) -> float:
        """OCR pages finished per minute, over the last `minutes`."""
        since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
        n = self.db.execute(
            "SELECT COUNT(*) FROM pages WHERE method='ocr' AND status='done' AND updated_at >= ?",
            (since,)).fetchone()[0]
        return n / minutes

    def recent_failures(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT p.doc_id, p.page, p.error, d.path FROM pages p JOIN docs d USING (doc_id)"
            " WHERE p.status='failed' ORDER BY p.updated_at DESC LIMIT ?", (limit,)).fetchall()
