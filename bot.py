#!/usr/bin/env python3
"""
🕵️ Claude Incognito Telegram Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Docker-ready — all config hardcoded below.
⚠️ UNOFFICIAL — Uses claude.ai web API. May break at any time.
"""

import json
import uuid
import re
import os
import io
import html as html_lib
import time
import logging
import base64
import signal
import sys
from typing import Optional
from dataclasses import dataclass, field
from collections import defaultdict

import requests
from telebot import TeleBot, apihelper
from telebot.types import Message, BotCommand

# ═══════════════════════════════════════════════════════════════════
#                        !! CONFIGURATION !!
#              Edit these values before building/running
# ═══════════════════════════════════════════════════════════════════

BOT_TOKEN     = "8891866405:AAFOavJJq6Pv_KMl94JXxH26kistSO4NzqY"

# Allowed Telegram user IDs — leave empty list [] to allow everyone
ADMIN_IDS     = []                          # e.g. [123456789, 987654321]

DEFAULT_MODEL = "claude-sonnet-4-20250514"  # Claude model to use

# Delete conversations after every reply (max stealth)
# Set False to only delete on bot shutdown
AUTO_WIPE     = False

# Minimum characters in a code block to send it as a file
FILE_SIZE_MIN = 200

# Maximum characters per Telegram message (hard limit is 4096)
MAX_CHUNK     = 4000

# Logging level: DEBUG | INFO | WARNING | ERROR
LOG_LEVEL     = "INFO"

# ═══════════════════════════════════════════════════════════════════
#                    INTERNAL CONSTANTS (do not touch)
# ═══════════════════════════════════════════════════════════════════

BASE_URL = "https://claude.ai/api"

# ── Logging setup ─────────────────────────────────────────────────
os.makedirs("/app/logs", exist_ok=True)

