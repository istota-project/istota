import { describe, expect, it } from 'vitest';
import { loadStarterScript } from '../../testing/classicScript';

type Point = { x: number; y: number };
type Options = { seed?: number; width?: number; height?: number; iterations?: number };
type Layout = {
  layout(nodes: string[], edges: Array<[string, string]>, options?: Options): Record<string, Point>;
};

function graphLayout(): Layout {
  return loadStarterScript('memory-map', 'graph-layout.js').GraphLayout as Layout;
}

const NODES = ['alex', 'oslo', 'aviation', 'sam', 'acme', 'bergen', 'tea', 'cessna 172'];
const EDGES: Array<[string, string]> = [
  ['alex', 'oslo'],
  ['alex', 'aviation'],
  ['alex', 'sam'],
  ['alex', 'acme'],
  ['sam', 'bergen'],
  ['alex', 'tea'],
  ['aviation', 'cessna 172'],
];

const distance = (a: Point, b: Point) => Math.hypot(a.x - b.x, a.y - b.y);

describe('GraphLayout.layout', () => {
  it('places every node, the same way each time for one seed', () => {
    const first = graphLayout().layout(NODES, EDGES, { seed: 7 });
    // A fresh context: nothing carries over from the first run.
    const second = graphLayout().layout(NODES, EDGES, { seed: 7 });
    expect(Object.keys(first).sort()).toEqual([...NODES].sort());
    expect(second).toEqual(first);
  });

  it('does not depend on the order or direction the graph was given in', () => {
    const G = graphLayout();
    const given = G.layout(NODES, EDGES, { seed: 7 });
    const shuffled = G.layout(
      [...NODES].reverse(),
      [...EDGES].reverse().map(([a, b]) => [b, a] as [string, string]),
      { seed: 7 },
    );
    expect(shuffled).toEqual(given);
  });

  it('lays the graph out differently for another seed', () => {
    const G = graphLayout();
    expect(G.layout(NODES, EDGES, { seed: 8 })).not.toEqual(G.layout(NODES, EDGES, { seed: 7 }));
  });

  it('keeps every node inside the box it was given', () => {
    const many = Array.from({ length: 60 }, (_, i) => `n${i}`);
    const star = many.slice(1).map((n) => [many[0], n] as [string, string]);
    const placed = graphLayout().layout(many, star, { seed: 3, width: 400, height: 300 });
    for (const point of Object.values(placed)) {
      expect(point.x).toBeGreaterThanOrEqual(0);
      expect(point.x).toBeLessThanOrEqual(400);
      expect(point.y).toBeGreaterThanOrEqual(0);
      expect(point.y).toBeLessThanOrEqual(300);
    }
  });

  it('draws connected nodes together and keeps them apart from the rest', () => {
    const left = ['a1', 'a2', 'a3', 'a4', 'a5'];
    const right = ['b1', 'b2', 'b3', 'b4', 'b5'];
    /** Every pair in a group, so each group is one clique. */
    const clique = (group: string[]) =>
      group.flatMap((a, i) => group.slice(i + 1).map((b) => [a, b] as [string, string]));
    const placed = graphLayout().layout([...left, ...right], [...clique(left), ...clique(right)], {
      seed: 11,
    });
    const mean = (pairs: Array<[string, string]>) =>
      pairs.reduce((sum, [a, b]) => sum + distance(placed[a], placed[b]), 0) / pairs.length;
    const within = mean([...clique(left), ...clique(right)]);
    const across = mean(left.flatMap((a) => right.map((b) => [a, b] as [string, string])));
    expect(within * 2).toBeLessThan(across);
  });

  it('never stacks two nodes on one point', () => {
    const many = Array.from({ length: 40 }, (_, i) => `leaf ${i}`);
    const star = many.map((n) => ['hub', n] as [string, string]);
    const placed = graphLayout().layout(['hub', ...many], star, { seed: 1 });
    const points = Object.values(placed);
    let closest = Infinity;
    for (let i = 0; i < points.length; i++) {
      for (let j = i + 1; j < points.length; j++) {
        closest = Math.min(closest, distance(points[i], points[j]));
      }
    }
    expect(closest).toBeGreaterThan(10);
  });

  it('places nothing for no nodes, and one node in the middle', () => {
    const G = graphLayout();
    expect(G.layout([], [], { seed: 1 })).toEqual({});
    expect(G.layout(['alone'], [], { seed: 1, width: 400, height: 200 })).toEqual({
      alone: { x: 200, y: 100 },
    });
  });

  it('ignores self-loops, repeated edges and edges to nodes it was not given', () => {
    const G = graphLayout();
    const clean = G.layout(NODES, EDGES, { seed: 5 });
    const noisy = G.layout(
      [...NODES, 'oslo'],
      [...EDGES, ['alex', 'alex'], ['oslo', 'alex'], ['alex', 'nowhere']],
      { seed: 5 },
    );
    expect(noisy).toEqual(clean);
  });
});
