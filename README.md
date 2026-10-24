# Telegram Desktop Chat-History Migration (v8.2)

A Python tool for reconstructing a Telegram conversation in a destination channel from a **Telegram Desktop export** (`messages.html`, `messages2.html`, etc.) and the media files that accompany it. 

> **Scope:** this program migrates a local export. It does not sign into Telegram Web or retrieve the complete live history of an unrelated channel. Most reconstructed messages are newly sent by your destination bot; the program cannot recreate arbitrary historical sender identities or original native reaction totals. This program uses Telegram's official Bot API/MTProto bot authentication to send migrated messages, rather than logging in through your personal Telegram account. It does not require your personal phone number, SMS login code, or personal account session, reducing the exposure of your personal account credentials. The bot operates under its own identity, so sending limits and restrictions primarily affect the bot.

## Features

- Streams Telegram Desktop HTML exports instead of loading every page into memory at once.
- Reconstructs supported rich-text formatting (including italics, bold, underline, strike, code, links, spoilers, and blockquotes) using Telegram message entities.
- Preserves sender names and original timestamps as archival text where available.
- Handles common exported media: photos, documents, videos, GIFs, voice messages, round videos, and sticker files.
- Maps source message IDs to destination IDs to restore reply links where possible; audits available reply links.
- Persists partial-send state so interrupted runs can often resume without intentionally resending already-recorded pieces.
- Checks oversized attachments before sending and stops with instructions for manual handling.
- Uses FloodWait handling and records failures and formatting fallbacks.
- Can archive reaction metadata from a compatible companion JSON export as **text notes**. It cannot restore the original native reaction bar, original voters, or historical reaction counts as live Telegram reactions.
- Uses a Telegram bot session for sending. It does not require a personal-account login for the migration itself.

## Requirements

- Python 3.10 or newer recommended.
- A Telegram API ID and API hash from [my.telegram.org](https://my.telegram.org).
- A bot token from [@BotFather](https://t.me/BotFather).
- The bot must be an administrator in the destination channel with permission to post.
- A Telegram Desktop export with its HTML pages and media folders kept together.

### Install

```bash
python -m venv .venv
```

Activate the virtual environment, then install dependencies:

```bash
python -m pip install -r requirements.txt
```

`static-ffmpeg` is an optional helper for automatic FFmpeg/ffprobe setup when a video sticker needs metadata. The script attempts a lazy install only if it is needed and no working system binaries are available.

## Configure

1. Copy `.env.example` to `.env`.
2. Replace the example API hash, bot token, and destination peer with your own values.
3. Put your exported Telegram HTML pages and media folders in the directory specified by `TELEGRAM_EXPORT_DIR`.

Example `.env`:

```dotenv
TELEGRAM_API_ID=39505167
TELEGRAM_API_HASH=replace_with_your_api_hash
TELEGRAM_BOT_TOKEN=replace_with_your_new_bot_token
TELEGRAM_CHANNEL_PEER=-1001234567890
TELEGRAM_EXPORT_DIR=./telegram_export
TELEGRAM_DRY_RUN=true
TELEGRAM_SKIP_CONFIRMATION=false
```

`TELEGRAM_CHANNEL_PEER` may be a numeric peer ID (for example `-1001234567890`) or a public `@username`. The program intentionally has no preconfigured destination ID.

The API ID default in the script is `39505167`, for this version; you can override it with `TELEGRAM_API_ID`.

### Google Colab

You can use Colab Secrets named `TELEGRAM_API_HASH`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHANNEL_PEER`; enable notebook access for each. Set `TELEGRAM_EXPORT_DIR` to the actual Drive path, for example:

```text
/content/drive/MyDrive/ChatExport_2025-01-01
```

The script mounts Google Drive automatically when the configured export directory is under `/content/drive/` and Drive is not mounted yet. You can run the `.py` file directly; no separate `await main()` cell is required.

## Run safely

**Dry-run is enabled by default.** Start by running:

```bash
python telegram_migration.py
```

Review the reported source file count, destination channel title/ID, and dry-run summary. No messages should be sent while `TELEGRAM_DRY_RUN=true`.

When you are satisfied with the preview, change this in `.env`:

```dotenv
TELEGRAM_DRY_RUN=false
```

Run the program again. Keep `TELEGRAM_SKIP_CONFIRMATION=false` so the program asks you to confirm the target before sending.

## Progress files and resuming

The script keeps its progress files inside the export directory. These include the ID map, partial-send state, pending replies, failure log, manual-upload list, formatting audit, and reply audit. Keep these files between runs if you want resumability. Do not commit them to a public repository: they can reveal private chat metadata.

If the program stops at a file larger than the configured upload ceiling, follow the printed manual-upload instructions **before** running the program again. The script can mark that source message to be skipped on the next run.

## Known limitations

- Telegram service messages (for example, join/leave events and title changes) are intentionally excluded.
- The destination messages are not identical to the original Telegram records. Sender names and timestamps are represented as text in reconstructed messages unless a native forward is possible.
- The source HTML and media folder must contain the required data. A missing attachment cannot be recreated by this program.
- If Telegram rejects formatting entities, the script can fall back to plain text for that message; check `migration_formatting_dropped.jsonl`.
- Reaction archives are informational text notes, not native Telegram reactions.
- Native forwards may be unavailable for source messages whose origins are inaccessible; the program's configured forwarding fallback may use a textual header.
- Files above `MAX_UPLOAD_MB` cause a clean stop and require manual handling. Files below the configured limit can still fail due to network, server, permissions, or media constraints.

## Development checks

Run the focused tests:

```bash
python -m unittest discover -s tests -v
```

## Security

- Never commit `.env`, bot tokens, API hashes, Telegram session files, exports, media, or migration state.
- If you have ever exposed a bot token, revoke it with BotFather and replace it before running the program.
- Treat API hashes and Telethon session files as private credentials.
- Avoid sharing migration logs publicly; they may contain source message IDs and message-specific error details.

```bash
git init
git add .gitignore .env.example README.md SECURITY.md CHANGELOG.md requirements.txt requirements-media.txt telegram_migration.py tests .github
git status --short
git diff --cached --stat
git commit -m "Prepare Telegram migration v8.2 repository"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/YOUR-REPOSITORY.git
git push -u origin main
```

Before committing, make sure `.env`, exported HTML, media directories, session files, and migration state are not staged. `.gitignore` excludes the usual locations and sensitive file types, but always inspect `git status` before pushing.

## License

GNU General Public License v3.0
