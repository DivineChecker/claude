#!/usr/bin/env python3
"""
🕵️ Claude Incognito Telegram Bot
Docker-ready — all config hardcoded below.
"""

import json
import uuid
import re
import os
import html as html_lib
import time
import logging
import signal
import sys
import urllib.parse
import io
from typing import Optional
from dataclasses import dataclass, field
from collections import defaultdict

import requests
from telebot import TeleBot, apihelper
from telebot.types import Message, BotCommand

# ═══════════════════════════════════════════════════════════════════
#                        !! CONFIGURATION !!
# ═══════════════════════════════════════════════════════════════════

BOT_TOKEN     = "8891866405:AAFOavJJq6Pv_KMl94JXxH26kistSO4NzqY"
ADMIN_IDS     = []
DEFAULT_MODEL = "claude-sonnet-4-20250514"
AUTO_WIPE     = False
FILE_SIZE_MIN = 200
MAX_CHUNK     = 4000
LOG_LEVEL     = "INFO"
DEFAULT_PROXIES: list[str] = []
RETRY_MAX     = 3
RETRY_DELAY   = 30

# ═══════════════════════════════════════════════════════════════════
#                    INTERNAL CONSTANTS
# ═══════════════════════════════════════════════════════════════════

BASE_URL = "https://claude.ai/api"

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

if not BOT_TOKEN or "YOUR_TELEGRAM_BOT_TOKEN_HERE" in BOT_TOKEN:
    log.critical("BOT_TOKEN is not configured.")
    sys.exit(1)

log.info("━━━ Claude Incognito Telegram Bot ━━━")
log.info(f"Model      : {DEFAULT_MODEL}")
log.info(f"AutoWipe   : {AUTO_WIPE}")
log.info(f"RetryMax   : {RETRY_MAX}x  RetryDelay: {RETRY_DELAY}s")

bot = TeleBot(BOT_TOKEN, parse_mode="HTML")

# ═══════════════════════════════════════════════════════════════════
#                    PROXY POOL MANAGER
# ═══════════════════════════════════════════════════════════════════

class ProxyPool:
    MAX_FAILS = 3

    def __init__(self, proxies: list[str] = None):
        self._proxies = list(proxies or [])
        self._index = 0
        self._fails = defaultdict(int)

    @property
    def count(self) -> int:
        return len(self._proxies)

    @property
    def active(self) -> str:
        if not self._proxies:
            return ""
        self._index = self._index % len(self._proxies)
        return self._proxies[self._index]

    @property
    def active_index(self) -> int:
        if not self._proxies:
            return 0
        return (self._index % len(self._proxies)) + 1

    def all_proxies(self) -> list[tuple[int, str, int]]:
        return [
            (i + 1, url, self._fails.get(url, 0))
            for i, url in enumerate(self._proxies)
        ]

    def add(self, proxy_url: str) -> bool:
        if proxy_url in self._proxies:
            return False
        self._proxies.append(proxy_url)
        log.info(f"Proxy added: {_mask_proxy(proxy_url)}")
        return True

    def remove(self, index_1based: int) -> Optional[str]:
        idx = index_1based - 1
        if idx < 0 or idx >= len(self._proxies):
            return None
        removed = self._proxies.pop(idx)
        self._fails.pop(removed, None)
        if self._proxies:
            self._index = self._index % len(self._proxies)
        else:
            self._index = 0
        log.info(f"Proxy removed: {_mask_proxy(removed)}")
        return removed

    def clear(self):
        self._proxies.clear()
        self._fails.clear()
        self._index = 0
        log.info("Proxy pool cleared")

    def rotate(self) -> str:
        if len(self._proxies) <= 1:
            return self.active
        self._index = (self._index + 1) % len(self._proxies)
        log.info(f"Rotated to proxy #{self.active_index}: {_mask_proxy(self.active)}")
        return self.active

    def mark_failed(self, proxy_url: str) -> bool:
        if proxy_url not in self._proxies:
            return False
        self._fails[proxy_url] += 1
        fails = self._fails[proxy_url]
        log.warning(f"Proxy fail #{fails}/{self.MAX_FAILS}: {_mask_proxy(proxy_url)}")
        if fails >= self.MAX_FAILS:
            idx = self._proxies.index(proxy_url)
            self._proxies.pop(idx)
            self._fails.pop(proxy_url, None)
            if self._proxies:
                self._index = self._index % len(self._proxies)
            else:
                self._index = 0
            log.warning(f"Proxy permanently removed: {_mask_proxy(proxy_url)}")
            return True
        if len(self._proxies) > 1:
            self.rotate()
        return False

    def mark_success(self, proxy_url: str):
        if proxy_url in self._fails:
            self._fails[proxy_url] = 0

    def as_requests_dict(self) -> dict:
        url = self.active
        if not url:
            return {}
        return {"http": url, "https": url}

