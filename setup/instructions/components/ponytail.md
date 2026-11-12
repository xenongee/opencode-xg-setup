## Ponytail

- First ask whether the change needs to exist; remove unnecessary work before optimizing its implementation.
- Follow the minimum-change ladder: reuse the repository's mechanism, then the standard library or platform, then an installed dependency, then write the smallest amount of clear new code needed.
- Reject speculative abstractions, configuration for hypothetical users, compatibility layers without callers and dependencies added for a few lines.
- Complete the whole affected contract, including callers and fixtures; a short diff that leaves broken consumers is not a small complete solution.
- For new nontrivial logic, add one focused test or self-check at the real data-loss, security or contract risk; do not generate test matrices for trivial edits or mirror implementation line by line.
- Never trade away boundary validation, security, accessibility or error handling that protects user data to save code; state any deliberate limit and when it must be revisited.
