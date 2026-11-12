# Provenance

`SKILL.md` and `SKILL-MECHANICS.md` are modified adaptations of Matt Pocock's
`writing-for-agents` skill.

- Upstream: https://github.com/mattpocock/skills
- Revision: `4ae2317ddb22b046e26455e4f9c9b3aaaf62212d`
- Upstream files used: `skills/productivity/writing-for-agents/SKILL.md`,
  `skills/productivity/writing-for-agents/SKILL-MECHANICS.md`,
  `skills/productivity/writing-for-agents/agents/openai.yaml`
- License: MIT, Copyright (c) 2026 Matt Pocock. `LICENSE` is the unmodified
  upstream license text.

## Changes

- Renamed to `x-writing-for-agents`; the description states the page it writes
  for and the one outcome it optimises.
- Condensed the reference text: the two loads, pointers, disclosure, completion
  criteria, leading words and pruning keep their levers with the long examples
  removed. Repetitions between `SKILL.md` and `SKILL-MECHANICS.md` were cut so
  each meaning has one home.
- Made the permanent/loaded split explicit: what is always loaded (descriptions,
  `AGENTS.md` lines, pointers) versus what a pointer loads on demand.
- `SKILL-MECHANICS.md` keeps frontmatter, invocation choice and shared reference.
  The upstream router-skill section is removed: this workkit has no router and
  does not want one introduced.
- No second workflow, no required spec/ticket documents, no mandatory templates.
- `agents/openai.yaml` (display metadata for another harness) is not carried
  over; this skill is delivered for OpenCode.
