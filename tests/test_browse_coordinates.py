"""The browser container's half of visual mode: the frame, and what refuses it.

`docker/browser/visual.py` is where a point read off a picture becomes an X11
screen position. Three things in it are decisions rather than arithmetic and
each is held here.

The capture's size is **parsed out of the PNG's own IHDR**, never derived as
`innerWidth * dpr`. A classic scrollbar is counted by `innerWidth` on some
platforms and not others, and a CDP capture need not agree with either, so the
two would compute scales that differ by about 1% on any page with a scrollbar
-- invisibly, and constantly.

The X11 inset is **measured**, never read off the page. Under Xvfb with no
window manager Chrome reports `window.screenX` and `screenY` as `0,0` whatever
the truth is, so a design that trusted them would have clicked 87 pixels high
on every page with every unit test passing around the wrong constant.

And a capture that no longer describes the page is **refused**, not clicked.
The whole failure mode this exists to prevent is a click landing somewhere the
caller did not intend, and a warning after the fact is the "success
indistinguishable from a no-op" shape: `ok: true`, and something else pressed.

The browser app runs only inside its own Docker image, so the standalone
modules are imported from `docker/browser/` directly -- the pattern
`test_browser_chrome_watchdog.py` and `test_browser_memory_eviction.py` already
use. `visual.py` imports `xdotool` alone, which is stdlib, so this half needs
no stubs.
"""

import struct
import sys
from io import BytesIO
from pathlib import Path
from unittest import mock

import pytest

_BROWSER_DIR = Path(__file__).resolve().parent.parent / "docker" / "browser"
if str(_BROWSER_DIR) not in sys.path:
    sys.path.insert(0, str(_BROWSER_DIR))

import visual  # noqa: E402


def _png(width, height):
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (width, height), (1, 2, 3)).save(buffer, format="PNG")
    return buffer.getvalue()


#: The shipped container: Xvfb at 1440x900, Chrome filling it, the page
#: viewport inside its own tab strip and omnibox.
WINDOW = {"x": 0, "y": 0, "width": 1439, "height": 899}
CAPTURE_W, CAPTURE_H = 1439, 812
UI_INSET_Y = WINDOW["height"] - CAPTURE_H  # 87, measured


@pytest.fixture
def window():
    with mock.patch.object(visual.xdotool, "window_geometry", return_value=dict(WINDOW)):
        yield WINDOW


class _Page:
    """A page that answers the one evaluate `visual.page_state` makes."""

    def __init__(self, viewport=(1439, 812), dpr=1, scroll=(0, 0), url="https://a.example/"):
        self.state = [viewport[0], viewport[1], dpr, scroll[0], scroll[1], url]
        self.raises = False

    def evaluate(self, _script):
        if self.raises:
            raise RuntimeError("Execution context was destroyed")
        return list(self.state)


class TestThePngHeaderParse:
    """Read, never derived -- and the parse refuses rather than unpacking
    garbage, since a caller that gets a size back builds a frame on it."""

    @pytest.mark.parametrize("size", [(1, 1), (320, 240), (1439, 812), (4000, 3)])
    def test_it_reads_a_real_png(self, size):
        assert visual.png_size(_png(*size)) == size

    def test_a_truncated_header_refuses(self):
        whole = _png(320, 240)
        for cut in (0, 8, 16, 23):
            assert visual.png_size(whole[:cut]) is None

    def test_a_non_png_refuses(self):
        assert visual.png_size(b"\xff\xd8\xff\xe0" + b"\x00" * 40) is None
        assert visual.png_size(b"") is None

    def test_a_zero_dimension_refuses(self):
        # A header this malformed is not a picture, and a frame built on a
        # zero width divides by it.
        forged = bytearray(_png(320, 240))
        forged[16:24] = struct.pack(">II", 0, 240)
        assert visual.png_size(bytes(forged)) is None


