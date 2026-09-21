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
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from unittest import mock

import pytest

_BROWSER_DIR = Path(__file__).resolve().parent.parent / "docker" / "browser"
if str(_BROWSER_DIR) not in sys.path:
    sys.path.insert(0, str(_BROWSER_DIR))

import visual  # noqa: E402
import xdotool  # noqa: E402


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


#: What the X11 window title reports while a page double is in front. The
#: double's own title by default, so the foreground check agrees with it.
DOC_TITLE = "A Example"


class _Page:
    """A page that answers the one evaluate `visual.page_state` makes.

    It also comes to the front and reports its title, because the real one
    does and every coordinate action now asks it to: X11 input reaches
    whichever tab the window is showing, so the action brings the named
    session's tab forward first. A double that cannot do that is less capable
    than the thing it stands in for, which makes every action refuse for a
    reason no test chose.
    """

    def __init__(self, viewport=(1439, 812), dpr=1, scroll=(0, 0), url="https://a.example/",
                 title=DOC_TITLE):
        self.state = [viewport[0], viewport[1], dpr, scroll[0], scroll[1], url]
        self.raises = False
        self.doc_title = title
        self.brought_to_front = 0
        # A page with no frames, so `click_challenge` reaches its own
        # `no_challenge` answer rather than an AttributeError from the frame
        # walk. A test that wants a challenge patches the point out.
        self.frames = []

    def evaluate(self, _script):
        if self.raises:
            raise RuntimeError("Execution context was destroyed")
        return list(self.state)

    def bring_to_front(self):
        self.brought_to_front += 1

    def title(self):
        return self.doc_title


@pytest.fixture(autouse=True)
def foreground_window():
    """The window showing whatever tab was last asked for.

    Autouse because the foreground check runs ahead of every coordinate
    action, so without it each one refuses with `tab_unavailable` before
    reaching the behaviour under test. `TestTheForegroundGuard` overrides it
    to drive the refusals themselves.
    """
    with mock.patch.object(visual.xdotool, "window_title",
                           return_value=f"{DOC_TITLE} - Google Chrome"):
        yield


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
        # And the horizontal inset is zero rather than half the difference:
        # the viewport starts at the window's left edge and the scrollbar takes
        # its pixels off the right, so centring the capture would put every
        # click half a scrollbar right. Measured on the shipped container at
        # both screen sizes: window 1439x899 with capture 1439x812, and window
        # 1919x1079 with capture 1919x992 — equal widths, inset [0, 87] in
        # both, which is what makes this arm reachable only under a scrollbar.
        assert record["offset"] == [0, WINDOW["height"] - 812]

    def test_a_capture_wider_than_its_window_is_refused(self, window):
        # A device pixel ratio above 1, or a capture of some other window.
        # Either way the frame is not the one the pointer acts in, and a
        # conversion against it would click somewhere arbitrary.
        record, why = visual.build_capture(_png(WINDOW["width"] + 40, 812), page=_Page())

        assert record is None
        assert "wider than its window" in why

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

# The skill half, for the parity guard at the end of this file alone. The two
# are separate programs in separate repositories -- `docker/browser/` is a
# vendored copy of stealth-browser -- so a number restated on both sides is
# held equal here rather than shared.
from istota.skills.browse import (  # noqa: E402
    KEY_PRESSES_DEFAULT,
    MAX_SCROLL_UNITS,
    WHEEL_CLICKS_DEFAULT,
    ZOOM_MODIFIER,
)


class _SettlePage(_Page):
    """A page that also answers the post-action settle wait."""

    def wait_for_timeout(self, _ms):
        return None


@pytest.fixture
def pointer():
    """The X11 half, recorded rather than performed.

    Both move calls answer whether the pointer reached the point, and both
    are set to True here rather than left as the default mock. A bare
    MagicMock is truthy, so a test resting on that would pass just as
    happily against a dispatcher that ignored the answer -- and a test that
    needs the pointer to have failed sets it to False.
    """
    with mock.patch.object(browse_api.browsing, "human_click_at") as click, \
         mock.patch.object(browse_api.browsing, "human_move_to") as move, \
         mock.patch.object(browse_api.xdotool, "key_native") as key, \
         mock.patch.object(browse_api.xdotool, "type_native") as typed:
        click.return_value = True
        move.return_value = True
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

    def test_text_past_the_cap_is_refused_rather_than_half_typed(
        self, window, pointer,
    ):
        result = browse_api._coordinate_action(
            {"capture": None, "tab_index": 0}, _SettlePage(),
            {"type": "type", "text": "x" * (browse_api.MAX_TYPE_CHARS + 500)},
        )

        assert result["ok"] is False
        assert result["error"] == "text_too_long"
        # The page is left as it was, which truncation could not promise.
        pointer.typed.assert_not_called()

    def test_text_at_the_cap_types(self, window, pointer):
        at_cap = "x" * browse_api.MAX_TYPE_CHARS
        result = browse_api._coordinate_action(
            {"capture": None, "tab_index": 0}, _SettlePage(),
            {"type": "type", "text": at_cap},
        )

        assert result["ok"] is True
        assert result["chars"] == browse_api.MAX_TYPE_CHARS
        pointer.typed.assert_called_once_with(at_cap)

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


