---
name: developer
triggers: [git, gitlab, github, repo, repository, commit, branch, merge request, MR, pull request, PR, code review, develop, worktree, clone]
description: Git repository management, GitLab merge requests, and GitHub pull requests
companion_skills: [commit, code_review, untrusted_input]
env: [{"var":"DEVELOPER_REPOS_DIR","from":"config","config_path":"developer.repos_dir","when":["developer.enabled","developer.repos_dir"]},{"var":"GITLAB_URL","from":"config","config_path":"developer.gitlab_url","when":["developer.enabled","developer.repos_dir"]},{"var":"GITHUB_URL","from":"config","config_path":"developer.github_url","when":["developer.enabled","developer.repos_dir"]},{"var":"GITLAB_DEFAULT_NAMESPACE","from":"config","config_path":"developer.gitlab_default_namespace","when":["developer.enabled","developer.gitlab_default_namespace"]},{"var":"GITLAB_REVIEWER_ID","from":"config","config_path":"developer.gitlab_reviewer_id","when":["developer.enabled","developer.gitlab_reviewer_id"]},{"var":"GITHUB_DEFAULT_OWNER","from":"config","config_path":"developer.github_default_owner","when":["developer.enabled","developer.github_default_owner"]},{"var":"GITHUB_REVIEWER","from":"config","config_path":"developer.github_reviewer","when":["developer.enabled","developer.github_reviewer"]},{"var":"DEVELOPER_AUTHOR_CREDIT","from":"config","config_path":"developer.author_credit","when":["developer.enabled","developer.author_credit"]},{"var":"GITLAB_TOKEN","from":"config","config_path":"developer.gitlab_token","when":["developer.enabled","developer.repos_dir","developer.gitlab_token"],"sensitive":true},{"var":"GITHUB_TOKEN","from":"config","config_path":"developer.github_token","when":["developer.enabled","developer.repos_dir","developer.github_token"],"sensitive":true}]
---
# Developer Skill — Git, GitLab & GitHub

Work in git repositories, manage merge requests on GitLab and pull requests on GitHub. Uses bare clones + git worktrees for branch isolation.

## Environment Variables

| Variable | Description |
|---|---|
| `DEVELOPER_REPOS_DIR` | Base directory for repo clones and worktrees |
| `GITLAB_URL` | GitLab instance URL (e.g., `https://gitlab.com`) |
| `GITLAB_DEFAULT_NAMESPACE` | Default GitLab namespace (user/group) for resolving short repo names |
| `GITLAB_REVIEWER_ID` | GitLab reviewer for new merge requests — a **username**, see below |
| `GITHUB_URL` | GitHub instance URL (e.g., `https://github.com`) |
| `GITHUB_DEFAULT_OWNER` | Default GitHub org/user for resolving short repo names |
| `GITHUB_REVIEWER` | GitHub username to request as PR reviewer |
| `DEVELOPER_AUTHOR_CREDIT` | Optional text appended to every commit message (e.g., `Co-Authored-By: ...`) |

Git credentials are configured automatically for both platforms — clone and push work without manual authentication.

**Namespace resolution**: When the user gives a short repo name (e.g., "nebula" instead of "namespace/nebula"), use `$GITLAB_DEFAULT_NAMESPACE` or `$GITHUB_DEFAULT_OWNER` as the default namespace/owner depending on the platform. Always confirm the resolved path exists before cloning: `gh repo view OWNER/REPO` or `glab repo view NAMESPACE/PROJECT`.

## The Forge CLIs

`gh` and `glab` are the real GitHub and GitLab command-line tools. You reach them through a wrapper that fetches the token when you invoke them and then gets out of the way, so the whole flag surface is yours — `gh <command> --help` and `glab <command> --help` are accurate.

**Credentials.** The wrapper hands the token to the CLI process and nowhere else: it is not in your environment, not written to disk, and not printed by anything. `git` authenticates through a credential helper the same way. Nothing this skill does needs the token itself, so do not go looking for it. That is a rule about conduct, not a claim that you would be stopped.

