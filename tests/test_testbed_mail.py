"""The mail service's wiring, and the certificate under it. No Docker.

Its sibling `test_testbed_services.py` covers the shared HTTP base, the profile
table and the two-file config rule — which now includes `mail`, since that is
what the rule's two files were out of step about. What is here is everything
specific to running a real mail server: the certificate both readers verify
against, the compose variables the overlay binds, and the split between the two
environment maps a service can contribute to.

All of it is in the default suite, deliberately. The wire and mail tiers are
deselected and a developer may go weeks without running either; a testbed whose
own wiring is only checked behind a marker rots.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from cryptography import x509

from testbed import certs, profiles
from testbed import stack as stack_support
from testbed.services import gitlab, mail

#: A stand-in for what `StackPool` generates on the full shape. Obviously fake,
#: so a reader never wonders whether these are real and a scan never flags them.
_CREDENTIALS = stack_support.FullCredentials(
    postgres_password="unit-test-postgres",
    admin_password="unit-test-admin",
    bot_password="unit-test-bot",
    user_password="unit-test-user",
    nc_port=18080,
)


class TestTheCertificateGenerator:
    """`testbed/certs.py`. No server — just the files it writes.

    Small, and worth testing anyway: every TLS assertion in the wire and mail
    tiers rests on this certificate being one both sides can verify, and the two
    ways it silently fails — a name missing from the SAN, an address written as
    a `DNSName` — look identical from the failing end.
    """

    def test_it_writes_a_certificate_and_a_private_key(self, tmp_path):
        crt, key = certs.generate_self_signed(tmp_path / "tls")

        assert crt.name == "mail.crt"
        assert key.name == "mail.key"
        assert crt.read_bytes().startswith(b"-----BEGIN CERTIFICATE-----")
        assert key.read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")

    def test_the_key_is_readable_by_its_owner_alone(self, tmp_path):
        _, key = certs.generate_self_signed(tmp_path / "tls")

        assert certs.key_is_private(key)

    def test_the_key_is_created_private_rather_than_made_private_after(
        self, tmp_path, monkeypatch
    ):
        """The mode the file is *created* with, not the mode it ends up with.

        `write_bytes` then `chmod` leaves a private key world-readable for the
        length of the write, and a test reading the final mode passes on exactly
        that bug. Same lesson `stack.write_env_file` learned; the check has to
        watch the `os.open` call.
        """
        seen: list[int] = []
        real_open = os.open

        def watching(path, flags, mode=0o777, **kwargs):
            if str(path).endswith("mail.key"):
                seen.append(mode)
            return real_open(path, flags, mode, **kwargs)

        monkeypatch.setattr(certs.os, "open", watching)
        certs.generate_self_signed(tmp_path / "tls")

        assert seen == [0o600], seen

    def test_every_san_is_present_and_an_address_is_written_as_one(self, tmp_path):
        """An address in a `DNSName` entry fails verification and says nothing
        useful about why.

        One certificate covers both readers: a container reaches the server as
        `mail`, the pytest process reaches the same container on loopback.
        """
        crt, _ = certs.generate_self_signed(tmp_path / "tls")
        parsed = x509.load_pem_x509_certificate(crt.read_bytes())
        san = parsed.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value

        assert set(san.get_values_for_type(x509.DNSName)) == {"mail", "localhost"}
        assert [
            str(address) for address in san.get_values_for_type(x509.IPAddress)
        ] == ["127.0.0.1"]

    def test_a_second_call_returns_the_same_material(self, tmp_path):
        """Idempotent, because a session fixture and a compose bind mount may
        both run against one directory — and regenerating underneath a running
        container leaves the server holding a key the client no longer trusts."""
        first_crt, first_key = certs.generate_self_signed(tmp_path / "tls")
        first_bytes = first_crt.read_bytes()

        second_crt, second_key = certs.generate_self_signed(tmp_path / "tls")

        assert (second_crt, second_key) == (first_crt, first_key)
        assert second_crt.read_bytes() == first_bytes


class TestTheMailService:
    """What `mail.serve` produces, with no container behind it."""

    def test_it_starts_nothing(self, tmp_path):
        """The overlay runs the container; this only prepares what it binds.

        Worth pinning, because the obvious implementation is the wrong one. A
        container the pytest process started would be reachable from the daemon
        only as `host.docker.internal` on an ephemeral port, and istota reads
        993/465 as implicit TLS and anything else as STARTTLS — so the deployed
        path would silently exercise the branch the deployment does not use.
        """
        service = mail.serve(tmp_path / "mail")

        assert service.cert_file.exists()
        assert service.key_file.exists()
        with pytest.raises(RuntimeError, match="published ports"):
            service.server

    def test_reset_and_describe_are_safe_before_the_stack_is_up(self, tmp_path):
        """Both are reached by a failed boot's cleanup path, where there is no
        stack to talk to."""
        service = mail.serve(tmp_path / "mail")

        service.reset()
        assert "no published ports" in service.describe()
        service.close()

    def test_compose_env_names_paths_that_exist(self, tmp_path):
        """Every one of these is a bind source in `mail.yml`, and Docker treats a
        missing source as a directory to create rather than as an error — so a
        typo here brings the container up with an empty `maddy.conf` and a
        server that answers nothing."""
        service = mail.serve(tmp_path / "mail")

        environment = service.compose_env()
        for key in (
            "ISTOTA_TESTBED_MAIL_CONF",
            "ISTOTA_TESTBED_MAIL_CERT",
            "ISTOTA_TESTBED_MAIL_KEY",
        ):
            assert Path(environment[key]).exists(), (key, environment[key])
        assert environment["ISTOTA_TESTBED_MAIL_IMAGE"] == mail.mail_image()

    def test_the_image_is_pinned_by_digest_and_overridable(self, monkeypatch):
        """`:latest` is acceptable for a manually driven campaign and not for a
        suite that has to give the same answer next month."""
        assert "@sha256:" in mail.MAIL_IMAGE

        monkeypatch.setenv(mail.MAIL_IMAGE_ENV, "foxcpp/maddy:experimental")
        assert mail.mail_image() == "foxcpp/maddy:experimental"

    def test_the_entrypoint_script_and_this_module_agree(self):
        """The drift guard the shared entrypoint exists to make possible.

        The script runs two ways — `docker run` and a compose `entrypoint:` —
        so it is one file rather than two copies, since a compose entrypoint has
        to write `$$` for every `$` and an inline copy would differ character by
        character. It still holds the addresses and the password as shell
        literals, and nothing else would notice those parting company with the
        constants every test uses.
        """
        script = mail.ENTRYPOINT.read_text()

        for account in mail.ACCOUNTS:
            assert account in script, account
        assert f"'{mail.MAIL_PASSWORD}'" in script

    def test_the_overlay_the_profiles_name_is_the_one_this_module_ships(self):
        assert profiles.MAIL_OVERLAY == mail.OVERLAY
        assert mail.OVERLAY.exists()

    def test_both_mail_profiles_carry_the_overlay_and_the_service(self):
        """A profile naming the service without the overlay boots a daemon
        configured to poll a mail server that does not exist, and one naming the
        overlay without the service brings a container up that nothing points
        at. Neither fails loudly."""
        for profile in (profiles.MAIL, profiles.FULL):
            assert "mail" in profile.services, profile.name
            assert mail.OVERLAY in profile.compose_overlays, profile.name


class TestComposeEnvIsSeparateFromConfigEnv:
    """Two maps, two rules, and conflating them would cost the tier its honesty.

    `config_env()` points the *daemon* at a service and may name only variables
    the shipped generator reads and `docker-compose.yml` passes through.
    `compose_env()` names host paths an overlay binds, which configure nothing
    about istota and appear in no shipped file. The guards below are what keep
    one from being smuggled in as the other.
    """

    def test_a_service_may_not_claim_a_variable_another_already_set(self, tmp_path):
        service = mail.serve(tmp_path / "mail")
        claimed = {"ISTOTA_TESTBED_MAIL_CONF": "someone-else"}

        with pytest.raises(stack_support.StackError, match="both set"):
            stack_support.compose_env({"mail": service}, claimed=claimed)

    def test_a_service_may_not_claim_a_variable_the_stack_owns(self, tmp_path):
        service = mail.serve(tmp_path / "mail")

        with pytest.raises(stack_support.StackError, match="the stack itself owns"):
            stack_support.compose_env(
                {"mail": service}, reserved={"ISTOTA_TESTBED_MAIL_CONF"}
            )

    def test_a_service_with_nothing_to_bind_contributes_nothing(self, tmp_path):
        """Optional on the protocol and read by `getattr`: four of the six
        services need no overlay and would otherwise carry an empty method
        apiece."""
        forge = gitlab.serve(tmp_path / "repos")
        try:
            assert stack_support.compose_env({"gitlab": forge}) == {}
        finally:
            forge.close()

    def test_the_full_env_carries_the_overlays_variables_too(self, tmp_path):
        """`full_env` is the whole answer to "what does a full profile boot", so
        an overlay variable missing from it is a `${…:?}` failure at `up` naming
        a key nobody set."""
        service = mail.serve(tmp_path / "mail")

        environment = stack_support.full_env({"mail": service}, _CREDENTIALS)

        assert environment["ISTOTA_TESTBED_MAIL_CONF"] == str(mail.CONF_DIR)
        assert environment["ISTOTA_EMAIL_ENABLED"] == "true"

    def test_a_profile_without_mail_leaves_the_email_module_off(self):
        """The map is what makes `Profile` mean anything on the full shape:
        `docker-compose.yml` defaults email on, so a profile that did not name
        the service would still boot a daemon polling one."""
        environment = stack_support.full_env({}, _CREDENTIALS)

        assert environment["ISTOTA_EMAIL_ENABLED"] == "false"


class TestPublishedPort:
    """`Stack.published_port`, which is how a service learns an ephemeral port."""

    @staticmethod
    def _stack() -> stack_support.Stack:
        return stack_support.Stack(
            profile=profiles.MAIL, args=["docker", "compose"], services={}
        )

    @staticmethod
    def _answering(stdout: str, returncode: int = 0):
        def run(argv, *args, **kwargs):
            return subprocess.CompletedProcess(argv, returncode, stdout, "")

        return run

    def test_it_reads_the_port_off_composes_answer(self, monkeypatch):
        monkeypatch.setattr(
            stack_support.subprocess, "run", self._answering("0.0.0.0:54321\n")
        )

        assert self._stack().published_port("mail", 993) == 54321

    def test_it_takes_the_first_of_several_interfaces(self, monkeypatch):
        """A port published on more than one interface answers with a line each.
        Either reaches the same container, so the first will do — but reading
        the whole block as one string would not."""
        monkeypatch.setattr(
            stack_support.subprocess,
            "run",
            self._answering("127.0.0.1:54321\n[::1]:54321\n"),
        )

        assert self._stack().published_port("mail", 993) == 54321

    def test_an_unpublished_port_is_refused_rather_than_guessed_at(self, monkeypatch):
        """Compose exits 0 with empty output for a port it does not publish, so
        "no answer" has to be caught here — otherwise it arrives as a connection
        to port 0, several layers away from the overlay that forgot to publish
        it."""
        monkeypatch.setattr(stack_support.subprocess, "run", self._answering(""))

        with pytest.raises(stack_support.StackError, match="which host port"):
            self._stack().published_port("mail", 993)

    def test_an_answer_that_is_not_an_address_is_refused(self, monkeypatch):
        monkeypatch.setattr(
            stack_support.subprocess, "run", self._answering("no such service\n")
        )

        with pytest.raises(stack_support.StackError, match="does not end in a port"):
            self._stack().published_port("mail", 993)
