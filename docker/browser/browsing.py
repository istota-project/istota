"""Browsing helpers: human simulation, captcha detection, content extraction."""

import logging
import math
import os
import random
import time

from xdotool import xdo, xdo_key

log = logging.getLogger(__name__)

# Captcha detection patterns
CAPTCHA_PATTERNS = [
    "just a moment",
    "checking your browser",
    "verify you are human",
    "please verify you are a human",
    "recaptcha",
    "hcaptcha",
    "captcha",
    "bot detection",
    "access denied",
    "please complete the security check",
]

CAPTCHA_FRAME_URLS = [
    "google.com/recaptcha",
    "hcaptcha.com",
    "challenges.cloudflare.com",
]


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
    """Check if the page shows a captcha challenge."""
    try:
        body_text = page.inner_text("body").lower()
    except Exception:
        body_text = ""

    for pattern in CAPTCHA_PATTERNS:
        if pattern in body_text:
            if len(body_text) < 2000:
                log.info(
                    "Captcha detected: pattern=%r, body_len=%d",
                    pattern, len(body_text),
                )
                return True
            else:
                log.debug(
                    "Captcha pattern %r found but page has %d chars",
                    pattern, len(body_text),
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
                except Exception:
                    pass
                log.info("Captcha detected: iframe=%r", frame.url)
                return True

    return False


def extract_page_content(page):
    """Extract text content, title, and links from a page."""
    title = page.title()

    try:
        text = page.inner_text("body")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        text = "\n".join(lines)
        if len(text) > 50000:
            text = text[:50000] + "\n\n[Content truncated at 50000 characters]"
    except Exception:
        text = ""

    links = []
    try:
        anchors = page.query_selector_all("a[href]")
        for a in anchors[:100]:
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
