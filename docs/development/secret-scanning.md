# Secret and private-data scanning

This repository is public. Two classes of thing must never reach a commit, and
they need different tools:

- **Credentials** — API keys, passwords, tokens. Caught by [gitleaks](https://github.com/gitleaks/gitleaks),
  which recognises them by shape and entropy.
- **Private data** — a real name, a production hostname, a home directory path,
  an account number, a personal address. These have no universal shape, so
  gitleaks cannot see them; they are caught by a denylist scan.

Both run in the same pre-commit hook.

## Setup

```bash
brew install gitleaks          # macOS
./scripts/setup.sh             # sets core.hooksPath to .githooks
cp .private-data-local.example .private-data-local   # then fill it in
```

On Linux, take the release tarball from
<https://github.com/gitleaks/gitleaks/releases> and put the binary on your PATH.
**Not the distribution package**: Debian 13 ships 8.16, and the hook calls
`gitleaks git --staged`, a subcommand that arrived in 8.19 when it replaced
`detect` and `protect`. The Ansible role installs the pinned upstream release on
any host with the developer skill enabled, for the same reason.

`scripts/setup.sh` is what enables the hook — a checked-in hook does nothing
until `core.hooksPath` points at it, and that is per-clone local config. Run it
once per checkout. It probes `gitleaks git --help` rather than just looking for
the binary, so a too-old install is reported as such instead of passing here and
failing at your first commit.

## When a scanner cannot run

The hook has to decide whether a scan it could not run is a warning or a refusal,
and the answer differs by who is committing.

A human at a workstation gets the warning and the commit goes through. Refusing
someone's commit because something is missing from their laptop is how a hook
ends up bypassed for good, and they can read the warning and act on it.

An unattended commit gets the refusal. Nobody is there to read a warning, and
half a gate is not a gate — the private-data scan matches patterns somebody wrote
down, not secret shape. This is what ISSUE-291 was about: the deployment ran for
weeks with the credential half inactive, and the only trace was a line of hook
output nobody saw.

The hook reads three markers, because the daemon spawns unattended shells three
ways and no single variable covers all of them:

| Marker | Set for |
|---|---|
| `ISTOTA_SANDBOXED` | every task the model runs |
| `DEVELOPER_REPOS_DIR` | a task additionally authorized for the developer skill |
| `PRECOMMIT_SCANS_REQUIRED=1` | cron `command` jobs and heartbeat shell commands, via `build_stripped_env` — they carry neither of the above |

`PRECOMMIT_SCANS_REQUIRED` is also the manual override, in both directions:
`1`/`true`/`yes`/`on` demands the scans, `0`/`false`/`no`/`off` releases them.
Any other value warns and demands them, since an unclear setting must not resolve
to the permissive reading in silence. Prefer this to `--no-verify`, which drops
both scans rather than the one that is broken.

The refusal message deliberately does not mention the override. That branch is
reached only when nobody is watching, so its only reader is the automated
committer being refused — printing its own way past the gate there would make the
gate advisory against exactly the actor it exists to bind. A refusal means a
broken install on that host; fix the install.

Both halves follow the same rule, so a `check-private-data.sh` that has lost its
executable bit refuses an unattended commit too.

The hook only runs where `core.hooksPath` points at it. The developer skill's
clone recipe sets it on every pass rather than only at clone time, so a bare
clone made before that step existed repairs itself the next time the skill runs
against it. Your own checkout is the one that needs it applied by hand:
`scripts/setup.sh`, or `git config core.hooksPath .githooks`.

## What the hook does

`.githooks/pre-commit` runs two scans over **staged** content:

1. `gitleaks git . --staged -c .gitleaks.toml`
2. `scripts/check-private-data.sh --staged`

Either one failing aborts the commit. Neither prints the matched value — a
scanner that echoes the secret puts it in your scrollback, and from there into a
chat log or an issue. You get `file:line` and the class that fired; open the file
to see what it found.

To see gitleaks' detail yourself:

```bash
gitleaks git . --staged -v -c .gitleaks.toml     # prints the secret
```

If the problem is a scanner that cannot run rather than a finding, use
`PRECOMMIT_SCANS_REQUIRED=0` — it releases only that check. Reach for
`git commit --no-verify` only when you are certain, and then go fix
whatever made it necessary.

## The three pattern sources

`scripts/check-private-data.sh` loads patterns from three places:

| Source | Committed? | Holds |
|---|---|---|
| `.private-data-patterns` | yes | Generic *shapes*: payment cards, IBAN, SSN, Nextcloud app passwords, unambiguous API-key prefixes |
| `.private-data-local` | **no**, gitignored | Your own literals: real names, production hostnames, account numbers |
| derived at runtime | n/a | Your home directory path, and your git `user.email` |

The local file is gitignored on purpose. On a public repo, a checked-in list of
the terms you want redacted *is* the leak — it tells a reader exactly what to
search the history for. Start from `.private-data-local.example`, which is
comments only.

Runtime derivation covers the two terms nobody should have to configure: an
absolute path into your home directory, and your personal address. The git user
*name* and the mail *domain* are deliberately **not** derived — this project puts
both in `LICENSE` and `README.md` on purpose, so denylisting them would block
every commit.

Patterns are POSIX extended regexes, one per line; a plain literal is a valid
regex. The comment heading above a block becomes the label a hit reports under.

## Running it by hand

```bash
scripts/check-private-data.sh              # staged content (what the hook runs)
scripts/check-private-data.sh --all        # every tracked file, ~2s
scripts/check-private-data.sh path/to/file # named files as they are on disk
```

Staged mode reads the **index**, not the worktree: a leak that was staged and
then edited out on disk is still what the commit would carry.

A run is one batched `grep -l` over every target at once, and only the files it
names pay for the per-pattern loop that attributes a hit to a line and a label.
A path containing a newline can't go through the batch — `grep -l` reports
matches by name — so those are scanned one at a time rather than skipped.

## False positives

Two escape hatches, in order of preference:

1. **Narrow the pattern.** If a shape is matching things it should not, fix it in
   `.private-data-patterns`.
2. **Mark the line.** Put `private-data-ok` anywhere on the offending line. It
   exempts that line only.

Documentation placeholders are exempt automatically: a line containing `xxxx`,
`<your-token>`, `CHANGEME` or similar is read as an example, not a value. That is
a heuristic, which is part of why gitleaks runs alongside it.

`.gitleaks.toml` carries its own allowlists for the synthetic values the test
suite uses — 16-hex conversation tokens, `key == "..."` comparisons. Add a
*shape* or a *path* there, never a real secret, not even to allowlist it.

`CHANGELOG.md` is deliberately **not** allowlisted. Prose written up from a
terminal session is exactly where a pasted credential or a production hostname
ends up.

## Tests

`tests/test_private_data_scan.py` gives every pattern class a positive control,
plus both ways of not matching (the exemption marker, the placeholder
heuristic). A scanner whose regex quietly stops matching reports a clean tree,
which is indistinguishable from having nothing to find — so the patterns are
tested rather than trusted.

Three further tests pin the batching, which fails the same silent way: a file
that never reaches the batch is never attributed. They cover a leak among sixty
clean files, several leaks in one batch, and a path the batch cannot take.

## If something already leaked

Assume the credential is compromised and **rotate it first**; history rewriting
is cleanup, not containment.

```bash
brew install git-filter-repo
git branch backup-before-cleanup

# Replace a literal (file format: literal:SECRET==>[REDACTED])
git filter-repo --replace-text secrets-to-replace.txt --force

# Or drop a whole file from history
git filter-repo --path path/to/file --invert-paths --force

git remote add origin <url>    # filter-repo drops remotes
git push origin main --force
```

Then force-push to every remote the branch tracks, tell collaborators to
re-clone or
`git reset --hard origin/main`, and ask the hosting provider to purge cached
views of the affected commits. A force-push does not remove a commit that a fork
or a cached web view still references.

When getting help with remediation, share only file paths, line numbers and rule
IDs — never the value.
