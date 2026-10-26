import { describe, expect, it } from 'vitest';
import { loadStarterScript } from '../../testing/classicScript';

type Fact = {
  id: string;
  subject: string;
  predicate: string;
  object: string;
  validFrom: string | null;
  validUntil: string | null;
  temporary: boolean;
};
type Entry = Omit<Fact, 'subject'>;
type Node = { id: string; degree: number; facts: number };
type Edge = { source: string; target: string; facts: Fact[] };
type Graph = { nodes: Node[]; edges: Edge[] };

type Model = {
  parseFacts(result: unknown): Fact[];
  parseTimeline(result: unknown): Entry[];
  buildGraph(facts: Fact[]): Graph;
  limitGraph(graph: Graph, max: number, keep?: string): Graph & { hidden: number };
  factsAbout(facts: Fact[], id: string): { about: Fact[]; mentions: Fact[] };
  dayOf(value: unknown): string | null;
  timelineStops(entries: Entry[], today: string): string[];
  heldOn(entry: Entry, day: string): boolean;
};

function model(): Model {
  return loadStarterScript('memory-map', 'memory-map-model.js').MemoryMapModel as Model;
}

/** Facts as `memory_search.facts` answers them, through `fact_to_dict`. */
function wire(id: number, subject: string, predicate: string, object: string, extra = {}) {
  return {
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
  };
}

const ANSWER = {
  facts: [
    wire(1, 'alex', 'lives_in', 'oslo', { valid_from: '2024-03-01' }),
    wire(2, 'alex', 'works_at', 'acme'),
    wire(3, 'alex', 'knows', 'sam'),
    wire(4, 'sam', 'lives_in', 'bergen'),
    wire(5, 'sam', 'knows', 'alex'),
    wire(6, 'alex', 'likes', 'oslo'),
  ],
  truncated: false,
};

describe('parseFacts', () => {
  it('reads the facts answer, and drops a fact without a subject or an object', () => {
    const M = model();
    const facts = M.parseFacts({
      facts: [...ANSWER.facts, { id: 9, subject: '', predicate: 'x', object: 'y' }, null, 'junk'],
    });
    expect(facts).toHaveLength(6);
    expect(facts[0]).toEqual({
      id: '1',
      subject: 'alex',
      predicate: 'lives_in',
      object: 'oslo',
      validFrom: '2024-03-01',
      validUntil: null,
      temporary: false,
    });
    expect(M.parseFacts(null)).toEqual([]);
    expect(M.parseFacts({ facts: 'no' })).toEqual([]);
  });
});

describe('buildGraph', () => {
  it('makes one node per entity and one edge per ordered pair, holding its facts', () => {
    const M = model();
    const graph = M.buildGraph(M.parseFacts(ANSWER));
    expect(graph.nodes.map((n) => n.id)).toEqual(['acme', 'alex', 'bergen', 'oslo', 'sam']);
    const alex = graph.nodes.find((n) => n.id === 'alex');
    // Neighbours, not facts: oslo twice and sam both ways still count once each.
    expect(alex).toEqual({ id: 'alex', degree: 3, facts: 5 });
    const toOslo = graph.edges.find((e) => e.source === 'alex' && e.target === 'oslo');
    expect(toOslo?.facts.map((f) => f.predicate)).toEqual(['lives_in', 'likes']);
    expect(graph.edges.map((e) => `${e.source}>${e.target}`)).toEqual([
      'alex>acme',
      'alex>oslo',
      'alex>sam',
      'sam>alex',
      'sam>bergen',
    ]);
  });

  it('leaves a fact about a thing and itself out of the edges', () => {
    const M = model();
    const graph = M.buildGraph(M.parseFacts({ facts: [wire(1, 'tea', 'is', 'tea')] }));
    expect(graph.nodes).toEqual([{ id: 'tea', degree: 0, facts: 1 }]);
    expect(graph.edges).toEqual([]);
  });
});

