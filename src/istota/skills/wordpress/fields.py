"""`fields get` and `fields edit`: ACF values edited by path, through the
istota-connector plugin's ``istota/fields-get`` and ``istota/fields-edit``.

The plugin owns the value model (field names for keys, ids for objects, null
for nothing stored, every defined sub-field present), the operations and the
validation; nothing here applies an operation to a value. This module parses
the operations, reads before it writes, refuses what it can tell is wrong
before the user is asked anything, runs the gate, uploads, and reports.

**One top-level field per edit, against a token.** ``fields get`` returns a
token per top-level field, a hash of its value. ``fields edit`` reads the field
first and refuses ``stale_value`` when the token moved, and the ability checks
again, so indices from an old read are never applied to rows that have since
shifted. A successful edit moves the token, which is what makes a resend after
``outcome_unknown`` safe: the second one is refused.

**Paths are checked here only as far as the read shows them.** Each op's path
is resolved in the value just read, with the list lengths earlier inserts and
removes change counted. Below a row that an earlier op shifted, or a node an
earlier set replaced, the read no longer says what is there, so the check
stops and the ability is the one that answers.

**The gate.** A post that is not a draft or pending (live, or an attachment,
whose status is ``inherit``), any options page, a post another user has open
in the editor, every row removal, and every set that leaves a row list shorter
ask first (no revision is made, so a dropped row is gone from WordPress). A set
the read cannot count rows for, because an earlier op of the edit replaced
what it lands in, asks too. A draft edit that loses no rows does not. The gate runs here, ahead of the uploads, and the ability is run with
``gated=False``.

**Fences come off on the way in** (`acf.unwrap_markers`): a value copied whole
out of ``fields get`` output is sent as its body, and one still carrying a
marker is refused.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from istota.skill_host_paths import write_resolved

from . import acf, media
from .client import WordPressError, fence, fence_tree, selector
from .common import gate, lookup, quoted, read_json_file
from .connector import _shown, check_page, connector_ability, run_connector
from .content import LIVE_STATUSES
from .discovery import FIELD_ABILITIES
from .generic import annotations, run_ability

FIELDS_GET, FIELDS_EDIT = FIELD_ABILITIES

MAX_OPS = 50
MAX_SEGMENTS = 16
MAX_OPS_BYTES = acf.MAX_ACF_BYTES
VERBS = ("set", "insert", "remove", "move")
#: A flexible content row's own keys beside its sub-fields; no path addresses them.
RESERVED = frozenset({"acf_fc_layout", "acf_fc_layout_disabled", "acf_fc_layout_custom_label"})
#: What reads leave unfenced: a layout name from the definition and a boolean.
#: A custom label is text an editor typed, fenced like any other (Decision 17).
BARE = frozenset({"acf_fc_layout", "acf_fc_layout_disabled"})
_ROW_TYPES = frozenset({"repeater", "flexible_content"})
_OBJECT_TYPES = frozenset({"group", "clone", "row"})
_OP_KEYS = frozenset({"op", "path", "value", "from"})
_NAME_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
_INDEX_RE = re.compile(r"\A(0|[1-9][0-9]{0,8})\Z")
_DIGITS_RE = re.compile(r"\A[0-9]+\Z")
_TOKEN_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_AT_RE = re.compile(r"\A(ops\[[0-9]{1,2}\] )?[A-Za-z0-9_/-]{1,1100}\Z")
#: How much of a value a `would` line shows.
_SHOWN_CHARS = 300
_UNKNOWN = object()
#: Where an edit that removes no row runs without --confirmed, as `update` does.
_UNGATED_STATUSES = frozenset({"draft", "pending", "auto-draft"})


def _invalid(message: str, **extra) -> WordPressError:
    return WordPressError(message, "validation_error", **extra)


# --------------------------------------------------------------------------- #
# Arguments
# --------------------------------------------------------------------------- #


class _Collect(argparse.Action):
    """One list of ``(kind, text)`` in command-line order, for every op flag:
    separate dests would lose the order between different flags."""

    def __call__(self, parser, namespace, values, option_string=None):
        ops = list(getattr(namespace, self.dest, None) or [])
        ops.append((self.const, values))
        setattr(namespace, self.dest, ops)


def add_op_arguments(parser) -> None:
    for flag, kind, metavar, text in (
        ("--set", "set", "PATH=JSON", "replace the value at a field path; repeatable"),
        ("--set-file", "set_file", "PATH=FILE",
         "--set with the JSON value read from a file in your own workspace"),
        ("--insert", "insert", "PATH=JSON",
         "insert a repeater or flexible content row at a field path ending in an index or -"),
        ("--insert-file", "insert_file", "PATH=FILE",
         "--insert with the JSON row read from a file in your own workspace"),
        ("--remove", "remove", "PATH", "remove the row at a field path"),
        ("--move", "move", "FROM=TO", "move a row within its list; both are field paths"),
    ):
        parser.add_argument(flag, dest="ops", action=_Collect, const=kind, metavar=metavar,
                            default=None, help=text)


def parse_path(path, where: str, *, append: bool = False) -> list:
    """A field path as segments: names as strings, indices as ints, a final
    ``-`` on an insert. The plugin's own rules, so a bad one costs no fetch."""
    if not isinstance(path, str) or not path:
        raise _invalid(f"{where}: a path is a non-empty string such as blocks/0/items/3/label.")
    parts = path.split("/")
    if len(parts) > MAX_SEGMENTS:
        raise _invalid(f"{where}: a path has at most {MAX_SEGMENTS} segments.")
    segments: list = []
    for i, part in enumerate(parts):
        if part == "-":
            if append and i == len(parts) - 1 and i > 0:
                segments.append("-")
                continue
            raise _invalid(f"{where}: '-' is only the last segment of an insert path.")
        if _DIGITS_RE.fullmatch(part):
            if i == 0:
                raise _invalid(f"{where}: a path starts with a top-level field name.")
            if not _INDEX_RE.fullmatch(part):
                raise _invalid(f"{where}: segment {i + 1} is not an index (no leading zeros).")
            segments.append(int(part))
            continue
        if not _NAME_RE.fullmatch(part):
            raise _invalid(f"{where}: segment {i + 1} is neither a field name nor an index.")
        if part in RESERVED:
            raise _invalid(
                f"{where}: {part} is a flexible content row's own key, not a field, and no "
                f"path addresses it. A row's layout changes by remove and insert; its "
                f"disabled flag and label change with a set of the whole row."
            )
        segments.append(part)
    return segments


