"""A draft's files built into the one self-contained document an installed version serves.

``index.html`` is tokenized with the stdlib :class:`html.parser.HTMLParser` and
edited in place by offset: every byte the builder does not deliberately change
is emitted as the user wrote it. The edits are

- ``<script src>`` and ``<link rel=stylesheet>`` replaced by the referenced
  file's text, inline;
- resource hints, ``<meta http-equiv>``, ``<base>``, every other ``<link>``
  and every frame, object and embed tag removed;
- the house kit inserted straight after the doctype, or at the very start
  when there is none, so its guard is the first script the browser runs.

The CSP then pins the hash of each inline script exactly as emitted, so a
script runs only if it is text the user approved. A script containing
``</script`` or ``<!--`` is refused rather than escaped, because the browser
would end or extend it somewhere else than this parser does, and the hash
would no longer cover what runs.
"""

from __future__ import annotations

import base64
import hashlib
import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser

from istota.apps.kit import Kit
from istota.apps.manifest import Manifest

INDEX = "index.html"
MANIFEST = "manifest.toml"
SKIPPED_FOLDERS = ("data", "preview")

RESOURCE_HINTS = frozenset({"dns-prefetch", "preconnect", "prefetch", "preload", "prerender", "modulepreload"})
FRAME_TAGS = frozenset({"iframe", "object", "embed", "frame", "frameset", "applet", "portal", "fencedframe"})
URL_ATTRIBUTES = frozenset({
    "src", "href", "action", "formaction", "poster", "data", "xlink:href", "background", "ping", "manifest",
})

_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_HOST_URL = re.compile(r"(?i)\b(?:https?|wss?|ftp)://([^\s/\"'`<>)\\]+)[^\s\"'`<>)\\]*")
# Namespace URIs, written as strings and never fetched.
_NAMESPACE_HOSTS = frozenset({"www.w3.org"})
_NATIVE_DIALOG = re.compile(r"(?:\bwindow\s*\.\s*|(?<![\w$.]))(alert|confirm|prompt)\s*\(")
_WIDTH = re.compile(r"(?<![-\w])(?:min-)?width\s*:\s*(\d+(?:\.\d+)?)px")
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)
# All a browser lets precede a doctype that still counts as one.
_BEFORE_DOCTYPE = re.compile(r"(?:[ \t\n\f\r\ufeff]|<!--.*?-->)*", re.S)
PHONE_WIDTH = 320
# Inlined, or removed, so their URLs are checked differently or not at all.
_NOT_URL_CHECKED = FRAME_TAGS | {"script", "link", "base", "meta"}


class DocumentError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class BuiltDocument:
    html: bytes
    script_hashes: tuple[str, ...]
    csp: str
    content_hash: str
    source: dict[str, str]
    warnings: tuple[str, ...]


def csp_for(script_hashes) -> str:
    """The frame's Content-Security-Policy for a document with these inline script hashes."""
    sources = " ".join(f"'{digest}'" for digest in script_hashes) or "'none'"
    return (
        f"default-src 'none'; script-src {sources}; style-src 'unsafe-inline'; "
        "img-src data: blob:; font-src data:; media-src data: blob:; connect-src 'none'; "
        "frame-src 'none'; worker-src 'none'; form-action 'none'; base-uri 'none'; "
        "frame-ancestors 'self'; sandbox allow-scripts"
    )


def script_hash(text: str) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii")


@dataclass(frozen=True)
class _Tag:
    kind: str  # "start", "end" or "decl"
    name: str
    attrs: dict[str, str]
    start: int
    end: int
    self_closing: bool = False


class _Scanner(HTMLParser):
    """Every tag with its offsets in the source; data is not recorded."""

    def __init__(self, source: str):
        super().__init__(convert_charrefs=False)
        self.source = source
        self.line_starts = [0] + [match.end() for match in re.finditer("\n", source)]
        self.tags: list[_Tag] = []

    def _offset(self) -> int:
        line, column = self.getpos()
        return self.line_starts[line - 1] + column

    def _start(self, tag, attrs, self_closing):
        start = self._offset()
        raw = self.get_starttag_text() or ""
        values = {}
        for key, value in attrs:
            values.setdefault(key, value if value is not None else "")
        self.tags.append(_Tag("start", tag, values, start, start + len(raw), self_closing))

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs, False)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, True)

    def handle_endtag(self, tag):
        start = self._offset()
        close = self.source.find(">", start)
        end = len(self.source) if close < 0 else close + 1
        self.tags.append(_Tag("end", tag, {}, start, end))

    def handle_decl(self, decl):
        start = self._offset()
        self.tags.append(_Tag("decl", decl, {}, start, start + len(decl) + 3))


