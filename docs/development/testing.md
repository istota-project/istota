# Testing

Istota uses TDD with pytest and pytest-asyncio. The test suite has ~2,760 tests across 56 files.

## Running tests

```bash
uv run pytest tests/ -v                                # Unit tests
uv run pytest -m integration -v                        # Integration tests
uv run pytest tests/ --cov=istota --cov-report=term-missing  # Coverage
```

Integration tests are deselected by default (configured in `pyproject.toml`). They require a live Nextcloud instance and are marked with `@pytest.mark.integration`.

## Test patterns

**Real SQLite via `tmp_path`**: No database mocking. Tests create real SQLite databases initialized from `schema.sql`. This catches actual SQL issues that mocks would hide.

**`unittest.mock` for external dependencies**: HTTP calls, subprocess invocations, and file system operations outside the test directory are mocked.

**Class-based tests**: Tests are organized in classes grouping related scenarios.

## Shared fixtures (`conftest.py`)

| Fixture | Purpose |
|---|---|
| `db_path` | Initialized SQLite database from schema.sql |
| `db_conn` | Database connection |
| `make_task` | Factory for creating test tasks |
| `make_config` | Factory for creating Config objects |
| `make_user_config` | Factory for creating UserConfig objects |

## Testing skills

Skill loader tests require isolation from bundled skills:

```python
# Always pass both params to isolate from bundled skills
index = load_skill_index(skills_dir, bundled_dir=_empty_bundled(tmp_path), skip_entrypoints=True)
```

Executor tests set `bundled_skills_dir` on the Config object to an empty directory, which automatically triggers `skip_entrypoints`.

## TDD workflow

For new features:

1. Read existing codebase structure and test patterns
2. Write failing tests covering happy path, edge cases, and error handling
3. Run tests to confirm they fail
4. Implement the feature
5. Run tests and iterate until all pass
6. Run linters/type checkers if configured
7. Commit
