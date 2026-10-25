// The escape tier. For each fixture (fixtures.ts), the shell page frames the
// built document and the spec watches every request the browser makes:
//
// - after the frame's own document, the app frame makes no request at all; a
//   navigation away is the one thing that may leave, and then the shell must
//   have caught it and removed the frame;
// - nothing opens a page or a popup;
// - the fixture reaches `done` (or navigates) within the deadline.
//
// What each fixture reported about itself (`report:*` keys) is attached to the
// test, so the per-channel results the spec records come from the run.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { expect, test, type Request } from '@playwright/test';
import { CONTRACT, OUT, cspRemoved, fixtureIds } from './fixtures';

interface EscapeState {
  connected: boolean;
  navigated: boolean;
  frameRemoved: boolean;
  calls: { capability: string; op: string }[];
  kv: Record<string, unknown>;
  errors: { kind: string; message: string }[];
}

const DEADLINE_MS = 20_000;
const SETTLE_MS = 1_500;

for (const id of fixtureIds()) {
  test(`app ${id} stays inside its frame`, async ({ page, context }, testInfo) => {
    const frameRequests: Request[] = [];
    const popups: string[] = [];
    context.on('page', (opened) => {
      if (opened !== page) popups.push(opened.url());
    });
    page.on('request', (request) => {
      const frame = request.frame();
      if (frame !== page.mainFrame()) frameRequests.push(request);
    });

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

    const reports = Object.fromEntries(
      Object.entries(state.kv).filter(([key]) => key.startsWith('report:')),
    );
    await testInfo.attach('reports.json', {
      body: JSON.stringify(
        { reports, calls: state.calls, errors: state.errors, navigated: state.navigated },
        null,
        2,
      ),
      contentType: 'application/json',
    });

    // The first request in the app frame is its own document; everything after it left.
    const [, ...later] = frameRequests;
    const navigations = later.filter((r) => r.isNavigationRequest());
    const leaks = later.filter((r) => !r.isNavigationRequest()).map((r) => r.url());

    expect(state.connected, 'the bridge handed the frame its port').toBe(true);
    expect(leaks, 'requests made from inside the app frame').toEqual([]);
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
  });
}
