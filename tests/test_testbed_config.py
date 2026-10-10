"""The testbed's config base is what `istota setup` writes, held equal.

`testbed/` may not import istota (it is meant for rigs that consume it without
the package), so the container layout and the answers a lean stack's one user
gets are restated there as `LEAN_BASE_CONFIG`, and the credential files as
`SECRET_NAMES`. These tests are what holds the copies to the authority, which
is `setup_wizard`: a stack booted from a config no operator could have written
would witness nothing about the product.
"""

from __future__ import annotations

import tomllib

from istota import setup_wizard
from testbed import stack as stack_support


def test_the_lean_base_is_what_setup_writes_for_the_same_answers():
    answers = setup_wizard.ContainerAnswers(
        user_id="testuser", hostname="localhost", session_secret="placeholder",
    )
    written = tomllib.loads(
        setup_wizard.render_container_config(answers, inline_credentials=False)
    )

    assert written == stack_support.LEAN_BASE_CONFIG


def test_the_secret_names_are_the_wizards():
    assert stack_support.SECRET_NAMES == setup_wizard.SECRET_NAMES


def test_the_concessions_are_few_and_named():
    """Each departure from setup's output is a reason in `stack.py`; a new one
    is a deliberate edit to this list as well."""
    assert stack_support.CONCESSIONS == {
        "memory_search": {"enabled": False},
        "web": {"auth": ["nextcloud"]},
    }


def test_the_full_base_departs_from_the_lean_one_only_where_nextcloud_does(tmp_path):
    """The full shape is the lean base plus full Nextcloud integration, which
    is what `istota setup` writes when it is pointed at a Nextcloud with an
    OAuth client. Compared key by key against the wizard's own answer."""
    credentials = stack_support.generate_credentials(18080)
    profile = stack_support.Profile("unit", shape="full", services=("model",))
    document, _ = stack_support.full_config({}, credentials, profile)

    answers = setup_wizard.ContainerAnswers(
        user_id="testuser", hostname="localhost:18080",
        nextcloud_url="http://nextcloud",
        nextcloud_public_url="http://localhost:18080",
        nextcloud_username="istota",
        nextcloud_dav_prefix="Shared Files",
        nextcloud_auto_share_bot_dir=False,
        oauth_client_id=credentials.oauth_client_id,
        oauth_client_secret=credentials.oauth_client_secret,
        session_secret="placeholder",
    )
    written = tomllib.loads(
        setup_wizard.render_container_config(answers, inline_credentials=False)
    )

    assert document["nextcloud"] == written["nextcloud"]
    assert document["workspace_path"] == written["workspace_path"] == "/mnt/shared"
    assert document["nextcloud_mount_path"] == written["nextcloud_mount_path"]
    for key in (
        "oauth2_provider", "oauth2_client_id", "oauth2_token_endpoint",
        "oauth2_userinfo_endpoint", "oauth2_redirect_uri",
    ):
        assert document["web"][key] == written["web"][key], key
    assert document["site"] == written["site"]
