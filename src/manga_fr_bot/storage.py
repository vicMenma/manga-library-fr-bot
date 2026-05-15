from __future__ import annotations

import sqlite3
from pathlib import Path

from manga_fr_bot.models import ProgressEntry


class LibraryStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS favorites (
                    user_id INTEGER NOT NULL,
                    manga_id TEXT NOT NULL,
                    manga_title TEXT NOT NULL,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, manga_id)
                );

                CREATE TABLE IF NOT EXISTS progress (
                    user_id INTEGER NOT NULL,
                    manga_id TEXT NOT NULL,
                    manga_title TEXT NOT NULL,
                    chapter_id TEXT NOT NULL,
                    chapter_label TEXT NOT NULL,
                    page_index INTEGER NOT NULL DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, manga_id)
                );

                CREATE TABLE IF NOT EXISTS updates_state (
                    user_id INTEGER NOT NULL,
                    manga_id TEXT NOT NULL,
                    manga_title TEXT NOT NULL,
                    last_seen_chapter_id TEXT NOT NULL,
                    last_seen_chapter_label TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, manga_id)
                );
                """
            )

    def toggle_favorite(self, user_id: int, manga_id: str, manga_title: str) -> bool:
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM favorites WHERE user_id = ? AND manga_id = ?",
                (user_id, manga_id),
            ).fetchone()
            if existing:
                conn.execute(
                    "DELETE FROM favorites WHERE user_id = ? AND manga_id = ?",
                    (user_id, manga_id),
                )
                conn.execute(
                    "DELETE FROM updates_state WHERE user_id = ? AND manga_id = ?",
                    (user_id, manga_id),
                )
                return False
            conn.execute(
                """
                INSERT INTO favorites (user_id, manga_id, manga_title)
                VALUES (?, ?, ?)
                """,
                (user_id, manga_id, manga_title),
            )
            return True

    def is_favorite(self, user_id: int, manga_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM favorites WHERE user_id = ? AND manga_id = ?",
                (user_id, manga_id),
            ).fetchone()
        return row is not None

    def list_favorites(self, user_id: int, limit: int = 12) -> list[tuple[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT manga_id, manga_title
                FROM favorites
                WHERE user_id = ?
                ORDER BY added_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [(row["manga_id"], row["manga_title"]) for row in rows]

    def save_progress(
        self,
        user_id: int,
        manga_id: str,
        manga_title: str,
        chapter_id: str,
        chapter_label: str,
        page_index: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO progress (
                    user_id, manga_id, manga_title, chapter_id, chapter_label, page_index, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, manga_id) DO UPDATE SET
                    manga_title = excluded.manga_title,
                    chapter_id = excluded.chapter_id,
                    chapter_label = excluded.chapter_label,
                    page_index = excluded.page_index,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, manga_id, manga_title, chapter_id, chapter_label, page_index),
            )

    def get_progress(self, user_id: int, manga_id: str) -> ProgressEntry | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT manga_id, manga_title, chapter_id, chapter_label, page_index
                FROM progress
                WHERE user_id = ? AND manga_id = ?
                """,
                (user_id, manga_id),
            ).fetchone()
        if row is None:
            return None
        return ProgressEntry(
            manga_id=row["manga_id"],
            manga_title=row["manga_title"],
            chapter_id=row["chapter_id"],
            chapter_label=row["chapter_label"],
            page_index=row["page_index"],
        )

    def list_recent_progress(self, user_id: int, limit: int = 8) -> list[ProgressEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT manga_id, manga_title, chapter_id, chapter_label, page_index
                FROM progress
                WHERE user_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [
            ProgressEntry(
                manga_id=row["manga_id"],
                manga_title=row["manga_title"],
                chapter_id=row["chapter_id"],
                chapter_label=row["chapter_label"],
                page_index=row["page_index"],
            )
            for row in rows
        ]

    def clear_progress(self, user_id: int, manga_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM progress WHERE user_id = ? AND manga_id = ?",
                (user_id, manga_id),
            )

    def mark_seen_chapter(
        self,
        user_id: int,
        manga_id: str,
        manga_title: str,
        chapter_id: str,
        chapter_label: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO updates_state (
                    user_id, manga_id, manga_title, last_seen_chapter_id, last_seen_chapter_label, updated_at
                )
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, manga_id) DO UPDATE SET
                    manga_title = excluded.manga_title,
                    last_seen_chapter_id = excluded.last_seen_chapter_id,
                    last_seen_chapter_label = excluded.last_seen_chapter_label,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, manga_id, manga_title, chapter_id, chapter_label),
            )

    def get_seen_chapter(self, user_id: int, manga_id: str) -> tuple[str, str] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT last_seen_chapter_id, last_seen_chapter_label
                FROM updates_state
                WHERE user_id = ? AND manga_id = ?
                """,
                (user_id, manga_id),
            ).fetchone()
        if row is None:
            return None
        return row["last_seen_chapter_id"], row["last_seen_chapter_label"]
