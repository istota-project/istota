"""Browsing helpers: human simulation, captcha detection, content extraction."""

import logging
import math
import os
import random
import time
from urllib.parse import urlparse

from xdotool import mouse_click, mouse_location, mouse_move, xdo, xdo_key

log = logging.getLogger(__name__)

# Phrases a challenge says in its own voice. No ordinary page has a reason to
# address its reader this way, so these decide at any length.
CHALLENGE_PHRASES = (
    "please verify you are a human",
    "please complete the security check",
)

# The same intent, except that each of these is also an ordinary English
# fragment: a transcript carries "just a moment", a support article carries
# "checking your browser", and a page explaining captchas carries "verify you
# are human" -- which is the very page class ISSUE-518 is about. They keep the
# length guard they have always had, so behaviour on them is unchanged.
#
# ISSUE-518 asked for the guard to come off these too, on the ground that they
# carry no false-positive risk at any length. They do carry one. The guard
# costs little now that the frame arm recognises the Cloudflare widget on its
# own (ISSUE-526), and a false positive here discards the page in silence,
# which is the failure the issue is about -- so the trade runs the wrong way.
GUARDED_CHALLENGE_PHRASES = (
    "just a moment",
    "checking your browser",
    "verify you are human",
)

# A challenge page is short. What the guard could never do is separate a real
# challenge from a short page *about* challenges, since both are short -- which
# is why the words below no longer reach it.
CHALLENGE_BODY_MAX_CHARS = 2000

# Words naming the *subject* rather than the state. A page listing captcha
# demos says the first three and is not a challenge: ISSUE-518's report is
# nopecha.com/demo, 1153 characters, no challenge frame at all, reported as
# `captcha` with its title, text and links discarded and the model told to ask
# a human to solve something that was not there.
#
# They decide nothing now. What recognises a challenge that does not announce
# itself in words is the frame arm, which since ISSUE-526 sees the Cloudflare
# managed-challenge widget these words used to stand in for. Kept as a list
# because `detect_captcha` still logs when one turns up, which is what makes a
# page the retired rule would have called a captcha auditable from the log.
CAPTCHA_SUBJECT_WORDS = (
    "recaptcha",
    "hcaptcha",
    "captcha",
    "bot detection",
    "access denied",
)

CAPTCHA_FRAME_URLS = [
    "google.com/recaptcha",
    "hcaptcha.com",
    "challenges.cloudflare.com",
]

# Frames whose document is not this page's content: ad exchanges, consent
# managers, analytics beacons and social embeds. On a news front page these are
# most of the frames, so an unfiltered include is worse than dropping them all
# — which is what makes the list part of the feature rather than a tidy-up.
# It doubles as surface reduction: an ad frame's text is classic injection real
# estate, and #516 makes it visible where it was invisible.
# The captcha hosts come along because that challenge is not page content
# either, and `detect_captcha` already owns the verdict about it.
FRAME_NOISE_URLS = CAPTCHA_FRAME_URLS + [
    "doubleclick.net",
    "googlesyndication.com",
    "googletagmanager.com",
    "googleadservices.com",
    "google-analytics.com",
    "googletagservices.com",
    "adnxs.com",
    "adsrvr.org",
    "adform.net",
    "amazon-adsystem.com",
    "casalemedia.com",
    "criteo.com",
    "indexexchange.com",
    "moatads.com",
    "openx.net",
    "outbrain.com",
    "pubmatic.com",
    "quantserve.com",
    "rubiconproject.com",
    "scorecardresearch.com",
    "sharethrough.com",
    "smartadserver.com",
    "taboola.com",
    "teads.tv",
    "yieldmo.com",
    "cookielaw.org",
    "consensu.org",
    "onetrust.com",
    "privacy-mgmt.com",
    "trustarc.com",
    "connect.facebook.net",
    "platform.twitter.com",
]

