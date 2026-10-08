"""Search the framework stores with the web room list's visibility rules."""

from datetime import date
from pathlib import Path

from istota import db, storage
from istota.lib.date_parse import iso_utc
from istota.lib.text_match import fts5_match, like_predicate, make_snippet, terms_to_plain, text_matches
from istota.memory.search import search
from istota.search.core import Provider, ProviderResult, SearchHit, open_with_deadline
from istota.search.links import file_link, route
from istota.webui.chat_files import ChatFileError, resolve_chat_file

MEMORY_TYPES = ["memory_file", "user_memory", "channel_memory", "channel_memory_durable", "playbook"]


def _visible_rooms(conn, user_id):
    hidden = db.hidden_room_tokens_for_member(conn, user_id)
    handles = {row["token"]: row["name"] for row in conn.execute(
        "SELECT token, name FROM web_chat_rooms WHERE user_id=?", (user_id,),
    )}
    return [(room, room.name if room.name is not None else handles.get(room.token) or "")
            for room in db.list_member_rooms(conn, user_id) if room.token not in hidden]


def chats(ctx, terms, mode, limit, offset):
    with open_with_deadline(lambda **options: db.get_db(ctx.config.db_path, **options), ctx.deadline) as conn:
        rows = db.search_messages(conn, ctx.user_id, fts5_match(terms, mode), limit=limit + 1,
                                  offset=offset, exclude_tokens=db.hidden_room_tokens_for_member(conn, ctx.user_id))
        hits = []
        for row in rows[:limit]:
            if row["role"] == "assistant":
                author = ctx.config.bot_name
            elif row["author_label"]:
                author = row["author_label"]
            elif row["role"] == "system":
                author = "System"
            else:
                user_id = row["author_user_id"]
                if user_id is None:
                    user_id = db.get_room(conn, row["room_token"]).user_id
                user = ctx.config.get_user(user_id)
                author = "You" if user_id == ctx.user_id else (user.display_name if user and user.display_name else user_id)
            hits.append(SearchHit(
                f"chats:{row['msg_id']}", "message", " ".join((row["room_name"] or "").split()),
                " ".join((author or "").split()), row["snippet"], row["highlights"], iso_utc(row["created_at"]),
                route("/chat/", room=row["room_token"], msg=row["msg_id"], ts=row["created_at"]),
                [badge for badge in ("shared", "starred") if row[badge]],
                cursor={"ts": row["created_at"], "id": row["msg_id"]},
            ))
        return ProviderResult(hits, len(rows) > limit)


def rooms(ctx, terms, mode, limit, offset):
    with open_with_deadline(lambda **options: db.get_db(ctx.config.db_path, **options), ctx.deadline) as conn:
        matched = [(room, name) for room, name in _visible_rooms(conn, ctx.user_id) if text_matches(name, terms, mode)]
        hits = []
        for room, name in matched[offset:offset + limit]:
            snippet, highlights = make_snippet(name, terms)
            hits.append(SearchHit(f"rooms:{room.token}", "room", " ".join(name.split()), None,
                                  snippet, highlights, iso_utc(room.last_activity or room.created_at),
                                  route("/chat/", room=room.token), []))
        return ProviderResult(hits, len(matched) > offset + limit)


def _memory_file_link(ctx, source):
    path = Path(source)
    if path.is_absolute():
        root = ctx.config.workspace_root()
        if root is None:
            return None
        try:
            path = Path("/") / path.relative_to(root)
        except ValueError:
            return None
    try:
        resolve_chat_file(ctx.config, ctx.user_id, str(path))
    except (ChatFileError, OSError):
        return None
    return file_link(str(path))


def memory(ctx, terms, mode, limit, offset):
    with open_with_deadline(lambda **options: db.get_db(ctx.config.db_path, **options), ctx.deadline) as conn:
        namespaces = {}
        for room, name in _visible_rooms(conn, ctx.user_id):
            for token in storage.channel_memory_tokens(ctx.config, room.token):
                namespaces[f"channel:{token}"] = (room.token, name)
        results = search(conn, ctx.user_id, terms_to_plain(terms), limit=offset + limit + 1,
                         source_types=MEMORY_TYPES, include_user_ids=list(namespaces), prefix=True,
                         match_mode="and" if mode == "strict" else "or", vector=False)
        hits = []
        for result in results[offset:offset + limit]:
            snippet, highlights = make_snippet(result.content, terms)
            title = Path(result.source_id).name
            link = None
            if result.source_type in ("channel_memory", "channel_memory_durable"):
                namespace = conn.execute("SELECT user_id FROM memory_chunks WHERE id=?", (result.chunk_id,)).fetchone()[0]
                room = namespaces.get(namespace)
                if room:
                    token, name = room
                    title = f"CHANNEL.md · {name}" if result.source_type == "channel_memory_durable" else f"{Path(result.source_id).stem} · {name}"
                    link = route("/chat/", room=token)
            else:
                if result.source_type == "user_memory":
                    title = "USER.md"
                link = _memory_file_link(ctx, result.source_id)
            hits.append(SearchHit(f"memory:{result.chunk_id}", result.source_type, " ".join(title.split()), None,
                                  snippet, highlights, iso_utc(result.created_at), link, []))
        return ProviderResult(hits, len(results) > offset + limit)


def facts(ctx, terms, mode, limit, offset):
    predicate, params = like_predicate(["subject", "predicate", "object"], terms, mode)
    with open_with_deadline(lambda **options: db.get_db(ctx.config.db_path, **options), ctx.deadline) as conn:
        rows = conn.execute(
            "SELECT * FROM knowledge_facts WHERE user_id=? AND (valid_until IS NULL OR valid_until>?) "
            f"AND ({predicate}) ORDER BY valid_from DESC, id DESC LIMIT ? OFFSET ?",
            [ctx.user_id, date.today().isoformat(), *params, limit + 1, offset],
        ).fetchall()
        hits = []
        for row in rows[:limit]:
            title = " ".join(f"{row['subject']} {row['predicate']} {row['object']}".split())
            snippet, highlights = make_snippet(title, terms)
            hits.append(SearchHit(f"facts:{row['id']}", "fact", title, None, snippet, highlights,
                                  iso_utc(row["valid_from"] or row["created_at"], preserve_date=True), None, []))
        return ProviderResult(hits, len(rows) > limit)


PROVIDERS = [
    Provider("chats", "Chats", 0, None, 2.0, False, chats),
    Provider("rooms", "Rooms", 1, None, 1.5, False, rooms),
    Provider("memory", "Memory", 2, None, 2.0, False, memory),
    Provider("facts", "Facts", 3, None, 1.5, False, facts),
]
