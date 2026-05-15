from __future__ import annotations

import asyncio
import logging

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


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


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
        self.app.on_message(filters.private & filters.command("library"))(self.library_cmd)
        self.app.on_message(filters.private & filters.command("latest"))(self.latest_cmd)
        self.app.on_message(filters.private & filters.text & ~filters.command(["start", "library", "latest"]))(
            self.search_handler
        )
        self.app.on_callback_query(filters.regex(r"^mfr\|"))(self.callback_router)

    async def start_cmd(self, _client: Client, msg: Message) -> None:
        text = (
            "<b>Manga Library FR</b>\n"
            "======================\n\n"
            "Envoie-moi le nom d'un manga et je chercherai les chapitres en francais.\n"
            "Tu peux aussi ouvrir les dernieres sorties FR ou reprendre ta lecture.\n\n"
            "Commandes utiles:\n"
            "- /library : favoris et reprise de lecture\n"
            "- /latest : derniers chapitres FR\n"
        )
        await msg.reply(text, reply_markup=self._home_kb())

    async def library_cmd(self, _client: Client, msg: Message) -> None:
        await self.show_library(msg, msg.from_user.id)

    async def latest_cmd(self, _client: Client, msg: Message) -> None:
        await self.show_latest(msg, 0)

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

        rows = [[InlineKeyboardButton(manga.title[:56], callback_data=f"mfr|detail|{manga.id}")] for manga in results]
        rows.append([InlineKeyboardButton("Bibliotheque", callback_data="mfr|library|0")])
        rows.append([InlineKeyboardButton("Latest FR", callback_data="mfr|latest_page|0")])
        await wait.edit(
            "<b>Resultats</b>\n======================\n\nChoisis un manga ci-dessous.",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def show_library(self, target: Message, user_id: int) -> None:
        favorites = self.store.list_favorites(user_id)
        progress = self.store.list_recent_progress(user_id)

        rows: list[list[InlineKeyboardButton]] = []
        if progress:
            rows.append([InlineKeyboardButton("Continuer", callback_data=f"mfr|continue|{progress[0].manga_id}")])
        for manga_id, title in favorites[:8]:
            rows.append([InlineKeyboardButton(title[:50], callback_data=f"mfr|detail|{manga_id}")])
        rows.append([InlineKeyboardButton("Accueil", callback_data="mfr|home|0")])

        if len(rows) == 1:
            await target.reply(
                "Ta bibliotheque est encore vide.\n\nAjoute des favoris depuis la fiche d'un manga.",
                reply_markup=self._home_kb(),
            )
            return

        await target.reply(
            "<b>Ta bibliotheque</b>\n======================\n\nRetrouve tes favoris et ta lecture en cours.",
            reply_markup=InlineKeyboardMarkup(rows),
        )

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
            rows.append([InlineKeyboardButton(label, callback_data=f"mfr|latest|{release.chapter_id}")])

        nav: list[InlineKeyboardButton] = []
        if offset > 0:
            nav.append(InlineKeyboardButton("Prev", callback_data=f"mfr|latest_page|{max(0, offset - 10)}"))
        if len(releases) == 10:
            nav.append(InlineKeyboardButton("Next", callback_data=f"mfr|latest_page|{offset + 10}"))
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton("Accueil", callback_data="mfr|home|0")])

        await target.reply(
            "<b>Dernieres sorties FR</b>\n======================\n\nChoisis un chapitre recent pour lire ou ouvrir la fiche du manga.",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def callback_router(self, client: Client, cb: CallbackQuery) -> None:
        parts = cb.data.split("|")
        action = parts[1]
        await cb.answer()

        if action == "home":
            await cb.message.reply(
                "<b>Accueil</b>\n======================\n\nEnvoie un titre pour chercher un manga, ou utilise les boutons.",
                reply_markup=self._home_kb(),
            )
            return
        if action == "library":
            await self.show_library(cb.message, cb.from_user.id)
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
        if action == "page":
            chapter_id, manga_id, page_idx = parts[2], parts[3], int(parts[4])
            await self.show_reader(client, cb.message, cb.from_user.id, chapter_id, manga_id, page_idx, send_new=False)
            return
        if action == "favorite":
            manga_id = parts[2]
            manga = DETAIL_CACHE.get(manga_id) or await self.source.get_manga(manga_id)
            favored = self.store.toggle_favorite(cb.from_user.id, manga_id, manga.title)
            await cb.answer("Ajoute aux favoris." if favored else "Retire des favoris.", show_alert=False)
            await self.show_manga_detail(client, cb.message, cb.from_user.id, manga_id, edit_existing=True)
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
        chapter_line = _chapter_title(release.chapter_label, release.chapter_title)
        group_line = release.scanlation_group or "Source non precisee"
        await target.reply(
            f"<b>{release.manga_title}</b>\n"
            "======================\n\n"
            f"{chapter_line}\n"
            f"Groupe: <i>{_truncate(group_line, 80)}</i>\n\n"
            "Tu peux ouvrir la fiche ou lire directement ce chapitre.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Fiche", callback_data=f"mfr|detail|{release.manga_id}"),
                        InlineKeyboardButton("Lire", callback_data=f"mfr|latest_read|{release.chapter_id}|{release.manga_id}"),
                    ],
                    [InlineKeyboardButton("Retour latest", callback_data="mfr|latest_page|0")],
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

        tags = ", ".join(manga.tags[:6]) if manga.tags else "Aucun tag"
        caption = (
            f"<b>{manga.title}</b>\n"
            "======================\n\n"
            f"Statut: <b>{_status_label(manga.status)}</b>\n"
            f"Annee: <b>{manga.year or '-'}</b>\n"
            f"Genres: <i>{_truncate(tags, 120)}</i>\n\n"
            f"{_truncate(manga.description or 'Aucun resume disponible.', 900)}"
        )
        rows = [
            [
                InlineKeyboardButton("Chapitres", callback_data=f"mfr|chapters|{manga_id}|0"),
                InlineKeyboardButton("Favori" if not is_favorite else "Retirer", callback_data=f"mfr|favorite|{manga_id}"),
            ]
        ]
        if progress:
            rows.append([InlineKeyboardButton("Continuer", callback_data=f"mfr|continue|{manga_id}")])
        rows.append([InlineKeyboardButton("Latest FR", callback_data="mfr|latest_page|0")])
        keyboard = InlineKeyboardMarkup(rows)

        if manga.cover_url and not edit_existing:
            await client.send_photo(
                chat_id=target.chat.id,
                photo=manga.cover_url,
                caption=caption,
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
            [InlineKeyboardButton(_chapter_title(ch.chapter, ch.title), callback_data=f"mfr|read|{ch.id}|{manga_id}")]
            for ch in chapters
        ]
        nav: list[InlineKeyboardButton] = []
        if offset > 0:
            nav.append(InlineKeyboardButton("Prev", callback_data=f"mfr|chapters|{manga_id}|{max(0, offset - 10)}"))
        if len(chapters) == 10:
            nav.append(InlineKeyboardButton("Next", callback_data=f"mfr|chapters|{manga_id}|{offset + 10}"))
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton("Retour fiche", callback_data=f"mfr|detail|{manga_id}")])

        await target.reply(
            f"<b>{manga.title}</b>\n======================\n\nChapitres FR disponibles:",
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

        caption = f"<b>{manga.title}</b>\n{pages.chapter} - Page <b>{page_idx + 1}/{len(pages.page_urls)}</b>"
        rows = [[]]
        if page_idx > 0:
            rows[0].append(InlineKeyboardButton("Prev page", callback_data=f"mfr|page|{chapter_id}|{manga_id}|{page_idx - 1}"))
        if page_idx < len(pages.page_urls) - 1:
            rows[0].append(InlineKeyboardButton("Next page", callback_data=f"mfr|page|{chapter_id}|{manga_id}|{page_idx + 1}"))
        rows.append([InlineKeyboardButton("Chapitres", callback_data=f"mfr|chapters|{manga_id}|0")])
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

    def _home_kb(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Latest FR", callback_data="mfr|latest_page|0"),
                    InlineKeyboardButton("Bibliotheque", callback_data="mfr|library|0"),
                ],
            ]
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

    bot = MangaLibraryBot(config)

    async def _main() -> None:
        try:
            await bot.run_async()
        finally:
            await bot.stop_async()

    asyncio.run(_main())
