"""SonyLIV Downloader Telegram bot (Pyrogram + aiohttp) with COOKIE SUPPORT.

ADDITIONS from original:
  - Cookie parsing + storage (like fbot FapHouse)
  - /setcookies, /clearcookies, /mycookies commands
  - Cookie-based auth as fallback (no need for OTP if cookies valid)
  - Search/resolve/download all work with cookies

Send a SonyLIV show/movie URL or search term -> bot resolves through SonyLIV API -> pick Download or Stream.
Login: /login → phone OTP → bearer token (stored per-user)
  OR  /setcookies → browser cookies (stored per-user, fallback auth)
Download: N_m3u8DL-RE (DRM) or yt-dlp (non-DRM)
Stream: M3U8 link via HLS proxy (playable in Telegram)

KEY CHANGES:
1. Import sonyliv_api_enhanced (has cookie functions)
2. Use store_enhanced (has cookie storage)
3. Add /setcookies, /clearcookies, /mycookies handlers
4. Modify search/stream/download to use cookies as fallback
"""
import asyncio
import contextlib
import glob
import hashlib
import html
import io
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
import subprocess
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from datetime import datetime, timezone
from typing import Optional, Dict, Tuple

from dotenv import load_dotenv
from pyrogram import Client, filters, idle
from pyrogram.enums import ChatMemberStatus, ParseMode
from pyrogram.errors import FloodWait, InputUserDeactivated, MessageNotModified, PeerIdInvalid, UserIsBlocked, UserNotParticipant
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import (InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup, LinkPreviewOptions, BotCommand,
                            ReplyKeyboardMarkup, KeyboardButton)

import aiohttp
import sonyliv_api as sony
import sonyliv_download as dl_engine
import mp4_convert
import split_upload
from store import Store  # ← Now this is store_enhanced.py

load_dotenv()

# ---- blockquote everywhere ----
BLOCKQUOTE_ALL = os.getenv("BLOCKQUOTE_ALL", "1") != "0"


def _install_blockquote():
    """Install blockquote wrapper for all HTML messages."""
    try:
        from pyrogram.parser.html import HTML as _HTMLParser
    except Exception:
        return
    _orig = _HTMLParser.parse

    async def parse(self, text):
        t = str(text if text is not None else "")
        if BLOCKQUOTE_ALL and t.strip() and "<blockquote" not in t.lower():
            t = f"<blockquote>{t.strip()}</blockquote>"
        return await _orig(self, t)

    _HTMLParser.parse = parse


_install_blockquote()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("sonyliv-bot")


def _req(name: str, default: str = "") -> str:
    """Require environment variable."""
    v = os.getenv(name, default).strip()
    if not v:
        raise SystemExit(f"Missing required environment variable {name} (see .env.example)")
    return v


# ---------------------------------------------------------------- config ----
API_ID = int(_req("API_ID", "33029767"))
API_HASH = _req("API_HASH", "5d897bed11bc8b062a12f6c1c3c5360a")
BOT_TOKEN = _req("BOT_TOKEN", "8651679866:AAH3ROfJyt0-SzN5Xpr9zf-n2slizqeP_Bs")
OWNER_ID = int(os.getenv("OWNER_ID", "8729304171") or 0)
ADMINS = {OWNER_ID, *(int(x) for x in re.findall(r"\d+", os.getenv("ADMINS", "8931907813")))} - {0}
LOG_CHANNEL = (os.getenv("LOG_CHANNEL", "-1004401290975") or "").strip()
FORCE_SUB = [x.strip() for x in os.getenv("FORCE_SUB", "").split(",") if x.strip()]

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")
MAX_FILE_BYTES = int(os.getenv("MAX_FILE_SIZE_MB", "2000")) * 1024 * 1024
SPLIT_LARGE = os.getenv("SPLIT_LARGE", "1").strip().lower() not in ("0", "false", "no", "off")
MAX_DL_BYTES = int(os.getenv("MAX_SPLIT_MB", "8192")) * 1024 * 1024 if SPLIT_LARGE else MAX_FILE_BYTES

# mp4 (default): mkv -> real .mp4 like fbot/AnimeHD so Telegram plays it inline; mkv: keep the .mkv as a document
OUTPUT_FORMAT = os.getenv("OUTPUT_FORMAT", "mp4").strip().lower()

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ---- persistence ----
store = Store(os.getenv("STORE_PATH", "bot_data.json"))

