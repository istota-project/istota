"""Web browsing skill - thin CLI client to the browser container API.

Usage:
    python -m istota.skills.browse get "https://example.com" [--keep-session] [--timeout 30]
    python -m istota.skills.browse render "https://example.com" [--mode full|article]
    python -m istota.skills.browse screenshot "https://example.com" [--output <workspace path>]
    python -m istota.skills.browse extract "https://example.com" --selector "article"
    python -m istota.skills.browse interact <session_id> --click ".button" --fill "#input=value"
    python -m istota.skills.browse interact <session_id> --fill-credential "#password=acme_password"
    python -m istota.skills.browse interact <session_id> --click-at 412,318 --type "Ada" --press Tab
    python -m istota.skills.browse interact <session_id> --click-challenge
    python -m istota.skills.browse links "https://example.com" [--selector "nav a"]
    python -m istota.skills.browse challenge <session_id>
    python -m istota.skills.browse close <session_id>

Reads BROWSER_API_URL env var for the container endpoint.
"""

import argparse
import json
import math
import os
import re
import time
from pathlib import Path

import httpx

from istota.image_sniff import SNIFF_BYTES, sniff_raster
from istota.skill_host_paths import (
    resolve_host_path,
    user_workspace_root,
    write_resolved,
)
from istota.skills._cli import error_envelope, parse_and_resolve, run_skill_cli
from istota.skills._credref import PAIR, CredentialPair, credential_ref
from istota.skills._hostpath import WRITE, host_path

DEFAULT_API_URL = "http://localhost:9223"
# Where a screenshot lands with no `--output`: `screenshots/` under the task's
# own per-user temp directory. A subdirectory rather than the temp dir itself,
# so a run of captures stays legible beside the deferred-op files and the
# credential shim that share it.
SCREENSHOT_SUBDIR = "screenshots"
# What the result says about a capture that took that default. The absence of
# `workspace_path` is the whole difference between a scratch picture and one
# the user can be shown, and an absence teaches nothing — so the note is
# attached where a model reading the result will meet it, rather than left to
# be inferred from a missing key.
SCRATCH_NOTE = (
    "This capture is scratch: it is in the task's temp directory, it is swept "
    "on the deployment's temp-file retention, and `/chat/files` does not serve "
    "it — so a reply cannot show it. To show the user the picture, or to keep "
    "it, take the capture again with -o naming a path inside your workspace."
)
# Where `OrderedAppend` records the command-line order of the `interact`
# arguments it is declared on. Not an argument of its own, so nothing parses
# it and no caller sets it; `interact` is the only reader.
ACTION_ORDER_DEST = "action_order"
# How many derived names one capture will try before giving up. Only reached
# when that many captures land in the same UTC second, so it is a bound on a
# loop rather than a capacity.
MAX_NAME_ATTEMPTS = 100
# One extension per media type `image_sniff` admits, for the derived default
# name. Deriving it rather than always writing `.png` keeps the name honest
# about the bytes; `/chat/files` sniffs and would serve it either way.
_SUFFIX_FOR_MEDIA_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
# What the container puts the capture frame on, and the reason it could not.
# Two headers rather than one, because "this container predates visual mode"
# and "this container tried and could not measure the window" have different
# remedies and the absence of a header cannot say which.
CAPTURE_HEADER = "X-Browse-Capture"
CAPTURE_ERROR_HEADER = "X-Browse-Capture-Error"
#: Actions whose `x`/`y` are in the *delivered picture's* pixel space, so the
#: container has to be told what that picture measured before it can convert.
IMAGE_SPACE_ACTIONS = ("click_at", "hover_at")
#: Every action type only a visual-mode container implements. An `unknown`
#: naming one of these means the image is older than this code, which is a
#: different sentence from the model having invented an action.
VISUAL_ACTION_TYPES = IMAGE_SPACE_ACTIONS + ("key", "type", "click_challenge")
REQUEST_TIMEOUT = 120.0  # HTTP client timeout (longer than page timeout)
MAX_BODY_EXCERPT = 400  # chars of an undecodable body to quote back
MAX_BODY_READ = 8192  # bytes of it to decode in the first place

# Characters that must not reach the excerpt. C0 and C1 cover the ANSI escapes
# (`\x1b`, and the C1 CSI at `\x9b`). The rest are invisible or reorder what
# follows them: U+202E and its neighbours reverse display order, so a body
# could otherwise render as a different message inside a line the model reads
# as this tool's own voice. U+2028/U+2029 are absent deliberately — the
# whitespace collapse below already takes them.
_CONTROL_RE = re.compile(
    r"[\x00-\x1f\x7f-\x9f"
    r"\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]",
)


def get_api_url():
    return os.environ.get("BROWSER_API_URL", DEFAULT_API_URL)


def _body_excerpt(resp, limit=MAX_BODY_EXCERPT):
    """A short, printable slice of a response body we could not decode.

    Returns the excerpt, `""` for a genuinely empty body, or None when the body
    could not be read at all — the caller renders those three apart, because
    "empty response" and "40 KB of something unreadable" are different outages
    and reporting the second as the first is a false statement rather than a
    missing detail.

    The body is whatever the container or an intermediary produced, so it is
    untrusted: `_CONTROL_RE` above says what is stripped and why, and runs of
    whitespace collapse, so a Flask HTML page or a proxy's error page reports
    as one readable line. Bounded before it is decoded rather than after, so an
    oversized error page costs one 8 KB copy instead of several full ones.
    Reading it must not raise, since this runs on the path that reports a
    failure — decoding with `errors="replace"` off a `bytes` that is already
    resident is what makes that true rather than hopeful.
    """
    try:
        raw = bytes(resp.content or b"")[:MAX_BODY_READ]
    except Exception:
        return None
    try:
        text = raw.decode(resp.encoding or "utf-8", errors="replace")
    except Exception:
        # A bogus charset in the Content-Type is a LookupError, which is the
        # intermediary's mistake rather than a reason to report nothing.
        text = raw.decode("utf-8", errors="replace")
    text = _CONTROL_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


def _decode(resp):
    """Return the endpoint's JSON object, or an error naming what came instead.

    Every verb goes through this. The check is on the *body*, not the status:
    the API reports its own failures as JSON with a non-2xx status
    (`{"error": "url is required"}` with 400, `Chrome unavailable` with 503),
    and those bodies carry the diagnosis, so a `raise_for_status()` would throw
    away the useful half of the answer.

    What has to be caught is a body that is not a JSON object at all — Flask's
    default HTML 500 page, a 502 from something in front of the container, an
    empty or truncated response, or well-formed JSON of the wrong shape, which
    every caller turns into an `AttributeError` one frame later. Before
    ISSUE-383 the decode error reached `main`'s catch-all and printed as
    "Expecting value: line 1 column 1 (char 0)", naming no status, no URL and
    no part of the body, so an outage could not be told apart from an empty
    reply without reading the container's logs.

    A body the API *did* report an error in is also normalized here, because
    the API has two spellings for one thing: its 500s and 503s say
    `{"status": "error", ...}`, while its argument and lookup failures say a
    bare `{"error": ...}` with no `status` key at all — nine such paths, two of
    them 500s. Every caller in this module branches on `status`, so without the
    stamp the commonest server-side rejection (bad arguments, an expired
    session) reads as a success: `main` exits 0 and `cmd_links` treats it as a
    page. It also made two verbs disagree about one server response, since
    `cmd_render` rewrites that shape by hand for its own 404 and nothing else
    did. Stamping it here makes that rewrite the general rule.
    """
    try:
        data = resp.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        if data.get("error") and "status" not in data:
            return {"status": "error", **data}
        return data

    excerpt = _body_excerpt(resp)
    if excerpt is None:
        shown = "(unreadable)"
    elif not excerpt:
        shown = "(empty)"
    else:
        shown = excerpt
    try:
        where = f" for {resp.url}"
    except Exception:
        where = ""
    error = (
        f"Browser API returned HTTP {resp.status_code}{where} "
        f"with a body that is not a JSON object: {shown}"
    )
    if resp.status_code == 503:
        # The message the unreachable `except httpx.HTTPStatusError` arm in
        # `main` used to hold, on a path that can actually be reached. The
        # API's own 503 is JSON and returns above with a better one.
        error += " — the browser may be restarting inside the container, retry in a few seconds."
    return {"status": "error", "error": error}