# --------------------------------------------------------------------------- #
# A pointer that did not move (ISSUE-523)
# --------------------------------------------------------------------------- #


class TestAPointerThatDidNotMove:
    """`ok: true` carrying the point that was *asked for* is the whole defect.

    `mouse_move` catches a `--sync` timeout and carries on, on the reasoning
    that the pointer clamps to the screen edge and is near enough to click.
    That is right for the clamp and for nothing else: an X server stall, a
    compositor hiccup or a slow round trip leaves the pointer where the last
    action put it, and `mouse_click` presses wherever the pointer is.

    So the click lands on an element nobody chose and the result says it went
    where it was aimed -- and the visual path has no selector to disconfirm
    it, since clicking a coordinate is the point. Nothing downstream catches
    the mismatch; the model reads `ok: true` and reasons from there.
    """

    @pytest.mark.parametrize("action", [
        {"type": "click_at", "x": 293, "y": 337},
        {"type": "hover_at", "x": 293, "y": 337},
    ])
    def test_it_is_refused_rather_than_reported_at_the_point_it_wanted(
        self, window, pointer, action,
    ):
        pointer.click.return_value = False
        pointer.move.return_value = False
        session = {"capture": _record(), "tab_index": 0}

        result = browse_api._coordinate_action(session, _SettlePage(), action)

        assert result["ok"] is False
        assert result["error"] == "pointer_did_not_move"
        assert result["detail"]
        # And it does not carry `screen`, which is the field that was the lie.
        assert "screen" not in result

    def test_the_challenge_click_answers_the_same_way(self, window, pointer):
        pointer.click.return_value = False
        session = {"capture": _record(), "tab_index": 0}

        with mock.patch.object(
            browse_api.browsing, "cloudflare_checkbox_point", return_value=(100, 200),
        ):
            result = browse_api._coordinate_action(
                session, _SettlePage(), {"type": "click_challenge"},
            )

        assert result["ok"] is False
        assert result["error"] == "pointer_did_not_move"

    def test_a_pointer_that_arrived_still_reports_ok(self, window, pointer):
        """The control: the refusal must not fire on the ordinary path."""
        session = {"capture": _record(), "tab_index": 0}

        result = browse_api._coordinate_action(
            session, _SettlePage(), {"type": "click_at", "x": 293, "y": 337},
        )

        assert result["ok"] is True
        assert result["screen"] == [293, 337 + UI_INSET_Y]


class TestTheChallengeClickReMeasures:
    """It converts through the recorded frame, so it takes click_at's pass.

    Latent rather than live: under Xvfb with no window manager the frame does
    not move, and a Chrome relaunch kills the session through the generation
    check before a stale frame could be used. Two paths converting against
    the same record on different evidence is the gap that becomes reachable
    when something unrelated changes, and it costs one call to close.
    """

    @pytest.fixture
    def checkbox(self):
        with mock.patch.object(
            browse_api.browsing, "cloudflare_checkbox_point", return_value=(100, 200),
        ) as point:
            yield point

    @pytest.mark.parametrize("page,expected", [
        (_SettlePage(scroll=(0, 400)), "stale_capture"),
        (_SettlePage(url="https://elsewhere.example/"), "stale_capture"),
    ])
    def test_a_capture_that_no_longer_describes_the_page_is_refused(
        self, window, pointer, checkbox, page, expected,
    ):
        session = {"capture": _record(), "tab_index": 0}

        result = browse_api._coordinate_action(
            session, page, {"type": "click_challenge"},
        )

        assert result["ok"] is False
        assert result["error"] == expected
        pointer.click.assert_not_called()

    @pytest.mark.parametrize("record,expected", [
        (None, "no_capture"),
        (_record(full_page=True), "full_page_capture"),
        (_record(offset=None), "no_coordinate_frame"),
    ])
    def test_it_names_the_same_codes_click_at_names(
        self, window, pointer, checkbox, record, expected,
    ):
        """A missing frame used to come back as `no_capture` from this arm."""
        session = {"capture": record, "tab_index": 0}

        result = browse_api._coordinate_action(
            session, _SettlePage(), {"type": "click_challenge"},
        )

        assert result["ok"] is False
        assert result["error"] == expected
        pointer.click.assert_not_called()

    def test_a_live_capture_still_presses(self, window, pointer, checkbox):
        session = {"capture": _record(), "tab_index": 0}

        result = browse_api._coordinate_action(
            session, _SettlePage(), {"type": "click_challenge"},
        )

        assert result["ok"] is True
        assert result["css"] == [100, 200]
        pointer.click.assert_called_once()


