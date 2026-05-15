from __future__ import annotations

from typing import Any

import httpx

from manga_fr_bot.models import ChapterPages, ChapterSummary, MangaSummary


class MangaDexClient:
    def __init__(
        self,
        api_base: str,
        uploads_base: str,
        language: str = "fr",
        data_saver: bool = False,
        timeout: float = 20.0,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.uploads_base = uploads_base.rstrip("/")
        self.language = language
        self.data_saver = data_saver
        self.client = httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "MangaLibraryFR/0.1"})

    async def close(self) -> None:
        await self.client.aclose()

    async def _get_json(self, path: str, params: list[tuple[str, str]] | None = None) -> dict[str, Any]:
        response = await self.client.get(f"{self.api_base}{path}", params=params)
        response.raise_for_status()
        return response.json()

    def _pick_text(self, mapping: dict[str, str] | None, fallback: str = "") -> str:
        if not mapping:
            return fallback
        return mapping.get(self.language) or mapping.get("en") or next(iter(mapping.values()), fallback)

    def _cover_from_relationships(self, manga_id: str, relationships: list[dict[str, Any]]) -> str | None:
        for rel in relationships:
            if rel.get("type") != "cover_art":
                continue
            file_name = (rel.get("attributes") or {}).get("fileName")
            if file_name:
                return f"{self.uploads_base}/covers/{manga_id}/{file_name}.512.jpg"
        return None

    def _scanlation_name(self, relationships: list[dict[str, Any]]) -> str | None:
        for rel in relationships:
            if rel.get("type") == "scanlation_group":
                return (rel.get("attributes") or {}).get("name")
        return None

    def _tags(self, attributes: dict[str, Any]) -> list[str]:
        tags = []
        for tag in attributes.get("tags") or []:
            name = self._pick_text((tag.get("attributes") or {}).get("name"), "")
            if name:
                tags.append(name)
        return tags

    async def search_manga(self, query: str, limit: int = 8) -> list[MangaSummary]:
        params = [
            ("title", query),
            ("limit", str(limit)),
            ("availableTranslatedLanguage[]", self.language),
            ("contentRating[]", "safe"),
            ("contentRating[]", "suggestive"),
            ("includes[]", "cover_art"),
            ("order[relevance]", "desc"),
        ]
        payload = await self._get_json("/manga", params=params)
        results: list[MangaSummary] = []
        for item in payload.get("data", []):
            manga_id = item["id"]
            attr = item.get("attributes") or {}
            results.append(
                MangaSummary(
                    id=manga_id,
                    title=self._pick_text(attr.get("title"), "Sans titre"),
                    description=self._pick_text(attr.get("description"), "Aucun resume disponible."),
                    status=attr.get("status", "unknown"),
                    year=attr.get("year"),
                    tags=self._tags(attr),
                    cover_url=self._cover_from_relationships(manga_id, item.get("relationships") or []),
                )
            )
        return results

    async def get_manga(self, manga_id: str) -> MangaSummary:
        payload = await self._get_json(
            f"/manga/{manga_id}",
            params=[("includes[]", "cover_art")],
        )
        item = payload["data"]
        attr = item.get("attributes") or {}
        return MangaSummary(
            id=manga_id,
            title=self._pick_text(attr.get("title"), "Sans titre"),
            description=self._pick_text(attr.get("description"), "Aucun resume disponible."),
            status=attr.get("status", "unknown"),
            year=attr.get("year"),
            tags=self._tags(attr),
            cover_url=self._cover_from_relationships(manga_id, item.get("relationships") or []),
        )

    async def get_chapters(
        self,
        manga_id: str,
        *,
        limit: int = 10,
        offset: int = 0,
    ) -> list[ChapterSummary]:
        params = [
            ("manga", manga_id),
            ("translatedLanguage[]", self.language),
            ("limit", str(limit)),
            ("offset", str(offset)),
            ("includes[]", "scanlation_group"),
            ("order[chapter]", "desc"),
            ("order[volume]", "desc"),
        ]
        payload = await self._get_json("/chapter", params=params)
        chapters: list[ChapterSummary] = []
        for item in payload.get("data", []):
            attr = item.get("attributes") or {}
            title = attr.get("title") or "Sans titre"
            chapter_num = attr.get("chapter") or "?"
            chapter_label = f"Ch. {chapter_num}"
            chapters.append(
                ChapterSummary(
                    id=item["id"],
                    title=title,
                    chapter=chapter_label,
                    pages=attr.get("pages", 0),
                    scanlation_group=self._scanlation_name(item.get("relationships") or []),
                )
            )
        return chapters

    async def get_chapter_pages(self, chapter_id: str, manga_id: str, manga_title: str) -> ChapterPages:
        chapter_payload = await self._get_json(f"/chapter/{chapter_id}")
        chapter_item = chapter_payload["data"]
        chapter_attr = chapter_item.get("attributes") or {}
        title = chapter_attr.get("title") or "Sans titre"
        chapter_num = chapter_attr.get("chapter") or "?"

        at_home = await self._get_json(f"/at-home/server/{chapter_id}")
        chapter = at_home["chapter"]
        page_files = chapter.get("dataSaver") if self.data_saver else chapter.get("data")
        folder = "data-saver" if self.data_saver else "data"
        page_urls = [
            f"{at_home['baseUrl']}/{folder}/{chapter['hash']}/{filename}"
            for filename in page_files or []
        ]
        return ChapterPages(
            chapter_id=chapter_id,
            manga_id=manga_id,
            title=f"{manga_title} - Ch. {chapter_num}",
            chapter=f"Ch. {chapter_num}",
            page_urls=page_urls,
        )