def cmd_get(args):
    """Browse a URL and return page content."""
    url = get_api_url()
    payload = {
        "url": args.url,
        "timeout": args.timeout,
        "keep_session": args.keep_session,
    }
    if args.session:
        payload["session_id"] = args.session
    if args.wait_for:
        payload["wait_for"] = args.wait_for
    if args.skip_behavior:
        payload["skip_behavior"] = True
    if args.max_chars:
        payload["max_chars"] = args.max_chars
    if args.max_links:
        payload["max_links"] = args.max_links

    resp = httpx.post(f"{url}/browse", json=payload, timeout=REQUEST_TIMEOUT)
    return _decode(resp)


def cmd_render(args):
    """Render a page to markdown, keeping headings and links together.

    The result carries a `frames` census whatever the flags say, because an
    iframe's document is a separate frame the markdown has always dropped in
    silence (ISSUE-516). `--include-frames` is what splices that content in.
    """
    url = get_api_url()
    if not args.url and not args.session:
        return {
            "status": "error",
            "error": "render needs a URL or --session <id>",
        }

    payload = {
        "mode": args.mode,
        "timeout": args.timeout,
        "keep_session": args.keep_session,
    }
    if args.url:
        payload["url"] = args.url
    if args.session:
        payload["session_id"] = args.session
    if args.wait_for:
        payload["wait_for"] = args.wait_for
    if args.max_chars:
        payload["max_chars"] = args.max_chars
    if args.include_frames:
        payload["include_frames"] = True
    if args.skip_behavior:
        payload["skip_behavior"] = True

    resp = httpx.post(f"{url}/render", json=payload, timeout=REQUEST_TIMEOUT)
    if resp.status_code == 404:
        # Two unrelated failures share this status. The endpoint's own
        # "session not found or expired" is a JSON body with an `error` key —
        # routine, since sessions expire after 10 minutes and the scroll-then-
        # re-render recipe re-uses one. A container predating the renderer
        # answers Flask's HTML route-miss instead, and only that one means the
        # feature is absent. Reporting both as absent would send the agent back
        # to the flattened-text path this command exists to replace.
        try:
            data = resp.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("error"):
            return {"status": "error", "error": data["error"]}
        return {
            "status": "error",
            "error": "This browser container has no render endpoint — use `browse get` instead.",
        }
    return _decode(resp)


def screenshot_dir():
    """Where a screenshot goes with no ``--output``: ``(directory, reason)``.

    ``$ISTOTA_DEFERRED_DIR/screenshots`` — that variable is
    ``{temp_dir}/{user_id}``, the task's own per-user temp directory, which is
    the first root of this CLI's write allowlist and is bound read-write into
    the sandbox. So the model can read back what it just captured, which is
    what the visual ladder needs, and nothing else can.

    **This is the third destination the default has had, and the middle one is
    the trap.** It was ``/tmp/screenshot.png``, which on a sandboxed deployment
    named a file on the far side of the boundary: the skill CLI is spawned
    host-side by the proxy while the model's ``/tmp`` is the sandbox's own
    ``--tmpfs``, so the write landed on the host and the model was handed a
    path it could not open. The fix was the user's *workspace*, which is
    readable from inside the sandbox and is also what ``/chat/files`` serves --
    and that second property is what made it the wrong answer. A capture is a
    working artifact of the visual loop, taken eight at a time, so filing them
    there made every scratch picture a permanent file in a directory the user
    reads. The per-user temp dir has the first property and not the second,
    which is the trade this verb wants.

    **The retention rule is one that already exists.**
    ``scheduler.cleanup_old_temp_files`` walks ``temp_dir`` recursively on
    ``[scheduler] temp_file_retention_days`` and removes the emptied directory
    behind it, so there is no sweep to write here and no second sweeper to
    disagree with that one. A capture the user is meant to keep is one written
    with ``--output`` into their own workspace, which nothing sweeps — the
    split is between a scratch file and a file, not between two retention
    policies. An operator who has set that retention to ``0`` has disabled the
    sweep for every temp file and not only for these.

    **What the move gives up is the embed, and the giving up is the point.**
    ``_workspace_relative`` answers ``None`` for anything outside
    ``/Users/{uid}``, so a derived capture carries no ``workspace_path`` and a
    reply cannot render it. That is why ``cmd_screenshot`` attaches
    :data:`SCRATCH_NOTE` rather than letting the missing key speak for itself.

    One failure reason rather than three, because the two the workspace
    derivation needed — a resolvable mount and an ``ISTOTA_BOT_DIR_NAME`` --
    went with it. The remedy it names is the right one and not a stock
    sentence: ``env_host_roots`` drops each ingredient independently, so an
    environment with no deferred dir still has the workspace root if the mount
    and the user id resolved, and an ``--output`` written there is accepted by
    the same allowlist that has nowhere to put the derived default.
    """
    deferred = os.environ.get("ISTOTA_DEFERRED_DIR", "").strip()
    if not deferred:
        return None, (
            "ISTOTA_DEFERRED_DIR is not set, so this task has no temp "
            "directory to put a screenshot in. Pass --output with a path "
            "inside your workspace."
        )
    return Path(deferred) / SCREENSHOT_SUBDIR, None


def _write_derived_capture(directory, media_type, content):
    """Write the capture under a name nothing else holds: ``(path, error)``.

    ``screenshot-<utc timestamp>.<ext>``, timestamped rather than a fixed
    ``screenshot.png`` because a task that captures two charts would otherwise
    embed the second one twice.

    **The uniqueness is the open's, not a prior `exists()` check.** Two tasks
    of one user can derive the same name inside one second, the scheduler runs
    a worker pool, and a check-then-write would let the second silently
    overwrite the first — both reporting `ok`, one with a `size` describing
    bytes that are no longer there. `O_EXCL` collapses the check and the create
    into one step; a collision is a `FileExistsError` and the next candidate is
    tried. Exhausting the bound is an error rather than a fallback to a name
    already known to be taken, which is the overwrite wearing a different hat.
    """
    suffix = _SUFFIX_FOR_MEDIA_TYPE.get(media_type, ".png")
    stem = time.strftime("screenshot-%Y%m%d-%H%M%S", time.gmtime())
    for n in range(1, MAX_NAME_ATTEMPTS + 1):
        name = f"{stem}{suffix}" if n == 1 else f"{stem}-{n}{suffix}"
        resolved, err = resolve_host_path(
            directory / name, writable=True, operation="browse screenshot",
        )
        if err:
            return None, err
        try:
            write_resolved(resolved, content, exclusive=True)
        except FileExistsError:
            continue
        except OSError as e:
            return None, f"could not write {resolved}: {e}"
        return resolved, None
    return None, (
        f"could not find an unused name under {directory} after "
        f"{MAX_NAME_ATTEMPTS} attempts; nothing was written."
    )


