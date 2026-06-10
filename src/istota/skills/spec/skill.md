---
name: spec
triggers: [spec, specs, draft spec, design doc, design document, write a spec, new spec, start spec, finish spec]
description: Manage specs for spec-driven development. Specs live in the user's notes_folder under Specs/ with three lifecycle subfolders — Drafts, Active, Done.
disclosure: lazy
companion_skills: [files, notes]
---

# Spec lifecycle

Specs are markdown files that capture the plan for a feature or change before it is implemented. They live in the user's `notes_folder`, separate from regular notes, so the same process applies to every project.

## What a spec must be

A spec is a detailed, thorough implementation document — written so a coding agent can pick it up cold and ship the work without further supervision. Treat the reader as a competent engineer who has not been part of the discussion: every decision they would otherwise have to ask about should already be answered in the document.

Concretely, a good spec:

- States the problem and the constraints, not just the solution.
- Names the files, modules, functions, data structures, and interfaces involved — with paths where they exist.
- Specifies behaviour for the happy path, edge cases, and error handling. No "handle errors appropriately" hand-waving.
- Calls out data-model changes (schemas, migrations, indexes) and config / env-var changes explicitly.
- Defines the test strategy: what tests to write, what they assert, what fixtures or mocks are needed.
- Breaks the work into ordered stages or phases, each independently completable and verifiable.
- Records decisions and the alternatives rejected, so the implementer doesn't relitigate them.
- Lists open questions explicitly — anything left unanswered is a blocker the implementer will have to escalate, so flag it before handoff rather than burying it.

If a draft is too thin to hand off blind, it isn't ready to leave `Drafts/`. When drafting, push back on under-specified requests rather than producing a stub.

## Layout

Default location is `{notes_folder}/Specs/`:

```
{notes_folder}/Specs/
├── Drafts/   # written but not yet being implemented
├── Active/   # implementation in progress, or partially complete
└── Done/     # every stage/phase fully implemented
```

A spec moves through these folders left-to-right. It never skips a state and rarely moves backwards (only if work is abandoned mid-flight, in which case it goes back to `Drafts/`).

## Project specs

`{notes_folder}/Specs/` is for cross-project / personal specs only. When a request explicitly names a project — e.g., "draft a spec for the Acme retry logic" or "list Acme specs" — the spec lives in **that project's own folder**, not under `{notes_folder}/Specs/`.

Where each project lives is deployment-specific — the skill does not assume a layout. Resolve the project folder for the named project in this order, stopping at the first hit:

1. A path the user gives in the request itself ("the spec for Acme — its folder is `~/work/acme`").
2. Channel memory (`CHANNEL.md`) — channel-scoped projects often record their working directory there.
3. A configured resource for that project (a `folder` resource whose `display_name` matches, etc.).
4. User memory (`USER.md`) — long-running projects usually have their root path noted there, sometimes alongside a convention like "all my projects live under `<some-root>/Projects/<name>/`". Honour whatever convention USER.md states.

If none of those resolve, ask the user where the project's folder is. Do **not** invent a path or fall back to `{notes_folder}/Specs/<Project>/` — that path is reserved for the bot's own notes.

Once the project folder is resolved, the layout is:

```
<project-folder>/Specs/{Drafts,Active,Done}/
```

Create the `Specs/{Drafts,Active,Done}` subfolders inside the project folder on first use without asking — the convention is fixed.

If a request is ambiguous about whether a project is meant, default to `{notes_folder}/Specs/` and ask only if the user pushes back.

## Operations

The skill is invoked through natural language, not as a CLI. Recognise these intents and use the `files` skill for filesystem operations.

### Draft a new spec

1. Ask the user for a title if not given.
2. Slugify to `lowercase-with-dashes.md`.
3. Refuse to overwrite if a file with the same slug exists in `Drafts/`, `Active/`, or `Done/` (within the resolved project scope). Suggest a numeric suffix.
4. Write `Drafts/<slug>.md` with this skeleton (soft-wrapped, no hard line breaks within paragraphs):

   ```markdown
   ---
   created: <YYYY-MM-DD>
   ---

   # <Title>

   ## Context
   <why this exists, what problem it solves, links to related specs or issues>

   ## Goals
   <what success looks like, in concrete terms>

   ## Non-goals
   <what is explicitly out of scope>

   ## Design
   <approach, data model, interfaces, key decisions, alternatives considered>

   ## Stages
   - [ ] Stage 1 — <name>
   - [ ] Stage 2 — <name>

   ## Open questions
   <things to resolve before or during implementation>
   ```

5. Confirm with the spec's path.

### Start implementing (Drafts → Active)

1. Find a unique match in `Drafts/` for the slug or fragment the user gave (case-insensitive substring on the filename).
2. If zero or multiple matches, list candidates and stop.
3. Move `Drafts/<file>` → `Active/<file>`.
4. Confirm the move with the new path.

### Mark done (Active → Done)

1. Resolve a unique match in `Active/`.
2. Read the file and check the `## Stages` (or equivalent) checklist:
   - If any unchecked items (`- [ ]`) remain, refuse the move and list the open items. The user must either complete them, edit the spec, or explicitly override (e.g., "force it" / "move it anyway").
3. Move `Active/<file>` → `Done/<file>`.
4. Confirm the move.

### List

Print three sections (`Drafts`, `Active`, `Done`) with the filenames under each, scoped to the resolved project (or the flat tree by default). If a section is empty, write `_none_`. Sort filenames alphabetically. Do not read file contents.

### Show

Resolve a unique match across all three folders (search Active first, then Drafts, then Done). Print the path and the file contents.

### Edit

When the user asks to edit, extend, or revise a spec, edit it in place — do not move it between folders as a side effect of editing. State transitions are explicit.

## Conventions

- Filenames: `lowercase-with-dashes.md`. No dates in filenames (the filesystem mtime is enough).
- Soft-wrap prose; no hard line wrapping inside paragraphs.
- Stages tracked as a checklist (`- [ ]` / `- [x]`) so the "done" gate can verify completion.
- Frontmatter: include at least a `created` date. Add an `agents:` field only if the spec has non-obvious read/write quirks (per the notes skill convention).
- Specs are not committed to project repos — they live in the user's notes_folder.

## Output

Be terse. After any state-changing operation, print one line: `<verb> <slug>: <old-path> → <new-path>`. After listing, print the section summary and nothing else. After drafting, print the absolute path of the created file.
