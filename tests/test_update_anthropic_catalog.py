"""`scripts/update-anthropic-catalog.py`: build, check and branch, all offline.

The models.dev input is a trimmed recording of the real `api.json`
(`tests/fixtures/model_catalogue/models_dev_api.json`), so every run here reads
a file and nothing touches the network.
"""

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "update-anthropic-catalog.py"
FIXTURES = REPO / "tests" / "fixtures" / "model_catalogue"
MODELS_DEV = FIXTURES / "models_dev_api.json"
OVERRIDES = FIXTURES / "overrides.json"
GOLDEN = FIXTURES / "anthropic_golden.json"


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("update_anthropic_catalog", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _args(output, *extra, overrides=OVERRIDES, source=MODELS_DEV):
    return [
        "--models-dev-file", str(source),
        "--overrides", str(overrides),
        "--output", str(output),
        *extra,
    ]


def _write_overrides(tmp_path, models):
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"models": models}))
    return path


class TestBuild:
    def test_output_equals_the_golden(self, script, tmp_path):
        out = tmp_path / "anthropic.json"
        assert script.main(_args(out)) == 0
        assert out.read_text() == GOLDEN.read_text()

    def test_what_the_golden_shows(self):
        models = {m["id"]: m for m in json.loads(GOLDEN.read_text())["models"]}
        # no tool calling, a dated twin of an undated id, and an excluded id are gone
        assert "claude-no-tools-1" not in models
        assert "claude-sonnet-4-5-20250929" not in models
        assert "claude-sonnet-4-5" not in models
        # overrides patch, and add a model models.dev does not list
        assert models["claude-opus-4-7"]["deprecated"] is True
        assert models["claude-haiku-4-5"]["effort_map"] == {"low": None}
        assert models["claude-opus-6"]["family"] == "opus"
        # families lose the "claude-" prefix; effort levels and price tiers map
        assert models["claude-sonnet-5-5"]["family"] == "sonnet"
        assert models["claude-opus-5-5"]["effort_map"] == {"xhigh": "xhigh", "max": "max"}
        assert models["claude-haiku-5-5"]["price_tiers"][0]["input_tokens_above"] == 100_000
        assert models["claude-haiku-5-5"]["input"] == ["text", "image"]

    def test_unknown_override_field_fails_and_writes_nothing(self, script, tmp_path, capsys):
        overrides = _write_overrides(tmp_path, {"claude-opus-5-5": {"colour": "blue"}})
        out = tmp_path / "anthropic.json"
        assert script.main(_args(out, overrides=overrides)) == 1
        assert not out.exists()
        assert "colour" in capsys.readouterr().err

    def test_a_family_left_empty_fails(self, script, tmp_path, capsys):
        overrides = _write_overrides(tmp_path, {
            "claude-haiku-4-5": {"deprecated": True},
            "claude-haiku-5-5": {"deprecated": True},
        })
        out = tmp_path / "anthropic.json"
        assert script.main(_args(out, overrides=overrides)) == 1
        assert not out.exists()
        assert "haiku" in capsys.readouterr().err

    def test_a_new_model_without_its_limits_fails(self, script, tmp_path):
        overrides = _write_overrides(tmp_path, {"claude-opus-7": {"family": "opus"}})
        assert script.main(_args(tmp_path / "a.json", overrides=overrides)) == 1


class TestCheck:
    def test_current_file_exits_0(self, script, tmp_path):
        out = tmp_path / "anthropic.json"
        shutil.copy(GOLDEN, out)
        assert script.main(_args(out, "--check")) == 0
        assert out.read_text() == GOLDEN.read_text()

    def test_a_difference_exits_2_and_writes_nothing(self, script, tmp_path, capsys):
        out = tmp_path / "anthropic.json"
        stale = json.loads(GOLDEN.read_text())
        stale["models"] = [m for m in stale["models"] if m["id"] != "claude-opus-6"]
        out.write_text(json.dumps(stale))
        before = out.read_text()
        assert script.main(_args(out, "--check")) == 2
        assert out.read_text() == before
        assert "+ claude-opus-6" in capsys.readouterr().out

    def test_a_fetch_failure_exits_1_and_writes_nothing(self, script, tmp_path):
        out = tmp_path / "anthropic.json"
        shutil.copy(GOLDEN, out)
        missing = tmp_path / "no-such-api.json"
        assert script.main(_args(out, "--check", source=missing)) == 1
        assert out.read_text() == GOLDEN.read_text()


def _api_page(*entries, has_more=False):
    return {"data": list(entries), "has_more": has_more, "last_id": entries[-1]["id"]}


API_PAGES = [
    _api_page(
        {"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5", "line": "opus",
         "lifecycle": "active", "created_at": "2026-09-22T00:00:00Z", "max_input_tokens": 900000,
         "max_tokens": None,
         "capabilities": {"effort": {"low": {"supported": False}, "max": {"supported": False}}}},
        {"id": "claude-haiku-5-5", "display_name": "Claude Haiku 5.5", "line": "haiku",
         "lifecycle": "active", "created_at": "2026-10-07T00:00:00Z"},
        has_more=True,
    ),
    _api_page(
        {"id": "claude-sonnet-6", "display_name": "Claude Sonnet 6", "line": "sonnet",
         "lifecycle": "active", "created_at": "2026-10-20T00:00:00Z", "max_input_tokens": 1000000,
         "max_tokens": 128000, "capabilities": {"thinking": {"supported": True}}},
    ),
]


