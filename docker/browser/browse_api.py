"""Browser automation API running inside Docker container.

Provides HTTP endpoints for Playwright-based browsing with stealth
anti-fingerprinting, persistent cookie/session storage, autoconsent
cookie banner handling, and VNC captcha fallback.
"""

import atexit
import logging
import os
import random
import subprocess
import threading
import time
import uuid

from flask import Flask, Response, jsonify, request
from patchright.sync_api import sync_playwright

app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Session management
_sessions = {}  # id -> {page, created_at}
_sessions_lock = threading.Lock()
SESSION_TTL = 600  # 10 minutes
MAX_SESSIONS = int(os.environ.get("MAX_BROWSER_SESSIONS", "3"))

# Playwright instance (started once)
_playwright = None
_context = None  # Single persistent context for cookie/session storage

PROFILE_DIR = os.environ.get("BROWSER_PROFILE_DIR", "/data/browser-profile")


# Captcha detection patterns
CAPTCHA_PATTERNS = [
    # Cloudflare
    "just a moment",
    "checking your browser",
    "verify you are human",
    "please verify you are a human",
    # reCAPTCHA / hCaptcha
    "recaptcha",
    "hcaptcha",
    # Generic
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

# Autoconsent cookie banner handling
AUTOCONSENT_SCRIPT_PATH = "/app/vendor/autoconsent.playwright.js"
_autoconsent_init_script = None


def _build_autoconsent_init_script():
    """Build a self-contained init script that handles cookie consent autonomously.

    Wraps the autoconsent IIFE bundle with a message handler so it runs
    entirely in-page without needing a Python-side message loop.
    """
    global _autoconsent_init_script
    if _autoconsent_init_script is not None:
        return _autoconsent_init_script

    try:
        with open(AUTOCONSENT_SCRIPT_PATH) as f:
            bundle = f.read()
    except FileNotFoundError:
        log.warning("Autoconsent script not found at %s", AUTOCONSENT_SCRIPT_PATH)
        _autoconsent_init_script = ""
        return ""

    # Wrap the bundle: provide autoconsentSendMessage as the message handler,
    # then inject the bundle which will call it. The handler auto-responds to
    # init and eval messages so the whole flow runs in-page.
    _autoconsent_init_script = """
    (function() {
        // Message handler that autoconsent calls to communicate
        window.autoconsentSendMessage = function(msg) {
            var type = msg.type;
            if (type === 'init') {
                // Respond with config to start detection
                setTimeout(function() {
                    if (window.autoconsentReceiveMessage) {
                        window.autoconsentReceiveMessage({
                            type: 'initResp',
                            config: {
                                enabled: true,
                                autoAction: 'optOut',
                                disabledCmps: [],
                                enablePrehide: true,
                                enableCosmeticRules: true,
                                detectRetries: 20,
                                isMainWorld: true,
                                enableFilterlist: false
                            }
                        });
                    }
                }, 0);
            } else if (type === 'eval') {
                // Evaluate JS and respond
                var id = msg.id;
                var result = false;
                try { result = !!eval(msg.code); } catch(e) {}
                setTimeout(function() {
                    if (window.autoconsentReceiveMessage) {
                        window.autoconsentReceiveMessage({
                            type: 'evalResp',
                            id: id,
                            result: result
                        });
                    }
                }, 0);
            }
            // Other message types (cmpDetected, optOutResult, etc.) are just logged
        };

        // Inject the autoconsent bundle
        """ + bundle + """
    })();
    """
    return _autoconsent_init_script


# Comprehensive stealth evasion script injected before any page JS runs.
# Based on puppeteer-extra-plugin-stealth evasions, adapted for Playwright.
_STEALTH_SCRIPT = r"""
(() => {
    // === UTILITY: Native function toString spoofing ===
    // All patched functions must appear native to toString() inspection.
    const _origToString = Function.prototype.toString;
    const _patchedFns = new Map();
    Function.prototype.toString = function() {
        if (_patchedFns.has(this)) return _patchedFns.get(this);
        return _origToString.call(this);
    };
    // Mark our patched toString as native too
    _patchedFns.set(Function.prototype.toString, 'function toString() { [native code] }');

    function _makeNative(fn, name) {
        _patchedFns.set(fn, 'function ' + (name || fn.name || '') + '() { [native code] }');
    }

    // === 1. navigator.webdriver ===
    // Handled natively by Patchright (avoids Runtime.enable) and
    // --disable-blink-features=AutomationControlled. No JS override needed —
    // JS-level Object.defineProperty creates a detectable non-native getter.

    // === 2. window.chrome ===
    if (!window.chrome) {
        const chrome = {
            app: {
                isInstalled: false,
                InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
                RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
                getDetails: function getDetails() { return null; },
                getIsInstalled: function getIsInstalled() { return false; },
            },
            csi: function csi() { return {}; },
            loadTimes: function loadTimes() {
                return {
                    commitLoadTime: Date.now() / 1000 - 1.5,
                    connectionInfo: 'http/1.1',
                    finishDocumentLoadTime: Date.now() / 1000 - 0.5,
                    finishLoadTime: Date.now() / 1000 - 0.2,
                    firstPaintAfterLoadTime: 0,
                    firstPaintTime: Date.now() / 1000 - 0.8,
                    navigationType: 'Other',
                    npnNegotiatedProtocol: 'unknown',
                    requestTime: Date.now() / 1000 - 2,
                    startLoadTime: Date.now() / 1000 - 1.8,
                    wasAlternateProtocolAvailable: false,
                    wasFetchedViaSpdy: false,
                    wasNpnNegotiated: false,
                };
            },
            runtime: {
                OnInstalledReason: { CHROME_UPDATE: 'chrome_update', INSTALL: 'install', SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update' },
                OnRestartRequiredReason: { APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' },
                PlatformArch: { ARM: 'arm', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
                PlatformNaclArch: { ARM: 'arm', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
                PlatformOs: { ANDROID: 'android', CROS: 'cros', LINUX: 'linux', MAC: 'mac', OPENBSD: 'openbsd', WIN: 'win' },
                RequestUpdateCheckStatus: { NO_UPDATE: 'no_update', THROTTLED: 'throttled', UPDATE_AVAILABLE: 'update_available' },
                connect: function connect() {
                    return { onMessage: { addListener: function() {} }, postMessage: function() {}, disconnect: function() {} };
                },
                sendMessage: function sendMessage() {},
                id: undefined,
            },
        };
        // Mark functions as native
        _makeNative(chrome.app.getDetails, 'getDetails');
        _makeNative(chrome.app.getIsInstalled, 'getIsInstalled');
        _makeNative(chrome.csi, 'csi');
        _makeNative(chrome.loadTimes, 'loadTimes');
        _makeNative(chrome.runtime.connect, 'connect');
        _makeNative(chrome.runtime.sendMessage, 'sendMessage');
        window.chrome = chrome;
    }

    // === 3. Permissions ===
    const _origQuery = window.Permissions && Permissions.prototype.query;
    if (_origQuery) {
        const _newQuery = function query(parameters) {
            if (parameters && parameters.name === 'notifications') {
                return Promise.resolve({ state: Notification.permission });
            }
            return _origQuery.call(this, parameters);
        };
        _makeNative(_newQuery, 'query');
        Permissions.prototype.query = _newQuery;
    }

    // === 4. Plugins & MimeTypes ===
    // Real Google Chrome in headed mode natively provides 5 PDF plugins.
    // No JS override needed — Object.defineProperty creates detectable
    // non-native property descriptors on navigator.plugins/mimeTypes.

    // === 5. Languages ===
    // Handled natively via Chrome --lang=en-US,en flag and Patchright locale.
    // No JS override needed — creates detectable non-native getter.

    // === 6. navigator.vendor ===
    if (navigator.vendor !== 'Google Inc.') {
        Object.defineProperty(navigator, 'vendor', {
            get: () => 'Google Inc.', configurable: true,
        });
    }

    // === 7. Platform ===
    // Native on Linux x86_64 — no override needed.

    // === 8. Hardware concurrency ===
    // Native value from host CPU count. No JS override needed —
    // creates detectable non-native getter.

    // === 9. Device memory ===
    // Native value from host RAM (bucketed: 0.25-8). Hosts with >=8GB
    // report 8 natively. No JS override needed.

    // === 10. Network information ===
    if (!navigator.connection) {
        Object.defineProperty(navigator, 'connection', {
            get: () => ({
                effectiveType: '4g', rtt: 50, downlink: 10, saveData: false,
                onchange: null, ontypechange: null,
            }),
            configurable: true,
        });
    }

    // === 11. WebGL vendor/renderer (hide SwiftShader) ===
    const WEBGL_VENDOR = 'Google Inc. (Intel)';
    const WEBGL_RENDERER = 'ANGLE (Intel, Mesa Intel(R) UHD Graphics 630, OpenGL 4.6)';
    for (const ctx of [WebGLRenderingContext, typeof WebGL2RenderingContext !== 'undefined' ? WebGL2RenderingContext : null]) {
        if (!ctx) continue;
        const orig = ctx.prototype.getParameter;
        ctx.prototype.getParameter = function getParameter(param) {
            if (param === 0x9245) return WEBGL_VENDOR;   // UNMASKED_VENDOR_WEBGL
            if (param === 0x9246) return WEBGL_RENDERER;  // UNMASKED_RENDERER_WEBGL
            return orig.call(this, param);
        };
        _makeNative(ctx.prototype.getParameter, 'getParameter');
    }

    // === 12. Screen and window dimensions (match MacBook display) ===
    // Report screen as 1440x900 (MacBook Pro 14" default scaled resolution)
    Object.defineProperty(screen, 'width', { get: () => 1440, configurable: true });
    Object.defineProperty(screen, 'height', { get: () => 900, configurable: true });
    Object.defineProperty(screen, 'availWidth', { get: () => 1440, configurable: true });
    Object.defineProperty(screen, 'availHeight', { get: () => 875, configurable: true }); // minus macOS menu bar
    // Outer dimensions should be slightly larger than inner (window chrome)
    if (window.outerWidth === 0 || window.outerHeight === 0) {
        Object.defineProperty(window, 'outerWidth', {
            get: () => window.innerWidth, configurable: true,
        });
        Object.defineProperty(window, 'outerHeight', {
            get: () => window.innerHeight + 85, configurable: true,
        });
    }
    // Window not at 0,0 — offset like a macOS window
    if (window.screenX === 0 && window.screenY === 0) {
        Object.defineProperty(window, 'screenX', { get: () => 76, configurable: true });
        Object.defineProperty(window, 'screenY', { get: () => 25, configurable: true });
    }

    // === 13. Broken image dimensions ===
    // Headless Chrome returns 0x0 for broken images; real Chrome returns 16x16.
    ['naturalWidth', 'naturalHeight', 'width', 'height'].forEach(prop => {
        const origDesc = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, prop);
        if (origDesc && origDesc.get) {
            const origGet = origDesc.get;
            Object.defineProperty(HTMLImageElement.prototype, prop, {
                get: function() {
                    // If image failed to load and would return 0, return 16 (Chrome default placeholder)
                    const val = origGet.call(this);
                    if (val === 0 && !this.complete) return 0; // Still loading
                    if (val === 0 && this.complete && !this.naturalWidth) {
                        return (prop === 'naturalWidth' || prop === 'width') ? 16 : 16;
                    }
                    return val;
                },
                configurable: true,
            });
        }
    });

    // === 14. Media codecs ===
    const _origCanPlayType = HTMLMediaElement.prototype.canPlayType;
    HTMLMediaElement.prototype.canPlayType = function canPlayType(type) {
        var result = _origCanPlayType.call(this, type);
        if (result === '') {
            var t = type.toLowerCase();
            // Video codecs
            if (t.includes('video/mp4')) return 'probably';
            if (t.includes('video/webm') && t.includes('vp8')) return 'probably';
            if (t.includes('video/webm') && t.includes('vp9')) return 'probably';
            if (t.includes('video/webm')) return 'maybe';
            if (t.includes('video/ogg')) return 'probably';
            // Audio codecs
            if (t.includes('audio/mpeg') || t.includes('audio/mp3')) return 'probably';
            if (t.includes('audio/mp4') || t.includes('audio/x-m4a') || t.includes('audio/m4a')) return 'maybe';
            if (t.includes('audio/aac')) return 'probably';
            if (t.includes('audio/ogg') || t.includes('audio/vorbis')) return 'probably';
            if (t.includes('audio/wav') || t.includes('audio/wave')) return 'probably';
            if (t.includes('audio/webm')) return 'probably';
            if (t.includes('audio/flac')) return 'probably';
        }
        return result;
    };
    _makeNative(HTMLMediaElement.prototype.canPlayType, 'canPlayType');

    // === 15. Iframe contentWindow ===
    // Prevent detection via srcdoc iframe trick where contentWindow.chrome
    // is compared to the parent window.chrome
    try {
        const _origCreate = document.createElement.bind(document);
        const _origSrcdocDesc = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'srcdoc');
        if (_origSrcdocDesc && _origSrcdocDesc.set) {
            const _origSrcdocSet = _origSrcdocDesc.set;
            Object.defineProperty(HTMLIFrameElement.prototype, 'srcdoc', {
                ...(_origSrcdocDesc),
                set: function(val) {
                    _origSrcdocSet.call(this, val);
                    // After srcdoc is set, the iframe contentWindow should have chrome
                    try {
                        const w = this.contentWindow;
                        if (w && !w.chrome) {
                            w.chrome = window.chrome;
                        }
                    } catch(e) {}
                },
            });
        }
    } catch(e) {}

    // === 16. console.debug ===
    // Ensure it looks native (Patchright avoids Runtime.enable at the
    // protocol level so no console.debug interception needed).
    if (typeof console !== 'undefined' && console.debug) {
        _makeNative(console.debug, 'debug');
    }

    // === 17. Error stack traces ===
    // Remove __puppeteer_evaluation_script__ and playwright-related URLs from stacks
    const _origErrorStack = Object.getOwnPropertyDescriptor(Error.prototype, 'stack');
    if (_origErrorStack && _origErrorStack.get) {
        Object.defineProperty(Error.prototype, 'stack', {
            get: function() {
                const stack = _origErrorStack.get.call(this);
                if (typeof stack === 'string') {
                    return stack
                        .replace(/__playwright_evaluation_script__/g, '')
                        .replace(/__puppeteer_evaluation_script__/g, '');
                }
                return stack;
            },
            set: function(val) {
                if (_origErrorStack.set) _origErrorStack.set.call(this, val);
            },
            configurable: true,
        });
    }

    // === 18. Prevent Notification.permission detection issues ===
    // Ensure Notification constructor looks native
    if (typeof Notification !== 'undefined') {
        _makeNative(Notification, 'Notification');
    }

    // === 19. Battery API ===
    // Ensure getBattery returns realistic values
    if (navigator.getBattery) {
        const _origGetBattery = navigator.getBattery.bind(navigator);
        navigator.getBattery = function getBattery() {
            return _origGetBattery().then(battery => {
                // If charging level is exactly 1 with no events, it looks headless
                // Real batteries have slightly less than 1.0
                return battery;
            });
        };
        _makeNative(navigator.getBattery, 'getBattery');
    }

    // === 20. Clean up Playwright/Puppeteer globals ===
    // Automation tools inject detectable global variables. Detection checks
    // whether these properties exist on window (via hasOwnProperty or 'in'),
    // not just their values. So we must delete them WITHOUT re-defining traps,
    // since Object.defineProperty creates an own property that's detectable.
    // Patchright 1.50.0 uses route-based injection and doesn't set these
    // globals in the main world, so deletion alone is sufficient.
    const _playwrightGlobals = [
        '__pwInitScripts', '__playwright__binding__', '__playwright_evaluation_script__',
        '__puppeteer_evaluation_script__', '__webDriverValue',
    ];
    for (const prop of _playwrightGlobals) {
        try { delete window[prop]; } catch(e) {}
    }

    // === 20b. Remove injected <script> elements from DOM ===
    // Patchright injects init scripts as <script class="injected-playwright-init-script-{hex}">
    // elements in the HTML. These are cleaned up after DOMContentLoaded by Patchright,
    // but detection scripts run before that. Remove them immediately.
    function _removeInjectedScripts() {
        try {
            const scripts = document.querySelectorAll('script[class*="playwright"], script[class*="puppeteer"]');
            scripts.forEach(s => s.remove());
        } catch(e) {}
    }
    // Run now (if DOM is available during synchronous script execution)
    _removeInjectedScripts();
    // Also run when the document is ready (catches elements added after this script)
    if (document.readyState === 'loading') {
        // Use a MutationObserver to catch script elements as they're parsed
        try {
            const _obs = new MutationObserver((mutations) => {
                for (const m of mutations) {
                    for (const node of m.addedNodes) {
                        if (node.nodeType === 1 && node.tagName === 'SCRIPT' && node.className &&
                            (node.className.includes('playwright') || node.className.includes('puppeteer'))) {
                            node.remove();
                        }
                    }
                }
            });
            _obs.observe(document.documentElement || document, { childList: true, subtree: true });
            // Stop observing after DOM is loaded (cleanup)
            document.addEventListener('DOMContentLoaded', () => { _obs.disconnect(); _removeInjectedScripts(); }, { once: true });
        } catch(e) {}
    }

    // === 21. Consistent Web Worker values ===
    // Inject WebGL vendor/renderer patch into worker contexts to hide SwiftShader.
    // Navigator properties (hardwareConcurrency, deviceMemory, platform, languages,
    // webdriver) no longer need worker patches — they use native values in both
    // main context and workers, so they're inherently consistent.
    const _OrigWorker = window.Worker;
    const _workerPatch = `
        // WebGL vendor/renderer must match main context (hides SwiftShader)
        if (typeof WebGLRenderingContext !== 'undefined') {
            const _origGetParam = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(p) {
                if (p === 0x9245) return 'Google Inc. (Intel)';
                if (p === 0x9246) return 'ANGLE (Intel, Mesa Intel(R) UHD Graphics 630, OpenGL 4.6)';
                return _origGetParam.call(this, p);
            };
        }
        if (typeof WebGL2RenderingContext !== 'undefined') {
            const _origGetParam2 = WebGL2RenderingContext.prototype.getParameter;
            WebGL2RenderingContext.prototype.getParameter = function(p) {
                if (p === 0x9245) return 'Google Inc. (Intel)';
                if (p === 0x9246) return 'ANGLE (Intel, Mesa Intel(R) UHD Graphics 630, OpenGL 4.6)';
                return _origGetParam2.call(this, p);
            };
        }
    `;
    window.Worker = function Worker(url, opts) {
        const urlStr = (url instanceof URL) ? url.href : String(url);
        const isModule = opts && opts.type === 'module';
        // For blob: URLs (common in bot detectors that inline worker code)
        if (urlStr.startsWith('blob:')) {
            try {
                const xhr = new XMLHttpRequest();
                xhr.open('GET', urlStr, false);
                xhr.send();
                if (xhr.status === 200) {
                    const patched = _workerPatch + '\n' + xhr.responseText;
                    const blob = new Blob([patched], { type: 'application/javascript' });
                    const newOpts = opts ? Object.assign({}, opts) : {};
                    // Patched blob is classic JS even if original was module
                    delete newOpts.type;
                    return new _OrigWorker(URL.createObjectURL(blob), newOpts);
                }
            } catch(e) {}
        }
        // For regular URLs
        if (typeof url === 'string' || (url instanceof URL)) {
            try {
                if (isModule) {
                    // Module workers don't support importScripts; prepend patch
                    // then dynamically import the original URL
                    const wrapper = _workerPatch + '\nimport("' + urlStr.replace(/"/g, '\\"') + '");';
                    const blob = new Blob([wrapper], { type: 'application/javascript' });
                    return new _OrigWorker(URL.createObjectURL(blob), opts);
                } else {
                    const wrapper = _workerPatch + '\nimportScripts("' + urlStr.replace(/"/g, '\\"') + '");';
                    const blob = new Blob([wrapper], { type: 'application/javascript' });
                    return new _OrigWorker(URL.createObjectURL(blob), opts);
                }
            } catch(e) {}
        }
        return new _OrigWorker(url, opts);
    };
    window.Worker.prototype = _OrigWorker.prototype;
    Object.defineProperty(window.Worker, 'name', { value: 'Worker' });
    _makeNative(window.Worker, 'Worker');
})();
"""


def _bezier_points(start, end, num_points=20):
    """Generate points along a quadratic bezier curve with a random control point."""
    sx, sy = start
    ex, ey = end
    # Random control point offset for natural-looking curve
    cx = (sx + ex) / 2 + random.uniform(-150, 150)
    cy = (sy + ey) / 2 + random.uniform(-100, 100)
    points = []
    for i in range(num_points + 1):
        t = i / num_points
        # Quadratic bezier: B(t) = (1-t)^2*P0 + 2*(1-t)*t*P1 + t^2*P2
        x = (1 - t) ** 2 * sx + 2 * (1 - t) * t * cx + t ** 2 * ex
        y = (1 - t) ** 2 * sy + 2 * (1 - t) * t * cy + t ** 2 * ey
        points.append((x, y))
    return points


def _simulate_human_behavior(page):
    """Simulate human-like mouse movements and scrolling after page load.

    Generates realistic browser events (MouseEvent, WheelEvent) through
    Playwright's CDP layer so bot detection scripts see natural interaction.
    """
    try:
        viewport = page.viewport_size
        if viewport:
            w, h = viewport["width"], viewport["height"]
        else:
            # viewport=None means Chrome manages sizing; get from evaluate
            dims = page.evaluate("[window.innerWidth, window.innerHeight]")
            w, h = dims[0], dims[1]
        if w < 100 or h < 100:
            return

        # Start from a realistic position (not 0,0)
        cur_x = random.uniform(w * 0.3, w * 0.7)
        cur_y = random.uniform(h * 0.2, h * 0.5)
        page.mouse.move(cur_x, cur_y)

        # 2-4 mouse movements to random positions along bezier curves
        for _ in range(random.randint(2, 4)):
            target_x = random.uniform(50, w - 50)
            target_y = random.uniform(50, h - 50)
            points = _bezier_points(
                (cur_x, cur_y), (target_x, target_y),
                num_points=random.randint(12, 25),
            )
            for px, py in points:
                page.mouse.move(px, py)
                # Variable micro-delay: faster in middle, slower at start/end
                time.sleep(random.uniform(0.003, 0.015))
            cur_x, cur_y = target_x, target_y
            # Small pause between movements
            time.sleep(random.uniform(0.05, 0.2))

        # Random scroll down (like reading the page)
        scroll_steps = random.randint(2, 5)
        for _ in range(scroll_steps):
            delta = random.randint(80, 300)
            page.mouse.wheel(0, delta)
            time.sleep(random.uniform(0.1, 0.4))

        # Occasionally scroll back up a little
        if random.random() < 0.4:
            page.mouse.wheel(0, -random.randint(50, 150))
            time.sleep(random.uniform(0.1, 0.3))

        # One more mouse movement
        target_x = random.uniform(100, w - 100)
        target_y = random.uniform(50, h * 0.4)
        points = _bezier_points(
            (cur_x, cur_y), (target_x, target_y),
            num_points=random.randint(8, 15),
        )
        for px, py in points:
            page.mouse.move(px, py)
            time.sleep(random.uniform(0.003, 0.012))
    except Exception:
        pass  # Non-critical — don't fail the browse request


def _ensure_browser():
    """Ensure the browser is running and connected, restarting if needed."""
    global _context, _playwright
    if _context is None:
        _init_browser()
        return
    try:
        connected = _context.browser.is_connected() if _context.browser else False
    except Exception:
        connected = False
    if not connected:
        log.warning("Browser disconnected — restarting")
        _restart_browser()


def _restart_browser():
    """Tear down dead browser and re-initialize."""
    global _context, _playwright
    # Clear all sessions — their pages are dead
    with _sessions_lock:
        _sessions.clear()
    # Best-effort cleanup of old instances
    try:
        if _context:
            _context.close()
    except Exception:
        pass
    try:
        if _playwright:
            _playwright.stop()
    except Exception:
        pass
    _context = None
    _playwright = None
    _init_browser()
    log.info("Browser restarted successfully")


def _init_browser():
    """Initialize Playwright with a persistent browser context (once)."""
    global _playwright, _context
    if _context is not None:
        return

    os.makedirs(PROFILE_DIR, exist_ok=True)

    # Xvfb display size (full "screen")
    screen_w = int(os.environ.get("SCREEN_WIDTH", "1440"))
    screen_h = int(os.environ.get("SCREEN_HEIGHT", "900"))

    # Use real Google Chrome instead of Playwright's Chromium for genuine
    # TLS fingerprint (JA3/JA4) and HTTP/2 settings that match real browsers.
    chrome_path = os.environ.get(
        "CHROME_EXECUTABLE", "/usr/bin/google-chrome-stable"
    )

    _playwright = sync_playwright().start()
    _context = _playwright.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        executable_path=chrome_path,
        headless=False,  # Headed mode on Xvfb for VNC captcha solving
        chromium_sandbox=False,  # Docker lacks namespace privileges for Chrome sandbox; non-root user suppresses the --no-sandbox infobar warning
        no_viewport=True,  # Disable fixed viewport emulation; viewport matches window size
        # locale intentionally omitted — Patchright's locale uses CDP
        # Emulation.setLocaleOverride which only affects the main page, not
        # workers. This creates detectable language inconsistency. Instead,
        # --lang flag + container LANG env var set locale natively everywhere.
        timezone_id="America/New_York",
        args=[
            "--disable-blink-features=AutomationControlled",
            "--lang=en-US,en",
            f"--window-size={screen_w},{screen_h}",
            "--window-position=0,0",
            # GPU/canvas support (software rendering in container)
            "--enable-unsafe-swiftshader",
            "--use-gl=swiftshader",
            "--enable-webgl",
            "--enable-features=SharedArrayBuffer",
            # Keep Chrome's async DNS resolver (non-blocking, reads /etc/resolv.conf
            # nameservers including Docker's 127.0.0.11). Only disable DoH which
            # bypasses Docker DNS with hardcoded public resolvers.
            "--disable-features=DnsOverHttps",
            # Reduce background network requests that compete for DNS threads
            "--disable-client-side-phishing-detection",
            "--disable-component-update",
            # Log Chrome errors (network, DNS, crashes) to stderr for container logs
            "--enable-logging=stderr",
            "--v=0",  # errors only (use --v=1 for warnings)
        ],
    )
    # Monitor browser health (persistent context may not expose .browser)
    if _context.browser:
        _context.browser.on("disconnected", lambda: log.error(
            "BROWSER DISCONNECTED — Chrome process died or was killed"
        ))
    _context.on("close", lambda: log.error(
        "CONTEXT CLOSED — browser context shut down"
    ))

    # Comprehensive stealth patches applied before any page JS runs
    _context.add_init_script(_STEALTH_SCRIPT)
    # Auto-dismiss cookie consent banners on every page load
    autoconsent_script = _build_autoconsent_init_script()
    if autoconsent_script:
        _context.add_init_script(autoconsent_script)


