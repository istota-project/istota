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
    // Removed: the override was a no-op AND created a detectable own property
    // on navigator (Object.getOwnPropertyNames(navigator) returns ["getBattery"]).
    // Real Chrome in a Docker container returns a battery object natively.

    // === 20. Clean up Playwright/Puppeteer globals ===
    // Automation tools inject detectable global variables. Detection checks
    // whether these properties exist on window (via hasOwnProperty or 'in'),
    // not just their values. So we must delete them WITHOUT re-defining traps,
    // since Object.defineProperty creates an own property that's detectable.
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