def _split(text: str, flag: str, shape: str) -> tuple[str, str]:
    left, sep, right = text.partition("=")
    if not sep:
        raise _invalid(f"{flag} takes {shape}; got {quoted(text[:80])}.")
    return left.strip(), right


def _json_value(raw: str, flag: str, path: str):
    try:
        return json.loads(raw)
    except ValueError:
        raise _invalid(
            f"{flag} {path}: the value is JSON, so a string is quoted "
            f"({flag} {path}='\"text\"')."
        ) from None


def _from_file(op: dict, i: int) -> dict:
    if not isinstance(op, dict):
        raise _invalid(f"--ops-file: ops[{i}] is not an object.")
    extra = sorted(set(op) - _OP_KEYS)
    if extra:
        raise _invalid(f"--ops-file: ops[{i}] has keys an op does not take: {', '.join(extra)}.")
    if op.get("op") not in VERBS:
        raise _invalid(f"--ops-file: ops[{i}] needs op: set, insert, remove or move.")
    if not isinstance(op.get("path"), str):
        raise _invalid(f"--ops-file: ops[{i}] needs a path string.")
    if op["op"] in ("set", "insert") and "value" not in op:
        raise _invalid(f"--ops-file: ops[{i}] ({op['op']}) needs a value.")
    if op["op"] == "move" and not isinstance(op.get("from"), str):
        raise _invalid(f"--ops-file: ops[{i}] (move) needs a from path.")
    if op["op"] in ("remove", "move") and "value" in op:
        raise _invalid(f"--ops-file: ops[{i}] ({op['op']}) takes no value.")
    if op["op"] != "move" and "from" in op:
        raise _invalid(f"--ops-file: ops[{i}] ({op['op']}) takes no from.")
    return dict(op)


