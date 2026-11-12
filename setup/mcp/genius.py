"""MCP server for genius.com: songs, lyrics and annotations (referents).

Uses the unofficial Genius web API (genius.com/api/*) that the site frontend
itself uses - no API token required, but browser-like headers are mandatory
(short curl UAs get 403).

Tools:
  genius_search      search songs by query
  genius_lyrics      lyrics text of a song
  genius_annotations annotated fragments + annotation bodies
  genius_full        lyrics with inline [^N] markers + footnotes section

Input for song tools (`ref`): song_id (digits), genius.com URL or page slug
(e.g. "Dystopia-now-and-forever-lyrics").

Runtime dependencies (must already be installed; this script does not install):
  mcp>=1.6,<2, httpx>=0.27, beautifulsoup4>=4.12

Environment variables:
  GENIUS_TIMEOUT (30s), GENIUS_CACHE_TTL (600s), GENIUS_MAX_CHARS (80000),
  GENIUS_USER_AGENT

Security: requests are restricted to HTTPS Genius page origins. Redirects are
followed manually only between allowlisted Genius hosts, with a hop limit and
loop detection. Hostname validation alone does not prevent DNS rebinding;
preventing that requires checking/pinning the resolved address at connection
time, which this script does not do.
"""
from __future__ import annotations

import html as _html
import logging
import os
import re
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP


def _isolate_http_library_logs() -> None:
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        if not any(isinstance(handler, logging.NullHandler) for handler in logger.handlers):
            logger.addHandler(logging.NullHandler())
        logger.propagate = False


_isolate_http_library_logs()

mcp = FastMCP("genius")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GENIUS_TIMEOUT = int(os.environ.get("GENIUS_TIMEOUT", "30"))
CACHE_TTL = int(os.environ.get("GENIUS_CACHE_TTL", "600"))
MAX_CHARS = int(os.environ.get("GENIUS_MAX_CHARS", "80000"))
USER_AGENT = os.environ.get(
    "GENIUS_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
)
BASE = "https://genius.com"
HARD_MAX_PER_PAGE = 20
MAX_REDIRECTS = 5
MAX_BODY_BYTES = 4 * 1024 * 1024
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_ALLOWED_HOSTS = frozenset({"genius.com", "www.genius.com"})
_SLUG_RE = re.compile(r"[A-Za-z0-9._%+~=-]+(?:-[A-Za-z0-9._%+~=-]+)*\Z")

_cache: OrderedDict[tuple, tuple[float, Any]] = OrderedDict()
_client: httpx.AsyncClient | None = None


def _headers() -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, text/html",
        "Accept-Language": "en-US,en;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://genius.com/",
    }


async def _client_get(url: str, params: dict[str, Any] | None = None) -> httpx.Response:
    """Fetch an allowlisted Genius URL and return a fully buffered response.

    The body is streamed through a hard byte cap before decode/parse/cache, and
    the stream is closed on every path, including redirects and errors.
    """
    global _client
    current = urldefrag(_validate_genius_url(url)).url
    if _client is None:
        _client = httpx.AsyncClient(headers=_headers(), timeout=GENIUS_TIMEOUT,
                                    follow_redirects=False)
    visited: set[str] = set()
    request_params = params
    for redirects_followed in range(MAX_REDIRECTS + 1):
        parts = urlsplit(current)
        loop_key = f"{parts.hostname.lower()}{parts.path or '/'}?{parts.query}"
        if loop_key in visited:
            raise RuntimeError("Genius redirect loop detected")
        visited.add(loop_key)

        async with _client.stream("GET", current, params=request_params) as resp:
            if resp.status_code == 200:
                body = await _read_body(resp)
                return httpx.Response(200, headers=_plain_headers(resp),
                                      content=body, request=resp.request)
            if resp.status_code not in _REDIRECT_STATUSES:
                raise RuntimeError(f"genius.com returned {resp.status_code} for {current}")
            if redirects_followed == MAX_REDIRECTS:
                raise RuntimeError(f"genius.com exceeded the {MAX_REDIRECTS}-redirect limit")
            location = resp.headers.get("location")
            if not location:
                raise RuntimeError(f"genius.com returned {resp.status_code} without a Location")
        current = urldefrag(_validate_genius_url(urljoin(current, location))).url
        request_params = None
    raise RuntimeError("Genius redirect limit reached")