# ---- bot setup ----
app = Client("sonyliv-bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ---- session management ----
_session: Optional[aiohttp.ClientSession] = None


async def get_session() -> aiohttp.ClientSession:
    """Get or create aiohttp session."""
    global _session
    if _session is None:
        _session = aiohttp.ClientSession()
    return _session


async def safe_send(chat_id, text, **kwargs):
    """Safe message send with error handling."""
    try:
        return await app.send_message(chat_id, text, **kwargs)
    except FloodWait as e:
        await asyncio.sleep(e.value)
        return await app.send_message(chat_id, text, **kwargs)
    except UserIsBlocked:
        log.warning(f"User {chat_id} blocked the bot")
    except UserNotParticipant:
        log.warning(f"User {chat_id} not in channel")
    except Exception as e:
        log.error(f"Failed to send message to {chat_id}: {e}")


# ---- fbot-style text helpers ----
_SMALLCAPS_MAP = str.maketrans(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "ᴀʙᴄᴅᴇғɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢᴀʙᴄᴅᴇғɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢ",
)
_TAG_OR_MENTION_RE = re.compile(r"(<[^>]+>|@[A-Za-z][A-Za-z0-9_]{3,31})")


def smallcaps(text: str) -> str:
    return text.translate(_SMALLCAPS_MAP)


def SC(text):
    """Small-caps every plain word; keep HTML tags, <code>...</code> contents and @mentions untouched (same as fbot)."""
    if not isinstance(text, str):
        return text
    out, in_code = [], 0
    for part in _TAG_OR_MENTION_RE.split(text):
        if part.startswith("<") and part.endswith(">"):
            low = part.lower()
            if low.startswith("<code"):
                in_code += 1
            elif low.startswith("</code"):
                in_code = max(0, in_code - 1)
            out.append(part)
        elif part.startswith("@"):
            out.append(part)
        else:
            out.append(part if in_code else part.translate(_SMALLCAPS_MAP))
    return "".join(out)


POWERED_BY = os.getenv("POWERED_BY", "Anuj Kumar")
POWERED_BY_URL = os.getenv("POWERED_BY_URL", "https://t.me/anujedits97")
START_PHOTO_URL = os.getenv("START_PHOTO_URL", "").strip()

EXAMPLE_LINKS = (
    ("📺 Episode", "https://www.sonyliv.com/shows/taarak-mehta-ka-ooltah-chashmah-1700000084/"
                   "taarak-mehta-ka-ooltah-chashmah-jethalal-in-pakistan-1000036981?watch=true"),
    ("🎬 Movie (Spider-Man: Homecoming)", "https://www.sonyliv.com/movies/spider-man-homecoming-1000042078"),
    ("🍥 Show (One-Punch Man)", "https://www.sonyliv.com/shows/one-punch-man-1790007515"),
)


def _welcome_text(first_name: str, bot_username: str, bot_name: str) -> str:
    powered = f'<a href="{POWERED_BY_URL}">{smallcaps(POWERED_BY)}</a>'
    return SC(
        f"<b>👋 Hello {html.escape(first_name or 'there')},</b>\n"
        f"<b>🤖 I am <a href=https://t.me/{bot_username}>{html.escape(bot_name)}</a></b>\n\n"
        "⚡ I'm a very powerful SonyLIV downloader bot.\n\n"
        "📥 Simply send me any SonyLIV url, and I'll fetch the video for you in seconds.\n\n"
        "🚀 Ultra-fast processing\n"
        "🎬 Instant video extraction\n"
        "⚡ Lightning-speed downloads\n"
        "🎞 Real MP4 files - play right inside Telegram\n"
        "🔐 DRM titles supported (with your cookies)\n"
        "🔗 Just paste your SonyLIV link below and let the magic begin!\n\n"
        "✅ Ye links supported hai:\n"
        "• sonyliv.com/movies/...\n"
        "• sonyliv.com/shows/...\n"
        "waale links\n\n"
        "━━━━━━━━━━━━━━━\n"
        f"👑 Powered by {powered}\n"
        "⚡ Speed • Performance • Reliability\n"
        "━━━━━━━━━━━━━━━"
    )


def _help_text() -> str:
    ex = "\n".join(f"{label}:\n<code>{url}</code>\n" for label, url in EXAMPLE_LINKS)
    return SC(
        "ℹ️ <b>How to use</b>\n\n"
        "🔹 <b>Just send the link:</b>\n"
        "Paste any SonyLIV url directly in the chat, then pick the quality.\n\n"
        "🔹 <b>Supported url formats:</b>\n"
        "<code>sonyliv.com/movies/...</code>\n"
        "<code>sonyliv.com/shows/...</code>\n\n"
        f"📌 <b>Examples:</b>\n{ex}\n"
        "🔑 <b>Login (required once):</b>\n"
        "• <code>/login +91XXXXXXXXXX</code> - phone OTP\n"
        "• <code>/setcookies</code> - browser cookies (add sl_ppid + sl_device_token for DRM / full quality)\n"
        "• <code>/mycookies</code> • <code>/clearcookies</code> • <code>/token</code>\n\n"
        "🔍 <b>Other commands:</b>\n"
        "• <code>/search name</code> - find a title\n"
        "• <code>/downloads</code> - your recent downloads\n"
        "• <code>/about</code> - about this bot\n\n"
        "💡 <b>Tips:</b>\n"
        "• Files up to 2 GB are uploaded directly - bigger ones are split into parts automatically\n"
        "• Pick Best quality for the highest resolution available\n"
        "• If a link fails, check that you are logged in and the cookies are fresh\n\n"
        "Having trouble? Make sure you're sending a valid SonyLIV link."
    )


def _about_text(bot_name: str, bot_username: str) -> str:
    return (
        f"💠 {smallcaps('About This Bot')} 💠\n\n"
        f"╭────[ ✨ {smallcaps('Anuj')} ]────⍟\n"
        f"├⍟ 🚀 {smallcaps('Bot Name')}  : <a href=\"https://t.me/{bot_username}\">{smallcaps(bot_name)}</a>\n"
        f"├⍟ 👨‍💻 {smallcaps('Developer')}  : <a href=\"{POWERED_BY_URL}\">{smallcaps(POWERED_BY)}</a>\n"
        f"├⍟ 🔗 {smallcaps('Library')}  : <a href=\"https://docs.pyrogram.org/\">{smallcaps('Pyrogram Async')}</a>\n"
        f"├⍟ ⚡️ {smallcaps('Language')}  : <a href=\"https://www.python.org/\">{smallcaps('Python')} 3.11+</a>\n"
        f"├⍟ ⚙️ {smallcaps('Storage')}  : {smallcaps('JSON store')}\n"
        f"├⍟ ⭐️ {smallcaps('Hosting')}  :  {smallcaps('Dedicated High-Speed VPS')}\n"
        "╰───────────────⍟"
    )


def _menu_kb(close_only: bool = False) -> Markup:
    if close_only:
        return Markup([[Btn("❌ Close", callback_data="about_close")]])
    return Markup([[Btn("❓ Help", callback_data="menu_help"), Btn("ℹ️ About", callback_data="menu_about")]])


# ---- command: /start ----
@app.on_message(filters.private & filters.command("start"))
async def on_start(client, message):
    me = await client.get_me()
    caption = _welcome_text(message.from_user.first_name, me.username or "", me.first_name or "SonyLIV Bot")
    if START_PHOTO_URL:
        try:
            await message.reply_photo(START_PHOTO_URL, caption=caption, reply_markup=_menu_kb(), parse_mode=ParseMode.HTML)
            return
        except Exception as e:
            log.warning(f"start photo failed, falling back to text: {e}")
    await message.reply(caption, reply_markup=_menu_kb(), parse_mode=ParseMode.HTML,
                        link_preview_options=LinkPreviewOptions(is_disabled=True))


# ---- command: /help ----
@app.on_message(filters.private & filters.command("help"))
async def on_help(client, message):
    await message.reply(_help_text(), parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True))