# --------------------------------------------------------------------------- #
# The type cap, and what actually bounds it (ISSUE-521)
# --------------------------------------------------------------------------- #


class TestTheTypeCapIsDerived:
    """The cap and the type timeout are one number, read from one place.

    They were two: `/interact` advertised 4,096 characters against a fixed
    30-second timeout that delivers about 1,220 of them at the measured
    24.5 ms a character the shipped build costs at `--delay 45`. Past that
    `subprocess.run` raised, killed xdotool mid-type, and left the field
    holding part of the text with the rest of the action list unrun.
    """

    def test_the_cap_is_asked_of_the_module_that_does_the_typing(self):
        assert browse_api.MAX_TYPE_CHARS == browse_api.xdotool.max_type_chars()

    def test_a_type_at_the_cap_fits_inside_its_own_timeout(self):
        at_cap = browse_api.xdotool.type_timeout_s(browse_api.MAX_TYPE_CHARS)
        assert at_cap <= browse_api.xdotool.TYPE_TIMEOUT_CEILING_S

    def test_the_ceiling_sits_under_the_watchdog_that_would_kill_the_session(self):
        """The bound is not the HTTP timeout, and this is the drift guard.

        `/interact` is registered in the in-flight slot the browse watchdog
        reads, and that watchdog kills and relaunches Chrome once a request
        outlives its deadline. So a type allowed to run longer does not time
        out -- it destroys the session it is typing into. Raising the ceiling
        past the deadline has to fail here rather than in production.
        """
        assert (
            browse_api.xdotool.TYPE_TIMEOUT_CEILING_S
            < browse_api.BROWSE_WATCHDOG_DEADLINE_S
        )


# --------------------------------------------------------------------------- #
# What may reach an xdotool argv slot (ISSUE-519)
# --------------------------------------------------------------------------- #


@pytest.fixture
def xdo_argv():
    """Let the real guard run, and record the argv it would have issued.

    The `pointer` fixture above replaces `type_native` and `key_native`
    outright, which is right for the tests that care where the pointer went
    and wrong for these: the refusal being pinned lives *inside* those
    functions, so a stub in front of them would assert about the stub. Only
    the two things that touch the display are replaced here -- the subprocess
    and the focus call -- and the real entry points run.
    """
    with mock.patch.object(browse_api.xdotool.subprocess, "run") as run, \
         mock.patch.object(browse_api.xdotool, "focus_chrome"):
        run.return_value = None
        yield run


class TestTheOptionShapedRefusal:
    """xdotool parses the trailing slot with getopt_long, so it is a boundary.

    `type` supports `--file <path>`, measured against the shipped build rather
    than read off the manual: `xdotool type --clearmodifiers --delay 45
    '--file=/nonexistent-probe'` answers `Failure opening ...`, and the same
    argv with `--` in front of the value types it literally. The value arrives
    from the model -- `/interact`'s `type` action carries it unexamined -- so
    an unguarded slot reads any file the browser container can see into a form
    field on a page the model chose, the shared Chrome profile included.
    """

    @pytest.mark.parametrize("action", [
        {"type": "type", "text": "--file=/etc/hostname"},
        {"type": "type", "text": "--file=-"},
        {"type": "key", "key": "--window=12345"},
    ])
    def test_it_is_refused_rather_than_typed(self, xdo_argv, action):
        result = browse_api._coordinate_action(
            {"capture": None, "tab_index": 0}, _SettlePage(), action,
        )

        assert result["ok"] is False
        assert result["error"] == "option_shaped_input"
        assert result["detail"]
        # The property the refusal exists for: nothing reached xdotool.
        assert xdo_argv.call_args_list == []

    def test_the_refusal_is_a_result_not_a_raise(self, xdo_argv):
        """`/interact` abandons its remaining actions on an exception.

        A list whose later actions are fine should not be lost to one
        argument this action declined, so the refusal comes back in the
        action's own slot and the loop carries on.
        """
        session = {"capture": None, "tab_index": 0}
        page = _SettlePage()

        refused = browse_api._coordinate_action(
            session, page, {"type": "type", "text": "--file=/etc/hostname"},
        )
        after = browse_api._coordinate_action(
            session, page, {"type": "type", "text": "ordinary text"},
        )

        assert refused["ok"] is False
        assert after["ok"] is True

    def test_ordinary_text_still_reaches_xdotool_behind_a_separator(self, xdo_argv):
        result = browse_api._coordinate_action(
            {"capture": None, "tab_index": 0}, _SettlePage(),
            {"type": "type", "text": "hello world"},
        )

        assert result["ok"] is True
        argv = xdo_argv.call_args[0][0]
        assert argv[:2] == ["xdotool", "type"]
        assert argv[-2:] == ["--", "hello world"]

    def test_a_text_value_that_is_not_a_string_is_refused(self, xdo_argv):
        """It comes off model-written JSON, so the type is not ours to assume."""
        result = browse_api._coordinate_action(
            {"capture": None, "tab_index": 0}, _SettlePage(),
            {"type": "type", "text": {"file": "/etc/hostname"}},
        )

        assert result["ok"] is False
        assert result["error"] == "option_shaped_input"
        assert xdo_argv.call_args_list == []


