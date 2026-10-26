// Memory map's graph layout: a force-directed placement (Fruchterman and
// Reingold, 1991) of named nodes in a box, with no DOM. The same nodes,
// edges and seed always give the same positions: the nodes are sorted before
// the seeded generator places them, and the order or direction the edges
// came in does not matter, so the map looks the same each time it opens.
//
// Every pair of nodes pushes apart, every edge pulls its two ends together,
// and a weak pull towards the middle keeps a graph of several parts from
// drifting to the edges. The step each node may take shrinks every round.

/** @typedef {{x: number, y: number}} GraphPoint */
/**
 * @typedef {{seed?: number, width?: number, height?: number, iterations?: number}} GraphLayoutOptions
 */
(() => {
  const WIDTH = 1000;
  const HEIGHT = 700;
  const ITERATIONS = 300;
  const GRAVITY = 0.1;
  /** The share of the box kept clear at each edge, so a label beside a node still fits. */
  const PADDING = 0.04;

  /**
   * mulberry32: a small seeded generator, uniform on [0, 1).
   * @param {number} seed
   */
  function generator(seed) {
    let state = seed >>> 0;
    return () => {
      state = (state + 0x6d2b79f5) >>> 0;
      let t = state;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  /** @param {number} value */
  const round = (value) => Math.round(value * 100) / 100;

  /**
   * @param {string[]} nodes
   * @param {Array<[string, string]>} edges
   * @param {GraphLayoutOptions} [options]
   * @returns {Record<string, GraphPoint>}
   */
  function layout(nodes, edges, options = {}) {
    const width = options.width ?? WIDTH;
    const height = options.height ?? HEIGHT;
    const iterations = options.iterations ?? ITERATIONS;
    const random = generator(options.seed ?? 1);

    const ids = [...new Set(nodes)].sort();
    /** @type {Record<string, GraphPoint>} */
    const placed = {};
    if (ids.length === 0) return placed;
    if (ids.length === 1) {
      placed[ids[0]] = { x: round(width / 2), y: round(height / 2) };
      return placed;
    }

    /** @type {Map<string, number>} */
    const index = new Map(ids.map((id, i) => [id, i]));
    /** @type {Set<string>} */
    const pairs = new Set();
    for (const [a, b] of edges) {
      const i = index.get(a);
      const j = index.get(b);
      if (i === undefined || j === undefined || i === j) continue;
      pairs.add(i < j ? `${i},${j}` : `${j},${i}`);
    }
    const links = [...pairs]
      .map((key) => key.split(',').map(Number))
      .sort((p, q) => p[0] - q[0] || p[1] - q[1]);

    const left = width * PADDING;
    const right = width - left;
    const top = height * PADDING;
    const bottom = height - top;
    const xs = ids.map(() => left + random() * (right - left));
    const ys = ids.map(() => top + random() * (bottom - top));
    const n = ids.length;
    const k = 0.6 * Math.sqrt(((right - left) * (bottom - top)) / n);
    const start = Math.min(width, height) / 10;
    const dx = new Float64Array(n);
    const dy = new Float64Array(n);

    for (let round_ = 0; round_ < iterations; round_++) {
      dx.fill(0);
      dy.fill(0);
      for (let i = 0; i < n; i++) {
        for (let j = i + 1; j < n; j++) {
          let ex = xs[i] - xs[j];
          let ey = ys[i] - ys[j];
          let d = Math.hypot(ex, ey);
          // Two nodes on one point: push them apart along x, the same way every time.
          if (d < 0.01) {
            ex = 0.01;
            ey = 0;
            d = 0.01;
          }
          const force = (k * k) / d;
          const fx = (ex / d) * force;
          const fy = (ey / d) * force;
          dx[i] += fx;
          dy[i] += fy;
          dx[j] -= fx;
          dy[j] -= fy;
        }
      }
      for (const [i, j] of links) {
        const ex = xs[i] - xs[j];
        const ey = ys[i] - ys[j];
        const d = Math.hypot(ex, ey);
        if (d === 0) continue;
        const force = (d * d) / k;
        const fx = (ex / d) * force;
        const fy = (ey / d) * force;
        dx[i] -= fx;
        dy[i] -= fy;
        dx[j] += fx;
        dy[j] += fy;
      }
      const temperature = start * (1 - round_ / iterations);
      for (let i = 0; i < n; i++) {
        dx[i] += (width / 2 - xs[i]) * GRAVITY * (k / 10);
        dy[i] += (height / 2 - ys[i]) * GRAVITY * (k / 10);
        const d = Math.hypot(dx[i], dy[i]);
        if (d === 0) continue;
        const step = Math.min(d, temperature);
        xs[i] = Math.min(right, Math.max(left, xs[i] + (dx[i] / d) * step));
        ys[i] = Math.min(bottom, Math.max(top, ys[i] + (dy[i] / d) * step));
      }
    }

    for (let i = 0; i < n; i++) placed[ids[i]] = { x: round(xs[i]), y: round(ys[i]) };
    return placed;
  }

  /** @type {any} */ (globalThis).GraphLayout = { WIDTH, HEIGHT, layout };
})();
