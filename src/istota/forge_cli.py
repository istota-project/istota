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
test fails when the two drift. Both copies are live: the sandbox writes this
file into ``.developer`` per task, and the devbox image installs it at
``/usr/local/bin/{gh,glab,github-api,gitlab-api}`` in front of the real
binaries under ``/usr/local/lib/istota_forge/``.

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

# A deliberate identity marker, near the top of the file so it lands in the
# first few KB. `doctor.check_forge_wrapper_shadowing` has to tell a copy of
# this wrapper from a real `gh` / `glab` when it finds one by name on PATH, and
# it reads only the file's head. Matching on documentation prose would mean a
# reworded docstring silently flips a correct install to a failure — so the
# thing being matched is this line, whose only purpose is to be matched.
ISTOTA_FORGE_WRAPPER = True

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

# The devbox-proxy action the ISTOTA_CRED_SOCK branch sends. Duplicated from
# devbox_proxy_protocol.ALL_ACTIONS because this module cannot import istota;
# test_devbox_proxy_protocol asserts the two spellings still agree.
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
# `form` is glab-only and says so in its own help: "Using this flag changes
# the default HTTP method to POST." gh has no --form, so carrying it in the
# shared list costs nothing there and closes the same implicit-POST hole
# that -f/-F open.
_BODY_FLAGS = ["f", "F", "field", "raw-field", "input", "form"]

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
        ["project", "delete"],
        # Publishing. `gh gist create` takes files or stdin and puts them on
        # github.com; `--public` lists them. That is a one-command
        # exfiltration path with no forge-side gate, so the tree goes.
        ["gist"],
        # Code execution somewhere else. `codespace ssh` is a remote shell,
        # `codespace cp` moves files in and out, and `skill`/`copilot`/
        # `agent-task`/`preview` install or run code on the bot's behalf.
        ["codespace"],
        ["skill"],
        ["copilot"],
        ["agent-task"],
        ["preview"],
        # Guard-evading: an alias or an extension renames a denied verb, and
        # gh expands both before dispatch, where this check no longer runs.
        # `extensions` and `ext` are gh's own aliases for `extension`
        # (confirmed against gh 2.98).
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
        # glab mints credentials of its own: `token create` issues user, group
        # and project access tokens. Nothing else in this list hands out a new
        # credential; this does.
        ["token"],
        ["ssh-key", "add"],
        ["ssh-key", "delete"],
        ["gpg-key", "add"],
        ["gpg-key", "delete"],
        ["deploy-key"],
        ["securefile"],
        ["variable", "set"],
        ["variable", "delete"],
        # Destructive.
        ["repo", "delete"],
        ["repo", "archive"],
        ["project", "delete"],     # `project` is glab's alias for `repo`
        ["project", "archive"],
        ["release", "delete"],
        # `ci` carries two deprecated-but-live aliases, `pipe` and `pipeline`.
        ["ci", "delete"],
        ["pipeline", "delete"],
        ["pipe", "delete"],
        # Publishing and remote execution, as above.
        ["snippet"],
        ["runner"],
        ["runner-controller"],
        ["cluster"],
        ["opentofu"],
        ["skills"],
        ["mcp"],
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


def policy_path(argv0: str) -> str:
    """Where the policy lives, computed rather than taken from the caller.

    **This is deliberately not an environment variable.** The wrapper runs
    inside the sandbox as a child of the model's own shell, so anything it
    reads out of ``os.environ`` is something the model can set: an
    ``ISTOTA_FORGE_POLICY`` pointing at a shape-valid file whose rules name
    nothing real would be a one-token bypass of every rule below, and would
    make the read-only bind, the seeding and the file modes decorative.

    So the wrapper locates its own policy: next to the copy of itself that is
    executing (inside ``.developer``, which is read-only in the sandbox),
    falling back to the image path for the devbox. Both are places the model
    cannot write; neither is anything it can redirect.
    """
    beside = os.path.join(os.path.dirname(os.path.abspath(argv0)), "forge-policy.json")
    if os.path.exists(beside):
        return beside
    return "/etc/istota-forge/policy.json"


