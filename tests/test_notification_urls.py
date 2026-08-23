"""The runtime URL allowlist, fed hostile ids through the real resolvers.

`object_id` is opaque TEXT so a source can key on something non-integer, and
every action path a resolver emits is built by interpolating it. A row whose
`object_id` is `1/../../admin/x` would otherwise yield a server-supplied path the
client POSTs with the session cookie; `link` and `href` are worse, because they
land in an anchor where `javascript:` or an off-origin absolute URL sails past
any text-node rule.

So there are two guards and both are tested here: a resolver coerces or
validates its id *before* interpolating, and `list_open` re-checks every
URL-carrying field of every view at runtime. The second is the one that has to
be exercised against a resolver that actually gets it wrong — a check that only
ever sees benign ids passes trivially and falsifies nothing.

Also asserted: every path the registered resolvers really do emit names a route
that exists on `api_router`. The design rests on resolvers naming real paths, so
a typo here is a button that silently 404s.
"""

from __future__ import annotations

import re

import pytest

from istota import (
    db,
    notification_sources as sources,
    notification_store as store,
    outbound_drafts as drafts,
)
from istota.config import Config, UserConfig
from istota.notification_resolvers import confirmation as confirmation_source
from istota.notification_resolvers import connected_service as service_source
from istota.notification_resolvers import cron_job as cron_source
from istota.notification_resolvers import health_panel as panel_source
from istota.notification_resolvers import outbound_draft as draft_source
from istota.notification_resolvers import task_alert as task_alert_source

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web_deps = True
except ImportError:
    _has_web_deps = False


# Values a source could plausibly be tricked into keying on, each of which
# breaks a *different* part of a naive `f"/chat/tasks/{id}/confirm"`.
HOSTILE_IDS = [
    "1/../../admin/x",
    "1/../admin",
    "1?x=1",
    "1%2F",
    "https://evil.example",
    "javascript:alert(1)",
    "1\nSet-Cookie: a=b",
    "1 2",
    "../..",
    "",
    "-1",
    "1#frag",
]


@pytest.fixture(autouse=True)
def _registry():
    sources.reset_registry()
    yield
    sources.reset_registry()


@pytest.fixture
def config(tmp_path):
    return Config(
        db_path=tmp_path / "test.db",
        nextcloud_mount_path=tmp_path / "mount",
        users={"alice": UserConfig(display_name="Alice")},
    )


@pytest.fixture
def conn(config):
    db.init_db(config.db_path)
    with db.get_db(config.db_path) as c:
        yield c


def _emitted_paths(item) -> list[str]:
    out: list[str] = []
    if item.link is not None:
        out.append(item.link)
    for action in item.actions:
        if action.endpoint is not None:
            out.append(action.endpoint)
        if action.href is not None:
            out.append(action.href)
    return out


# ---------------------------------------------------------------------------
# 1. hostile object_ids through the registered resolvers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source_module",
    [confirmation_source, draft_source, cron_source, service_source, panel_source],
)
@pytest.mark.parametrize("hostile", HOSTILE_IDS)
def test_a_hostile_object_id_never_reaches_the_client(
    config, conn, source_module, hostile,
):
    """Whatever the row says, no unsafe path is ever emitted.

    Either the resolver refuses the id (the row is swept `stale` and vanishes)
    or the view is downgraded to stored text with no actions. Both are fine;
    an emitted `/chat/tasks/1/../../admin/x/confirm` is not.

    Every registered source is fed the same values, including the one whose ids
    are not integers: `connected_service` keys on a service *name*, so `int()`
    is not available to it and an explicit allowlist check stands in. A source
    that validates its ids by not having any interesting ones is exactly the
    kind this file exists to catch.
    """
    store.write_notification(
        conn, "alice",
        source=source_module.SOURCE,
        dedup_key=f"{source_module.OBJECT_TYPE}:{hostile}",
        title="something is waiting",
        object_type=source_module.OBJECT_TYPE,
        object_id=hostile,
        actionable=True,
    )

    items, _total = store.list_open(config, conn, "alice")

    # Stated rather than left implicit: every one of these ids is refused by the
    # resolver's own `int()` coercion, so the row is swept `stale` and the loop
    # below has nothing to iterate. Asserting only inside the loop would make
    # the whole parametrization pass by never executing — which is exactly the
    # vacuity this file's docstring says a URL test must not have.
    assert items == [], (
        f"{source_module.SOURCE} rendered a row for object_id {hostile!r}"
    )
    for item in items:  # pragma: no cover - defence if the refusal ever changes
        for path in _emitted_paths(item):
            assert sources.is_safe_path(path), (
                f"{source_module.SOURCE} emitted {path!r} for object_id {hostile!r}"
            )


