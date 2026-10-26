// The escape tier. For each fixture (fixtures.ts), the shell page frames the
// built document and the spec judges it two ways:
//
// - **The recorder.** Every fixture points the requests it attempts at the
//   recorder's own origin (recorder.mjs). What reached the recorder is what
//   actually left the frame. This is the leak verdict, because Chromium emits
//   Playwright `request` events for CSP-blocked XHR and images even though the
//   request never reaches the network, and Firefox and WebKit do not.
// - **The browser's request events.** Only a real navigation of the frame emits
//   one, in every browser, so a navigation is judged from these: the shell must
//   have noticed the frame navigate and removed it.
//
// Nothing opens a page. The fixture reaches `done` (or navigates) within the
// deadline. What each fixture reported about itself (`report:*` keys) and the
// recorder's log are attached to the test, so the per-channel results the spec
// records come from the run.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { expect, test, type Request } from '@playwright/test';
import { CONTRACT, EARLY_NAV, HOSTILE, OUT, RECORDER, cspRemoved, fixtureIds } from './fixtures';

interface EscapeState {
  connected: boolean;
  navigated: boolean;
  frameRemoved: boolean;
  calls: { capability: string; op: string }[];
  kv: Record<string, unknown>;
  errors: { kind: string; message: string }[];
}

interface RecorderHit {
  method: string;
  path: string;
  query: string;
  at: number;
}

/** The recorder paths a fixture may reach by design. */
const ALLOWED: Record<string, RegExp> = {
  // The one open channel: the frame navigates itself away with data in the URL,
  // which the shell must notice and remove.
  [HOSTILE]: /^\/exfil/,
  // The pre-load navigation fixture: the page it lands on, and that page's own
  // reports back to the recorder.
  [EARLY_NAV]: /^\/(landed|mark)$/,
};

const DEADLINE_MS = 20_000;
const SETTLE_MS = 1_500;

for (const id of fixtureIds()) {
  test(`app ${id} stays inside its frame`, async ({ page, context, request }, testInfo) => {
    // The recorder outlives one test; clear it so the log is this run's alone.
    await request.post(`${RECORDER}/log/clear`);

    const frameRequests: Request[] = [];
    const popups: string[] = [];
    const sockets: string[] = [];
    context.on('page', (opened) => {
      if (opened !== page) popups.push(opened.url());
    });
    page.on('request', (request) => {
      const frame = request.frame();
      if (frame !== page.mainFrame()) frameRequests.push(request);
    });
    page.on('websocket', (socket) => sockets.push(socket.url()));

    const frameResponse = page.waitForResponse((r) => r.url().includes(`/frame/${id}`));
    await page.goto(`/?app=${encodeURIComponent(id)}`);
    const response = await frameResponse;

    await page.waitForFunction(
      () => {
        const s = (window as unknown as { __escape: EscapeState }).__escape;
        return s.kv.done === true || s.navigated;
      },
      undefined,
      { timeout: DEADLINE_MS },
    );
    await page.waitForTimeout(SETTLE_MS);
    const state = await page.evaluate(
      () => (window as unknown as { __escape: EscapeState }).__escape,
    );

    const log = (await (await request.get(`${RECORDER}/log`)).json()) as RecorderHit[];
    const allowed = ALLOWED[id];
    const reached = log.filter((hit) => !allowed || !allowed.test(hit.path));

    const reports = Object.fromEntries(
      Object.entries(state.kv).filter(([key]) => key.startsWith('report:')),
    );
    // The spec's per-channel results table comes from this line.
    console.log(`[${id}] ${JSON.stringify(reports)}`);
    await testInfo.attach('reports.json', {
      body: JSON.stringify(
        {
          reports,
          calls: state.calls,
          errors: state.errors,
          navigated: state.navigated,
          recorder: log,
          frameRequests: frameRequests.map((request) => ({
            url: request.url(),
            navigation: request.isNavigationRequest(),
          })),
          sockets,
        },
        null,
        2,
      ),
      contentType: 'application/json',
    });

    // The first request in the app frame is its own document; a later navigation
    // is judged below, and nothing else may leave.
    const [, ...later] = frameRequests;
    const navigations = later.filter((r) => r.isNavigationRequest());

    expect(state.connected, 'the bridge handed the frame its port').toBe(true);
    expect(
      reached.map((hit) => `${hit.method} ${hit.path}${hit.query}`),
      'requests that reached the recorder',
    ).toEqual([]);
    expect(popups, 'pages opened from the app frame').toEqual([]);
    if (navigations.length > 0) {
      expect(state.navigated, 'the shell noticed the frame navigate').toBe(true);
      expect(state.frameRemoved, 'the shell removed the navigated frame').toBe(true);
    }

    if (!cspRemoved()) {
      const expected = readFileSync(join(OUT, id, 'csp.txt'), 'utf8').trim();
      expect(response.headers()['content-security-policy']).toBe(expected);
    }

    if (id === CONTRACT) {
      expect(reports['report:kit']).toMatchObject({ outcome: 'present' });
      expect(reports['report:round-trip']).toMatchObject({ outcome: 'ok' });
      expect(state.navigated).toBe(false);
    }

    if (id === HOSTILE) {
      // The last attempt navigates the frame away with data in the URL. It has
      // left by the time the shell notices; the record and the removal are the
      // defence, so the tier requires both.
      expect(state.navigated, 'the hostile fixture navigated itself away').toBe(true);
    }

    if (id === EARLY_NAV) {
      // The page that landed holds the port (the first load was its own) but
      // never the key, so nothing it sent was relayed.
      const marks = log.map((hit) => `${hit.path}${hit.query}`);
      expect(marks, 'the landing page reached the recorder').toContain('/mark?page=landed&port=1');
      expect(marks, 'a call the landing page made was answered').not.toContain(
        '/mark?page=landed&call-answered=1',
      );
      expect(state.calls, 'a call relayed for the page that landed').toEqual([]);
    }
  });
}