**Refused verbs.** A small set of verbs is refused before anything is contacted: the destructive ones (`repo delete`, `repo archive`, `release delete`), the ones that print or mint credentials (`auth`, `glab token create`), the ones that publish (`gh gist create`, `glab snippet`), the ones that run code elsewhere (`gh codespace`, `glab runner`), `config`, `alias`, `extension`, and `gh api graphql`. Writing methods through `gh api` / `glab api` are refused too — an explicit `-X POST`, and any body flag (`-f`, `-F`, `--field`, `--raw-field`, `--form`, `--input`), which both CLIs treat as an implicit POST. Use the verb, not the raw endpoint. You get a one-line reason and exit status 3.

This is an accident guard, not a security boundary. Hitting it means you are about to do something outside this skill's job, so stop and ask the user — do not look for another route to the same effect.

**No terminal, and a 120-second budget.** These commands run non-interactively under a Bash tool that times out. Avoid every watch and follow mode: `gh pr checks --watch`, `gh run watch`, `glab ci status --live`, `glab ci status --wait`, `glab ci trace`, and `glab ci view` (a full-screen TUI). Run the plain command again instead of waiting inside one.

**Pre-submission checks** (mandatory before every MR/PR):
1. **Namespace verification**: Before creating any MR or PR, confirm the remote you are about to push to is the intended one — and read it from the worktree rather than from a path you retyped, because the worktree's `origin` is what will actually receive the push. From inside `$WORK_DIR`: `gh repo view --json nameWithOwner -q .nameWithOwner`, or on GitLab `glab repo view -F json` piped to a parser — glab has no `--jq`, and the full recipe below fails closed on a glab error rather than comparing an empty string. If the user said "submit to `acme/widget`", verify it resolves to `acme`. Abort and ask on any mismatch.
2. **Response verification**: `gh pr create` and `glab mr create` exit non-zero on failure and print the URL on success, so check the exit status rather than scraping the output for error text. Then confirm the thing exists before reporting success: `gh pr view --json number,url,state` or `glab mr view -F json`.
3. **No live source editing**: Never edit files under production installation paths (e.g., `/srv/app/*/src/`). All source changes must go through worktrees in `$DEVELOPER_REPOS_DIR` and be submitted as MRs/PRs.

## Directory Layout

```
$DEVELOPER_REPOS_DIR/
├── namespace/project.git/                    # bare clone
├── namespace/project--istota-42-add-auth/      # worktree for task 42
└── namespace/project--istota-55-fix-bug/       # worktree for task 55
```

- Bare clones go in `<namespace>/<project>.git/`
- Worktrees are siblings: `<namespace>/<project>--<branch-slug>/`

## Cloning a Repository

First time — create a bare clone:

