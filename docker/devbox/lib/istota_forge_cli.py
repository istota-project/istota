#!/usr/bin/env python3
"""Thin wrapper around the real ``gh`` / ``glab`` binaries.

Installed under both names, ahead of the real binaries on PATH. It fetches a
token from whichever credential proxy the environment has, checks the argv
against a policy, builds a deliberate child environment, and ``execve``s the
real CLI. Everything after the exec is the real CLI.

Two homes, one file. The canonical copy is here; ``docker/devbox/lib/`` holds a
byte-identical copy because Docker cannot COPY from outside its build context.
Keep it stdlib-only and free of ``istota`` imports so the container copy runs
under a bare ``python3`` — ``scripts/sync-devbox-lib.sh`` regenerates it and a
test fails when the two drift. The container copy is staged, not live: the
Dockerfile still ships the curated shims and does not COPY this file yet, and
the ``ISTOTA_CRED_SOCK`` branch below talks to a devbox-proxy action that does
not exist until Stage 4 of the spec.

What the policy is and is not: it stops a *mistaken* ``gh repo delete``. It does
not stop a determined model, which can read the raw token from the credential
socket or out of ``/proc/<pid>/environ`` of the running CLI — everything in the
sandbox shares one uid. The boundary that does the work is the token's own
scope. Do not write code or messages here implying more than that.
"""

from __future__ import annotations

import errno as _errno
import json
import os
import socket
import sys
from urllib.parse import urlsplit

FORGE_GITHUB = "github"
FORGE_GITLAB = "gitlab"
RETIRED = "retired"

# argv[0] basename -> what we are being asked to be.
_ARGV0_MAP = {
    "gh": FORGE_GITHUB,
    "glab": FORGE_GITLAB,
    "github-api": RETIRED,
    "gitlab-api": RETIRED,
}

_TOKEN_VAR = {FORGE_GITHUB: "GITHUB_TOKEN", FORGE_GITLAB: "GITLAB_TOKEN"}

# The devbox-proxy action the ISTOTA_CRED_SOCK branch sends. Named here so a
# test can assert it against devbox_proxy_protocol.ALL_ACTIONS once Stage 4
# adds it there; today it is deliberately absent and that branch is inert.
ACTION_FORGE_TOKEN = "forge_token"

# Exit codes. Anything above these is the real CLI's own status.
EXIT_USAGE = 2
EXIT_DENIED = 3
EXIT_NO_PROXY = 4
EXIT_CREDENTIAL = 5
EXIT_EXEC = 6
EXIT_MISCONFIGURED = 7

# Subcommand-free invocations that must work without a credential proxy. A
# version check is not a forge call, and requiring a socket for one breaks
# every deployment with the proxy switched off.
_META_ARGS = frozenset({"--version", "-v", "--help", "-h", "help", "completion"})

# Write methods on the raw REST passthrough. Compared case-folded.
_WRITE_METHODS = ["post", "put", "patch", "delete"]
# ...and the read methods that make a body-carrying `gh api` legitimate;
# `gh api -X GET -f q=… /search/issues` is a real pattern.
_READ_METHODS = ["get", "head"]
# Any of these turns `gh api` into a POST with no -X anywhere in the argv.
_BODY_FLAGS = ["f", "F", "field", "raw-field", "input"]