def _scan(source: str) -> list[_Tag]:
    scanner = _Scanner(source)
    scanner.feed(source)
    scanner.close()
    return scanner.tags


def _elements(tags: list[_Tag], name: str, source: str):
    """``(start tag, content start, content end, element end)`` for each raw-text element ``name``."""
    found = []
    for index, tag in enumerate(tags):
        if tag.kind != "start" or tag.name != name or tag.self_closing:
            continue
        following = tags[index + 1] if index + 1 < len(tags) else None
        if following is not None and following.kind == "end" and following.name == name:
            found.append((tag, tag.end, following.start, following.end))
        else:
            found.append((tag, tag.end, len(source), len(source)))
    return found


def _inline_scripts(document: str) -> list[str]:
    tags = _scan(document)
    return [document[begin:finish] for _tag, begin, finish, _end in _elements(tags, "script", document)]


def _reference(value: str, suffix: str) -> str | None:
    """The app-relative path ``value`` names, or None when it is not a plain relative path."""
    value = value.strip()
    if not value or _SCHEME.match(value) or value.startswith("/"):
        return None
    if any(char in value for char in "\\?#%"):
        return None
    segments = [segment for segment in value.split("/") if segment != "."]
    if not segments or any(segment in ("", "..") for segment in segments):
        return None
    path = "/".join(segments)
    if not path.endswith(suffix) or segments[0] in SKIPPED_FOLDERS:
        return None
    return path


def _url_problem(value: str) -> str | None:
    """``"host"`` for a URL that reaches somewhere, ``"relative"`` for one that will not load."""
    value = value.strip()
    if not value or value.startswith("#"):
        return None
    if value.startswith("//"):
        return "host"
    scheme = _SCHEME.match(value)
    if scheme:
        if scheme.group(0).lower() in ("data:", "blob:"):
            return None
        return "host"
    return "relative"


def _decode(files: dict[str, bytes], path: str, errors: list[str]) -> str | None:
    try:
        return files[path].decode("utf-8")
    except UnicodeDecodeError:
        errors.append(f"{path} is not UTF-8 text.")
        return None


def _host_urls(text: str) -> list[str]:
    urls = []
    for match in _HOST_URL.finditer(text):
        host = match.group(1).lower()
        if host not in _NAMESPACE_HOSTS:
            urls.append(match.group(0))
    return urls


def _wide_css(css: str) -> bool:
    css = _CSS_COMMENT.sub("", css)
    for chunk in css.split("}"):
        if "{" not in chunk:
            continue
        head, _, body = chunk.rpartition("{")
        selector = re.split(r"[{;]", head)[-1]
        names = {part.strip().lower() for part in selector.split(",")}
        if names & {"html", "body", ":root"} and _wide_declarations(body):
            return True
    return False


def _wide_declarations(text: str) -> bool:
    return any(float(match.group(1)) > PHONE_WIDTH for match in _WIDTH.finditer(text))


