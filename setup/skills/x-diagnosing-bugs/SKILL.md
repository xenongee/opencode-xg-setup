---
name: x-diagnosing-bugs
description: Use for a hard bug, flaky failure or performance regression that resists a quick look. Reproduce, minimise, test ranked hypotheses, then lock the cause with a regression test.
license: MIT
metadata:
  opencode/autoinvoke: false
---

# Diagnosing bugs

A loop for hard bugs. A bug with an obvious cause and a one-line fix needs none of
this: fix it and state what it was. Use this when the cause is unknown after a
quick read, the failure is intermittent, or the symptom and the code disagree.

Adapted from Matt Pocock's `diagnosing-bugs` (MIT). See `NOTICE.md`.

## Redact first

Show commands and output only after replacing every credential, token and
personal datum with `<REDACTED>`. Feed secrets through environment variables so
they never reach the transcript. If the redacted output is not enough to
diagnose, say so and ask.

## 1. Reproduce

Build a **tight** pass/fail signal that goes red on this bug. That loop is the
work; everything after it is mechanical. Prefer, in order: a failing test at a
seam that reaches the bug; a curl/HTTP script; a CLI call diffed against a known
good snapshot; a headless browser check; a replay of a captured request; a
minimal harness; a property/fuzz loop; bisection.

Then tighten it: faster, sharper (assert the user's symptom, not "did not
crash"), more deterministic. For flaky bugs raise the reproduction rate until it
is debuggable instead of waiting for a clean repro.

Done when one command, already run at least once, fails for the reported symptom,
runs in seconds, and needs no human. If reading code for a theory starts before
that command exists, stop: a hypothesis-first guess is the failure this loop
prevents. If no loop can be built, say what you tried and ask for access, a
redacted artifact, or permission to instrument temporarily.

## 2. Minimise

Confirm the loop fails the way the user described, reproducibly, and capture the
exact symptom. Then shrink the repro one cut at a time - inputs, callers, config,
data, steps - re-running after each cut. Done when every remaining element is
load-bearing: remove any one and it goes green. The minimal case becomes the
regression test.

## 3. Hypothesise

Write 3-5 ranked, falsifiable hypotheses before testing any of them; a single
hypothesis anchors on the first plausible idea. Each states its prediction: "if X
is the cause, changing Y makes it disappear". A hypothesis without a prediction
is a vibe. Share the ranking when the user is reachable - their domain knowledge
re-ranks it cheaply - but do not block on it.

## 4. Probe

Each probe answers one prediction. Change one variable at a time. Prefer a
debugger or REPL inspection over logs, and targeted logs at the boundaries that
separate hypotheses over "log everything". Tag debug output with a unique prefix
so cleanup is one search.

For a performance regression, measure a baseline first (timer, profiler, query
plan) and bisect it; logs rarely answer why something got slower.

## 5. Fix and regress

Write the regression test before the fix, at a seam that exercises the real bug
path as the call site sees it. If only a shallow seam exists, a test there gives
false confidence: state that the architecture prevents locking the bug down
instead of adding it. Then:

1. Turn the minimised repro into a failing test at that seam; watch it fail.
2. Apply the fix.
3. Watch it pass.
4. Re-run the original, un-minimised repro.

## 6. Clean up

- The original repro no longer reproduces.
- The regression test passes, or its missing seam is reported.
- Instrumentation is gone and throwaway prototypes are deleted.
- The cause is stated where the change is reviewed, in one line. No commit or PR
  is required to finish the diagnosis.
