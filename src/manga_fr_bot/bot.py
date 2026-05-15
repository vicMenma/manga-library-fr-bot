from __future__ import annotations

import asyncio
import html
import logging
import secrets

from pyrogram import Client, filters, idle
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message

from manga_fr_bot.adapters import MangaDexClient
from manga_fr_bot.config import Config, load_config
from manga_fr_bot.models import ChapterPages, LatestRelease, MangaSummary
from manga_fr_bot.storage import LibraryStore

try:
    import uvloop
except ImportError:  # pragma: no cover
    uvloop = None


log = logging.getLogger(__name__)

DETAIL_CACHE: dict[str, MangaSummary] = {}
PAGE_CACHE: dict[str, ChapterPages] = {}
LATEST_CACHE: dict[str, LatestRelease] = {}
CHAPTER_NAV_CACHE: dict[tuple[str, str], tuple[str | None, str | None]] = {}
CALLBACK_PAYLOAD_CACHE: dict[str, list[str]] = {}


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _esc(text: str | None) -> str:
    return html.escape(text or "", quote=False)


def _status_label(status: str) -> str:
    return {
        "ongoing": "En cours",
        "completed": "Termine",
        "hiatus": "En pause",
        "cancelled": "Annule",
    }.get(status.lower(), status or "Inconnu")


def _chapter_title(chapter_label: str, title: str) -> str:
    title = title.strip()
    if not title or title.lower() == "sans titre":
        return chapter_label
    return f"{chapter_label} - {_truncate(title, 36)}"


def _manga_detail_caption(
    manga: MangaSummary,
    *,
    seen_label: str,
    description_limit: int,
) -> str:
    tags = _esc(", ".join(manga.tags[:6]) if manga.tags else "Aucun tag")
    description = _esc(_truncate(manga.description or "Aucun resume disponible.", description_limit))
    return (
        f"<b>{_esc(manga.title)}</b>\n"
        "======================\n\n"
        f"Statut: <b>{_status_label(manga.status)}</b>\n"
        f"Annee: <b>{manga.year or '-'}</b>\n"
        f"Genres: <i>{_truncate(tags, 120)}</i>\n\n"
        f"Dernier vu: <b>{seen_label}</b>\n\n"
        f"{description}"
    )


def _cb(action: str, *parts: str) -> str:
    raw = "|".join(["mfr", action, *parts])
    if len(raw.encode("utf-8")) <= 64:
        return raw

    for _ in range(8):
        token = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:10]
        if token in CALLBACK_PAYLOAD_CACHE:
            continue
        CALLBACK_PAYLOAD_CACHE[token] = [action, *parts]
        return f"mfr|ref|{token}"

    raise RuntimeError("Could not allocate callback token")