```bash
BARE_DIR="$DEVELOPER_REPOS_DIR/namespace/project.git"

FRESH=""
if [ ! -d "$BARE_DIR" ]; then
    mkdir -p "$(dirname "$BARE_DIR")"
    # Use $GITLAB_URL or $GITHUB_URL depending on where the repo lives
    git clone --bare "$GITLAB_URL/namespace/project.git" "$BARE_DIR"
    git -C "$BARE_DIR" config remote.origin.fetch "+refs/heads/*:refs/remotes/origin/*"
    FRESH=1
fi

# Always fetch latest
git -C "$BARE_DIR" fetch origin

# Everything below restores the invariant stated after this block. It runs on
# every pass, not just at clone time: the shape lives on disk, so a clone made
# before ISSUE-269 is still broken and the `if` above never runs for it again.
# `rev-parse`, not `symbolic-ref`: a dangling origin/HEAD survives the upstream
# default branch being renamed and reads as present to a check that does not
# resolve it (code_review's `_default_base` guards the same state).
git -C "$BARE_DIR" rev-parse -q --verify origin/HEAD >/dev/null 2>&1 ||
    git -C "$BARE_DIR" remote set-head origin -a
DEFAULT_REF=$(git -C "$BARE_DIR" symbolic-ref -q refs/remotes/origin/HEAD)
DEFAULT_BRANCH="${DEFAULT_REF#refs/remotes/origin/}"
[ -n "$DEFAULT_BRANCH" ] || { echo "origin has no default branch"; exit 1; }

# HEAD below refs/heads/ (ISSUE-269): pointed into refs/remotes/ it reads as
# stale just the same, but `worktree add -b` resolves HEAD while writing its new
# local head and aborts with `fatal: HEAD not found below refs/heads!`. `-q` so
# a detached HEAD is a state to repair, not a `fatal:` in the log.
case "$(git -C "$BARE_DIR" symbolic-ref -q HEAD)" in
    "refs/heads/$DEFAULT_BRANCH") ;;
    *) git -C "$BARE_DIR" symbolic-ref HEAD "refs/heads/$DEFAULT_BRANCH" ;;
esac

# ...and nothing under it (ISSUE-125): `clone --bare` fills refs/heads/* once,
# at clone time, and the remote-tracking refspec never updates them again, so a
# local `main` stays frozen at clone day while origin/main moves on and
# `git show main:db.py` silently returns clone-day source. Deleting the fossils
# turns that silent-wrong into a loud `invalid object name`. Only on clone day
# is *every* local head a fossil: later, refs/heads/ also holds the
# {BOT_DIR}/<task> branch of every worktree ever made here, and one whose
# worktree was pruned may be the only copy of that work.
if [ -n "$FRESH" ]; then
    FOSSILS=$(git -C "$BARE_DIR" for-each-ref --format='%(refname:short)' refs/heads/)
else
    FOSSILS="$DEFAULT_BRANCH"
fi
CHECKED_OUT=$(git -C "$BARE_DIR" worktree list --porcelain | sed -n 's/^branch refs\/heads\///p')
for ref in $FOSSILS; do
    # `update-ref -d`, not `branch -D`: it takes a full refname and consults
    # neither HEAD nor the worktree list, so CHECKED_OUT is the only thing
    # deciding what survives.
    echo "$CHECKED_OUT" | grep -qx "$ref" || git -C "$BARE_DIR" update-ref -d "refs/heads/$ref"
done
```

### Reading current source from a bare clone

**Invariant: `refs/remotes/origin/HEAD` resolves, `HEAD` is a `refs/heads/` ref
that does not, and you never name a local branch — always `origin/<branch>` or
`origin/HEAD`.** The first lets every later step discover the base branch
instead of assuming `main`; the rest keeps a clone-day fossil unreadable rather
than silently stale. To read the live tree in one step that can't point at a
stale ref:

```bash
# dev-show <BARE_DIR> <path> — current source from origin/HEAD, always fetched.
git -C "$BARE_DIR" fetch -q origin && git -C "$BARE_DIR" show origin/HEAD:"$path"
```

Use this (or `git -C "$BARE_DIR" log origin/HEAD`) for any hand-rolled
verification read. Never `git show main:<path>` / `git log master` on a bare clone.

## Creating a Worktree for Development

```bash
TASK_ID="$ISTOTA_TASK_ID"
SLUG="add-auth"                                  # short description, lowercase, hyphens
BRANCH="{BOT_DIR}/${TASK_ID}-${SLUG}"
BARE_DIR="$DEVELOPER_REPOS_DIR/namespace/project.git"
WORK_DIR="$DEVELOPER_REPOS_DIR/namespace/project--{BOT_DIR}-${TASK_ID}-${SLUG}"

# Create branch from latest main (or master — check which exists)
git -C "$BARE_DIR" fetch origin
# Assign, then default. `cmd | sed || echo main` takes the exit status of the
# *pipeline*, which is sed's, and sed succeeds on empty input — so a missing
# origin/HEAD gave an empty DEFAULT_BRANCH and `worktree add origin/` rather
# than the intended fallback.
DEFAULT_BRANCH=$(git -C "$BARE_DIR" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null)
DEFAULT_BRANCH="${DEFAULT_BRANCH#origin/}"
DEFAULT_BRANCH="${DEFAULT_BRANCH:-main}"
git -C "$BARE_DIR" worktree add -b "$BRANCH" "$WORK_DIR" "origin/$DEFAULT_BRANCH"
```

All work happens inside `$WORK_DIR`.

## The Job Lifecycle

A coding task runs as a lifecycle, not as a set of habits. The steps below are ordered; each one has a reason to exist and a way to fail. Do not skip ahead because a change looks small — the tier system below is how small changes get less process, not skipping steps.

### 1. Preflight, before a worktree exists

