# Cutting a release, and the announcement that opens one

`scripts/release.sh 0.41.0` does the mechanics: moves `## [Unreleased]` to `## [0.41.0] - DATE`, opens a fresh empty `[Unreleased]`, bumps `pyproject.toml`, reconciles `uv.lock` and `web/vite-mock-api.ts`, commits, tags with `--cleanup=verbatim` so the `###` headings survive, and pushes with `--follow-tags` to both remotes. `.github/workflows/release.yml` then builds the GitHub Release from `CHANGELOG.md` in the checked-out tag — never from the tag annotation, which `actions/checkout` does not reliably fetch as an object. Neither the annotation nor the release body is the chronological file: both extractors consolidate duplicate `### Added` / `### Changed` / … sections into one of each in Keep-a-Changelog order, while `CHANGELOG.md` itself stays as merged.

## The announcement

Every release opens with prose describing what happened, before the changeset. A release section is 460 bullets in the large case, each written by whoever merged it and correct on its own; nobody reads that and comes away knowing what the release *is*, and an upgrade note that wants an action from an operator is one bullet among hundreds with nothing marking it as urgent. The announcement is the only place the release is described as a whole.

**It is the `[Unreleased]` section's preamble: everything between the `## [Unreleased]` line and the first `### `.** That location is not arbitrary — both extractors already bucket pre-`###` content under a `None` key and emit it first, so an announcement written there reaches the tag annotation and the GitHub Release body with no change to either, and reaches anyone reading `CHANGELOG.md` in the repository at the same time. One text, four surfaces.

**No headings inside it.** A `### Highlights` is read by both extractors as a changeset subsection, bucketed, and emitted *after* `### Security` — the announcement would arrive at the bottom of the release notes it is meant to open. A `#` or `##` outranks the version heading it sits under. Paragraphs and bold lead-ins only. `scripts/release.sh` refuses a cut with either mistake in it, and refuses one with no announcement at all; `--no-announcement` is the escape for a hotfix that genuinely needs none (a single-bullet patch release), not a way past a cut you have not written yet.

**Write it last, against the finished section.** Bullets accumulate per merge over weeks from branches that never see each other; the announcement is written once, immediately before running `release.sh`, by someone who has just read the whole of `[Unreleased]`. Written earlier it describes a release that then kept changing.

## Writing one

Read the entire `[Unreleased]` section first. Not the headings, not the first sentence of each bullet — the whole thing. The themes are not the Keep-a-Changelog buckets and cannot be derived from them: in 0.41 the largest single theme was spread across `Security`, `Fixed` and `Changed`, and the `Added` bullets for offline web chat, the notification bell and usage reporting are three separate features that arrived interleaved.

Then, in order:

1. **Open with a feature.** The first sentence is something new a person can now do, in their words — "web chat works on a phone with no connection", not a count and not a problem. Never open with what was broken. A release whose real weight is security or bug fixing still opens on its best feature; the fixing is named in a clause near the end and carried by the changeset, which is where a reader goes for it.
2. **Two paragraphs of prose, and no more.** The first is the features that reach the most people, the second is the rest plus one closing clause for the scale of the release and what the remainder was — the commit count against the two previous releases (`git log --oneline --no-merges vPREV..HEAD | wc -l`) belongs there, not in the opening. Group across buckets: a feature is usually spread over `Added`, `Changed` and `Fixed`. A theme that will not fit is not a third paragraph; it is a clause, or it is left to the changeset.
3. **`**Before you upgrade.**` last**, as a list. One entry per thing that wants a decision or an action: a credential to reissue or rotate, a setting that is gone or has changed meaning, a migration a deploy performs and its refusal conditions, a default that flipped, a new alert to expect. Say what to do, not only what changed. Every `**Upgrade note:**` bullet in the section is a candidate; a bullet that merely says something is fixed is not, however large the fix.

What to leave out: anything with no reader outside this repository (a refactor, a test-only change, a rule file), and the enumeration itself — the changeset is directly below and the announcement does not summarise all of it. An entry that matters and did not make a paragraph is not lost; it is one screen down.

Voice is the changelog's own: second person, plain, specific, what it does rather than what it enables. Bold is for the `Before you upgrade` lead-ins and nothing else. The full checklist is the `writing-style` skill, which is worth invoking before drafting rather than after.

The prose is capped at two paragraphs whatever the release: 980 commits earned about 350 words of it, and 36 commits earn three sentences and no upgrade list at all. Only the list grows with the release, because every entry in it is an action somebody has to take. The worked example is the announcement opening the most recent release in `CHANGELOG.md` — read it beside the section it opens.

## Two gotchas that have already cost a release

- **A tag force-push does not re-trigger `push: tags:`.** A workflow that ran with bad input cannot be fixed by re-pushing the tag; re-run it through `workflow_dispatch` with the tag as input.
- **`git tag -a -m` strips lines starting with `#`** unless `--cleanup=verbatim` is passed, which `release.sh` does. Anything else building an annotation from the changeset needs it too.
- **A GitHub Release body is capped at 125,000 characters**, and the API refuses the whole body rather than truncating it — 0.41's first run failed with a 422 and published nothing. `release.yml` now cuts a larger section down: the announcement goes in whole, what is left is split between the `###` sections in proportion to their size so a long `Fixed` cannot crowd `Security` out, each cut section says how many entries it is missing, and a footer links `CHANGELOG.md` at the tag. The tag annotation is not capped and still carries everything. A release under the cap is byte-identical to what the old extractor produced.
