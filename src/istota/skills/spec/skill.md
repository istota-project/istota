---
name: spec
triggers: [spec, specs, draft spec, design doc, design document, write a spec, new spec, start spec, finish spec]
description: Manage specs for spec-driven development. Specs live in the user's notes_folder under Specs/ with three lifecycle subfolders — Drafts, Active, Done.
companion_skills: [files, notes]
---

# Spec lifecycle

Specs are markdown files that capture the plan for a feature or change before it is implemented. They live in the user's `notes_folder`, separate from regular notes, so the same process applies to every project.

## Who reads a spec

A spec you write here is usually implemented later by a coding agent working in the project's own checkout, stage by stage, with no way to ask you anything. Each stage is handed to a fresh agent that gets the spec's path and one stage line, and nothing else. That agent reads the sections below **by their exact headings** — `## Design`, `## Existing code this builds on`, `## Test strategy`, `## Decisions & rejected alternatives` — and an implementation run refuses to start while anything is left under `## Open questions`. So the skeleton is a contract, not a suggestion: keep every heading, spelled as shown, and fill every section. A section with nothing in it says so (`_none_`) rather than being dropped.

## What a spec must be

A spec is a detailed, thorough implementation document — written so a coding agent can pick it up cold and ship the work without further supervision. Treat the reader as a competent engineer who has not been part of the discussion: every decision they would otherwise have to ask about should already be answered in the document.

Concretely, a good spec:

- States the problem and the constraints, not just the solution.
- Names the files, modules, functions, data structures, and interfaces involved — with paths where they exist.
- **Surveys what already exists before designing anything new.** Name the modules, helpers and patterns the work builds on, with paths, and say for each new thing why an existing one could not be widened to cover it. A spec is where duplication gets designed in: an implementer handed "add `parse_foo()` to a new `foo_util.py`" writes exactly that, and never asks whether the project already parses foo somewhere. Read the source to do this, not memory or a summary (see "Reading the code" below).
- Specifies behaviour for the happy path, edge cases, and error handling. No "handle errors appropriately" hand-waving.
- Calls out data-model changes (schemas, migrations, indexes) and config / env-var changes explicitly.
- Defines the test strategy: what tests to write, what they assert, what fixtures or mocks are needed.
- Breaks the work into ordered stages, each independently completable and verifiable (see "Stages" below).
- Records decisions and the alternatives rejected, so the implementer doesn't relitigate them.
- Lists open questions explicitly — anything left unanswered is a blocker the implementer will have to escalate, so flag it before handoff rather than burying it.

If a draft is too thin to hand off blind, it isn't ready to leave `Drafts/`. When drafting, push back on under-specified requests rather than producing a stub.

### Reading the code

The survey and the file paths have to come from the current source. If the project is a repository you can reach through the `developer` skill, run `istota-dev clone` to fetch it and read files with `istota-dev show` (or a worktree for wider searches) before writing `## Affected files & interfaces` and `## Existing code this builds on`. Search for what the code does, not only for the name you would give it. If you cannot reach the source, write the spec anyway, but say at the top of `## Existing code this builds on` that the survey was not done against the code, and add an open question asking the implementer to do it. Never present remembered paths as surveyed ones.

### Stages

Each stage is one line in a `- [ ]` checklist under `## Stages`, in this form:

```
- [ ] Stage 1 — <name>: <what is done>. Verified by <the named tests or check>.
```

Plain checklist lines only: no nested sub-bullets, no tables, no prose stages outside the checklist. A spec with no checklist cannot be implemented or marked done.

A stage is one coherent slice that lands with its own tests and that one agent can close in a fresh context: one concern, one seam, a diff a reviewer can hold in their head. Split a stage when its line needs "and" to join two outcomes that could each fail on their own, when it reaches into two unrelated subsystems, or when its diff would plausibly run past a few hundred lines. Merge in one direction only, for trivia: a stage that is only a rename, a config key, a doc update or one function goes into the stage that needs it. Never fold separate work together to keep the list short.

Most specs come out at 4 to 8 stages. Use that as a sanity check, never as a limit: fewer than 4 usually means stages are bundling separate work, and more than 8 is the point to ask whether this is two specs. If it is, split where the first ships something usable on its own, and say in each spec's Context what the other covers. If it isn't, keep every stage.

A stage that depends on another spec landing first says so as its own first stage (`- [ ] Stage 0 — dependency: <spec> landed`), so the implementer checks it before starting.

## Layout

Default location is `{notes_folder}/Specs/`:

```
{notes_folder}/Specs/
├── Drafts/   # written but not yet being implemented
├── Active/   # implementation in progress, or partially complete
├── Done/     # every stage fully implemented
└── Logs/     # <slug>.results/ — written by the implementer, one result file per closed stage
```

