// Board's page: draws the state board-model.js holds, and writes each change
// to `app.board` through the kit's `istota.kv` as it happens.
//
// A card moves by drag (a mouse drags from anywhere on the card once it
// moves, a touch or pen after a long press on the card's grip) or by its move
// menu, which works at every width and with a keyboard. On a phone the columns become one list behind a column picker,
// and a drag reorders within that list; the move menu changes column.
(() => {
  const istota = /** @type {any} */ (globalThis).istota;
  const Model = /** @type {any} */ (globalThis).BoardModel;
  const PHONE = window.matchMedia('(max-width: 640px)');
  const COLUMNS_KEY = 'columns';
  const PAGE = 1000;
  /** @type {Record<string, string>} */
  const COLOR_NAMES = { sky: 'Blue', coral: 'Coral', teal: 'Teal', plum: 'Plum', citron: 'Yellow' };

  const boardEl = /** @type {HTMLElement} */ (document.getElementById('board'));
  const emptyEl = /** @type {HTMLElement} */ (document.getElementById('empty'));
  const statusEl = /** @type {HTMLElement} */ (document.getElementById('status'));
  const picker = /** @type {HTMLSelectElement} */ (document.getElementById('column-picker'));
  const pickerLabel = /** @type {HTMLElement} */ (picker.closest('label'));

  /** @type {BoardState} */
  let state = { columns: [], cards: [] };
  let picked = '';
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

  /** @param {any[]} cards */
  async function writeCards(cards) {
    try {
      for (const card of cards) await istota.kv.set(Model.cardKey(card.id), card);
    } catch (error) {
      failed(error);
    }
  }

  async function writeColumns() {
    try {
      await istota.kv.set(COLUMNS_KEY, state.columns);
    } catch (error) {
      failed(error);
    }
  }

  /** @param {{state: any, changed: any[]}} result */
  function applyCards(result) {
    state = result.state;
    render();
    if (result.changed.length) writeCards(result.changed);
  }

  async function load() {
    say('');
    try {
      const columns = await istota.kv.get(COLUMNS_KEY);
      const items = [];
      for (let offset = 0; ;) {
        const page = await istota.kv.list({ prefix: 'card:', offset, limit: PAGE });
        items.push(...page.items);
        offset += page.items.length;
        if (!page.items.length || offset >= page.total) break;
      }
      state = Model.fromStorage(columns, items);
      loaded = true;
    } catch (error) {
      const err = /** @type {{message?: string}} */ (error || {});
      say(`Could not load the board: ${err.message || 'no answer'}.`);
    }
    boardEl.removeAttribute('aria-busy');
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
   * @param {string} label
   * @param {string} text
   * @param {() => void} onClick
   */
  function iconButton(label, text, onClick) {
    const button = /** @type {HTMLButtonElement} */ (el('button', 'board-icon-button', text));
    button.type = 'button';
    button.setAttribute('aria-label', label);
    button.title = label;
    button.addEventListener('click', onClick);
    return button;
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

  /** @param {any} card */
  function cardElement(card) {
    const item = el('li', `board-card${card.color ? ` board-card--${card.color}` : ''}`);
    item.dataset.id = card.id;
    if (card.struck) item.classList.add('is-struck');
    const open = /** @type {HTMLButtonElement} */ (el('button', 'board-card-open'));
    open.type = 'button';
    open.append(
      el('span', 'board-card-title', card.title),
      el('span', 'board-card-body', card.body),
    );
    open.addEventListener('click', () => {
      if (!suppressClick) editCard(card.id);
    });
    // The grip is where a touch drag starts. It takes no keyboard focus: the
    // move menu does everything a drag does.
    const grip = el('span', 'board-grip', '⠿');
    grip.setAttribute('aria-hidden', 'true');
    item.append(
      grip,
      open,
      iconButton(`Move or change ${card.title || 'card'}`, '⋯', () => cardMenu(card.id)),
    );
    item.addEventListener('pointerdown', onPointerDown);
    // A long press on a phone opens the context menu unless it is refused.
    item.addEventListener('contextmenu', (event) => {
      if (press || drag) event.preventDefault();
    });
    return item;
  }

  /** @param {any} column */
  function columnElement(column) {
    const section = el('section', 'board-column');
    section.dataset.column = column.id;
    const cards = Model.columnCards(state, column.id);
    const head = el('header', 'board-column-head');
    head.append(
      el('h2', 'board-column-title', column.title || 'Untitled'),
      el('span', 'board-count', String(cards.length)),
    );
    if (!column.synthetic) {
      head.append(
        iconButton(`Change column ${column.title || ''}`.trim(), '⋯', () => columnMenu(column.id)),
      );
    }
    const list = el('ul', 'board-cards');
    list.setAttribute('aria-label', column.title || 'Cards');
    for (const card of cards) list.append(cardElement(card));
    const add = kitButton('Add card', 'secondary', () => addCard(column.id));
    add.classList.add('board-add-card');
    section.append(head, list, add);
    return section;
  }

  function render() {
    const phone = PHONE.matches;
    document.body.classList.toggle('board--phone', phone);
    /** @type {BoardColumn[]} */
    const columns = Model.visibleColumns(state);
    emptyEl.hidden = !loaded || columns.length > 0;
    if (!columns.some((c) => c.id === picked)) picked = columns.length ? columns[0].id : '';

    picker.replaceChildren(
      ...columns.map((column) => {
        const option = /** @type {HTMLOptionElement} */ (el('option'));
        option.value = column.id;
        option.textContent = `${column.title || 'Untitled'} (${Model.columnCards(state, column.id).length})`;
        option.selected = column.id === picked;
        return option;
      }),
    );
    pickerLabel.hidden = !phone || columns.length === 0;

    const shown = phone ? columns.filter((c) => c.id === picked) : columns;
    boardEl.replaceChildren(...shown.map(columnElement));
  }

  // -- the move menu and the editors -----------------------------------------

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
   * @param {string} label
   * @param {Node[]} children
   */
  function section(label, children) {
    const box = el('div', 'board-menu-section');
    const row = el('div', 'board-menu-row');
    row.append(...children);
    box.append(el('p', 'board-menu-label', label), row);
    return box;
  }

  /** @param {string} id */
  function cardMenu(id) {
    const card = state.cards.find((c) => c.id === id);
    if (!card) return;
    /** @type {BoardColumn[]} */
    const columns = Model.visibleColumns(state);
    const home = columns.find((c) =>
      Model.columnCards(state, c.id).some((/** @type {BoardCard} */ x) => x.id === id),
    );
    if (!home) return;
    openPanel(card.title || 'Card', (close) => {
      /** @param {() => void} action */
      const then = (action) => () => {
        close();
        action();
      };
      const others = columns.filter((c) => c !== home);
      const nodes = [
        section('Position', [
          kitButton(
            'To top',
            'secondary',
            then(() => applyCards(Model.moveCard(state, id, home.id, 0, now()))),
          ),
          kitButton(
            'Up',
            'secondary',
            then(() => applyCards(Model.moveCardBy(state, id, -1, now()))),
          ),
          kitButton(
            'Down',
            'secondary',
            then(() => applyCards(Model.moveCardBy(state, id, 1, now()))),
          ),
          kitButton(
            'To bottom',
            'secondary',
            then(() =>
              applyCards(Model.moveCard(state, id, home.id, Number.MAX_SAFE_INTEGER, now())),
            ),
          ),
        ]),
      ];
      if (others.length) {
        nodes.push(
          section(
            'Move to column',
            others.map((column) =>
              kitButton(
                column.title || 'Untitled',
                'secondary',
                then(() =>
                  applyCards(Model.moveCard(state, id, column.id, Number.MAX_SAFE_INTEGER, now())),
                ),
              ),
            ),
          ),
        );
      }
      nodes.push(
        section('Card', [
          kitButton(
            card.struck ? 'Unstrike' : 'Strike out',
            'secondary',
            then(() => applyCards(Model.updateCard(state, id, { struck: !card.struck }, now()))),
          ),
          kitButton(
            'Edit',
            'secondary',
            then(() => editCard(id)),
          ),
          kitButton(
            'Delete',
            'danger',
            then(() => deleteCard(id)),
          ),
        ]),
        kitButton('Close', 'primary', close),
      );
      return nodes;
    });
  }

  /** @param {string} id */
  function editCard(id) {
    const card = state.cards.find((c) => c.id === id);
    if (!card) return;
    openPanel('Edit card', (close) => {
      const wrapper = el('istota-form', 'board-editor');
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
      const title = /** @type {HTMLInputElement} */ (el('input'));
      title.name = 'title';
      title.value = card.title;
      const body = /** @type {HTMLTextAreaElement} */ (el('textarea'));
      body.name = 'body';
      body.value = card.body;
      const color = /** @type {HTMLSelectElement} */ (el('select'));
      color.name = 'color';
      for (const [value, name] of [
        ['', 'None'],
        ...Model.COLORS.map((/** @type {string} */ c) => [c, COLOR_NAMES[c] || c]),
      ]) {
        const option = /** @type {HTMLOptionElement} */ (el('option', '', name));
        option.value = value;
        option.selected = (card.color || '') === value;
        color.append(option);
      }
      const struck = /** @type {HTMLInputElement} */ (el('input'));
      struck.type = 'checkbox';
      struck.name = 'struck';
      struck.checked = card.struck;
      const check = el('label', 'board-check');
      check.append(struck, el('span', '', 'Struck out'));

      // Saved from the button and from Enter in the title, never from the
      // form's submit: Chromium and Firefox fire no submit event in a frame
      // sandboxed without allow-forms, which an app's frame always is.
      const commit = () => {
        close();
        applyCards(
          Model.updateCard(
            state,
            id,
            { title: title.value, body: body.value, color: color.value, struck: struck.checked },
            now(),
          ),
        );
      };
      title.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter' || event.isComposing) return;
        event.preventDefault();
        commit();
      });
      const actions = el('div', 'istota-dialog-actions');
      actions.append(kitButton('Cancel', 'secondary', close), kitButton('Save', 'primary', commit));
      form.append(
        field('Title', title),
        field('Notes', body),
        field('Colour', color),
        check,
        actions,
      );
      wrapper.append(form);
      return [wrapper];
    });
  }

  /** @param {string} id */
  async function deleteCard(id) {
    const card = state.cards.find((c) => c.id === id);
    if (!card) return;
    const sure = await istota.dialog.confirm(`Delete “${card.title || 'Untitled'}”?`, {
      confirmLabel: 'Delete',
      danger: true,
    });
    if (!sure) return;
    state = Model.removeCard(state, id);
    render();
    try {
      await istota.kv.delete(Model.cardKey(id));
    } catch (error) {
      failed(error);
    }
  }

  /** @param {string} column */
  async function addCard(column) {
    const title = await istota.dialog.prompt('Card title', { title: 'Add card', okLabel: 'Add' });
    if (title === null || !title.trim()) return;
    applyCards(Model.addCard(state, { column, title }, now(), Model.newId()));
  }

  async function addColumn() {
    const title = await istota.dialog.prompt('Column name', {
      title: 'Add column',
      okLabel: 'Add',
    });
    if (title === null || !title.trim()) return;
    const id = Model.newId();
    state = Model.addColumn(state, title, id);
    picked = id;
    render();
    writeColumns();
  }

  /** @param {string} id */
  function columnMenu(id) {
    const column = state.columns.find((c) => c.id === id);
    if (!column) return;
    const count = Model.columnCards(state, id).length;
    openPanel(column.title || 'Column', (close) => {
      /** @param {(s: any) => any} change */
      const shift = (change) => () => {
        close();
        state = change(state);
        render();
        writeColumns();
      };
      const nodes = [
        section('Column', [
          kitButton('Rename', 'secondary', async () => {
            close();
            const title = await istota.dialog.prompt('Column name', {
              title: 'Rename column',
              value: column.title,
              okLabel: 'Rename',
            });
            if (title === null || !title.trim()) return;
            state = Model.renameColumn(state, id, title);
            render();
            writeColumns();
          }),
          kitButton(
            'Move left',
            'secondary',
            shift((s) => Model.shiftColumn(s, id, -1)),
          ),
          kitButton(
            'Move right',
            'secondary',
            shift((s) => Model.shiftColumn(s, id, 1)),
          ),
        ]),
      ];
      if (count) {
        nodes.push(
          el(
            'p',
            'board-menu-note',
            `Move its ${count} card${count === 1 ? '' : 's'} to another column before deleting it.`,
          ),
        );
      } else {
        nodes.push(
          section('Remove', [
            kitButton('Delete column', 'danger', () => {
              close();
              const next = Model.removeColumn(state, id);
              if (!next) return;
              state = next;
              render();
              writeColumns();
            }),
          ]),
        );
      }
      nodes.push(kitButton('Close', 'primary', close));
      return nodes;
    });
  }

  // -- dragging --------------------------------------------------------------

  /** @type {null | {id: string, item: HTMLElement, pointerId: number, pointerType: string, x: number, y: number, moved: number, start: number, timer: number}} */
  let press = null;
  /** @type {null | {id: string, item: HTMLElement, ghost: HTMLElement, placeholder: HTMLElement, dx: number, dy: number, column: string, index: number}} */
  let drag = null;
  let suppressClick = false;

  /** @param {PointerEvent} event */
  function onPointerDown(event) {
    if (event.button !== 0 || press || drag) return;
    const target = /** @type {HTMLElement} */ (event.target);
    if (target.closest('.board-icon-button')) return;
    // A touch anywhere but the grip is left to scroll the page. The grip has
    // touch-action: none, since a browser decides at touchstart whether a
    // touch may pan, and once it pans it cancels the pointer for good.
    if (event.pointerType !== 'mouse' && !target.closest('.board-grip')) return;
    const item = /** @type {HTMLElement} */ (event.currentTarget);
    press = {
      id: String(item.dataset.id),
      item,
      pointerId: event.pointerId,
      pointerType: event.pointerType,
      x: event.clientX,
      y: event.clientY,
      moved: 0,
      start: performance.now(),
      timer: 0,
    };
    if (event.pointerType !== 'mouse') {
      press.timer = window.setTimeout(
        () => decide(press ? press.x : 0, press ? press.y : 0),
        Model.LONG_PRESS_MS,
      );
    }
    document.addEventListener('pointermove', onPointerMove);
    document.addEventListener('pointerup', onPointerUp);
    document.addEventListener('pointercancel', onPointerCancel);
  }

  /**
   * @param {number} x
   * @param {number} y
   */
  function decide(x, y) {
    if (!press) return;
    const intent = Model.pressIntent({
      pointerType: press.pointerType,
      elapsed: performance.now() - press.start,
      moved: press.moved,
    });
    if (intent === 'cancel') endPress();
    else if (intent === 'drag') startDrag(x, y);
  }

  /** @param {PointerEvent} event */
  function onPointerMove(event) {
    if (drag) {
      event.preventDefault();
      placeGhost(event.clientX, event.clientY);
      track(event.clientX, event.clientY);
      return;
    }
    if (!press || event.pointerId !== press.pointerId) return;
    press.moved = Math.max(
      press.moved,
      Math.hypot(event.clientX - press.x, event.clientY - press.y),
    );
    decide(event.clientX, event.clientY);
  }

  /**
   * @param {number} x
   * @param {number} y
   */
  function startDrag(x, y) {
    if (!press) return;
    const { item, id, pointerId } = press;
    window.clearTimeout(press.timer);
    press = null;
    const rect = item.getBoundingClientRect();
    const ghost = /** @type {HTMLElement} */ (item.cloneNode(true));
    ghost.classList.add('board-ghost');
    ghost.style.width = `${rect.width}px`;
    ghost.setAttribute('aria-hidden', 'true');
    const placeholder = el('li', 'board-placeholder');
    placeholder.setAttribute('aria-hidden', 'true');
    placeholder.style.height = `${rect.height}px`;
    item.classList.add('is-dragging');
    item.after(placeholder);
    document.body.append(ghost);
    const column = String(item.closest('.board-column')?.getAttribute('data-column') || '');
    drag = { id, item, ghost, placeholder, dx: x - rect.left, dy: y - rect.top, column, index: 0 };
    try {
      item.setPointerCapture(pointerId);
    } catch {
      // The pointer may already be gone; the document listeners still end the drag.
    }
    placeGhost(x, y);
    track(x, y);
  }

  /**
   * @param {number} x
   * @param {number} y
   */
  function placeGhost(x, y) {
    if (!drag) return;
    drag.ghost.style.left = `${x - drag.dx}px`;
    drag.ghost.style.top = `${y - drag.dy}px`;
  }

  /**
   * Follow the pointer: which column it is over, and where among that
   * column's cards the placeholder goes.
   * @param {number} x
   * @param {number} y
   */
  function track(x, y) {
    if (!drag) return;
    const under = document.elementFromPoint(x, y);
    const columnEl = under ? under.closest('.board-column') : null;
    const target = /** @type {HTMLElement | null} */ (
      columnEl || boardEl.querySelector(`.board-column[data-column="${CSS.escape(drag.column)}"]`)
    );
    if (!target) return;
    const list = /** @type {HTMLElement} */ (target.querySelector('.board-cards'));
    const others = [...list.querySelectorAll('.board-card')].filter((node) => node !== drag?.item);
    const midpoints = others.map((node) => {
      const r = node.getBoundingClientRect();
      return r.top + r.height / 2;
    });
    const index = Model.dropIndex(midpoints, y);
    drag.column = String(target.dataset.column);
    drag.index = index;
    const before = others[index];
    if (before) before.before(drag.placeholder);
    else list.append(drag.placeholder);
  }

  /** @param {PointerEvent} event */
  function onPointerUp(event) {
    if (drag) {
      const { id, column, index } = drag;
      endDrag();
      suppressClick = true;
      window.setTimeout(() => {
        suppressClick = false;
      }, 0);
      applyCards(Model.moveCard(state, id, column, index, now()));
      event.preventDefault();
      return;
    }
    endPress();
  }

  function onPointerCancel() {
    if (drag) {
      endDrag();
      render();
    }
    endPress();
  }

  function endPress() {
    if (press) window.clearTimeout(press.timer);
    press = null;
    if (!drag) removeListeners();
  }

  function endDrag() {
    if (!drag) return;
    drag.ghost.remove();
    drag.placeholder.remove();
    drag.item.classList.remove('is-dragging');
    drag = null;
    removeListeners();
  }

  function removeListeners() {
    document.removeEventListener('pointermove', onPointerMove);
    document.removeEventListener('pointerup', onPointerUp);
    document.removeEventListener('pointercancel', onPointerCancel);
  }

  // Once a long press has become a drag the page must not scroll under it.
  // A non-passive touchmove is the one listener a browser lets cancel that.
  document.addEventListener(
    'touchmove',
    (event) => {
      if (drag) event.preventDefault();
    },
    { passive: false },
  );

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && drag) {
      endDrag();
      render();
    }
  });

  // -- wiring ----------------------------------------------------------------

  picker.addEventListener('change', () => {
    picked = picker.value;
    render();
  });
  document.getElementById('add-column')?.addEventListener('click', addColumn);
  document.getElementById('refresh')?.addEventListener('click', load);
  PHONE.addEventListener('change', () => {
    if (!drag) render();
  });
  // The agent may have changed the board while the page was hidden.
  let visibility = istota.env.visibility;
  istota.on('env', (/** @type {{visibility: string}} */ env) => {
    const returned = visibility === 'hidden' && env.visibility === 'visible';
    visibility = env.visibility;
    if (returned && !drag && !dialogOpen) load();
  });

  load();
})();
