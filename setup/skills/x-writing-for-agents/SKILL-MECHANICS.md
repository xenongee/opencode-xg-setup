# Skill mechanics

What changes when the document is a skill: frontmatter and invocation. Everything
else about writing it is in `SKILL.md`.

## Frontmatter

- `name` matches the directory name.
- `description` is the skill's top-level pointer: say what it does and which
  branches trigger it. A description is always loaded, so it earns the hardest
  pruning. It is honest about cost and scope, and does not claim coverage the
  body lacks.
- `license` names the license covering the file.

## Invocation

- **Model-invoked**: keep the description so the agent can reach the skill on its
  own and other skills can link to it. This is permanent context load, paid for
  discovery. Choose it when the agent must fire the skill unprompted.
- **User-invoked**: set `disable-model-invocation: true`. Only a human typing the
  name reaches it, so nothing is always loaded - but the human must remember it
  exists. The description then only needs to help a human recognise the skill.

Pick model-invocation only when independent discovery is worth the load. A skill
that fires only by hand should be user-invoked.

## Reference shared by several skills

A model-invoked skill's body is one home for reference that other skills need:
another skill can invoke it. If the skills that need the reference are
user-invoked, neither can reach the other; put the shared text in an ordinary
file outside the skill system and point at it.

## What this skill does not define

No router skill, no required spec or ticket documents, no second workflow. When a
skill needs no automation, write a shorter skill instead of adding structure.
