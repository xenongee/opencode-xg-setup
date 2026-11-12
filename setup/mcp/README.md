# Optional MCP declarations

The optional MCP declarations live in `packages.json`; this document records their
provenance and operational risks. They are catalog entries, not installed servers,
and disabled-by-default declarations do not prove runtime activation. No source,
executable, license file, or third-party package is bundled here.

V2 substitutes `{env:NAME}` in strings, replacing missing variables with an empty
string. Before enabling, validate every required variable without printing it;
for local entries require an absolute, preinstalled, verified executable for the
actual backend OS, including a verified native Windows launch strategy. Never
resolve a missing executable by silently invoking `npx`, `uvx`, a shell, or an
installer. Versions, dependency versions, checksums, and platform support remain
unresolved for some entries; the own-MCP preparation is described below.
The installer must treat unresolved requirements as blockers rather than running
these examples as provisioning recipes.

| Entry | Source and license information | Preconditions and risks |
|---|---|---|
| `exa` | [Exa MCP](https://github.com/exa-labs/exa-mcp-server), [MIT](https://github.com/exa-labs/exa-mcp-server/blob/main/LICENSE), [authentication and tools](https://exa.ai/docs/get-started/exa-mcp) | `EXA_API_KEY`; the explicit tool list excludes the separately billed agent. Remote calls disclose queries; network/region access and billing vary. |
| `tavily` | [Tavily MCP](https://github.com/tavily-ai/tavily-mcp), [MIT at commit 3be0c424](https://github.com/tavily-ai/tavily-mcp/blob/3be0c424ed2e9132e3830aa6a8a41ba96becd6d6/LICENCE), [docs](https://docs.tavily.com/documentation/mcp); hosted revision unknown | `TAVILY_API_KEY`; remote search/crawl/research can incur costs and disclose inputs. |
| `huggingface_remote` | [Hugging Face official MCP](https://github.com/huggingface/hf-mcp-server), [MIT](https://github.com/huggingface/hf-mcp-server/blob/main/LICENSE) | `HF_TOKEN`; this is **not** the legacy local read-only Python server. The hosted server may offer write, job, sandbox, or Space tools depending on account settings; review permissions and token scope before use. |
| `duckduckgo` | [duckduckgo-mcp-server](https://github.com/nickclyde/duckduckgo-mcp-server), [MIT](https://github.com/nickclyde/duckduckgo-mcp-server/blob/main/LICENSE) | `WORKKIT_DDG_MCP_EXECUTABLE` must point to a separately prepared compatible stdio executable. [v0.7.0](https://github.com/nickclyde/duckduckgo-mcp-server/blob/8182af3f2295017ca90527a924f48147fbf18d5b/pyproject.toml) requires MCP >=2.1.1,<3. Select and verify a release, dependencies and private-URL protections. Upstream notes a DNS rebinding race; its URL guard is not a security boundary. Search/fetch use the network. |
| `semgrep` | [Semgrep](https://github.com/semgrep/semgrep), [LGPL-2.1-or-later for v1.183.0](https://github.com/semgrep/semgrep/blob/4fe07d805dd2a4b64fed486e95136923f08648d0/cli/pyproject.toml), [MCP documentation](https://github.com/semgrep/semgrep/tree/develop/cli/src/semgrep/mcp) | `WORKKIT_SEMGREP_EXECUTABLE` must be a separately prepared build with `mcp` support. v1.183.0 pins MCP 1.29.0. Rule retrieval or remote tools may use network or send code; select local rules and review tools/permissions. Native Windows support is not established. |

`repository.json` remains untouched. There is no automatic registration, selection,
permission change, browser installation, or model download in these files.
Direct executables are deliberately not package runners; a missing binary is a
blocker. Offline `sync` must not attempt to prepare a missing dependency.

## Private environment

Root `.env.example` names the optional `EXA_API_KEY`, `TAVILY_API_KEY`, `HF_TOKEN`
and `P4DA_COOKIE`; leave unused entries unset. Workkit has no dotenv loader.
Keep remote config as `{env:NAME}` references, never literal tokens.
For local Linux/WSL, keep trusted shell assignments in `<config-dir>/.env`
(chmod 600) and export them from the shell/profile before launching OpenCode.
On Windows set user environment variables; prefer native `/connect`/OAuth when available.
On rex use the existing private `/etc/openchamber-backend.env` (chmod 600),
already attached to `openchamber.service` by `EnvironmentFile=`.
Verify child-process inheritance with a nonsecret probe, not by printing keys.
The stdio 4PDA server inherits `P4DA_COOKIE`; it is not copied into managed files.
Existing backend/OpenCode processes do not reload an edited environment file;
any necessary restart must be coordinated separately.

## Deferred candidates

- **jscpd MCP**: [upstream](https://github.com/kucherenko/jscpd), MIT. Its former
  disabled review template was based on v5.4.2 source at commit
  [`db415b5`](https://github.com/kucherenko/jscpd/tree/db415b5d7cc34d72a324fe5eae2147646dc3558b)
  ([`main.rs`](https://github.com/kucherenko/jscpd/blob/db415b5d7cc34d72a324fe5eae2147646dc3558b/rust/crates/cpd/src/main.rs),
  [`cli.rs`](https://github.com/kucherenko/jscpd/blob/db415b5d7cc34d72a324fe5eae2147646dc3558b/rust/crates/cpd/src/cli.rs)).
  It passes `--mcp --workers 1 --config <config> <root>`: MCP startup scans the root, and an
  omitted root falls back to the process working directory. Before enabling,
  verify all substitutions are nonempty absolute paths that exist; verify the
  executable and select a deliberately bounded root. The explicit config must
  itself be trusted and have `semantic: false`; do not rely on auto-discovery or
  download a model. The Linux x86_64 GNU v5.4.2 archive was checked against the
  [release API digest](https://api.github.com/repos/kucherenko/jscpd/releases/tags/v5.4.2)
  and tested in a network-isolated scratch fixture; this is not an installation,
  a portable dependency lock, or a readiness claim for other targets.
- **Playwright MCP**: [upstream](https://github.com/microsoft/playwright-mcp),
  Apache-2.0. Its former disabled review template was based on
  [v0.0.84](https://github.com/microsoft/playwright-mcp/tree/v0.0.84) and invoked
  a prepared absolute Node executable and [`cli.js`](https://github.com/microsoft/playwright-mcp/blob/v0.0.84/cli.js)
  with `--executable-path <browser>`. The
  [source metadata](https://github.com/microsoft/playwright-mcp/blob/v0.0.84/package.json)
  declares Node >=18 and depends on Playwright `1.65.0-alpha-2026-10-30`, whose
  packages require Node >=20; these are template evidence,
  not an installation, lock, or runtime verification. Verify all three absolute
  paths are nonempty, exist, and refer to trusted prepared files before enabling.
  The command does not invoke `npx` or `install-browser`, but the browser MCP has
  substantial browser, network, and filesystem capabilities; review its tools
  and permissions before any use. A network-isolated Linux pilot of v0.0.84
  completed MCP initialize and tools/list without a browser; the listed tools
  included `browser_run_code_unsafe`. Browser operation and OpenCode activation
  were not tested. This template does not provision a browser.
- **4pda and Genius**: own sources and provenance/limitations are documented in
  [`4pda.md`](4pda.md) and [`genius.md`](genius.md). Both validate permitted HTTPS
  origins and use bounded, checked redirects; origin checks alone do not prevent
  DNS rebinding. Workkit can prepare their Python dependencies online during
  install/update and stage managed script copies at stable paths during sync.
  Runtime uses an isolated, offline `uv run`; sync does not install dependencies.
  The catalog declaration uses explicit path placeholders, not an installed
  configuration. Preparation does not establish runtime activation, native Windows
  compatibility or provenance.
- **Fossil** remains an optional candidate, not a required graph replacement.
  [v0.1.8](https://github.com/yfedoseev/fossil-mcp/blob/35e30b831095ab58f015feab947fe1c53637ad71/Cargo.toml)
  is licensed MIT OR Apache-2.0. Release preparation and runtime behavior have
  not been verified for this workkit.
