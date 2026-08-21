"""Thin wrapper around the real ``gh`` / ``glab`` binaries.

Installed under both names, ahead of the real binaries on PATH. It fetches a
token from whichever credential proxy the environment has, checks the argv
against a policy, builds a deliberate child environment, and ``execve``s the
real CLI. Everything after the exec is the real CLI.

Two homes, one file. The canonical copy is here; ``docker/devbox/lib/`` holds a
byte-identical copy because Docker cannot COPY from outside its build context.
Keep it stdlib-only and free of ``istota`` imports so the container copy runs
under a bare ``python3`` — ``scripts/sync-devbox-lib.sh`` regenerates it and a
test fails when the two drift.

What the policy is and is not: it stops a *mistaken* ``gh repo delete``. It does
not stop a determined model, which can read the raw token from the credential
socket or out of ``/proc/<pid>/environ`` of the running CLI — everything in the
sandbox shares one uid. The boundary that does the work is the token's own
scope. Do not write code or messages here implying more than that.
"""

from __future__ import annotations

import json
import os
import socket
import sys

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

# Exit codes. Anything above these is the real CLI's own status.
EXIT_USAGE = 2
EXIT_DENIED = 3
EXIT_NO_PROXY = 4
EXIT_CREDENTIAL = 5
EXIT_EXEC = 6

# Subcommand-free invocations that must work without a credential proxy. A
# version check is not a forge call, and requiring a socket for one breaks
# every deployment with the proxy switched off.
_META_ARGS = frozenset({"--version", "-v", "--help", "-h", "help", "completion"})

# Write methods on the raw REST passthrough. Compared case-folded.
_WRITE_METHODS = ["post", "put", "patch", "delete"]

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
_BASELINE_PATH_RULES = {
    FORGE_GITHUB: [
        # Credential-revealing and persistence-granting.
        ["auth"],          # `auth token` and `auth status --show-token` print it
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
        ["release", "delete"],
        ["ci", "delete"],
        ["alias"],
        ["extension"],
        ["ext"],
        ["config"],
        ["api", "graphql"],
    ],
}

# Flag rules cover what a path rule cannot see: the method on the raw REST
# passthrough. Both spellings of the flag, because gh accepts either.
_BASELINE_FLAG_RULES = [
    {"path": ["api"], "flag": "method", "in": _WRITE_METHODS},
    {"path": ["api"], "flag": "X", "in": _WRITE_METHODS},
]


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #


def baseline_policy(forge: str) -> dict:
    """The in-code policy for one forge, as the JSON shape on the wire."""
    return {
        "path_rules": [list(r) for r in _BASELINE_PATH_RULES.get(forge, [])],
        "flag_rules": [dict(r) for r in _BASELINE_FLAG_RULES],
    }


def _parse_entry(entry: str) -> tuple[str | None, list[str]]:
    """Split ``"gh repo delete"`` into ``("github", ["repo", "delete"])``.

    An entry that does not name a binary applies to both forges.
    """
    words = entry.split()
    if not words:
        return None, []
    scoped = _ARGV0_MAP.get(words[0])
    if scoped in (FORGE_GITHUB, FORGE_GITLAB):
        return scoped, words[1:]
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


def unmatched_permits(forges: list[str], permit: list[str] | None) -> list[str]:
    """Permit entries that match no baseline rule for any given forge.

    A hatch that silently stopped matching after a baseline rewording reads
    exactly like a hatch that is still open. The daemon logs these at startup.
    """
    unmatched = []
    for entry in permit or []:
        scope, words = _parse_entry(entry)
        if not words:
            continue
        targets = [scope] if scope else forges
        if not any(words in _BASELINE_PATH_RULES.get(f, []) for f in targets):
            unmatched.append(entry)
    return unmatched


def load_policy(path: str | None, forge: str) -> dict:
    """Read the policy file, falling back to the baseline.

    Fails closed to the baseline, never to an empty policy: a missing or
    corrupt file must not silently unlock everything.
    """
    if not path:
        return baseline_policy(forge)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        section = data[forge]
        if not isinstance(section, dict):
            raise ValueError("policy section is not an object")
        return {
            "path_rules": [list(r) for r in section.get("path_rules", [])],
            "flag_rules": [dict(r) for r in section.get("flag_rules", [])],
        }
    except Exception as e:  # noqa: BLE001 - any failure means "use the baseline"
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


