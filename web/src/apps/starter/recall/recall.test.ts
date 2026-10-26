// recall.js against a fake `istota.kv` and `istota.files`, with the real kit
// components, sm2.js and recall-model.js, run the way the installed document
// runs them.

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const ROOT = join(process.cwd(), 'src', 'apps');
const read = (path: string) => readFileSync(join(ROOT, path), 'utf8');
const INDEX = read('starter/recall/index.html');
const BODY = INDEX.slice(INDEX.indexOf('<body>') + 6, INDEX.indexOf('<script'));

type Store = Map<string, unknown>;
type Globals = Record<string, unknown>;

class IstotaError extends Error {
  constructor(
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

const NOTE = [
  '# Aviation weather',
  '',
  '## METAR',
  '',
  'A METAR is a routine report, issued every hour.',
  '',
  '## TAF',
  '',
  'A TAF is a forecast.',
].join('\n');

function fakeIstota(store: Store, files: Map<string, string>, granted = true) {
  return {
    env: { visibility: 'visible' },
    on: () => () => {},
    kv: {
      set: vi.fn(async (key: string, value: unknown) => {
        store.set(key, JSON.parse(JSON.stringify(value)));
      }),
      delete: vi.fn(async (key: string) => ({ deleted: store.delete(key) })),
      list: vi.fn(async ({ prefix = '', offset = 0, limit = 100 } = {}) => {
        const items = [...store]
          .filter(([key]) => key.startsWith(prefix))
          .map(([key, value]) => ({ key, value }));
        return { items: items.slice(offset, offset + limit), total: items.length };
      }),
    },
    files: {
      // Paths are relative to the folder the user granted, here `study/notes`.
      readShared: vi.fn(async (folder: string, path: string) => {
        if (!granted) throw new IstotaError('not_granted', 'not granted');
        expect(folder).toBe('notes');
        if (!files.has(path)) throw new IstotaError('not_found', 'No such file.');
        return files.get(path);
      }),
    },
  };
}

function run(store: Store, files = new Map([['aviation/weather.md', NOTE]]), granted = true) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    addEventListener() {},
    removeEventListener() {},
  })) as unknown as typeof window.matchMedia;
  document.body.innerHTML = BODY;
  const istota = fakeIstota(store, files, granted);
  (globalThis as Globals).istota = istota;
  for (const file of [
    'kit/components.js',
    'starter/recall/sm2.js',
    'starter/recall/recall-model.js',
    'starter/recall/recall.js',
  ]) {
    new Function(read(file))();
  }
  return istota;
}

function seeded(): Store {
  return new Map<string, unknown>([
    [
      'card:metar',
      {
        q: 'What is a METAR?',
        a: 'A routine weather report.',
        source_path: 'notes/aviation/weather.md',
        source_anchor: 'METAR',
        created_at: '2026-10-01T00:00:00Z',
      },
    ],
    ['card:taf', { q: 'What is a TAF?', a: 'A forecast.', created_at: '2026-10-02T00:00:00Z' }],
    ['card:later', { q: 'Not due yet', a: 'x', created_at: '2026-09-01T00:00:00Z' }],
    ['review:later', { due: '2026-11-03', interval: 15, ease: 2.5, reps: 3, lapses: 0 }],
  ]);
}

function button(label: string, root: ParentNode = document) {
  const found = [...root.querySelectorAll('button')].find((b) =>
    (b.textContent || '').startsWith(label),
  ) as HTMLButtonElement | undefined;
  expect(found, label).toBeDefined();
  return found!;
}

const text = (id: string) => document.getElementById(id)?.textContent;
const hidden = (id: string) => document.getElementById(id)?.hidden;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date(2026, 9, 25, 12, 0));
  document.body.innerHTML = '';
});

afterEach(() => {
  vi.useRealTimers();
  for (const name of ['istota', 'SM2', 'RecallModel']) delete (globalThis as Globals)[name];
});