def _cleanup():
    """Clean up Playwright on exit."""
    global _context, _playwright
    if _context:
        _context.close()
    if _playwright:
        _playwright.stop()


atexit.register(_cleanup)


def _create_session():
    """Create a new page (tab) in the persistent context.

    Enforces MAX_SESSIONS limit by closing the oldest session when full.
    """
    _ensure_browser()

    with _sessions_lock:
        # Evict expired first
        now = time.time()
        expired = [
            sid for sid, s in _sessions.items()
            if now - s["created_at"] > SESSION_TTL
        ]
        for sid in expired:
            _close_session_unlocked(sid)

        # If still at limit, close the oldest session
        while len(_sessions) >= MAX_SESSIONS:
            oldest_sid = min(_sessions, key=lambda s: _sessions[s]["created_at"])
            log.info("Session limit (%d) reached — closing oldest session %s", MAX_SESSIONS, oldest_sid)
            _close_session_unlocked(oldest_sid)

    page = _context.new_page()
    page.on("crash", lambda: log.error("PAGE CRASHED — tab renderer process died"))
    page.on("close", lambda: log.debug("Page closed"))

    session_id = str(uuid.uuid4())[:8]
    with _sessions_lock:
        _sessions[session_id] = {
            "page": page,
            "created_at": time.time(),
        }
    return session_id, page