# ---- command: /about ----
@app.on_message(filters.private & filters.command("about"))
async def on_about(client, message):
    me = await client.get_me()
    await message.reply(_about_text(me.first_name or "SonyLIV Bot", me.username or ""), reply_markup=_menu_kb(True),
                        parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True))


@app.on_callback_query(filters.regex(r"^(menu_help|menu_about|about_close)$"))
async def on_menu_cb(client, query):
    if query.data == "about_close":
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.answer()
        return
    if query.data == "menu_help":
        await query.message.reply(_help_text(), parse_mode=ParseMode.HTML,
                                  link_preview_options=LinkPreviewOptions(is_disabled=True))
    else:
        me = await client.get_me()
        await query.message.reply(_about_text(me.first_name or "SonyLIV Bot", me.username or ""),
                                  reply_markup=_menu_kb(True), parse_mode=ParseMode.HTML,
                                  link_preview_options=LinkPreviewOptions(is_disabled=True))
    await query.answer()


# ---- command: /login ----
_login_sessions: Dict[int, Dict] = {}  # temp storage during OTP flow


@app.on_message(filters.private & filters.command("login"))
async def on_login(client, message):
    """Start OTP login flow."""
    user_id = message.from_user.id
    if user_id in _login_sessions:
        await safe_send(message.chat.id, "⏳ Login in progress. Send OTP or /cancel.")
        return

    phone = (message.text or "").split(maxsplit=1)
    phone = phone[1].strip() if len(phone) > 1 else ""

    if not phone or not phone.replace("+", "").replace(" ", "").isdigit():
        await safe_send(
            message.chat.id,
            "📱 Usage: <code>/login +919876543210</code>\n"
            "(or <code>/login 919876543210</code>)",
            parse_mode="html",
        )
        return

    phone = "+" + phone.lstrip("+").replace(" ", "")
    await safe_send(message.chat.id, f"📤 Sending OTP to {phone}...")

    try:
        _session = await get_session()
        result = await sony.send_otp(_session, phone)

        if result.get("error"):
            await safe_send(message.chat.id, f"❌ OTP send failed: {result['error']}")
            return

        _login_sessions[user_id] = {"phone": phone, "step": "otp"}
        await safe_send(
            message.chat.id,
            f"✅ OTP sent to {phone}\n\n"
            f"Enter OTP (or <code>/cancel</code> to stop):",
            parse_mode="html",
        )
    except Exception as e:
        log.error(f"Login OTP send failed: {e}")
        await safe_send(message.chat.id, f"❌ Error: {e}")


