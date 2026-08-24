"""What the suite deletes from `os.environ` before every test, and why.

ISSUE-301. `tests/conftest.py` reset every process global it knew about and no
part of the environment, so a shell carrying real istota config changed the
suite's answers: `NTFY_SERVER_URL` made three ntfy tests read the operator's
server instead of `DEFAULT_SERVER`, `CALDAV_URL` and `NC_URL` passed straight
through `_execute_command_task` into tests asserting they were absent,
`ISTOTA_SANDBOXED` made the skill client take its fail-closed branch, and
`HTTP_PROXY` routed nineteen loopback stub servers at a proxy that answered 405.
Thirty of the thirty-two failures on the deployment host were this, and none of
them was about the code.

That environment is not exotic. It is what a task sandbox, a cron `command` job
and an operator shell on the server all carry by design — so the suite was
unrunnable exactly where istota runs.

The policy lives here rather than in `conftest.py` so it can be asserted as a
table (`tests/test_env_isolation.py`) without importing a conftest.

Closed by default, in three rules, none of which is a list somebody has to
remember to extend:

1. **`ISTOTA_`** — the whole framework namespace goes.
2. **The credential shape** — any name containing `PASSWORD`, `SECRET`,
   `TOKEN`, `API_KEY` or `PRIVATE_KEY`. The same patterns `build_stripped_env`
   strips, copied rather than imported (see `CREDENTIAL_PATTERNS`).
3. **Whatever a skill manifest declares** — scraped from the `env:` frontmatter
   of every `src/istota/skills/*/skill.md`. This is the rule that matters. The
   obvious fix for ISSUE-301 was a hand-written list of the dozen names that
   had bitten, and a hand-written list grows a hole the next time a skill
   declares a variable — which is how the bug got here. Scraping means a new
   skill's variables are scrubbed the day they are declared.

Plus a short literal set for the handful istota reads that no manifest declares
(`NEXTCLOUD_MOUNT_PATH`, `WHISPER_MAX_MODEL`, …) and for the `GIT_*` variables
that outrank a config file.

One thing is *forced* rather than removed: `NO_PROXY`, so a loopback stub is
reached directly. See `NO_PROXY_VALUE`.

Two things are deliberately *kept*, and both are narrow:

* **The harness's own inputs** (`ISTOTA_TEST*`, `ISTOTA_IMAGE_TAG`,
  `ISTOTA_UPGRADE_*`, `ISTOTA_LINUX_TIER`, …). These match rule 1 and must
  survive it: they are how a person selects a discretionary tier, they are read
  from the ambient shell on purpose, and unlike a skill variable they are
  defined in `tests/` where this file can see them change.
* **Ordinary shell furniture** — `PATH`, `HOME`, `TMPDIR`, `DOCKER_HOST`. None
  matches any rule above; the rules are namespaced rather than a blanket wipe
  precisely so that spawning a subprocess keeps working.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = REPO_ROOT / "src" / "istota" / "skills"

# `executor._CREDENTIAL_ENV_PATTERNS`, copied. Importing it would be the
# obvious thing and costs 666ms on top of what `conftest.py` already imports —
# paid at collection, once per xdist worker, on every `uv run pytest`. So the
# names are duplicated here and
# `test_env_isolation.py::TestTheCredentialPatternsHaveNotDrifted` does the
# import inside a test body, where it is paid once and only when the suite runs
# far enough to check it.
CREDENTIAL_PATTERNS = frozenset({
    "PASSWORD", "SECRET", "TOKEN", "API_KEY",
    "APP_PASSWORD", "NC_PASS", "PRIVATE_KEY",
})

# Values the suite sets for itself. Scrubbed with everything else and then set
# back by the fixture, so an ambient `ISTOTA_FEEDS_SKIP_DEFAULT_SEED=0` cannot
# reach a test — the point of the exercise is that the suite decides its own
# environment. Tests that need one of these off clear it in the test body or in
# a fixture they request; both run after the autouse scrub.
SUITE_ENV_DEFAULTS = {
    # Default-off in tests: most feeds tests expect an empty DB. The seed tests
    # in test_feeds_migrate.py clear this explicitly.
    "ISTOTA_FEEDS_SKIP_DEFAULT_SEED": "1",
    # Same pattern for the money default-ledger seed: most money tests expect a
    # clean ledgers/ dir. The seed tests in money/test_migrate.py clear it.
    "ISTOTA_MONEY_SKIP_DEFAULT_SEED": "1",
    # web_app's session middleware fails closed without a signing secret
    # (ISSUE-124). Tests don't configure one, so opt into the random
    # per-process dev secret. The tests asserting the fail-closed behaviour
    # clear this explicitly.
    "ISTOTA_WEB_ALLOW_INSECURE_SESSION": "1",
}

_SCRUB_PREFIXES = ("ISTOTA_",)

# Read from the ambient shell on purpose: each one selects or parameterises a
# discretionary tier (`-m image`, `-m smoke`, `scripts/test-linux.sh`,
# `scripts/test-upgrade.sh`). `ISTOTA_TEST` covers `ISTOTA_TEST_*` and
# `ISTOTA_TESTBED_*` both.
_KEEP_PREFIXES = (
    "ISTOTA_TEST",
    "ISTOTA_UPGRADE_",
)
_KEEP_NAMES = frozenset({
    "ISTOTA_IMAGE_TAG",
    # The golden-regeneration switch, which is a harness input like the tier
    # selectors above but matches neither keep-prefix. Scrubbing it did not
    # merely disable the feature — it made the documented command
    # (`ISTOTA_UPDATE_GOLDEN=1 uv run pytest tests/test_prompt_golden.py -n0`,
    # in AGENTS.md and `.claude/rules/testbed.md`) report the goldens as
    # *failing* while writing nothing, which reads as a real prompt regression.
    # `test_prompt_golden.updating()` is what keeps a stale value from rubber
    # stamping: it takes only affirmative/negative spellings and raises on
    # anything else, so the strict parse is the guard rather than the scrub.
    "ISTOTA_UPDATE_GOLDEN",
    "ISTOTA_DEVBOX_IMAGE_TAG",
    "ISTOTA_LINUX_TIER",
    # Contains TOKEN and so matches the credential rule, and is not one: it
    # silences the huggingface fork warning, and the `ml` tier is where it gets
    # set. The rule is a substring match by design, so an exception belongs
    # here rather than in `CREDENTIAL_PATTERNS`, which is pinned equal to
    # `executor`'s copy.
    "TOKENIZERS_PARALLELISM",
})

# istota reads these and no skill manifest declares them, so nothing above
# catches them. Short by construction: anything a *skill* needs arrives through
# the manifest scrape instead, and belongs there rather than here.
_SCRUB_NAMES = frozenset({
    "NEXTCLOUD_MOUNT_PATH",
    "BROWSER_HOST",
    "BROWSER_API_URL",
    "FEEDS_WORKSPACE",
    "WHISPER_MAX_MODEL",
    "RAM_HEADROOM_MB",
    # `build_stripped_env` sets this for unattended shells (ISSUE-291). An
    # ambient one would make the pre-commit hook tests read the operator's
    # choice rather than the case under test.
    "PRECOMMIT_SCANS_REQUIRED",
    # git reads these at "command line" scope, which outranks every config
    # file — so `GIT_CONFIG_NOSYSTEM` and `GIT_CONFIG_GLOBAL=/dev/null`, which
    # six test files already set when building fixture repositories, do not
    # neutralise them. istota exports them itself: the developer skill
    # registers a per-host credential helper this way, so a suite run from
    # inside a developer-authorized task had an injected helper in every
    # fixture repo, and `test_git_remote_scrub.py` is a file that parses
    # `git config --includes --show-origin` output for a living.
    "GIT_CONFIG_COUNT",
    # `GIT_DIR` makes a git command serve whatever repository the shell was
    # last in, whatever `cwd` says. `testbed/services/gitlab.py` already
    # refuses to pass `os.environ` to git for exactly this reason; the fixture
    # helpers in `tests/` do pass it.
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
})

# `GIT_CONFIG_KEY_0` / `GIT_CONFIG_VALUE_0` / … — the numbered half of the pair
# above, which is why this is a prefix rather than a name.
_SCRUB_PREFIXES_EXTRA = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")

# Loopback is never proxied, so a stub server on 127.0.0.1 is reached directly.
# Set rather than read: an ambient `NO_PROXY` narrower than this one is what
# sent nineteen of the reported failures at a proxy that answered 405.
NO_PROXY_VALUE = "127.0.0.1,localhost,::1,0.0.0.0"
NO_PROXY_NAMES = ("NO_PROXY", "no_proxy")

# Turns the whole fixture off in a child pytest, so the negative control in
# `tests/test_env_isolation.py` can watch the reported tests go red. It starts
# with `ISTOTA_TEST`, so the keep list carries it into the child rather than the
# scrub eating the flag that disables the scrub.
NO_SCRUB_FLAG = "ISTOTA_TEST_NO_ENV_SCRUB"

# `HTTP_PROXY` and friends are deliberately *left alone*. ISSUE-301 preferred
# deleting them, on the reasoning that no test should reach a real network —
# which is true of the default suite and false of four of the seven
# discretionary tiers. `tests/support/upgrade.py::_git` runs git with no
# explicit env from inside a test body, `capture_config` hands `dict(os.environ)`
# to `docker run`, and the `full` tier's first boot fetches Talk and Calendar
# from the Nextcloud app store. On a proxied host — which is the host that
# reported this issue — deleting the proxy would trade nineteen failures for a
# tier that cannot reach anything. Bypassing loopback is what Group B actually
# needed, and it costs the tiers nothing.

# `env: [{"var":"NTFY_TOPIC", ...}, ...]` in a skill.md's YAML frontmatter.
_MANIFEST_VAR_RE = re.compile(r'"var"\s*:\s*"([A-Z][A-Z0-9_]*)"')

_manifest_cache: frozenset[str] | None = None


def manifest_env_names(skills_root: Path | None = None) -> frozenset[str]:
    """Every environment variable name declared by a skill manifest.

    A regex over the frontmatter rather than the real loader: importing
    `istota.skills` star-imports every skill (~190ms), and this runs at conftest
    import on every single pytest invocation. The same reason `forge_bin.py` and
    `git_hardening.py` exist as stdlib-only leaves.

    The cost of the regex being wrong is that the set comes back empty and the
    scrub quietly covers less, so `tests/test_env_isolation.py` asserts a floor
    on its size as well as on specific names.
    """
    global _manifest_cache
    if skills_root is None:
        if _manifest_cache is not None:
            return _manifest_cache
        names = _scrape(SKILLS_ROOT)
        _manifest_cache = names
        return names
    return _scrape(skills_root)


def _scrape(skills_root: Path) -> frozenset[str]:
    names: set[str] = set()
    for manifest in sorted(skills_root.glob("*/skill.md")):
        try:
            text = manifest.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        names.update(_MANIFEST_VAR_RE.findall(text))
    return frozenset(names)


def is_scrubbed(name: str) -> bool:
    """Whether `name` is removed from the environment before every test."""
    if name in _KEEP_NAMES or name.startswith(_KEEP_PREFIXES):
        return False
    if name in _SCRUB_NAMES:
        return True
    if name.startswith(_SCRUB_PREFIXES) or name.startswith(_SCRUB_PREFIXES_EXTRA):
        return True
    if any(pattern in name.upper() for pattern in CREDENTIAL_PATTERNS):
        return True
    return name in manifest_env_names()


def scrubbed_env_names(environ: Mapping[str, str] | Iterable[str]) -> set[str]:
    """The subset of `environ`'s names the scrub removes.

    Takes the mapping rather than reading `os.environ` so the policy is a pure
    function and the table in `tests/test_env_isolation.py` needs no subprocess.
    """
    return {name for name in environ if is_scrubbed(name)}
