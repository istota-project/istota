# The apps escape tier

A Playwright tier that frames built app documents in Chromium, Firefox and WebKit and checks that nothing leaves the frame. It is discretionary: `npm run test` never runs it.

```bash
npx --prefix web playwright install chromium firefox webkit   # once
npm --prefix web run test:apps-escape
scripts/test-apps-escape-negative-control.sh                    # must report red
```

## What runs

- `global-setup.ts` runs `npm run build:apps`, then `scripts/apps-escape-fixture.py` for each fixture. That script uses the real `apps.manifest`, `apps.document` and `apps.kit`, so the frame is what an install would serve, kit and CSP included.
- `vite.config.ts` serves the shell page from `shell/` and each built fixture at `/frame/<id>`, with the frame route's headers. `APPS_ESCAPE_NO_CSP=1` drops the CSP header, which is what the negative control does.
- `recorder.mjs` is a second loopback origin (`127.0.0.1:5198`) that serves `/landed` (the page the pre-load navigation fixture points at) and logs every request and WebSocket upgrade it receives. The spec clears its log at the top of each test and reads it back; what reached the recorder is what actually left the frame.
- `shell/main.ts` frames the fixture with `sandbox="allow-scripts"` and runs the real `src/lib/apps/bridge.ts` and `frameWatch.ts` around it. An in-memory `kv:own` stands in for the server. The page publishes the state on `window.__escape`: whether the port connected, whether the frame navigated and was removed, every relayed call, the store's contents.
- `escape.spec.ts` runs once per fixture per browser. The frame is judged two ways: the recorder's log for what reached the network, and Playwright's own request events for navigations. A navigation away is the one thing that may leave, and then the shell must have noticed it and removed the frame. Nothing may open a page. The fixture must reach `done` or navigate within 20 seconds.

### Why the verdict is the recorder, not the request events

Chromium emits a Playwright `request` event for a CSP-blocked `fetch`-adjacent XHR or image even though the request never reaches the network; Firefox and WebKit do not. A tier that failed on any request event would therefore go red on Chromium for a frame whose CSP held. Only a real navigation emits a request event in all three, so navigations are judged from those events and every other channel from the recorder.

## Fixtures

A fixture is a folder under `tests/fixtures/apps/` holding `manifest.toml` and `index.html`, written like any app draft. Its folder name equals its `id`, and it is built by the same rules as a real install, so a fixture the builder refuses fails global setup.

- `escape-contract/` is the tier's check on itself. It uses only the kit and reports through it, so it shows the frame loads under its CSP, the port connects and a call round-trips. It is green with or without the CSP.
- `hostile/` is the adversarial fixture. It tries every way out of the frame the apps-foundation spec lists (Design sections 1 to 3), reports each attempt, then navigates itself away with data in the URL as its last act.
- `early-nav/` navigates the frame to the recorder's `/landed` page before its own first `load`, so that load belongs to a page the shell has never seen. The landing page takes the port, sends a `hello` with no key and a call, and reports what happened through the recorder's `/mark`. The tier asserts the port arrived and that nothing it sent was answered.

### Reporting convention

A fixture reports through the bridge, so the reports travel the same path as any app call:

- one `istota.kv.set('report:<name>', {outcome, detail})` per thing it tried, where `outcome` is a short word (`blocked`, `undefined`, `threw`, `sent`, and so on) and `detail` a string;
- `istota.kv.set('done', true)` when it has finished, unless it navigated away.

The spec attaches every `report:*` value and the recorder's log to the test as `reports.json`, and prints the reports on one line per run, which is where the per-channel table in the spec's "Escape tier results" section comes from. The verdict does not rest on the reports: it comes from what reached the recorder and from the browser's record of navigations.

### What the hostile fixture is for

It tries each way out of the frame and reports each attempt, so the per-channel table is filled per browser, not asserted. The two questions earlier stages left for this tier:

- whether app code can get the WebRTC constructors back from a fresh realm after the guard deleted them — answered: a nested `src` or `srcdoc` iframe is blocked by `frame-src 'none'`, and its `contentWindow` is a `SecurityError` in all three, so no fresh realm is reachable;
- whether a page the frame navigates to before its own `load` can reach the bridge — answered: it is handed the port (that load is its own), but the kit read the fragment's key and stripped it before any app script ran, and the page never saw it, so its `hello` fails and its calls are refused.

A request the tier is meant to catch only shows up as a failure if it reaches the network layer, so point any attempted request at the recorder (`http://127.0.0.1:5198`) or another loopback address. Nothing in this tier needs a host outside the machine. Whether a `dns-prefetch` hint causes a DNS lookup is not observable here and is recorded as open.

## Negative control

`scripts/test-apps-escape-negative-control.sh` serves the hostile fixture without its CSP and exits 0 only when the hostile test fails in all three browsers. Run it after any change to the frame, its CSP, the kit's guard or the bridge, and write down which assertions it turned red.