class TestTheForegroundGuard:
    """Which tab a coordinate action addresses.

    Sessions are tabs in one Chrome window and X11 input reaches whatever tab
    that window is showing, so an action that does not switch tabs lands in
    whichever session navigated last -- with `ok: true` and the point it was
    asked for. The staleness check cannot see it: it compares the *named*
    session's url, scroll and viewport, and none of those moves when the input
    goes somewhere else.

    These drive `_coordinate_action` rather than `visual.bring_to_front`,
    because the guard's placement is the half that can regress silently. A
    check that ran after the staleness pass, or on only one of the five
    actions, would leave every test in `visual`'s own suite green.
    """

    @pytest.fixture
    def other_tab_in_front(self):
        """The window is showing a different tab from the one named."""
        with mock.patch.object(visual.xdotool, "window_title",
                               return_value="Some Other Tab - Google Chrome"):
            yield

    @pytest.fixture
    def live(self, window):
        """A session whose picture is current, so only the tab is in question."""
        page = _SettlePage()
        record, error = visual.build_capture(_png(CAPTURE_W, CAPTURE_H), page=page)
        assert not error
        return {"capture": record, "tab_index": 0}, page

    ACTIONS = [
        {"type": "click_at", "x": 10, "y": 10},
        {"type": "hover_at", "x": 10, "y": 10},
        {"type": "click_challenge"},
        {"type": "key", "key": "Return"},
        {"type": "type", "text": "hello"},
    ]

    @pytest.mark.parametrize("action", ACTIONS,
                             ids=[a["type"] for a in ACTIONS])
    def test_every_action_asks_for_its_own_tab(self, live, pointer, action):
        session, page = live
        browse_api._coordinate_action(session, page, action)
        assert page.brought_to_front == 1

    @pytest.mark.parametrize("action", ACTIONS,
                             ids=[a["type"] for a in ACTIONS])
    def test_a_tab_that_is_not_in_front_refuses_before_acting(
        self, live, pointer, other_tab_in_front, action,
    ):
        """The 524 shape. Nothing may be pressed or typed on this path: the
        input would reach another session's page."""
        session, page = live
        result = browse_api._coordinate_action(session, page, action)

        assert result["ok"] is False
        assert result["error"] == "tab_not_foreground"
        assert pointer.click.call_args_list == []
        assert pointer.move.call_args_list == []
        assert pointer.typed.call_args_list == []
        assert pointer.key.call_args_list == []

    def test_key_and_type_are_guarded_though_they_skip_staleness(
        self, other_tab_in_front, pointer,
    ):
        """They need no picture, which is why they never reach the staleness
        check -- so the tab guard is the only thing standing in front of them,
        and it has to run on a session that has never been screenshotted."""
        page = _SettlePage()
        for action in ({"type": "key", "key": "Return"},
                       {"type": "type", "text": "hello"}):
            result = browse_api._coordinate_action(
                {"capture": None, "tab_index": 0}, page, action,
            )
            assert result["error"] == "tab_not_foreground", action

    def test_the_guard_runs_before_the_staleness_check(self, other_tab_in_front,
                                                       pointer):
        """Asking whether the picture still describes the page is worthless
        once it has passed on a tab nobody is looking at. With both wrong, the
        tab is the answer that comes back."""
        result = browse_api._coordinate_action(
            {"capture": None, "tab_index": 0}, _SettlePage(),
            {"type": "click_at", "x": 10, "y": 10},
        )
        assert result["error"] == "tab_not_foreground"

    def test_a_tab_that_will_not_come_forward_refuses(self, live, pointer):
        session, page = live

        def gone():
            raise RuntimeError("Target closed")

        page.bring_to_front = gone
        result = browse_api._coordinate_action(
            session, page, {"type": "click_at", "x": 10, "y": 10},
        )
        assert result["ok"] is False
        assert result["error"] == "tab_unavailable"
        assert pointer.click.call_args_list == []

    def test_an_unconfirmed_switch_is_allowed_and_carried_on_the_result(
        self, live, pointer,
    ):
        """A page with no title cannot be checked against the window title.
        Refusing there would strand every caller on an untitled page, so it
        runs and says the tab could not be confirmed."""
        session, page = live
        page.doc_title = ""

        result = browse_api._coordinate_action(
            session, page, {"type": "click_at", "x": 10, "y": 10},
        )
        assert result["ok"] is True
        assert result["foreground"] == "foreground_unconfirmed"
        assert pointer.click.call_args_list != []

    def test_a_confirmed_switch_says_nothing(self, live, pointer):
        """The control for the test above: the ordinary case adds no key, so
        `foreground` on a result means something was not established."""
        session, page = live
        result = browse_api._coordinate_action(
            session, page, {"type": "click_at", "x": 10, "y": 10},
        )
        assert result["ok"] is True
        assert "foreground" not in result