def test_a_coercible_id_is_interpolated_as_the_int_not_as_the_stored_text(
    config, conn,
):
    """The coercion is what builds the path, not `row.object_id`.

    ` 1 ` and `+1` are the case the refusals above cannot reach: both name a
    real task, so the resolver renders — and a path built by interpolating the
    *stored* text rather than the parsed int would emit `/chat/tasks/ 1 /confirm`,
    which the allowlist then has to catch. It should never get that far.
    """
    task_id = db.create_task(
        conn, prompt="do it", user_id="alice", source_type="web",
    )
    db.set_task_confirmation(conn, task_id, "Shall I proceed?")
    store.write_notification(
        conn, "alice", source=confirmation_source.SOURCE,
        dedup_key="task:padded", title="waiting",
        object_type=confirmation_source.OBJECT_TYPE,
        object_id=f" {task_id} ", actionable=True,
    )

    items, _total = store.list_open(config, conn, "alice")
    assert len(items) == 1
    endpoints = {a.id: a.endpoint for a in items[0].actions}
    assert endpoints["confirm"] == f"/chat/tasks/{task_id}/confirm"
    for path in _emitted_paths(items[0]):
        assert sources.is_safe_path(path)


# ---------------------------------------------------------------------------
# 2. the runtime backstop, against a resolver that really does get it wrong
# ---------------------------------------------------------------------------


class _BadResolver:
    source = "bad_source"
    auto_resolve_on_seen = False

    def __init__(self, view):
        self._view = view

    def resolve(self, config, conn, row):
        return self._view


@pytest.mark.parametrize(
    "view_kwargs",
    [
        {"link": "https://evil.example/steal"},
        {"link": "javascript:alert(1)"},
        {"link": "/chat/../admin"},
        {"link": "/chat\n"},
        {"actions": (
            sources.NotificationAction(
                id="go", label="Go", kind="primary", method="POST",
                endpoint="/chat/tasks/1/../../admin/wipe",
            ),
        )},
        {"actions": (
            sources.NotificationAction(
                id="go", label="Go", kind="default", method="LINK",
                href="//evil.example/steal",
            ),
        )},
        # A POST action carrying a hostile *href*: `to_dict` serializes both
        # fields whatever the method says, so a branch on `method` would ship
        # this to the client unvalidated.
        {"actions": (
            sources.NotificationAction(
                id="go", label="Go", kind="primary", method="POST",
                endpoint="/chat/tasks/1/confirm", href="javascript:alert(1)",
            ),
        )},
    ],
)
def test_an_unsafe_view_is_downgraded_not_emitted(config, conn, caplog, view_kwargs):
    sources.register(_BadResolver(sources.NotificationView(
        title="rendered title", body="rendered body", **view_kwargs,
    )))
    store.write_notification(
        conn, "alice", source="bad_source", dedup_key="x:1",
        title="stored title", body="stored body", actionable=True,
    )

    with caplog.at_level("ERROR"):
        items, total = store.list_open(config, conn, "alice")

    assert total == 1
    assert len(items) == 1
    item = items[0]
    assert item.title == "stored title"
    assert item.body == "stored body"
    assert item.link is None
    assert item.actions == ()
    assert item.status_note
    assert _emitted_paths(item) == []
    assert any(
        record.levelname == "ERROR" and "bad_source" in record.getMessage()
        for record in caplog.records
    ), "an unsafe path must be logged at ERROR with the source name"