class _Build:
    def __init__(self, files: dict[str, bytes], manifest: Manifest, kit: Kit):
        self.files = files
        self.manifest = manifest
        self.kit = kit
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.source: dict[str, str] = {}
        self.referenced: list[str] = []
        self.scripts: list[str] = []
        self.styles: list[str] = []
        self.edits: list[tuple[int, int, str]] = []

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def referenced_text(self, path: str) -> str | None:
        if path not in self.files:
            self.errors.append(f"{INDEX} references {path}, which is not in the app folder.")
            return None
        text = _decode(self.files, path, self.errors)
        if text is None:
            return None
        if path not in self.referenced:
            self.referenced.append(path)
            self.source[path] = text
        return text

    def script(self, tag: _Tag, content_start: int, content_end: int, element_end: int, index: str):
        if tag.self_closing:
            self.errors.append(f"{INDEX}: a <script> tag cannot be self-closing; close it with </script>.")
            return
        if "src" not in tag.attrs:
            text = index[content_start:content_end]
            if "<!--" in text:
                self.errors.append(f"{INDEX}: an inline script contains '<!--'; write it another way.")
            self.scripts.append(text)
            return
        value = tag.attrs["src"]
        path = _reference(value, ".js")
        if path is None:
            self.errors.append(
                f"{INDEX}: <script src={value!r}> must be a relative path to a .js file in the app folder."
            )
            return
        text = self.referenced_text(path)
        if text is None:
            return
        lowered = text.lower()
        if "</script" in lowered:
            self.errors.append(f"{path} contains '</script', which would end the inlined script early.")
            return
        if "<!--" in text:
            self.errors.append(f"{path} contains '<!--', which changes how an inlined script is read.")
            return
        for dropped in ("defer", "async"):
            if dropped in tag.attrs:
                self.warn(
                    f"{INDEX}: {dropped} has no effect on the inlined {path}; "
                    "put the script at the end of <body> instead."
                )
        opening = "<script>"
        if "type" in tag.attrs:
            opening = f'<script type="{html.escape(tag.attrs["type"], quote=True)}">'
        self.scripts.append(text)
        self.edits.append((tag.start, element_end, f"{opening}{text}</script>"))

    def link(self, tag: _Tag):
        rels = set(tag.attrs.get("rel", "").lower().split())
        if "stylesheet" not in rels:
            if not rels & RESOURCE_HINTS:
                self.warn(f"{INDEX}: <link rel={tag.attrs.get('rel', '')!r}> was removed.")
            self.edits.append((tag.start, tag.end, ""))
            return
        value = tag.attrs.get("href", "")
        path = _reference(value, ".css")
        if path is None:
            self.errors.append(
                f"{INDEX}: <link href={value!r}> must be a relative path to a .css file in the app folder."
            )
            return
        text = self.referenced_text(path)
        if text is None:
            return
        if "</style" in text.lower():
            self.errors.append(f"{path} contains '</style', which would end the inlined stylesheet early.")
            return
        opening = "<style>"
        if "media" in tag.attrs:
            opening = f'<style media="{html.escape(tag.attrs["media"], quote=True)}">'
        self.styles.append(text)
        self.edits.append((tag.start, tag.end, f"{opening}{text}</style>"))

    def check_attributes(self, tag: _Tag, phone: bool, root_element: bool):
        handlers = sorted(name for name in tag.attrs if name.startswith("on"))
        if handlers:
            self.warn(
                f"{INDEX}: inline event handlers ({', '.join(handlers)}) never run in an app; "
                "use addEventListener in a script."
            )
        if phone and root_element and _wide_declarations(tag.attrs.get("style", "")):
            self.warn(f"{INDEX}: <{tag.name}> has a fixed width over {PHONE_WIDTH}px, too wide for a phone.")
        if tag.name in _NOT_URL_CHECKED:
            return
        for name in URL_ATTRIBUTES & set(tag.attrs):
            value = tag.attrs[name]
            problem = _url_problem(value)
            if problem == "host":
                self.errors.append(f"{INDEX}: <{tag.name} {name}={value!r}> points outside the app.")
            elif problem == "relative":
                self.warn(
                    f"{INDEX}: <{tag.name} {name}={value!r}> is not inlined and will not load; use a data: URL."
                )
        srcset = tag.attrs.get("srcset", "")
        if "//" in srcset:
            self.errors.append(f"{INDEX}: <{tag.name} srcset> points outside the app.")

    def kit_insertion(self, tags: list[_Tag], index: str) -> None:
        """The kit at the document's first insertion point, before any element can open.

        The browser puts a script there in an implied head, and a following
        ``<html>`` or ``<head>`` from the source merges into what is already
        open, so no source markup can run first or wrap the guard.
        """
        kit = (
            f"<script>{self.kit.guard_js}</script>"
            f"<script>{self.kit.client_js}</script>"
            f"<script>{self.kit.components_js}</script>"
            f"<style>{self.kit.css}</style>"
        )
        position = 0
        first = tags[0] if tags else None
        if (
            first is not None
            and first.kind == "decl"
            and first.name.lower().startswith("doctype")
            and _BEFORE_DOCTYPE.fullmatch(index[:first.start])
        ):
            position = first.end
        self.edits.append((position, position, kit))

    def index(self, index: str) -> str:
        tags = _scan(index)
        phone = self.manifest.narrowest == "phone"
        scripts = {element[0].start: element for element in _elements(tags, "script", index)}
        for _tag, begin, finish, _end in _elements(tags, "style", index):
            self.styles.append(index[begin:finish])

        removed_frames: set[str] = set()
        in_body = False
        first_in_body_seen = False
        for tag in tags:
            if tag.kind == "end":
                if tag.name in FRAME_TAGS:
                    self.edits.append((tag.start, tag.end, ""))
                continue
            if tag.kind != "start":
                continue
            root_element = tag.name in ("html", "body")
            if in_body and not first_in_body_seen:
                first_in_body_seen = True
                root_element = True
            if tag.name == "body":
                in_body = True
            self.check_attributes(tag, phone, root_element)
            if tag.name == "script":
                # A self-closing one has no element; `script` refuses it.
                _tag, begin, finish, end = scripts.get(tag.start, (tag, tag.end, tag.end, tag.end))
                self.script(tag, begin, finish, end, index)
            elif tag.name == "link":
                self.link(tag)
            elif tag.name == "base" or (tag.name == "meta" and "http-equiv" in tag.attrs):
                self.edits.append((tag.start, tag.end, ""))
            elif tag.name in FRAME_TAGS:
                self.edits.append((tag.start, tag.end, ""))
                if tag.name not in removed_frames:
                    removed_frames.add(tag.name)
                    self.warn(f"{INDEX}: <{tag.name}> was removed; an app cannot embed other content.")

        self.kit_insertion(tags, index)
        return _apply(index, self.edits)

    def scan_text_warnings(self, index: str) -> None:
        for path, text in [(INDEX, index), *((p, self.source[p]) for p in self.referenced)]:
            urls = _host_urls(text)
            if urls:
                self.warn(f"{path} contains a URL with a host ({urls[0]}); an app cannot reach the network.")
        if self.manifest.narrowest != "phone":
            return
        for text in self.scripts:
            match = _NATIVE_DIALOG.search(text)
            if match:
                self.warn(
                    f"A script calls {match.group(1)}(); native dialogs do not work in an app on a phone, "
                    "use istota.dialog instead."
                )
                break
        if any(_wide_css(text) for text in self.styles):
            self.warn(f"A stylesheet gives html or body a fixed width over {PHONE_WIDTH}px, too wide for a phone.")

    def other_files(self) -> None:
        for path in sorted(self.files):
            if path in (INDEX, MANIFEST) or path in self.referenced:
                continue
            if path.split("/")[0] in SKIPPED_FOLDERS:
                continue
            if path.endswith(".wasm"):
                self.errors.append(f"{path}: wasm arrives with apps-authoring; this version refuses .wasm files.")
                continue
            self.warn(f"{path} is not referenced by {INDEX} and was skipped.")


