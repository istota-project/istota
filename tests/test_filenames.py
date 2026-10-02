"""The one rule for turning a name somebody else chose into a filename (ISSUE-593)."""

import pytest

from istota.filenames import filename_parts, safe_filename


class TestTheReportedName:
    def test_a_carriage_return_does_not_survive(self):
        """The production case: a forwarded booking whose MIME filename carried
        a CR, which Nextcloud refused and rclone retried indefinitely."""
        assert safe_filename("Your Booking Confirmation\r Receipt.pdf") == (
            "Your Booking Confirmation Receipt.pdf"
        )

    def test_a_folded_header_line_break_collapses_to_one_space(self):
        assert safe_filename("Booking-Ref\r\n\tReceipt.pdf") == "Booking-Ref Receipt.pdf"


class TestReadable:
    @pytest.mark.parametrize("raw,expected", [
        ("invoice.pdf", "invoice.pdf"),
        ("Résumé 2026.pdf", "Résumé 2026.pdf"),
        ("../../etc/passwd", "passwd"),
        (r"C:\Users\bob\scan.pdf", "scan.pdf"),
        ("Re: invoice?.pdf", "Re_ invoice_.pdf"),
        ('a<b>c|d*e"f.txt', "a_b_c_d_e_f.txt"),
        (".hidden.pdf", "hidden.pdf"),
        ("trailing dots... .pdf", "trailing dots.pdf"),
        ("name.", "name"),
        ("  spaced  out  .txt", "spaced out.txt"),
        ("in\x00voice.pdf", "in voice.pdf"),
        ("bidi\u202egnp.exe", "bidi gnp.exe"),
        ("line\u2028sep.txt", "line sep.txt"),
        ("next\u0085line.txt", "next line.txt"),
        ("archive.tar.gz", "archive.tar.gz"),
        ("notes.final draft", "notes.final draft"),
        ("", "file"),
        ("...", "file"),
        ("\r\n", "file"),
    ])
    def test_table(self, raw, expected):
        assert safe_filename(raw) == expected

    def test_decomposed_unicode_is_composed(self):
        assert safe_filename("Re\u0301sume\u0301.pdf") == "Résumé.pdf"

    def test_the_extension_survives_a_long_stem(self):
        out = safe_filename("a" * 400 + ".pdf")
        assert out.endswith(".pdf")
        assert len(out) == 120 + len(".pdf")

    def test_the_name_fits_a_filesystem_in_bytes(self):
        out = safe_filename("é" * 200 + ".pdf")
        assert out.endswith(".pdf")
        assert len(out.encode("utf-8")) <= 255

    def test_a_non_string_is_coerced(self):
        assert safe_filename(None) == "file"
        assert safe_filename(1234) == "1234"


class TestReviewFindings:
    @pytest.mark.parametrize("raw,expected", [
        ("upload.part", "upload_part"),
        ("upload.FILEPART", "upload_FILEPART"),
        (".png", "file.png"),
        ("\ud800a.pdf", "a.pdf"),
    ])
    def test_table(self, raw, expected):
        assert safe_filename(raw) == expected

    def test_the_source_holds_no_invisible_character(self):
        import istota.filenames as module
        from pathlib import Path

        assert Path(module.__file__).read_text(encoding="utf-8").isascii()


class TestAsciiOnly:
    @pytest.mark.parametrize("raw,expected", [
        ("discharge-summary.pdf", "discharge-summary.pdf"),
        ("my scan (2).pdf", "my_scan_2.pdf"),
        ("Résumé.pdf", "R_sum.pdf"),
        ("Booking-Ref\r Receipt.pdf", "Booking-Ref_Receipt.pdf"),
        ("???.pdf", "document.pdf"),
        ("", "document.bin"),
        ("...", "document.bin"),
        ("noextension", "noextension"),
    ])
    def test_table(self, raw, expected):
        assert safe_filename(raw, ascii_only=True, fallback="document.bin") == expected


class TestFallback:
    def test_an_empty_stem_keeps_the_real_extension(self):
        assert safe_filename("???.pdf", ascii_only=True, fallback="document.bin") == (
            "document.pdf"
        )

    def test_a_bare_fallback_is_used_whole(self):
        assert safe_filename("", fallback="upload") == "upload"


class TestParts:
    def test_parts_split_the_extension_with_its_dot(self):
        assert filename_parts("Report.PDF") == ("Report", ".PDF")

    def test_an_empty_stem_is_reported_empty(self):
        assert filename_parts("..", ascii_only=True) == ("", "")

    def test_an_invalid_extension_is_part_of_the_stem(self):
        assert filename_parts("a.p\rdf") == ("a.p df", "")


@pytest.mark.parametrize("ascii_only", [False, True])
@pytest.mark.parametrize("raw", [
    "Your Booking Confirmation\r Receipt.pdf",
    "../x/..//y\\z.tar.gz",
    "   .  .hidden . pdf",
    "a" * 300 + ".verylongextension",
    "é" * 200 + ".pdf",
    "x" * 119 + " .pdf",
    "a.b c",
    'q?"<>.txt',
])
def test_sanitising_twice_changes_nothing(raw, ascii_only):
    """Callers layer: the inbound poll sanitises a name and the inbox writer
    sanitises it again, so the rule has to be idempotent."""
    once = safe_filename(raw, ascii_only=ascii_only)
    assert safe_filename(once, ascii_only=ascii_only) == once


@pytest.mark.parametrize("raw", ["..", ".", "../..", "/", "\\", "a/../..", "./"])
def test_nothing_path_shaped_comes_back(raw):
    for ascii_only in (False, True):
        out = safe_filename(raw, ascii_only=ascii_only)
        assert "/" not in out and "\\" not in out
        assert out not in ("", ".", "..")
