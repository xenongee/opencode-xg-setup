# 4PDA forum MCP

This is a repository-owned, optional stdio MCP source. It preserves the eight
archived tools: `thread_info`, `read_post`, `read_pages`, `read_last_pages`,
`search_in_thread`, `scan_topic`, `summarize_issues`, and `analyze_usage`, including
their page/post/character bounds and resumable cursors.
The V2 declaration is maintained in the `x-4pda` entry of `packages.json` and is
disabled by default. Workkit's install/update recipes prepare its isolated Python
environment; sync stages the script at a stable data-root path and never installs
dependencies. This does not prove runtime activation or compatibility on other OSes.

## Provenance and license status

The implementation is adapted from the early pre-v2 implementation
`mcp/brew/4pda.py`, historically attributed to xenongee. The repository's current license is the
Unlicense, but the provenance of individual code fragments has not been established
independently. The repository license and historical attribution do not resolve
that provenance uncertainty; retain it as a review blocker before making broader
distribution or ownership claims. No other archived policies are imported here.

The archived script declared `mcp>=1.6.0,<2.0.0`, `httpx>=0.27.0`, and
`beautifulsoup4>=4.12`. The Workkit recipe owns dependency preparation; the CLI itself
does not require these packages. This source has no self-install path and is launched
through the prepared environment declared by the catalog entry.

## Network and credential boundary

`P4DA_BASE_URL` is accepted only as a root HTTPS URL for exactly `4pda.to` or
`www.4pda.to`; userinfo, explicit ports, paths, queries, fragments, and other
origins are rejected. HTTPX automatic redirects are disabled for every request.
`read_post` opts into bounded manual handling of 301/302/303/307/308 responses:
relative targets are resolved against the current URL, then checked before the next
request to require HTTPS and the exact configured host. At most five hops are
followed; loops, missing locations, overlong chains, and other origins fail closed.
Other tools do not follow redirects. A configured `P4DA_COOKIE` is attached only to
requests made to the selected configured origin. In the MCP process, propagation is
disabled for the `httpx` and `httpcore` loggers and each receives a `NullHandler`, so
their request URLs/queries and headers cannot reach root logging handlers. The root
logger is not changed; tool failures remain visible in MCP error responses.

Topic IDs must be positive integers or nonempty ASCII-decimal strings containing at
least one nonzero digit. Validation happens before constructing a request URL; invalid
values are rejected with a fixed message that does not echo the supplied value.

This URL/origin validation is **not** a DNS-rebinding sandbox and does not pin DNS
answers or prevent an approved hostname from resolving unexpectedly. Egress
filtering or a transport that pins and revalidates resolved addresses is still
needed for that stronger SSRF boundary. No live HTTP or cookie test was performed.

Runtime activation and compatibility on other operating systems remain unverified.
Do not infer installation or runtime compatibility from the lock check or offline
regression tests.
