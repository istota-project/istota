"""Keep credentials out of the git configs under ``DEVELOPER_REPOS_DIR``.

``repos_dir`` is bound read-write into the admin sandbox (``build_bwrap_cmd``,
the "Developer repos (RW)" bind) and every worktree cut from a bare clone
inherits that clone's remotes. ``git remote -v``, ``git config --list``,
``git remote show origin`` and a number of ordinary failure messages print
configured URLs in full — so a token embedded in one is read by the model as a
matter of routine and travels from there into a task result, a transcript and
the memory index. That routes around the entire credential architecture the
rest of this package implements: proxy injection, ``_split_credential_env``,
and the per-host credential helper :mod:`istota.skills.developer` registers
through ``GIT_CONFIG_KEY_*``.

Nothing in this package writes such a config — the skill clones from a bare
``$GITLAB_URL`` and authenticates through the helper. This is the guard for one
that arrived some other way: a clone made by hand, a repo copied in, a
``set-url`` typed during an incident. :func:`scrub_and_report` runs on the
developer skill's setup path, before the model can read anything.

**Git is the parser, not this module.** Every question about what a repository
is configured to do is asked of ``git config`` rather than answered by reading
the file, because the file is not the whole story:

- ``include.path`` / ``includeIf`` pull in values that live in another file
  entirely. Reading with ``--includes`` finds them, and ``--show-origin`` says
  which file to write the correction back to.
- A value may contain a literal newline, so output is read ``-z`` and split on
  NUL. Splitting on lines lets a crafted value forge a second config entry.
- ``remote.<name>.url`` is multi-valued. Rewrites pass ``--fixed-value`` so
  only the offending value is touched and clean siblings survive.
- A credential can sit in a *key*, not a value: ``url.<base>.insteadOf``
  rewrites every fetch and push through ``<base>`` while ``remote -v`` shows
  something clean. Those are removed by section.
- ``extensions.worktreeConfig`` puts per-worktree overrides in
  ``<gitdir>/worktrees/<id>/config.worktree``, which the repository's own
  config does not include.

**What counts as a credential.** A userinfo password, in any URL, in a key or
a value; a userinfo *username* matching a known forge-token prefix
(``https://ghp_…@github.com/…`` is a documented, working auth form); and any
``http.*.extraheader``, which is how a job token is injected without touching
a URL at all. A bare high-entropy username with no recognised prefix is
knowingly **not** detected — telling one from a legitimate
``https://myname@host/repo`` needs a guess, and guessing wrong rewrites a
working remote. The rule is otherwise structural, so it has no false positives:
``git@github.com:o/r`` and ``https://oauth2@host/x`` carry no secret and are
left alone. This mirrors what the deploy side settled on in ISSUE-258 — see the
``urlsplit('password')`` assert in ``deploy/ansible/tasks/main.yml``.

Best-effort by construction. It sits on a task setup path, so no function here
raises: a repository it cannot read or rewrite is reported, not thrown. A
finding it could not remove comes back with ``removed=False`` rather than being
dropped, because a caller must never mistake a failed sweep for a clean one.

stdlib-only leaf: imported by the developer skill's ``setup_env`` hook, and
usable from a maintenance one-liner.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger("istota.git_remote_scrub")

# How far below `repos_dir` to look. The documented layout puts a bare clone at
# `<namespace>/<project>.git` with its worktrees alongside, so everything of
# interest is at depth 2; the extra levels cover a repo filed one directory
# deeper by hand. Exceeding it is logged — a sweep that silently declines to
# look is indistinguishable from one that looked and found nothing.
_MAX_DEPTH = 4

_GIT_TIMEOUT = 15

# Files that make a directory a git directory, bare or `.git`. Cheaper than
# asking `git rev-parse --resolve-git-dir`, which is a subprocess per candidate
# directory on a path that runs for every task.
_GIT_DIR_MARKERS = ("HEAD", "config", "objects")

# Keys whose *value* is a URL that may carry userinfo. `submodule.<name>.url`
# is included because it is a plain remote URL with the same rewrite, and a
# pasted token lands there as readily as in `remote.origin.url`.
_URL_VALUE_KEY = re.compile(
    r"^(?:remote\.(?P<name>.+)\.(?:url|pushurl)|submodule\..+\.url)$", re.IGNORECASE
)

# An extraheader worth removing. A repository may legitimately set a non-auth
# header (a trace id, a routing hint); removing that breaks it and reports a
# credential that never existed.
_AUTH_HEADER = re.compile(
    r"^\s*(?:authorization|proxy-authorization|private-token|job-token|"
    r"x-[\w-]*token)\s*:", re.IGNORECASE
)

# `url.<base>.insteadOf` / `pushInsteadOf` — the credential rides in <base>,
# which is part of the key. Removing the value alone leaves it in the section
# header, so these are removed by section.
_URL_REWRITE_KEY = re.compile(r"^url\.(?P<base>.+)\.(?:insteadof|pushinsteadof)$", re.IGNORECASE)

# `http.<url>.extraheader` (and the bare `http.extraheader`) carry an
# Authorization header directly. This is how Azure DevOps and GitLab CI inject
# a job token, and it never appears in a remote URL.
_EXTRAHEADER_KEY = re.compile(r"^http\.(?:.+\.)?extraheader$", re.IGNORECASE)

# Userinfo usernames that are protocol boilerplate rather than a secret. A URL
# whose username is one of these and which has no password carries nothing.
_SAFE_USERNAMES = frozenset({
    "git", "oauth2", "oauth", "token", "x-access-token", "x-oauth-basic",
    "x-token-auth", "gitlab-ci-token", "api", "user", "username",
})

# Forge token prefixes. Matching a known prefix is decidable; guessing at a
# high-entropy username is not, so only these are treated as a secret when they
# appear as the userinfo username. Matched case-sensitively, because no forge
# issues these in mixed case and a case-insensitive test turns an ordinary
# username like `GHP_deploybot` into a false positive that strips a working
# remote. Deliberately not exhaustive — a token shape not listed here is a
# miss, not a crash, and the password rule catches the far commoner form.
_TOKEN_PREFIXES = (
    "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_",   # GitHub
    "glpat-", "gldt-", "glrt-", "glsoat-", "glptt-",          # GitLab
    "glcbt-", "glimt-",
    "ATATT",                                                  # Atlassian
    "xoxb-", "xoxp-",                                         # Slack
)


class ScrubFinding(NamedTuple):
    """One credential found. Deliberately carries no secret.

    ``host`` is here so an operator can tell which token to rotate without the
    value ever being logged. ``removed`` is ``False`` when the credential was
    found but could not be taken out — a locked config during a concurrent
    sweep, a read-only filesystem — so that a caller cannot read a failure as
    a clean result.
    """

    repo: Path
    config: Path
    key: str
    host: str
    removed: bool


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

def url_credential(url: str) -> str | None:
    """The secret embedded in ``url``'s userinfo, or ``None``.

    A non-empty password counts. So does a username matching a known forge
    token prefix, which is a real auth form (``https://ghp_…@github.com/o/r``)
    that carries no password at all.

    An empty password (``https://user:@host/x``) does not, matching the
    deploy-side ``| length == 0`` rule: there is no secret to remove, and
    rewriting would be churn.
    """
    try:
        parts = urlsplit(url)
        password = parts.password
        username = parts.username
    except ValueError:
        # A URL git accepts but urlsplit rejects (an unclosed IPv6 literal, for
        # one). Unparseable is not evidence of a credential, and this must not
        # raise on the setup path.
        return None

    if password:
        return password
    if username and _looks_like_token(username):
        return username
    return None


def _looks_like_token(username: str) -> bool:
    if username.lower() in _SAFE_USERNAMES:
        return False
    return username.startswith(_TOKEN_PREFIXES)


def strip_url_credential(url: str) -> str:
    """``url`` with its whole userinfo removed, or ``url`` unchanged.

    The username goes too, not just the password. Leaving ``oauth2@`` behind
    pins git to a username the credential helper does not necessarily answer
    with, which would convert a fixed leak into an authentication failure.

    The host is taken verbatim from the netloc rather than reassembled from
    ``hostname`` and ``port``: the latter lowercases the host and strips the
    brackets off an IPv6 literal, turning ``https://u:t@[::1]:8443/x`` into the
    unparseable ``https://::1:8443/x``.

    Returns the input byte-identical when there is nothing to strip, so a
    caller can use inequality as "something changed".
    """
    if url_credential(url) is None:
        return url
    try:
        parts = urlsplit(url)
        _, at, hostpart = parts.netloc.rpartition("@")
        if not at:
            return url
        return urlunsplit((parts.scheme, hostpart, parts.path, parts.query, parts.fragment))
    except ValueError:
        return url


def _first_url(text: str) -> str | None:
    """The first ``scheme://…`` substring in ``text``, credential or not.

    Used to name the host of an ``http.<url>.extraheader`` — that URL never
    carries a credential itself, so :func:`_first_credentialed_url` always
    returns ``None`` for it and the operator is told "host unknown" in exactly
    the case where the key is the only record of which host it was.
    """
    for candidate in re.findall(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s]+", text):
        trimmed = candidate
        while trimmed:
            try:
                if urlsplit(trimmed).hostname:
                    return trimmed
            except ValueError:
                pass
            if "." not in trimmed:
                break
            trimmed = trimmed.rsplit(".", 1)[0]
    return None


def _first_credentialed_url(text: str) -> str | None:
    """The first ``scheme://…`` substring in ``text`` carrying a credential.

    Used on config *keys*, where the URL is embedded in a longer string
    (``url.https://tok@host/.insteadof``) rather than being the whole value.
    """
    for candidate in re.findall(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s]+", text):
        # A key ends in `.insteadof`; trim trailing key components so the
        # parse sees the URL rather than the whole config key.
        while candidate:
            if url_credential(candidate) is not None:
                return candidate
            if "." not in candidate:
                break
            candidate = candidate.rsplit(".", 1)[0]
    return None


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def _is_git_dir(path: Path) -> bool:
    """Structural test, not a name test.

    ``git clone --bare <url> myrepo`` produces a bare repository whose
    directory is not named ``*.git``, and the threat model here is explicitly a
    repository made by hand.

    Checked strictly, because finding a git directory *prunes the walk*.
    ``repos_dir`` is bound read-write into the sandbox, so three empty files
    named ``HEAD``, ``config`` and ``objects`` are something the model can
    create — and a loose test would let that hide every repository beneath it.
    """
    try:
        if not (path / "config").is_file() or not (path / "objects").is_dir():
            return False
        head = (path / "HEAD").read_bytes()[:64]
    except OSError:
        return False
    # A real HEAD is a symref or a raw object id, never empty.
    return head.startswith(b"ref:") or bool(re.match(rb"^[0-9a-fA-F]{40,64}\s*$", head))


def _is_under(path: Path, root: Path) -> bool:
    """Whether ``path`` is strictly inside ``root``. Both already resolved."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return path != root


def _is_worktree_pointer(path: Path) -> bool:
    """Whether ``path`` is a linked worktree's ``.git`` file rather than any
    old file that happens to be called that."""
    try:
        return path.read_bytes()[:8].startswith(b"gitdir:")
    except OSError:
        return False


def find_git_dirs(
    root: Path,
    max_depth: int = _MAX_DEPTH,
    skip: Iterable[Path | str] = (),
) -> list[Path]:
    """Every git directory under ``root``, including ``root`` itself.

    Covers a bare clone (``<project>.git/`` or any other name) and an ordinary
    one (``<project>/.git/``). A *linked worktree* has a ``.git`` file rather
    than a directory; its remotes live in the bare clone's config, which is
    found separately, and its overrides in ``config.worktree``, which
    :func:`config_files_for` picks up from the git directory.

    Prunes at every repository boundary. Inside a repository a ``config`` file
    is not necessarily the repository's own (a bare clone holds
    ``modules/*/config`` for submodules), and a worktree is a full source
    checkout — ``repos_dir`` holds one per active task, and walking into them
    is thousands of wasted stat calls on a path that runs for every task.

    ``skip`` prunes named subtrees outright, matched on the resolved path. The
    caller that needs it is ``security.sandbox_cache_dir``, whose documented
    home is inside ``repos_dir`` (ISSUE-319): uv's ``archive-v0`` is one
    directory per unpacked wheel, and while ``max_depth`` stops the walk from
    descending into them it still lists and lstats every one. Measured at 25 ms
    per sweep over 4,500 cache directories — small, and the cost that actually
    matters is the ``not descending past depth`` line the walk then logs on
    every task, which reads as thousands of directories going unswept for
    credentials when none of them is a repository or ever will be.
    """
    try:
        root = Path(root).resolve()
        if not root.is_dir():
            return []
    except OSError:
        return []

    found: list[Path] = []
    if _is_git_dir(root):
        return [root]

    # Strictly *under* the root, never the root itself and never above it.
    # `skip` reaches here as an operator's raw `security.sandbox_cache_dir`, and
    # a value equal to or above `developer.repos_dir` would prune the walk at
    # its first step: `find_git_dirs` returns nothing, `scrub_remotes` reports a
    # clean sweep, and the ISSUE-270 credential scrub plus the whole worktree
    # reaper are silently switched off by a config typo. `build_bwrap_cmd`
    # refuses such a value, but neither caller here consults that predicate —
    # they read the key straight from config, so the check belongs here.
    pruned: set[Path] = set()
    for entry in skip:
        try:
            resolved = Path(entry).resolve()
        except OSError:
            continue
        if resolved == root or not _is_under(resolved, root):
            logger.warning(
                "git_remote_scrub: refusing to prune %s from the sweep of %s — "
                "it is not strictly inside it, and pruning it would skip "
                "repositories rather than a cache", resolved, root,
            )
            continue
        pruned.add(resolved)

    def _on_error(exc: OSError) -> None:
        logger.debug("git_remote_scrub: skipping %s (%s)", getattr(exc, "filename", "?"), exc)

    for dirpath, dirnames, filenames in os.walk(root, onerror=_on_error, followlinks=False):
        here = Path(dirpath)
        depth = len(here.relative_to(root).parts)

        if pruned and here in pruned:
            # No `resolve()` per directory: `root` is already resolved, `os.walk`
            # runs with `followlinks=False`, and a symlinked child is removed
            # from `dirnames` below rather than descended into — so `here` can
            # carry no symlink component and already equals its own realpath.
            dirnames[:] = []
            continue

        if _is_git_dir(here):
            found.append(here)
            dirnames[:] = []
            continue

        # An ordinary clone: the git directory is `.git` beneath a checkout.
        if ".git" in dirnames and _is_git_dir(here / ".git"):
            found.append(here / ".git")
            dirnames[:] = []
            continue
        # A linked worktree. Nothing below it holds config of its own — but
        # only prune on a `.git` file that really is one, since an arbitrary
        # file of that name would otherwise hide every repository below it.
        if ".git" in filenames and _is_worktree_pointer(here / ".git"):
            dirnames[:] = []
            continue

        # `os.walk` never yields a symlinked directory as a dirpath while
        # followlinks is off, so a repo symlinked into repos_dir would be
        # invisible. Check each one directly instead of walking through it —
        # descending is what risks a loop, looking does not.
        for name in list(dirnames):
            child = here / name
            if not child.is_symlink():
                continue
            dirnames.remove(name)
            try:
                target = child.resolve()
            except OSError:
                continue
            if _is_git_dir(target):
                found.append(target)
            elif _is_git_dir(target / ".git"):
                found.append(target / ".git")

        if depth >= max_depth:
            if dirnames:
                logger.info(
                    "git_remote_scrub: not descending past depth %d at %s; "
                    "%d subdirectories were not swept for credentials",
                    max_depth, here, len(dirnames),
                )
            dirnames[:] = []

    return sorted(set(found))


def config_files_for(git_dir: Path) -> list[Path]:
    """The config files that apply to ``git_dir``.

    The repository's own config, plus one per linked worktree when
    ``extensions.worktreeConfig`` is on. A worktree config is not included by
    the repository config, so reading the latter never reveals it — and
    ``git remote -v`` inside that worktree prints whatever it says.
    """
    files: list[Path] = []
    main = git_dir / "config"
    if main.is_file():
        files.append(main)
    try:
        worktrees = sorted((git_dir / "worktrees").iterdir())
    except OSError:
        return files
    for entry in worktrees:
        candidate = entry / "config.worktree"
        if candidate.is_file():
            files.append(candidate)
    return files


# --------------------------------------------------------------------------
# Reading and rewriting
# --------------------------------------------------------------------------

def _git_config(*args: str) -> tuple[int, str]:
    """``(exit_status, stdout)``. Bytes are decoded, never rejected.

    ``text=True`` would raise ``UnicodeDecodeError`` on a config holding one
    non-UTF-8 byte — a ``ValueError``, so neither ``OSError`` nor
    ``SubprocessError`` catches it, and it would abort the whole sweep and
    return the empty list that means "looked, found nothing".
    ``surrogateescape`` round-trips those bytes back through ``os.fsencode``
    when a value is handed to git again, so a rewrite still matches.
    """
    proc = subprocess.run(
        ["git", "config", *args],
        capture_output=True, timeout=_GIT_TIMEOUT,
        # The named file is the whole input; no repository is discovered and no
        # user or system config can redirect what this reads or writes.
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
    )
    return proc.returncode, proc.stdout.decode("utf-8", "surrogateescape")


def _read_entries(config_file: Path) -> list[tuple[Path, str, str]] | None:
    """``(origin_file, key, value)`` for every setting ``config_file`` yields.

    ``--includes`` so a value pulled in from another file is seen, and
    ``--show-origin`` so the correction can be written back to the file that
    actually holds it rather than to the one that included it.

    ``None`` means the config could not be read at all — distinct from an empty
    list, which means it was read and holds nothing.
    """
    try:
        status, stdout = _git_config(
            "--file", str(config_file), "--list", "--includes", "-z", "--show-origin",
        )
    except Exception as exc:  # noqa: BLE001 — one bad config must not end the sweep
        logger.warning("git_remote_scrub: cannot read %s: %s", config_file, exc)
        return None
    if status != 0:
        logger.warning(
            "git_remote_scrub: cannot read %s (git exit %d); it was not swept "
            "for credentials", config_file, status,
        )
        return None

    # `--show-origin -z` emits `<origin>NUL<key>NL<value>NUL`, so records
    # alternate. Splitting on newlines instead would let a value containing one
    # forge an entry that was never in the file.
    records = [r for r in stdout.split("\0") if r]
    entries: list[tuple[Path, str, str]] = []
    for origin, keyvalue in zip(records[::2], records[1::2]):
        key, _, value = keyvalue.partition("\n")
        path = origin[len("file:"):] if origin.startswith("file:") else origin
        entries.append((Path(path), key, value))
    return entries


def _rewrite(origin: Path, key: str, old: str, new: str) -> bool:
    """Replace exactly the ``old`` value of ``key``. True when it is gone.

    ``--fixed-value`` confines the change to that one value: ``remote.*.url``
    is multi-valued, and a bare ``--replace-all`` collapses a clean sibling URL
    into the rewritten one.
    """
    return _run_write("--file", str(origin), "--replace-all", "--fixed-value", key, new, old)


def _remove_section(origin: Path, section: str) -> bool:
    return _run_write("--file", str(origin), "--remove-section", section)


def _unset(origin: Path, key: str, value: str) -> bool:
    return _run_write("--file", str(origin), "--unset-all", "--fixed-value", key, value)


def _run_write(*args: str) -> bool:
    try:
        status, _ = _git_config(*args)
    except Exception as exc:  # noqa: BLE001 — a setup-path guard must not fail the task
        logger.warning("git_remote_scrub: rewrite failed (%s)", exc)
        return False
    if status != 0:
        # Exit 255 with a lock error is what two overlapping sweeps look like.
        logger.warning("git_remote_scrub: rewrite failed (git exit %d)", status)
        return False
    return True


def _writable(origin: Path, root: Path) -> bool:
    """Whether the sweep may rewrite ``origin``.

    ``repos_dir`` is bound read-write into the sandbox, so its contents are
    model-controlled. A planted ``[include] path = /anywhere`` — or a symlinked
    repository — makes ``git config --includes`` report a value whose origin is
    any file on the host that git will parse, and rewriting that would give the
    model a daemon-privileged write at a path of its choosing. Detection may
    range that far; correction may not. Anything outside the resolved root is
    reported with ``removed=False`` instead.
    """
    try:
        origin.resolve().relative_to(root)
        return True
    except (OSError, ValueError):
        return False


def _host_of(url: str) -> str:
    try:
        return urlsplit(strip_url_credential(url)).hostname or ""
    except ValueError:
        return ""


def scrub_config(config_file: Path, repo: Path, root: Path) -> list[ScrubFinding]:
    """Remove every credential ``config_file`` exposes. Never raises.

    ``root`` bounds where a correction may be *written* — see :func:`_writable`.
    """
    entries = _read_entries(config_file)
    if entries is None:
        # Unreadable. Report it as an unremoved finding rather than as silence,
        # so a caller cannot read "could not look" as "nothing there".
        return [ScrubFinding(repo=repo, config=config_file, key="<unreadable>",
                             host="", removed=False)]

    findings: list[ScrubFinding] = []
    # Keyed on the file as well as the section: the same `url.<base>` can be
    # defined in the config and again in a file it includes, and deduping on
    # the name alone removed one and dropped the other without a finding.
    removed_sections: set[tuple[Path, str]] = set()

    for origin, key, value in entries:
        # 1. A credential in the key: `url.<base>.insteadOf`. `remote -v` shows
        #    a clean URL while every fetch is rewritten through <base>.
        rewrite = _URL_REWRITE_KEY.match(key)
        if rewrite is not None:
            base = rewrite.group("base")
            if url_credential(base) is not None:
                section = f"url.{base}"
                already = (origin, section) in removed_sections
                removed_sections.add((origin, section))
                findings.append(ScrubFinding(
                    repo=repo, config=origin, key=key, host=_host_of(base),
                    removed=already or (
                        _writable(origin, root) and _remove_section(origin, section)
                    ),
                ))
            continue

        # 2. An Authorization header, which never appears in a URL at all.
        #    Gated on the header *name*: a repository may legitimately set a
        #    non-auth header, and removing it would break it while telling the
        #    operator to rotate a credential that never existed.
        if _EXTRAHEADER_KEY.match(key) and value.strip():
            host = _host_of(_first_url(key) or "")
            if _AUTH_HEADER.match(value):
                findings.append(ScrubFinding(
                    repo=repo, config=origin, key=key, host=host,
                    removed=_writable(origin, root) and _unset(origin, key, value),
                ))
            continue

        # 3. A credential in a URL-valued setting, the ordinary case.
        if _URL_VALUE_KEY.match(key):
            if url_credential(value) is not None:
                clean = strip_url_credential(value)
                findings.append(ScrubFinding(
                    repo=repo, config=origin, key=key, host=_host_of(value),
                    removed=clean != value and _writable(origin, root)
                    and _rewrite(origin, key, value, clean),
                ))
                continue
            # The value is not a single URL but has one buried in it, which is
            # what a value carrying a literal newline looks like. It is not a
            # working remote, so there is nothing to preserve by rewriting —
            # drop it rather than leaving the token live and merely reported.
            if _first_credentialed_url(value) is not None:
                findings.append(ScrubFinding(
                    repo=repo, config=origin, key=key,
                    host=_host_of(_first_credentialed_url(value) or ""),
                    removed=_writable(origin, root) and _unset(origin, key, value),
                ))
                continue

        # 4. Anything else that embeds a credentialed URL in its key or value.
        #    Reported rather than rewritten: the right correction depends on
        #    what the setting means, and a guess could break the repository
        #    or, worse, look like it worked.
        embedded = _first_credentialed_url(key) or _first_credentialed_url(value)
        if embedded is not None:
            findings.append(ScrubFinding(
                repo=repo, config=origin, key=key, host=_host_of(embedded),
                removed=False,
            ))

    return findings


def scrub_remotes(root: Path, skip: Iterable[Path | str] = ()) -> list[ScrubFinding]:
    """Remove embedded credentials from every git config under ``root``.

    An empty list means the sweep ran and found nothing. A finding with
    ``removed=False`` means a credential is present and still on disk.

    ``skip`` prunes subtrees from the walk — see :func:`find_git_dirs`.
    """
    try:
        resolved = Path(root).resolve()
    except OSError:
        return []

    findings: list[ScrubFinding] = []
    for git_dir in find_git_dirs(Path(root), skip=skip):
        for config_file in config_files_for(git_dir):
            # Per config, so one repository that blows up in an unforeseen way
            # cannot end the sweep and leave every later repository unswept
            # while the caller reads the result as clean.
            try:
                findings.extend(scrub_config(config_file, git_dir, resolved))
            except Exception:  # noqa: BLE001 — see above
                logger.exception("git_remote_scrub: sweeping %s failed", config_file)
                findings.append(ScrubFinding(
                    repo=git_dir, config=config_file, key="<unreadable>",
                    host="", removed=False,
                ))
    return findings


def scrub_and_report(root: Path, skip: Iterable[Path | str] = ()) -> list[ScrubFinding]:
    """:func:`scrub_remotes`, with a warning per finding. Never raises.

    The warning names the repository, the setting and the host, and never the
    value — an operator needs to know which token to rotate, and printing it
    here would move the credential from a config file into the application log.
    """
    try:
        findings = scrub_remotes(root, skip=skip)
    except Exception:  # noqa: BLE001 — a setup-path guard must not fail the task
        logger.exception("git_remote_scrub: sweep of %s failed", root)
        return []

    for entry in findings:
        if entry.removed:
            logger.warning(
                "Removed an embedded credential from %s in %s (host %s). It was "
                "readable by any task with developer repos bound; treat that "
                "credential as disclosed and rotate it. Git authenticates through "
                "the credential helper for your configured forge — a remote on any "
                "other host will need a helper configured for it.",
                entry.key, entry.config, entry.host or "unknown",
            )
        else:
            logger.warning(
                "An embedded credential in %s in %s (host %s) could NOT be removed "
                "and is still on disk, readable by any task with developer repos "
                "bound. Rotate it and remove it by hand.",
                entry.key, entry.config, entry.host or "unknown",
            )
    return findings