def normalize_args(args: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """Split argv into a subcommand path and a flag multimap.

    The path is the run of non-flag tokens *before the first flag*, which is
    where every CLI here puts its subcommand. Tokens after a flag are operands
    or flag values and never join the path, so a denied word appearing in
    ``--label config`` cannot trip a path rule.

    Flags are a multimap because a short token is ambiguous without knowing
    each flag's arity: ``-XDELETE`` is recorded both as ``X -> "DELETE"`` and
    as the cluster ``X``/``D``/``E``/... with empty values. Rules only ever ask
    whether a named flag carries one of a set of values, so the extra cluster
    entries contribute nothing, and neither reading can be evaded.
    """
    path: list[str] = []
    flags: dict[str, list[str]] = {}
    seen_flag = False

    def _record(name: str, value: str) -> None:
        flags.setdefault(name, []).append(value)

    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            break  # everything after is operands
        if tok.startswith("--"):
            seen_flag = True
            body = tok[2:]
            if "=" in body:
                name, _, value = body.partition("=")
                _record(name, value)
            else:
                nxt = args[i + 1] if i + 1 < len(args) else None
                if nxt is not None and not nxt.startswith("-"):
                    _record(body, nxt)
                    i += 2
                    continue
                _record(body, "")
        elif tok.startswith("-") and len(tok) > 1:
            seen_flag = True
            body = tok[1:]
            if len(body) > 1:
                _record(body[0], body[1:])          # attached-value reading
                for ch in body:
                    _record(ch, "")                 # cluster reading
            else:
                nxt = args[i + 1] if i + 1 < len(args) else None
                if nxt is not None and not nxt.startswith("-"):
                    _record(body, nxt)
                    i += 2
                    continue
                _record(body, "")
        elif not seen_flag:
            path.append(tok)
        i += 1

    return path, flags


def _path_matches(path: list[str], rule: list[str]) -> bool:
    """Anchored at index 0 — a rule names a subcommand, not a substring."""
    return len(rule) > 0 and path[: len(rule)] == rule


def denied_reason(forge: str, args: list[str], policy: dict) -> str | None:
    """The human-readable reason this invocation is refused, or None."""
    path, flags = normalize_args(args)

    for rule in policy.get("path_rules", []):
        if _path_matches(path, rule):
            return " ".join(rule)

    for rule in policy.get("flag_rules", []):
        if not _path_matches(path, rule.get("path", [])):
            continue
        name = rule.get("flag", "")
        wanted = [str(v).lower() for v in rule.get("in", [])]
        for value in flags.get(name, []):
            if value.lower() in wanted:
                return f"{' '.join(rule['path'])} --{name} {value}"

    return None


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #


class CredentialError(Exception):
    """The proxy answered, and the answer was not a token."""


class NoProxyError(Exception):
    """No credential proxy is reachable from this environment."""


def _sock_roundtrip(sock_path: str, request: dict) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(30)
        s.connect(sock_path)
        s.sendall(json.dumps(request).encode("utf-8") + b"\n")
        chunks = b""
        while b"\n" not in chunks:
            got = s.recv(4096)
            if not got:
                break
            chunks += got
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
    helper makes. Devbox: the devbox proxy's forge_token action. An ambient
    GH_TOKEN is never used as a fallback — that would mean something upstream
    failed to strip it, and inheriting it would hide the failure.
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
            cred_sock, {"action": "forge_token", "provider": forge},
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
    "HOME", "PATH", "TZ", "LANG",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
    "https_proxy", "http_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE",
    "ISTOTA_SKILL_PROXY_SOCK", "ISTOTA_CRED_SOCK",
    "TMPDIR", "USER", "LOGNAME", "TERM",
)
_CARRY_PREFIX = ("LC_", "GIT_")

# Removed even when the parent has them. GH_DEBUG=api makes gh print request
# headers, Authorization among them — the one setting that turns a permitted
# command into a token disclosure. ISTOTA_FORGE_POLICY goes so the real CLI
# cannot hand it to a nested invocation.
_SCRUB = (
    "GH_DEBUG", "GLAB_DEBUG", "DEBUG",
    "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
    "GITLAB_TOKEN", "GITLAB_ACCESS_TOKEN", "OAUTH_TOKEN",
    "ISTOTA_FORGE_POLICY",
    "GH_HOST", "GITLAB_HOST", "GH_CONFIG_DIR", "GLAB_CONFIG_DIR",
)


def _hostname(url: str) -> str:
    """Bare hostname from a configured URL, without importing urllib."""
    rest = url.split("://", 1)[-1]
    return rest.split("/", 1)[0].split("@")[-1].split(":")[0]


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
        if key in _CARRY_EXACT or key.startswith(_CARRY_PREFIX):
            env[key] = value

    env["GH_PAGER"] = "cat"
    env["PAGER"] = "cat"
    env["NO_COLOR"] = "1"
    env["GH_NO_UPDATE_CHECKER"] = "1"
    env["GH_PROMPT_DISABLED"] = "1"

    if forge == FORGE_GITHUB:
        env["GH_CONFIG_DIR"] = config_dir
        host = _hostname(forge_url) if forge_url else "github.com"
        env["GH_HOST"] = host
        if token:
            # gh resolves auth per host: GH_TOKEN is github.com's, and an
            # enterprise host reads GH_ENTERPRISE_TOKEN instead. Setting the
            # wrong one leaves every call unauthenticated.
            if host == "github.com" or host.endswith(".github.com"):
                env["GH_TOKEN"] = token
            else:
                env["GH_ENTERPRISE_TOKEN"] = token
    else:
        env["GLAB_CONFIG_DIR"] = config_dir
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
        reason = denied_reason(forge, args, load_policy(env.get("ISTOTA_FORGE_POLICY"), forge))
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
        print(
            f"{name}: cannot run {path}: {e.__class__.__name__} "
            f"({os.strerror(e.errno) if e.errno else e})",
            file=sys.stderr,
        )
        return EXIT_EXEC
    return 0  # unreachable: execve either replaces the process or raises


if __name__ == "__main__":
    raise SystemExit(main())
