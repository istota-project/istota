"""The HTML document every message Istota composes itself is sent in.

The look is deliberately unstyled: no fonts, colours, branding, images or
layout tables, so each mail client's own typography renders the message. What
it does fix is the rough edges of browser-default HTML: list indent, heading
size, uneven block spacing, the bevelled rule, a line length past reading
width, and a long URL that makes a phone scroll sideways.

Every style is inline, on the element. Gmail and others drop or ignore a
`<style>` block in many contexts, so there is none. Body styles go on a wrapper
`<div>` rather than `<body>`, which several clients replace with their own.

Model-written HTML (an `html` email output, a held draft) is not wrapped: it is
the model's whole document, not a fragment of ours.
"""

from __future__ import annotations

# A neutral grey reads in both light and dark themes; `rgba` and `opacity` are
# not honoured everywhere, and a rule that disappears is worse than a grey one.
_RULE_COLOR = "#999999"

STYLES: dict[str, str] = {
    "p": "margin:0 0 0.75em 0",
    "h2": "font-size:1.15em;line-height:1.3;margin:1.5em 0 0.5em 0",
    # Inside markers keep the bullet visible once the padding that holds an
    # outside marker is gone, so bullets line up with the paragraphs.
    "ul": "margin:0 0 0.75em 0;padding-left:0;list-style-position:inside",
    "li": "margin:0 0 0.25em 0",
    "hr": f"border:0;border-top:1px solid {_RULE_COLOR};height:0;margin:1.25em 0",
}

CONTAINER_STYLE = (
    "max-width:40em;margin:0;padding:8px 16px;line-height:1.5;"
    "overflow-wrap:anywhere;word-wrap:break-word"
)


def open_tag(name: str, extra_style: str = "") -> str:
    """The opening tag for ``name`` with the template's style, plus ``extra_style``."""
    style = ";".join(s for s in (STYLES[name], extra_style) if s)
    if name == "hr":
        return f'<hr style="{style}">'
    return f'<{name} style="{style}">'


def wrap_document(body_html: str) -> str:
    """Wrap a body fragment in the full document. ``body_html`` must already be escaped."""
    return (
        "<!DOCTYPE html>\n"
        "<html><head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="color-scheme" content="light dark">\n'
        '<meta name="supported-color-schemes" content="light dark">\n'
        "</head><body>\n"
        f'<div style="{CONTAINER_STYLE}">\n'
        f"{body_html}\n"
        "</div>\n"
        "</body></html>"
    )
