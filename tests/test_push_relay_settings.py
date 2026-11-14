"""Relay delivery settings, their load-time rules, and the `push.relay` check."""

import httpx
import pytest

from istota import config as config_module
from istota import doctor
from istota.config import Config, PushConfig, load_config, normalize_relay_url


RELAY = "https://relay.example.com/istota"


@pytest.fixture(autouse=True)
def no_push_overrides(monkeypatch):
    for variable in ("ISTOTA_PUSH_RELAY_URL", "ISTOTA_PUSH_SIGNING_KEY_PATH", "ISTOTA_APNS_KEY_PATH"):
        monkeypatch.delenv(variable, raising=False)


def write_config(tmp_path, push_body: str):
    path = tmp_path / "config.toml"
    path.write_text(f'db_path = "{tmp_path / "istota.db"}"\n[push]\n{push_body}')
    return path


def relay_body(url: str = RELAY, extra: str = "") -> str:
    return (
        'enabled = true\ndelivery = "relay"\ncontent = "private"\n'
        f'signing_key_path = "/example/push/signing.pem"\nrelay_url = "{url}"\n{extra}'
    )


def load_push(tmp_path, body: str) -> PushConfig:
    return load_config(write_config(tmp_path, body), apply_db_overlays=False).push


# --- Settings ---------------------------------------------------------------


def test_defaults_are_direct_with_no_relay():
    push = PushConfig()
    assert (push.delivery, push.relay_url, push.relay_timeout_seconds) == ("direct", "", 15)


def test_an_omitted_delivery_loads_as_direct(tmp_path):
    push = load_push(tmp_path, "enabled = true\n")
    assert push.delivery == "direct"
    assert push.relay_url == ""


def test_relay_with_private_content_and_a_key_loads(tmp_path):
    push = load_push(tmp_path, relay_body(extra="relay_timeout_seconds = 20\n"))
    assert (push.delivery, push.relay_url, push.relay_timeout_seconds) == ("relay", RELAY, 20)


@pytest.mark.parametrize("raw,normalized", [
    ("HTTPS://Relay.Example.COM/istota/", RELAY),
    ("https://relay.example.com:443/istota", RELAY),
    ("https://relay.example.com/istota/", RELAY),
    ("https://relay.example.com:8443/istota", "https://relay.example.com:8443/istota"),
    ("https://relay.example.com", "https://relay.example.com"),
    ("https://relay.example.com/", "https://relay.example.com"),
    # The path is the relay's prefix for this app and keeps its case.
    ("https://relay.example.com/Istota/v2", "https://relay.example.com/Istota/v2"),
    ("  https://relay.example.com/istota  ", RELAY),
])
def test_the_relay_url_is_normalized_once_at_load(tmp_path, raw, normalized):
    assert normalize_relay_url(raw) == normalized
    assert load_push(tmp_path, relay_body(raw)).relay_url == normalized


@pytest.mark.parametrize("raw", [
    "",
    "   ",
    "http://relay.example.com/istota",
    "relay.example.com/istota",
    "https:///istota",
    "https://user:secret@relay.example.com/istota",
    "https://user@relay.example.com/istota",
    "https://relay.example.com/istota?route=1",
    "https://relay.example.com/istota?",
    "https://relay.example.com/istota#fragment",
    "https://relay.example.com/istota#",
    "https://relay.example.com/a/../istota",
    "https://relay.example.com/istota/..",
    "https://relay.example.com/a//istota",
    "https://relay.example.com/a%2Fistota",
    "https://relay.example.com/a%2fistota",
    "https://relay.example.com/%2e%2e/istota",
    "https://relay.example.com/%2E/istota",
    "https://relay.example.com:notaport/istota",
    "https://relay.example.com:0/istota",
    "https://relay.example.com/is ota",
    "https://relay.example.com/istota\\x",
    "https://relay.example.com/" + "a" * 300,
])
def test_a_relay_url_that_is_not_a_clean_https_base_is_refused(tmp_path, raw):
    with pytest.raises(ValueError):
        normalize_relay_url(raw)
    with pytest.raises(ValueError, match="relay_url"):
        load_push(tmp_path, relay_body(raw.replace("\\", "\\\\")))


def test_relay_without_private_content_is_a_config_error(tmp_path):
    body = relay_body().replace('content = "private"', 'content = "legacy"')
    with pytest.raises(ValueError, match="private"):
        load_push(tmp_path, body)


def test_relay_with_content_omitted_is_a_config_error(tmp_path):
    body = relay_body().replace('content = "private"\n', "")
    with pytest.raises(ValueError, match="private"):
        load_push(tmp_path, body)


def test_relay_without_a_signing_key_is_a_config_error(tmp_path):
    body = relay_body().replace('signing_key_path = "/example/push/signing.pem"\n', "")
    with pytest.raises(ValueError, match="signing_key_path"):
        load_push(tmp_path, body)


