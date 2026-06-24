#!/usr/bin/env python3
"""
🕵️ Claude Incognito Telegram Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Docker-ready — all config hardcoded below.
⚠️ UNOFFICIAL — Uses claude.ai web API. May break at any time.

PROXY COMMANDS (manage from bot — no code changes needed):
  /addproxy http://user:pass@host:port   — add proxy to pool
  /proxies                               — list all proxies + active one
  /delproxy 2                            — remove proxy #2
  /clearproxies                          — remove all proxies
  /proxystatus                           — show active proxy + exit IP
  /nextproxy                             — manually rotate to next proxy
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
import urllib.parse
from typing import Optional
from dataclasses import dataclass, field
from collections import defaultdict

import requests
from telebot import TeleBot, apihelper
from telebot.types import Message, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

try:
    from pypdf import PdfReader
    PYPDF_AVAILABLE = True
except ImportError:
    PYPDF_AVAILABLE = False

# ═══════════════════════════════════════════════════════════════════
#                        !! CONFIGURATION !!
#              Edit these values before building/running
# ═══════════════════════════════════════════════════════════════════

BOT_TOKEN     = "8891866405:AAHb_67CJmrBbco3ag425qBfC8EprCjx9cU"

# Allowed Telegram user IDs — leave empty list [] to allow everyone
ADMIN_IDS     = []                          # e.g. [123456789, 987654321]

DEFAULT_MODEL = "claude-sonnet-4-20250514"  # Claude model to use

# Delete conversations after every reply (max stealth)
AUTO_WIPE     = False

# Minimum characters in a code block to send it as a file
FILE_SIZE_MIN = 200

# Maximum characters per Telegram message (hard limit is 4096)
MAX_CHUNK     = 4000

# Logging level: DEBUG | INFO | WARNING | ERROR
LOG_LEVEL     = "INFO"

# ─── Default proxy pool for ALL users (optional) ───────────────────
# Add proxy URLs here to pre-load them for every user.
# Leave as empty list [] to start with no proxies.
# Format: "http://user:pass@host:port" or "socks5://user:pass@host:port"
DEFAULT_PROXIES: list[str] = []

# ─── Auto-retry on 429 rate limit ──────────────────────────────────
# How many times to retry before giving up
RETRY_MAX     = 3
# Seconds to wait between retries (doubles each attempt: 30 → 60 → 120)
RETRY_DELAY   = 30

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
log.info(f"Model      : {DEFAULT_MODEL}")
log.info(f"AutoWipe   : {AUTO_WIPE}")
log.info(f"Admins     : {ADMIN_IDS or 'Everyone'}")
log.info(f"LogLevel   : {LOG_LEVEL}")
log.info(f"RetryMax   : {RETRY_MAX}x  RetryDelay: {RETRY_DELAY}s")
log.info(f"DefProxies : {len(DEFAULT_PROXIES)}")
log.info(f"PDF support: {'✅ pypdf available' if PYPDF_AVAILABLE else '❌ pypdf NOT installed — PDFs will be skipped'}")

bot = TeleBot(BOT_TOKEN, parse_mode="HTML")

# ═══════════════════════════════════════════════════════════════════
#                    PROXY POOL MANAGER
# ═══════════════════════════════════════════════════════════════════

class ProxyPool:
    """
    Manages a rotating pool of proxies.
    - Automatically rotates to next on failure
    - Tracks fail counts per proxy
    - Removes permanently dead proxies after MAX_FAILS failures
    """
    MAX_FAILS = 3

    def __init__(self, proxies: list[str] = None):
        self._proxies   : list[str] = list(proxies or [])
        self._index     : int       = 0
        self._fails     : dict      = defaultdict(int)

    # ── Read ──────────────────────────────────────────────────────

    @property
    def count(self) -> int:
        return len(self._proxies)

    @property
    def active(self) -> str:
        """Return the current active proxy URL, or '' if pool is empty."""
        if not self._proxies:
            return ""
        self._index = self._index % len(self._proxies)
        return self._proxies[self._index]

    @property
    def active_index(self) -> int:
        """1-based index of active proxy (for display)."""
        if not self._proxies:
            return 0
        return (self._index % len(self._proxies)) + 1

    def all_proxies(self) -> list[tuple[int, str, int]]:
        """Return [(1-based-index, url, fail_count), ...]"""
        return [
            (i + 1, url, self._fails.get(url, 0))
            for i, url in enumerate(self._proxies)
        ]

    # ── Write ─────────────────────────────────────────────────────

    def add(self, proxy_url: str) -> bool:
        """Add a proxy. Returns False if already in pool."""
        if proxy_url in self._proxies:
            return False
        self._proxies.append(proxy_url)
        log.info(f"Proxy added to pool: {_mask_proxy(proxy_url)}")
        return True

    def remove(self, index_1based: int) -> Optional[str]:
        """Remove proxy by 1-based index. Returns removed URL or None."""
        idx = index_1based - 1
        if idx < 0 or idx >= len(self._proxies):
            return None
        removed = self._proxies.pop(idx)
        self._fails.pop(removed, None)
        # Adjust current index
        if self._proxies:
            self._index = self._index % len(self._proxies)
        else:
            self._index = 0
        log.info(f"Proxy removed: {_mask_proxy(removed)}")
        return removed

    def clear(self):
        """Remove all proxies."""
        self._proxies.clear()
        self._fails.clear()
        self._index = 0
        log.info("Proxy pool cleared")

    # ── Rotation ──────────────────────────────────────────────────

    def rotate(self) -> str:
        """Move to next proxy. Returns new active proxy URL."""
        if len(self._proxies) <= 1:
            return self.active
        self._index = (self._index + 1) % len(self._proxies)
        log.info(f"Rotated to proxy #{self.active_index}: {_mask_proxy(self.active)}")
        return self.active

    def mark_failed(self, proxy_url: str) -> bool:
        """
        Mark a proxy as failed. Auto-removes after MAX_FAILS.
        Returns True if proxy was removed from pool.
        """
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
            log.warning(f"Proxy permanently removed (too many failures): {_mask_proxy(proxy_url)}")
            return True
        # Rotate away from the failed proxy
        if len(self._proxies) > 1:
            self.rotate()
        return False

    def mark_success(self, proxy_url: str):
        """Reset fail count on success."""
        if proxy_url in self._fails:
            self._fails[proxy_url] = 0

    def as_requests_dict(self) -> dict:
        """Return proxies dict for requests, or {} if no proxies."""
        url = self.active
        if not url:
            return {}
        return {"http": url, "https": url}


# ═══════════════════════════════════════════════════════════════════
#                        DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════════

@dataclass
class UserSession:
    session_key     : str        = ""
    organization_id : str        = ""
    conversation_id : str        = ""
    model           : str        = field(default_factory=lambda: DEFAULT_MODEL)
    tracked_convs   : list       = field(default_factory=list)
    history         : list       = field(default_factory=list)
    pending_history : list       = field(default_factory=list)  # old history offered for resume
    http            : requests.Session = field(default_factory=requests.Session)
    incognito       : bool       = True
    busy            : bool       = False
    web_search      : bool       = False  # removed feature, kept for compat
    proxy_pool      : ProxyPool  = field(default_factory=lambda: ProxyPool(DEFAULT_PROXIES))

    def __post_init__(self):
        self._apply_headers()
        self._sync_proxy()

    def _apply_headers(self):
        self.http.headers.update({
            "User-Agent"            : (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept"                : "*/*",
            "Accept-Language"       : "en-US,en;q=0.9",
            "Accept-Encoding"       : "gzip, deflate, br",
            "Content-Type"          : "application/json",
            "Origin"                : "https://claude.ai",
            "Referer"               : "https://claude.ai/chats",
            "Sec-Ch-Ua"             : '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "Sec-Ch-Ua-Mobile"      : "?0",
            "Sec-Ch-Ua-Platform"    : '"Windows"',
            "Sec-Fetch-Dest"        : "empty",
            "Sec-Fetch-Mode"        : "cors",
            "Sec-Fetch-Site"        : "same-origin",
        })

    def _sync_proxy(self):
        """Sync requests.Session proxies from the pool's active proxy."""
        self.http.proxies = self.proxy_pool.as_requests_dict()

    def rotate_proxy(self) -> str:
        """Rotate to next proxy and sync session."""
        url = self.proxy_pool.rotate()
        self._sync_proxy()
        return url

    def set_key(self, key: str):
        self.session_key    = key
        self.conversation_id = ""  # always reset — old conv belongs to old key/org
        self.http.cookies.clear()
        self.http.cookies.set(
            name   = "sessionKey",
            value  = key,
            domain = ".claude.ai",
            path   = "/",
            secure = True,
        )
        log.debug(f"Session key set: {key[:20]}... (conversation_id cleared)")


