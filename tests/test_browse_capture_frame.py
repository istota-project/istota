"""The coordinate frame a screenshot is delivered in, as pure arithmetic.

This is the piece of the visual-mode contract that decides whether a click
lands, and the only piece that can be checked with no container and no model.
A provider rescales an image over its own envelope server-side and says
nothing; so the skill delivers a picture already inside that envelope, and the
size it delivered is what the container converts a point against.

Two properties carry the whole thing. The delivered size must be inside
`MAX_EDGE` and `MAX_AREA_PIXELS` for every viewport a deployment can be
configured at, or the provider rescales again and the frame the model saw stops
being the frame the container knows about. And `delivered_size` must be a pure
function of the capture's own pixel dimensions, because `cmd_screenshot` calls
it in one process and `cmd_interact` calls it in another, minutes later, with
nothing carried between them.
"""

import pytest

from istota.image_attachments import MAX_AREA_PIXELS, MAX_EDGE
from istota.skills.browse import capture_image_size, delivered_size, envelope_scale


class TestTheEnvelopeScale:
    def test_a_capture_inside_the_envelope_is_the_identity(self):
        # The common case on a deployment whose screen already fits, and the
        # one where nothing in the conversion can go wrong.
        assert envelope_scale(1280, 800) == 1.0
        assert delivered_size(1280, 800) == (1280, 800)

    def test_the_shipped_container_size_is_over_the_area_cap(self):
        # SCREEN_WIDTH 1440 by SCREEN_HEIGHT 900, less Chrome's own tab strip
        # and omnibox: about 1.8% over the area cap, which is enough to put a
        # click near the bottom of the page some nine pixels high.
        assert envelope_scale(1440, 813) < 1.0
        assert delivered_size(1440, 813) == (1427, 805)

    def test_a_1920_by_1080_deployment_scales_by_a_quarter(self):
        # Both dimensions are operator environment variables, so this is the
        # size at which the arithmetic is doing real work rather than rounding.
        scale = envelope_scale(1920, 1080)
        assert 0.74 < scale < 0.75
        assert delivered_size(1920, 1080) == (1429, 804)

    def test_a_tall_capture_is_bounded_by_its_long_edge(self):
        # A full-page capture of a feed: the area cap is nowhere near, and
        # MAX_EDGE is what binds.
        assert delivered_size(800, 4000) == (313, MAX_EDGE)

    def test_it_never_enlarges(self):
        assert envelope_scale(320, 240) == 1.0
        assert delivered_size(320, 240) == (320, 240)

    @pytest.mark.parametrize("size", [(0, 800), (1280, 0), (-5, -5)])
    def test_a_nonsense_size_is_the_identity_rather_than_a_raise(self, size):
        # The record comes off a header. A malformed one has to read as "no
        # frame" upstream, not as a ZeroDivisionError on the screenshot path.
        assert envelope_scale(*size) == 1.0


class TestEveryDeliveredSizeIsInsideTheEnvelope:
    """The property the whole contract rests on, over the shapes a deployment
    can actually be configured at plus the extremes around them."""

    @pytest.mark.parametrize("width", [640, 1024, 1280, 1366, 1440, 1600, 1920, 2560, 3840])
    @pytest.mark.parametrize("height", [480, 720, 813, 900, 1080, 1440, 2160])
    def test_a_viewport_grid(self, width, height):
        out_w, out_h = delivered_size(width, height)
        assert out_w >= 1 and out_h >= 1
        assert max(out_w, out_h) <= MAX_EDGE
        assert out_w * out_h <= MAX_AREA_PIXELS

    @pytest.mark.parametrize("height", [200, 1000, 5000, 20000])
    def test_a_full_page_shape(self, height):
        out_w, out_h = delivered_size(1440, height)
        assert max(out_w, out_h) <= MAX_EDGE
        assert out_w * out_h <= MAX_AREA_PIXELS

    def test_rounding_down_is_what_keeps_the_area_cap(self):
        # 1440x813 scales to 1427.2 x 805.9. Rounding to nearest gives
        # 1427x806, which is 162 pixels over the cap -- the provider would
        # rescale again and the delivered frame would stop being the one the
        # container converts against. This is the control for that choice.
        scale = envelope_scale(1440, 813)
        rounded = (round(1440 * scale), round(813 * scale))
        assert rounded[0] * rounded[1] > MAX_AREA_PIXELS
        assert delivered_size(1440, 813)[0] * delivered_size(1440, 813)[1] <= MAX_AREA_PIXELS


class TestReadingTheCaptureRecord:
    def test_a_well_formed_record(self):
        assert capture_image_size({"image": [1439, 812]}) == (1439, 812)

    @pytest.mark.parametrize("record", [
        None,
        "not a dict",
        {},
        {"image": None},
        {"image": [1439]},
        {"image": [1439, 812, 3]},
        {"image": ["wide", "tall"]},
        {"image": [0, 812]},
        {"image": [-1, -1]},
    ])
    def test_anything_else_is_no_frame_rather_than_a_raise(self, record):
        assert capture_image_size(record) is None