def load_policy(path: str | None, forge: str) -> dict:
    """Read the policy file, falling back to the baseline.

    Fails closed to the baseline, never to an empty or half-typed policy. The
    shape checks are not defensive noise: a hand-written ``"path_rules":
    ["repo delete"]`` (strings, not lists of words) parses as valid JSON and
    then matches nothing, which disables the deny list without a word of
    complaint. Anything that does not validate is treated as absent.

    Beyond the rules, the file carries the settings the wrapper must not take
    from the environment either — ``real_bin``, ``url``, ``config_dir``,
    ``data_dir``, ``state_dir`` and ``direct_token``. They ride along here
    because this file's location is trustworthy and the environment is not.
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
        loaded = {
            "path_rules": [list(r) for r in path_rules],
            "flag_value_rules": [dict(r) for r in flag_value_rules],
            "body_flag_rules": [dict(r) for r in body_flag_rules],
        }
        for key in ("real_bin", "url", "config_dir", "data_dir", "state_dir"):
            value = section.get(key)
            if isinstance(value, str) and value:
                loaded[key] = value
        loaded["direct_token"] = bool(section.get("direct_token", False))
        return loaded
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
            # Presence, not a non-empty value. `normalize_args` cannot know a
            # flag's arity, so it declines to swallow a next token beginning
            # with `-` and records an empty value instead. pflag has no such
            # doubt: `--input -` hands `-` to the flag and reads the body from
            # stdin. Testing the value therefore missed every body whose value
            # starts with a dash — including `--input -`, which is the
            # documented stdin form in both CLIs.
            if name in flags:
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


def fetch_forge_credentials(
    forge: str, parent_env: dict[str, str], policy: dict | None = None,
) -> tuple[str, str]:
    """Ask whichever proxy this environment has for the token and the URL.

    Returns ``(token, url_hint)``. ``url_hint`` is ``""`` everywhere except
    the devbox: the image is built once and shared by every user, so its baked
    policy cannot name a per-user ``gitlab_url``. Left unresolved, ``glab``
    falls back to its own default and a self-hosted token is sent to
    gitlab.com — a disclosure, not a misroute. The devbox proxy is per-user,
    host-side, and already the thing handing over the token, so it is the
    right place to learn the URL from. The policy still wins where it has one
    (see ``main``), which keeps the sandbox's anchor supreme.

    Sandbox: the skill proxy, same request the generated credential-fetch
    helper makes. Devbox: the devbox proxy's ``forge_token`` action.

    Every return is a 2-tuple and the no-proxy case raises, so a caller cannot
    accidentally bind the token to the whole result.

    An ambient token is used only when the *policy* says to. On a deployment
    with the skill proxy switched off — which the local single-user installer
    writes — there is no socket to ask, and the token is legitimately in the
    environment rather than having escaped a stripping step. Refusing it there
    would leave the wrapper shadowing the real binary on PATH while being
    incapable of ever working. The permission comes from the policy file
    because that is the one input the model cannot forge; an env-carried
    "direct mode" flag would let it opt itself into reading whatever token it
    had planted.
    """
    if policy and policy.get("direct_token"):
        ambient = parent_env.get(_TOKEN_VAR[forge], "")
        if ambient:
            return ambient, ""
        raise CredentialError(
            f"{_TOKEN_VAR[forge]} not set (policy allows direct tokens)"
        )

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
        return str(value), ""

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
        url = reply.get("url")
        return str(value), str(url) if isinstance(url, str) else ""

    # Name the deployment shape, not the socket path. The path is a fact about
    # this process's environment and tells the reader nothing about why it is
    # missing; the shape is the actual answer, and the two shapes differ on
    # purpose (ISSUE-282). Under Ansible a per-user `istota-devbox-proxy@`
    # instance provides the socket; the docker-compose deployment ships no
    # devbox at all, so forge commands in a container are unavailable there
    # rather than broken.
    raise NoProxyError(
        "no credential proxy: neither ISTOTA_SKILL_PROXY_SOCK (sandbox) nor "
        "ISTOTA_CRED_SOCK (devbox) is set in this environment. The Ansible "
        "deployment runs a per-user credential proxy for the devbox; the "
        "docker-compose deployment ships no devbox at all, so forge commands "
        "in a container are not available on that shape. Report this "
        "rather than retrying — no retry finds a socket that is not "
        "configured."
    )



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
    # Named by `gh help environment` (2.98) and each one runs a command of the
    # caller's choosing inside a process that holds the token, or redirects
    # where the call goes: editors, browsers, the repo override, and gh's own
    # notion of its executable path.
    "GH_EDITOR", "GIT_EDITOR", "VISUAL", "EDITOR",
    "GH_BROWSER", "BROWSER",
    "GH_REPO", "GH_PATH", "GH_FORCE_TTY",
    "GLAMOUR_STYLE", "CLICOLOR_FORCE",
    # Deprecated in glab 1.114 in favour of GLAB_NO_PROMPT. Scrubbed rather
    # than passed through: inherited from the parent it buys nothing and costs
    # a DEPRECATION WARNING on every call.
    "NO_PROMPT",
    "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
    # Retired inputs. Read from the environment, each of these let the model
    # redirect the wrapper's own trust anchors; they now come from the policy
    # file, whose location the wrapper computes. Scrubbed so a leftover in a
    # deployed environment cannot quietly resurrect the old behaviour.
    "ISTOTA_GH_REAL", "ISTOTA_GLAB_REAL",
    "ISTOTA_GH_CONFIG_DIR", "ISTOTA_GLAB_CONFIG_DIR",
    "ISTOTA_GH_URL", "ISTOTA_GITLAB_URL", "ISTOTA_FORGE_STATE_DIR",
)


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _hostname(url: str) -> str:
    """Bare hostname from a configured URL, or "" if there isn't one.

    ``urlsplit`` rather than string splitting: it handles bracketed IPv6
    literals, ports, and userinfo containing ``/`` or ``@``, each of which a
    hand-rolled split gets wrong. A wrong answer here is not cosmetic — the
    GH_TOKEN / GH_ENTERPRISE_TOKEN branch keys off it, and picking the wrong
    one leaves every call unauthenticated, which reads like a scope problem.
    """
    # isinstance, not just falsiness: `url` comes from a JSON policy file, so a
    # number or a list there would make `"://" in url` raise TypeError —
    # uncaught, in a process holding a credential, one call before `execve`.
    if not isinstance(url, str) or not url:
        return ""
    candidate = url if "://" in url else f"https://{url}"
    try:
        host = urlsplit(candidate).hostname or ""
    except ValueError:
        return ""
    return host.rstrip(".")


def _gh_host(url: str) -> str:
    """The whole of what gh is told about the forge: host, plus a non-default port.

    Built on :func:`_hostname` rather than parsing independently, so the host
    it names is the same host by construction. That matters because this one
    string answers *both* of gh's questions — where to connect, and which token
    variable to read — and a version that could disagree with itself would send
    a credential somewhere the credential was not chosen for.

    Everything below is measured against gh 2.98.0, because the shapes gh
    accepts here are narrower than the variable's name suggests:

    - **A port is honoured.** ``GH_HOST=ghe.example.com:8443`` makes gh request
      ``https://ghe.example.com:8443/api/v3/``. This is what ISSUE-279 was: the
      wrapper passed ``_hostname``'s answer, which strips the port, so a forge
      configured on a non-443 port was silently addressed on 443.
    - **The port is part of gh's host identity, token included.** ``gh auth
      token`` with ``GH_HOST=github.com:8443`` reads GH_ENTERPRISE_TOKEN and
      ignores GH_TOKEN — the same classification that picks the API endpoint
      picks the variable. So the caller must decide the token from *this*
      answer, never from the bare hostname; doing otherwise sets the variable
      gh will not read and leaves every call unauthenticated.
    - **A default port must be dropped.** ``GH_HOST=github.com:443`` takes gh
      *off* api.github.com and onto ``https://github.com:443/api/v3``, which
      404s, and onto GH_ENTERPRISE_TOKEN with it. Only a port differing from
      the scheme's default is passed on, so every URL that does not carry one
      behaves exactly as it did before.
    - **A scheme is refused outright** ("error connecting to http"), so plain
      HTTP remains unreachable for gh however this value is spelled. That is a
      separate limitation from the port and the one
      `doctor.check_forge_transport` reports; it is not fixable here.

    Userinfo is dropped with the rest of the authority: this is a destination,
    not a credential, and the token travels in its own variable. Returns "" when
    there is no host, so the caller's github.com fallback still applies.
    """
    host = _hostname(url)
    if not host:
        return ""
    # `hostname` unwraps an IPv6 literal; unbracketed it is not a parseable
    # authority, and gh dials `https://[::1]:8443/api/v3/` from the bracketed
    # form.
    if ":" in host:
        host = f"[{host}]"
    candidate = url if "://" in url else f"https://{url}"
    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError:
        # A port that will not parse (`:notaport`, `:99999`, a trailing space)
        # degrades to the bare host rather than raising: this runs on the way
        # to an exec with a credential in scope, where an exception is not
        # benign. It is not silent in the deployment that matters —
        # `executor._build_network_allowlist` reads `parsed.port` on the same
        # configured URL and raises before a sandboxed task starts, so the
        # operator gets a failure at setup rather than a misdirected call.
        return host
    # Port 0 parses but addresses nothing, so `if port` rather than
    # `is not None`. The default is 443 for *any* scheme, not just https: gh
    # speaks https whatever the URL said, so `ssh://host:22` must not become
    # `host:22`, and an unrecognised scheme must not turn an explicit `:443`
    # into a ported host — which would move it onto GH_ENTERPRISE_TOKEN.
    if port and port != _DEFAULT_PORTS.get(parts.scheme, 443):
        host = f"{host}:{port}"
    return host


def _is_github_com(host: str) -> bool:
    """Hosts whose token is GH_TOKEN rather than GH_ENTERPRISE_TOKEN.

    `gh help environment` (2.98): GH_TOKEN "will be used when a command targets
    either github.com or a subdomain of ghe.com". ghe.com is Enterprise Cloud
    with data residency — an enterprise product that nonetheless takes the
    *non*-enterprise variable, which is exactly the sort of thing worth reading
    rather than inferring from the name.

    A *subdomain* of ghe.com, and not the apex. Measured with `gh auth token`
    on 2.98.0: `tenant.ghe.com` reads GH_TOKEN, while a bare `ghe.com` reads
    GH_ENTERPRISE_TOKEN. The apex used to be listed here anyway, which is the
    documented sentence read one word too generously and would have left a
    forge configured at `https://ghe.com` unauthenticated.

    The caller passes `_gh_host`'s answer, so a host carrying a port arrives
    here as `github.com:8443` and is correctly *not* github.com — which matches
    gh, whose own classification does not strip the port either.
    """
    return (
        host == "github.com"
        or host.endswith(".github.com")
        or host.endswith(".ghe.com")
    )


def build_invocation(
    forge: str,
    args: list[str],
    parent_env: dict[str, str],
    token: str | None,
    real_bin: str,
    config_dir: str,
    forge_url: str = "",
    state_dir: str = "",
    data_dir: str = "",
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
    env["CLICOLOR"] = "0"
    env["DO_NOT_TRACK"] = "1"
    # gh writes a device id under $XDG_STATE_HOME (or $HOME/.local/state) on
    # every run — outside GH_CONFIG_DIR, and therefore outside the read-only
    # config dir. Point it at the config dir's neighbour so a read-only HOME
    # cannot turn a forge call into a filesystem error.
    if state_dir:
        env["XDG_STATE_HOME"] = state_dir
    # Pinned so gh cannot reach an extensions directory the model can write.
    # Unset, gh derives it from HOME, and HOME's .local/share is writable
    # inside the sandbox — `gh <anything>` then execs gh-<anything> from
    # there, which no argv rule can see. See _data_dir().
    if data_dir:
        env["XDG_DATA_HOME"] = data_dir

    if forge == FORGE_GITHUB:
        env["GH_CONFIG_DIR"] = config_dir
        env["GH_PAGER"] = "cat"
        # GH_NO_UPDATE_NOTIFIER, not GH_NO_UPDATE_CHECKER. The latter is not a
        # gh variable at all — it was set here for a while and did nothing,
        # which is the failure mode this spec's reconnaissance stage exists to
        # catch: a wrong name is silent, not an error.
        env["GH_NO_UPDATE_NOTIFIER"] = "1"
        env["GH_NO_EXTENSION_UPDATE_NOTIFIER"] = "1"
        env["GH_TELEMETRY"] = "0"
        env["GH_PROMPT_DISABLED"] = "1"
        # One derivation feeding both answers, because gh derives both from
        # this single string: a non-443 port reaches the forge the operator
        # configured (ISSUE-279) *and* moves the host onto the enterprise
        # variable. Deciding the token from the bare hostname instead would set
        # the one gh does not read. See `_gh_host`.
        host = _gh_host(forge_url) or "github.com"
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
        # glab reads none of the GH_* knobs and ships no `help environment`
        # topic, so these spellings were taken from its documentation. All
        # three have since been run against glab 1.114: GLAB_CHECK_UPDATE and
        # GLAB_SEND_TELEMETRY are accepted silently, and the bare NO_PROMPT
        # this used to set is **deprecated** — glab prints a DEPRECATION
        # WARNING on every single call and says to use GLAB_NO_PROMPT. That
        # warning lands in the task transcript, and the variable stops working
        # altogether whenever glab drops it. Only the prefixed name is set.
        env["GLAB_CHECK_UPDATE"] = "false"
        env["GLAB_SEND_TELEMETRY"] = "0"
        env["GLAB_NO_PROMPT"] = "1"
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


# Every setting below comes from the policy file, never from os.environ. The
# wrapper is a child of the model's shell, so an env-supplied path is a path
# the model chooses — see policy_path() for the full reasoning. Defaults apply
# only when the policy omits a key.
def _real_bin(forge: str, policy: dict) -> str:
    default = "/usr/local/bin/gh" if forge == FORGE_GITHUB else "/usr/local/bin/glab"
    return policy.get("real_bin") or default


def _config_dir(forge: str, policy: dict) -> str:
    """The read-only, pre-seeded CLI config directory.

    Read-only is the point: gh expands ``aliases`` from ``config.yml`` before
    command dispatch, so a writable config dir is a complete policy bypass.
    Measured against gh 2.98 and glab 1.114 — gh runs fine with the directory
    at 0500 and the file at 0400, but **glab refuses to start unless
    config.yml is exactly 0600** ("has the permissions 400, but glab requires
    600"). So the seeded file is 0600 and the immutability comes from the
    sandbox's read-only bind over ``.developer``, not from the file mode.
    """
    return policy.get("config_dir", "")


def _state_dir(policy: dict) -> str:
    """Writable scratch for the CLI's own state, kept out of the config dir."""
    return policy.get("state_dir", "")


def _data_dir(policy: dict) -> str:
    """Pinned, empty, read-only — the extensions directory.

    gh dispatches an unknown first argument to ``gh-<name>`` in
    ``$XDG_DATA_HOME/gh/extensions``, which the argv rules cannot see: the
    argv is ``gh pwned`` and matches nothing. Verified against gh 2.98 — a
    planted extension runs, both via XDG_DATA_HOME and via
    ``$HOME/.local/share``, and pointing XDG_DATA_HOME at an empty directory
    shuts both. Leaving it unset means gh derives it from HOME, and HOME
    inside the sandbox has model-writable ``.local/share``.
    """
    return policy.get("data_dir", "")


def _forge_url(forge: str, policy: dict) -> str:
    return policy.get("url", "")


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

    policy = load_policy(policy_path(argv0), forge)
    real_bin = _real_bin(forge, policy)
    config_dir = _config_dir(forge, policy)
    forge_url = _forge_url(forge, policy)

    token: str | None = None
    url_hint = ""
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
        reason = denied_reason(forge, args, policy)
        if reason is not None:
            print(
                f"{name}: '{reason}' is not permitted by this deployment. It is "
                f"an accident guard, not a security boundary; ask the user if "
                f"you need it.",
                file=sys.stderr,
            )
            return EXIT_DENIED
        try:
            token, url_hint = fetch_forge_credentials(forge, env, policy)
        except NoProxyError as e:
            print(f"{name}: {e}", file=sys.stderr)
            return EXIT_NO_PROXY
        except (CredentialError, OSError) as e:
            print(f"{name}: {e}", file=sys.stderr)
            return EXIT_CREDENTIAL

        # Refuse rather than guess where to send the token. Neither CLI's own
        # default is safe here: unset, glab talks to gitlab.com and gh to
        # github.com, and on a self-hosted or Enterprise Server deployment
        # that hands a scoped credential to a vendor that was never meant to
        # see it. Both supported shapes always have a URL — the sandbox's
        # policy is written with one, and the devbox proxy sends one with the
        # token — so this fires only when something upstream is misconfigured,
        # which is exactly when guessing is worst.
        if not (forge_url or url_hint):
            print(
                f"{name}: no forge URL configured, and the credential proxy "
                f"did not supply one. Refusing to fall back to the public "
                f"host with a token that may not belong to it.",
                file=sys.stderr,
            )
            return EXIT_MISCONFIGURED

    # The policy's URL wins; the proxy's fills in where the policy has none.
    # Only the devbox is in that position — see fetch_forge_credentials.
    path, child_argv, child_env = build_invocation(
        forge, args, env, token, real_bin, config_dir, forge_url or url_hint,
        _state_dir(policy), _data_dir(policy),
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
