"""
High-fidelity Telegram Desktop export migration
(v8.2: oversize-file stop matching Telegram's MTProto ceiling, hardened entity handling,
crash-safe single-captioned media; configurable, GitHub-ready setup).

Changes in v8.2
  - MAX_UPLOAD_MB raised from 50 to 2000, matching the MTProto upload ceiling
    Telethon reaches when a bot connects directly (not via the HTTP Bot API).
    Files up to 2000 MiB are now attempted automatically; the halt path only
    fires for files that genuinely exceed Telegram's limit.

Changes in v8.1
  - save_state is called unconditionally after a single captioned media upload,
    matching the multi-media and no-caption paths. A crash between the upload
    returning and the id-map write can no longer duplicate the media on resume.
  - _is_entity_error no longer matches the bare word "entities", which was
    over-broad and could route unrelated exceptions into the formatting-fallback
    path. It now relies on the exception class name and the "invalid bounds" /
    "entity_bounds" phrasings.
  - Reaction archive notes are sanitised and sent with parse_mode=None, so a
    custom reaction label containing markdown characters is not re-parsed.

Changes in v8
  - When an oversize file stops the run, the script now writes that message's id
    into MANUAL_UPLOADS_FILE by itself (AUTO_MARK_MANUAL_ON_STOP), so you do not
    have to edit anything: upload the file(s) by hand, run the script again, and it
    continues with the next message. It records the big message's OWN id (not
    id+1, which would skip a different message).

Changes in v7
  - Before uploading, every message's media is size-checked. If any file is
    larger than MAX_UPLOAD_MB, the script uploads NOTHING of that message,
    logs its id to the failures file, prints exactly where the files are and
    what to do, saves all progress and stops cleanly. Rerun later to resume.
    Files you uploaded by hand are listed in MANUAL_UPLOADS_FILE.
  - Entity hardening: text is normalised (carriage returns and control
    characters removed) before entity offsets are measured; entities are
    clamped to valid bounds before every send; Telethon is told NOT to
    re-parse our text as markdown (parse_mode=None); and if Telegram still
    rejects the formatting ("entities have invalid bounds"), the message is
    retried once as plain text instead of failing, with the details written
    to FORMAT_DEBUG_FILE.
  - A FloodWait during an album upload is no longer swallowed by the
    retry-without-formatting fallback.

Earlier changes (v6): send_file keyword fallback, honest video flags, hachoir,
single-pass indexing, album/GIF/photo/document handling, deferred replies.
"""

import asyncio
import copy
import json
import os
import re
import subprocess
import sys
import shutil
import inspect
from datetime import datetime
from urllib.parse import urlparse, parse_qs

try:
    from lxml import etree
    from telethon import TelegramClient
    from telethon.tl import types as tl
    from telethon.errors import FloodWaitError
    try:
        import ijson
    except ImportError:
        ijson = None