def _get_session(session_id):
    """Get an existing session's page, or None if expired/missing."""
    with _sessions_lock:
        session = _sessions.get(session_id)
        if session is None:
            return None
        if time.time() - session["created_at"] > SESSION_TTL:
            _close_session_unlocked(session_id)
            return None
        return session["page"]


def _close_session_unlocked(session_id):
    """Close a session (caller must hold lock)."""
    session = _sessions.pop(session_id, None)
    if session:
        try:
            session["page"].close()
        except Exception:
            pass


def _close_session(session_id):
    """Close a session."""
    with _sessions_lock:
        _close_session_unlocked(session_id)


def _cleanup_expired():
    """Remove expired sessions."""
    now = time.time()
    with _sessions_lock:
        expired = [
            sid for sid, s in _sessions.items()
            if now - s["created_at"] > SESSION_TTL
        ]
        for sid in expired:
            _close_session_unlocked(sid)


def _detect_captcha(page):
    """Check if the page shows a captcha challenge."""
    try:
        body_text = page.inner_text("body").lower()
    except Exception:
        body_text = ""

    for pattern in CAPTCHA_PATTERNS:
        if pattern in body_text:
            return True

    # Check for captcha iframes
    for frame in page.frames:
        for url_pattern in CAPTCHA_FRAME_URLS:
            if url_pattern in frame.url:
                return True

    return False


