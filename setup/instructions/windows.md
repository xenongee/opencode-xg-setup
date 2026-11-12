## Windows

- Identify pwsh, powershell or cmd before constructing a command; obey its quoting rules and prefer Python, uv and direct argument arrays to fragile bat/cmd wrapper chains.
- Use native paths, preserve drive letters and quote spaces; resolve the real profile, APPDATA, LOCALAPPDATA and ~/.config target rather than assuming ProgramData.
- Respect NTFS sharing locks, ACLs, reparse points and junctions; coordinate writers and preserve the previous valid file if replacement fails.
- Preserve existing encoding and CRLF/LF; default new text to UTF-8 without BOM, and do not change global core.autocrlf or system locale for a local fix.
- Enter wsl.exe only for a requested WSL task; do not mix C:\ paths, \\wsl$\ shares and /mnt/c paths as interchangeable file identities.
- Identify which OS owns a process, service, environment and toolchain before invoking it; never silently substitute a Linux workflow for native Windows or alter machine-wide settings without approval.