# Global store: { telegram_user_id: UserSession }
sessions: dict[int, UserSession] = {}


def get_session(uid: int) -> UserSession:
    if uid not in sessions:
        sessions[uid] = UserSession()
    return sessions[uid]


# ═══════════════════════════════════════════════════════════════════
#                      PROXY UTILITIES
# ═══════════════════════════════════════════════════════════════════

def _mask_proxy(proxy_url: str) -> str:
    """Mask password in proxy URL for safe logging/display."""
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
    """
    Validate a proxy URL.
    Returns (is_valid, error_message).
    """
    try:
        p = urllib.parse.urlparse(proxy_url)
        if p.scheme not in ("http", "https", "socks5", "socks4"):
            return False, (
                f"Unsupported scheme '{p.scheme}'.\n"
                f"Use: http://, https://, socks5://, or socks4://"
            )
        if not p.hostname:
            return False, "Missing hostname in proxy URL"
        if not p.port:
            return False, "Missing port in proxy URL"
        return True, ""
    except Exception as e:
        return False, str(e)


def _test_proxy(proxy_url: str) -> tuple[bool, str, str]:
    """
    Test proxy by fetching exit IP from api.ipify.org.
    Returns (success, ip_address, error_message).
    """
    try:
        s = requests.Session()
        if proxy_url:
            s.proxies = {"http": proxy_url, "https": proxy_url}
        r = s.get("https://api.ipify.org?format=json", timeout=10)
        r.raise_for_status()
        ip = r.json().get("ip", "unknown")
        return True, ip, ""
    except requests.exceptions.ProxyError as e:
        return False, "", f"Proxy unreachable: {e}"
    except requests.exceptions.Timeout:
        return False, "", "Timeout — proxy too slow or offline"
    except Exception as e:
        return False, "", str(e)


# ═══════════════════════════════════════════════════════════════════
#                     CLAUDE API FUNCTIONS
# ═══════════════════════════════════════════════════════════════════