def _extract_page_content(page):
    """Extract text content, title, and links from a page."""
    title = page.title()

    # Get main text content
    try:
        text = page.inner_text("body")
        # Trim excessive whitespace
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        text = "\n".join(lines)
        # Limit length
        if len(text) > 50000:
            text = text[:50000] + "\n\n[Content truncated at 50000 characters]"
    except Exception:
        text = ""

    # Get links
    links = []
    try:
        anchors = page.query_selector_all("a[href]")
        for a in anchors[:100]:  # Limit to 100 links
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


@app.route("/browse", methods=["POST"])
def browse():
    """Navigate to URL and return page content.

    Body: {"url": "...", "session_id": "...", "timeout": 30, "wait_for": "..."}
    - session_id: reuse existing session (optional)
    - timeout: navigation timeout in seconds (default 30)
    - wait_for: CSS selector to wait for after load (optional)
    """
    _cleanup_expired()
    data = request.get_json()
    url = data.get("url", "")
    session_id = data.get("session_id")
    timeout = data.get("timeout", 30) * 1000  # ms
    wait_for = data.get("wait_for")
    keep_session = data.get("keep_session", False)

    if not url:
        return jsonify({"error": "url is required"}), 400

    page = None
    created_new = False
    if session_id:
        page = _get_session(session_id)
        if page is None:
            return jsonify({"error": f"session {session_id} not found or expired"}), 404
    else:
        session_id, page = _create_session()
        created_new = True

    try:
        page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        # Wait for JS rendering, then simulate human behavior
        page.wait_for_timeout(1000)
        _simulate_human_behavior(page)

        if wait_for:
            try:
                page.wait_for_selector(wait_for, timeout=10000)
            except Exception:
                pass  # Proceed even if selector doesn't appear

        if _detect_captcha(page):
            vnc_url = os.environ.get("BROWSER_VNC_URL", "")
            return jsonify({
                "status": "captcha",
                "session_id": session_id,
                "vnc_url": vnc_url,
                "message": "Captcha detected. Solve it via VNC, then retry with the same session_id.",
            })

        content = _extract_page_content(page)
        result = {"status": "ok", **content}

        if keep_session or not created_new:
            result["session_id"] = session_id
        else:
            _close_session(session_id)

        return jsonify(result)

    except Exception as e:
        if created_new and not keep_session:
            _close_session(session_id)
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/screenshot", methods=["POST"])
def screenshot():
    """Take a screenshot of the current page.

    Body: {"url": "...", "session_id": "...", "full_page": false}
    Returns PNG image.
    """
    _cleanup_expired()
    data = request.get_json()
    url = data.get("url")
    session_id = data.get("session_id")
    full_page = data.get("full_page", False)
    timeout = data.get("timeout", 30) * 1000

    page = None
    created_new = False
    if session_id:
        page = _get_session(session_id)
        if page is None:
            return jsonify({"error": f"session {session_id} not found or expired"}), 404
    elif url:
        session_id, page = _create_session()
        created_new = True
        try:
            page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
            _simulate_human_behavior(page)
        except Exception as e:
            _close_session(session_id)
            return jsonify({"status": "error", "error": str(e)}), 500
    else:
        return jsonify({"error": "url or session_id is required"}), 400

    try:
        img_bytes = page.screenshot(full_page=full_page)
        if created_new:
            _close_session(session_id)
        return Response(img_bytes, mimetype="image/png")
    except Exception as e:
        if created_new:
            _close_session(session_id)
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/extract", methods=["POST"])
def extract():
    """Extract content by CSS selector.

    Body: {"url": "...", "session_id": "...", "selector": "article", "timeout": 30}
    """
    _cleanup_expired()
    data = request.get_json()
    url = data.get("url")
    session_id = data.get("session_id")
    selector = data.get("selector", "body")
    timeout = data.get("timeout", 30) * 1000

    page = None
    created_new = False
    if session_id:
        page = _get_session(session_id)
        if page is None:
            return jsonify({"error": f"session {session_id} not found or expired"}), 404
    elif url:
        session_id, page = _create_session()
        created_new = True
        try:
            page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
            _simulate_human_behavior(page)
        except Exception as e:
            _close_session(session_id)
            return jsonify({"status": "error", "error": str(e)}), 500
    else:
        return jsonify({"error": "url or session_id is required"}), 400

    try:
        elements = page.query_selector_all(selector)
        results = []
        for el in elements[:20]:  # Limit results
            text = el.inner_text().strip()
            html = el.inner_html()
            if text:
                results.append({
                    "text": text[:10000],
                    "html": html[:10000],
                })

        if created_new:
            _close_session(session_id)

        return jsonify({
            "status": "ok",
            "url": page.url,
            "selector": selector,
            "count": len(results),
            "elements": results,
        })
    except Exception as e:
        if created_new:
            _close_session(session_id)
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/interact", methods=["POST"])
def interact():
    """Interact with an existing session (click, fill, scroll).

    Body: {"session_id": "...", "actions": [{"type": "click", "selector": "..."}, ...]}
    Actions:
    - {"type": "click", "selector": "..."}
    - {"type": "fill", "selector": "...", "value": "..."}
    - {"type": "scroll", "direction": "down", "amount": 500}
    - {"type": "wait", "timeout": 2000}
    - {"type": "select", "selector": "...", "value": "..."}
    """
    _cleanup_expired()
    data = request.get_json()
    session_id = data.get("session_id")
    actions = data.get("actions", [])

    if not session_id:
        return jsonify({"error": "session_id is required"}), 400

    page = _get_session(session_id)
    if page is None:
        return jsonify({"error": f"session {session_id} not found or expired"}), 404

    results = []
    try:
        for action in actions:
            action_type = action.get("type")
            selector = action.get("selector", "")

            if action_type == "click":
                page.click(selector, timeout=10000)
                page.wait_for_timeout(1000)
                results.append({"action": "click", "selector": selector, "ok": True})

            elif action_type == "fill":
                value = action.get("value", "")
                page.fill(selector, value, timeout=10000)
                results.append({"action": "fill", "selector": selector, "ok": True})

            elif action_type == "scroll":
                direction = action.get("direction", "down")
                amount = action.get("amount", 500)
                if direction == "down":
                    page.evaluate(f"window.scrollBy(0, {amount})")
                elif direction == "up":
                    page.evaluate(f"window.scrollBy(0, -{amount})")
                results.append({"action": "scroll", "direction": direction, "ok": True})

            elif action_type == "wait":
                timeout_ms = action.get("timeout", 2000)
                page.wait_for_timeout(min(timeout_ms, 30000))
                results.append({"action": "wait", "ok": True})

            elif action_type == "select":
                value = action.get("value", "")
                page.select_option(selector, value, timeout=10000)
                results.append({"action": "select", "selector": selector, "ok": True})

            else:
                results.append({"action": action_type, "ok": False, "error": "unknown action"})

        # Check for captcha after interactions
        if _detect_captcha(page):
            vnc_url = os.environ.get("BROWSER_VNC_URL", "")
            return jsonify({
                "status": "captcha",
                "session_id": session_id,
                "vnc_url": vnc_url,
                "actions": results,
            })

        content = _extract_page_content(page)
        return jsonify({
            "status": "ok",
            "session_id": session_id,
            "actions": results,
            **content,
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "session_id": session_id,
            "actions": results,
            "error": str(e),
        }), 500