# ═══════════════════════════════════════════════════════════════════
#                        DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════════

@dataclass
class UserSession:
    session_key     : str = ""
    organization_id : str = ""
    conversation_id : str = ""
    model           : str = field(default_factory=lambda: DEFAULT_MODEL)
    tracked_convs   : list = field(default_factory=list)
    history         : list = field(default_factory=list)
    http            : requests.Session = field(default_factory=requests.Session)
    incognito       : bool = True
    busy            : bool = False
    proxy_pool      : ProxyPool = field(default_factory=lambda: ProxyPool(DEFAULT_PROXIES))

    def __post_init__(self):
        self._apply_headers()
        self._sync_proxy()

    def _apply_headers(self):
        self.http.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": "https://claude.ai",
            "Referer": "https://claude.ai/chats",
        })

    def _sync_proxy(self):
        self.http.proxies = self.proxy_pool.as_requests_dict()

    def rotate_proxy(self) -> str:
        url = self.proxy_pool.rotate()
        self._sync_proxy()
        return url

    def set_key(self, key: str):
        self.session_key = key
        self.http.cookies.clear()
        self.http.cookies.set(
            name="sessionKey",
            value=key,
            domain=".claude.ai",
            path="/",
            secure=True,
        )
        log.debug(f"Session key set: {key[:20]}...")

sessions: dict[int, UserSession] = {}

def get_session(uid: int) -> UserSession:
    if uid not in sessions:
        sessions[uid] = UserSession()
    return sessions[uid]

# ═══════════════════════════════════════════════════════════════════
#                      PROXY UTILITIES
# ═══════════════════════════════════════════════════════════════════

def _mask_proxy(proxy_url: str) -> str:
    if not proxy_url:
        return ""
    try:
        p = urllib.parse.urlparse(proxy_url)
        if p.password:
            return proxy_url.replace(p.password, "****")
    except Exception:
        pass
    return proxy_url[:40] + ("..." if len(proxy_url) > 40 else "")

def _parse_proxy_url(proxy_url: str) -> tuple[bool, str]:
    try:
        p = urllib.parse.urlparse(proxy_url)
        if p.scheme not in ("http", "https", "socks5", "socks4"):
            return False, f"Unsupported scheme '{p.scheme}'"
        if not p.hostname:
            return False, "Missing hostname"
        if not p.port:
            return False, "Missing port"
        return True, ""
    except Exception as e:
        return False, str(e)

def _test_proxy(proxy_url: str) -> tuple[bool, str, str]:
    try:
        s = requests.Session()
        if proxy_url:
            s.proxies = {"http": proxy_url, "https": proxy_url}
        r = s.get("https://api.ipify.org?format=json", timeout=10)
        r.raise_for_status()
        ip = r.json().get("ip", "unknown")
        return True, ip, ""
    except requests.exceptions.ProxyError as e:
        return False, "", f"Proxy unreachable"
    except requests.exceptions.Timeout:
        return False, "", "Timeout"
    except Exception as e:
        return False, "", str(e)

# ═══════════════════════════════════════════════════════════════════
#                     CLAUDE API FUNCTIONS
# ═══════════════════════════════════════════════════════════════════

