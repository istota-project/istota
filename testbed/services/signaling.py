"""The Nextcloud Talk high-performance backend, run for real.

Rule 4 in `.claude/rules/testbed.md` says not to stub a service whose client
negotiates with it, and `welcome`/`hello` feature negotiation is exactly that:
the client reads the server's advertised feature list before it decides which
hello version to send and whether to expect a relayed comment or a bare
refresh. A stub answering that wrongly steers the daemon down a path no test
chose. So this service starts nothing of its own — it configures and then
attaches to the `signaling` container `docker-compose.yml` and
`docker-compose.test.yml` both declare behind a compose profile, the same
`strukturag/nextcloud-spreed-signaling` image the estate runs and that Nextcloud
All-in-One builds from.

**The internal secret here is the harness's, and istota must never hold one.**
The signaling protocol has two doors. istota goes through the one that
authenticates as its own Nextcloud user, so that Nextcloud authorizes every room
join and istota's reach is its user's reach. The other door — an *internal
client* holding the server's shared secret — joins any room on the instance
(`hub.go:1922-1927`) and leaves no participant row behind, and the whole design
rejects it. The lean shape has no Nextcloud to mint a hello-v2 token, so the
harness uses that second door to *publish* messages the way Talk's
`BackendNotifier` does and to observe a room the way another client would. That
is a property of the test rig, not of the product, and
`tests/smoke/test_signaling_protocol.py` carries a guard asserting that
`build_hello` never produces `auth.type == "internal"` — a test that let it
would be testing the door this design exists to refuse.

On the **full** shape the harness does not use that door at all: there is a real
Nextcloud there, so an observing client authenticates as the test user with a
real hello-v2 token, which is both closer to what istota does and one less
secret in the stack. The container still gets a random internal secret it has no
reader for, because `docker-compose.yml`'s entrypoint wrapper generates one
rather than letting the image's published-constant default stand.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass

#: The image tag both compose files default to. Pinned, and the same version
#: verified on the estate: `chat-relay` landed in 2.1.0, so anything older
#: connects fine and silently only ever sends a bare refresh.
IMAGE_TAG = "2.1.1"

#: The compose service name, which is also the hostname every other container
#: on the compose network reaches it by.
CONTAINER_SERVICE = "signaling"

#: The port the server listens on inside the container.
CONTAINER_PORT = 8080

#: The Nextcloud the server is configured to accept `auth.url` values for.
#:
#: `http://nextcloud` on both shapes, and on the lean shape there is nothing
#: behind it — the server only ever *matches* this string against what a client
#: names, and the backend round trip that would resolve it happens on hello-v1
#: and hello-v2 paths the lean shape cannot reach anyway.
BACKEND_URL = "http://nextcloud"


class SignalingError(RuntimeError):
    """The server refused something the harness asked it to do."""


@dataclass
class SignalingService:
    """The signaling container, configured by the harness and attached to.

    Every field is a per-session secret the pool generated, written into the
    compose env-file rather than committed: the `[clients] internalsecret`,
    `[sessions] hashkey` and `[sessions] blockkey` placeholders in upstream's
    `server.conf.in` are *published constants*, so an image left to its own
    defaults ships an administrative door with a password anybody can read.
    """

    internal_secret: str
    """The `[clients] internalsecret`. The harness's, never istota's."""

    backend_secret: str
    """Shared between Talk and the HPB. `occ talk:signaling:add` on one side,
    `[backend] secret` on the other; a mismatch refuses every hello."""

    hash_key: str
    block_key: str

    name: str = CONTAINER_SERVICE

    _stack: object | None = None

    # -- the Service protocol ------------------------------------------------

    @property
    def container_url(self) -> str:
        """What a process inside the istota container reaches the server on."""
        return f"http://{CONTAINER_SERVICE}:{CONTAINER_PORT}"

    def config_env(self) -> dict[str, str]:
        """Point the daemon straight at the container, rather than at Talk's answer.

        `[talk.signaling] url` is the operator's override for "the daemon must
        reach the HPB by a different route than the one Nextcloud advertises to
        browsers", and a compose deployment is exactly that shape: Talk hands
        out whatever public URL a browser uses while the daemon sits on the
        container network beside the server. Setting it here also means a
        scenario does not depend on which URL `provision-nc.sh` happened to
        register.

        What it deliberately does *not* set is `ISTOTA_TALK_SIGNALING_ENABLED`.
        That is a module switch, so it lives in `stack.FULL_MODULE_SWITCHES`
        beside the other eight — and it must stay off on the lean shape, where
        there is no Nextcloud to report `signalingMode` and the daemon's
        `require_hpb` refusal would stop the container booting at all.
        """
        return {"ISTOTA_TALK_SIGNALING_URL": self.container_url}

    def compose_env(self) -> dict[str, str]:
        """What the compose files need to run and reach the container.

        These configure the *container*, not the daemon, so the two-file rule
        that governs `config_env()` does not reach them — see `stack.compose_env`.
        Both shapes read the same names, because both declare the service from
        the same set of variables.

        `ISTOTA_TALK_SIGNALING_PORT` is `0`: the harness reads the real one back
        with `docker compose port`, so two sessions never collide on a fixed
        one. `_SERVER` and `_SECRET` are what `provision-nc.sh` registers with
        Talk at first install, and registering *something* is what puts Talk in
        `external` signaling mode — without which there are no `helloAuthParams`
        for istota to authenticate with and the daemon refuses to start.
        """
        return {
            "ISTOTA_TALK_SIGNALING_IMAGE_TAG": IMAGE_TAG,
            "ISTOTA_TALK_SIGNALING_BIND": "127.0.0.1",
            "ISTOTA_TALK_SIGNALING_PORT": "0",
            "ISTOTA_TALK_SIGNALING_SERVER": self.container_url,
            "ISTOTA_TALK_SIGNALING_SECRET": self.backend_secret,
            "ISTOTA_TALK_SIGNALING_HASH_KEY": self.hash_key,
            "ISTOTA_TALK_SIGNALING_BLOCK_KEY": self.block_key,
            "ISTOTA_TALK_SIGNALING_INTERNAL_SECRET": self.internal_secret,
        }

    def reset(self) -> None:
        """Nothing to undo, and that is a fact about this service rather than a gap.

        It records no calls — the harness observes it by holding a WebSocket,
        which the scenario opens and closes itself — and it keeps no state a
        scenario writes: a signaling room exists only while a session is in it,
        so the last test's rooms are gone the moment its connections close.
        `container_state_paths` is absent for the same reason; the server writes
        nothing to disk but the config it renders at first start.
        """

    def close(self) -> None:
        """Idempotent, and there is nothing to close: the container is compose's."""

    def bind_stack(self, stack) -> None:
        """Take the stack, so `ws_url` can read the published port back.

        The same hook `nextcloud` and `mail` use, and for the same reason: this
        service attaches to a container the compose file runs, so the address
        the *harness* reaches it on is not known until the stack is up.
        """
        self._stack = stack

    def describe(self) -> str:  # pragma: no cover - diagnostic
        return f"signaling: {CONTAINER_SERVICE}:{CONTAINER_PORT}, backend {BACKEND_URL}"

    # -- what a scenario drives ----------------------------------------------

    @property
    def host_port(self) -> int:
        if self._stack is None:
            raise SignalingError(
                "the signaling service has no stack bound, so it cannot know "
                "which host port compose published; `bind_stack` runs during boot"
            )
        return self._stack.published_port(CONTAINER_SERVICE, CONTAINER_PORT)

    @property
    def ws_url(self) -> str:
        """The WebSocket URL *the harness* connects on.

        Not `container_url`: that name resolves only inside the compose network
        and this runs in the pytest process. `websocket_url` is istota's own
        derivation rule, exercised here rather than restated.
        """
        from istota.transport.talk import signaling as sig

        return sig.websocket_url(f"http://127.0.0.1:{self.host_port}")

    def internal_hello(self, request_id: str = "1", *, features=("chat-relay",)) -> dict:
        """A hello frame for the harness's own internal client.

        Deliberately hand-built here and **not** taken from
        `signaling.build_hello`, which cannot produce one: that function has no
        branch that emits `auth.type`, which is what a test in
        `tests/smoke/test_signaling_protocol.py` asserts. Two doors, and only
        the harness has a key to this one.
        """
        random = secrets.token_hex(16)
        token = hmac.new(
            self.internal_secret.encode(), random.encode(), hashlib.sha256
        ).hexdigest()
        return {
            "id": request_id,
            "type": "hello",
            "hello": {
                "version": "1.0",
                "features": list(features),
                "auth": {
                    "type": "internal",
                    "params": {
                        "random": random,
                        "token": token,
                        "backend": BACKEND_URL,
                    },
                },
            },
        }

    def publish_chat(
        self,
        room_token: str,
        comment: dict | None = None,
        *,
        refresh_only: bool = False,
        timeout: float = 10.0,
    ) -> None:
        """Publish a chat message into a room the way Talk's `BackendNotifier` does.

        `POST /api/v1/room/{token}` with the three `Spreed-Signaling-*` headers,
        the checksum being an HMAC over the random value concatenated with the
        body. The payload shape is `Listener::notifyMessageSent`'s
        (`Listener.php:505-551`): a `chat` object carrying `refresh` and, unless
        Talk withheld it, the serialized `comment`.

        This is the lean shape's substitute for a Nextcloud, and it is the only
        thing it substitutes: what the *server* then does with the message —
        whether it relays the comment or strips it to a bare refresh, and to
        which sessions — is the real server's own behaviour, which is the whole
        reason the container is here rather than a stub.
        """
        chat: dict = {"refresh": True}
        if not refresh_only:
            if comment is None:
                raise SignalingError(
                    "publish_chat needs a comment unless refresh_only is set; a "
                    "message with neither is a frame Talk never sends"
                )
            chat["comment"] = comment
        body = json.dumps(
            {"type": "message", "message": {"data": {"type": "chat", "chat": chat}}}
        ).encode()

        random = secrets.token_hex(32)
        checksum = hmac.new(
            self.backend_secret.encode(), random.encode() + body, hashlib.sha256
        ).hexdigest()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.host_port}/api/v1/room/{room_token}",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Spreed-Signaling-Random": random,
                "Spreed-Signaling-Checksum": checksum,
                "Spreed-Signaling-Backend": BACKEND_URL,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:  # pragma: no cover - server fault
            raise SignalingError(
                f"the signaling server refused the backend room request: "
                f"{error.code} {error.reason}"
            ) from None
        if isinstance(payload, dict) and payload.get("type") == "error":
            raise SignalingError(f"backend room request failed: {payload}")


def serve(
    *,
    internal_secret: str = "",
    backend_secret: str = "",
    hash_key: str = "",
    block_key: str = "",
) -> SignalingService:
    """Configure the service, generating any secret the caller did not name.

    Generated rather than defaulted to a constant, for the reason the compose
    entrypoint generates them too: every one of these has a published value in
    upstream's `server.conf.in`, and a committed test secret in a public repo is
    the same shape of mistake one step removed.

    `token_urlsafe` for three of them and a 32-character hex value for the block
    key, which the server requires to be exactly 16, 24 or 32 bytes.
    """
    return SignalingService(
        internal_secret=internal_secret or secrets.token_urlsafe(24),
        backend_secret=backend_secret or secrets.token_urlsafe(24),
        hash_key=hash_key or secrets.token_urlsafe(24),
        block_key=block_key or secrets.token_hex(16),
    )