Batch these reads and act on them together. Do not start work that cannot land.

```bash
cd "$BARE_DIR"
git rev-parse --is-bare-repository
git symbolic-ref --quiet --short refs/remotes/origin/HEAD   # -> origin/main or origin/master
git fetch origin --prune
```

- No repository, or the fetch fails: stop and report. Do not clone something else and carry on.
- The base branch is whatever `symbolic-ref` reports, stripped of `origin/`. Never assume `main` — plenty of repositories are still on `master`, and a worktree branched from a base that does not exist is the single most common way this dies.

### 2. Create the worktree, then read back what was made

Create it as described under "Creating a Worktree for Development". Then read back what actually exists and use those values for the rest of the run:

```bash
cd "$WORK_DIR"
git rev-parse --show-toplevel   # the worktree path
git branch --show-current       # the branch name
```

Refer to those two values from here on. Nothing later should hardcode the branch name you intended to create; a push or an MR against a branch name that does not exist fails in a way that reads like an API problem and is not.

### 3. Make the worktree runnable, do not baseline it

A fresh worktree has the tracked files and nothing else.

- **Install only the stack the task touches.** A repository with a Python service and a frontend has an install for each, and a task that only touches one has no use for the other. If the work reaches the other stack later, install it then.
- **Never share a `node_modules` or a `.venv` between worktrees.** Vite, Vitest and esbuild resolve plugins and their native binaries through the real path of `node_modules`, so a borrowed tree fails at transform time and surfaces as dozens of unrelated red suites. Each worktree gets its own.
- **Copy the gitignored files the stack needs to run** — `.env`, local config, test fixtures kept out of git. Never print their contents.
- **Prove it can run tests, cheaply.** A collection step (`pytest --collect-only -q`, one small component test), not a full suite. The base branch was green when it was last committed, so a full pass here re-confirms what the last commit already established. What is unknown is whether *this worktree* can run anything.

If the collection step fails, that is the environment and not the code. Fix the setup and retry. If it fails in a way you cannot explain, stop and report rather than starting work on a worktree you cannot run.

### 4. Understand before changing

Before writing any code, read enough of the codebase to understand the existing patterns:

- **Read `CLAUDE.md`, `AGENTS.md`, and any `.claude/rules/` files** in the repository — these carry project-specific conventions and architecture notes that must be followed.
- **Read existing code** that does something similar to what you are implementing. Match the naming conventions, error handling patterns, env var names and module structure already in use. Never guess — grep for how other modules solve the same problem.
- **Check how the module integrates** with the rest of the system. If you are adding a new skill, plugin or module, look at how existing ones are wired in (env vars, config, imports, tests) and copy the established pattern.

Reading existing patterns is the cheapest step here and skipping it is the most common source of bugs. Five minutes reading saves an hour of debugging.

### 5. Pick a change tier, and say which

The tier decides how much process the rest of the change gets. Pick it before writing code and state it in your report.

**Boundary surfaces.** Any change touching one of these is Full tier regardless of size: authn/authz, secrets and credentials, money or billing, schema migrations, deletion and other destructive paths, external API contracts and payload shapes, concurrency and locking, anything crossing a network, anything running as root or over ssh on a remote host.

- **Fast** — under about 30 changed lines, one or two files, no boundary surface. Implement, add or extend one test, run the affected tests, run the suite once, commit. No pre-written failing test, and no review.
- **Standard** — the default. Tests written alongside the implementation, full suite green before the commit, review before the MR.
- **Full** — any boundary surface, a diff over about 150 lines, or anything you would not want to be wrong about. Failing test first, full suite before and after, review with both agents.

**Escalate, never downgrade.** Move up a tier the moment any of these happens: the suite goes red in a way you did not predict, you need to read a third file to understand the change, or you find a boundary surface you had not counted. Say that you escalated and why. Never move down mid-change, and never pick Fast to avoid work you already suspect is needed.

### 6. Implement, and verify as you go

