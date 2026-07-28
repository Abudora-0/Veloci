"""SQLite-backed download queue: dedupe + resume across runs."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

QUEUED = "queued"
DOWNLOADING = "downloading"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
PAUSED = "paused"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    url TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'queued',
    dest_path TEXT,
    error TEXT,
    title TEXT,
    duration REAL,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Columns added after the initial release -- existing DBs get migrated in
# place on open rather than requiring a manual wipe.
_MIGRATED_COLUMNS = {
    "title": "TEXT",
    "duration": "REAL",
    "filesize": "INTEGER",
    "thumbnail": "TEXT",
}


@dataclass
class VideoRecord:
    url: str
    status: str
    dest_path: str | None
    error: str | None
    title: str | None = None
    duration: float | None = None
    filesize: int | None = None
    thumbnail: str | None = None


class QueueDB:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SCHEMA)
        self._migrate()
        self._reconcile_stale_downloads()
        self._conn.commit()

    def _reconcile_stale_downloads(self) -> None:
        # "downloading" only ever means "a yt-dlp subprocess is live right
        # now, tracked in Engine._active" -- that in-memory tracking can't
        # survive the engine process exiting, so any row still marked
        # downloading when a fresh QueueDB opens is necessarily a leftover
        # from a previous run that was killed (crash, forced restart) rather
        # than cleanly cancelled/paused. Left as "downloading" it shows as a
        # permanently stuck row with no real process behind it and no way to
        # ever update it. Paused is the honest state: resumable, not active.
        self._conn.execute(
            "UPDATE videos SET status = ? WHERE status = ?", (PAUSED, DOWNLOADING)
        )

    def _migrate(self) -> None:
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(videos)")}
        for column, sql_type in _MIGRATED_COLUMNS.items():
            if column not in existing:
                self._conn.execute(f"ALTER TABLE videos ADD COLUMN {column} {sql_type}")

    def close(self) -> None:
        self._conn.close()

    def enqueue(self, urls: list[str]) -> int:
        """Insert new urls as queued, skipping ones already known. Returns count added."""
        with self._lock() as conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO videos (url, status) VALUES (?, ?)",
                [(url, QUEUED) for url in urls],
            )
            return conn.total_changes - before

    def list_queued_urls(self) -> list[str]:
        with self._lock() as conn:
            rows = conn.execute(
                "SELECT url FROM videos WHERE status = ? ORDER BY added_at", (QUEUED,)
            ).fetchall()
            return [row["url"] for row in rows]

    def list_startable_urls(self) -> list[str]:
        """Queued or paused urls -- what "Download All"/"Start Queue" should pick
        up, since a bulk start is also how a paused video gets resumed in bulk."""
        with self._lock() as conn:
            rows = conn.execute(
                "SELECT url FROM videos WHERE status IN (?, ?) ORDER BY added_at", (QUEUED, PAUSED)
            ).fetchall()
            return [row["url"] for row in rows]

    def mark_downloading(self, url: str) -> None:
        with self._lock() as conn:
            conn.execute(
                "UPDATE videos SET status = ?, updated_at = datetime('now') WHERE url = ?",
                (DOWNLOADING, url),
            )

    def mark_done(self, url: str, dest_path: str) -> None:
        with self._lock() as conn:
            conn.execute(
                "UPDATE videos SET status = ?, dest_path = ?, error = NULL, "
                "updated_at = datetime('now') WHERE url = ?",
                (DONE, dest_path, url),
            )

    def mark_failed(self, url: str, error: str) -> None:
        with self._lock() as conn:
            conn.execute(
                "UPDATE videos SET status = ?, error = ?, updated_at = datetime('now') WHERE url = ?",
                (FAILED, error, url),
            )

    def mark_cancelled(self, url: str) -> None:
        with self._lock() as conn:
            conn.execute(
                "UPDATE videos SET status = ?, error = NULL, updated_at = datetime('now') WHERE url = ?",
                (CANCELLED, url),
            )

    def mark_paused(self, url: str) -> None:
        with self._lock() as conn:
            conn.execute(
                "UPDATE videos SET status = ?, error = NULL, updated_at = datetime('now') WHERE url = ?",
                (PAUSED, url),
            )

    def remove(self, url: str) -> None:
        with self._lock() as conn:
            conn.execute("DELETE FROM videos WHERE url = ?", (url,))

    def set_metadata(
        self,
        url: str,
        title: str | None,
        duration: float | None,
        filesize: int | None = None,
        thumbnail: str | None = None,
    ) -> None:
        with self._lock() as conn:
            conn.execute(
                "UPDATE videos SET title = ?, duration = ?, filesize = ?, thumbnail = ?, "
                "updated_at = datetime('now') WHERE url = ?",
                (title, duration, filesize, thumbnail, url),
            )

    def get_records(self, urls: list[str]) -> dict[str, VideoRecord]:
        """Current record for each url that exists (missing urls are omitted).
        Used to enrich scan results with what's already known -- e.g. a video
        downloaded last week shows up as done instead of re-queueing."""
        if not urls:
            return {}
        with self._lock() as conn:
            placeholders = ",".join("?" * len(urls))
            rows = conn.execute(
                f"SELECT * FROM videos WHERE url IN ({placeholders})", urls
            ).fetchall()
            return {row["url"]: _row_to_record(row) for row in rows}

    def counts_by_status(self) -> dict[str, int]:
        with self._lock() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM videos GROUP BY status").fetchall()
            return {row["status"]: row["n"] for row in rows}

    def list_all(self) -> list[VideoRecord]:
        with self._lock() as conn:
            rows = conn.execute("SELECT * FROM videos ORDER BY added_at").fetchall()
            return [_row_to_record(r) for r in rows]

    @contextmanager
    def _lock(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise


def _row_to_record(row: sqlite3.Row, *, status: str | None = None) -> VideoRecord:
    return VideoRecord(
        url=row["url"],
        status=status or row["status"],
        dest_path=row["dest_path"],
        error=row["error"],
        title=row["title"],
        duration=row["duration"],
        filesize=row["filesize"],
        thumbnail=row["thumbnail"],
    )