@app.on_message(filters.private & filters.text, group=1)
async def on_otp_input(client, message):
    """Receive OTP and verify."""
    user_id = message.from_user.id
    if user_id not in _login_sessions or _login_sessions[user_id]["step"] != "otp":
        return

    text = (message.text or "").strip()
    if text.startswith("/") and text.lower() != "/cancel":
        return  # another command - let its own handler deal with it
    if text.lower() == "/cancel":
        del _login_sessions[user_id]
        await safe_send(message.chat.id, "❌ Login cancelled.")
        return

    if not text.isdigit() or len(text) != 4:
        await safe_send(message.chat.id, "⚠️ Please send a 4-digit OTP.")
        return

    phone = _login_sessions[user_id]["phone"]
    await safe_send(message.chat.id, "🔐 Verifying OTP...")

    try:
        _session = await get_session()
        result = await sony.verify_otp(_session, phone, text)

        token = result.get("accessToken")
        if token:
            store.set_user_token(user_id, token, phone=phone)
            await store.flush()
            payload = sony._decode_jwt(token)
            exp = payload.get("exp", 0)
            exp_date = datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M:%S")

            await safe_send(
                message.chat.id,
                f"✅ <b>Login successful!</b>\n"
                f"Token valid until: <code>{exp_date}</code>\n"
                f"Use <code>/search query</code> to search.",
                parse_mode="html",
            )
        else:
            await safe_send(message.chat.id, f"❌ Login failed: {result.get('error', 'unknown error')}")

        if user_id in _login_sessions:
            del _login_sessions[user_id]
    except Exception as e:
        log.error(f"OTP verify failed: {e}")
        await safe_send(message.chat.id, f"❌ Error: {e}")


# ---- command: /token ----
@app.on_message(filters.private & filters.command("token"))
async def on_token(client, message):
    """Show current token status."""
    user_id = message.from_user.id
    await safe_send(message.chat.id, "⏳ Checking token...")

    token = store.get_user_token(user_id)

    if not token:
        await safe_send(message.chat.id, "❌ Not logged in. Use /login")
        return

    payload = sony._decode_jwt(token)
    exp = payload.get("exp", 0)
    exp_date = datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M:%S")
    remaining = max(0, exp - time.time())
    remaining_days = remaining / 86400

    status = "✅ Valid" if remaining > 0 else "⏰ Expired"
    msg = (
        f"🔐 <b>Your Token</b>\n\n"
        f"Status: {status}\n"
        f"Expires: <code>{exp_date}</code>\n"
        f"Remaining: <code>{remaining_days:.1f} days</code>\n\n"
        f"Phone: <code>{store.get_user_phone(user_id)}</code>"
    )
    await safe_send(message.chat.id, msg, parse_mode="html")


def _device_cookie_status(cookies) -> str:
    dc = sony.get_device_cookies(cookies)
    ppid = "✅" if dc.get("sl_ppid") else "❌"
    tok = "✅" if dc.get("sl_device_token") else "❌"
    line = f"📱 Device cookies: sl_ppid {ppid} · sl_device_token {tok}"
    if not dc:
        line += "\n⚠️ Needed for DRM / full-quality streams - export cookies again while logged in on sonyliv.com."
    return line


# ---- NEW: command: /setcookies ----
@app.on_message(filters.private & filters.command("setcookies"))
async def on_setcookies(client, message):
    """Set SonyLIV cookies for this user (fallback auth if OTP expires).
    
    Usage:
      /setcookies name=value; name2=value2        inline header format
      /setcookies                                  reply to text message or cookies.txt file
      /setcookies clear                            remove saved cookies
    """
    user_id = message.from_user.id
    parts = (message.text or "").split(maxsplit=1)
    raw = parts[1].strip() if len(parts) > 1 else ""
    had_inline_secret = bool(raw)
    
    # Handle "clear" command
    if raw.lower() in ("clear", "off", "delete", "del"):
        n = store.clear_user_cookies(user_id)
        await store.flush()
        if had_inline_secret:
            try:
                await message.delete()
            except Exception:
                pass
        await safe_send(
            message.chat.id,
            f"🗑 <b>SonyLIV cookies cleared</b> ({n} removed).\n"
            f"You'll need to /login again using OTP.",
            parse_mode="html"
        )
        return
    
    # Get cookies from reply (text message or file)
    reply = message.reply_to_message
    if not raw and reply and reply.document:
        try:
            doc_path = await client.download_media(
                reply.document, 
                file_name=os.path.join(DOWNLOAD_DIR, "setcookies_upload.txt")
            )
            try:
                with open(doc_path, "r", encoding="utf-8", errors="replace") as f:
                    raw = f.read()
            finally:
                try:
                    os.remove(doc_path)
                except OSError:
                    pass
        except Exception as e:
            await safe_send(message.chat.id, f"❌ Failed to download cookies file: {e}")
            return
    elif not raw and reply and reply.text:
        raw = reply.text.strip()
    
    if not raw:
        await safe_send(
            message.chat.id,
            "⚠️ <b>Usage:</b>\n"
            "<code>/setcookies name=value; name2=value2</code>\n"
            "or reply to a text message / uploaded cookies.txt with <code>/setcookies</code>\n"
            "<code>/setcookies clear</code> removes them.",
            parse_mode="html"
        )
        return
    
    # Parse cookies
    parsed = sony.parse_cookie_string(raw)
    
    if not parsed:
        await safe_send(
            message.chat.id,
            "⚠️ <b>Saved, but 0 SonyLIV cookies were parsed.</b>\n"
            "Send a <code>name=value; ...</code> string or Netscape cookies.txt with sonyliv.com cookies.",
            parse_mode="html"
        )
        return
    
    # Store cookies
    store.set_user_cookies(user_id, parsed)
    await store.flush()
    
    # Delete message if inline (security)
    if had_inline_secret:
        try:
            await message.delete()
        except Exception:
            pass
    
    await safe_send(
        message.chat.id,
        f"✅ <b>SonyLIV cookies updated</b> - {len(parsed)} cookie(s).\n"
        f"{_device_cookie_status(parsed)}\n"
        f"Now you can search/stream/download without /login (cookies as fallback).",
        parse_mode="html"
    )


