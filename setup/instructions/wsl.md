## WSL

- Treat the distribution's Linux home, configuration, environment and processes as separate from the Windows host; do not equate ~ with /mnt/c/Users/<user>.
- Keep Git checkouts, virtual environments, node_modules and SQLite databases on the distribution's native Linux filesystem, normally ext4, to preserve I/O performance, locking and permissions.
- Identify the distribution and mount backing each target before modifying files; a Windows executable reached through PATH is not a Linux toolchain substitute.
- Cross into Windows only when the task requires it; convert paths explicitly with wslpath and pass arguments safely to cmd.exe or powershell.exe rather than mixing path syntaxes.
- Preserve LF/CRLF expectations at the boundary and select the intended Windows browser or clipboard explicitly; do not rewrite a repository's line endings to fix one interop command.
- Check which side owns a listener, process or credential source; keep interop configuration and host changes separate from distribution-local work.