logging.basicConfig(
    level    = getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format   = "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/app/logs/bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("ClaudeBot")

# ── Startup validation ────────────────────────────────────────────
if not BOT_TOKEN or "YOUR_TELEGRAM_BOT_TOKEN_HERE" in BOT_TOKEN:
    log.critical("BOT_TOKEN is not configured. Edit bot.py and rebuild.")
    sys.exit(1)

log.info("━━━ Claude Incognito Telegram Bot ━━━")
log.info(f"Model    : {DEFAULT_MODEL}")
log.info(f"AutoWipe : {AUTO_WIPE}")
log.info(f"Admins   : {ADMIN_IDS or 'Everyone'}")
log.info(f"LogLevel : {LOG_LEVEL}")

bot = TeleBot(BOT_TOKEN, parse_mode="HTML")

# ═══════════════════════════════════════════════════════════════════
#                        DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════════

@dataclass
class UserSession:
    session_key     : str  = ""
    organization_id : str  = ""
    conversation_id : str  = ""
    model           : str  = field(default_factory=lambda: DEFAULT_MODEL)
    tracked_convs   : list = field(default_factory=list)
    history         : list = field(default_factory=list)
    http            : requests.Session = field(default_factory=requests.Session)
    incognito       : bool = True
    busy            : bool = False

    def __post_init__(self):
        """Initialize session with full browser-like headers."""
        self.http.headers.update({
            "User-Agent"           : (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept"               : "*/*",
            "Accept-Language"      : "en-US,en;q=0.9",
            "Accept-Encoding"      : "gzip, deflate, br",
            "Content-Type"         : "application/json",
            "Origin"               : "https://claude.ai",
            "Referer"              : "https://claude.ai/chats",
            "Sec-Ch-Ua"            : '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "Sec-Ch-Ua-Mobile"     : "?0",
            "Sec-Ch-Ua-Platform"   : '"Windows"',
            "Sec-Fetch-Dest"       : "empty",
            "Sec-Fetch-Mode"       : "cors",
            "Sec-Fetch-Site"       : "same-origin",
        })

    def set_key(self, key: str):
        """Set session key with proper cookie configuration."""
        self.session_key = key
        # Clear any existing sessionKey cookies
        self.http.cookies.clear()
        # Set with proper domain (note the leading dot)
        self.http.cookies.set(
            name   = "sessionKey",
            value  = key,
            domain = ".claude.ai",
            path   = "/",
            secure = True,
        )
        log.debug(f"Set session key cookie: {key[:20]}...")


# Global store: { telegram_user_id: UserSession }
sessions: dict[int, UserSession] = {}


def get_session(uid: int) -> UserSession:
    if uid not in sessions:
        sessions[uid] = UserSession()
    return sessions[uid]


# ═══════════════════════════════════════════════════════════════════
#                     CLAUDE API FUNCTIONS
# ═══════════════════════════════════════════════════════════════════

def validate_key(session_key: str) -> tuple[bool, str, str]:
    """
    Validate a Claude session key.
    Returns (is_valid, org_id, org_name_or_error).
    """
    s = requests.Session()
    s.headers.update({
        "User-Agent"           : (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept"               : "*/*",
        "Accept-Language"      : "en-US,en;q=0.9",
        "Content-Type"         : "application/json",
        "Origin"               : "https://claude.ai",
        "Referer"              : "https://claude.ai/chats",
        "Sec-Ch-Ua"            : '"Chromium";v="124", "Google Chrome";v="124"',
        "Sec-Ch-Ua-Mobile"     : "?0",
        "Sec-Ch-Ua-Platform"   : '"Windows"',
        "Sec-Fetch-Dest"       : "empty",
        "Sec-Fetch-Mode"       : "cors",
        "Sec-Fetch-Site"       : "same-origin",
    })
    s.cookies.set(
        name   = "sessionKey",
        value  = session_key,
        domain = ".claude.ai",
        path   = "/",
        secure = True,
    )
    
    try:
        log.debug(f"Validating key: {session_key[:20]}...")
        resp = s.get(f"{BASE_URL}/organizations", timeout=15)
        
        log.debug(f"Validation response status: {resp.status_code}")
        
        if resp.status_code == 403:
            log.warning("Validation got 403 - key expired or invalid")
            return (False, "", "Expired / Invalid")
        
        if resp.status_code == 401:
            log.warning("Validation got 401 - unauthorized")
            return (False, "", "Unauthorized / Invalid Key")
            
        resp.raise_for_status()
        orgs = resp.json()
        
        if not orgs:
            return (False, "", "No organizations found")
            
        org_name = orgs[0].get("name", "Unknown Organization")
        org_id   = orgs[0]["uuid"]
        
        log.info(f"Key validated ✓ Org: {org_name}")
        return (True, org_id, org_name)
        
    except requests.exceptions.Timeout:
        log.error("Validation timed out")
        return (False, "", "Request timed out")
    except requests.exceptions.ConnectionError:
        log.error("Connection error during validation")
        return (False, "", "Connection error")
    except Exception as e:
        log.error(f"Validation error: {e}")
        return (False, "", str(e))


def create_conversation(us: UserSession) -> str:
    """Create a new blank incognito conversation."""
    url     = f"{BASE_URL}/organizations/{us.organization_id}/chat_conversations"
    payload = {"uuid": str(uuid.uuid4()), "name": "", "model": us.model}
    
    try:
        resp = us.http.post(url, json=payload, timeout=15)
        log.debug(f"Create conversation status: {resp.status_code}")
        resp.raise_for_status()
        
        cid = resp.json()["uuid"]
        us.conversation_id = cid
        us.tracked_convs.append(cid)
        us.history = []
        log.info(f"Created conversation: {cid[:12]}...")
        return cid
        
    except requests.exceptions.HTTPError as e:
        log.error(f"Failed to create conversation: {e}")
        if e.response is not None:
            log.error(f"Response body: {e.response.text[:500]}")
        raise


def delete_conversation(us: UserSession, conv_id: str) -> bool:
    """Silently delete a conversation by ID."""
    url = (
        f"{BASE_URL}/organizations/{us.organization_id}"
        f"/chat_conversations/{conv_id}"
    )
    try:
        r = us.http.delete(url, timeout=10)
        if conv_id in us.tracked_convs:
            us.tracked_convs.remove(conv_id)
        log.debug(f"Deleted conversation: {conv_id[:12]}...")
        return r.ok or r.status_code == 204
    except Exception as e:
        log.warning(f"Failed to delete conversation: {e}")
        return False


def wipe_all(us: UserSession):
    """Delete every tracked conversation for this user."""
    count = len(us.tracked_convs)
    for cid in list(us.tracked_convs):
        delete_conversation(us, cid)
    us.conversation_id = ""
    us.history = []
    log.info(f"Wiped {count} conversation(s)")


def send_message(us: UserSession, text: str, attachments: list = None) -> dict:
    """
    Send a message to Claude and stream the full response.
    Returns { 'text': str, 'files': list }
    """
    if not us.conversation_id:
        create_conversation(us)

    url = (
        f"{BASE_URL}/organizations/{us.organization_id}"
        f"/chat_conversations/{us.conversation_id}/completion"
    )
    
    payload = {
        "prompt"     : text,
        "timezone"   : "UTC",
        "attachments": attachments or [],
        "files"      : [],
    }

    try:
        log.debug(f"Sending message to conversation {us.conversation_id[:12]}...")
        log.debug(f"Cookies: {us.http.cookies}")
        
        resp = us.http.post(
            url, 
            json    = payload, 
            stream  = True, 
            timeout = 120
        )
        
        log.debug(f"Completion response status: {resp.status_code}")
        
        if resp.status_code == 403:
            log.error("Got 403 on completion endpoint")
            log.error(f"Request headers: {dict(resp.request.headers)}")
            log.error(f"Cookies sent: {resp.request._cookies}")
            log.error(f"Response: {resp.text[:500]}")
            raise requests.exceptions.HTTPError(
                response=resp,
                request=resp.request
            )
        
        resp.raise_for_status()

        full_text = ""
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
                etype = event.get("type", "")
                if etype == "completion":
                    full_text += event.get("completion", "")
                elif etype == "error":
                    err_msg = event.get("error", {}).get("message", "Unknown error")
                    log.error(f"Claude API error: {err_msg}")
                    raise RuntimeError(err_msg)
            except json.JSONDecodeError:
                continue

        log.info(f"Received {len(full_text)} chars from Claude")

        # Incognito: wipe immediately after reply
        if us.incognito and AUTO_WIPE and us.conversation_id:
            cid = us.conversation_id
            us.conversation_id = ""
            delete_conversation(us, cid)

        return {"text": full_text, "files": extract_code_files(full_text)}
        
    except requests.exceptions.HTTPError as e:
        log.error(f"HTTP error during completion: {e}")
        raise
    except Exception as e:
        log.error(f"Error during send_message: {e}")
        raise


# ═══════════════════════════════════════════════════════════════════
#               CODE EXTRACTION & FILE GENERATION
# ═══════════════════════════════════════════════════════════════════

LANG_EXTENSIONS = {
    "python": ".py",      "py": ".py",
    "javascript": ".js",  "js": ".js",
    "typescript": ".ts",  "ts": ".ts",
    "java": ".java",      "c": ".c",
    "cpp": ".cpp",        "c++": ".cpp",
    "csharp": ".cs",      "cs": ".cs",
    "go": ".go",          "rust": ".rs",
    "ruby": ".rb",        "php": ".php",
    "swift": ".swift",    "kotlin": ".kt",
    "scala": ".scala",    "html": ".html",
    "css": ".css",        "scss": ".scss",
    "sql": ".sql",        "bash": ".sh",
    "sh": ".sh",          "shell": ".sh",
    "zsh": ".sh",         "yaml": ".yaml",
    "yml": ".yaml",       "toml": ".toml",
    "json": ".json",      "xml": ".xml",
    "markdown": ".md",    "md": ".md",
    "dockerfile": "Dockerfile",
    "docker": "Dockerfile",
    "makefile": "Makefile",
    "r": ".r",            "lua": ".lua",
    "perl": ".pl",        "dart": ".dart",
    "vue": ".vue",        "svelte": ".svelte",
    "jsx": ".jsx",        "tsx": ".tsx",
    "graphql": ".graphql",
    "proto": ".proto",    "tf": ".tf",
    "powershell": ".ps1", "ps1": ".ps1",
    "bat": ".bat",
}


def extract_code_files(text: str) -> list:
    """Extract code blocks from response and return as sendable files."""
    pattern = r"```(\w+)?\n(.*?)```"
    matches = re.findall(pattern, text, re.DOTALL)
    files   = []
    counter = defaultdict(int)

    for lang, code in matches:
        lang = (lang or "txt").lower().strip()
        if len(code.strip()) < FILE_SIZE_MIN:
            continue
        ext      = LANG_EXTENSIONS.get(lang, f".{lang}" if lang != "txt" else ".txt")
        counter[lang] += 1
        idx      = f"_{counter[lang]}" if counter[lang] > 1 else ""
        filename = guess_filename(code, lang) or f"code{idx}{ext}"
        files.append({"name": filename, "content": code.strip(), "language": lang})

    return files


def guess_filename(code: str, lang: str) -> Optional[str]:
    """Try to guess a meaningful filename from the code content."""
    ext = LANG_EXTENSIONS.get(lang, f".{lang}")

    if lang in ("python", "py"):
        m = re.search(r"^(?:class|def)\s+(\w+)", code, re.MULTILINE)
        if m:
            return f"{m.group(1).lower()}{ext}"

    if lang in ("javascript", "js", "typescript", "ts", "jsx", "tsx"):
        m = re.search(
            r"(?:export\s+default\s+(?:class|function)\s+|function\s+)(\w+)", code
        )
        if m:
            return f"{m.group(1)}{ext}"

    if lang == "html":
        m = re.search(r"<title>(.*?)</title>", code, re.IGNORECASE)
        if m:
            safe = re.sub(r"[^\w\s-]", "", m.group(1)).strip().replace(" ", "_")[:30]
            return f"{safe or 'index'}.html"

    return None


# ═══════════════════════════════════════════════════════════════════
#            MARKDOWN → TELEGRAM HTML CONVERTER
# ═══════════════════════════════════════════════════════════════════

def md_to_tg_html(text: str) -> str:
    """Convert Claude's Markdown output to Telegram-safe HTML."""
    chunks        = []
    pos           = 0
    code_block_re = re.compile(r"```(\w+)?\n(.*?)```", re.DOTALL)

    for m in code_block_re.finditer(text):
        chunks.append(_convert_inline(text[pos:m.start()]))
        lang = m.group(1) or ""
        code = html_lib.escape(m.group(2).rstrip())
        if lang:
            chunks.append(f'<pre><code class="language-{lang}">{code}</code></pre>')
        else:
            chunks.append(f"<pre>{code}</pre>")
        pos = m.end()

    chunks.append(_convert_inline(text[pos:]))
    return "".join(chunks)


def _convert_inline(text: str) -> str:
    """Convert inline markdown elements to Telegram HTML."""
    text = html_lib.escape(text)
    text = re.sub(r"`([^`\n]+)`",               r"<code>\1</code>",     text)
    text = re.sub(r"\*\*(.+?)\*\*",             r"<b>\1</b>",           text, flags=re.DOTALL)
    text = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<i>\1</i>",           text)
    text = re.sub(r"~~(.+?)~~",                 r"<s>\1</s>",           text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",   r'<a href="\2">\1</a>', text)
    text = re.sub(r"^#{1,6}\s+(.+)$",           r"<b>\1</b>",           text, flags=re.MULTILINE)
    text = re.sub(r"^[-•]\s+",                  "• ",                   text, flags=re.MULTILINE)
    return text


# ═══════════════════════════════════════════════════════════════════
#               CHUNK MESSAGING SYSTEM
# ═══════════════════════════════════════════════════════════════════

def smart_split(text: str, max_len: int = MAX_CHUNK) -> list[str]:
    """
    Intelligently split a long message into Telegram-safe chunks.
    Respects code blocks, paragraphs, and lines.
    """
    if len(text) <= max_len:
        return [text]

    chunks    = []
    remaining = text

    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break

        segment = remaining[:max_len]

        if "</pre>" in segment[max_len // 2:]:
            cut = segment.rfind("</pre>") + len("</pre>")
        elif "\n\n" in segment[max_len // 2:]:
            cut = segment.rfind("\n\n") + 1
        elif "\n" in segment[max_len // 2:]:
            cut = segment.rfind("\n") + 1
        elif " " in segment[max_len // 2:]:
            cut = segment.rfind(" ") + 1
        else:
            cut = max_len

        chunks.append(remaining[:cut])
        remaining = remaining[cut:]

    return [_fix_unclosed_tags(c) for c in chunks]


def _fix_unclosed_tags(chunk: str) -> str:
    """Close any HTML tags that were split across chunks."""
    for open_tag, close_tag in [
        ("<pre>",  "</pre>"),
        ("<code>", "</code>"),
        ("<b>",    "</b>"),
        ("<i>",    "</i>"),
        ("<s>",    "</s>"),
    ]:
        opens  = chunk.count(open_tag)
        closes = chunk.count(close_tag)
        if opens > closes:
            chunk += close_tag * (opens - closes)
        elif closes > opens:
            chunk = (open_tag * (closes - opens)) + chunk
    return chunk


def send_chunked(chat_id: int, text: str, reply_to: int = None):
    """Send a long message split into smart chunks."""
    chunks = smart_split(text)
    total  = len(chunks)

    for i, chunk in enumerate(chunks):
        if total > 1:
            chunk = f"<i>📄 Part {i+1}/{total}</i>\n\n" + chunk
        try:
            bot.send_message(
                chat_id, chunk,
                parse_mode               = "HTML",
                reply_to_message_id      = reply_to if i == 0 else None,
                disable_web_page_preview = True,
            )
        except apihelper.ApiTelegramException as e:
            if "can't parse entities" in str(e).lower():
                plain = re.sub(r"<[^>]+>", "", chunk)
                bot.send_message(
                    chat_id, plain,
                    reply_to_message_id = reply_to if i == 0 else None,
                )
            else:
                raise
        if i < total - 1:
            time.sleep(0.3)


def send_files(chat_id: int, files: list, reply_to: int = None):
    """Send extracted code blocks as downloadable Telegram files."""
    for f in files:
        try:
            bio      = io.BytesIO(f["content"].encode("utf-8"))
            bio.name = f["name"]
            bot.send_document(
                chat_id, bio,
                caption             = f"📎 <code>{f['name']}</code>  ({f['language']})",
                parse_mode          = "HTML",
                reply_to_message_id = reply_to,
            )
        except Exception as e:
            log.warning(f"Failed to send file {f['name']}: {e}")


# ═══════════════════════════════════════════════════════════════════
#                    ACCESS CONTROL
# ═══════════════════════════════════════════════════════════════════

def is_authorized(msg: Message) -> bool:
    if not ADMIN_IDS:
        return True
    return msg.from_user.id in ADMIN_IDS


def auth_check(func):
    def wrapper(msg: Message, *args, **kwargs):
        if not is_authorized(msg):
            bot.reply_to(
                msg,
                "🚫 <b>Access Denied</b>\n"
                f"Your ID (<code>{msg.from_user.id}</code>) is not authorized.",
                parse_mode="HTML",
            )
            log.warning(f"Unauthorized access from user {msg.from_user.id}")
            return
        return func(msg, *args, **kwargs)
    return wrapper


# ═══════════════════════════════════════════════════════════════════
#                    BOT COMMAND HANDLERS
# ═══════════════════════════════════════════════════════════════════

@bot.message_handler(commands=["start", "help"])
@auth_check
def cmd_start(msg: Message):
    bot.reply_to(msg, """
🕵️ <b>Claude Incognito Bot</b>

Chat with Claude — all conversations <b>auto-deleted</b> (incognito).

<b>━━━ Setup ━━━</b>
/setkey <code>&lt;session_key&gt;</code> — Set your Claude session key
/validate — Check if your current key still works
/massvalidate — Bulk validate multiple keys

<b>━━━ Chat ━━━</b>
Just send any message! Files and images supported.

<b>━━━ Controls ━━━</b>
/newchat   — Start fresh conversation
/model     — Change Claude model
/incognito — Toggle incognito mode
/wipe      — Delete all tracked chats
/status    — Session info
/myid      — Show your Telegram user ID

<b>━━━ Get Session Key ━━━</b>
1. Go to <a href="https://claude.ai">claude.ai</a>
2. Login to your account
3. Press F12 → Application → Cookies
4. Find <code>sessionKey</code> cookie
5. Copy the value (starts with sk-ant-sid01-)
6. Send: /setkey &lt;paste_here&gt;
""".strip(), parse_mode="HTML", disable_web_page_preview=True)


@bot.message_handler(commands=["myid"])
def cmd_myid(msg: Message):
    bot.reply_to(
        msg,
        f"🪪 Your Telegram User ID: <code>{msg.from_user.id}</code>",
        parse_mode="HTML",
    )


# ── Set Key ────────────────────────────────────────────────────────

@bot.message_handler(commands=["setkey"])
@auth_check
def cmd_setkey(msg: Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(
            msg,
            "⚠️ <b>Usage:</b> /setkey <code>&lt;your_session_key&gt;</code>\n\n"
            "Get your key from:\n"
            "1. <a href='https://claude.ai'>claude.ai</a> → Login\n"
            "2. F12 → Application → Cookies\n"
            "3. Copy <code>sessionKey</code> value",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    key = parts[1].strip()
    uid = msg.from_user.id
    us  = get_session(uid)

    # Delete message immediately — it contains the secret key
    try:
        bot.delete_message(msg.chat.id, msg.message_id)
        log.info(f"Deleted /setkey message from user {uid} for security")
    except Exception as e:
        log.warning(f"Could not delete /setkey message: {e}")

    thinking = bot.send_message(msg.chat.id, "🔄 <i>Validating your key...</i>", parse_mode="HTML")
    
    valid, org_id, info = validate_key(key)

    try:
        bot.delete_message(msg.chat.id, thinking.message_id)
    except Exception:
        pass

    if not valid:
        bot.send_message(
            msg.chat.id,
            f"❌ <b>Invalid Session Key</b>\n\n"
            f"Error: <code>{html_lib.escape(info)}</code>\n\n"
            f"<b>Troubleshooting:</b>\n"
            f"• Make sure you copied the FULL key\n"
            f"• Key should start with <code>sk-ant-sid01-</code>\n"
            f"• Try getting a fresh key from claude.ai\n"
            f"• Make sure you're logged in to claude.ai",
            parse_mode="HTML",
        )
        return

    # Wipe old conversations if switching keys
    if us.organization_id and us.organization_id != org_id:
        wipe_all(us)
        log.info(f"User {uid} switched organizations, wiped old conversations")

    us.set_key(key)
    us.organization_id = org_id
    log.info(f"User {uid} successfully set key for org: {info}")

    bot.send_message(
        msg.chat.id,
        f"✅ <b>Session Key Configured!</b>\n\n"
        f"🏢 Organization: <code>{html_lib.escape(info)}</code>\n"
        f"🕵️ Incognito: <b>{'ON' if us.incognito else 'OFF'}</b>\n"
        f"🤖 Model: <code>{us.model}</code>\n\n"
        f"<i>🔐 Your key message was deleted for security.</i>\n\n"
        f"Start chatting now! Just send any message.",
        parse_mode="HTML",
    )


# ── Validate ───────────────────────────────────────────────────────

@bot.message_handler(commands=["validate"])
@auth_check
def cmd_validate(msg: Message):
    us = get_session(msg.from_user.id)
    if not us.session_key:
        bot.reply_to(msg, 
            "⚠️ <b>No session key configured</b>\n\n"
            "Use /setkey first to set your Claude session key.",
            parse_mode="HTML")
        return

    bot.send_chat_action(msg.chat.id, "typing")
    valid, _, info = validate_key(us.session_key)

    if valid:
        bot.reply_to(
            msg,
            f"✅ <b>Session Key is Valid!</b>\n\n"
            f"🏢 Organization: <code>{html_lib.escape(info)}</code>\n"
            f"✨ Your key is working correctly.",
            parse_mode="HTML",
        )
    else:
        bot.reply_to(
            msg,
            f"❌ <b>Session Key Expired or Invalid</b>\n\n"
            f"Error: <code>{html_lib.escape(info)}</code>\n\n"
            f"Use /setkey to configure a new key.",
            parse_mode="HTML",
        )


# ── Mass Validate ──────────────────────────────────────────────────

@bot.message_handler(commands=["massvalidate"])
@auth_check
def cmd_massvalidate(msg: Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(
            msg,
            "📋 <b>Mass Key Validator</b>\n\n"
            "Paste multiple session keys, one per line:\n\n"
            "<code>/massvalidate\n"
            "sk-ant-sid01-key1...\n"
            "sk-ant-sid01-key2...\n"
            "sk-ant-sid01-key3...</code>",
            parse_mode="HTML",
        )
        return

    keys  = [k.strip() for k in parts[1].strip().split("\n") if k.strip()]
    total = len(keys)

    # Delete original message — it contains keys
    try:
        bot.delete_message(msg.chat.id, msg.message_id)
    except Exception:
        pass

    status_msg = bot.send_message(
        msg.chat.id,
        f"🔄 Validating <b>{total}</b> key(s)…",
        parse_mode="HTML",
    )

    results_valid   = []
    results_invalid = []

    for i, key in enumerate(keys):
        short = key[:20] + "…" + key[-8:] if len(key) > 30 else key
        valid, _, info = validate_key(key)

        if valid:
            results_valid.append(
                f"  ✅ <code>{html_lib.escape(short)}</code>\n     → {html_lib.escape(info)}"
            )
        else:
            results_invalid.append(
                f"  ❌ <code>{html_lib.escape(short)}</code>\n     → {html_lib.escape(info)}"
            )

        if (i + 1) % 3 == 0 or i == total - 1:
            try:
                bot.edit_message_text(
                    f"🔄 Validating… <b>{i+1}/{total}</b>",
                    chat_id    = status_msg.chat.id,
                    message_id = status_msg.message_id,
                    parse_mode = "HTML",
                )
            except Exception:
                pass

    report = (
        f"📊 <b>Mass Validation Report</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Total: {total}  |  ✅ {len(results_valid)}  |  ❌ {len(results_invalid)}\n\n"
    )
    if results_valid:
        report += "<b>✅ Valid Keys:</b>\n" + "\n".join(results_valid) + "\n\n"
    if results_invalid:
        report += "<b>❌ Invalid Keys:</b>\n" + "\n".join(results_invalid)

    try:
        bot.delete_message(status_msg.chat.id, status_msg.message_id)
    except Exception:
        pass

    send_chunked(msg.chat.id, report.strip())
    log.info(f"Mass validate completed: {len(results_valid)}/{total} valid")


# ── New Chat ───────────────────────────────────────────────────────

@bot.message_handler(commands=["newchat"])
@auth_check
def cmd_newchat(msg: Message):
    us = get_session(msg.from_user.id)
    if not us.session_key or not us.organization_id:
        bot.reply_to(msg, "⚠️ No session key configured. Use /setkey first.")
        return

    if us.conversation_id:
        delete_conversation(us, us.conversation_id)
        us.conversation_id = ""

    us.history = []
    bot.reply_to(
        msg,
        "🆕 <b>New conversation started!</b>\n<i>Previous chat was deleted.</i>",
        parse_mode="HTML",
    )


# ── Model ──────────────────────────────────────────────────────────

@bot.message_handler(commands=["model"])
@auth_check
def cmd_model(msg: Message):
    parts = msg.text.split(maxsplit=1)
    us    = get_session(msg.from_user.id)

    if len(parts) < 2:
        bot.reply_to(
            msg,
            f"🤖 Current model: <code>{us.model}</code>\n\n"
            "<b>Available models:</b>\n"
            "• <code>claude-sonnet-4-20250514</code> (latest, best)\n"
            "• <code>claude-3-5-sonnet-20241022</code>\n"
            "• <code>claude-3-5-haiku-20241022</code> (fast)\n"
            "• <code>claude-3-opus-20240229</code>\n\n"
            "Usage: /model <code>&lt;model_name&gt;</code>",
            parse_mode="HTML",
        )
        return

    us.model = parts[1].strip()
    bot.reply_to(msg, f"✅ Model changed to: <code>{us.model}</code>", parse_mode="HTML")


# ── Incognito Toggle ───────────────────────────────────────────────

@bot.message_handler(commands=["incognito"])
@auth_check
def cmd_incognito(msg: Message):
    us           = get_session(msg.from_user.id)
    us.incognito = not us.incognito
    state        = "ON 🟢" if us.incognito else "OFF 🔴"
    bot.reply_to(
        msg,
        f"🕵️ Incognito mode: <b>{state}</b>\n\n"
        + (
            "<i>✅ All conversations will be deleted after each reply.\n"
            "No trace left in your claude.ai history.</i>"
            if us.incognito else
            "<i>⚠️ Conversations will remain on claude.ai.\n"
            "They will only be deleted when you stop the bot.</i>"
        ),
        parse_mode="HTML",
    )


# ── Wipe ───────────────────────────────────────────────────────────

@bot.message_handler(commands=["wipe"])
@auth_check
def cmd_wipe(msg: Message):
    us    = get_session(msg.from_user.id)
    count = len(us.tracked_convs)
    if count == 0:
        bot.reply_to(msg, "ℹ️ No conversations to delete.", parse_mode="HTML")
        return
    wipe_all(us)
    bot.reply_to(msg, f"🧹 Deleted <b>{count}</b> conversation(s) from claude.ai.", parse_mode="HTML")


# ── Status ─────────────────────────────────────────────────────────

@bot.message_handler(commands=["status"])
@auth_check
def cmd_status(msg: Message):
    us   = get_session(msg.from_user.id)
    conv = (
        f"<code>{us.conversation_id[:12]}…</code>"
        if us.conversation_id else "None"
    )
    bot.reply_to(
        msg,
        f"📊 <b>Session Status</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🔑 Key      : {'✅ Configured' if us.session_key else '❌ Not set'}\n"
        f"🕵️ Incognito : {'🟢 ON' if us.incognito else '🔴 OFF'}\n"
        f"🤖 Model    : <code>{us.model}</code>\n"
        f"💬 Current  : {conv}\n"
        f"📋 Tracked  : {len(us.tracked_convs)} conv(s)\n"
        f"💾 History  : {len(us.history)} msg(s)\n"
        f"🐳 Docker   : ✅ Running",
        parse_mode="HTML",
    )


# ═══════════════════════════════════════════════════════════════════
#               MAIN MESSAGE HANDLER (CHAT WITH CLAUDE)
# ═══════════════════════════════════════════════════════════════════

@bot.message_handler(content_types=["text", "document", "photo"])
@auth_check
def handle_message(msg: Message):
    uid = msg.from_user.id
    us  = get_session(uid)

    if not us.session_key or not us.organization_id:
        bot.reply_to(
            msg,
            "⚠️ <b>No session key configured!</b>\n\n"
            "Please set your Claude session key first:\n"
            "/setkey <code>&lt;your_key&gt;</code>\n\n"
            "See /help for detailed instructions.",
            parse_mode="HTML",
        )
        return

    if us.busy:
        bot.reply_to(
            msg,
            "⏳ <i>Please wait — still processing your previous message…</i>",
            parse_mode="HTML",
        )
        return

    user_text   = msg.text or msg.caption or ""
    attachments = []

    # ── Document upload ─────────────────────────────────────────
    if msg.document:
        try:
            finfo = bot.get_file(msg.document.file_id)
            fdata = bot.download_file(finfo.file_path)
            fname = msg.document.file_name or "file"
            try:
                content    = fdata.decode("utf-8")
                user_text += f"\n\n[File: {fname}]\n```\n{content}\n```"
                log.info(f"User {uid} uploaded text file: {fname}")
            except UnicodeDecodeError:
                b64        = base64.b64encode(fdata).decode()
                user_text += f"\n\n[Binary file: {fname}, {len(fdata)} bytes]\n{b64[:2000]}…"
                log.info(f"User {uid} uploaded binary file: {fname}")
        except Exception as e:
            bot.reply_to(msg, f"⚠️ Could not process file: {e}")
            return

    # ── Photo upload ────────────────────────────────────────────
    if msg.photo:
        try:
            finfo      = bot.get_file(msg.photo[-1].file_id)
            fdata      = bot.download_file(finfo.file_path)
            b64        = base64.b64encode(fdata).decode()
            user_text += f"\n\n[Image attached — base64 preview: {b64[:3000]}…]"
            log.info(f"User {uid} uploaded image")
        except Exception as e:
            bot.reply_to(msg, f"⚠️ Could not process image: {e}")

    if not user_text.strip():
        return

    us.busy  = True
    thinking = bot.reply_to(msg, "🧠 <i>Claude is thinking…</i>", parse_mode="HTML")
    bot.send_chat_action(msg.chat.id, "typing")

    try:
        result    = send_message(us, user_text, attachments)
        resp_text = result["text"]
        files     = result["files"]

        if not resp_text.strip():
            bot.edit_message_text(
                "⚠️ <i>Claude sent an empty response.</i>",
                chat_id    = thinking.chat.id,
                message_id = thinking.message_id,
                parse_mode = "HTML",
            )
            return

        try:
            bot.delete_message(thinking.chat.id, thinking.message_id)
        except Exception:
            pass

        send_chunked(msg.chat.id, md_to_tg_html(resp_text), reply_to=msg.message_id)

        if files:
            send_files(msg.chat.id, files, reply_to=msg.message_id)

        us.history.append({"role": "user",      "text": user_text[:200]})
        us.history.append({"role": "assistant", "text": resp_text[:200]})
        log.info(f"User {uid} → response: {len(resp_text)} chars, {len(files)} file(s)")

    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        msgs = {
            403: "🔑 Session key expired or invalid. Use /setkey to update.",
            429: "⏳ Rate limited by Claude. Please wait and try again.",
            500: "💥 Claude server error. Try again in a moment.",
        }
        err = msgs.get(code, f"HTTP {code} error occurred")
        try:
            bot.edit_message_text(
                f"❌ <b>Error:</b> {err}",
                chat_id    = thinking.chat.id,
                message_id = thinking.message_id,
                parse_mode = "HTML",
            )
        except Exception:
            bot.send_message(msg.chat.id, f"❌ {err}")
        log.error(f"HTTP {code} error for user {uid}")

    except RuntimeError as e:
        try:
            bot.edit_message_text(
                f"❌ <b>Claude Error:</b>\n{html_lib.escape(str(e))}",
                chat_id    = thinking.chat.id,
                message_id = thinking.message_id,
                parse_mode = "HTML",
            )
        except Exception:
            bot.send_message(msg.chat.id, f"❌ Claude error: {e}")

    except Exception as e:
        log.exception(f"Unhandled error for user {uid}")
        try:
            bot.edit_message_text(
                f"❌ <b>Unexpected error:</b>\n<code>{html_lib.escape(str(e))}</code>",
                chat_id    = thinking.chat.id,
                message_id = thinking.message_id,
                parse_mode = "HTML",
            )
        except Exception:
            pass
    finally:
        us.busy = False


# ═══════════════════════════════════════════════════════════════════
#               GRACEFUL SHUTDOWN
# ═══════════════════════════════════════════════════════════════════

def graceful_shutdown(sig, frame):
    log.info("Shutdown signal received — cleaning up incognito sessions…")
    wiped = 0
    for uid, us in sessions.items():
        if us.tracked_convs:
            count = len(us.tracked_convs)
            wipe_all(us)
            wiped += count
            log.info(f"  Wiped {count} conv(s) for user {uid}")
    log.info(f"✓ Cleanup complete. Wiped {wiped} total conversations. Goodbye.")
    sys.exit(0)


signal.signal(signal.SIGINT,  graceful_shutdown)
signal.signal(signal.SIGTERM, graceful_shutdown)


# ═══════════════════════════════════════════════════════════════════
#                            MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    log.info("Setting up bot commands menu…")
    try:
        bot.set_my_commands([
            BotCommand("start",        "Welcome & help"),
            BotCommand("setkey",       "Set your Claude session key"),
            BotCommand("newchat",      "Start a fresh conversation"),
            BotCommand("model",        "Change Claude model"),
            BotCommand("validate",     "Check if your key still works"),
            BotCommand("massvalidate", "Bulk validate multiple keys"),
            BotCommand("incognito",    "Toggle incognito mode"),
            BotCommand("wipe",         "Delete all tracked conversations"),
            BotCommand("status",       "Show session info"),
            BotCommand("myid",         "Get your Telegram user ID"),
            BotCommand("help",         "Show help"),
        ])
        log.info("✓ Bot commands registered successfully")
    except Exception as e:
        log.warning(f"Could not register bot commands: {e}")

    log.info("🚀 Bot is now polling for messages…")
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    bot.infinity_polling(timeout=60, long_polling_timeout=60)


if __name__ == "__main__":
    main()
