---
name: x-writing-for-agents
description: Use when writing or pruning text an agent reads - a skill, AGENTS.md, or a doc behind a pointer. Keeps the always-loaded context short and pushes detail behind pointers.
license: MIT
metadata:
  opencode/autoinvoke: false
---

# Writing for agents

The same levers apply to a skill, an `AGENTS.md`, or a doc reached by a pointer.
For frontmatter and invocation choices, read `SKILL-MECHANICS.md`.

Adapted from Matt Pocock's `writing-for-agents` (MIT). See `NOTICE.md`.

## Two loads

Every line you add spends one of two budgets:

- **Context load** - always-loaded text: a description, an `AGENTS.md` line, a
  pointer. It costs tokens and attention on every turn, whether or not it fires.
- **Cognitive load** - the human's cost of remembering which documents exist.
  Not a budget to minimise; spend it where human judgement matters.

The design goal: keep the always-loaded part small, and reach the rest only when
a pointer fires.

## Pointers

A **pointer** names out-of-context material and encodes when to reach it. Its
wording decides whether the material is ever loaded, so sharpen the wording
before inlining the material itself.

A pointer states what the material is and which branches trigger it. Because
every pointer word is always-loaded, prune it harder than body text: front-load
the leading word, one trigger per branch (synonyms for one branch are one branch),
and drop identity the body already carries.

## Steps, reference and disclosure

Content is either **steps** (ordered actions) or **reference** (rules and facts
consulted on demand). Both may coexist. Rank each piece by how immediately it is
needed:

1. In-file step - what the agent does, in order.
2. In-file reference - consulted on demand; a flat peer set is fine.
3. Disclosed reference - a separate file behind a pointer.

**Progressive disclosure** moves a piece down that ladder so the top stays
legible. The test is branching: inline what every branch needs, disclose what
only some branches reach. **Co-location** keeps a concept's definition, rules and
caveats under one heading, so reading one part brings its neighbours. Both are
about placement, not length.

**Sprawl** is the failure: a document too long even when every line is live.
Attention thins and every line must stay relevant. Disclose reference, and split
by branch or sequence.

## Completion criteria

Every step ends on a criterion that says when it is done:

- **Clarity**: can the agent tell done from not-done? A vague bound invites
  finishing early. Sharpen the bound first; only if it stays fuzzy and you see
  the rush, split the sequence so later steps are out of view. Hiding only works
  across a real context boundary.
- **Demand**: how much the step requires. "Every changed caller accounted for"
  forces digging; "produce a list" does not. Demand works on flat reference too,
  not only on steps.

The strongest criteria are both checkable and exhaustive.

## Leading words

A **leading word** is a compact pretrained concept the agent thinks with (_loop_,
_seam_, _budget_). Repeated as a token rather than a sentence, it anchors a region
of behaviour in few tokens. Prefer an existing word over a coined one: a made-up
word recruits no priors and must be defined.

Hunt for passages that are a leading word spelled out: "fast, deterministic and
repeatable" collapses to _tight_. Fewer tokens and a sharper hook.

State the **positive** target ("write one-line comments") rather than the
prohibition: a ban drags the forbidden behaviour into context and makes it more
available. Keep a prohibition only as a hard guardrail, paired with what to do
instead.

## Pruning

- **One source of truth** per meaning. Duplication costs maintenance and tokens
  and inflates a meaning's prominence past its rank.
- The **environment** is a source of truth too: scripts, config, layout, `--help`.
  Restating it is a **cache**; cache only what the agent cannot find by looking -
  unwritten conventions, reasons, gotchas.
- Check **relevance**: a line loses it by never bearing on the task or by going
  stale. Without pruning, documents accumulate **sediment**; shorter documents
  are easier to keep live.
- Delete **no-ops**: a sentence the model already obeys by default. The test is
  model-relative - does it change behaviour? - so settle it by running the
  document, not by arguing. Delete the whole sentence rather than trim words.