# The baseline policy. Held in code rather than config: it is a safety default,
# not a preference. Operators extend it (developer.forge_cli_extra_denied) or
# punch holes in it (developer.forge_cli_permit); both are folded in by
# build_policy() host-side and travel as JSON, because this module cannot read
# the config and an env-carried literal would be model-editable.
#
# Not denied, deliberately: `gh run rerun`, `gh workflow run`, `glab ci run`,
# `glab ci retry`. They start work but destroy nothing, and a developer bot has
# real use for them. `pr merge` / `mr merge` are governed by landing mode and by
# forge-side branch protection; a second weaker gate here would invite an
# operator to skip the real one.
#
# Aliases matter: cobra resolves them before dispatch, so a rule naming only the
# canonical spelling is walked around by typing the alias. `ext`/`extensions`
# for `extension` are gh's; `project` and `pipeline` are glab's for `repo` and
# `ci`. Stage 2a confirms these against the installed binaries (`gh extension
# --help` prints the alias list) — until then they are from documentation.
_BASELINE_PATH_RULES = {
    FORGE_GITHUB: [
        # Credential-revealing and persistence-granting. The whole `auth` tree:
        # `auth token` and `auth status --show-token` print the token, and no
        # auth verb has workflow value when the token arrives by environment.
        ["auth"],
        ["ssh-key", "add"],
        ["ssh-key", "delete"],
        ["gpg-key", "add"],
        ["gpg-key", "delete"],
        ["secret", "set"],
        ["secret", "delete"],
        ["variable", "set"],
        ["variable", "delete"],
        # Destructive.
        ["repo", "delete"],
        ["repo", "archive"],
        ["repo", "rename"],
        ["repo", "edit"],      # can flip visibility to public
        ["release", "delete"],
        ["release", "delete-asset"],
        ["run", "delete"],
        ["cache", "delete"],
        # Guard-evading: an alias or an extension renames a denied verb, and
        # gh expands both before dispatch, where this check no longer runs.
        ["alias"],
        ["extension"],
        ["extensions"],
        ["ext"],
        ["config"],
        # A GraphQL mutation and a query are the same HTTP request; telling
        # them apart means parsing the query body. Deny the door instead.
        ["api", "graphql"],
    ],
    FORGE_GITLAB: [
        ["auth"],
        ["ssh-key", "add"],
        ["ssh-key", "delete"],
        ["variable", "set"],
        ["variable", "delete"],
        ["repo", "delete"],
        ["repo", "archive"],
        ["project", "delete"],     # `project` is glab's alias for `repo`
        ["project", "archive"],
        ["release", "delete"],
        ["ci", "delete"],
        ["pipeline", "delete"],    # `pipeline` is glab's alias for `ci`
        ["alias"],
        ["extension"],
        ["extensions"],
        ["ext"],
        ["config"],
        ["api", "graphql"],
    ],
}

# Flag rules cover what a path rule cannot see: the method on the raw REST
# passthrough. Both spellings, because gh accepts either.
_BASELINE_FLAG_VALUE_RULES = [
    {"path": ["api"], "flag": "method", "in": _WRITE_METHODS},
    {"path": ["api"], "flag": "X", "in": _WRITE_METHODS},
]

# `gh api -f name=x /orgs/o/repos` is a POST with no method flag anywhere —
# gh switches from GET the moment a body parameter appears. A method rule
# cannot see that, so the body flags are their own rule kind, waived when the
# caller has said GET or HEAD outright.
_BASELINE_BODY_FLAG_RULES = [
    {
        "path": ["api"],
        "flags": _BODY_FLAGS,
        "unless_method_in": _READ_METHODS,
        "method_flags": ["method", "X"],
    },
]


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #


def baseline_policy(forge: str) -> dict:
    """The in-code policy for one forge, as the JSON shape on the wire."""
    return {
        "path_rules": [list(r) for r in _BASELINE_PATH_RULES.get(forge, [])],
        "flag_value_rules": [dict(r) for r in _BASELINE_FLAG_VALUE_RULES],
        "body_flag_rules": [dict(r) for r in _BASELINE_BODY_FLAG_RULES],
    }


def _parse_entry(entry: str) -> tuple[str | None, list[str]]:
    """Split ``"gh repo delete"`` into ``("github", ["repo", "delete"])``.

    An entry that does not name a binary applies to both forges. An entry
    naming a *retired* binary yields no words, so the caller can report it
    rather than silently installing a rule that matches nothing.
    """
    words = entry.split()
    if not words:
        return None, []
    scoped = _ARGV0_MAP.get(words[0])
    if scoped in (FORGE_GITHUB, FORGE_GITLAB):
        return scoped, words[1:]
    if scoped == RETIRED:
        return RETIRED, []
    return None, words


