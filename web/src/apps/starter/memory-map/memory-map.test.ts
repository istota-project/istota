// memory-map.js against a fake `istota.skill`, with the real kit components,
// graph-layout.js and memory-map-model.js, run the way the installed
// document runs them.

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const ROOT = join(process.cwd(), 'src', 'apps');
const read = (path: string) => readFileSync(join(ROOT, path), 'utf8');
const INDEX = read('starter/memory-map/index.html');
const BODY = INDEX.slice(INDEX.indexOf('<body>') + 6, INDEX.indexOf('<script'));

type Globals = Record<string, unknown>;

class IstotaError extends Error {
  constructor(
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

const fact = (id: number, subject: string, predicate: string, object: string, extra = {}) => ({
  id,
  subject,
  predicate,
  object,
  valid_from: null,
  valid_until: null,
  temporary: false,
  source_type: 'extracted',
  source_task_id: null,
  ...extra,
});

const FACTS = {
  facts: [
    fact(1, 'alex', 'lives_in', 'oslo', { valid_from: '2024-03-01' }),
    fact(2, 'alex', 'works_at', 'acme'),
    fact(3, 'alex', 'knows', 'sam'),
    fact(4, 'sam', 'lives_in', 'bergen'),
  ],
  truncated: false,
};

const TIMELINE = {
  subject: 'alex',
  facts: [
    {
      id: 9,
      predicate: 'lives_in',
      object: 'bergen',
      valid_from: '2019-05-01',
      valid_until: '2024-03-01',
    },
    { id: 1, predicate: 'lives_in', object: 'oslo', valid_from: '2024-03-01', valid_until: null },
    { id: 2, predicate: 'works_at', object: 'acme', valid_from: null, valid_until: null },
  ],
  truncated: false,
};

type Answer = unknown | IstotaError;

function fakeIstota(facts: Answer, timeline: Answer) {
  const answer = async (value: Answer) => {
    if (value instanceof IstotaError) throw value;
    return value;
  };
  return {
    env: { visibility: 'visible' },
    on: () => () => {},
    skill: vi.fn(async (operation: string) =>
      answer(operation === 'memory_search.facts' ? facts : timeline),
    ),
  };
}

function run(facts: Answer = FACTS, timeline: Answer = TIMELINE) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    addEventListener() {},
    removeEventListener() {},
  })) as unknown as typeof window.matchMedia;
  document.body.innerHTML = BODY;
  const istota = fakeIstota(facts, timeline);
  (globalThis as Globals).istota = istota;
  for (const file of [
    'kit/components.js',
    'starter/memory-map/graph-layout.js',
    'starter/memory-map/memory-map-model.js',
    'starter/memory-map/memory-map.js',
  ]) {
    new Function(read(file))();
  }
  return istota;
}

const $ = (id: string) => document.getElementById(id) as HTMLElement;
const node = (id: string) => document.querySelector(`.map-node[data-id="${id}"]`) as SVGGElement;
const texts = (selector: string) =>
  [...document.querySelectorAll(selector)].map((el) => el.textContent);
const historyRows = () =>
  [...document.querySelectorAll('#history .map-row')].map((row) => [
    row.querySelector('.map-row-text')?.textContent,
    row.classList.contains('is-held'),
  ]);

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date(2026, 9, 25, 12, 0));
  document.body.innerHTML = '';
});

afterEach(() => {
  vi.useRealTimers();
  for (const name of ['istota', 'GraphLayout', 'MemoryMapModel']) {
    delete (globalThis as Globals)[name];
  }
});