def _workspace_relative(path):
    """``/Users/{uid}/…`` for a resolved capture, or None.

    The ``?path=`` value ``/chat/files`` takes and the guidelines teach, handed
    back beside the host path so the reply's URL is a copy rather than a
    reconstruction.

    **Built against the workspace root, not the mount, because that is the
    confinement the consumer applies.** `_resolve_chat_file` serves
    `/Users/{uid}` and nothing else, while `allowed_host_roots` also admits the
    task's own `{mount}/Channels/{token}` as a destination — so a `--output`
    there is a legitimate write whose mount-relative spelling is a URL the
    endpoint refuses by design.

    **`None` is the ordinary answer now, not the edge case.** Since the derived
    default moved to the per-user temp dir (`screenshot_dir`), every capture
    taken without `--output` lands outside the workspace and so has no URL.
    That is the intended shape rather than a gap, and `SCRATCH_NOTE` is what
    says so in the result; this function is unchanged, because the rule it
    applies — is this file one `/chat/files` would serve — did not move.
    """
    root = user_workspace_root()
    if root is None:
        return None
    user_id = os.environ.get("ISTOTA_USER_ID", "").strip()
    try:
        relative = path.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    # `user_workspace_root` already established that the user id is one plain
    # component, so this join cannot walk anywhere.
    return f"/Users/{user_id}/{relative}"


def envelope_scale(width, height):
    """How far a `width` x `height` capture must shrink to survive delivery.

    A vision provider rescales an image over its own envelope server-side, and
    that rescale is invisible to everything in this tree — so a point read off
    the picture the model saw would be converted against a capture it never
    saw, and every click would land short by the difference. The answer is to
    deliver a picture already inside the envelope, which makes the provider's
    own rescale a no-op and collapses the two frames into one.

    `MAX_EDGE` and `MAX_AREA_PIXELS` are imported rather than restated —
    `image_attachments` is this repository's statement of the envelope and a
    second copy would drift silently. The import is at function scope with
    Pillow's below, because this module is loaded for every `render`, `get`,
    `extract`, `links`, `interact` and `close` call and none of those resizes
    anything.

    Returns `1.0` where nothing has to move, which is the common case on a
    deployment whose screen already fits. Never above 1: this shrinks a capture
    and never enlarges one.
    """
    from istota.image_attachments import MAX_AREA_PIXELS, MAX_EDGE

    if width <= 0 or height <= 0:
        return 1.0
    scale = 1.0
    longest = max(width, height)
    if longest > MAX_EDGE:
        scale = MAX_EDGE / longest
    area = width * height
    if area > MAX_AREA_PIXELS:
        scale = min(scale, math.sqrt(MAX_AREA_PIXELS / area))
    return scale


def delivered_size(width, height):
    """The pixel size of the picture the model is handed: `(width, height)`.

    The one number the coordinate contract rests on, and the reason it is a
    function rather than two expressions at two call sites: `cmd_screenshot`
    resizes the capture to it, and `cmd_interact` recomputes it in a *different
    process* to tell the container what the model was looking at. A skill CLI
    is a fresh process per invocation, so it cannot remember; it does not have
    to, because the inputs are on the capture record the container kept. The
    two computations must agree exactly, rounding included, or every click is
    off by the difference.

    Rounding is **down**, and that is the half worth stating: `MAX_AREA_PIXELS`
    is a ceiling, and rounding a dimension up can cross it — 1440x813 scales to
    1427.2 x 805.9, which rounds to 1427x806 and is 162 pixels over the cap,
    so the provider would rescale again and the frame the model saw would stop
    being the frame the container converts against. Truncating cannot: both
    factors only shrink.
    """
    scale = envelope_scale(width, height)
    if scale >= 1.0:
        return int(width), int(height)
    return max(1, int(width * scale)), max(1, int(height * scale))


def capture_image_size(record):
    """`(width, height)` off a container capture record, or None.

    The record is the container's JSON, arriving over a header or a session
    read, so its shape is checked rather than assumed: a malformed one has to
    read as "no coordinate frame" and not as a traceback on the screenshot
    path.
    """
    if not isinstance(record, dict):
        return None
    size = record.get("image")
    if not isinstance(size, (list, tuple)) or len(size) != 2:
        return None
    try:
        width, height = int(size[0]), int(size[1])
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def _capture_from_response(headers):
    """The capture record off a screenshot response: `(record, note)`.

    Three things leave a capture with no coordinate frame and the caller has to
    be able to say which: a container predating visual mode sends no header at
    all, one that could not measure the Chrome window sends
    `X-Browse-Capture-Error` with its own reason, and a header that will not
    parse means something rewrote it in transit. None of the three is a failed
    screenshot — the picture is still worth reading — so each is a note beside
    `capture: null` rather than an error.
    """
    reason = headers.get(CAPTURE_ERROR_HEADER)
    if reason:
        return None, (
            f"The browser container recorded no coordinate frame for this "
            f"capture ({reason}), so --click-at and --hover-at cannot be used "
            f"against this session. The picture itself is fine to read."
        )
    raw = headers.get(CAPTURE_HEADER)
    if not raw:
        return None, (
            "This browser container reports no capture frame, so it predates "
            "visual mode: --click-at and --hover-at will not work against it. "
            "The picture itself is fine to read."
        )
    try:
        record = json.loads(raw)
    except ValueError:
        record = None
    if capture_image_size(record) is None:
        return None, (
            "The browser container's capture header did not parse, so "
            "--click-at and --hover-at cannot be used against this session. "
            "The picture itself is fine to read."
        )
    return record, None


def _resize_capture(content, media_type, target):
    """The capture resized to `target` pixels: `(bytes, error)`.

    Pillow is imported here rather than at module scope for the reason
    `envelope_scale`'s import states. A palette or bilevel image is resampled
    nearest-neighbour instead of Lanczos, since Lanczos on an indexed mode
    either loses the palette or refuses, and keeping the mode is what lets the
    result be saved back in its own format.
    """
    from io import BytesIO

    from PIL import Image

    try:
        with Image.open(BytesIO(content)) as image:
            fmt = image.format or _PIL_FORMAT_FOR_MEDIA_TYPE.get(media_type, "PNG")
            resample = (
                Image.Resampling.NEAREST
                if image.mode in ("P", "1")
                else Image.Resampling.LANCZOS
            )
            resized = image.resize(target, resample)
            buffer = BytesIO()
            resized.save(buffer, format=fmt)
        return buffer.getvalue(), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


#: The Pillow format name for each media type the capture sniff admits, for the
#: case where the opened image carries no `format` of its own.
_PIL_FORMAT_FOR_MEDIA_TYPE = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/gif": "GIF",
    "image/webp": "WEBP",
}