# ---- NEW: command: /clearcookies ----
@app.on_message(filters.private & filters.command("clearcookies"))
async def on_clearcookies(client, message):
    """Clear all stored SonyLIV cookies for this user."""
    user_id = message.from_user.id
    n = store.clear_user_cookies(user_id)
    await store.flush()
    
    if n == 0:
        await safe_send(message.chat.id, "ℹ️ No cookies stored.")
    else:
        await safe_send(
            message.chat.id,
            f"🗑 <b>{n} SonyLIV cookie(s) removed.</b>\n"
            f"Use /login to sign in with OTP, or /setcookies to add new cookies.",
            parse_mode="html"
        )


# ---- NEW: command: /cookiecount or /mycookies ----
@app.on_message(filters.private & filters.command(["cookiecount", "mycookies"]))
async def on_cookiecount(client, message):
    """Show how many SonyLIV cookies you have stored."""
    user_id = message.from_user.id
    count = store.get_cookie_count(user_id)
    
    if count == 0:
        msg = (
            "🍪 <b>No SonyLIV cookies stored.</b>\n\n"
            "To use cookies as fallback auth:\n"
            "1️⃣ <code>/setcookies &lt;inline&gt;</code> - paste header format\n"
            "2️⃣ <code>/setcookies</code> - reply to cookies.txt file\n"
            "3️⃣ <code>/clearcookies</code> - remove them later"
        )
    else:
        msg = (
            f"🍪 <b>You have {count} SonyLIV cookie(s) stored.</b>\n"
            f"{_device_cookie_status(store.get_user_cookies(user_id))}\n\n"
            f"Cookies are used as fallback auth when your OTP token expires.\n"
            f"Use <code>/clearcookies</code> to remove them.\n"
            f"Use <code>/setcookies</code> to replace them."
        )
    
    await safe_send(message.chat.id, msg, parse_mode="html")


# ---- command: /search (MODIFIED - cookie fallback) ----
@app.on_message(filters.private & filters.command("search"))
async def search_query(client, message):
    """Search for SonyLIV content."""
    user_id = message.from_user.id
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await safe_send(message.chat.id, "❌ Usage: /search <query>")
        return

    query = parts[1].strip()
    await safe_send(message.chat.id, f"🔍 Searching for '<i>{html.escape(query)}</i>'...", parse_mode="html")

    # ← MODIFIED: Try token first, then cookies
    token = store.get_user_token(user_id)
    cookies = store.get_user_cookies(user_id) if not token else None
    
    if not token and not cookies:
        await safe_send(
            message.chat.id,
            "❌ Not logged in. Use <code>/login</code> (OTP) or <code>/setcookies</code> (cookies).",
            parse_mode="html"
        )
        return

    try:
        _session = await get_session()
        
        # Use token if available, fall back to cookies
        if token:
            result = await sony.search(_session, token, query, page_size=5)
        else:
            result = await sony.search_with_cookies(_session, cookies, query, page_size=5)

        if result.get("error"):
            await safe_send(message.chat.id, f"❌ Search error: {result['error']}")
            return

        items = result.get("results", [])
        if not items:
            await safe_send(message.chat.id, f"❌ No results for '{html.escape(query)}'")
            return

        btns = [
            [Btn(text=item.get("title", "?")[:40], callback_data=f"sel_{item['id']}")]
            for item in items[:5]
        ]
        markup = Markup(btns)
        await safe_send(message.chat.id, "📺 <b>Results:</b>", reply_markup=markup, parse_mode="html")
    except Exception as e:
        log.error(f"Search failed: {e}")
        await safe_send(message.chat.id, f"❌ Error: {e}")


