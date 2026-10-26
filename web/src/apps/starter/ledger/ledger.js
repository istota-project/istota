// Ledger's page: draws the guesses ledger-model.js reads, asks about the ones
// whose day has come, and writes each change to `app.ledger` through the
// kit's `istota.kv` as it happens.
(() => {
  const istota = /** @type {any} */ (globalThis).istota;
  const Model = /** @type {any} */ (globalThis).LedgerModel;
  const PAGE = 1000;

  /** @param {string} id */
  const byId = (id) => /** @type {HTMLElement} */ (document.getElementById(id));
  const statusEl = byId('status');
  const emptyEl = byId('empty');
  const scoreEl = byId('score');
  const chartEl = /** @type {HTMLElement & {data: unknown}} */ (byId('chart'));
  const binsEl = byId('bins');
  const lists = {
    due: byId('due'),
    open: byId('open'),
    resolved: byId('resolved'),
  };

  /** @type {LedgerGuess[]} */
  let guesses = [];
  let loaded = false;
  let dialogOpen = false;

  function now() {
    return new Date().toISOString();
  }

  /** @param {string} message */
  function say(message) {
    statusEl.textContent = message;
  }

  /** @param {unknown} error */
  function failed(error) {
    const err = /** @type {{message?: string}} */ (error || {});
    say(
      `Could not save: ${err.message || 'the change was refused'}. Refresh to see what is stored.`,
    );
  }

  /** @param {LedgerGuess} guess */
  async function write(guess) {
    try {
      await istota.kv.set(Model.guessKey(guess.id), guess);
    } catch (error) {
      failed(error);
    }
  }

  /** @param {LedgerChange | null} change */
  function apply(change) {
    if (!change) return;
    guesses = change.guesses;
    render();
    write(change.changed);
  }

  async function load() {
    say('');
    try {
      const items = [];
      for (let offset = 0; ;) {
        const page = await istota.kv.list({ prefix: 'guess:', offset, limit: PAGE });
        items.push(...page.items);
        offset += page.items.length;
        if (!page.items.length || offset >= page.total) break;
      }
      guesses = Model.fromStorage(items);
      loaded = true;
    } catch (error) {
      const err = /** @type {{message?: string}} */ (error || {});
      say(`Could not load the ledger: ${err.message || 'no answer'}.`);
    }
    render();
  }

  // -- drawing ---------------------------------------------------------------

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
   * @param {string} text
   * @param {string} variant
   * @param {() => void} onClick
   */
  function kitButton(text, variant, onClick) {
    const button = el('istota-button', '', text);
    button.setAttribute('variant', variant);
    button.addEventListener('click', onClick);
    return button;
  }

  /** @param {string | null} day */
  function dayLabel(day) {
    if (!day) return 'no date';
    const [year, month, date] = day.split('-').map(Number);
    return new Date(year, month - 1, date).toLocaleDateString(undefined, {
      day: 'numeric',
      month: 'short',
      year: 'numeric',
    });
  }

  /** @param {LedgerGuess} guess */
  function meta(guess) {
    const parts = [Model.percent(guess.p)];
    if (guess.outcome === null) parts.push(`resolves ${dayLabel(guess.resolves_on)}`);
    else parts.push(guess.outcome ? 'happened' : 'did not happen');
    return parts.join(' · ');
  }

  /**
   * @param {LedgerGuess} guess
   * @param {'due' | 'open' | 'resolved'} kind
   */
  function row(guess, kind) {
    const item = el('div', 'ledger-row');
    item.dataset.id = guess.id;
    const main = el('div', 'ledger-row-main');
    main.append(
      el('span', 'ledger-row-text', guess.text || 'Untitled guess'),
      el('span', 'ledger-row-meta', meta(guess)),
    );
    const actions = el('div', 'ledger-row-actions');
    if (kind === 'due') {
      actions.append(
        kitButton('Happened', 'primary', () => resolve(guess.id, true)),
        kitButton('Didn’t happen', 'secondary', () => resolve(guess.id, false)),
      );
    }
    const menu = /** @type {HTMLButtonElement} */ (el('button', 'ledger-icon-button', '⋯'));
    menu.type = 'button';
    menu.setAttribute('aria-label', `More for ${guess.text || 'guess'}`);
    menu.title = 'More';
    menu.addEventListener('click', () => guessMenu(guess.id));
    actions.append(menu);
    item.append(main, actions);
    return item;
  }

  function renderScore() {
    const brier = Model.brierScore(guesses);
    byId('score-section').hidden = !brier;
    if (!brier) return;
    const count = `${brier.count} resolved guess${brier.count === 1 ? '' : 'es'}`;
    scoreEl.textContent =
      `Brier score ${brier.score.toFixed(3)} over ${count}. Lower is better: 0 is perfect, ` +
      'and saying 50% every time scores 0.25.';
    /** @type {LedgerBin[]} */
    const bins = Model.calibration(guesses);
    const filled = bins.filter((b) => b.count > 0);
    /** @param {number | string} v */
    const pct = (v) => `${Math.round(Number(v))}%`;
    chartEl.data = {
      series: [
        {
          name: 'Your guesses',
          points: filled.map((b) => ({ x: (b.meanP ?? 0) * 100, y: (b.observed ?? 0) * 100 })),
        },
        {
          name: 'Perfectly calibrated',
          points: [
            { x: 0, y: 0 },
            { x: 100, y: 100 },
          ],
        },
      ],
      yMin: 0,
      yMax: 100,
      xFormat: pct,
      yFormat: pct,
    };
    binsEl.replaceChildren(
      ...filled.map((bin) => {
        const tr = el('tr');
        const range =
          bin.index === bins.length - 1
            ? `${Math.round(bin.lo * 100)}–100%`
            : `${Math.round(bin.lo * 100)}–${Math.round(bin.hi * 100) - 1}%`;
        tr.append(
          el('td', '', range),
          el('td', '', String(bin.count)),
          el('td', '', `${bin.hits} (${Model.percent(bin.observed)})`),
        );
        return tr;
      }),
    );
  }

  function render() {
    const parts = Model.sections(guesses, Model.localDay(new Date()));
    emptyEl.hidden = !loaded || guesses.length > 0;
    for (const kind of /** @type {const} */ (['due', 'open', 'resolved'])) {
      byId(`${kind}-section`).hidden = parts[kind].length === 0;
      lists[kind].replaceChildren(
        ...parts[kind].map((/** @type {LedgerGuess} */ g) => row(g, kind)),
      );
    }
    renderScore();
  }

  // -- the menu and the editor -----------------------------------------------

  /**
   * An istota-dialog whose content `build` fills; `build` gets `close`.
   * @param {string} label
   * @param {(close: () => void) => Node[]} build
   */
  function openPanel(label, build) {
    const dialog = /** @type {any} */ (el('istota-dialog'));
    dialog.setAttribute('label', label);
    const close = () => dialog.close(null);
    dialog.append(el('h2', 'istota-dialog-title', label), ...build(close));
    dialog.addEventListener('close', () => {
      dialog.remove();
      dialogOpen = false;
    });
    dialogOpen = true;
    document.body.append(dialog);
    dialog.show();
  }

  /**
   * @param {string} id
   * @param {boolean | null} outcome
   */
  function resolve(id, outcome) {
    apply(Model.resolveGuess(guesses, id, outcome, now()));
  }

  /** @param {string} id */
  function guessMenu(id) {
    const guess = guesses.find((g) => g.id === id);
    if (!guess) return;
    openPanel(guess.text || 'Guess', (close) => {
      /** @param {() => void} action */
      const then = (action) => () => {
        close();
        action();
      };
      const row = el('div', 'ledger-menu-row');
      if (guess.outcome === null) {
        row.append(
          kitButton(
            'Happened',
            'secondary',
            then(() => resolve(id, true)),
          ),
          kitButton(
            'Didn’t happen',
            'secondary',
            then(() => resolve(id, false)),
          ),
        );
      } else {
        row.append(
          kitButton(
            guess.outcome ? 'Mark as didn’t happen' : 'Mark as happened',
            'secondary',
            then(() => resolve(id, !guess.outcome)),
          ),
          kitButton(
            'Reopen',
            'secondary',
            then(() => resolve(id, null)),
          ),
        );
      }
      row.append(
        kitButton(
          'Edit',
          'secondary',
          then(() => editGuess(id)),
        ),
        kitButton(
          'Delete',
          'danger',
          then(() => deleteGuess(id)),
        ),
      );
      return [el('p', 'ledger-note', meta(guess)), row, kitButton('Close', 'primary', close)];
    });
  }

  /**
   * The add and edit form. It submits through istota-form's `istota-submit`,
   * which the kit raises from the button and from Enter in a field.
   * @param {string} [id] the guess to edit; none adds one
   * @param {LedgerGuess | null} [guess]
   */
  function editGuess(id = '', guess = guesses.find((g) => g.id === id) || null) {
    openPanel(guess ? 'Edit guess' : 'Add guess', (close) => {
      const wrapper = el('istota-form', 'ledger-editor');
      const form = /** @type {HTMLFormElement} */ (el('form'));

      /**
       * @param {string} label
       * @param {HTMLElement} control
       */
      const field = (label, control) => {
        const row = el('label');
        row.append(el('span', '', label), control);
        return row;
      };
      const text = /** @type {HTMLInputElement} */ (el('input'));
      text.name = 'text';
      text.required = true;
      text.value = guess ? guess.text : '';
      text.placeholder = 'What do you think will happen?';
      const p = /** @type {HTMLInputElement} */ (el('input'));
      p.name = 'p';
      p.type = 'number';
      p.min = '0';
      p.max = '100';
      p.step = 'any';
      p.inputMode = 'decimal';
      p.required = true;
      p.value = guess && guess.p !== null ? String(Math.round(guess.p * 1000) / 10) : '';
      const date = /** @type {HTMLInputElement} */ (el('input'));
      date.name = 'resolves_on';
      date.type = 'date';
      date.required = true;
      date.value = guess && guess.resolves_on ? guess.resolves_on : '';

      wrapper.addEventListener('istota-submit', (event) => {
        const values = /** @type {CustomEvent} */ (event).detail;
        const fields = {
          text: String(values.text || ''),
          // Always a percentage here, so 1 means 1% rather than certainty.
          p: Number(values.p) / 100,
          resolves_on: String(values.resolves_on || ''),
        };
        if (!fields.text.trim()) return;
        close();
        apply(
          guess
            ? Model.updateGuess(guesses, guess.id, fields)
            : Model.addGuess(guesses, fields, now(), Model.newId()),
        );
      });
      const actions = el('div', 'istota-dialog-actions');
      const save = el('istota-button', '', guess ? 'Save' : 'Add');
      save.setAttribute('variant', 'primary');
      save.setAttribute('type', 'submit');
      actions.append(kitButton('Cancel', 'secondary', close), save);
      form.append(
        field('Guess', text),
        field('Probability (%)', p),
        field('Resolves on', date),
        actions,
      );
      wrapper.append(form);
      return [wrapper];
    });
  }

  /** @param {string} id */
  async function deleteGuess(id) {
    const guess = guesses.find((g) => g.id === id);
    if (!guess) return;
    const sure = await istota.dialog.confirm(`Delete “${guess.text || 'Untitled guess'}”?`, {
      confirmLabel: 'Delete',
      danger: true,
    });
    if (!sure) return;
    guesses = Model.removeGuess(guesses, id);
    render();
    try {
      await istota.kv.delete(Model.guessKey(id));
    } catch (error) {
      failed(error);
    }
  }

  // -- wiring ----------------------------------------------------------------

  byId('add').addEventListener('click', () => editGuess());
  byId('refresh').addEventListener('click', load);
  // The agent may have logged a guess, or a date passed, while the page was hidden.
  let visibility = istota.env.visibility;
  istota.on('env', (/** @type {{visibility: string}} */ env) => {
    const returned = visibility === 'hidden' && env.visibility === 'visible';
    visibility = env.visibility;
    if (returned && !dialogOpen) load();
  });

  load();
})();
