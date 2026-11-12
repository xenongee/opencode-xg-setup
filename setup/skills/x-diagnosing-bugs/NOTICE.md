# Provenance

`SKILL.md` is a modified adaptation of Matt Pocock's `diagnosing-bugs` skill.

- Upstream: https://github.com/mattpocock/skills
- Revision: `4ae2317ddb22b046e26455e4f9c9b3aaaf62212d`
- Upstream files used: `skills/engineering/diagnosing-bugs/SKILL.md`,
  `skills/engineering/diagnosing-bugs/agents/openai.yaml`
- License: MIT, Copyright (c) 2026 Matt Pocock. `LICENSE` is the unmodified
  upstream license text.

## Changes

- Renamed to `x-diagnosing-bugs`; the description now says when the loop is worth
  its cost.
- Reduced to five phases: reproduce, minimise, hypothesise, probe, fix and
  regress, plus redaction and cleanup.
- Dropped the `GLOSSARY.md` and ADR reading requirement, which assumed a
  repository convention this skill cannot rely on.
- Dropped the `scripts/hitl-loop.template.sh` reference and the template: the
  loop here must be agent-runnable, so a human-driven script is not shipped.
- Dropped the mandatory commit/PR message requirement; the cause is recorded
  where the change is reviewed.
- Added an explicit exemption: an obvious fix needs none of this.
- `agents/openai.yaml` (display metadata for another harness) is not carried
  over; this skill is delivered for OpenCode.