def parse_ops(args) -> list[dict]:
    """``--ops-file`` first, then the flags in command-line order, as op objects.

    The ``-file`` flags name a file inside the value, past the argv stamp, so
    each is resolved here under the `EGRESS` roots (`media.egress_path`).
    """
    ops: list[dict] = []
    if getattr(args, "ops_file", None):
        loaded = read_json_file(args.ops_file, "--ops-file", MAX_OPS_BYTES)
        if not isinstance(loaded, list):
            raise _invalid("--ops-file must hold a JSON array of op objects.")
        ops += [_from_file(op, i) for i, op in enumerate(loaded)]
    for kind, text in getattr(args, "ops", None) or []:
        flag = "--" + kind.replace("_", "-")
        if kind in ("set", "insert"):
            path, raw = _split(text, flag, "PATH=JSON")
            ops.append({"op": kind, "path": path, "value": _json_value(raw, flag, path)})
        elif kind in ("set_file", "insert_file"):
            path, name = _split(text, flag, "PATH=FILE")
            resolved = media.egress_path(name.strip(), f"wordpress {flag}")
            value = read_json_file(resolved, f"{flag} {path}", MAX_OPS_BYTES)
            ops.append({"op": kind.split("_")[0], "path": path, "value": value})
        elif kind == "remove":
            ops.append({"op": "remove", "path": text.strip()})
        else:
            source, target = _split(text, flag, "FROM=TO")
            ops.append({"op": "move", "from": source, "path": target.strip()})
    return ops


def _check_target(args) -> None:
    if args.id is not None and args.id < 1:
        raise _invalid("--id takes a post id, 1 or more.")
    if args.page is not None:
        check_page(args)


def check_fields_get(args) -> None:
    _check_target(args)
    if args.path is not None:
        parse_path(args.path, "--path")
    elif args.output:
        raise _invalid("--output takes --path: without one there is no value to write.")


def check_fields_edit(args) -> None:
    """Everything before the vault fetch: ops, sizes, paths, fences, uploads."""
    _check_target(args)
    if not args.token.strip():
        raise _invalid("--token takes the token `fields get` returned for the field.")
    ops = parse_ops(args)
    if not ops:
        raise _invalid("Name at least one op (--set, --insert, --remove, --move, --ops-file).")
    if len(ops) > MAX_OPS:
        raise _invalid(f"An edit has at most {MAX_OPS} ops; split it into several edits.")
    if len(json.dumps(ops, ensure_ascii=False).encode()) > MAX_OPS_BYTES:
        raise _invalid(f"The ops are over {MAX_OPS_BYTES // (1024 * 1024)} MiB.")
    top = None
    for i, op in enumerate(ops):
        label = f"ops[{i}] ({op['op']} {op['path']})"
        paths = [parse_path(op["path"], label, append=op["op"] == "insert")]
        if op["op"] == "move":
            paths.append(parse_path(op["from"], label))
        for segments in paths:
            top = segments[0] if top is None else top
            if segments[0] != top:
                raise _invalid(
                    f"{label} addresses {segments[0]}, and an edit changes one top-level "
                    f"field ({top}). Run one edit per field."
                )
        if op["op"] != "set":
            last = paths[0][-1] if len(paths[0]) > 1 else None
            if not (isinstance(last, int) or (last == "-" and op["op"] == "insert")):
                raise _invalid(f"{label}: {op['op']} takes the path of a row, ending in "
                               f"its index, such as {top}/0.")
            if op["op"] == "move" and not isinstance(paths[1][-1], int):
                raise _invalid(f"{label}: move takes a from path ending in a row index.")
    uploads = acf.Uploads(media.upload_cap(args.config), args.closers)
    for i, op in enumerate(ops):
        if "value" in op:
            where = f"ops[{i}] {op['path']}"
            op["value"] = acf.substitute_markers(acf.unwrap_markers(op["value"], where),
                                                 uploads, where)
    args.field_ops = ops
    args.top = top
    args.uploads = uploads
    args.deadline = media.call_deadline(args.config)


