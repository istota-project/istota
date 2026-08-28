"""Subprocess probe for the devbox pytest option's environment handoff."""

import os

import pytest


pytestmark = pytest.mark.integration


def test_devbox_user_reaches_the_test_body_after_the_ambient_scrub():
    expected = os.environ["ISTOTA_TEST_EXPECT_DEVBOX_USER"]

    assert os.environ.get("ISTOTA_USER_ID") == expected