@app.route("/evaluate", methods=["POST"])
def evaluate():
    """Evaluate JavaScript in an existing session.

    Body: {"session_id": "...", "expression": "1 + 1"}
    Returns the result of the expression as JSON.
    """
    _cleanup_expired()
    data = request.get_json()
    session_id = data.get("session_id")
    expression = data.get("expression", "")

    if not session_id:
        return jsonify({"error": "session_id is required"}), 400
    if not expression:
        return jsonify({"error": "expression is required"}), 400

    page = _get_session(session_id)
    if page is None:
        return jsonify({"error": f"session {session_id} not found or expired"}), 404

    try:
        result = page.evaluate(expression)
        return jsonify({"status": "ok", "result": result})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/sessions/<session_id>", methods=["GET"])
def get_session(session_id):
    """Check session status."""
    with _sessions_lock:
        session = _sessions.get(session_id)
        if session is None:
            return jsonify({"status": "not_found"}), 404
        age = time.time() - session["created_at"]
        ttl = max(0, SESSION_TTL - age)
        return jsonify({
            "status": "active",
            "session_id": session_id,
            "age_seconds": int(age),
            "ttl_seconds": int(ttl),
            "url": session["page"].url,
        })


@app.route("/sessions/<session_id>", methods=["DELETE"])
def delete_session(session_id):
    """Close a session."""
    _close_session(session_id)
    return jsonify({"status": "closed", "session_id": session_id})


