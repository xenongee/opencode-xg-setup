# OpenCode Workkit

Personal setup kit for OpenCode V2 / OpenChamber: tools, MCP servers, skills,
custom providers and generated engineering instructions. Experimental; tested
on Linux. Native Windows and WSL execution remain unverified.

## Install and use

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), Node.js 22.18+ and npm.
From a checkout:

```sh
uv tool install .
workkit install --dry-run
workkit install
workkit update
workkit sync
workkit check
```

`install` reads [packages.json](packages.json): it installs the base components
(`optional: false`) and retains your saved selection. A later bare install also
picks up newly added defaults. An explicitly saved empty selection stays
fail-closed; select components explicitly to start again.

```sh
workkit install rea                 # optional reverse-engineering CLI/MCP
workkit install untangle-codebase   # optional skill
workkit install --all               # all catalog components
workkit update taskmd              # update one selected component
```

`update` refreshes selected npm/uv packages, upstream skills, GitHub release
binaries and the provider SDK. Initial binary installs use pinned checksums;
updates resolve the latest release and require its advertised SHA-256 digest.
`sync` applies configuration offline without downloading; `check` is read-only.
Both report local state, not whether model calls or MCP sessions actually work.

Modifying commands support `--dry-run` without executing actions or writing files.
Optional MCP entries start disabled; existing user toggles are preserved.
PolzaAI is disabled in the bundled provider registry; RouterAI remains enabled.

The default OpenCode directory is `~/.config/opencode` on every OS. Tools and
receipts live separately in the Workkit data directory. Override paths as needed:

```sh
workkit install --config-dir "$HOME/.config/opencode-test" --data-root "$HOME/.local/share/workkit-test"
```

## Environment variables

Real `install` and `sync` import one private `.env`: `--env-file` takes precedence,
then the current directory, then an identifiable source checkout. A copy installed
with `uv tool install <path>` records that path (PEP 610 `direct_url.json`), so the
checkout `.env` is still found when the command runs from any other directory.
Missing automatic files are skipped. Preview, update and check do not import secrets.
[.env.example](.env.example) lists supported credential names; use a private file,
not a committed copy. Empty values are skipped; values are never printed.

```sh
workkit sync --env-file "$HOME/.config/opencode/.env"
```

- **Linux / WSL:** upsert `~/.config/opencode/workkit.env` and
  `~/.config/environment.d/opencode-workkit.conf` as private files. Existing
  `.profile` and `.bashrc` receive one source block. An existing writable
  `/etc/openchamber-backend.env` is updated in place with metadata preserved.
- **Windows:** upsert user variables in `HKCU\Environment` and broadcast one
  environment-change notification when values change.

Repeated runs replace keys rather than adding duplicates. The importer never
executes dotenv text or injects it into installer subprocesses. Already running
applications retain their old environment; Workkit does not restart services.
Prefer native `/connect` or OAuth where available.

## Safety and limits

- JSONC comments, unrelated settings and user toggles are preserved. Ownership
  receipts block overwriting manually changed managed files or values; reconcile
  conflicts instead of deleting receipts.
- Recipes run with your permissions, not in a sandbox. Install/update may download
  dependencies. Upstream skills follow declared `main` files; local hashes detect
  later changes, not upstream authenticity.
- Multi-file publication is not atomic. Failed actions may leave partial resources;
  reports identify the failure rather than claiming a machine-wide rollback.
- One compact [test file](tests/test_core.py) covers critical safety contracts;
  release checks also inspect wheel/sdist and CLI use outside the checkout.
  Native Windows/WSL, actual API keys and complete model workflows are not certified.
- Workkit does not migrate Engram databases or publish repositories.

Own MCP details: [4PDA](setup/mcp/4pda.md), [Genius](setup/mcp/genius.md).
Custom providers: [plugin](setup/plugins/x-custom-providers/README.md).

## License

[Unlicense](LICENSE). Third-party code retains its own licenses and notices;
see [bundled skills](setup/skills) and the
[provider plugin](setup/plugins/x-custom-providers/LICENSE).
