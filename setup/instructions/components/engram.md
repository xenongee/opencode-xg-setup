## Engram

- Call mem_context and targeted mem_search at the start of nontrivial work and after compaction to recover project decisions, constraints and previously encountered failures.
- Read the complete relevant observation before relying on a truncated search result; compare remembered paths and contracts with the current checkout.
- Save architectural decisions with their why, complex bug root causes and durable project invariants proactively through mem_save, including concrete paths and evidence.
- Separate established facts from proposals and stale assumptions; resolve surfaced memory conflicts with scoped evidence rather than silently overwriting earlier conclusions.
- Close substantive sessions with mem_session_summary before the final user reply, recording the result, limits and useful recovery context.
- Never store credentials, tokens, private configuration contents or live task-queue statuses in memory; retain durable knowledge here and operational work state in taskmd.
