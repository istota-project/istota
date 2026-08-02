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
brew install gitleaks          # macOS; see the gitleaks README for other platforms
./scripts/setup.sh             # sets core.hooksPath to .githooks
cp .private-data-local.example .private-data-local   # then fill it in
```

`scripts/setup.sh` is what enables the hook — a checked-in hook does nothing
until `core.hooksPath` points at it, and that is per-clone local config. Run it
once per checkout.

Without gitleaks installed the hook warns and skips the credential half; the
private-data half is pure bash and always runs.

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

Bypass with `git commit --no-verify` only when you are certain, and then go fix
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
scripts/check-private-data.sh --all        # every tracked file, ~10s
scripts/check-private-data.sh path/to/file # named files as they are on disk
```

Staged mode reads the **index**, not the worktree: a leak that was staged and
then edited out on disk is still what the commit would carry.

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

`CHANGELOG.md` and `DEVLOG.md` are deliberately **not** allowlisted. Prose
written up from a terminal session is exactly where a pasted credential or a
production hostname ends up.

## Tests

`tests/test_private_data_scan.py` gives every pattern class a positive control,
plus both ways of not matching (the exemption marker, the placeholder
heuristic). A scanner whose regex quietly stops matching reports a clean tree,
which is indistinguishable from having nothing to find — so the patterns are
tested rather than trusted.

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
