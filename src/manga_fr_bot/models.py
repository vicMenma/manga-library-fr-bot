from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class MangaSummary:
    id: str
    title: str
    description: str
    status: str
    year: int | None
    tags: list[str]
    cover_url: str | None


@dataclass(slots=True)
class ChapterSummary:
    id: str
    title: str
    chapter: str
    pages: int
    scanlation_group: str | None


@dataclass(slots=True)
class ChapterPages:
    chapter_id: str
    manga_id: str
    title: str
    chapter: str
    page_urls: list[str]


@dataclass(slots=True)
class ProgressEntry:
    manga_id: str
    manga_title: str
    chapter_id: str
    chapter_label: str
    page_index: int
