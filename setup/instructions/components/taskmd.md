## taskmd

- Use the single file-backed tasks/ queue for work that spans sessions, has blockers or dependencies, or is deferred as a side request; do not register quick one-step actions.
- Read taskmd list and taskmd next --strict-priority before choosing queued work; explicit user priorities outrank the recommendation and phase ordering can affect readiness.
- Record touches and consult taskmd tracks before parallel assignments; use shared scopes to prevent conflicting writers, not as a substitute for a clear handoff.
- Allow only the active orchestrator to change shared statuses; specialists report evidence and blockers without independently rewriting the queue.
- After compaction or a pause, recover by exact ID with taskmd get <ID>, read dependencies, and compare the task with real git status and git diff before continuing.
- Use taskmd from PATH or the verified executable path reported by workkit check; recheck after updates, quote paths with spaces, and create no duplicate status files, wrappers or scheduler.
