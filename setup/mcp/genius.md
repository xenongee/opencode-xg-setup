# Own Genius MCP

This is a standalone stdio MCP source for Genius search, lyrics, annotations,
and combined lyrics/footnotes. The disabled-by-default V2 declaration is maintained
in the `x-genius` entry of `packages.json`; Workkit recipes prepare its isolated
Python environment and sync stages the script. Neither proves runtime activation.

## Provenance

The implementation is adapted from the early pre-v2 implementation
`mcp/brew/genius.py`, historically attributed to xenongee. The repository's
current license is Unlicense. The provenance of individual copied fragments was
not independently established; this note does not make a separate authorship or
license claim for those fragments. The adapter is kept separate from upstream
resources.

## Tools and inputs

- `genius_search(query, per_page=5)` searches Genius songs; `per_page` is clamped
  to 1–20.
- `genius_lyrics(ref)`, `genius_annotations(ref)`, and `genius_full(ref)` accept a
  decimal song ID, a page slug, or an HTTPS song URL on `genius.com` or
  `www.genius.com`. Credentials, explicit ports, and non-Genius origins are
  rejected. Query strings and fragments on a Genius ref are discarded while the
  checked page path is resolved.
- Song/API results are cached in-process for 600 seconds by default, with at most
  128 LRU entries. `refresh=true` bypasses the cache for that tool call.
- Output is capped at 80,000 characters by default. The timeout, cache TTL, output
  cap, and browser-like User-Agent can be set with `GENIUS_TIMEOUT`,
  `GENIUS_CACHE_TTL`, `GENIUS_MAX_CHARS`, and `GENIUS_USER_AGENT` respectively.

The tools call Genius's unofficial site API without an API token; Genius receives
search queries and requested song references. Site behavior, availability, terms,
and rate limits may change.

The server does not configure the root logging level. It stops `httpx` and
`httpcore` log records from propagating to root handlers, preventing their
URL-bearing request records from reaching those handlers even at DEBUG level.
Handlers separately attached to these library loggers can still receive records;
request exceptions and non-200 responses can also reach MCP tool error text.

## Network boundary

Every requested URL and every redirect target is checked for HTTPS and an exact
Genius hostname; userinfo, explicit ports, literal IP/private hosts, and other
origins are rejected. Automatic redirects are disabled; the client manually
follows only 301/302/303/307/308 responses between `genius.com` and
`www.genius.com`, allows at most five hops, and rejects loops. Request `params`
(including search terms) are sent only on the initial request, never replayed on
the redirect target. Other non-200 responses fail. Search-result links that fail
the same URL check are omitted.

This is a URL/origin check, **not** a defense against DNS rebinding: a Genius
hostname resolving to a local or otherwise forbidden IP is not detected or pinned
at connection time. Do not claim that this adapter prevents DNS rebinding or is a
complete SSRF boundary. Enforcing address policy requires connection-time DNS/IP
validation for every hop.

## Runtime status and blockers

Requires Python 3.12+. Dependency preparation belongs to the install/update recipes
in the `x-genius` catalog entry; sync does not install dependencies. The source
has no package-runner/self-install path, so missing prepared dependencies remain a
setup blocker. Runtime activation and other operating systems are unverified. Offline
unit tests use local fixtures and do not contact Genius; no network self-test is provided.