class MangaLibraryBot:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = LibraryStore(config.db_path)
        self.source = MangaDexClient(
            api_base=config.mangadex_api_base,
            uploads_base=config.mangadex_uploads_base,
            language=config.manga_language,
            data_saver=config.mangadex_data_saver,
        )
        self.app = Client(
            "manga-library-fr-bot",
            api_id=config.api_id,
            api_hash=config.api_hash,
            bot_token=config.bot_token,
            workdir=str(config.data_dir),
        )
        self._register_handlers()

    def _register_handlers(self) -> None:
        self.app.on_message(filters.private & filters.command("start"))(self.start_cmd)
        self.app.on_message(filters.private & filters.command("help"))(self.help_cmd)
        self.app.on_message(filters.private & filters.command("library"))(self.library_cmd)
        self.app.on_message(filters.private & filters.command("history"))(self.history_cmd)
        self.app.on_message(filters.private & filters.command("latest"))(self.latest_cmd)
        self.app.on_message(filters.private & filters.command("updates"))(self.updates_cmd)
        self.app.on_message(
            filters.private
            & filters.text
            & ~filters.command(["start", "help", "library", "history", "latest", "updates"])
        )(self.search_handler)
        self.app.on_callback_query(filters.regex(r"^mfr\|"))(self.callback_router)

    async def start_cmd(self, _client: Client, msg: Message) -> None:
        text = (
            "<b>Manga Library FR</b>\n"
            "======================\n\n"
            "Envoie-moi le nom d'un manga et je chercherai les chapitres en francais.\n"
            "Tu peux aussi ouvrir les dernieres sorties FR ou reprendre ta lecture.\n\n"
            "Commandes utiles:\n"
            "- /library : favoris et reprise de lecture\n"
            "- /history : historique recent\n"
            "- /latest : derniers chapitres FR\n"
            "- /updates : nouvelles sorties dans tes favoris\n"
            "- /help : aide rapide\n"
        )
        await msg.reply(text, reply_markup=self._home_kb())

    async def help_cmd(self, _client: Client, msg: Message) -> None:
        await msg.reply(self._help_text(), reply_markup=self._home_kb())

    async def library_cmd(self, _client: Client, msg: Message) -> None:
        await self.show_library(msg, msg.from_user.id)

    async def history_cmd(self, _client: Client, msg: Message) -> None:
        await self.show_history(msg, msg.from_user.id)

    async def latest_cmd(self, _client: Client, msg: Message) -> None:
        await self.show_latest(msg, 0)

    async def updates_cmd(self, _client: Client, msg: Message) -> None:
        await self.show_updates(msg, msg.from_user.id)

    async def search_handler(self, _client: Client, msg: Message) -> None:
        query = msg.text.strip()
        if len(query) < 2:
            await msg.reply("Donne-moi au moins 2 caracteres pour lancer une recherche.")
            return

        wait = await msg.reply(f"Recherche FR pour <b>{query}</b>...")
        try:
            results = await self.source.search_manga(query)
        except Exception as exc:  # pragma: no cover
            log.exception("Search failed")
            await wait.edit(f"Recherche impossible.\n\n<code>{exc}</code>")
            return

        if not results:
            await wait.edit("Aucun manga FR trouve pour cette recherche.")
            return

        for manga in results:
            DETAIL_CACHE[manga.id] = manga

        rows = [[InlineKeyboardButton(manga.title[:56], callback_data=_cb("detail", manga.id))] for manga in results]
        rows.append([InlineKeyboardButton("Bibliotheque", callback_data=_cb("library", "0"))])
        rows.append([InlineKeyboardButton("Latest FR", callback_data=_cb("latest_page", "0"))])
        await wait.edit(
            "<b>Resultats</b>\n======================\n\nChoisis un manga ci-dessous.",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def show_library(self, target: Message, user_id: int) -> None:
        favorites = self.store.list_favorites(user_id)
        progress = self.store.list_recent_progress(user_id)

        lines = ["<b>Ta bibliotheque</b>", "======================", ""]
        rows: list[list[InlineKeyboardButton]] = []
        if progress:
            lines.append("<b>Continuer</b>")
            for entry in progress[:4]:
                lines.append(f"- {_esc(_truncate(entry.manga_title, 38))} - {_esc(entry.chapter_label)}")
                rows.append(
                    [
                        InlineKeyboardButton(
                            f"Lire {_truncate(entry.manga_title, 28)}",
                            callback_data=_cb("continue", entry.manga_id),
                        )
                    ]
                )
            lines.append("")
        if favorites:
            lines.append("<b>Favoris</b>")
        for manga_id, title in favorites[:8]:
            lines.append(f"- {_esc(_truncate(title, 48))}")
            rows.append([InlineKeyboardButton(title[:50], callback_data=_cb("detail", manga_id))])
        rows.append(
            [
                InlineKeyboardButton("Historique", callback_data=_cb("history", "0")),
                InlineKeyboardButton("Voir updates", callback_data=_cb("updates", "0")),
            ]
        )
        rows.append([InlineKeyboardButton("Accueil", callback_data=_cb("home", "0"))])

        if not favorites and not progress:
            await target.reply(
                "Ta bibliotheque est encore vide.\n\nAjoute des favoris depuis la fiche d'un manga.",
                reply_markup=self._home_kb(),
            )
            return

        await target.reply("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    async def show_history(self, target: Message, user_id: int) -> None:
        progress = self.store.list_recent_progress(user_id, limit=12)
        if not progress:
            await target.reply(
                "Aucun historique pour le moment.\n\nOuvre un chapitre et le bot enregistrera automatiquement ta progression.",
                reply_markup=self._home_kb(),
            )
            return

        lines = ["<b>Historique recent</b>", "======================", ""]
        rows: list[list[InlineKeyboardButton]] = []
        for entry in progress:
            lines.append(
                f"- <b>{_esc(_truncate(entry.manga_title, 34))}</b> - {_esc(entry.chapter_label)} "
                f"(page {entry.page_index + 1})"
            )
            rows.append(
                [
                    InlineKeyboardButton("Continuer", callback_data=_cb("continue", entry.manga_id)),
                    InlineKeyboardButton("Fiche", callback_data=_cb("detail", entry.manga_id)),
                ]
            )
        rows.append([InlineKeyboardButton("Bibliotheque", callback_data=_cb("library", "0"))])
        await target.reply("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    async def show_updates(self, target: Message, user_id: int) -> None:
        favorites = self.store.list_favorites(user_id, limit=50)
        if not favorites:
            await target.reply(
                "Tu n'as pas encore de favoris.\n\nAjoute un manga a ta bibliotheque pour suivre les nouveaux chapitres.",
                reply_markup=self._home_kb(),
            )
            return

        rows: list[list[InlineKeyboardButton]] = []
        lines = ["<b>Nouveautes dans ta bibliotheque</b>", "======================", ""]
        found = 0

        for manga_id, manga_title in favorites:
            try:
                chapters = await self.source.get_chapters(manga_id, limit=1, offset=0)
            except Exception as exc:  # pragma: no cover
                log.warning("Updates check failed for %s: %s", manga_id, exc)
                continue
            if not chapters:
                continue

            latest = chapters[0]
            seen = self.store.get_seen_chapter(user_id, manga_id)
            if seen and seen[0] == latest.id:
                continue

            found += 1
            prev_label = seen[1] if seen else "Aucune lecture enregistree"
            lines.append(f"- <b>{_esc(_truncate(manga_title, 42))}</b>")
            lines.append(f"  Nouveau: {_esc(latest.chapter)}")
            lines.append(f"  Dernier vu: {_esc(prev_label)}")
            rows.append(
                [
                    InlineKeyboardButton("Lire", callback_data=_cb("read", latest.id, manga_id)),
                    InlineKeyboardButton("Marquer vu", callback_data=_cb("seen", manga_id, latest.id)),
                    InlineKeyboardButton("Fiche", callback_data=_cb("detail", manga_id)),
                ]
            )
            if found >= 8:
                break

        if found == 0:
            await target.reply(
                "Aucune nouveaute detectee dans tes favoris pour le moment.",
                reply_markup=self._home_kb(),
            )
            return

        rows.append([InlineKeyboardButton("Accueil", callback_data=_cb("home", "0"))])
        await target.reply("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    async def show_latest(self, target: Message, offset: int) -> None:
        try:
            releases = await self.source.get_latest_releases(offset=offset)
        except Exception as exc:  # pragma: no cover
            log.exception("Latest releases failed")
            await target.reply(f"Impossible de charger les dernieres sorties.\n\n<code>{exc}</code>")
            return

        if not releases:
            await target.reply("Aucune sortie FR recente trouvee pour le moment.")
            return

        for release in releases:
            LATEST_CACHE[release.chapter_id] = release

        rows = []
        for release in releases:
            label = f"{_truncate(release.manga_title, 28)} - {release.chapter_label}"
            rows.append([InlineKeyboardButton(label, callback_data=_cb("latest", release.chapter_id))])

        nav: list[InlineKeyboardButton] = []
        if offset > 0:
            nav.append(InlineKeyboardButton("Prev", callback_data=_cb("latest_page", str(max(0, offset - 10)))))
        if len(releases) == 10:
            nav.append(InlineKeyboardButton("Next", callback_data=_cb("latest_page", str(offset + 10))))
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton("Accueil", callback_data=_cb("home", "0"))])

        await target.reply(
            "<b>Dernieres sorties FR</b>\n======================\n\nChoisis un chapitre recent pour lire ou ouvrir la fiche du manga.",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def callback_router(self, client: Client, cb: CallbackQuery) -> None:
        parts = cb.data.split("|")
        if len(parts) >= 3 and parts[1] == "ref":
            resolved = CALLBACK_PAYLOAD_CACHE.get(parts[2])
            if resolved is None:
                await cb.answer("Ce bouton a expire. Relance la commande.", show_alert=True)
                return
            parts = ["mfr", *resolved]
        action = parts[1]
        await cb.answer()

        if action == "home":
            await cb.message.reply(
                "<b>Accueil</b>\n======================\n\nEnvoie un titre pour chercher un manga, ou utilise les boutons.",
                reply_markup=self._home_kb(),
            )
            return
        if action == "updates":
            await self.show_updates(cb.message, cb.from_user.id)
            return
        if action == "help":
            await cb.message.reply(self._help_text(), reply_markup=self._home_kb())
            return
        if action == "library":
            await self.show_library(cb.message, cb.from_user.id)
            return
        if action == "history":
            await self.show_history(cb.message, cb.from_user.id)
            return
        if action == "latest_page":
            await self.show_latest(cb.message, int(parts[2]))
            return
        if action == "latest":
            release = LATEST_CACHE.get(parts[2])
            if release is None:
                await cb.answer("Cette liste a expire. Relance /latest.", show_alert=True)
                return
            await self.show_latest_release(cb.message, release)
            return
        if action == "latest_read":
            chapter_id, manga_id = parts[2], parts[3]
            await self.show_reader(client, cb.message, cb.from_user.id, chapter_id, manga_id, 0, send_new=True)
            return
        if action == "detail":
            await self.show_manga_detail(client, cb.message, cb.from_user.id, parts[2])
            return
        if action == "chapters":
            manga_id = parts[2]
            offset = int(parts[3])
            await self.show_chapters(cb.message, manga_id, offset)
            return
        if action == "read":
            chapter_id, manga_id = parts[2], parts[3]
            await self.show_reader(client, cb.message, cb.from_user.id, chapter_id, manga_id, 0, send_new=True)
            return
        if action == "chapter":
            chapter_id, manga_id = parts[2], parts[3]
            await self.show_reader(client, cb.message, cb.from_user.id, chapter_id, manga_id, 0, send_new=False)
            return
        if action == "page":
            chapter_id, manga_id, page_idx = parts[2], parts[3], int(parts[4])
            await self.show_reader(client, cb.message, cb.from_user.id, chapter_id, manga_id, page_idx, send_new=False)
            return
        if action == "favorite":
            manga_id = parts[2]
            manga = DETAIL_CACHE.get(manga_id) or await self.source.get_manga(manga_id)
            favored = self.store.toggle_favorite(cb.from_user.id, manga_id, manga.title)
            if favored:
                try:
                    chapters = await self.source.get_chapters(manga_id, limit=1, offset=0)
                    if chapters:
                        self.store.mark_seen_chapter(
                            cb.from_user.id,
                            manga_id,
                            manga.title,
                            chapters[0].id,
                            chapters[0].chapter,
                        )
                except Exception as exc:  # pragma: no cover
                    log.warning("Could not prime seen chapter for %s: %s", manga_id, exc)
            await cb.answer("Ajoute aux favoris." if favored else "Retire des favoris.", show_alert=False)
            await self.show_manga_detail(client, cb.message, cb.from_user.id, manga_id, edit_existing=True)
            return
        if action == "mark_latest":
            manga_id = parts[2]
            manga = DETAIL_CACHE.get(manga_id) or await self.source.get_manga(manga_id)
            chapters = await self.source.get_chapters(manga_id, limit=1, offset=0)
            if not chapters:
                await cb.answer("Aucun chapitre FR a marquer.", show_alert=True)
                return
            self.store.mark_seen_chapter(cb.from_user.id, manga_id, manga.title, chapters[0].id, chapters[0].chapter)
            await cb.answer("Manga marque comme a jour.", show_alert=False)
            await self.show_manga_detail(client, cb.message, cb.from_user.id, manga_id, edit_existing=True)
            return
        if action == "seen":
            manga_id, chapter_id = parts[2], parts[3]
            manga = DETAIL_CACHE.get(manga_id) or await self.source.get_manga(manga_id)
            chapter_label = "Chapitre lu"
            latest = LATEST_CACHE.get(chapter_id)
            if latest is not None:
                chapter_label = latest.chapter_label
            else:
                chapters = await self.source.get_chapters(manga_id, limit=20, offset=0)
                for chapter in chapters:
                    if chapter.id == chapter_id:
                        chapter_label = chapter.chapter
                        break
            self.store.mark_seen_chapter(cb.from_user.id, manga_id, manga.title, chapter_id, chapter_label)
            await cb.answer("Marque comme vu.", show_alert=False)
            await self.show_updates(cb.message, cb.from_user.id)
            return
        if action == "continue":
            manga_id = parts[2]
            progress = self.store.get_progress(cb.from_user.id, manga_id)
            if progress is None:
                await cb.answer("Aucune progression sauvegardee.", show_alert=True)
                return
            await self.show_reader(
                client,
                cb.message,
                cb.from_user.id,
                progress.chapter_id,
                manga_id,
                progress.page_index,
                send_new=True,
            )

    async def show_latest_release(self, target: Message, release: LatestRelease) -> None:
        chapter_line = _esc(_chapter_title(release.chapter_label, release.chapter_title))
        group_line = _esc(release.scanlation_group or "Source non precisee")
        await target.reply(
            f"<b>{_esc(release.manga_title)}</b>\n"
            "======================\n\n"
            f"{chapter_line}\n"
            f"Groupe: <i>{_truncate(group_line, 80)}</i>\n\n"
            "Tu peux ouvrir la fiche ou lire directement ce chapitre.",
            reply_markup=InlineKeyboardMarkup(
                [
                [
                    InlineKeyboardButton("Fiche", callback_data=_cb("detail", release.manga_id)),
                    InlineKeyboardButton("Lire", callback_data=_cb("latest_read", release.chapter_id, release.manga_id)),
                ],
                    [InlineKeyboardButton("Retour latest", callback_data=_cb("latest_page", "0"))],
                ]
            ),
        )

    async def show_manga_detail(
        self,
        client: Client,
        target: Message,
        user_id: int,
        manga_id: str,
        *,
        edit_existing: bool = False,
    ) -> None:
        manga = DETAIL_CACHE.get(manga_id) or await self.source.get_manga(manga_id)
        DETAIL_CACHE[manga_id] = manga
        progress = self.store.get_progress(user_id, manga_id)
        is_favorite = self.store.is_favorite(user_id, manga_id)
        seen = self.store.get_seen_chapter(user_id, manga_id)

        last_seen = _esc(seen[1]) if seen else "Aucun"
        caption = _manga_detail_caption(manga, seen_label=last_seen, description_limit=900)
        rows = [
            [
                InlineKeyboardButton("Chapitres", callback_data=_cb("chapters", manga_id, "0")),
                InlineKeyboardButton("Favori" if not is_favorite else "Retirer", callback_data=_cb("favorite", manga_id)),
            ]
        ]
        if progress:
            rows.append([InlineKeyboardButton("Continuer", callback_data=_cb("continue", manga_id))])
        rows.append(
            [
                InlineKeyboardButton("Marquer a jour", callback_data=_cb("mark_latest", manga_id)),
                InlineKeyboardButton("Latest FR", callback_data=_cb("latest_page", "0")),
            ]
        )
        keyboard = InlineKeyboardMarkup(rows)

        if manga.cover_url and not edit_existing:
            await client.send_photo(
                chat_id=target.chat.id,
                photo=manga.cover_url,
                caption=_manga_detail_caption(manga, seen_label=last_seen, description_limit=420),
                reply_markup=keyboard,
            )
            return

        if edit_existing:
            if target.photo:
                await target.edit_caption(caption, reply_markup=keyboard)
            else:
                await target.edit_text(caption, reply_markup=keyboard)
        else:
            await target.reply(caption, reply_markup=keyboard)

    async def show_chapters(self, target: Message, manga_id: str, offset: int) -> None:
        manga = DETAIL_CACHE.get(manga_id) or await self.source.get_manga(manga_id)
        DETAIL_CACHE[manga_id] = manga
        chapters = await self.source.get_chapters(manga_id, offset=offset)
        if not chapters:
            await target.reply("Aucun chapitre FR trouve pour ce manga.")
            return

        rows = [
            [InlineKeyboardButton(_chapter_title(ch.chapter, ch.title), callback_data=_cb("read", ch.id, manga_id))]
            for ch in chapters
        ]
        nav: list[InlineKeyboardButton] = []
        if offset > 0:
            nav.append(InlineKeyboardButton("Prev", callback_data=_cb("chapters", manga_id, str(max(0, offset - 10)))))
        if len(chapters) == 10:
            nav.append(InlineKeyboardButton("Next", callback_data=_cb("chapters", manga_id, str(offset + 10))))
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton("Retour fiche", callback_data=_cb("detail", manga_id))])

        await target.reply(
            f"<b>{_esc(manga.title)}</b>\n======================\n\nChapitres FR disponibles:",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def show_reader(
        self,
        client: Client,
        target: Message,
        user_id: int,
        chapter_id: str,
        manga_id: str,
        page_idx: int,
        *,
        send_new: bool,
    ) -> None:
        manga = DETAIL_CACHE.get(manga_id) or await self.source.get_manga(manga_id)
        DETAIL_CACHE[manga_id] = manga
        pages = PAGE_CACHE.get(chapter_id)
        if pages is None:
            pages = await self.source.get_chapter_pages(chapter_id, manga_id, manga.title)
            PAGE_CACHE[chapter_id] = pages

        if not pages.page_urls:
            if pages.external_url:
                await target.reply(
                    f"<b>{_esc(manga.title)}</b>\n"
                    f"{_esc(pages.chapter)}\n\n"
                    "Ce chapitre FR est fourni via une source externe et n'est pas lisible page par page directement dans Telegram.",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [InlineKeyboardButton("Lire sur la source", url=pages.external_url)],
                            [InlineKeyboardButton("Retour chapitres", callback_data=_cb("chapters", manga_id, "0"))],
                        ]
                    ),
                    disable_web_page_preview=True,
                )
                return
            await target.reply("Impossible de charger les pages de ce chapitre.")
            return

        page_idx = max(0, min(page_idx, len(pages.page_urls) - 1))
        self.store.save_progress(
            user_id,
            manga_id,
            manga.title,
            chapter_id,
            pages.chapter,
            page_idx,
        )
        self.store.mark_seen_chapter(
            user_id,
            manga_id,
            manga.title,
            chapter_id,
            pages.chapter,
        )

        newer_id, older_id = await self._chapter_nav_ids(manga_id, chapter_id)
        caption = (
            f"<b>{_esc(manga.title)}</b>\n"
            f"{_esc(pages.chapter)} - Page <b>{page_idx + 1}/{len(pages.page_urls)}</b>"
        )
        rows = [[]]
        if page_idx > 0:
            rows[0].append(InlineKeyboardButton("Prev page", callback_data=_cb("page", chapter_id, manga_id, str(page_idx - 1))))
        if page_idx < len(pages.page_urls) - 1:
            rows[0].append(InlineKeyboardButton("Next page", callback_data=_cb("page", chapter_id, manga_id, str(page_idx + 1))))
        chapter_row: list[InlineKeyboardButton] = []
        if newer_id:
            chapter_row.append(InlineKeyboardButton("Newer ch.", callback_data=_cb("chapter", newer_id, manga_id)))
        if older_id:
            chapter_row.append(InlineKeyboardButton("Older ch.", callback_data=_cb("chapter", older_id, manga_id)))
        if chapter_row:
            rows.append(chapter_row)
        rows.append([InlineKeyboardButton("Chapitres", callback_data=_cb("chapters", manga_id, "0"))])
        reply_markup = InlineKeyboardMarkup([row for row in rows if row])

        if send_new:
            await client.send_photo(
                chat_id=target.chat.id,
                photo=pages.page_urls[page_idx],
                caption=caption,
                reply_markup=reply_markup,
            )
            return

        await target.edit_media(
            InputMediaPhoto(media=pages.page_urls[page_idx], caption=caption),
            reply_markup=reply_markup,
        )

    async def _chapter_nav_ids(self, manga_id: str, chapter_id: str) -> tuple[str | None, str | None]:
        cached = CHAPTER_NAV_CACHE.get((manga_id, chapter_id))
        if cached is not None:
            return cached

        newer, older = await self.source.get_chapter_context(manga_id, chapter_id)
        nav_ids = (newer.id if newer else None, older.id if older else None)
        CHAPTER_NAV_CACHE[(manga_id, chapter_id)] = nav_ids
        return nav_ids

    def _home_kb(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Latest FR", callback_data=_cb("latest_page", "0")),
                    InlineKeyboardButton("Bibliotheque", callback_data=_cb("library", "0")),
                ],
                [
                    InlineKeyboardButton("Updates", callback_data=_cb("updates", "0")),
                    InlineKeyboardButton("Historique", callback_data=_cb("history", "0")),
                ],
                [InlineKeyboardButton("Aide", callback_data=_cb("help", "0"))],
            ]
        )

    def _help_text(self) -> str:
        return (
            "<b>Aide rapide</b>\n"
            "======================\n\n"
            "1. Envoie le nom d'un manga.\n"
            "2. Ouvre sa fiche.\n"
            "3. Ajoute-le en favori pour suivre les sorties.\n"
            "4. Lis un chapitre: la progression est sauvegardee automatiquement.\n\n"
            "<b>Commandes</b>\n"
            "- /library : favoris + reprise\n"
            "- /history : dernieres lectures\n"
            "- /latest : sorties FR recentes\n"
            "- /updates : nouveaux chapitres de tes favoris\n\n"
            "<b>Astuces</b>\n"
            "- \"Marquer a jour\" enregistre le dernier chapitre comme deja vu.\n"
            "- \"Continuer\" reprend exactement a la page sauvegardee.\n"
            "- Les updates se basent sur tes favoris, donc pense a les ajouter."
        )

    async def run_async(self) -> None:
        await self.app.start()
        me = await self.app.get_me()
        log.info("Started @%s", me.username)
        await idle()

    async def stop_async(self) -> None:
        await self.source.close()
        await self.app.stop()


def run() -> None:
    config = load_config()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if uvloop is not None:
        uvloop.install()

    async def _main(bot: MangaLibraryBot) -> None:
        try:
            await bot.run_async()
        finally:
            await bot.stop_async()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot = MangaLibraryBot(config)
    try:
        loop.run_until_complete(_main(bot))
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        asyncio.set_event_loop(None)
        loop.close()