def _apply(source: str, edits: list[tuple[int, int, str]]) -> str:
    out = []
    position = 0
    for start, end, text in sorted(edits, key=lambda edit: (edit[0], edit[1])):
        out.append(source[position:start])
        out.append(text)
        position = max(position, end)
    out.append(source[position:])
    return "".join(out)


def build_document(files: dict[str, bytes], manifest: Manifest, kit: Kit, *, max_bytes: int) -> BuiltDocument:
    """The installed document for a draft's ``files`` (bytes by relative path). Raises :class:`DocumentError`."""
    build = _Build(files, manifest, kit)
    if MANIFEST in files:
        manifest_text = _decode(files, MANIFEST, build.errors)
        if manifest_text is not None:
            build.source[MANIFEST] = manifest_text
    if INDEX not in files:
        build.errors.append(f"The app folder has no {INDEX}.")
        build.other_files()
        raise DocumentError(build.errors)
    index = _decode(files, INDEX, build.errors)
    if index is None:
        build.other_files()
        raise DocumentError(build.errors)
    build.source[INDEX] = index

    emitted = build.index(index)
    build.other_files()
    build.scan_text_warnings(index)

    data = emitted.encode("utf-8")
    if len(data) > max_bytes:
        build.errors.append(f"The built app is {len(data)} bytes, larger than the {max_bytes}-byte limit.")
    if build.errors:
        raise DocumentError(build.errors)

    hashes = tuple(script_hash(text) for text in _inline_scripts(emitted))
    return BuiltDocument(
        html=data,
        script_hashes=hashes,
        csp=csp_for(hashes),
        content_hash=hashlib.sha256(data).hexdigest(),
        source=build.source,
        warnings=tuple(build.warnings),
    )
