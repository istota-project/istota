// Memory map's logic: the knowledge graph's current facts as entities and
// the edges between them, and one subject's history as the days its facts
// were learned and stopped holding. No DOM and no bridge; memory-map.js
// draws the result and graph-layout.js places it.
//
// A fact is held from its `valid_from` day (or always, when that was not
// recorded) until the day before its `valid_until`, the rule the knowledge
// graph itself uses for "current" (`valid_until > today`).

/**
 * @typedef {{id: string, subject: string, predicate: string, object: string,
 *   validFrom: string | null, validUntil: string | null, temporary: boolean}} MapFact
 * @typedef {Omit<MapFact, 'subject'>} MapEntry
 * @typedef {{id: string, degree: number, facts: number}} MapNode
 * @typedef {{source: string, target: string, facts: MapFact[]}} MapEdge
 * @typedef {{nodes: MapNode[], edges: MapEdge[]}} MapGraph
 */
(() => {
  /** @param {unknown} value */
  const text = (value) => (typeof value === 'string' ? value.trim() : '');

  /**
   * The `YYYY-MM-DD` a date or a date-time starts with, else null.
   * @param {unknown} value
   * @returns {string | null}
   */
  function dayOf(value) {
    if (typeof value !== 'string') return null;
    const match = /^(\d{4}-\d{2}-\d{2})/.exec(value.trim());
    return match ? match[1] : null;
  }

  /**
   * @param {unknown} item
   * @returns {MapEntry | null}
   */
  function entryOf(item) {
    if (!item || typeof item !== 'object') return null;
    const raw = /** @type {Record<string, unknown>} */ (item);
    const object = text(raw.object);
    if (!object) return null;
    return {
      id: String(raw.id),
      predicate: text(raw.predicate),
      object,
      validFrom: dayOf(raw.valid_from),
      validUntil: dayOf(raw.valid_until),
      temporary: raw.temporary === true,
    };
  }

  /**
   * The facts `memory_search.facts` answered, those with a subject and an object.
   * @param {unknown} result
   * @returns {MapFact[]}
   */
  function parseFacts(result) {
    const list = /** @type {{facts?: unknown}} */ (result || {}).facts;
    if (!Array.isArray(list)) return [];
    /** @type {MapFact[]} */
    const facts = [];
    for (const item of list) {
      const entry = entryOf(item);
      const subject = text(/** @type {{subject?: unknown}} */ (item || {}).subject);
      if (!entry || !subject) continue;
      facts.push({ ...entry, subject });
    }
    return facts;
  }

  /**
   * One subject's facts as `memory_search.timeline` answered them, in its order.
   * @param {unknown} result
   * @returns {MapEntry[]}
   */
  function parseTimeline(result) {
    const list = /** @type {{facts?: unknown}} */ (result || {}).facts;
    if (!Array.isArray(list)) return [];
    /** @type {MapEntry[]} */
    const entries = [];
    for (const item of list) {
      const entry = entryOf(item);
      if (entry) entries.push(entry);
    }
    return entries;
  }

  /** @param {string} a @param {string} b */
  const byText = (a, b) => (a < b ? -1 : a > b ? 1 : 0);

  /**
   * Every entity a fact names, and one edge per subject and object holding
   * the facts between them. A node's degree counts its neighbours, not its facts.
   * @param {MapFact[]} facts
   * @returns {MapGraph}
   */
  function buildGraph(facts) {
    /** @type {Map<string, Set<string>>} */
    const neighbours = new Map();
    /** @type {Map<string, number>} */
    const counts = new Map();
    /** @type {Map<string, MapEdge>} */
    const edges = new Map();
    /** @param {string} id */
    const touch = (id) => {
      if (!neighbours.has(id)) neighbours.set(id, new Set());
      counts.set(id, (counts.get(id) || 0) + 1);
    };
    for (const fact of facts) {
      touch(fact.subject);
      if (fact.object === fact.subject) continue;
      touch(fact.object);
      /** @type {Set<string>} */ (neighbours.get(fact.subject)).add(fact.object);
      /** @type {Set<string>} */ (neighbours.get(fact.object)).add(fact.subject);
      const key = JSON.stringify([fact.subject, fact.object]);
      const edge = edges.get(key) || { source: fact.subject, target: fact.object, facts: [] };
      edge.facts.push(fact);
      edges.set(key, edge);
    }
    const nodes = [...neighbours.keys()].sort(byText).map((id) => ({
      id,
      degree: /** @type {Set<string>} */ (neighbours.get(id)).size,
      facts: counts.get(id) || 0,
    }));
    const sorted = [...edges.values()].sort(
      (a, b) => byText(a.source, b.source) || byText(a.target, b.target),
    );
    return { nodes, edges: sorted };
  }

  /**
   * At most `max` entities, the best connected first, plus `keep` when it is
   * one of them, and the edges between those kept.
   * @param {MapGraph} graph
   * @param {number} max
   * @param {string} [keep]
   * @returns {MapGraph & {hidden: number}}
   */
  function limitGraph(graph, max, keep) {
    const ranked = [...graph.nodes].sort(
      (a, b) => b.degree - a.degree || b.facts - a.facts || byText(a.id, b.id),
    );
    const kept = new Set(ranked.slice(0, max).map((n) => n.id));
    if (keep && graph.nodes.some((n) => n.id === keep)) kept.add(keep);
    return {
      nodes: graph.nodes.filter((n) => kept.has(n.id)),
      edges: graph.edges.filter((e) => kept.has(e.source) && kept.has(e.target)),
      hidden: graph.nodes.length - kept.size,
    };
  }

  /**
   * @param {MapFact[]} facts
   * @param {string} id
   */
  function factsAbout(facts, id) {
    return {
      about: facts.filter((f) => f.subject === id),
      mentions: facts.filter((f) => f.object === id && f.subject !== id),
    };
  }

  /**
   * The days the scrubber stops on: each day a fact was learned or stopped
   * holding, then today when it is later than all of them.
   * @param {MapEntry[]} entries
   * @param {string} today
   * @returns {string[]}
   */
  function timelineStops(entries, today) {
    /** @type {Set<string>} */
    const days = new Set();
    for (const entry of entries) {
      if (entry.validFrom) days.add(entry.validFrom);
      if (entry.validUntil) days.add(entry.validUntil);
    }
    const stops = [...days].sort(byText);
    if (!stops.length || stops[stops.length - 1] < today) stops.push(today);
    return stops;
  }

  /**
   * @param {MapEntry} entry
   * @param {string} day
   */
  function heldOn(entry, day) {
    if (entry.validFrom && entry.validFrom > day) return false;
    return !entry.validUntil || entry.validUntil > day;
  }

  /** @type {any} */ (globalThis).MemoryMapModel = {
    dayOf,
    parseFacts,
    parseTimeline,
    buildGraph,
    limitGraph,
    factsAbout,
    timelineStops,
    heldOn,
  };
})();
