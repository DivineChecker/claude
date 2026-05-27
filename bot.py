#!/usr/bin/env python3
"""
🕵️ Claude Incognito Telegram Bot - Fixed Web Search
"""

import json
import uuid
import re
import os
import io
import html as html_lib
import time
import logging
import signal
import sys
import urllib.parse
from typing import Optional
from dataclasses import dataclass, field
from collections import defaultdict
import threading

import requests
from telebot import TeleBot, apihelper
from telebot.types import Message, BotCommand

# ═══════════════════════════════════════════════════════════════════
#                        CONFIGURATION
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

bot = TeleBot(BOT_TOKEN, parse_mode="HTML")

# ═══════════════════════════════════════════════════════════════════
#                    PROXY POOL MANAGER
# ═══════════════════════════════════════════════════════════════════

class ProxyPool:
    MAX_FAILS = 3

    def __init__(self, proxies: list[str] = None):
        self._proxies : list[str] = list(proxies or [])
        self._index   : int       = 0
        self._fails   : dict      = defaultdict(int)

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
        return [(i+1, url, self._fails.get(url, 0)) for i, url in enumerate(self._proxies)]

    def add(self, proxy_url: str) -> bool:
        if proxy_url in self._proxies:
            return False
        self._proxies.append(proxy_url)
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
        return removed

    def clear(self):
        self._proxies.clear()
        self._fails.clear()
        self._index = 0

    def rotate(self) -> str:
        if len(self._proxies) <= 1:
            return self.active
        self._index = (self._index + 1) % len(self._proxies)
        return self.active

    def mark_failed(self, proxy_url: str) -> bool:
        if proxy_url not in self._proxies:
            return False
        self._fails[proxy_url] += 1
        if self._fails[proxy_url] >= self.MAX_FAILS:
            idx = self._proxies.index(proxy_url)
            self._proxies.pop(idx)
            self._fails.pop(proxy_url, None)
            if self._proxies:
                self._index = self._index % len(self._proxies)
            else:
                self._index = 0
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
    session_key     : str       = ""
    organization_id : str       = ""
    conversation_id : str       = ""
    model           : str       = field(default_factory=lambda: DEFAULT_MODEL)
    tracked_convs   : list      = field(default_factory=list)
    history         : list      = field(default_factory=list)
    http            : requests.Session = field(default_factory=requests.Session)
    incognito       : bool      = True
    busy            : bool      = False
    web_search      : bool      = False
    proxy_pool      : ProxyPool = field(default_factory=lambda: ProxyPool(DEFAULT_PROXIES))

    def __post_init__(self):
        self._apply_headers()
        self._sync_proxy()

    def _apply_headers(self):
        self.http.headers.update({
            "User-Agent"                : (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept"                    : "text/event-stream, */*",
            "Accept-Language"           : "en-US,en;q=0.9",
            "Accept-Encoding"           : "gzip, deflate, br",
            "Content-Type"              : "application/json",
            "Origin"                    : "https://claude.ai",
            "Referer"                   : "https://claude.ai/chats",
            "Sec-Ch-Ua"                 : '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "Sec-Ch-Ua-Mobile"          : "?0",
            "Sec-Ch-Ua-Platform"        : '"Windows"',
            "Sec-Fetch-Dest"            : "empty",
            "Sec-Fetch-Mode"            : "cors",
            "Sec-Fetch-Site"            : "same-origin",
            "anthropic-client-platform" : "web_claude_ai",
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
            name="sessionKey", value=key,
            domain=".claude.ai", path="/", secure=True,
        )


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
        return False, "", f"Proxy unreachable: {e}"
    except requests.exceptions.Timeout:
        return False, "", "Timeout"
    except Exception as e:
        return False, "", str(e)


# ═══════════════════════════════════════════════════════════════════
#            INTERCEPT CLAUDE.AI NETWORK — HOW WEB SEARCH WORKS
# ═══════════════════════════════════════════════════════════════════
#
# When you enable web search on claude.ai and send a message, the
# browser sends to the /completion endpoint with this in the payload:
#
#   "tools": [{
#       "name": "web_search",
#       "type": "computer_20250124"   ← this is the key field
#   }]
#
# BUT the conversation must also be created with web_search capability.
# The correct approach (observed from browser DevTools) is:
#
#  1. Create conversation normally (already done)
#  2. POST /completion with tools array containing web_search
#  3. The response stream includes tool_use events when Claude searches,
#     followed by tool_result events with search results,
#     then the final text_delta events with the answer
#
# The real issue was the tool definition format. Claude.ai sends:
#
#   "tools": [{"name": "web_search", "type": "computer_20250124"}]
#
# NOT the format we were sending before.
# ═══════════════════════════════════════════════════════════════════

def _get_web_search_tool_definition() -> dict:
    """
    Exact tool definition that claude.ai sends in the browser.
    Captured via DevTools Network tab on claude.ai with web search enabled.
    """
    return {
        "name": "web_search",
        "type": "computer_20250124",
    }


# ═══════════════════════════════════════════════════════════════════
#                     CLAUDE API FUNCTIONS
# ═══════════════════════════════════════════════════════════════════

def validate_key(session_key: str, proxy_url: str = "") -> tuple[bool, str, str]:
    s = requests.Session()
    s.headers.update({
        "User-Agent"                : "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept"                    : "*/*",
        "Content-Type"              : "application/json",
        "Origin"                    : "https://claude.ai",
        "Referer"                   : "https://claude.ai/chats",
        "anthropic-client-platform" : "web_claude_ai",
    })
    s.cookies.set("sessionKey", session_key, domain=".claude.ai", path="/", secure=True)
    if proxy_url:
        s.proxies = {"http": proxy_url, "https": proxy_url}

    try:
        resp = s.get(f"{BASE_URL}/organizations", timeout=15)
        if resp.status_code == 403:
            return False, "", "Expired/Invalid key — or VPS IP blocked (use /addproxy)"
        if resp.status_code == 401:
            return False, "", "Unauthorized / Invalid Key"
        resp.raise_for_status()
        orgs = resp.json()
        if not orgs:
            return False, "", "No organizations found"
        org_name = orgs[0].get("name", "Unknown Org")
        org_id   = orgs[0]["uuid"]

        conv_resp = s.post(
            f"{BASE_URL}/organizations/{org_id}/chat_conversations",
            json={"uuid": str(uuid.uuid4()), "name": "", "model": DEFAULT_MODEL},
            timeout=15,
        )
        if conv_resp.status_code == 403:
            return False, "", "Blocked for chat (403) — add a proxy"
        if conv_resp.status_code not in (200, 201):
            return False, "", f"Cannot create conversations (HTTP {conv_resp.status_code})"

        test_id = conv_resp.json().get("uuid")
        if test_id:
            try:
                s.delete(f"{BASE_URL}/organizations/{org_id}/chat_conversations/{test_id}", timeout=5)
            except Exception:
                pass

        return True, org_id, org_name

    except Exception as e:
        return False, "", str(e)


def create_conversation(us: UserSession) -> str:
    url  = f"{BASE_URL}/organizations/{us.organization_id}/chat_conversations"
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
        log.warning(f"Failed to delete {conv_id[:12]}: {e}")
        return False


def wipe_all(us: UserSession):
    count = len(us.tracked_convs)
    for cid in list(us.tracked_convs):
        delete_conversation(us, cid)
    us.conversation_id = ""
    us.history = []
    log.info(f"Wiped {count} conversation(s)")


# ── SSE Stream Parsers ─────────────────────────────────────────────

def _parse_sse_stream(resp: requests.Response, web_search: bool = False) -> tuple[str, list[str]]:
    """
    Parse a Claude SSE stream.
    Returns (full_text, search_queries_used)

    Handles ALL event types:
      - completion           (old /completion format)
      - content_block_start  (new format)
      - content_block_delta  (new format)
      - content_block_stop
      - message_stop
      - tool_use             (web search queries)
      - tool_result          (web search results — we skip the raw results)
      - error
    """
    full_text      = ""
    search_queries = []
    in_tool        = False
    in_thinking    = False
    current_tool   = {}

    for raw_line in resp.iter_lines(decode_unicode=True):
        if not raw_line:
            continue

        # Strip "data: " prefix
        if raw_line.startswith("data: "):
            line = raw_line[6:].strip()
        elif raw_line.startswith("{"):
            line = raw_line.strip()
        else:
            continue

        if line in ("", "[DONE]"):
            continue

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            log.debug(f"Non-JSON SSE: {line[:100]}")
            continue

        etype = event.get("type", "")

        # ── Old /completion style ──────────────────────────────────
        if etype == "completion":
            if not in_tool and not in_thinking:
                full_text += event.get("completion", "")

        # ── New streaming format ───────────────────────────────────
        elif etype == "message_start":
            pass  # ignore

        elif etype == "content_block_start":
            block = event.get("content_block", {})
            btype = block.get("type", "")

            if btype == "tool_use":
                in_tool      = True
                in_thinking  = False
                current_tool = {
                    "id"   : block.get("id", ""),
                    "name" : block.get("name", ""),
                    "input": "",
                }

            elif btype == "thinking":
                in_thinking = True
                in_tool     = False

            elif btype == "text":
                in_tool     = False
                in_thinking = False
                # Some blocks start with text already
                txt = block.get("text", "")
                if txt:
                    full_text += txt

            elif btype == "server_tool_use":
                # Web search appears as server_tool_use in newer API versions
                in_tool      = True
                in_thinking  = False
                current_tool = {
                    "id"   : block.get("id", ""),
                    "name" : block.get("name", ""),
                    "input": "",
                }

        elif etype == "content_block_delta":
            delta = event.get("delta", {})
            dtype = delta.get("type", "")

            if in_tool:
                # Accumulate tool input JSON
                if dtype == "input_json_delta":
                    current_tool["input"] = current_tool.get("input", "") + delta.get("partial_json", "")

            elif in_thinking:
                pass  # skip thinking content

            else:
                if dtype == "text_delta":
                    full_text += delta.get("text", "")

        elif etype == "content_block_stop":
            if in_tool and current_tool:
                # Extract search query from accumulated tool input
                tool_name = current_tool.get("name", "")
                if tool_name == "web_search" and current_tool.get("input"):
                    try:
                        tool_input = json.loads(current_tool["input"])
                        query = tool_input.get("query", "")
                        if query:
                            search_queries.append(query)
                            log.info(f"Web search query: {query}")
                    except (json.JSONDecodeError, Exception):
                        pass
                current_tool = {}
            in_tool     = False
            in_thinking = False

        elif etype == "message_delta":
            pass  # stop_reason etc

        elif etype == "message_stop":
            break

        elif etype == "error":
            err = event.get("error", {})
            raise RuntimeError(f"Claude API error: {err.get('message', str(err))}")

        elif etype == "ping":
            pass

    return full_text, search_queries


def _build_completion_payload(
    text       : str,
    attachments: list,
    web_search : bool,
    model      : str,
) -> dict:
    """
    Build the exact payload that claude.ai sends.

    With web search OFF:
        Standard payload, no tools.

    With web search ON:
        Adds the web_search tool definition.
        This matches what the claude.ai browser sends when
        "Search the web" is toggled on.
    """
    payload = {
        "prompt"     : text,
        "timezone"   : "UTC",
        "attachments": attachments,
        "files"      : [],
        "model"      : model,
    }

    if web_search:
        # Exact format observed in claude.ai browser network requests
        # when web search is enabled. The type "computer_20250124" is
        # the internal identifier claude.ai uses for its web search tool.
        payload["tools"] = [
            {
                "name": "web_search",
                "type": "computer_20250124",
            }
        ]

    return payload


def send_message(
    us         : UserSession,
    text       : str,
    attachments: list = None,
    status_msg = None,
    chat_id    : int  = None,
) -> dict:
    """
    Send a message to Claude.
    Returns { 'text': str, 'files': list, 'search_queries': list }
    """
    if not us.conversation_id:
        create_conversation(us)

    url = (
        f"{BASE_URL}/organizations/{us.organization_id}"
        f"/chat_conversations/{us.conversation_id}/completion"
    )

    payload     = _build_completion_payload(
        text        = text,
        attachments = attachments or [],
        web_search  = us.web_search,
        model       = us.model,
    )

    last_error  = None
    retry_delay = RETRY_DELAY

    for attempt in range(1, RETRY_MAX + 1):
        current_proxy = us.proxy_pool.active

        try:
            us._sync_proxy()
            log.debug(
                f"Attempt {attempt}/{RETRY_MAX} | "
                f"WebSearch={us.web_search} | "
                f"Proxy={_mask_proxy(current_proxy) or 'direct'}"
            )

            resp = us.http.post(url, json=payload, stream=True, timeout=120)

            # ── Handle HTTP errors before reading stream ───────────
            if resp.status_code == 429:
                if attempt < RETRY_MAX:
                    wait = retry_delay * attempt
                    log.warning(f"429 — waiting {wait}s (attempt {attempt})")
                    if status_msg and chat_id:
                        try:
                            bot.edit_message_text(
                                f"⏳ <i>Rate limited — retrying in {wait}s… ({attempt}/{RETRY_MAX})</i>",
                                chat_id=chat_id, message_id=status_msg.message_id,
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass
                    time.sleep(wait)
                    if us.proxy_pool.count > 1:
                        us.rotate_proxy()
                    continue
                resp.raise_for_status()

            if resp.status_code == 403:
                log.error(f"403 (attempt {attempt})")
                if current_proxy:
                    us.proxy_pool.mark_failed(current_proxy)
                    if us.proxy_pool.count > 0:
                        us._sync_proxy()
                        if attempt < RETRY_MAX:
                            time.sleep(2)
                            continue
                resp.raise_for_status()

            if resp.status_code == 400:
                # Log the body for debugging
                body = resp.text[:500]
                log.error(f"400 Bad Request body: {body}")

                # If web search caused the 400, retry without it
                if us.web_search and "tools" in payload:
                    log.warning("400 with web_search tools — retrying without tools (account may not support web search)")
                    if status_msg and chat_id:
                        try:
                            bot.edit_message_text(
                                "⚠️ <i>Web search not supported on this account — sending without…</i>",
                                chat_id=chat_id, message_id=status_msg.message_id,
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass
                    # Rebuild payload without tools and retry immediately
                    payload = _build_completion_payload(
                        text        = text,
                        attachments = attachments or [],
                        web_search  = False,   # ← disabled for this request
                        model       = us.model,
                    )
                    continue

                raise RuntimeError(
                    f"HTTP 400 Bad Request.\nDetails: {body}\n"
                    "The API payload may be malformed."
                )

            resp.raise_for_status()

            # ── Parse the SSE stream ───────────────────────────────
            full_text, search_queries = _parse_sse_stream(resp, web_search=us.web_search)

            # ── Success ────────────────────────────────────────────
            us.proxy_pool.mark_success(current_proxy)
            log.info(
                f"Response: {len(full_text)} chars | "
                f"searches: {search_queries} | "
                f"attempt: {attempt}"
            )

            if us.incognito and AUTO_WIPE and us.conversation_id:
                cid = us.conversation_id
                us.conversation_id = ""
                delete_conversation(us, cid)

            return {
                "text"          : full_text,
                "files"         : extract_code_files(full_text),
                "search_queries": search_queries,
            }

        except requests.exceptions.ProxyError as e:
            log.error(f"Proxy error attempt {attempt}: {e}")
            if current_proxy:
                us.proxy_pool.mark_failed(current_proxy)
                if us.proxy_pool.count > 0:
                    us._sync_proxy()
                    if attempt < RETRY_MAX:
                        time.sleep(2)
                        last_error = e
                        continue
            raise RuntimeError(f"All proxies failed: {e}")

        except requests.exceptions.Timeout:
            log.warning(f"Timeout attempt {attempt}")
            if attempt < RETRY_MAX:
                time.sleep(5)
                continue
            raise RuntimeError("Timed out after all retries")

        except RuntimeError:
            raise

        except Exception as e:
            log.error(f"Unexpected error attempt {attempt}: {e}")
            last_error = e
            break

    if last_error:
        raise last_error
    raise RuntimeError("Failed after all retries")


# ═══════════════════════════════════════════════════════════════════
#               CODE EXTRACTION & FILE GENERATION
# ═══════════════════════════════════════════════════════════════════

LANG_EXTENSIONS = {
    "python": ".py", "py": ".py", "javascript": ".js", "js": ".js",
    "typescript": ".ts", "ts": ".ts", "java": ".java", "c": ".c",
    "cpp": ".cpp", "c++": ".cpp", "csharp": ".cs", "cs": ".cs",
    "go": ".go", "rust": ".rs", "ruby": ".rb", "php": ".php",
    "swift": ".swift", "kotlin": ".kt", "scala": ".scala",
    "html": ".html", "css": ".css", "scss": ".scss", "sql": ".sql",
    "bash": ".sh", "sh": ".sh", "shell": ".sh", "zsh": ".sh",
    "yaml": ".yaml", "yml": ".yaml", "toml": ".toml", "json": ".json",
    "xml": ".xml", "markdown": ".md", "md": ".md",
    "dockerfile": "Dockerfile", "docker": "Dockerfile",
    "makefile": "Makefile", "r": ".r", "lua": ".lua",
    "perl": ".pl", "dart": ".dart", "vue": ".vue", "svelte": ".svelte",
    "jsx": ".jsx", "tsx": ".tsx", "graphql": ".graphql",
    "proto": ".proto", "tf": ".tf", "powershell": ".ps1", "ps1": ".ps1",
    "bat": ".bat",
}


def extract_code_files(text: str) -> list:
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
    for open_tag, close_tag in [
        ("<pre>","</pre>"), ("<code>","</code>"),
        ("<b>","</b>"),     ("<i>","</i>"),     ("<s>","</s>"),
    ]:
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
            bot.reply_to(msg,
                f"🚫 <b>Access Denied</b>\nYour ID: <code>{msg.from_user.id}</code>",
                parse_mode="HTML")
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
/setkey <code>&lt;session_key&gt;</code> — Set Claude session key
/validate — Check if key works

<b>━━━ Proxy Pool ━━━</b>
/addproxy <code>&lt;url&gt;</code> — Add proxy
/proxies — List proxies
/delproxy <code>&lt;n&gt;</code> — Remove proxy #n
/clearproxies — Remove all proxies
/proxystatus — Active proxy + exit IP
/nextproxy — Rotate to next proxy

<b>━━━ Chat ━━━</b>
Just send any message!

<b>━━━ Controls ━━━</b>
/newchat — Fresh conversation
/model — Change model
/incognito — Toggle incognito
/websearch — Toggle web search
/wipe — Delete tracked chats
/status — Session info
/myid — Your Telegram ID

<b>━━━ Web Search Note ━━━</b>
Web search requires it to be enabled on your claude.ai account.
If not supported, it auto-falls back to normal mode.

<b>━━━ Get Session Key ━━━</b>
1. Login at <a href="https://claude.ai">claude.ai</a>
2. F12 → Application → Cookies → <code>sessionKey</code>
3. /setkey &lt;paste_here&gt;
""".strip(), parse_mode="HTML", disable_web_page_preview=True)


@bot.message_handler(commands=["myid"])
def cmd_myid(msg: Message):
    bot.reply_to(msg, f"🪪 Your ID: <code>{msg.from_user.id}</code>", parse_mode="HTML")


@bot.message_handler(commands=["setkey"])
@auth_check
def cmd_setkey(msg: Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(msg,
            "⚠️ Usage: /setkey <code>&lt;session_key&gt;</code>",
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
    valid, org_id, info = validate_key(key, proxy_url=us.proxy_pool.active)

    try:
        bot.delete_message(msg.chat.id, thinking.message_id)
    except Exception:
        pass

    if not valid:
        bot.send_message(msg.chat.id,
            f"❌ <b>Invalid Key</b>\n\n<code>{html_lib.escape(info)}</code>",
            parse_mode="HTML")
        return

    if us.organization_id and us.organization_id != org_id:
        wipe_all(us)

    us.set_key(key)
    us.organization_id = org_id

    bot.send_message(msg.chat.id,
        f"✅ <b>Key Set!</b>\n\n"
        f"🏢 Org: <code>{html_lib.escape(info)}</code>\n"
        f"🤖 Model: <code>{us.model}</code>\n"
        f"🕵️ Incognito: {'🟢' if us.incognito else '🔴'}\n"
        f"<i>🔐 Key message deleted.</i>",
        parse_mode="HTML")


@bot.message_handler(commands=["addproxy"])
@auth_check
def cmd_addproxy(msg: Message):
    parts = msg.text.split(maxsplit=1)
    uid   = msg.from_user.id

    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(msg,
            "🌐 Usage: <code>/addproxy http://user:pass@host:port</code>",
            parse_mode="HTML")
        return

    proxy_url = parts[1].strip()
    try:
        bot.delete_message(msg.chat.id, msg.message_id)
    except Exception:
        pass

    valid, err = _parse_proxy_url(proxy_url)
    if not valid:
        bot.send_message(msg.chat.id,
            f"❌ Invalid proxy: <code>{html_lib.escape(err)}</code>",
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
            f"❌ Proxy test failed: <code>{html_lib.escape(test_err)}</code>",
            parse_mode="HTML")
        return

    us    = get_session(uid)
    added = us.proxy_pool.add(proxy_url)
    us._sync_proxy()

    if not added:
        bot.send_message(msg.chat.id,
            f"⚠️ Already in pool. Exit IP: <code>{html_lib.escape(ip)}</code>",
            parse_mode="HTML")
        return

    bot.send_message(msg.chat.id,
        f"✅ <b>Proxy #{us.proxy_pool.count} added!</b>\n"
        f"📍 Exit IP: <code>{html_lib.escape(ip)}</code>\n"
        f"🔄 Pool: {us.proxy_pool.count} proxy(s)",
        parse_mode="HTML")


@bot.message_handler(commands=["proxies"])
@auth_check
def cmd_proxies(msg: Message):
    us = get_session(msg.from_user.id)
    if us.proxy_pool.count == 0:
        bot.reply_to(msg, "🌐 Pool empty. /addproxy to add.", parse_mode="HTML")
        return

    lines = []
    for idx, url, fails in us.proxy_pool.all_proxies():
        active = " ◀ <b>ACTIVE</b>" if idx == us.proxy_pool.active_index else ""
        fail   = f" ⚠️{fails}" if fails > 0 else ""
        lines.append(f"<b>#{idx}</b> <code>{html_lib.escape(_mask_proxy(url))}</code>{fail}{active}")

    bot.reply_to(msg,
        f"🌐 <b>Proxy Pool ({us.proxy_pool.count})</b>\n━━━━━━━━━━\n" + "\n".join(lines),
        parse_mode="HTML")


@bot.message_handler(commands=["delproxy"])
@auth_check
def cmd_delproxy(msg: Message):
    parts = msg.text.split(maxsplit=1)
    us    = get_session(msg.from_user.id)

    if us.proxy_pool.count == 0:
        bot.reply_to(msg, "ℹ️ No proxies in pool.")
        return

    if len(parts) < 2 or not parts[1].strip().isdigit():
        bot.reply_to(msg, f"⚠️ Usage: /delproxy <code>&lt;number&gt;</code>", parse_mode="HTML")
        return

    removed = us.proxy_pool.remove(int(parts[1].strip()))
    us._sync_proxy()

    if removed is None:
        bot.reply_to(msg, "❌ Invalid proxy number.")
        return

    bot.reply_to(msg,
        f"🗑 Removed: <code>{html_lib.escape(_mask_proxy(removed))}</code>\n"
        f"Remaining: {us.proxy_pool.count}",
        parse_mode="HTML")


@bot.message_handler(commands=["clearproxies"])
@auth_check
def cmd_clearproxies(msg: Message):
    us = get_session(msg.from_user.id)
    if us.proxy_pool.count == 0:
        bot.reply_to(msg, "ℹ️ Already empty.")
        return
    count = us.proxy_pool.count
    us.proxy_pool.clear()
    us._sync_proxy()
    bot.reply_to(msg, f"🗑 Removed all {count} proxy(s).", parse_mode="HTML")


@bot.message_handler(commands=["nextproxy"])
@auth_check
def cmd_nextproxy(msg: Message):
    us = get_session(msg.from_user.id)
    if us.proxy_pool.count < 2:
        bot.reply_to(msg, "ℹ️ Need at least 2 proxies to rotate.")
        return
    new_proxy = us.rotate_proxy()
    ok, ip, err = _test_proxy(new_proxy)
    bot.reply_to(msg,
        f"🔄 Now on proxy #{us.proxy_pool.active_index}\n"
        f"<code>{html_lib.escape(_mask_proxy(new_proxy))}</code>\n"
        + (f"📍 IP: <code>{ip}</code>" if ok else f"⚠️ Test failed: {err}"),
        parse_mode="HTML")


@bot.message_handler(commands=["proxystatus"])
@auth_check
def cmd_proxystatus(msg: Message):
    us = get_session(msg.from_user.id)
    bot.send_chat_action(msg.chat.id, "typing")
    active     = us.proxy_pool.active
    ok, ip, err = _test_proxy(active)
    bot.reply_to(msg,
        f"📊 <b>Proxy Status</b>\n━━━━━━━━━━\n"
        f"🌐 Active: {('<code>' + html_lib.escape(_mask_proxy(active)) + '</code>') if active else '<i>None (direct)</i>'}\n"
        f"📍 Exit IP: {('<code>' + html_lib.escape(ip) + '</code>') if ok else ('❌ ' + err)}\n"
        f"🔄 Pool: {us.proxy_pool.count} proxy(s)",
        parse_mode="HTML")


@bot.message_handler(commands=["validate"])
@auth_check
def cmd_validate(msg: Message):
    us = get_session(msg.from_user.id)
    if not us.session_key:
        bot.reply_to(msg, "⚠️ No key set. Use /setkey first.")
        return
    bot.send_chat_action(msg.chat.id, "typing")
    valid, _, info = validate_key(us.session_key, proxy_url=us.proxy_pool.active)
    if valid:
        bot.reply_to(msg, f"✅ Key valid! Org: <code>{html_lib.escape(info)}</code>", parse_mode="HTML")
    else:
        bot.reply_to(msg, f"❌ Invalid: <code>{html_lib.escape(info)}</code>", parse_mode="HTML")


@bot.message_handler(commands=["massvalidate"])
@auth_check
def cmd_massvalidate(msg: Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(msg,
            "📋 One key per line:\n<code>/massvalidate\nkey1\nkey2</code>",
            parse_mode="HTML")
        return

    keys   = [k.strip() for k in parts[1].strip().split("\n") if k.strip()]
    us     = get_session(msg.from_user.id)
    try:
        bot.delete_message(msg.chat.id, msg.message_id)
    except Exception:
        pass

    status = bot.send_message(msg.chat.id, f"🔄 Validating {len(keys)} key(s)…", parse_mode="HTML")
    valid_keys   = []
    invalid_keys = []

    for i, key in enumerate(keys):
        v, _, info = validate_key(key, proxy_url=us.proxy_pool.active)
        if v:
            valid_keys.append(f"✅ <code>{html_lib.escape(key)}</code> → {html_lib.escape(info)}")
        else:
            invalid_keys.append(f"❌ <code>{html_lib.escape(key)}</code> → {html_lib.escape(info)}")
        if (i+1) % 3 == 0 or i == len(keys)-1:
            try:
                bot.edit_message_text(
                    f"🔄 {i+1}/{len(keys)}…",
                    chat_id=status.chat.id, message_id=status.message_id, parse_mode="HTML")
            except Exception:
                pass

    try:
        bot.delete_message(status.chat.id, status.message_id)
    except Exception:
        pass

    report = (
        f"📊 <b>Results: {len(keys)} total | ✅ {len(valid_keys)} | ❌ {len(invalid_keys)}</b>\n\n"
        + ("\n".join(valid_keys) + "\n\n" if valid_keys else "")
        + ("\n".join(invalid_keys) if invalid_keys else "")
    )
    send_chunked(msg.chat.id, report.strip())


@bot.message_handler(commands=["newchat"])
@auth_check
def cmd_newchat(msg: Message):
    us = get_session(msg.from_user.id)
    if not us.session_key:
        bot.reply_to(msg, "⚠️ Set a key first with /setkey")
        return
    if us.conversation_id:
        delete_conversation(us, us.conversation_id)
        us.conversation_id = ""
    us.history = []
    bot.reply_to(msg, "🆕 New conversation started!", parse_mode="HTML")


@bot.message_handler(commands=["model"])
@auth_check
def cmd_model(msg: Message):
    parts = msg.text.split(maxsplit=1)
    us    = get_session(msg.from_user.id)
    if len(parts) < 2:
        bot.reply_to(msg,
            f"🤖 Current: <code>{us.model}</code>\n\n"
            "Models:\n"
            "• <code>claude-sonnet-4-20250514</code>\n"
            "• <code>claude-3-5-sonnet-20241022</code>\n"
            "• <code>claude-3-5-haiku-20241022</code>\n"
            "• <code>claude-3-opus-20240229</code>\n\n"
            "Usage: /model <code>model-name</code>",
            parse_mode="HTML")
        return
    us.model = parts[1].strip()
    bot.reply_to(msg, f"✅ Model: <code>{us.model}</code>", parse_mode="HTML")


@bot.message_handler(commands=["incognito"])
@auth_check
def cmd_incognito(msg: Message):
    us           = get_session(msg.from_user.id)
    us.incognito = not us.incognito
    bot.reply_to(msg,
        f"🕵️ Incognito: <b>{'ON 🟢' if us.incognito else 'OFF 🔴'}</b>",
        parse_mode="HTML")


@bot.message_handler(commands=["websearch"])
@auth_check
def cmd_websearch(msg: Message):
    us            = get_session(msg.from_user.id)
    us.web_search = not us.web_search
    state         = "ON 🟢" if us.web_search else "OFF 🔴"

    extra = ""
    if us.web_search:
        extra = (
            "\n\n✅ Claude will search the web for current information.\n"
            "<i>Note: Requires web search to be enabled on your claude.ai account.\n"
            "Auto-falls back to normal mode if not supported.</i>"
        )
    else:
        extra = "\n\nClaude will use training data only."

    bot.reply_to(msg,
        f"🌐 Web Search: <b>{state}</b>{extra}",
        parse_mode="HTML")


@bot.message_handler(commands=["wipe"])
@auth_check
def cmd_wipe(msg: Message):
    us    = get_session(msg.from_user.id)
    count = len(us.tracked_convs)
    if count == 0:
        bot.reply_to(msg, "ℹ️ Nothing to delete.")
        return
    wipe_all(us)
    bot.reply_to(msg, f"🧹 Deleted {count} conversation(s).", parse_mode="HTML")


@bot.message_handler(commands=["status"])
@auth_check
def cmd_status(msg: Message):
    us = get_session(msg.from_user.id)
    bot.reply_to(msg,
        f"📊 <b>Status</b>\n━━━━━━━━━━\n"
        f"🔑 Key       : {'✅' if us.session_key else '❌ not set'}\n"
        f"🕵️ Incognito : {'🟢 ON' if us.incognito else '🔴 OFF'}\n"
        f"🌐 WebSearch : {'🟢 ON' if us.web_search else '🔴 OFF'}\n"
        f"🤖 Model     : <code>{us.model}</code>\n"
        f"💬 Conv      : {'<code>' + us.conversation_id[:12] + '…</code>' if us.conversation_id else 'None'}\n"
        f"📋 Tracked   : {len(us.tracked_convs)}\n"
        f"🌐 Proxies   : {us.proxy_pool.count} (active #{us.proxy_pool.active_index})",
        parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════════
#               MEDIA GROUP BUFFER
# ═══════════════════════════════════════════════════════════════════

MEDIA_GROUP_WAIT = 1.5
_media_buffers: dict = {}
_media_lock          = threading.Lock()


def _flush_media_group(uid: int, group_id: str):
    with _media_lock:
        key  = (uid, group_id)
        data = _media_buffers.pop(key, None)
    if not data:
        return
    msgs      = data["msgs"]
    chat_id   = data["chat_id"]
    first_msg = msgs[0]
    caption   = next((m.caption for m in msgs if m.caption), "")
    _process_combined(uid, chat_id, first_msg, msgs, caption)


def _process_combined(
    uid      : int,
    chat_id  : int,
    first_msg: Message,
    all_msgs : list[Message],
    user_text: str,
):
    us = get_session(uid)

    if not us.session_key or not us.organization_id:
        bot.send_message(chat_id, "⚠️ Use /setkey first.", parse_mode="HTML")
        return

    if us.busy:
        bot.send_message(chat_id, "⏳ Still processing…", parse_mode="HTML")
        return

    doc_parts  = []
    has_photo  = False
    item_count = len(all_msgs)

    for m in all_msgs:
        if m.photo:
            has_photo = True

        if m.document:
            try:
                finfo = bot.get_file(m.document.file_id)
                fdata = bot.download_file(finfo.file_path)
                fname = m.document.file_name or "file"
                try:
                    doc_parts.append(f"[File: {fname}]\n```\n{fdata.decode('utf-8')}\n```")
                except UnicodeDecodeError:
                    doc_parts.append(f"[Binary file: {fname}, {len(fdata)} bytes]")
            except Exception as e:
                log.warning(f"Document error: {e}")

        if m.text and not m.photo and not m.document:
            if m.text.strip() and m.text.strip() != user_text:
                user_text += "\n" + m.text.strip()

    if has_photo:
        bot.send_message(
            chat_id,
            "⚠️ <i>Images not supported. Describe in text instead.</i>",
            parse_mode="HTML", reply_to_message_id=first_msg.message_id,
        )
        if not user_text.strip() and not doc_parts:
            return

    combined = user_text or ""
    for doc in doc_parts:
        combined += f"\n\n{doc}"

    if not combined.strip():
        return

    us.busy  = True
    thinking = bot.send_message(
        chat_id,
        ("🔍 <i>Claude is thinking (with web search)…</i>" if us.web_search else "🧠 <i>Claude is thinking…</i>"),
        parse_mode="HTML", reply_to_message_id=first_msg.message_id,
    )
    bot.send_chat_action(chat_id, "typing")

    try:
        result         = send_message(us, combined, [], status_msg=thinking, chat_id=chat_id)
        resp_text      = result["text"]
        files          = result["files"]
        search_queries = result.get("search_queries", [])

        if not resp_text.strip():
            bot.edit_message_text(
                "⚠️ <i>Empty response from Claude.</i>",
                chat_id=thinking.chat.id, message_id=thinking.message_id, parse_mode="HTML",
            )
            return

        try:
            bot.delete_message(thinking.chat.id, thinking.message_id)
        except Exception:
            pass

        # Show search queries used (if any)
        if search_queries:
            queries_text = "\n".join(f"• {q}" for q in search_queries)
            bot.send_message(
                chat_id,
                f"🔍 <i>Searched the web for:</i>\n{html_lib.escape(queries_text)}",
                parse_mode="HTML",
                reply_to_message_id=first_msg.message_id,
                disable_web_page_preview=True,
            )

        send_chunked(chat_id, md_to_tg_html(resp_text), reply_to=first_msg.message_id)

        if files:
            send_files(chat_id, files, reply_to=first_msg.message_id)

        us.history.append({"role": "user",      "text": combined[:200]})
        us.history.append({"role": "assistant",  "text": resp_text[:200]})
        log.info(f"User {uid}: {len(resp_text)} chars, {len(files)} files, searches={search_queries}")

    except RuntimeError as e:
        try:
            bot.edit_message_text(
                f"❌ <b>Error:</b>\n<code>{html_lib.escape(str(e))}</code>",
                chat_id=thinking.chat.id, message_id=thinking.message_id, parse_mode="HTML",
            )
        except Exception:
            bot.send_message(chat_id, f"❌ {e}")

    except requests.exceptions.HTTPError as e:
        code     = e.response.status_code if e.response is not None else "?"
        msgs_map = {
            400: "🔧 Bad request — check logs for details.",
            403: "🔑 Key expired or IP blocked. /validate or /addproxy",
            429: f"⏳ Rate limited after {RETRY_MAX} retries. Wait or /addproxy.",
            500: "💥 Claude server error. Try again later.",
        }
        err = msgs_map.get(code, f"HTTP {code}")
        try:
            bot.edit_message_text(
                f"❌ <b>Error {code}:</b>\n{err}",
                chat_id=thinking.chat.id, message_id=thinking.message_id, parse_mode="HTML",
            )
        except Exception:
            bot.send_message(chat_id, f"❌ HTTP {code}: {err}")

    except Exception as e:
        log.exception(f"Unhandled error user {uid}")
        try:
            bot.edit_message_text(
                f"❌ <b>Unexpected error:</b>\n<code>{html_lib.escape(str(e))}</code>",
                chat_id=thinking.chat.id, message_id=thinking.message_id, parse_mode="HTML",
            )
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
        bot.reply_to(msg, "⚠️ Use /setkey first.", parse_mode="HTML")
        return

    if us.busy:
        bot.reply_to(msg, "⏳ Still processing…", parse_mode="HTML")
        return

    if msg.media_group_id:
        key = (uid, msg.media_group_id)
        with _media_lock:
            if key not in _media_buffers:
                timer = threading.Timer(MEDIA_GROUP_WAIT, _flush_media_group, args=(uid, msg.media_group_id))
                _media_buffers[key] = {"msgs": [msg], "timer": timer, "chat_id": msg.chat.id}
                timer.start()
            else:
                _media_buffers[key]["msgs"].append(msg)
                _media_buffers[key]["timer"].cancel()
                timer = threading.Timer(MEDIA_GROUP_WAIT, _flush_media_group, args=(uid, msg.media_group_id))
                _media_buffers[key]["timer"] = timer
                timer.start()
        return

    _process_combined(uid, msg.chat.id, msg, [msg], msg.text or msg.caption or "")


# ═══════════════════════════════════════════════════════════════════
#               GRACEFUL SHUTDOWN
# ═══════════════════════════════════════════════════════════════════

def graceful_shutdown(sig, frame):
    log.info("Shutting down…")
    for us in sessions.values():
        if us.tracked_convs:
            wipe_all(us)
    sys.exit(0)

signal.signal(signal.SIGINT,  graceful_shutdown)
signal.signal(signal.SIGTERM, graceful_shutdown)


# ═══════════════════════════════════════════════════════════════════
#                            MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    try:
        bot.set_my_commands([
            BotCommand("start",        "Help"),
            BotCommand("setkey",       "Set Claude session key"),
            BotCommand("newchat",      "Fresh conversation"),
            BotCommand("model",        "Change model"),
            BotCommand("validate",     "Check key"),
            BotCommand("massvalidate", "Bulk validate keys"),
            BotCommand("incognito",    "Toggle incognito"),
            BotCommand("websearch",    "Toggle web search"),
            BotCommand("wipe",         "Delete tracked chats"),
            BotCommand("status",       "Session info"),
            BotCommand("myid",         "Your Telegram ID"),
            BotCommand("addproxy",     "Add proxy"),
            BotCommand("proxies",      "List proxies"),
            BotCommand("delproxy",     "Remove proxy"),
            BotCommand("clearproxies", "Remove all proxies"),
            BotCommand("proxystatus",  "Proxy info + exit IP"),
            BotCommand("nextproxy",    "Rotate proxy"),
            BotCommand("help",         "Help"),
        ])
    except Exception as e:
        log.warning(f"Commands not registered: {e}")

    log.info("🚀 Polling…")
    bot.infinity_polling(timeout=60, long_polling_timeout=60)


if __name__ == "__main__":
    main()