async def _resolve_for_user(user_id: int, content_id: str):
    """Resolve with token AND cookies together (sl_ppid / sl_device_token are needed for DRM x-did even when
    a bearer token exists). Keeps one stable device_id per user. Raises sony.SonyLIVError with a readable reason."""
    token = store.get_user_token(user_id)
    cookies = store.get_user_cookies(user_id)
    if not token and not cookies:
        raise sony.SonyLIVError("not_logged_in - use /login or /setcookies")
    device_id = sony.pick_device_id(token, cookies, store.get_user_device_id(user_id))
    if device_id != store.get_user_device_id(user_id):
        store.set_user_device_id(user_id, device_id)
        await store.flush()
    _session = await get_session()
    return await sony.resolve(_session, content_id, token=token or None, cookies=cookies or None,
                              device_id=device_id, user_key=str(user_id))


# ---- callback: stream & download (MODIFIED - cookie fallback) ----
@app.on_callback_query(filters.regex(r"^(sel_|stream_|dl_)"))
async def on_action(client, query):
    """Handle content selection and actions."""
    data = query.data
    content_id = data.split("_", 1)[1]

    # ← MODIFIED: Try token first, then cookies
    token = store.get_user_token(query.message.chat.id)
    cookies = store.get_user_cookies(query.message.chat.id)  # always load: device cookies matter even with a token
    
    if not token and not cookies:
        await query.answer("❌ Session expired. Use /login or /setcookies.", show_alert=True)
        return

    if data.startswith("sel_"):
        await query.answer("⏳ Resolving...", show_alert=False)
        try:
            playable = await _resolve_for_user(query.message.chat.id, content_id)

            if not playable:
                await query.answer("❌ Could not resolve video", show_alert=True)
                return

            btns = [[
                Btn(text="▶️ Stream", callback_data=f"stream_{content_id}"),
                Btn(text="📥 Download", callback_data=f"dl_{content_id}"),
            ]]
            markup = Markup(btns)
            await query.edit_message_text(
                f"<b>{html.escape(playable.title)}</b>\n"
                f"<code>{playable.duration}s</code>",
                reply_markup=markup,
                parse_mode="html",
            )
        except Exception as e:
            log.error(f"Resolve failed: {e}")
            await query.answer(f"❌ {str(e)[:180]}", show_alert=True)

    elif data.startswith("stream_"):
        await query.answer("⏳ Getting stream...", show_alert=False)
        try:
            playable = await _resolve_for_user(query.message.chat.id, content_id)
            if playable.is_drm or not playable.stream_url:
                await query.answer("🔐 DRM title - it cannot be streamed as a link. Use 📥 Download.", show_alert=True)
                return
            await safe_send(query.message.chat.id,
                            f"▶️ <b>{html.escape(playable.title)}</b>\n<code>{html.escape(playable.stream_url)}</code>",
                            parse_mode="html")
        except Exception as e:
            log.error(f"Stream failed: {e}")
            await query.answer(f"❌ {str(e)[:180]}", show_alert=True)

    elif data.startswith("dl_"):
        uid = query.message.chat.id
        if uid in _active:
            await query.answer("⏳ You already have a download running.", show_alert=True)
            return
        await query.answer("⏳ Analysing...", show_alert=False)
        try:
            text, markup = await _prepare_download(uid, content_id)
            await query.edit_message_text(text, reply_markup=markup, parse_mode="html")
        except Exception as e:
            log.error(f"Download prepare failed: {e}")
            await query.answer(f"❌ {str(e)[:180]}", show_alert=True)


async def _prepare_download(uid: int, content_id: str):
    """Resolve + read the MPD and build the 'choose quality' message. Returns (text, markup). Raises on failure."""
    playable = await _resolve_for_user(uid, content_id)
    if not (playable.mpd_url or playable.stream_url):
        raise sony.SonyLIVError("No download available for this title")
    token = _valid_token(uid)
    streams = {"video": [], "audio": [], "subs": []}
    if playable.mpd_url:
        streams = await dl_engine.get_streams(await get_session(), playable, token)
    _pending[uid] = {"playable": playable, "token": token, "ts": time.time()}

    # one button per resolution (highest bitrate each), best first
    by_h: Dict[int, dict] = {}
    for v in streams["video"]:
        if v["height"] and v["height"] not in by_h:
            by_h[v["height"]] = v
    btns = [[Btn(text="⭐ Best quality", callback_data="q_best")]]
    row = []
    for h in sorted(by_h, reverse=True)[:6]:
        row.append(Btn(text=f"{h}p · {by_h[h]['kbps']/1000:.1f} Mbps", callback_data=f"q_{by_h[h]['id']}"[:64]))
        if len(row) == 2:
            btns.append(row); row = []
    if row:
        btns.append(row)
    langs = ", ".join(sorted({a['lang'].upper() for a in streams["audio"]})) or "-"
    dur = f"{playable.duration//60}m {playable.duration%60:02d}s" if playable.duration else "?"
    text = (f"<b>{html.escape(playable.title)}</b>\n"
            f"{'🔐 DRM' if playable.is_drm else '✅ Non-DRM'} · ⏱ {dur} · 🌍 {playable.cluster}\n"
            f"🔊 Audio: {langs}\n\n<b>Choose quality:</b>")
    return text, Markup(btns)


