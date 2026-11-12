## Linux

- Use native POSIX paths and respect case-sensitive names; identify the effective user and owner rather than assuming the interactive shell owns a service's files.
- Inspect umask, permissions and symlink boundaries before filesystem changes; keep private directories at 0700 and private files at 0600 without rewriting unrelated permissions.
- Resolve the active configuration, XDG roots and launch context before editing settings; distinguish shell exports from systemd unit and process environments without dumping their secret values.
- Do not restart live daemons, alter global environment settings or change service ownership without an explicit request and a concrete recovery path.
- Check tool availability instead of assuming extra packages; prefer uv, local virtual environments and project data roots over system pip or global installs.
- Preserve executable bits and native file-locking behavior; verify the actual mounted filesystem when durability or atomic replacement is part of the contract.
