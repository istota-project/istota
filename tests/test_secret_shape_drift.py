"""Keep the container detection copy in step with the host leaf."""
import pytest
from istota.lib import secret_shapes
from tests.test_browser_navigation import browse_api

FIXTURES = [
    "Your scratch token is a1b2c3d4e5",
    "1. abcd-1234 2. efgh-5678",
    "\n".join(f"1234 {i:04d}" for i in range(10)),
    "\n".join(f"abcd-{i:04d}  efgh-{i:04d}" for i in range(8)),
    "apple berry cherry lemon mango peach " * 2,
    "apple berry cherry lemon mango peach " * 4,
    "apple " * 13,
    "otpauth://totp/test?secret=JBSWY3DPEHPK3PXP",
    "Your secret key is JBSW Y3DP EHPK 3PXP",
    "JBSWY3DPEHPK3PXP\nJBSWY3DPEHPK3PXP",
    "abcd1234\na123456789012",
    "Order #12; ordinary sentence.",
]

@pytest.mark.parametrize("text", FIXTURES)
def test_detection_copy(text):
    assert browse_api.detect_secret_shapes(text) == secret_shapes.detect_secret_shapes(text)
    assert browse_api.mask_secret_text(text) == secret_shapes.mask_secret_text(text)

@pytest.mark.parametrize("text", FIXTURES[:10])
def test_mask_removes_value(text):
    masked = browse_api.mask_secret_text(text)
    assert "[secret]" in masked
    for token in ("a1b2c3d4e5", "abcd-1234", "1234", "JBSW", "apple berry"):
        if token in text:
            assert token not in masked

def test_ordinary_text_is_unchanged():
    text = FIXTURES[-1]
    assert browse_api.mask_secret_text(text) == text
    assert browse_api.detect_secret_shapes(text) == []