# --------------------------------------------------------------------------- #
# The site's answers
# --------------------------------------------------------------------------- #


def _target(args) -> dict:
    return {"post_id": args.id} if args.id is not None else {"page": args.page}


def _token(value) -> str | None:
    return value if isinstance(value, str) and _TOKEN_RE.fullmatch(value) else None


def _path_text(value):
    """A path the site sent back: bare when path-shaped, fenced otherwise."""
    if isinstance(value, str) and _AT_RE.fullmatch(value):
        return value
    return fence(value) if value is not None else None


def _site_refusal(exc: WordPressError) -> WordPressError:
    """The ability's own error, under the reason the skill documents for it."""
    data = exc.wp_data.get("data") if isinstance(exc.wp_data.get("data"), dict) else {}
    params = data.get("params") if isinstance(data.get("params"), dict) else {}
    code = exc.extra.get("wp_code")
    extra = dict(exc.extra)
    if code == "istota_stale_value":
        return WordPressError(
            "The field changed since its token was read, so nothing was written. Read it "
            "again with `fields get` and redo the edit against what it holds now; the "
            "indices in the old read may point at different rows.",
            "stale_value", token=_token(params.get("token")), **extra,
        )
    if code == "istota_unknown_path":
        return WordPressError(f"{exc} `fields get` with a shorter path shows what is there.",
                              "unknown_path", path=_path_text(params.get("path")),
                              exists=_path_text(params.get("exists")), **extra)
    if code == "istota_unsupported_field":
        return WordPressError(str(exc), "unsupported_field",
                              field=selector(params.get("field")), **extra)
    if code == "acf_not_in_rest":
        return WordPressError(
            f"{exc} Either the name is wrong (`fields get` with no --path lists the "
            f"fields), or its field group has 'Show in REST API' off.",
            "acf_not_in_rest", fields=[selector(params.get("field"))], **extra)
    if code in ("rest_invalid_param", "istota_mixed_fields") and params:
        # Keyed by op and path ("ops[2] blocks/0/url"); the messages are the site's.
        errors = {(k if _AT_RE.fullmatch(str(k)) else fence(str(k))): fence(v)
                  for k, v in params.items() if isinstance(v, str)}
        extra.pop("fields", None)
        return WordPressError(
            "The edit was refused and nothing was written: "
            + "; ".join(f"{k}: {v}" for k, v in errors.items()),
            "validation_error", errors=errors, **extra)
    return exc


def _run_get(args, ctx, value: dict) -> dict:
    try:
        return run_connector(args, ctx, FIELDS_GET, value, read=True)
    except WordPressError as exc:
        raise _site_refusal(exc) from None


def _context(result: dict) -> dict:
    return {
        "post_id": selector(result.get("post_id")),
        "post_status": selector(result.get("post_status")),
        "post_type": selector(result.get("post_type")),
        "title": fence(result.get("title")) or None,
        "modified_gmt": selector(result.get("modified_gmt")),
        "locked_by": fence(result.get("locked_by")) or None,
    }


def fence_value(value):
    """A normalized value with its site text fenced, a row's layout and flag bare."""
    return fence_tree(value, keep=BARE)


def fence_definition(definition):
    """A field definition: names and types bare, labels and choice labels fenced."""
    if isinstance(definition, list):
        return [fence_definition(item) for item in definition]
    if not isinstance(definition, dict):
        return definition
    out = {}
    for key, item in definition.items():
        if key == "label":
            out[key] = fence(item)
        elif key == "choices" and isinstance(item, dict):
            out[key] = fence_tree(item)
        elif key in ("sub_fields", "layouts"):
            out[key] = fence_definition(item)
        elif key in ("name", "type", "layout", "return_format"):
            out[key] = selector(item)
        elif key in ("required", "multiple", "raw") or (
                key in ("min", "max") and isinstance(item, (int, float))):
            out[key] = item
        else:
            out[key] = fence_tree(item)
    return out


# --------------------------------------------------------------------------- #
# fields get
# --------------------------------------------------------------------------- #