class TestBuildingTheCaptureRecord:
    def test_the_inset_is_the_difference_between_the_window_and_the_capture(
        self, window,
    ):
        record, why = visual.build_capture(_png(CAPTURE_W, CAPTURE_H), page=_Page())

        assert why is None
        assert record["image"] == [CAPTURE_W, CAPTURE_H]
        assert record["offset"] == [0, UI_INSET_Y]
        assert record["window"] == WINDOW
        assert record["page"]["url"] == "https://a.example/"

    def test_the_size_is_read_from_the_png_never_derived_from_the_viewport(
        self, window,
    ):
        # The scrollbar shape, and the reason the IHDR parse exists at all: a
        # classic scrollbar is counted by `innerWidth` on some platforms and
        # not others, and a CDP capture need not agree with either. Deriving
        # `viewport * dpr` would compute a frame about 1% wrong on any page
        # with a scrollbar -- constantly, and with nothing to notice it by.
        page = _Page(viewport=(1439, 812))
        record, why = visual.build_capture(_png(1424, 812), page=page)

        assert why is None
        assert record["image"] == [1424, 812]
        assert record["image"][0] != record["page"]["viewport"][0]
        # The conversion's horizontal extent is the capture's, not the
        # viewport's: a point at the right edge of the picture is 1424 pixels
        # from the picture's own left edge, whatever `innerWidth` says.
        left = visual.image_to_screen(record, 0, 0)[0]
        right = visual.image_to_screen(record, 1424, 0)[0]
        assert right - left == 1424
        # Recorded rather than asserted as correct: where the window and the
        # capture differ, `build_capture` centres the capture inside the
        # window. On the shipped container the two are equal and the inset is
        # zero, so nothing in production depends on this; a real scrollbar
        # would put the capture at the left edge rather than centred. The
        # container is vendored from stealth-browser, which is where a change
        # to that rule belongs.
        assert record["offset"][0] == (WINDOW["width"] - 1424) // 2

    def test_a_full_page_capture_gets_no_offset_at_all(self, window):
        record, why = visual.build_capture(
            _png(CAPTURE_W, 6000), page=_Page(), full_page=True,
        )

        assert why is None
        assert record["full_page"] is True
        # Deliberately no coordinate frame: a full-page image is a different
        # space from the one the pointer acts in, and nothing should convert
        # against it.
        assert record["offset"] is None

    def test_an_implausible_inset_is_refused_with_both_measurements(self, window):
        # A capture taller than the window it came from means the window that
        # was measured is not the one that was captured.
        record, why = visual.build_capture(_png(CAPTURE_W, 2000), page=_Page())

        assert record is None
        assert "implausible UI inset" in why
        assert "2000" in why

    def test_bytes_that_are_not_a_png_are_refused(self, window):
        record, why = visual.build_capture(b"not a picture", page=_Page())
        assert record is None
        assert "not a PNG" in why

    def test_no_chrome_window_is_refused(self):
        with mock.patch.object(visual.xdotool, "window_geometry", return_value=None):
            record, why = visual.build_capture(_png(320, 240), page=_Page())
        assert record is None
        assert "Chrome window not found" in why

    def test_measure_false_sends_nothing_to_the_page(self, window):
        page = _Page()
        page.raises = True  # would blow up if the evaluate ran

        record, why = visual.build_capture(
            _png(CAPTURE_W, CAPTURE_H), page=page, measure=False,
        )

        assert why is None
        assert record["page"] is None
        # The X11 half is intact, so the picture is still clickable.
        assert record["offset"] == [0, UI_INSET_Y]


def _record(window_geometry=None, **overrides):
    record = {
        "image": [CAPTURE_W, CAPTURE_H],
        "window": dict(window_geometry or WINDOW),
        "offset": [0, UI_INSET_Y],
        "page": {
            "viewport": [CAPTURE_W, CAPTURE_H],
            "dpr": 1,
            "scroll": [0, 0],
            "url": "https://a.example/",
        },
        "full_page": False,
        "at": 1_758_000_000.0,
    }
    record.update(overrides)
    return record


