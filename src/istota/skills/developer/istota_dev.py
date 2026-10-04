#!/usr/bin/env python3
"""``istota-dev``: the developer skill's repository mechanics, as a program.

Four verbs replace shell recipes the skill body used to ask the model to copy:
``clone`` (the bare clone and its invariants), ``worktree`` (a task worktree),
``show`` (current source from a bare clone) and ``verify-remote`` (the
pre-submission namespace check). Each recipe shipped defects a model retyping
it could make again (ISSUE-125, ISSUE-264, ISSUE-269, ISSUE-291); a program
makes each mistake at most once.

Where it runs. ``setup_env`` copies this file into ``{user_temp_dir}/.developer``
beside the forge wrappers, so it runs as a child of the model's own shell,
inside the sandbox, with the model's own ``git``, credential helper and CONNECT
proxy. It adds no capability the model does not already have. That is why it
is not an ``istota-skill`` verb: a skill CLI runs host-side with the daemon's
network and filesystem view.

Keep it stdlib-only and free of ``istota`` imports: it is run by a bare
``python3`` from a copy outside the package, and ``tests/test_istota_dev.py``
pins both halves of that.

Static inputs come from ``istota-dev.json`` beside the program, written by
``setup_env``, never from the environment. The one per-task input is
``ISTOTA_TASK_ID``.

Every verb prints one JSON object on stdout (``show`` prints the file on
success), git's own output goes to stderr, and the exit code says what
happened:

    0  done
    1  verify-remote: the remote does not match
    2  usage error: bad arguments, missing config, repos_dir mismatch, no task id
    3  stop: a credential in repository config or a remote URL
    4  a git command failed
    5  the repository or worktree is not where the verb expected

No verb prints a credential value. Credential findings name the config key,
with any userinfo inside the key itself redacted.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import traceback
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_USAGE = 2
EXIT_CREDENTIAL = 3
EXIT_GIT = 4
EXIT_MISSING = 5

CONFIG_NAME = "istota-dev.json"
CONFIG_VERSION = 1
FORGES = ("gitlab", "github")

NETWORK_TIMEOUT = 300
LOCAL_TIMEOUT = 60
ERROR_TAIL = 2000

# Forge token prefixes, matched case-sensitively when they appear as a URL's
# username with no password. A copy of `istota.sandbox.git_remote_scrub`'s
# `_TOKEN_PREFIXES`, which this file cannot import; a test holds them equal.
TOKEN_PREFIXES = (
    "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_",
    "glpat-", "gldt-", "glrt-", "glsoat-", "glptt-",
    "glcbt-", "glimt-",
    "ATATT",
    "xoxb-", "xoxp-",
)

# Inherited git variables that outrank `-C`, so a stray one in the model's
# shell would point every call below at a repository nobody named.
_GIT_ENV_DROP = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
)

_USERINFO_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)([^/@\s]*)@")
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$")
_BOT_DIR_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_ORIGIN_REF_RE = re.compile(r"^origin/[A-Za-z0-9._/-]+$")
_OBJECT_ID_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_EXTRAHEADER_RE = re.compile(r"^http\..*extraheader$", re.IGNORECASE)


class Stop(Exception):
    """End the verb with ``code`` and ``payload`` printed as JSON."""

    def __init__(self, code: int, payload: dict) -> None:
        super().__init__(payload.get("error", ""))
        self.code = code
        self.payload = payload


def usage(message: str, **extra: object) -> Stop:
    return Stop(EXIT_USAGE, {"error": message, **extra})


class HelperConfig(NamedTuple):
    repos_dir: Path
    bot_dir: str
    forges: dict  # forge name -> base URL, no trailing slash


class Repo(NamedTuple):
    forge: str | None
    path: str  # namespace/.../project
    bare_dir: Path


class WorktreeRecord(NamedTuple):
    path: str
    branch: str  # full refname, or "" when detached or bare
    bare: bool


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------

def redact(text: str) -> str:
    """``text`` with the userinfo of every ``scheme://…@`` replaced by ``***``."""
    return _USERINFO_RE.sub(r"\1***@", text)