class TestTheWheel:
    """`scroll_at` is a wheel at a point, and it takes click_at's whole pass.

    ISSUE-528. `--scroll` was the last model-driven action still going through
    `page.evaluate`, and `window.scrollBy` was also the weakest scroll
    available: it moves the document, never the pane the pointer is over, so a
    chat log, a code viewer, a results list inside a modal and a PDF viewer --
    precisely the widgets visual mode exists for -- were the cases where it did
    nothing useful.

    Two paths now, and the split is what each one can address. A wheel tick is
    delivered wherever the pointer is, so `scroll_at` converts a picture point
    exactly as `click_at` does and takes the same refusals. A keyless scroll has
    no point to aim at, so it goes to whatever holds keyboard focus -- the
    document -- through `xdo_key`, which is what `simulate_human_behavior` has
    always used.
    """

    @pytest.fixture
    def wheel(self):
        """The wheel half, recorded. Nothing here performs X11 input."""
        with mock.patch.object(browse_api.browsing, "human_scroll_at") as at, \
             mock.patch.object(browse_api.xdotool, "key_native") as key:
            at.return_value = True
            key.return_value = True
            yield types.SimpleNamespace(at=at, key=key)

    def test_a_wheel_reaches_the_pointer_at_the_converted_point(
        self, window, pointer, wheel,
    ):
        session = {"capture": _record(), "tab_index": 0}

        result = browse_api._coordinate_action(
            session, _SettlePage(),
            {"type": "scroll_at", "x": 293, "y": 337,
             "direction": "down", "clicks": 3},
        )

        assert result["ok"] is True
        assert result["screen"] == [293, 337 + UI_INSET_Y]
        assert result["clicks"] == 3
        x, y = wheel.at.call_args[0]
        assert (round(x), round(y)) == (293, 337 + UI_INSET_Y)
        assert wheel.at.call_args[1]["button"] == browse_api.xdotool.WHEEL_DOWN
        assert wheel.at.call_args[1]["clicks"] == 3

    def test_up_is_the_other_wheel_button(self, window, pointer, wheel):
        session = {"capture": _record(), "tab_index": 0}

        browse_api._coordinate_action(
            session, _SettlePage(),
            {"type": "scroll_at", "x": 10, "y": 10, "direction": "up"},
        )

        assert wheel.at.call_args[1]["button"] == browse_api.xdotool.WHEEL_UP

    def test_a_zoom_holds_the_modifier_across_the_ticks(
        self, window, pointer, wheel,
    ):
        """ctrl plus wheel at a point is the map-zoom gesture, and it falls out
        of the same primitive rather than being a second mechanism."""
        session = {"capture": _record(), "tab_index": 0}

        result = browse_api._coordinate_action(
            session, _SettlePage(),
            {"type": "scroll_at", "x": 10, "y": 10, "modifier": "ctrl"},
        )

        assert result["ok"] is True
        assert result["modifier"] == "ctrl"
        assert wheel.at.call_args[1]["modifier"] == "ctrl"

    def test_a_modifier_the_container_does_not_know_is_refused(
        self, window, pointer, wheel,
    ):
        """An allowlist rather than a pass-through: the value reaches
        `xdotool keydown` and a modifier nobody vetted is an arbitrary key
        held down across the rest of the action list."""
        session = {"capture": _record(), "tab_index": 0}

        result = browse_api._coordinate_action(
            session, _SettlePage(),
            {"type": "scroll_at", "x": 10, "y": 10, "modifier": "--file=/etc/x"},
        )

        assert result["ok"] is False
        assert result["error"] == "unknown_modifier"
        wheel.at.assert_not_called()

    @pytest.mark.parametrize("record,expected", [
        (None, "no_capture"),
        (_record(full_page=True), "full_page_capture"),
        (_record(offset=None), "no_coordinate_frame"),
    ])
    def test_it_takes_click_at_s_refusals(
        self, window, pointer, wheel, record, expected,
    ):
        session = {"capture": record, "tab_index": 0}

        result = browse_api._coordinate_action(
            session, _SettlePage(), {"type": "scroll_at", "x": 10, "y": 10},
        )

        assert result["ok"] is False
        assert result["error"] == expected
        wheel.at.assert_not_called()

    def test_a_scrolled_page_is_refused_rather_than_wheeled(
        self, window, pointer, wheel,
    ):
        session = {"capture": _record(), "tab_index": 0}

        result = browse_api._coordinate_action(
            session, _SettlePage(scroll=(0, 400)),
            {"type": "scroll_at", "x": 10, "y": 10},
        )

        assert result["error"] == "stale_capture"
        wheel.at.assert_not_called()

    def test_a_point_outside_the_picture_is_refused_before_the_pointer_moves(
        self, window, pointer, wheel,
    ):
        session = {"capture": _record(), "tab_index": 0}

        result = browse_api._coordinate_action(
            session, _SettlePage(), {"type": "scroll_at", "x": 99999, "y": 10},
        )

        assert result["error"] == "out_of_picture"
        wheel.at.assert_not_called()

    def test_a_pointer_that_did_not_arrive_wheels_nothing(
        self, window, pointer, wheel,
    ):
        """`human_scroll_at` answers False when the landing move failed, and a
        wheel tick delivered wherever the pointer happens to be scrolls
        whatever the previous action was aimed at."""
        session = {"capture": _record(), "tab_index": 0}
        wheel.at.return_value = False

        result = browse_api._coordinate_action(
            session, _SettlePage(), {"type": "scroll_at", "x": 10, "y": 10},
        )

        assert result["ok"] is False
        assert result["error"] == "pointer_did_not_move"

    @pytest.mark.parametrize("clicks", [0, -1, 99999, "3", True])
    def test_a_click_count_outside_the_bound_is_refused(
        self, window, pointer, wheel, clicks,
    ):
        """Refused rather than clamped: a caller that asked for 500 ticks and
        silently got 30 has been told the pane reached its bottom when it did
        not. `True` is in the list because `bool` is an `int` in Python, so it
        would otherwise be accepted as one tick."""
        session = {"capture": _record(), "tab_index": 0}

        result = browse_api._coordinate_action(
            session, _SettlePage(),
            {"type": "scroll_at", "x": 10, "y": 10, "clicks": clicks},
        )

        assert result["ok"] is False
        assert result["error"] == "bad_click_count"
        wheel.at.assert_not_called()

    def test_an_omitted_count_takes_the_container_s_own_default(self, window, pointer, wheel):
        """The skill always sends a count, so this is the hand-built request's
        path -- and it must not be the refusal above."""
        session = {"capture": _record(), "tab_index": 0}

        result = browse_api._coordinate_action(
            session, _SettlePage(), {"type": "scroll_at", "x": 10, "y": 10},
        )

        assert result["ok"] is True
        assert result["clicks"] == browse_api.DEFAULT_WHEEL_CLICKS