def validate_key(session_key: str, proxy_url: str = "") -> tuple[bool, str, str]:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/json",
    })
    s.cookies.set("sessionKey", session_key, domain=".claude.ai")
    if proxy_url:
        s.proxies = {"http": proxy_url, "https": proxy_url}

    try:
        resp = s.get(f"{BASE_URL}/organizations", timeout=15)

        if resp.status_code == 403:
            return False, "", "Expired/Invalid key or IP blocked"
        if resp.status_code == 401:
            return False, "", "Unauthorized"

        resp.raise_for_status()
        orgs = resp.json()

        if not orgs:
            return False, "", "No organizations found"

        org_name = orgs[0].get("name", "Unknown")
        org_id = orgs[0]["uuid"]

        conv_resp = s.post(
            f"{BASE_URL}/organizations/{org_id}/chat_conversations",
            json={"uuid": str(uuid.uuid4()), "name": "", "model": DEFAULT_MODEL},
            timeout=15,
        )

        if conv_resp.status_code == 403:
            return False, "", "Key valid but blocked for chat (403)"
        if conv_resp.status_code not in (200, 201):
            return False, "", f"Cannot create conversations"

        test_id = conv_resp.json().get("uuid")
        if test_id:
            try:
                s.delete(f"{BASE_URL}/organizations/{org_id}/chat_conversations/{test_id}", timeout=5)
            except Exception:
                pass

        log.info(f"Key validated ✓ Org: {org_name}")
        return True, org_id, org_name

    except Exception as e:
        return False, "", str(e)

def create_conversation(us: UserSession) -> str:
    url = f"{BASE_URL}/organizations/{us.organization_id}/chat_conversations"
    resp = us.http.post(
        url,
        json={"uuid": str(uuid.uuid4()), "name": "", "model": us.model},
        timeout=15,
    )
    resp.raise_for_status()
    cid = resp.json()["uuid"]
    us.conversation_id = cid
    us.tracked_convs.append(cid)
    us.history = []
    log.info(f"Created conversation: {cid[:12]}...")
    return cid

def delete_conversation(us: UserSession, conv_id: str) -> bool:
    try:
        r = us.http.delete(
            f"{BASE_URL}/organizations/{us.organization_id}/chat_conversations/{conv_id}",
            timeout=10,
        )
        if conv_id in us.tracked_convs:
            us.tracked_convs.remove(conv_id)
        return r.ok or r.status_code == 204
    except Exception as e:
        log.warning(f"Failed to delete: {e}")
        return False

def wipe_all(us: UserSession):
    count = len(us.tracked_convs)
    for cid in list(us.tracked_convs):
        delete_conversation(us, cid)
    us.conversation_id = ""
    us.history = []
    log.info(f"Wiped {count} conversation(s)")

