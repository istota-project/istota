"""The shared HTML mail template."""

from istota.mail.html_template import STYLES, open_tag, wrap_document


def test_document_has_the_head_a_mail_client_needs():
    out = wrap_document("<p>x</p>")
    assert out.startswith("<!DOCTYPE html>\n<html><head>")
    assert '<meta charset="utf-8">' in out
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in out
    assert '<meta name="color-scheme" content="light dark">' in out
    # No wrapper: the body runs the client's full width.
    assert out.endswith("</head><body>\n<p>x</p>\n</body></html>")
    assert "<style" not in out


def test_the_look_stays_unstyled():
    """Spacing and layout only: the client keeps its own fonts and colours."""
    for style in STYLES.values():
        assert "font-family" not in style
        assert "color:" not in style and "background" not in style
        assert "width" not in style


def test_lists_have_no_left_indent():
    assert "padding-left:0" in STYLES["ul"]
    assert "list-style-position:inside" in STYLES["ul"]
    assert "margin:0 0 0.75em 0" in STYLES["ul"]


def test_open_tag_appends_an_extra_style():
    assert open_tag("p") == f'<p style="{STYLES["p"]}">'
    assert open_tag("p", "font-size:2em") == f'<p style="{STYLES["p"]};font-size:2em">'
    assert open_tag("hr") == f'<hr style="{STYLES["hr"]}">'