describe('limitGraph', () => {
  it('keeps the best connected entities, and the one asked for', () => {
    const M = model();
    const graph = M.buildGraph(M.parseFacts(ANSWER));
    const top = M.limitGraph(graph, 2);
    expect(top.nodes.map((n) => n.id)).toEqual(['alex', 'sam']);
    expect(top.edges.map((e) => `${e.source}>${e.target}`)).toEqual(['alex>sam', 'sam>alex']);
    expect(top.hidden).toBe(3);
    const kept = M.limitGraph(graph, 2, 'acme');
    expect(kept.nodes.map((n) => n.id)).toEqual(['acme', 'alex', 'sam']);
    expect(M.limitGraph(graph, 50).hidden).toBe(0);
  });
});

describe('factsAbout', () => {
  it('splits the facts into those about an entity and those naming it', () => {
    const M = model();
    const { about, mentions } = M.factsAbout(M.parseFacts(ANSWER), 'sam');
    expect(about.map((f) => f.id)).toEqual(['4', '5']);
    expect(mentions.map((f) => f.id)).toEqual(['3']);
  });
});

describe('the timeline', () => {
  const timeline = {
    subject: 'alex',
    facts: [
      {
        id: 1,
        predicate: 'lives_in',
        object: 'bergen',
        valid_from: null,
        valid_until: '2024-03-01',
      },
      { id: 2, predicate: 'lives_in', object: 'oslo', valid_from: '2024-03-01', valid_until: null },
      {
        id: 3,
        predicate: 'works_at',
        object: 'initech',
        valid_from: '2022-01-10T09:30:00',
        valid_until: '2025-06-30',
        temporary: true,
      },
    ],
    truncated: false,
  };

  it('reads the timeline answer', () => {
    const M = model();
    const entries = M.parseTimeline(timeline);
    expect(entries.map((e) => [e.id, e.object, e.validFrom, e.validUntil, e.temporary])).toEqual([
      ['1', 'bergen', null, '2024-03-01', false],
      ['2', 'oslo', '2024-03-01', null, false],
      ['3', 'initech', '2022-01-10', '2025-06-30', true],
    ]);
    expect(M.parseTimeline({ facts: [{ id: 4, predicate: 'x', object: '' }] })).toEqual([]);
  });

  it('scrubs over every day something was learned or stopped holding, then today', () => {
    const M = model();
    const entries = M.parseTimeline(timeline);
    expect(M.timelineStops(entries, '2026-10-25')).toEqual([
      '2022-01-10',
      '2024-03-01',
      '2025-06-30',
      '2026-10-25',
    ]);
    // Today is not added twice, nor before the last change.
    expect(M.timelineStops(entries, '2025-06-30')).toEqual([
      '2022-01-10',
      '2024-03-01',
      '2025-06-30',
    ]);
    expect(M.timelineStops([], '2026-10-25')).toEqual(['2026-10-25']);
  });

  it('holds a fact from the day it was learned until the day it stopped', () => {
    const M = model();
    const [bergen, oslo, initech] = M.parseTimeline(timeline);
    expect(M.heldOn(bergen, '2020-01-01')).toBe(true);
    expect(M.heldOn(bergen, '2024-03-01')).toBe(false);
    expect(M.heldOn(oslo, '2024-02-29')).toBe(false);
    expect(M.heldOn(oslo, '2024-03-01')).toBe(true);
    expect(M.heldOn(initech, '2025-06-29')).toBe(true);
    expect(M.heldOn(initech, '2025-06-30')).toBe(false);
  });

  it('reads a day from a date or a time, and nothing else', () => {
    const M = model();
    expect(M.dayOf('2026-10-25')).toBe('2026-10-25');
    expect(M.dayOf('2026-10-25 08:00:00')).toBe('2026-10-25');
    expect(M.dayOf('last week')).toBeNull();
    expect(M.dayOf(20261025)).toBeNull();
  });
});