class TestTheKeylessScroll:
    """No point, so it goes to keyboard focus -- and never to CDP.

    The discriminating assertion is that `page.evaluate` is not called. A page
    that scrolled is indistinguishable from one that did not at this layer, so
    asserting only that a key was sent would pass just as happily against a
    build that sent the key *and* kept the evaluate.
    """

    @pytest.fixture
    def wheel(self):
        with mock.patch.object(browse_api.browsing, "human_scroll_at") as at, \
             mock.patch.object(browse_api.xdotool, "key_native") as key:
            at.return_value = True
            key.return_value = True
            yield types.SimpleNamespace(at=at, key=key)

    def test_down_is_page_down_through_xdo_key(self, window, pointer, wheel):
        result = browse_api._coordinate_action(
            {"capture": None, "tab_index": 0}, _SettlePage(),
            {"type": "scroll", "direction": "down", "presses": 2},
        )

        assert result["ok"] is True
        assert result["presses"] == 2
        assert wheel.key.call_args_list == [mock.call("Page_Down")] * 2

    def test_up_is_page_up(self, window, pointer, wheel):
        browse_api._coordinate_action(
            {"capture": None, "tab_index": 0}, _SettlePage(),
            {"type": "scroll", "direction": "up"},
        )
        assert wheel.key.call_args_list == [mock.call("Page_Up")]

    def test_it_needs_no_capture(self, window, pointer, wheel):
        """A scroll with no point converts nothing, so a session that has
        never been screenshotted can still scroll -- which is what the
        infinite-scroll recipe does before it ever takes a picture."""
        result = browse_api._coordinate_action(
            {"capture": None, "tab_index": 0}, _SettlePage(),
            {"type": "scroll", "direction": "down"},
        )
        assert result["ok"] is True

    def test_nothing_on_this_path_evaluates(self, window, pointer, wheel):
        class _NoEvaluate(_SettlePage):
            def evaluate(self, *_a, **_k):
                raise AssertionError(
                    "the scroll reached page.evaluate -- ISSUE-528's whole "
                    "subject is that it must not"
                )

        result = browse_api._coordinate_action(
            {"capture": None, "tab_index": 0}, _NoEvaluate(),
            {"type": "scroll", "direction": "down"},
        )
        assert result["ok"] is True

    @pytest.mark.parametrize("presses", [0, -1, 99999, "2", True])
    def test_a_press_count_outside_the_bound_is_refused(
        self, window, pointer, wheel, presses,
    ):
        result = browse_api._coordinate_action(
            {"capture": None, "tab_index": 0}, _SettlePage(),
            {"type": "scroll", "direction": "down", "presses": presses},
        )

        assert result["ok"] is False
        assert result["error"] == "bad_click_count"
        wheel.key.assert_not_called()

    def test_an_unknown_direction_is_refused(self, window, pointer, wheel):
        result = browse_api._coordinate_action(
            {"capture": None, "tab_index": 0}, _SettlePage(),
            {"type": "scroll", "direction": "sideways"},
        )

        assert result["ok"] is False
        assert result["error"] == "bad_direction"
        wheel.key.assert_not_called()


