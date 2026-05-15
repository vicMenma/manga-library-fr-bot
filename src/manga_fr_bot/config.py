from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


@dataclass(slots=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    owner_id: int | None
    data_dir: Path
    db_path: Path
    log_level: str
    manga_language: str
    mangadex_api_base: str
    mangadex_uploads_base: str
    mangadex_data_saver: bool


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_config() -> Config:
    data_dir = Path(os.environ.get("BOT_DATA_DIR", "./data")).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        api_id=int(_require("API_ID")),
        api_hash=_require("API_HASH"),
        bot_token=_require("BOT_TOKEN"),
        owner_id=int(os.environ["OWNER_ID"]) if os.environ.get("OWNER_ID", "").strip() else None,
        data_dir=data_dir,
        db_path=data_dir / "manga_library.sqlite3",
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        manga_language=os.environ.get("MANGA_LANGUAGE", "fr").strip() or "fr",
        mangadex_api_base=(
            os.environ.get("MANGADEX_API_BASE", "https://api.mangadex.org").rstrip("/")
        ),
        mangadex_uploads_base=(
            os.environ.get("MANGADEX_UPLOADS_BASE", "https://uploads.mangadex.org").rstrip("/")
        ),
        mangadex_data_saver=os.environ.get("MANGADEX_DATA_SAVER", "false").strip().lower()
        in {"1", "true", "yes", "on"},
    )
