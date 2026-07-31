"""Vault-link flattening for workspace-note briefing sources (ISSUE-215).

The ``todos`` / ``reminders`` / ``notes`` sources read markdown out of the
user's workspace, in practice an Obsidian vault. Its note-links resolve only
inside the vault, so anything delivered to email or chat carrying one ships a
dead link.
"""

import pytest

from istota.briefings.sources._markdown import flatten_vault_links


class TestInlineNoteLinks:
    """``[text](target)`` with no URL scheme is flattened to its text."""

    def test_percent_encoded_note_link(self):
        # The exact line from the issue's briefing run.
        assert flatten_vault_links(
            "-- Oliver Burkeman, [Eight secrets to a fairly fulfilled life]"
            "(Eight%20secrets%20to%20a%20fairly%20fulfilled%20life.md)"
        ) == "-- Oliver Burkeman, Eight secrets to a fairly fulfilled life"

    def test_target_containing_parentheses(self):
        # `List of Temptations (Simone Weil).md` — one level of balanced parens
        # inside the target, which a naive `\\(([^)]*)\\)` would cut short.
        assert flatten_vault_links(
            "--Simone Weil, [List of Temptations]"
            "(List%20of%20Temptations%20(Simone%20Weil).md)"
        ) == "--Simone Weil, List of Temptations"

    def test_link_is_whole_line(self):
        assert flatten_vault_links("[Advice for living](Advice%20for%20living.md)") == (
            "Advice for living"
        )

    def test_relative_path_and_anchor(self):
        assert flatten_vault_links("see [notes](../vault/Note.md)") == "see notes"
        assert flatten_vault_links("see [top](#heading)") == "see top"
        assert flatten_vault_links("see [abs](/vault/Note.md)") == "see abs"

    def test_several_links_on_one_line(self):
        assert flatten_vault_links("[a](a.md) and [b](b.md)") == "a and b"

    def test_quoted_title_is_dropped_with_the_target(self):
        assert flatten_vault_links('[a](a.md "A title")') == "a"

    def test_angle_bracketed_target(self):
        assert flatten_vault_links("[a](<My Note.md>)") == "a"

    def test_image_embed_keeps_alt_text(self):
        assert flatten_vault_links("![diagram](diagram.png)") == "diagram"
        assert flatten_vault_links("![](diagram.png)") == ""


class TestRealLinksSurvive:
    """A link that resolves outside the vault is left exactly as written."""

    @pytest.mark.parametrize("target", [
        "https://example.com/story",
        "http://example.com",
        "mailto:someone@example.com",
        "tel:+15550100",
        "//example.com/protocol-relative",
        "HTTPS://EXAMPLE.COM",
    ])
    def test_deliverable_target_untouched(self, target):
        text = f"-- [The Author]({target})"
        assert flatten_vault_links(text) == text

    @pytest.mark.parametrize("target", [
        "obsidian://open?vault=Notes&file=My%20Note",
        "file:///Users/me/vault/Note.md",
    ])
    def test_vault_only_scheme_is_still_flattened(self, target):
        """A scheme is not enough — it has to resolve on the reader's device.

        Obsidian's own "Copy Obsidian URL" emits the first of these, so it is
        an ordinary way to link a note and exactly as dead in email as a bare
        relative path.
        """
        assert flatten_vault_links(f"-- [The Author]({target})") == "-- The Author"

    def test_mixed_line_keeps_only_the_real_link(self):
        assert flatten_vault_links(
            "[story](https://example.com/s) via [My Note](My%20Note.md)"
        ) == "[story](https://example.com/s) via My Note"

    def test_autolink_untouched(self):
        # Carries a bracket, so it reaches the regexes rather than tripping the
        # no-brackets early return — which is what this is meant to pin.
        text = "see [a](A.md) at <https://example.com>"
        assert flatten_vault_links(text) == "see a at <https://example.com>"

    def test_escaped_bracket_is_not_a_link(self):
        # CommonMark: `\[` is a literal bracket, so no renderer linkifies this.
        assert flatten_vault_links(r"\[not a link\](x.md)") == r"\[not a link\](x.md)"


