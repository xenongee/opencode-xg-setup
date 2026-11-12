## Engineering

- Map module boundaries, data flow and invariants before editing behavior; inspect callers, configuration, schemas and relevant checks, not only the target function.
- Change a contract together with every affected consumer and migration; distinguish an intentional compatibility break from a missed caller.
- Reproduce the failure and fix its root cause. Do not hide broken trusted contracts behind empty exception handlers, silent fallbacks or another local retry.
- Name consequential assumptions before relying on them; when ambiguity changes the architecture or outcome, ask one or two precise questions before coding.
- Validate network, filesystem, CLI and external-API inputs at trust boundaries; reject invalid state before side effects rather than spreading defensive branches through trusted code.
- Keep secrets out of prompts, output, logs, receipts, memory and commits. Read credentials only through an explicitly authorized private workflow; report variable names and counts, never their values.
- Make writes and external effects idempotent and safe to retry; coordinate writers, preserve manual changes, and account for partial publication without promising a rollback that does not exist.
- Edit the source of truth rather than generated artifacts; preserve the project's established mechanisms and remove dead paths instead of maintaining parallel implementations.
- Comment only a non-obvious why, invariant or trade-off; keep unrelated cleanup and speculative flexibility outside the requested scope.
- Converge after the last change: compare the integrated diff with the original goal, remove accidental edits, run the relevant check on that exact state, and separate verified results from remaining assumptions.
