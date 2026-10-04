"""A forge URL carrying userinfo is refused at config load (ISSUE-620).

`developer.gitlab_url` / `github_url` reach the sandbox verbatim, through
`forge-policy.json` and the manifest's `GITLAB_URL` / `GITHUB_URL`, so a
credential written into one was readable by every admin task with the
developer skill. The loader is the one place every reader goes through.
"""

import pytest

from istota.config import load_config

SECRET = "hunter2-sentinel"


def _load(tmp_path, body: str):
    cfg = tmp_path / "config.toml"
    cfg.write_text(body)
    return load_config(cfg)


@pytest.mark.parametrize("key", ["gitlab_url", "github_url"])
@pytest.mark.parametrize(
    "url",
    [
        f"https://user:{SECRET}@forge.example.com",
        f"https://{SECRET}@forge.example.com",
        f"http://user:{SECRET}@forge.example.com:8080/",
    ],
)
def test_userinfo_in_a_forge_url_fails_the_load(tmp_path, key, url):
    with pytest.raises(ValueError) as excinfo:
        _load(tmp_path, f'[developer]\nenabled = true\n{key} = "{url}"\n')
    message = str(excinfo.value)
    assert f"developer.{key}" in message
    assert SECRET not in message
    assert "user" not in message.replace("userinfo", "")


def test_a_disabled_developer_section_is_refused_too(tmp_path):
    # The value is in config.toml either way, and enabling the skill later
    # must not be the moment a credential starts reaching the sandbox.
    with pytest.raises(ValueError, match="developer.gitlab_url"):
        _load(
            tmp_path,
            f'[developer]\ngitlab_url = "https://u:{SECRET}@gitlab.example.com"\n',
        )


@pytest.mark.parametrize(
    "url",
    [
        # Without `//` the userinfo lands in the path, where an `@`-in-netloc
        # test alone never looks.
        f"oauth2:{SECRET}@gitlab.example.com",
        f"user:{SECRET}@gitlab.example.com",
        f"{SECRET}@gitlab.example.com",
        f"https:user:{SECRET}@gitlab.example.com",
        f"https:/user:{SECRET}@gitlab.example.com",
        "gitlab.example.com",
        "ssh://gitlab.example.com",
    ],
)
def test_a_url_without_an_http_authority_fails_the_load(tmp_path, url):
    with pytest.raises(ValueError) as excinfo:
        _load(tmp_path, f'[developer]\ngitlab_url = "{url}"\n')
    message = str(excinfo.value)
    assert "developer.gitlab_url" in message
    assert SECRET not in message


@pytest.mark.parametrize(
    "url", ["http://[::1", "https://forge.example.com:99999", "https://forge.example.com:abc"],
)
def test_an_unparseable_forge_url_fails_the_load(tmp_path, url):
    with pytest.raises(ValueError, match="developer.github_url is not a parseable URL"):
        _load(tmp_path, f'[developer]\ngithub_url = "{url}"\n')


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.example.com",
        "http://127.0.0.1:8929/",
        "https://forge.example.com:8443/gitlab",
        # An `@` in the path is not userinfo.
        "https://forge.example.com/@team",
        "",
    ],
)
def test_a_url_without_userinfo_loads_and_reaches_the_dataclass(tmp_path, url):
    config = _load(tmp_path, f'[developer]\nenabled = true\ngitlab_url = "{url}"\n')
    assert config.developer.gitlab_url == url


def test_the_defaults_load(tmp_path):
    config = _load(tmp_path, 'bot_name = "Istota"\n')
    assert config.developer.gitlab_url == "https://gitlab.com"
    assert config.developer.github_url == "https://github.com"
