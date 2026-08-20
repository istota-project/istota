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
| `GITLAB_REVIEWER_ID` | GitLab user ID to assign as reviewer on new merge requests |
| `GITLAB_API_CMD` | Pre-authenticated wrapper script for GitLab API calls |
| `GITHUB_URL` | GitHub instance URL (e.g., `https://github.com`) |
| `GITHUB_DEFAULT_OWNER` | Default GitHub org/user for resolving short repo names |
| `GITHUB_REVIEWER` | GitHub username to request as PR reviewer |
| `GITHUB_API_CMD` | Pre-authenticated wrapper script for GitHub API calls |
| `DEVELOPER_AUTHOR_CREDIT` | Optional text appended to every commit message (e.g., `Co-Authored-By: ...`) |

Git credentials are configured automatically for both platforms — clone and push work without manual authentication.

**Namespace resolution**: When the user gives a short repo name (e.g., "nebula" instead of "namespace/nebula"), use `$GITLAB_DEFAULT_NAMESPACE` or `$GITHUB_DEFAULT_OWNER` as the default namespace/owner depending on the platform. Always confirm the resolved path exists via the API before cloning.

**Security**: Tokens are embedded in helper scripts and never exposed as environment variables. Do NOT attempt to read or extract credentials from helper scripts. Use `$GITLAB_API_CMD` / `$GITHUB_API_CMD` for API calls and plain `git` commands for repository operations.

**Pre-submission checks** (mandatory before every MR/PR):
1. **Namespace verification**: Before creating any MR or PR, extract the resolved namespace/owner from the API response and confirm it matches the intended target. If the user said "submit to `cynium/istota`", verify the project resolves to `cynium`, not some other namespace. Abort and ask the user if there is any mismatch.
2. **Response verification**: After creating an MR/PR, parse the API response to extract the URL and ID. If the response contains an error, treat it as failure. Then query the open MR/PR list to confirm it actually exists before reporting success.
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

if [ ! -d "$BARE_DIR" ]; then
    mkdir -p "$(dirname "$BARE_DIR")"
    # Use $GITLAB_URL or $GITHUB_URL depending on where the repo lives
    git clone --bare "$GITLAB_URL/namespace/project.git" "$BARE_DIR"
    # Configure fetch to track remote branches under refs/remotes/origin/*
    git -C "$BARE_DIR" config remote.origin.fetch "+refs/heads/*:refs/remotes/origin/*"
    git -C "$BARE_DIR" fetch origin

    # Delete the clone-day local heads and repoint HEAD at the remote default.
    # WHY (ISSUE-125): `git clone --bare` populates refs/heads/* once, at clone
    # time, and the remote-tracking refspec above never updates them again — so
    # a local `main`/`master` stays frozen at clone day while origin/main moves
    # on. `git show main:db.py` then silently returns clone-day source. Deleting
    # the fossils turns that silent-wrong into a loud `unknown revision`: you
    # can't act on stale bytes you can't read. Worktree creation is unaffected —
    # it branches from `origin/$DEFAULT_BRANCH` (below), not a local head.
    DEFAULT_BRANCH=$(git -C "$BARE_DIR" remote show origin | sed -n 's/.*HEAD branch: //p')
    git -C "$BARE_DIR" symbolic-ref HEAD "refs/remotes/origin/$DEFAULT_BRANCH"
    # Skip any head currently checked out by a worktree (an istota/<task> branch);
    # only the unused clone-day main/master get dropped.
    CHECKED_OUT=$(git -C "$BARE_DIR" worktree list --porcelain | sed -n 's/^branch refs\/heads\///p')
    for ref in $(git -C "$BARE_DIR" for-each-ref --format='%(refname:short)' refs/heads/); do
        # `update-ref -d`, not `branch -D`: HEAD was just repointed at a
        # remote-tracking ref, and every `branch` subcommand then dies with
        # `fatal: HEAD not found below refs/heads!` before deleting anything.
        # `branch -D` here silently left the clone-day fossils in place.
        echo "$CHECKED_OUT" | grep -qx "$ref" || git -C "$BARE_DIR" update-ref -d "refs/heads/$ref"
    done
fi