# ---- paste a SonyLIV link -> quality menu (no /search needed) ----
SONY_URL_RE = re.compile(r"(?<![\w./@-])(?:https?://)?(?:www\.)?sonyliv\.com/[^\s]+", re.I)


@app.on_message(filters.private & filters.text & filters.regex(SONY_URL_RE))
async def on_sony_link(client, message):
    uid = message.from_user.id
    if uid in _active:
        await safe_send(message.chat.id, "⏳ You already have a download running.")
        return
    url = SONY_URL_RE.search(message.text).group(0)
    cid = sony.extract_content_id(url)
    if not re.fullmatch(r"\d{7,12}", cid or ""):
        await safe_send(message.chat.id, "❌ Is link mein koi valid SonyLIV ID nahi mili. Episode ya movie ka link bhejo.")
        return
    status = await safe_send(message.chat.id, "🔎 Analysing link...")
    try:
        text, markup = await _prepare_download(uid, cid)
        await status.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    except Exception as e:
        log.error(f"Link failed: {e}")
        hint = ""
        path = url.split("?")[0].rstrip("/")
        if "/shows/" in path and len(re.findall(r"\d{7,12}", path)) == 1:
            hint = "\n\nℹ️ Ye poore show ka link lag raha hai - kisi episode ka link bhejo."
        try:
            await status.edit_text(f"❌ {html.escape(str(e)[:300])}{hint}", parse_mode=ParseMode.HTML)
        except Exception:
            pass


_pending: Dict[int, dict] = {}   # user_id -> {playable, token, ts} (waiting for a quality tap)
_active: set = set()             # users with a running download


def _valid_token(user_id: int) -> Optional[str]:
    t = store.get_user_token(user_id)
    return t if t and not sony._is_token_expired(t) else None


@app.on_callback_query(filters.regex(r"^q_"))
async def on_quality(client, query):
    """Quality picked -> download, decrypt, mux, upload."""
    uid = query.message.chat.id
    pend = _pending.pop(uid, None)
    if not pend or time.time() - pend["ts"] > 1800:
        await query.answer("⌛ Session expired - search again.", show_alert=True)
        return
    if uid in _active:
        await query.answer("⏳ You already have a download running.", show_alert=True)
        return
    video_id = query.data[2:]
    playable, token = pend["playable"], pend["token"]
    await query.answer("▶️ Starting")
    _active.add(uid)
    asyncio.create_task(_run_download(query.message, uid, playable, token, video_id))


async def _make_thumb(video: str, dur: float, out_dir: str) -> Optional[str]:
    """One frame at ~10% as a small JPEG so Telegram shows a preview. None on failure."""
    thumb = os.path.join(out_dir, "thumb.jpg")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-ss", str(int((dur or 0) * 0.1)), "-i", video,
            "-frames:v", "1", "-vf", "scale=320:-2", thumb,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), timeout=60)
        return thumb if os.path.exists(thumb) and os.path.getsize(thumb) > 0 else None
    except Exception:
        return None


async def _run_download(msg, uid: int, playable, token, video_id: str):
    out_dir = os.path.join(DOWNLOAD_DIR, str(uid), playable.content_id)
    title = html.escape(playable.title)
    state = {"t": 0.0}

    async def edit(text: str):
        try:
            await msg.edit_text(text, parse_mode=ParseMode.HTML)
        except MessageNotModified:
            pass
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except Exception:
            pass

    def throttled(text: str):
        if time.time() - state["t"] >= 4:
            state["t"] = time.time()
            asyncio.create_task(edit(text))

    def dl_progress(pct, speed, eta):
        throttled(f"⬇️ <b>{title}</b>\n{pct:.1f}% · {speed} · ETA {eta}")

    try:
        await edit(f"⬇️ <b>{title}</b>\nStarting download...")
        mkv = await dl_engine.download(await get_session(), playable, out_dir,
                                       video_id=None if video_id == "best" else video_id,
                                       audio_langs=[x for x in os.getenv("AUDIO_LANGS", "").split(",") if x.strip()] or None,
                                       sub_langs=[x for x in os.getenv("SUB_LANGS", "").split(",") if x.strip()] or None,
                                       token=token, progress=dl_progress)
        final = mkv
        if OUTPUT_FORMAT == "mp4":
            # same converter fbot uses for AnimeHD: remux (seconds, lossless) -> remux+aac -> re-encode only if needed
            await edit(f"🎞 <b>{title}</b>\nConverting to MP4...")
            final = await mp4_convert.ensure_mp4(mkv, mp4_convert.status_progress(msg, playable.title))
        await edit(f"📦 <b>{title}</b>\nPreparing upload...")

        if os.path.getsize(final) <= MAX_FILE_BYTES:
            parts = [final]
        elif final.lower().endswith(".mp4"):
            parts = await asyncio.to_thread(split_upload.split_video_file, final, out_dir,
                                            dl_engine.safe_name(playable.title), MAX_FILE_BYTES)
        else:
            parts = await dl_engine.split_for_telegram(final, MAX_FILE_BYTES)

        for i, part in enumerate(parts, 1):
            tag = f" (part {i}/{len(parts)})" if len(parts) > 1 else ""

            async def up_progress(cur, total, _t=tag):
                throttled(f"⬆️ <b>{title}</b>{_t}\n{cur*100/total:.1f}% of {total/1048576:.0f} MB")

            cap = f"🎬 {playable.title}{tag}"
            if part.lower().endswith(".mp4"):
                dur, w, h = await asyncio.to_thread(split_upload._probe, part)
                thumb = await _make_thumb(part, dur, out_dir)
                await app.send_video(uid, part, caption=cap, supports_streaming=True, thumb=thumb,
                                     duration=int(dur or 0), width=w or 0, height=h or 0, progress=up_progress)
            else:
                await app.send_document(uid, part, caption=cap, progress=up_progress)

        store.add_download(uid, playable.content_id, playable.title, playable.mpd_url or playable.stream_url or "")
        await store.flush()
        await edit(f"✅ <b>{title}</b>\nDone.")
    except dl_engine.DownloadError as e:
        log.error(f"Download failed: {e}")
        await edit(f"❌ <b>{title}</b>\n{html.escape(str(e))}")
    except Exception as e:
        log.exception("Download crashed")
        await edit(f"❌ <b>{title}</b>\nUnexpected error: {html.escape(str(e)[:300])}")
    finally:
        _active.discard(uid)
        shutil.rmtree(out_dir, ignore_errors=True)