- **Edit files in the worktree**, never in the bare clone.
- **Write the test first in exactly three cases**: reproducing a reported bug, where the failing test is what proves the fix; pure logic whose semantics are tricky enough that the assertion is the real spec; and any Full-tier change. In all three, run it and confirm it fails for the reason you expect.
- **Everywhere else, write the test and the implementation together and run once.** Do not spend a separate run confirming red. Check by reading instead: an assertion that could have passed against the pre-change code is vacuous, so rewrite it.
- **Breadth**: the happy path, the specific edge case that motivated the change, and one integration test through the real seam. Not an exhaustive edge-case sweep. Integration tests are the highest-value layer — high enough to prove the system works, low enough to debug when they break. Avoid mocks; where unavoidable, mock at a system boundary, never per collaborator.
- **Verify integration points.** If the change adds env vars, config fields, CLI commands or dependencies, check that every consumer and producer is updated together. A new env var is useless if the code reading it uses a different name than the code setting it. Adding a package to `pyproject.toml` or `package.json` is not enough — run the install and commit the lockfile.
- **Keep metadata in step.** If you change a module's purpose, update its descriptions, docstrings and config manifests to match.

### 7. The verification budget

Test and lint output is the largest single source of wasted context. Spend it deliberately.

- **Failure-only output.** Use the runner's own quiet flags: `pytest -q --no-header`, `vitest --reporter=dot`, `go test -failfast`.
- **Bail on the first failure while iterating** (`-x`, `--bail=1`). One real failure beats forty cascading ones. Drop the flag for the final full run.
- **One command, one output.** Chain lint, typecheck and tests into a single invocation rather than three.
- **Affected tests during the loop, the full suite once at the end of the work.** Not after every fix and not before every commit.
- **For the full pass, use the project's own entry point** — `npm run check`, `make check`, `just check`, `tox`, whatever the repository already has. Read the `package.json` scripts or the Makefile once and use what is there. If there is no single command, chain the linters, the type checker and the tests yourself. Do not write a wrapper script to avoid doing that; a second entry point drifts from the real one and hides which step failed.
- **In a multi-stack repository, cover the stacks the branch touched.** Untouched code cannot break, with one exception: anything crossing between the stacks — an API payload shape, a serialized schema, a shared fixture — is a boundary surface and pulls the other stack's tests into scope whether or not you edited its files. Say which stacks the pass covered, so a partial run never reads as a whole one.
- **Never re-read a file you just wrote.** The edit would have failed loudly if it had not applied.

### 8. Commit

Commit in coherent steps rather than one lump. The `commit` companion carries the message format, what lands alongside a commit, and the scrub rules — follow it rather than improvising.

**Commit before you review.** The review resolves a commit range and reads it with `git diff`, `git log` and `git show`; uncommitted work appears in none of those, so a review run against a dirty worktree reviews an empty diff and comes back clean for the wrong reason. Everything you want reviewed has to be committed first.

### 9. Review before landing

Unless the change is Fast tier, run a review after the work's full pass and after the commits exist, but before the branch is pushed. See the `code_review` companion for the command and for how to read what comes back.

The review is part of the lifecycle rather than optional diligence, because this workflow has no separate owning process that would run one. Fix every must-fix. Fix every high you agree with, and report any you decline as a decision, with the reason — a declined finding is a judgement call to be surfaced, not an omission to be quiet about. Fixes land as their own commits on the same branch; do not amend a commit the review already read.

If the review is unavailable — the CLI is not configured, the brain is degraded, the call cap is reached, no reviewer returned a usable answer — that is a state of the environment, not of the diff. It comes back `skipped`. Land the work and report it as unreviewed, naming the reason. If the review *errors*, something is wrong with the request itself — a bad range, a path outside the allowed roots — and it is yours to correct: report it and do not open the MR.

### 10. Land

Push the branch and open a merge request or pull request, following the platform sections below. **Landing is an MR or a PR, not a merge.** Merge to the default branch only when the task text explicitly asks for it.

### 11. The abort path

Whenever a step above says stop: stop, change nothing further, and leave the worktree and branch exactly as they are. They hold the work. Report what failed with the command output, the worktree path and branch name, what state the base branch is in, and what you would do next.

Never delete a worktree whose work did not land.

### 12. Report

Report in this shape every time, so the room gets a consistent block:

