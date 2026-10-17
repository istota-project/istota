"""The two health download routes serve the file they opened, not a name.

``/panels/{id}/source`` and ``/documents/{id}/file`` read from the uploads
tree in the user's workspace, which their tasks can write. A route that
checks a resolved path and then lets ``FileResponse`` open the name again
serves whatever a task swapped in between; so does a walk anchored at a
directory inside that tree, since the anchor itself is opened following
links. Each case here plants an outside sentinel and requires that its bytes
are never served.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from istota.health import db as health_db
from istota.health import routes as health_routes
from tests.support.health_extract import PREFIX, handoff_app, handoff_config

INSIDE = b"%PDF-1.4 the user's own scan"
SENTINEL = b"%PDF-1.4 OUTSIDE-SENTINEL"


def _place_panel(ctx) -> tuple[str, Path]:
    with health_db.connect(ctx.db_path) as conn:
        pid = health_db.insert_panel(
            conn, drawn_at="2026-05-08", source_file="1/original.pdf",
            source_mime="application/pdf",
        )
        conn.commit()
    leaf = ctx.uploads_dir / "1" / "original.pdf"
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_bytes(INSIDE)
    return f"{PREFIX}/panels/{pid}/source", leaf


def _place_document(ctx) -> tuple[str, Path]:
    with health_db.connect(ctx.db_path) as conn:
        did = health_db.insert_document(
            conn, filename="scan.pdf", mime="application/pdf",
            byte_size=len(INSIDE), content_hash="cafe",
            stored_path="documents/1/scan.pdf",
        )
        conn.commit()
    leaf = ctx.uploads_dir / "documents" / "1" / "scan.pdf"
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_bytes(INSIDE)
    return f"{PREFIX}/documents/{did}/file", leaf


ROUTES = {"panel_source": _place_panel, "document_file": _place_document}


@pytest.fixture(params=sorted(ROUTES))
def placed(request, make_config, tmp_path):
    config = handoff_config(make_config)
    client, ctx = handoff_app(config)
    url, leaf = ROUTES[request.param](ctx)
    return client, ctx, url, leaf, tmp_path


def _outside_file(tmp_path: Path) -> Path:
    outside = tmp_path / "outside" / "secret.pdf"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(SENTINEL)
    return outside


def test_the_unswapped_file_is_served(placed):
    client, _ctx, url, _leaf, _tmp = placed
    resp = client.get(url)
    assert resp.status_code == 200
    assert resp.content == INSIDE
    assert resp.headers["content-type"].startswith("application/pdf")


def test_a_swap_after_validation_does_not_serve_the_outside_file(
    placed, monkeypatch,
):
    """The leaf is replaced by a link to an outside file the moment the
    route builds its response, after every check it makes."""
    client, _ctx, url, leaf, tmp_path = placed
    outside = _outside_file(tmp_path)

    def swap():
        leaf.rename(leaf.with_name(leaf.name + ".real"))
        leaf.symlink_to(outside)

    for name in ("FileResponse", "fd_file_response"):
        original = getattr(health_routes, name, None)
        if original is None:
            continue

        def swapping(*args, _original=original, **kwargs):
            swap()
            return _original(*args, **kwargs)

        monkeypatch.setattr(health_routes, name, swapping)

    resp = client.get(url)
    assert SENTINEL not in resp.content
    assert resp.status_code == 200
    assert resp.content == INSIDE


def test_a_link_at_the_leaf_is_refused(placed):
    client, _ctx, url, leaf, tmp_path = placed
    leaf.unlink()
    leaf.symlink_to(_outside_file(tmp_path))
    resp = client.get(url)
    assert SENTINEL not in resp.content
    assert resp.status_code == 400


def test_a_link_inside_the_tree_is_refused(placed):
    """Pointing back inside the uploads tree is still a link, and a link can
    be repointed after it is checked."""
    client, ctx, url, leaf, _tmp = placed
    other = ctx.uploads_dir / "elsewhere.pdf"
    leaf.rename(other)
    leaf.symlink_to(other)
    resp = client.get(url)
    assert resp.status_code == 400


def test_a_linked_health_directory_is_refused(placed):
    """``{bot_dir}/health`` swapped for a link to an outside tree laid out
    the same way: every name below it then resolves outside on both sides
    of a resolve-and-compare check."""
    client, ctx, url, leaf, tmp_path = placed
    health = ctx.data_dir
    mirror = tmp_path / "mirror-health"
    mirrored = mirror / leaf.relative_to(health)
    mirrored.parent.mkdir(parents=True)
    mirrored.write_bytes(SENTINEL)
    health.rename(health.with_name("health.real"))
    health.symlink_to(mirror, target_is_directory=True)

    resp = client.get(url)
    assert SENTINEL not in resp.content
    assert resp.status_code == 400


def test_the_walk_is_anchored_at_the_user_root_not_inside_it(
    make_config, tmp_path,
):
    """A bot directory linked elsewhere inside the user's tree is allowed,
    and the context resolves through it. The directory that link lands in is
    the user's to write, so it may not be the anchor of the walk: swapped for
    an outside link after the context was resolved, it would be followed."""
    config = handoff_config(make_config)
    user_root = config.workspace_root("alice")
    bot_dir = user_root / config.bot_dir_name
    moved = user_root / "elsewhere" / config.bot_dir_name
    moved.parent.mkdir(parents=True)
    if bot_dir.exists():
        bot_dir.rename(moved)
    else:
        moved.mkdir()
    bot_dir.symlink_to(moved, target_is_directory=True)

    for place in (_place_panel, _place_document):
        client, ctx = handoff_app(config)
        assert ctx.workspace_root == moved.resolve()
        url, leaf = place(ctx)

        anchor = user_root / "elsewhere"
        outside = tmp_path / f"outside-anchor-{place.__name__}"
        mirrored = outside / leaf.relative_to(anchor.resolve())
        mirrored.parent.mkdir(parents=True)
        mirrored.write_bytes(SENTINEL)
        anchor.rename(user_root / "elsewhere.real")
        anchor.symlink_to(outside, target_is_directory=True)
        try:
            resp = client.get(url)
        finally:
            anchor.unlink()
            (user_root / "elsewhere.real").rename(anchor)
        assert SENTINEL not in resp.content
        assert resp.status_code == 400


def test_an_escaping_stored_path_is_400(placed):
    client, ctx, url, _leaf, _tmp = placed
    with health_db.connect(ctx.db_path) as conn:
        conn.execute("UPDATE panels SET source_file = '../../../etc/passwd'")
        conn.execute("UPDATE documents SET stored_path = '../../../etc/passwd'")
        conn.commit()
    resp = client.get(url)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid source path"


def test_a_missing_file_is_404(placed):
    client, _ctx, url, leaf, _tmp = placed
    os.unlink(leaf)
    resp = client.get(url)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "source file missing"


def test_a_range_request_is_served_from_the_open_file(placed):
    """``FileResponse`` answered Range for both routes, which the viewer's
    PDF iframe relies on; the fd response keeps it."""
    client, _ctx, url, _leaf, _tmp = placed
    resp = client.get(url, headers={"Range": "bytes=0-3"})
    assert resp.status_code == 206
    assert resp.content == INSIDE[:4]