describe('recall.js', () => {
  it('reviews the due cards in order, the answer hidden until asked for', async () => {
    run(seeded());
    await vi.waitFor(() => expect(text('question')).toBe('What is a METAR?'));
    expect(text('progress')).toBe('2 cards to review today.');
    expect(hidden('answer')).toBe(true);
    expect(hidden('grade-row')).toBe(true);
    button('Show answer').click();
    expect(hidden('answer')).toBe(false);
    expect(text('answer')).toBe('A routine weather report.');
    expect([...document.querySelectorAll('#grades button')].map((b) => b.textContent)).toEqual([
      'Again · 1 day',
      'Hard · 1 day',
      'Good · 1 day',
      'Easy · 1 day',
    ]);
    // Every card is listed, the one not due yet with its day.
    expect(
      [...document.querySelectorAll('#cards .recall-list-meta')].map((n) => n.textContent),
    ).toEqual(['New', 'New', expect.stringContaining('Due')]);
  });

  it('writes the SM-2 review on a pass and moves to the next card', async () => {
    const store = seeded();
    const istota = run(store);
    await vi.waitFor(() => expect(text('question')).toBe('What is a METAR?'));
    button('Show answer').click();
    button('Good').click();
    await vi.waitFor(() => expect(istota.kv.set).toHaveBeenCalledTimes(1));
    expect(store.get('review:metar')).toMatchObject({
      due: '2026-10-26',
      interval: 1,
      ease: 2.5,
      reps: 1,
      lapses: 0,
      history: [{ on: '2026-10-25', grade: 4, interval: 1 }],
    });
    expect(text('question')).toBe('What is a TAF?');
    expect(hidden('source-block')).toBe(true);
    expect(istota.files.readShared).not.toHaveBeenCalled();
  });

  it('shows the source paragraph on a miss, read from the granted folder', async () => {
    const store = seeded();
    const istota = run(store);
    await vi.waitFor(() => expect(text('question')).toBe('What is a METAR?'));
    button('Show answer').click();
    button('Again').click();
    await vi.waitFor(() => expect(hidden('source-text')).toBe(false));
    expect(text('source-text')).toBe('## METAR\n\nA METAR is a routine report, issued every hour.');
    expect(text('source-path')).toBe('notes/aviation/weather.md');
    // Tried as written, then without the leading folder, which is what the grant holds.
    expect(istota.files.readShared.mock.calls.map((c) => c[1])).toEqual([
      'notes/aviation/weather.md',
      'aviation/weather.md',
    ]);
    expect(store.get('review:metar')).toMatchObject({ lapses: 1, reps: 0, interval: 1 });
    button('Next card').click();
    expect(text('question')).toBe('What is a TAF?');
    expect(hidden('source-block')).toBe(true);
  });

  it('says so on a miss when no folder was granted', async () => {
    run(seeded(), new Map(), false);
    await vi.waitFor(() => expect(text('question')).toBe('What is a METAR?'));
    button('Show answer').click();
    button('Again').click();
    await vi.waitFor(() => expect(text('source-note')).toContain('has not been given a folder'));
    expect(hidden('source-text')).toBe(true);
  });

  it('says the note is missing when no path is in the folder', async () => {
    run(seeded(), new Map());
    await vi.waitFor(() => expect(text('question')).toBe('What is a METAR?'));
    button('Show answer').click();
    button('Again').click();
    await vi.waitFor(() =>
      expect(text('source-note')).toBe('That note is not in the folder Recall may read.'),
    );
  });

  it('says when the next card is due once the day is done', async () => {
    run(seeded());
    await vi.waitFor(() => expect(text('question')).toBe('What is a METAR?'));
    for (let i = 0; i < 2; i += 1) {
      button('Show answer').click();
      button('Easy').click();
    }
    expect(hidden('review-section')).toBe(true);
    expect(hidden('done')).toBe(false);
    expect(text('done')).toContain('Nothing left to review today. The next card is due');
  });

  it('adds a card from the form', async () => {
    const store = new Map<string, unknown>();
    const istota = run(store);
    await vi.waitFor(() => expect(hidden('empty')).toBe(false));
    button('Add card').click();
    const field = (name: string) =>
      document.querySelector<HTMLInputElement>(`istota-dialog [name="${name}"]`)!;
    field('q').value = 'Squawk for radio failure?';
    field('a').value = '7600';
    button('Add', document.querySelector('istota-dialog')!).click();
    await vi.waitFor(() => expect(istota.kv.set).toHaveBeenCalledTimes(1));
    const [key, value] = istota.kv.set.mock.calls[0];
    expect(key).toMatch(/^card:[0-9a-z]{8}$/);
    expect(value).toMatchObject({ q: 'Squawk for radio failure?', a: '7600', source_path: null });
    expect(text('question')).toBe('Squawk for radio failure?');
    expect(hidden('empty')).toBe(true);
  });
});
