"""MCP server for the 4pda.to forum via stdio.

Improvements:
- Cursor-based pagination for tool results (scan_topic, search_in_thread) per MCP best practices
- Increased limits + chunked responses to avoid context blow-up
- New tools: scan_topic, analyze_usage for full-topic analysis
- Updated categories for mini-PC / generic devices

Environment variables:
  P4DA_BASE_URL, P4DA_TIMEOUT, P4DA_CONCURRENCY, P4DA_DELAY, P4DA_USER_AGENT, P4DA_COOKIE
  P4DA_CACHE_TTL, P4DA_MAX_POSTS, P4DA_MAX_CHARS
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import html as _html
import json
import logging
import math
import os
import re
import time
from collections import OrderedDict
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

_ALLOWED_HOSTS = {"4pda.to", "www.4pda.to"}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 5


def _require_approved_origin(url: str) -> str:
    """Return the normalized host only for an approved HTTPS 4PDA origin."""
    # URL allowlisting is not a DNS-rebinding boundary; see setup/mcp/4pda.md.
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
    except (TypeError, ValueError):
        host = None
        parsed = None
    if (
        parsed is None
        or parsed.scheme != "https"
        or host not in _ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.netloc.casefold() not in _ALLOWED_HOSTS
    ):
        raise ValueError("URL must use the approved HTTPS 4pda.to origin")
    return host


def _validate_base_url(value: str) -> str:
    host = _require_approved_origin(value)
    parsed = urlsplit(value)
    if (
        parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or "?" in value
        or "#" in value
    ):
        raise ValueError(
            "P4DA_BASE_URL must be an HTTPS 4pda.to origin without path, query, or fragment"
        )
    return f"https://{host}"


def _validate_topic_id(topic_id: str | int) -> str:
    if type(topic_id) is int and topic_id > 0:
        return str(topic_id)
    if (
        isinstance(topic_id, str)
        and topic_id.isascii()
        and topic_id.isdecimal()
        and topic_id.strip("0")
    ):
        return topic_id
    raise ValueError("topic_id must be a positive ASCII decimal ID")


P4DA_BASE_URL = _validate_base_url(os.environ.get("P4DA_BASE_URL", "https://4pda.to"))
P4DA_TIMEOUT = int(os.environ.get("P4DA_TIMEOUT", "30"))
P4DA_CONCURRENCY = int(os.environ.get("P4DA_CONCURRENCY", "5"))
P4DA_DELAY = float(os.environ.get("P4DA_DELAY", "0"))
P4DA_USER_AGENT = os.environ.get(
    "P4DA_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
)
P4DA_COOKIE = os.environ.get("P4DA_COOKIE", "")


def _isolate_http_library_logs() -> None:
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        if not any(
            isinstance(handler, logging.NullHandler) for handler in logger.handlers
        ):
            logger.addHandler(logging.NullHandler())
        logger.propagate = False


_isolate_http_library_logs()
POSTS_PER_PAGE = 20
CACHE_TTL = int(os.environ.get("P4DA_CACHE_TTL", "600"))
DEFAULT_MAX_POSTS = int(os.environ.get("P4DA_MAX_POSTS", "200"))
DEFAULT_MAX_CHARS = int(os.environ.get("P4DA_MAX_CHARS", "120000"))
# hard caps for safety (server-enforced)
HARD_MAX_POSTS = 2000
HARD_MAX_CHARS = 500000
HARD_MAX_PAGES_PER_CALL = 100
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_RETRY_AFTER = 10.0
CACHE_MAX_ENTRIES = 1000
_page_cache: OrderedDict[tuple[str, int], tuple[float, dict[str, Any]]] = OrderedDict()
SERVER = Server("forum-4pda")


def _require_configured_origin(url: str) -> str:
    host = _require_approved_origin(url)
    if host != urlsplit(P4DA_BASE_URL).hostname:
        raise ValueError("URL must remain on the configured HTTPS 4PDA host")
    return host


def _redirect_loop_key(url: str) -> tuple[str, str, str, str]:
    parsed = urlsplit(url)
    return (
        parsed.scheme.casefold(),
        parsed.netloc.casefold(),
        parsed.path or "/",
        parsed.query,
    )


def _build_url(topic_id: str | int, page_offset: int) -> str:
    params = {"showtopic": _validate_topic_id(topic_id)}
    if page_offset > 0:
        params["st"] = str(page_offset)
    return f"{P4DA_BASE_URL}/forum/index.php?{urlencode(params)}"


def _headers() -> dict[str, str]:
    h = {"User-Agent": P4DA_USER_AGENT, "Accept-Charset": "windows-1251"}
    if P4DA_COOKIE:
        h["Cookie"] = P4DA_COOKIE
    return h


def _encode_cursor(page: int, post_index: int = 0) -> str:
    state = (
        str(page)
        if post_index == 0
        else json.dumps({"page": page, "post": post_index}, separators=(",", ":"))
    )
    return base64.urlsafe_b64encode(state.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[int, int]:
    if not cursor:
        return 1, 0
    if len(cursor) > 4096:
        raise ValueError("Invalid cursor: token is too long")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode()
        try:
            page = int(decoded)
            post_index = 0
        except ValueError:
            state = json.loads(decoded)
            if not isinstance(state, dict) or set(state) != {"page", "post"}:
                raise ValueError("invalid cursor state")
            page, post_index = state["page"], state["post"]
            if type(page) is not int or type(post_index) is not int:
                raise ValueError("invalid cursor position")
        if page < 1 or post_index < 0 or post_index > POSTS_PER_PAGE:
            raise ValueError("invalid cursor position")
        return page, post_index
    except (ValueError, UnicodeDecodeError, binascii.Error):
        try:
            page = int(cursor)
            if page < 1:
                raise ValueError
            return page, 0
        except ValueError:
            raise ValueError(
                f"Invalid cursor: {cursor}. Use next_cursor from previous response."
            ) from None


def _plain_headers(response: httpx.Response) -> dict[str, str]:
    # Drop framing/encoding headers: the buffered content is already decoded.
    return {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in ("content-encoding", "content-length")
    }


async def _read_capped(response: httpx.Response) -> httpx.Response:
    """Buffer a streamed response under MAX_BODY_BYTES and close the stream.

    aiter_bytes() decodes content-encoding, so the cap bounds what the parser
    would actually see; a chunked body without Content-Length is covered too.
    Only 200 and 403 bodies are inspected (403 detects the login page); other
    bodies are discarded because only status and headers matter.
    """
    if response.status_code not in (200, 403):
        await response.aclose()
        return httpx.Response(
            response.status_code,
            headers=_plain_headers(response),
            request=response.request,
        )
    body = bytearray()
    try:
        async for chunk in response.aiter_bytes():
            # Check before copying so an oversized chunk is never buffered.
            if len(body) + len(chunk) > MAX_BODY_BYTES:
                raise ValueError(
                    f"4PDA response body exceeds the {MAX_BODY_BYTES}-byte limit"
                )
            body.extend(chunk)
    finally:
        await response.aclose()
    return httpx.Response(
        response.status_code,
        headers=_plain_headers(response),
        content=bytes(body),
        request=response.request,
    )


def _retry_after_delay(value: str | None, fallback: float) -> float:
    """Return the usable Retry-After delay, or raise for an unusable value.

    Accepts delta-seconds or an HTTP date. HTTP dates are compared to the
    current UTC time; a past date means no wait. A date without a zone (the
    obsolete asctime form) is read as UTC, never as host-local time.
    The overall deadline is enforced by the caller, which owns the clock.
    """
    if value is None:
        return fallback
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            # HTTP-date has one-second resolution; a past date means retry now.
            delay = max(0.0, retry_at.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            raise ValueError(
                "4PDA rate limited (HTTP 429) with an unreadable Retry-After header"
            ) from None
    if not math.isfinite(delay) or delay < 0:
        raise ValueError("4PDA rate limited (HTTP 429) with an invalid Retry-After value")
    if delay > MAX_RETRY_AFTER:
        raise ValueError(
            f"4PDA rate limited (HTTP 429); Retry-After {delay:g}s exceeds"
            f" the {MAX_RETRY_AFTER:g}s limit"
        )
    return delay


class _RetryClient:
    def __init__(self, client: httpx.AsyncClient, max_retries: int = 3):
        self._client = client
        self._max_retries = max_retries

    async def __aenter__(self):
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *a):
        await self._client.__aexit__(*a)

    async def _get_with_retries(self, url: str, kw) -> httpx.Response:
        last: Exception | None = None
        # Wall-clock deadline for the whole retry sequence, built from the
        # caller's own timeout, so an honoured Retry-After always fits.
        timeout = kw.get("timeout")
        budget = float(
            timeout
            if isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
            else P4DA_TIMEOUT
        )
        deadline = time.monotonic() + budget

        def remaining() -> float:
            return deadline - time.monotonic()

        for attempt in range(self._max_retries):
            last_attempt = attempt == self._max_retries - 1
            status = None
            try:
                async with self._client.stream(
                    "GET", url, follow_redirects=False, **kw
                ) as response:
                    status = response.status_code
                    retrying = status == 429 or (status >= 500 and not last_attempt)
                    if retrying:
                        # Discard retry bodies: only headers drive the retry.
                        await response.aclose()
                        if status == 429:
                            delay = _retry_after_delay(
                                response.headers.get("retry-after"), float(2**attempt)
                            )
                        else:
                            delay = 2**attempt * 0.5
                    else:
                        return await _read_capped(response)
            except (TimeoutError, httpx.TransportError) as e:
                last = e
                if last_attempt:
                    break
                delay = 2**attempt * 0.5
            if status == 429 and last_attempt:
                raise ValueError(
                    f"4PDA rate limited (HTTP 429) after {self._max_retries} attempts"
                )
            if delay > remaining():
                raise ValueError("4PDA request deadline exceeded while waiting to retry")
            await asyncio.sleep(delay)
        raise last or Exception("Request failed")

    async def get(self, url: str, **kw) -> httpx.Response:
        follow_redirects = kw.pop("follow_redirects", False)
        if type(follow_redirects) is not bool:
            raise ValueError("follow_redirects must be a boolean")
        current_url = url
        _require_configured_origin(current_url)
        seen = set()
        redirects = 0
        while True:
            _require_configured_origin(current_url)
            key = _redirect_loop_key(current_url)
            if key in seen:
                raise ValueError("4PDA redirect loop detected")
            seen.add(key)
            response = await self._get_with_retries(current_url, kw)
            if not follow_redirects or response.status_code not in _REDIRECT_STATUSES:
                return response
            location = response.headers.get("location")
            if not location:
                raise ValueError("4PDA redirect response omitted Location")
            if redirects >= _MAX_REDIRECTS:
                raise ValueError(f"4PDA redirect limit exceeded ({_MAX_REDIRECTS})")
            target_url = urljoin(current_url, location)
            _require_configured_origin(target_url)
            target_key = _redirect_loop_key(target_url)
            if target_key in seen:
                raise ValueError("4PDA redirect loop detected")
            current_url = target_url
            redirects += 1


async def _fetch_page(
    client: _RetryClient, topic_id: str | int, page_offset: int
) -> tuple[int, str]:
    url = _build_url(topic_id, page_offset)
    r = await client.get(url, headers=_headers(), timeout=P4DA_TIMEOUT)
    if r.status_code == 404:
        raise ValueError(f"Topic {topic_id} not found (404)")
    enc = "windows-1251"
    ct = r.headers.get("content-type", "")
    if "charset=" in ct.lower():
        m = re.search(r"charset=([^\s;]+)", ct, re.IGNORECASE)
        if m:
            enc = m.group(1).strip('"')
    text = r.content.decode(enc, errors="replace")
    if r.status_code == 403 or "вы должны быть авторизованы" in text.lower():
        raise ValueError("This thread requires authorization. Set P4DA_COOKIE env.")
    if r.status_code != 200:
        raise ValueError(f"HTTP {r.status_code} for {url}")
    return page_offset, text


def _decode_entities(t: str) -> str:
    return _html.unescape(t).replace("\xa0", " ")


def _strip_html(h: str) -> str:
    h = re.sub(r"<br\s*/?>", "\n", h, flags=re.IGNORECASE)
    h = re.sub(r"</p>", "\n\n", h, flags=re.IGNORECASE)
    return (
        __import__("bs4", fromlist=["BeautifulSoup"])
        .BeautifulSoup(h, "html.parser")
        .get_text(separator="\n", strip=True)
    )


def _parse_post(post_div, post_body_div):
    try:
        date_el = post_div.find("span", class_="post_date")
        if not date_el:
            return None
        date_text = date_el.get_text(separator=" ", strip=True)
        m = re.search(r"#(\d+)", date_text)
        if not m:
            return None
        num = int(m.group(1))
        link = date_el.find("a", href=True)
        pid_m = re.search(r"[?&]p=(\d+)", link.get("href", "") if link else "")
        pid = pid_m.group(1) if pid_m else str(num)
        nick = post_div.find("span", class_="post_nick")
        author = _decode_entities(nick.get_text(strip=True)) if nick else ""
        rank_el = post_div.find("span", class_="post_user_info")
        rank = _decode_entities(rank_el.get_text(strip=True)) if rank_el else ""
        edit_el = post_div.find("span", class_="edit")
        edit_note = _decode_entities(edit_el.get_text(strip=True)) if edit_el else ""
        body_html = str(post_body_div) if post_body_div else ""
        quotes = []
        for q in re.findall(
            r'<div class="?quotetop"?[^>]*>(.*?)</div>\s*<div class="?quotemain"?[^>]*>(.*?)</div>',
            body_html,
            re.DOTALL | re.IGNORECASE,
        ):
            quotes.append(f"[QUOTE]{_strip_html(q[1]).strip()}[/QUOTE]")
        body_text = _strip_html(body_html)
        if quotes:
            body_text = "\n".join(
                l
                for l in body_text.split("\n")
                if l.strip() not in [x.strip() for x in quotes]
            ).strip()
        links = []
        for href, label in re.findall(
            r'<a\s[^>]*href="([^"]+)"[^>]*>([^<]*)</a>', body_html
        ):
            href, label = href.strip(), _decode_entities(label.strip())
            if href.startswith("http") and label:
                links.append({"href": href, "label": label})
        atts = []
        for mm in re.finditer(
            r'<a\s[^>]*class="?attach"?[^>]*href="([^"]+)"[^>]*>\s*<[^>]*>([^<]+)<',
            body_html,
            re.IGNORECASE,
        ):
            atts.append({"href": mm.group(1), "name": _decode_entities(mm.group(2))})
        return {
            "post_id": pid,
            "number": num,
            "date": re.sub(r"\s*\|\s*#\d+", "", date_text).strip(),
            "author": author,
            "rank": rank,
            "body": _decode_entities(body_text),
            "edit_note": _decode_entities(edit_note),
            "links": links,
            "attachments": atts,
        }
    except (AttributeError, IndexError, TypeError, ValueError):
        return None


def _parse_table_post(post_table):
    """Parse the current 4PDA table-based markup, including collapsed spoilers."""
    try:
        post_id = str(post_table.get("data-post", ""))
        if not post_id.isdigit():
            return None
        header = post_table.select_one(f"#ph-{post_id}-d2")
        body = post_table.select_one(f"#post-main-{post_id}")
        if not header or not body:
            return None
        header_text = _decode_entities(header.get_text(" ", strip=True))
        number_match = re.search(r"#(\d+)", header_text)
        date_match = re.search(r"\b\d{2}\.\d{2}\.\d{2},\s*\d{2}:\d{2}\b", header_text)
        author_el = post_table.select_one("span.normalname a")
        rank_el = post_table.select_one(".mem-title")
        edit_el = body.select_one("span.edit")
        links = []
        for link in body.find_all("a", href=True):
            href = link["href"].strip()
            label = _decode_entities(link.get_text(" ", strip=True))
            if href.startswith("//"):
                href = f"https:{href}"
            if label:
                links.append({"href": href, "label": label})
        attachments = []
        for link in body.select("a.attach-file[href]"):
            attachments.append(
                {
                    "href": link["href"].strip(),
                    "name": _decode_entities(link.get_text(" ", strip=True)),
                }
            )
        return {
            "post_id": post_id,
            "number": int(number_match.group(1)) if number_match else None,
            "date": date_match.group(0) if date_match else header_text,
            "author": _decode_entities(author_el.get_text(" ", strip=True))
            if author_el
            else "",
            "rank": _decode_entities(rank_el.get_text(" ", strip=True))
            if rank_el
            else "",
            "body": _decode_entities(body.get_text("\n", strip=True)),
            "edit_note": _decode_entities(edit_el.get_text(" ", strip=True))
            if edit_el
            else "",
            "links": links,
            "attachments": attachments,
        }
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _parse_page(topic_id, page_offset, html):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    title = (
        soup.find("title").get_text(strip=True).replace(" - 4PDA", "").strip()
        if soup.find("title")
        else ""
    )
    breadcrumb = [
        a.get_text(strip=True) for a in soup.find_all("a", class_="showcrumb")
    ]
    total_pages = 1
    m = re.search(r"(\d+)\s+страниц", html)
    if m:
        total_pages = int(m.group(1))
    # Current 4PDA pages use one table[data-post] per post. This is checked
    # first so permalink pages preserve every spoiler's text as well.
    table_posts = []
    seen = set()
    for table in soup.select("table[data-post]"):
        parsed = _parse_table_post(table)
        if not parsed or parsed["post_id"] in seen:
            continue
        seen.add(parsed["post_id"])
        parsed["page"] = page_offset // POSTS_PER_PAGE + 1
        table_posts.append(parsed)
    if table_posts:
        return {
            "topic_id": str(topic_id),
            "page": page_offset // POSTS_PER_PAGE + 1,
            "title": title,
            "breadcrumb": breadcrumb,
            "total_pages": total_pages,
            "posts": table_posts,
        }

    divs = soup.find_all("div", class_="post_header_container")
    bodies = {}
    for pb in soup.find_all(
        "div", class_="post_body", id=re.compile(r"^post-main-\d+")
    ):
        mid = re.search(r"post-main-(\d+)", pb.get("id", ""))
        if mid:
            bodies[mid.group(1)] = pb
    posts = []
    seen = set()
    for hdiv in divs:
        date_el = hdiv.find("span", class_="post_date")
        if not date_el:
            continue
        link = date_el.find("a", href=True)
        mm = re.search(r"[?&]p=(\d+)", link.get("href", "") if link else "")
        if not mm:
            continue
        pid = mm.group(1)
        if pid in seen:
            continue
        parsed = _parse_post(hdiv, bodies.get(pid))
        if not parsed:
            continue
        seen.add(pid)
        parsed["page"] = page_offset // POSTS_PER_PAGE + 1
        posts.append(parsed)
    return {
        "topic_id": str(topic_id),
        "page": page_offset // POSTS_PER_PAGE + 1,
        "title": title,
        "breadcrumb": breadcrumb,
        "total_pages": total_pages,
        "posts": posts,
    }


async def _fetch_and_parse(client, topic_id, page_offset):
    topic_id = _validate_topic_id(topic_id)
    key = (topic_id, page_offset)
    _prune_page_cache()
    cached = _page_cache.get(key)
    if cached and time.monotonic() - cached[0] < CACHE_TTL:
        return cached[1]
    _, html = await _fetch_page(client, topic_id, page_offset)
    res = _parse_page(topic_id, page_offset, html)
    _page_cache[key] = (time.monotonic(), res)
    _prune_page_cache()
    return res


def _prune_page_cache(now=None):
    now = time.monotonic() if now is None else now
    expired = [
        key for key, (stored, _) in _page_cache.items() if now - stored >= CACHE_TTL
    ]
    for key in expired:
        _page_cache.pop(key, None)
    while len(_page_cache) > CACHE_MAX_ENTRIES:
        _page_cache.popitem(last=False)


def _parse_post_url(url: str) -> tuple[str, str]:
    _require_approved_origin(url)
    parsed = urlsplit(url)
    params = parse_qs(parsed.query)
    topic_id = params.get("showtopic", [""])[0]
    post_id = params.get("p", [""])[0]
    if not post_id.isdigit():
        raise ValueError("url must include numeric showtopic and p parameters")
    return _validate_topic_id(topic_id), post_id


async def _do_read_post(
    url: str | None = None, topic_id: str | None = None, post_id: str | None = None
):
    if url:
        topic_id, post_id = _parse_post_url(url)
    if not topic_id or not post_id:
        raise ValueError("provide a 4pda.to permalink or numeric topic_id and post_id")
    topic_id = _validate_topic_id(topic_id)
    if not str(post_id).isdigit():
        raise ValueError("provide a 4pda.to permalink or numeric topic_id and post_id")
    post_url = (
        f"{P4DA_BASE_URL}/forum/index.php?"
        f"{urlencode({'showtopic': str(topic_id), 'view': 'findpost', 'p': str(post_id)})}"
    )
    async with _RetryClient(
        httpx.AsyncClient(timeout=httpx.Timeout(P4DA_TIMEOUT))
    ) as client:
        response = await client.get(
            post_url, headers=_headers(), timeout=P4DA_TIMEOUT, follow_redirects=True
        )
    if response.status_code != 200:
        raise ValueError(f"HTTP {response.status_code} for post permalink")
    parsed = _parse_page(
        topic_id, 0, response.content.decode("windows-1251", errors="replace")
    )
    for post in parsed["posts"]:
        if post["post_id"] == str(post_id):
            return {
                "topic_id": str(topic_id),
                "url": post_url,
                "post": post,
                "note": "Spoiler text is included in body.",
            }
    raise ValueError(
        f"Post {post_id} was not found in the response for topic {topic_id}"
    )


async def _do_thread_info(topic_id):
    async with _RetryClient(
        httpx.AsyncClient(timeout=httpx.Timeout(P4DA_TIMEOUT))
    ) as c:
        result = await _fetch_and_parse(c, topic_id, 0)
    total_pages = result["total_pages"]
    header = None
    for p in result["posts"]:
        if p["number"] == 1:
            header = {
                "post_id": p["post_id"],
                "date": p["date"],
                "author": p["author"],
                "rank": p["rank"],
                "body": p["body"][:500] + ("…" if len(p["body"]) > 500 else ""),
            }
            break
    return {
        "topic_id": str(topic_id),
        "title": result["title"],
        "breadcrumb": result["breadcrumb"],
        "total_pages": total_pages,
        "total_posts_approx": total_pages * POSTS_PER_PAGE,
        "header_post": header,
    }


def _limit_posts(posts, max_posts, max_chars):
    max_posts = max(1, min(int(max_posts), HARD_MAX_POSTS))
    max_chars = max(1, min(int(max_chars), HARD_MAX_CHARS))
    limited = []
    used = 0
    trunc = False
    for post in posts:
        if len(limited) >= max_posts:
            trunc = True
            break
        rem = max_chars - used
        if rem <= 0:
            trunc = True
            break
        item = dict(post)
        if len(item.get("body", "")) > rem:
            item["body"] = item["body"][: max(0, rem - 1)] + "…"
            item["body_truncated"] = True
            trunc = True
        limited.append(item)
        used += len(item.get("body", ""))
    return limited, trunc


async def _collect_topic_posts(topic_id, from_page, to_page, start_post=0, query=None):
    """Collect bounded candidates while retaining the exact next unscanned position."""
    candidates = []
    seen = set()
    scanned = 0
    examined = 0
    scan_cursor = None
    title = ""
    total_pages = 1
    query_lower = query.lower() if query else None
    async with _RetryClient(
        httpx.AsyncClient(timeout=httpx.Timeout(P4DA_TIMEOUT))
    ) as client:
        page = from_page
        concurrency = max(1, int(P4DA_CONCURRENCY))

        async def fetch_one(target_page):
            if P4DA_DELAY > 0:
                await asyncio.sleep(P4DA_DELAY)
            return await _fetch_and_parse(
                client, topic_id, (target_page - 1) * POSTS_PER_PAGE
            )

        while page <= to_page:
            batch_pages = list(range(page, min(page + concurrency, to_page + 1)))
            batch = await asyncio.gather(
                *(fetch_one(target_page) for target_page in batch_pages)
            )
            for target_page, result in zip(batch_pages, batch):
                if target_page == from_page:
                    title = result["title"]
                    total_pages = result["total_pages"]
                for index, post in enumerate(result["posts"]):
                    if target_page == from_page and index < start_post:
                        continue
                    if examined >= HARD_MAX_POSTS:
                        scan_cursor = _encode_cursor(target_page, index)
                        break
                    examined += 1
                    post_id = str(post.get("post_id", ""))
                    if post_id in seen:
                        continue
                    seen.add(post_id)
                    if target_page > 1 and post.get("number") == 1:
                        continue
                    scanned += 1
                    if (
                        query_lower
                        and query_lower not in post.get("body", "").lower()
                        and query_lower not in post.get("author", "").lower()
                    ):
                        continue
                    candidates.append((post, target_page, index))
                if scan_cursor:
                    break
            if scan_cursor:
                break
            page += len(batch_pages)
    return {
        "title": title,
        "total_pages": total_pages,
        "candidates": candidates,
        "scanned": scanned,
        "scan_cursor": scan_cursor,
    }


def _limited_candidates(candidates, max_posts, max_chars, fallback_cursor=None):
    limited, truncated = _limit_posts(
        [post for post, _, _ in candidates], max_posts, max_chars
    )
    next_cursor = None
    if len(limited) < len(candidates):
        _, page, index = candidates[len(limited)]
        next_cursor = _encode_cursor(page, index)
    elif fallback_cursor:
        next_cursor = fallback_cursor
    return limited, truncated, next_cursor


async def _do_read_pages(
    topic_id,
    from_page,
    to_page,
    max_posts=DEFAULT_MAX_POSTS,
    max_chars=DEFAULT_MAX_CHARS,
):
    if from_page < 1:
        raise ValueError("from_page >=1")
    if to_page < from_page:
        raise ValueError("to_page >= from_page")
    if to_page - from_page + 1 > HARD_MAX_PAGES_PER_CALL:
        raise ValueError(
            f"Range too large ({to_page - from_page + 1} pages). Max {HARD_MAX_PAGES_PER_CALL} per call. Use scan_topic with cursor for full topic or chunk with from_page/to_page."
        )
    collected = await _collect_topic_posts(topic_id, from_page, to_page)
    candidates = collected["candidates"]
    limited, truncated, next_cursor = _limited_candidates(
        candidates, max_posts, max_chars, collected["scan_cursor"]
    )
    truncated = truncated or collected["scan_cursor"] is not None
    return {
        "topic_id": str(topic_id),
        "title": collected["title"],
        "from_page": from_page,
        "to_page": to_page,
        "total_posts_returned": len(limited),
        "total_posts_found": len(candidates),
        "total_posts_scanned": collected["scanned"],
        "truncated": truncated,
        "has_more": next_cursor is not None,
        "next_cursor": next_cursor,
        "posts": limited,
        "total_pages": collected["total_pages"],
    }


async def _do_read_last_pages(topic_id, count):
    count = max(1, min(count, HARD_MAX_PAGES_PER_CALL))
    info = await _do_thread_info(topic_id)
    total = info["total_pages"]
    frm = max(1, total - count + 1)
    res = await _do_read_pages(topic_id, frm, total)
    res["note"] = f"Fetched last {count} pages (pages {frm}-{total})"
    return res


async def _do_search_in_thread(
    topic_id,
    query,
    from_page=1,
    to_page=None,
    max_posts=DEFAULT_MAX_POSTS,
    max_chars=DEFAULT_MAX_CHARS,
):
    if not query or len(query) < 2:
        raise ValueError("Query >=2 chars")
    info = await _do_thread_info(topic_id)
    total = info["total_pages"]
    if to_page is None:
        to_page = total
    from_page = max(1, from_page)
    to_page = min(to_page, total)
    if to_page < from_page:
        raise ValueError("to_page >= from_page")
    if to_page - from_page + 1 > HARD_MAX_PAGES_PER_CALL:
        raise ValueError(
            f"Search range {to_page - from_page + 1} > {HARD_MAX_PAGES_PER_CALL}. Use scan_topic with query or narrow range / use cursor. For full scan call search with cursor pagination or use analyze_usage."
        )
    collected = await _collect_topic_posts(topic_id, from_page, to_page, query=query)
    matches = collected["candidates"]
    limited, trunc, next_cursor = _limited_candidates(
        matches, max_posts, max_chars, collected["scan_cursor"]
    )
    trunc = trunc or collected["scan_cursor"] is not None
    return {
        "topic_id": str(topic_id),
        "title": info["title"],
        "query": query,
        "from_page": from_page,
        "to_page": to_page,
        "total_scanned": collected["scanned"],
        "matches_count": len(matches),
        "truncated": trunc,
        "has_more": next_cursor is not None,
        "next_cursor": next_cursor,
        "matches": limited,
        "total_pages": total,
    }


# NEW: scan_topic with cursor pagination - the proper way to scan everything
async def _do_scan_topic(
    topic_id,
    cursor=None,
    limit_pages=10,
    max_posts=500,
    max_chars=DEFAULT_MAX_CHARS,
    query=None,
):
    """
    Cursor-based scan. limit_pages = how many forum pages per chunk (default 10, max 50).
    cursor = opaque token from previous call (encodes start page).
    Returns next_cursor if more data exists.
    If query provided, filters by substring (like search) but paginated.
    Handles header duplication: post #1 appears on every page, filter for start_page>1.
    """
    info = await _do_thread_info(topic_id)
    total = info["total_pages"]
    start_page, start_post = _decode_cursor(cursor)
    if start_page < 1 or start_page > total:
        raise ValueError(f"Cursor page {start_page} out of range 1-{total}")
    limit_pages = max(1, min(limit_pages, 50))
    end_page = min(start_page + limit_pages - 1, total)
    collected = await _collect_topic_posts(
        topic_id, start_page, end_page, start_post, query
    )
    posts = collected["candidates"]
    limited, trunc, next_cursor = _limited_candidates(
        posts, max_posts, max_chars, collected["scan_cursor"]
    )
    trunc = trunc or collected["scan_cursor"] is not None
    if next_cursor is None and end_page < total:
        next_cursor = _encode_cursor(end_page + 1)
    has_more = next_cursor is not None
    return {
        "topic_id": str(topic_id),
        "title": info["title"],
        "cursor": cursor,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "from_page": start_page,
        "to_page": end_page,
        "total_pages": total,
        "total_posts_found": len(posts),
        "total_posts_scanned": collected["scanned"],
        "total_posts_returned": len(limited),
        "truncated": trunc,
        "query": query,
        "posts": limited,
        "note": "Use next_cursor to fetch the next post position; cursors preserve unreturned posts within their page. Returned bodies marked body_truncated are partial and cannot be resumed. Loop until has_more=false. Header #1 filtered for start_page>1.",
    }


# Categories
ISSUE_CATEGORIES = {
    "Cooling and temperature": (
        "температур",
        "греет",
        "перегрев",
        "нагрев",
        "охлажд",
        "тротл",
        "градус",
    ),
    "Fan noise": ("шум", "вентилятор", "кулер", "воет", "шелест", "слышно", "тихий"),
    "Wi-Fi / Bluetooth": (
        "wifi",
        "wi-fi",
        "bluetooth",
        "bt ",
        "сеть",
        "отвалива",
        "не видит сети",
    ),
    "SSD / memory / power": (
        "ssd",
        "nvme",
        "память",
        "питание",
        "блок",
        "отрубает",
        "hdd",
        "m.2",
    ),
    "BIOS / firmware / drivers": (
        "bios",
        "биос",
        "прошив",
        "драйвер",
        "обнов",
        "прошивк",
    ),
    "Audio / HDMI / USB": ("звук", "hdmi", "usb", "динамик", "микрофон"),
    "Freezes / reboots / glitches": (
        "виснет",
        "зависа",
        "перезагруз",
        "глюк",
        "баг",
        "лага",
    ),
}

USAGE_CATEGORIES = {
    "Home Assistant / smart home": (
        "home assistant",
        "haos",
        "умный дом",
        "zigbee",
        "mqtt",
        "esphome",
        "дом ",
    ),
    "Proxmox / virtualization / Docker": (
        "proxmox",
        "qemu",
        "kvm",
        "lxc",
        "виртуал",
        "docker",
        "контейнер",
        "vm ",
    ),
    "NAS / storage / torrents": (
        "nas",
        "openmediavault",
        "truenas",
        "xpenology",
        "synology",
        "хранилищ",
        "samba",
        "торрент",
        "qbittorrent",
        "transmission",
        "torrserver",
        "hdd",
    ),
    "Media center / Plex / Kodi / YouTube 4K": (
        "kodi",
        "libreelec",
        "plex",
        "jellyfin",
        "медиа",
        "youtube",
        " 4k",
        "av1",
        "hdr",
    ),
    "24/7 server / networking / VPN / AdGuard": (
        "сервер",
        "24/7",
        "nextcloud",
        "wireguard",
        "vpn",
        "adguard",
        "pihole",
        "xray",
        "vless",
        "роутер",
        "openwrt",
        "pfsense",
    ),
    "Office / browser / work / 1C / AutoCAD": (
        "офис",
        "браузер",
        "word",
        "excel",
        "1с",
        "autocad",
        "удаленк",
        "офисн",
    ),
    "Games / emulators": ("игр", "steam", "эмулятор", "gta", "retro"),
}


async def _do_summarize_issues(
    topic_id, from_page=None, to_page=None, max_per_category=5
):
    info = await _do_thread_info(topic_id)
    total = info["total_pages"]
    to_page = min(to_page or total, total)
    from_page = max(1, from_page or max(1, to_page - 29))
    if to_page < from_page:
        raise ValueError("to_page >= from_page")
    if to_page - from_page + 1 > HARD_MAX_PAGES_PER_CALL:
        raise ValueError(
            f"Max {HARD_MAX_PAGES_PER_CALL} pages per call. Use smaller range or scan_topic iteratively."
        )
    result = await _do_read_pages(
        topic_id, from_page, to_page, max_posts=5000, max_chars=2000000
    )
    cats = {}
    for cat, terms in ISSUE_CATEGORIES.items():
        ms = [
            p
            for p in result["posts"]
            if any(
                t in (p.get("body", "") + " " + p.get("author", "")).lower()
                for t in terms
            )
        ]
        cats[cat] = {
            "mentions": len(ms),
            "examples": [
                {
                    "number": p["number"],
                    "date": p["date"],
                    "author": p["author"],
                    "body": p["body"][:700],
                }
                for p in ms[: max(1, min(max_per_category, 10))]
            ],
        }
    return {
        "topic_id": str(topic_id),
        "title": info["title"],
        "from_page": from_page,
        "to_page": to_page,
        "posts_scanned": len(result["posts"]),
        "total_posts_scanned": result["total_posts_scanned"],
        "truncated": result["truncated"],
        "has_more": result["has_more"],
        "next_cursor": result["next_cursor"],
        "categories": cats,
        "note": "Counts are keyword-based indicators over the returned posts; truncation means the category counts may be incomplete.",
    }


async def _do_analyze_usage(topic_id, from_page=None, to_page=None, max_examples=5):
    """
    Analyze what people do with device / for what tasks they use it.
    Scans range (default all pages chunked internally) and groups by USAGE_CATEGORIES.
    For full topic (>100 pages) it chunks internally in batches of 50 to avoid overload, but aggregates.
    """
    info = await _do_thread_info(topic_id)
    total = info["total_pages"]
    to_page = min(to_page or total, total)
    from_page = max(1, from_page or 1)
    if to_page < from_page:
        raise ValueError("to_page >= from_page")
    # Bound total retained posts, body chars, and pages even when callers request a whole long topic.
    all_posts = []
    used_chars = 0
    truncated = False
    next_cursor = None
    bounded_to = min(to_page, from_page + HARD_MAX_PAGES_PER_CALL - 1)
    # Chunked fetch remains bounded by the same public hard limits as read_pages.
    chunk_size = 50
    for chunk_start in range(from_page, bounded_to + 1, chunk_size):
        remaining_posts = HARD_MAX_POSTS - len(all_posts)
        remaining_chars = HARD_MAX_CHARS - used_chars
        if remaining_posts <= 0 or remaining_chars <= 0:
            truncated = True
            next_cursor = _encode_cursor(chunk_start)
            break
        chunk_end = min(chunk_start + chunk_size - 1, bounded_to)
        res = await _do_read_pages(
            topic_id,
            chunk_start,
            chunk_end,
            max_posts=remaining_posts,
            max_chars=remaining_chars,
        )
        all_posts.extend(res["posts"])
        used_chars += sum(len(post.get("body", "")) for post in res["posts"])
        if res["truncated"]:
            truncated = True
        if res["next_cursor"]:
            next_cursor = res["next_cursor"]
            break
        if chunk_end < to_page and P4DA_DELAY > 0:
            await asyncio.sleep(P4DA_DELAY)
    if bounded_to < to_page and next_cursor is None:
        truncated = True
        next_cursor = _encode_cursor(bounded_to + 1)
    # filter header
    all_posts = [p for p in all_posts if p["number"] != 1 or len(all_posts) < 5]
    all_posts.sort(key=lambda p: p["number"])
    categories = {}
    for cat, terms in USAGE_CATEGORIES.items():
        ms = [
            p for p in all_posts if any(t in p.get("body", "").lower() for t in terms)
        ]
        categories[cat] = {
            "mentions": len(ms),
            "examples": [
                {
                    "number": p["number"],
                    "date": p["date"],
                    "author": p["author"],
                    "page": p["page"],
                    "body": p["body"][:700].replace("\n", " | "),
                }
                for p in ms[: max(1, min(max_examples, 10))]
            ],
        }
    # summary stats
    total_mentions = sum(c["mentions"] for c in categories.values())
    return {
        "topic_id": str(topic_id),
        "title": info["title"],
        "from_page": from_page,
        "to_page": min(to_page, bounded_to),
        "posts_scanned": len(all_posts),
        "total_pages": total,
        "truncated": truncated,
        "has_more": next_cursor is not None,
        "next_cursor": next_cursor,
        "categories": categories,
        "total_mentions": total_mentions,
        "note": "Grouped by keyword matching over returned posts. Truncation means the category counts may be incomplete. For detailed posts use scan_topic with query filter.",
    }


@SERVER.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="thread_info",
            description="Get topic metadata: title, total pages, total posts (approx), breadcrumb, first post (header/shapka).",
            inputSchema={
                "type": "object",
                "properties": {
                    "topic_id": {
                        "type": "string",
                        "description": "4pda topic ID (number from showtopic=URL param)",
                    }
                },
                "required": ["topic_id"],
            },
        ),
        Tool(
            name="read_post",
            description="Read one exact 4PDA post from its share permalink or topic_id + post_id. The returned body includes text inside spoilers.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "4pda.to permalink containing showtopic and p, e.g. view=findpost&p=123",
                    },
                    "topic_id": {
                        "type": "string",
                        "description": "4PDA topic ID; use with post_id if url is omitted",
                    },
                    "post_id": {
                        "type": "string",
                        "description": "4PDA post ID; use with topic_id if url is omitted",
                    },
                },
                "anyOf": [{"required": ["url"]}, {"required": ["topic_id", "post_id"]}],
            },
        ),
        Tool(
            name="read_pages",
            description="Read a range of pages (max 100 pages/call). For full topic use scan_topic with cursor pagination. Returns posts with pagination hint next_cursor/has_more.",
            inputSchema={
                "type": "object",
                "properties": {
                    "topic_id": {"type": "string"},
                    "from_page": {
                        "type": "integer",
                        "description": "First page (1-based)",
                    },
                    "to_page": {
                        "type": "integer",
                        "description": "Last page inclusive",
                    },
                    "max_posts": {
                        "type": "integer",
                        "description": "Max posts returned (default 200, max 2000)",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Max body chars (default 120000, max 500000)",
                    },
                },
                "required": ["topic_id", "from_page", "to_page"],
            },
        ),
        Tool(
            name="read_last_pages",
            description="Read last N pages (max 100).",
            inputSchema={
                "type": "object",
                "properties": {
                    "topic_id": {"type": "string"},
                    "count": {
                        "type": "integer",
                        "description": "Number of last pages (default 5, max 100)",
                    },
                },
                "required": ["topic_id"],
            },
        ),
        Tool(
            name="search_in_thread",
            description="Search posts by substring (max 100 pages/call). For full topic search use scan_topic with query+cursor.",
            inputSchema={
                "type": "object",
                "properties": {
                    "topic_id": {"type": "string"},
                    "query": {
                        "type": "string",
                        "description": "Search string >=2 chars",
                    },
                    "from_page": {
                        "type": "integer",
                        "description": "First page (default 1)",
                    },
                    "to_page": {
                        "type": "integer",
                        "description": "Last page (default last)",
                    },
                    "max_posts": {
                        "type": "integer",
                        "description": "Max matches returned (default 200, max 2000)",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Max body chars (default 120000)",
                    },
                },
                "required": ["topic_id", "query"],
            },
        ),
        Tool(
            name="summarize_issues",
            description="Group likely device complaints (mini-PC tuned categories: cooling, noise, Wi-Fi/BT, SSD/power, BIOS, audio/HDMI, freezes). Max 100 pages/call.",
            inputSchema={
                "type": "object",
                "properties": {
                    "topic_id": {"type": "string"},
                    "from_page": {
                        "type": "integer",
                        "description": "First page (default last 30)",
                    },
                    "to_page": {
                        "type": "integer",
                        "description": "Last page (default last)",
                    },
                    "max_per_category": {
                        "type": "integer",
                        "description": "Examples per category max 10 (default 5)",
                    },
                },
                "required": ["topic_id"],
            },
        ),
        Tool(
            name="scan_topic",
            description="Cursor-paginated full-topic scanner (MCP pagination pattern). Returns limit_pages per chunk with opaque next_cursor. Use cursor from previous response to get next chunk. Supports optional query filter. Loop until has_more=false to get all posts without context blow-up. This is the correct way to scan 100+ page topics.",
            inputSchema={
                "type": "object",
                "properties": {
                    "topic_id": {"type": "string", "description": "4pda topic ID"},
                    "cursor": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "description": "Opaque cursor from previous scan_topic response (base64 page). Omit or null for first page.",
                    },
                    "limit_pages": {
                        "type": "integer",
                        "description": "Forum pages per chunk (default 10, max 50). 10=~200 posts.",
                    },
                    "max_posts": {
                        "type": "integer",
                        "description": "Max posts in this chunk (default 200, max 2000)",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Max chars in this chunk (default 120000, max 500000)",
                    },
                    "query": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "description": "Optional substring filter (case-insensitive) applied per chunk",
                    },
                },
                "required": ["topic_id"],
            },
        ),
        Tool(
            name="analyze_usage",
            description="Analyze how people use the device and what tasks they use it for. Groups posts by usage categories (Home Assistant, Proxmox, NAS, media, servers, office, games). Auto-chunks large topics (>50 pages).",
            inputSchema={
                "type": "object",
                "properties": {
                    "topic_id": {"type": "string"},
                    "from_page": {
                        "type": "integer",
                        "description": "First page (default 1)",
                    },
                    "to_page": {
                        "type": "integer",
                        "description": "Last page (default last)",
                    },
                    "max_examples": {
                        "type": "integer",
                        "description": "Examples per category (default 5, max 10)",
                    },
                },
                "required": ["topic_id"],
            },
        ),
    ]


@SERVER.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    try:
        if name == "thread_info":
            result = await _do_thread_info(str(arguments["topic_id"]))
        elif name == "read_post":
            result = await _do_read_post(
                arguments.get("url"),
                arguments.get("topic_id"),
                arguments.get("post_id"),
            )
        elif name == "read_pages":
            result = await _do_read_pages(
                str(arguments["topic_id"]),
                int(arguments["from_page"]),
                int(arguments["to_page"]),
                int(arguments.get("max_posts", DEFAULT_MAX_POSTS)),
                int(arguments.get("max_chars", DEFAULT_MAX_CHARS)),
            )
        elif name == "read_last_pages":
            result = await _do_read_last_pages(
                str(arguments["topic_id"]), int(arguments.get("count", 5))
            )
        elif name == "search_in_thread":
            result = await _do_search_in_thread(
                str(arguments["topic_id"]),
                str(arguments["query"]),
                int(arguments.get("from_page", 1)),
                int(arguments["to_page"])
                if arguments.get("to_page") is not None
                else None,
                int(arguments.get("max_posts", DEFAULT_MAX_POSTS)),
                int(arguments.get("max_chars", DEFAULT_MAX_CHARS)),
            )
        elif name == "summarize_issues":
            result = await _do_summarize_issues(
                str(arguments["topic_id"]),
                int(arguments["from_page"])
                if arguments.get("from_page") is not None
                else None,
                int(arguments.get("to_page"))
                if arguments.get("to_page") is not None
                else None,
                int(arguments.get("max_per_category", 5)),
            )
        elif name == "scan_topic":
            # default max_posts for scan is 500 (10 pages ~200 posts) to avoid truncation on header duplication
            result = await _do_scan_topic(
                str(arguments["topic_id"]),
                arguments.get("cursor"),
                int(arguments.get("limit_pages", 10)),
                int(arguments.get("max_posts", 500)),
                int(arguments.get("max_chars", DEFAULT_MAX_CHARS)),
                arguments.get("query"),
            )
        elif name == "analyze_usage":
            result = await _do_analyze_usage(
                str(arguments["topic_id"]),
                int(arguments["from_page"])
                if arguments.get("from_page") is not None
                else None,
                int(arguments.get("to_page"))
                if arguments.get("to_page") is not None
                else None,
                int(arguments.get("max_examples", 5)),
            )
        else:
            raise ValueError(f"Unknown tool: {name}")
        summary = _make_summary(name, result)
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {"summary": summary, "data": result}, ensure_ascii=False, indent=2
                ),
            )
        ]
    except Exception as exc:  # noqa: BLE001 -- turn tool failures into MCP error responses.
        return [
            TextContent(
                type="text", text=json.dumps({"error": str(exc)}, ensure_ascii=False)
            )
        ]


def _make_summary(tool, data):
    if tool == "thread_info":
        return f"[4pda] {data.get('title', '')} — {data.get('total_pages', '?')} pages, ~{data.get('total_posts_approx', '?')} posts"
    elif tool == "read_post":
        post = data.get("post", {})
        return f"[4pda] post #{post.get('number', '?')} by {post.get('author', '?')} ({post.get('post_id', '?')})"
    elif tool == "read_pages":
        return f"[4pda] pages {data.get('from_page', '?')}–{data.get('to_page', '?')}: {data.get('total_posts_returned', 0)} posts (has_more={data.get('has_more', False)})"
    elif tool == "read_last_pages":
        return f"[4pda] {data.get('note', '')} , {data.get('total_posts_returned', 0)} posts"
    elif tool == "search_in_thread":
        return f"[4pda] searched for '{data.get('query', '')}' : {data.get('matches_count', 0)}/{data.get('total_scanned', 0)} matched"
    elif tool == "summarize_issues":
        return f"[4pda] issues {data.get('from_page', '?')}-{data.get('to_page', '?')}: {data.get('posts_scanned', 0)} posts scanned"
    elif tool == "scan_topic":
        return f"[4pda] scan {data.get('from_page', '?')}-{data.get('to_page', '?')}/{data.get('total_pages', '?')} has_more={data.get('has_more')} next_cursor={data.get('next_cursor')}"
    elif tool == "analyze_usage":
        return f"[4pda] usage analysis {data.get('from_page', '?')}-{data.get('to_page', '?')}: {data.get('posts_scanned', 0)} posts, {data.get('total_mentions', 0)} mentions"
    return "[4pda]"


async def main():
    async with stdio_server() as (rs, ws):
        await SERVER.run(rs, ws, SERVER.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