class TestTheScrollCeilingsAgree:
    """The skill restates the container's bound, so the two are held equal.

    The skill refuses an out-of-range count before the request is sent, which
    saves a round trip and names the flag rather than the wire field. That is
    a second copy of a number, and a skill whose ceiling drifted *above* the
    container's would send requests that come back `bad_click_count` with the
    flag's own help text saying they were fine.
    """

    def test_the_maximum_is_the_same_number(self):
        assert MAX_SCROLL_UNITS == browse_api.MAX_SCROLL_UNITS

    def test_the_wheel_default_is_the_same_number(self):
        assert WHEEL_CLICKS_DEFAULT == browse_api.DEFAULT_WHEEL_CLICKS

    def test_the_press_default_is_the_same_number(self):
        assert KEY_PRESSES_DEFAULT == browse_api.DEFAULT_SCROLL_PRESSES

    def test_the_zoom_modifier_is_one_the_container_will_hold(self):
        assert ZOOM_MODIFIER in browse_api.xdotool.MODIFIERS


class TestTheRetiredWireArgument:
    """A caller still speaking the old contract is refused, not half-obeyed.

    `--scroll-amount` defaulted to 500, so *every* scroll an older caller sends
    carries `amount`. Reading `presses` and ignoring it would do one Page_Down
    and answer `ok: true` for a request that asked to move four screens — the
    shape `_scroll_count` refuses a few lines up, arriving by the one route the
    skill's own refusal cannot cover: a caller that does not go through the
    skill.
    """

    @pytest.fixture
    def wheel(self):
        with mock.patch.object(browse_api.browsing, "human_scroll_at") as at, \
             mock.patch.object(browse_api.xdotool, "key_native") as key:
            at.return_value = True
            key.return_value = True
            yield types.SimpleNamespace(at=at, key=key)

    def test_the_old_pixel_argument_is_refused_by_name(self, window, pointer, wheel):
        result = browse_api._coordinate_action(
            {"capture": None, "tab_index": 0}, _SettlePage(),
            {"type": "scroll", "direction": "down", "amount": 2000},
        )

        assert result["ok"] is False
        assert result["error"] == "retired_argument"
        assert "presses" in result["detail"]
        wheel.key.assert_not_called()

    def test_it_is_refused_even_where_a_press_count_is_also_sent(
        self, window, pointer, wheel,
    ):
        """A caller sending both has two different distances in mind and this
        cannot tell which it meant."""
        result = browse_api._coordinate_action(
            {"capture": None, "tab_index": 0}, _SettlePage(),
            {"type": "scroll", "direction": "down", "amount": 2000, "presses": 2},
        )

        assert result["error"] == "retired_argument"
        wheel.key.assert_not_called()

    def test_a_scroll_without_it_is_unaffected(self, window, pointer, wheel):
        """The control: the refusal keys on the argument's presence, so an
        ordinary scroll must not trip it."""
        result = browse_api._coordinate_action(
            {"capture": None, "tab_index": 0}, _SettlePage(),
            {"type": "scroll", "direction": "down", "presses": 2},
        )

        assert result["ok"] is True
        assert wheel.key.call_count == 2