def _plain_headers(response: httpx.Response) -> dict[str, str]:
    # Drop framing/encoding headers: the buffered content is already decoded.
    return {key: value for key, value in response.headers.items()
            if key.lower() not in ("content-encoding", "content-length")}


async def _read_body(resp: httpx.Response) -> bytes:
    """Return the decompressed body, aborting past MAX_BODY_BYTES.

    aiter_bytes() decodes content-encoding, so the cap bounds what the JSON and
    HTML parsers actually see, including chunked bodies without Content-Length.
    """
    body = bytearray()
    async for chunk in resp.aiter_bytes():
        # Check before copying so an oversized chunk is never buffered.
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise RuntimeError(
                f"genius.com response body exceeds the {MAX_BODY_BYTES}-byte limit"
            )
        body.extend(chunk)
    return bytes(body)


def _cache_get(key: tuple) -> Any | None:
    item = _cache.get(key)
    if item is None:
        return None
    ts, val = item
    if time.time() - ts > CACHE_TTL:
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)
    return val


def _cache_put(key: tuple, val: Any) -> None:
    _cache[key] = (time.time(), val)
    _cache.move_to_end(key)
    while len(_cache) > 128:
        _cache.popitem(last=False)


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def _validate_genius_url(url: str) -> str:
    """Accept only HTTPS Genius URLs, never arbitrary origins or explicit ports."""
    if not isinstance(url, str) or not url or url != url.strip():
        raise ValueError("Expected an HTTPS Genius URL")
    if any(ch.isspace() or ord(ch) < 32 or ch == "\\" for ch in url):
        raise ValueError("Invalid characters in Genius URL")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        # A colon in the authority rejects every explicit port, including :443.
        if (parts.scheme.lower() != "https" or host is None
                or host.lower() not in _ALLOWED_HOSTS
                or "@" in parts.netloc or ":" in parts.netloc):
            raise ValueError("Only HTTPS Genius URLs without credentials or ports are allowed")
    except ValueError as exc:
        if str(exc).startswith(("Only HTTPS", "Explicit URL")):
            raise
        raise ValueError("Invalid Genius URL") from exc
    return url

async def _api_json(path: str, params: dict[str, Any] | None = None,
                    refresh: bool = False) -> dict:
    key = ("api", path, tuple(sorted((params or {}).items())))
    if not refresh:
        cached = _cache_get(key)
        if cached is not None:
            return cached
    data = (await _client_get(BASE + path, params)).json()
    _cache_put(key, data)
    return data


async def _page_html(url: str, refresh: bool = False) -> str:
    key = ("page", url)
    if not refresh:
        cached = _cache_get(key)
        if cached is not None:
            return cached
    resp = await _client_get(url)
    _cache_put(key, resp.text)
    return resp.text


def _cap(text: str) -> str:
    if len(text) <= MAX_CHARS:
        return text
    return text[:MAX_CHARS] + f"\n\n[truncated: {len(text)} > {MAX_CHARS} characters; increase GENIUS_MAX_CHARS]"


def _slug_from_ref(ref: str) -> str | None:
    ref = ref.strip()
    if re.match(r"https?://", ref, re.IGNORECASE):
        try:
            _validate_genius_url(ref)
        except ValueError:
            return None
        path = urlsplit(ref).path
        if path.startswith("/"):
            slug = path[1:-1] if path.endswith("/") else path[1:]
            if _SLUG_RE.fullmatch(slug):
                return slug
        return None
    if re.fullmatch(r"\d+", ref):
        return None
    if _SLUG_RE.fullmatch(ref):
        return ref
    return None


