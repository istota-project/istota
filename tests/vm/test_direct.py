"""Parity row 19: TLS in `direct` ingress, with a certificate from ACME.

`istota setup --ingress direct --tls-cert-source acme` on the VM, certbot on
the VM (`istota-certbot.service`, started by `istota-stack up` while there is
no certificate) and the compose nginx terminating TLS. The CA is Pebble, the
ACME test server, in a container in the VM, with a DNS server beside it that
resolves every name to the VM; nothing here reaches a real CA. certbot is
pointed at it by `ACME_SERVER` and trusts its directory through
`REQUESTS_CA_BUNDLE`, both in host.env.

The witness: the certificate is issued and served, port 80 redirects to https,
a TLS 1.1 handshake is refused by the server, HSTS is sent, doctor's `web.tls`
is OK (given Pebble's root, which no client trusts by default), and the private
key is nowhere in the istota container.

Control `certbot-disabled`: the certificate is removed and certbot masked before
the first issuance, so nginx stays on its port-80 bootstrap: `web.tls` FAILs
and nothing listens on 443.
"""

from __future__ import annotations

import json

import pytest

from tests.support import parity

from . import lima

pytestmark = pytest.mark.vm

RESOLVE = f"--resolve {lima.DOMAIN}:443:127.0.0.1 --resolve {lima.DOMAIN}:80:127.0.0.1"
ROOT = f"{lima.SCRATCH}/pebble-root.pem"
CONTAINER_ROOT = "/tmp/vmtier-pebble-root.pem"


@pytest.fixture(scope="module")
def stack(direct_stack):
    vm = direct_stack
    if lima.control() == "certbot-disabled":
        vm.run(f"""
systemctl disable --now istota-certbot.timer >/dev/null 2>&1 || true
systemctl mask istota-certbot.service >/dev/null
rm -rf {lima.STACK}/letsencrypt/*
istota-stack compose restart nginx >/dev/null
""")
        try:
            yield vm
        finally:
            vm.run("systemctl unmask istota-certbot.service >/dev/null; "
                   "systemctl enable --now istota-certbot.timer >/dev/null")
            lima.ensure_certificate(vm)
        return
    lima.ensure_certificate(vm)
    yield vm


@parity.witness(19)
class TestDirectIngressServesTls:
    def test_the_certificate_is_issued_and_served(self, stack):
        listening = stack.out("ss -Hltn 'sport = :443' | wc -l")
        assert listening != "0", "row 19: nothing listens on port 443"
        chain = stack.out(
            f"echo | openssl s_client -connect 127.0.0.1:443 -servername {lima.DOMAIN} "
            f"-CAfile {ROOT} -verify_return_error 2>&1 || true")
        assert "Verify return code: 0 (ok)" in chain, chain[-2000:]
        assert "Pebble Intermediate" in chain, chain[-2000:]

    def test_port_80_redirects_to_https(self, stack):
        head = stack.out(f"curl -s -o /dev/null -w '%{{http_code}} %{{redirect_url}}' {RESOLVE} http://{lima.DOMAIN}/istota/")
        assert head == f"301 https://{lima.DOMAIN}/istota/", head

    def test_a_tls_1_1_handshake_is_refused_by_the_server(self, stack):
        modern = stack.out(
            f"echo | openssl s_client -connect 127.0.0.1:443 -servername {lima.DOMAIN} -tls1_2 2>&1 || true")
        assert "Protocol  : TLSv1.2" in modern or "Protocol: TLSv1.2" in modern, modern[-1500:]
        old = stack.out(
            f"echo | openssl s_client -connect 127.0.0.1:443 -servername {lima.DOMAIN} -tls1_1 "
            "-cipher 'DEFAULT:@SECLEVEL=0' 2>&1 || true")
        # The server's alert, not a client that never offered TLS 1.1.
        assert "alert protocol version" in old, old[-1500:]

    def test_hsts_is_sent(self, stack):
        head = stack.out(f"curl -s -D - -o /dev/null --cacert {ROOT} {RESOLVE} https://{lima.DOMAIN}/istota/")
        assert "strict-transport-security: max-age=31536000" in head.lower(), head

    def test_doctor_reports_web_tls_ok(self, stack):
        lima.stack_exec(stack, f"cat > {CONTAINER_ROOT}", stdin_file=ROOT)
        result = lima.doctor(stack, "web.tls", env=f"SSL_CERT_FILE={CONTAINER_ROOT}")
        assert result["status"] == "ok", result

    def test_the_private_key_is_not_in_the_istota_container(self, stack):
        for service in ("istota", "web"):
            cid = lima.container(stack, service)
            mounts = json.loads(stack.out(f"docker inspect -f '{{{{json .Mounts}}}}' {cid}"))
            sources = [m["Source"] for m in mounts]
            assert not any("letsencrypt" in s or s.endswith("/certs") for s in sources), sources
        found = lima.stack_exec(
            stack, "find / -xdev \\( -name 'privkey*.pem' -o -path '*letsencrypt*' \\) 2>/dev/null | head -5",
            check=False).stdout.strip()
        assert found == "", f"row 19: key material in the istota container: {found}"