class TestWhatMakesACaptureStale:
    """Each refusal fires on its own condition and only on it. Both halves
    matter: a check that refused everything would pass the first assertion for
    the wrong reason."""

    def test_an_unchanged_page_is_not_stale(self, window):
        assert visual.staleness(_record(), _Page()) is None

    def test_no_record_at_all(self, window):
        code, detail = visual.staleness(None, _Page())
        assert code == "no_capture"
        assert "never been screenshotted" in detail

    def test_a_full_page_record(self, window):
        code, _ = visual.staleness(_record(full_page=True), _Page())
        assert code == "full_page_capture"

    def test_a_record_with_no_coordinate_frame(self, window):
        code, _ = visual.staleness(_record(offset=None), _Page())
        assert code == "no_coordinate_frame"

    def test_a_navigation(self, window):
        code, detail = visual.staleness(
            _record(), _Page(url="https://b.example/other"),
        )
        assert code == "stale_capture"
        # Both states named: an operator reading a log wants to know which way
        # the page moved.
        assert "https://a.example/" in detail
        assert "https://b.example/other" in detail

    def test_a_scroll(self, window):
        code, detail = visual.staleness(_record(), _Page(scroll=(0, 40)))
        assert code == "stale_capture"
        assert "40" in detail

    def test_sub_pixel_scroll_drift_is_not_a_refusal(self, window):
        # Scroll positions are doubles, a sticky header or a smooth-scroll
        # settle leaves fractions behind, and refusing a click over 0.4 of a
        # pixel costs the caller a round for nothing.
        assert visual.staleness(_record(), _Page(scroll=(0.4, 0.4))) is None

    def test_a_resized_viewport(self, window):
        code, _ = visual.staleness(_record(), _Page(viewport=(1000, 700)))
        assert code == "viewport_changed"

    def test_a_moved_window(self):
        moved = {"x": 100, "y": 0, "width": 1439, "height": 899}
        with mock.patch.object(visual.xdotool, "window_geometry", return_value=moved):
            code, detail = visual.staleness(_record(), _Page())
        assert code == "viewport_changed"
        assert "100" in detail

    def test_a_window_that_is_gone(self):
        with mock.patch.object(visual.xdotool, "window_geometry", return_value=None):
            code, _ = visual.staleness(_record(), _Page())
        assert code == "window_gone"

    def test_a_page_that_will_not_answer_is_allowed_on_the_window_check(self, window):
        # Refusing here would strand a caller whose page is merely busy, and
        # the window comparison above has already passed.
        page = _Page()
        page.raises = True
        assert visual.staleness(_record(), page) is None


