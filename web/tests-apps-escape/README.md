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
- `shell/main.ts` frames the fixture with `sandbox="allow-scripts"` and runs the real `src/lib/apps/bridge.ts` and `frameWatch.ts` around it. An in-memory `kv:own` stands in for the server. The page publishes the state on `window.__escape`: whether the port connected, whether the frame navigated and was removed, every relayed call, the store's contents.
- `escape.spec.ts` runs once per fixture per browser. After the frame's own document, the app frame may make no request. A navigation away is the one exception, and then the shell must have noticed it and removed the frame. Nothing may open a page. The fixture must reach `done` or navigate within 20 seconds.

## Fixtures

A fixture is a folder under `tests/fixtures/apps/` holding `manifest.toml` and `index.html`, written like any app draft. Its folder name equals its `id`, and it is built by the same rules as a real install, so a fixture the builder refuses fails global setup.

- `escape-contract/` is the tier's check on itself. It uses only the kit and reports through it, so it shows the frame loads under its CSP, the port connects and a call round-trips. It is green with or without the CSP.
- `hostile/` is the adversarial fixture. Until it exists the tier runs the contract fixture alone, and the negative control exits 2.

### Reporting convention

A fixture reports through the bridge, so the reports travel the same path as any app call:

- one `istota.kv.set('report:<name>', {outcome, detail})` per thing it tried, where `outcome` is a short word (`blocked`, `undefined`, `threw`, `sent`, and so on) and `detail` a string;
- `istota.kv.set('done', true)` when it has finished, unless it navigated away.

The spec attaches every `report:*` value to the test as `reports.json`, which is where the per-channel table in the spec's "Escape tier results" section comes from. The verdict does not rest on the reports: it comes from the browser's own record of requests.

### What the hostile fixture is for

It tries each way out of the frame listed in the apps-foundation spec (Design, sections 1 to 3, and the escape-tier part of Test strategy), and reports each attempt. The two questions earlier stages left for this tier belong to it as well:

- whether app code can get the WebRTC constructors back from a fresh realm after the guard deleted them;
- whether a page the frame navigates to can reach the bridge before its own `load`.

A request the tier is meant to catch only shows up as a failure if it reaches the network layer, so point any attempted request at the harness itself (`http://127.0.0.1:5199`) or another loopback address. Nothing in this tier needs a host outside the machine.

## Negative control

`scripts/test-apps-escape-negative-control.sh` serves the hostile fixture without its CSP and exits 0 only when the hostile test fails in all three browsers. Run it after any change to the frame, its CSP, the kit's guard or the bridge, and write down which assertions it turned red.