def cmd_screenshot(args):
    """Take a screenshot: scratch by default, into the workspace on request.

    With no `--output` the capture is a working file in the task's own temp
    directory (`screenshot_dir`), which is where the visual ladder's eight
    captures per loop belong; `--output` is how a model says this one is for
    the user to see or keep. The two are told apart in the result by
    `workspace_path`, which only a workspace write earns, and by
    :data:`SCRATCH_NOTE`, which only the derived default carries.

    `--output` is declared `WRITE`, so it arrives already resolved and already
    contained: `parse_and_resolve` refuses an out-of-allowlist path before the
    handler is dispatched at all, which is a stronger version of the early
    check this used to make by hand — no browser time, no envelope from here,
    and no second resolution to keep in step with the first. Resolution
    creates no directory, so a refusal leaves nothing behind; `write_resolved`
    is what makes the parent, and only where a write actually happens.

    The derived default still resolves per candidate name in
    `_write_derived_capture`, because those names are built here rather than
    declared, and the extension is not known until the bytes are.
    """
    directory = None
    if not args.output:
        directory, reason = screenshot_dir()
        if directory is None:
            return {"status": "error", "error": reason}

    url = get_api_url()
    payload = {
        "timeout": args.timeout,
        "full_page": args.full_page,
    }
    if args.url:
        payload["url"] = args.url
    if args.session:
        payload["session_id"] = args.session

    resp = httpx.post(f"{url}/screenshot", json=payload, timeout=REQUEST_TIMEOUT)

    # The status is checked as well as the content type, because this is the
    # one verb that reports success off a body it never parses: an intermediary
    # answering 502 while labelling it `image/png` used to have its error page
    # written to disk as a .png and reported `status: ok`. A zero-length body
    # is refused for the same reason — `size: 0` reads as a screenshot.
    is_image = resp.headers.get("content-type", "").startswith("image/")
    if resp.status_code == 200 and is_image and resp.content:
        content = bytes(resp.content)
        # The content type is the container's claim about the body; this is the
        # body. Same predicate `/chat/files` sniffs the file with, so a capture
        # that would come back as a download rather than an image is refused
        # here instead of being embedded as a broken one — and a 200 carrying
        # an `image/png` label over an HTML error page is caught one level
        # deeper than the status check above catches it.
        media_type = sniff_raster(content[:SNIFF_BYTES])
        if media_type is None:
            return {
                "status": "error",
                "error": (
                    f"The browser API returned {len(content)} bytes labelled "
                    f"{resp.headers.get('content-type', 'nothing')} that are "
                    f"not a PNG, JPEG, GIF or WebP — not a screenshot, and "
                    f"nothing was written."
                ),
            }
        # The envelope resize runs before the write, so `size` and the bytes on
        # disk are the same picture. A capture the provider would rescale again
        # is a capture whose coordinate frame the container cannot reproduce.
        record, capture_note = _capture_from_response(resp.headers)
        capture = None
        raw_size = capture_image_size(record)
        if raw_size is not None:
            target = delivered_size(*raw_size)
            if target != raw_size:
                resized, resize_err = _resize_capture(content, media_type, target)
                if resize_err:
                    # Deliberately not a fallback to the unresized bytes: an
                    # oversize picture delivered as if it were in frame is a
                    # click that lands somewhere else, which is worse than no
                    # picture at all.
                    return {
                        "status": "error",
                        "error": (
                            f"The {raw_size[0]}x{raw_size[1]} capture is over "
                            f"the vision envelope and could not be resized to "
                            f"{target[0]}x{target[1]}: {resize_err}. Nothing "
                            f"was written."
                        ),
                    }
                content = resized
            page = record.get("page")
            if not isinstance(page, dict):
                page = None
            capture = {
                "image": list(target),
                "viewport": page.get("viewport") if page else None,
                "dpr": page.get("dpr", 1) if page else 1,
                "scale": round(envelope_scale(*raw_size), 6),
                "full_page": bool(record.get("full_page")),
            }

        if args.output:
            # Already resolved and already contained — the stamp on the
            # declaration did it, and the value on the namespace *is* the
            # resolved path.
            resolved = Path(args.output)
            try:
                write_resolved(resolved, content)
            except OSError as e:
                return {
                    "status": "error",
                    "error": f"could not write {resolved}: {e}",
                }
        else:
            resolved, path_err = _write_derived_capture(
                directory, media_type, content,
            )
            if path_err:
                return {"status": "error", "error": path_err}
        result = {
            "status": "ok",
            "path": str(resolved),
            "size": len(content),
            "media_type": media_type,
            "capture": capture,
        }
        notes = []
        if capture_note:
            notes.append(capture_note)
        # `directory` is set only where the derived default was taken, so this
        # is the one branch that knows the capture is scratch — the resolved
        # path cannot be asked, since `--output` may legitimately name the temp
        # dir too and that write is still a file the caller chose.
        if directory is not None:
            notes.append(SCRATCH_NOTE)
        if notes:
            result["notes"] = notes
        workspace_path = _workspace_relative(resolved)
        if workspace_path:
            result["workspace_path"] = workspace_path
        return result
    if is_image:
        return {
            "status": "error",
            "error": (
                f"Browser API returned HTTP {resp.status_code} with "
                f"{len(resp.content)} bytes labelled "
                f"{resp.headers.get('content-type', 'nothing')} — not a screenshot."
            ),
        }
    return _decode(resp)


def cmd_extract(args):
    """Extract content by CSS selector."""
    url = get_api_url()
    payload = {
        "selector": args.selector,
        "timeout": args.timeout,
    }
    if args.url:
        payload["url"] = args.url
    if args.session:
        payload["session_id"] = args.session
    if args.max_chars:
        payload["max_chars"] = args.max_chars
    if args.limit:
        payload["limit"] = args.limit

    resp = httpx.post(f"{url}/extract", json=payload, timeout=REQUEST_TIMEOUT)
    return _decode(resp)