def cmd_fields_get(args) -> dict:
    ctx = args.wp
    value = _target(args)
    if args.path is not None:
        value["path"] = args.path
    result = _run_get(args, ctx, value)
    out = {"status": "ok", **ctx.envelope(), "target": _target(args), **_context(result)}
    if args.path is None:
        rows = result.get("fields") if isinstance(result.get("fields"), list) else []
        out["fields"] = [
            {"name": selector(row.get("name")), "type": selector(row.get("type")),
             "label": fence(row.get("label")), "token": _token(row.get("token"))}
            for row in rows if isinstance(row, dict)
        ]
        return out
    out["path"] = args.path
    out["token"] = _token(result.get("token"))
    out["definition"] = fence_definition(result.get("definition"))
    fenced = fence_value(result.get("value"))
    if args.output:
        record = {"post_id": out["post_id"], "path": args.path, "token": out["token"],
                  "value": fenced, "definition": out["definition"]}
        data = json.dumps(record, indent=2, ensure_ascii=False).encode()
        write_resolved(Path(args.output), data)
        out["written_to"] = str(args.output)
        out["bytes"] = len(data)
        return out
    out["value"] = fenced
    return out


# --------------------------------------------------------------------------- #
# fields edit: the local path check
# --------------------------------------------------------------------------- #


def _row_def(list_def: dict, row) -> dict:
    if list_def.get("type") == "flexible_content":
        name = row.get("acf_fc_layout") if isinstance(row, dict) else None
        for layout in list_def.get("layouts") or []:
            if isinstance(layout, dict) and layout.get("name") == name:
                return {"type": "row", "layout": name, "sub_fields": layout.get("sub_fields") or []}
        return {"type": "row", "layout": name, "sub_fields": []}
    return {"type": "row", "sub_fields": list_def.get("sub_fields") or []}


def _named_child(definition: dict, value, name: str):
    if definition.get("type") not in _OBJECT_TYPES or definition.get("raw"):
        return None
    for sub in definition.get("sub_fields") or []:
        if isinstance(sub, dict) and sub.get("name") == name:
            return sub, value.get(name) if isinstance(value, dict) else None
    return None


def _dropped_rows(definition: dict, current, new, here: str, out: list) -> None:
    """Each repeater or flexible content list at or under `here` that a set of
    `new` over `current` leaves with fewer rows, as ``(path, before, after)``.
    A set clears what its value leaves out, so a list it omits drops to none."""
    kind = definition.get("type")
    if kind in _ROW_TYPES:
        before = current if isinstance(current, list) else []
        after = new if isinstance(new, list) else []
        if len(after) < len(before):
            out.append((here, len(before), len(after)))
        for i in range(min(len(before), len(after))):
            _dropped_rows(_row_def(definition, before[i]), before[i], after[i], f"{here}/{i}", out)
        return
    if kind not in _OBJECT_TYPES or definition.get("raw"):
        return
    layout = definition.get("layout")
    if layout is not None and isinstance(new, dict) and new.get("acf_fc_layout", layout) != layout:
        new = {}
    for sub in definition.get("sub_fields") or []:
        if isinstance(sub, dict) and isinstance(sub.get("name"), str):
            name = sub["name"]
            _dropped_rows(sub, current.get(name) if isinstance(current, dict) else None,
                          new.get(name) if isinstance(new, dict) else None,
                          f"{here}/{name}", out)