def send_message(us: UserSession, text: str, status_msg=None, chat_id: int = None) -> dict:
    if not us.conversation_id:
        create_conversation(us)

    url = (
        f"{BASE_URL}/organizations/{us.organization_id}"
        f"/chat_conversations/{us.conversation_id}/completion"
    )
    payload = {
        "prompt": text,
        "timezone": "UTC",
        "attachments": [],
        "files": [],
    }

    last_error = None
    retry_delay = RETRY_DELAY

    for attempt in range(1, RETRY_MAX + 1):
        current_proxy = us.proxy_pool.active

        try:
            us._sync_proxy()

            log.debug(f"Attempt {attempt}/{RETRY_MAX} | Proxy: {_mask_proxy(current_proxy) or 'direct'}")

            resp = us.http.post(url, json=payload, stream=True, timeout=120)

            if resp.status_code == 429:
                log.warning(f"429 rate limit")
                if attempt < RETRY_MAX:
                    wait = retry_delay * attempt
                    if status_msg and chat_id:
                        try:
                            bot.edit_message_text(
                                f"⏳ Rate limited — retrying in {wait}s…",
                                chat_id=chat_id,
                                message_id=status_msg.message_id,
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass
                    time.sleep(wait)
                    if us.proxy_pool.count > 1:
                        us.rotate_proxy()
                    continue
                last_error = RuntimeError("Rate limited after retries")
                break

            if resp.status_code == 403:
                log.error(f"403 Forbidden")
                if current_proxy:
                    us.proxy_pool.mark_failed(current_proxy)
                    if us.proxy_pool.count > 0:
                        us._sync_proxy()
                        if status_msg and chat_id and attempt < RETRY_MAX:
                            try:
                                bot.edit_message_text(
                                    f"⚠️ Proxy blocked — switching…",
                                    chat_id=chat_id,
                                    message_id=status_msg.message_id,
                                    parse_mode="HTML",
                                )
                            except Exception:
                                pass
                        if attempt < RETRY_MAX:
                            time.sleep(2)
                            continue
                last_error = RuntimeError("403 Forbidden - IP blocked")
                break

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
                        raise RuntimeError(event.get("error", {}).get("message", "Unknown error"))
                except json.JSONDecodeError:
                    continue

            us.proxy_pool.mark_success(current_proxy)
            log.info(f"Response received: {len(full_text)} chars")

            if us.incognito and AUTO_WIPE and us.conversation_id:
                cid = us.conversation_id
                us.conversation_id = ""
                delete_conversation(us, cid)

            return {"text": full_text, "files": extract_code_files(full_text)}

        except requests.exceptions.ProxyError as e:
            log.error(f"Proxy error: {e}")
            if current_proxy:
                us.proxy_pool.mark_failed(current_proxy)
                if us.proxy_pool.count > 0:
                    us._sync_proxy()
                    if attempt < RETRY_MAX:
                        time.sleep(2)
                        last_error = e
                        continue
            raise RuntimeError("All proxies failed")

        except requests.exceptions.Timeout:
            log.warning(f"Timeout")
            if attempt < RETRY_MAX:
                time.sleep(5)
                last_error = RuntimeError("Timeout")
                continue
            raise RuntimeError("Request timed out")

        except RuntimeError:
            raise

        except Exception as e:
            log.error(f"Error: {e}")
            last_error = e
            break

    if last_error:
        raise last_error
    raise RuntimeError("Failed after retries")

# ═══════════════════════════════════════════════════════════════════
#               CODE EXTRACTION & FILE GENERATION
# ═══════════════════════════════════════════════════════════════════

LANG_EXTENSIONS = {
    "python": ".py", "py": ".py", "javascript": ".js", "js": ".js",
    "typescript": ".ts", "ts": ".ts", "java": ".java", "c": ".c",
    "cpp": ".cpp", "c++": ".cpp", "csharp": ".cs", "cs": ".cs",
    "go": ".go", "rust": ".rs", "ruby": ".rb", "php": ".php",
    "html": ".html", "css": ".css", "sql": ".sql", "bash": ".sh",
    "json": ".json", "yaml": ".yaml", "dockerfile": "Dockerfile",
}

def extract_code_files(text: str) -> list:
    pattern = r"```(\w+)?\n(.*?)```"
    matches = re.findall(pattern, text, re.DOTALL)
    files = []
    counter = defaultdict(int)

    for lang, code in matches:
        lang = (lang or "txt").lower().strip()
        if len(code.strip()) < FILE_SIZE_MIN:
            continue
        ext = LANG_EXTENSIONS.get(lang, f".{lang}")
        counter[lang] += 1
        idx = f"_{counter[lang]}" if counter[lang] > 1 else ""
        filename = f"code{idx}{ext}"
        files.append({"name": filename, "content": code.strip(), "language": lang})

    return files

# ═══════════════════════════════════════════════════════════════════
#            MARKDOWN → TELEGRAM HTML CONVERTER
# ═══════════════════════════════════════════════════════════════════

def md_to_tg_html(text: str) -> str:
    chunks = []
    pos = 0
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
    text = html_lib.escape(text)
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    return text

# ═══════════════════════════════════════════════════════════════════
#               CHUNK MESSAGING SYSTEM
# ═══════════════════════════════════════════════════════════════════

def smart_split(text: str, max_len: int = MAX_CHUNK) -> list[str]:
    if len(text) <= max_len:
        return [text]

    chunks = []
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
    for open_tag, close_tag in [("<pre>", "</pre>"), ("<code>", "</code>"), ("<b>", "</b>"), ("<i>", "</i>")]:
        opens = chunk.count(open_tag)
        closes = chunk.count(close_tag)
        if opens > closes:
            chunk += close_tag * (opens - closes)
        elif closes > opens:
            chunk = (open_tag * (closes - opens)) + chunk
    return chunk

def send_chunked(chat_id: int, text: str, reply_to: int = None):
    chunks = smart_split(text)
    total = len(chunks)
    for i, chunk in enumerate(chunks):
        if total > 1:
            chunk = f"<i>Part {i+1}/{total}</i>\n\n" + chunk
        try:
            bot.send_message(
                chat_id, chunk,
                parse_mode="HTML",
                reply_to_message_id=reply_to if i == 0 else None,
                disable_web_page_preview=True,
            )
        except apihelper.ApiTelegramException as e:
            if "can't parse entities" in str(e).lower():
                plain = re.sub(r"<[^>]+>", "", chunk)
                bot.send_message(chat_id, plain, reply_to_message_id=reply_to if i == 0 else None)
        if i < total - 1:
            time.sleep(0.3)

def send_files(chat_id: int, files: list, reply_to: int = None):
    for f in files:
        try:
            bio = io.BytesIO(f["content"].encode("utf-8"))
            bio.name = f["name"]
            bot.send_document(
                chat_id, bio,
                caption=f"📎 <code>{f['name']}</code>",
                parse_mode="HTML",
                reply_to_message_id=reply_to,
            )
        except Exception as e:
            log.warning(f"Failed to send file: {e}")

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
            bot.reply_to(msg, f"🚫 Access Denied\nID: <code>{msg.from_user.id}</code>", parse_mode="HTML")
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

<b>Setup</b>
/setkey <code>&lt;key&gt;</code> - Set session key
/validate - Check key status
/massvalidate - Bulk validate keys

<b>Proxy (Fix IP Blocks)</b>
/addproxy <code>&lt;url&gt;</code> - Add proxy
/proxies - List all proxies
/delproxy <code>&lt;#&gt;</code> - Remove proxy
/clearproxies - Remove all
/proxystatus - Active proxy info
/nextproxy - Rotate proxy

<b>Chat</b>
Send text or documents to chat
Claude will analyze and respond

<b>Controls</b>
/newchat - Fresh conversation
/model - Change model
/incognito - Toggle privacy mode
/wipe - Delete all chats
/status - Session info
/myid - Your Telegram ID

<b>Get Key</b>
1. Go to claude.ai and login
2. F12 → Application → Cookies
3. Copy sessionKey value
4. Send: /setkey &lt;paste&gt;
""".strip(), parse_mode="HTML")

@bot.message_handler(commands=["myid"])
def cmd_myid(msg: Message):
    bot.reply_to(msg, f"ID: <code>{msg.from_user.id}</code>", parse_mode="HTML")

@bot.message_handler(commands=["setkey"])
@auth_check
def cmd_setkey(msg: Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(msg, "Usage: /setkey <code>&lt;key&gt;</code>", parse_mode="HTML")
        return

    key = parts[1].strip()
    uid = msg.from_user.id
    us = get_session(uid)

    try:
        bot.delete_message(msg.chat.id, msg.message_id)
    except Exception:
        pass

    thinking = bot.send_message(msg.chat.id, "🔄 Validating…", parse_mode="HTML")

    valid, org_id, info = validate_key(key, proxy_url=us.proxy_pool.active)

    try:
        bot.delete_message(msg.chat.id, thinking.message_id)
    except Exception:
        pass

    if not valid:
        bot.send_message(msg.chat.id, f"❌ Invalid: {info}", parse_mode="HTML")
        return

    us.set_key(key)
    us.organization_id = org_id

    bot.send_message(msg.chat.id, f"✅ Configured!\nOrg: <code>{info}</code>", parse_mode="HTML")

@bot.message_handler(commands=["addproxy"])
@auth_check
def cmd_addproxy(msg: Message):
    parts = msg.text.split(maxsplit=1)
    uid = msg.from_user.id

    if len(parts) < 2:
        bot.reply_to(msg, "Usage: /addproxy <code>http://host:port</code>", parse_mode="HTML")
        return

    proxy_url = parts[1].strip()

    try:
        bot.delete_message(msg.chat.id, msg.message_id)
    except Exception:
        pass

    valid, err = _parse_proxy_url(proxy_url)
    if not valid:
        bot.send_message(msg.chat.id, f"❌ Invalid: {err}", parse_mode="HTML")
        return

    thinking = bot.send_message(msg.chat.id, "🔄 Testing…", parse_mode="HTML")
    ok, ip, test_err = _test_proxy(proxy_url)

    try:
        bot.delete_message(msg.chat.id, thinking.message_id)
    except Exception:
        pass

    if not ok:
        bot.send_message(msg.chat.id, f"❌ Test failed: {test_err}", parse_mode="HTML")
        return

    us = get_session(uid)
    added = us.proxy_pool.add(proxy_url)
    us._sync_proxy()

    if not added:
        bot.send_message(msg.chat.id, f"⚠️ Already in pool\nIP: {ip}", parse_mode="HTML")
        return

    bot.send_message(msg.chat.id, f"✅ Proxy added!\nIP: {ip}", parse_mode="HTML")

@bot.message_handler(commands=["proxies"])
@auth_check
def cmd_proxies(msg: Message):
    us = get_session(msg.from_user.id)

    if us.proxy_pool.count == 0:
        bot.reply_to(msg, "No proxies. Use /addproxy", parse_mode="HTML")
        return

    lines = []
    for idx, url, fails in us.proxy_pool.all_proxies():
        marker = " ◀ ACTIVE" if idx == us.proxy_pool.active_index else ""
        fail = f" ⚠️ {fails}" if fails > 0 else ""
        lines.append(f"<b>#{idx}</b> {_mask_proxy(url)}{fail}{marker}")

    bot.reply_to(msg, f"Pool ({us.proxy_pool.count}):\n" + "\n".join(lines), parse_mode="HTML")

@bot.message_handler(commands=["delproxy"])
@auth_check
def cmd_delproxy(msg: Message):
    parts = msg.text.split(maxsplit=1)
    uid = msg.from_user.id
    us = get_session(uid)

    if not parts or len(parts) < 2 or not parts[1].strip().isdigit():
        bot.reply_to(msg, f"Usage: /delproxy <code>&lt;#&gt;</code>\nPool has {us.proxy_pool.count}", parse_mode="HTML")
        return

    idx = int(parts[1].strip())
    removed = us.proxy_pool.remove(idx)
    us._sync_proxy()

    if removed:
        bot.reply_to(msg, f"✅ Removed proxy #{idx}", parse_mode="HTML")
    else:
        bot.reply_to(msg, f"❌ No proxy #{idx}", parse_mode="HTML")

@bot.message_handler(commands=["clearproxies"])
@auth_check
def cmd_clearproxies(msg: Message):
    uid = msg.from_user.id
    us = get_session(uid)

    if us.proxy_pool.count == 0:
        bot.reply_to(msg, "Already empty")
        return

    count = us.proxy_pool.count
    us.proxy_pool.clear()
    us._sync_proxy()

    bot.reply_to(msg, f"✅ Cleared {count} proxy(s)")

@bot.message_handler(commands=["nextproxy"])
@auth_check
def cmd_nextproxy(msg: Message):
    uid = msg.from_user.id
    us = get_session(uid)

    if us.proxy_pool.count == 0:
        bot.reply_to(msg, "No proxies")
        return

    new_proxy = us.rotate_proxy()
    ok, ip, _ = _test_proxy(new_proxy)

    bot.reply_to(msg, f"✅ Proxy #{us.proxy_pool.active_index}\nIP: {ip if ok else 'Test failed'}", parse_mode="HTML")

@bot.message_handler(commands=["proxystatus"])
@auth_check
def cmd_proxystatus(msg: Message):
    us = get_session(msg.from_user.id)

    active = us.proxy_pool.active
    ok, ip, err = _test_proxy(active)

    proxy_line = f"Proxy: {_mask_proxy(active)}" if active else "No proxy (direct)"
    ip_line = f"IP: {ip}" if ok else f"IP check: Failed"

    bot.reply_to(msg, f"{proxy_line}\n{ip_line}", parse_mode="HTML")

@bot.message_handler(commands=["validate"])
@auth_check
def cmd_validate(msg: Message):
    us = get_session(msg.from_user.id)
    if not us.session_key:
        bot.reply_to(msg, "No key set. Use /setkey")
        return

    bot.send_chat_action(msg.chat.id, "typing")
    valid, _, info = validate_key(us.session_key, proxy_url=us.proxy_pool.active)

    if valid:
        bot.reply_to(msg, f"✅ Valid\nOrg: {info}", parse_mode="HTML")
    else:
        bot.reply_to(msg, f"❌ Invalid: {info}", parse_mode="HTML")

@bot.message_handler(commands=["massvalidate"])
@auth_check
def cmd_massvalidate(msg: Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(msg, "One key per line:\n/massvalidate\nkey1\nkey2", parse_mode="HTML")
        return

    keys = [k.strip() for k in parts[1].strip().split("\n") if k.strip()]
    total = len(keys)
    us = get_session(msg.from_user.id)

    try:
        bot.delete_message(msg.chat.id, msg.message_id)
    except Exception:
        pass

    status_msg = bot.send_message(msg.chat.id, f"🔄 Checking {total}…", parse_mode="HTML")

    valid_count = 0
    invalid_count = 0

    for i, key in enumerate(keys):
        valid, _, _ = validate_key(key, proxy_url=us.proxy_pool.active)
        if valid:
            valid_count += 1
        else:
            invalid_count += 1

    try:
        bot.delete_message(status_msg.chat.id, status_msg.message_id)
    except Exception:
        pass

    bot.send_message(msg.chat.id, f"✅ Valid: {valid_count}\n❌ Invalid: {invalid_count}", parse_mode="HTML")

@bot.message_handler(commands=["newchat"])
@auth_check
def cmd_newchat(msg: Message):
    us = get_session(msg.from_user.id)
    if us.conversation_id:
        delete_conversation(us, us.conversation_id)
    us.conversation_id = ""
    us.history = []
    bot.reply_to(msg, "✅ New chat started")

@bot.message_handler(commands=["model"])
@auth_check
def cmd_model(msg: Message):
    parts = msg.text.split(maxsplit=1)
    us = get_session(msg.from_user.id)
    if len(parts) < 2:
        bot.reply_to(msg, f"Current: <code>{us.model}</code>\n\nSet: /model <code>&lt;name&gt;</code>", parse_mode="HTML")
        return
    us.model = parts[1].strip()
    bot.reply_to(msg, f"✅ Model: <code>{us.model}</code>", parse_mode="HTML")

@bot.message_handler(commands=["incognito"])
@auth_check
def cmd_incognito(msg: Message):
    us = get_session(msg.from_user.id)
    us.incognito = not us.incognito
    state = "ON" if us.incognito else "OFF"
    bot.reply_to(msg, f"🕵️ Incognito: {state}")

@bot.message_handler(commands=["wipe"])
@auth_check
def cmd_wipe(msg: Message):
    us = get_session(msg.from_user.id)
    count = len(us.tracked_convs)
    if count == 0:
        bot.reply_to(msg, "Nothing to delete")
        return
    wipe_all(us)
    bot.reply_to(msg, f"✅ Deleted {count} chat(s)")

@bot.message_handler(commands=["status"])
@auth_check
def cmd_status(msg: Message):
    us = get_session(msg.from_user.id)
    proxy_info = f"Proxies: {us.proxy_pool.count}" if us.proxy_pool.count > 0 else "No proxies"
    bot.reply_to(msg,
        f"<b>Status</b>\n"
        f"Key: {'✅' if us.session_key else '❌'}\n"
        f"Mode: {'Incognito' if us.incognito else 'Normal'}\n"
        f"Model: {us.model}\n"
        f"Tracked: {len(us.tracked_convs)}\n"
        f"{proxy_info}",
        parse_mode="HTML")

# ═══════════════════════════════════════════════════════════════════
#                    MAIN MESSAGE HANDLER
# ═══════════════════════════════════════════════════════════════════

@bot.message_handler(content_types=["text", "document"])
@auth_check
def handle_message(msg: Message):
    uid = msg.from_user.id
    us = get_session(uid)

    if not us.session_key or not us.organization_id:
        bot.reply_to(msg, "⚠️ No key. Use /setkey first", parse_mode="HTML")
        return

    if us.busy:
        bot.reply_to(msg, "⏳ Processing…")
        return

    user_text = (msg.text or msg.caption or "").strip()

    # Handle documents
    if msg.document:
        try:
            finfo = bot.get_file(msg.document.file_id)
            fdata = bot.download_file(finfo.file_path)
            fname = msg.document.file_name or "file"

            try:
                doc_content = fdata.decode("utf-8")
                user_text += f"\n\n[File: {fname}]\n```\n{doc_content}\n```"
            except UnicodeDecodeError:
                user_text += f"\n\n[Binary: {fname}]"

        except Exception as e:
            user_text += f"\n\n[Error reading file]"

    if not user_text.strip():
        return

    us.busy = True
    thinking = bot.send_message(msg.chat.id, "🧠 Thinking…", parse_mode="HTML", reply_to_message_id=msg.message_id)
    bot.send_chat_action(msg.chat.id, "typing")

    try:
        result = send_message(us, user_text, status_msg=thinking, chat_id=msg.chat.id)
        resp_text = result["text"]
        files = result["files"]

        if not resp_text.strip():
            bot.edit_message_text("⚠️ Empty response", chat_id=thinking.chat.id, message_id=thinking.message_id)
            return

        try:
            bot.delete_message(thinking.chat.id, thinking.message_id)
        except Exception:
            pass

        send_chunked(msg.chat.id, md_to_tg_html(resp_text), reply_to=msg.message_id)

        if files:
            send_files(msg.chat.id, files, reply_to=msg.message_id)

        log.info(f"User {uid} → {len(resp_text)} chars")

    except RuntimeError as e:
        try:
            bot.edit_message_text(f"❌ {str(e)}", chat_id=thinking.chat.id, message_id=thinking.message_id)
        except Exception:
            bot.send_message(msg.chat.id, f"❌ {e}")

    except Exception as e:
        log.exception(f"Error: {e}")
        try:
            bot.edit_message_text(f"❌ Error: {str(e)[:100]}", chat_id=thinking.chat.id, message_id=thinking.message_id)
        except Exception:
            pass

    finally:
        us.busy = False

# ═══════════════════════════════════════════════════════════════════
#               GRACEFUL SHUTDOWN
# ═══════════════════════════════════════════════════════════════════

def graceful_shutdown(sig, frame):
    log.info("Shutting down…")
    wiped = 0
    for uid, us in sessions.items():
        if us.tracked_convs:
            count = len(us.tracked_convs)
            wipe_all(us)
            wiped += count
    log.info(f"Done. Wiped {wiped} chat(s)")
    sys.exit(0)

signal.signal(signal.SIGINT, graceful_shutdown)
signal.signal(signal.SIGTERM, graceful_shutdown)

# ═══════════════════════════════════════════════════════════════════
#                            MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    try:
        bot.set_my_commands([
            BotCommand("start", "Help & setup"),
            BotCommand("setkey", "Set session key"),
            BotCommand("validate", "Check key"),
            BotCommand("massvalidate", "Bulk validate"),
            BotCommand("newchat", "New conversation"),
            BotCommand("model", "Change model"),
            BotCommand("incognito", "Toggle privacy"),
            BotCommand("wipe", "Delete chats"),
            BotCommand("status", "Session status"),
            BotCommand("myid", "Your ID"),
            BotCommand("addproxy", "Add proxy"),
            BotCommand("proxies", "List proxies"),
            BotCommand("delproxy", "Remove proxy"),
            BotCommand("clearproxies", "Clear all"),
            BotCommand("proxystatus", "Proxy info"),
            BotCommand("nextproxy", "Rotate proxy"),
        ])
        log.info("✓ Commands registered")
    except Exception as e:
        log.warning(f"Commands: {e}")

    log.info("🚀 Starting…")
    bot.infinity_polling(timeout=60, long_polling_timeout=60)

if __name__ == "__main__":
    main()