class OrderedAppend(argparse.Action):
    """`append`, plus a note of where this value sat on the command line.

    `--click`, `--fill` and `--fill-credential` are separate arguments — a
    marker inside `--fill`'s value would be ambiguous against a literal
    beginning with it, and the declaration is what the coverage walk reads —
    but a login is a form filled field by field and *then* submitted, so the
    three have to interleave in the order the caller wrote them. Argparse
    keeps no cross-argument order, so each value records `(dest, index)` in
    `ACTION_ORDER_DEST` as it lands and `cmd_interact` replays that list.

    Not a shared dest, which is the shape this replaces: the credential stamp
    resolves whatever sits on its own dest, so a mixed list would send every
    literal `--fill` value to the proxy as a credential name.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        items = list(getattr(namespace, self.dest, None) or [])
        items.append(values)
        setattr(namespace, self.dest, items)
        order = list(getattr(namespace, ACTION_ORDER_DEST, None) or [])
        order.append((self.dest, len(items) - 1))
        setattr(namespace, ACTION_ORDER_DEST, order)


class OrderedFlag(OrderedAppend):
    """`OrderedAppend` for an option that takes no value.

    `--click-challenge` names no target: the checkbox lives in a closed shadow
    root inside a cross-origin frame, so the container measures it and there is
    nothing for the caller to pass. A `store_true` would record no position,
    which is the wrinkle ISSUE-525 asks to be decided rather than inherited —
    and appending last is wrong the moment somebody presses the checkbox and
    then types into the page behind it, which is the ordering ISSUE-507
    established has to be the caller's to choose.

    So it keeps a position. Argparse hands a zero-argument action an empty
    list, so what lands on the dest is this class's own marker: the record
    needs a slot to count and an index to replay, and the emitter reads
    neither. That also keeps `_interact_actions`' invariant intact, since one
    order entry still answers to one value.
    """

    def __init__(self, option_strings, dest, nargs=None, **kwargs):
        if nargs not in (None, 0):
            raise ValueError("OrderedFlag takes no value, so nargs must be 0")
        super().__init__(option_strings, dest, nargs=0, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        super().__call__(parser, namespace, True, option_string)


def _click_action(selector):
    return {"type": "click", "selector": selector}


def _fill_action(spec):
    if "=" not in spec:
        # Refused, not skipped. A dropped fill leaves the clicks around it to
        # run against an empty form and report `ok`, which is ISSUE-507's
        # symptom by a second route; `--fill-credential` already refuses the
        # same shape before dispatch.
        raise ValueError(
            f"Malformed --fill value: expected SELECTOR=VALUE, got {spec!r}"
        )
    selector, value = spec.split("=", 1)
    return {"type": "fill", "selector": selector, "value": value}


def _fill_credential_action(pair):
    if not isinstance(pair, CredentialPair):
        raise ValueError(
            "--fill-credential was not resolved; the shared-credential "
            "lookup did not run for this call"
        )
    return {"type": "fill", "selector": pair.label, "value": pair.value.reveal()}


def _point(spec, flag):
    """`X,Y` in the delivered picture's pixel space → `(x, y)`.

    Refused, not coerced. A point the caller meant and this could not read is
    a click somewhere else, and the actions around it would still run and
    still report `ok` — the shape `_fill_action` already refuses for the same
    reason.
    """
    if not isinstance(spec, str) or "," not in spec:
        raise ValueError(f"Malformed {flag} value: expected X,Y, got {spec!r}")
    raw_x, _, raw_y = spec.partition(",")
    try:
        x, y = float(raw_x.strip()), float(raw_y.strip())
    except ValueError:
        raise ValueError(
            f"Malformed {flag} value: X and Y must be numbers, got {spec!r}"
        ) from None
    if not (math.isfinite(x) and math.isfinite(y)):
        raise ValueError(f"Malformed {flag} value: X and Y must be finite, got {spec!r}")
    return x, y


def _click_at_action(spec):
    x, y = _point(spec, "--click-at")
    return {"type": "click_at", "x": x, "y": y}


def _hover_at_action(spec):
    x, y = _point(spec, "--hover-at")
    return {"type": "hover_at", "x": x, "y": y}


def _press_action(key):
    # The container hands the name to xdotool, which refuses one it does not
    # know; nothing here tries to keep a second copy of that key table.
    return {"type": "key", "key": key}


def _type_action(text):
    return {"type": "type", "text": text}


def _click_challenge_action(_marker):
    """The one action that locates its own target.

    `_marker` is `OrderedFlag`'s placeholder and carries nothing: the
    container finds the Cloudflare checkbox from the challenge frame's
    bounding box plus a measured inset, which is the only handle on an
    element no selector reaches.
    """
    return {"type": "click_challenge"}


#: Which dest an order record may name, and what each one emits. One table
#: rather than a tuple of dests beside a chain of branches: an argument added
#: to only one of those reaches whichever branch is last and is resolved as
#: what *that* branch handles, which for the shape this replaces meant a
#: selector unwrapped as a `CredentialPair`. A dest with no entry cannot be
#: replayed at all, and there is nowhere to add one that leaves it unhandled.
ACTION_EMITTERS = {
    "click": _click_action,
    "fill": _fill_action,
    "fill_credential": _fill_credential_action,
    "click_at": _click_at_action,
    "hover_at": _hover_at_action,
    "press": _press_action,
    "type": _type_action,
    "click_challenge": _click_challenge_action,
}
#: The dests `OrderedAppend` may be declared on, and the fallback order.
ORDERED_ACTION_DESTS = tuple(ACTION_EMITTERS)


def _interact_actions(args):
    """The click and fill actions for one `interact`, in the order written.

    The browser runs this array in order, so the order is the behaviour: a
    login is two fills and then a click, and emitting the click first submits
    an empty form and reports `ok` for every action (ISSUE-507). Position is
    the only rule here; nothing infers that a click ought to follow a fill,
    because a click that opens a modal is as ordinary as one that submits.
    `--scroll` is not in the record — it is not repeatable, so it has no
    position — and `cmd_interact` appends it last.

    A `--fill-credential` value arrives here already resolved by the stamp, as
    a `CredentialPair`, and `reveal()` is the one call that unwraps it — the
    value goes into the request body and nowhere else. An *unresolved* string
    on that dest means the argument reached a handler without going through
    `parse_and_resolve`, which is the one thing the pre-dispatch refusal exists
    to prevent; it raises rather than filling the field with the credential's
    name, since typing a name into a password box is a failed login whose cause
    is invisible from the result.
    """
    values = {}
    for dest in ORDERED_ACTION_DESTS:
        raw = getattr(args, dest, None) or []
        if not isinstance(raw, (list, tuple)):
            # Named rather than left to `list(True)`'s raw TypeError, which
            # reads as a browser failure rather than as a malformed call.
            # `--click-challenge` is the one that invites it: a hand-built
            # namespace naturally spells a valueless option `True`, where
            # `OrderedFlag` appends one marker per occurrence so that the
            # order record keeps a slot to count (ISSUE-531).
            raise ValueError(
                f"{dest} must be a list of values, not {type(raw).__name__}; "
                f"a valueless action is recorded as one marker per occurrence"
            )
        values[dest] = list(raw)
    order = list(getattr(args, ACTION_ORDER_DEST, None) or [])
    if not order:
        # A caller that built the namespace itself has no order record —
        # `OrderedAppend` is the argparse action, so it fires under a plain
        # `parse_args` too and argv callers never land here. Clicks, then
        # literals, then credentials: what such a caller saw before ISSUE-507.
        order = [
            (dest, i)
            for dest in ORDERED_ACTION_DESTS
            for i in range(len(values[dest]))
        ]
    # Dests before lengths: an unrecognised dest contributes an order entry and
    # no value, so it fails the count too, and that is the less useful of the
    # two answers.
    for dest, _index in order:
        if dest not in ACTION_EMITTERS:
            raise ValueError(f"no interact action is defined for {dest}")
    if len(order) != sum(len(v) for v in values.values()):
        # A hand-built record naming fewer values than were parsed would drop
        # the rest in silence, leaving the clicks it does name to run against a
        # form that was never filled.
        raise ValueError("the action order record does not match the values parsed")

    actions = []
    for dest, index in order:
        source = values[dest]
        if index >= len(source):
            raise ValueError(
                f"the {dest} order record does not match the values parsed"
            )
        actions.append(ACTION_EMITTERS[dest](source[index]))
    return actions


#: What a credential that came back in a response is replaced with.
CREDENTIAL_REDACTION = "[credential]"


def _scrub(payload, secrets):
    """The response with any of `secrets` replaced, wherever a string holds one.

    The container does not echo a filled value — `/interact` reports the
    selector and `ok` — but three things it returns are not under that rule and
    are read by the model: `page.url`, which carries the value outright when a
    submit navigates a **GET** form; the page text, when the page itself echoes
    what was typed into a confirmation or a validation message; and the `error`
    string on the container's 500 branch, which is a third-party library's
    exception text this repository does not control. So the one place that
    knows which strings are credentials takes them back out, rather than the
    claim resting on what three other programs happen to print.

    It is a backstop and not a boundary: a page can encode, split or re-case a
    value, and none of those is matched. What it removes is the plain case,
    which is the one that actually happens.
    """
    if not secrets:
        return payload
    if isinstance(payload, str):
        for secret in secrets:
            if secret:
                payload = payload.replace(secret, CREDENTIAL_REDACTION)
        return payload
    if isinstance(payload, dict):
        return {key: _scrub(value, secrets) for key, value in payload.items()}
    if isinstance(payload, list):
        return [_scrub(item, secrets) for item in payload]
    return payload


def _session_capture(url, session_id):
    """The session's recorded capture frame: `(record, error)`.

    A coordinate action is interpreted against the frame the container
    recorded when the screenshot was taken, and this process has no memory of
    that call — so the frame is fetched rather than carried. It does not have
    to be carried: everything the conversion needs is on the record, which is
    why nothing asks the model to quote a picture size back.

    Three absences, named apart because the remedies differ: a container with
    no `capture` key at all predates visual mode, a `null` one has never been
    screenshotted (or its screenshot was taken by the URL form, which closes
    its own session, or Chrome has been relaunched since), and one that will
    not read is a record this skill cannot use.
    """
    resp = httpx.get(f"{url}/sessions/{session_id}", timeout=REQUEST_TIMEOUT)
    data = _decode(resp)
    if not isinstance(data, dict) or data.get("status") == "error":
        return None, (
            data.get("error")
            if isinstance(data, dict)
            else f"could not read session {session_id}"
        )
    if "capture" not in data:
        return None, (
            "This browser container keeps no capture frame on its session "
            "record, so it predates visual mode — --click-at and --hover-at "
            "are unavailable here. Drive the page with --click and a CSS "
            "selector instead."
        )
    record = data["capture"]
    if record is None:
        return None, (
            f"Session {session_id} has no screenshot on record, so there is "
            f"nothing to interpret a point against. Take one first with "
            f"`browse screenshot --session {session_id}`. A screenshot taken "
            f"by the URL form records nothing, because it closes its own "
            f"session, and a Chrome relaunch clears the record."
        )
    if capture_image_size(record) is None:
        return None, (
            f"Session {session_id} carries a capture record this skill could "
            f"not read, so no point on it can be converted. Re-take the "
            f"screenshot."
        )
    return record, None


def _note_stale_container(data):
    """Name the container as too old where it refused an action it never had.

    `/interact`'s unknown-action branch answers `{"ok": false, "error":
    "unknown"}` and carries on, which is the right shape and says nothing a
    reader can act on. The visual-mode types are the only ones this skill
    emits that a deployed image may not implement, so an `unknown` naming one
    of them means the container is behind this code rather than that the model
    invented an action.
    """
    if not isinstance(data, dict):
        return data
    stale = sorted({
        result.get("action")
        for result in (data.get("actions") or ())
        if isinstance(result, dict)
        and result.get("error") == "unknown"
        and result.get("action") in VISUAL_ACTION_TYPES
    })
    if not stale:
        return data
    notes = list(data.get("notes") or [])
    notes.append(
        "This browser container does not implement "
        + ", ".join(stale)
        + " — it predates visual mode. Rebuild the browser image, or drive the "
        "page with --click and a CSS selector."
    )
    return {**data, "notes": notes}


def _note_unreported_actions(data, actions):
    """Say which actions came back with no result, and that they may have run.

    `/interact` runs its list in order and appends one result per action; a
    raise abandons the rest and the handler returns the partial list with a
    500. So an action with no result is **not** an action that did not happen:
    the raise can land after the pointer has already moved and pressed, which
    is measured rather than hypothetical — `xdotool mousemove --sync` blocks
    on a zero-distance move and times out, and the timeout can fire on the
    final landing move of a click that has already been delivered.

    Naming it is the whole of what this can do. The shape is `cmd_render`'s
    404 arm's: say what is known rather than inferring, because the one answer
    that must not be given here is a bare failure — a model reading that
    retries the click, and a retried click that already landed is a second
    click nobody asked for.
    """
    if not isinstance(data, dict) or data.get("status") != "error":
        return data
    results = data.get("actions")
    if not isinstance(results, list) or len(results) >= len(actions):
        return data
    unreported = [str(a.get("type")) for a in actions[len(results):]]
    notes = list(data.get("notes") or [])
    notes.append(
        "The browser failed before these actions reported a result: "
        + ", ".join(unreported)
        + ". The first of them may still have happened — the failure can land "
        "after the pointer has moved and pressed. Take a fresh screenshot and "
        "look at the page before repeating it; do not simply retry."
    )
    return {**data, "notes": notes, "unreported_actions": unreported}


# A transport failure that provably happened before the request left this
# process. Nothing reached the container, so no action can have run, and the
# caution below would be a false alarm pointing at the wrong remedy — these
# re-raise and `main`'s `describe` reports them by class. Only `ConnectError`
# reads as "is the container running?"; the other four name themselves.
# `WriteTimeout` and `WriteError` are deliberately *not* here: those fire
# mid-send, where the body may already be complete on the wire.
PRE_SEND_TRANSPORT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
    httpx.UnsupportedProtocol,
)


def _note_unanswered_interaction(url, command, actions, exc):
    """Say the whole list is in doubt where nothing answered at all.

    `_note_unreported_actions` is the same caution reasoned from a *response*
    — it reads the results list and names the actions past its end. A failure
    that produces no response misses it entirely (ISSUE-522), and the
    container is the one party that never learns the client went away: it
    goes on executing the rest of the list.

    Necessarily weaker than the response arm, and worded so the difference is
    readable. With no results list there is no way to say *which* actions are
    in doubt, so the honest answer is that all of them are — "some of these
    may have happened" and "any of these may have happened" call for
    different recovery, and collapsing them into one sentence would lose the
    distinction the model needs.

    The class name is carried into the message for the reason `describe`
    records one layer up: `str()` on an httpx `ReadTimeout` is "timed out"
    and on several of its siblings the empty string, naming no verb, no URL
    and no class.
    """
    unreported = [str(a.get("type")) for a in actions]
    detail = str(exc).strip()
    return error_envelope(
        f"browse {command} against {url} got no answer: "
        f"{type(exc).__name__}{': ' + detail if detail else ''}",
        notes=[
            "The browser never answered, so none of these actions reported a "
            "result: " + ", ".join(unreported)
            + ". The container does not learn that the client went away, so "
            "it carries on running the list — any of them may have happened, "
            "in full or in part. Take a fresh screenshot and look at the page "
            "before repeating any of it; do not simply retry."
        ],
        unreported_actions=unreported,
    )


def cmd_interact(args):
    """Interact with an existing session."""
    url = get_api_url()
    actions = _interact_actions(args)
    if args.scroll:
        # Last, and the one action whose position the caller does not choose;
        # skill.md says so rather than leaving it to be found in a result.
        actions.append({"type": "scroll", "direction": args.scroll, "amount": args.scroll_amount})

    # A point is read off the delivered picture, so the container is told what
    # that picture measured and converts from it. The size is recomputed from
    # the record rather than remembered, which is the same arithmetic
    # `cmd_screenshot` applied — one function, so the two cannot disagree.
    framed = [a for a in actions if a.get("type") in IMAGE_SPACE_ACTIONS]
    if framed:
        record, capture_err = _session_capture(url, args.session_id)
        if capture_err:
            return {"status": "error", "error": capture_err}
        size = list(delivered_size(*capture_image_size(record)))
        for action in framed:
            action["image_size"] = size

    payload = {
        "session_id": args.session_id,
        "actions": actions,
    }

    # Only the POST is wrapped. `_session_capture`'s GET above runs before any
    # action is sent, so a failure there is provably pre-send and must keep
    # propagating — cautioning about it would claim actions may have run
    # against a call that had not been made.
    try:
        resp = httpx.post(f"{url}/interact", json=payload, timeout=REQUEST_TIMEOUT)
    except PRE_SEND_TRANSPORT_ERRORS:
        raise
    except httpx.TransportError as exc:
        return _note_unanswered_interaction(url, args.command, actions, exc)

    secrets = [
        pair.value.reveal()
        for pair in (getattr(args, "fill_credential", None) or [])
        if isinstance(pair, CredentialPair)
    ]
    decoded = _note_stale_container(_scrub(_decode(resp), secrets))
    return _note_unreported_actions(decoded, actions)


def _links_from_extract(data):
    """Extract links from /extract response elements.

    Prefers the 'href' attribute returned directly on each element
    (set when the matched element is itself a link). Falls back to
    parsing <a href> tags from inner HTML for nested links.
    """
    links = []
    for el in data.get("elements", []):
        href = el.get("href")
        if href:
            # Element itself is a link — use its text and href directly
            links.append({"text": el.get("text", "").strip(), "href": href})
        else:
            # Search for <a> tags inside the element's inner HTML
            html = el.get("html", "")
            for match in re.finditer(
                r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                html,
                re.DOTALL,
            ):
                h, text = match.group(1), match.group(2)
                text = re.sub(r"<[^>]+>", "", text).strip()
                links.append({"text": text, "href": h})
    return links


def cmd_links(args):
    """Fetch a page and return only the links."""
    url = get_api_url()

    if args.selector and args.session:
        # Extract links from specific elements in existing session
        payload = {"selector": args.selector, "timeout": args.timeout}
        payload["session_id"] = args.session
        resp = httpx.post(f"{url}/extract", json=payload, timeout=REQUEST_TIMEOUT)
        data = _decode(resp)
        if data.get("status") != "ok":
            return data
        links = _links_from_extract(data)
        return {
            "status": "ok",
            "url": data.get("url", ""),
            "count": len(links),
            "links": links,
        }
    elif args.selector:
        # Fetch page then extract links from selector
        payload = {"url": args.url, "timeout": args.timeout, "keep_session": False}
        resp = httpx.post(f"{url}/browse", json=payload, timeout=REQUEST_TIMEOUT)
        browse_data = _decode(resp)
        if browse_data.get("status") != "ok":
            return browse_data
        session_id = browse_data.get("session_id")
        # Extract from selector
        ext_payload = {"selector": args.selector, "timeout": args.timeout}
        if session_id:
            ext_payload["session_id"] = session_id
        else:
            ext_payload["url"] = args.url
        ext_resp = httpx.post(f"{url}/extract", json=ext_payload, timeout=REQUEST_TIMEOUT)
        data = _decode(ext_resp)
        # Clean up session if we got one
        if session_id:
            try:
                httpx.delete(f"{url}/sessions/{session_id}", timeout=5.0)
            except Exception:
                pass
        if data.get("status") != "ok":
            return data
        links = _links_from_extract(data)
        return {
            "status": "ok",
            "url": browse_data.get("url", args.url),
            "count": len(links),
            "links": links,
        }
    else:
        # Simple: fetch page, return only links
        payload = {"url": args.url, "timeout": args.timeout, "keep_session": False}
        if args.session:
            payload["session_id"] = args.session
        if args.max_links:
            payload["max_links"] = args.max_links
        resp = httpx.post(f"{url}/browse", json=payload, timeout=REQUEST_TIMEOUT)
        data = _decode(resp)
        if data.get("status") != "ok":
            return data
        links = data.get("links", [])
        result = {
            "status": "ok",
            "url": data.get("url", args.url),
            "count": len(links),
            "links": links,
        }
        # The envelope is rebuilt rather than filtered, so the container's
        # clipping verdict has to be copied across or it is lost here
        # (ISSUE-531). `count` alone cannot carry it: a full array of
        # navigation chrome and a complete list of the same length are the
        # same number. Copied only when present, which is how the container
        # sends it — an untruncated page carries none of them.
        #
        # `links_truncated_by` is the one that says whether the remedy the
        # other two imply can work at all (ISSUE-533): a budget this verb
        # can raise, or the container's scan ceiling, which no --max-links
        # reaches past. Dropping it here would leave this verb advertising a
        # retry it cannot perform, which is the shape ISSUE-531 was about.
        for key in ("links_truncated", "links_truncated_by", "anchors_total"):
            if key in data:
                result[key] = data[key]
        return result


def _note_missing_challenge_route(data, resp):
    """Name the container as too old where `/challenge` is not a route at all.

    The API answers an unknown session with its own JSON 404, so a 404 whose
    body is not JSON is Flask saying the route does not exist — which on this
    endpoint means the image predates the visual path. Without this the caller
    gets `_decode`'s excerpt of an HTML error page, which names a status and
    nothing to do about it. Same sentence `_note_stale_container` gives for an
    action the container never had.
    """
    if resp.status_code != 404:
        return data
    if "json" in (resp.headers.get("content-type") or "").lower():
        return data
    if not isinstance(data, dict):
        return data
    notes = list(data.get("notes") or [])
    notes.append(
        "This browser container has no /challenge endpoint — it predates "
        "visual mode. Rebuild the browser image, or drive the page with "
        "--click and a CSS selector."
    )
    return {**data, "notes": notes}


def cmd_challenge(args):
    """Where the challenge widgets are, without pressing anything.

    Geometry rather than a verdict: `get` answers whether a page is a
    challenge, this answers where the widget is and whether the container can
    locate it. That is the distinction ISSUE-525 wanted, because "no challenge
    here" and "a challenge this cannot find" both leave `--click-challenge`
    with nothing to press and want opposite things done about them.
    """
    url = get_api_url()
    resp = httpx.post(
        f"{url}/challenge",
        json={"session_id": args.session_id},
        timeout=REQUEST_TIMEOUT,
    )
    return _note_missing_challenge_route(_decode(resp), resp)


def cmd_close(args):
    """Close a session."""
    url = get_api_url()
    resp = httpx.delete(f"{url}/sessions/{args.session_id}", timeout=30.0)
    return _decode(resp)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.browse",
        description="Web browsing via headless browser container",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # get
    p_get = sub.add_parser("get", help="Browse a URL")
    p_get.add_argument("url", help="URL to browse")
    p_get.add_argument("--keep-session", action="store_true", help="Keep session alive for follow-up")
    p_get.add_argument("--session", help="Reuse existing session ID")
    p_get.add_argument("--timeout", type=int, default=30, help="Navigation timeout in seconds")
    p_get.add_argument("--wait-for", help="CSS selector to wait for after load")
    p_get.add_argument("--skip-behavior", action="store_true",
                       help="Skip simulated mouse/scroll after load (for DataDome-protected sites)")
    p_get.add_argument("--max-chars", type=int, help="Page text budget (default 50000)")
    p_get.add_argument("--max-links", type=int, help="Link budget (default 100)")

    # render
    p_render = sub.add_parser(
        "render", help="Render a page to markdown (keeps headings + links together)",
    )
    p_render.add_argument("url", nargs="?", help="URL to render")
    p_render.add_argument("--mode", choices=["full", "article"], default="full",
                          help="full = whole page (hubs/indexes); article = main content only")
    p_render.add_argument("--session", help="Existing session ID")
    p_render.add_argument("--keep-session", action="store_true", help="Keep session alive")
    p_render.add_argument("--timeout", type=int, default=30, help="Navigation timeout in seconds")
    p_render.add_argument("--wait-for", help="CSS selector to wait for after load")
    p_render.add_argument("--max-chars", type=int, help="Markdown budget (default 100000)")
    p_render.add_argument("--include-frames", action="store_true",
                          help="Splice iframe content into the markdown (counts "
                               "against --max-chars)")
    p_render.add_argument("--skip-behavior", action="store_true",
                          help="Skip simulated mouse/scroll after load")

    # screenshot
    p_ss = sub.add_parser("screenshot", help="Take a screenshot")
    p_ss.add_argument("url", nargs="?", help="URL to screenshot")
    p_ss.add_argument("--session", help="Existing session ID")
    host_path(
        p_ss, "--output", "-o", mode=WRITE,
        help=(
            "Output file path, absolute and inside your own workspace. Pass "
            "it when the picture is for the user to see or keep. Without it "
            "the capture is a scratch file in this task's temp directory, "
            "which is swept and which a reply cannot show."
        ),
    )
    p_ss.add_argument("--full-page", action="store_true", help="Capture full page")
    p_ss.add_argument("--timeout", type=int, default=30)

    # extract
    p_ext = sub.add_parser("extract", help="Extract content by CSS selector")
    p_ext.add_argument("url", nargs="?", help="URL to extract from")
    p_ext.add_argument("--selector", "-s", required=True, help="CSS selector")
    p_ext.add_argument("--session", help="Existing session ID")
    p_ext.add_argument("--timeout", type=int, default=30)
    p_ext.add_argument("--max-chars", type=int,
                       help="Per-element text/HTML budget (default 25000)")
    p_ext.add_argument("--limit", type=int, help="Max matched elements (default 20)")

    # interact
    p_int = sub.add_parser("interact", help="Interact with existing session")
    p_int.add_argument("session_id", help="Session ID")
    p_int.add_argument(
        "--click", action=OrderedAppend,
        help="CSS selector to click, where you wrote it among the fills",
    )
    p_int.add_argument(
        "--fill", action=OrderedAppend, help="selector=value to fill",
    )
    credential_ref(
        p_int, "--fill-credential", form=PAIR, action=OrderedAppend,
        metavar="SELECTOR=NAME",
        help=(
            "Fill a form field with one of your shared credentials, named "
            "rather than typed: SELECTOR=NAME, split at the last =. Prefer "
            "this to --fill for a password or a token — the value is looked "
            "up outside the sandbox and never enters your command line or "
            "your argv. `istota-credential list` names what is available."
        ),
    )
    p_int.add_argument(
        "--click-at", action=OrderedAppend, metavar="X,Y",
        help=(
            "Click a point, in the pixel space of the picture `browse "
            "screenshot` delivered — the coordinates you read off it. Needs a "
            "screenshot of this session on record, and is refused if the page "
            "has scrolled or navigated since."
        ),
    )
    p_int.add_argument(
        "--hover-at", action=OrderedAppend, metavar="X,Y",
        help="Move the pointer to a point, in the delivered picture's pixel space.",
    )
    p_int.add_argument(
        "--click-challenge", action=OrderedFlag,
        help=(
            "Press the Cloudflare challenge checkbox. Takes no coordinate — "
            "the container measures the widget itself, which is more accurate "
            "than reading it off a screenshot and is the only way to reach an "
            "element that has no selector. Use `browse challenge` first to see "
            "whether there is one and where it is."
        ),
    )
    p_int.add_argument(
        "--press", action=OrderedAppend, metavar="KEY",
        help="Press a key (Tab, Enter, Escape, ctrl+a) at whatever has focus.",
    )
    p_int.add_argument(
        "--type", action=OrderedAppend, metavar="TEXT",
        help=(
            "Type text at whatever has focus. Use --fill-credential for a "
            "password: a coordinate click that missed types the value into "
            "whatever holds focus instead, with no failure and no signal."
        ),
    )
    p_int.add_argument("--scroll", choices=["up", "down"], help="Scroll direction")
    p_int.add_argument("--scroll-amount", type=int, default=500, help="Scroll pixels")

    # links
    p_links = sub.add_parser("links", help="Fetch a page and return only links")
    p_links.add_argument("url", nargs="?", help="URL to fetch links from")
    p_links.add_argument("--selector", "-s", help="CSS selector to extract links from")
    p_links.add_argument("--session", help="Existing session ID")
    p_links.add_argument("--timeout", type=int, default=30, help="Navigation timeout in seconds")
    p_links.add_argument(
        "--max-links", type=int,
        help="Link budget (default 100). Raise it when the answer says "
             "links_truncated; ignored with --selector, which has its own.",
    )

    # challenge
    p_chal = sub.add_parser(
        "challenge", help="Where the challenge widgets on this page are",
    )
    p_chal.add_argument("session_id", help="Session ID")

    # close
    p_close = sub.add_parser("close", help="Close a session")
    p_close.add_argument("session_id", help="Session ID to close")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parse_and_resolve(parser, argv)

    commands = {
        "get": cmd_get,
        "render": cmd_render,
        "screenshot": cmd_screenshot,
        "extract": cmd_extract,
        "interact": cmd_interact,
        "links": cmd_links,
        "challenge": cmd_challenge,
        "close": cmd_close,
    }

    def describe(exc: BaseException) -> dict:
        if isinstance(exc, httpx.ConnectError):
            # Nothing answered at all, which is a different fact from the
            # container answering badly and stays a separate message.
            return error_envelope(
                f"Cannot connect to browser API at {get_api_url()}. "
                "Is the container running?"
            )
        # `str(exc)` alone is the same defect ISSUE-383 fixed one layer down:
        # httpx's ReadTimeout stringifies to "timed out" and several of its
        # siblings to the empty string, naming no verb, no URL and no class.
        # That is reachable on the ordinary path, since REQUEST_TIMEOUT sits
        # above the container's own 90s watchdog.
        detail = str(exc).strip()
        return error_envelope(
            f"browse {args.command} against {get_api_url()} failed: "
            f"{type(exc).__name__}{': ' + detail if detail else ''}"
        )

    # A failure the endpoint reported in a well-formed body is still a failure,
    # which `run_skill_cli` is what enforces. `_decode` turned what used to be
    # an uncaught exception into a return value, so without that a 500 would
    # report on exit 0 where the decode error at least exited 1. Only "error"
    # counts: "closed" and "not_found" are answers.
    run_skill_cli(commands, args, on_exception=describe)


if __name__ == "__main__":
    main()