```
<repo>/<branch> — <task>

Tier: <Fast | Standard | Full>, <why — boundary surface, size, or default>
Worked: <what changed, one or two sentences>
Tests: <result of the final full pass, which stacks it covered; N added>
Review: <counts by severity and what you did about them> | <skipped, why> | <not run, Fast tier>
Landed: <MR/PR URL> | <why not>
Worktree: <path, left in place>

Deferred: <anything you did not take on, and why it is separate — one line each>
```

Omit `Deferred` when there is nothing in it.

## GitLab: Pushing and Creating a Merge Request

Run these from inside `$WORK_DIR` — both CLIs read the repository from the worktree's `origin` remote.

```bash
cd "$WORK_DIR"

# glab has no `--jq`: a field read is `-F json` piped to a parser. pipefail so a
# glab that fails after printing is not masked by the python3 that parsed it.
set -o pipefail

# REQUIRED: confirm the project before creating anything (pre-submission check 1).
# `||` fails closed: a glab error aborts rather than becoming an empty string.
RESOLVED=$(glab repo view -F json | python3 -c 'import json,sys; print(json.load(sys.stdin)["path_with_namespace"])') || {
    echo "ERROR: could not read origin's project path from glab. Aborting."
    exit 1
}
if [ "$RESOLVED" != "namespace/project" ]; then
    echo "ERROR: origin resolves to '$RESOLVED', not 'namespace/project'. Aborting."
    exit 1
fi

git push -u origin "$BRANCH"

# The reviewer variable is absent entirely unless the operator configured one,
# and `--reviewer ""` is an error rather than a no-op — so build the flag
# rather than interpolating it.
REVIEWER_ARGS=""
[ -n "${GITLAB_REVIEWER_ID:-}" ] && REVIEWER_ARGS="--reviewer $GITLAB_REVIEWER_ID"

glab mr create \
    --source-branch "$BRANCH" \
    --target-branch "$DEFAULT_BRANCH" \
    --title "Add user authentication" \
    --description "Implements JWT auth. Created by {BOT_NAME} task $TASK_ID." \
    --remove-source-branch \
    $REVIEWER_ARGS \
    --yes
```

`--yes` skips the confirmation prompt; without it the command waits for a terminal that is not there. **`--reviewer` takes a username**, despite the variable's name. If the configured value is numeric, create the MR without the flag and tell the user their `developer.gitlab_reviewer_id` needs to be the reviewer's username.

Then verify it exists, and capture the id for later steps (pre-submission check 2):

```bash
set -o pipefail
# Abort rather than carry an empty id: `glab mr merge ""` acts on whatever MR the
# current branch has.
MR_IID=$(glab mr view -F json | python3 -c 'import json,sys; print(json.load(sys.stdin)["iid"])') || {
    echo "ERROR: could not read the merge request id. Aborting."
    exit 1
}
glab mr view -F json | python3 -c 'import json,sys; d=json.load(sys.stdin); print("!%s %s" % (d["iid"], d["web_url"]))'
```

## GitHub: Pushing and Creating a Pull Request

```bash
cd "$WORK_DIR"

# REQUIRED: confirm the repository before creating anything.
# Substitute the owner/repo the user actually asked for.
RESOLVED=$(gh repo view --json nameWithOwner -q .nameWithOwner)
if [ "$RESOLVED" != "owner/repo" ]; then
    echo "ERROR: origin resolves to '$RESOLVED', not 'owner/repo'. Aborting."
    exit 1
fi

git push -u origin "$BRANCH"

REVIEWER_ARGS=""
[ -n "${GITHUB_REVIEWER:-}" ] && REVIEWER_ARGS="--reviewer $GITHUB_REVIEWER"

gh pr create \
    --head "$BRANCH" \
    --base "$DEFAULT_BRANCH" \
    --title "Add user authentication" \
    --body "Implements JWT auth. Created by {BOT_NAME} task $TASK_ID." \
    $REVIEWER_ARGS
```

`--reviewer` requests the review as part of creating the pull request — this is the whole reviewer step, not a follow-up call.

Then verify, and capture the number for later steps:

```bash
PR_NUMBER=$(gh pr view --json number -q .number)
gh pr view --json number,url,state -q '"#\(.number) \(.state) \(.url)"'
```