def build_policy(
    forge: str,
    extra_denied: list[str] | None = None,
    permit: list[str] | None = None,
) -> dict:
    """Baseline plus operator additions, minus operator permits.

    Called host-side (the developer skill's setup_env hook, and the devbox
    image build) to produce the JSON this module later reads back.
    """
    policy = baseline_policy(forge)
    for entry in extra_denied or []:
        scope, words = _parse_entry(entry)
        if not words or (scope is not None and scope != forge):
            continue
        if words not in policy["path_rules"]:
            policy["path_rules"].append(words)
    for entry in permit or []:
        scope, words = _parse_entry(entry)
        if not words or (scope is not None and scope != forge):
            continue
        if words in policy["path_rules"]:
            policy["path_rules"].remove(words)
    return policy


def unmatched_permits(
    forges: list[str],
    permit: list[str] | None,
    extra_denied: list[str] | None = None,
) -> list[str]:
    """Permit entries that cancel no rule the deployment actually has.

    A hatch that silently stopped matching after a baseline rewording reads
    exactly like a hatch that is still open, so the daemon logs these at
    startup. ``extra_denied`` counts: cancelling one's own addition is a
    legitimate thing to write, and warning about it is how a real warning
    gets ignored.
    """
    added: dict[str | None, list[list[str]]] = {}
    for entry in extra_denied or []:
        scope, words = _parse_entry(entry)
        if words:
            added.setdefault(scope, []).append(words)

    unmatched = []
    for entry in permit or []:
        scope, words = _parse_entry(entry)
        if not words:
            continue
        targets = [scope] if scope else forges
        in_baseline = any(words in _BASELINE_PATH_RULES.get(f, []) for f in targets)
        in_added = any(
            words in added.get(key, [])
            for key in ({scope, None} if scope else set(forges) | {None})
        )
        if not (in_baseline or in_added):
            unmatched.append(entry)
    return unmatched


def _valid_path_rule(rule: object) -> bool:
    return (
        isinstance(rule, list)
        and len(rule) > 0
        and all(isinstance(w, str) and w for w in rule)
    )


def _valid_flag_value_rule(rule: object) -> bool:
    return (
        isinstance(rule, dict)
        and _valid_path_rule(rule.get("path"))
        and isinstance(rule.get("flag"), str)
        and isinstance(rule.get("in"), list)
    )


def _valid_body_flag_rule(rule: object) -> bool:
    return (
        isinstance(rule, dict)
        and _valid_path_rule(rule.get("path"))
        and isinstance(rule.get("flags"), list)
    )


def load_policy(path: str | None, forge: str) -> dict:
    """Read the policy file, falling back to the baseline.

    Fails closed to the baseline, never to an empty or half-typed policy. The
    shape checks are not defensive noise: a hand-written ``"path_rules":
    ["repo delete"]`` (strings, not lists of words) parses as valid JSON and
    then matches nothing, which disables the deny list without a word of
    complaint. Anything that does not validate is treated as absent.
    """
    if not path:
        return baseline_policy(forge)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        section = data[forge]
        if not isinstance(section, dict):
            raise ValueError("policy section is not an object")
        path_rules = section.get("path_rules")
        if not isinstance(path_rules, list) or not path_rules:
            raise ValueError("path_rules missing or empty")
        bad = [r for r in path_rules if not _valid_path_rule(r)]
        if bad:
            raise ValueError(f"{len(bad)} malformed path rule(s), e.g. {bad[0]!r}")
        flag_value_rules = section.get("flag_value_rules", [])
        if not all(_valid_flag_value_rule(r) for r in flag_value_rules):
            raise ValueError("malformed flag_value_rules")
        body_flag_rules = section.get("body_flag_rules", [])
        if not all(_valid_body_flag_rule(r) for r in body_flag_rules):
            raise ValueError("malformed body_flag_rules")
        return {
            "path_rules": [list(r) for r in path_rules],
            "flag_value_rules": [dict(r) for r in flag_value_rules],
            "body_flag_rules": [dict(r) for r in body_flag_rules],
        }
    except Exception as e:
        print(
            f"forge-cli: policy file unusable ({e}); falling back to the "
            f"built-in baseline",
            file=sys.stderr,
        )
        return baseline_policy(forge)


# --------------------------------------------------------------------------- #
# Argument handling
# --------------------------------------------------------------------------- #


def forge_from_argv0(argv0: str) -> str | None:
    """Which forge this invocation is, from the name we were called by."""
    return _ARGV0_MAP.get(os.path.basename(argv0))


