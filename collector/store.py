"""SQLite crawl state: every URL seen, its status, and where its file was saved.

Only the main thread touches the database, so one connection is enough.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS urls (
    url          TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,                    -- page | file
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | fetching | done | skipped | blocked | error
    depth        INTEGER NOT NULL DEFAULT 0,
    referrer     TEXT,
    http_status  INTEGER,
    content_type TEXT,
    final_url    TEXT,
    path         TEXT,     -- saved file, relative to the output dir
    sha256       TEXT,
    size         INTEGER,
    note         TEXT,     -- error message, skip reason, "duplicate of ..."
    attempts     INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_urls_queue ON urls(status, kind, depth);
CREATE INDEX IF NOT EXISTS idx_urls_sha ON urls(sha256);
"""

EXCLUDED = "excluded by config"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
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

    def reset_claims(self) -> int:
        # Only when a crawl starts: `status` running next to a live crawl must not hand its
        # in-flight URLs out again.
        cur = self.db.execute("UPDATE urls SET status='pending' WHERE status='fetching'")
        self.db.commit()
        return cur.rowcount

    def snapshot(self, path: str) -> None:
        """Consistent one-file copy of the state, safe to take while a crawl is running."""
        dst = sqlite3.connect(path)
        try:
            self.db.backup(dst)
        finally:
            dst.close()

    def all_urls(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT url FROM urls")}

    def add(self, url: str, kind: str, depth: int, referrer: str | None,
            status: str = "pending", note: str | None = None) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO urls (url, kind, status, depth, referrer, note, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (url, kind, status, depth, referrer, note, _now()),
        )
        return cur.rowcount == 1

    def claim(self, n: int, kind: str, skip_prefixes: list[str] = ()) -> list[sqlite3.Row]:
        # Breadth-first. skip_prefixes: servers given up on this run; their URLs stay pending.
        skip = "".join(" AND substr(url, 1, ?) != ?" for _ in skip_prefixes)
        args = [x for p in skip_prefixes for x in (len(p), p)]
        rows = self.db.execute(
            "SELECT url, kind, depth, attempts FROM urls WHERE status='pending' AND kind=?"
            f"{skip} ORDER BY depth LIMIT ?",
            (kind, *args, n),
        ).fetchall()
        self.db.executemany(
            "UPDATE urls SET status='fetching' WHERE url=?", [(r["url"],) for r in rows]
        )
        self.db.commit()
        return rows

    def release(self, url: str) -> None:
        self.db.execute("UPDATE urls SET status='pending' WHERE url=?", (url,))

    def finish(self, url: str, status: str, **fields) -> None:
        fields["status"] = status
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE urls SET {cols} WHERE url=?", (*fields.values(), url))

    def path_for_sha(self, sha256: str) -> str | None:
        row = self.db.execute(
            "SELECT path FROM urls WHERE sha256=? AND status='done' AND path IS NOT NULL"
            " AND (note IS NULL OR note NOT LIKE 'duplicate of%') LIMIT 1",
            (sha256,),
        ).fetchone()
        return row[0] if row else None

    def commit(self) -> None:
        self.db.commit()

    def requeue_pages(self) -> int:
        cur = self.db.execute(
            "UPDATE urls SET status='pending' WHERE kind='page' AND status='done'"
        )
        self.db.commit()
        return cur.rowcount

    def requeue_blocked(self) -> int:
        cur = self.db.execute("UPDATE urls SET status='pending' WHERE status='blocked'")
        self.db.commit()
        return cur.rowcount

    def requeue_transient_errors(self, max_attempts: int = 10) -> int:
        # Network failures (no HTTP status), overload responses, and 403s (which may have come
        # from a temporary block), but not 404s and the like.
        cur = self.db.execute(
            "UPDATE urls SET status='pending' WHERE status='error' AND attempts < ?"
            " AND (http_status IS NULL OR http_status IN (403, 408, 429, 500, 502, 503, 504))",
            (max_attempts,),
        )
        self.db.commit()
        return cur.rowcount

    def apply_scope(self, classify) -> tuple[int, int, int]:
        """Re-check queued URLs against the current config and rules, which may have changed
        since they were found: skip the ones now excluded, re-queue earlier exclusions now
        allowed, and move the ones now classified differently (page/file) to the right queue.

        classify(url) returns 'page', 'file' or None (excluded)."""
        dropped, moved = [], []
        for r in self.db.execute("SELECT url, kind FROM urls WHERE status='pending'"):
            kind = classify(r["url"])
            if kind is None:
                dropped.append((EXCLUDED, r["url"]))
            elif kind != r["kind"]:
                moved.append((kind, r["url"]))
        revived = [(kind, r["url"]) for r in self.db.execute(
                       "SELECT url FROM urls WHERE status='skipped' AND note=?", (EXCLUDED,))
                   if (kind := classify(r["url"]))]
        self.db.executemany("UPDATE urls SET status='skipped', note=? WHERE url=?", dropped)
        self.db.executemany("UPDATE urls SET kind=? WHERE url=?", moved)
        self.db.executemany("UPDATE urls SET status='pending', kind=?, note=NULL WHERE url=?", revived)
        self.db.commit()
        return len(dropped), len(revived), len(moved)

    def requeue_errors(self) -> int:
        cur = self.db.execute(
            "UPDATE urls SET status='pending', attempts=0 WHERE status='error'"
        )
        self.db.commit()
        return cur.rowcount

    def counts(self) -> dict[tuple[str, str], int]:
        return {
            (r["kind"], r["status"]): r["n"]
            for r in self.db.execute(
                "SELECT kind, status, COUNT(*) AS n FROM urls GROUP BY kind, status"
            )
        }

    def saved_bytes(self) -> int:
        row = self.db.execute(
            "SELECT COALESCE(SUM(size), 0) FROM urls WHERE kind='file' AND status='done'"
            " AND (note IS NULL OR note NOT LIKE 'duplicate of%')"
        ).fetchone()
        return row[0]

    def files(self):
        return self.db.execute(
            "SELECT url, final_url, path, size, sha256, content_type, referrer, note, updated_at"
            " FROM urls WHERE kind='file' AND status='done' ORDER BY path"
        )

    def errors(self, limit: int = 20):
        return self.db.execute(
            "SELECT url, kind, http_status, note FROM urls WHERE status='error'"
            " ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
