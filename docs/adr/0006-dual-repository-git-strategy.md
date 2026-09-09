# ADR-0006 — Two independent repositories with independent commit history

**Status:** Accepted
**Date:** 2026-09-09
**Requirements served:** 21 (auditability), 28 (production engineering practices)

## Context

The Causiq project must exist in two GitHub repositories under two different accounts:

| Role | Account | Repository |
|---|---|---|
| Work | `PS-HENIL-PATEL` | `L3-PROJECT` |
| Personal | `henil0109` | `CAUSIQ-Autonomous-AI-Reliability-Engineer` |

Both must contain identical project content at each milestone, but the commits must be created
independently — the same commit object must not be pushed to both, so the resulting SHAs differ.

There is an operational hazard. Both remotes are on `github.com`, and Git Credential Manager keys
credentials by host. The machine's default `github.com` credential resolves to the **work**
account, so any push with an unqualified remote URL authenticates as `PS-HENIL-PATEL` regardless
of which repository is intended.

## Decision

**Two separate working directories, each a distinct Git repository with exactly one remote.**

| Directory | Repository | Remote (username-qualified) |
|---|---|---|
| `…/OneDrive - ProductSquads Technolabs LLP/CAUSIQ` | L3-PROJECT | `https://PS-HENIL-PATEL@github.com/PS-HENIL-PATEL/L3-PROJECT.git` |
| `…/causiq-personal` | CAUSIQ-Autonomous-AI-Reliability-Engineer | `https://henil0109@github.com/henil0109/CAUSIQ-Autonomous-AI-Reliability-Engineer.git` |

Rules:

1. **Remote URLs always embed the account username.** This makes Git Credential Manager select the
   per-account credential (`git:https://henil0109@github.com`) rather than the host default, and
   makes the intended account visible in `git remote -v`.
2. **Each repository has exactly one remote.** A wrong-account push is structurally impossible,
   not merely discouraged — there is no second remote to mistype.
3. **The primary working directory is the source of truth.** At each milestone, content is
   mirrored to the personal directory excluding `.git`, then committed there independently.
4. **The same conventional commit message is used in both**, so milestones are comparable; SHAs
   differ because the commits are authored separately.
5. **The personal mirror lives outside the corporate OneDrive folder**, so a personal-account
   repository is not synchronised into work storage.

## Alternatives considered

**One repository with two remotes, pushing the same commit to both.** Rejected outright: it
produces identical SHAs, which the requirement explicitly forbids, and two remotes in one
repository is precisely the wrong-account hazard.

**One repository with two remotes, amending between pushes to force a new SHA.** Rejected: the
histories silently diverge in confusing ways, `git push --force` becomes routine, and the
wrong-account hazard remains.

**One working tree with two `.git` directories, switched via `--git-dir`.** Content is identical by
construction and no copy step is needed. Rejected as too easy to run against the wrong git
directory, and hard to explain in a review.

## Consequences

**Positive.** A wrong-account push is prevented structurally. Each repository's history is
genuinely independent. The mirror step is an explicit, inspectable action rather than an implicit
one.

**Negative.** Content must be mirrored at each milestone, which is a step that can be forgotten or
done partially. Mitigated by verifying content equality (per-file digest comparison excluding
`.git`) before each personal-repo commit, and by treating that verification as part of the
milestone's definition of done.

**Known constraint at time of writing.** The stored `henil0109` credential is expired, and the
session that created this record was non-interactive, so the personal remote could not be
authenticated. Local commits are unaffected; only the push is blocked. See README for the
re-authentication procedure.

## How we test it

Before each personal-repo commit, a content-equality check compares SHA-256 digests of every
tracked file in both directories, excluding `.git`. After each push, `git log --oneline -1` and
`git remote -v` are recorded for both repositories to evidence that the messages match and the
SHAs differ.

## Review script

*"Both repos live on github.com and the machine's default credential is the work account, so the
failure mode is pushing personal work to the work org without noticing. I made that impossible
structurally rather than procedurally: two directories, one remote each, and every remote URL
carries the account name so the credential manager picks the right identity and `git remote -v`
shows you which account you're about to push as."*