class TestTheListingKey:
    """`--listing-key-file`: the model set and capabilities from Anthropic's
    list, through istota's own parser and merge; prices from models.dev."""

    def _expect(self, out):
        models = {m["id"]: m for m in json.loads(out.read_text())["models"]}
        assert set(models) == {"claude-opus-5-5", "claude-haiku-5-5", "claude-sonnet-6"}
        opus = models["claude-opus-5-5"]
        assert opus["context_window"] == 900000
        assert opus["max_output_tokens"] == 128000  # null in the API: models.dev's stays
        assert opus["input_price_per_mtok"] == 4.0
        assert opus["effort_map"] == {"xhigh": "xhigh", "max": None, "low": None}
        sonnet = models["claude-sonnet-6"]
        assert sonnet["family"] == "sonnet"
        assert sonnet["input_price_per_mtok"] == 0.0
        assert sonnet["reasoning"] is True

    def test_the_recorded_pages(self, script, tmp_path):
        pages = tmp_path / "pages.json"
        pages.write_text(json.dumps(API_PAGES))
        out = tmp_path / "anthropic.json"
        assert script.main(_args(
            out, "--anthropic-models-file", str(pages), overrides=tmp_path / "none.json",
        )) == 0
        self._expect(out)

    def test_the_key_comes_from_the_file(self, script, tmp_path, monkeypatch):
        from istota.llm import discovery

        key = tmp_path / "listing-key"
        key.write_text("LISTING-SENTINEL\n")
        sent = []
        answers = [json.dumps(p).encode() for p in API_PAGES]

        def transport(url, headers, timeout):
            sent.append((url, headers["x-api-key"]))
            return 200, answers.pop(0), {}

        monkeypatch.setattr(discovery, "_anthropic_transport", transport)
        out = tmp_path / "anthropic.json"
        assert script.main(_args(
            out, "--listing-key-file", str(key), overrides=tmp_path / "none.json",
        )) == 0
        assert [k for _url, k in sent] == ["LISTING-SENTINEL", "LISTING-SENTINEL"]
        assert all(url.startswith(discovery.ANTHROPIC_MODELS_URL) for url, _k in sent)
        self._expect(out)

    def test_an_empty_key_file_fails_and_writes_nothing(self, script, tmp_path):
        key = tmp_path / "listing-key"
        key.write_text("")
        out = tmp_path / "anthropic.json"
        assert script.main(_args(out, "--listing-key-file", str(key))) == 1
        assert not out.exists()


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for var, value in {
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
    }.items():
        monkeypatch.setenv(var, value)
    root = tmp_path / "repo"
    data = root / "data"
    data.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    (root / "README").write_text("x\n")
    (data / "anthropic.json").write_text(GOLDEN.read_text())
    _git(root, "add", "README", "data/anthropic.json")
    _git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def recorded(script, monkeypatch):
    calls = []
    real = script.run_git

    def run_git(args, **kw):
        calls.append(list(args))
        return real(args, **kw)

    monkeypatch.setattr(script, "run_git", run_git)
    return calls


class TestWriteBranch:
    def test_commits_only_the_data_file_on_the_branch(self, script, repo, recorded, tmp_path):
        overrides = _write_overrides(tmp_path, {**json.loads(OVERRIDES.read_text())["models"],
                                                "claude-opus-5-5": {"deprecated": True}})
        head = _git(repo, "rev-parse", "HEAD")
        out = repo / "data" / "anthropic.json"
        args = _args(out, "--write-branch", "catalogue-refresh", overrides=overrides)
        assert script.main(args) == 0

        assert _git(repo, "rev-parse", "HEAD") == head
        assert _git(repo, "status", "--porcelain") == ""
        assert out.read_text() == GOLDEN.read_text()
        assert _git(repo, "rev-parse", "catalogue-refresh^") == head
        changed = _git(repo, "diff", "--name-only", head, "catalogue-refresh")
        assert changed == "data/anthropic.json"
        subject = _git(repo, "log", "-1", "--format=%s", "catalogue-refresh")
        assert re.fullmatch(r"llm: refresh the anthropic catalogue \(~claude-opus-5-5\)", subject)
        assert not any("push" in call for call in recorded)

    def test_no_change_creates_no_branch(self, script, repo, recorded):
        out = repo / "data" / "anthropic.json"
        assert script.main(_args(out, "--write-branch", "catalogue-refresh")) == 0
        assert _git(repo, "branch", "--list", "catalogue-refresh") == ""
        assert not any("push" in call or "branch" in call for call in recorded)


def test_release_sh_runs_the_check_and_has_the_bypass():
    text = (REPO / "scripts" / "release.sh").read_text()
    assert "scripts/update-anthropic-catalog.py --check" in text
    assert "--skip-catalog-check) CHECK_CATALOG=0" in text