# Always fetch latest
git -C "$BARE_DIR" fetch origin
```

### Reading current source from a bare clone

**Invariant: in a bare clone, never name a local branch — always `origin/<branch>`
or `origin/HEAD`.** A local `main`/`master` is a clone-day fossil (deleted by the
setup above, but the habit still bites on an older clone). To read the live tree
in one fetch-then-read step that can't point at a stale ref, use:

```bash
# dev-show <BARE_DIR> <path> — current source from origin/HEAD, always fetched.
git -C "$BARE_DIR" fetch -q origin && git -C "$BARE_DIR" show origin/HEAD:"$path"
```

Use this (or `git -C "$BARE_DIR" log origin/HEAD`, `git -C "$BARE_DIR" show
origin/main:<path>`) for any hand-rolled verification read. Never `git show
main:<path>` / `git log master` against a bare clone.

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

If the review is unavailable — the CLI is not configured, the brain is degraded, the call cap is reached — that is a state of the environment, not of the diff. Land the work and report it as unreviewed, naming the reason. If the review *errors*, something is wrong with the request itself: report it and do not open the MR.

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

Push the branch (git credentials are configured automatically):

```bash
cd "$WORK_DIR"
git push origin "$BRANCH"
```

Create MR via GitLab API:

```bash
# Get project ID from path
PROJECT_PATH="namespace/project"
ENCODED_PATH=$(echo "$PROJECT_PATH" | sed 's|/|%2F|g')
PROJECT_INFO=$($GITLAB_API_CMD GET "/api/v4/projects/$ENCODED_PATH")
PROJECT_ID=$(echo "$PROJECT_INFO" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# REQUIRED: Verify the resolved namespace matches the intended target.
# Extract the namespace from the project info and confirm it is correct
# before creating any MR. Abort and ask the user if it doesn't match.
RESOLVED_NS=$(echo "$PROJECT_INFO" | python3 -c "import sys,json; print(json.load(sys.stdin)['path_with_namespace'].split('/')[0])")
if [ "$RESOLVED_NS" != "namespace" ]; then
    echo "ERROR: Resolved namespace '$RESOLVED_NS' does not match expected 'namespace'. Aborting MR creation."
    exit 1
fi

# Create merge request (assign configured reviewer)
MR_RESPONSE=$($GITLAB_API_CMD POST "/api/v4/projects/$PROJECT_ID/merge_requests" \
    --header "Content-Type: application/json" \
    --data "{
        \"source_branch\": \"$BRANCH\",
        \"target_branch\": \"$DEFAULT_BRANCH\",
        \"title\": \"Add user authentication\",
        \"description\": \"Implements JWT auth.\\n\\nCreated by istota task $TASK_ID.\",
        \"remove_source_branch\": true,
        \"reviewer_ids\": [$GITLAB_REVIEWER_ID]
    }")

# REQUIRED: Verify the MR was actually created. Parse the response for
# web_url and iid. If the response contains "error" or "message" fields
# instead, treat it as a failure.
MR_URL=$(echo "$MR_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('web_url',''))")
MR_IID=$(echo "$MR_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('iid',''))")
if [ -z "$MR_URL" ] || [ -z "$MR_IID" ]; then
    echo "ERROR: MR creation failed. Response: $MR_RESPONSE"
    exit 1
fi
echo "MR created: !$MR_IID — $MR_URL"

# Verify the MR appears in the open MRs list
$GITLAB_API_CMD GET "/api/v4/projects/$PROJECT_ID/merge_requests?state=opened" \
    | python3 -c "import sys,json; mrs=json.load(sys.stdin); match=[m for m in mrs if m['iid']==$MR_IID]; assert match, 'MR !$MR_IID not found in open MRs'"
```

## GitHub: Pushing and Creating a Pull Request

Push the branch:

```bash
cd "$WORK_DIR"
git push origin "$BRANCH"
```

Create PR via GitHub API:

```bash
OWNER="myorg"  # or $GITHUB_DEFAULT_OWNER
REPO="project"

# REQUIRED: Verify the owner/repo resolves to the intended target before creating a PR.
REPO_INFO=$($GITHUB_API_CMD GET "/repos/$OWNER/$REPO")
RESOLVED_OWNER=$(echo "$REPO_INFO" | python3 -c "import sys,json; print(json.load(sys.stdin)['owner']['login'])")
if [ "$RESOLVED_OWNER" != "$OWNER" ]; then
    echo "ERROR: Resolved owner '$RESOLVED_OWNER' does not match expected '$OWNER'. Aborting PR creation."
    exit 1
fi

PR_RESPONSE=$($GITHUB_API_CMD POST "/repos/$OWNER/$REPO/pulls" \
    --header "Content-Type: application/json" \
    --data "{
        \"head\": \"$BRANCH\",
        \"base\": \"$DEFAULT_BRANCH\",
        \"title\": \"Add user authentication\",
        \"body\": \"Implements JWT auth.\\n\\nCreated by istota task $TASK_ID.\"
    }")

# REQUIRED: Verify the PR was actually created. Parse the response for
# html_url and number. If missing, treat as failure.
PR_URL=$(echo "$PR_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('html_url',''))")
PR_NUMBER=$(echo "$PR_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('number',''))")
if [ -z "$PR_URL" ] || [ -z "$PR_NUMBER" ]; then
    echo "ERROR: PR creation failed. Response: $PR_RESPONSE"
    exit 1
fi
echo "PR created: #$PR_NUMBER — $PR_URL"
```

Request a reviewer:

```bash
$GITHUB_API_CMD POST "/repos/$OWNER/$REPO/pulls/$PR_NUMBER/reviews" \
    --header "Content-Type: application/json" \
    --data "{\"reviewers\": [\"$GITHUB_REVIEWER\"]}"
```

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
# List open MRs
$GITLAB_API_CMD GET "/api/v4/projects/$PROJECT_ID/merge_requests?state=opened" \
    | python3 -c "import sys,json; [print(f'!{mr[\"iid\"]} {mr[\"title\"]} ({mr[\"web_url\"]})') for mr in json.load(sys.stdin)]"

# Merge an MR
$GITLAB_API_CMD PUT "/api/v4/projects/$PROJECT_ID/merge_requests/$MR_IID/merge"
```

Options: add `"squash": true` or `"should_remove_source_branch": true` via `--data '{"squash": true}'`.

## GitHub: Listing and Merging PRs

```bash
OWNER="myorg"
REPO="project"

# List open PRs
$GITHUB_API_CMD GET "/repos/$OWNER/$REPO/pulls?state=open" \
    | python3 -c "import sys,json; [print(f'#{pr[\"number\"]} {pr[\"title\"]} ({pr[\"html_url\"]})') for pr in json.load(sys.stdin)]"

# Merge a PR
$GITHUB_API_CMD PUT "/repos/$OWNER/$REPO/pulls/$PR_NUMBER/merge" \
    --header "Content-Type: application/json" \
    --data '{"merge_method": "squash"}'
```

Merge methods: `"merge"`, `"squash"`, or `"rebase"`.

## Cleanup After Merge

```bash
BARE_DIR="$DEVELOPER_REPOS_DIR/namespace/project.git"
WORK_DIR="$DEVELOPER_REPOS_DIR/namespace/project--istota-42-add-auth"
git -C "$BARE_DIR" worktree remove "$WORK_DIR"
git -C "$BARE_DIR" branch -d "istota/42-add-auth"
```

## GitLab API Quick Reference

Use `$GITLAB_API_CMD METHOD ENDPOINT [extra curl args]` for all API calls.

The API wrapper enforces an endpoint allowlist — only the operations below are permitted. Deleting and admin operations are blocked.

| Action | Method | Endpoint |
|---|---|---|
| Get project by path | GET | `/api/v4/projects/:encoded_path` |
| List branches | GET | `/api/v4/projects/:id/repository/branches` |
| List open MRs | GET | `/api/v4/projects/:id/merge_requests?state=opened` |
| Get single MR | GET | `/api/v4/projects/:id/merge_requests/:iid` |
| Create MR | POST | `/api/v4/projects/:id/merge_requests` |
| Merge MR | PUT | `/api/v4/projects/:id/merge_requests/:iid/merge` |
| Add MR comment | POST | `/api/v4/projects/:id/merge_requests/:iid/notes` |
| Create issue | POST | `/api/v4/projects/:id/issues` |
| Add issue comment | POST | `/api/v4/projects/:id/issues/:iid/notes` |
| Look up user by username | GET | `/api/v4/users?username=:name` |

## GitHub API Quick Reference

Use `$GITHUB_API_CMD METHOD ENDPOINT [extra curl args]` for all API calls.

The API wrapper enforces an endpoint allowlist — only the operations below are permitted. Deleting and admin operations are blocked.

| Action | Method | Endpoint |
|---|---|---|
| Get repo | GET | `/repos/:owner/:repo` |
| List branches | GET | `/repos/:owner/:repo/branches` |
| List open PRs | GET | `/repos/:owner/:repo/pulls?state=open` |
| Get single PR | GET | `/repos/:owner/:repo/pulls/:number` |
| Create PR | POST | `/repos/:owner/:repo/pulls` |
| Merge PR | PUT | `/repos/:owner/:repo/pulls/:number/merge` |
| Update PR | PATCH | `/repos/:owner/:repo/pulls/:number` |
| Add PR comment | POST | `/repos/:owner/:repo/pulls/:number/comments` |
| Request PR review | POST | `/repos/:owner/:repo/pulls/:number/reviews` |
| Create issue | POST | `/repos/:owner/:repo/issues` |
| Add issue comment | POST | `/repos/:owner/:repo/issues/:number/comments` |
| Update issue | PATCH | `/repos/:owner/:repo/issues/:number` |
| Search code | GET | `/search/code?q=...` |
| Look up user | GET | `/users/:username` |
| List org repos | GET | `/orgs/:org/repos` |

**Important**: When piping API wrapper output, always redirect to a temp file first, then read:
```bash
$GITHUB_API_CMD GET "/repos/$OWNER/$REPO" > /tmp/result.json
DEFAULT_BRANCH=$(python3 -c "import sys,json; print(json.load(sys.stdin)['default_branch'])" < /tmp/result.json)
```

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
- **Endpoint not allowed**: The API wrappers enforce an allowlist. Deleting and admin actions are blocked.
- **Project not found**: Verify the namespace/project or owner/repo path matches exactly (case-sensitive).