A spec moves through `Drafts` → `Active` → `Done` left-to-right. It never skips a state and rarely moves backwards (only if work is abandoned mid-flight, in which case it goes back to `Drafts/`). `Logs/` is a sibling of the three so results stay put when a spec moves. Leave its contents alone unless asked; it belongs to the implementation run.

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
<project-folder>/Specs/{Drafts,Active,Done,Logs}/
```

Create any missing `Specs/{Drafts,Active,Done,Logs}` subfolders inside the project folder on first use without asking — the convention is fixed.

If a request is ambiguous about whether a project is meant, default to `{notes_folder}/Specs/` and ask only if the user pushes back.

## Operations

The skill is invoked through natural language, not as a CLI. Recognise these intents and use the `files` skill for filesystem operations.

### Draft a new spec

1. Ask the user for a title if not given.
2. Slugify to `lowercase-with-dashes.md`.
3. Refuse to overwrite if a file with the same slug exists in `Drafts/`, `Active/`, or `Done/` (within the resolved project scope). Suggest a numeric suffix.
4. Read the code (see "Reading the code") before filling in the file and interface sections.
5. Write `Drafts/<slug>.md` with this skeleton (soft-wrapped, no hard line breaks within paragraphs). Keep every heading exactly as written; you may add `###` subsections inside a section and an appendix after `## Open questions`, but no new `##` sections in between:

   ```markdown
   ---
   created: <YYYY-MM-DD>
   author: {BOT_DIR}
   ---

   # <Title>

   ## Context
   <why this exists, what problem it solves, links to related specs or issues>

   ## Goals
   <what success looks like, in concrete terms>

   ## Non-goals
   <what is explicitly out of scope>

   ## Design
   <approach, data flow, key interfaces, behavioural rules>

   ## Affected files & interfaces
   <concrete paths to files/modules/functions to add or change; for each, what changes and why. Name new types, function signatures, and public APIs explicitly.>

   ### Existing code this builds on
   <what is already in the tree that this work uses or extends, with paths — the helpers, clients, parsers, config readers, test fixtures and patterns it should reuse rather than reimplement. For every genuinely new module or function above, one line on what was searched for and why nothing existing fits. If nothing like this exists anywhere, say that explicitly; it is a finding, not an empty section.>

   ## Data & config changes
   <schema changes, migrations, indexes, new env vars, feature flags, config keys. If none, write "_none_".>

   ## Behaviour
   ### Happy path
   <step-by-step description of the normal flow>

   ### Edge cases
   <enumerate the specific cases — empty inputs, concurrent callers, partial failures, boundary values, etc. — and the expected behaviour for each>

   ### Error handling
   <what errors can occur, how they propagate, what the user/caller sees, what gets logged>

   ## Test strategy
   <which tests to add and where; what each asserts; fixtures, mocks, or test data needed; how to run them; which tests belong to which stage>

   ## Decisions & rejected alternatives
   <decisions made and the alternatives considered but rejected, with the reason. Prevents relitigation.>

   ## Stages
   - [ ] Stage 1 — <name>: <what is done>. Verified by <tests or check>.
   - [ ] Stage 2 — <name>: <what is done>. Verified by <tests or check>.

   ## Open questions
   <unresolved items that block handoff. Each one is a blocker — resolve it before the spec moves to Active, or the implementation will refuse to start. When none remain, write "_none_".>
   ```

6. Confirm with the spec's path.

### Revising a spec after review

When the user answers an open question, move the answer into the section it belongs to (usually `## Design` or `## Decisions & rejected alternatives`) and delete the question. Don't leave answered questions under `## Open questions` with "resolved" next to them: anything left in that section reads as unresolved.

### Start implementing (Drafts → Active)

1. Find a unique match in `Drafts/` for the slug or fragment the user gave (case-insensitive substring on the filename).
2. If zero or multiple matches, list candidates and stop.
3. Check the file before moving it: every heading from the skeleton is present, `## Stages` has at least one `- [ ]` line, and `## Open questions` is empty or `_none_`. If any check fails, say which and ask whether to fix it first or move it anyway.
4. Move `Drafts/<file>` → `Active/<file>`.
5. Confirm the move with the new path.

### Mark done (Active → Done)

1. Resolve a unique match in `Active/`.
2. Read the file and check the `## Stages` checklist:
   - If any unchecked items (`- [ ]`) remain, refuse the move and list the open items. The user must either complete them, edit the spec, or explicitly override (e.g., "force it" / "move it anyway").
3. Move `Active/<file>` → `Done/<file>`.
4. Confirm the move.

### List

Print three sections (`Drafts`, `Active`, `Done`) with the filenames under each, scoped to the resolved project (or the flat tree by default). If a section is empty, write `_none_`. Sort filenames alphabetically. Do not read file contents.

### Show

Resolve a unique match across all three folders (search Active first, then Drafts, then Done). Print the path and the file contents.

### Edit

When the user asks to edit, extend, or revise a spec, edit it in place — do not move it between folders as a side effect of editing. State transitions are explicit. Keep the skeleton's headings intact while editing.

## Conventions

- Filenames: `lowercase-with-dashes.md`. No dates in filenames (the filesystem mtime is enough).
- Soft-wrap prose; no hard line wrapping inside paragraphs.
- Stages tracked as a `- [ ]` / `- [x]` checklist under `## Stages`, one line per stage, so the "done" gate and the implementer can read them.
- Frontmatter: `created` and `author`. Add an `agents:` field only if the spec has non-obvious read/write quirks (per the notes skill convention).
- Specs are not committed to project repos — they live in the user's notes_folder or the project's folder.

## Output

Be terse. After any state-changing operation, print one line: `<verb> <slug>: <old-path> → <new-path>`. After listing, print the section summary and nothing else. After drafting, print the absolute path of the created file.