def is_meta_invocation(args: list[str]) -> bool:
    """True for invocations that reach no forge and so need no token.

    Only ``args[0]`` is considered: ``gh pr --help`` has already picked a
    subcommand and goes through the normal path.
    """
    return not args or args[0] in _META_ARGS


def normalize_args(args: list[str]) -> tuple[list[list[str]], dict[str, list[str]]]:
    """Split argv into candidate subcommand paths and a flag multimap.

    Two candidate paths, not one, because whether ``--foo bar`` consumes
    ``bar`` depends on ``--foo``'s arity and this module has no flag table.
    Cobra consults one per traversal level; guessing either way is wrong half
    the time, and each wrong guess is a hole:

      ``gh -R o/r repo delete``  — guess "no value" and the path starts at
      ``o/r``, so the ``repo delete`` rule never matches.
      ``gh api --paginate graphql``  — guess "takes a value" and ``graphql``
      is swallowed as ``--paginate``'s argument, so the ``api graphql`` rule
      never matches.

    So both readings are returned and a rule denies if it matches *either*.
    Over-collecting is the safe direction for a deny list, and anchoring at
    index 0 (see ``_path_matches``) is what keeps it from firing on flag
    values: ``gh issue create --label config`` yields ``["issue", "create"]``
    and ``["issue", "create", "config"]``, and the one-word ``config`` rule
    matches neither, because neither *starts* with it.

    Flags are a multimap because a short token is ambiguous the same way:
    ``-XDELETE`` is recorded both as ``X -> "DELETE"`` and as the cluster
    ``X``/``D``/``E``/… with empty values.

    Empty argv entries are dropped, matching cobra's own ``stripFlags``; left
    in, one shifts every path index by one and unmatches every rule.
    """
    swallowed: list[str] = []
    unswallowed: list[str] = []
    flags: dict[str, list[str]] = {}

    def _record(name: str, value: str) -> None:
        flags.setdefault(name, []).append(value)

    def _takes_next(index: int) -> str | None:
        nxt = args[index + 1] if index + 1 < len(args) else None
        if nxt is None or nxt == "" or nxt.startswith("-"):
            return None
        return nxt

    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            break  # everything after is operands
        if tok == "":
            i += 1
            continue
        if tok.startswith("--"):
            body = tok[2:]
            if "=" in body:
                name, _, value = body.partition("=")
                _record(name, value)
                i += 1
                continue
            nxt = _takes_next(i)
            if nxt is not None:
                _record(body, nxt)
                unswallowed.append(nxt)  # the reading where the flag is boolean
                i += 2
                continue
            _record(body, "")
            i += 1
            continue
        if tok.startswith("-") and len(tok) > 1:
            body = tok[1:]
            if len(body) > 1:
                _record(body[0], body[1:])          # attached-value reading
                for ch in body:
                    _record(ch, "")                 # cluster reading
                i += 1
                continue
            nxt = _takes_next(i)
            if nxt is not None:
                _record(body, nxt)
                unswallowed.append(nxt)
                i += 2
                continue
            _record(body, "")
            i += 1
            continue
        swallowed.append(tok)
        unswallowed.append(tok)
        i += 1

    return [swallowed, unswallowed], flags


def _path_matches(paths: list[list[str]], rule: list[str]) -> bool:
    """Anchored at index 0 — a rule names a subcommand, not a substring."""
    if not rule:
        return False
    return any(path[: len(rule)] == rule for path in paths)


def _flag_spelling(name: str) -> str:
    """`-X`, not `--X`: the model reads this back and retypes it."""
    return f"-{name}" if len(name) == 1 else f"--{name}"