A one-line description keeps the shell quoting simple. For a real multi-paragraph body, write it to a file first and pass `gh pr create --body-file BODY.md`; on the GitLab side pass the file's contents with `glab mr create --description "$(cat BODY.md)"`. Do not build a multi-paragraph string inline — the escaping is where these recipes break.

## Follow-Up Work on Existing MRs/PRs

To push additional commits to an open MR/PR, reuse the existing worktree:

```bash
WORK_DIR="$DEVELOPER_REPOS_DIR/namespace/project--istota-42-add-auth"
cd "$WORK_DIR"
# Make changes, then stage the specific files you touched — never `git add -A`.
# See the `commit` companion for the message format and the scrub rules.
git status --short
git add src/validation.py tests/test_validation.py
git commit -m "Address review feedback: add input validation"
git push origin HEAD
```

## GitLab: Listing and Merging MRs

```bash
set -o pipefail
# $MR_IID came from an earlier block, and a block is its own shell. Re-check it:
# `glab mr merge ""` merges the current branch's MR rather than refusing.
[ -n "${MR_IID:-}" ] || { echo "ERROR: MR_IID is empty. Re-read it before merging."; exit 1; }
glab mr list                       # open MRs, human-readable
glab mr list -F json | python3 -c 'import json,sys; print("\n".join("!%s %s" % (m["iid"], m["title"]) for m in json.load(sys.stdin)) or "(none open)")'
glab mr view "$MR_IID"             # description, discussions, pipeline state
glab mr diff "$MR_IID"

glab mr merge "$MR_IID" --yes
```

Merge options: `--squash`, `--rebase`, `--remove-source-branch`. `--yes` is required in a non-interactive context.

**This may queue rather than merge.** When a pipeline is running, glab enables auto-merge by default and still exits 0 — so the MR is scheduled behind CI, not merged. Pass `--auto-merge=false` if you mean now, and read the command's output before reporting a merge as done.

## GitHub: Listing and Merging PRs

```bash
gh pr list                         # open PRs, human-readable
gh pr list --json number,title -q '.[] | "#\(.number) \(.title)"'
gh pr view "$PR_NUMBER"
gh pr diff "$PR_NUMBER"

gh pr merge "$PR_NUMBER" --squash --delete-branch
```

Merge methods: `--merge`, `--squash`, `--rebase`. Whether the bot may merge at all is a forge-side branch-protection question — if the merge is refused, report it rather than looking for another way to land the change.

## Watching CI

This is the loop that makes a bot useful on a real repository: push, see what broke, fix it, push again.

```bash
# GitHub
gh pr checks                       # one line per check
gh run list --branch "$BRANCH" --limit 5
RUN_ID=$(gh run list --branch "$BRANCH" --limit 1 --json databaseId -q '.[0].databaseId')
gh run view "$RUN_ID" --log-failed  # only the failing steps

# GitLab
glab ci status                     # current branch's pipeline
glab ci list --ref "$BRANCH"
set -o pipefail
PIPELINE_ID=$(glab ci list --ref "$BRANCH" --per-page 1 -F json | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])') || {
    echo "No pipeline for $BRANCH yet, or glab could not read one. Re-run shortly."
    exit 1
}
glab ci get -p "$PIPELINE_ID"
```

`gh pr checks` exits 0 when everything passed, 8 when checks are still pending, and non-zero otherwise — so treat 8 as "come back later", not as a failure.

`gh run view --log-failed` is the one to reach for — it prints only the failing steps, where `--log` prints the whole run and will bury the transcript.

**`gh run download` is not available.** Artifact downloads redirect to a per-request Azure Blob Storage shard, and the only network-allowlist entry that would cover it opens all of Azure Blob Storage to this sandbox. Logs carry what a fix needs; artifacts are not worth that trade. Do not try to route around it.

## Cleanup After Merge

```bash
BARE_DIR="$DEVELOPER_REPOS_DIR/namespace/project.git"
WORK_DIR="$DEVELOPER_REPOS_DIR/namespace/project--istota-42-add-auth"
git -C "$BARE_DIR" worktree remove "$WORK_DIR"
git -C "$BARE_DIR" branch -d "istota/42-add-auth"
```

## Quick Reference