def validate_key(session_key: str, proxy_url: str = "") -> tuple[bool, str, str]:
    """
    Validate a Claude session key.
    Returns (is_valid, org_id, org_name_or_error).
    """
    s = requests.Session()
    s.headers.update({
        "User-Agent"         : (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept"             : "*/*",
        "Accept-Language"    : "en-US,en;q=0.9",
        "Content-Type"       : "application/json",
        "Origin"             : "https://claude.ai",
        "Referer"            : "https://claude.ai/chats",
        "Sec-Ch-Ua"          : '"Chromium";v="124", "Google Chrome";v="124"',
        "Sec-Ch-Ua-Mobile"   : "?0",
        "Sec-Ch-Ua-Platform" : '"Windows"',
        "Sec-Fetch-Dest"     : "empty",
        "Sec-Fetch-Mode"     : "cors",
        "Sec-Fetch-Site"     : "same-origin",
    })
    s.cookies.set("sessionKey", session_key, domain=".claude.ai", path="/", secure=True)
    if proxy_url:
        s.proxies = {"http": proxy_url, "https": proxy_url}

    try:
        resp = s.get(f"{BASE_URL}/organizations", timeout=15)

        if resp.status_code == 403:
            return False, "", "Session expired or invalid — log in to claude.ai again for a fresh key (or IP blocked, try /addproxy)"
        if resp.status_code == 401:
            return False, "", "Unauthorized — key is invalid or has been revoked"
        if resp.status_code == 429:
            return False, "", "Account rate-limited — usage limit reached, wait for reset or use a different account"

        resp.raise_for_status()
        orgs = resp.json()

        if not orgs:
            return False, "", "No organizations found on this account"

        org_name = orgs[0].get("name", "Unknown Org")
        org_id   = orgs[0]["uuid"]

        log.info(f"Key validated ✓ Org: {org_name}")
        return True, org_id, org_name

    except requests.exceptions.ProxyError as e:
        return False, "", f"Proxy error: {e}"
    except requests.exceptions.Timeout:
        return False, "", "Request timed out"
    except requests.exceptions.ConnectionError as e:
        return False, "", f"Connection error: {e}"
    except Exception as e:
        return False, "", str(e)


def create_conversation(us: UserSession) -> str:
    """Create a new blank conversation."""
    url = f"{BASE_URL}/organizations/{us.organization_id}/chat_conversations"

    payload = {"uuid": str(uuid.uuid4()), "name": ""}
    if us.model:
        payload["model"] = us.model

    resp = us.http.post(url, json=payload, timeout=15)

    if resp.status_code == 400:
        log.error(f"create_conversation 400 body: {resp.text[:500]}")
        # Retry once without the model field — claude.ai may reject unknown model strings
        if "model" in payload:
            log.info("Retrying conversation creation without 'model' field...")
            payload.pop("model")
            resp = us.http.post(url, json=payload, timeout=15)
            if resp.status_code == 400:
                log.error(f"create_conversation 400 body (retry): {resp.text[:500]}")

    resp.raise_for_status()
    cid = resp.json()["uuid"]
    us.conversation_id = cid
    us.tracked_convs.append(cid)
    us.history = []
    log.info(f"Created conversation: {cid[:12]}...")
    return cid


def delete_conversation(us: UserSession, conv_id: str) -> bool:
    """Silently delete a conversation."""
    try:
        r = us.http.delete(
            f"{BASE_URL}/organizations/{us.organization_id}/chat_conversations/{conv_id}",
            timeout=10,
        )
        if conv_id in us.tracked_convs:
            us.tracked_convs.remove(conv_id)
        return r.ok or r.status_code == 204
    except Exception as e:
        log.warning(f"Failed to delete conversation {conv_id[:12]}: {e}")
        return False


def wipe_all(us: UserSession):
    """Delete every tracked conversation. Clears local state immediately."""
    convs_to_delete = list(us.tracked_convs)
    count           = len(convs_to_delete)

    # Clear local state FIRST — so stale IDs are never reused even if deletes fail
    us.conversation_id = ""
    us.tracked_convs   = []
    us.history         = []

    # Then attempt to delete from claude.ai (best-effort, failures are silent)
    for cid in convs_to_delete:
        try:
            us.http.delete(
                f"{BASE_URL}/organizations/{us.organization_id}/chat_conversations/{cid}",
                timeout=5,
            )
            log.debug(f"Deleted conversation {cid[:12]}")
        except Exception as e:
            log.debug(f"Could not delete conversation {cid[:12]}: {e}")

    log.info(f"Wiped {count} conversation(s)")


def send_message(us: UserSession, text: str, attachments: list = None, status_msg=None, chat_id: int = None) -> dict:
    """
    Send a message to Claude with:
    - Automatic proxy rotation on proxy failure
    - Auto-retry with backoff on 429 rate limit
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

    last_error    = None
    retry_delay   = RETRY_DELAY

    for attempt in range(1, RETRY_MAX + 1):
        current_proxy = us.proxy_pool.active

        try:
            # Sync proxy before each attempt
            us._sync_proxy()

            log.debug(
                f"Attempt {attempt}/{RETRY_MAX} | "
                f"Proxy: {_mask_proxy(current_proxy) or 'direct'} | "
                f"Conv: {us.conversation_id[:12]}"
            )

            resp = us.http.post(url, json=payload, stream=True, timeout=120)

            # ── 429 Rate Limited ──────────────────────────────────
            if resp.status_code == 429:
                log.warning(f"429 rate limit on attempt {attempt}/{RETRY_MAX}")

                # Try to extract reset time from response body
                reset_info = ""
                try:
                    body = json.loads(resp.text)
                    err  = body.get("error", {})
                    msg_text = err.get("message", "") or body.get("message", "")
                    # Claude returns resets_at as unix timestamp sometimes
                    resets_at = err.get("resets_at") or body.get("resets_at")
                    if resets_at:
                        try:
                            reset_dt = time.strftime("%H:%M UTC", time.gmtime(float(resets_at)))
                            reset_info = f" (resets at {reset_dt})"
                        except Exception:
                            pass
                    if msg_text:
                        log.warning(f"429 detail: {msg_text}")
                except Exception:
                    pass

                if attempt < RETRY_MAX:
                    wait = retry_delay * attempt
                    log.info(f"Waiting {wait}s before retry…")
                    if status_msg and chat_id:
                        try:
                            bot.edit_message_text(
                                f"⏳ <i>Rate limited by Claude — retrying in {wait}s… (attempt {attempt}/{RETRY_MAX})</i>",
                                chat_id    = chat_id,
                                message_id = status_msg.message_id,
                                parse_mode = "HTML",
                            )
                        except Exception:
                            pass
                    time.sleep(wait)
                    # Rotate proxy on rate limit too — different proxy = different exit IP = separate rate limit bucket
                    if us.proxy_pool.count > 1:
                        new_proxy = us.rotate_proxy()
                        log.info(f"Rotated proxy after 429: {_mask_proxy(new_proxy)}")
                    continue
                last_error = requests.exceptions.HTTPError(response=resp, request=resp.request)
                last_error.reset_info = reset_info
                break

            # ── 403 Forbidden ─────────────────────────────────────
            if resp.status_code == 403:
                log.error(f"403 on attempt {attempt} with proxy {_mask_proxy(current_proxy)}")
                if current_proxy:
                    us.proxy_pool.mark_failed(current_proxy)
                    if us.proxy_pool.count > 0:
                        new_proxy = us.proxy_pool.active
                        us._sync_proxy()
                        log.info(f"Switched to proxy: {_mask_proxy(new_proxy)}")
                        if status_msg and chat_id and attempt < RETRY_MAX:
                            try:
                                bot.edit_message_text(
                                    f"⚠️ <i>Proxy blocked — switching to backup proxy… (attempt {attempt}/{RETRY_MAX})</i>",
                                    chat_id    = chat_id,
                                    message_id = status_msg.message_id,
                                    parse_mode = "HTML",
                                )
                            except Exception:
                                pass
                        if attempt < RETRY_MAX:
                            time.sleep(2)
                            continue
                last_error = requests.exceptions.HTTPError(response=resp, request=resp.request)
                break

            if resp.status_code == 400:
                try:
                    log.error(f"completion 400 body: {resp.text[:500]}")
                except Exception:
                    pass

            resp.raise_for_status()

            # ── Stream response ───────────────────────────────────
            full_text  = ""
            raw_events = []
            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                raw = line[6:]
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if len(raw_events) < 10:
                    raw_events.append(event)

                etype = event.get("type", "")

                # Legacy format: {"type": "completion", "completion": "..."}
                if etype == "completion":
                    full_text += event.get("completion", "")

                # Current format: content_block_delta with text_delta
                elif etype == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        full_text += delta.get("text", "")

                # Alternate: message_delta / message events sometimes carry text directly
                elif etype in ("message_delta", "message_start", "message_stop"):
                    pass  # metadata only, no text

                elif etype == "error":
                    err = event.get("error", {})
                    raise RuntimeError(err.get("message", "Unknown error"))

            if not full_text:
                log.warning(f"Empty response — first events seen: {json.dumps(raw_events)[:1000]}")

            # ── Success ───────────────────────────────────────────
            us.proxy_pool.mark_success(current_proxy)
            log.info(f"Response received: {len(full_text)} chars (attempt {attempt})")

            if us.incognito and AUTO_WIPE and us.conversation_id:
                cid = us.conversation_id
                us.conversation_id = ""
                delete_conversation(us, cid)

            return {"text": full_text, "files": extract_code_files(full_text)}

        except requests.exceptions.ProxyError as e:
            log.error(f"Proxy error on attempt {attempt}: {e}")
            if current_proxy:
                removed = us.proxy_pool.mark_failed(current_proxy)
                if us.proxy_pool.count > 0:
                    us._sync_proxy()
                    if status_msg and chat_id and attempt < RETRY_MAX:
                        try:
                            bot.edit_message_text(
                                f"⚠️ <i>Proxy error — switching to backup… (attempt {attempt}/{RETRY_MAX})</i>",
                                chat_id    = chat_id,
                                message_id = status_msg.message_id,
                                parse_mode = "HTML",
                            )
                        except Exception:
                            pass
                    if attempt < RETRY_MAX:
                        time.sleep(2)
                        last_error = e
                        continue
            raise RuntimeError(
                f"All proxies failed or no proxy set.\n"
                f"Add more proxies with /addproxy\n"
                f"Last error: {e}"
            )

        except requests.exceptions.Timeout as e:
            log.warning(f"Timeout on attempt {attempt}")
            last_error = e
            if attempt < RETRY_MAX:
                time.sleep(5)
                continue
            raise RuntimeError("Request timed out after all retries")

        except RuntimeError:
            raise

        except Exception as e:
            log.error(f"Unexpected error on attempt {attempt}: {e}")
            last_error = e
            break

    # All retries exhausted
    if last_error:
        raise last_error
    raise RuntimeError("Failed after all retries")


def extract_pdf_text(pdf_data: bytes, max_chars: int = 50000) -> str:
    """
    Extract text content from a PDF file's bytes.
    Returns extracted text (truncated to max_chars) or an error message.
    """
    if not PYPDF_AVAILABLE:
        return "[PDF received but pypdf is not installed on the server — cannot extract text]"

    try:
        reader = PdfReader(io.BytesIO(pdf_data))
        pages  = []
        for i, page in enumerate(reader.pages):
            try:
                text = page.extract_text() or ""
            except Exception as e:
                text = f"[Error extracting page {i+1}: {e}]"
            if text.strip():
                pages.append(f"--- Page {i+1} ---\n{text.strip()}")

        full_text = "\n\n".join(pages)

        if not full_text.strip():
            return "[PDF appears to contain no extractable text — may be scanned/image-based]"

        if len(full_text) > max_chars:
            full_text = full_text[:max_chars] + f"\n\n[...truncated, PDF has {len(reader.pages)} pages total...]"

        return full_text

    except Exception as e:
        log.warning(f"PDF extraction error: {e}")
        return f"[Could not read PDF: {e}]"


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
    """Extract large code blocks as sendable files."""
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
    ext = LANG_EXTENSIONS.get(lang, f".{lang}")
    if lang in ("python", "py"):
        m = re.search(r"^(?:class|def)\s+(\w+)", code, re.MULTILINE)
        if m:
            return f"{m.group(1).lower()}{ext}"
    if lang in ("javascript", "js", "typescript", "ts", "jsx", "tsx"):
        m = re.search(r"(?:export\s+default\s+(?:class|function)\s+|function\s+)(\w+)", code)
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
    for open_tag, close_tag in [("<pre>","</pre>"),("<code>","</code>"),("<b>","</b>"),("<i>","</i>"),("<s>","</s>")]:
        opens  = chunk.count(open_tag)
        closes = chunk.count(close_tag)
        if opens > closes:
            chunk += close_tag * (opens - closes)
        elif closes > opens:
            chunk = (open_tag * (closes - opens)) + chunk
    return chunk


def send_chunked(chat_id: int, text: str, reply_to: int = None):
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
                bot.send_message(chat_id, plain, reply_to_message_id=reply_to if i == 0 else None)
            else:
                raise
        if i < total - 1:
            time.sleep(0.3)


def send_files(chat_id: int, files: list, reply_to: int = None):
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
                f"🚫 <b>Access Denied</b>\nYour ID: <code>{msg.from_user.id}</code>",
                parse_mode="HTML",
            )
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

<b>━━━ Setup ━━━</b>
/setkey <code>&lt;session_key&gt;</code> — Set your Claude session key
/validate — Check if current key works
/massvalidate — Bulk validate multiple keys

<b>━━━ Proxy Pool (fix VPS 403 / rotate IPs) ━━━</b>
/addproxy <code>&lt;url&gt;</code> — Add proxy to rotation pool
/proxies — List all proxies + which is active
/delproxy <code>&lt;number&gt;</code> — Remove proxy by number
/clearproxies — Remove all proxies
/proxystatus — Show active proxy + exit IP
/nextproxy — Manually rotate to next proxy

<b>━━━ Chat ━━━</b>
Just send any message! Files and images supported.

<b>━━━ Controls ━━━</b>
/newchat — Start fresh conversation
/incognito — Toggle incognito mode
/wipe — Delete all tracked chats
/status — Session info
/myid — Show your Telegram user ID

<b>━━━ Proxy Format ━━━</b>
<code>http://user:pass@host:port</code>
<code>socks5://user:pass@host:port</code>
<code>http://host:port</code>  (no auth)

<b>━━━ Get Session Key ━━━</b>
1. Go to <a href="https://claude.ai">claude.ai</a> and login
2. Press F12 → Application → Cookies
3. Find and copy the <code>sessionKey</code> value
4. Send: /setkey &lt;paste_here&gt;
""".strip(), parse_mode="HTML", disable_web_page_preview=True)


@bot.message_handler(commands=["myid"])
def cmd_myid(msg: Message):
    bot.reply_to(msg, f"🪪 Your Telegram ID: <code>{msg.from_user.id}</code>", parse_mode="HTML")


# ── Set Key ────────────────────────────────────────────────────────

@bot.message_handler(commands=["setkey"])
@auth_check
def cmd_setkey(msg: Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(msg,
            "⚠️ <b>Usage:</b> /setkey <code>&lt;session_key&gt;</code>\n\n"
            "Get from claude.ai → F12 → Application → Cookies → <code>sessionKey</code>",
            parse_mode="HTML")
        return

    key = parts[1].strip()
    uid = msg.from_user.id
    us  = get_session(uid)

    try:
        bot.delete_message(msg.chat.id, msg.message_id)
    except Exception:
        pass

    thinking = bot.send_message(msg.chat.id, "🔄 <i>Validating key…</i>", parse_mode="HTML")

    # Use active proxy from pool for validation
    valid, org_id, info = validate_key(key, proxy_url=us.proxy_pool.active)

    try:
        bot.delete_message(msg.chat.id, thinking.message_id)
    except Exception:
        pass

    if not valid:
        proxy_hint = (
            "\n\n💡 No proxies in pool. Add one with /addproxy to bypass VPS IP blocks."
            if us.proxy_pool.count == 0 else
            f"\n\n🌐 Tried with proxy: <code>{html_lib.escape(_mask_proxy(us.proxy_pool.active))}</code>"
        )
        bot.send_message(msg.chat.id,
            f"❌ <b>Invalid Session Key</b>\n\nError: <code>{html_lib.escape(info)}</code>{proxy_hint}",
            parse_mode="HTML")
        return

    org_switched = bool(us.organization_id and us.organization_id != org_id)
    had_history  = len(us.history) > 0

    if org_switched:
        if had_history:
            # Save old history for possible resume, wipe old org's conversations
            us.pending_history = list(us.history)
            wipe_all(us)
        else:
            wipe_all(us)

    us.set_key(key)
    us.organization_id = org_id

    proxy_line = (
        f"\n🌐 Proxies  : <b>{us.proxy_pool.count}</b> in pool (active: #{us.proxy_pool.active_index})"
        if us.proxy_pool.count > 0 else
        "\n🌐 Proxies  : <i>None — add with /addproxy</i>"
    )

    bot.send_message(msg.chat.id,
        f"✅ <b>Session Key Configured!</b>\n\n"
        f"🏢 Org   : <code>{html_lib.escape(info)}</code>\n"
        f"🕵️ Mode  : {'Incognito 🟢' if us.incognito else 'Normal 🔴'}\n"
        f"🤖 Model : <code>{us.model}</code>"
        f"{proxy_line}\n\n"
        f"<i>🔐 Key message deleted for security.</i>",
        parse_mode="HTML")

    # Offer to resume previous conversation if we have saved history
    if org_switched and had_history:
        kb = InlineKeyboardMarkup()
        kb.row(
            InlineKeyboardButton("✅ Yes, continue", callback_data="resume_yes"),
            InlineKeyboardButton("🆕 No, start fresh", callback_data="resume_no"),
        )
        bot.send_message(
            msg.chat.id,
            f"💬 <b>New session key detected for a different account.</b>\n\n"
            f"You have <b>{len(us.pending_history)}</b> message(s) from your previous "
            f"project/conversation.\n\n"
            f"Do you want to <b>continue your previous project</b> with this new key?\n\n"
            f"<i>⚠️ Continuing will resend the full previous conversation as context "
            f"on your next message — this uses more tokens.</i>",
            parse_mode="HTML",
            reply_markup=kb,
        )


# ── Resume Prompt Callback ──────────────────────────────────────────

@bot.callback_query_handler(func=lambda call: call.data in ("resume_yes", "resume_no"))
def cb_resume(call: CallbackQuery):
    uid = call.from_user.id
    us  = get_session(uid)

    if call.data == "resume_yes":
        if not us.pending_history:
            bot.answer_callback_query(call.id, "No previous history found.")
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            except Exception:
                pass
            return

        us.history = list(us.pending_history)
        us.pending_history = []
        bot.answer_callback_query(call.id, "Resuming previous project…")

        try:
            bot.edit_message_text(
                "✅ <b>Resuming previous project!</b>\n\n"
                "Your next message will include the full previous conversation as context.",
                chat_id=call.message.chat.id, message_id=call.message.message_id,
                parse_mode="HTML",
            )
        except Exception:
            pass
        log.info(f"User {uid} chose to resume previous project ({len(us.history)} history msg(s))")

    else:  # resume_no
        us.pending_history = []
        bot.answer_callback_query(call.id, "Starting fresh.")
        try:
            bot.edit_message_text(
                "🆕 <b>Starting fresh!</b>\n\nPrevious conversation history discarded.",
                chat_id=call.message.chat.id, message_id=call.message.message_id,
                parse_mode="HTML",
            )
        except Exception:
            pass
        log.info(f"User {uid} chose to start fresh, discarded pending history")




@bot.message_handler(commands=["addproxy"])
@auth_check
def cmd_addproxy(msg: Message):
    """Add a proxy to the rotation pool."""
    parts = msg.text.split(maxsplit=1)
    uid   = msg.from_user.id

    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(msg,
            "🌐 <b>Add Proxy to Pool</b>\n\n"
            "<b>Usage:</b>\n"
            "<code>/addproxy http://user:pass@host:port</code>\n"
            "<code>/addproxy socks5://user:pass@host:port</code>\n\n"
            "You can add multiple proxies — they rotate automatically on failure.\n"
            "Use /proxies to see the full pool.",
            parse_mode="HTML")
        return

    proxy_url = parts[1].strip()

    # Delete message — may contain credentials
    try:
        bot.delete_message(msg.chat.id, msg.message_id)
    except Exception:
        pass

    valid, err = _parse_proxy_url(proxy_url)
    if not valid:
        bot.send_message(msg.chat.id,
            f"❌ <b>Invalid proxy URL</b>\n\nError: <code>{html_lib.escape(err)}</code>\n\n"
            f"Format: <code>http://user:pass@host:port</code>",
            parse_mode="HTML")
        return

    thinking = bot.send_message(msg.chat.id, "🔄 <i>Testing proxy…</i>", parse_mode="HTML")
    ok, ip, test_err = _test_proxy(proxy_url)

    try:
        bot.delete_message(msg.chat.id, thinking.message_id)
    except Exception:
        pass

    if not ok:
        bot.send_message(msg.chat.id,
            f"❌ <b>Proxy test failed</b>\n\nError: <code>{html_lib.escape(test_err)}</code>",
            parse_mode="HTML")
        return

    us = get_session(uid)
    added = us.proxy_pool.add(proxy_url)
    us._sync_proxy()

    if not added:
        bot.send_message(msg.chat.id,
            f"⚠️ Proxy already in pool.\n📍 Exit IP: <code>{html_lib.escape(ip)}</code>",
            parse_mode="HTML")
        return

    log.info(f"User {uid} added proxy #{us.proxy_pool.count}: {_mask_proxy(proxy_url)} → IP: {ip}")

    bot.send_message(msg.chat.id,
        f"✅ <b>Proxy #{us.proxy_pool.count} added!</b>\n\n"
        f"🌐 Proxy  : <code>{html_lib.escape(_mask_proxy(proxy_url))}</code>\n"
        f"📍 Exit IP: <code>{html_lib.escape(ip)}</code>\n"
        f"🔄 Pool   : <b>{us.proxy_pool.count}</b> proxy(s) total\n\n"
        f"<i>🔐 Proxy message deleted for security.</i>\n\n"
        f"Use /proxies to see the full pool.",
        parse_mode="HTML")


@bot.message_handler(commands=["proxies"])
@auth_check
def cmd_proxies(msg: Message):
    """List all proxies in the pool."""
    us = get_session(msg.from_user.id)

    if us.proxy_pool.count == 0:
        bot.reply_to(msg,
            "🌐 <b>Proxy Pool: Empty</b>\n\n"
            "Add proxies with:\n<code>/addproxy http://user:pass@host:port</code>",
            parse_mode="HTML")
        return

    lines = []
    for idx, url, fails in us.proxy_pool.all_proxies():
        active_marker = " ◀ <b>ACTIVE</b>" if idx == us.proxy_pool.active_index else ""
        fail_indicator = f" ⚠️ {fails} fail(s)" if fails > 0 else ""
        lines.append(
            f"<b>#{idx}</b> <code>{html_lib.escape(_mask_proxy(url))}</code>"
            f"{fail_indicator}{active_marker}"
        )

    bot.reply_to(msg,
        f"🌐 <b>Proxy Pool</b> ({us.proxy_pool.count} total)\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        + "\n".join(lines) + "\n\n"
        f"<i>Proxies auto-rotate on failure.\n"
        f"Remove one: /delproxy &lt;number&gt;</i>",
        parse_mode="HTML")


@bot.message_handler(commands=["delproxy"])
@auth_check
def cmd_delproxy(msg: Message):
    """Remove a proxy by its number from the pool."""
    parts = msg.text.split(maxsplit=1)
    uid   = msg.from_user.id
    us    = get_session(uid)

    if us.proxy_pool.count == 0:
        bot.reply_to(msg, "ℹ️ No proxies in pool.", parse_mode="HTML")
        return

    if len(parts) < 2 or not parts[1].strip().isdigit():
        bot.reply_to(msg,
            f"⚠️ <b>Usage:</b> /delproxy <code>&lt;number&gt;</code>\n\n"
            f"Use /proxies to see proxy numbers.\n"
            f"Current pool has <b>{us.proxy_pool.count}</b> proxy(s).",
            parse_mode="HTML")
        return

    idx     = int(parts[1].strip())
    removed = us.proxy_pool.remove(idx)
    us._sync_proxy()

    if removed is None:
        bot.reply_to(msg,
            f"❌ No proxy #{idx}. Pool has <b>{us.proxy_pool.count}</b> proxy(s).\nUse /proxies to see numbers.",
            parse_mode="HTML")
        return

    log.info(f"User {uid} removed proxy #{idx}: {_mask_proxy(removed)}")

    bot.reply_to(msg,
        f"🗑 <b>Proxy #{idx} removed.</b>\n\n"
        f"Removed: <code>{html_lib.escape(_mask_proxy(removed))}</code>\n"
        f"Remaining: <b>{us.proxy_pool.count}</b> proxy(s) in pool",
        parse_mode="HTML")


@bot.message_handler(commands=["clearproxies"])
@auth_check
def cmd_clearproxies(msg: Message):
    """Remove all proxies from the pool."""
    uid = msg.from_user.id
    us  = get_session(uid)

    if us.proxy_pool.count == 0:
        bot.reply_to(msg, "ℹ️ Proxy pool is already empty.", parse_mode="HTML")
        return

    count = us.proxy_pool.count
    us.proxy_pool.clear()
    us._sync_proxy()
    log.info(f"User {uid} cleared all {count} proxies")

    bot.reply_to(msg,
        f"🗑 <b>All {count} proxy(s) removed.</b>\n\n"
        f"Requests will now go directly from this server.\n"
        f"Add new proxies with /addproxy",
        parse_mode="HTML")


@bot.message_handler(commands=["nextproxy"])
@auth_check
def cmd_nextproxy(msg: Message):
    """Manually rotate to the next proxy."""
    uid = msg.from_user.id
    us  = get_session(uid)

    if us.proxy_pool.count == 0:
        bot.reply_to(msg, "ℹ️ No proxies in pool. Add with /addproxy", parse_mode="HTML")
        return

    if us.proxy_pool.count == 1:
        bot.reply_to(msg,
            f"ℹ️ Only 1 proxy in pool — nothing to rotate to.\n"
            f"Active: <code>{html_lib.escape(_mask_proxy(us.proxy_pool.active))}</code>",
            parse_mode="HTML")
        return

    new_proxy = us.rotate_proxy()
    log.info(f"User {uid} manually rotated to proxy #{us.proxy_pool.active_index}")

    bot.send_chat_action(msg.chat.id, "typing")
    ok, ip, err = _test_proxy(new_proxy)

    bot.reply_to(msg,
        f"🔄 <b>Rotated to proxy #{us.proxy_pool.active_index}</b>\n\n"
        f"🌐 Proxy  : <code>{html_lib.escape(_mask_proxy(new_proxy))}</code>\n"
        + (f"📍 Exit IP: <code>{html_lib.escape(ip)}</code>" if ok else f"⚠️ Test failed: <code>{html_lib.escape(err)}</code>"),
        parse_mode="HTML")


@bot.message_handler(commands=["proxystatus"])
@auth_check
def cmd_proxystatus(msg: Message):
    """Show active proxy info and exit IP."""
    uid = msg.from_user.id
    us  = get_session(uid)

    bot.send_chat_action(msg.chat.id, "typing")

    active = us.proxy_pool.active
    ok, ip, err = _test_proxy(active)

    if us.proxy_pool.count == 0:
        pool_line = "🔄 Pool   : <i>Empty — add with /addproxy</i>"
    else:
        pool_line = f"🔄 Pool   : <b>{us.proxy_pool.count}</b> proxy(s), active #{us.proxy_pool.active_index}"

    proxy_line = (
        f"🌐 Active  : <code>{html_lib.escape(_mask_proxy(active))}</code>"
        if active else
        "🌐 Active  : <i>None (direct connection)</i>"
    )
    ip_line = (
        f"📍 Exit IP : <code>{html_lib.escape(ip)}</code>"
        if ok else
        f"📍 Exit IP : ❌ <code>{html_lib.escape(err)}</code>"
    )

    bot.reply_to(msg,
        f"📊 <b>Proxy Status</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{proxy_line}\n"
        f"{ip_line}\n"
        f"{pool_line}",
        parse_mode="HTML")


# ── Validate ───────────────────────────────────────────────────────

@bot.message_handler(commands=["validate"])
@auth_check
def cmd_validate(msg: Message):
    us = get_session(msg.from_user.id)
    if not us.session_key:
        bot.reply_to(msg, "⚠️ No session key. Use /setkey first.", parse_mode="HTML")
        return

    bot.send_chat_action(msg.chat.id, "typing")
    valid, _, info = validate_key(us.session_key, proxy_url=us.proxy_pool.active)

    pool_line = (
        f"\n🌐 Proxy #{us.proxy_pool.active_index}/{us.proxy_pool.count}: "
        f"<code>{html_lib.escape(_mask_proxy(us.proxy_pool.active))}</code>"
        if us.proxy_pool.count > 0 else ""
    )

    if valid:
        bot.reply_to(msg,
            f"✅ <b>Session Key Valid!</b>\n\n"
            f"🏢 Org: <code>{html_lib.escape(info)}</code>{pool_line}",
            parse_mode="HTML")
    else:
        bot.reply_to(msg,
            f"❌ <b>Session Key Invalid</b>\n\n"
            f"Error: <code>{html_lib.escape(info)}</code>\n\n"
            f"Use /setkey to reconfigure."
            + ("\n\n💡 Add proxies with /addproxy to bypass IP blocks." if us.proxy_pool.count == 0 else ""),
            parse_mode="HTML")


# ── Mass Validate ──────────────────────────────────────────────────

@bot.message_handler(commands=["massvalidate"])
@auth_check
def cmd_massvalidate(msg: Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(msg,
            "📋 <b>Mass Key Validator</b>\n\n"
            "One key per line:\n\n"
            "<code>/massvalidate\nsk-ant-sid01-key1...\nsk-ant-sid01-key2...</code>",
            parse_mode="HTML")
        return

    keys  = [k.strip() for k in parts[1].strip().split("\n") if k.strip()]
    total = len(keys)
    us    = get_session(msg.from_user.id)

    try:
        bot.delete_message(msg.chat.id, msg.message_id)
    except Exception:
        pass

    status_msg = bot.send_message(msg.chat.id, f"🔄 Validating <b>{total}</b> key(s)…", parse_mode="HTML")

    results_valid   = []
    results_invalid = []

    for i, key in enumerate(keys):
        valid, _, info = validate_key(key, proxy_url=us.proxy_pool.active)
        if valid:
            results_valid.append(f"  ✅ <code>{html_lib.escape(key)}</code>\n     → {html_lib.escape(info)}")
        else:
            results_invalid.append(f"  ❌ <code>{html_lib.escape(key)}</code>\n     → {html_lib.escape(info)}")
        if (i + 1) % 3 == 0 or i == total - 1:
            try:
                bot.edit_message_text(
                    f"🔄 Validating… <b>{i+1}/{total}</b>",
                    chat_id=status_msg.chat.id, message_id=status_msg.message_id, parse_mode="HTML")
            except Exception:
                pass

    report = (
        f"📊 <b>Mass Validation Report</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"Total: {total}  |  ✅ {len(results_valid)}  |  ❌ {len(results_invalid)}\n\n"
    )
    if results_valid:
        report += "<b>✅ Valid:</b>\n" + "\n\n".join(results_valid) + "\n\n"
    if results_invalid:
        report += "<b>❌ Invalid:</b>\n" + "\n\n".join(results_invalid)

    try:
        bot.delete_message(status_msg.chat.id, status_msg.message_id)
    except Exception:
        pass

    send_chunked(msg.chat.id, report.strip())


# ── New Chat ───────────────────────────────────────────────────────

@bot.message_handler(commands=["newchat"])
@auth_check
def cmd_newchat(msg: Message):
    us = get_session(msg.from_user.id)
    if not us.session_key or not us.organization_id:
        bot.reply_to(msg, "⚠️ No session key. Use /setkey first.")
        return
    if us.conversation_id:
        delete_conversation(us, us.conversation_id)
        us.conversation_id = ""
    us.history = []
    bot.reply_to(msg, "🆕 <b>New conversation started!</b>", parse_mode="HTML")


# ── Incognito ──────────────────────────────────────────────────────

@bot.message_handler(commands=["incognito"])
@auth_check
def cmd_incognito(msg: Message):
    us           = get_session(msg.from_user.id)
    us.incognito = not us.incognito
    state        = "ON 🟢" if us.incognito else "OFF 🔴"
    bot.reply_to(msg,
        f"🕵️ Incognito: <b>{state}</b>\n\n"
        + ("✅ Conversations deleted after each reply." if us.incognito
           else "⚠️ Conversations kept on claude.ai until /wipe."),
        parse_mode="HTML")


# ── Wipe ───────────────────────────────────────────────────────────

@bot.message_handler(commands=["wipe"])
@auth_check
def cmd_wipe(msg: Message):
    us    = get_session(msg.from_user.id)
    count = len(us.tracked_convs)
    if count == 0:
        bot.reply_to(msg, "ℹ️ Nothing to delete.")
        return
    wipe_all(us)
    bot.reply_to(msg, f"🧹 Deleted <b>{count}</b> conversation(s).", parse_mode="HTML")


# ── Status ─────────────────────────────────────────────────────────

@bot.message_handler(commands=["status"])
@auth_check
def cmd_status(msg: Message):
    us   = get_session(msg.from_user.id)
    conv = f"<code>{us.conversation_id[:12]}…</code>" if us.conversation_id else "None"

    if us.proxy_pool.count == 0:
        proxy_line = "\n🌐 Proxies  : <i>None — /addproxy to add</i>"
    else:
        proxy_line = (
            f"\n🌐 Proxies  : <b>{us.proxy_pool.count}</b> in pool, "
            f"active <b>#{us.proxy_pool.active_index}</b> → "
            f"<code>{html_lib.escape(_mask_proxy(us.proxy_pool.active))}</code>"
        )

    bot.reply_to(msg,
        f"📊 <b>Session Status</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🔑 Key      : {'✅ Set' if us.session_key else '❌ Not set'}\n"
        f"🕵️ Incognito : {'🟢 ON' if us.incognito else '🔴 OFF'}\n"
        f"🤖 Model    : <code>{us.model}</code>\n"
        f"💬 Current  : {conv}\n"
        f"📋 Tracked  : {len(us.tracked_convs)} conv(s)\n"
        f"💾 History  : {len(us.history)} msg(s)\n"
        f"🔁 Retry    : up to {RETRY_MAX}x ({RETRY_DELAY}s delay)"
        f"{proxy_line}",
        parse_mode="HTML")



def _process_combined(uid: int, chat_id: int, first_msg: Message,
                      all_msgs: list[Message], user_text: str):
    """
    Process text + document messages as ONE Claude request.
    Images are not supported and will be politely rejected.
    """
    us = get_session(uid)

    if not us.session_key or not us.organization_id:
        bot.send_message(chat_id,
            "⚠️ <b>No session key!</b>\n\nUse /setkey first.",
            parse_mode="HTML")
        return

    if us.busy:
        bot.send_message(chat_id,
            "⏳ <i>Still processing previous message…</i>",
            parse_mode="HTML")
        return

    doc_parts = []
    has_photo = any(m.photo for m in all_msgs)

    for msg in all_msgs:
        # ── Documents ───────────────────────────────────────────
        if msg.document:
            try:
                finfo = bot.get_file(msg.document.file_id)
                fdata = bot.download_file(finfo.file_path)
                fname = msg.document.file_name or "file"
                mime  = (msg.document.mime_type or "").lower()

                is_pdf = fname.lower().endswith(".pdf") or mime == "application/pdf"

                if is_pdf:
                    pdf_text = extract_pdf_text(fdata)
                    doc_parts.append(f"[PDF: {fname}]\n{pdf_text}")
                    log.info(f"User {uid}: extracted PDF {fname} ({len(fdata)} bytes)")
                else:
                    try:
                        content = fdata.decode("utf-8")
                        doc_parts.append(f"[File: {fname}]\n```\n{content}\n```")
                    except UnicodeDecodeError:
                        doc_parts.append(f"[Binary file: {fname}, {len(fdata)} bytes — cannot display]")
                    log.info(f"User {uid}: loaded document {fname}")
            except Exception as e:
                log.warning(f"Could not process document: {e}")

        # ── Extra text in a group ────────────────────────────────
        if msg.text and not msg.photo and not msg.document:
            if msg.text.strip() and msg.text.strip() != user_text:
                user_text += "\n" + msg.text.strip()

    # Reject photos with a clear message
    if has_photo:
        bot.send_message(chat_id,
            "⚠️ <b>Images not supported</b>\n\n"
            "This bot can't process images via the unofficial API.\n"
            "Please describe what you need in text instead.",
            parse_mode="HTML",
            reply_to_message_id=first_msg.message_id)
        if not user_text.strip() and not doc_parts:
            return

    # Build combined prompt
    combined = user_text or ""
    for doc in doc_parts:
        combined += f"\n\n{doc}"

    if not combined.strip():
        return

    # ── Replay resumed history as context (one-time, on first message) ─
    if us.history and not us.conversation_id:
        replay_lines = ["[Continuing previous conversation — context below]\n"]
        for h in us.history:
            role = "User" if h["role"] == "user" else "Claude"
            replay_lines.append(f"{role}: {h['text']}")
        replay_lines.append("\n[New message]")
        combined = "\n".join(replay_lines) + "\n" + combined
        log.info(f"User {uid}: replaying {len(us.history)} history msg(s) into new conversation")

    group_note = f"📎 <i>Grouped {len(doc_parts)} file(s) into one request</i>\n" if len(doc_parts) > 1 else ""

    us.busy  = True
    thinking = bot.send_message(
        chat_id,
        group_note + "🧠 <i>Claude is thinking…</i>",
        parse_mode          = "HTML",
        reply_to_message_id = first_msg.message_id,
    )
    bot.send_chat_action(chat_id, "typing")

    try:
        result    = send_message(us, combined, [], status_msg=thinking, chat_id=chat_id)
        resp_text = result["text"]
        files     = result["files"]

        if not resp_text.strip():
            bot.edit_message_text("⚠️ <i>Empty response from Claude.</i>",
                chat_id=thinking.chat.id, message_id=thinking.message_id, parse_mode="HTML")
            return

        try:
            bot.delete_message(thinking.chat.id, thinking.message_id)
        except Exception:
            pass

        send_chunked(chat_id, md_to_tg_html(resp_text), reply_to=first_msg.message_id)

        if files:
            send_files(chat_id, files, reply_to=first_msg.message_id)

        us.history.append({"role": "user",      "text": combined[:200]})
        us.history.append({"role": "assistant", "text": resp_text[:200]})
        log.info(f"User {uid} → {len(resp_text)} chars, {len(files)} file(s)")

    except RuntimeError as e:
        try:
            bot.edit_message_text(
                f"❌ <b>Error:</b>\n<code>{html_lib.escape(str(e))}</code>",
                chat_id=thinking.chat.id, message_id=thinking.message_id, parse_mode="HTML")
        except Exception:
            bot.send_message(chat_id, f"❌ {e}")

    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"

        if code == 403:
            err = (
                "🔑 <b>Session key expired or invalid.</b>\n\n"
                "Your claude.ai session has likely logged out or the key was revoked.\n\n"
                "<b>Fix:</b>\n"
                "1. Open claude.ai in your browser and log in again\n"
                "2. Get a fresh <code>sessionKey</code> cookie\n"
                "3. Run /setkey with the new key\n\n"
                "If you're on a VPS, this could also mean your IP got blocked — "
                "try /validate first, and /addproxy if needed."
            )
        elif code == 429:
            reset_info = getattr(e, "reset_info", "") or ""
            err = (
                f"⏳ <b>Claude usage limit reached{reset_info}.</b>\n\n"
                f"This account has hit its message limit for the current window. "
                f"Retried {RETRY_MAX}x with proxy rotation — still limited.\n\n"
                "<b>Options:</b>\n"
                "• Wait for the limit to reset (usually a few hours for free accounts)\n"
                "• Switch to a different account with /setkey\n"
                "• Add more proxies with /addproxy to spread requests across IPs"
            )
        elif code == 500:
            err = "💥 Claude server error. Try again in a moment."
        else:
            err = f"HTTP {code} error from Claude."

        try:
            bot.edit_message_text(f"❌ <b>Error {code}</b>\n\n{err}",
                chat_id=thinking.chat.id, message_id=thinking.message_id, parse_mode="HTML")
        except Exception:
            bot.send_message(chat_id, f"❌ Error {code}: {err}", parse_mode="HTML")

    except Exception as e:
        log.exception(f"Unhandled error for user {uid}")
        try:
            bot.edit_message_text(
                f"❌ <b>Unexpected error:</b>\n<code>{html_lib.escape(str(e))}</code>",
                chat_id=thinking.chat.id, message_id=thinking.message_id, parse_mode="HTML")
        except Exception:
            pass
    finally:
        us.busy = False


# ═══════════════════════════════════════════════════════════════════
#               MAIN MESSAGE HANDLER
# ═══════════════════════════════════════════════════════════════════

@bot.message_handler(content_types=["text", "document", "photo"])
@auth_check
def handle_message(msg: Message):
    uid = msg.from_user.id
    us  = get_session(uid)

    if not us.session_key or not us.organization_id:
        bot.reply_to(msg,
            "⚠️ <b>No session key!</b>\n\nUse /setkey <code>&lt;key&gt;</code> first.\nSee /help for instructions.",
            parse_mode="HTML")
        return

    if us.busy:
        bot.reply_to(msg, "⏳ <i>Still processing previous message…</i>", parse_mode="HTML")
        return

    _process_combined(uid, msg.chat.id, msg, [msg], msg.text or msg.caption or "")


# ═══════════════════════════════════════════════════════════════════
#               GRACEFUL SHUTDOWN
# ═══════════════════════════════════════════════════════════════════

def graceful_shutdown(sig, frame):
    log.info("Shutdown — cleaning up conversations…")
    wiped = 0
    for uid, us in sessions.items():
        if us.tracked_convs:
            count = len(us.tracked_convs)
            wipe_all(us)
            wiped += count
    log.info(f"Wiped {wiped} conversation(s). Goodbye.")
    sys.exit(0)


signal.signal(signal.SIGINT,  graceful_shutdown)
signal.signal(signal.SIGTERM, graceful_shutdown)


# ═══════════════════════════════════════════════════════════════════
#                            MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    try:
        bot.set_my_commands([
            BotCommand("start",         "Welcome & help"),
            BotCommand("setkey",        "Set Claude session key"),
            BotCommand("newchat",       "Start fresh conversation"),
            BotCommand("validate",      "Check if key still works"),
            BotCommand("massvalidate",  "Bulk validate multiple keys"),
            BotCommand("incognito",     "Toggle incognito mode"),
            BotCommand("wipe",          "Delete all tracked conversations"),
            BotCommand("status",        "Show session info"),
            BotCommand("myid",          "Get your Telegram user ID"),
            BotCommand("addproxy",      "Add proxy to rotation pool"),
            BotCommand("proxies",       "List all proxies in pool"),
            BotCommand("delproxy",      "Remove proxy by number"),
            BotCommand("clearproxies",  "Remove all proxies"),
            BotCommand("proxystatus",   "Show active proxy + exit IP"),
            BotCommand("nextproxy",     "Manually rotate to next proxy"),
            BotCommand("help",          "Show help"),
        ])
        log.info("✓ Commands registered")
    except Exception as e:
        log.warning(f"Could not register commands: {e}")

    log.info("🚀 Polling…")
    bot.infinity_polling(timeout=60, long_polling_timeout=60)


if __name__ == "__main__":
    main()