def test_an_unknown_delivery_is_a_config_error(tmp_path):
    with pytest.raises(ValueError, match=r"\[push\] delivery"):
        load_push(tmp_path, 'delivery = "fetch"\n')


@pytest.mark.parametrize("timeout", [0, -1])
def test_a_relay_timeout_below_one_second_is_a_config_error(tmp_path, timeout):
    with pytest.raises(ValueError, match="relay_timeout_seconds"):
        load_push(tmp_path, relay_body(extra=f"relay_timeout_seconds = {timeout}\n"))


def test_relay_with_apns_settings_present_still_loads(tmp_path):
    extra = 'apns_key_path = "/example/AuthKey.p8"\napns_key_id = "EXAMPLEKEY"\napns_team_id = "EXAMPLETEAM"\n'
    push = load_push(tmp_path, relay_body(extra=extra))
    assert push.delivery == "relay"
    assert push.apns_key_path == "/example/AuthKey.p8"


def test_direct_delivery_keeps_its_rules_and_ignores_the_relay_url(tmp_path):
    push = load_push(tmp_path, 'enabled = true\nrelay_url = "http://not-checked.example.com"\n')
    assert (push.delivery, push.content) == ("direct", "legacy")


def test_the_environment_supplies_and_replaces_the_relay_url(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_PUSH_RELAY_URL", "https://RELAY.example.com/istota/")
    push = load_push(tmp_path, relay_body().replace(f'relay_url = "{RELAY}"\n', ""))
    assert push.relay_url == RELAY
    push = load_push(tmp_path, relay_body("https://file.example.com/istota"))
    assert push.relay_url == RELAY
    monkeypatch.setenv("ISTOTA_PUSH_RELAY_URL", "  ")
    assert load_push(tmp_path, relay_body("https://file.example.com/istota")).relay_url == (
        "https://file.example.com/istota"
    )


def test_an_environment_relay_url_is_held_to_the_same_rules(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_PUSH_RELAY_URL", "http://relay.example.com/istota")
    with pytest.raises(ValueError, match="relay_url"):
        load_push(tmp_path, relay_body())


def test_delivery_is_a_closed_vocabulary_for_the_editor():
    assert config_module.FIELD_CHOICES[("push", "delivery")] == ("direct", "relay")
    assert ("ISTOTA_PUSH_RELAY_URL", ("push", "relay_url")) in config_module.ENV_OVERRIDES


# --- Doctor -----------------------------------------------------------------


def relay_config(**overrides) -> Config:
    fields = dict(enabled=True, delivery="relay", content="private",
                  signing_key_path="/example/push/signing.pem", relay_url=RELAY)
    fields.update(overrides)
    return Config(push=PushConfig(**fields))


def test_the_relay_check_is_deployment_scoped_and_registered():
    assert doctor.CHECK_SCOPES["push.relay"] == doctor.DEPLOYMENT
    assert "push.relay" in {name for name, _ in doctor.CHECKS}


@pytest.mark.parametrize("push", [
    PushConfig(enabled=False, delivery="relay", relay_url=RELAY),
    PushConfig(enabled=True),
])
def test_the_relay_check_skips_without_relay_delivery(push):
    assert doctor.check_push_relay(Config(push=push), True).status == doctor.SKIP


def test_the_relay_check_without_a_probe_reads_config_only(monkeypatch, tmp_path):
    def no_network(*args, **kwargs):
        pytest.fail("no request without --probe")
    monkeypatch.setattr(httpx, "get", no_network)
    result = doctor.check_push_relay(relay_config(push_credentials_dir=str(tmp_path / "caps")), False)
    assert result.status == doctor.OK
    assert RELAY in result.detail


def test_apns_settings_under_relay_delivery_warn_that_they_are_ignored(monkeypatch):
    config = relay_config(apns_key_path="/example/AuthKey.p8", apns_key_id="EXAMPLEKEY")
    result = doctor.check_push_relay(config, False)
    assert result.status == doctor.WARN
    assert "ignored" in result.detail
    assert result.remedy


@pytest.mark.parametrize("url", ["", "http://relay.example.com/istota", "https://relay.example.com/istota/"])
def test_a_relay_url_that_is_not_the_normalized_form_fails(url):
    result = doctor.check_push_relay(relay_config(relay_url=url), False)
    assert result.status == doctor.FAIL
    assert result.remedy


def test_the_push_credentials_check_skips_under_relay_delivery():
    result = doctor.check_push_credentials(relay_config(), False)
    assert result.status == doctor.SKIP
    assert "relay" in result.detail.lower()


@pytest.fixture
def relay_probe(monkeypatch):
    """`httpx.get` answered by a `MockTransport`, recording each request and its options."""
    seen = {"requests": [], "kwargs": []}
    answer = {"handler": lambda request: httpx.Response(200, text="ok")}

    def handler(request):
        seen["requests"].append(request)
        return answer["handler"](request)

    def fake_get(url, **kwargs):
        seen["kwargs"].append(kwargs)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return client.get(url, follow_redirects=kwargs.get("follow_redirects", True))

    monkeypatch.setattr(httpx, "get", fake_get)
    return seen, answer


def test_the_probe_asks_healthz_under_the_base_path_without_redirects(relay_probe, tmp_path):
    seen, _ = relay_probe
    result = doctor.check_push_relay(
        relay_config(relay_timeout_seconds=7, push_credentials_dir=str(tmp_path / "caps")), True)
    assert result.status == doctor.OK
    assert [str(r.url) for r in seen["requests"]] == [f"{RELAY}/healthz"]
    assert seen["requests"][0].method == "GET"
    assert seen["kwargs"][0]["follow_redirects"] is False
    assert seen["kwargs"][0]["timeout"] == 7


@pytest.mark.parametrize("status,expected", [
    (503, doctor.WARN), (502, doctor.WARN), (404, doctor.FAIL),
    (302, doctor.FAIL), (401, doctor.FAIL),
])
def test_the_probe_reads_an_outage_as_a_warning_and_a_wrong_url_as_a_failure(relay_probe, status, expected):
    seen, answer = relay_probe
    answer["handler"] = lambda request: httpx.Response(
        status, headers={"location": "https://elsewhere.example.com/"}, text="private body text")
    result = doctor.check_push_relay(relay_config(), True)
    assert result.status == expected
    assert str(status) in result.detail
    assert "private body text" not in result.detail + result.remedy
    assert len(seen["requests"]) == 1


def test_an_unreachable_relay_is_a_warning_naming_the_exception_type_only(relay_probe):
    _, answer = relay_probe

    def refuse(request):
        raise httpx.ConnectError("private failure detail")

    answer["handler"] = refuse
    result = doctor.check_push_relay(relay_config(), True)
    assert result.status == doctor.WARN
    assert "ConnectError" in result.detail
    assert "private failure detail" not in result.detail + result.remedy


# --- The credentials directory arm -------------------------------------------


def caps_config(directory) -> Config:
    return relay_config(push_credentials_dir=str(directory))


def test_a_private_credentials_directory_passes(tmp_path):
    directory = tmp_path / "caps"
    directory.mkdir(mode=0o700)
    (directory / "4.cap").write_text("x")
    (directory / "4.cap").chmod(0o600)
    assert doctor.check_push_relay(caps_config(directory), False).status == doctor.OK


def test_the_default_directory_is_beside_the_signing_key(tmp_path):
    key_dir = tmp_path / "push"
    (key_dir / "relay").mkdir(parents=True, mode=0o755)
    (key_dir / "relay").chmod(0o755)
    config = relay_config(signing_key_path=str(key_dir / "signing.pem"))
    result = doctor.check_push_relay(config, False)
    assert result.status == doctor.FAIL
    assert str(key_dir / "relay") in result.detail


@pytest.mark.parametrize("mode", [0o750, 0o705, 0o755])
def test_a_directory_others_can_read_fails(tmp_path, mode):
    directory = tmp_path / "caps"
    directory.mkdir()
    directory.chmod(mode)
    result = doctor.check_push_relay(caps_config(directory), False)
    assert result.status == doctor.FAIL
    assert f"{mode:04o}" in result.detail


def test_a_readable_capability_file_fails_without_naming_its_contents(tmp_path):
    directory = tmp_path / "caps"
    directory.mkdir(mode=0o700)
    (directory / "4.cap").write_text("private-capability-text")
    (directory / "4.cap").chmod(0o644)
    result = doctor.check_push_relay(caps_config(directory), False)
    assert result.status == doctor.FAIL
    assert "private-capability-text" not in result.detail + result.remedy


def test_a_file_where_the_directory_should_be_fails(tmp_path):
    (tmp_path / "caps").write_text("")
    assert doctor.check_push_relay(caps_config(tmp_path / "caps"), False).status == doctor.FAIL


def test_a_missing_directory_under_a_writable_parent_passes(tmp_path):
    assert doctor.check_push_relay(caps_config(tmp_path / "a" / "caps"), False).status == doctor.OK


@pytest.mark.requires_dac
def test_a_directory_that_cannot_be_created_warns(tmp_path):
    parent = tmp_path / "readonly"
    parent.mkdir()
    parent.chmod(0o500)
    try:
        result = doctor.check_push_relay(caps_config(parent / "caps"), False)
    finally:
        parent.chmod(0o700)
    assert result.status == doctor.WARN
    assert "push_credentials_dir" in result.remedy


@pytest.mark.requires_dac
def test_an_unwritable_directory_warns(tmp_path):
    directory = tmp_path / "caps"
    directory.mkdir(mode=0o500)
    directory.chmod(0o500)
    try:
        result = doctor.check_push_relay(caps_config(directory), False)
    finally:
        directory.chmod(0o700)
    assert result.status == doctor.WARN
