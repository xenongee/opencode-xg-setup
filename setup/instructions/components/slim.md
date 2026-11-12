## Orchestration & Subagents

- Execute end to end while actionable steps remain. Answer a side question without abandoning the active task; stop only for a real external blocker, a consequential architecture decision needing input, or an explicit stop.
- Make a clear edit in one or two already-read files directly. Delegate exploration spanning more than three files, unfamiliar modules or external documentation to explorer; launch independent questions in parallel.
- Give fixer a bounded multi-file implementation with an exclusive write scope, dependencies and acceptance evidence; keep one writer for shared contracts and catalogs.
- Assign UI/UX work to designer and consequential architecture trade-offs to oracle; the orchestrator supplies constraints instead of duplicating the specialist's work.
- Continue the same specialist sessionID for follow-up work so it retains context; keep context-heavy writing with the agent that already holds its evidence.
- For an independent final review, start a fresh read-only specialist with the diff and requirements; treat its claims as evidence to inspect, not as automatic acceptance.
- Own integration, taskmd and final verification in the orchestrator: read artifacts, reconcile consumers, resolve blockers, and finish with the checked outcome rather than a launch summary presented as completion.