def _userinfo_is_credential(userinfo: str) -> bool:
    if ":" in userinfo:
        return userinfo.split(":", 1)[1] != ""
    return userinfo.startswith(TOKEN_PREFIXES)


def has_credentialed_url(text: str) -> bool:
    """Whether ``text`` holds a ``scheme://`` URL carrying a secret: a non-empty
    password, or a username that is a known forge token."""
    for match in _USERINFO_RE.finditer(text):
        if _userinfo_is_credential(match.group(2)):
            return True
    return False


def config_findings(entries: list[tuple[str, str]]) -> list[str]:
    """The keys among ``entries`` that expose a credential, redacted.

    Three rules, the tripwire the skill body used to carry as awk: a
    credentialed URL in the key (``url.<base>.insteadOf``), any
    ``http.*.extraheader``, and a credentialed URL in the value. Stricter than
    `git_remote_scrub` on extraheaders, which it flags whatever the header is:
    this one only stops, it never rewrites.
    """
    found: list[str] = []
    for key, value in entries:
        if has_credentialed_url(key):
            found.append(redact(key))
        elif _EXTRAHEADER_RE.match(key) or has_credentialed_url(value):
            found.append(key)
    return found


def parse_config_list(data: str) -> list[tuple[str, str]]:
    """Entries from ``git config --list -z``: ``key\\nvalue`` records, NUL-ended.

    The NUL form because a value holding a newline would otherwise split into a
    second record of the writer's choosing.
    """
    entries = []
    for record in data.split("\0"):
        if not record:
            continue
        key, _, value = record.partition("\n")
        entries.append((key, value))
    return entries


def credential_findings(bare_dir: Path) -> list[str]:
    out = git(bare_dir, "config", "--list", "--includes", "-z")
    return config_findings(parse_config_list(out))


def _stop_on_credentials(bare_dir: Path, result: dict | None = None) -> None:
    keys = credential_findings(bare_dir)
    if keys:
        payload = dict(result or {})
        payload.update({
            "error": "credential in repository config; do not work in this "
                     "repository, report these keys as a credential to rotate",
            "keys": keys,
            "bare_dir": str(bare_dir),
        })
        raise Stop(EXIT_CREDENTIAL, payload)


# --------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------

def _git_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def git(cwd: Path | None, *args: str, timeout: int = LOCAL_TIMEOUT,
        check: bool = True) -> str:
    """Stdout of ``git -C cwd args``. Raises :class:`Stop` (exit 4) on failure
    when ``check``; returns ``""`` on failure otherwise."""
    argv = ["git"] + (["-C", str(cwd)] if cwd is not None else []) + list(args)
    try:
        proc = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            errors="replace", timeout=timeout, env=_git_env(),
        )
    except subprocess.TimeoutExpired:
        raise Stop(EXIT_GIT, {"error": f"timed out after {timeout}s",
                              "command": [redact(a) for a in argv]})
    except OSError as exc:
        raise Stop(EXIT_GIT, {"error": f"could not run git: {exc.strerror}",
                              "command": [redact(a) for a in argv]})
    if proc.returncode != 0:
        if not check:
            return ""
        raise Stop(EXIT_GIT, {"error": redact(proc.stderr)[-ERROR_TAIL:],
                              "command": [redact(a) for a in argv]})
    if proc.stderr:
        sys.stderr.write(redact(proc.stderr))
    return proc.stdout