# ---------------------------------------------------------------------------
# 3. the paths the real resolvers emit are routes that exist
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _has_web_deps, reason="web dependencies not installed")
def test_every_emitted_path_names_a_real_api_route(config, conn):
    from istota import web_app

    task_id = db.create_task(
        conn, prompt="do it", user_id="alice", source_type="web",
        conversation_token="room-1",
    )
    db.set_task_confirmation(conn, task_id, "Shall I proceed?")
    confirmation_source.write(
        conn, "alice", task_id=task_id, title="Shall I proceed?",
    )

    draft_id = drafts.hold(
        conn, user_id="alice", task_id=None, room_token=None,
        to_addrs=["someone@example.invalid"], cc_addrs=[], bcc_addrs=[],
        subject="Re: hi", body="hello", html=False,
        in_reply_to=None, references=None, attachments=[],
        origin_target=None, hold_reason="untrusted_recipient",
    )
    draft_source.write(
        conn, "alice", draft_id=draft_id, title="A reply is waiting",
    )

    items, _total = store.list_open(config, conn, "alice")
    emitted = [p for item in items for p in _emitted_paths(item)]
    assert emitted, "neither resolver emitted an action path"

    # The router's own paths carry its `/istota/api` prefix; an emitted path is
    # apiFetch-relative, because that is the form the client's fetcher takes.
    # Prepending the prefix here is what asserts the two halves compose — a path
    # written `/api/notifications/...` would resolve to `/istota/api/api/...`
    # and match nothing.
    prefix = web_app.api_router.prefix
    patterns = [
        re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", route.path) + "$")
        for route in web_app.api_router.routes
        if getattr(route, "path", None)
    ]
    for path in emitted:
        assert sources.is_safe_path(path)
        assert any(p.match(prefix + path) for p in patterns), (
            f"{path!r} names no route on api_router"
        )


@pytest.mark.skipif(not _has_web_deps, reason="web dependencies not installed")
def test_paths_are_apifetch_relative_not_double_prefixed(config, conn):
    """`/notifications/...`, never `/api/notifications/...`.

    `apiFetch` prepends `/istota/api`, so a path written with its own `/api`
    prefix resolves to `/istota/api/api/...` and 404s.
    """
    task_id = db.create_task(
        conn, prompt="do it", user_id="alice", source_type="web",
    )
    db.set_task_confirmation(conn, task_id, "Shall I proceed?")
    confirmation_source.write(
        conn, "alice", task_id=task_id, title="Shall I proceed?",
    )
    items, _total = store.list_open(config, conn, "alice")
    for path in (p for item in items for p in _emitted_paths(item)):
        assert not path.startswith("/api/")
        assert not path.startswith("/istota/")


# ---------------------------------------------------------------------------
# 4. `task_alert` emits no URL of any kind, on any path
# ---------------------------------------------------------------------------


TASK_ALERT_KEYS = [
    "task:1:security",
    "task:1:action_needed",
    "throttle:held",
    "throttle:throttled",
    "expired:1",
    "dmarc:fail",
    "undelivered:1",
]

# Text a model could write into the JSON file `_process_deferred_user_alerts`
# reads from inside the sandbox. A `link` is rendered into an anchor, where a
# text-node rule buys nothing and `javascript:` or an off-origin absolute URL
# sails straight through.
MODEL_AUTHORED = [
    "https://evil.example/steal",
    "javascript:alert(1)",
    "[click me](https://evil.example)",
    "/istota/api/chat/tasks/1/confirm",
    "//evil.example",
    "1/../../admin/x",
]


@pytest.mark.parametrize("key", TASK_ALERT_KEYS)
@pytest.mark.parametrize("hostile", MODEL_AUTHORED)
def test_task_alert_never_emits_a_link_or_a_link_action(config, conn, key, hostile):
    """Unconditional, and the one rule in this file with no escape hatch.

    Every other source builds a path from an id it coerces first, with the
    runtime allowlist behind it as a backstop. This source builds none at all:
    its content is model-authored, so the guarantee is structural rather than
    validated.
    """
    sources.register(task_alert_source.RESOLVER)
    task_alert_source.write(
        conn, "alice",
        dedup_key=key,
        title=hostile,
        body=hostile,
        params={"messages": [hostile], "link": hostile, "href": hostile},
    )

    items, total = store.list_open(config, conn, "alice")
    assert total == 1
    assert _emitted_paths(items[0]) == []
    assert items[0].link is None
    assert items[0].actions == ()
    # And the rendered text carries no live markup either — this class is
    # delivered into Talk, which renders it.
    for char in "[]()`*_~<>|":
        assert char not in items[0].title
        assert char not in items[0].body


def test_task_alert_is_registered_and_auto_resolving(config, conn):
    resolver = sources.get_resolver("task_alert")
    assert resolver is not None
    assert resolver.auto_resolve_on_seen is True
    assert "task_alert" in sources.auto_resolve_sources()