def denied_reason(forge: str, args: list[str], policy: dict) -> str | None:
    """The human-readable reason this invocation is refused, or None."""
    paths, flags = normalize_args(args)

    for rule in policy.get("path_rules", []):
        if _path_matches(paths, rule):
            return " ".join(rule)

    for rule in policy.get("flag_value_rules", []):
        if not _path_matches(paths, rule.get("path", [])):
            continue
        name = rule.get("flag", "")
        wanted = [str(v).lower() for v in rule.get("in", [])]
        for value in flags.get(name, []):
            if value.lower() in wanted:
                return f"{' '.join(rule['path'])} {_flag_spelling(name)} {value}"

    for rule in policy.get("body_flag_rules", []):
        if not _path_matches(paths, rule.get("path", [])):
            continue
        declared = [
            v.lower()
            for name in rule.get("method_flags", [])
            for v in flags.get(name, [])
            if v
        ]
        waived = [str(v).lower() for v in rule.get("unless_method_in", [])]
        if declared and all(m in waived for m in declared):
            continue
        for name in rule.get("flags", []):
            if any(v for v in flags.get(name, [])):
                return (
                    f"{' '.join(rule['path'])} {_flag_spelling(name)} "
                    f"(a request body makes this a write)"
                )

    return None


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #

# Matches the devbox wire protocol's own line cap. The peer is trusted, so this
# is about a hung proxy, not about exposure.
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class CredentialError(Exception):
    """The proxy answered, and the answer was not a token."""


class NoProxyError(Exception):
    """No credential proxy is reachable from this environment."""


def _sock_roundtrip(sock_path: str, request: dict) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(30)  # covers connect and every recv
        s.connect(sock_path)
        s.sendall(json.dumps(request).encode("utf-8") + b"\n")
        chunks = b""
        while b"\n" not in chunks:
            got = s.recv(4096)
            if not got:
                break
            chunks += got
            if len(chunks) > _MAX_RESPONSE_BYTES:
                raise CredentialError("credential proxy response exceeds 16 MiB")
    finally:
        s.close()
    if not chunks.strip():
        raise CredentialError("credential proxy closed without answering")
    try:
        return json.loads(chunks.split(b"\n", 1)[0].decode("utf-8"))
    except ValueError as e:
        raise CredentialError(f"unparseable proxy response: {e}") from e


def fetch_token(forge: str, parent_env: dict[str, str]) -> str:
    """Ask whichever proxy this environment has for the forge token.

    Sandbox: the skill proxy, same request the generated credential-fetch
    helper makes. Devbox: the devbox proxy's forge_token action, which Stage 4
    adds — until then that branch reaches a proxy that answers
    ``unknown_action`` and the call exits 5. An ambient GH_TOKEN is never used
    as a fallback; that would mean something upstream failed to strip it, and
    inheriting it would hide the failure.
    """
    skill_sock = parent_env.get("ISTOTA_SKILL_PROXY_SOCK")
    if skill_sock:
        reply = _sock_roundtrip(
            skill_sock, {"type": "credential", "name": _TOKEN_VAR[forge]},
        )
        if "error" in reply:
            raise CredentialError(str(reply.get("error")))
        value = reply.get("value")
        if not value:
            raise CredentialError(f"{_TOKEN_VAR[forge]} not available")
        return str(value)

    cred_sock = parent_env.get("ISTOTA_CRED_SOCK")
    if cred_sock:
        reply = _sock_roundtrip(
            cred_sock, {"action": ACTION_FORGE_TOKEN, "provider": forge},
        )
        if not reply.get("ok"):
            raise CredentialError(
                str(reply.get("message") or reply.get("error") or "proxy error")
            )
        value = reply.get("token")
        if not value:
            raise CredentialError(f"no token configured for {forge}")
        return str(value)

    raise NoProxyError("no credential proxy available")


# --------------------------------------------------------------------------- #
# Child environment
# --------------------------------------------------------------------------- #

# Carried through verbatim. ISTOTA_SKILL_PROXY_SOCK and ISTOTA_CRED_SOCK are
# load-bearing rather than incidental: `gh pr create` pushes the head branch,
# git reaches for its credential helper, and that helper reads the socket path
# out of its own environment.
_CARRY_EXACT = (
    "HOME", "PATH", "TZ", "LANG", "TMPDIR",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
    "https_proxy", "http_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE",
    "ISTOTA_SKILL_PROXY_SOCK", "ISTOTA_CRED_SOCK",
)
_CARRY_PREFIX = ("LC_",)