class _Walk:
    """The read value, with what earlier ops of the edit did to it counted.

    Only list lengths are simulated. A list an op shifted rows in, and a node
    a set replaced, are no longer described by the read below them, and a path
    reaching either is let through for the ability to answer.
    """

    def __init__(self, definition: dict, value) -> None:
        self.definition = definition
        self.value = value
        self.lengths: dict[str, int] = {}
        self.shifted: set[str] = set()
        self.replaced: set[str] = set()
        #: Why the last `find` could not say: "replaced" or "shifted".
        self.blind: str | None = None

    def _unknown(self, label: str, path: list, exists: str) -> WordPressError:
        where = "/".join(str(s) for s in path)
        return WordPressError(
            f"{label}: {where} does not exist; {exists} is the deepest part that does. "
            f"`fields get --path {exists}` shows what is there.",
            "unknown_path", path=where, exists=exists, op=label.split(" ", 1)[0],
        )

    def length(self, here: str, node) -> int:
        return self.lengths.get(here, len(node) if isinstance(node, list) else 0)

    def find(self, path: list, label: str):
        """``(definition, value, path string)``, or None where the read cannot say."""
        definition, node = self.definition, self.value
        here = str(path[0])
        self.blind = "replaced"
        if here in self.replaced:
            return None
        for segment in path[1:]:
            if isinstance(segment, int):
                if definition.get("type") not in _ROW_TYPES:
                    raise self._unknown(label, path, here)
                if segment >= self.length(here, node):
                    raise self._unknown(label, path, here)
                if here in self.shifted:
                    self.blind = "shifted"
                    return None
                definition, node = _row_def(definition, node[segment]), node[segment]
            else:
                child = _named_child(definition, node, segment)
                if child is None:
                    raise self._unknown(label, path, here)
                definition, node = child
            here = f"{here}/{segment}"
            if here in self.replaced:
                return None
        self.blind = None
        return definition, node, here

    def rows(self, path: list, label: str, verb: str):
        """The list a row op acts in: ``(definition, value, path string)`` or None."""
        found = self.find(path[:-1], label)
        if found is None:
            return None
        definition, node, here = found
        if definition.get("type") not in _ROW_TYPES:
            raise _invalid(
                f"{label}: {verb} acts on a row of a repeater or flexible content field, "
                f"and {here} is a {definition.get('type')}. To change it, set it."
            )
        return found

    def apply(self, op: dict, label: str) -> dict:
        """What the `would` line needs about this op, after checking its path."""
        verb = op["op"]
        path = parse_path(op["path"], label, append=verb == "insert")
        seen: dict = {"current": _UNKNOWN, "layout": None, "dropped": [], "blind": False}
        if verb == "set":
            found = self.find(path, label)
            value = op["value"]
            # A string, number or boolean replaces no list; anything else might.
            could_drop = value is None or isinstance(value, (dict, list))
            if found is None:
                seen["blind"] = self.blind == "replaced" or could_drop
            elif any(p == found[2] or p.startswith(found[2] + "/") for p in self.shifted):
                # Rows under it moved earlier in this edit, so the read's counts are stale.
                seen["blind"] = could_drop
            else:
                seen["current"] = found[1]
                _dropped_rows(found[0], found[1], value, found[2], seen["dropped"])
            self.replaced.add(op["path"])
            return seen
        found = self.rows(path, label, verb)
        if found is None:
            return seen
        definition, node, here = found
        count = self.length(here, node)
        index = path[-1]
        if verb == "insert":
            index = count if index == "-" else index
            if index > count:
                raise _unknown_row(label, op["path"], here, count)
            if definition.get("type") == "flexible_content":
                layouts = [lay.get("name") for lay in definition.get("layouts") or []
                           if isinstance(lay, dict)]
                given = op["value"].get("acf_fc_layout") if isinstance(op["value"], dict) else None
                if given not in layouts:
                    raise _invalid(
                        f"{label}: a flexible content row names its layout in acf_fc_layout, "
                        f"one of {', '.join(str(n) for n in layouts) or '(none)'}."
                    )
            self.lengths[here] = count + 1
        elif verb == "remove":
            if index >= count:
                raise _unknown_row(label, op["path"], here, count)
            if here not in self.shifted:
                row = node[index]
                seen["current"] = row
                if isinstance(row, dict) and isinstance(row.get("acf_fc_layout"), str):
                    seen["layout"] = row["acf_fc_layout"]
            self.lengths[here] = count - 1
        else:
            source = parse_path(op["from"], label)
            if source[:-1] != path[:-1] or not isinstance(source[-1], int):
                raise _invalid(f"{label}: move takes a row within one list: from and the "
                               f"path have the same parent and end in indices.")
            for at in (source[-1], index):
                if at >= count:
                    raise _unknown_row(label, op["path"], here, count)
        self.shifted.add(here)
        return seen