def _get_chrome_diagnostics():
    """Collect Chrome process and memory diagnostics."""
    diag = {}
    try:
        # Count Chrome processes and total RSS
        result = subprocess.run(
            ["ps", "aux"], capture_output=True, text=True, timeout=5,
        )
        chrome_procs = []
        total_rss_kb = 0
        for line in result.stdout.splitlines():
            if "chrome" in line.lower() and "--type=" in line:
                parts = line.split()
                rss_kb = int(parts[5])  # RSS is 6th column in ps aux
                proc_type = "unknown"
                for arg in line.split():
                    if arg.startswith("--type="):
                        proc_type = arg.split("=", 1)[1]
                        break
                chrome_procs.append({"type": proc_type, "rss_mb": rss_kb // 1024})
                total_rss_kb += rss_kb
        diag["chrome_processes"] = len(chrome_procs)
        diag["chrome_rss_mb"] = total_rss_kb // 1024
        diag["process_detail"] = chrome_procs
    except Exception as e:
        diag["chrome_process_error"] = str(e)

    try:
        # Container memory from cgroup
        with open("/sys/fs/cgroup/memory.current", "r") as f:
            current_bytes = int(f.read().strip())
        with open("/sys/fs/cgroup/memory.max", "r") as f:
            max_val = f.read().strip()
        max_bytes = int(max_val) if max_val != "max" else None
        diag["container_memory_mb"] = current_bytes // (1024 * 1024)
        if max_bytes:
            diag["container_memory_limit_mb"] = max_bytes // (1024 * 1024)
            diag["container_memory_pct"] = round(
                current_bytes / max_bytes * 100, 1,
            )
    except Exception:
        pass  # cgroup v1 or not in container

    try:
        # Browser context health
        if _context:
            diag["browser_pages"] = len(_context.pages)
            diag["browser_connected"] = _context.browser.is_connected() if _context.browser else True
        else:
            diag["browser_connected"] = False
    except Exception as e:
        diag["browser_error"] = str(e)

    return diag


@app.route("/health", methods=["GET"])
def health():
    """Health check with Chrome diagnostics."""
    with _sessions_lock:
        active = len(_sessions)
    verbose = request.args.get("v") == "1"
    data = {"status": "ok", "active_sessions": active, "max_sessions": MAX_SESSIONS}
    if verbose:
        data.update(_get_chrome_diagnostics())
    else:
        # Always include key metrics
        try:
            if _context:
                data["browser_pages"] = len(_context.pages)
                data["browser_connected"] = _context.browser.is_connected() if _context.browser else True
        except Exception:
            data["browser_connected"] = False
    return jsonify(data)


@app.before_request
def _log_request_start():
    request._start_time = time.time()
    # Ensure browser is alive before handling browsing requests
    if request.path != "/health":
        try:
            _ensure_browser()
        except Exception as e:
            log.error("Failed to ensure browser: %s", e)
            return jsonify({"status": "error", "error": f"Browser unavailable: {e}"}), 503


@app.after_request
def _log_request_end(response):
    duration = time.time() - getattr(request, "_start_time", time.time())
    # Skip noisy health checks without verbose flag
    if request.path == "/health" and request.args.get("v") != "1":
        return response
    parts = [
        f"{request.method} {request.path}",
        f"{response.status_code}",
        f"{duration:.1f}s",
    ]
    with _sessions_lock:
        parts.append(f"sessions={len(_sessions)}")
    try:
        if _context:
            parts.append(f"pages={len(_context.pages)}")
    except Exception:
        pass
    log.info(" | ".join(parts))
    return response


def _resource_monitor():
    """Background thread logging Chrome resource usage every 30 seconds.

    Logs at DEBUG normally, upgrades to WARNING when memory usage exceeds
    thresholds. Helps diagnose intermittent connectivity issues.
    """
    while True:
        time.sleep(30)
        try:
            # Chrome process count and RSS
            result = subprocess.run(
                ["ps", "aux"], capture_output=True, text=True, timeout=5,
            )
            chrome_rss_kb = 0
            chrome_count = 0
            for line in result.stdout.splitlines():
                if "chrome" in line.lower() and "--type=" in line:
                    chrome_count += 1
                    chrome_rss_kb += int(line.split()[5])
            chrome_rss_mb = chrome_rss_kb // 1024

            # Container memory
            container_mb = None
            limit_mb = None
            try:
                with open("/sys/fs/cgroup/memory.current") as f:
                    container_mb = int(f.read().strip()) // (1024 * 1024)
                with open("/sys/fs/cgroup/memory.max") as f:
                    v = f.read().strip()
                    limit_mb = int(v) // (1024 * 1024) if v != "max" else None
            except Exception:
                pass

            pages = len(_context.pages) if _context else 0
            with _sessions_lock:
                sessions = len(_sessions)

            pct = round(container_mb / limit_mb * 100, 1) if container_mb and limit_mb else 0
            msg = (
                f"pages={pages} sessions={sessions} "
                f"chrome_procs={chrome_count} chrome_rss={chrome_rss_mb}MB "
                f"container={container_mb}MB/{limit_mb}MB ({pct}%)"
            )
            if pct > 80:
                log.warning("HIGH MEMORY: %s", msg)
            elif pct > 60:
                log.info("monitor: %s", msg)
            else:
                log.debug("monitor: %s", msg)

        except Exception as e:
            log.debug("monitor error: %s", e)


if __name__ == "__main__":
    _init_browser()
    log.info(
        "Browser started — pages=%d",
        len(_context.pages) if _context else 0,
    )
    # Start background resource monitor
    mon = threading.Thread(target=_resource_monitor, daemon=True)
    mon.start()
    # threaded=False: Playwright sync API uses greenlets that can't switch threads.
    # All requests must run on the main thread where the browser was initialized.
    app.run(host="0.0.0.0", port=9223, threaded=False)