class TestConvertingAPoint:
    def test_a_point_on_the_capture_itself_is_the_identity_plus_the_inset(self):
        x, y = visual.image_to_screen(_record(), 293, 337)
        assert (x, y) == (293, 337 + UI_INSET_Y)

    def test_a_half_size_picture_doubles_the_offset(self):
        # The delivered picture is not necessarily the captured one: a vision
        # provider rescales over its own envelope, and the skill shrinks ahead
        # of that so the two frames stay the same one.
        half = (CAPTURE_W // 2, CAPTURE_H // 2)
        x, y = visual.image_to_screen(_record(), 100, 100, image_size=half)
        assert round(x) == round(100 * CAPTURE_W / half[0])
        assert round(y) == round(100 * CAPTURE_H / half[1]) + UI_INSET_Y

    def test_the_delivered_size_the_skill_computes_round_trips(self):
        from istota.skills.browse import delivered_size

        size = delivered_size(CAPTURE_W, CAPTURE_H)
        # A point at the bottom-right of the delivered picture is the
        # bottom-right of the capture, which is what makes the two halves one
        # contract rather than two that happen to agree.
        x, y = visual.image_to_screen(_record(), size[0], size[1], image_size=list(size))
        assert round(x) == CAPTURE_W
        assert round(y) == CAPTURE_H + UI_INSET_Y

    @pytest.mark.parametrize("point", [(-1, 10), (10, -1), (CAPTURE_W + 1, 10), (10, CAPTURE_H + 1)])
    def test_a_point_outside_the_picture_refuses_with_the_bound(self, point):
        with pytest.raises(ValueError, match="outside the picture"):
            visual.image_to_screen(_record(), *point)

    @pytest.mark.parametrize("value", [float("nan"), "412", None])
    def test_a_point_that_is_not_a_finite_number_refuses(self, value):
        with pytest.raises(ValueError, match="finite number"):
            visual.image_to_screen(_record(), value, 10)

    def test_a_non_positive_image_size_refuses(self):
        with pytest.raises(ValueError, match="must be positive"):
            visual.image_to_screen(_record(), 1, 1, image_size=[0, 100])

    def test_page_css_pixels_go_through_the_same_frame(self):
        # The other entry point: a caller that located something through the
        # DOM has CSS pixels rather than picture pixels, and both paths must
        # land identically.
        assert visual.page_to_screen(_record(), 293, 337) == (293, 337 + UI_INSET_Y)

    def test_page_css_pixels_scale_by_the_device_pixel_ratio(self):
        record = _record()
        record["page"]["dpr"] = 2
        assert visual.page_to_screen(record, 100, 100) == (200, 200 + UI_INSET_Y)


# --------------------------------------------------------------------------- #
# `/interact`'s coordinate actions, through the real dispatcher
# --------------------------------------------------------------------------- #
#
# The arithmetic above is pinned against a recorded frame; this is the layer
# that decides whether a refusal reaches a caller at all. `browse_api` builds a
# Flask app and imports the Chrome driver at module scope, so the same stubs
# `test_browser_memory_eviction.py` installs are installed here.

import types  # noqa: E402

if "patchright" not in sys.modules:
    _patchright = types.ModuleType("patchright")
    _sync_api = types.ModuleType("patchright.sync_api")
    _sync_api.sync_playwright = mock.MagicMock(name="sync_playwright")
    _patchright.sync_api = _sync_api
    sys.modules["patchright"] = _patchright
    sys.modules["patchright.sync_api"] = _sync_api

if "markdownify" not in sys.modules:
    _markdownify = types.ModuleType("markdownify")

    class _StubConverter:
        def __init__(self, **options):
            self.options = options

        def convert_hN(self, *a, **k):  # pragma: no cover - never converts here
            raise AssertionError("stub converter used for a real conversion")

    _markdownify.MarkdownConverter = _StubConverter
    sys.modules["markdownify"] = _markdownify

if "flask" not in sys.modules:
    _flask = types.ModuleType("flask")

    class _StubFlask:
        def __init__(self, *_a, **_k):
            pass

        def route(self, *_a, **_k):
            return lambda fn: fn

        def before_request(self, fn):
            return fn

        def teardown_request(self, fn):
            return fn

        def after_request(self, fn):
            return fn

    _flask.Flask = _StubFlask
    _flask.Response = type("Response", (), {})
    _flask.jsonify = lambda *a, **k: dict(*a, **k)
    _flask.request = types.SimpleNamespace()
    sys.modules["flask"] = _flask

# Scoped to the one dependency that is genuinely optional here: bs4 reaches the
# env transitively, so it can be absent. Skipping on `browse_api` itself would
# swallow a syntax error in the module under test.
pytest.importorskip("bs4", reason="browser render module needs bs4")

import browse_api  # noqa: E402


class _SettlePage(_Page):
    """A page that also answers the post-action settle wait."""

    def wait_for_timeout(self, _ms):
        return None


@pytest.fixture
def pointer():
    """The X11 half, recorded rather than performed."""
    with mock.patch.object(browse_api.browsing, "human_click_at") as click, \
         mock.patch.object(browse_api.browsing, "human_move_to") as move, \
         mock.patch.object(browse_api.xdotool, "key_native") as key, \
         mock.patch.object(browse_api.xdotool, "type_native") as typed:
        yield types.SimpleNamespace(click=click, move=move, key=key, typed=typed)


class TestTheCoordinateActions:
    def test_a_click_reaches_the_pointer_at_the_converted_point(self, window, pointer):
        session = {"capture": _record(), "tab_index": 0}

        result = browse_api._coordinate_action(
            session, _SettlePage(), {"type": "click_at", "x": 293, "y": 337},
        )

        assert result == {
            "action": "click_at", "ok": True,
            "screen": [293, 337 + UI_INSET_Y],
        }
        pointer.click.assert_called_once()
        assert pointer.click.call_args[0] == (293, 337 + UI_INSET_Y)
        assert pointer.click.call_args[1] == {"button": 1}

    def test_the_delivered_picture_size_rides_the_action(self, window, pointer):
        # The half the skill supplies: a point on a downscaled picture is
        # converted back up before the pointer moves.
        half = [CAPTURE_W // 2, CAPTURE_H // 2]
        session = {"capture": _record(), "tab_index": 0}

        browse_api._coordinate_action(
            session, _SettlePage(),
            {"type": "click_at", "x": 100, "y": 100, "image_size": half},
        )

        x, _y = pointer.click.call_args[0]
        assert round(x) == round(100 * CAPTURE_W / half[0])

    def test_a_right_click_is_button_three(self, window, pointer):
        session = {"capture": _record(), "tab_index": 0}
        browse_api._coordinate_action(
            session, _SettlePage(),
            {"type": "click_at", "x": 10, "y": 10, "button": "right"},
        )
        assert pointer.click.call_args[1] == {"button": 3}

    def test_a_hover_moves_and_does_not_click(self, window, pointer):
        session = {"capture": _record(), "tab_index": 0}

        result = browse_api._coordinate_action(
            session, _SettlePage(), {"type": "hover_at", "x": 10, "y": 20},
        )

        assert result["ok"] is True
        pointer.move.assert_called_once_with(10, 20 + UI_INSET_Y)
        pointer.click.assert_not_called()

    @pytest.mark.parametrize("record,expected", [
        (None, "no_capture"),
        (_record(full_page=True), "full_page_capture"),
        (_record(offset=None), "no_coordinate_frame"),
    ])
    def test_each_refusal_fires_on_its_own_condition(
        self, window, pointer, record, expected,
    ):
        session = {"capture": record, "tab_index": 0}

        result = browse_api._coordinate_action(
            session, _SettlePage(), {"type": "click_at", "x": 10, "y": 10},
        )

        assert result["ok"] is False
        assert result["error"] == expected
        assert result["detail"]
        # Nothing was pressed, which is the property the refusal exists for.
        pointer.click.assert_not_called()

    def test_a_scrolled_page_is_refused_rather_than_clicked(self, window, pointer):
        session = {"capture": _record(), "tab_index": 0}

        result = browse_api._coordinate_action(
            session, _SettlePage(scroll=(0, 400)),
            {"type": "click_at", "x": 10, "y": 10},
        )

        assert result["error"] == "stale_capture"
        pointer.click.assert_not_called()

    def test_a_navigated_page_is_refused_rather_than_clicked(self, window, pointer):
        session = {"capture": _record(), "tab_index": 0}

        result = browse_api._coordinate_action(
            session, _SettlePage(url="https://elsewhere.example/"),
            {"type": "click_at", "x": 10, "y": 10},
        )

        assert result["error"] == "stale_capture"
        pointer.click.assert_not_called()

    def test_two_clicks_both_run_when_the_first_moved_nothing(self, window, pointer):
        # The checkbox case. `cmd_interact` always appends `--scroll` last, so
        # a scroll and a click can never share a call; two clicks can.
        session = {"capture": _record(), "tab_index": 0}
        page = _SettlePage()

        first = browse_api._coordinate_action(
            session, page, {"type": "click_at", "x": 10, "y": 10},
        )
        second = browse_api._coordinate_action(
            session, page, {"type": "click_at", "x": 20, "y": 20},
        )

        assert first["ok"] is True and second["ok"] is True
        assert pointer.click.call_count == 2

    def test_a_point_outside_the_picture_is_refused_before_the_pointer_moves(
        self, window, pointer,
    ):
        session = {"capture": _record(), "tab_index": 0}

        result = browse_api._coordinate_action(
            session, _SettlePage(), {"type": "click_at", "x": 99999, "y": 10},
        )

        assert result["error"] == "out_of_picture"
        pointer.click.assert_not_called()
        pointer.move.assert_not_called()

    def test_a_key_needs_no_capture_and_no_point(self, window, pointer):
        result = browse_api._coordinate_action(
            {"capture": None, "tab_index": 0}, _SettlePage(), {"type": "key", "key": "Tab"},
        )

        assert result == {"action": "key", "key": "Tab", "ok": True}
        pointer.key.assert_called_once_with("Tab")

    def test_an_empty_key_is_refused(self, window, pointer):
        result = browse_api._coordinate_action(
            {"capture": None, "tab_index": 0}, _SettlePage(), {"type": "key", "key": ""},
        )
        assert result["ok"] is False
        pointer.key.assert_not_called()

    def test_typed_text_is_capped(self, window, pointer):
        result = browse_api._coordinate_action(
            {"capture": None, "tab_index": 0}, _SettlePage(),
            {"type": "type", "text": "x" * (browse_api.MAX_TYPE_CHARS + 500)},
        )

        assert result["chars"] == browse_api.MAX_TYPE_CHARS
        assert len(pointer.typed.call_args[0][0]) == browse_api.MAX_TYPE_CHARS

    def test_a_navigation_under_the_settle_wait_does_not_lose_the_click(
        self, window, pointer,
    ):
        # A visual click is most useful exactly when it navigates, and a
        # navigation destroys the execution context the timer waits in.
        # Letting that raise would report a failure for a press that already
        # happened at the X11 level.
        page = _SettlePage()
        page.wait_for_timeout = mock.Mock(
            side_effect=RuntimeError("Execution context was destroyed"),
        )
        session = {"capture": _record(), "tab_index": 0}

        result = browse_api._coordinate_action(
            session, page, {"type": "click_at", "x": 10, "y": 10},
        )

        assert result["ok"] is True
        pointer.click.assert_called_once()
