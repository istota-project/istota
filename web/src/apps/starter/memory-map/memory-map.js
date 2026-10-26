// Memory map's page: reads the knowledge graph's current facts through
// `skill:memory_search.facts` and draws them as entities joined by the facts
// between them. Choosing an entity lists what Istota holds about it now and
// reads its history through `skill:memory_search.timeline`, which takes one
// subject at a time; the scrubber steps through the days its facts were
// learned and stopped holding. Either read may be refused ("Not now" on the
// first-open screen); the page says what it is missing.
(() => {
  const istota = /** @type {any} */ (globalThis).istota;
  const Model = /** @type {any} */ (globalThis).MemoryMapModel;
  const Layout = /** @type {any} */ (globalThis).GraphLayout;
  const SVG = 'http://www.w3.org/2000/svg';
  /** More than this and the names cannot be read; the rest stay in the list. */
  const MAX_NODES = 120;
  /** The same picture every time the map opens. */
  const SEED = 1;
  const LABEL_MAX = 24;

  /** @param {string} id */
  const byId = (id) => /** @type {HTMLElement} */ (document.getElementById(id));
  const statusEl = byId('status');
  const emptyEl = byId('empty');
  const bodyEl = byId('body');
  const pickerEl = byId('subject-picker');
  const selectEl = /** @type {HTMLSelectElement} */ (byId('subject'));
  const svgEl = byId('graph');
  const graphNoteEl = byId('graph-note');
  const headingEl = byId('subject-heading');
  const promptEl = byId('subject-prompt');
  const detailsEl = byId('details');
  const currentEl = byId('current');
  const currentEmptyEl = byId('current-empty');
  const historyNoteEl = byId('history-note');
  const scrubberEl = byId('scrubber');
  const scrubEl = /** @type {HTMLInputElement} */ (byId('scrub'));
  const scrubDayEl = byId('scrub-day');
  const scrubCountEl = byId('scrub-count');
  const historyEl = byId('history');

  /** @type {MapFact[]} */
  let facts = [];
  /** @type {MapGraph} */
  let graph = { nodes: [], edges: [] };
  let selected = '';
  /** @type {MapEntry[]} */
  let history = [];
  /** @type {string[]} */
  let stops = [];
  /** Each timeline read is numbered, so a slow answer for an earlier choice is dropped. */
  let historyRequest = 0;

  /** @param {unknown} error */
  function codeOf(error) {
    return /** @type {{code?: string}} */ (error || {}).code || 'internal';
  }

  /** @param {unknown} error */
  function messageOf(error) {
    return /** @type {{message?: string}} */ (error || {}).message || 'no answer';
  }

  /** @param {string} predicate */
  const words = (predicate) => predicate.replace(/_/g, ' ');

  /** @param {string} name */
  const short = (name) => (name.length > LABEL_MAX ? `${name.slice(0, LABEL_MAX - 1)}…` : name);

  function today() {
    const now = new Date();
    const pad = (/** @type {number} */ n) => String(n).padStart(2, '0');
    return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  }

  /**
   * @param {string} tag
   * @param {string} [className]
   * @param {string} [text]
   */
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  /**
   * @param {string} tag
   * @param {Record<string, string | number>} attributes
   */
  function svg(tag, attributes) {
    const node = document.createElementNS(SVG, tag);
    for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
    return node;
  }

  /**
   * @param {string} text
   * @param {string} meta
   * @param {string} [className]
   */
  function row(text, meta, className) {
    const item = el('div', `map-row${className ? ` ${className}` : ''}`);
    item.append(el('span', 'map-row-text', text), el('span', 'map-row-meta', meta));
    return item;
  }

  /** @param {{validFrom: string | null, validUntil: string | null, temporary: boolean}} fact */
  function span(fact) {
    const from = fact.validFrom ? `learned ${fact.validFrom}` : 'learned on a day not recorded';
    const until = fact.validUntil ? `until ${fact.validUntil}` : 'still held';
    return [from, until, ...(fact.temporary ? ['temporary'] : [])].join(' · ');
  }

  async function load() {
    statusEl.textContent = '';
    let problem = '';
    let truncated = false;
    try {
      const answer = await istota.skill('memory_search.facts');
      facts = Model.parseFacts(answer);
      truncated = Boolean(answer && answer.truncated);
    } catch (error) {
      facts = [];
      problem = codeOf(error);
      if (problem !== 'not_granted' && problem !== 'not_found') {
        statusEl.textContent = `Could not read what Istota knows: ${messageOf(error)}.`;
      }
    }
    graph = Model.buildGraph(facts);
    if (selected && !graph.nodes.some((/** @type {MapNode} */ n) => n.id === selected)) {
      selected = '';
    }
    showEmpty(problem);
    if (!graph.nodes.length) return;
    if (truncated) statusEl.textContent = 'Only the first 5,000 facts were read.';
    renderPicker();
    renderGraph();
    renderSelection();
    if (selected) readHistory(selected);
  }

  /** @param {string} problem */
  function showEmpty(problem) {
    const none = graph.nodes.length === 0;
    emptyEl.hidden = !none;
    bodyEl.hidden = none;
    pickerEl.hidden = none;
    if (!none) return;
    const [title, detail] =
      problem === 'not_granted'
        ? [
            'Memory map cannot see what Istota knows.',
            'Allow it to read your facts from Permissions in this app’s menu.',
          ]
        : problem === 'not_found'
          ? [
              'Memory search is off.',
              'Memory map reads the facts Istota has learned through the memory search skill, which is turned off for you.',
            ]
          : [
              'Istota has not learned any facts yet.',
              'As you talk, Istota learns facts about you and the people, places and things in your life, and they appear here.',
            ];
    byId('empty-title').textContent = title;
    byId('empty-detail').textContent = detail;
  }

  function renderPicker() {
    const choose = /** @type {HTMLOptionElement} */ (el('option', '', 'Choose…'));
    choose.value = '';
    selectEl.replaceChildren(
      choose,
      ...graph.nodes.map((/** @type {MapNode} */ node) => {
        const option = /** @type {HTMLOptionElement} */ (el('option', '', node.id));
        option.value = node.id;
        return option;
      }),
    );
    selectEl.value = selected;
  }

  function renderGraph() {
    const drawn = Model.limitGraph(graph, MAX_NODES);
    graphNoteEl.hidden = drawn.hidden === 0;
    graphNoteEl.textContent = `Showing the ${drawn.nodes.length} best connected of ${graph.nodes.length} entities. Choose any of them from the list above.`;
    const placed = Layout.layout(
      drawn.nodes.map((/** @type {MapNode} */ n) => n.id),
      drawn.edges.map((/** @type {MapEdge} */ e) => [e.source, e.target]),
      { seed: SEED },
    );
    const points = Object.values(placed);
    const xs = points.map((p) => p.x);
    const ys = points.map((p) => p.y);
    // Fit the view to the drawing, with room for the labels beside the nodes.
    const left = Math.min(...xs) - 40;
    const top = Math.min(...ys) - 40;
    const width = Math.max(Math.max(...xs) - left + 200, 400);
    const height = Math.max(Math.max(...ys) - top + 40, 240);
    svgEl.setAttribute('viewBox', `${left} ${top} ${width} ${height}`);

    const edgeLayer = svg('g', { class: 'map-edges' });
    const labelLayer = svg('g', { class: 'map-edge-labels' });
    for (const edge of drawn.edges) {
      const a = placed[edge.source];
      const b = placed[edge.target];
      const line = svg('line', { class: 'map-edge', x1: a.x, y1: a.y, x2: b.x, y2: b.y });
      line.dataset.source = edge.source;
      line.dataset.target = edge.target;
      edgeLayer.append(line);
      const label = svg('text', {
        class: 'map-edge-label',
        x: (a.x + b.x) / 2,
        y: (a.y + b.y) / 2,
        'text-anchor': 'middle',
      });
      label.dataset.source = edge.source;
      label.dataset.target = edge.target;
      label.textContent = edge.facts
        .map((/** @type {MapFact} */ f) => words(f.predicate))
        .join(', ');
      labelLayer.append(label);
    }

    const nodeLayer = svg('g', { class: 'map-nodes' });
    for (const node of drawn.nodes) {
      const at = placed[node.id];
      const radius = 6 + Math.min(10, 2 * Math.sqrt(node.degree));
      const group = svg('g', {
        class: 'map-node',
        tabindex: 0,
        role: 'button',
        'aria-label': `${node.id}, ${node.facts} ${node.facts === 1 ? 'fact' : 'facts'}`,
      });
      group.dataset.id = node.id;
      const title = svg('title', {});
      title.textContent = node.id;
      const label = svg('text', { class: 'map-label', x: at.x + radius + 4, y: at.y + 5 });
      label.textContent = short(node.id);
      group.append(title, svg('circle', { cx: at.x, cy: at.y, r: radius }), label);
      group.addEventListener('click', () => choose(node.id));
      group.addEventListener('keydown', (event) => {
        const key = /** @type {KeyboardEvent} */ (event).key;
        if (key !== 'Enter' && key !== ' ') return;
        event.preventDefault();
        choose(node.id);
      });
      nodeLayer.append(group);
    }
    svgEl.replaceChildren(edgeLayer, labelLayer, nodeLayer);
  }

  /** @param {string} id */
  function choose(id) {
    if (id === selected) return;
    selected = id;
    selectEl.value = id;
    renderSelection();
    if (id) readHistory(id);
  }

  function renderSelection() {
    const near = new Set([selected]);
    for (const edge of svgEl.querySelectorAll('.map-edge, .map-edge-label')) {
      const { source, target } = /** @type {SVGElement} */ (edge).dataset;
      const touches = Boolean(selected) && (source === selected || target === selected);
      edge.classList.toggle('is-near', touches);
      if (touches) {
        near.add(/** @type {string} */ (source));
        near.add(/** @type {string} */ (target));
      }
    }
    for (const node of svgEl.querySelectorAll('.map-node')) {
      const id = /** @type {SVGElement} */ (node).dataset.id || '';
      node.classList.toggle('is-selected', id === selected);
      node.classList.toggle('is-near', Boolean(selected) && id !== selected && near.has(id));
      node.setAttribute('aria-pressed', String(id === selected));
    }

    promptEl.hidden = Boolean(selected);
    detailsEl.hidden = !selected;
    headingEl.textContent = selected || 'Choose an entity';
    if (!selected) return;
    const { about, mentions } = Model.factsAbout(facts, selected);
    currentEmptyEl.hidden = about.length + mentions.length > 0;
    currentEmptyEl.textContent = `Istota holds no current fact about ${selected}.`;
    currentEl.hidden = about.length + mentions.length === 0;
    currentEl.replaceChildren(
      ...about.map((/** @type {MapFact} */ f) => row(`${words(f.predicate)} ${f.object}`, span(f))),
      ...mentions.map((/** @type {MapFact} */ f) =>
        row(`${f.subject} ${words(f.predicate)} ${selected}`, span(f), 'map-row--mention'),
      ),
    );
  }

  /** @param {string} subject */
  async function readHistory(subject) {
    const request = ++historyRequest;
    history = [];
    stops = [];
    scrubberEl.hidden = true;
    historyEl.replaceChildren();
    historyNoteEl.hidden = false;
    historyNoteEl.textContent = 'Reading the history…';
    let note = '';
    try {
      history = Model.parseTimeline(await istota.skill('memory_search.timeline', { subject }));
      if (!history.length)
        note = `Istota holds no history for ${subject} as the subject of a fact.`;
    } catch (error) {
      const code = codeOf(error);
      if (code === 'not_granted') {
        note =
          'Memory map has not been allowed to read the history of what Istota knows. Allow it from Permissions in this app’s menu.';
      } else if (code === 'not_found') {
        note = 'The history is not available: memory search is turned off for you.';
      } else {
        note = `Could not read the history: ${messageOf(error)}.`;
      }
    }
    if (request !== historyRequest) return;
    historyNoteEl.hidden = !note;
    historyNoteEl.textContent = note;
    if (!history.length) return;
    stops = Model.timelineStops(history, today());
    scrubEl.max = String(stops.length - 1);
    scrubEl.value = String(stops.length - 1);
    scrubberEl.hidden = stops.length < 2;
    renderHistory();
  }

  function renderHistory() {
    const stop = Math.min(Number(scrubEl.value) || 0, stops.length - 1);
    const day = stops[stop];
    const held = history.filter((/** @type {MapEntry} */ e) => Model.heldOn(e, day));
    scrubDayEl.textContent = day === today() ? 'Today' : `On ${day}`;
    scrubCountEl.textContent = `Istota held ${held.length} of these ${history.length} ${
      history.length === 1 ? 'fact' : 'facts'
    }.`;
    historyEl.replaceChildren(
      ...history.map((/** @type {MapEntry} */ e) => {
        const holds = Model.heldOn(e, day);
        const item = row(
          `${words(e.predicate)} ${e.object}`,
          span(e),
          holds ? 'is-held' : 'is-not-held',
        );
        if (!holds) item.append(el('span', 'map-visually-hidden', ' (not held on this day)'));
        return item;
      }),
    );
  }

  scrubEl.addEventListener('input', renderHistory);
  selectEl.addEventListener('change', () => choose(selectEl.value));
  byId('refresh').addEventListener('click', load);
  // Back after a while: Istota may have learned something since.
  let visibility = istota.env.visibility;
  istota.on('env', (/** @type {{visibility: string}} */ env) => {
    const returned = visibility === 'hidden' && env.visibility === 'visible';
    visibility = env.visibility;
    if (returned) load();
  });

  load();
})();