def _unknown_row(label: str, path: str, here: str, count: int) -> WordPressError:
    return WordPressError(
        f"{label}: {path} does not exist; the list at {here} has {count} rows.",
        "unknown_path", path=path, exists=here, op=label.split(" ", 1)[0],
    )


# --------------------------------------------------------------------------- #
# fields edit
# --------------------------------------------------------------------------- #


def _cut(value) -> str:
    text = json.dumps(value, ensure_ascii=False)
    if len(text) <= _SHOWN_CHARS:
        return text
    return f"{text[:_SHOWN_CHARS]}... ({len(text)} characters)"


def _describe_op(op: dict, seen: dict, uploads: acf.Uploads) -> str:
    verb, path = op["op"], op["path"]
    new = _cut(_shown(op.get("value"), uploads)) if "value" in op else None
    if verb == "set":
        if seen["current"] is _UNKNOWN:
            return f"set {path} to {new}"
        # Cut, then fence once, so the cut cannot drop the closing marker.
        return f"set {path} from {fence(_cut(seen['current']))} to {new}"
    if verb == "insert":
        return f"insert at {path} {new}"
    if verb == "remove":
        layout = f" (layout {quoted(seen['layout'])})" if seen["layout"] else ""
        return (f"remove {path}{layout} for good, with no revision made, so WordPress "
                f"keeps no copy of the row")
    return f"move {op['from']} to {path}"


def _subject(args, context: dict) -> str:
    if args.page is not None:
        return f"{args.top} on the options page {quoted(args.page)}"
    kind = context.get("post_type") or "post"
    # A connector from before the title was added answers without one.
    title = context.get("title")
    if title:
        return f"{args.top} of {title} ({kind} #{args.id})"
    return f"{args.top} of {kind} #{args.id}"


def _actions(args, context: dict, seen: list[dict]) -> list[str]:
    """The `would` lines, or none for a draft edit that loses no rows."""
    status = context.get("post_status")
    # An attachment is `inherit` and public, and a status this list does not
    # know is asked about rather than assumed private.
    live = args.page is None and status not in _UNGATED_STATUSES
    locked = context.get("locked_by")
    removes = [(op, s) for op, s in zip(args.field_ops, seen) if op["op"] == "remove"]
    dropped = [d for s in seen for d in s.get("dropped", [])]
    blind = [op for op, s in zip(args.field_ops, seen) if s.get("blind")]
    if not (live or args.page is not None or locked or removes or dropped or blind):
        return []
    subject = _subject(args, context)
    changes = "; ".join(_describe_op(op, s, args.uploads) for op, s in zip(args.field_ops, seen))
    head = f"change {subject}"
    if live:
        head += (f", which is live ({status})" if status in LIVE_STATUSES
                 else f", which is not a draft ({status})")
    actions = [f"{head}: {changes}"]
    if locked:
        actions.append(
            f"change {subject} while {locked} has it open in the editor; their next "
            f"save of it will overwrite this change"
        )
    for path, before, after in dropped:
        actions.append(f"drop rows for good, {path}: {before} rows → {after}; no revision "
                       f"is made, so WordPress keeps no copy of them")
    for op in blind:
        actions.append(f"set {op['path']} below a node an earlier operation of this edit "
                       f"changed, so the rows it may replace cannot be counted beforehand")
    return actions


def _final_path(changed: list, i: int) -> list | None:
    entry = changed[i] if i < len(changed) and isinstance(changed[i], dict) else {}
    path = entry.get("path")
    return path.split("/") if isinstance(path, str) else None


def _overwritten(sent_ops: list[dict], changed: list, i: int) -> bool:
    """Whether a later op wrote at, inside or over op i's node, or removed a row:
    then the node after the edit is not op i's value to compare."""
    mine = _final_path(changed, i)
    if mine is None:
        return True
    for j in range(i + 1, len(sent_ops)):
        if sent_ops[j]["op"] == "remove":
            return True
        theirs = _final_path(changed, j)
        if sent_ops[j]["op"] in ("set", "insert") and theirs:
            shorter = min(len(mine), len(theirs))
            if mine[:shorter] == theirs[:shorter]:
                return True
    return False