def git_ok(cwd: Path, *args: str) -> bool:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args], stdin=subprocess.DEVNULL,
            capture_output=True, timeout=LOCAL_TIMEOUT, env=_git_env(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def default_branch(bare_dir: Path) -> str:
    """The default branch ``refs/remotes/origin/HEAD`` names, refreshing it
    first when it does not resolve (absent, or dangling after an upstream
    rename)."""
    if not git_ok(bare_dir, "rev-parse", "-q", "--verify", "refs/remotes/origin/HEAD"):
        git(bare_dir, "remote", "set-head", "origin", "-a", timeout=NETWORK_TIMEOUT,
            check=False)
    ref = git(bare_dir, "symbolic-ref", "-q", "refs/remotes/origin/HEAD", check=False).strip()
    prefix = "refs/remotes/origin/"
    if not ref.startswith(prefix) or ref == prefix or not git_ok(
        bare_dir, "rev-parse", "-q", "--verify", ref
    ):
        raise Stop(EXIT_GIT, {"error": "origin has no default branch",
                              "bare_dir": str(bare_dir)})
    return ref[len(prefix):]


def worktree_records(bare_dir: Path) -> list[WorktreeRecord]:
    """Records from ``git worktree list --porcelain -z``.

    The NUL form for the reason `maintenance/worktree_reaper.py` gives: the
    line form does not quote a newline in a path, so one forges a record.
    """
    data = git(bare_dir, "worktree", "list", "--porcelain", "-z")
    records: list[WorktreeRecord] = []
    path: str | None = None
    branch = ""
    bare = False
    for field in data.split("\0") + [""]:
        if field.startswith("worktree "):
            if path is not None:
                records.append(WorktreeRecord(path, branch, bare))
            path, branch, bare = field[len("worktree "):], "", False
        elif field.startswith("branch "):
            branch = field[len("branch "):]
        elif field == "bare":
            bare = True
        elif field == "" and path is not None:
            records.append(WorktreeRecord(path, branch, bare))
            path, branch, bare = None, "", False
    return records


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

def default_config_path() -> Path:
    return Path(os.path.abspath(__file__)).parent / CONFIG_NAME


def load_config(path: Path) -> HelperConfig:
    try:
        raw = json.loads(Path(path).read_text())
    except FileNotFoundError:
        raise usage(f"no config file at {path}; the developer skill writes it at task setup")
    except (OSError, ValueError) as exc:
        raise usage(f"unreadable config file {path}: {type(exc).__name__}")
    if not isinstance(raw, dict) or raw.get("version") != CONFIG_VERSION:
        raise usage(f"config file {path} is not version {CONFIG_VERSION}")

    repos_dir = raw.get("repos_dir")
    if not isinstance(repos_dir, str) or not os.path.isabs(repos_dir):
        raise usage("config file has no absolute repos_dir")
    bot_dir = raw.get("bot_dir")
    if not isinstance(bot_dir, str) or not _BOT_DIR_RE.match(bot_dir):
        raise usage("config file has no valid bot_dir")

    forges: dict = {}
    for name, entry in (raw.get("forges") or {}).items():
        url = entry.get("url") if isinstance(entry, dict) else None
        if name not in FORGES or not isinstance(url, str) or not url:
            raise usage(f"config file has an invalid forge entry {name!r}")
        try:
            parts = urlsplit(url)
        except ValueError:
            raise usage(f"config file has an unparseable {name} URL")
        if "@" in parts.netloc:
            raise Stop(EXIT_CREDENTIAL, {
                "error": f"the configured {name} URL carries userinfo; refusing "
                         "to use it. Report it as a credential to rotate",
            })
        forges[name] = url.rstrip("/")

    env_dir = os.environ.get("DEVELOPER_REPOS_DIR")
    if env_dir and os.path.normpath(env_dir) != os.path.normpath(repos_dir):
        raise usage("DEVELOPER_REPOS_DIR differs from the config file's repos_dir",
                    config_repos_dir=repos_dir, env_repos_dir=env_dir)
    return HelperConfig(Path(repos_dir), bot_dir, forges)


def _validate_repo_path(path: str) -> str:
    components = path.split("/")
    if len(components) < 2:
        raise usage(f"repository must be <namespace>/<project>, got {path!r}")
    for part in components:
        if (not part or part in (".", "..") or part.startswith("-")
                or not _COMPONENT_RE.match(part)):
            raise usage(f"invalid repository path component {part!r} in {path!r}")
    if components[-1].endswith(".git"):
        raise usage(f"name the project without .git, got {path!r}")
    return path


def parse_repo(arg: str, forge: str | None, cfg: HelperConfig,
               need_forge: bool = True) -> Repo:
    """``<forge>:<namespace>/<project>`` or ``<namespace>/<project>`` with
    ``--forge``. With neither, the single configured forge."""
    named = None
    path = arg
    head, sep, tail = arg.partition(":")
    if sep:
        named, path = head, tail
    if named is not None and forge is not None and named != forge:
        raise usage(f"{arg!r} names forge {named!r} but --forge is {forge!r}")
    chosen = named or forge
    if chosen is not None and chosen not in FORGES:
        raise usage(f"unknown forge {chosen!r}; use one of {', '.join(FORGES)}")
    path = _validate_repo_path(path)

    if need_forge:
        if chosen is None:
            if len(cfg.forges) != 1:
                raise usage("say which forge: <forge>:<namespace>/<project> or --forge",
                            configured=sorted(cfg.forges))
            chosen = next(iter(cfg.forges))
        elif chosen not in cfg.forges:
            raise usage(f"forge {chosen!r} is not configured on this deployment",
                        configured=sorted(cfg.forges))

    namespace, _, project = path.rpartition("/")
    bare_dir = cfg.repos_dir / namespace / f"{project}.git"
    return Repo(chosen, path, bare_dir)


def task_id() -> str:
    value = os.environ.get("ISTOTA_TASK_ID", "")
    if not value.isdigit() or int(value) <= 0:
        raise usage("ISTOTA_TASK_ID is missing or not a task id")
    return str(int(value))


# --------------------------------------------------------------------------
# Verbs
# --------------------------------------------------------------------------

def cmd_clone(args: argparse.Namespace, cfg: HelperConfig) -> dict:
    repo = parse_repo(args.repo, args.forge, cfg)
    bare = repo.bare_dir
    fresh = False
    if not bare.exists():
        bare.parent.mkdir(parents=True, exist_ok=True)
        url = f"{cfg.forges[repo.forge]}/{repo.path}.git"
        git(None, "clone", "--bare", url, str(bare), timeout=NETWORK_TIMEOUT)
        git(bare, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        fresh = True

    git(bare, "fetch", "origin", "--prune", timeout=NETWORK_TIMEOUT)
    # ISSUE-291: core.hooksPath is per-clone config; without it a repository's
    # committed hooks never run in the worktrees cut from this clone.
    git(bare, "config", "core.hooksPath", ".githooks")

    default = default_branch(bare)
    # ISSUE-269: `worktree add -b` aborts on a HEAD outside refs/heads/.
    head = git(bare, "symbolic-ref", "-q", "HEAD", check=False).strip()
    if head != f"refs/heads/{default}":
        git(bare, "symbolic-ref", "HEAD", f"refs/heads/{default}")

    # ISSUE-125: clone-day refs/heads/* never move again, so a local `main`
    # silently returns stale source. Every local head is a fossil only on
    # clone day; later refs/heads/ also holds task branches, one of which may
    # be the only copy of its work.
    if fresh:
        heads = git(bare, "for-each-ref", "--format=%(refname)", "refs/heads/").split()
        candidates = [h[len("refs/heads/"):] for h in heads]
    else:
        candidates = [default]
    checked_out = {r.branch for r in worktree_records(bare) if r.branch}
    removed = []
    for name in candidates:
        ref = f"refs/heads/{name}"
        if ref in checked_out or not git_ok(bare, "rev-parse", "-q", "--verify", ref):
            continue
        git(bare, "update-ref", "-d", ref)
        removed.append(name)

    result = {
        "bare_dir": str(bare),
        "default_branch": default,
        "fresh": fresh,
        "fossils_removed": removed,
        "hooks_path": ".githooks",
    }
    _stop_on_credentials(bare, result)
    return result


def _same_path(a: str | Path, b: str | Path) -> bool:
    return os.path.realpath(str(a)) == os.path.realpath(str(b))


def cmd_worktree(args: argparse.Namespace, cfg: HelperConfig) -> dict:
    repo = parse_repo(args.repo, args.forge, cfg, need_forge=False)
    tid = task_id()
    slug = args.slug
    if not _SLUG_RE.match(slug):
        raise usage("slug must be lowercase [a-z0-9-], 1 to 48 characters, "
                    "no leading or trailing '-'", slug=slug)
    base = args.base
    if base is not None and (not _ORIGIN_REF_RE.match(base) or ".." in base):
        raise usage("--base must be origin/<branch>; a bare clone has no "
                    "maintained local branches", base=base)

    bare = repo.bare_dir
    if not bare.is_dir():
        raise Stop(EXIT_MISSING, {"error": f"no bare clone at {bare}",
                                  "hint": "run istota-dev clone first"})

    git(bare, "fetch", "origin", "--prune", timeout=NETWORK_TIMEOUT)
    _stop_on_credentials(bare)

    branch = f"{cfg.bot_dir}/{tid}-{slug}"
    namespace, _, project = repo.path.rpartition("/")
    work_dir = cfg.repos_dir / namespace / f"{project}--{cfg.bot_dir}-{tid}-{slug}"
    if base is None:
        base = f"origin/{default_branch(bare)}"

    existing = False
    if os.path.lexists(work_dir):
        ours = [
            r for r in worktree_records(bare)
            if _same_path(r.path, work_dir) and r.branch == f"refs/heads/{branch}"
        ]
        if not ours or not work_dir.is_dir():
            raise Stop(EXIT_MISSING, {
                "error": f"{work_dir} exists and is not the worktree of {branch}",
                "work_dir": str(work_dir),
            })
        existing = True
    else:
        git(bare, "worktree", "add", "-b", branch, str(work_dir), base)

    agents_file = None
    for name in ("AGENTS.md", "CLAUDE.md"):
        if (work_dir / name).is_file():
            agents_file = name
            break
    return {
        "work_dir": str(work_dir),
        "branch": branch,
        "base": base,
        "existing": existing,
        "agents_file": agents_file,
    }


def cmd_show(args: argparse.Namespace, cfg: HelperConfig) -> None:
    repo = parse_repo(args.repo, args.forge, cfg, need_forge=False)
    ref = args.ref
    if not ((_ORIGIN_REF_RE.match(ref) and ".." not in ref) or _OBJECT_ID_RE.match(ref)):
        raise usage("--ref must be origin/<branch> or an object id; a local "
                    "branch name in a bare clone is a stale clone-day copy", ref=ref)
    if not args.path or args.path.startswith("/"):
        raise usage("path must be relative to the repository root", path=args.path)
    bare = repo.bare_dir
    if not bare.is_dir():
        raise Stop(EXIT_MISSING, {"error": f"no bare clone at {bare}",
                                  "hint": "run istota-dev clone first"})
    git(bare, "fetch", "-q", "origin", timeout=NETWORK_TIMEOUT)
    argv = ["git", "-C", str(bare), "show", f"{ref}:{args.path}"]
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=LOCAL_TIMEOUT, env=_git_env())
    except subprocess.TimeoutExpired:
        raise Stop(EXIT_GIT, {"error": f"timed out after {LOCAL_TIMEOUT}s", "command": argv})
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace")
        raise Stop(EXIT_GIT, {"error": redact(stderr)[-ERROR_TAIL:], "command": argv})
    sys.stdout.flush()
    sys.stdout.buffer.write(proc.stdout)
    sys.stdout.flush()


def _parse_remote(url: str) -> tuple[str, str, str] | None:
    """``(host, path, userinfo)`` of a remote URL, scp-style included."""
    if "://" in url:
        try:
            parts = urlsplit(url)
            host = parts.hostname or ""
        except ValueError:
            return None
        userinfo = parts.netloc.rpartition("@")[0] if "@" in parts.netloc else ""
        return host, parts.path, userinfo
    match = re.match(r"^(?:([^@/:]+)@)?([^:/]+):(.+)$", url)
    if not match:
        return None
    return match.group(2).lower(), "/" + match.group(3), match.group(1) or ""


def cmd_verify_remote(args: argparse.Namespace, cfg: HelperConfig) -> tuple[int, dict]:
    expected = _validate_repo_path(args.expected)
    url = git(Path.cwd(), "remote", "get-url", "origin").strip()
    parsed = _parse_remote(url)
    if parsed is None:
        raise Stop(EXIT_GIT, {"error": "could not parse the origin URL"})
    host, path, userinfo = parsed
    if userinfo and _userinfo_is_credential(userinfo):
        raise Stop(EXIT_CREDENTIAL, {"error": "credential embedded in the origin URL; "
                                              "report it as a credential to rotate"})

    forge = None
    for name, base in cfg.forges.items():
        try:
            base_parts = urlsplit(base)
        except ValueError:
            continue
        if (base_parts.hostname or "") != host:
            continue
        prefix = base_parts.path.rstrip("/")
        if prefix and not path.startswith(prefix + "/"):
            continue
        forge = name
        path = path[len(prefix):]
        break

    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    remote = f"{host}/{path}"
    if forge is not None and path == expected:
        return EXIT_OK, {"remote": remote, "forge": forge}
    return EXIT_MISMATCH, {
        "error": "origin does not match the expected repository",
        "remote": remote,
        "expected": expected,
        "forge": forge,
    }


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # type: ignore[override]
        raise usage(message)


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="istota-dev", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="verb", parser_class=_Parser)

    p = sub.add_parser("clone", help="bare-clone a repository, or bring one up to date")
    p.add_argument("repo")
    p.add_argument("--forge", choices=FORGES)

    p = sub.add_parser("worktree", help="cut this task's worktree")
    p.add_argument("repo")
    p.add_argument("slug")
    p.add_argument("--forge", choices=FORGES)
    p.add_argument("--base")

    p = sub.add_parser("show", help="print current source from a bare clone")
    p.add_argument("repo")
    p.add_argument("path")
    p.add_argument("--ref", default="origin/HEAD")
    p.add_argument("--forge", choices=FORGES)

    p = sub.add_parser("verify-remote", help="check this worktree's origin")
    p.add_argument("expected")
    return parser


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None, *, config_path: Path | None = None) -> int:
    try:
        args = _build_parser().parse_args(argv)
        if args.verb is None:
            raise usage("name a verb: clone, worktree, show, verify-remote")
        cfg = load_config(config_path or default_config_path())
        if args.verb == "clone":
            _emit(cmd_clone(args, cfg))
            return EXIT_OK
        if args.verb == "worktree":
            _emit(cmd_worktree(args, cfg))
            return EXIT_OK
        if args.verb == "show":
            cmd_show(args, cfg)
            return EXIT_OK
        code, payload = cmd_verify_remote(args, cfg)
        _emit(payload)
        return code
    except Stop as stop:
        _emit(stop.payload)
        return stop.code
    except Exception as exc:  # noqa: BLE001 - the contract is JSON on stdout
        traceback.print_exc(file=sys.stderr)
        _emit({"error": f"internal: {type(exc).__name__}"})
        return EXIT_GIT


if __name__ == "__main__":
    sys.exit(main())
