# Testing

Istota uses TDD with pytest and pytest-asyncio. The test suite has ~10,750 tests across ~280 files.

## Running tests

```bash
uv run pytest tests/                                   # Unit tests
uv run pytest -m integration                           # Integration tests
uv run pytest -m live                                  # Native-brain tests against a real API
uv run pytest tests/ --cov=istota --cov-report=term-missing  # Coverage
```

`addopts` in `pyproject.toml` pins `-n auto`, so the suite runs under pytest-xdist by default. New tests must be order-independent. For a local edit loop, `--testmon -n0` reruns only what your change touched; `-v` is only readable with `-n0`, since xdist interleaves worker output.

Two marker sets are deselected by default (also via `addopts`):

- `@pytest.mark.integration` — needs a live Nextcloud instance.
- `@pytest.mark.live` — native-brain tests that hit a real LLM API, so they need a key and cost money.

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

Three autouse fixtures apply to every test whether you ask for them or not: `_no_network_symbol_lookups` (fails a test that tries to resolve a ticker symbol over the network), `_reset_async_runtime_singletons` (drops the persistent asyncio loop and pooled HTTP client between tests), and `_reset_expunge_warning_latch` (clears the once-per-process IMAP expunge warning).

## Testing skills

Skill loader tests require isolation from bundled skills:

```python
# Pass bundled_dir to isolate from bundled skills
index = load_skill_index(skills_dir, bundled_dir=_empty_bundled(tmp_path))
```

Executor tests set `bundled_skills_dir` on the Config object to an empty directory to isolate from bundled skills.

## TDD workflow

For new features:

1. Read existing codebase structure and test patterns
2. Write failing tests covering happy path, edge cases, and error handling
3. Run tests to confirm they fail
4. Implement the feature
5. Run tests and iterate until all pass
6. Run linters/type checkers if configured
7. Commit