except ImportError:
    print("Installing Telethon and lxml...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "telethon", "lxml"])
    from lxml import etree
    from telethon import TelegramClient
    from telethon.tl import types as tl
    from telethon.errors import FloodWaitError
    try:
        import ijson
    except ImportError:
        ijson = None

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════
# Load a local .env file when python-dotenv is installed. In Colab, secrets
# are also supported through _load_secret() below.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

API_ID = int(os.environ.get("TELEGRAM_API_ID", "12345678"))


def _load_secret(name):
    """Read a Colab Secret when available, otherwise use an environment variable."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    try:
        from google.colab import userdata
        return (userdata.get(name) or "").strip()
    except Exception:
        return ""


API_HASH = _load_secret("TELEGRAM_API_HASH")
BOT_TOKEN = _load_secret("TELEGRAM_BOT_TOKEN")

# Destination can be a numeric Telegram peer ID (for example -1001234567890)
# or a public @username. It is intentionally not hard-coded in this repository.
_CHANNEL_PEER_RAW = _load_secret("TELEGRAM_CHANNEL_PEER").strip()
if _CHANNEL_PEER_RAW:
    try:
        CHANNEL_PEER = int(_CHANNEL_PEER_RAW)
    except ValueError:
        CHANNEL_PEER = _CHANNEL_PEER_RAW
else:
    CHANNEL_PEER = None

# Folder containing messages.html, messages2.html, media folders, and optional
# result.json/messages.json. Defaults to ./telegram_export for a portable repo.
BASE_DIR = os.path.abspath(os.path.expanduser(
    os.environ.get("TELEGRAM_EXPORT_DIR", "./telegram_export")
))
HTML_FILE_PATTERN = r"^messages(\d*)\.html$"      # messages.html, messages2.html, ...
IDMAP_FILE   = os.path.join(BASE_DIR, "migration_id_map.jsonl")
FAILED_FILE  = os.path.join(BASE_DIR, "failed_messages.txt")
PARTIAL_FILE = os.path.join(BASE_DIR, "migration_partial_sends.jsonl")
PENDING_REPLIES_FILE = os.path.join(BASE_DIR, "migration_pending_replies.json")
REACTION_NOTE_MAP_FILE = os.path.join(BASE_DIR, "migration_reaction_notes.jsonl")
REACTION_ARCHIVE_FILE = os.path.join(BASE_DIR, "migration_reactions_archive.jsonl")
REACTION_SOURCE_FILE = os.path.join(BASE_DIR, "migration_reactions_source.jsonl")
REPLY_AUDIT_FILE = os.path.join(BASE_DIR, "migration_reply_audit.jsonl")
FORMAT_DEBUG_FILE = os.path.join(BASE_DIR, "migration_formatting_dropped.jsonl")
# Source message ids you uploaded by hand after an oversize stop (see below).
MANUAL_UPLOADS_FILE = os.path.join(BASE_DIR, "migration_manual_uploads.txt")

# Reactions in Telegram HTML exports may be absent. If a companion result.json
# contains reaction metadata, the script archives that metadata as a reply to
# the migrated message. It does NOT fabricate native reaction totals or voters.
PRESERVE_REACTIONS_AS_ANNOTATIONS = True
JSON_REACTION_FILE_CANDIDATES = ("result.json", "messages.json")
MAX_JSON_FALLBACK_MB = 512
VERIFY_REPLIES = True

# Largest file the script will attempt to upload. Telethon connects via MTProto
# (not the HTTP Bot API), so the effective ceiling for a bot is 2000 MiB, far
# above the 50 MB Bot API limit. A message with any bigger file stops the run
# cleanly (nothing from that message is uploaded) so you can upload it by hand
# and resume on the next run. Set to None or 0 to disable the check.
MAX_UPLOAD_MB         = 2000
# On an oversize stop, add that message's id to MANUAL_UPLOADS_FILE automatically, so
# the next run skips it and continues with the following message. UPLOAD THE FILE(S)
# BEFORE RERUNNING: a rerun without the upload leaves that message out of the channel
# (it stays listed in the failures file and in the manual-uploads file). Set to False
# to add the id yourself instead.
AUTO_MARK_MANUAL_ON_STOP = True

BATCH_LIMIT           = 1000
# Safe by default: preview the migration before the first real send.
# Set TELEGRAM_DRY_RUN=false in .env to enable sending.
DRY_RUN = os.environ.get("TELEGRAM_DRY_RUN", "true").strip().casefold() in {"1", "true", "yes", "y", "on"}
MAX_RETRIES           = 5
MAX_FLOOD_WAITS       = 10
SEND_DELAY_SECONDS    = 2.5
# GIFs are detected from explicit export markup/labels or a .gif filename.
# Do not infer GIFs merely because an MP4 is short and silent.
PRESERVE_SENDER_NAMES = True
SKIP_CONFIRMATION = os.environ.get("TELEGRAM_SKIP_CONFIRMATION", "false").strip().casefold() in {"1", "true", "yes", "y", "on"}

CAPTION_LIMIT = 1024              # 2048 for Telegram Premium accounts
TEXT_LIMIT    = 4096              # 4096 for everyone (Premium too)

# Forwarding policy:
#   True  -> try native forward first (only possible when the export carries a
#            usable message link), fall back to the textual header.
#   False -> always use the textual "Forwarded from ..." header.
NATIVE_FORWARD = True
# A native forward cannot carry our timestamp / sender line. When True, that
# line is sent as a short reply to the forwarded message.
FORWARD_ANNOTATE = True

# Sticker files need the sticker-specific upload attributes and cannot be
# captioned like ordinary photos/videos. Keep all recognized sticker subtypes
# in this set so their text is sent separately.
STICKER_KINDS = ("sticker_static", "sticker_animated", "sticker_video", "sticker_unknown")
NO_CAPTION_KINDS = ("voice", "round_video", *STICKER_KINDS)


# ═══════════════════════════════════════════════════════════════
# UTILITIES
# ═══════════════════════════════════════════════════════════════
def u16len(s):
    """Length in UTF-16 code units (Telegram's unit for entity offsets)."""
    return len(s.encode("utf-16-le")) // 2


def _classes(el):
    return (el.get("class") or "").split()


def _find_first(el, class_name):
    for child in el.iter():
        if child is not el and class_name in _classes(child):
            return child
    return None


def _itertext(el):
    return "".join(el.itertext()).strip() if el is not None else ""


def _text_without(el, skip_class):
    """Text of `el`, leaving out descendants that carry `skip_class`."""
    if el is None:
        return ""
    out = []

    def rec(e):
        if e.text:
            out.append(e.text)
        for c in e:
            if isinstance(c.tag, str) and skip_class not in _classes(c):
                rec(c)
            if c.tail:
                out.append(c.tail)

    rec(el)
    return "".join(out).strip()


_DATE_SUFFIX = re.compile(r"\s*UTC[+-]\d{2}:\d{2}\s*$|\s*UTC\s*$")


def parse_export_date(s):
    if not s:
        return None
    s = _DATE_SUFFIX.sub("", s).strip()
    for fmt in (
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


_GIF_LABELS = ("gif", "animation", "animated gif")


def _label_says_gif(label):
    """True only when a whole line of the export's media block is the GIF/Animation label.

    A file name or title that merely contains the word "GIF" (for example
    "my GIF collection.mp4") must not make an ordinary video a GIF.
    """
    prefixes = tuple(x + "," for x in _GIF_LABELS)
    for line in str(label or "").splitlines():
        t = line.strip().casefold()
        if t in _GIF_LABELS or t.startswith(prefixes):
            return True
    return False


def _is_gif(path, anchor_classes=(), export_label=""):
    """Identify GIFs only from explicit export metadata or a .gif filename.

    Telegram Desktop commonly stores animations as silent MP4 files. Their
    export markup may flag them as animated or label the media as GIF /
    Animation. A short, silent MP4 by itself is not enough evidence.
    """
    classes = {str(c).casefold() for c in anchor_classes}
    if any(c == "gif" or "animated" in c for c in classes):
        return True
    if os.path.splitext(str(path))[1].casefold() == ".gif":
        return True
    return _label_says_gif(export_label)


# These paths are set by ensure_ffmpeg_tools(). Using explicit paths avoids relying
# on an updated PATH in notebook kernels that were started before installation.
_FFMPEG_PATH = shutil.which("ffmpeg")
_FFPROBE_PATH = shutil.which("ffprobe")
_FFMPEG_SETUP_ATTEMPTED = False
_FFMPEG_SETUP_OK = False


def _tool_works(path, timeout=12):
    """Return True if an FFmpeg executable starts successfully."""
    if not path or not os.path.isfile(path) and not shutil.which(path):
        return False
    try:
        result = subprocess.run(
            [path, "-version"], capture_output=True, text=True,
            timeout=timeout, check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def _quiet_pip_install(package, timeout=300):
    """Install a Python package non-interactively, capturing noisy output."""
    cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", package]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "pip returned a non-zero exit code").strip()
        raise RuntimeError(detail[-1200:])


def _enable_hachoir():
    """Make sure Telethon can read duration/dimensions of videos, voice notes and round videos.

    Telethon uses hachoir for that, but only if it was importable when Telethon
    itself was imported. After installing it here, hand the module to Telethon.
    """
    try:
        import hachoir  # noqa: F401
        import hachoir.metadata  # noqa: F401
        import hachoir.parser  # noqa: F401
    except ImportError:
        try:
            print("Installing hachoir (media duration/size metadata)...")
            _quiet_pip_install("hachoir")
            import hachoir  # noqa: F401
            import hachoir.metadata  # noqa: F401
            import hachoir.parser  # noqa: F401
        except Exception as exc:
            print(f"WARNING: hachoir is unavailable ({type(exc).__name__}: {exc}).")
            print("  Voice notes / videos may show no duration. Try: pip install hachoir, then restart the runtime.")
            return False
    try:
        import telethon.utils as _tu
        if getattr(_tu, "hachoir", None) is None:
            _tu.hachoir = sys.modules["hachoir"]
    except Exception:
        pass
    return True


def ensure_ffmpeg_tools():
    """Ensure both ffmpeg and ffprobe are available, installing them quietly if needed.

    First uses working system binaries. Otherwise installs the `static-ffmpeg`
    Python package, which fetches platform-specific ffmpeg/ffprobe binaries without
    requiring administrator rights on supported platforms. This is suitable for
    Google Colab and common Windows/macOS/Linux Python environments.
    """
    global _FFMPEG_PATH, _FFPROBE_PATH, _FFMPEG_SETUP_ATTEMPTED, _FFMPEG_SETUP_OK
    if _FFMPEG_SETUP_ATTEMPTED:
        return _FFMPEG_SETUP_OK
    _FFMPEG_SETUP_ATTEMPTED = True

    candidate_ffmpeg = shutil.which("ffmpeg") or _FFMPEG_PATH
    candidate_ffprobe = shutil.which("ffprobe") or _FFPROBE_PATH
    if _tool_works(candidate_ffmpeg) and _tool_works(candidate_ffprobe):
        _FFMPEG_PATH, _FFPROBE_PATH = candidate_ffmpeg, candidate_ffprobe
        _FFMPEG_SETUP_OK = True
        print("FFmpeg/ffprobe ready (existing installation).")
        return True

    print("FFmpeg/ffprobe not found; setting them up automatically in the background...")
    errors = []
    try:
        _quiet_pip_install("static-ffmpeg")
        import static_ffmpeg

        # The package downloads its platform binaries on first use. This call is
        # intentionally non-interactive and can take a little while on first run.
        try:
            ffmpeg_path, ffprobe_path = static_ffmpeg.run.get_or_fetch_platform_executables_else_raise()
        except AttributeError:
            # Compatibility with package releases that expose add_paths but not
            # the path-returning helper documented by current releases.
            static_ffmpeg.add_paths()
            ffmpeg_path, ffprobe_path = shutil.which("ffmpeg"), shutil.which("ffprobe")

        if not (_tool_works(ffmpeg_path) and _tool_works(ffprobe_path)):
            raise RuntimeError("the downloaded FFmpeg executables did not pass their version checks")

        _FFMPEG_PATH, _FFPROBE_PATH = ffmpeg_path, ffprobe_path
        # Also expose the tools to child processes and any helper code that uses PATH.
        tool_dir = os.path.dirname(ffmpeg_path)
        if tool_dir and tool_dir not in os.environ.get("PATH", "").split(os.pathsep):
            os.environ["PATH"] = tool_dir + os.pathsep + os.environ.get("PATH", "")
        _FFMPEG_SETUP_OK = True
        print("FFmpeg/ffprobe ready (automatic setup succeeded).")
        return True
    except Exception as exc:
        errors.append(f"static-ffmpeg setup: {type(exc).__name__}: {exc}")

    # Colab/Linux fallback: when running with sufficient privilege, use the OS
    # package manager. The command is suppressed unless it fails; it never asks
    # for an interactive password.
    apt_get = shutil.which("apt-get")
    is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    if apt_get and is_root:
        try:
            update = subprocess.run(
                [apt_get, "update", "-qq"], capture_output=True, text=True,
                timeout=240, check=False,
            )
            install = subprocess.run(
                [apt_get, "install", "-y", "-qq", "ffmpeg"], capture_output=True,
                text=True, timeout=300, check=False,
            ) if update.returncode == 0 else update
            candidate_ffmpeg = shutil.which("ffmpeg")
            candidate_ffprobe = shutil.which("ffprobe")
            if install.returncode == 0 and _tool_works(candidate_ffmpeg) and _tool_works(candidate_ffprobe):
                _FFMPEG_PATH, _FFPROBE_PATH = candidate_ffmpeg, candidate_ffprobe
                _FFMPEG_SETUP_OK = True
                print("FFmpeg/ffprobe ready (system package setup succeeded).")
                return True
            detail = (install.stderr or install.stdout or update.stderr or update.stdout or "package-manager installation failed").strip()
            errors.append(f"apt-get setup: {detail[-700:]}")
        except Exception as exc:
            errors.append(f"apt-get setup: {type(exc).__name__}: {exc}")

    _FFMPEG_SETUP_OK = False
    print("WARNING: automatic FFmpeg/ffprobe setup failed. Video stickers may use fallback metadata.")
    for error in errors:
        print("  " + error)
    print("You can install FFmpeg manually from https://ffmpeg.org/download.html and restart the runtime.")
    return False


def _probe_video_metadata(path):
    """Return width, height and duration from ffprobe, or None if probing fails.

    FFmpeg setup is lazy: this helper is only called for video-sticker metadata,
    so text/photo-only migrations never install FFmpeg packages or binaries.
    """
    ffprobe_path = shutil.which("ffprobe") or _FFPROBE_PATH
    if not ffprobe_path:
        ensure_ffmpeg_tools()
        ffprobe_path = shutil.which("ffprobe") or _FFPROBE_PATH
    if not ffprobe_path:
        return None
    try:
        result = subprocess.run(
            [
                ffprobe_path, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height,duration:format=duration",
                "-of", "json", path,
            ],
            capture_output=True, text=True, timeout=20, check=False,
        )
        if result.returncode != 0:
            return None
        info = json.loads(result.stdout or "{}")
        streams = info.get("streams") or []
        stream = streams[0] if streams else {}
        fmt = info.get("format") or {}
        duration = stream.get("duration", fmt.get("duration"))
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        seconds = float(duration) if duration not in (None, "N/A", "") else 0.0
        if width <= 0 or height <= 0 or seconds <= 0:
            return None
        return {"width": width, "height": height, "duration": seconds}
    except Exception:
        return None


def _video_has_audio(path):
    """True / False when an already-installed ffprobe can tell, None when it cannot.

    Never installs anything: ordinary videos must not trigger an FFmpeg download.
    """
    ffprobe_path = shutil.which("ffprobe") or _FFPROBE_PATH
    if not ffprobe_path:
        return None
    try:
        result = subprocess.run(
            [ffprobe_path, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=20, check=False,
        )
        if result.returncode != 0:
            return None
        return bool(result.stdout.strip())
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════
# ENTITY HELPERS  (all offsets/lengths in UTF-16 code units)
# ═══════════════════════════════════════════════════════════════
def _clip_entities(entities, a, b):
    """Entities restricted to [a, b) and re-based so that `a` becomes 0."""
    out = []
    for e in entities:
        s, t = max(e.offset, a), min(e.offset + e.length, b)
        if t > s:
            e2 = copy.copy(e)
            e2.offset = s - a
            e2.length = t - s
            out.append(e2)
    return out


def strip_with_entities(text, entities):
    """Strip leading/trailing whitespace and keep entities aligned.

    The leading offset is computed in UTF-16 code units (Telegram's unit)
    rather than Python codepoints. For ASCII/BMP whitespace the two are
    identical, but measuring in UTF-16 avoids an off-by-N shift if the
    composed message ever starts with a non-BMP codepoint.
    """
    stripped = text.lstrip()
    lead = u16len(text) - u16len(stripped)
    stripped = stripped.rstrip()
    if lead == 0 and len(stripped) == len(text):
        return text, entities
    return stripped, _clip_entities(entities, lead, lead + u16len(stripped))


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_text(s):
    """Normalise text BEFORE entity offsets are measured.

    Carriage returns become newlines and control characters are removed, so the
    text Telegram receives is exactly the text the offsets were computed on.
    """
    if not s:
        return s
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL_CHARS_RE.sub("", s)


def _shift(e, shift):
    if shift == 0:
        return e
    e2 = copy.copy(e)
    e2.offset = e.offset + shift
    return e2


def compose_message(watermark, sender, forward, body_text, body_entities, extra_before_body=None):
    text, entities = "", []
    parts = [watermark, sender, forward]
    if extra_before_body:
        parts.append(extra_before_body)
    parts.append((body_text, body_entities))
    for part, part_ents in parts:
        part = sanitize_text(part)
        if not part:
            continue
        base = u16len(text)
        text += part
        for e in part_ents:
            entities.append(_shift(e, base))
    return text, entities


def split_with_entities(text, entities, first_limit, limit=TEXT_LIMIT):
    b = text.encode("utf-16-le")
    n = len(b) // 2
    if n <= first_limit:
        return [(text, entities)]

    def unit(i):
        return b[2 * i] | (b[2 * i + 1] << 8)

    chunks, start, lim = [], 0, first_limit
    while start < n:
        hard = min(n, start + lim)
        if hard == n:
            end = nxt = n
        else:
            end = nxt = hard
            lo = start + (lim * 3) // 4
            for i in range(hard - 1, lo - 1, -1):      # last whitespace in the final quarter
                if unit(i) in (0x0A, 0x20):
                    end, nxt = i, i + 1
                    break
            else:
                if 0xD800 <= unit(hard - 1) <= 0xDBFF:  # do not split a surrogate pair
                    end = nxt = hard - 1
        seg = b[2 * start:2 * end].decode("utf-16-le")
        ents = _clip_entities(entities, start, end)
        seg, ents = strip_with_entities(seg, ents)
        if seg:
            chunks.append((seg, ents))
        start, lim = nxt, limit
    return chunks or [(text, entities)]


# ═══════════════════════════════════════════════════════════════
# RICH TEXT  (export HTML -> Telegram entities)
# ═══════════════════════════════════════════════════════════════
def clean_invalid_entities(text, entities):
    """Drop or clip entities whose bounds are invalid for `text`.

    Telegram rejects a message outright with "Some of provided entities have
    invalid bounds" when any entity has offset < 0, length <= 0, or
    offset + length > UTF-16 length of the text. This helper enforces those
    three invariants:

      1. entities entirely outside the text range     -> discarded
      2. entities that spill past the end of the text -> length shrunk
      3. entities with zero or negative length         -> discarded

    Valid entities pass through unchanged.
    """
    if not entities:
        return entities
    text_len = u16len(text or "")
    out = []
    for e in entities:
        offset = getattr(e, "offset", None)
        length = getattr(e, "length", None)
        if not isinstance(offset, int) or not isinstance(length, int):
            continue
        if length <= 0:
            continue
        if offset < 0 or offset >= text_len:
            continue
        if offset + length > text_len:
            e2 = copy.copy(e)
            e2.length = text_len - offset
            if e2.length <= 0:
                continue
            out.append(e2)
        else:
            out.append(e)
    return out


_URL_SCHEMES = ("http", "https", "tg", "ftp", "ftps")


def _make_entity(name, **kwargs):
    cls = getattr(tl, name, None)
    if cls is None:
        return None
    try:
        return cls(**kwargs)
    except TypeError:
        kwargs.pop("collapsed", None)        # older Telethon: no `collapsed`
        try:
            return cls(**kwargs)
        except TypeError:
            return None


def parse_rich_text(root):
    if root is None:
        return "", []

    parts, entities, pos = [], [], [0]
    pre_depth = [0]

    def add(s):
        s = sanitize_text(s)
        if s:
            parts.append(s)
            pos[0] += u16len(s)

    def emit(name, start, end, **extra):
        length = end - start
        if length > 0:
            e = _make_entity(name, offset=start, length=length, **extra)
            if e is not None:
                entities.append(e)

    def span(child, name, **extra):
        start = pos[0]
        walk(child)
        emit(name, start, pos[0], **extra)

    def walk(el):
        if el.text:
            add(el.text)
        for child in el:
            tag = child.tag if isinstance(child.tag, str) else ""
            cls = _classes(child) if tag else []

            if not tag:
                pass                                   # comment / processing instruction

            elif tag == "br":
                add("\n")

            elif tag == "i" and "emoji" in cls:
                add("".join(child.itertext()))

            elif tag == "a" and child.get("href"):
                href = child.get("href")
                start = pos[0]
                walk(child)
                end = pos[0]
                if href.startswith("tg://user?id="):
                    try:
                        uid = int(href.split("id=", 1)[1])
                        emit("MessageEntityMentionName", start, end, user_id=uid)
                    except (ValueError, IndexError):
                        pass
                elif href.startswith("mailto:"):
                    emit("MessageEntityEmail", start, end)
                elif urlparse(href).scheme.lower() in _URL_SCHEMES:
                    emit("MessageEntityTextUrl", start, end, url=href)

            elif tag in ("b", "strong"):
                span(child, "MessageEntityBold")

            elif tag in ("i", "em"):
                span(child, "MessageEntityItalic")

            elif tag in ("u", "ins"):
                span(child, "MessageEntityUnderline")

            elif tag in ("s", "strike", "del"):
                span(child, "MessageEntityStrike")

            elif tag == "code":
                if pre_depth[0]:
                    walk(child)                        # already inside <pre>: no second entity
                else:
                    span(child, "MessageEntityCode")

            elif tag == "pre":
                inner = next((c for c in child if c.tag == "code"), None)
                lang = ""
                if inner is not None:
                    for c in _classes(inner):
                        if c.startswith("language-"):
                            lang = c[len("language-"):]
                            break
                pre_depth[0] += 1
                try:
                    span(child, "MessageEntityPre", language=lang)
                finally:
                    pre_depth[0] -= 1

            elif tag == "blockquote":
                span(child, "MessageEntityBlockquote", collapsed=False)

            elif tag == "tg-spoiler" or (tag == "span" and ("spoiler" in cls or "tg-spoiler" in cls)):
                span(child, "MessageEntitySpoiler")

            else:
                walk(child)

            if child.tail:
                add(child.tail)

    walk(root)
    return strip_with_entities("".join(parts), entities)


# ═══════════════════════════════════════════════════════════════
# MEDIA
# ═══════════════════════════════════════════════════════════════
def _classify(rel, wrap_class, full, anchor_classes=(), export_label=""):
    """Classify export media using both wrapper/anchor markup and file extension."""
    rel = rel.replace("\\", "/")
    lower_rel = rel.casefold()
    classes = {str(c).casefold() for c in anchor_classes}
    is_sticker = (
        wrap_class == "sticker_wrap"
        or "sticker_wrap" in classes
        or "stickers/" in lower_rel
    )
    ext = os.path.splitext(lower_rel)[1]

    if is_sticker:
        if ext in (".webp", ".png"):
            return "sticker_static"
        if ext == ".tgs":
            return "sticker_animated"
        if ext == ".webm":
            return "sticker_video"
        return "sticker_unknown"

    if wrap_class == "roundvideo_file_wrap" or "round_video_messages" in lower_rel:
        return "round_video"
    if "voice_messages" in lower_rel:
        return "voice"
    if ext == ".gif":
        return "gif"
    if "video_files" in lower_rel or ext in (".mp4", ".mov", ".m4v", ".avi"):
        return "gif" if _is_gif(full, anchor_classes, export_label) else "video"
    # Telegram Desktop distinguishes actual photos by their photos/ export path
    # and/or photo-specific markup. Do not infer "photo" solely from .jpg/.png:
    # those extensions can also be ordinary files whose document form matters.
    photo_markup = any(
        c in {"photo", "photo_wrap", "photo_file_wrap"} or c.startswith("photo_")
        for c in classes
    )
    if "photos/" in lower_rel or photo_markup:
        return "photo"
    return "document"


def find_media(msg):
    results, seen = [], set()
    # The sticker_wrap class is sometimes on the link inside media_wrap rather
    # than on the wrapper itself, so _classify also checks anchor/image classes.
    for wrap_class in ("media_wrap", "sticker_wrap", "roundvideo_file_wrap"):
        wrap = _find_first(msg, wrap_class)
        if wrap is None:
            continue
        # One line per text node, so GIF/Animation labels can be matched as whole lines.
        export_label = "\n".join(t.strip() for t in wrap.itertext() if t and t.strip())
        found_in_wrapper = False
        for a in wrap.iter("a"):
            href = a.get("href")
            if not href:
                continue
            path = os.path.join(BASE_DIR, href)
            if os.path.exists(path) and path not in seen:
                seen.add(path)
                child_image_classes = [
                    cls for img in a.iter("img") for cls in _classes(img)
                ]
                results.append((path, _classify(
                    href, wrap_class, path,
                    _classes(wrap) + _classes(a) + child_image_classes,
                    export_label,
                )))
                found_in_wrapper = True
        if not found_in_wrapper:
            for img in wrap.iter("img"):
                src = img.get("src")
                if not src:
                    continue
                path = os.path.join(BASE_DIR, src)
                if os.path.exists(path) and path not in seen:
                    seen.add(path)
                    results.append((path, _classify(
                        src, wrap_class, path, _classes(wrap) + _classes(img), export_label
                    )))
    return results


def media_note(msg_div):
    wrap = _find_first(msg_div, "media_wrap")
    if wrap is None:
        return ""
    lines = [t.strip() for t in wrap.itertext() if t and t.strip()]
    if not lines:
        return ""
    note = " | ".join(lines)
    return "[" + (note[:500] + "..." if len(note) > 500 else note) + "]"


# ═══════════════════════════════════════════════════════════════
# OVERSIZE FILES  (stop cleanly, hand the file to the user)
# ═══════════════════════════════════════════════════════════════
def _max_upload_bytes():
    """The size limit in bytes, or 0 when the check is disabled."""
    return int(MAX_UPLOAD_MB * 1024 * 1024) if MAX_UPLOAD_MB else 0


def _mb(n_bytes):
    return f"{n_bytes / (1024 * 1024):.1f} MB"


def find_oversize_media(media):
    """[(path, kind, size_bytes)] for every file bigger than the limit (strictly greater)."""
    limit = _max_upload_bytes()
    if not limit:
        return []
    out = []
    for path, kind in media:
        try:
            size = os.path.getsize(path)
        except OSError:
            continue                      # unreadable here: the upload itself will report it
        if size > limit:
            out.append((path, kind, size))
    return out


def load_manual_uploads():
    """Source message ids you handled by hand, from MANUAL_UPLOADS_FILE.

    One id per line, optionally followed by the id of the message you posted in
    the channel (so later replies can link to it):
        1234
        1234 5678        # the second number is the destination message id
    Blank lines and anything after '#' are ignored.
    Returns (set_of_source_ids, {source_id: destination_id}).
    """
    done, mapped = set(), {}
    if not os.path.exists(MANUAL_UPLOADS_FILE):
        return done, mapped
    with open(MANUAL_UPLOADS_FILE, encoding="utf-8") as f:
        for raw in f:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.replace(",", " ").split()
            src = re.sub(r"^message", "", parts[0], flags=re.IGNORECASE)
            if not src.isdigit():
                print(f"Warning: ignoring unreadable line in {os.path.basename(MANUAL_UPLOADS_FILE)}: {raw.strip()!r}")
                continue
            done.add(src)
            if len(parts) > 1 and parts[1].isdigit():
                mapped[src] = int(parts[1])
    return done, mapped


def mark_manual_upload(source_id, rel_files):
    """Append `source_id` to MANUAL_UPLOADS_FILE (once). True if it is recorded afterwards.

    The line is "<id>    # comment"; load_manual_uploads() ignores the comment, and you can
    write the channel message id of your upload after the number to link replies to it.
    """
    try:
        done, _mapped = load_manual_uploads()
        if str(source_id) in done:
            return True
        existing = ""
        if os.path.exists(MANUAL_UPLOADS_FILE):
            with open(MANUAL_UPLOADS_FILE, encoding="utf-8") as f:
                existing = f.read()
        lead = "" if (not existing or existing.endswith("\n")) else "\n"
        header = "" if existing else (
            "# Source message ids you uploaded by hand (the script skips them).\n"
            "# Format:  <id>   or   <id> <channel message id of your upload>   # comment\n")
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        files = ", ".join(rel_files)
        with open(MANUAL_UPLOADS_FILE, "a", encoding="utf-8") as f:
            f.write(f"{lead}{header}{source_id}    # auto-added {stamp} after an oversize stop: upload {files} by hand\n")
            f.flush()
        return True
    except Exception as exc:
        print(f"Warning: could not write {MANUAL_UPLOADS_FILE}: {type(exc).__name__}: {exc}")
        return False


def format_oversize_instructions(source_id, data, caption, oversize, last_dest_id, migrated, auto_marked=False):
    big = {os.path.abspath(p) for p, _k, _s in oversize}
    ts = data.get("timestamp")
    when = ts.strftime("%Y-%m-%d %H:%M") if ts else "unknown time"
    who = data.get("sender") or "unknown sender"
    lines = [
        "",
        "═" * 72,
        f"STOPPED: message id={source_id} has a file over the {_mb(_max_upload_bytes())} upload limit.",
        "═" * 72,
        "None of this message's files were uploaded by the script. It is logged in:",
        f"    {FAILED_FILE}",
        "",
        f"Upload these file(s) to the channel by hand (original message: {when}, {who}):",
    ]
    for path, kind in data["media"]:
        ap = os.path.abspath(path)
        try:
            size = _mb(os.path.getsize(path))
        except OSError:
            size = "size unknown"
        lines.append(f"    [{kind}] {ap}  ({size}){'   <-- TOO LARGE' if ap in big else ''}")
    if caption and caption.strip():
        shown = caption if len(caption) <= 1500 else caption[:1500] + "…"
        lines += ["", "Caption to paste with it:"] + ["    | " + ln for ln in shown.splitlines()]
    where = (f"after the {migrated} message(s) already migrated"
             + (f" (the last one is message #{last_dest_id} in the channel)" if last_dest_id else ""))
    if auto_marked:
        lines += [
            "",
            "To resume:",
            f"  1. Upload the file(s) now, BEFORE running the script again, so they land {where}",
            "     and ahead of the messages that follow.",
            f"  2. Nothing to edit: message {source_id} is already recorded in",
            f"         {MANUAL_UPLOADS_FILE}",
            "     so the next run skips it and continues with the following message.",
            f"     (Optional: write the channel message id of your upload after the number, e.g.",
            f"      \"{source_id} 5678\", so replies to this message link to it. To undo, delete the line.)",
            "  3. Run the script again.",
            "",
            "WARNING: if you run the script again BEFORE uploading, this message will be skipped.",
        ]
    else:
        lines += [
            "",
            "To resume:",
            f"  1. Upload the file(s) now, BEFORE running the script again, so they land {where}",
            "     and ahead of the messages that follow.",
            f"  2. Add this line to {MANUAL_UPLOADS_FILE}:",
            f"         {source_id}",
            "     (optionally add the channel message id of your upload after it, e.g.",
            f"      \"{source_id} 5678\", so replies to this message link to it).",
            "  3. Run the script again. It continues with the next message.",
        ]
    lines += [
        "",
        f"All progress is saved ({migrated} message(s) migrated so far).",
        "═" * 72,
    ]
    return "\n".join(lines)


def halt_for_oversize(source_id, data, caption, oversize, id_map, ctx):
    """Log the message id, print the manual-upload instructions and request a clean stop."""
    reason = "FILE_TOO_LARGE: " + "; ".join(
        f"{os.path.relpath(p, BASE_DIR)} ({_mb(sz)} > {_mb(_max_upload_bytes())} limit)"
        for p, _k, sz in oversize
    )
    log_failure(source_id, reason)
    last_dest = ctx.get("last_dest_id") or (max(id_map.values()) if id_map else None)
    auto = False
    if AUTO_MARK_MANUAL_ON_STOP:
        auto = mark_manual_upload(source_id, [os.path.relpath(p, BASE_DIR) for p, _k in data["media"]])
    print(format_oversize_instructions(source_id, data, caption, oversize, last_dest, len(id_map), auto_marked=auto))
    ctx["halt"].append(source_id)
    ctx["stop"] = True


def print_oversize_dry_run(source_id, data, oversize):
    for path, kind, size in oversize:
        print(f"  [dry] ⚠ id={source_id:<6} OVERSIZE [{kind}] {os.path.abspath(path)} ({_mb(size)})"
              f" - a real run stops here")


# ═══════════════════════════════════════════════════════════════
# FORWARD SOURCE EXTRACTION
# ═══════════════════════════════════════════════════════════════
def _forward_name(fwd_div):
    if fwd_div is None:
        return ""
    a = next(fwd_div.iter("a"), None)
    if a is not None and _itertext(a):
        return _itertext(a)
    return _text_without(_find_first(fwd_div, "from_name"), "date")


def extract_forward_source(msg_div):
    fwd_div = _find_first(msg_div, "forwarded")
    if fwd_div is None:
        return None

    name = _forward_name(fwd_div)
    author_a = next(fwd_div.iter("a"), None)
    href = (author_a.get("href") if author_a is not None else "") or ""

    if href.startswith("https://t.me/") or href.startswith("http://t.me/"):
        m = re.match(r"https?://t\.me/(?:c/)?([^/?#]+)/(\d+)", href)
        if m and not href.startswith(("https://t.me/c/", "http://t.me/c/")):
            return (m.group(1), int(m.group(2)))
        return ("__text__", name)

    if href.startswith("tg://openmessage"):
        qs = parse_qs(urlparse(href).query)
        uid = qs.get("user_id", [None])[0]
        mid = qs.get("message_id", [None])[0]
        if uid and mid and mid.isdigit():
            return (str(uid), int(mid))
        return ("__text__", name)

    return ("__text__", name)


# ═══════════════════════════════════════════════════════════════
# PROGRESS
# ═══════════════════════════════════════════════════════════════
def load_id_map():
    out = {}
    if not os.path.exists(IDMAP_FILE):
        return out
    with open(IDMAP_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                out[str(r["src"])] = int(r["dst"])
            except Exception:
                continue
    return out


def append_id_map(src, dst):
    with open(IDMAP_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"src": str(src), "dst": int(dst)}) + "\n")
        f.flush()


def load_partial():
    """Load per-message partial-send state from PARTIAL_FILE.

    The return value is a dict-of-dicts: {src: state_dict}. Records written
    by earlier versions ({"src": ..., "text_id": N}) are upgraded on load to
    {"text_id": N}. For every src the last record wins, so an interrupted
    run resumes exactly where it stopped, at the granularity of the smallest
    unit that was persisted (one text chunk, one album item, one media item,
    or one native forward).
    """
    out = {}
    if not os.path.exists(PARTIAL_FILE):
        return out
    with open(PARTIAL_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except Exception:
                continue
            src = record.get("src")
            if src is None:
                continue
            src = str(src)
            merged = out.get(src, {})
            if isinstance(record.get("state"), dict):
                merged.update(record["state"])
            elif "text_id" in record:
                # Legacy single-field record from an earlier version.
                try:
                    merged["text_id"] = int(record["text_id"])
                except (TypeError, ValueError):
                    continue
            out[src] = merged
    return out


def save_state(src, state):
    """Append a full snapshot of this message's send state to PARTIAL_FILE.

    The file is append-only; the last record for a given `src` is the
    authoritative one on reload. Persisting the whole state (not just
    text_id) is what lets the migration resume after a crash anywhere
    inside a multi-step send without re-posting text, media, album items,
    or a native forward.
    """
    snapshot = {
        key: value
        for key, value in state.items()
        if key not in ("src", "force_reconstruction")
    }
    try:
        payload = json.loads(json.dumps(snapshot, ensure_ascii=False, default=int))
    except (TypeError, ValueError) as exc:
        print(f"Warning: could not serialise send state for src={src}: {exc}")
        return
    try:
        with open(PARTIAL_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"src": str(src), "state": payload}) + "\n")
            f.flush()
    except Exception as exc:
        print(f"Warning: could not persist send state for src={src}: "
              f"{type(exc).__name__}: {exc}")


def log_failure(msg_id, reason):
    reason = " ".join(str(reason).split())
    with open(FAILED_FILE, "a", encoding="utf-8") as f:
        f.write(f"{msg_id}\t{reason}\n")
        f.flush()


def export_files():
    if not os.path.isdir(BASE_DIR):
        return []
    pat = re.compile(HTML_FILE_PATTERN)
    found = []
    for name in os.listdir(BASE_DIR):
        m = pat.match(name)
        if m:
            found.append((int(m.group(1) or 1), os.path.join(BASE_DIR, name)))
    return [p for _, p in sorted(found)]


def collect_source_ids(files):
    """One cheap regex pass over every HTML page: all real message ids.

    The number of ids is also the message count, so no separate counting pass
    is needed. Service messages (id="message-N") never match.
    """
    out = set()
    id_re = re.compile(rb'''id=["']message(\d+)["']''')
    for path in files:
        carry = b""
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                data = carry + chunk
                for value in id_re.findall(data):
                    out.add(value.decode("ascii", errors="ignore"))
                carry = data[-128:]
    return out


def _chat_message_candidates(root):
    """Find plausible message arrays in Telegram Desktop JSON export layouts."""
    candidates = []
    if isinstance(root, dict) and isinstance(root.get("messages"), list):
        candidates.append({"name": root.get("name") or root.get("title") or "", "messages": root["messages"], "direct": True})
    chats = root.get("chats") if isinstance(root, dict) else None
    if isinstance(chats, dict):
        chats = chats.get("list")
    if isinstance(chats, list):
        for chat in chats:
            if isinstance(chat, dict) and isinstance(chat.get("messages"), list):
                candidates.append({"name": chat.get("name") or chat.get("title") or "", "messages": chat["messages"], "direct": False})
    return candidates


def load_json_reactions(files, source_ids):
    """Load reaction fields from companion result.json/messages.json when present.

    The preferred path streams single-chat `messages.item` arrays via ijson. For
    other layouts it uses a bounded-size JSON fallback and chooses the chat with
    the strongest ID overlap with the HTML export.
    """
    for filename in JSON_REACTION_FILE_CANDIDATES:
        path = os.path.join(BASE_DIR, filename)
        if not os.path.isfile(path):
            continue
        found_any = False
        streamed = {}
        if ijson is not None:
            try:
                with open(path, "rb") as f:
                    for row in ijson.items(f, "messages.item"):
                        found_any = True
                        if not isinstance(row, dict):
                            continue
                        mid = row.get("id")
                        if mid is None:
                            continue
                        mid = str(mid)
                        if mid not in source_ids:
                            continue
                        raw = row.get("reactions", row.get("reaction_counts", row.get("reactions_count")))
                        normalized = normalize_reactions(raw)
                        if normalized:
                            streamed[mid] = normalized
                if found_any and streamed:
                    print(f"Loaded reaction metadata from {filename} ({len(streamed)} message(s)).")
                    return streamed
                if found_any:
                    print(f"Found message rows in {filename}, but no matching reaction data in the streaming pass; checking its export layout.")
            except Exception as exc:
                print(f"Warning: streaming reaction read from {filename} failed: {exc}")

        try:
            size_mb = os.path.getsize(path) / (1024 * 1024)
            if size_mb > MAX_JSON_FALLBACK_MB:
                print(f"Warning: {filename} is {size_mb:.0f} MB and is not a simple streaming chat JSON; skipping JSON reaction import to avoid excessive memory use.")
                continue
            with open(path, "r", encoding="utf-8") as f:
                root = json.load(f)
            candidates = _chat_message_candidates(root)
            if not candidates:
                print(f"Warning: no recognized message array found in {filename}; reaction import skipped.")
                continue
            ranked = []
            for candidate in candidates:
                rows = candidate["messages"]
                ids = {str(m.get("id")) for m in rows if isinstance(m, dict) and m.get("id") is not None}
                overlap = len(ids & source_ids)
                ranked.append((overlap, candidate))
            ranked.sort(key=lambda pair: pair[0], reverse=True)
            if not ranked or ranked[0][0] == 0:
                print(f"Warning: no message IDs from {filename} match the HTML export.")
                continue
            if len(ranked) > 1 and ranked[0][0] == ranked[1][0] and not ranked[0][1].get("direct"):
                print(f"Warning: multiple chats in {filename} match the HTML IDs equally; not guessing which chat's reactions to use.")
                continue
            selected = ranked[0][1]
            reaction_map = {}
            for row in selected["messages"]:
                if not isinstance(row, dict) or row.get("id") is None:
                    continue
                mid = str(row["id"])
                if mid not in source_ids:
                    continue
                raw = row.get("reactions", row.get("reaction_counts", row.get("reactions_count")))
                normalized = normalize_reactions(raw)
                if normalized:
                    reaction_map[mid] = normalized
            print(f"Loaded reaction metadata from {filename} ({len(reaction_map)} message(s), chat {selected.get('name')!r}).")
            return reaction_map
        except Exception as exc:
            print(f"Warning: could not read reaction metadata from {filename}: {type(exc).__name__}: {exc}")
    return {}


def load_pending_replies():
    if not os.path.exists(PENDING_REPLIES_FILE):
        return {}
    try:
        with open(PENDING_REPLIES_FILE, "r", encoding="utf-8") as f:
            obj = json.load(f)
        return {str(k): str(v) for k, v in obj.items()} if isinstance(obj, dict) else {}
    except Exception as exc:
        print(f"Warning: could not read pending replies file: {exc}")
        return {}


def save_pending_replies(pending):
    """Atomically save deferred replies without forcing a slow Drive fsync.

    Persistence failures are reported but not raised into the message-send path:
    once Telegram has accepted a message, a local sidecar-write failure must not
    cause that successful send to be logged/retried as a failed upload.
    """
    tmp = PENDING_REPLIES_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(pending, f, ensure_ascii=False, separators=(",", ":"))
            f.write("\n")
            f.flush()
        os.replace(tmp, PENDING_REPLIES_FILE)
        return True
    except Exception as exc:
        print(f"Warning: could not persist deferred-reply state: {type(exc).__name__}: {exc}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def _append_jsonl(path, record):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        f.flush()


def write_reply_audit(records):
    """Rewrite the reply audit from this run's records (no duplicate lines across runs)."""
    tmp = REPLY_AUDIT_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            f.flush()
        os.replace(tmp, REPLY_AUDIT_FILE)
        return True
    except Exception as exc:
        print(f"Warning: could not write the reply audit: {type(exc).__name__}: {exc}")
        return False


def load_reaction_note_map():
    out = {}
    if not os.path.exists(REACTION_NOTE_MAP_FILE):
        return out
    with open(REACTION_NOTE_MAP_FILE, encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
                out[str(row["src"])] = int(row["note_id"])
            except Exception:
                continue
    return out


def format_reaction_archive_note(reactions):
    pieces = []
    for reaction in reactions:
        label = str(reaction.get("label") or "reaction").strip()
        count = reaction.get("count")
        piece = label + (f" × {count}" if count is not None else "")
        actors = reaction.get("actors") or []
        if actors:
            piece += " (listed actors: " + ", ".join(str(a) for a in actors[:20]) + ")"
            if len(actors) > 20:
                piece += f" and {len(actors) - 20} more"
        pieces.append(piece)
    if not pieces:
        return ""
    return "Archived reaction metadata from the source export (not live Telegram reaction totals):\n" + "\n".join(pieces)


def _reply_id_from_message(msg):
    reply_header = getattr(msg, "reply_to", None) if msg is not None else None
    actual = getattr(reply_header, "reply_to_msg_id", None) if reply_header is not None else None
    if actual is None and msg is not None:
        actual = getattr(msg, "reply_to_msg_id", None)
    return actual


async def verify_all_reply_links(client, channel, source_reply_map, id_map, audit):
    """Verify every available source reply, batching message reads. Results go into `audit`."""
    if not VERIFY_REPLIES or not source_reply_map:
        return

    candidates = []
    for source_id, source_target in source_reply_map.items():
        source_id, source_target = str(source_id), str(source_target)
        if source_id not in id_map:
            continue
        expected = id_map.get(source_target)
        if expected is None:
            audit.append({
                "src": source_id,
                "source_reply_to": source_target,
                "dst": int(id_map[source_id]),
                "verified": False,
                "status": "target_not_migrated",
            })
        else:
            candidates.append((source_id, source_target, int(id_map[source_id]), int(expected)))

    print(f"Verifying reply links for {len(candidates)} migrated source message(s)...")
    # Telegram's GetMessages request accepts multiple IDs; use batches to avoid
    # making one network round-trip per reply in a large archive.
    batch_size = 100
    for offset in range(0, len(candidates), batch_size):
        batch = candidates[offset:offset + batch_size]
        dest_ids = [row[2] for row in batch]
        try:
            received = await client.get_messages(channel, ids=dest_ids)
            if received is None:
                received = []
            elif not isinstance(received, (list, tuple)):
                received = [received]
            by_id = {int(m.id): m for m in received if m is not None and getattr(m, "id", None) is not None}
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            for source_id, source_target, dest_id, expected in batch:
                audit.append({
                    "src": source_id,
                    "source_reply_to": source_target,
                    "dst": dest_id,
                    "expected_destination_reply_to": expected,
                    "verified": False,
                    "status": "verification_unavailable",
                    "error": reason,
                })
            print(f"  Warning: could not verify a batch of {len(batch)} replies: {reason}")
            continue

        for source_id, source_target, dest_id, expected in batch:
            msg = by_id.get(dest_id)
            actual = _reply_id_from_message(msg)
            verified = actual == expected
            audit.append({
                "src": source_id,
                "source_reply_to": source_target,
                "dst": dest_id,
                "expected_destination_reply_to": expected,
                "actual_destination_reply_to": actual,
                "verified": verified,
                "status": "verified" if verified else ("message_not_found" if msg is None else "mismatch"),
            })
            if not verified:
                print(f"  WARNING: reply id={source_id} expected destination target {expected}, got {actual}")
        await asyncio.sleep(0.1)


def persist_reaction_source_metadata(reaction_map):
    """Write normalized export reaction details to a deduplicated JSONL sidecar."""
    if not reaction_map:
        return
    seen = set()
    if os.path.exists(REACTION_SOURCE_FILE):
        with open(REACTION_SOURCE_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                    if row.get("record_type") == "source_reactions":
                        seen.add(str(row.get("src")))
                except Exception:
                    continue
    for src, reactions in reaction_map.items():
        if str(src) in seen:
            continue
        _append_jsonl(REACTION_SOURCE_FILE, {
            "record_type": "source_reactions",
            "src": str(src),
            "reactions": reactions,
        })


async def restore_reaction_archive_notes(client, channel, id_map, reaction_map, note_map, stats):
    if not PRESERVE_REACTIONS_AS_ANNOTATIONS or not reaction_map:
        return
    print(f"Archiving reactions for {len(reaction_map)} source message(s)...")
    for source_id, reactions in reaction_map.items():
        if source_id not in id_map or source_id in note_map:
            continue
        note_text = format_reaction_archive_note(reactions)
        if not note_text:
            continue
        try:
            # Sanitise and disable markdown parsing: a custom reaction label may
            # contain * or _, which Telethon would otherwise treat as formatting.
            sent = await client.send_message(
                channel, sanitize_text(note_text),
                reply_to=id_map[source_id], parse_mode=None, link_preview=False,
            )
            note_id = int(sent.id)
            note_map[source_id] = note_id
            _append_jsonl(REACTION_NOTE_MAP_FILE, {"src": str(source_id), "note_id": note_id})
            _append_jsonl(REACTION_ARCHIVE_FILE, {
                "src": str(source_id),
                "destination_message_id": int(id_map[source_id]),
                "reaction_note_id": note_id,
                "reactions": reactions,
            })
            stats["reaction_notes"] = stats.get("reaction_notes", 0) + 1
            print(f"  reaction metadata archived for source id={source_id} → note {note_id}")
            await asyncio.sleep(SEND_DELAY_SECONDS)
        except FloodWaitError as fw:
            print(f"  FloodWait while archiving reactions: {fw.seconds}s; stopping reaction annotation pass for this run.")
            await asyncio.sleep(fw.seconds + 5)
            break
        except Exception as exc:
            _append_jsonl(REACTION_ARCHIVE_FILE, {
                "src": str(source_id), "destination_message_id": int(id_map[source_id]),
                "status": "note_failed", "error": f"{type(exc).__name__}: {exc}", "reactions": reactions,
            })
            print(f"  reaction-note failed for source id={source_id}: {type(exc).__name__}: {exc}")


# ═══════════════════════════════════════════════════════════════
# MESSAGE EXTRACTION
# ═══════════════════════════════════════════════════════════════
def _extract_html_reactions(msg_div):
    """Best-effort extraction for reaction markup, if a particular export has it."""
    found = []
    seen = set()
    for el in msg_div.iter():
        if not isinstance(el.tag, str):
            continue
        classes = {c.casefold() for c in _classes(el)}
        if not any("reaction" in c for c in classes):
            continue
        # Skip container nodes; their children carry the individual emoji/count.
        if any(c in {"reactions", "message_reactions", "reactions_wrap", "reaction_wrap", "reactions-container"}
               for c in classes):
            continue

        raw_attrs = {
            k: v for k, v in el.attrib.items()
            if k.casefold().startswith("data-") or k.casefold() in ("title", "aria-label", "alt")
        }
        label = (el.get("data-emoji") or el.get("data-reaction") or el.get("emoji") or
                 el.get("aria-label") or el.get("title") or "").strip()
        if not label:
            for img in el.iter("img"):
                label = (img.get("alt") or img.get("title") or img.get("data-emoji") or "").strip()
                if label:
                    break
        text = _itertext(el)
        count = None
        for attr in ("data-count", "data-reaction-count", "count"):
            value = el.get(attr)
            if value and str(value).isdigit():
                count = int(value)
                break
        if count is None:
            match = re.search(r"(?:^|\s)(\d+)\s*$", text)
            if match:
                count = int(match.group(1))
        if not label:
            label = re.sub(r"\s*\d+\s*$", "", text).strip()
        if not label or label.isdigit():
            continue
        key = (label, count)
        if key in seen:
            continue
        seen.add(key)
        found.append({"label": label, "count": count, "actors": [], "raw": raw_attrs, "source": "html"})
    return found


def _reaction_actor_names(value):
    if not isinstance(value, list):
        return []
    out = []
    for actor in value:
        if isinstance(actor, str):
            name = actor.strip()
        elif isinstance(actor, dict):
            name = str(actor.get("name") or actor.get("from") or actor.get("title") or
                       actor.get("username") or actor.get("from_id") or actor.get("id") or "").strip()
        else:
            name = str(actor).strip()
        if name and name not in out:
            out.append(name)
    return out


def normalize_reactions(raw):
    """Normalize common Telegram Desktop JSON reaction shapes without losing raw data."""
    if raw is None:
        return []
    if isinstance(raw, dict) and "label" in raw and "count" in raw:
        item = dict(raw)
        item.setdefault("actors", [])
        item.setdefault("source", "export")
        return [item]
    if isinstance(raw, list):
        out = []
        for item in raw:
            out.extend(normalize_reactions(item))
        return out
    if isinstance(raw, str):
        value = raw.strip()
        return [{"label": value, "count": None, "actors": [], "raw": raw, "source": "json"}] if value else []
    if not isinstance(raw, dict):
        return []

    # Nested containers seen in different exporter versions.
    for container_key in ("results", "reactions", "reaction_counts", "items", "list"):
        if container_key in raw and isinstance(raw[container_key], (list, dict)):
            nested = normalize_reactions(raw[container_key])
            if nested:
                return nested

    label = (raw.get("emoji") or raw.get("emoticon") or raw.get("label") or
             raw.get("reaction") or raw.get("reaction_emoji") or "")
    reaction_type = str(raw.get("type") or raw.get("kind") or "")
    custom_id = raw.get("document_id", raw.get("custom_emoji_id", raw.get("emoji_id")))
    if not label and custom_id is not None:
        label = f"custom emoji ID {custom_id}"
    if isinstance(label, dict):
        label = label.get("emoji") or label.get("emoticon") or label.get("label") or ""
    label = str(label).strip() if label is not None else ""

    count = None
    for key in ("count", "total", "reaction_count", "number"):
        value = raw.get(key)
        if isinstance(value, int):
            count = value
            break
        if isinstance(value, str) and value.isdigit():
            count = int(value)
            break

    actors = _reaction_actor_names(raw.get("recent") or raw.get("actors") or
                                   raw.get("users") or raw.get("reactors"))
    if label:
        return [{
            "label": label,
            "count": count,
            "actors": actors,
            "type": reaction_type or ("custom_emoji" if custom_id is not None else "emoji"),
            "raw": raw,
            "source": "json",
        }]

    # Some export tools encode reactions as {"👍": 12, "❤️": 4}.
    out = []
    for key, value in raw.items():
        if isinstance(value, (int, str)) and (isinstance(value, int) or str(value).isdigit()):
            out.append({"label": str(key), "count": int(value), "actors": [], "raw": {key: value}, "source": "json"})
    return out


def _reply_preview(reply_div):
    if reply_div is None:
        return ""
    text = _itertext(reply_div)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500]


def _extract_reply_source_id(msg_div):
    reply_div = _find_first(msg_div, "reply_to")
    if reply_div is None:
        return None
    a = next(reply_div.iter("a"), None)
    if a is None:
        return None
    match = re.search(r"#go_to_message(\d+)", a.get("href") or "")
    return match.group(1) if match else None


def extract_message_data(msg_div):
    classes = _classes(msg_div)
    if "message" not in classes or "service" in classes:
        return None

    raw_id = msg_div.get("id", "")
    if not raw_id.startswith("message"):
        return None
    msg_id = raw_id[len("message"):]
    if not msg_id.isdigit():
        return None

    body = _find_first(msg_div, "body")
    if body is None:
        body = msg_div

    date_div = None
    for el in body.iter():
        if el.tag == "div" and "date" in _classes(el) and el.get("title"):
            date_div = el
            break
    timestamp = parse_export_date(date_div.get("title")) if date_div is not None else None

    sender = ""
    if PRESERVE_SENDER_NAMES:
        for child in body:
            if isinstance(child.tag, str) and "from_name" in _classes(child):
                sender = _text_without(child, "date")
                break

    fwd_div = _find_first(msg_div, "forwarded")
    forward_header = ""
    if fwd_div is not None:
        name = _forward_name(fwd_div)
        if name:
            forward_header = f"Forwarded from {name}\n\n"
    fwd_src = extract_forward_source(msg_div) if NATIVE_FORWARD else None

    reply_div = _find_first(msg_div, "reply_to")
    preview = _reply_preview(reply_div)
    reply_src_id = _extract_reply_source_id(msg_div)

    text_div = _find_first(msg_div, "text")
    body_text, body_entities = parse_rich_text(text_div)

    media = find_media(msg_div)
    note = "" if media else media_note(msg_div)
    if note:
        body_text = (body_text + "\n\n" if body_text else "") + note

    return {
        "msg_id": msg_id,
        "joined": "joined" in classes,
        "timestamp": timestamp,
        "sender": sender,
        "forward_header": forward_header,
        "forward_source": fwd_src,
        "reply_src_id": reply_src_id,
        "reply_preview": preview,
        "body_text": body_text,
        "body_entities": body_entities,
        "media": media,
        "media_note": note,
        "reactions": _extract_html_reactions(msg_div),
    }


# ═══════════════════════════════════════════════════════════════
# SENDING
# ═══════════════════════════════════════════════════════════════
_native_warned = [False]


async def resolve_mentions(client, entities):
    """Bots usually cannot resolve arbitrary users; such mentions stay plain text."""
    cls_in = getattr(tl, "InputMessageEntityMentionName", None)
    cls_out = getattr(tl, "MessageEntityMentionName", None)
    if cls_out is None:
        return entities
    out = []
    for e in entities:
        if isinstance(e, cls_out):
            if cls_in is None:
                continue
            try:
                inp = await client.get_input_entity(e.user_id)
                out.append(cls_in(e.offset, e.length, inp))
            except FloodWaitError:
                raise
            except Exception:
                pass
        else:
            out.append(e)
    return out


async def _try_native_forward(client, channel, peer_hint, source_msg_id):
    try:
        pid = int(peer_hint) if str(peer_hint).lstrip("-").isdigit() else peer_hint
        peer = await client.get_entity(pid)
        result = await client.forward_messages(channel, source_msg_id, from_peer=peer)
        msg = result[0] if isinstance(result, (list, tuple)) else result
        return msg.id
    except FloodWaitError:
        raise
    except Exception as exc:
        if not _native_warned[0]:
            _native_warned[0] = True
            print(f"    native forward not possible ({type(exc).__name__}: {exc}); "
                  f"using the textual header (further notices suppressed)")
        return None


# Source id of the message being sent (for diagnostics only).
_current_src = [None]


def _is_entity_error(exc):
    """True when Telegram/Telethon rejected the formatting entities of a message.

    Matches on the exception class name (all entity exceptions Telethon raises
    contain "Entit") and on Telegram's own wording. Deliberately does NOT match
    the bare word "entities" in the message, because unrelated exceptions might
    contain it and would then be routed into the formatting-fallback path.
    """
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    return ("entit" in name or "invalid bounds" in msg or "entity_bounds" in msg)


def note_formatting_dropped(src, text, entities, exc):
    """Record why formatting was dropped, so the real cause can be found."""
    try:
        _append_jsonl(FORMAT_DEBUG_FILE, {
            "src": src,
            "error": f"{type(exc).__name__}: {exc}",
            "text_utf16_len": u16len(text or ""),
            "text_repr": (text or "")[:400],
            "entities": [
                {"type": type(e).__name__, "offset": getattr(e, "offset", None),
                 "length": getattr(e, "length", None)}
                for e in (entities or [])[:60]
            ],
        })
    except Exception:
        pass
    print(f"    note: Telegram rejected the formatting of source id={src} "
          f"({type(exc).__name__}); sending it as plain text (details: {FORMAT_DEBUG_FILE})")


async def _with_formatting_fallback(send, kwargs, text, entities, src=None):
    """Run send(**kwargs); if the formatting is rejected, retry once without it.

    Formatting is cosmetic, the content is not: a message must not fail (and be
    retried five times) because of an entity problem. FloodWait is never
    swallowed here.
    """
    try:
        return await send(**kwargs)
    except FloodWaitError:
        raise
    except Exception as exc:
        if entities and (_is_entity_error(exc) or isinstance(exc, (TypeError, ValueError))):
            note_formatting_dropped(src, text, entities, exc)
            retry = dict(kwargs)
            retry["formatting_entities"] = None
            return await send(**retry)
        raise


async def _send_text_chunks(client, channel, chunks, reply_to, state, prog_key, persist=True):
    """Send each text chunk in order, remembering progress.

    Takes the whole `state` so each successful chunk can be checkpointed to
    disk (when `persist` is true). A crash between chunks then resumes at the
    next unsent chunk rather than replaying from the top. Single-chunk sends
    pass persist=False: there is nothing to resume in the middle of.
    """
    prog = state.setdefault(prog_key, {})
    # If a persisted run used different chunking, `done` may not correspond to
    # any real chunk boundary in this run. Reset it whenever it looks stale.
    done = prog.get("done", 0)
    if done > len(chunks):
        print(f"    note: resetting stale {prog_key}.done={done} for src={state.get('src')} (current chunk count {len(chunks)})")
        prog.clear()
    for i, (text, ents) in enumerate(chunks):
        if i < prog.get("done", 0):
            continue
        # Enforce Telegram's entity-bounds invariants before every send.
        ents = clean_invalid_entities(text, ents)
        # parse_mode=None: our text is final; Telethon must not re-parse it as markdown.
        m = await _with_formatting_fallback(
            lambda **kw: client.send_message(channel, text, **kw),
            {"formatting_entities": ents or None,
             "reply_to": prog.get("last", reply_to),
             "parse_mode": None},
            text, ents, _current_src[0],
        )
        prog.setdefault("first", m.id)
        prog["last"] = m.id
        prog["done"] = i + 1
        src = state.get("src")
        if persist and src is not None:
            save_state(src, state)
    return prog["first"]


def _sticker_upload_options(path, kind):
    """Build Telethon options that mark a file as a Telegram sticker."""
    ext = os.path.splitext(path)[1].casefold()
    sticker_attr_cls = getattr(tl, "DocumentAttributeSticker", None)
    empty_set_cls = getattr(tl, "InputStickerSetEmpty", None)
    if sticker_attr_cls is None or empty_set_cls is None:
        raise RuntimeError(
            "This Telethon version lacks sticker upload attributes; upgrade Telethon."
        )

    attributes = [sticker_attr_cls(alt="🙂", stickerset=empty_set_cls())]
    options = {
        "force_document": True,
        "attributes": attributes,
    }

    if kind == "sticker_animated" or ext == ".tgs":
        options["mime_type"] = "application/x-tgsticker"
    elif kind == "sticker_video" or ext == ".webm":
        options["mime_type"] = "video/webm"
        metadata = _probe_video_metadata(path) or {}
        width = int(metadata.get("width") or 512)
        height = int(metadata.get("height") or 512)
        duration = float(metadata.get("duration") or 1.0)
        video_attr_cls = getattr(tl, "DocumentAttributeVideo", None)
        if video_attr_cls is None:
            raise RuntimeError(
                "This Telethon version lacks video attributes required for video stickers."
            )
        try:
            video_attr = video_attr_cls(
                duration=duration, w=width, h=height,
                supports_streaming=False, nosound=True,
            )
        except TypeError:
            # Compatibility with older Telethon schema layers that lack `nosound`.
            video_attr = video_attr_cls(
                duration=duration, w=width, h=height,
                supports_streaming=False,
            )
        attributes.append(video_attr)
        # Keep the upload running if a file is malformed or ffprobe cannot read it.
        if not metadata:
            print(f"    warning: could not read video metadata for sticker {os.path.basename(path)}; using 512x512 / 1s fallback")
        options["nosound_video"] = True
    elif ext == ".webp":
        options["mime_type"] = "image/webp"
    elif ext == ".png":
        options["mime_type"] = "image/png"
    else:
        # Unknown files from the stickers directory are still preserved as a
        # document instead of blocking the whole migration, but cannot be
        # guaranteed to display as a Telegram sticker.
        print(f"    warning: unknown sticker format {ext or '(no extension)'}; sending as a document")
        options.pop("attributes", None)
        options["mime_type"] = None

    return options


# send_file options that some Telethon builds do not accept. They are dropped
# (once, remembered) when the installed Telethon rejects them with a TypeError.
_OPTIONAL_SEND_KWARGS = ("mime_type", "nosound_video")
_UNSUPPORTED_SEND_KWARGS = set()


def configure_send_file_compat():
    """Look at the installed send_file() signature and note options it lacks. Never aborts.

    If the signature takes **kwargs (or cannot be read) nothing is assumed and the
    per-call TypeError fallback in _send_file() does the work.
    """
    try:
        params = inspect.signature(TelegramClient.send_file).parameters
    except (TypeError, ValueError, AttributeError):
        return
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return
    for name in _OPTIONAL_SEND_KWARGS:
        if name not in params:
            _UNSUPPORTED_SEND_KWARGS.add(name)
    if _UNSUPPORTED_SEND_KWARGS:
        print("Note: this Telethon's send_file() lacks: " + ", ".join(sorted(_UNSUPPORTED_SEND_KWARGS))
              + ". Those options will be skipped (upgrade Telethon for best GIF/sticker fidelity).")


async def _send_file(client, channel, files, **kwargs):
    """client.send_file() that tolerates optional keywords an older Telethon rejects."""
    for key in list(kwargs):
        if key in _UNSUPPORTED_SEND_KWARGS:
            kwargs.pop(key)
    while True:
        try:
            return await client.send_file(channel, files, **kwargs)
        except TypeError as exc:
            m = re.search(r"unexpected keyword argument '(\w+)'", str(exc))
            bad = m.group(1) if m else None
            if bad in _OPTIONAL_SEND_KWARGS and bad in kwargs:
                _UNSUPPORTED_SEND_KWARGS.add(bad)
                kwargs.pop(bad)
                print(f"    note: this Telethon does not accept send_file({bad}=...); skipping that option from now on")
                continue
            raise


async def _send_media_item(client, channel, path, kind, reply_to,
                           caption="", entities=None):
    """Send one media item with format-specific GIF/sticker handling."""
    caption = caption or ""
    # Enforce Telegram's entity-bounds invariants on captions too.
    entities = clean_invalid_entities(caption, entities)
    kwargs = {
        "reply_to": reply_to,
        "caption": caption,
        "formatting_entities": entities or None,
        "parse_mode": None,          # our caption is final; do not re-parse it as markdown
    }

    if kind in STICKER_KINDS:
        sticker_options = _sticker_upload_options(path, kind)
        kwargs.update(sticker_options)
    elif kind == "gif":
        # Telegram GIFs are documents marked with DocumentAttributeAnimated.
        # For exported silent MP4 animations, nosound_video=False asks Telegram
        # to display it as a GIF. For actual .gif files, the Animated attribute
        # asks Telegram to convert the uploaded GIF to its native MP4 form.
        kwargs["force_document"] = True
        kwargs["attributes"] = [tl.DocumentAttributeAnimated()]
        ext = os.path.splitext(path)[1].casefold()
        if ext == ".gif":
            kwargs["mime_type"] = "image/gif"
        else:
            kwargs["mime_type"] = "video/mp4"
            kwargs["nosound_video"] = False
    elif kind == "video":
        # Only a video that really has no audio track needs the "this is a video,
        # not a GIF" flag. Videos with sound (or when ffprobe cannot tell) are
        # sent without it, so no file is mislabelled as silent.
        if _video_has_audio(path) is False:
            kwargs["nosound_video"] = True
    elif kind == "voice":
        kwargs["voice_note"] = True
    elif kind == "round_video":
        kwargs["video_note"] = True
    elif kind == "document":
        # Telethon auto-detects .jpg/.png files as photos by extension unless
        # force_document is set. For files/ attachments, preserve the original
        # document/file presentation rather than uploading a compressed photo.
        kwargs["force_document"] = True

    sent = await _with_formatting_fallback(
        lambda **kw: _send_file(client, channel, path, **kw),
        kwargs, caption, entities, _current_src[0],
    )
    return sent[0] if isinstance(sent, (list, tuple)) else sent


def _album_compatible(media):
    """Telegram albums hold photos/videos together, or documents together. Nothing else."""
    kinds = {kind for _path, kind in media}
    if kinds <= {"photo", "video"}:
        return True
    if kinds == {"document"}:
        return True
    return False


async def _do_send(client, channel, chunks, media, reply_to, forward_source,
                   state, annotation):
    if (forward_source is not None and NATIVE_FORWARD
            and reply_to is None and not state.get("force_reconstruction")):
        new_id = state.get("native_id")
        if new_id is None and not state.get("fwd_tried"):
            peer_hint, src_id = forward_source
            if peer_hint != "__text__" and isinstance(src_id, int):
                new_id = await _try_native_forward(client, channel, peer_hint, src_id)
            if new_id is not None:
                state["native"] = True
                state["native_id"] = int(new_id)
                save_state(state["src"], state)
            else:
                state["fwd_tried"] = True
                save_state(state["src"], state)
        if new_id is not None:
            state["native"] = True
            state["native_id"] = int(new_id)
            if FORWARD_ANNOTATE and annotation:
                await _send_text_chunks(client, channel, annotation, new_id,
                                        state, "ann_prog")
            return new_id

    first_text, first_ents = chunks[0]
    rest = chunks[1:]

    if len(media) > 1:
        # GIFs and stickers cannot be reliably sent as ordinary media albums.
        # Send such groups item-by-item. If a sticker is present, preserve the
        # timestamp/sender/body as a text message first because stickers do not
        # support captions.
        has_sticker = any(kind in STICKER_KINDS for _path, kind in media)
        # File attachments with image extensions must bypass album auto-detection:
        # force_document is a per-upload option, not a per-item option for a mixed
        # album, so send these individually to preserve their document form.
        image_file_extensions = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
        has_document_image = any(
            kind == "document" and os.path.splitext(path)[1].casefold() in image_file_extensions
            for path, kind in media
        )
        individual_media = (
            has_sticker or has_document_image
            or any(kind in ("gif", "voice", "round_video") for _path, kind in media)
            or not _album_compatible(media)       # e.g. documents mixed with photos/videos
        )

        if individual_media:
            needs_prefix_text = has_sticker or media[0][1] in NO_CAPTION_KINDS
            if needs_prefix_text:
                if "prefix_text_id" not in state:
                    # Reuse a previously persisted text ID after restarting the
                    # migration rather than sending the archival text twice.
                    saved_text_id = state.get("text_id")
                    if saved_text_id is not None:
                        state["prefix_text_id"] = saved_text_id
                        save_state(state["src"], state)
                    elif any(t.strip() for t, _ in chunks):
                        text_id = await _send_text_chunks(
                            client, channel, chunks, reply_to,
                            state, "prefix_prog",
                        )
                        state["prefix_text_id"] = text_id
                        state["text_id"] = text_id
                        save_state(state["src"], state)
                media_reply_to = state.get("prefix_text_id") or reply_to
                caption_first = False
            else:
                media_reply_to = reply_to
                caption_first = True

            state.setdefault("multi_media_ids", [])
            while len(state["multi_media_ids"]) < len(media):
                index = len(state["multi_media_ids"])
                path_i, kind_i = media[index]
                caption_i = first_text if (index == 0 and caption_first) else ""
                entities_i = first_ents if (index == 0 and caption_first) else None
                sent_msg = await _send_media_item(
                    client, channel, path_i, kind_i, media_reply_to,
                    caption=caption_i, entities=entities_i,
                )
                state["multi_media_ids"].append(sent_msg.id)
                save_state(state["src"], state)
            state["album_id"] = state["multi_media_ids"][0]
            save_state(state["src"], state)
        elif "album_id" not in state:
            files = [m[0] for m in media]
            captions = [first_text] + [""] * (len(files) - 1)
            # Telethon's album path may ignore formatting_entities (it can parse
            # the captions itself), so parse_mode=None keeps our text literal
            # either way. Formatting is tried once; if Telegram rejects it the
            # album is resent without it. FloodWait still propagates.
            safe_first_ents = clean_invalid_entities(first_text, first_ents)
            album_kwargs = {"caption": captions, "reply_to": reply_to, "parse_mode": None}
            if safe_first_ents:
                album_kwargs["formatting_entities"] = safe_first_ents
            msgs = await _with_formatting_fallback(
                lambda **kw: _send_file(client, channel, files, **kw),
                album_kwargs, first_text, safe_first_ents, _current_src[0],
            )

            # Persist the album's first message ID in state immediately. This
            # avoids re-uploading it if a later text chunk fails in this run.
            if not msgs:
                raise RuntimeError("Album upload returned no messages")
            first_msg = msgs[0] if isinstance(msgs, (list, tuple)) else msgs
            state["album_id"] = first_msg.id
            save_state(state["src"], state)

        # When a prefix text was needed, all text chunks were sent before the
        # individual media items. Otherwise, any overflow follows the first item.
        needs_prefix_text = individual_media and (
            has_sticker or media[0][1] in NO_CAPTION_KINDS
        )
        if rest and not needs_prefix_text:
            await _send_text_chunks(client, channel, rest, state["album_id"],
                                    state, "tail_prog")
        if needs_prefix_text:
            return state.get("prefix_text_id") or state["album_id"]
        return state["album_id"]

    if media and media[0][1] in NO_CAPTION_KINDS:
        path, kind = media[0]
        text_id = state.get("text_id")
        if text_id is None and any(t.strip() for t, _ in chunks):
            text_id = await _send_text_chunks(client, channel, chunks, reply_to,
                                              state, "text_prog")
            state["text_id"] = text_id
            save_state(state["src"], state)
        thread = text_id or reply_to
        if "media_id" not in state:
            media_msg = await _send_media_item(client, channel, path, kind, thread)
            state["media_id"] = media_msg.id
            save_state(state["src"], state)
        return text_id or state["media_id"]

    if not media:
        # A single-chunk text has no half-finished state worth writing to disk.
        return await _send_text_chunks(client, channel, chunks, reply_to,
                                       state, "text_prog", persist=len(chunks) > 1)

    path, kind = media[0]
    if "media_id" not in state:
        media_msg = await _send_media_item(
            client, channel, path, kind, reply_to,
            caption=first_text, entities=first_ents,
        )
        state["media_id"] = media_msg.id
        # Always persist, so a crash between the upload returning and the id-map
        # write cannot re-upload the same media on the next run.
        save_state(state["src"], state)
    if rest:
        await _send_text_chunks(client, channel, rest, state["media_id"],
                                state, "tail_prog")
    return state["media_id"]


async def send_one(client, channel, data, chunks, reply_to, state, annotation):
    attempt, floods = 0, 0
    while True:
        try:
            return await _do_send(
                client, channel, chunks, data["media"], reply_to,
                data["forward_source"], state, annotation,
            )
        except FloodWaitError as fw:
            floods += 1
            if floods > MAX_FLOOD_WAITS:
                reason = f"gave up after {MAX_FLOOD_WAITS} FloodWait errors"
                print(f"    failed id={data['msg_id']}: {reason}")
                log_failure(data["msg_id"], reason)
                return None
            print(f"    FloodWait {fw.seconds}s — sleeping...")
            await asyncio.sleep(fw.seconds + 5)
        except Exception as exc:
            attempt += 1
            if attempt >= MAX_RETRIES:
                print(f"    failed id={data['msg_id']}: {exc}")
                log_failure(data["msg_id"], str(exc))
                return None
            print(f"    retry {attempt}/{MAX_RETRIES} id={data['msg_id']}: {exc}")
            await asyncio.sleep(2 ** attempt)


# ═══════════════════════════════════════════════════════════════
# CONFIRMATION
# ═══════════════════════════════════════════════════════════════
async def confirm_target(channel, pending, n_files):
    print()
    print("═" * 60)
    print(f"  Target:  {getattr(channel, 'title', '?')!r}")
    print(f"  ID:      {getattr(channel, 'id', '?')}")
    uname = getattr(channel, "username", None)
    if uname:
        print(f"  Handle:  @{uname}")
    print(f"  Files:   {n_files} export file(s)")
    print(f"  Pending: ~{pending} messages")
    print("═" * 60)
    if DRY_RUN:
        print("DRY RUN — nothing will actually be sent.\n")
        return True
    if SKIP_CONFIRMATION:
        print("SKIP_CONFIRMATION is on — proceeding.\n")
        return True
    return input("Send to this channel? [y/N]: ").strip().lower() in ("y", "yes")


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════
async def process_file(client, channel, path, id_map, partial, pending_replies, stats, ctx,
                      only_pending=False):
    print(("Retrying deferred replies in " if only_pending else "Streaming ") + os.path.basename(path) + " ...")
    context = etree.iterparse(path, events=("end",), tag="div", html=True, encoding="utf-8")

    for _event, elem in context:
        if "message" not in _classes(elem):
            continue

        data = None
        source_id = None
        mark_completed = False
        try:
            raw_elem_id = elem.get("id", "")
            if raw_elem_id.startswith("message") and raw_elem_id[7:].isdigit():
                source_id = raw_elem_id[7:]
            data = extract_message_data(elem)
            if data is None:
                mark_completed = bool(source_id)
                continue
            source_id = str(data["msg_id"])
            _current_src[0] = source_id

            if data["sender"]:
                ctx["last_sender"] = data["sender"]
            elif data["joined"] and PRESERVE_SENDER_NAMES:
                data["sender"] = ctx["last_sender"]

            # Metadata gathered during this pass (no separate full parse at startup):
            # reply targets for the audit, and HTML reaction data to supplement JSON.
            if data["reply_src_id"]:
                ctx["reply_map"][source_id] = str(data["reply_src_id"])
            if data.get("reactions") and source_id not in ctx["reaction_map"]:
                ctx["reaction_map"][source_id] = data["reactions"]

            if only_pending and source_id not in pending_replies:
                continue

            if source_id in id_map:
                was_pending = source_id in pending_replies
                pending_replies.pop(source_id, None)
                if was_pending and not DRY_RUN:
                    save_pending_replies(pending_replies)
                mark_completed = True
                continue

            if source_id in ctx["manual_done"]:
                # Uploaded by hand after an oversize stop: never sent by the script.
                was_pending = source_id in pending_replies
                pending_replies.pop(source_id, None)
                if was_pending and not DRY_RUN:
                    save_pending_replies(pending_replies)
                mark_completed = True
                continue

            if stats["sent"] + stats["failed"] >= BATCH_LIMIT:
                print(f"Batch limit reached ({BATCH_LIMIT}). Restart to continue.")
                ctx["stop"] = True
                break

            has_content = bool(data["body_text"].strip() or data["media"])
            if not has_content:
                stats["skipped"] += 1
                was_pending = source_id in pending_replies
                pending_replies.pop(source_id, None)
                if was_pending and not DRY_RUN:
                    save_pending_replies(pending_replies)
                mark_completed = True
                continue

            ts = data["timestamp"]
            watermark = (f"[{ts.strftime('%Y-%m-%d %H:%M')}]\n", []) if ts else ("", [])
            sender = (f"{data['sender']}:\n", []) if data["sender"] else ("", [])
            forward = (data["forward_header"], [])

            reply_to = id_map.get(str(data["reply_src_id"])) if data["reply_src_id"] else None
            reply_fallback = ("", [])
            if data["reply_src_id"] and reply_to is None:
                target_src = str(data["reply_src_id"])
                target_is_in_export = target_src in ctx["source_ids"]
                target_already_processed = target_src in ctx["processed_source_ids"]
                if target_is_in_export and not target_already_processed:
                    # Target appears later in the export, or has not yet been
                    # migrated in this run. Persist this message for a second pass.
                    if not DRY_RUN:
                        if pending_replies.get(source_id) != target_src:
                            pending_replies[source_id] = target_src
                            save_pending_replies(pending_replies)
                    else:
                        print(f"  [dry] would defer reply id={source_id} until target id={target_src} is migrated")
                    continue
                # A target absent from the export or already processed but unmapped
                # cannot become a native reply. Preserve the exported quote/context.
                preview = data.get("reply_preview", "")
                fallback = f"[Original reply target #{target_src} is unavailable in the destination]\n"
                if preview:
                    fallback += f"Original reply preview: {preview}\n"
                fallback += "\n"
                reply_fallback = (fallback, [])

            text, entities = compose_message(
                watermark, sender, forward, data["body_text"], data["body_entities"],
                extra_before_body=reply_fallback,
            )
            text, entities = strip_with_entities(text, entities)

            # Size preflight: nothing from this message is uploaded if any file is too big.
            oversize = find_oversize_media(data["media"])
            if oversize:
                if DRY_RUN:
                    print_oversize_dry_run(source_id, data, oversize)
                    ctx["oversize_report"].append(source_id)
                    mark_completed = True
                    continue
                halt_for_oversize(source_id, data, text, oversize, id_map, ctx)
                break

            ann_text, ann_ents = compose_message(watermark, sender, ("", []), "", [])
            ann_text, ann_ents = strip_with_entities(ann_text, ann_ents)
            annotation = [(ann_text, ann_ents)] if ann_text.strip() else None

            kinds_list = [k for _, k in data["media"]]
            captionable = bool(kinds_list) and kinds_list[0] not in NO_CAPTION_KINDS
            has_sticker = any(k in STICKER_KINDS for k in kinds_list)
            first_limit = TEXT_LIMIT if has_sticker else (
                CAPTION_LIMIT if (captionable or len(kinds_list) > 1) else TEXT_LIMIT
            )
            kinds = ",".join(kinds_list) or "text"
            when = ts.strftime("%Y-%m-%d %H:%M") if ts else "?"

            if DRY_RUN:
                chunks = split_with_entities(text, entities, first_limit, TEXT_LIMIT)
                flag = "↪" if reply_to else ("?" if data["reply_src_id"] else " ")
                fwd = "F" if data["forward_source"] else " "
                preview = text[:80].replace("\n", "⏎")
                extra = f" +{len(chunks) - 1} part(s)" if len(chunks) > 1 else ""
                print(f"  [dry] {flag}{fwd} id={source_id:<6} ({kinds:<15}) {preview!r}{extra}")
                stats["sent"] += 1
                pending_replies.pop(source_id, None)
                mark_completed = True
                continue

            entities = await resolve_mentions(client, entities)
            chunks = split_with_entities(text, entities, first_limit, TEXT_LIMIT)

            state = {"src": source_id, "force_reconstruction": bool(data["reply_src_id"])}
            if source_id in partial:
                state.update(partial[source_id])

            new_id = await send_one(client, channel, data, chunks, reply_to, state, annotation)

            if new_id:
                id_map[source_id] = int(new_id)
                ctx["last_dest_id"] = int(new_id)
                append_id_map(source_id, new_id)
                stats["sent"] += 1
                if state.get("native"):
                    stats["forwarded"] += 1
                was_pending = source_id in pending_replies
                pending_replies.pop(source_id, None)
                if was_pending:
                    save_pending_replies(pending_replies)
                print(f"  [{stats['sent']:>4}] id={source_id:<6} → {new_id:<7}  ({kinds})  {when}")
            else:
                stats["failed"] += 1
                mark_completed = True
                if data["reply_src_id"]:
                    ctx["audit"].append({
                        "src": source_id,
                        "source_reply_to": str(data["reply_src_id"]),
                        "verified": False,
                        "status": "send_failed",
                    })

            mark_completed = True
            await asyncio.sleep(SEND_DELAY_SECONDS)

        except Exception as exc:
            mid = ("message" + source_id) if source_id else elem.get("id", "?")
            print(f"    failed {mid}: {type(exc).__name__}: {exc}")
            log_failure(mid, f"{type(exc).__name__}: {exc}")
            stats["failed"] += 1
            mark_completed = True
        finally:
            if source_id and mark_completed:
                ctx["processed_source_ids"].add(source_id)
            elem.clear()
            while elem.getprevious() is not None:
                del elem.getparent()[0]

    return ctx["stop"]


async def main():
    if not API_HASH or not BOT_TOKEN:
        print("Missing Telegram credentials.")
        print("Set TELEGRAM_API_HASH and TELEGRAM_BOT_TOKEN in .env/environment, or add them")
        print("as Colab Secrets with notebook access enabled. Never commit credentials to Git.")
        return
    if CHANNEL_PEER is None:
        print("Missing destination channel.")
        print("Set TELEGRAM_CHANNEL_PEER in .env or Colab Secrets to a numeric peer ID or @username.")
        return

    configure_send_file_compat()
    _enable_hachoir()

    drive_root = os.path.abspath("/content/drive")
    configured_dir = os.path.abspath(BASE_DIR)
    using_drive = configured_dir == drive_root or configured_dir.startswith(drive_root + os.sep)
    if "google.colab" in sys.modules and using_drive and not os.path.isdir("/content/drive/MyDrive"):
        from google.colab import drive
        drive.mount("/content/drive")

    # FFmpeg/ffprobe are installed lazily only if a .webm video sticker needs
    # dimensions/duration and no usable ffprobe is already available.
    files = export_files()
    if not files:
        print(f"No messages.html / messages2.html ... found in: {BASE_DIR}")
        return

    id_map = load_id_map()
    manual_done, manual_map = load_manual_uploads()
    for _src, _dst in manual_map.items():
        id_map.setdefault(_src, _dst)         # so replies to your manual uploads link to them
    if manual_done:
        _ids = sorted(manual_done, key=int)
        _shown = ", ".join(_ids[:20]) + (" ..." if len(_ids) > 20 else "")
        print(f"{len(manual_done)} message(s) marked as uploaded by hand and SKIPPED by the script (ids: {_shown}).")
        print(f"  Make sure they are really in the channel; remove a line from {os.path.basename(MANUAL_UPLOADS_FILE)} to have the script send it.")
    partial = load_partial()
    pending_replies = load_pending_replies()
    source_ids = collect_source_ids(files)           # one cheap pass; len() is the message count
    total = len(source_ids)
    reaction_map = load_json_reactions(files, source_ids)

    print(f"Export files: {len(files)}   Already processed: {len(id_map)}   File(s) contain: {total} messages")
    print(f"{len(pending_replies)} deferred replies are queued.")

    # Session renamed to '_bot' to keep it isolated from any user-account session.
    client = TelegramClient("colab_bot_restorer_session", API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)
    print("Logged in officially as a bot.")

    try:
        channel = await client.get_entity(CHANNEL_PEER)
    except Exception as e:
        print(f"Channel error: {e}")
        await client.disconnect()
        return

    if not await confirm_target(channel, max(0, total - len(set(id_map) | manual_done)), len(files)):
        print("Aborted by user.")
        await client.disconnect()
        return

    stats = {"sent": 0, "skipped": 0, "failed": 0, "forwarded": 0, "reaction_notes": 0}
    ctx = {
        "last_sender": "",
        "stop": False,
        "source_ids": source_ids,
        "processed_source_ids": set(),
        "reaction_map": reaction_map,
        "reply_map": {},          # source reply targets, collected during the main pass
        "audit": [],              # reply-audit records for this run
        "manual_done": manual_done,   # ids you uploaded by hand (never sent by the script)
        "halt": [],                   # non-empty => an oversize file stopped the run
        "oversize_report": [],        # dry run: every message with an oversize file
        "last_dest_id": None,
    }

    for path in files:
        if await process_file(client, channel, path, id_map, partial, pending_replies, stats, ctx):
            break

    # Second pass: deferred replies whose target has now been migrated. Any whose
    # target ultimately failed are sent with the archived reply preview instead.
    if not ctx["stop"] and pending_replies:
        # Repeat if a deferred reply was itself a parent for another deferred reply.
        while pending_replies and not ctx["stop"]:
            before_pending = len(pending_replies)
            before_completed = len(ctx["processed_source_ids"])
            pending_ctx = dict(ctx)          # shallow copy: shares the maps/lists above
            pending_ctx["last_sender"] = ""
            pending_ctx["stop"] = False
            for path in files:
                if await process_file(client, channel, path, id_map, partial, pending_replies,
                                      stats, pending_ctx, only_pending=True):
                    ctx["stop"] = True
                    break
            ctx["processed_source_ids"].update(pending_ctx["processed_source_ids"])
            if len(pending_replies) >= before_pending and len(ctx["processed_source_ids"]) <= before_completed:
                break

    if ctx["halt"]:
        # Clean stop: everything already sent is in the id map, deferred replies and
        # partial-send states are on disk. No audit/reaction pass, no further sends.
        save_pending_replies(pending_replies)
        print(f"Stopped at message id={ctx['halt'][0]}: sent={stats['sent']} this run, progress saved.")
        print("Follow the instructions above, then run the script again to resume.")
        await client.disconnect()
        return

    if not reaction_map:
        print("No reaction metadata was found. Telegram Desktop HTML exports generally do not expose full reaction data.")
        print("To archive available reactions, place the matching Telegram JSON export (result.json or messages.json, with reaction fields) directly in BASE_DIR.")

    note_map = load_reaction_note_map()
    if not DRY_RUN:
        persist_reaction_source_metadata(reaction_map)
        await verify_all_reply_links(client, channel, ctx["reply_map"], id_map, ctx["audit"])
        write_reply_audit(ctx["audit"])
        if not ctx["stop"]:
            await restore_reaction_archive_notes(client, channel, id_map, reaction_map, note_map, stats)

    if DRY_RUN and ctx["oversize_report"]:
        print()
        print(f"{len(ctx['oversize_report'])} message(s) have files over {_mb(_max_upload_bytes())} "
              f"(ids: {', '.join(ctx['oversize_report'])}). A real run stops at the first one.")

    print()
    print("═" * 60)
    print(f"Done.  sent={stats['sent']}   skipped={stats['skipped']}   failed={stats['failed']}   "
          f"native_forwards={stats['forwarded']}   reaction_notes={stats.get('reaction_notes', 0)}")
    print(f"Reply audit: {REPLY_AUDIT_FILE}")
    if pending_replies:
        print(f"Deferred replies still waiting for a target mapping: {len(pending_replies)} (saved in {PENDING_REPLIES_FILE})")
    if stats["failed"]:
        print(f"Failed IDs are logged in {FAILED_FILE}")
    print("═" * 60)
    await client.disconnect()


# ═══════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════
def _run_main():
    """Run directly from Python or auto-run inside an active Colab/Jupyter loop.

    In notebooks, patch the existing loop with nest_asyncio and run `main()` on
    the notebook's main thread. This avoids requiring a second `await main()`
    cell and avoids moving `input()` / `google.colab.drive.mount()` into a worker
    thread, where interactive behavior can be unreliable.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(main())

    try:
        import nest_asyncio
    except ImportError:
        print("Installing the small nest_asyncio helper for automatic notebook execution...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "nest_asyncio"])
        import nest_asyncio

    nest_asyncio.apply(loop)
    return loop.run_until_complete(main())

if __name__ == "__main__":
    _run_main()
