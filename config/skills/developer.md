# Developer Skill — Git & GitLab Workflows

Work in git repositories and manage merge requests on GitLab. Uses bare clones + git worktrees for branch isolation.

## Environment Variables

| Variable | Description |
|---|---|
| `DEVELOPER_REPOS_DIR` | Base directory for repo clones and worktrees |
| `GITLAB_URL` | GitLab instance URL (e.g., `https://gitlab.com`) |
| `GITLAB_DEFAULT_NAMESPACE` | Default GitLab namespace (user/group) for resolving short repo names |
| `GITLAB_REVIEWER_ID` | GitLab user ID to assign as reviewer on new merge requests |
| `GITLAB_API_CMD` | Pre-authenticated wrapper script for GitLab API calls |

Git credentials are configured automatically — clone and push work without manual authentication.

**Namespace resolution**: When the user gives a short repo name (e.g., "nebula" instead of "namespace/nebula"), use `$GITLAB_DEFAULT_NAMESPACE` as the default namespace. Always confirm the resolved path exists via the API before cloning.

**Security**: The GitLab token is embedded in helper scripts and never exposed as an environment variable. Do NOT attempt to read or extract credentials from helper scripts. Use `$GITLAB_API_CMD` for API calls and plain `git` commands for repository operations.

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
    git clone --bare "$GITLAB_URL/namespace/project.git" "$BARE_DIR"
    # Configure fetch to get all branches
    git -C "$BARE_DIR" config remote.origin.fetch "+refs/heads/*:refs/remotes/origin/*"
fi

# Always fetch latest
git -C "$BARE_DIR" fetch origin
```

## Creating a Worktree for Development

```bash
TASK_ID="$ISTOTA_TASK_ID"
SLUG="add-auth"                                  # short description, lowercase, hyphens
BRANCH="{BOT_DIR}/${TASK_ID}-${SLUG}"
BARE_DIR="$DEVELOPER_REPOS_DIR/namespace/project.git"
WORK_DIR="$DEVELOPER_REPOS_DIR/namespace/project--{BOT_DIR}-${TASK_ID}-${SLUG}"

# Create branch from latest main (or master — check which exists)
git -C "$BARE_DIR" fetch origin
DEFAULT_BRANCH=$(git -C "$BARE_DIR" symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||' || echo "main")
git -C "$BARE_DIR" worktree add -b "$BRANCH" "$WORK_DIR" "origin/$DEFAULT_BRANCH"
```

All work happens inside `$WORK_DIR`.

## Development Workflow

1. **Edit files** in the worktree directory
2. **Run the test suite** before committing (check README/CI config for the test command):
   ```bash
   cd "$WORK_DIR"
   # Common patterns:
   make test          # Makefile
   pytest             # Python
   npm test           # Node.js
   go test ./...      # Go
   ```
3. **Commit** with a meaningful message:
   ```bash
   cd "$WORK_DIR"
   git add -A
   git commit -m "Add user authentication middleware

   Implements JWT-based auth with refresh token support.
   Closes #123"
   ```
4. **Check for secrets** before pushing — never commit tokens, passwords, or private keys

## Pushing and Creating a Merge Request

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
PROJECT_ID=$($GITLAB_API_CMD GET "/api/v4/projects/$ENCODED_PATH" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# Create merge request (assign configured reviewer)
$GITLAB_API_CMD POST "/api/v4/projects/$PROJECT_ID/merge_requests" \
    --header "Content-Type: application/json" \
    --data "{
        \"source_branch\": \"$BRANCH\",
        \"target_branch\": \"$DEFAULT_BRANCH\",
        \"title\": \"Add user authentication\",
        \"description\": \"Implements JWT auth.\\n\\nCreated by istota task $TASK_ID.\",
        \"remove_source_branch\": true,
        \"reviewer_ids\": [$GITLAB_REVIEWER_ID]
    }"
```

The response includes `web_url` (link to share) and `iid` (MR number like `!42`).

## Follow-Up Work on Existing MRs

To push additional commits to an open MR, reuse the existing worktree:

```bash
WORK_DIR="$DEVELOPER_REPOS_DIR/namespace/project--istota-42-add-auth"
cd "$WORK_DIR"
# Make changes, commit, push
git add -A
git commit -m "Address review feedback: add input validation"
git push origin HEAD
```

## Listing Open MRs

```bash
$GITLAB_API_CMD GET "/api/v4/projects/$PROJECT_ID/merge_requests?state=opened" \
    | python3 -c "import sys,json; [print(f'!{mr[\"iid\"]} {mr[\"title\"]} ({mr[\"web_url\"]})') for mr in json.load(sys.stdin)]"
```

## Merging a Merge Request

```bash
$GITLAB_API_CMD PUT "/api/v4/projects/$PROJECT_ID/merge_requests/$MR_IID/merge"
```

Options: add `"squash": true` or `"should_remove_source_branch": true` via `--data '{"squash": true}'`.

After merge, clean up the worktree:

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

**Important**: When piping `$GITLAB_API_CMD` output, always redirect to a temp file first, then read:
```bash
$GITLAB_API_CMD GET "/api/v4/projects/$ENCODED_PATH" > /tmp/result.json
PROJECT_ID=$(python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" < /tmp/result.json)
```

## Error Handling

- **Tests fail**: Fix the code and re-run. Do not push failing tests.
- **Push rejected (non-fast-forward)**: Fetch and rebase onto the target branch:
  ```bash
  cd "$WORK_DIR"
  git fetch origin "$DEFAULT_BRANCH"
  git rebase "origin/$DEFAULT_BRANCH"
  # Resolve conflicts if any, then force-push
  git push origin "$BRANCH" --force-with-lease
  ```
- **MR has merge conflicts**: Rebase the worktree branch onto latest target, force-push.
- **Endpoint not allowed**: The API wrapper enforces an allowlist. Deleting and admin actions are blocked.
- **Project not found**: Verify the namespace/project path matches exactly (case-sensitive).