async def _song_json(song_id: int, refresh: bool = False) -> dict:
    data = await _api_json(f"/api/songs/{song_id}", refresh=refresh)
    song = data["response"]["song"]
    _validate_genius_url(song.get("url"))
    return song


async def _resolve_song_id(ref: str, refresh: bool = False) -> int:
    ref = (ref or "").strip()
    if not ref:
        raise RuntimeError("Empty ref; provide a song_id, genius.com URL, or page slug")
    if re.fullmatch(r"\d+", ref):
        return int(ref)
    slug = _slug_from_ref(ref)
    if slug is None:
        raise RuntimeError(f"Unrecognized ref: {ref!r}; expected a song_id, genius.com URL, "
                           "or slug (Title-artist-lyrics). Use genius_search for a text query")
    html = await _page_html(f"{BASE}/{slug}", refresh=refresh)
    m = re.search(r"genius://songs/(\d+)", html)
    if not m:
        raise RuntimeError(f"Could not find a song_id on page {slug}; the page may not exist")
    return int(m.group(1))


def _dom_text(node: Any) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if isinstance(node.get("data"), str):
            return node["data"]
        parts = []
        for child in node.get("children") or []:
            parts.append(_dom_text(child))
            if isinstance(child, dict) and child.get("tag") in ("p", "br", "div"):
                parts.append("\n")
        return "".join(parts)
    if isinstance(node, list):
        return "".join(_dom_text(c) for c in node)
    return ""


