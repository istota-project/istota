"""Deployment wiring for the provider-neutral SMS webhook surface."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
COMPOSE = REPO / "docker" / "docker-compose.yml"
DOCKER_NGINX = REPO / "docker" / "nginx" / "istota.conf.template"


def test_the_web_service_serves_every_webhook_route():
    """The receiver is no service of its own: its routes are included into the
    web process (webui/stack_app.py), as `istota serve` includes them."""
    compose = yaml.safe_load(COMPOSE.read_text())
    services = compose["services"]
    web = services["web"]

    assert "webhooks" not in services
    assert "istota.webui.stack_app:app" in web["entrypoint"][-1]
    # Always running: a feature switched on in the config needs no profile.
    assert "profiles" not in web
    assert "ports" not in web
    assert web["expose"] == ["${WEB_PORT:-8766}"]
    # Deliberately no assertion on the location default. Flipping it to
    # `false` disables a module for every Docker deployment that never set it,
    # which is an unannounced behaviour change and no part of the SMS work —
    # the two subsystems share this service through `profiles`, and nothing in
    # SMS needs location off. Pinning the flip here would have made the
    # out-of-scope change the tested behaviour. The SMS settings themselves
    # are config.toml keys now; compose passes no istota configuration.


def test_docker_nginx_keeps_webhooks_behind_the_shared_proxy():
    nginx = DOCKER_NGINX.read_text()

    assert "set $upstream_webhooks  web:${WEB_PORT};" in nginx
    assert "location /webhooks/" in nginx
    assert "proxy_pass http://$upstream_webhooks" in nginx