# git vars are carried by name, not by a `GIT_` prefix. The prefix would also
# admit GIT_TRACE_CURL (which prints request headers, Authorization among them,
# into the task log and the log channel), GIT_SSH_COMMAND / GIT_ASKPASS /
# GIT_EXTERNAL_DIFF / GIT_PAGER (each runs a chosen command inside a process
# holding the token), and a GIT_CONFIG_COUNT overwrite, which silently
# unregisters the developer skill's credential helper and surfaces as an auth
# failure on push. The identity and config-injection vars below are the ones
# the developer skill's own setup_env sets and git needs.
_CARRY_GIT = (
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE",
    "GIT_CONFIG_COUNT", "GIT_TERMINAL_PROMPT",
)
_CARRY_GIT_PREFIX = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")

# Removed even when the parent has them. Redundant against the allowlist above
# by construction — an invariant a test pins — and kept because the allowlist
# is the sort of thing a later change widens.
_SCRUB = (
    "GH_DEBUG", "GLAB_DEBUG", "DEBUG",
    "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
    "GITLAB_TOKEN", "GITLAB_ACCESS_TOKEN", "OAUTH_TOKEN",
    "ISTOTA_FORGE_POLICY",
    "GH_HOST", "GITLAB_HOST", "GH_CONFIG_DIR", "GLAB_CONFIG_DIR",
    "GIT_TRACE", "GIT_TRACE_CURL", "GIT_TRACE_PACKET", "GIT_CURL_VERBOSE",
    "GIT_TRACE_REDACT", "GIT_TRACE_CURL_NO_DATA",
    "GIT_SSH_COMMAND", "GIT_ASKPASS", "GIT_EXTERNAL_DIFF", "GIT_PAGER",
)


def _hostname(url: str) -> str:
    """Bare hostname from a configured URL, or "" if there isn't one.

    ``urlsplit`` rather than string splitting: it handles bracketed IPv6
    literals, ports, and userinfo containing ``/`` or ``@``, each of which a
    hand-rolled split gets wrong. A wrong answer here is not cosmetic — the
    GH_TOKEN / GH_ENTERPRISE_TOKEN branch keys off it, and picking the wrong
    one leaves every call unauthenticated, which reads like a scope problem.
    """
    if not url:
        return ""
    candidate = url if "://" in url else f"https://{url}"
    try:
        host = urlsplit(candidate).hostname or ""
    except ValueError:
        return ""
    return host.rstrip(".")


def _is_github_com(host: str) -> bool:
    return host == "github.com" or host.endswith(".github.com")


def build_invocation(
    forge: str,
    args: list[str],
    parent_env: dict[str, str],
    token: str | None,
    real_bin: str,
    config_dir: str,
    forge_url: str = "",
) -> tuple[str, list[str], dict[str, str]]:
    """The exact ``(path, argv, env)`` to exec. Pure; ``main`` does the exec.

    PATH is carried unchanged. An earlier design removed the wrapper's own
    directory to prevent recursion, which breaks the devbox — the wrapper lives
    in /usr/local/bin there, next to git-credential-istota (which /etc/gitconfig
    resolves by bare name) and uv. The recursion guard is that ``real_bin`` is
    an absolute path we never look up on PATH.
    """
    env: dict[str, str] = {}
    for key, value in parent_env.items():
        if key in _SCRUB:
            continue
        carried = (
            key in _CARRY_EXACT
            or key in _CARRY_GIT
            or key.startswith(_CARRY_PREFIX)
            or key.startswith(_CARRY_GIT_PREFIX)
        )
        if carried:
            env[key] = value

    env["PAGER"] = "cat"
    env["NO_COLOR"] = "1"

    if forge == FORGE_GITHUB:
        env["GH_CONFIG_DIR"] = config_dir
        env["GH_PAGER"] = "cat"
        env["GH_NO_UPDATE_CHECKER"] = "1"
        env["GH_PROMPT_DISABLED"] = "1"
        host = _hostname(forge_url) or "github.com"
        env["GH_HOST"] = host
        if token:
            # gh resolves auth per host: GH_TOKEN is github.com's, and an
            # enterprise host reads GH_ENTERPRISE_TOKEN instead. Setting the
            # wrong one leaves every call unauthenticated.
            if _is_github_com(host):
                env["GH_TOKEN"] = token
            else:
                env["GH_ENTERPRISE_TOKEN"] = token
    else:
        env["GLAB_CONFIG_DIR"] = config_dir
        # glab reads none of the GH_* knobs. Spellings confirmed in Stage 2a.
        env["GLAB_CHECK_UPDATE"] = "false"
        env["GLAB_SEND_TELEMETRY"] = "0"
        env["NO_PROMPT"] = "1"
        if forge_url:
            # The whole URL, not the hostname: a non-443 port and a subpath
            # install are both supported shapes and a hostname loses them.
            env["GITLAB_HOST"] = forge_url
        if token:
            env["GITLAB_TOKEN"] = token

    return real_bin, [os.path.basename(real_bin), *args], env


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _real_bin(forge: str, env: dict[str, str]) -> str:
    var = "ISTOTA_GH_REAL" if forge == FORGE_GITHUB else "ISTOTA_GLAB_REAL"
    default = "/usr/local/bin/gh" if forge == FORGE_GITHUB else "/usr/local/bin/glab"
    return env.get(var) or default


