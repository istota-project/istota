"""`istota.apps.document`: a draft's files built into one self-contained document."""

from __future__ import annotations

import base64
import hashlib
import re

import pytest

from istota.apps.document import BuiltDocument, DocumentError, build_document, csp_for
from istota.apps.kit import Kit
from istota.apps.manifest import parse_manifest

KIT = Kit(
    guard_js="/*guard*/delete window.RTCPeerConnection;",
    client_js="/*client*/globalThis.istota = {};",
    components_js="/*components*/customElements.define('istota-x', class extends HTMLElement {});",
    css="/*kit css*/:root { --space-1: 0.25rem; }",
)

MANIFEST_TOML = 'id = "board"\nname = "Board"\nicon = "layout"\n'

INDEX = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Board</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <main id="board">A &amp B &copy</main>
  <script src="./js/app.js"></script>
  <script>console.log(1 < 2);</script>
</body>
</html>
"""

APP_JS = "document.getElementById('board').textContent = 'hi';\n"
STYLE_CSS = "main { color: var(--text); }\n"

MAX = 2_000_000


def _manifest(narrowest="phone"):
    text = MANIFEST_TOML
    if narrowest != "phone":
        text += f'[display]\nnarrowest = "{narrowest}"\nreason = "Needs the width."\n'
    return parse_manifest(text, folder_name="board", bot_dir="istota")


def _files(index=INDEX, **extra):
    files = {
        "manifest.toml": MANIFEST_TOML.encode(),
        "index.html": index.encode(),
        "js/app.js": APP_JS.encode(),
        "style.css": STYLE_CSS.encode(),
    }
    files.update({path: data for path, data in extra.items()})
    return files


def _build(index=INDEX, files=None, narrowest="phone", max_bytes=MAX):
    return build_document(
        files if files is not None else _files(index), _manifest(narrowest), KIT, max_bytes=max_bytes,
    )


def _errors(**kwargs):
    with pytest.raises(DocumentError) as caught:
        _build(**kwargs)
    return caught.value.errors


_SCRIPT = re.compile(r"<script\b[^>]*>(.*?)</script\s*>", re.S | re.I)


KIT_TEXT = (
    f"<script>{KIT.guard_js}</script>"
    f"<script>{KIT.client_js}</script>"
    f"<script>{KIT.components_js}</script>"
    f"<style>{KIT.css}</style>"
)
# What may precede the guard: nothing that opens an element.
_OPENING_PREFIX = re.compile(r"(?:\s|<!--.*?-->)*(?:<!doctype[^>]*>)?", re.S | re.I)


def _inline_scripts(html: str) -> list[str]:
    return _SCRIPT.findall(html)


class TestInlining:
    def test_a_script_and_a_stylesheet_are_inlined(self):
        built = _build()
        html = built.html.decode()
        assert isinstance(built, BuiltDocument)
        assert f"<script>{APP_JS}</script>" in html
        assert f"<style>{STYLE_CSS}</style>" in html
        assert "src=" not in html
        assert 'rel="stylesheet"' not in html

    def test_the_rest_of_the_document_is_kept_as_written(self):
        html = _build().html.decode()
        assert "<main id=\"board\">A &amp B &copy</main>" in html
        assert "<script>console.log(1 < 2);</script>" in html
        assert html.startswith(f"<!doctype html>{KIT_TEXT}\n<html lang=\"en\">\n<head>")

    def test_the_type_is_kept(self):
        index = INDEX.replace('<script src="./js/app.js">', '<script type="module" src="js/app.js">')
        assert f'<script type="module">{APP_JS}</script>' in _build(index).html.decode()

    def test_the_kit_follows_the_doctype_in_order(self):
        html = _build().html.decode()
        start = len("<!doctype html>")
        assert html[start:start + len(KIT_TEXT)] == KIT_TEXT

    def test_the_kit_opens_a_document_with_no_doctype(self):
        index = "<html><body><p>x</p></body></html>\n"
        assert _build(index).html.decode() == KIT_TEXT + index

    def test_a_document_with_no_head_gets_none_added(self):
        index = "<!doctype html>\n<html><body><p>x</p></body></html>\n"
        assert _build(index).html.decode() == "<!doctype html>" + KIT_TEXT + index[len("<!doctype html>"):]

    def test_a_comment_before_the_doctype_keeps_the_doctype_first(self):
        index = "<!-- built by hand -->\n<!DOCTYPE html>\n<p>x</p>\n"
        html = _build(index).html.decode()
        assert html.startswith("<!-- built by hand -->\n<!DOCTYPE html>" + KIT_TEXT)

    @pytest.mark.parametrize("index", [
        '<!doctype html><script src="js/app.js"></script><head></head><body></body>',
        '<!doctype html><html><body><template><head></head></template>'
        '<script src="js/app.js"></script></body></html>',
        '<p>x</p><!doctype html><script src="js/app.js"></script>',
        '<template><!doctype html></template><script src="js/app.js"></script>',
    ])
    def test_the_guard_runs_first_and_inside_no_element(self, index):
        html = _build(index).html.decode()
        first = _SCRIPT.search(html)
        assert first.group(1) == KIT.guard_js
        assert _OPENING_PREFIX.fullmatch(html[:first.start()]), html[:first.start()]

    def test_the_source_is_the_files_as_read(self):
        built = _build()
        assert built.source == {
            "manifest.toml": MANIFEST_TOML,
            "index.html": INDEX,
            "js/app.js": APP_JS,
            "style.css": STYLE_CSS,
        }

    def test_a_missing_referenced_file(self):
        errors = _errors(index=INDEX.replace("style.css", "missing.css"))
        assert any("missing.css" in error for error in errors)

    def test_a_missing_index(self):
        files = _files()
        del files["index.html"]
        assert any("index.html" in error for error in _errors(files=files))

    @pytest.mark.parametrize("src", [
        "https://example.com/x.js", "//example.com/x.js", "/x.js", "../other/x.js",
        "js/../../x.js", "data:text/javascript,alert(1)", "app.js?v=1", "app.mjs",
    ])
    def test_a_script_src_that_is_not_a_plain_relative_js_file(self, src):
        index = INDEX.replace('src="./js/app.js"', f'src="{src}"')
        assert any(src in error for error in _errors(index=index))

    def test_a_stylesheet_href_with_a_host(self):
        index = INDEX.replace('href="style.css"', 'href="https://cdn.example.com/x.css"')
        assert _errors(index=index)

    @pytest.mark.parametrize("tag", [
        '<img src="https://example.com/p.png">', '<a href="https://example.com/">x</a>',
        '<a href="//example.com/">x</a>', '<form action="https://example.com/"></form>',
    ])
    def test_a_host_url_on_any_element_is_refused(self, tag):
        assert _errors(index=INDEX.replace("<main", tag + "<main"))

    def test_a_data_url_and_a_fragment_are_fine(self):
        index = INDEX.replace("<main", '<img src="data:image/png;base64,AAAA"><a href="#top">t</a><main')
        _build(index)

    def test_a_script_containing_a_closing_tag_is_refused(self):
        files = _files(**{"js/app.js": b"const s = '</SCRIPT>';\n"})
        errors = _errors(files=files)
        assert any("js/app.js" in error for error in errors)

    def test_a_stylesheet_containing_a_closing_tag_is_refused(self):
        files = _files(**{"style.css": b"/* </style> */\n"})
        assert any("style.css" in error for error in _errors(files=files))

    def test_a_self_closing_script_is_refused(self):
        index = INDEX.replace('<script src="./js/app.js"></script>', '<script src="js/app.js" />')
        assert _errors(index=index)

    def test_wasm_is_refused(self):
        files = _files(**{"lib/ephemeris.wasm": b"\0asm"})
        errors = _errors(files=files)
        assert any("wasm" in error for error in errors)

    def test_an_unreferenced_file_is_skipped_with_a_warning(self):
        built = _build(files=_files(**{"notes.txt": b"x", "data/state.json": b"{}", "preview/a.png": b"x"}))
        assert "notes.txt" not in built.source
        assert [w for w in built.warnings if "notes.txt" in w]
        assert not [w for w in built.warnings if "data/" in w or "preview/" in w]

    def test_a_non_utf8_file(self):
        files = _files(**{"js/app.js": b"\xff\xfe"})
        assert any("js/app.js" in error for error in _errors(files=files))

    def test_the_size_cap(self):
        errors = _errors(max_bytes=200)
        assert any("200" in error or "large" in error for error in errors)

    def test_errors_are_collected(self):
        index = INDEX.replace("style.css", "missing.css").replace("./js/app.js", "https://x.example/a.js")
        assert len(_errors(index=index)) >= 2


class TestRemoved:
    @pytest.mark.parametrize("tag", [
        '<link rel="dns-prefetch" href="//example.com">',
        '<link rel="preconnect" href="https://example.com">',
        '<link rel="prefetch" href="x.js">',
        '<link rel="preload" href="x.js" as="script">',
        '<link rel="prerender" href="x.html">',
        '<link rel="modulepreload" href="x.js">',
        '<meta http-equiv="refresh" content="0; url=https://example.com/">',
        '<meta http-equiv="Content-Security-Policy" content="default-src *">',
        '<base href="https://example.com/">',
    ])
    def test_hints_meta_http_equiv_and_base(self, tag):
        index = INDEX.replace("<title>", tag + "<title>")
        html = _build(index).html.decode()
        assert tag not in html
        assert "<title>Board</title>" in html

    @pytest.mark.parametrize("tag, closing", [
        ('<iframe src="x.html">', "</iframe>"),
        ('<object data="x.swf">', "</object>"),
        ('<embed src="x.swf">', ""),
    ])
    def test_frames_objects_and_embeds_are_removed_with_a_warning(self, tag, closing):
        index = INDEX.replace("<main", f"{tag}fallback{closing}<main")
        built = _build(index)
        html = built.html.decode()
        name = tag[1:tag.index(" ")]
        assert f"<{name}" not in html
        assert "fallback" in html
        assert [w for w in built.warnings if name in w]


class TestHashesAndCsp:
    def test_hashes_match_a_recomputation_over_the_emitted_scripts(self):
        built = _build()
        scripts = _inline_scripts(built.html.decode())
        assert len(scripts) == 5
        expected = tuple(
            "sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()
            for text in scripts
        )
        assert built.script_hashes == expected

    def test_the_csp_is_the_template(self):
        built = _build()
        sources = " ".join(f"'{h}'" for h in built.script_hashes)
        assert built.csp == (
            f"default-src 'none'; script-src {sources}; style-src 'unsafe-inline'; "
            "img-src data: blob:; font-src data:; media-src data: blob:; connect-src 'none'; "
            "frame-src 'none'; worker-src 'none'; form-action 'none'; base-uri 'none'; "
            "frame-ancestors 'self'; sandbox allow-scripts"
        )
        assert csp_for(built.script_hashes) == built.csp

    def test_no_hashes_is_script_src_none(self):
        assert "script-src 'none';" in csp_for(())

    def test_the_content_hash_is_of_the_emitted_bytes(self):
        built = _build()
        assert built.content_hash == hashlib.sha256(built.html).hexdigest()

    def test_the_build_is_deterministic(self):
        assert _build().content_hash == _build().content_hash


class TestWarnings:
    def _warnings(self, index=INDEX, narrowest="phone", **extra):
        return _build(files=_files(index, **extra), narrowest=narrowest).warnings

    def test_a_clean_app_has_no_warnings(self):
        assert self._warnings() == ()

    @pytest.mark.parametrize("js", ["alert('x');", "if (confirm('ok?')) {}", "window.prompt('name')"])
    def test_a_native_dialog_in_a_phone_app(self, js):
        warnings = self._warnings(**{"js/app.js": js.encode()})
        assert [w for w in warnings if "dialog" in w]

    def test_the_kits_own_dialog_is_not_a_native_one(self):
        assert self._warnings(**{"js/app.js": b"istota.dialog.alert('x');"}) == ()

    def test_a_native_dialog_in_a_desktop_app_is_not_warned(self):
        warnings = self._warnings(narrowest="desktop", **{"js/app.js": b"alert('x');"})
        assert not [w for w in warnings if "dialog" in w]

    def test_an_inline_event_handler(self):
        warnings = self._warnings(INDEX.replace('<main id="board">', '<main id="board" onclick="go()">'))
        assert [w for w in warnings if "onclick" in w]

    @pytest.mark.parametrize("change", [
        {"style.css": b"body { width: 1024px; }"},
        {"style.css": b"html,body{margin:0}\nhtml { min-width: 400px }"},
        {"index": INDEX.replace('<main id="board">', '<main id="board" style="width: 800px">')},
    ])
    def test_a_wide_root_in_a_phone_app(self, change):
        index = change.pop("index", INDEX)
        warnings = self._warnings(index, **change)
        assert [w for w in warnings if "320" in w]

    def test_a_narrow_root_is_fine(self):
        assert self._warnings(**{"style.css": b"body { width: 300px; max-width: 2000px; }"}) == ()

    def test_a_url_with_a_host_in_text_is_warned(self):
        warnings = self._warnings(**{"js/app.js": b"const u = 'https://example.com/collect';"})
        assert [w for w in warnings if "example.com" in w]

    def test_the_svg_namespace_is_not_a_url_to_warn_on(self):
        js = b"document.createElementNS('http://www.w3.org/2000/svg', 'svg');"
        assert self._warnings(**{"js/app.js": js}) == ()

    def test_defer_on_an_inlined_script_is_warned(self):
        index = INDEX.replace('<script src="./js/app.js">', '<script src="./js/app.js" defer>')
        assert [w for w in self._warnings(index) if "defer" in w]
