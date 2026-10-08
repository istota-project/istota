"""Shapes describe captured secrets without exposing their characters."""
import pytest
from istota.lib import secret_shapes as shapes, totp

@pytest.mark.parametrize("text, expected", [
    ("Your scratch token is a1b2c3d4e5", ["a1b2c3d4e5"]),
    ("1. abcd-1234 2. efgh-5678", ["abcd-1234", "efgh-5678"]),
    ("\n".join(f"1234 {i:04d}" for i in range(10)), [f"1234{i:04d}" for i in range(10)]),
    ("\n".join(f"abcd-{i:04d}  efgh-{i:04d}" for i in range(8)),
     [code for i in range(8) for code in (f"abcd-{i:04d}", f"efgh-{i:04d}")]),
])
def test_codes(text, expected):
    codes, shape = shapes.parse_codes(text)
    assert codes == expected
    assert shape.count == len(expected)
    assert shape.charset in {"[a-z0-9]", "[a-z0-9-]", "[A-Z0-9]", "[A-Za-z0-9-]"}

@pytest.mark.parametrize("count", [12, 24])
def test_phrase(count):
    text, shape = shapes.parse_phrase("APPLE, " * count)
    assert text == " ".join(["apple"] * count)
    assert shape.length == count and shape.count == 1 and shape.charset == "words"

@pytest.mark.parametrize("text,reason", [("abcd1234\na123456789012", "codes_inconsistent"), ("ordinary sentence", "codes_not_found")])
def test_bad_codes(text, reason):
    with pytest.raises(shapes.ShapeError, match=reason):
        shapes.parse_codes(text)

def test_bad_phrase():
    with pytest.raises(shapes.ShapeError, match="phrase_word_count"):
        shapes.parse_phrase("apple " * 13)

@pytest.mark.parametrize("text", ["Your secret key is JBSW Y3DP EHPK 3PXP", "otpauth://totp/test?secret=JBSWY3DPEHPK3PXP"])
def test_otp(text):
    uri, shape = shapes.parse_otp(text, totp=totp)
    assert totp.parse_otpauth(uri) == totp.parse_user_input("JBSWY3DPEHPK3PXP")
    assert (shape.count, shape.length, shape.charset) == (1, 10, "base32")

def test_ambiguous_otp():
    with pytest.raises(shapes.ShapeError, match="otp_ambiguous"):
        shapes.parse_otp("JBSWY3DPEHPK3PXP\nJBSWY3DPEHPK3PXP", totp=totp)
