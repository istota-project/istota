// board.js against a fake `istota.kv`, with the real kit components and
// board-model.js, run the way the installed document runs them.

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const ROOT = join(process.cwd(), 'src', 'apps');
const read = (path: string) => readFileSync(join(ROOT, path), 'utf8');
const INDEX = read('starter/board/index.html');
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

function run(store: Store, { phone = false } = {}) {
  window.matchMedia = ((query: string) => ({
    matches: phone,
    media: query,
    addEventListener() {},
    removeEventListener() {},
  })) as unknown as typeof window.matchMedia;
  document.body.innerHTML = BODY;
  const istota = fakeIstota(store);
  (globalThis as Globals).istota = istota;
  for (const file of [
    'kit/components.js',
    'starter/board/board-model.js',
    'starter/board/board.js',
  ]) {
    new Function(read(file))();
  }
  return istota;
}

function seeded(): Store {
  return new Map<string, unknown>([
    [
      'columns',
      [
        { id: 'a', title: 'Options' },
        { id: 'b', title: 'Shortlist' },
      ],
    ],
    ['card:one', { title: 'Flat on Elm St', column: 'a', order: 2, body: 'Rent 1450' }],
    ['card:two', { title: 'House on Oak Ave', column: 'a', order: 1 }],
    ['card:three', { title: 'Loft', column: 'b', order: 1, struck: true }],
    ['card:lost', { title: 'No column yet' }],
  ]);
}

function columnTitles() {
  return [...document.querySelectorAll('.board-column-title')].map((n) => n.textContent);
}

function cardTitles(column: string) {
  return [
    ...document.querySelectorAll(`.board-column[data-column="${column}"] .board-card-title`),
  ].map((n) => n.textContent);
}

function clickButton(label: string) {
  const button = [...document.querySelectorAll('istota-dialog button')].find(
    (b) => b.textContent === label,
  ) as HTMLButtonElement | undefined;
  expect(button, label).toBeDefined();
  button?.click();
}

beforeEach(() => {
  document.body.innerHTML = '';
});

afterEach(() => {
  delete (globalThis as Globals).istota;
  delete (globalThis as Globals).BoardModel;
});

describe('board.js', () => {
  it('draws the stored columns in order, with Unsorted for a card the agent left without one', async () => {
    run(seeded());
    await vi.waitFor(() => expect(columnTitles()).toEqual(['Options', 'Shortlist', 'Unsorted']));
    expect(cardTitles('a')).toEqual(['House on Oak Ave', 'Flat on Elm St']);
    expect(document.querySelector('[data-id="three"]')?.classList.contains('is-struck')).toBe(true);
    expect(document.querySelector<HTMLElement>('.board-picker')?.hidden).toBe(true);
  });

  it('moves a card to another column from its move menu and writes only that card', async () => {
    const store = seeded();
    const istota = run(store);
    await vi.waitFor(() => expect(cardTitles('a')).toHaveLength(2));
    document.querySelector<HTMLButtonElement>('[data-id="one"] .board-icon-button')?.click();
    clickButton('Shortlist');
    expect(cardTitles('b')).toEqual(['Loft', 'Flat on Elm St']);
    await vi.waitFor(() => expect(istota.kv.set).toHaveBeenCalledTimes(1));
    expect(store.get('card:one')).toMatchObject({ id: 'one', column: 'b', order: 2 });
    expect(document.querySelector('istota-dialog')).toBeNull();
  });

  it('edits a card from its dialog, saved by the button rather than a form submit', async () => {
    const store = seeded();
    run(store);
    await vi.waitFor(() => expect(cardTitles('a')).toHaveLength(2));
    document.querySelector<HTMLButtonElement>('[data-id="two"] .board-card-open')?.click();
    const title = document.querySelector<HTMLInputElement>('istota-dialog input[name="title"]');
    const color = document.querySelector<HTMLSelectElement>('istota-dialog select[name="color"]');
    if (!title || !color) throw new Error('no editor');
    title.value = 'House on Oak Avenue';
    color.value = 'coral';
    clickButton('Save');
    await vi.waitFor(() =>
      expect(store.get('card:two')).toMatchObject({ title: 'House on Oak Avenue', color: 'coral' }),
    );
    expect(document.querySelector('[data-id="two"]')?.classList.contains('board-card--coral')).toBe(
      true,
    );
  });

  it('shows one column at a time on a phone, behind the column picker', async () => {
    run(seeded(), { phone: true });
    const picker = document.getElementById('column-picker') as HTMLSelectElement;
    await vi.waitFor(() => expect(picker.options).toHaveLength(3));
    expect(document.querySelector<HTMLElement>('.board-picker')?.hidden).toBe(false);
    expect([...picker.options].map((o) => o.textContent)).toEqual([
      'Options (2)',
      'Shortlist (1)',
      'Unsorted (1)',
    ]);
    expect(document.querySelectorAll('.board-column')).toHaveLength(1);
    picker.value = 'b';
    picker.dispatchEvent(new Event('change'));
    expect(cardTitles('b')).toEqual(['Loft']);
    expect(cardTitles('a')).toEqual([]);
  });

  it('says the board is empty when nothing is stored', async () => {
    run(new Map());
    await vi.waitFor(() => expect(document.getElementById('empty')?.hidden).toBe(false));
    expect(document.querySelectorAll('.board-column')).toHaveLength(0);
  });
});