def _song_description(song: dict) -> str:
    d = song.get("description")
    if isinstance(d, dict) and "dom" in d:
        text = _dom_text(d["dom"])
    elif isinstance(d, str):
        text = _html.unescape(d)
    else:
        text = ""
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def _extract_lyrics(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    containers = soup.find_all(attrs={"data-lyrics-container": "true"})
    if not containers:
        return None
    parts = []
    for div in containers:
        for br in div.find_all("br"):
            br.replace_with("\n")
        for block in div.find_all(["div", "p"]):
            block.append("\n")
        parts.append(div.get_text())
    text = "\n".join(parts)
    text = re.sub(r"^\s*\d+\s*Contributors?\s*\n[^\n]*Lyrics[^\n]*\n?", "",
                  text, count=1, flags=re.IGNORECASE)
    text = re.sub(r"^\s*\d+\s*Contributors?[^\n]*Lyrics[^\n]*\n?", "",
                  text, count=1, flags=re.IGNORECASE)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() or None


def _song_header(song: dict) -> str:
    lines = [f"# {song.get('full_title') or song.get('title')}"]
    meta = []
    if song.get("album"):
        meta.append(f"Album: {song['album'].get('name')}")
    if song.get("release_date"):
        meta.append(f"Released: {song['release_date']}")
    for key, label in (("producer_artists", "Producer"), ("writer_artists", "Writers")):
        ppl = [a.get("name") for a in (song.get(key) or []) if a.get("name")]
        if ppl:
            meta.append(f"{label}: {', '.join(ppl)}")
    if song.get("url"):
        meta.append(_validate_genius_url(song["url"]))
    if meta:
        lines.append(" · ".join(meta))
    return "\n".join(lines)


def _norm_map(text: str) -> tuple[str, list[int]]:
    out: list[str] = []
    idx: list[int] = []
    prev_space = True
    for i, ch in enumerate(text):
        if ch.isspace():
            if prev_space:
                continue
            out.append(" ")
            idx.append(i)
            prev_space = True
        else:
            out.append(ch)
            idx.append(i)
            prev_space = False
    return "".join(out), idx


async def _referents(song_id: int, refresh: bool = False) -> list[dict]:
    data = _cache_get(("refs", song_id)) if not refresh else None
    if data is None:
        data = (await _api_json("/api/referents",
                                {"song_id": song_id, "text_format": "plain"},
                                refresh=refresh))["response"]["referents"]
        _cache_put(("refs", song_id), data)
    return data


def _annotation_body(annotations: list[dict]) -> str | None:
    for ann in annotations:
        if ann.get("deleted"):
            continue
        body = ann.get("body") or {}
        text = body.get("plain") or ""
        text = _html.unescape(text).strip()
        if text:
            return text
    return None


def _build_full(lyrics: str, referents: list[dict]) -> str:
    matches: list[tuple[int, int]] = []  # (insert_pos, n)
    footnotes: list[str] = []
    unmatched: list[int] = []
    norm, idx = _norm_map(lyrics)
    used_positions: set[int] = set()
    n = 0
    for ref in referents:
        n += 1
        frag = (ref.get("fragment") or "").strip()
        body = _annotation_body(ref.get("annotations") or [])
        label = f'"{frag}"' if frag else '"fragment"'
        footnotes.append(f"[^{n}] {label} — {body or '(no annotation text)'}")
        fnorm, _ = _norm_map(frag)
        if not fnorm:
            unmatched.append(n)
            continue
        start = 0
        placed = False
        while True:
            pos = norm.find(fnorm, start)
            if pos == -1:
                break
            if pos not in used_positions:
                used_positions.add(pos)
                insert_pos = idx[pos + len(fnorm) - 1] + 1
                matches.append((insert_pos, n))
                placed = True
                break
            start = pos + 1
        if not placed:
            unmatched.append(n)
    for insert_pos, n in sorted(matches, key=lambda m: -m[0]):
        lyrics = lyrics[:insert_pos] + f" [^{n}]" + lyrics[insert_pos:]
    out = [lyrics, "", "---", "Footnotes:"]
    if footnotes:
        out.extend(footnotes)
    else:
        out.append("(This song has no annotations.)")
    if unmatched:
        out.append("")
        out.append(f"(Footnotes {', '.join(f'[^{n}]' for n in unmatched)}: "
                   "fragment not found in lyrics; Genius links it outside the scraped section.)")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Core functions (shared by MCP tools)
# ---------------------------------------------------------------------------

async def _core_search(query: str, per_page: int = 5) -> str:
    query = (query or "").strip()
    if not query:
        raise RuntimeError("Search query is empty")
    per_page = max(1, min(int(per_page or 5), HARD_MAX_PER_PAGE))
    data = await _api_json("/api/search", {"q": query})
    hits = [h for h in data["response"]["hits"] if h.get("index") == "song"]
    if not hits:
        return f"No songs found for query {query!r}"
    hits = hits[:per_page]
    lines = [f"Found {len(hits)} songs:"]
    for h in hits:
        r = h["result"]
        stats = r.get("stats") or {}
        lines.append(
            f"- [{r['id']}] {r.get('full_title')}"
            f" — annotations: {r.get('annotation_count', '?')},"
            f" pageviews: {stats.get('pageviews', '?')}"
        )
        if r.get("url"):
            try:
                lines.append(f"  {_validate_genius_url(r['url'])}")
            except ValueError:
                pass
    lines.append("\nFor lyrics/footnotes, use genius_lyrics / genius_annotations / genius_full with a song_id or URL")
    return _cap("\n".join(lines))


async def _core_lyrics(ref: str, refresh: bool = False) -> str:
    song_id = await _resolve_song_id(ref, refresh=refresh)
    song = await _song_json(song_id, refresh=refresh)
    head = _song_header(song)
    if song.get("instrumental"):
        desc = _song_description(song)
        body = "Instrumental (no lyrics).\n\nDescription:\n" + (desc or "(no description)")
        return _cap(head + "\n\n" + body)
    html = await _page_html(song["url"], refresh=refresh)
    lyrics = _extract_lyrics(html)
    if not lyrics:
        desc = _song_description(song)
        body = ("Lyrics have not been added to Genius yet.\n\nSong description:\n"
                + (desc or "(no description)"))
        return _cap(head + "\n\n" + body)
    return _cap(head + "\n\n" + lyrics)


async def _core_annotations(ref: str, refresh: bool = False) -> str:
    song_id = await _resolve_song_id(ref, refresh=refresh)
    song = await _song_json(song_id, refresh=refresh)
    referents = await _referents(song_id, refresh=refresh)
    head = _song_header(song)
    if not referents:
        return _cap(head + "\n\nThis song has no annotations.")
    lines = [head, "", f"Annotated fragments: {len(referents)}", ""]
    for i, ref_ in enumerate(referents, 1):
        frag = (ref_.get("fragment") or "").strip()
        body = _annotation_body(ref_.get("annotations") or [])
        ann = next((a for a in (ref_.get("annotations") or []) if not a.get("deleted")), {})
        verified = "verified: yes" if ann.get("verified") else "verified: no"
        votes = ann.get("votes_total")
        lines.append(f'[{i}] "{frag}" ({verified}, votes: {votes if votes is not None else "?"})')
        lines.append(f"    {body or '(no annotation text)'}")
        lines.append("")
    return _cap("\n".join(lines).rstrip())


async def _core_full(ref: str, refresh: bool = False) -> str:
    song_id = await _resolve_song_id(ref, refresh=refresh)
    song = await _song_json(song_id, refresh=refresh)
    head = _song_header(song)
    if song.get("instrumental"):
        desc = _song_description(song)
        return _cap(head + "\n\nInstrumental (no lyrics).\n\nDescription:\n" + (desc or "(no description)"))
    html = await _page_html(song["url"], refresh=refresh)
    lyrics = _extract_lyrics(html)
    if not lyrics:
        desc = _song_description(song)
        return _cap(head + "\n\nLyrics have not been added to Genius yet.\n\nDescription:\n" + (desc or "(no description)"))
    referents = await _referents(song_id, refresh=refresh)
    body = _build_full(lyrics, referents) if referents else lyrics
    return _cap(head + "\n\n" + body)


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def genius_search(query: str, per_page: int = 5) -> str:
    """Search genius.com for songs. Returns song_id, full title, artist,
    annotation count, pageviews and page URL per hit.

    Args:
        query: search query, e.g. "Dystopia Now and Forever" or "dystopia quagmire"
        per_page: number of hits to return (1-20, default 5)
    """
    try:
        return await _core_search(query, per_page)
    except Exception as e:  # noqa: BLE001 - keep MCP failures visible to the caller
        return f"Error: {e}"


@mcp.tool()
async def genius_lyrics(ref: str, refresh: bool = False) -> str:
    """Get song lyrics text from genius.com. `ref` = song_id, genius.com URL
    or page slug (e.g. "Dystopia-now-and-forever-lyrics"). For instrumentals
    returns the song description instead.

    Args:
        ref: song_id, genius.com URL or slug
        refresh: bypass cache (TTL 600s)
    """
    try:
        return await _core_lyrics(ref, refresh=refresh)
    except Exception as e:  # noqa: BLE001 - keep MCP failures visible to the caller
        return f"Error: {e}"


@mcp.tool()
async def genius_annotations(ref: str, refresh: bool = False) -> str:
    """Get all annotations (footnotes) of a song from genius.com: each annotated
    fragment with its annotation body (plain text) and verification status.

    Args:
        ref: song_id, genius.com URL or slug
        refresh: bypass cache (TTL 600s)
    """
    try:
        return await _core_annotations(ref, refresh=refresh)
    except Exception as e:  # noqa: BLE001 - keep MCP failures visible to the caller
        return f"Error: {e}"


@mcp.tool()
async def genius_full(ref: str, refresh: bool = False) -> str:
    """Get song lyrics WITH annotations: inline [^N] markers after annotated
    fragments + "Footnotes" section at the end linking every [^N] to its fragment
    quote and annotation body. Best single tool for reading a song.

    Args:
        ref: song_id, genius.com URL or slug
        refresh: bypass cache (TTL 600s)
    """
    try:
        return await _core_full(ref, refresh=refresh)
    except Exception as e:  # noqa: BLE001 - keep MCP failures visible to the caller
        return f"Error: {e}"


if __name__ == "__main__":
    mcp.run()