| Task | GitHub | GitLab |
|---|---|---|
| Confirm the repo | `gh repo view --json nameWithOwner` | `glab repo view -F json` |
| Create the change | `gh pr create` | `glab mr create --yes` |
| List open | `gh pr list` | `glab mr list` |
| Read one | `gh pr view N` | `glab mr view N` |
| Its diff | `gh pr diff N` | `glab mr diff N` |
| Comment on it | `gh pr comment N --body "..."` | `glab mr note create N -m "..."` |
| Request a review | `gh pr edit N --add-reviewer USER` | `glab mr update N --reviewer USER` |
| CI state | `gh pr checks` | `glab ci status` |
| Failing CI logs | `gh run view ID --log-failed` | `glab ci get -p PIPELINE_ID` |
| Merge it | `gh pr merge N --squash` | `glab mr merge N --yes` |
| File an issue | `gh issue create --title ... --body ...` | `glab issue create --title ... --description ...` |
| Look up a user | `gh api /users/USERNAME` | `glab api /users?username=USERNAME` |

**The two CLIs do not have the same structured-output surface.** `gh` takes `--json` plus `--jq`/`-q` and filters in-process. `glab` takes `-F json` and nothing else — there is no `--jq` — so a glab field read is `-F json` piped to `python3`, and the pipeline needs `set -o pipefail` for its exit status to mean anything. Newer glab does have `--jq`, which is the trap: the deployment installs glab from the Debian archive on the Ansible path and pins a much newer build in the Docker image, so a recipe here has to run on the older of the two. Check `glab <command> --help` on the deployed binary before using a flag you know from `gh`. Anything not covered here: `gh <command> --help`, `glab <command> --help`.

Check the help before trusting a spelling from memory — the deployed CLIs may be older than the ones these examples were written against, and `glab mr note` in particular was restructured. Newer glab wants `glab mr note create N -m "..."`; older glab wants `glab mr note N -m "..."` with no subcommand. Run `glab mr note --help` and use whichever it shows.

`gh api` and `glab api` reach any read endpoint the token allows. Writes through them are refused — use the verb.

## Error Handling

- **Tests fail**: Fix the code and re-run. Do not push failing tests.
- **Push rejected (non-fast-forward)**: Fetch and rebase onto the target branch:
  ```bash
  cd "$WORK_DIR"
  git fetch origin "$DEFAULT_BRANCH"
  git rebase "origin/$DEFAULT_BRANCH"
  # Resolve conflicts if any, then force-push — YOUR OWN topic branch only.
  git push origin "$BRANCH" --force-with-lease
  ```
  **Never force-push a shared branch.** `--force-with-lease` is permitted on `$BRANCH`, the topic branch you created for this task, and nowhere else. A rejected push to `$DEFAULT_BRANCH` or any branch you did not create means someone else moved it: report it via the abort path and let the user decide. Do not resolve it.
- **MR/PR has merge conflicts**: Rebase the worktree branch onto the latest target and force-push `$BRANCH`, subject to the same restriction.
- **Exit 3, "not permitted by this deployment"**: a refused verb. Stop and tell the user what you were about to do and why you wanted to. Do not reach for `gh api`, a raw `curl`, or the web UI to get the same effect.
- **Exit 4 or 5 from `gh` / `glab`**: the credential path, not your command. Exit 4 means no credential proxy is reachable; exit 5 means the proxy refused or has no token for that forge. Both are deployment problems — report them, and note that only the affected forge is down (a missing GitLab token does not stop `gh`).
- **Exit 2**: a usage error, or one of the retired `github-api` / `gitlab-api` names. Use `gh` / `glab`.
- **Exit 7**: the wrapper is misconfigured (no CLI config directory). A deployment problem — report it.
- **Exit 6**: the real CLI is missing or not executable on this host. Report the path in the message; the operator has to install it.
- **`gh api` write refused**: writes through the raw API are blocked on purpose. There is a verb for it — `gh pr edit`, `gh issue comment`, and so on.
- **Project not found**: Verify the namespace/project or owner/repo path matches exactly (case-sensitive), and that the token's scope covers it. A fine-grained token restricted to a repository list returns 404, not 403, for anything outside it — so "not found" can mean "not granted".