def _config_dir(forge: str, env: dict[str, str]) -> str:
    var = "ISTOTA_GH_CONFIG_DIR" if forge == FORGE_GITHUB else "ISTOTA_GLAB_CONFIG_DIR"
    return env.get(var, "")


def _forge_url(forge: str, env: dict[str, str]) -> str:
    """The configured forge URL, under a name distinct from what we emit.

    The wrapper writes GH_HOST / GITLAB_HOST into the child; reading the same
    names here would make the input and the output indistinguishable.
    """
    var = "ISTOTA_GH_URL" if forge == FORGE_GITHUB else "ISTOTA_GITLAB_URL"
    return env.get(var, "")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    argv0 = argv[0] if argv else "forge-cli"
    args = argv[1:]
    env = dict(os.environ)

    forge = forge_from_argv0(argv0)
    name = os.path.basename(argv0)

    if forge == RETIRED:
        print(
            f"{name} is retired; use 'gh api' or 'glab api' — see the "
            f"developer skill document.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if forge is None:
        print(
            f"{name}: not a recognised forge CLI name (expected gh or glab)",
            file=sys.stderr,
        )
        return EXIT_USAGE

    real_bin = _real_bin(forge, env)
    config_dir = _config_dir(forge, env)
    forge_url = _forge_url(forge, env)

    token: str | None = None
    if not is_meta_invocation(args):
        # An empty config dir is not a usable default: gh and glab both read an
        # empty value as unset and fall back to $HOME/.config, which is
        # writable — and gh expands `aliases` from that file before dispatch,
        # so the deny list stops applying. Fail loudly; this is a deployment
        # wiring error, not something to paper over.
        if not config_dir or not os.path.isdir(config_dir):
            print(
                f"{name}: no CLI config directory configured "
                f"(ISTOTA_{'GH' if forge == FORGE_GITHUB else 'GLAB'}_CONFIG_DIR), "
                f"refusing to fall back to a writable one",
                file=sys.stderr,
            )
            return EXIT_MISCONFIGURED
        reason = denied_reason(
            forge, args, load_policy(env.get("ISTOTA_FORGE_POLICY"), forge),
        )
        if reason is not None:
            print(
                f"{name}: '{reason}' is not permitted by this deployment. It is "
                f"an accident guard, not a security boundary; ask the user if "
                f"you need it.",
                file=sys.stderr,
            )
            return EXIT_DENIED
        try:
            token = fetch_token(forge, env)
        except NoProxyError as e:
            print(f"{name}: {e}", file=sys.stderr)
            return EXIT_NO_PROXY
        except (CredentialError, OSError) as e:
            print(f"{name}: {e}", file=sys.stderr)
            return EXIT_CREDENTIAL

    path, child_argv, child_env = build_invocation(
        forge, args, env, token, real_bin, config_dir, forge_url,
    )
    try:
        os.execve(path, child_argv, child_env)
    except OSError as e:
        # Never interpolate the token here — it is in scope on this path.
        code = _errno.errorcode.get(e.errno, "OSError") if e.errno else "OSError"
        print(f"{name}: cannot run {path}: {code}", file=sys.stderr)
        return EXIT_EXEC
    return 0  # unreachable: execve either replaces the process or raises


if __name__ == "__main__":
    raise SystemExit(main())
