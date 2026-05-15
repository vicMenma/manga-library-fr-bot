# Manga Library FR Bot

A Telegram manga library bot focused on French chapters, built with a MangaDex-first catalog layer.

## What is already in this starter

- French manga search
- Manga details with status, year, genres, and cover
- Chapter browser
- Page-by-page Telegram reader
- Chapter-to-chapter reader navigation
- Latest French releases view
- Favorite update tracking
- Favorites
- Reading progress / continue reading
- SQLite storage for personal library data

## Stack

- Pyrogram
- httpx
- SQLite
- MangaDex public API

## Setup

1. Create a Telegram bot with BotFather.
2. Get your Telegram `API_ID` and `API_HASH`.
3. Copy `.env.example` to `.env` and fill it in.
4. Install dependencies:

```bash
pip install -e .
```

5. Run the bot:

```bash
python main.py
```

## Colab

There is now a dedicated launcher:

```bash
python colab_launcher.py
```

If you open the file in Colab, you can use the form fields directly.

## Commands

- `/start` - welcome screen
- `/library` - favorites and continue reading
- `/latest` - latest French releases
- `/updates` - new French chapters from your favorites

## Notes

- This V1 starter filters chapter browsing to French by default.
- The Colab launcher writes a fresh `.env` for each run and stores data on Drive.
- The source layer is adapter-based so more French manga sources can be added later.
- MangaDex API behavior can evolve, so the adapter should stay isolated from the Telegram UI.