def _readback(sent_ops: list[dict], changed: list) -> dict:
    differ = []
    for i, op in enumerate(sent_ops):
        if op["op"] not in ("set", "insert") or i >= len(changed):
            continue
        entry = changed[i] if isinstance(changed[i], dict) else {}
        got = entry.get("value")
        if _overwritten(sent_ops, changed, i):
            continue
        if not acf.same(op["value"], got):
            differ.append(_path_text(entry.get("path")) or op["path"])
    notes = []
    if differ:
        notes.append(
            "A value did not store as sent. For an account without unfiltered_html, "
            "WordPress filters HTML in written text as the editor would. `fields get` "
            "shows what was saved."
        )
    return {"changed": differ, "notes": notes}


def cmd_fields_edit(args) -> dict:
    ctx = args.wp
    target = _target(args)
    read = _run_get(args, ctx, {**target, "path": args.top})
    context = _context(read)
    current = _token(read.get("token"))
    if current != args.token.strip():
        raise WordPressError(
            f"{args.top} changed since the token you gave was read, so nothing was sent. "
            f"Read it again with `fields get` and redo the edit against what it holds now.",
            "stale_value", token=current,
        )

    walk = _Walk(read.get("definition") if isinstance(read.get("definition"), dict) else {},
                 read.get("value"))
    seen = [walk.apply(op, f"ops[{i}] ({op['op']} {op['path']})")
            for i, op in enumerate(args.field_ops)]

    ability = connector_ability(ctx, FIELDS_EDIT)
    notes = annotations(ability)
    # Readonly would move it to GET, and destructive to DELETE.
    if notes.get("readonly") is True or notes.get("destructive") is True:
        raise WordPressError(
            f"The site's {FIELDS_EDIT} is not marked the way the istota-connector plugin "
            f"marks it, so this verb will not run it.",
            "connector_mismatch",
        )
    gate(args, ctx, _actions(args, context, seen))

    upload_ids, report = acf.run_uploads(ctx, args.uploads, args.deadline)
    uploaded = [{"path": row["path"], "id": row["id"]} for row in report]
    if report:
        media.check_deadline(args.deadline, "editing the field", uploaded)
    ops = [acf.fill(op, upload_ids) for op in args.field_ops]
    where = ["--id", str(args.id)] if args.id is not None else ["--page", args.page]
    hint = lookup(ctx, "fields", "get", *where, "--path", args.top)
    try:
        result, _, _ = run_ability(args, ctx, FIELDS_EDIT, ability,
                                   {**target, "token": args.token.strip(), "ops": ops},
                                   gated=False, hint=hint)
    except WordPressError as exc:
        refusal = _site_refusal(exc)
        if uploaded:
            refusal.extra["uploaded"] = uploaded
        raise refusal from None
    result = result if isinstance(result, dict) else {}
    changed = result.get("changed") if isinstance(result.get("changed"), list) else []
    previous = result.get("previous") if isinstance(result.get("previous"), list) else []
    missing = result.get("missing_required")
    missing = [_path_text(p) for p in missing] if isinstance(missing, list) else []
    out = {
        "status": "ok",
        **ctx.envelope(),
        "target": _target(args),
        "post_status": selector(result.get("post_status")),
        "token": _token(result.get("token")),
        "previous_token": _token(result.get("previous_token")),
        "changed": [
            {"op": selector(c.get("op")), "path": _path_text(c.get("path")),
             "value": fence_value(c.get("value"))}
            for c in changed if isinstance(c, dict)
        ],
        "previous": [fence_value(p) for p in previous],
        "readback": _readback(ops, changed),
        "missing_required": missing,
    }
    if missing:
        out["missing_required_note"] = (
            "These required fields are empty. The edit stands; the editor asks for them "
            "on the next manual save unless conditional logic hides them there."
        )
    storage = result.get("storage")
    if isinstance(storage, dict) and storage.get("skipped"):
        out["storage_note"] = (
            "The plugin could not match how this site stores the field, so sub-fields "
            "the edit did not name may now hold an empty stored value where they held "
            "none. The values read the same in `fields get`."
        )
    if report:
        out["uploads"] = report
    return out
