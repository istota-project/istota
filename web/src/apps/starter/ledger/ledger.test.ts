// ledger.js against a fake `istota.kv`, with the real kit components and
// ledger-model.js, run the way the installed document runs them.

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const ROOT = join(process.cwd(), 'src', 'apps');
const read = (path: string) => readFileSync(join(ROOT, path), 'utf8');
const INDEX = read('starter/ledger/index.html');
const BODY = INDEX.slice(INDEX.indexOf('<body>') + 6, INDEX.indexOf('<script'));

type Store = Map<string, unknown>;
type Globals = Record<string, unknown>;

function fakeIstota(store: Store) {
  return {
    env: { visibility: 'visible' },
    on: () => () => {},
    kv: {
      get: vi.fn(async (key: string) => (store.has(key) ? store.get(key) : null)),
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
  };
}

function run(store: Store) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    addEventListener() {},
    removeEventListener() {},
  })) as unknown as typeof window.matchMedia;
  document.body.innerHTML = BODY;
  const istota = fakeIstota(store);
  (globalThis as Globals).istota = istota;
  for (const file of [
    'kit/components.js',
    'starter/ledger/ledger-model.js',
    'starter/ledger/ledger.js',
  ]) {
    new Function(read(file))();
  }
  return istota;
}

function seeded(): Store {
  return new Map<string, unknown>([
    ['guess:due', { text: 'The vote passes', p: 0.6, resolves_on: '2026-10-20' }],
    ['guess:later', { text: 'Snow by December', p: 30, resolves_on: '2026-12-01' }],
    [
      'guess:hit',
      { text: 'Train on time', p: 0.8, outcome: true, resolved_at: '2026-10-01T00:00:00Z' },
    ],
    [
      'guess:miss',
      { text: 'Rain on Saturday', p: 0.9, outcome: false, resolved_at: '2026-10-02T00:00:00Z' },
    ],
  ]);
}

function texts(list: string) {
  return [...document.querySelectorAll(`#${list} .ledger-row-text`)].map((n) => n.textContent);
}

function dialogButton(label: string) {
  const button = [...document.querySelectorAll('istota-dialog button')].find(
    (b) => b.textContent === label,
  ) as HTMLButtonElement | undefined;
  expect(button, label).toBeDefined();
  return button!;
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date(2026, 9, 25, 12, 0));
  document.body.innerHTML = '';
});

afterEach(() => {
  vi.useRealTimers();
  delete (globalThis as Globals).istota;
  delete (globalThis as Globals).LedgerModel;
});

describe('ledger.js', () => {
  it('asks about a guess whose day has come, and lists the rest', async () => {
    run(seeded());
    await vi.waitFor(() => expect(texts('due')).toEqual(['The vote passes']));
    expect(texts('open')).toEqual(['Snow by December']);
    expect(texts('resolved')).toEqual(['Rain on Saturday', 'Train on time']);
    expect(document.querySelector('#open .ledger-row-meta')?.textContent).toContain('30%');
    expect(document.getElementById('empty')?.hidden).toBe(true);
  });

  it('records the outcome from the prompt and moves the guess to Resolved', async () => {
    const store = seeded();
    const istota = run(store);
    await vi.waitFor(() => expect(texts('due')).toHaveLength(1));
    const happened = [...document.querySelectorAll('#due button')].find(
      (b) => b.textContent === 'Happened',
    ) as HTMLButtonElement;
    happened.click();
    await vi.waitFor(() => expect(istota.kv.set).toHaveBeenCalledTimes(1));
    expect(store.get('guess:due')).toMatchObject({ id: 'due', outcome: true, p: 0.6 });
    expect((store.get('guess:due') as { resolved_at: string }).resolved_at).toMatch(/^2026-10-25/);
    expect(document.getElementById('due-section')?.hidden).toBe(true);
    expect(texts('resolved')[0]).toBe('The vote passes');
  });

  it('shows the Brier score, the calibration chart and its table', async () => {
    run(seeded());
    await vi.waitFor(() => expect(document.getElementById('score-section')?.hidden).toBe(false));
    // (0.8 - 1)^2 = 0.04 and (0.9 - 0)^2 = 0.81, over two.
    expect(document.getElementById('score')?.textContent).toContain(
      'Brier score 0.425 over 2 resolved guesses',
    );
    const chart = document.getElementById('chart') as HTMLElement & {
      data: { series: Array<{ name: string; points: Array<{ x: number; y: number }> }> };
    };
    expect(chart.data.series[0].points).toEqual([
      { x: 80, y: 100 },
      { x: 90, y: 0 },
    ]);
    expect(chart.querySelectorAll('circle.istota-chart-dot').length).toBeGreaterThan(0);
    expect([...document.querySelectorAll('#bins tr')].map((tr) => tr.textContent)).toEqual([
      '80–89%11 (100%)',
      '90–100%10 (0%)',
    ]);
  });

  it('adds a guess from the form, its percentage stored as a fraction', async () => {
    const store = new Map<string, unknown>();
    const istota = run(store);
    await vi.waitFor(() => expect(document.getElementById('empty')?.hidden).toBe(false));
    (document.querySelector('#add button') as HTMLButtonElement).click();
    const field = (name: string) =>
      document.querySelector<HTMLInputElement>(`istota-dialog input[name="${name}"]`)!;
    field('text').value = 'The bridge reopens';
    field('p').value = '65';
    field('resolves_on').value = '2026-11-30';
    dialogButton('Add').click();
    await vi.waitFor(() => expect(istota.kv.set).toHaveBeenCalledTimes(1));
    const [key, value] = istota.kv.set.mock.calls[0];
    expect(key).toMatch(/^guess:[0-9a-z]{8}$/);
    expect(value).toMatchObject({
      text: 'The bridge reopens',
      p: 0.65,
      resolves_on: '2026-11-30',
      outcome: null,
      resolved_at: null,
    });
    expect(document.querySelector('istota-dialog')).toBeNull();
    expect(texts('open')).toEqual(['The bridge reopens']);
  });

  it('edits a guess from its menu, saved by Enter in a field (which jsdom never submits on)', async () => {
    const store = seeded();
    run(store);
    await vi.waitFor(() => expect(texts('open')).toHaveLength(1));
    (document.querySelector('#open .ledger-icon-button') as HTMLButtonElement).click();
    dialogButton('Edit').click();
    const p = document.querySelector<HTMLInputElement>('istota-dialog input[name="p"]')!;
    expect(p.value).toBe('30');
    p.value = '45';
    p.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }),
    );
    await vi.waitFor(() => expect(store.get('guess:later')).toMatchObject({ p: 0.45 }));
    expect(document.querySelector('#open .ledger-row-meta')?.textContent).toContain('45%');
  });

  it('says the ledger is empty when nothing is stored', async () => {
    run(new Map());
    await vi.waitFor(() => expect(document.getElementById('empty')?.hidden).toBe(false));
    expect(document.getElementById('score-section')?.hidden).toBe(true);
  });
});
