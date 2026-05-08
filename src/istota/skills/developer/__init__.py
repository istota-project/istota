"""Developer skill — setup_env hook.

Generates the credential-fetch helper script and the per-platform
git-credential-helper / gitlab-api / github-api wrapper scripts inside the
task's user temp directory, and exports the GIT_CONFIG_* env vars that
point git at those helpers.

Static env vars (DEVELOPER_REPOS_DIR, GITLAB_URL, GITHUB_URL, the optional
namespace/owner/reviewer/credit knobs, GITLAB_TOKEN, GITHUB_TOKEN) come
from the manifest's ``env:`` block — this hook only handles the parts
that aren't expressible as static EnvSpecs.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("istota.skills.developer")


def _allowlist_pattern_to_case(pattern: str) -> str:
    """Convert an allowlist pattern like 'GET /api/v4/projects/*' to a shell case glob."""
    parts = pattern.split("*")
    result = "*".join(f'"{p}"' for p in parts if p)
    if pattern.endswith("*"):
        result += "*"
    return result


def setup_env(ctx) -> dict[str, str]:
    """Write helper scripts and return GIT_CONFIG_* / *_API_CMD env vars.

    Self-gates on ``config.developer.enabled`` and a non-empty
    ``repos_dir`` — the hook is invoked for every skill in the index, so
    skills must opt themselves out when their config isn't ready.
    """
    config = ctx.config
    dev = getattr(config, "developer", None)
    if dev is None or not dev.enabled or not dev.repos_dir:
        return {}

    env: dict[str, str] = {}
    user_temp_dir = Path(ctx.user_temp_dir)
    dev_bin = user_temp_dir / ".developer"
    dev_bin.mkdir(parents=True, exist_ok=True)

    use_proxy = config.security.skill_proxy_enabled
    cred_fetch_cmd = ""
    if use_proxy:
        cred_fetch = dev_bin / "credential-fetch"
        cred_fetch.write_text(
            "#!/usr/bin/env python3\n"
            "import json, socket, sys\n"
            "import os\n"
            "sock_path = os.environ.get('ISTOTA_SKILL_PROXY_SOCK', '')\n"
            "if not sock_path:\n"
            "    print('ISTOTA_SKILL_PROXY_SOCK not set', file=sys.stderr)\n"
            "    sys.exit(1)\n"
            "s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "s.connect(sock_path)\n"
            "s.sendall(json.dumps({'type': 'credential', 'name': sys.argv[1]}).encode() + b'\\n')\n"
            "d = b''\n"
            "while b'\\n' not in d:\n"
            "    c = s.recv(4096)\n"
            "    if not c: break\n"
            "    d += c\n"
            "s.close()\n"
            "r = json.loads(d)\n"
            "if 'error' in r:\n"
            "    print(r['error'], file=sys.stderr)\n"
            "    sys.exit(1)\n"
            "print(r.get('value', ''), end='')\n"
        )
        cred_fetch.chmod(0o700)
        cred_fetch_cmd = str(cred_fetch)

    def _token_expr(var_name: str) -> str:
        if use_proxy:
            return f"$({cred_fetch_cmd} {var_name})"
        return f"${var_name}"

    git_config_index = 0

    if dev.gitlab_token:
        gitlab_host = dev.gitlab_url.rstrip("/")

        git_cred = dev_bin / "git-credential-helper"
        git_cred.write_text(
            "#!/bin/sh\n"
            '[ "$1" = "get" ] || exit 0\n'
            f"echo username={dev.gitlab_username}\n"
            f"echo password={_token_expr('GITLAB_TOKEN')}\n"
        )
        git_cred.chmod(0o700)
        env[f"GIT_CONFIG_KEY_{git_config_index}"] = f"credential.{gitlab_host}.helper"
        env[f"GIT_CONFIG_VALUE_{git_config_index}"] = str(git_cred)
        git_config_index += 1

        api_script = dev_bin / "gitlab-api"
        allowlist_cases = "\n".join(
            f"  {_allowlist_pattern_to_case(p)}) ;;"
            for p in dev.gitlab_api_allowlist
        )
        if use_proxy:
            token_line = f'TOKEN=$({cred_fetch_cmd} GITLAB_TOKEN)\n'
            curl_header = '"PRIVATE-TOKEN: $TOKEN"'
        else:
            token_line = ""
            curl_header = '"PRIVATE-TOKEN: $GITLAB_TOKEN"'
        api_script.write_text(
            "#!/bin/sh\n"
            'METHOD="$1"; shift\n'
            'ENDPOINT="$1"; shift\n'
            'CLEAN="${ENDPOINT%%\\?*}"\n'
            'case "$METHOD $CLEAN" in\n'
            f"{allowlist_cases}\n"
            '  *) printf \'{"error":"endpoint not allowed: %s %s"}\\n\' '
            '"$METHOD" "$CLEAN" >&2; exit 1 ;;\n'
            "esac\n"
            f'{token_line}'
            f'curl -s --header {curl_header} '
            f'--request "$METHOD" "{gitlab_host}$ENDPOINT" "$@"\n'
        )
        api_script.chmod(0o700)
        env["GITLAB_API_CMD"] = str(api_script)

    if dev.github_token:
        github_host = dev.github_url.rstrip("/")
        gh_username = dev.github_username or "x-access-token"

        gh_cred = dev_bin / "git-credential-helper-github"
        gh_cred.write_text(
            "#!/bin/sh\n"
            '[ "$1" = "get" ] || exit 0\n'
            f"echo username={gh_username}\n"
            f"echo password={_token_expr('GITHUB_TOKEN')}\n"
        )
        gh_cred.chmod(0o700)
        env[f"GIT_CONFIG_KEY_{git_config_index}"] = f"credential.{github_host}.helper"
        env[f"GIT_CONFIG_VALUE_{git_config_index}"] = str(gh_cred)
        git_config_index += 1

        gh_api_script = dev_bin / "github-api"
        gh_allowlist_cases = "\n".join(
            f"  {_allowlist_pattern_to_case(p)}) ;;"
            for p in dev.github_api_allowlist
        )
        gh_host_stripped = github_host.rstrip("/")
        if "github.com" == gh_host_stripped.split("//")[-1]:
            gh_api_base = "https://api.github.com"
        else:
            gh_api_base = f"{gh_host_stripped}/api/v3"
        if use_proxy:
            gh_token_line = f'TOKEN=$({cred_fetch_cmd} GITHUB_TOKEN)\n'
            gh_curl_header = '"Authorization: Bearer $TOKEN"'
        else:
            gh_token_line = ""
            gh_curl_header = '"Authorization: Bearer $GITHUB_TOKEN"'
        gh_api_script.write_text(
            "#!/bin/sh\n"
            'METHOD="$1"; shift\n'
            'ENDPOINT="$1"; shift\n'
            'CLEAN="${ENDPOINT%%\\?*}"\n'
            'case "$METHOD $CLEAN" in\n'
            f"{gh_allowlist_cases}\n"
            '  *) printf \'{"error":"endpoint not allowed: %s %s"}\\n\' '
            '"$METHOD" "$CLEAN" >&2; exit 1 ;;\n'
            "esac\n"
            f'{gh_token_line}'
            f'curl -s --header {gh_curl_header} '
            f'--header "Accept: application/vnd.github+json" '
            f'--request "$METHOD" "{gh_api_base}$ENDPOINT" "$@"\n'
        )
        gh_api_script.chmod(0o700)
        env["GITHUB_API_CMD"] = str(gh_api_script)

    if git_config_index > 0:
        env["GIT_CONFIG_COUNT"] = str(git_config_index)

    return env