# A frame smaller than this in either dimension carries nothing a reader wants:
# tracking pixels, 0x0 beacons, and the passive reCAPTCHA badge. The floor is
# deliberately well below a banner ad — separating an ad from a widget is the
# host list's job, and a size rule tight enough to do it would also drop real
# embedded content. `is_blocking_challenge_box` is the precedent for having a
# size rule at all, not for its values: that one is asking whether a challenge
# is in the way, which is a different question and is answered per host.
FRAME_MIN_WIDTH = 100
FRAME_MIN_HEIGHT = 50

# How many frames the walk will *probe*. Measured on a live news page, an
# ad-heavy front page carries well past 30 frames, and probing one costs a CDP
# round trip — so the walk is bounded and the caller is told when the bound bit
# (a census that silently stops counting is the failure this survey exists to
# remove, one level up).
#
# The budget is spent on probes, not on frames seen: `_url_skip_reason` decides
# `nested`, `blank` and `noise` from the URL alone and costs nothing, so a page
# whose first forty frames are ad slots still has its whole budget left for the
# content frame behind them. Counting every frame seen was the first cut, and
# measured against a live page with thirty-odd ad frames it returned
# `found: 0, capped: true` — a correct census of nothing.
MAX_FRAMES_PROBED = 30


def _is_noise_host(url):
    """Is this URL's *host* one of the noise domains?

    Matched on the parsed host and by suffix, not as a substring of the whole
    URL. A substring test is what `detect_captcha` does, and it is right there
    because a false positive costs a captcha warning; here it costs a content
    frame silently leaving the census — the census being the whole product.
    `https://widget.example/embed?ref=taboola.com` is a real frame, not an ad.
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    for pattern in FRAME_NOISE_URLS:
        # Entries are written as hosts, some with a path ("google.com/recaptcha")
        # because they are shared with CAPTCHA_FRAME_URLS, where the path is
        # what distinguishes a challenge from the rest of the domain.
        domain, _, path = pattern.partition("/")
        if host != domain and not host.endswith("." + domain):
            continue
        if path and path not in url:
            continue
        return True
    return False


def _url_skip_reason(frame, main_frame):
    """The half of the verdict that needs no round trip. `(skip, url)`."""
    try:
        url = frame.url or ""
    except Exception:
        return "detached", ""

    # Depth 1 only. `page.frames` is a flat list of the whole tree, so a nested
    # frame is reachable here — it is counted and named rather than descended
    # into, because its <iframe> node lives in a parent frame's document and
    # not in the one being rendered, so there is nowhere to put its content.
    try:
        if frame.parent_frame is not main_frame:
            return "nested", url
    except Exception:
        return "detached", url

    # `about:blank` has no content, and `about:srcdoc` has content that no
    # `<iframe src>` names, so neither can be matched to a node. Counted as
    # skipped rather than as dropped content, which understates a srcdoc page
    # by design: the alternative is a count the splice can never satisfy.
    if not url or url.startswith("about:"):
        return "blank", url

    if _is_noise_host(url):
        return "noise", url

    return None, url


def _element_skip_reason(frame):
    """The half that costs a round trip: is this frame on screen and big enough?"""
    try:
        element = frame.frame_element()
        if not element.is_visible():
            return "hidden"
        box = element.bounding_box()
    except Exception:
        return "detached"

    # A frame the layout has not placed reports no box at all. That is an
    # unknown size rather than a small one, so it stays a candidate.
    if box and (box["width"] < FRAME_MIN_WIDTH or box["height"] < FRAME_MIN_HEIGHT):
        return "small"

    return None


def survey_frames(page, limit=MAX_FRAMES_PROBED):
    """Census of the page's child frames: which carry content, and which don't.

    Returns ``(records, capped)``. Each record is
    ``{"frame": Frame, "url": str, "skip": str | None}``; ``skip`` is None for
    the frames whose content a reader would want. ``capped`` says the walk ran
    out of probe budget, so the count is "N or more" — and the caller has to
    say so even when it found nothing, since "capped at zero" is precisely the
    silent short read this census exists to replace.

    The main frame is never a record — it is the document being rendered.

    Read-only, on purpose. `detect_captcha` already walks `page.frames` and
    takes `frame_element()`; this is the same pattern with a different filter,
    and it writes nothing into the page. The alternative for matching a frame
    to its DOM node is a marker attribute, which is a DOM write a
    MutationObserver sees — see BOT_DETECTION.md for why that is not a trade
    worth making on the read path.

    Never raises: a frame walk that fails must cost the census, never the
    render.
    """
    records = []
    try:
        frames = list(page.frames)
        main_frame = page.main_frame
    except Exception as e:
        log.debug("frame survey unavailable: %s", e)
        return [], False

    probed = 0
    for frame in frames:
        if frame is main_frame:
            continue

        skip, url = _url_skip_reason(frame, main_frame)
        if skip is not None:
            records.append({"frame": frame, "url": url, "skip": skip})
            continue

        if probed >= limit:
            log.info("frame survey stopped after probing %d frames", limit)
            return records, True
        probed += 1
        records.append({
            "frame": frame, "url": url, "skip": _element_skip_reason(frame),
        })

    return records, False


def gauss_clamp(mu, sigma, lo, hi):
    """Gaussian random clamped to [lo, hi]."""
    return max(lo, min(hi, random.gauss(mu, sigma)))


def bezier_points(start, end, num_points=20):
    """Generate points along a quadratic bezier curve."""
    sx, sy = start
    ex, ey = end
    cx = (sx + ex) / 2 + random.uniform(-150, 150)
    cy = (sy + ey) / 2 + random.uniform(-100, 100)
    points = []
    for i in range(num_points + 1):
        t = i / num_points
        x = (1 - t) ** 2 * sx + 2 * (1 - t) * t * cx + t ** 2 * ex
        y = (1 - t) ** 2 * sy + 2 * (1 - t) * t * cy + t ** 2 * ey
        x += random.uniform(-1.5, 1.5)
        y += random.uniform(-1.5, 1.5)
        points.append((x, y))
    return points


def simulate_human_behavior(page):
    """Simulate human-like mouse movements and scrolling after page load.

    Uses OS-level X11 input via xdotool. Designed to mimic real human
    browsing cadence with Gaussian timing and Fitts's Law speed profile.
    """
    try:
        w = int(os.environ.get("SCREEN_WIDTH", "1440"))
        h = int(os.environ.get("SCREEN_HEIGHT", "900"))

        time.sleep(gauss_clamp(0.4, 0.2, 0.1, 0.8))

        cur_x = random.uniform(w * 0.3, w * 0.7)
        cur_y = random.uniform(h * 0.2, h * 0.5)
        xdo("mousemove", "--screen", "0", str(int(cur_x)), str(int(cur_y)))

        for _ in range(random.randint(2, 3)):
            target_x = random.uniform(50, w - 50)
            target_y = random.uniform(50, h - 50)
            num_pts = random.randint(15, 30)
            points = bezier_points(
                (cur_x, cur_y), (target_x, target_y), num_points=num_pts,
            )
            for i, (px, py) in enumerate(points):
                xdo(
                    "mousemove", "--screen", "0",
                    str(int(px)), str(int(py)),
                )
                progress = i / max(len(points) - 1, 1)
                speed = 0.008 + 0.014 * (1 - math.sin(progress * math.pi))
                time.sleep(gauss_clamp(speed, speed * 0.3, 0.005, 0.04))
            cur_x, cur_y = target_x, target_y
            time.sleep(gauss_clamp(0.3, 0.15, 0.1, 0.7))
            if random.random() < 0.2:
                time.sleep(gauss_clamp(0.5, 0.3, 0.2, 1.2))

        scroll_steps = random.randint(1, 3)
        for i in range(scroll_steps):
            xdo_key("Page_Down")
            time.sleep(gauss_clamp(1.0, 0.4, 0.5, 2.0))

        if random.random() < 0.4:
            xdo_key("Page_Up")
            time.sleep(gauss_clamp(0.8, 0.3, 0.4, 1.5))

        target_x = random.uniform(100, w - 100)
        target_y = random.uniform(50, h * 0.4)
        num_pts = random.randint(10, 18)
        points = bezier_points(
            (cur_x, cur_y), (target_x, target_y), num_points=num_pts,
        )
        for i, (px, py) in enumerate(points):
            xdo(
                "mousemove", "--screen", "0",
                str(int(px)), str(int(py)),
            )
            progress = i / max(len(points) - 1, 1)
            speed = 0.008 + 0.014 * (1 - math.sin(progress * math.pi))
            time.sleep(gauss_clamp(speed, speed * 0.3, 0.005, 0.04))
    except Exception:
        pass


def human_move_to(target_x, target_y, settle_s=None):
    """Move the pointer to an X11 screen point along a human-ish arc.

    Same Bezier-plus-jitter path simulate_human_behavior() uses, aimed at a
    point instead of at random. The approach matters as much as the click:
    a pointer that teleports to a checkbox and fires has no movement history,
    and the movement history is what the challenge is watching.

    Returns whether the landing move put the pointer on the target. Only that
    last move is answered for: the points before it are jitter on the way,
    and whatever blocks one of them blocks the landing move too, which is the
    one a click would be sent from.
    """
    start = mouse_location() or (target_x, target_y - 200)
    num_pts = random.randint(12, 22)
    points = bezier_points(start, (target_x, target_y), num_points=num_pts)
    for i, (px, py) in enumerate(points):
        mouse_move(px, py)
        progress = i / max(len(points) - 1, 1)
        speed = 0.008 + 0.014 * (1 - math.sin(progress * math.pi))
        time.sleep(gauss_clamp(speed, speed * 0.3, 0.005, 0.04))
    # Land exactly on target: the path carries +-1.5px of jitter, and the
    # Cloudflare checkbox is about 24px across.
    landed = mouse_move(target_x, target_y)
    if settle_s is None:
        settle_s = gauss_clamp(0.35, 0.15, 0.15, 0.8)
    time.sleep(settle_s)
    return landed


def human_click_at(target_x, target_y, button=1):
    """Approach an X11 screen point and click it.

    Returns False without pressing when the pointer did not reach the point.
    mouse_click() presses wherever the pointer currently is, so a press after
    a move that did not happen lands on whatever the previous action was
    aimed at -- and the caller would report it at the point it asked for.
    """
    if not human_move_to(target_x, target_y):
        return False
    mouse_click(button=button)
    time.sleep(gauss_clamp(0.25, 0.1, 0.1, 0.5))
    return True


def challenge_boxes(page):
    """Bounding boxes of visible captcha/challenge iframes, in CSS pixels.

    Same frame walk detect_captcha() does, reporting geometry instead of a
    verdict. It exists because the Cloudflare interstitial's checkbox is
    inside a closed shadow root in a cross-origin frame -- no selector reaches
    it and no querySelector sees it, but the frame element itself has a box,
    and the checkbox sits at a fixed inset within it.
    """
    boxes = []
    for frame in page.frames:
        if not any(u in frame.url for u in CAPTCHA_FRAME_URLS):
            continue
        try:
            el = frame.frame_element()
            if not el.is_visible():
                continue
            box = el.bounding_box()
        except Exception:
            continue
        if not box:
            continue
        boxes.append({
            "url": frame.url,
            "x": box["x"], "y": box["y"],
            "width": box["width"], "height": box["height"],
        })
    return boxes


# The Cloudflare managed-challenge widget draws its checkbox at a fixed inset
# from the frame's left edge, vertically centred. Measured against a 300x65
# frame whose left edge was at CSS x=271.5: the checkbox spanned roughly
# x 281-306, so its centre sits 22px in. An inset rather than a ratio because
# the widget does not scale with its frame -- a wider frame moves the label,
# not the checkbox -- and the box is about 24px across, so the inset has
# around 10px of slack either way.
CF_CHECKBOX_INSET_X = 22
CF_CHECKBOX_MIN_HEIGHT = 40
CLOUDFLARE_FRAME_URL = "challenges.cloudflare.com"
# The reCAPTCHA/hCaptcha passive badge: visible, on the page, and not in the
# way. A frame on those hosts counts as a challenge only once it is larger
# than this in one dimension.
PASSIVE_BADGE_MAX_WIDTH = 400
PASSIVE_BADGE_MAX_HEIGHT = 200


def is_blocking_challenge_box(url, box):
    """Whether this challenge frame is in the way, or a passive badge.

    Per host, because the two families draw their passive artifact at
    different sizes and one threshold cannot separate both:

    * Cloudflare's invisible Turnstile beacon has almost no height, while the
      managed-challenge widget -- the thing a person has to click -- is about
      300x65. Height alone separates them, and CF_CHECKBOX_MIN_HEIGHT is the
      measured line `cloudflare_checkbox_point` has pressed against since
      before this function existed.
    * reCAPTCHA's passive badge is about 256x60 and is *not* blocking, so
      Cloudflare's rule applied to that host would report a challenge on
      every page carrying reCAPTCHA v3. Those hosts keep the 400x200 test.

    The two thresholds used to disagree about the same frame, which is
    ISSUE-526: a 300x65 Cloudflare widget was clickable according to
    `cloudflare_checkbox_point` and invisible to `detect_captcha`, so a page
    whose only challenge was that widget was caught only when its *text*
    happened to trip a pattern -- and ISSUE-518 is why the text arm can no
    longer stand in for the frame.

    A frame with no measurable box is blocking. That is what the frame walk
    already did, and a challenge that cannot be measured is the wrong one to
    wave through.
    """
    if not box:
        return True
    if CLOUDFLARE_FRAME_URL in url:
        return box["height"] >= CF_CHECKBOX_MIN_HEIGHT
    return (
        box["width"] >= PASSIVE_BADGE_MAX_WIDTH
        or box["height"] >= PASSIVE_BADGE_MAX_HEIGHT
    )


def cloudflare_checkbox_point(page):
    """Where the Cloudflare checkbox is, in CSS pixels, or None.

    Returns (x, y) for the first challenges.cloudflare.com frame large enough
    to be the interstitial widget rather than an invisible Turnstile beacon.
    """
    for box in challenge_boxes(page):
        if CLOUDFLARE_FRAME_URL not in box["url"]:
            continue
        if not is_blocking_challenge_box(box["url"], box):
            continue
        return (
            box["x"] + CF_CHECKBOX_INSET_X,
            box["y"] + box["height"] / 2,
        )
    return None


def wait_for_datadome(page, timeout_ms=15000):
    """Wait for DataDome challenge to resolve if present."""
    try:
        is_challenge = page.evaluate(
            "document.documentElement.outerHTML.indexOf('captcha-delivery') > -1"
        )
        if not is_challenge:
            return
        log.info("DataDome challenge detected -- waiting for resolution")
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            time.sleep(1)
            try:
                still = page.evaluate(
                    "document.documentElement.outerHTML.indexOf('captcha-delivery') > -1"
                )
                if not still:
                    log.info("DataDome challenge resolved")
                    return
            except Exception:
                return
        log.warning(
            "DataDome challenge did not resolve within %dms", timeout_ms,
        )
    except Exception:
        pass


def detect_captcha(page):
    """Whether this page is a challenge the caller has to clear.

    Two arms, and since ISSUE-518 they no longer share a rule.

    The **text** arm matches phrases, never product names. A page is a
    challenge because of what it says about the reader's situation, not
    because it mentions captchas -- and the bare words `captcha`,
    `recaptcha` and `hcaptcha` deciding it meant an ordinary page listing
    captcha demos came back as `status: captcha` with its content thrown
    away and the model told to hand the user a VNC URL.

    The **frame** arm is signature-based: a frame on a known challenge host,
    visible, and big enough to be in the way. Since ISSUE-526 that includes
    the Cloudflare managed-challenge widget, which is what lets the subject
    words stop deciding anything -- before that they were the only thing
    catching such a page, so the two issues pull the same constant in
    opposite directions and this one had to land second.
    """
    try:
        body_text = page.inner_text("body").lower()
    except Exception:
        body_text = ""

    for phrase in CHALLENGE_PHRASES:
        if phrase in body_text:
            log.info(
                "Captcha detected: phrase=%r, body_len=%d",
                phrase, len(body_text),
            )
            return True

    for phrase in GUARDED_CHALLENGE_PHRASES:
        if phrase not in body_text:
            continue
        if len(body_text) < CHALLENGE_BODY_MAX_CHARS:
            log.info(
                "Captcha detected: phrase=%r, body_len=%d",
                phrase, len(body_text),
            )
            return True
        log.debug(
            "Challenge phrase %r found but page has %d chars -- reading it "
            "as prose", phrase, len(body_text),
        )

    for frame in page.frames:
        for url_pattern in CAPTCHA_FRAME_URLS:
            if url_pattern in frame.url:
                try:
                    el = frame.frame_element()
                    if not el.is_visible():
                        log.debug(
                            "Captcha iframe hidden, ignoring: %r", frame.url,
                        )
                        continue
                    box = el.bounding_box()
                    if not is_blocking_challenge_box(frame.url, box):
                        log.debug(
                            "Challenge iframe not blocking (%dx%d), likely a "
                            "passive badge: %r",
                            box["width"], box["height"], frame.url,
                        )
                        continue
                except Exception:
                    pass
                log.info("Captcha detected: iframe=%r", frame.url)
                return True

    subject = next((w for w in CAPTCHA_SUBJECT_WORDS if w in body_text), None)
    if subject:
        # The page ISSUE-518 is about, seen from the other side: this is the
        # verdict the retired rule would have returned. Logged rather than
        # decided, so an operator reading the container log can still tell a
        # page the old rule got wrong from one it never looked at.
        log.debug(
            "Page mentions %r and shows no challenge -- not a captcha "
            "(body_len=%d)", subject, len(body_text),
        )
    return False


DEFAULT_TEXT_MAX_CHARS = 50000
DEFAULT_MAX_LINKS = 100
# Hard ceilings. Like the /extract and /render budgets in browse_api, a
# caller-supplied value is a ceiling it may *lower*, never one it may raise
# without bound: the response goes straight into an agent's context. Clamping
# also disarms a negative budget, which slices from the wrong end — max_links=-5
# would return every anchor but the last five, i.e. more than the default.
MAX_TEXT_MAX_CHARS = 500000
MAX_MAX_LINKS = 2000


def extract_page_content(page, max_chars=None, max_links=None):
    """Extract text content, title, and links from a page.

    Both budgets are caller-overridable within the ceilings above. The defaults
    suit an article but clip a link-dense hub, which is half of why an index
    page used to read as dead (ISSUE-192). For structure-preserving output
    prefer render.to_markdown — inner_text drops every href, and this flat link
    list drops every position.
    """
    title = page.title()
    max_chars = max(1, min(int(max_chars or DEFAULT_TEXT_MAX_CHARS), MAX_TEXT_MAX_CHARS))
    max_links = max(1, min(int(max_links or DEFAULT_MAX_LINKS), MAX_MAX_LINKS))

    try:
        text = page.inner_text("body")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n[Content truncated at {max_chars} characters]"
    except Exception:
        text = ""

    links = []
    try:
        anchors = page.query_selector_all("a[href]")
        for a in anchors[:max_links]:
            href = a.get_attribute("href")
            link_text = a.inner_text().strip()
            if href and not href.startswith(("javascript:", "#", "mailto:")):
                links.append({"text": link_text[:100], "href": href})
    except Exception:
        pass

    return {
        "title": title,
        "url": page.url,
        "text": text,
        "links": links,
    }
