"""Reading HTML mail built on `istota.mail.html_template` in tests."""

import re

from istota.mail.html_template import CONTAINER_STYLE, STYLES

TEMPLATE_STYLES = frozenset(STYLES.values()) | {CONTAINER_STYLE}


def bare(html: str) -> str:
    """``html`` with the template's own style attributes removed, and nothing else.

    Only the exact template strings are removed, so a ``style=`` that came from
    anywhere else is still there for an assertion to see.
    """
    for style in sorted(TEMPLATE_STYLES, key=len, reverse=True):
        html = html.replace(f' style="{style}"', "")
    return html


def style_values(html: str) -> list[str]:
    """Every ``style`` attribute value in ``html``, in order."""
    return re.findall(r'style="([^"]*)"', html)