describe('memory-map.js', () => {
  it('draws a node for every entity and a line for every pair a fact joins', async () => {
    const istota = run();
    await vi.waitFor(() => expect($('body').hidden).toBe(false));
    expect(istota.skill).toHaveBeenCalledWith('memory_search.facts');
    expect(texts('.map-node .map-label')).toEqual(['acme', 'alex', 'bergen', 'oslo', 'sam']);
    expect(document.querySelectorAll('.map-edge')).toHaveLength(4);
    const oslo = node('oslo').querySelector('circle') as SVGCircleElement;
    expect(Number(oslo.getAttribute('cx'))).toBeGreaterThan(0);
    expect(texts('#subject option')).toEqual(['Choose…', 'acme', 'alex', 'bergen', 'oslo', 'sam']);
    expect($('subject-prompt').hidden).toBe(false);
    expect($('details').hidden).toBe(true);
    expect($('graph-note').hidden).toBe(true);
    expect($('empty').hidden).toBe(true);
  });

  it("lists a chosen entity's facts and reads its history, scrubbing back through it", async () => {
    const istota = run();
    await vi.waitFor(() => expect(node('alex')).toBeTruthy());
    node('alex').dispatchEvent(new MouseEvent('click', { bubbles: true }));
    expect(istota.skill).toHaveBeenLastCalledWith('memory_search.timeline', { subject: 'alex' });
    expect($('subject-heading').textContent).toBe('alex');
    expect(node('alex').classList.contains('is-selected')).toBe(true);
    expect(node('sam').classList.contains('is-near')).toBe(true);
    expect(node('bergen').classList.contains('is-near')).toBe(false);
    expect(texts('.map-edge-label.is-near')).toEqual(['works at', 'lives in', 'knows']);
    expect(texts('#current .map-row-text')).toEqual([
      'lives in oslo',
      'works at acme',
      'knows sam',
    ]);
    expect(texts('#current .map-row-meta')[0]).toBe('learned 2024-03-01 · still held');

    await vi.waitFor(() => expect($('scrubber').hidden).toBe(false));
    expect($('scrub-day').textContent).toBe('Today');
    expect($('scrub-count').textContent).toBe('Istota held 2 of these 3 facts.');
    expect(historyRows()).toEqual([
      ['lives in bergen', false],
      ['lives in oslo', true],
      ['works at acme', true],
    ]);

    const scrub = $('scrub') as HTMLInputElement;
    expect(scrub.max).toBe('2');
    scrub.value = '0';
    scrub.dispatchEvent(new Event('input'));
    expect($('scrub-day').textContent).toBe('On 2019-05-01');
    expect(historyRows()).toEqual([
      ['lives in bergen', true],
      ['lives in oslo', false],
      ['works at acme', true],
    ]);
  });

  it('chooses an entity from the keyboard, or from the list', async () => {
    const istota = run();
    await vi.waitFor(() => expect(node('sam')).toBeTruthy());
    expect(node('sam').getAttribute('tabindex')).toBe('0');
    node('sam').dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    expect($('subject-heading').textContent).toBe('sam');
    // Sam is the subject of one fact and the object of another.
    expect(texts('#current .map-row-text')).toEqual(['lives in bergen', 'alex knows sam']);
    const select = $('subject') as HTMLSelectElement;
    select.value = 'acme';
    select.dispatchEvent(new Event('change'));
    expect($('subject-heading').textContent).toBe('acme');
    expect(istota.skill).toHaveBeenLastCalledWith('memory_search.timeline', { subject: 'acme' });
  });

  it('says so when the history was not allowed, and still lists the current facts', async () => {
    run(FACTS, new IstotaError('not_granted', 'not granted'));
    await vi.waitFor(() => expect(node('alex')).toBeTruthy());
    node('alex').dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await vi.waitFor(() =>
      expect($('history-note').textContent).toBe(
        'Memory map has not been allowed to read the history of what Istota knows. Allow it from Permissions in this app’s menu.',
      ),
    );
    expect($('scrubber').hidden).toBe(true);
    expect(texts('#current .map-row-text')).toHaveLength(3);
  });

  it('asks for the facts when they were not allowed', async () => {
    run(new IstotaError('not_granted', 'not granted'));
    await vi.waitFor(() => expect($('empty').hidden).toBe(false));
    expect($('empty-title').textContent).toBe('Memory map cannot see what Istota knows.');
    expect($('body').hidden).toBe(true);
    expect($('subject-picker').hidden).toBe(true);
  });

  it('says Istota has learned nothing yet when there are no facts', async () => {
    run({ facts: [], truncated: false });
    await vi.waitFor(() => expect($('empty').hidden).toBe(false));
    expect($('empty-title').textContent).toBe('Istota has not learned any facts yet.');
    expect($('status').textContent).toBe('');
  });

  it('draws only the best connected entities of a large graph, and says so', async () => {
    const many = Array.from({ length: 150 }, (_, i) =>
      fact(i + 1, 'hub', 'knows', `person ${String(i).padStart(3, '0')}`),
    );
    run({ facts: many, truncated: false });
    await vi.waitFor(() => expect($('graph-note').hidden).toBe(false));
    expect(document.querySelectorAll('.map-node')).toHaveLength(120);
    expect(node('hub')).toBeTruthy();
    expect($('graph-note').textContent).toBe(
      'Showing the 120 best connected of 151 entities. Choose any of them from the list above.',
    );
    expect(document.querySelectorAll('#subject option')).toHaveLength(152);
  });
});