class TestWikilinks:
    """Obsidian's ``[[Note]]`` form is the same dead reference in another shape."""

    def test_bare_wikilink(self):
        assert flatten_vault_links("see [[Eight secrets]]") == "see Eight secrets"

    def test_aliased_wikilink_keeps_the_display_text(self):
        assert flatten_vault_links("see [[Eight secrets|the essay]]") == "see the essay"

    def test_embed_wikilink(self):
        assert flatten_vault_links("![[diagram.png]]") == "diagram.png"

    def test_heading_reference(self):
        assert flatten_vault_links("[[Note#Section]]") == "Note#Section"

    def test_alias_splits_on_the_first_pipe(self):
        # Obsidian's separator is the first pipe; the rest is display text.
        assert flatten_vault_links("[[Note|a|b]]") == "a|b"

    def test_empty_alias_falls_back_to_the_target(self):
        # A trailing pipe is what the alias autocomplete leaves mid-edit.
        # Emitting nothing would delete the reference from the sentence.
        assert flatten_vault_links("see [[Note|]] here") == "see Note here"
        assert flatten_vault_links("see [[Note|  ]] here") == "see Note here"


class TestNestedLinks:
    """Invalid markdown, but a vault note can still contain it."""

    def test_inner_and_outer_are_both_flattened(self):
        assert flatten_vault_links("[a [b](b.md) c](c.md)") == "a b c"

    def test_external_outer_link_survives_an_inner_flatten(self):
        assert flatten_vault_links(
            "[a [b](b.md) c](https://example.com)"
        ) == "[a b c](https://example.com)"


class TestCodeIsNotRewritten:
    """A markdown renderer does not linkify inside code, and neither do we."""

    def test_inline_code_span(self):
        # `handlers[key](args)` is ordinary code and matches the link shape.
        text = "call `handlers[key](args)` to dispatch"
        assert flatten_vault_links(text) == text

    def test_fenced_block(self):
        text = (
            "before [a](a.md)\n"
            "```python\n"
            "handlers[key](args)\n"
            "```\n"
            "after [b](b.md)\n"
        )
        assert flatten_vault_links(text) == (
            "before a\n"
            "```python\n"
            "handlers[key](args)\n"
            "```\n"
            "after b\n"
        )

    def test_tilde_fence(self):
        text = "~~~\nhandlers[key](args)\n~~~"
        assert flatten_vault_links(text) == text

    def test_unclosed_fence_protects_the_rest(self):
        text = "```\nhandlers[key](args)\n"
        assert flatten_vault_links(text) == text

    def test_wikilink_inside_inline_code(self):
        text = "use `[[Note]]` here"
        assert flatten_vault_links(text) == text

    def test_wikilink_inside_a_fence(self):
        text = "```\n[[Note]]\n```"
        assert flatten_vault_links(text) == text

    def test_indented_fence(self):
        # A fenced block nested in a list item runs past CommonMark's 3-space
        # limit; treating it as prose would corrupt the code inside it.
        text = "- item\n    ```\n    handlers[key](args)\n    ```\n"
        assert flatten_vault_links(text) == text

    def test_a_different_fence_character_does_not_close_the_block(self):
        text = "```\n~~~\nhandlers[key](args)\n~~~\n```\n"
        assert flatten_vault_links(text) == text

    def test_a_shorter_run_does_not_close_a_longer_fence(self):
        text = "````\n```\nhandlers[key](args)\n```\n````\n"
        assert flatten_vault_links(text) == text

    def test_a_longer_run_does_close_a_shorter_fence(self):
        assert flatten_vault_links("```\ncode\n`````\n[a](a.md)\n") == (
            "```\ncode\n`````\na\n"
        )


class TestShapePreservation:
    def test_blank_and_empty_input(self):
        assert flatten_vault_links("") == ""
        assert flatten_vault_links("   ") == "   "

    def test_line_structure_and_trailing_newline_preserved(self):
        assert flatten_vault_links("a\n\nb\n") == "a\n\nb\n"

    def test_text_with_no_links_is_returned_unchanged(self):
        text = "Know when to move on.\n\n-- Oliver Burkeman\n"
        assert flatten_vault_links(text) == text

    def test_bare_brackets_left_alone(self):
        assert flatten_vault_links("a [draft] item") == "a [draft] item"
        assert flatten_vault_links("- [ ] todo") == "- [ ] todo"