# ---- command: /downloads ----
@app.on_message(filters.private & filters.command("downloads"))
async def on_downloads(client, message):
    items = store.get_downloads(message.from_user.id, limit=10)
    if not items:
        await safe_send(message.chat.id, "📥 No downloads yet.")
        return
    lines = [f"• {html.escape(i['title'])} <i>({i['date']})</i>" for i in reversed(items)]
    await safe_send(message.chat.id, "📥 <b>Recent downloads</b>\n\n" + "\n".join(lines), parse_mode="html")


# ---- admin: /ban /unban ----
@app.on_message(filters.private & filters.command(["ban", "unban"]))
async def on_ban(client, message):
    if message.from_user.id not in ADMINS:
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await safe_send(message.chat.id, "Usage: /ban <user_id>  or  /unban <user_id>")
        return
    uid = int(parts[1])
    if parts[0].lower().startswith("/ban"):
        store.ban_user(uid)
    else:
        store.unban_user(uid)
    await store.flush()
    await safe_send(message.chat.id, f"✅ Done for {uid}")


@app.on_message(filters.private & filters.incoming, group=-1)
async def _ban_gate(client, message):
    """Banned users are silently ignored."""
    if message.from_user and store.is_banned(message.from_user.id):
        message.stop_propagation()


# ---- command: /stats (admin) ----
@app.on_message(filters.private & filters.command("stats"))
async def on_stats(client, message):
    """Show bot statistics."""
    if message.from_user.id not in ADMINS:
        return

    stats = store.get_stats()
    msg = (
        f"📈 <b>Bot Statistics</b>\n\n"
        f"👥 Users: {stats['users']}\n"
        f"📥 Downloads: {stats['downloads']}\n"
        f"📺 Channels: {stats['channels']}\n"
        f"⛔ Banned: {stats['banned']}\n"
        f"🍪 Cookies stored: {stats.get('cookies', 0)}"
    )
    await safe_send(message.chat.id, msg, parse_mode="html")


# ---- bot startup ----
async def on_startup():
    """Called when bot starts."""
    log.info("SonyLIV Bot starting up...")
    log.info(f"Users: {len(store.data['users'])}")
    log.info(f"Cookies: {sum(len(u.get('cookies', [])) for u in store.data['users'].values())}")
    
    await app.set_bot_commands([
        BotCommand("start", "Start the bot"),
        BotCommand("help", "❓ How to use"),
        BotCommand("about", "ℹ️ About this bot"),
        BotCommand("login", "Sign in with OTP"),
        BotCommand("token", "Show token status"),
        BotCommand("setcookies", "🍪 Add SonyLIV cookies (fallback auth)"),
        BotCommand("clearcookies", "🗑 Remove stored cookies"),
        BotCommand("mycookies", "📊 Show cookie count"),
        BotCommand("search", "🔍 Search for content"),
        BotCommand("downloads", "📥 Recent downloads"),
        BotCommand("stats", "📈 [Admin] Bot stats"),
    ])
    log.info("✅ Bot ready!")


# ---- graceful shutdown ----
async def on_shutdown():
    """Called when bot stops."""
    await store.flush()
    if _session:
        await _session.close()
    log.info("Bot stopped")


async def main():
    await app.start()
    await on_startup()
    try:
        await idle()
    finally:
        await on_shutdown()
        await app.stop()


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