class TestAScrollThatCouldNotAimIsNotReportedAsDistance:
    """`presses: 3` is a claim about the page, so it needs the window.

    `key_native` sends the key whatever happens — XTest delivers to whatever
    holds focus — but it answers whether the Chrome window could be focused,
    and a scroll is the one of the three keyboard actions that reports how far
    something moved. `key` and `type` deliberately still ignore that answer:
    they report a keystroke, not a distance.
    """

    @pytest.fixture
    def wheel(self):
        with mock.patch.object(browse_api.browsing, "human_scroll_at") as at, \
             mock.patch.object(browse_api.xdotool, "key_native") as key:
            at.return_value = True
            key.return_value = True
            yield types.SimpleNamespace(at=at, key=key)

    def test_an_unfocusable_window_refuses_rather_than_reporting_presses(
        self, window, pointer, wheel,
    ):
        wheel.key.return_value = False

        result = browse_api._coordinate_action(
            {"capture": None, "tab_index": 0}, _SettlePage(),
            {"type": "scroll", "direction": "down", "presses": 3},
        )

        assert result["ok"] is False
        assert result["error"] == "window_not_focused"

    def test_it_stops_at_the_first_one_rather_than_pressing_on(
        self, window, pointer, wheel,
    ):
        """Three presses against a window nothing can aim at is three chances
        for the keys to land somewhere else."""
        wheel.key.return_value = False

        browse_api._coordinate_action(
            {"capture": None, "tab_index": 0}, _SettlePage(),
            {"type": "scroll", "direction": "down", "presses": 3},
        )

        assert wheel.key.call_count == 1

    def test_the_key_action_still_ignores_the_same_answer(
        self, window, pointer, wheel,
    ):
        """The control, and the decision: `key` reports that a key was sent,
        which is true whether or not the window took focus. Changing that is a
        different contract and was not part of ISSUE-528."""
        wheel.key.return_value = False

        result = browse_api._coordinate_action(
            {"capture": None, "tab_index": 0}, _SettlePage(),
            {"type": "key", "key": "Tab"},
        )

        assert result["ok"] is True


class TestTheModifierIsAlwaysReleased:
    """A modifier left down outlives the action, and the display is shared.

    X11 modifier state belongs to the server, so a stuck ctrl turns every later
    keystroke on this container into a shortcut until something releases it —
    not merely the rest of this action list. The whole of `modifier_held` is
    that the keyup happens anyway, and it is the one thing in the wheel path no
    higher-level test reaches, since every container test mocks
    `human_scroll_at` clean over it.
    """

    @pytest.fixture
    def runs(self):
        with mock.patch.object(xdotool, "focus_chrome", return_value=True), \
             mock.patch.object(xdotool.subprocess, "run") as run:
            yield run

    def _argvs(self, run):
        return [call.args[0] for call in run.call_args_list]

    def test_the_ordinary_path_presses_and_releases(self, runs):
        with xdotool.modifier_held("ctrl"):
            pass

        assert self._argvs(runs) == [
            ["xdotool", "keydown", "--", "ctrl"],
            ["xdotool", "keyup", "--", "ctrl"],
        ]

    def test_a_raise_inside_the_block_still_releases(self, runs):
        with pytest.raises(RuntimeError):
            with xdotool.modifier_held("ctrl"):
                raise RuntimeError("the wheel failed mid-run")

        assert self._argvs(runs)[-1] == ["xdotool", "keyup", "--", "ctrl"]

    def test_a_keydown_that_times_out_still_releases(self, runs):
        """The finding this test exists for. `subprocess.run` raises
        `TimeoutExpired` *after* xdotool may already have delivered the press,
        so a keydown outside the `try` leaves ctrl held with no `finally` to
        run. Moving the keydown back above the `try` turns this red and
        nothing else."""
        runs.side_effect = [
            subprocess.TimeoutExpired(["xdotool", "keydown"], 5),
            None,
        ]

        with pytest.raises(subprocess.TimeoutExpired):
            with xdotool.modifier_held("ctrl"):
                raise AssertionError("the block must not run")

        assert self._argvs(runs)[-1] == ["xdotool", "keyup", "--", "ctrl"]

    def test_no_modifier_touches_the_display_at_all(self, runs):
        """`None` is the ordinary case, so it must cost nothing — a keyup for
        a key nobody pressed is a keystroke this container did not intend."""
        with xdotool.modifier_held(None):
            pass

        assert runs.call_args_list == []

    def test_a_modifier_outside_the_allowlist_presses_nothing(self, runs):
        with pytest.raises(ValueError):
            with xdotool.modifier_held("--file=/etc/passwd"):
                pass

        assert runs.call_args_list == []


class TestTheWheelButtonGuard:
    """`mouse_wheel` takes a button rather than a direction, so it checks."""

    def test_a_wheel_tick_is_a_press_and_release_of_that_button(self):
        with mock.patch.object(xdotool, "mouse_click") as click:
            xdotool.mouse_wheel(xdotool.WHEEL_DOWN)

        assert click.call_args[1]["button"] == xdotool.WHEEL_DOWN
        # Far shorter than a click's: a wheel tick is a detent, and a mouse
        # holding button 5 down for 90ms is its own signature.
        assert click.call_args[1]["dwell_s"] < 0.05

    @pytest.mark.parametrize("button", [1, 3, 0, -1, "4"])
    def test_a_button_that_is_not_the_wheel_presses_nothing(self, button):
        with mock.patch.object(xdotool, "mouse_click") as click:
            with pytest.raises(ValueError):
                xdotool.mouse_wheel(button)

        click.assert_not_called()
