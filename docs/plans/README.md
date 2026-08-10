# Planning docs

Planning notes for features in this repo, usually written while building something (often with an AI assistant) and then left alone.

Read these as history, not documentation. Each file captures the intent and approach at the time it was written. The code is the source of truth for how things work now; a plan can be out of date the moment it merges.

## Conventions

- **Date-prefixed filename** so they sort chronologically: `YYYY-MM-DD-<feature>.md`, e.g. `2026-07-07-depth-areas.md` or `2026-07-12-datum-correction.md`. Use the date you start the plan.
- **Ships with the PR.** The plan lands in the same PR as the implementation, so a reviewer can read the intent and the diff side by side. After that it's just history.
- **Clearly communicate status.** Keep the `status` field and each work item up to date as the work progresses. A plan is a snapshot of intent and progress at the time the work is being done. At the time the work merges, the plan should accurately represent what was done.

## Template

```markdown
# <Feature> — planning doc

_Written YYYY-MM-DD. Point-in-time; the code is the source of truth._

Status: [draft | in progress | done]

## Problem

What we're solving and why. Link the issue(s).

## Goals / Non-goals

What this does, and explicitly what it doesn't.

## Approach

The plan: data sources (+ datum/provenance), pipeline stages, key decisions.

## Alternatives considered

What else we looked at, and why not.

## Validation

How we'll know it's correct — fixtures, accuracy checks, tests.

## Open questions

Unresolved decisions.

## Follow Ups

Deferred work, future improvements, or related issues.
```

Keep them short.
